r"""Local FastAPI backend for the New Era desktop application.

Run from ``C:\Users\LeVI\Desktop\NewEraTradingBot``:

    python -m uvicorn main_api:app --host 127.0.0.1 --port 8787

The API is intentionally research/demo-only. It starts the existing fixed
research scripts, streams their output, reads live MetaTrader 5 account/symbol
telemetry and closed OHLCV bars, stores completed-cycle memory, and never
exposes an order-placement endpoint.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import bot_manager
from newera_api import __version__
from newera_api.bot_lab import StrategyLab
from newera_api.config import settings
from newera_api.events import EventBus, event
from newera_api.mt5_bridge import get_mt5_status, get_mt5_symbol_snapshot
from newera_api.orchestrator import Orchestrator
from newera_api.schemas import (
    AutonomousModeRequest,
    AIAnalyzeRequest,
    BotEnabledRequest,
    BotProfileCreateRequest,
    ControlsUpdateRequest,
    DemoCloseRequest,
    DemoManageRequest,
    DemoOrderRequest,
    GeminiAnalyzeRequest,
    GeminiChatRequest,
    MemorySearchResponse,
    BacktestRequest,
    ScanRequest,
    ScanResponse,
    ScanStatus,
    StrategyLoadRequest,
    StrategySaveRequest,
    TelegramCommandRequest,
    WalkForwardRequest,
)


bus = EventBus()
orchestrator = Orchestrator(settings, bus)
strategy_lab = StrategyLab(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    await orchestrator.telegram.start_polling()
    await orchestrator.start_autonomous_screener()
    await bus.publish(event("api.started", safe_mode=settings.safe_mode, version=__version__))
    yield
    await orchestrator.stop_autonomous_screener()
    await orchestrator.telegram.stop_polling()
    await bus.publish(event("api.stopped"))


app = FastAPI(
    title="New Era Trading Bot API",
    version=__version__,
    description=(
        "Enterprise AI Quant Platform: 4-Step Waterfall Pipeline (TA-Lib -> Kronos+Matrix -> L2 DOM -> Gemini CIO 'ჯოი'), "
        "8-Pair Autonomous Screener, Dynamic Position Management, and Telegram Remote Control."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allow_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "new-era-api",
        "version": __version__,
        "safe_mode": settings.safe_mode,
        "gemini_key_configured": settings.gemini_key_configured,
        "gemini": orchestrator.gemini_cio.gemini_status(),
        "autonomous_mode": orchestrator.gemini_cio.autonomous_mode,
        "dynamic_trailing_enabled": orchestrator.gemini_cio.dynamic_trailing_enabled,
        "screener_running": orchestrator._screener_running,
        "paused": orchestrator.paused,
        "telegram_configured": orchestrator.telegram.is_configured,
        "default_provider": settings.default_provider,
        "max_spread_points": settings.max_spread_points,
        "max_spread_to_atr": settings.max_spread_to_atr,
        "active_scan_id": orchestrator._active_scan_id,
        "websocket_subscribers": bus.subscriber_count,
        "memory": orchestrator.memory.health(),
        "controls": orchestrator.get_controls(),
        "decision_history_summary": orchestrator.decision_history_summary(),
        "decision_history_path": str(settings.decision_history_path),
        "ai": orchestrator.ai_status(),
        "ai_bot_factory": orchestrator.ai_status()["ai_bot_factory"],
    }


@app.get("/ai/status")
async def ai_status_endpoint() -> dict[str, Any]:
    return orchestrator.ai_status()


@app.get("/ai/proposals")
async def ai_proposals_endpoint() -> dict[str, Any]:
    return await orchestrator.get_ai_proposals()


@app.post("/ai/analyze")
async def ai_analyze_endpoint(request: AIAnalyzeRequest) -> dict[str, Any]:
    context = dict(request.context)
    for key in ("ohlcv", "indicators", "kronos_matrix", "l2_dom"):
        value = getattr(request, key)
        if value is not None:
            context[key] = value
    return await orchestrator.analyze_ai(request.symbol, context=context, source="manual")


@app.post("/mcp")
async def mcp_endpoint(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Expose the in-process read-only MCP JSON-RPC facade over the local API."""

    return await orchestrator.handle_mcp(payload)


