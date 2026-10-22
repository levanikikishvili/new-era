import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from newera_api.controls import RuntimeControls
from newera_api.config import ApiSettings
from newera_api.events import EventBus
from newera_api.gemini_manager import GeminiPortfolioManager
from newera_api.orchestrator import Orchestrator
from newera_api.trade_logger import TradeDecisionLogger


def _step1_fixture():
    from newera_api.gemini_manager import Step1TALibResult

    return Step1TALibResult(
        symbol="XAUUSD",
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
        summary="fixture",
    )


def _kronos_fixture():
    return {
        "symbol": "XAUUSD",
        "action": "BUY",
        "confidence": 0.9,
        "decision": {
            "matrix": {"direction": "BUY", "score": 6.5, "buy_score": 6.5, "sell_score": 0.0},
            "analog": {"direction": "BUY", "win_rate": 0.9},
            "forecast": {"direction": "BUY", "move_atr": 1.4, "path_agreement": 0.9},
            "hard_blocks": [],
        },
    }


def _l2_fixture():
    return {
        "mid_price": 100.0,
        "best_bid": 99.9,
        "best_ask": 100.1,
        "l2_direction": "BUY",
        "confluence_score": 82.0,
        "veto_buy": False,
        "veto_sell": False,
        "nearest_buy_wall": {"price": 99.0, "volume": 10.0},
    }


