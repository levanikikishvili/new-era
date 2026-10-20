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
from newera_api.config import settings
from newera_api.events import EventBus, event
from newera_api.mt5_bridge import get_mt5_status, get_mt5_symbol_snapshot
from newera_api.orchestrator import Orchestrator
from newera_api.schemas import (
    AutonomousModeRequest,
    DemoCloseRequest,
    DemoManageRequest,
    DemoOrderRequest,
    GeminiAnalyzeRequest,
    GeminiChatRequest,
    MemorySearchResponse,
    ScanRequest,
    ScanResponse,
    ScanStatus,
    TelegramCommandRequest,
)


bus = EventBus()
orchestrator = Orchestrator(settings, bus)


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
    allow_methods=["GET", "POST"],
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
    }


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