@app.get("/controls")
async def controls_get() -> dict[str, Any]:
    """Return the normalized runtime decision profile and override metadata."""

    return orchestrator.get_controls()


@app.post("/controls")
async def controls_post(request: ControlsUpdateRequest) -> dict[str, Any]:
    """Apply a partial runtime decision-profile update and return its state."""

    try:
        return orchestrator.update_controls(request.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/decision-history")
async def decision_history(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, Any]:
    return orchestrator.decision_history(limit)


@app.get("/decision-history/summary")
async def decision_history_summary() -> dict[str, Any]:
    return orchestrator.decision_history_summary()


@app.get("/mt5/status")
async def mt5_status(max_symbols: int = Query(default=250, ge=10, le=2000)) -> dict[str, Any]:
    return await asyncio.to_thread(get_mt5_status, max_symbols)


@app.get("/mt5/symbol/{symbol}")
async def mt5_symbol(symbol: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_mt5_symbol_snapshot, symbol)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/mt5/positions")
async def mt5_positions(symbol: str | None = Query(default=None, max_length=32)) -> dict[str, Any]:
    return await orchestrator.get_open_positions(symbol=symbol)


@app.get("/mt5/history")
async def mt5_history(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=150, ge=5, le=1000),
) -> dict[str, Any]:
    return await orchestrator.get_trade_history(days=days, limit=limit)


@app.get("/screener/status")
async def screener_status() -> dict[str, Any]:
    return orchestrator.get_screener_status()


@app.post("/screener/run")
async def screener_run() -> dict[str, Any]:
    return await orchestrator.run_multi_pair_screener_cycle()


