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
from typing import Any, Iterable

from kronos_matrix_strategy import STRATEGY_ID, write_gate_csv

from .config import ApiSettings, load_workspace_dotenv
from .events import EventBus, event
from .gemini_manager import GeminiPortfolioManager, Step1TALibResult, TALibTechnicalEngine
from .memory import MemoryStore
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
from .telegram_bot import TelegramRemoteController


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
AUTO_SCREENER_INTERVAL_SECONDS: int = 120

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
        self.telegram = TelegramRemoteController(self.settings.root)
        self.telegram.set_command_handler(self._handle_telegram_command)
        self.paused: bool = False
        self._latest_l2: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, ScanJob] = {}
        self._active_scan_id: str | None = None
        self._lock = asyncio.Lock()
        self.screener_symbols: list[str] = list(SCREENER_SYMBOLS)
        self.screener_interval_seconds: int = AUTO_SCREENER_INTERVAL_SECONDS
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

    async def _run_scan(self, job: ScanJob, request: dict[str, Any]) -> None:
        job.status = "running"
        job.started_at = utc_now()
        symbol = str(request.get("symbol") or self.settings.default_symbol).upper()
        ttl_minutes = int(request.get("gate_ttl_minutes") or 90)
        include_tradingagents = bool(request.get("include_tradingagents", True))
        include_kronos = bool(request.get("include_kronos", True))
        phase_results: list[dict[str, Any]] = []
        mt5_meta: dict[str, Any] | None = None
        l2_snapshot: dict[str, Any] | None = None
        try:
            await self._publish(event("scan.started", scan_id=job.scan_id, symbol=symbol, mode=job.mode), job)
            if not include_tradingagents and not include_kronos:
                raise ValueError("at least one research phase must be enabled")

            # Phase 0: Capture L2 Orderbook & Liquidity Intelligence (`MarketBookGet`)
            try:
                l2_snapshot = await self.MarketBookGet(symbol, levels=10, publish_events=True)
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
            )
            job.result = result
            job.return_code = max((int(item["return_code"]) for item in phase_results), default=0)
            job.status = "completed" if job.return_code == 0 else "failed"
            if job.status == "failed":
                job.error = "one or more research phases stopped safely; gate remains ABSTAIN"
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
            return int(phase.get("return_code"))
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
                    )
                    if approved:
                        autonomous_trade_result = await self.execute_demo_trade(
                            symbol=symbol,
                            side=final_act,
                            volume=gated_volume,
                            sl=waterfall_decision.sl_price,
                            tp=waterfall_decision.tp_price,
                            comment=f"JoeyCIO_{int(waterfall_decision.win_probability_pct)}pct",
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

        result: dict[str, Any] = {
            "scan_id": job.scan_id,
            "symbol": symbol,
            "gate": gate,
            "waterfall": waterfall_dict,
            "autonomous_trade": autonomous_trade_result,
            "gemini_review": gemini_review,
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
            },
            "result": "autonomous_demo_order_opened" if autonomous_trade_result else "research_cycle_completed_no_order",
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
    ) -> tuple[bool, float, dict[str, Any]]:
        """Final autonomous gate: the live Gemini API must APPROVE before any auto-trade.

        Returns (approved, volume_to_send, review). Gemini can only shrink the lot, never grow it.
        """
        meta = mt5_meta if isinstance(mt5_meta, dict) else {}
        l2 = l2_snap if isinstance(l2_snap, dict) else {}
        ctx = {
            "origin": origin,
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
        review = await asyncio.to_thread(self.gemini_cio.review_trade_with_gemini, wf_decision, ctx)
        approved = review.get("verdict") == "APPROVE"

        base_lot = float(getattr(wf_decision, "recommended_lot", 0.0) or 0.0)
        step = float(meta.get("volume_step") or 0.01) or 0.01
        vol_min = float(meta.get("volume_min") or 0.01) or 0.01
        mult = float(review.get("risk_multiplier") or 0.0)
        volume = math.floor((base_lot * mult) / step + 1e-9) * step
        volume = round(max(vol_min, volume), 2) if approved else 0.0

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
            "gemini": self.gemini_cio.gemini_status(),
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
        self._gemini_warmup_task = asyncio.create_task(self._warmup_gemini_connection())
        self._screener_task = asyncio.create_task(self._autonomous_screener_loop())

    async def stop_autonomous_screener(self) -> None:
        self._screener_running = False
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
                await asyncio.sleep(float(max(60, self.screener_interval_seconds)))
            except asyncio.CancelledError:
                break

    async def run_multi_pair_screener_cycle(
        self,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Iterate over the Top 8 pairs, run the 4-Step Waterfall Pipeline, and auto-execute Demo trades when Win-Prob >= 68%."""
        pairs = [str(s).strip().upper() for s in (symbols or self.screener_symbols) if str(s).strip()]
        started_ts = utc_now().isoformat()

        await self.bus.publish(
            event(
                "kronos.forecast",
                source="multi-pair-screener",
                message=(
                    f"[Autonomous Multi-Pair Screener] Scanning {len(pairs)} pairs "
                    f"({', '.join(pairs)}) | Auto-Exec={'ON' if self.gemini_cio.autonomous_mode else 'STANDBY'} "
                    f"(Min Win-Prob >= {AUTO_SCREENER_MIN_WIN_PROB:.0f}%)..."
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
                l2_snap = await self.MarketBookGet(sym, levels=10, publish_events=False)
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
            cached_kronos = self._read_current_research_report(
                self.settings.report_dir / "kronos_matrix_latest.json",
                symbol=sym,
                source="kronos",
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
            )
            wf_dict = wf_decision.to_dict()

            auto_trade_info: dict[str, Any] | None = None
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
                    approved, gated_volume, pair_review = await self._gemini_gate_before_auto_trade(
                        wf_decision,
                        symbol=sym,
                        origin="8-pair-screener",
                        mt5_meta=mt5_meta,
                        l2_snap=l2_snap if isinstance(l2_snap, dict) else None,
                        open_symbols=open_symbols,
                    )
                    if approved:
                        auto_trade_info = await self.execute_demo_trade(
                            symbol=sym,
                            side=wf_decision.final_action,
                            volume=gated_volume,
                            sl=wf_decision.sl_price,
                            tp=wf_decision.tp_price,
                            comment=f"AutoScreener_{int(wf_decision.win_probability_pct)}pct",
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
                "l2_direction": l2_snap.get("l2_direction", "NEUTRAL"),
                "l2_wall_support": wf_decision.l2_wall_support,
                "l2_vetoed": wf_decision.l2_vetoed,
                "auto_executed": bool(auto_trade_info),
                "waterfall": wf_dict,
            }
            pair_results.append(pair_summary)

        pair_results.sort(key=lambda item: float(item.get("win_probability_pct") or 0.0), reverse=True)
        best_setup = pair_results[0] if pair_results else None

        cycle_payload = {
            "ok": True,
            "started_at": started_ts,
            "finished_at": utc_now().isoformat(),
            "autonomous_mode": self.gemini_cio.autonomous_mode,
            "min_win_probability_pct": AUTO_SCREENER_MIN_WIN_PROB,
            "pairs_scanned": len(pair_results),
            "best_setup": best_setup,
            "auto_executed_count": len(auto_executed_trades),
            "auto_executed_trades": auto_executed_trades,
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
        gates = self.latest_gates()
        l2_snap = self._latest_l2.get(norm_sym) or gates.get("l2_orderflow")
        if not l2_snap or str(l2_snap.get("symbol", "")).upper() != norm_sym:
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
        kronos_report = self._read_current_research_report(
            self.settings.report_dir / "kronos_matrix_latest.json",
            symbol=norm_sym,
            source="kronos",
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
