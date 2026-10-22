"""Durable decision telemetry for scans and screener cycles.

Decision records are deliberately separate from order history.  A blocked
signal, a shadow-only signal, and a demo execution are all useful outcomes for
the UI and for later review.  Writes use a process-local lock plus atomic
replacement so concurrent async scans cannot leave a partially written JSON
file behind.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .events import EventBus, event


DEFAULT_MAX_RECORDS = 1000
_EXECUTION_MODES = {"LIVE", "SHADOW", "BLOCKED"}
_LAYER_NAMES = (
    "step1_talib",
    "step2_kronos_matrix",
    "step3_l2_safety",
    "step4_gemini_cio",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, default: str = "") -> str:
    return str(value).strip() if value is not None else default


class TradeDecisionLogger:
    """Append-only bounded decision history with optional EventBus fan-out."""

    def __init__(
        self,
        path: str | Path | None = None,
        bus: EventBus | None = None,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self.path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / "logs" / "decision_history.json"
        self.bus = bus
        try:
            parsed_max = int(max_records)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_records must be a positive integer") from exc
        if parsed_max < 1:
            raise ValueError("max_records must be a positive integer")
        self.max_records = parsed_max
        self._file_lock = threading.RLock()

    @staticmethod
    def _layer_reason(layer: Mapping[str, Any]) -> str:
        for key in ("reason", "veto_reason", "rationale", "message"):
            value = layer.get(key)
            if value not in (None, ""):
                return _text(value)
        for key in ("evidence_reasons", "hard_blocks", "blocks"):
            value = layer.get(key)
            if isinstance(value, (list, tuple)) and value:
                return "; ".join(_text(item) for item in value if _text(item))
        return ""

    @classmethod
    def extract_waterfall_layers(cls, waterfall: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        """Return stable layer snapshots with explicit status and reason fields."""

        source = waterfall if isinstance(waterfall, Mapping) else {}
        steps = source.get("steps") if isinstance(source.get("steps"), Mapping) else source
        layers: dict[str, dict[str, Any]] = {}
        for name in _LAYER_NAMES:
            raw = steps.get(name) if isinstance(steps, Mapping) else None
            layer = dict(raw) if isinstance(raw, Mapping) else {}
            status = _text(layer.get("status")).upper()
            if status not in {"PASS", "BLOCKED", "BYPASSED", "ERROR", "UNKNOWN"}:
                if layer.get("enabled") is False:
                    status = "BYPASSED"
                elif name == "step1_talib":
                    status = "PASS" if bool(layer.get("passed")) and not bool(layer.get("veto_buy") or layer.get("veto_sell")) else "BLOCKED"
                elif name == "step2_kronos_matrix":
                    status = "PASS" if bool(layer.get("components_agree")) else "BLOCKED"
                elif name == "step3_l2_safety":
                    status = "BLOCKED" if bool(layer.get("vetoed")) else "PASS"
                elif name == "step4_gemini_cio":
                    status = "PASS" if _text(layer.get("final_action")).upper() in {"BUY", "SELL"} else "BLOCKED"
                else:
                    status = "UNKNOWN"
            reason = cls._layer_reason(layer)
            if not reason:
                if status == "BYPASSED":
                    reason = "layer disabled by runtime controls"
                elif status == "BLOCKED":
                    reason = "layer blocked the candidate"
                elif status == "PASS":
                    reason = "layer passed"
            layer["status"] = status
            layer["reason"] = reason
            layers[name] = layer
        return layers

    @classmethod
    def build_record(
        cls,
        *,
        symbol: str,
        signal: str | None = None,
        action: str | None = None,
        final_decision: str | None = None,
        execution_mode: str = "BLOCKED",
        reason: str = "",
        block_reason: str | None = None,
        scan_id: str | None = None,
        cycle: str | None = None,
        waterfall: Mapping[str, Any] | None = None,
        waterfall_layers: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Build a JSON-safe record without touching disk or the event bus."""

        normalized_mode = _text(execution_mode, "BLOCKED").upper().replace("_AUTO", "")
        if normalized_mode not in _EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of: {', '.join(sorted(_EXECUTION_MODES))}")
        normalized_symbol = _text(symbol, "UNKNOWN").upper() or "UNKNOWN"
        normalized_action = _text(action or final_decision or "ABSTAIN").upper() or "ABSTAIN"
        normalized_final = _text(final_decision or normalized_action, "ABSTAIN").upper() or "ABSTAIN"
        normalized_signal = _text(signal or normalized_action, "ABSTAIN").upper() or "ABSTAIN"
        layers = (
            cls.extract_waterfall_layers({"steps": waterfall_layers})
            if isinstance(waterfall_layers, Mapping)
            else cls.extract_waterfall_layers(waterfall)
        )
        for name in _LAYER_NAMES:
            layers.setdefault(name, {"status": "UNKNOWN", "reason": "layer telemetry unavailable"})
        human_reason = _text(reason)
        if not human_reason and isinstance(waterfall, Mapping):
            human_reason = _text(waterfall.get("rationale_ka") or waterfall.get("reason"))
        if not human_reason:
            human_reason = "decision recorded without a human-readable rationale"
        normalized_block_reason = _text(block_reason)
        if normalized_mode == "BLOCKED" and not normalized_block_reason:
            normalized_block_reason = human_reason
        return {
            "record_id": str(uuid.uuid4()),
            "timestamp": timestamp or _utc_now_iso(),
            "scan_id": _text(scan_id),
            "cycle": _text(cycle or scan_id),
            "symbol": normalized_symbol,
            "signal": normalized_signal,
            "action": normalized_action,
            "waterfall_layers": layers,
            "waterfall_layer_statuses": {name: _text(layer.get("status"), "UNKNOWN") for name, layer in layers.items()},
            "waterfall_layer_reasons": {name: _text(layer.get("reason")) for name, layer in layers.items()},
            "final_decision": normalized_final,
            "execution_mode": normalized_mode,
            "reason": human_reason,
            "block_reason": normalized_block_reason,
            "metadata": dict(metadata or {}),
        }

    def _read_unlocked(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            raw = raw["records"]
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, Mapping)][-self.max_records :]

    def _write_unlocked(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(records[-self.max_records :], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def append_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one already-built record and return the normalized copy."""

        normalized = dict(record)
        with self._file_lock:
            records = self._read_unlocked()
            records.append(normalized)
            self._write_unlocked(records)
        return normalized

    def read_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Read newest records first; malformed files fail closed to an empty list."""

        with self._file_lock:
            records = self._read_unlocked()
        newest = list(reversed(records))
        if limit is None:
            return newest
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            return []
        if requested <= 0:
            return []
        return newest[:requested]

    def summary(self) -> dict[str, Any]:
        with self._file_lock:
            records = self._read_unlocked()
        final_decisions = Counter(_text(item.get("final_decision"), "UNKNOWN").upper() for item in records)
        execution_modes = Counter(_text(item.get("execution_mode"), "UNKNOWN").upper() for item in records)
        block_reasons = Counter(_text(item.get("block_reason"), "none") or "none" for item in records)
        return {
            "total": len(records),
            "final_decision": dict(sorted(final_decisions.items())),
            "execution_mode": dict(sorted(execution_modes.items())),
            "block_reason": dict(sorted(block_reasons.items())),
        }

    async def record_decision(self, **kwargs: Any) -> dict[str, Any]:
        """Build, persist, and publish one decision record."""

        record = self.build_record(**kwargs)
        self.append_record(record)
        if self.bus is not None:
            message = event(
                "trade.decision",
                scan_id=record.get("scan_id") or None,
                cycle=record.get("cycle") or None,
                symbol=record["symbol"],
                signal=record["signal"],
                action=record["action"],
                final_decision=record["final_decision"],
                execution_mode=record["execution_mode"],
                reason=record["reason"],
                block_reason=record["block_reason"],
                waterfall_layers=record["waterfall_layers"],
                record=record,
            )
            published = self.bus.publish(message)
            if inspect.isawaitable(published):
                await published
        return record

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        """Alias suitable for callers that prefer a short method name."""

        return await self.record_decision(**kwargs)


TradeLogger = TradeDecisionLogger

__all__ = ["DEFAULT_MAX_RECORDS", "TradeDecisionLogger", "TradeLogger"]