@app.post("/mt5/demo/execute")
async def mt5_demo_execute(request: DemoOrderRequest) -> dict[str, Any]:
    try:
        return await orchestrator.execute_demo_trade(
            symbol=request.symbol,
            side=request.side,
            volume=request.volume,
            sl=request.sl,
            tp=request.tp,
            comment=request.comment,
            allow_paper_fallback=request.allow_paper_fallback,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/mt5/demo/close")
async def mt5_demo_close(request: DemoCloseRequest) -> dict[str, Any]:
    try:
        return await orchestrator.close_demo_positions(
            symbol=request.symbol,
            ticket=request.ticket,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/mt5/demo/manage")
async def mt5_demo_manage(request: DemoManageRequest) -> dict[str, Any]:
    try:
        return await orchestrator.manage_demo_positions(
            symbol=request.symbol,
            price_override=request.price_override,
            atr_override=request.atr_override,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/waterfall/latest")
async def waterfall_latest() -> dict[str, Any]:
    gates = orchestrator.latest_gates()
    return {
        "waterfall": gates.get("waterfall"),
        "autonomous_mode": orchestrator.gemini_cio.autonomous_mode,
        "dynamic_trailing_enabled": orchestrator.gemini_cio.dynamic_trailing_enabled,
        "paused": orchestrator.paused,
    }


@app.post("/gemini/analyze")
async def gemini_analyze(request: GeminiAnalyzeRequest) -> dict[str, Any]:
    return await orchestrator.analyze_setup_with_joey(
        symbol=request.symbol,
        account_equity=request.account_equity,
    )


@app.post("/gemini/chat")
async def gemini_chat(request: GeminiChatRequest) -> dict[str, Any]:
    return await orchestrator.chat_with_joey(
        symbol=request.symbol,
        message=request.message,
    )


@app.get("/gemini/status")
async def gemini_status_endpoint(live: bool = Query(default=False)) -> dict[str, Any]:
    if live:
        return await asyncio.to_thread(orchestrator.gemini_cio.ping_gemini, 15.0)
    return orchestrator.gemini_cio.gemini_status()


@app.post("/gemini/autonomous")
async def gemini_autonomous(request: AutonomousModeRequest) -> dict[str, Any]:
    return orchestrator.set_autonomous_settings(
        autonomous_mode=request.autonomous_mode,
        dynamic_trailing_enabled=request.dynamic_trailing_enabled,
        paused=request.paused,
    )


@app.get("/telegram/status")
async def telegram_status() -> dict[str, Any]:
    return orchestrator.telegram.status_summary()


@app.post("/telegram/command")
async def telegram_command(request: TelegramCommandRequest) -> dict[str, Any]:
    reply = await orchestrator.telegram.handle_command_text(request.command)
    return {
        "ok": True,
        "command": request.command,
        "reply": reply,
        "paused": orchestrator.paused,
        "autonomous_mode": orchestrator.gemini_cio.autonomous_mode,
    }


@app.get("/l2/book/{symbol}")
async def l2_market_book(
    symbol: str,
    levels: int = Query(default=10, ge=3, le=25),
    publish: bool = Query(default=True),
) -> dict[str, Any]:
    try:
        return await orchestrator.MarketBookGet(symbol=symbol, levels=levels, publish_events=publish)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/l2/alerts")
async def l2_recent_alerts(limit: int = Query(default=40, ge=1, le=200)) -> dict[str, Any]:
    return {
        "alerts": orchestrator.orderflow.recent_alerts(limit=limit),
        "latest_orderflow": orchestrator.latest_gates().get("l2_orderflow"),
    }




@app.get("/bots/status")
async def bots_status() -> dict[str, Any]:
    try:
        registry = bot_manager.load_registry()
        validation_errors = bot_manager.validate_registry(registry)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    bots: list[dict[str, Any]] = []
    for bot in registry.get("bots", []):
        if not isinstance(bot, dict):
            continue
        bots.append(
            {
                "id": bot.get("id"),
                "display_name": bot.get("display_name"),
                "status": bot.get("status"),
                "enabled": bot.get("enabled", str(bot.get("status") or "").lower() not in {"disabled", "off"}),
                "symbol": bot.get("symbol"),
                "safety": bot.get("safety", {}),
            }
        )
    return {
        "active_bot": registry.get("active_bot"),
        "bots": bots,
        "registry_valid": not validation_errors,
        "validation_errors": validation_errors,
        "real_account_allowed": False,
    }


@app.post("/bots")
async def bots_add(request: BotProfileCreateRequest) -> dict[str, Any]:
    """Register a new research-only profile; no MT5 artifact is activated."""

    try:
        return bot_manager.add_bot_profile(
            request.display_name,
            bot_id=request.bot_id,
            symbol=request.symbol,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/bots/{bot_id}/enabled")
async def bots_set_enabled(bot_id: str, request: BotEnabledRequest) -> dict[str, Any]:
    try:
        return bot_manager.set_bot_enabled(bot_id, request.enabled)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/bots/{bot_id}")
async def bots_remove(bot_id: str) -> dict[str, Any]:
    try:
        return bot_manager.remove_bot_profile(bot_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _strategy_profile_payload(request: StrategySaveRequest) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True, by_alias=False)
    # ``module_path`` and ``class_name`` are accepted as convenient top-level
    # fields, while bot_lab persists them under the code descriptor.
    if payload.get("module_path") or payload.get("class_name"):
        code = dict(payload.get("code") or {})
        if payload.get("module_path"):
            code.setdefault("module_path", payload["module_path"])
        if payload.get("class_name"):
            code.setdefault("class_name", payload["class_name"])
        payload["code"] = code
    return payload


def _strategy_bars(request: BacktestRequest) -> list[dict[str, Any]] | None:
    rows = request.bars or request.ohlcv or request.data
    if rows is None:
        return None
    return [item if isinstance(item, dict) else item.model_dump() for item in rows]


def _strategy_target(request: BacktestRequest) -> str | dict[str, Any]:
    profile = request.profile or request.strategy
    if profile is not None:
        return profile.model_dump(exclude_none=True, by_alias=False)
    if request.strategy_id:
        return request.strategy_id
    raise HTTPException(status_code=422, detail="strategy_id or profile is required")


@app.get("/strategies")
async def strategies_list() -> dict[str, Any]:
    profiles = await asyncio.to_thread(strategy_lab.list_profiles)
    return {
        "strategies": profiles,
        "count": len(profiles),
        "research_only": True,
        "real_account_allowed": False,
        "order_execution_allowed": False,
    }


@app.get("/strategies/{strategy_id}")
async def strategies_get(strategy_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(strategy_lab.load_profile, strategy_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategies")
async def strategies_save(request: StrategySaveRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(strategy_lab.save_profile, _strategy_profile_payload(request))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/strategies/{strategy_id}")
async def strategies_save_at(strategy_id: str, request: StrategySaveRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            strategy_lab.save_profile,
            _strategy_profile_payload(request),
            strategy_id=strategy_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategies/backtest")
async def strategies_backtest(request: BacktestRequest) -> dict[str, Any]:
    target = _strategy_target(request)
    try:
        return await asyncio.to_thread(
            strategy_lab.backtest,
            target,
            _strategy_bars(request),
            data_path=request.data_path,
            initial_cash=request.initial_cash,
            position_size=request.position_size,
            commission=request.commission,
            slippage=request.slippage,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategies/walk-forward")
async def strategies_walk_forward(request: WalkForwardRequest) -> dict[str, Any]:
    target = _strategy_target(request)
    try:
        return await asyncio.to_thread(
            strategy_lab.walk_forward,
            target,
            _strategy_bars(request),
            data_path=request.data_path,
            train_ratio=request.train_ratio,
            train_bars=request.train_bars,
            initial_cash=request.initial_cash,
            position_size=request.position_size,
            commission=request.commission,
            slippage=request.slippage,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/strategies/load")
async def strategies_load(request: StrategyLoadRequest) -> dict[str, Any]:
    try:
        loaded = await asyncio.to_thread(
            strategy_lab.load_strategy,
            strategy_id=request.strategy_id,
            module_path=request.module_path,
            class_name=request.class_name,
        )
        return loaded.metadata()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/gates/latest")
async def gates_latest() -> dict[str, Any]:
    return orchestrator.latest_gates()


async def _queue_scan(request: ScanRequest, mode: str) -> ScanResponse:
    try:
        job = await orchestrator.start_scan(request.model_dump(), mode)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ScanResponse(
        scan_id=job.scan_id,
        status="queued",
        mode=mode,  # type: ignore[arg-type]
        message="scan queued; follow /scans/{scan_id} or connect to /ws/logs",
    )


@app.post("/scan/manual", response_model=ScanResponse, status_code=202)
async def scan_manual(request: ScanRequest) -> ScanResponse:
    return await _queue_scan(request, "manual")


@app.post("/scan/automatic", response_model=ScanResponse, status_code=202)
async def scan_automatic(request: ScanRequest) -> ScanResponse:
    request.include_tradingagents = True
    request.include_kronos = True
    return await _queue_scan(request, "automatic")


@app.get("/scans", response_model=list[ScanStatus])
async def scans() -> list[dict[str, Any]]:
    return orchestrator.list_jobs()


@app.get("/scans/{scan_id}", response_model=ScanStatus)
async def scan_status(scan_id: str) -> dict[str, Any]:
    job = orchestrator.get_job(scan_id)
    if job is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return job.public()


@app.get("/memory/search", response_model=MemorySearchResponse)
async def memory_search(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=5, ge=1, le=20),
) -> MemorySearchResponse:
    results = await asyncio.to_thread(orchestrator.memory.search, q, limit)
    return MemorySearchResponse(query=q, results=results)


# ---------------------------------------------------------------------------
# New Era 2.0 — Signal Intelligence, Execution Monitor, Analytics, Providers & SaaS
# ---------------------------------------------------------------------------
@app.get("/intelligence/signals")
async def intelligence_signals(
    search: str = Query(default="", max_length=200),
    symbol: str = Query(default=""),
    strategy: str = Query(default=""),
    direction: str = Query(default=""),
    lifecycle_status: str = Query(default=""),
    blocking_category: str = Query(default=""),
    blocking_agent: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    limit: int = Query(default=250, ge=1, le=2000),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        orchestrator.historical_intelligence.query_signals,
        search=search,
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        lifecycle_status=lifecycle_status,
        blocking_category=blocking_category,
        blocking_agent=blocking_agent,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


@app.get("/intelligence/signals/export")
async def intelligence_signals_export(
    fmt: str = Query(default="csv"),
    search: str = Query(default=""),
    symbol: str = Query(default=""),
    lifecycle_status: str = Query(default=""),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    queried = await asyncio.to_thread(
        orchestrator.historical_intelligence.query_signals,
        search=search,
        symbol=symbol,
        lifecycle_status=lifecycle_status,
        limit=limit,
    )
    exported = orchestrator.historical_intelligence.export_signals(queried["signals"], fmt=fmt)
    return {
        "format": fmt.lower(),
        "count": len(queried["signals"]),
        "content": exported,
    }


@app.get("/intelligence/signals/{signal_id}")
async def intelligence_signal_inspector(signal_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(orchestrator.historical_intelligence.get_signal_inspector, signal_id)


@app.get("/intelligence/why-no-trade")
async def intelligence_why_no_trade() -> dict[str, Any]:
    gates = orchestrator.latest_gates()
    return await asyncio.to_thread(orchestrator.historical_intelligence.build_why_no_trade_diagnostics, gates)


@app.get("/intelligence/council")
async def intelligence_ai_council() -> dict[str, Any]:
    gates = orchestrator.latest_gates()
    return await asyncio.to_thread(orchestrator.historical_intelligence.build_ai_council_view, gates)


@app.get("/execution/pipeline")
async def execution_pipeline_status() -> dict[str, Any]:
    gates = orchestrator.latest_gates()
    mt5_st = await asyncio.to_thread(get_mt5_status, 15)
    combined = {**gates, "mt5": mt5_st}
    return await asyncio.to_thread(orchestrator.historical_intelligence.build_execution_pipeline, combined)


@app.get("/analytics/performance")
async def analytics_performance(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    hist = await orchestrator.get_trade_history(days=days, limit=500)
    trades = hist.get("trades") if isinstance(hist, dict) else None
    return await asyncio.to_thread(orchestrator.historical_intelligence.build_performance_analytics, trades)


@app.get("/intelligence/bot-performance")
async def intelligence_bot_performance(refresh_live_positions: bool = Query(default=True)) -> dict[str, Any]:
    return await asyncio.to_thread(
        orchestrator.bot_intelligence.build_full_intelligence_report,
        refresh_live_positions=refresh_live_positions,
    )


@app.get("/intelligence/bot-performance/export")
async def intelligence_bot_performance_export(fmt: str = Query(default="json")) -> dict[str, Any]:
    report = await asyncio.to_thread(
        orchestrator.bot_intelligence.build_full_intelligence_report,
        refresh_live_positions=False,
    )
    if fmt.lower() == "csv":
        content = await asyncio.to_thread(orchestrator.bot_intelligence.export_trades_csv, report.get("closed_trades"))
        return {"ok": True, "format": "csv", "count": len(report.get("closed_trades") or []), "content": content}
    return {
        "ok": True,
        "format": "json",
        "count": len(report.get("closed_trades") or []),
        "content": json.dumps(report, indent=2, ensure_ascii=False),
    }


@app.get("/logs/unified")
async def logs_unified(
    category: str = Query(default="ALL"),
    search: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    ev_hist = list(getattr(bus, "_history", []))
    return await asyncio.to_thread(
        orchestrator.historical_intelligence.build_unified_logs,
        category=category,
        search=search,
        limit=limit,
        event_history=ev_hist,
    )


@app.get("/providers/status")
async def providers_status() -> dict[str, Any]:
    return orchestrator.gemini_cio.provider_router.status_snapshot()


@app.post("/providers/config")
async def providers_config(payload: dict[str, Any]) -> dict[str, Any]:
    return orchestrator.gemini_cio.provider_router.configure(payload)


@app.get("/saas/architecture")
async def saas_architecture() -> dict[str, Any]:
    return orchestrator.saas.architecture_overview()


@app.get("/saas/session")
async def saas_session() -> dict[str, Any]:
    return {
        "authenticated": True,
        "mode": "local_demo_operator",
        "environment": "DEMO_ONLY",
        "operator": orchestrator.saas.current_operator().to_public_dict(),
    }


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json(event("websocket.connected", safe_mode=settings.safe_mode))
    try:
        async for message in bus.subscribe():
            await websocket.send_json(message)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main_api:app", host=settings.host, port=settings.port, reload=False)