class TradeLoggerAndControlsTests(unittest.TestCase):
    def test_persistence_bounded_readback_summary_and_event(self):
        async def run() -> tuple[list[dict], dict]:
            with tempfile.TemporaryDirectory() as directory:
                bus = EventBus()
                logger = TradeDecisionLogger(Path(directory) / "logs" / "decision_history.json", bus, max_records=3)
                for index in range(5):
                    await logger.record_decision(
                        scan_id=f"scan-{index}",
                        symbol="XAUUSD",
                        signal="BUY" if index % 2 else "ABSTAIN",
                        action="BUY" if index % 2 else "ABSTAIN",
                        final_decision="BUY" if index % 2 else "ABSTAIN",
                        execution_mode="SHADOW" if index % 2 else "BLOCKED",
                        reason="shadow signal" if index % 2 else "waterfall blocked",
                        block_reason="" if index % 2 else "waterfall_abstain",
                        waterfall={
                            "rationale_ka": "test rationale",
                            "steps": {
                                "step1_talib": {"passed": True},
                                "step2_kronos_matrix": {"components_agree": index % 2 == 1},
                                "step3_l2_safety": {"vetoed": False},
                                "step4_gemini_cio": {"final_action": "BUY" if index % 2 else "ABSTAIN"},
                            },
                        },
                    )
                return logger.read_records(), logger.summary()

        records, summary = asyncio.run(run())
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["scan_id"], "scan-4")
        self.assertEqual(records[0]["waterfall_layer_statuses"]["step2_kronos_matrix"], "BLOCKED")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["execution_mode"]["SHADOW"], 1)
        self.assertEqual(summary["block_reason"]["waterfall_abstain"], 2)

    def test_concurrent_async_records_are_not_lost_and_event_type_is_stable(self):
        async def run() -> tuple[list[dict], AsyncMock]:
            with tempfile.TemporaryDirectory() as directory:
                bus = AsyncMock()
                logger = TradeDecisionLogger(Path(directory) / "decision_history.json", bus, max_records=50)
                await asyncio.gather(
                    *(
                        logger.record_decision(
                            scan_id=f"scan-{index}",
                            symbol="EURUSD",
                            signal="ABSTAIN",
                            action="ABSTAIN",
                            final_decision="ABSTAIN",
                            execution_mode="BLOCKED",
                            reason="blocked",
                            block_reason="test_block",
                        )
                        for index in range(20)
                    )
                )
                return logger.read_records(), bus

        records, bus = asyncio.run(run())
        self.assertEqual(len(records), 20)
        self.assertEqual(bus.publish.await_count, 20)
        self.assertTrue(all(call.args[0]["type"] == "trade.decision" for call in bus.publish.await_args_list))

    def test_controls_validate_and_disabled_layers_are_bypassed(self):
        controls = RuntimeControls()
        self.assertFalse(controls.snapshot()["override_active"])
        controls.update({"l2_enabled": False, "confluence_mode": "3-of-3", "risk_pct": 0.5})
        snapshot = controls.snapshot()
        self.assertTrue(snapshot["override_active"])
        self.assertEqual(set(snapshot["override_fields"]), {"l2_enabled", "confluence_mode", "risk_pct"})
        with self.assertRaises(ValueError):
            controls.update({"risk_pct": 5.1})
        with self.assertRaises(ValueError):
            controls.update({"confluence_mode": "1-of-3"})

        controls.update({"risk_pct": 5.0, "min_rr": 1.0, "max_sl_atr": 5.0, "time_stop_minutes": 240})
        self.assertEqual(controls.values()["risk_pct"], 5.0)
        self.assertEqual(controls.execution_values()["risk_pct"], 1.5)
        self.assertEqual(controls.execution_values()["min_rr"], 1.5)
        self.assertEqual(controls.execution_values()["max_sl_atr"], 2.5)
        self.assertEqual(controls.execution_values()["time_stop_minutes"], 120)

        manager = GeminiPortfolioManager()
        decision = manager.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=_step1_fixture(),
            kronos_report=_kronos_fixture(),
            tradingagents_report={"ai_action": "BUY"},
            l2_snapshot=None,
            mt5_meta={"bid": 100.0, "ask": 100.1, "atr_m5": 1.0, "digits": 2, "point": 0.01},
            controls=controls,
        )
        self.assertEqual(decision.step3_l2_safety["status"], "BYPASSED")
        self.assertEqual(decision.step3_l2_safety["reason"], "L2 radar bypassed by runtime controls")
        self.assertLessEqual(decision.risk_pct, 0.5)

        controls.update({"kronos_enabled": False})
        bypassed_kronos = manager.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=_step1_fixture(),
            kronos_report=_kronos_fixture(),
            tradingagents_report={"ai_action": "BUY"},
            l2_snapshot=_l2_fixture(),
            mt5_meta={"bid": 100.0, "ask": 100.1, "atr_m5": 1.0, "digits": 2, "point": 0.01},
            controls=controls,
        )
        self.assertEqual(bypassed_kronos.step2_kronos_matrix["status"], "BYPASSED")
        self.assertEqual(bypassed_kronos.step2_kronos_matrix["candidate_direction"], "BUY")
        self.assertEqual(bypassed_kronos.final_action, "BUY")

        controls.update({"kronos_enabled": False, "l2_enabled": False, "confluence_mode": "raw", "risk_pct": 1.5})
        bypassed = manager.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=_step1_fixture(),
            kronos_report=None,
            tradingagents_report={"ai_action": "BUY"},
            l2_snapshot=None,
            mt5_meta={"bid": 100.0, "ask": 100.1, "atr_m5": 1.0, "digits": 2, "point": 0.01},
            controls=controls,
        )
        self.assertEqual(bypassed.step2_kronos_matrix["status"], "BYPASSED")
        self.assertEqual(bypassed.step3_l2_safety["status"], "BYPASSED")
        self.assertEqual(bypassed.step2_kronos_matrix["candidate_direction"], "BUY")
        self.assertEqual(bypassed.final_action, "BUY")

    def test_failed_scan_still_persists_a_blocked_decision(self):
        async def run() -> list[dict]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
                orchestrator = Orchestrator(settings)
                job = await orchestrator.start_scan(
                    {
                        "symbol": "XAUUSD",
                        "include_tradingagents": False,
                        "include_kronos": False,
                    },
                    "manual",
                )
                assert job.task is not None
                await job.task
                return orchestrator.decision_logger.read_records()

        records = asyncio.run(run())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["execution_mode"], "BLOCKED")
        self.assertEqual(records[0]["final_decision"], "ABSTAIN")
        self.assertEqual(records[0]["block_reason"], "scan_error")


if __name__ == "__main__":
    unittest.main()
