"""Research-only strategy profiles, deterministic backtests, and safe loaders.

The Bot Strategy Lab deliberately stops at research.  Profiles are JSON
documents, visual rules are evaluated against closed OHLCV bars, and Python
strategies are loaded only from files inside the New Era workspace.  Nothing
in this module exposes a broker, order, account, or real-time execution
capability.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable


STRATEGY_SCHEMA_VERSION = 1
DEFAULT_PROFILE_DIR = Path("TradingAgentsData") / "strategies"
STRATEGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
CLASS_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SUPPORTED_INDICATORS = frozenset(
    {
        "sma",
        "ema",
        "rsi",
        "atr",
        "momentum",
        "highest",
        "lowest",
        "stddev",
        "change",
        "returns",
    }
)
SUPPORTED_CONDITIONS = frozenset(
    {
        ">",
        ">=",
        "<",
        "<=",
        "==",
        "!=",
        "crosses_above",
        "crosses_below",
        "between",
        "outside",
        "rising",
        "falling",
        "and",
        "or",
        "not",
    }
)
SUPPORTED_ACTIONS = frozenset(
    {
        "enter_long",
        "enter_short",
        "exit",
        "exit_long",
        "exit_short",
        "close",
        "hold",
        "reverse",
    }
)
PRICE_FIELDS = frozenset({"timestamp", "time", "date", "open", "high", "low", "close", "volume"})


@runtime_checkable
class ResearchStrategyProtocol(Protocol):
    """Protocol required by workspace-local Python strategy modules.

    Implementations must be constructible without arguments and expose
    ``on_bar(bar, context)``.  The return value may be an action string, an
    action mapping, or a list of action mappings.  The action vocabulary is
    the same research-only vocabulary used by visual profiles.
    """

    def on_bar(self, bar: Mapping[str, Any], context: Mapping[str, Any]) -> Any:
        ...


@dataclass(frozen=True)
class LoadedStrategy:
    """Validated class instance and its workspace-local source path."""

    module_path: Path
    module_name: str
    class_name: str
    strategy_class: type[Any]
    instance: ResearchStrategyProtocol

    def metadata(self) -> dict[str, Any]:
        return {
            "module_path": self.module_path.as_posix(),
            "module_name": self.module_name,
            "class_name": self.class_name,
            "protocol": "on_bar(bar, context)",
            "research_only": True,
            "real_account_allowed": False,
            "order_execution_allowed": False,
        }


def _finite(value: Any, *, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _clean_text(value: Any, *, name: str, max_length: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    if len(text) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return text


def _normalize_id(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace(" ", "-")
    if not STRATEGY_ID_PATTERN.fullmatch(candidate):
        raise ValueError("strategy id must be 2-64 characters using letters, numbers, '.', '_' or '-'")
    return candidate


def _slug_from_name(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    candidate = candidate[:64]
    if len(candidate) < 2:
        raise ValueError("strategy name must produce a valid strategy id")
    return _normalize_id(candidate)


def _json_clone(value: Any) -> Any:
    """Return a JSON-safe copy and reject NaN/Infinity before persistence."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy profile must contain JSON-compatible values") from exc


def _assert_research_only(raw: Mapping[str, Any]) -> None:
    """Reject attempts to turn a research profile into an execution profile."""

    safety_values: list[tuple[str, Any]] = []
    for container_name in ("safety", "permissions", "execution"):
        container = raw.get(container_name)
        if isinstance(container, Mapping):
            for key in (
                "real_account_allowed",
                "order_execution_allowed",
                "direct_python_orders",
                "live_trading",
                "can_trade",
                "place_orders",
            ):
                if key in container:
                    safety_values.append((f"{container_name}.{key}", container[key]))
    for key in (
        "real_account_allowed",
        "order_execution_allowed",
        "direct_python_orders",
        "live_trading",
        "can_trade",
        "place_orders",
    ):
        if key in raw:
            safety_values.append((key, raw[key]))
    for key, value in safety_values:
        if value not in (False, None, 0, "false", "False"):
            raise ValueError(f"{key} must remain false for a research-only strategy")


