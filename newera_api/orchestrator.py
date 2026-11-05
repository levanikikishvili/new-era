"""Async orchestration of the research runners with Direct MT5 OHLCV + L2 Orderbook DOM Intelligence.

The API starts fixed, workspace-local scripts only. It does not expose a shell
endpoint and it does not place MT5 orders.
Includes:
- `MarketBookGet(symbol, levels)`: Real-time L2 Orderbook DOM via MT5 `market_book_get()`
  for Forex/Metals/Indices and Binance/Bybit L2 Depth WebSocket for Crypto pairs (`BTCUSD`, `ETHUSD`, etc.).
- `OrderflowLiquidityAgent` integration into the Multi-Layer Alignment Gate (Liquidity Walls,
  Whale Orders, Spoofing Alerts, Absorption/Iceberg detection, and Gate Veto on opposing walls).
"""

from __future__ import annotations

import asyncio
import csv
import inspect
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from kronos_matrix_strategy import STRATEGY_ID, write_gate_csv

from .ai_engine import (
    DEFAULT_OLLAMA_MODEL,
    OllamaAIEngine,
    SafeDecision,
    build_scan_context,
    json_safe_copy,
    normalize_model_result,
    normalize_research_strategy_profile,
)
from .bot_lab import StrategyLab
from .bot_performance_intelligence import BotPerformanceIntelligenceEngine
from .config import ApiSettings, load_workspace_dotenv
from .controls import RuntimeControls
from .events import EventBus, event
from .external_bot_hub import MultiBotIntegrationHub
from .gemini_manager import GeminiPortfolioManager, Step1TALibResult, TALibTechnicalEngine
from .historical_intelligence import HistoricalIntelligenceEngine
from .memory import MemoryStore
from .mcp_server import MCPServer
from .mt5_bridge import (
    close_mt5_demo_positions,
    execute_mt5_demo_order,
    fetch_mt5_bars_to_csv,
    get_mt5_open_positions,
    get_mt5_status,
    get_mt5_symbol_snapshot,
    get_mt5_trade_history,
    manage_mt5_demo_positions,
)
from .orderflow_agent import OrderflowAnalysis, OrderflowLiquidityAgent, resolve_crypto_pair
from .saas_architecture import SaaSArchitectureManager
from .telegram_bot import TelegramRemoteController
from .trade_logger import TradeDecisionLogger


SCREENER_SYMBOLS: tuple[str, ...] = (
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "BTCUSD",
    "ETHUSD",
    "US500",
    "USTEC",
)
AUTO_SCREENER_MIN_WIN_PROB: float = 68.0
AUTO_SCREENER_INTERVAL_SECONDS: int = 15
JOEY_LIVE_AUTO_EXECUTION_SYMBOLS: frozenset[str] = frozenset({"XAUUSD", "USTEC", "US500"})


def is_joey_live_auto_execution_symbol(symbol: str) -> bool:
    """Return whether JoeyCIO may open an autonomous Demo order for a symbol."""
    return str(symbol or "").strip().upper() in JOEY_LIVE_AUTO_EXECUTION_SYMBOLS


def build_joey_profile_comment(symbol: str, step2: dict[str, Any] | None) -> str:
    """Build the compact MT5 comment used to profile JoeyCIO executions."""
    normalized_symbol = str(symbol or "").strip().upper() or "UNKNOWN"
    step2_obj = step2 if isinstance(step2, dict) else {}
    try:
        confluence_count = int(step2_obj.get("confluence_count") or 0)
    except (TypeError, ValueError):
        confluence_count = 0
    confluence_count = max(0, min(3, confluence_count))
    return f"JoeyCIO_{normalized_symbol}_{confluence_count}of3"[:31]

LLM_ENV_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "DASHSCOPE_API_KEY",
    "NVIDIA_API_KEY",
)



def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_symbol_filename(symbol: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_+-]", "_", symbol.strip().lower())
    return cleaned or "xauusd"


def _fetch_binance_klines_to_csv(pair: str, output_csv: Path, interval: str = "5m", limit: int = 1000) -> dict[str, Any]:
    """Fallback OHLCV fetcher for Crypto pairs (BTCUSD, ETHUSD) when MT5 lacks crypto history."""
    url = f"https://api.binance.com/api/v3/klines?symbol={pair.upper()}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "NewEraQuant/1.0"})
    with urllib.request.urlopen(req, timeout=8.0) as resp:
        klines = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(klines, list) or len(klines) < 601:
        raise RuntimeError(f"Insufficient crypto klines returned for {pair}")

    # Exclude currently forming candle
    closed = klines[:-1]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume", "amount"))
        for row in closed:
            ts = datetime.fromtimestamp(int(row[0]) / 1000.0, timezone.utc).isoformat()
            open_p = float(row[1])
            high_p = float(row[2])
            low_p = float(row[3])
            close_p = float(row[4])
            vol = float(row[5])
            quote_amt = float(row[7]) if len(row) > 7 else vol * close_p
            writer.writerow((ts, open_p, high_p, low_p, close_p, vol, quote_amt))

    last_row = closed[-1]
    w20 = closed[-20:]
    return {
        "ok": True,
        "requested_symbol": pair,
        "resolved_symbol": pair,
        "timeframe": "M5",
        "bars_exported": len(closed),
        "output_csv": str(output_csv),
        "last_bar": datetime.fromtimestamp(int(last_row[0]) / 1000.0, timezone.utc).isoformat(),
        "last_close": float(last_row[4]),
        "bid": float(last_row[4]),
        "ask": float(last_row[4]),
        "point": 0.01,
        "digits": 2,
        "spread_points": 5.0,
        "atr_m5": max(float(r[2]) - float(r[3]) for r in w20) * 0.5,
        "range_20_low": min(float(r[3]) for r in w20),
        "range_20_high": max(float(r[2]) for r in w20),
    }


@dataclass
class ScanJob:
    scan_id: str
    mode: str
    status: str = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    last_event: dict[str, Any] | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "last_event": self.last_event,
            "error": self.error,
            "result": self.result,
        }


