from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot_manager
from newera_api.ai_engine import SafeDecision, normalize_model_result
from newera_api.config import ApiSettings
from newera_api.gemini_manager import Step1TALibResult, TALibTechnicalEngine
from newera_api.mcp_server import MCPServer
from newera_api.orchestrator import Orchestrator

try:  # FastAPI is optional in the bare stdlib test environment.
    import main_api
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal envs
    main_api = None  # type: ignore[assignment]


def step1_fixture(symbol: str = "XAUUSD") -> Step1TALibResult:
    return Step1TALibResult(
        symbol=symbol,
        passed=True,
        trend_bias="BUY",
        filter_score=82.0,
        close=100.0,
        rsi_14=58.0,
        atr_14=1.0,
        ema_20=100.5,
        ema_50=100.0,
        ema_200=99.0,
        macd_line=0.4,
        macd_signal=0.2,
        macd_hist=0.2,
        bb_upper=102.0,
        bb_middle=100.0,
        bb_lower=98.0,
        adx_14=28.0,
        veto_buy=False,
        veto_sell=False,
        veto_reason="",
        engine="fixture",
        summary="deterministic fixture",
    )


def l2_fixture() -> dict[str, object]:
    return {
        "mid_price": 100.0,
        "best_bid": 99.9,
        "best_ask": 100.1,
        "l2_direction": "BUY",
        "confluence_score": 82.0,
        "veto_buy": False,
        "veto_sell": False,
    }


def kronos_fixture(now: int) -> dict[str, object]:
    return {
        "symbol": "XAUUSD",
        "action": "BUY",
        "confidence": 0.9,
        "generated_epoch": now,
        "expires_epoch": now + 300,
        "decision": {
            "matrix": {"direction": "BUY", "score": 6.5, "buy_score": 6.5, "sell_score": 0.0},
            "analog": {"direction": "BUY", "win_rate": 0.875},
            "forecast": {"direction": "BUY", "move_atr": 1.45, "path_agreement": 0.85},
            "hard_blocks": [],
        },
    }


class FakeAIEngine:
    model = "deepseek-r1:8b"
    endpoint = "http://ollama.test/v1/chat/completions"
    offline = False

    def __init__(self, result: SafeDecision) -> None:
        self.result = result
        self.contexts: list[dict] = []

    async def safe_call_async(self, context: dict, *, symbol: str | None = None) -> SafeDecision:
        self.contexts.append(context)
        return self.result


def ai_decision(*, proposals: list[dict] | None = None) -> SafeDecision:
    raw = {
        "action": "BUY",
        "confidence": 0.82,
        "risk_pct": 1.0,
        "min_rr": 1.8,
        "max_sl_atr": 2.0,
        "time_stop_minutes": 60,
        "reasoning_summary": "Concise research rationale.",
        "evidence": [{"source": "kronos", "direction": "BUY"}],
        "bot_proposals": proposals or [],
    }
    return normalize_model_result(raw, symbol="XAUUSD")