def _normalize_indicator_specs(flow: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_indicators = flow.get("indicators", [])
    if raw_indicators is None:
        raw_indicators = []
    if not isinstance(raw_indicators, list) or len(raw_indicators) > 200:
        raise ValueError("flow.indicators must be a list of at most 200 items")
    indicators: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_indicators:
        if not isinstance(raw, Mapping):
            raise ValueError("each indicator must be an object")
        indicator_id = _clean_text(raw.get("id"), name="indicator.id", max_length=64, required=True)
        if not CLASS_NAME_PATTERN.fullmatch(indicator_id.replace("-", "_")):
            raise ValueError(f"invalid indicator id: {indicator_id}")
        if indicator_id in ids or indicator_id in PRICE_FIELDS:
            raise ValueError(f"duplicate or reserved indicator id: {indicator_id}")
        ids.add(indicator_id)
        kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
        if kind not in SUPPORTED_INDICATORS:
            raise ValueError(f"unsupported indicator type: {kind or '<empty>'}")
        period_raw = raw.get("period", raw.get("window", 14))
        try:
            period = int(period_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"indicator {indicator_id} period must be an integer") from exc
        if period < 1 or period > 2000:
            raise ValueError(f"indicator {indicator_id} period must be between 1 and 2000")
        source = raw.get("source", "close")
        if isinstance(source, str):
            source = source.strip().lower()
            if not source:
                source = "close"
        else:
            raise ValueError(f"indicator {indicator_id} source must be a string")
        multiplier = raw.get("multiplier", 1.0)
        multiplier = _finite(multiplier, name=f"indicator {indicator_id} multiplier")
        indicators.append(
            {
                "id": indicator_id,
                "type": kind,
                "source": source,
                "period": period,
                "multiplier": multiplier,
            }
        )
    known_sources = set(PRICE_FIELDS) | ids
    for indicator in indicators:
        if indicator["source"] not in known_sources:
            raise ValueError(f"indicator {indicator['id']} references unknown source: {indicator['source']}")
    return indicators


def _normalize_condition_specs(flow: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_conditions = flow.get("conditions", [])
    if raw_conditions is None:
        raw_conditions = []
    if not isinstance(raw_conditions, list) or len(raw_conditions) > 300:
        raise ValueError("flow.conditions must be a list of at most 300 items")
    conditions: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_conditions:
        if not isinstance(raw, Mapping):
            raise ValueError("each condition must be an object")
        condition_id = _clean_text(raw.get("id"), name="condition.id", max_length=64, required=True)
        if not CLASS_NAME_PATTERN.fullmatch(condition_id.replace("-", "_")):
            raise ValueError(f"invalid condition id: {condition_id}")
        if condition_id in ids:
            raise ValueError(f"duplicate condition id: {condition_id}")
        ids.add(condition_id)
        operator = str(raw.get("operator") or raw.get("op") or "").strip().lower()
        if operator not in SUPPORTED_CONDITIONS:
            raise ValueError(f"unsupported condition operator: {operator or '<empty>'}")
        item: dict[str, Any] = {"id": condition_id, "operator": operator}
        if operator in {"and", "or", "not"}:
            operands = raw.get("conditions", raw.get("operands", raw.get("refs", [])))
            if isinstance(operands, str):
                operands = [operands]
            if not isinstance(operands, list) or not operands:
                raise ValueError(f"condition {condition_id} needs a non-empty conditions list")
            normalized_operands = [_clean_text(value, name="condition reference", max_length=64, required=True) for value in operands]
            item["conditions"] = normalized_operands
        else:
            left = raw.get("left", raw.get("lhs"))
            right = raw.get("right", raw.get("rhs"))
            if left is None and operator not in {"rising", "falling"}:
                raise ValueError(f"condition {condition_id} needs a left operand")
            if operator not in {"rising", "falling"} and right is None:
                raise ValueError(f"condition {condition_id} needs a right operand")
            if left is not None:
                item["left"] = _json_clone(left)
            if right is not None:
                item["right"] = _json_clone(right)
        conditions.append(item)
    known_ids = {item["id"] for item in conditions}
    for condition in conditions:
        for ref in condition.get("conditions", []):
            if ref not in known_ids:
                raise ValueError(f"condition {condition['id']} references unknown condition: {ref}")
    return conditions


def _normalize_action_specs(flow: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_actions = flow.get("actions", [])
    if raw_actions is None:
        raw_actions = []
    if not isinstance(raw_actions, list) or len(raw_actions) > 300:
        raise ValueError("flow.actions must be a list of at most 300 items")
    actions: list[dict[str, Any]] = []
    for raw in raw_actions:
        if isinstance(raw, str):
            raw = {"type": raw}
        if not isinstance(raw, Mapping):
            raise ValueError("each action must be a string or object")
        action_type = str(raw.get("type") or raw.get("action") or "").strip().lower()
        aliases = {"buy": "enter_long", "sell": "enter_short", "close_all": "exit", "flat": "exit"}
        action_type = aliases.get(action_type, action_type)
        if action_type not in SUPPORTED_ACTIONS:
            raise ValueError(f"unsupported action type: {action_type or '<empty>'}")
        item: dict[str, Any] = {"type": action_type}
        when = raw.get("when", raw.get("condition"))
        if when is not None:
            if isinstance(when, str):
                item["when"] = _clean_text(when, name="action.when", max_length=64, required=True)
            elif isinstance(when, list):
                item["when"] = [_clean_text(value, name="action.when", max_length=64, required=True) for value in when]
            else:
                raise ValueError("action.when must be a condition id or list of ids")
        if bool(raw.get("any", False)):
            item["any"] = True
        if "size" in raw or "quantity" in raw:
            size = _finite(raw.get("size", raw.get("quantity")), name="action size")
            if size <= 0 or size > 1_000_000:
                raise ValueError("action size must be greater than 0 and at most 1000000")
            item["size"] = size
        actions.append(item)
    return actions


def _normalize_flow(raw_flow: Any) -> dict[str, Any]:
    if raw_flow is None:
        raw_flow = {}
    if not isinstance(raw_flow, Mapping):
        raise ValueError("flow must be an object")
    indicators = _normalize_indicator_specs(raw_flow)
    conditions = _normalize_condition_specs(raw_flow)
    actions = _normalize_action_specs(raw_flow)
    settings = raw_flow.get("settings", raw_flow.get("position", {}))
    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping):
        raise ValueError("flow.settings must be an object")
    allow_short = bool(settings.get("allow_short", True))
    default_size = _finite(settings.get("default_size", 1.0), name="flow.settings.default_size")
    if default_size <= 0:
        raise ValueError("flow.settings.default_size must be greater than 0")
    return {
        "indicators": indicators,
        "conditions": conditions,
        "actions": actions,
        "settings": {"allow_short": allow_short, "default_size": default_size},
    }


def _local_resolver(root: Path, value: str | Path | None, *, default: Path | None = None) -> Path:
    candidate = Path(value) if value else (default or root)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path must stay inside the NewEraTradingBot workspace: {value}") from exc
    return resolved


def normalize_strategy_profile(
    profile: Mapping[str, Any],
    *,
    workspace_root: Path | None = None,
    resolve_workspace_path: Callable[..., Path] | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """Normalize and validate a visual or code profile without enabling execution."""

    if not isinstance(profile, Mapping):
        raise ValueError("strategy profile must be an object")
    raw = _json_clone(dict(profile))
    _assert_research_only(raw)
    name = _clean_text(raw.get("name", raw.get("display_name")), name="name", max_length=120, required=True)
    requested_id = raw.get("id", raw.get("strategy_id", strategy_id))
    normalized_id = _normalize_id(requested_id) if requested_id else _slug_from_name(name)
    builder = str(raw.get("builder", raw.get("builder_type", raw.get("mode", "visual")))).strip().lower()
    if builder in {"rules", "rule", "visual_rules"}:
        builder = "visual"
    if builder not in {"visual", "code"}:
        raise ValueError("builder must be 'visual' or 'code'")
    symbol = _clean_text(raw.get("symbol", "XAUUSD"), name="symbol", max_length=32, required=True).upper()
    if any(character not in set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-") for character in symbol):
        raise ValueError("symbol contains unsupported characters")
    timeframe = _clean_text(raw.get("timeframe", "M5"), name="timeframe", max_length=20, required=True).upper()
    description = _clean_text(raw.get("description", ""), name="description", max_length=2000)
    flow_raw = raw.get("flow", raw.get("rules", {}))
    flow = _normalize_flow(flow_raw)

    code: dict[str, Any] | None = None
    if builder == "code":
        raw_code = raw.get("code")
        if raw_code is None:
            raw_code = {}
        if not isinstance(raw_code, Mapping):
            raise ValueError("code must be an object")
        module_path = raw_code.get("module_path", raw_code.get("path", raw.get("module_path")))
        class_name = raw_code.get("class_name", raw_code.get("class", raw.get("class_name")))
        module_path = _clean_text(module_path, name="code.module_path", max_length=400, required=True)
        class_name = _clean_text(class_name, name="code.class_name", max_length=128, required=True)
        if not CLASS_NAME_PATTERN.fullmatch(class_name):
            raise ValueError("code.class_name must be a simple Python class name")
        resolver = resolve_workspace_path
        root = (workspace_root or Path.cwd()).resolve()
        resolved = resolver(module_path) if resolver else _local_resolver(root, module_path)
        resolved = Path(resolved).resolve()
        if resolved.suffix.lower() != ".py":
            raise ValueError("code.module_path must reference a .py module")
        try:
            relative_module = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("code.module_path must stay inside the NewEraTradingBot workspace") from exc
        code = {"module_path": relative_module, "class_name": class_name}

    metadata = raw.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    normalized = {
        "schema_version": STRATEGY_SCHEMA_VERSION,
        "id": normalized_id,
        "name": name,
        "builder": builder,
        "symbol": symbol,
        "timeframe": timeframe,
        "description": description,
        "flow": flow,
        "code": code,
        "metadata": _json_clone(dict(metadata)),
        "status": "research_only",
        "safety": {
            "research_only": True,
            "real_account_allowed": False,
            "order_execution_allowed": False,
            "direct_python_orders": False,
        },
    }
    return normalized


def _bar_value(row: Mapping[str, Any], field: str, default: Any = None) -> Any:
    if field in row:
        return row[field]
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    return lowered.get(field.lower(), default)


def normalize_ohlcv_bar(row: Mapping[str, Any] | Sequence[Any], *, index: int = 0) -> dict[str, Any]:
    """Normalize one OHLCV mapping or ``timestamp, open, high, low, close, volume`` row."""

    if isinstance(row, Mapping):
        timestamp = _bar_value(row, "timestamp", _bar_value(row, "time", _bar_value(row, "date", index)))
        open_value = _bar_value(row, "open")
        high_value = _bar_value(row, "high")
        low_value = _bar_value(row, "low")
        close_value = _bar_value(row, "close")
        volume_value = _bar_value(row, "volume", 0.0)
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        if len(row) < 5:
            raise ValueError("OHLCV sequence needs at least timestamp, open, high, low and close")
        timestamp = row[0]
        open_value, high_value, low_value, close_value = row[1:5]
        volume_value = row[5] if len(row) > 5 else 0.0
    else:
        raise ValueError("each OHLCV bar must be an object or sequence")
    values = {
        "open": _finite(open_value, name="open"),
        "high": _finite(high_value, name="high"),
        "low": _finite(low_value, name="low"),
        "close": _finite(close_value, name="close"),
        "volume": _finite(volume_value, name="volume"),
    }
    if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
        raise ValueError("OHLCV prices must be greater than 0")
    if values["high"] < values["low"]:
        raise ValueError("OHLCV high cannot be below low")
    values["timestamp"] = index if timestamp is None else timestamp
    return values


def normalize_ohlcv(rows: Iterable[Mapping[str, Any] | Sequence[Any]]) -> list[dict[str, Any]]:
    bars = [normalize_ohlcv_bar(row, index=index) for index, row in enumerate(rows)]
    if len(bars) < 2:
        raise ValueError("at least two OHLCV bars are required")
    return bars


def load_ohlcv_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("OHLCV CSV must include a header row")
            return normalize_ohlcv(list(reader))
    except OSError as exc:
        raise FileNotFoundError(f"cannot read OHLCV data: {path}") from exc


def _source_series(source: str, bars: list[dict[str, Any]], series: Mapping[str, list[float | None]]) -> list[float | None]:
    if source in series:
        return list(series[source])
    if source in PRICE_FIELDS:
        return [float(bar.get(source, 0.0)) if source not in {"timestamp", "time", "date"} else None for bar in bars]
    raise ValueError(f"unknown indicator source: {source}")


def _valid_window(values: Sequence[float | None], index: int, period: int) -> list[float] | None:
    start = index - period + 1
    if start < 0:
        return None
    window = values[start : index + 1]
    if len(window) != period or any(value is None for value in window):
        return None
    numeric_window: list[float] = []
    for value in window:
        if value is None:
            return None
        numeric_window.append(float(value))
    return numeric_window


def _indicator_series(indicator: Mapping[str, Any], bars: list[dict[str, Any]], series: dict[str, list[float | None]]) -> list[float | None]:
    kind = str(indicator["type"])
    source_values = _source_series(str(indicator["source"]), bars, series)
    period = int(indicator["period"])
    multiplier = float(indicator.get("multiplier", 1.0))
    result: list[float | None] = [None] * len(bars)
    if kind == "change":
        for index in range(1, len(bars)):
            current_value = source_values[index]
            previous_value = source_values[index - 1]
            if current_value is not None and previous_value is not None:
                result[index] = (float(current_value) - float(previous_value)) * multiplier
        return result
    if kind == "returns":
        for index in range(1, len(bars)):
            previous = source_values[index - 1]
            current = source_values[index]
            if previous not in (None, 0) and current is not None:
                result[index] = ((float(current) / float(previous)) - 1.0) * 100.0 * multiplier
        return result
    if kind == "momentum":
        for index in range(period, len(bars)):
            previous = source_values[index - period]
            current = source_values[index]
            if previous is not None and current is not None:
                result[index] = (float(current) - float(previous)) * multiplier
        return result
    if kind in {"highest", "lowest", "stddev", "sma"}:
        for index in range(len(bars)):
            window = _valid_window(source_values, index, period)
            if window is None:
                continue
            if kind == "highest":
                result[index] = max(window) * multiplier
            elif kind == "lowest":
                result[index] = min(window) * multiplier
            elif kind == "stddev":
                mean = sum(window) / period
                result[index] = math.sqrt(sum((value - mean) ** 2 for value in window) / period) * multiplier
            else:
                result[index] = (sum(window) / period) * multiplier
        return result
    if kind == "ema":
        seed = _valid_window(source_values, period - 1, period)
        if seed is None:
            return result
        ema = sum(seed) / period
        result[period - 1] = ema * multiplier
        alpha = 2.0 / (period + 1.0)
        for index in range(period, len(bars)):
            current = source_values[index]
            if current is None:
                ema = math.nan
                result[index] = None
            elif not math.isfinite(ema):
                ema = float(current)
                result[index] = ema * multiplier
            else:
                ema = ((float(current) - ema) * alpha) + ema
                result[index] = ema * multiplier
        return result
    if kind == "rsi":
        for index in range(period, len(bars)):
            window_start = index - period
            changes: list[float] = []
            valid = True
            for offset in range(window_start + 1, index + 1):
                current = source_values[offset]
                previous = source_values[offset - 1]
                if current is None or previous is None:
                    valid = False
                    break
                changes.append(float(current) - float(previous))
            if not valid or len(changes) != period:
                continue
            gains = sum(max(change, 0.0) for change in changes) / period
            losses = sum(max(-change, 0.0) for change in changes) / period
            if losses == 0.0:
                result[index] = 100.0
            else:
                result[index] = (100.0 - (100.0 / (1.0 + gains / losses))) * multiplier
        return result
    if kind == "atr":
        true_ranges: list[float | None] = [None] * len(bars)
        for index, bar in enumerate(bars):
            if index == 0:
                true_ranges[index] = bar["high"] - bar["low"]
                continue
            previous_close = bars[index - 1]["close"]
            true_ranges[index] = max(
                bar["high"] - bar["low"],
                abs(bar["high"] - previous_close),
                abs(bar["low"] - previous_close),
            )
        for index in range(len(bars)):
            window = _valid_window(true_ranges, index, period)
            if window is not None:
                result[index] = (sum(window) / period) * multiplier
        return result
    raise ValueError(f"unsupported indicator type: {kind}")


def calculate_indicators(profile: Mapping[str, Any], bars: list[dict[str, Any]]) -> dict[str, list[float | None]]:
    flow = profile.get("flow", {})
    series: dict[str, list[float | None]] = {}
    for indicator in flow.get("indicators", []):
        indicator_id = str(indicator["id"])
        if indicator_id in series:
            raise ValueError(f"duplicate indicator id: {indicator_id}")
        series[indicator_id] = _indicator_series(indicator, bars, series)
    return series


def _shifted(values: list[float | None], index: int, offset: int) -> float | None:
    target = index - offset
    if target < 0 or target >= len(values):
        return None
    return values[target]


def _operand_series(operand: Any, bars: list[dict[str, Any]], indicators: Mapping[str, list[float | None]]) -> list[float | None]:
    if isinstance(operand, Mapping):
        offset = int(operand.get("offset", 0) or 0)
        if "indicator" in operand:
            name = str(operand["indicator"])
            if name not in indicators:
                raise ValueError(f"unknown indicator operand: {name}")
            source = indicators[name]
        elif "field" in operand:
            field = str(operand["field"]).strip().lower()
            if field not in PRICE_FIELDS or field in {"timestamp", "time", "date"}:
                raise ValueError(f"unknown OHLCV operand field: {field}")
            source = [float(bar[field]) for bar in bars]
        elif "value" in operand or "constant" in operand:
            value = _finite(operand.get("value", operand.get("constant")), name="condition constant")
            return [value] * len(bars)
        else:
            raise ValueError("operand must reference an indicator, OHLCV field, or numeric value")
        if offset:
            return [_shifted(list(source), index, offset) for index in range(len(bars))]
        return list(source)
    if isinstance(operand, str):
        name = operand.strip().lower()
        if name in indicators:
            return list(indicators[name])
        if name in PRICE_FIELDS and name not in {"timestamp", "time", "date"}:
            return [float(bar[name]) for bar in bars]
        try:
            value = _finite(operand, name="condition constant")
        except ValueError as exc:
            raise ValueError(f"unknown condition operand: {operand}") from exc
        return [value] * len(bars)
    if isinstance(operand, bool):
        return [1.0 if operand else 0.0] * len(bars)
    return [_finite(operand, name="condition constant")] * len(bars)


def _condition_values(
    conditions: Mapping[str, Mapping[str, Any]],
    condition_id: str,
    index: int,
    bars: list[dict[str, Any]],
    indicators: Mapping[str, list[float | None]],
    cache: dict[tuple[str, int], bool],
    visiting: set[str],
) -> bool:
    cache_key = (condition_id, index)
    if cache_key in cache:
        return cache[cache_key]
    if condition_id in visiting:
        raise ValueError(f"cyclic condition reference: {condition_id}")
    if condition_id not in conditions:
        raise ValueError(f"unknown condition: {condition_id}")
    visiting.add(condition_id)
    condition = conditions[condition_id]
    operator = str(condition["operator"])
    refs = condition.get("conditions", [])
    if operator == "and":
        result = all(_condition_values(conditions, ref, index, bars, indicators, cache, visiting) for ref in refs)
    elif operator == "or":
        result = any(_condition_values(conditions, ref, index, bars, indicators, cache, visiting) for ref in refs)
    elif operator == "not":
        result = not _condition_values(conditions, refs[0], index, bars, indicators, cache, visiting)
    else:
        left_operand = condition.get("left", "close")
        right_operand = condition.get("right")
        left_values = _operand_series(left_operand, bars, indicators)
        if operator in {"rising", "falling"}:
            current = left_values[index]
            previous = left_values[index - 1] if index > 0 else None
            result = current is not None and previous is not None and (current > previous if operator == "rising" else current < previous)
        else:
            left = left_values[index]
            if operator in {"crosses_above", "crosses_below"}:
                right_values = _operand_series(right_operand, bars, indicators)
                right = right_values[index]
                previous_left = left_values[index - 1] if index > 0 else None
                previous_right = right_values[index - 1] if index > 0 else None
                if None in {left, right, previous_left, previous_right}:
                    result = False
                elif operator == "crosses_above":
                    result = bool(
                        _finite(previous_left, name="crossing previous left")
                        <= _finite(previous_right, name="crossing previous right")
                        and _finite(left, name="crossing left") > _finite(right, name="crossing right")
                    )
                else:
                    result = bool(
                        _finite(previous_left, name="crossing previous left")
                        >= _finite(previous_right, name="crossing previous right")
                        and _finite(left, name="crossing left") < _finite(right, name="crossing right")
                    )
            elif operator in {"between", "outside"}:
                if left is None or not isinstance(right_operand, Sequence) or isinstance(right_operand, (str, bytes)) or len(right_operand) != 2:
                    result = False
                else:
                    low = _finite(right_operand[0], name="between lower bound")
                    high = _finite(right_operand[1], name="between upper bound")
                    left_number = float(left)
                    result = low <= left_number <= high if operator == "between" else left_number < low or left_number > high
            else:
                right_values = _operand_series(right_operand, bars, indicators)
                right = right_values[index]
                if left is None or right is None:
                    result = False
                elif operator == ">":
                    result = left > right
                elif operator == ">=":
                    result = left >= right
                elif operator == "<":
                    result = left < right
                elif operator == "<=":
                    result = left <= right
                elif operator == "==":
                    result = left == right
                elif operator == "!=":
                    result = left != right
                else:
                    raise ValueError(f"unsupported condition operator: {operator}")
    visiting.remove(condition_id)
    cache[cache_key] = bool(result)
    return bool(result)


def evaluate_visual_actions(profile: Mapping[str, Any], bars: list[dict[str, Any]], indicators: Mapping[str, list[float | None]]) -> list[list[dict[str, Any]]]:
    flow = profile.get("flow", {})
    condition_specs = {str(item["id"]): item for item in flow.get("conditions", [])}
    action_specs = flow.get("actions", [])
    all_actions: list[list[dict[str, Any]]] = []
    for index in range(len(bars)):
        cache: dict[tuple[str, int], bool] = {}
        current: list[dict[str, Any]] = []
        for spec in action_specs:
            when = spec.get("when")
            if when is None:
                triggered = True
            else:
                references = [when] if isinstance(when, str) else list(when)
                results = [
                    _condition_values(condition_specs, str(reference), index, bars, indicators, cache, set())
                    for reference in references
                ]
                triggered = any(results) if spec.get("any") else all(results)
            if triggered:
                current.append(dict(spec))
        all_actions.append(current)
    return all_actions


def _normalize_runtime_actions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        values = [value]
    elif isinstance(value, Sequence):
        values = list(value)
    else:
        raise ValueError("strategy on_bar must return an action, list of actions, or None")
    result: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, str):
            action = item.strip().lower()
            action = {"type": {"buy": "enter_long", "sell": "enter_short", "close": "exit"}.get(action, action)}
        elif isinstance(item, Mapping):
            action = dict(item)
        else:
            raise ValueError("strategy actions must be strings or objects")
        action_type = str(action.get("type") or action.get("action") or "").strip().lower()
        action_type = {"buy": "enter_long", "sell": "enter_short", "close_all": "exit", "flat": "exit"}.get(action_type, action_type)
        if action_type not in SUPPORTED_ACTIONS:
            raise ValueError(f"unsupported runtime action: {action_type or '<empty>'}")
        normalized: dict[str, Any] = {"type": action_type}
        if "size" in action or "quantity" in action:
            size = _finite(action.get("size", action.get("quantity")), name="runtime action size")
            if size <= 0:
                raise ValueError("runtime action size must be greater than 0")
            normalized["size"] = size
        result.append(normalized)
    return result


def _trade_metrics(trades: list[dict[str, Any]], equity_curve: list[dict[str, Any]], initial_cash: float, bars_count: int) -> dict[str, Any]:
    net_values = [float(trade["net_profit"]) for trade in trades]
    gross_values = [float(trade["gross_profit"]) for trade in trades]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    gross_profit = sum(value for value in gross_values if value > 0)
    gross_loss = abs(sum(value for value in gross_values if value < 0))
    equity_values = [float(item["equity"]) for item in equity_curve] or [initial_cash]
    peak = equity_values[0]
    max_drawdown = 0.0
    for value in equity_values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    net_profit = sum(net_values)
    final_equity = initial_cash + net_profit
    if gross_loss:
        profit_factor: float | str = round(gross_profit / gross_loss, 8)
    else:
        profit_factor = "inf" if gross_profit else 0.0
    return {
        "bars": bars_count,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 8),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "net_profit": round(net_profit, 8),
        "average_trade": round((net_profit / len(trades)) if trades else 0.0, 8),
        "profit_factor": profit_factor,
        "max_drawdown": round(max_drawdown, 8),
        "return_pct": round((net_profit / initial_cash * 100.0) if initial_cash else 0.0, 8),
        "initial_cash": round(initial_cash, 8),
        "final_equity": round(final_equity, 8),
        "long_trades": sum(1 for trade in trades if trade["side"] == "long"),
        "short_trades": sum(1 for trade in trades if trade["side"] == "short"),
    }


def run_backtest(
    profile: Mapping[str, Any],
    bars: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    initial_cash: float = 10_000.0,
    position_size: float | None = None,
    commission: float = 0.0,
    slippage: float = 0.0,
    strategy_instance: ResearchStrategyProtocol | None = None,
    workspace_root: Path | None = None,
    resolve_workspace_path: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    """Run a deterministic, single-position research backtest."""

    normalized_profile = normalize_strategy_profile(
        profile,
        workspace_root=workspace_root or Path.cwd(),
        resolve_workspace_path=resolve_workspace_path,
    )
    normalized_bars = normalize_ohlcv(list(bars))
    initial_cash = _finite(initial_cash, name="initial_cash")
    commission = _finite(commission, name="commission")
    slippage = _finite(slippage, name="slippage")
    if initial_cash <= 0 or commission < 0 or slippage < 0:
        raise ValueError("initial_cash must be positive; commission and slippage cannot be negative")
    flow = normalized_profile["flow"]
    default_size = float(position_size if position_size is not None else flow["settings"]["default_size"])
    if default_size <= 0:
        raise ValueError("position_size must be greater than 0")
    is_code = normalized_profile["builder"] == "code"
    if is_code and strategy_instance is None:
        raise ValueError("a validated strategy_instance is required for a code profile")
    indicators = calculate_indicators(normalized_profile, normalized_bars) if not is_code else {}
    visual_actions = evaluate_visual_actions(normalized_profile, normalized_bars, indicators) if not is_code else []
    conditions_snapshot = {str(item["id"]): item for item in flow.get("conditions", [])}
    position: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    realized = 0.0

    def close_position(index: int, price: float, reason: str) -> None:
        nonlocal position, realized
        if position is None:
            return
        side = str(position["side"])
        quantity = float(position["quantity"])
        exit_price = price - slippage if side == "long" else price + slippage
        gross_profit = (exit_price - float(position["entry_price"])) * quantity if side == "long" else (float(position["entry_price"]) - exit_price) * quantity
        fees = commission * quantity * 2.0
        net_profit = gross_profit - fees
        realized += net_profit
        trades.append(
            {
                "side": side,
                "quantity": quantity,
                "entry_index": int(position["entry_index"]),
                "exit_index": index,
                "entry_timestamp": position["entry_timestamp"],
                "exit_timestamp": normalized_bars[index]["timestamp"],
                "entry_price": round(float(position["entry_price"]), 8),
                "exit_price": round(float(exit_price), 8),
                "gross_profit": round(gross_profit, 8),
                "fees": round(fees, 8),
                "net_profit": round(net_profit, 8),
                "reason": reason,
            }
        )
        position = None

    for index, bar in enumerate(normalized_bars):
        # Signals from the previous closed bar fill at this bar's open.
        for item in list(pending):
            if item["fill_index"] != index:
                continue
            pending.remove(item)
            if position is not None:
                continue
            side = item["side"]
            if side == "short" and not bool(flow["settings"].get("allow_short", True)):
                continue
            entry_price = bar["open"] + slippage if side == "long" else bar["open"] - slippage
            position = {
                "side": side,
                "quantity": float(item["quantity"]),
                "entry_index": index,
                "entry_timestamp": bar["timestamp"],
                "entry_price": entry_price,
            }

        if is_code:
            context = {
                "index": index,
                "history": tuple(dict(item) for item in normalized_bars[: index + 1]),
                "position": dict(position) if position else None,
                "indicators": {},
                "research_only": True,
                "real_account_allowed": False,
                "order_execution_allowed": False,
            }
            try:
                raw_actions = strategy_instance.on_bar(dict(bar), context)  # type: ignore[union-attr]
            except Exception as exc:
                raise ValueError(f"strategy on_bar failed at bar {index}: {exc}") from exc
            actions = _normalize_runtime_actions(raw_actions)
        else:
            actions = visual_actions[index]

        # Close/reverse actions are applied at the current closed price before
        # entries are queued for the next bar.  This keeps rule evaluation
        # point-in-time and deterministic.
        for action in actions:
            action_type = str(action["type"])
            if action_type in {"exit", "close"}:
                close_position(index, bar["close"], "signal_exit")
            elif action_type == "exit_long" and position is not None and position["side"] == "long":
                close_position(index, bar["close"], "signal_exit_long")
            elif action_type == "exit_short" and position is not None and position["side"] == "short":
                close_position(index, bar["close"], "signal_exit_short")
            elif action_type == "reverse" and position is not None:
                current_side = str(position["side"])
                close_position(index, bar["close"], "signal_reverse")
                pending.append(
                    {
                        "fill_index": index + 1,
                        "side": "short" if current_side == "long" else "long",
                        "quantity": float(action.get("size", default_size)),
                    }
                )

        for action in actions:
            action_type = str(action["type"])
            if action_type not in {"enter_long", "enter_short"} or position is not None:
                continue
            if index + 1 >= len(normalized_bars):
                continue
            side = "long" if action_type == "enter_long" else "short"
            if side == "short" and not bool(flow["settings"].get("allow_short", True)):
                continue
            pending.append(
                {
                    "fill_index": index + 1,
                    "side": side,
                    "quantity": float(action.get("size", default_size)),
                }
            )

        mark_to_market = 0.0
        if position is not None:
            if position["side"] == "long":
                mark_to_market = (bar["close"] - float(position["entry_price"])) * float(position["quantity"])
            else:
                mark_to_market = (float(position["entry_price"]) - bar["close"]) * float(position["quantity"])
        equity_curve.append(
            {
                "index": index,
                "timestamp": bar["timestamp"],
                "equity": round(initial_cash + realized + mark_to_market, 8),
            }
        )

    if position is not None:
        close_position(len(normalized_bars) - 1, normalized_bars[-1]["close"], "end_of_data")
        equity_curve[-1]["equity"] = round(initial_cash + realized, 8)

    metrics = _trade_metrics(trades, equity_curve, initial_cash, len(normalized_bars))
    return {
        "ok": True,
        "strategy": {
            "id": normalized_profile["id"],
            "name": normalized_profile["name"],
            "builder": normalized_profile["builder"],
            "symbol": normalized_profile["symbol"],
            "timeframe": normalized_profile["timeframe"],
        },
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
        "indicator_names": sorted(indicators),
        "execution": "backtest_only",
        "research_only": True,
        "real_account_allowed": False,
        "order_execution_allowed": False,
    }


def validate_strategy_class(strategy_class: Any) -> ResearchStrategyProtocol:
    """Instantiate and validate the explicit research strategy protocol."""

    if not inspect.isclass(strategy_class):
        raise ValueError("strategy target must be a class")
    if not callable(getattr(strategy_class, "on_bar", None)):
        raise ValueError("strategy class must define callable on_bar(bar, context)")
    try:
        instance = strategy_class()
    except Exception as exc:
        raise ValueError("strategy class must be constructible without arguments") from exc
    method = getattr(instance, "on_bar", None)
    if not callable(method):
        raise ValueError("strategy instance must define callable on_bar(bar, context)")
    try:
        inspect.signature(method).bind({}, {})
    except (TypeError, ValueError) as exc:
        raise ValueError("on_bar must accept (bar, context)") from exc
    return instance  # type: ignore[return-value]


def _resolve_module_path(
    module_path: str | Path,
    *,
    workspace_root: Path,
    resolve_workspace_path: Callable[..., Path] | None = None,
    require_exists: bool = True,
) -> Path:
    resolver = resolve_workspace_path
    resolved = Path(resolver(module_path) if resolver else _local_resolver(workspace_root, module_path)).resolve()
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise ValueError("strategy module must stay inside the NewEraTradingBot workspace") from exc
    if resolved.suffix.lower() != ".py":
        raise ValueError("strategy module must be a .py file")
    if require_exists and not resolved.is_file():
        raise FileNotFoundError(f"strategy module not found: {module_path}")
    return resolved


def load_python_strategy(
    module_path: str | Path,
    class_name: str,
    *,
    workspace_root: Path | None = None,
    resolve_workspace_path: Callable[..., Path] | None = None,
) -> LoadedStrategy:
    """Load one validated class from a workspace-local Python module."""

    root = (workspace_root or Path.cwd()).resolve()
    class_name = _clean_text(class_name, name="class_name", max_length=128, required=True)
    if not CLASS_NAME_PATTERN.fullmatch(class_name):
        raise ValueError("class_name must be a simple Python class name")
    resolved = _resolve_module_path(
        module_path,
        workspace_root=root,
        resolve_workspace_path=resolve_workspace_path,
        require_exists=True,
    )
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    module_name = f"newera_workspace_strategy_{digest}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot create import specification for strategy module: {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ValueError(f"strategy module failed to load: {exc}") from exc
    target = getattr(module, class_name, None)
    if not inspect.isclass(target):
        sys.modules.pop(module_name, None)
        raise ValueError(f"module does not define class {class_name}")
    if getattr(target, "__module__", None) != module_name:
        sys.modules.pop(module_name, None)
        raise ValueError("strategy class must be defined by the selected workspace module")
    instance = validate_strategy_class(target)
    return LoadedStrategy(
        module_path=resolved,
        module_name=module_name,
        class_name=class_name,
        strategy_class=target,
        instance=instance,
    )


class StrategyLab:
    """Persistence and execution facade used by the local API."""

    def __init__(self, settings_or_root: Any, *, profile_dir: str | Path = DEFAULT_PROFILE_DIR) -> None:
        root_value = (
            settings_or_root
            if isinstance(settings_or_root, (str, Path))
            else getattr(settings_or_root, "root", settings_or_root)
        )
        self.workspace_root = Path(root_value).resolve()
        self._resolver = getattr(settings_or_root, "resolve_workspace_path", None)
        self.profile_dir = self._resolve(profile_dir)

    def _resolve(self, value: str | Path, *, default: Path | None = None) -> Path:
        if self._resolver is not None:
            return Path(self._resolver(value, default=default)).resolve()
        return _local_resolver(self.workspace_root, value, default=default)

    def _profile_path(self, strategy_id: str) -> Path:
        normalized_id = _normalize_id(strategy_id)
        path = (self.profile_dir / f"{normalized_id}.json").resolve()
        try:
            path.relative_to(self.profile_dir.resolve())
        except ValueError as exc:
            raise ValueError("strategy profile path must stay inside the strategy profile directory") from exc
        return path

    def normalize_profile(self, profile: Mapping[str, Any], *, strategy_id: str | None = None) -> dict[str, Any]:
        return normalize_strategy_profile(
            profile,
            workspace_root=self.workspace_root,
            resolve_workspace_path=self._resolver,
            strategy_id=strategy_id,
        )

    def list_profiles(self) -> list[dict[str, Any]]:
        if not self.profile_dir.exists():
            return []
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.profile_dir.glob("*.json")):
            try:
                profiles.append(self.load_profile(path.stem))
            except (OSError, ValueError, json.JSONDecodeError):
                # A malformed file is not executable and is omitted from the
                # usable profile list; save_profile never creates one.
                continue
        return profiles

    def load_profile(self, strategy_id: str) -> dict[str, Any]:
        path = self._profile_path(strategy_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"strategy profile not found: {strategy_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read strategy profile {strategy_id}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("strategy profile must be a JSON object")
        return self.normalize_profile(payload, strategy_id=strategy_id)

    def save_profile(self, profile: Mapping[str, Any], *, strategy_id: str | None = None) -> dict[str, Any]:
        normalized = self.normalize_profile(profile, strategy_id=strategy_id)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        path = self._profile_path(normalized["id"])
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return normalized

    def load_strategy(
        self,
        *,
        strategy_id: str | None = None,
        module_path: str | Path | None = None,
        class_name: str | None = None,
    ) -> LoadedStrategy:
        profile: dict[str, Any] | None = None
        if strategy_id:
            profile = self.load_profile(strategy_id)
            if profile["builder"] != "code":
                raise ValueError("strategy profile is visual and has no Python class to load")
            code = profile.get("code") or {}
            module_path = code.get("module_path")
            class_name = code.get("class_name")
        if not module_path or not class_name:
            raise ValueError("strategy_id or both module_path and class_name are required")
        return load_python_strategy(
            module_path,
            class_name,
            workspace_root=self.workspace_root,
            resolve_workspace_path=self._resolver,
        )

    def _bars(self, bars: Iterable[Mapping[str, Any] | Sequence[Any]] | None, data_path: str | Path | None) -> list[dict[str, Any]]:
        if bars is not None:
            return normalize_ohlcv(list(bars))
        if not data_path:
            raise ValueError("bars or data_path is required")
        resolved = self._resolve(data_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"OHLCV data not found: {data_path}")
        return load_ohlcv_csv(resolved)

    def backtest(
        self,
        profile_or_id: Mapping[str, Any] | str,
        bars: Iterable[Mapping[str, Any] | Sequence[Any]] | None = None,
        *,
        data_path: str | Path | None = None,
        initial_cash: float = 10_000.0,
        position_size: float | None = None,
        commission: float = 0.0,
        slippage: float = 0.0,
    ) -> dict[str, Any]:
        profile = self.load_profile(profile_or_id) if isinstance(profile_or_id, str) else self.normalize_profile(profile_or_id)
        normalized_bars = self._bars(bars, data_path)
        strategy_instance: ResearchStrategyProtocol | None = None
        if profile["builder"] == "code":
            strategy_instance = self.load_strategy(strategy_id=profile["id"]).instance
        return run_backtest(
            profile,
            normalized_bars,
            initial_cash=initial_cash,
            position_size=position_size,
            commission=commission,
            slippage=slippage,
            strategy_instance=strategy_instance,
            workspace_root=self.workspace_root,
            resolve_workspace_path=self._resolver,
        )

    def walk_forward(
        self,
        profile_or_id: Mapping[str, Any] | str,
        bars: Iterable[Mapping[str, Any] | Sequence[Any]] | None = None,
        *,
        data_path: str | Path | None = None,
        train_ratio: float = 0.7,
        train_bars: int | None = None,
        initial_cash: float = 10_000.0,
        position_size: float | None = None,
        commission: float = 0.0,
        slippage: float = 0.0,
    ) -> dict[str, Any]:
        profile = self.load_profile(profile_or_id) if isinstance(profile_or_id, str) else self.normalize_profile(profile_or_id)
        normalized_bars = self._bars(bars, data_path)
        if train_bars is None:
            train_ratio = _finite(train_ratio, name="train_ratio")
            if not 0.1 <= train_ratio <= 0.9:
                raise ValueError("train_ratio must be between 0.1 and 0.9")
            split_index = int(len(normalized_bars) * train_ratio)
        else:
            split_index = int(train_bars)
        if split_index < 2 or split_index >= len(normalized_bars) - 1:
            raise ValueError("walk-forward split must leave at least two bars in train and validation sets")
        train_bars_data = normalized_bars[:split_index]
        validation_bars_data = normalized_bars[split_index:]

        def run_segment(segment: list[dict[str, Any]]) -> dict[str, Any]:
            strategy_instance: ResearchStrategyProtocol | None = None
            if profile["builder"] == "code":
                strategy_instance = self.load_strategy(strategy_id=profile["id"]).instance
            return run_backtest(
                profile,
                segment,
                initial_cash=initial_cash,
                position_size=position_size,
                commission=commission,
                slippage=slippage,
                strategy_instance=strategy_instance,
                workspace_root=self.workspace_root,
                resolve_workspace_path=self._resolver,
            )

        train_result = run_segment(train_bars_data)
        validation_result = run_segment(validation_bars_data)
        return {
            "ok": True,
            "strategy": {
                "id": profile["id"],
                "name": profile["name"],
                "builder": profile["builder"],
                "symbol": profile["symbol"],
                "timeframe": profile["timeframe"],
            },
            "split": {
                "split_index": split_index,
                "train_bars": len(train_bars_data),
                "validation_bars": len(validation_bars_data),
                "train_ratio": round(split_index / len(normalized_bars), 8),
            },
            "train": train_result,
            "validation": validation_result,
            "metrics": {
                "train": train_result["metrics"],
                "validation": validation_result["metrics"],
            },
            "execution": "walk_forward_backtest_only",
            "research_only": True,
            "real_account_allowed": False,
            "order_execution_allowed": False,
        }


def walk_forward_backtest(
    profile: Mapping[str, Any] | str,
    bars: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    workspace_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenient standalone wrapper around :class:`StrategyLab`."""

    lab = StrategyLab(workspace_root or Path.cwd())
    return lab.walk_forward(profile, bars, **kwargs)


# Convenient alias for callers that prefer a function name over the facade.
backtest_strategy = run_backtest


__all__ = [
    "DEFAULT_PROFILE_DIR",
    "LoadedStrategy",
    "ResearchStrategyProtocol",
    "STRATEGY_SCHEMA_VERSION",
    "StrategyLab",
    "backtest_strategy",
    "calculate_indicators",
    "evaluate_visual_actions",
    "load_ohlcv_csv",
    "load_python_strategy",
    "normalize_ohlcv",
    "normalize_ohlcv_bar",
    "normalize_strategy_profile",
    "run_backtest",
    "validate_strategy_class",
    "walk_forward_backtest",
]