class Orchestrator:
    def __init__(self, settings: ApiSettings, bus: EventBus | None = None) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self.bus = bus or EventBus()
        self.memory = MemoryStore(
            self.settings.memory_dir,
            enabled=self.settings.chroma_enabled,
            collection_name=self.settings.chroma_collection,
        )
        self.orderflow = OrderflowLiquidityAgent()
        self.gemini_cio = GeminiPortfolioManager(self.settings.root)
        self.historical_intelligence = HistoricalIntelligenceEngine(self.settings.root)
        self.bot_intelligence = BotPerformanceIntelligenceEngine(self.settings.root)
        self.multi_bot_hub = MultiBotIntegrationHub(self.settings.root)
        self.saas = SaaSArchitectureManager(self.settings.root)
        self.telegram = TelegramRemoteController(self.settings.root)
        self.telegram.set_command_handler(self._handle_telegram_command)
        self.controls = RuntimeControls()
        self.decision_logger = TradeDecisionLogger(
            self.settings.decision_history_path,
            self.bus,
            max_records=max(1, int(getattr(self.settings, "decision_history_max_records", 1000))),
        )
        # The local model is an untrusted, research-only advisor. It is kept
        # separate from every existing execution gate and is never consulted by
        # the order path to authorize a trade.
        self.ai_engine = OllamaAIEngine(
            endpoint=getattr(self.settings, "ai_endpoint", None),
            model=getattr(self.settings, "ai_model", DEFAULT_OLLAMA_MODEL),
            timeout=getattr(self.settings, "ai_timeout_seconds", 30.0),
            offline=getattr(self.settings, "ai_offline", True),
        )
        self.strategy_lab = StrategyLab(self.settings)
        self._ai_proposal_profiles: dict[str, dict[str, Any]] = {}
        self._ai_proposal_records: list[dict[str, Any]] = []
        self._ai_metrics: dict[str, Any] = {
            "analyses_total": 0,
            "analyses_completed": 0,
            "analyses_blocked": 0,
            "analyses_disabled": 0,
            "proposals_seen": 0,
            "proposals_persisted": 0,
            "proposals_deduplicated": 0,
            "proposals_failed": 0,
            "registry_profiles_created": 0,
            "registry_profiles_updated": 0,
            "last_analysis_at": None,
            "last_error": None,
        }
        try:
            for profile in self.strategy_lab.list_profiles():
                metadata = profile.get("metadata") if isinstance(profile, Mapping) else None
                if not isinstance(metadata, Mapping) or metadata.get("source") != "ai_engine":
                    continue
                profile_id = str(profile.get("id") or "").strip().lower()
                if profile_id:
                    self._ai_proposal_profiles[profile_id] = self._ai_profile_view(profile)
        except Exception:
            # Existing malformed research files are omitted by StrategyLab;
            # startup must remain available even if the profile directory is
            # unreadable.
            self._ai_proposal_profiles = {}
        self.mcp_server = MCPServer(
            market_snapshot_provider=self._mcp_market_snapshot,
            decision_audit_provider=self._mcp_decision_audit,
            strategy_proposals=self._ai_proposal_profiles.values(),
            max_parallel=4,
        )
        self.paused: bool = False
        self._latest_l2: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, ScanJob] = {}
        self._active_scan_id: str | None = None
        self._lock = asyncio.Lock()
        self.screener_symbols: list[str] = list(SCREENER_SYMBOLS)
        self.screener_interval_seconds: int = max(
            1,
            int(getattr(self.settings, "screener_interval_seconds", AUTO_SCREENER_INTERVAL_SECONDS)),
        )
        self._screener_task: asyncio.Task[Any] | None = None
        self._screener_running: bool = False
        self._last_screener_cycle: dict[str, Any] | None = None

    @property
    def l2_report_path(self) -> Path:
        return self.settings.report_dir / "l2_orderflow_latest.json"

    @property
    def waterfall_report_path(self) -> Path:
        return self.settings.report_dir / "waterfall_latest.json"

    @property
    def screener_report_path(self) -> Path:
        return self.settings.report_dir / "multi_pair_screener_latest.json"

    def get_controls(self) -> dict[str, Any]:
        return self.controls.snapshot()

    @staticmethod
    def _blocked_ai_decision(reason: str, *, symbol: str = "") -> SafeDecision:
        """Create a bounded, non-executable decision for every AI failure path."""

        return SafeDecision(
            action="ABSTAIN",
            confidence=0.0,
            risk_pct=0.0,
            min_rr=1.5,
            max_sl_atr=2.5,
            time_stop_minutes=120,
            reasoning_summary="AI analysis was blocked; no executable decision was produced.",
            evidence=[{"type": "safety", "status": "BLOCKED", "reason": str(reason)[:240]}],
            bot_proposals=[],
            status="BLOCKED",
            block_reason=str(reason)[:240],
            symbol=str(symbol or "").strip().upper(),
            research_only=True,
            executable=False,
            execution_allowed=False,
        )

    @staticmethod
    def _bound_ai_value(value: Any, *, depth: int = 0) -> Any:
        """Keep model context finite without exposing arbitrary objects."""

        if depth > 5:
            return None
        if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
            try:
                value = value.to_dict()
            except Exception:
                return None
        if value is None or isinstance(value, (str, bool, int)):
            return value[:1000] if isinstance(value, str) else value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, Mapping):
            bounded: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 64:
                    break
                bounded[str(key)[:120]] = Orchestrator._bound_ai_value(item, depth=depth + 1)
            return bounded
        if isinstance(value, (list, tuple)):
            return [Orchestrator._bound_ai_value(item, depth=depth + 1) for item in list(value)[:64]]
        return str(value)[:256]

    @staticmethod
    def _redact_ai_value(value: Any, *, depth: int = 0) -> Any:
        """Remove hidden-reasoning tags before telemetry or profile storage."""

        if depth > 6:
            return None
        if isinstance(value, str):
            text = re.sub(
                r"<(?:think|analysis)>.*?</(?:think|analysis)>",
                "",
                value,
                flags=re.IGNORECASE | re.DOTALL,
            )
            text = re.sub(r"<(?:think|analysis)>.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"</(?:think|analysis)>", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:480] + ("…" if len(text) > 480 else "")
        if isinstance(value, Mapping):
            return {
                str(key): Orchestrator._redact_ai_value(item, depth=depth + 1)
                for key, item in list(value.items())[:128]
            }
        if isinstance(value, (list, tuple)):
            return [Orchestrator._redact_ai_value(item, depth=depth + 1) for item in list(value)[:64]]
        return value

    @staticmethod
    def _read_latest_ohlcv_csv(path: Path | None, max_bars: int = 32) -> list[dict[str, Any]]:
        """Read only the latest finite OHLCV rows for the local AI prompt."""

        if path is None or not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    normalized_raw = {str(key).strip().lower(): value for key, value in raw.items()}
                    try:
                        values: dict[str, Any] = {
                            "open": float(normalized_raw.get("open") or 0.0),
                            "high": float(normalized_raw.get("high") or 0.0),
                            "low": float(normalized_raw.get("low") or 0.0),
                            "close": float(normalized_raw.get("close") or 0.0),
                            "volume": float(normalized_raw.get("volume") or 0.0),
                        }
                        if any(not math.isfinite(float(item)) for item in values.values()):
                            continue
                        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    timestamp = normalized_raw.get("timestamp") or normalized_raw.get("time") or normalized_raw.get("date")
                    if timestamp not in (None, ""):
                        values["timestamp"] = timestamp
                    rows.append(values)
        except OSError:
            return []
        return rows[-max(1, int(max_bars)) :]

    @staticmethod
    def _mapping_or_empty(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                converted = to_dict()
                return dict(converted) if isinstance(converted, Mapping) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _waterfall_step2(waterfall: Any) -> dict[str, Any]:
        waterfall_obj = Orchestrator._mapping_or_empty(waterfall)
        steps = waterfall_obj.get("steps")
        if isinstance(steps, Mapping):
            return Orchestrator._mapping_or_empty(steps.get("step2_kronos_matrix"))
        return Orchestrator._mapping_or_empty(waterfall_obj.get("step2_kronos_matrix"))

    def _mcp_market_snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = str(arguments.get("symbol") or self.settings.default_symbol).strip().upper()
        try:
            return get_mt5_symbol_snapshot(symbol)
        except Exception as exc:
            latest = self._latest_l2.get(symbol)
            if latest is not None:
                return {"symbol": symbol, "source": "l2_memory", "snapshot": latest, "read_only": True}
            return {
                "symbol": symbol,
                "available": False,
                "source": "offline",
                "error": str(exc)[:160],
                "read_only": True,
            }

    def _mcp_decision_audit(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            limit = max(1, min(int(arguments.get("limit", 100)), 5000))
        except (TypeError, ValueError):
            limit = 100
        return self.decision_logger.read_records(limit)

    def ai_status(self) -> dict[str, Any]:
        enabled = bool(getattr(self.settings, "ai_enabled", True))
        offline = bool(getattr(self.ai_engine, "offline", getattr(self.settings, "ai_offline", True)))
        if not enabled:
            factory_status = "disabled"
        elif offline:
            factory_status = "offline"
        else:
            factory_status = "ready"
        counts = {
            **self._ai_metrics,
            "strategy_profiles": len(self._ai_proposal_profiles),
            "proposal_records": len(self._ai_proposal_records),
        }
        return {
            "enabled": enabled,
            "offline": offline,
            "model": str(getattr(self.ai_engine, "model", DEFAULT_OLLAMA_MODEL)),
            "endpoint": str(getattr(self.ai_engine, "endpoint", "")),
            "fail_closed": True,
            "research_only": True,
            "execution_allowed": False,
            "real_account_allowed": False,
            "order_execution_allowed": False,
            "ai_bot_factory": {
                "status": factory_status,
                "analysis_enabled": enabled,
                "registry_enabled": bool(getattr(self.settings, "ai_bot_factory_registry_enabled", False)),
                "default_registry_enabled": False,
                "research_only": True,
                "enabled": False,
                "real_account_allowed": False,
                "order_execution_allowed": False,
                "counts": counts,
            },
            "counts": counts,
        }

    def _refresh_ai_profiles_sync(self) -> list[dict[str, Any]]:
        profiles = [
            profile
            for profile in self.strategy_lab.list_profiles()
            if isinstance(profile.get("metadata"), Mapping)
            and profile["metadata"].get("source") == "ai_engine"
        ]
        self._ai_proposal_profiles = {
            str(profile.get("id") or "").strip().lower(): self._ai_profile_view(profile)
            for profile in profiles
            if str(profile.get("id") or "").strip()
        }
        if hasattr(self, "mcp_server"):
            self.mcp_server._strategy_proposals = [  # type: ignore[attr-defined]
                json_safe_copy(profile) for profile in self._ai_proposal_profiles.values()
            ]
        return list(self._ai_proposal_profiles.values())

    async def get_ai_proposals(self) -> dict[str, Any]:
        profiles = await asyncio.to_thread(self._refresh_ai_profiles_sync)
        return {
            "proposals": profiles,
            "count": len(profiles),
            "ai_bot_factory": self.ai_status()["ai_bot_factory"],
            "research_only": True,
            "enabled": False,
            "real_account_allowed": False,
            "order_execution_allowed": False,
        }

    @staticmethod
    def _registry_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
        profile_id = str(profile.get("id") or "").strip().lower()
        name = str(profile.get("name") or profile.get("display_name") or profile_id).strip()
        symbol = str(profile.get("symbol") or "XAUUSD").strip().upper()
        return {
            "id": profile_id,
            "display_name": name,
            "status": "research_only",
            "enabled": False,
            "symbol": symbol,
            "execution": {"kind": "research_profile", "source": None},
            "research": {
                "kind": "ai_bot_factory",
                "profile": f"TradingAgentsData/strategies/{profile_id}.json",
                "output_directory": "TradingAgentsData/research",
            },
            "safety": {
                "research_only": True,
                "real_account_allowed": False,
                "order_execution_allowed": False,
                "direct_python_orders": False,
            },
        }

    @staticmethod
    def _ai_profile_view(profile: Mapping[str, Any]) -> dict[str, Any]:
        """Expose every AI proposal with an explicit disabled safety contract."""

        safety = dict(profile.get("safety") or {})
        safety.update(
            {
                "research_only": True,
                "real_account_allowed": False,
                "order_execution_allowed": False,
                "direct_python_orders": False,
            }
        )
        return {
            **json_safe_copy(dict(profile)),
            "status": "research_only",
            "enabled": False,
            "research_only": True,
            "real_account_allowed": False,
            "order_execution_allowed": False,
            "safety": safety,
        }

    def _upsert_registry_profiles_sync(self, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        """Optionally expose research profiles in the safe bot registry."""

        if not bool(getattr(self.settings, "ai_bot_factory_registry_enabled", False)) or not profiles:
            return {"enabled": False, "created": 0, "updated": 0, "errors": []}

        import bot_manager

        registry_path = self.settings.registry_path
        if registry_path.exists():
            registry = bot_manager.load_json(registry_path)
        else:
            registry = {
                "schema_version": 1,
                "workspace_name": "New Era Trading Bot Lab",
                "active_bot": None,
                "shared_rules": {
                    "real_account_allowed": False,
                    "new_bots_start_in": "research_only",
                },
                "bots": [],
            }
        bots = registry.get("bots")
        if not isinstance(bots, list):
            raise ValueError("bot registry bots must be a list")
        existing_errors = bot_manager.validate_registry(registry) if registry_path.exists() else []
        if existing_errors:
            raise ValueError("existing bot registry is invalid: " + "; ".join(existing_errors[:5]))

        created = 0
        updated = 0
        for profile in profiles:
            safe_profile = self._registry_profile(profile)
            profile_id = safe_profile["id"]
            profile_name = safe_profile["display_name"].casefold()
            existing = next(
                (
                    item
                    for item in bots
                    if isinstance(item, dict)
                    and (
                        str(item.get("id") or "").strip().lower() == profile_id
                        or str(item.get("display_name") or "").strip().casefold() == profile_name
                    )
                ),
                None,
            )
            if existing is None:
                bots.append(safe_profile)
                created += 1
            else:
                existing_id = str(existing.get("id") or "")
                existing.clear()
                existing.update(safe_profile)
                if str(registry.get("active_bot") or "") == existing_id:
                    registry["active_bot"] = safe_profile["id"]
                updated += 1
        if not registry.get("active_bot"):
            fallback = next(
                (
                    item
                    for item in bots
                    if isinstance(item, dict)
                    and str((item.get("research") or {}).get("kind") or "") != "ai_bot_factory"
                ),
                None,
            )
            if fallback is None:
                return {
                    "enabled": True,
                    "created": 0,
                    "updated": 0,
                    "errors": ["existing registry has no non-AI active bot"],
                }
            registry["active_bot"] = str(fallback.get("id"))
        bot_manager.save_registry(registry, registry_path)
        return {"enabled": True, "created": created, "updated": updated, "errors": []}

    def _persist_ai_proposals_sync(self, proposals: list[dict[str, Any]]) -> dict[str, Any]:
        existing_profiles = self.strategy_lab.list_profiles()
        by_id = {
            str(profile.get("id") or "").strip().lower(): profile
            for profile in existing_profiles
            if str(profile.get("id") or "").strip()
        }
        by_name = {
            str(profile.get("name") or "").strip().casefold(): profile
            for profile in existing_profiles
            if str(profile.get("name") or "").strip()
        }
        saved_profiles: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        deduplicated = 0
        failed = 0
        for index, proposal in enumerate(proposals):
            try:
                normalized = normalize_research_strategy_profile(proposal, index=index)
                proposal_id = str(normalized["id"]).strip().lower()
                name_key = str(normalized["name"]).strip().casefold()
                existing = by_id.get(proposal_id) or by_name.get(name_key)
                if proposal_id in seen_ids or name_key in seen_names:
                    deduplicated += 1
                    records.append({"id": proposal_id, "name": normalized["name"], "saved": False, "deduplicated": True})
                    continue
                seen_ids.add(proposal_id)
                seen_names.add(name_key)
                target_id = str(existing.get("id") or proposal_id).strip().lower() if existing else proposal_id
                normalized["id"] = target_id
                normalized["metadata"] = {
                    **dict(normalized.get("metadata") or {}),
                    "source": "ai_engine",
                    "research_only": True,
                }
                saved = self.strategy_lab.save_profile(normalized, strategy_id=target_id)
                safe_saved = {
                    **saved,
                    "enabled": False,
                    "research_only": True,
                    "real_account_allowed": False,
                    "order_execution_allowed": False,
                }
                saved_profiles.append(safe_saved)
                by_id[target_id] = saved
                by_name[str(saved.get("name") or normalized["name"]).strip().casefold()] = saved
                if existing is not None:
                    deduplicated += 1
                records.append(
                    {
                        "id": str(saved.get("id") or target_id),
                        "name": str(saved.get("name") or normalized["name"]),
                        "saved": True,
                        "deduplicated": existing is not None,
                        "status": "research_only",
                    }
                )
            except Exception as exc:
                failed += 1
                records.append(
                    {
                        "id": str(proposal.get("id") or "")[:64],
                        "name": str(proposal.get("name") or "")[:120],
                        "saved": False,
                        "deduplicated": False,
                        "error": str(exc)[:240] or "proposal persistence failed",
                    }
                )

        try:
            registry_result = self._upsert_registry_profiles_sync(saved_profiles)
        except Exception as exc:
            registry_result = {
                "enabled": bool(getattr(self.settings, "ai_bot_factory_registry_enabled", False)),
                "created": 0,
                "updated": 0,
                "errors": [str(exc)[:240] or "registry update failed"],
            }
        return {
            "validated": len(proposals),
            "persisted": len(saved_profiles),
            "deduplicated": deduplicated,
            "failed": failed,
            "records": records,
            "profiles": saved_profiles,
            "registry": registry_result,
        }

    async def _invoke_ai_engine(self, context: Mapping[str, Any], symbol: str) -> Any:
        method = None
        for name in ("safe_call_async", "analyze_async", "call_async", "safe_call", "analyze", "call"):
            candidate = getattr(self.ai_engine, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            raise RuntimeError("AI engine has no callable analysis method")
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters.values()
            if "symbol" in signature.parameters or any(
                parameter.kind == parameter.VAR_KEYWORD for parameter in parameters
            ):
                kwargs["symbol"] = symbol
        except (TypeError, ValueError):
            kwargs["symbol"] = symbol
        if name.endswith("_async") or inspect.iscoroutinefunction(method):
            result = method(context, **kwargs)
        else:
            result = await asyncio.to_thread(method, context, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _run_ai_analysis(
        self,
        *,
        symbol: str,
        source: str,
        scan_id: str | None = None,
        job: ScanJob | None = None,
        ohlcv: Any = None,
        indicators: Any = None,
        kronos_matrix: Any = None,
        l2_dom: Any = None,
        extra_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol or self.settings.default_symbol).strip().upper()
        self._ai_metrics["analyses_total"] += 1
        bounded_bars = self._bound_ai_value(
            list(ohlcv or [])[-int(getattr(self.settings, "ai_context_max_bars", 32)) :]
            if isinstance(ohlcv, (list, tuple))
            else [],
        )
        bounded_indicators = self._bound_ai_value(indicators or {})
        bounded_kronos = self._bound_ai_value(kronos_matrix or {})
        bounded_l2 = self._bound_ai_value(l2_dom or {})
        extras = dict(extra_context or {})
        extras.update(
            {
                "source": source,
                "scan_id": scan_id,
                "waterfall_step2": bounded_kronos,
            }
        )
        try:
            context = build_scan_context(
                normalized_symbol,
                bounded_bars,
                bounded_indicators,
                bounded_kronos,
                bounded_l2,
                **{str(key): self._bound_ai_value(value) for key, value in extras.items()},
            )
        except Exception as exc:
            context = {
                "symbol": normalized_symbol,
                "ohlcv": [],
                "indicators": {},
                "kronos_matrix": {},
                "l2_dom": {},
                "research_only": True,
            }
            decision = self._blocked_ai_decision(f"context rejected: {exc}", symbol=normalized_symbol)
        else:
            decision: SafeDecision
            raw_result: Any = None
            if not bool(getattr(self.settings, "ai_enabled", True)):
                decision = self._blocked_ai_decision("AI analysis is disabled by configuration", symbol=normalized_symbol)
                self._ai_metrics["analyses_disabled"] += 1
            else:
                try:
                    raw_result = await self._invoke_ai_engine(context, normalized_symbol)
                    if isinstance(raw_result, SafeDecision):
                        # Preserve the engine's explicit BLOCKED/offline
                        # status; re-normalizing an ABSTAIN dict would turn a
                        # fail-closed transport result into a generic
                        # research decision.
                        decision = (
                            raw_result
                            if raw_result.blocked
                            else normalize_model_result(raw_result.to_dict(), symbol=normalized_symbol)
                        )
                    else:
                        decision = normalize_model_result(raw_result, symbol=normalized_symbol)
                except Exception as exc:
                    decision = self._blocked_ai_decision(
                        f"local model unavailable or unsafe: {exc}",
                        symbol=normalized_symbol,
                    )
                    self._ai_metrics["last_error"] = str(exc)[:240]

        if decision.blocked:
            self._ai_metrics["analyses_blocked"] += 1
        else:
            self._ai_metrics["analyses_completed"] += 1
        persistence: dict[str, Any] = {
            "validated": 0,
            "persisted": 0,
            "deduplicated": 0,
            "failed": 0,
            "records": [],
            "registry": {"enabled": False, "created": 0, "updated": 0, "errors": []},
        }
        proposals = [
            self._redact_ai_value(proposal)
            for proposal in list(decision.bot_proposals or [])
            if isinstance(proposal, Mapping)
        ]
        proposals = [
            {
                **dict(proposal),
                "enabled": False,
                "research_only": True,
                "real_account_allowed": False,
                "order_execution_allowed": False,
                "safety": {
                    **dict(proposal.get("safety") or {}),
                    "research_only": True,
                    "real_account_allowed": False,
                    "order_execution_allowed": False,
                    "direct_python_orders": False,
                },
            }
            for proposal in proposals
            if isinstance(proposal, Mapping)
        ]
        self._ai_metrics["proposals_seen"] += len(proposals)
        if proposals:
            try:
                persistence = await asyncio.to_thread(self._persist_ai_proposals_sync, proposals)
                self._ai_metrics["proposals_persisted"] += int(persistence.get("persisted", 0))
                self._ai_metrics["proposals_deduplicated"] += int(persistence.get("deduplicated", 0))
                self._ai_metrics["proposals_failed"] += int(persistence.get("failed", 0))
                registry_result = persistence.get("registry") or {}
                self._ai_metrics["registry_profiles_created"] += int(registry_result.get("created", 0))
                self._ai_metrics["registry_profiles_updated"] += int(registry_result.get("updated", 0))
                for profile in persistence.get("profiles", []):
                    profile_id = str(profile.get("id") or "").strip().lower()
                    if profile_id:
                        self._ai_proposal_profiles[profile_id] = self._ai_profile_view(profile)
                self.mcp_server._strategy_proposals = [  # type: ignore[attr-defined]
                    json_safe_copy(profile) for profile in self._ai_proposal_profiles.values()
                ]
                self._ai_proposal_records.extend(persistence.get("records", []))
                self._ai_proposal_records = self._ai_proposal_records[-200:]
            except Exception as exc:
                persistence = {
                    "validated": len(proposals),
                    "persisted": 0,
                    "deduplicated": 0,
                    "failed": len(proposals),
                    "records": [{"saved": False, "error": str(exc)[:240]}],
                    "registry": {"enabled": False, "created": 0, "updated": 0, "errors": [str(exc)[:240]]},
                }
                self._ai_metrics["proposals_failed"] += len(proposals)
                self._ai_metrics["last_error"] = str(exc)[:240]

        self._ai_metrics["last_analysis_at"] = utc_now().isoformat()
        decision_payload = self._redact_ai_value(decision.to_dict())
        if not isinstance(decision_payload, dict):
            decision_payload = {}
        decision_payload["bot_proposals"] = json_safe_copy(proposals)
        decision_payload.update(
            {
                "research_only": True,
                "executable": False,
                "execution_allowed": False,
                "real_account_allowed": False,
                "order_execution_allowed": False,
            }
        )
        if not bool(getattr(self.settings, "ai_enabled", True)):
            availability = "disabled"
            status = "DISABLED"
        elif decision.blocked:
            availability = "offline" if bool(getattr(self.ai_engine, "offline", False)) else "blocked"
            status = "BLOCKED"
        else:
            availability = "ready"
            status = "RESEARCH_ONLY"
        telemetry = {
            "status": status,
            "availability": availability,
            "enabled": bool(getattr(self.settings, "ai_enabled", True)),
            "model": str(getattr(self.ai_engine, "model", DEFAULT_OLLAMA_MODEL)),
            "source": source,
            "symbol": normalized_symbol,
            "action": decision.action,
            "confidence": decision.confidence,
            "rationale": decision.reasoning_summary,
            "decision": decision_payload,
            "proposal_persistence": persistence,
            "context": {
                "ohlcv_bars": len(bounded_bars) if isinstance(bounded_bars, list) else 0,
                "has_step1_indicators": bool(bounded_indicators),
                "has_step2_kronos": bool(bounded_kronos),
                "has_l2_snapshot": bool(bounded_l2),
            },
            "research_only": True,
            "execution_allowed": False,
            "real_account_allowed": False,
            "order_execution_allowed": False,
        }
        await self._publish(
            event(
                "ai.engine.decision",
                scan_id=scan_id,
                source=source,
                symbol=normalized_symbol,
                status=status,
                availability=availability,
                action=decision.action,
                confidence=decision.confidence,
                rationale=decision.reasoning_summary,
                proposal_count=len(proposals),
                persisted_proposal_count=int(persistence.get("persisted", 0)),
                research_only=True,
                execution_allowed=False,
            ),
            job,
        )
        return telemetry

    async def analyze_ai(
        self,
        symbol: str = "XAUUSD",
        *,
        context: Mapping[str, Any] | None = None,
        source: str = "manual",
        scan_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one bounded, read-only local AI analysis."""

        normalized_symbol = str(symbol or self.settings.default_symbol).strip().upper()
        supplied = dict(context or {})
        bars = supplied.get("ohlcv", supplied.get("bars", []))
        indicators = supplied.get("indicators", {})
        kronos = supplied.get("kronos_matrix", supplied.get("waterfall_step2", {}))
        l2 = supplied.get("l2_dom", supplied.get("l2_orderflow", {}))
        extras = {
            key: value
            for key, value in supplied.items()
            if key not in {"symbol", "ohlcv", "bars", "indicators", "kronos_matrix", "waterfall_step2", "l2_dom", "l2_orderflow"}
        }
        if not bars:
            market_csv = self.settings.root / "TradingAgentsData" / "market" / f"{_safe_symbol_filename(normalized_symbol)}_m5.csv"
            bars = await asyncio.to_thread(
                self._read_latest_ohlcv_csv,
                market_csv,
                int(getattr(self.settings, "ai_context_max_bars", 32)),
            )
        if not indicators:
            market_csv = self.settings.root / "TradingAgentsData" / "market" / f"{_safe_symbol_filename(normalized_symbol)}_m5.csv"
            mt5_meta: dict[str, Any] = {}
            try:
                mt5_meta = await asyncio.to_thread(get_mt5_symbol_snapshot, normalized_symbol)
            except Exception:
                pass
            step1 = await asyncio.to_thread(
                TALibTechnicalEngine.evaluate_from_csv,
                normalized_symbol,
                market_csv,
                fallback_close=float(mt5_meta.get("last_close") or 0.0),
                fallback_atr=float(mt5_meta.get("atr_m5") or 0.0),
            )
            indicators = step1.to_dict()
        if not l2:
            l2 = self._latest_l2.get(normalized_symbol) or {}
        if not kronos:
            waterfall = self._read_json(self.waterfall_report_path) or {}
            kronos = self._waterfall_step2(waterfall)
            if not kronos:
                latest_kronos = self._read_json(self.settings.report_dir / "kronos_matrix_latest.json") or {}
                kronos = latest_kronos.get("decision", latest_kronos)
        return await self._run_ai_analysis(
            symbol=normalized_symbol,
            source=source,
            scan_id=scan_id,
            ohlcv=bars,
            indicators=indicators,
            kronos_matrix=kronos,
            l2_dom=l2,
            extra_context=extras,
        )

    async def handle_mcp(self, request: Mapping[str, Any] | str) -> dict[str, Any] | None:
        """Dispatch one local read-only MCP JSON-RPC request."""

        return await self.mcp_server.handle_async(request)

    def update_controls(self, updates: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        self.controls.update(updates, **kwargs)
        snapshot = self.controls.snapshot()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.bus.publish(
                    event(
                        "controls.changed",
                        source="runtime-controls",
                        controls=snapshot,
                        message=(
                            "Runtime decision controls updated"
                            if snapshot["override_active"]
                            else "Runtime decision controls restored to safe defaults"
                        ),
                    )
                )
            )
        except RuntimeError:
            pass
        return snapshot

    def decision_history(self, limit: int | None = None) -> dict[str, Any]:
        return {"records": self.decision_logger.read_records(limit)}

    def decision_history_summary(self) -> dict[str, Any]:
        return self.decision_logger.summary()

    @staticmethod
    def _decision_atr(decision: Any, fallback: Any = 0.0) -> float:
        """Extract the ATR used by the waterfall for bridge-side geometry checks."""
        step1 = getattr(decision, "step1_talib", {})
        if not isinstance(step1, dict):
            step1 = {}
        try:
            value = float(step1.get("atr_14") or fallback or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0.0 else 0.0

    @staticmethod
    def _shadow_trade_payload(
        decision: Any,
        *,
        symbol: str,
        origin: str,
    ) -> dict[str, Any]:
        step2 = getattr(decision, "step2_kronos_matrix", {})
        comment = build_joey_profile_comment(symbol, step2 if isinstance(step2, dict) else None)
        return {
            "virtual": True,
            "execution_mode": "SHADOW",
            "execution_source": "shadow_mode",
            "symbol": str(symbol).strip().upper(),
            "side": str(getattr(decision, "final_action", "ABSTAIN") or "ABSTAIN").upper(),
            "volume": float(getattr(decision, "recommended_lot", 0.0) or 0.0),
            "sl": float(getattr(decision, "sl_price", 0.0) or 0.0),
            "tp": float(getattr(decision, "tp_price", 0.0) or 0.0),
            "win_probability_pct": float(getattr(decision, "win_probability_pct", 0.0) or 0.0),
            "risk_pct": float(getattr(decision, "risk_pct", 0.0) or 0.0),
            "comment": comment,
            "confluence": (step2 or {}).get("confluence_label", "0-of-3") if isinstance(step2, dict) else "0-of-3",
            "origin": origin,
            "logged_at": utc_now().isoformat(),
        }

    async def MarketBookGet(
        self,
        symbol: str = "XAUUSD",
        levels: int = 10,
        *,
        publish_events: bool = True,
    ) -> dict[str, Any]:
        """Read live L2 DOM (`mt5.market_book_get` or Crypto L2 WebSocket) and run `OrderflowLiquidityAgent`."""
        norm_symbol = symbol.strip().upper() or "XAUUSD"
        analysis: OrderflowAnalysis = await self.orderflow.fetch_l2_snapshot(norm_symbol, levels=levels)
        payload = analysis.to_dict()
        self._latest_l2[norm_symbol] = payload

        try:
            self.l2_report_path.parent.mkdir(parents=True, exist_ok=True)
            self.l2_report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        if publish_events:
            await self.bus.publish(
                event(
                    "l2.snapshot",
                    symbol=norm_symbol,
                    source=analysis.source,
                    l2_direction=analysis.l2_direction,
                    imbalance_ratio=analysis.imbalance_ratio,
                    bid_pressure_pct=analysis.bid_pressure_pct,
                    ask_pressure_pct=analysis.ask_pressure_pct,
                    confluence_score=analysis.confluence_score,
                    veto_buy=analysis.veto_buy,
                    veto_sell=analysis.veto_sell,
                    veto_reason=analysis.veto_reason,
                )
            )
            for alert_item in analysis.alerts[:3]:
                await self.bus.publish(
                    event(
                        "l2.alert",
                        source="orderflow-agent",
                        symbol=norm_symbol,
                        kind=alert_item.get("kind"),
                        severity=alert_item.get("severity"),
                        message=alert_item.get("message"),
                    )
                )
        return payload

    def _powershell(self) -> str:
        configured = shutil.which(self.settings.process_shell)
        if configured:
            return configured
        fallback = shutil.which("pwsh")
        if fallback:
            return fallback
        return self.settings.process_shell

    def _has_tradingagents_api_key(self) -> bool:
        load_workspace_dotenv(self.settings.root)
        for key in LLM_ENV_KEYS:
            val = os.getenv(key, "").strip()
            if val and not val.startswith("your_"):
                return True
        for env_file in (self.settings.root / ".env", self.settings.root / "TradingAgents" / ".env"):
            if env_file.exists():
                try:
                    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k in LLM_ENV_KEYS and v and not v.startswith("your_"):
                            return True
                except OSError:
                    pass
        return False

    async def _publish(self, message: dict[str, Any], job: ScanJob | None = None) -> None:
        if job is not None:
            job.last_event = message
        await self.bus.publish(message)

    def _reset_gate(self, path: Path, *, strategy: str, symbol: str, ttl_minutes: int) -> None:
        write_gate_csv(
            path,
            symbol=symbol,
            action="ABSTAIN",
            confidence=0.0,
            generated_epoch=int(time.time()),
            expires_epoch=int(time.time()) + max(60, ttl_minutes * 60),
            strategy=strategy,
        )

    async def start_scan(self, request: dict[str, Any], mode: str) -> ScanJob:
        async with self._lock:
            if self._active_scan_id is not None:
                raise RuntimeError(f"scan already running: {self._active_scan_id}")
            scan_id = str(uuid.uuid4())
            job = ScanJob(scan_id=scan_id, mode=mode)
            self.jobs[scan_id] = job
            self._active_scan_id = scan_id
            job.task = asyncio.create_task(self._run_scan(job, request))
            return job

    async def _sync_mt5_bars_for_symbol(
        self,
        job: ScanJob,
        symbol: str,
        request: dict[str, Any],
    ) -> tuple[Path, float | None, float | None, dict[str, Any] | None]:
        """Pull latest closed M5/M15 bars from MetaTrader 5 (or Binance for Crypto if MT5 lacks pair)."""
        default_csv = (
            self.settings.root
            / "TradingAgentsData"
            / "market"
            / f"{_safe_symbol_filename(symbol)}_m5.csv"
        )
        explicit_csv = request.get("input_csv")
        target_csv = self.settings.resolve_workspace_path(explicit_csv, default=default_csv)
        spread_points: float | None = (
            float(request["spread_points"]) if request.get("spread_points") is not None else None
        )
        point_val: float | None = None

        if explicit_csv:
            return target_csv, spread_points, point_val, None

        await self._publish(
            event(
                "kronos.forecast",
                scan_id=job.scan_id,
                source="mt5",
                message=f"[MT5 Direct API] Fetching closed M5/M15 candles for {symbol}...",
            ),
            job,
        )
        try:
            mt5_meta = await asyncio.to_thread(
                fetch_mt5_bars_to_csv,
                symbol,
                target_csv,
                timeframe="M5",
                bars=5000,
            )
            if spread_points is None and mt5_meta.get("spread_points") is not None:
                spread_points = float(mt5_meta["spread_points"])
            if mt5_meta.get("point") is not None:
                point_val = float(mt5_meta["point"])
            await self._publish(
                event(
                    "kronos.forecast",
                    scan_id=job.scan_id,
                    source="mt5",
                    message=(
                        f"[MT5 Direct API] Synced {mt5_meta.get('bars_exported', 5000)} closed M5 bars for "
                        f"{mt5_meta.get('resolved_symbol', symbol)} | Close={mt5_meta.get('last_close')} | "
                        f"Spread={mt5_meta.get('spread_points')} pts | ATR(14)={float(mt5_meta.get('atr_m5', 0.0)):.4f}"
                    ),
                ),
                job,
            )
            return target_csv, spread_points, point_val, mt5_meta
        except Exception as mt5_exc:
            # If symbol is a Crypto pair (BTCUSD, ETHUSD, etc.), fall back to Binance klines
            crypto_pair = resolve_crypto_pair(symbol)
            if crypto_pair is not None:
                try:
                    crypto_meta = await asyncio.to_thread(
                        _fetch_binance_klines_to_csv,
                        crypto_pair,
                        target_csv,
                        "5m",
                        1000,
                    )
                    if spread_points is None:
                        spread_points = float(crypto_meta.get("spread_points", 5.0))
                    point_val = float(crypto_meta.get("point", 0.01))
                    await self._publish(
                        event(
                            "kronos.forecast",
                            scan_id=job.scan_id,
                            source="crypto-ws",
                            message=(
                                f"[Crypto API] Synced {crypto_meta.get('bars_exported')} closed M5 bars for "
                                f"{crypto_pair} | Close={crypto_meta.get('last_close')}"
                            ),
                        ),
                        job,
                    )
                    return target_csv, spread_points, point_val, crypto_meta
                except Exception:
                    pass

            await self._publish(
                event(
                    "process.log",
                    scan_id=job.scan_id,
                    source="mt5",
                    message=f"[MT5 Direct API] Live MT5 bar fetch skipped ({mt5_exc}); using cached CSV if present.",
                ),
                job,
            )
            return target_csv, spread_points, point_val, None

    @staticmethod
    def _decision_outcome(
        waterfall: dict[str, Any] | None,
        *,
        autonomous_mode: bool,
        paused: bool,
        autonomous_trade: dict[str, Any] | None = None,
        shadow_trade: dict[str, Any] | None = None,
        gemini_review: dict[str, Any] | None = None,
        symbol_in_open_positions: bool = False,
    ) -> dict[str, str]:
        """Derive telemetry mode and a stable block reason from one outcome."""

        wf = waterfall if isinstance(waterfall, dict) else {}
        final_action = str(wf.get("final_action") or "ABSTAIN").upper()
        rationale = str(wf.get("rationale_ka") or wf.get("reason") or "").strip()
        if autonomous_trade:
            return {
                "execution_mode": "LIVE",
                "block_reason": "",
                "reason": "Demo-only autonomous execution completed; real-account execution remains hard blocked.",
                "final_decision": final_action,
            }
        if shadow_trade:
            return {
                "execution_mode": "SHADOW",
                "block_reason": "symbol_shadow_allowlist",
                "reason": "Signal recorded in SHADOW mode because the symbol is outside the demo auto-execution allowlist.",
                "final_decision": final_action,
            }
        if final_action not in {"BUY", "SELL"}:
            return {
                "execution_mode": "BLOCKED",
                "block_reason": "waterfall_abstain",
                "reason": rationale or "Waterfall returned ABSTAIN; no order was eligible.",
                "final_decision": "ABSTAIN",
            }
        if paused:
            reason = "Trading is paused; no demo order was sent."
            block_reason = "paused"
        elif not autonomous_mode:
            reason = "Autonomous mode is disabled; signal was evaluated but no demo order was sent."
            block_reason = "autonomous_mode_disabled"
        elif not bool(wf.get("research_evidence_valid", False)):
            reason = "Research evidence was invalid or incomplete; execution was blocked."
            block_reason = "research_evidence_invalid"
        elif float(wf.get("win_probability_pct") or 0.0) < AUTO_SCREENER_MIN_WIN_PROB:
            reason = f"Win probability is below the {AUTO_SCREENER_MIN_WIN_PROB:.0f}% execution threshold."
            block_reason = "below_win_probability"
        elif float(wf.get("recommended_lot") or 0.0) < 0.01:
            reason = "Recommended lot is below the broker minimum/risk budget."
            block_reason = "minimum_lot_or_risk_cap"
        elif symbol_in_open_positions:
            reason = f"{str(wf.get('symbol') or 'Symbol')} already has an open position."
            block_reason = "position_already_open"
        elif gemini_review and str(gemini_review.get("verdict") or "").upper() not in {"APPROVE", "BYPASSED"}:
            reason = str(gemini_review.get("reason_ka") or "Gemini CIO vetoed the demo signal.")
            block_reason = "gemini_cio_veto"
        else:
            reason = "Signal was not eligible for a demo execution under the current safety profile."
            block_reason = "execution_guard"
        return {
            "execution_mode": "BLOCKED",
            "block_reason": block_reason,
            "reason": reason,
            "final_decision": final_action,
        }

    async def _record_decision(
        self,
        *,
        scan_id: str | None,
        cycle: str | None,
        symbol: str,
        waterfall: dict[str, Any] | None,
        signal: str | None,
        outcome: dict[str, str],
        source: str,
        controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.decision_logger.record_decision(
            scan_id=scan_id,
            cycle=cycle,
            symbol=symbol,
            signal=signal,
            action=str((waterfall or {}).get("final_action") or outcome.get("final_decision") or "ABSTAIN"),
            final_decision=outcome.get("final_decision") or "ABSTAIN",
            execution_mode=outcome.get("execution_mode") or "BLOCKED",
            reason=outcome.get("reason") or "decision recorded",
            block_reason=outcome.get("block_reason") or "",
            waterfall=waterfall,
            metadata={
                "source": source,
                "controls": controls or self.controls.execution_values(),
                "real_account_allowed": False,
                "demo_only": True,
            },
        )

    async def _run_scan(self, job: ScanJob, request: dict[str, Any]) -> None:
        job.status = "running"
        job.started_at = utc_now()
        symbol = str(request.get("symbol") or self.settings.default_symbol).upper()
        ttl_minutes = int(request.get("gate_ttl_minutes") or 90)
        controls_snapshot = self.controls.execution_values()
        include_tradingagents = bool(request.get("include_tradingagents", True))
        include_kronos = bool(request.get("include_kronos", True)) and bool(controls_snapshot["kronos_enabled"])
        phase_results: list[dict[str, Any]] = []
        mt5_meta: dict[str, Any] | None = None
        l2_snapshot: dict[str, Any] | None = None
        telemetry_recorded = False
        try:
            await self._publish(event("scan.started", scan_id=job.scan_id, symbol=symbol, mode=job.mode), job)
            if not include_tradingagents and not include_kronos:
                raise ValueError("at least one research phase must be enabled")

            # Phase 0: Capture L2 Orderbook & Liquidity Intelligence (`MarketBookGet`)
            try:
                if not bool(controls_snapshot["l2_enabled"]):
                    await self._publish(
                        event(
                            "process.log",
                            scan_id=job.scan_id,
                            source="orderflow-agent",
                            message="[L2 Orderflow Radar] Bypassed by runtime controls.",
                        ),
                        job,
                    )
                else:
                    l2_snapshot = await self.MarketBookGet(symbol, levels=10, publish_events=True)
                if l2_snapshot is not None:
                    await self._publish(
                        event(
                            "kronos.forecast",
                            scan_id=job.scan_id,
                            source="orderflow-agent",
                            message=(
                                f"[L2 Orderflow Radar] {symbol} ({l2_snapshot.get('source')}) -> "
                                f"Bid={l2_snapshot.get('bid_pressure_pct')}% / Ask={l2_snapshot.get('ask_pressure_pct')}% | "
                                f"L2 Dir={l2_snapshot.get('l2_direction')} | Score={l2_snapshot.get('confluence_score')}%"
                            ),
                        ),
                        job,
                    )
            except Exception as l2_exc:
                await self._publish(
                    event(
                        "process.log",
                        scan_id=job.scan_id,
                        source="orderflow-agent",
                        message=f"[L2 Orderflow Radar] Snapshot skipped: {l2_exc}",
                    ),
                    job,
                )

            if include_tradingagents:
                self._reset_gate(
                    self.settings.ai_gate_path,
                    strategy="new-era-gold-council-v1",
                    symbol=symbol,
                    ttl_minutes=ttl_minutes,
                )
            if include_kronos:
                self._reset_gate(
                    self.settings.kronos_gate_path,
                    strategy=STRATEGY_ID,
                    symbol=symbol,
                    ttl_minutes=ttl_minutes,
                )

            if include_tradingagents:
                has_llm_key = self._has_tradingagents_api_key()
                ta_args = [
                    "-Ticker",
                    symbol,
                    "-GateTtlMinutes",
                    str(ttl_minutes),
                ]
                provider = request.get("provider") or self.settings.default_provider
                if provider:
                    ta_args.extend(["-Provider", str(provider)])
                if not has_llm_key:
                    ta_args.append("-DryRun")
                    await self._publish(
                        event(
                            "tradingagents.debate",
                            scan_id=job.scan_id,
                            source="tradingagents",
                            message=(
                                "TradingAgents LLM API key not configured in .env — "
                                "running safe deterministic dry-run gate while Kronos + Matrix + L2 Orderflow analyze live data."
                            ),
                        ),
                        job,
                    )
                else:
                    await self._publish(
                        event(
                            "tradingagents.debate",
                            scan_id=job.scan_id,
                            source="tradingagents",
                            message=(
                                f"[TradingAgents LLM Council] Synced API key from root .env (Provider={provider or 'google'}) — "
                                f"launching Multi-Agent Bull vs. Bear Debate for {symbol}..."
                            ),
                        ),
                            job,
                        )
                ta_res = await self._run_powershell_phase(
                    job,
                    "tradingagents",
                    self.settings.tradingagents_runner,
                    ta_args,
                    request,
                )
                if ta_res.get("return_code") != 0 and "-DryRun" not in ta_args:
                    initial_ta_return_code = ta_res.get("return_code")
                    fallback_ta_res = await self._run_powershell_phase(
                        job,
                        "tradingagents",
                        self.settings.tradingagents_runner,
                        [*ta_args, "-DryRun"],
                        request,
                    )
                    # Keep the initial failure attached to the phase result;
                    # a safe dry-run fallback must not unlock a stale BUY/SELL.
                    fallback_ta_res["fallback_after_failure"] = True
                    fallback_ta_res["initial_return_code"] = initial_ta_return_code
                    fallback_ta_res["fallback_return_code"] = fallback_ta_res.get("return_code")
                    fallback_ta_res["return_code"] = initial_ta_return_code
                    ta_res = fallback_ta_res
                phase_results.append(ta_res)

            if include_kronos:
                input_path, spread_points, point_val, mt5_meta = await self._sync_mt5_bars_for_symbol(
                    job, symbol, request
                )
                kronos_args = [
                    "-Ticker",
                    symbol,
                    "-InputCsv",
                    str(input_path),
                    "-GateTtlMinutes",
                    str(ttl_minutes),
                    "-MaxSpreadPoints",
                    str(self.settings.max_spread_points),
                    "-MaxSpreadToAtr",
                    str(self.settings.max_spread_to_atr),
                ]
                if spread_points is not None:
                    kronos_args.extend(["-SpreadPoints", str(spread_points)])
                if point_val is not None and point_val > 0:
                    kronos_args.extend(["-Point", str(point_val)])
                phase_results.append(
                    await self._run_powershell_phase(
                        job,
                        "kronos",
                        self.settings.kronos_runner,
                        kronos_args,
                        request,
                    )
                )

            successful_sources = {item["source"] for item in phase_results if item["return_code"] == 0}
            if include_tradingagents and include_kronos:
                combine_args = [
                    str(self.settings.combine_runner),
                    "--ai-gate",
                    str(self.settings.ai_gate_path),
                    "--kronos-gate",
                    str(self.settings.kronos_gate_path),
                    "--output",
                    str(self.settings.kronos_gate_path),
                    "--report",
                    str(self.settings.combined_report_path),
                    "--symbol",
                    symbol,
                ]
                phase_results.append(
                    await self._run_python_phase(job, "gate-combiner", combine_args, request)
                )
            elif include_kronos and "kronos" not in successful_sources:
                self._reset_gate(
                    self.settings.kronos_gate_path,
                    strategy=STRATEGY_ID,
                    symbol=symbol,
                    ttl_minutes=ttl_minutes,
                )

            result = await self._build_result(
                job,
                symbol,
                phase_results,
                mt5_meta=mt5_meta,
                l2_snapshot=l2_snapshot,
                ttl_minutes=ttl_minutes,
                controls=controls_snapshot,
            )
            job.result = result
            job.return_code = max((int(item["return_code"]) for item in phase_results), default=0)
            job.status = "completed" if job.return_code == 0 else "failed"
            if job.status == "failed":
                job.error = "one or more research phases stopped safely; gate remains ABSTAIN"
            waterfall = result.get("waterfall") if isinstance(result, dict) else None
            step2 = waterfall.get("steps", {}).get("step2_kronos_matrix", {}) if isinstance(waterfall, dict) else {}
            outcome = self._decision_outcome(
                waterfall,
                autonomous_mode=self.gemini_cio.autonomous_mode,
                paused=self.paused,
                autonomous_trade=result.get("autonomous_trade") if isinstance(result, dict) else None,
                shadow_trade=result.get("shadow_trade") if isinstance(result, dict) else None,
                gemini_review=result.get("gemini_review") if isinstance(result, dict) else None,
            )
            decision_record = await self._record_decision(
                scan_id=job.scan_id,
                cycle=None,
                symbol=symbol,
                waterfall=waterfall if isinstance(waterfall, dict) else None,
                signal=step2.get("candidate_direction") if isinstance(step2, dict) else None,
                outcome=outcome,
                source="scan",
                controls=controls_snapshot,
            )
            result["decision_record"] = decision_record
            telemetry_recorded = True
            await self._publish(
                event("scan.completed", scan_id=job.scan_id, status=job.status, result=result),
                job,
            )
        except asyncio.CancelledError:
            job.status = "failed"
            job.error = "scan task cancelled"
            await self._publish(event("scan.cancelled", scan_id=job.scan_id), job)
            raise
        except Exception as exc:
            job.status = "failed"
            job.return_code = 2
            job.error = str(exc)
            await self._publish(event("scan.failed", scan_id=job.scan_id, error=str(exc)), job)
        finally:
            if not telemetry_recorded:
                try:
                    fallback_waterfall = (
                        job.result.get("waterfall")
                        if isinstance(job.result, dict) and isinstance(job.result.get("waterfall"), dict)
                        else None
                    )
                    fallback_outcome = self._decision_outcome(
                        fallback_waterfall,
                        autonomous_mode=self.gemini_cio.autonomous_mode,
                        paused=self.paused,
                    ) if fallback_waterfall else {
                        "execution_mode": "BLOCKED",
                        "block_reason": "scan_error",
                        "reason": f"Scan failed before a waterfall decision: {job.error or 'unknown error'}",
                        "final_decision": "ABSTAIN",
                    }
                    await self._record_decision(
                        scan_id=job.scan_id,
                        cycle=None,
                        symbol=symbol,
                        waterfall=fallback_waterfall,
                        signal=None,
                        outcome=fallback_outcome,
                        source="scan",
                        controls=controls_snapshot,
                    )
                except Exception:
                    pass
            job.finished_at = utc_now()
            self._active_scan_id = None

    async def _run_powershell_phase(
        self,
        job: ScanJob,
        source: str,
        script: Path,
        script_args: list[str],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        command = [
            self._powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *script_args,
        ]
        return await self._run_command(job, source, command, request)

    async def _run_python_phase(
        self,
        job: ScanJob,
        source: str,
        script_args: list[str],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._run_command(job, source, [sys.executable, *script_args], request)

    async def _run_command(
        self,
        job: ScanJob,
        source: str,
        command: list[str],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timeout = int(request.get("timeout_seconds") or self.settings.scan_timeout_seconds)
        await self._publish(
            event("process.started", scan_id=job.scan_id, source=source, command=self._safe_command(command)),
            job,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.settings.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            await self._publish(event("process.failed", scan_id=job.scan_id, source=source, error=str(exc)), job)
            return {"source": source, "return_code": 2, "error": str(exc)}

        async def consume() -> None:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if any(
                    noisy in line
                    for noisy in ("CategoryInfo", "FullyQualifiedErrorId", "At C:\\", "+ ~~~~")
                ):
                    continue
                event_type = self._line_event_type(source, line)
                await self._publish(
                    event(event_type, scan_id=job.scan_id, source=source, message=line),
                    job,
                )

        try:
            await asyncio.wait_for(consume(), timeout=timeout)
            return_code = await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            message = f"{source} exceeded timeout of {timeout} seconds"
            await self._publish(event("process.timeout", scan_id=job.scan_id, source=source, message=message), job)
            return {"source": source, "return_code": 2, "error": message}
        except Exception as exc:
            process.kill()
            await process.wait()
            await self._publish(event("process.failed", scan_id=job.scan_id, source=source, error=str(exc)), job)
            return {"source": source, "return_code": 2, "error": str(exc)}

        await self._publish(
            event("process.finished", scan_id=job.scan_id, source=source, return_code=return_code),
            job,
        )
        return {"source": source, "return_code": return_code}

    @staticmethod
    def _safe_command(command: Iterable[str]) -> list[str]:
        return [str(item) for item in command]

    @staticmethod
    def _line_event_type(source: str, line: str) -> str:
        lower = line.lower()
        if source == "tradingagents" and any(word in lower for word in ("debate", "bull", "bear", "research manager")):
            return "tradingagents.debate"
        if source == "kronos" and any(word in lower for word in ("forecast", "kronos", "matrix", "analog")):
            return "kronos.forecast"
        if "gate" in lower or "abstain" in lower or "buy" in lower or "sell" in lower:
            return "gate.changed"
        return "process.log"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _invalid_evidence_marker(symbol: str, reason: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "_evidence_invalid": True,
            "safe_failure": True,
            "reason": reason,
        }

    @staticmethod
    def _phase_return_code(phase: dict[str, Any]) -> int:
        if phase.get("fallback_after_failure"):
            return 2
        try:
            value = phase.get("return_code")
            return int(value) if value is not None else 2
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _report_generated_epochs(report: dict[str, Any]) -> list[float]:
        values: list[float] = []
        for key in ("generated_epoch", "generated_at", "created_at", "timestamp", "updated_at"):
            if key in report and report.get(key) not in (None, ""):
                parsed = GeminiPortfolioManager._coerce_report_timestamp(report.get(key))
                if parsed is not None:
                    values.append(parsed)
        return values

    @classmethod
    def _report_has_required_evidence(cls, report: dict[str, Any], source: str) -> bool:
        if source == "kronos":
            decision = report.get("decision")
            if not isinstance(decision, dict):
                return False
            return all(isinstance(decision.get(key), dict) for key in ("matrix", "analog", "forecast"))
        if source == "tradingagents":
            return any(key in report for key in ("ai_action", "consensus_action", "mt5_gate_action", "action"))
        return True

    def _read_current_research_report(
        self,
        path: Path,
        *,
        symbol: str,
        source: str,
        minimum_generated_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Read only symbol-bound, fresh, structurally complete research evidence."""

        raw = self._read_json(path)
        if raw is None:
            return self._invalid_evidence_marker(symbol, f"{source} report is missing")
        valid, reason = GeminiPortfolioManager._validate_research_report(
            raw,
            symbol,
            require_symbol=True,
        )
        if not valid:
            return self._invalid_evidence_marker(symbol, reason)
        if not self._report_has_required_evidence(raw, source):
            return self._invalid_evidence_marker(symbol, f"{source} report is structurally incomplete")
        if not any(
            key in raw and raw.get(key) not in (None, "")
            for key in (
                "generated_epoch",
                "generated_at",
                "created_at",
                "timestamp",
                "updated_at",
                "expires_epoch",
                "mt5_gate_expires_epoch",
                "expires_at",
            )
        ):
            return self._invalid_evidence_marker(symbol, f"{source} report has no freshness metadata")
        if minimum_generated_epoch is not None:
            generated = self._report_generated_epochs(raw)
            if not generated or any(value < minimum_generated_epoch - 1.0 for value in generated):
                return self._invalid_evidence_marker(symbol, f"{source} report predates the current scan")
        return raw

    def _load_phase_research_report(
        self,
        phases: list[dict[str, Any]],
        *,
        path: Path,
        symbol: str,
        source: str,
        minimum_generated_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Bind a report to a successful phase so failed scans cannot reuse it."""

        source_phases = [item for item in phases if str(item.get("source") or "") == source]
        if not source_phases:
            # The phase was disabled; do not read an older artifact from disk.
            return None
        if any(self._phase_return_code(item) != 0 for item in phases):
            return self._invalid_evidence_marker(symbol, f"{source} scan phase failed")
        return self._read_current_research_report(
            path,
            symbol=symbol,
            source=source,
            minimum_generated_epoch=minimum_generated_epoch,
        )

    @staticmethod
    def _read_gate(
        path: Path,
        *,
        expected_strategy: str | None = None,
        expected_symbol: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            with path.open("r", newline="", encoding="ascii") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error):
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        required = {"version", "strategy", "symbol", "action", "confidence", "generated_epoch", "expires_epoch"}
        if not required.issubset(row):
            return None
        if row.get("version") != "1":
            return None
        if expected_strategy is not None and row.get("strategy") != expected_strategy:
            return None
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if not row_symbol:
            return None
        if expected_symbol is not None and row_symbol != expected_symbol.strip().upper():
            return None
        action = str(row.get("action") or "").strip().upper()
        if action not in {"BUY", "SELL", "ABSTAIN"}:
            return None
        try:
            confidence = float(row.get("confidence") or 0.0)
            generated_epoch = int(row.get("generated_epoch") or 0)
            expires_epoch = int(row.get("expires_epoch") or 0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return None
        now_epoch = int(time.time())
        if generated_epoch <= 0 or expires_epoch <= generated_epoch or expires_epoch <= now_epoch:
            return None
        if generated_epoch > now_epoch + 60:
            return None
        row["symbol"] = row_symbol
        row["action"] = action
        return row

    async def _build_result(
        self,
        job: ScanJob,
        symbol: str,
        phases: list[dict[str, Any]],
        *,
        mt5_meta: dict[str, Any] | None = None,
        l2_snapshot: dict[str, Any] | None = None,
        ttl_minutes: int = 90,
        controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate = self._read_gate(
            self.settings.kronos_gate_path,
            expected_strategy=STRATEGY_ID,
            expected_symbol=symbol,
        ) or self._read_gate(
            self.settings.ai_gate_path,
            expected_strategy="new-era-gold-council-v1",
            expected_symbol=symbol,
        )
        scan_started_epoch = job.started_at.timestamp() if job.started_at is not None else None
        kronos_report = self._load_phase_research_report(
            phases,
            path=self.settings.report_dir / "kronos_matrix_latest.json",
            symbol=symbol,
            source="kronos",
            minimum_generated_epoch=scan_started_epoch,
        )
        trading_report = self._load_phase_research_report(
            phases,
            path=self.settings.report_dir / "tradingagents_latest.json",
            symbol=symbol,
            source="tradingagents",
            minimum_generated_epoch=scan_started_epoch,
        )
        combined_report = self._read_json(self.settings.combined_report_path) or {}

        # -------------------------------------------------------------------
        # WATERFALL DECISION PIPELINE & VETO HIERARCHY (Steps 1 -> 2 -> 3 -> 4)
        # -------------------------------------------------------------------
        market_csv = (
            Path(mt5_meta["output_csv"])
            if (isinstance(mt5_meta, dict) and mt5_meta.get("output_csv"))
            else (self.settings.root / "TradingAgentsData" / "market" / f"{_safe_symbol_filename(symbol)}_m5.csv")
        )
        fallback_px = float((mt5_meta or {}).get("last_close") or (l2_snapshot or {}).get("mid_price") or 0.0)
        fallback_atr = float((mt5_meta or {}).get("atr_m5") or 0.0)

        # Step 1: TA-Lib Fast Technical Filter (RSI / ATR / EMA 20-50-200 / MACD / ADX)
        step1_talib: Step1TALibResult = await asyncio.to_thread(
            TALibTechnicalEngine.evaluate_from_csv,
            symbol,
            market_csv,
            fallback_close=fallback_px,
            fallback_atr=fallback_atr,
        )
        await self._publish(
            event("kronos.forecast", scan_id=job.scan_id, source="talib-step1", message=step1_talib.summary),
            job,
        )

        # Fetch live MT5 Demo Account Equity for Step 4 Dynamic Risk Sizing
        account_equity = 1000.0
        try:
            mt5_st = await asyncio.to_thread(get_mt5_status, 15)
            acc_obj = mt5_st.get("account") if isinstance(mt5_st, dict) else None
            if isinstance(acc_obj, dict) and float(acc_obj.get("equity") or 0.0) > 0:
                account_equity = float(acc_obj["equity"])
        except Exception:
            pass

        # Steps 2, 3 & 4: Kronos+Matrix -> L2 DOM Safety Veto -> Gemini CIO ("ჯოი") Synthesis
        waterfall_decision = self.gemini_cio.evaluate_waterfall_pipeline(
            symbol=symbol,
            step1_talib=step1_talib,
            kronos_report=kronos_report,
            tradingagents_report=trading_report,
            l2_snapshot=l2_snapshot,
            mt5_meta=mt5_meta,
            account_equity=account_equity,
            market_csv=market_csv,
            controls=controls or self.controls.execution_values(),
        )
        waterfall_dict = waterfall_decision.to_dict()
        try:
            self.waterfall_report_path.parent.mkdir(parents=True, exist_ok=True)
            self.waterfall_report_path.write_text(
                json.dumps(waterfall_dict, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

        await self._publish(
            event(
                "tradingagents.debate",
                scan_id=job.scan_id,
                source="gemini-cio-joey",
                message=waterfall_decision.rationale_ka,
            ),
            job,
        )

        # Sync final Waterfall Gate into CSV & Combined Report so modules never deadlock each other
        final_act = waterfall_decision.final_action
        final_conf = round(waterfall_decision.win_probability_pct / 100.0, 4)
        write_gate_csv(
            self.settings.kronos_gate_path,
            symbol=symbol,
            action=final_act,
            confidence=final_conf if final_act in {"BUY", "SELL"} else 0.0,
            generated_epoch=int(time.time()),
            expires_epoch=int(time.time()) + max(60, ttl_minutes * 60),
            strategy=STRATEGY_ID,
        )
        gate = self._read_gate(
            self.settings.kronos_gate_path,
            expected_strategy=STRATEGY_ID,
            expected_symbol=symbol,
        )

        combined_report["symbol"] = symbol
        combined_report["action"] = final_act
        combined_report["confidence"] = final_conf
        combined_report["waterfall"] = waterfall_dict
        combined_report["scan_id"] = job.scan_id
        try:
            self.settings.combined_report_path.write_text(
                json.dumps(combined_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

        # 100% Autonomous Mode: quant signal -> live Gemini CIO review -> Demo execution
        autonomous_trade_result: dict[str, Any] | None = None
        shadow_trade_result: dict[str, Any] | None = None
        gemini_review: dict[str, Any] | None = None
        if (
            self.gemini_cio.autonomous_mode
            and not self.paused
            and bool(getattr(waterfall_decision, "research_evidence_valid", False))
            and final_act in {"BUY", "SELL"}
            and waterfall_decision.win_probability_pct >= AUTO_SCREENER_MIN_WIN_PROB
            and waterfall_decision.recommended_lot >= 0.01
        ):
            try:
                if not is_joey_live_auto_execution_symbol(symbol):
                    shadow_trade_result = self._shadow_trade_payload(
                        waterfall_decision,
                        symbol=symbol,
                        origin="scan",
                    )
                    await self._publish(
                        event(
                            "trade.shadow",
                            scan_id=job.scan_id,
                            source="gemini-cio-joey",
                            symbol=symbol,
                            shadow_trade=shadow_trade_result,
                            message=(
                                f"[SHADOW MODE] {symbol} {final_act} logged virtually; "
                                "JoeyCIO auto-execution is limited to XAUUSD, USTEC, and US500."
                            ),
                        ),
                        job,
                    )
                else:
                    open_syms: set[str] = set()
                    try:
                        pos_res = await self.get_open_positions()
                        open_syms = {
                            str(p.get("symbol") or "").strip().upper()
                            for p in pos_res.get("positions", [])
                            if isinstance(p, dict)
                        }
                    except Exception:
                        pass
                    if symbol.strip().upper() in open_syms:
                        await self._publish(
                            event(
                                "process.log",
                                scan_id=job.scan_id,
                                source="gemini-cio-joey",
                                message=f"[Autonomous CIO] {symbol} already has an open position — skipping duplicate entry.",
                            ),
                            job,
                        )
                    else:
                        approved, gated_volume, gemini_review = await self._gemini_gate_before_auto_trade(
                            waterfall_decision,
                            symbol=symbol,
                            origin="scan",
                            mt5_meta=mt5_meta if isinstance(mt5_meta, dict) else None,
                            l2_snap=l2_snapshot if isinstance(l2_snapshot, dict) else None,
                            open_symbols=open_syms,
                            controls=controls or self.controls.execution_values(),
                        )
                        if approved and gated_volume >= 0.01:
                            autonomous_trade_result = await self.execute_demo_trade(
                                symbol=symbol,
                                side=final_act,
                                volume=gated_volume,
                                sl=waterfall_decision.sl_price,
                                tp=waterfall_decision.tp_price,
                                atr=self._decision_atr(
                                    waterfall_decision,
                                    (mt5_meta or {}).get("atr_m5") if isinstance(mt5_meta, dict) else 0.0,
                                ),
                                comment=build_joey_profile_comment(
                                    symbol,
                                    waterfall_decision.step2_kronos_matrix,
                                ),
                                allow_paper_fallback=False,
                            )
            except Exception as auto_exc:
                await self._publish(
                    event(
                        "process.log",
                        scan_id=job.scan_id,
                        source="gemini-cio-joey",
                        message=f"[Autonomous CIO Execution Skipped]: {auto_exc}",
                    ),
                    job,
                )

        # Local AI is deliberately called only after the deterministic
        # waterfall and any existing demo/shadow handling have completed. Its
        # result is telemetry/research and cannot authorize or alter an order.
        latest_ai_bars = await asyncio.to_thread(
            self._read_latest_ohlcv_csv,
            market_csv,
            int(getattr(self.settings, "ai_context_max_bars", 32)),
        )
        ai_step2 = self._waterfall_step2(waterfall_dict)
        if not ai_step2 and isinstance(kronos_report, dict):
            ai_step2 = self._mapping_or_empty(kronos_report.get("decision", kronos_report))
        ai_telemetry = await self._run_ai_analysis(
            symbol=symbol,
            source="scan",
            scan_id=job.scan_id,
            job=job,
            ohlcv=latest_ai_bars,
            indicators=step1_talib.to_dict(),
            kronos_matrix=ai_step2,
            l2_dom=l2_snapshot,
            extra_context={"waterfall_action": final_act},
        )

        result: dict[str, Any] = {
            "scan_id": job.scan_id,
            "symbol": symbol,
            "gate": gate,
            "waterfall": waterfall_dict,
            "autonomous_trade": autonomous_trade_result,
            "shadow_trade": shadow_trade_result,
            "gemini_review": gemini_review,
            "ai": ai_telemetry,
            "ai_engine": ai_telemetry,
            "mt5_sync": mt5_meta,
            "l2_orderflow": l2_snapshot,
            "execution_allowed": False,
            "real_account_allowed": False,
            "phases": phases,
            "reports": {
                "kronos": self._report_summary(kronos_report),
                "tradingagents": self._report_summary(trading_report),
                "combined": self._report_summary(combined_report),
                "waterfall": waterfall_dict,
            },
        }
        if gate is not None:
            await self._publish(event("gate.changed", scan_id=job.scan_id, gate=gate, waterfall=waterfall_dict), job)

        memory_payload = {
            "scan_id": job.scan_id,
            "strategy": str((gate or {}).get("strategy") or STRATEGY_ID),
            "symbol": symbol,
            "action": final_act,
            "score": float((kronos_report or {}).get("score") or waterfall_decision.win_probability_pct),
            "confidence": final_conf,
            "reasons": (kronos_report or {}).get("decision", {}).get("hard_blocks", []),
            "analysis": {
                "waterfall": waterfall_dict,
                "kronos": kronos_report,
                "tradingagents": trading_report,
                "l2_orderflow": l2_snapshot,
                "shadow_trade": shadow_trade_result,
                "ai": ai_telemetry,
            },
            "result": (
                "autonomous_demo_order_opened"
                if autonomous_trade_result
                else "shadow_signal_logged"
                if shadow_trade_result
                else "research_cycle_completed_no_order"
            ),
            "created_at": time.time(),
        }
        try:
            memory_result = await asyncio.to_thread(self.memory.store_cycle, memory_payload)
            result["memory"] = memory_result
            await self._publish(event("memory.stored", scan_id=job.scan_id, **memory_result), job)
        except Exception as exc:
            result["memory"] = {"stored": False, "error": str(exc)}
            await self._publish(event("memory.failed", scan_id=job.scan_id, error=str(exc)), job)
        return result

    @staticmethod
    def _report_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
        if report is None:
            return None
        summary: dict[str, Any] = {}
        for key in (
            "strategy",
            "symbol",
            "action",
            "ai_action",
            "consensus_action",
            "score",
            "confidence",
            "safe_failure",
            "reason",
            "device",
            "last_bar",
            "decision",
            "waterfall",
        ):
            if key in report:
                summary[key] = report[key]
        return summary

    def get_job(self, scan_id: str) -> ScanJob | None:
        return self.jobs.get(scan_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [job.public() for job in reversed(list(self.jobs.values()))]

    def _latest_report_for_ui(self, path: Path, source: str) -> dict[str, Any] | None:
        """Expose only fresh, structurally valid research reports to the UI."""
        raw = self._read_json(path)
        if raw is None:
            return None
        identities = GeminiPortfolioManager._report_identity_symbols(raw)
        if not identities:
            return self._invalid_evidence_marker("", f"{source} report has no symbol identity")
        return self._read_current_research_report(
            path,
            symbol=identities[0],
            source=source,
        )

    def latest_gates(self) -> dict[str, Any]:
        return {
            "kronos_matrix": self._read_gate(
                self.settings.kronos_gate_path,
                expected_strategy=STRATEGY_ID,
            ),
            "tradingagents": self._read_gate(
                self.settings.ai_gate_path,
                expected_strategy="new-era-gold-council-v1",
            ),
            "kronos_report": self._latest_report_for_ui(
                self.settings.report_dir / "kronos_matrix_latest.json",
                "kronos",
            ),
            "tradingagents_report": self._latest_report_for_ui(
                self.settings.report_dir / "tradingagents_latest.json",
                "tradingagents",
            ),
            "combined_report": self._read_json(self.settings.combined_report_path),
            "l2_orderflow": self._read_json(self.l2_report_path),
            "waterfall": self._read_json(self.waterfall_report_path) or self.gemini_cio._last_decision,
            "screener": self.get_screener_status(),
            "autonomous_mode": self.gemini_cio.autonomous_mode,
            "dynamic_trailing_enabled": self.gemini_cio.dynamic_trailing_enabled,
            "paused": self.paused,
            "telegram": self.telegram.status_summary(),
            "controls": self.controls.snapshot(),
            "ai": self.ai_status(),
            "decision_history_summary": self.decision_history_summary(),
            "decision_history": self.decision_history(20)["records"],
            "execution_allowed": False,
            "real_account_allowed": False,
        }

    async def get_trade_history(self, days: int = 30, limit: int = 150) -> dict[str, Any]:
        return await asyncio.to_thread(get_mt5_trade_history, days, limit)

    async def get_open_positions(self, symbol: str | None = None) -> dict[str, Any]:
        if self.gemini_cio.dynamic_trailing_enabled:
            try:
                managed = await self.manage_demo_positions(symbol=symbol)
                if managed.get("ok") and isinstance(managed.get("positions"), list):
                    total_pnl = round(sum(float(p.get("profit") or 0.0) for p in managed["positions"]), 2)
                    return {
                        "ok": True,
                        "connected": True,
                        "is_demo": True,
                        "positions": managed["positions"],
                        "count": len(managed["positions"]),
                        "total_floating_pnl": total_pnl,
                        "modifications": managed.get("modifications", []),
                    }
            except Exception:
                pass
        return await asyncio.to_thread(get_mt5_open_positions, symbol)

    async def manage_demo_positions(
        self,
        *,
        symbol: str | None = None,
        price_override: float | None = None,
        atr_override: float | None = None,
    ) -> dict[str, Any]:
        payload = await asyncio.to_thread(
            manage_mt5_demo_positions,
            symbol,
            price_override=price_override,
            atr_override=atr_override,
            time_stop_minutes=self.controls.execution_values()["time_stop_minutes"],
        )
        for mod in payload.get("modifications", []):
            await self.bus.publish(
                event(
                    "trade.managed",
                    source="dynamic-position-manager",
                    ticket=mod.get("ticket"),
                    symbol=mod.get("symbol"),
                    action_type=mod.get("action_type"),
                    new_sl=mod.get("new_sl"),
                    new_volume=mod.get("new_volume"),
                    atr_multiple=mod.get("atr_multiple"),
                    message=f"[Dynamic Position Manager] #{mod.get('ticket')} ({mod.get('symbol')}): {mod.get('description')}",
                )
            )
            await self.telegram.notify_position_managed(mod)
        return payload

    async def execute_demo_trade(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
        atr: float | None = None,
        comment: str = "NewEraQuantDemo",
        allow_paper_fallback: bool = False,
    ) -> dict[str, Any]:
        if self.paused:
            raise RuntimeError("Trading is currently PAUSED via Remote Control (/pause). Send /resume to re-enable.")
        payload = await asyncio.to_thread(
            execute_mt5_demo_order,
            symbol,
            side,
            volume,
            sl=sl,
            tp=tp,
            atr=atr,
            comment=comment,
            allow_paper_fallback=allow_paper_fallback,
        )
        pos = payload.get("position") or {}
        execution_source = str(payload.get("execution_source") or "mt5_demo")
        paper_fallback = bool(
            payload.get("paper_fallback")
            or payload.get("demo_overlay")
            or execution_source == "mt5_demo_paper_fallback"
        )
        execution_label = "LOCAL PAPER OVERLAY" if paper_fallback else "MT5 DEMO LIVE"
        await self.bus.publish(
            event(
                "trade.opened",
                source="mt5-demo",
                symbol=pos.get("symbol", symbol),
                side=pos.get("side", side),
                volume=pos.get("volume", volume),
                price_open=pos.get("price_open"),
                sl=pos.get("sl", sl),
                tp=pos.get("tp", tp),
                risk_usd=pos.get("risk_usd"),
                reward_usd=pos.get("reward_usd"),
                ticket=pos.get("ticket"),
                execution_source=execution_source,
                demo_overlay=paper_fallback,
                paper_fallback=paper_fallback,
                message=(
                    f"[{execution_label}] Opened {pos.get('side', side)} {pos.get('volume', volume)} lot "
                    f"{pos.get('symbol', symbol)} @ {pos.get('price_open')} | "
                    f"SL={pos.get('sl')} (-${pos.get('risk_usd')}) | TP={pos.get('tp')} (+${pos.get('reward_usd')}) "
                    f"[{execution_source}]"
                ),
            )
        )
        wf = self._read_json(self.waterfall_report_path) or self.gemini_cio._last_decision
        await self.telegram.notify_trade_opened(
            pos,
            execution_source=execution_source,
            waterfall=wf,
        )
        return payload

    async def close_demo_positions(
        self,
        *,
        symbol: str | None = None,
        ticket: int | None = None,
    ) -> dict[str, Any]:
        payload = await asyncio.to_thread(close_mt5_demo_positions, symbol=symbol, ticket=ticket)
        await self.bus.publish(
            event(
                "trade.closed",
                source="mt5-demo",
                symbol=symbol or "ALL",
                closed_tickets=payload.get("closed_tickets", []),
                closed_count=payload.get("closed_count", 0),
                message=(
                    f"[MT5 DEMO EXECUTION] Closed {payload.get('closed_count', 0)} demo position(s) "
                    f"{payload.get('closed_tickets', [])}"
                ),
            )
        )
        await self.telegram.notify_trade_closed(
            {
                "symbol": symbol or "ALL",
                "closed_count": payload.get("closed_count", 0),
                "closed_tickets": payload.get("closed_tickets", []),
            }
        )
        return payload

    def set_autonomous_settings(
        self,
        *,
        autonomous_mode: bool | None = None,
        dynamic_trailing_enabled: bool | None = None,
        paused: bool | None = None,
    ) -> dict[str, Any]:
        if autonomous_mode is not None:
            previous = self.gemini_cio.autonomous_mode
            self.gemini_cio.autonomous_mode = bool(autonomous_mode)
            if previous != self.gemini_cio.autonomous_mode:
                state_txt = "ON" if self.gemini_cio.autonomous_mode else "OFF"
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.bus.publish(
                            event(
                                "autonomous.changed",
                                source="gemini-cio-joey",
                                autonomous_mode=self.gemini_cio.autonomous_mode,
                                message=f"[AUTO-TRADE {state_txt}] Autonomous Demo trading is now {state_txt}.",
                            )
                        )
                    )
                except RuntimeError:
                    pass  # called outside an event loop (tests) -> UI syncs via /health polling
        if dynamic_trailing_enabled is not None:
            self.gemini_cio.dynamic_trailing_enabled = bool(dynamic_trailing_enabled)
        if paused is not None:
            self.paused = bool(paused)
        return {
            "autonomous_mode": self.gemini_cio.autonomous_mode,
            "dynamic_trailing_enabled": self.gemini_cio.dynamic_trailing_enabled,
            "paused": self.paused,
            "gemini": self.gemini_cio.gemini_status(),
        }

    async def _gemini_gate_before_auto_trade(
        self,
        wf_decision: Any,
        *,
        symbol: str,
        origin: str,
        mt5_meta: dict[str, Any] | None = None,
        l2_snap: dict[str, Any] | None = None,
        open_symbols: Iterable[str] | None = None,
        controls: dict[str, Any] | None = None,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Final autonomous gate: the live Gemini API must APPROVE before any auto-trade.

        Returns (approved, volume_to_send, review). Gemini can only shrink the lot, never grow it.
        """
        meta = mt5_meta if isinstance(mt5_meta, dict) else {}
        l2 = l2_snap if isinstance(l2_snap, dict) else {}
        control_state = controls or self.controls.execution_values()
        ctx = {
            "origin": origin,
            "controls": control_state,
            "utc_now": utc_now().isoformat(),
            "quote": {
                "bid": meta.get("bid") or l2.get("best_bid"),
                "ask": meta.get("ask") or l2.get("best_ask"),
                "spread_points": meta.get("spread_points"),
                "atr_m5": meta.get("atr_m5"),
            },
            "l2": {
                "direction": l2.get("l2_direction"),
                "bid_pressure_pct": l2.get("bid_pressure_pct"),
                "ask_pressure_pct": l2.get("ask_pressure_pct"),
                "veto_buy": l2.get("veto_buy"),
                "veto_sell": l2.get("veto_sell"),
                "veto_reason": l2.get("veto_reason"),
            },
            "already_open_symbols": sorted(set(open_symbols or [])),
        }
        if bool(control_state.get("gemini_cio_enabled", True)):
            review = await asyncio.to_thread(self.gemini_cio.review_trade_with_gemini, wf_decision, ctx)
        else:
            review = {
                "symbol": symbol,
                "action": str(getattr(wf_decision, "final_action", "ABSTAIN")),
                "verdict": "BYPASSED",
                "risk_multiplier": 1.0,
                "confidence_pct": 0.0,
                "reason_ka": "Gemini CIO veto disabled by explicit runtime override; deterministic demo safeguards remain active.",
                "model": None,
                "source": "control_bypass",
                "reviewed_at": utc_now().isoformat(),
            }
        approved = review.get("verdict") in {"APPROVE", "BYPASSED"}

        base_lot = float(getattr(wf_decision, "recommended_lot", 0.0) or 0.0)
        step = float(meta.get("volume_step") or 0.01) or 0.01
        vol_min = float(meta.get("volume_min") or 0.01) or 0.01
        mult = float(review.get("risk_multiplier") or 0.0)
        volume = math.floor((base_lot * mult) / step + 1e-9) * step
        volume = round(volume, 2) if approved and volume >= vol_min else 0.0
        if approved and volume <= 0.0:
            approved = False
            review = dict(review)
            review["reason_ka"] = (
                str(review.get("reason_ka") or "")
                + " Broker minimum lot would exceed the JoeyCIO risk budget."
            ).strip()

        side = str(getattr(wf_decision, "final_action", "ABSTAIN"))
        prob = float(getattr(wf_decision, "win_probability_pct", 0.0) or 0.0)
        icon = "✅" if approved else "🛑"
        await self.bus.publish(
            event(
                "gemini.review",
                source="gemini-cio-joey",
                symbol=symbol,
                review=review,
                message=(
                    f"{icon} [Gemini CIO {review.get('verdict')}] {symbol} {side} ({prob:.1f}%) "
                    f"| model={review.get('model') or review.get('source')} | lot={volume:.2f} "
                    f"| {review.get('reason_ka')}"
                ),
            )
        )
        return approved, volume, review

    # ------------------------------------------------------------------
    # 100% Autonomous Multi-Pair Screener & Auto-Execution Engine
    # ------------------------------------------------------------------
    def get_screener_status(self) -> dict[str, Any]:
        cached = self._last_screener_cycle or self._read_json(self.screener_report_path) or {}
        return {
            "running": self._screener_running,
            "autonomous_mode": self.gemini_cio.autonomous_mode,
            "paused": self.paused,
            "interval_seconds": self.screener_interval_seconds,
            "min_win_probability_pct": AUTO_SCREENER_MIN_WIN_PROB,
            "symbols": list(self.screener_symbols),
            "live_auto_execution_symbols": sorted(JOEY_LIVE_AUTO_EXECUTION_SYMBOLS),
            "shadow_mode_symbols": sorted(
                symbol for symbol in self.screener_symbols if not is_joey_live_auto_execution_symbol(symbol)
            ),
            "gemini": self.gemini_cio.gemini_status(),
            "controls": self.controls.snapshot(),
            "ai": self.ai_status(),
            "decision_history_summary": self.decision_history_summary(),
            "decision_history": self.decision_history(20)["records"],
            "last_cycle": cached,
        }

    async def _warmup_gemini_connection(self) -> None:
        try:
            status = await asyncio.to_thread(self.gemini_cio.ping_gemini, 20.0)
        except Exception as exc:  # never break startup
            status = {"ping_ok": False, "last_error": f"{type(exc).__name__}"}
        msg = (
            f"[Gemini CIO] Live API connected ✅ model={status.get('active_model')}"
            if status.get("ping_ok")
            else f"[Gemini CIO] Live API NOT reachable ⚠️ ({status.get('last_error')}) — auto-trades will ABSTAIN until it recovers."
        )
        await self.bus.publish(event("process.log", source="gemini-cio-joey", message=msg, gemini=status))

    async def start_autonomous_screener(self) -> None:
        if self._screener_running:
            return
        self._screener_running = True
        self.multi_bot_hub.start_auto_fleet_loop(interval_seconds=12.0)
        self._gemini_warmup_task = asyncio.create_task(self._warmup_gemini_connection())
        self._screener_task = asyncio.create_task(self._autonomous_screener_loop())

    async def stop_autonomous_screener(self) -> None:
        self._screener_running = False
        self.multi_bot_hub.stop_auto_fleet_loop()
        if self._screener_task is not None:
            self._screener_task.cancel()
            try:
                await self._screener_task
            except (asyncio.CancelledError, Exception):
                pass
            self._screener_task = None

    async def _autonomous_screener_loop(self) -> None:
        # Initial startup delay so API server finishes booting cleanly
        try:
            await asyncio.sleep(8.0)
        except asyncio.CancelledError:
            return
        while self._screener_running:
            try:
                if not self.paused and self._active_scan_id is None:
                    await self.run_multi_pair_screener_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self.bus.publish(
                    event(
                        "process.log",
                        source="multi-pair-screener",
                        message=f"[Autonomous 8-Pair Screener Warning]: {exc}",
                    )
                )
            try:
                await asyncio.sleep(float(max(1, self.screener_interval_seconds)))
            except asyncio.CancelledError:
                break

    async def run_multi_pair_screener_cycle(
        self,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Iterate over the Top 8 pairs, run the 4-Step Waterfall Pipeline, and auto-execute Demo trades when Win-Prob >= 68%."""
        pairs = [str(s).strip().upper() for s in (symbols or self.screener_symbols) if str(s).strip()]
        started_ts = utc_now().isoformat()
        cycle_id = f"screener-{uuid.uuid4()}"
        controls_snapshot = self.controls.execution_values()

        await self.bus.publish(
            event(
                "kronos.forecast",
                scan_id=cycle_id,
                source="multi-pair-screener",
                message=(
                    f"[Autonomous Multi-Pair Screener] Scanning {len(pairs)} pairs "
                    f"({', '.join(pairs)}) | Auto-Exec={'ON' if self.gemini_cio.autonomous_mode else 'STANDBY'} "
                    f"(Min Win-Prob >= {AUTO_SCREENER_MIN_WIN_PROB:.0f}%) | cycle={cycle_id}..."
                ),
            )
        )

        # Fetch live MT5 Account Equity & existing open symbols once per cycle
        account_equity = 1000.0
        open_symbols: set[str] = set()
        try:
            mt5_st = await asyncio.to_thread(get_mt5_status, 15)
            acc_obj = mt5_st.get("account") if isinstance(mt5_st, dict) else None
            if isinstance(acc_obj, dict) and float(acc_obj.get("equity") or 0.0) > 0:
                account_equity = float(acc_obj["equity"])
        except Exception:
            pass

        try:
            pos_res = await self.get_open_positions()
            for p in pos_res.get("positions", []):
                if isinstance(p, dict) and p.get("symbol"):
                    open_symbols.add(str(p["symbol"]).strip().upper())
        except Exception:
            pass

        pair_results: list[dict[str, Any]] = []
        auto_executed_trades: list[dict[str, Any]] = []
        shadow_trades: list[dict[str, Any]] = []

        for sym in pairs:
            target_csv = (
                self.settings.root
                / "TradingAgentsData"
                / "market"
                / f"{_safe_symbol_filename(sym)}_m5.csv"
            )
            mt5_meta: dict[str, Any] = {}
            try:
                mt5_meta = await asyncio.to_thread(
                    fetch_mt5_bars_to_csv,
                    sym,
                    target_csv,
                    timeframe="M5",
                    bars=1000,
                )
            except Exception:
                crypto_pair = resolve_crypto_pair(sym)
                if crypto_pair is not None:
                    try:
                        mt5_meta = await asyncio.to_thread(
                            _fetch_binance_klines_to_csv,
                            crypto_pair,
                            target_csv,
                            "5m",
                            800,
                        )
                    except Exception:
                        pass
                if not mt5_meta:
                    try:
                        mt5_meta = await asyncio.to_thread(get_mt5_symbol_snapshot, sym)
                    except Exception:
                        mt5_meta = {}

            try:
                l2_snap = (
                    await self.MarketBookGet(sym, levels=10, publish_events=False)
                    if bool(controls_snapshot["l2_enabled"])
                    else {}
                )
            except Exception:
                l2_snap = {}

            fallback_px = float(mt5_meta.get("last_close") or mt5_meta.get("bid") or l2_snap.get("mid_price") or 0.0)
            fallback_atr = float(mt5_meta.get("atr_m5") or 0.0)

            step1_talib = await asyncio.to_thread(
                TALibTechnicalEngine.evaluate_from_csv,
                sym,
                target_csv,
                fallback_close=fallback_px,
                fallback_atr=fallback_atr,
            )

            # Each pair gets only its own current reports.  A latest report for
            # XAUUSD is invalid evidence for EURUSD (and so on), even when the
            # local market CSV for the latter happens to exist.
            cached_kronos = (
                self._read_current_research_report(
                    self.settings.report_dir / "kronos_matrix_latest.json",
                    symbol=sym,
                    source="kronos",
                )
                if bool(controls_snapshot["kronos_enabled"])
                else None
            )
            cached_ta = self._read_current_research_report(
                self.settings.report_dir / "tradingagents_latest.json",
                symbol=sym,
                source="tradingagents",
            )

            wf_decision = self.gemini_cio.evaluate_waterfall_pipeline(
                symbol=sym,
                step1_talib=step1_talib,
                kronos_report=cached_kronos,
                tradingagents_report=cached_ta,
                l2_snapshot=l2_snap,
                mt5_meta=mt5_meta,
                account_equity=account_equity,
                market_csv=target_csv,
                controls=controls_snapshot,
            )
            wf_dict = wf_decision.to_dict()

            auto_trade_info: dict[str, Any] | None = None
            shadow_trade_info: dict[str, Any] | None = None
            pair_review: dict[str, Any] | None = None
            if (
                self.gemini_cio.autonomous_mode
                and not self.paused
                and bool(getattr(wf_decision, "research_evidence_valid", False))
                and wf_decision.final_action in {"BUY", "SELL"}
                and wf_decision.win_probability_pct >= AUTO_SCREENER_MIN_WIN_PROB
                and wf_decision.recommended_lot >= 0.01
                and sym not in open_symbols
            ):
                try:
                    if not is_joey_live_auto_execution_symbol(sym):
                        shadow_trade_info = self._shadow_trade_payload(
                            wf_decision,
                            symbol=sym,
                            origin="8-pair-screener",
                        )
                        shadow_trades.append(shadow_trade_info)
                        await self.bus.publish(
                            event(
                                "trade.shadow",
                                source="multi-pair-screener",
                                symbol=sym,
                                shadow_trade=shadow_trade_info,
                                message=(
                                    f"[SHADOW MODE] {sym} {wf_decision.final_action} logged virtually; "
                                    "no autonomous order was sent."
                                ),
                            )
                        )
                    else:
                        approved, gated_volume, pair_review = await self._gemini_gate_before_auto_trade(
                            wf_decision,
                            symbol=sym,
                            origin="8-pair-screener",
                            mt5_meta=mt5_meta,
                            l2_snap=l2_snap if isinstance(l2_snap, dict) else None,
                            open_symbols=open_symbols,
                            controls=controls_snapshot,
                        )
                        if approved and gated_volume >= 0.01:
                            auto_trade_info = await self.execute_demo_trade(
                                symbol=sym,
                                side=wf_decision.final_action,
                                volume=gated_volume,
                                sl=wf_decision.sl_price,
                                tp=wf_decision.tp_price,
                                atr=self._decision_atr(wf_decision, mt5_meta.get("atr_m5")),
                                comment=build_joey_profile_comment(
                                    sym,
                                    wf_decision.step2_kronos_matrix,
                                ),
                                allow_paper_fallback=False,
                            )
                            open_symbols.add(sym)
                            auto_executed_trades.append(
                                {
                                    "symbol": sym,
                                    "side": wf_decision.final_action,
                                    "win_probability_pct": wf_decision.win_probability_pct,
                                    "lot": gated_volume,
                                    "gemini_review": pair_review,
                                    "trade": auto_trade_info,
                                }
                            )
                except Exception as exec_exc:
                    await self.bus.publish(
                        event(
                            "process.log",
                            source="multi-pair-screener",
                            message=f"[Auto-Screener Execution Skipped ({sym})]: {exec_exc}",
                        )
                    )

            # Run the local research-only AI after this pair's waterfall. The
            # screener's existing execution/shadow branch is already complete,
            # so this analysis cannot unlock or modify a trade.
            latest_ai_bars = await asyncio.to_thread(
                self._read_latest_ohlcv_csv,
                target_csv,
                int(getattr(self.settings, "ai_context_max_bars", 32)),
            )
            ai_telemetry = await self._run_ai_analysis(
                symbol=sym,
                source="screener",
                scan_id=cycle_id,
                ohlcv=latest_ai_bars,
                indicators=step1_talib.to_dict(),
                kronos_matrix=self._waterfall_step2(wf_dict),
                l2_dom=l2_snap,
                extra_context={"waterfall_action": wf_decision.final_action},
            )

            pair_summary = {
                "symbol": sym,
                "final_action": wf_decision.final_action,
                "win_probability_pct": round(wf_decision.win_probability_pct, 1),
                "risk_tier": wf_decision.risk_tier,
                "recommended_lot": wf_decision.recommended_lot,
                "gemini_verdict": (pair_review or {}).get("verdict"),
                "gemini_reason_ka": (pair_review or {}).get("reason_ka"),
                "sl_price": wf_decision.sl_price,
                "tp_price": wf_decision.tp_price,
                "talib_bias": step1_talib.trend_bias,
                "rsi_14": round(step1_talib.rsi_14, 1),
                "l2_direction": l2_snap.get("l2_direction", "BYPASSED" if not controls_snapshot["l2_enabled"] else "NEUTRAL"),
                "l2_wall_support": wf_decision.l2_wall_support,
                "l2_vetoed": wf_decision.l2_vetoed,
                "auto_executed": bool(auto_trade_info),
                "execution_mode": "LIVE" if auto_trade_info else "SHADOW" if shadow_trade_info else "BLOCKED",
                "auto_execution_allowed": is_joey_live_auto_execution_symbol(sym),
                "shadow_mode": bool(shadow_trade_info),
                "shadow_trade": shadow_trade_info,
                "profile_comment": build_joey_profile_comment(
                    sym,
                    wf_decision.step2_kronos_matrix,
                ),
                "waterfall": wf_dict,
                "ai": ai_telemetry,
                "ai_engine": ai_telemetry,
            }
            outcome = self._decision_outcome(
                wf_dict,
                autonomous_mode=self.gemini_cio.autonomous_mode,
                paused=self.paused,
                autonomous_trade=auto_trade_info,
                shadow_trade=shadow_trade_info,
                gemini_review=pair_review,
                symbol_in_open_positions=sym in open_symbols,
            )
            step2 = wf_dict.get("steps", {}).get("step2_kronos_matrix", {}) if isinstance(wf_dict, dict) else {}
            decision_record = await self._record_decision(
                scan_id=cycle_id,
                cycle=cycle_id,
                symbol=sym,
                waterfall=wf_dict,
                signal=step2.get("candidate_direction") if isinstance(step2, dict) else None,
                outcome=outcome,
                source="multi-pair-screener",
                controls=controls_snapshot,
            )
            pair_summary["decision_record_id"] = decision_record["record_id"]
            pair_summary["block_reason"] = outcome.get("block_reason") or ""
            pair_results.append(pair_summary)

        pair_results.sort(key=lambda item: float(item.get("win_probability_pct") or 0.0), reverse=True)
        best_setup = pair_results[0] if pair_results else None

        cycle_payload = {
            "ok": True,
            "cycle_id": cycle_id,
            "started_at": started_ts,
            "finished_at": utc_now().isoformat(),
            "autonomous_mode": self.gemini_cio.autonomous_mode,
            "min_win_probability_pct": AUTO_SCREENER_MIN_WIN_PROB,
            "pairs_scanned": len(pair_results),
            "best_setup": best_setup,
            "auto_executed_count": len(auto_executed_trades),
            "auto_executed_trades": auto_executed_trades,
            "shadow_logged_count": len(shadow_trades),
            "shadow_trades": shadow_trades,
            "ai": {
                "analyses": len(pair_results),
                "research_only": True,
                "execution_allowed": False,
                "proposals_persisted": sum(
                    int((item.get("ai") or {}).get("proposal_persistence", {}).get("persisted", 0))
                    for item in pair_results
                    if isinstance(item, dict)
                ),
            },
            "controls": self.controls.snapshot(),
            "decision_history_summary": self.decision_history_summary(),
            "results": pair_results,
        }
        self._last_screener_cycle = cycle_payload
        try:
            self.screener_report_path.parent.mkdir(parents=True, exist_ok=True)
            self.screener_report_path.write_text(
                json.dumps(cycle_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

        top_str = (
            f"{best_setup['symbol']} -> {best_setup['final_action']} ({best_setup['win_probability_pct']}%)"
            if best_setup
            else "NONE"
        )
        await self.bus.publish(
            event(
                "screener.completed",
                scan_id=cycle_id,
                source="multi-pair-screener",
                pairs_scanned=len(pair_results),
                best_setup=best_setup,
                auto_executed_count=len(auto_executed_trades),
                message=(
                    f"[Autonomous Multi-Pair Screener] Cycle complete ({len(pair_results)} pairs) | "
                    f"Best Setup: {top_str} | Auto-Trades Opened: {len(auto_executed_trades)}"
                ),
            )
        )
        return cycle_payload

    async def _build_joey_context(self, symbol: str, account_equity: float = 1000.0) -> dict[str, Any]:
        norm_sym = symbol.strip().upper() or "XAUUSD"
        controls_snapshot = self.controls.execution_values()
        gates = self.latest_gates()
        l2_snap = (
            self._latest_l2.get(norm_sym) or gates.get("l2_orderflow")
            if controls_snapshot["l2_enabled"]
            else {}
        )
        if controls_snapshot["l2_enabled"] and (not l2_snap or str(l2_snap.get("symbol", "")).upper() != norm_sym):
            try:
                l2_snap = await self.MarketBookGet(norm_sym, levels=10, publish_events=False)
            except Exception:
                l2_snap = {}

        mt5_sym: dict[str, Any] = {}
        try:
            mt5_sym = await asyncio.to_thread(get_mt5_symbol_snapshot, norm_sym)
        except Exception:
            pass

        market_csv = self.settings.root / "TradingAgentsData" / "market" / f"{_safe_symbol_filename(norm_sym)}_m5.csv"
        step1_talib = await asyncio.to_thread(
            TALibTechnicalEngine.evaluate_from_csv,
            norm_sym,
            market_csv,
            fallback_close=float(mt5_sym.get("last_close") or (l2_snap or {}).get("mid_price") or 0.0),
            fallback_atr=float(mt5_sym.get("atr_m5") or 0.0),
        )
        kronos_report = (
            self._read_current_research_report(
                self.settings.report_dir / "kronos_matrix_latest.json",
                symbol=norm_sym,
                source="kronos",
            )
            if controls_snapshot["kronos_enabled"]
            else None
        )
        tradingagents_report = self._read_current_research_report(
            self.settings.report_dir / "tradingagents_latest.json",
            symbol=norm_sym,
            source="tradingagents",
        )
        wf_decision = self.gemini_cio.evaluate_waterfall_pipeline(
            symbol=norm_sym,
            step1_talib=step1_talib,
            kronos_report=kronos_report,
            tradingagents_report=tradingagents_report,
            l2_snapshot=l2_snap,
            mt5_meta=mt5_sym,
            account_equity=account_equity,
            market_csv=market_csv,
            controls=controls_snapshot,
        )
        wf_dict = wf_decision.to_dict()
        try:
            self.waterfall_report_path.write_text(json.dumps(wf_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        pos_data = await self.get_open_positions(norm_sym)
        return {
            "symbol": norm_sym,
            "waterfall": wf_dict,
            "l2_orderflow": l2_snap,
            "kronos_report": kronos_report,
            "mt5_symbol": mt5_sym,
            "positions": pos_data.get("positions", []),
        }

    async def analyze_setup_with_joey(self, symbol: str = "XAUUSD", account_equity: float = 1000.0) -> dict[str, Any]:
        ctx = await self._build_joey_context(symbol, account_equity=account_equity)
        reply = await asyncio.to_thread(self.gemini_cio.analyze_current_setup, ctx)
        reply["waterfall"] = ctx.get("waterfall")
        await self.bus.publish(
            event(
                "tradingagents.debate",
                source="gemini-cio-joey",
                symbol=symbol,
                message=f"[ჯოი — Setup Analysis ({symbol})]: {reply.get('text', '')[:240]}...",
            )
        )
        return reply

    async def chat_with_joey(self, symbol: str, message: str) -> dict[str, Any]:
        ctx = await self._build_joey_context(symbol)
        reply = await asyncio.to_thread(self.gemini_cio.chat, message, ctx)
        reply["waterfall"] = ctx.get("waterfall")
        return reply

    async def _handle_telegram_command(self, cmd: str) -> str:
        norm = cmd.strip().lower()
        if norm == "/status":
            gates = self.latest_gates()
            wf = gates.get("waterfall") or {}
            pos_res = await self.get_open_positions()
            positions = pos_res.get("positions", [])
            total_pnl = pos_res.get("total_floating_pnl", 0.0)
            return (
                f"📊 <b>NEWERA QUANT BOT STATUS</b>\n"
                f"• <b>State:</b> <code>{'PAUSED ⏸️' if self.paused else 'ACTIVE 🟢'}</code>\n"
                f"• <b>Autonomous CIO Mode:</b> <code>{'ON' if self.gemini_cio.autonomous_mode else 'OFF'}</code>\n"
                f"• <b>Waterfall Action:</b> <code>{wf.get('final_action', 'ABSTAIN')}</code> "
                f"(Win-Prob: <code>{wf.get('win_probability_pct', '--')}%</code>)\n"
                f"• <b>Open Demo Positions:</b> <code>{len(positions)}</code> (Live PnL: <code>${total_pnl:+.2f}</code>)"
            )
        if norm == "/close_all":
            closed = await self.close_demo_positions()
            return (
                f"🛑 <b>EMERGENCY CLOSE ALL EXECUTED</b>\n"
                f"• Closed <code>{closed.get('closed_count', 0)}</code> Demo position(s): "
                f"<code>{closed.get('closed_tickets', [])}</code>"
            )
        if norm == "/pause":
            self.paused = True
            self.gemini_cio.autonomous_mode = False
            await self.bus.publish(
                event("process.log", source="telegram-remote", message="[Telegram /pause] Trading paused remotely.")
            )
            return "⏸️ <b>NewEra Trading Bot PAUSED.</b> Autonomous Demo execution disabled. Send /resume to re-enable."
        if norm == "/resume":
            self.paused = False
            await self.bus.publish(
                event("process.log", source="telegram-remote", message="[Telegram /resume] Trading resumed remotely.")
            )
            return "▶️ <b>NewEra Trading Bot RESUMED.</b> Ready for Demo execution."
        if norm == "/analyze":
            res = await self.analyze_setup_with_joey(self.settings.default_symbol)
            return str(res.get("text") or "Analysis complete.")
        return (
            "🤖 <b>NewEra Quant Bot Commands:</b>\n"
            "/status — Live status & open PnL\n"
            "/close_all — Close all Demo positions\n"
            "/pause — Pause trading\n"
            "/resume — Resume trading\n"
            "/analyze — Ask Joey (Gemini CIO) for setup analysis"
        )