class AIIntegrationTests(unittest.TestCase):
    def test_manual_analysis_bounds_context_persists_and_deduplicates_proposals(self) -> None:
        proposal = {
            "id": "ai-range-study",
            "name": "AI Range Study",
            "builder": "visual",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "flow": {"actions": [{"type": "enter_long"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(
                root=root,
                memory_dir=root / "memory",
                report_dir=root / "reports",
                ai_offline=False,
            )
            orchestrator = Orchestrator(settings)
            fake = FakeAIEngine(ai_decision(proposals=[proposal, dict(proposal)]))
            orchestrator.ai_engine = fake  # type: ignore[assignment]

            result = asyncio.run(
                orchestrator.analyze_ai(
                    "xauusd",
                    context={
                        "ohlcv": [
                            {"open": float(index), "high": float(index + 1), "low": float(index - 1), "close": float(index), "volume": 1.0}
                            for index in range(100)
                        ],
                        "indicators": {"rsi_14": 55.0},
                        "kronos_matrix": {"candidate_direction": "BUY"},
                        "l2_dom": {"l2_direction": "BUY"},
                    },
                )
            )

            self.assertEqual(result["status"], "RESEARCH_ONLY")
            self.assertEqual(result["context"]["ohlcv_bars"], 32)
            self.assertEqual(result["proposal_persistence"]["persisted"], 1)
            self.assertEqual(result["proposal_persistence"]["deduplicated"], 1)
            self.assertFalse(result["decision"]["bot_proposals"][0]["enabled"])
            self.assertFalse(result["decision"]["bot_proposals"][0]["order_execution_allowed"])
            self.assertEqual(len(fake.contexts), 1)
            self.assertEqual(fake.contexts[0]["symbol"], "XAUUSD")
            self.assertEqual(len(orchestrator.strategy_lab.list_profiles()), 1)
            saved = orchestrator.strategy_lab.list_profiles()[0]
            self.assertEqual(saved["status"], "research_only")
            self.assertFalse(saved["safety"]["real_account_allowed"])
            self.assertFalse(saved["safety"]["order_execution_allowed"])
            listed = asyncio.run(orchestrator.get_ai_proposals())
            self.assertFalse(listed["proposals"][0]["enabled"])
            self.assertTrue(listed["proposals"][0]["research_only"])
            self.assertFalse(listed["proposals"][0]["order_execution_allowed"])
            self.assertFalse(orchestrator.ai_status()["ai_bot_factory"]["registry_enabled"])
            self.assertEqual(orchestrator.ai_status()["counts"]["proposals_persisted"], 1)

    def test_offline_analysis_fails_closed_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
            orchestrator = Orchestrator(settings)
            calls = 0

            def forbidden_transport(*args, **kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError("offline AI must not call transport")

            orchestrator.ai_engine._opener = forbidden_transport
            result = asyncio.run(orchestrator.analyze_ai("EURUSD", context={"ohlcv": []}))
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["availability"], "offline")
            self.assertFalse(result["decision"]["execution_allowed"])
            self.assertEqual(calls, 0)

    def test_ai_telemetry_redacts_hidden_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports", ai_offline=False)
            orchestrator = Orchestrator(settings)
            setattr(
                orchestrator,
                "ai_engine",
                FakeAIEngine(
                    SafeDecision(
                        action="BUY",
                        confidence=0.8,
                        risk_pct=1.0,
                        min_rr=1.5,
                        max_sl_atr=2.0,
                        time_stop_minutes=60,
                        reasoning_summary="public rationale",
                        evidence=[{"summary": "<think>private chain</think>public evidence"}],
                        status="RESEARCH_ONLY",
                        symbol="XAUUSD",
                    )
                ),
            )
            result = asyncio.run(orchestrator.analyze_ai("XAUUSD", context={"ohlcv": []}))
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("private chain", rendered)
            self.assertIn("public evidence", rendered)

    def test_registry_factory_is_opt_in_and_stays_disabled(self) -> None:
        proposal = {
            "id": "ai-registry-study",
            "name": "AI Registry Study",
            "builder": "visual",
            "symbol": "EURUSD",
            "timeframe": "M5",
            "flow": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(
                root=root,
                memory_dir=root / "memory",
                report_dir=root / "reports",
                ai_offline=False,
                ai_bot_factory_registry_enabled=True,
            )
            (root / "bot_registry.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workspace_name": "test",
                        "active_bot": "baseline",
                        "shared_rules": {"real_account_allowed": False},
                        "bots": [
                            {
                                "id": "baseline",
                                "display_name": "Baseline",
                                "status": "research_only",
                                "symbol": "XAUUSD",
                                "execution": {"kind": "research_profile", "source": None},
                                "safety": {"real_account_allowed": False},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            orchestrator = Orchestrator(settings)
            orchestrator.ai_engine = FakeAIEngine(ai_decision(proposals=[proposal]))  # type: ignore[assignment]
            asyncio.run(orchestrator.analyze_ai("EURUSD", context={"ohlcv": []}))
            registry = json.loads((root / "bot_registry.json").read_text(encoding="utf-8"))
            created = next(item for item in registry["bots"] if item["id"] == "ai-registry-study")
            self.assertFalse(created["enabled"])
            self.assertEqual(created["status"], "research_only")
            self.assertFalse(created["safety"]["real_account_allowed"])
            self.assertFalse(created["safety"]["order_execution_allowed"])
            with self.assertRaises(ValueError):
                bot_manager.set_bot_enabled("ai-registry-study", True, path=root / "bot_registry.json")

    def test_screener_result_contains_ai_telemetry_and_event(self) -> None:
        async def run_cycle() -> tuple[dict, AsyncMock]:
            now = int(time.time())
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
                orchestrator = Orchestrator(settings)
                orchestrator.screener_symbols = ["XAUUSD"]
                orchestrator.get_open_positions = AsyncMock(return_value={"positions": []})  # type: ignore[method-assign]
                orchestrator.MarketBookGet = AsyncMock(return_value=l2_fixture())  # type: ignore[method-assign]
                orchestrator.ai_engine = FakeAIEngine(ai_decision())  # type: ignore[assignment]
                settings.report_dir.joinpath("kronos_matrix_latest.json").write_text(
                    json.dumps(kronos_fixture(now)), encoding="utf-8"
                )
                settings.report_dir.joinpath("tradingagents_latest.json").write_text(
                    json.dumps(
                        {
                            "user_symbol": "XAUUSD",
                            "ai_action": "BUY",
                            "generated_epoch": now,
                            "mt5_gate_expires_epoch": now + 300,
                        }
                    ),
                    encoding="utf-8",
                )
                publish = AsyncMock()
                orchestrator.bus.publish = publish
                with (
                    patch("newera_api.orchestrator.get_mt5_status", return_value={"account": {"equity": 1000.0}}),
                    patch("newera_api.orchestrator.fetch_mt5_bars_to_csv", return_value={"last_close": 100.0, "bid": 100.0, "ask": 100.1, "atr_m5": 1.0}),
                    patch.object(TALibTechnicalEngine, "evaluate_from_csv", return_value=step1_fixture()),
                ):
                    payload = await orchestrator.run_multi_pair_screener_cycle()
                return payload, publish

        payload, publish = asyncio.run(run_cycle())
        self.assertEqual(payload["ai"]["analyses"], 1)
        self.assertEqual(payload["results"][0]["ai"]["source"], "screener")
        self.assertFalse(payload["results"][0]["ai"]["execution_allowed"])
        self.assertTrue(any(call.args[0]["type"] == "ai.engine.decision" for call in publish.await_args_list))

    def test_completed_scan_result_contains_ai_telemetry(self) -> None:
        async def run_scan() -> dict:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
                orchestrator = Orchestrator(settings)
                orchestrator.ai_engine = FakeAIEngine(ai_decision())  # type: ignore[assignment]
                orchestrator.MarketBookGet = AsyncMock(return_value=l2_fixture())  # type: ignore[method-assign]
                orchestrator._has_tradingagents_api_key = lambda: False  # type: ignore[method-assign]
                orchestrator._run_powershell_phase = AsyncMock(  # type: ignore[method-assign]
                    side_effect=lambda job, source, script, args, request: {"source": source, "return_code": 0}
                )
                orchestrator._run_python_phase = AsyncMock(  # type: ignore[method-assign]
                    return_value={"source": "gate-combiner", "return_code": 0}
                )
                target_csv = root / "market.csv"
                target_csv.write_text(
                    "timestamp,open,high,low,close,volume\n1,99,101,98,100,10\n",
                    encoding="utf-8",
                )
                orchestrator._sync_mt5_bars_for_symbol = AsyncMock(  # type: ignore[method-assign]
                    return_value=(target_csv, 1.0, 0.01, {"output_csv": str(target_csv), "last_close": 100.0, "atr_m5": 1.0})
                )
                with patch.object(TALibTechnicalEngine, "evaluate_from_csv", return_value=step1_fixture()):
                    job = await orchestrator.start_scan(
                        {
                            "symbol": "XAUUSD",
                            "include_tradingagents": True,
                            "include_kronos": True,
                        },
                        "manual",
                    )
                    assert job.task is not None
                    await job.task
                    self.assertEqual(job.status, "completed")
                    return job.result or {}

        result = asyncio.run(run_scan())
        self.assertEqual(result["ai"]["source"], "scan")
        self.assertFalse(result["ai"]["execution_allowed"])
        self.assertTrue(result["ai"]["context"]["has_step1_indicators"])
        self.assertTrue(result["ai"]["context"]["has_step2_kronos"])
        self.assertTrue(result["ai"]["context"]["has_l2_snapshot"])

    def test_api_routes_and_mcp_reject_order_tools(self) -> None:
        if main_api is None:
            self.skipTest("FastAPI dependencies are not installed")
        api_module = main_api
        paths = {getattr(route, "path", "") for route in api_module.app.routes}
        self.assertTrue({"/ai/status", "/ai/proposals", "/ai/analyze", "/mcp"}.issubset(paths))
        response = asyncio.run(
            api_module.mcp_endpoint(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "place_order", "arguments": {}}}
            )
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response["error"]["code"], -32602)
        self.assertFalse(response.get("result"))

    def test_api_route_declarations_are_present_without_optional_imports(self) -> None:
        source = Path(__file__).with_name("main_api.py").read_text(encoding="utf-8")
        for route in ('@app.get("/ai/status")', '@app.get("/ai/proposals")', '@app.post("/ai/analyze")', '@app.post("/mcp")'):
            self.assertIn(route, source)

    def test_mcp_facade_rejects_order_tool_without_network_server(self) -> None:
        server = MCPServer()
        response = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "execute_order", "arguments": {}}}
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
