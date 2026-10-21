import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from newera_api.config import ApiSettings
from newera_api.gemini_manager import GeminiPortfolioManager, Step1TALibResult, TALibTechnicalEngine
from newera_api.orchestrator import (
    Orchestrator,
    build_joey_profile_comment,
    is_joey_live_auto_execution_symbol,
)


def step1_fixture() -> Step1TALibResult:
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
        "nearest_buy_wall": {"price": 99.0, "volume": 10.0},
    }


def kronos_fixture(
    *,
    symbol: str = "XAUUSD",
    matrix: str = "BUY",
    analogue: str = "BUY",
    forecast: str = "BUY",
    generated_epoch: int | None = None,
    expires_epoch: int | None = None,
) -> dict[str, object]:
    matrix_score = 6.5 if matrix in {"BUY", "SELL"} else 0.0
    buy_score = matrix_score if matrix == "BUY" else 0.0
    sell_score = matrix_score if matrix == "SELL" else 0.0
    forecast_move_atr = 1.45 if forecast == "BUY" else -1.45 if forecast == "SELL" else 0.0
    report: dict[str, object] = {
        "symbol": symbol,
        "action": matrix if matrix == analogue == forecast else "ABSTAIN",
        "confidence": 0.9,
        "decision": {
            "matrix": {
                "direction": matrix,
                "score": matrix_score,
                "buy_score": buy_score,
                "sell_score": sell_score,
            },
            "analog": {"direction": analogue, "win_rate": 0.875},
            "forecast": {
                "direction": forecast,
                "move_atr": forecast_move_atr,
                "path_agreement": 0.85,
            },
            "hard_blocks": [],
        },
    }
    if generated_epoch is not None:
        report["generated_epoch"] = generated_epoch
    if expires_epoch is not None:
        report["expires_epoch"] = expires_epoch
    return report


class WaterfallSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = GeminiPortfolioManager()

    def test_runtime_gate_reader_requires_fresh_versioned_symbol_bound_contract(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
            orchestrator = Orchestrator(settings)
            gate_path = root / "gate.csv"
            header = "version,strategy,symbol,action,confidence,generated_epoch,expires_epoch\n"

            def write_gate(row: str) -> None:
                gate_path.write_text(header + row + "\n", encoding="ascii")

            write_gate(f"1,new-era-kronos-matrix-v1,XAUUSD,BUY,0.84,{now},{now + 300}")
            parsed = orchestrator._read_gate(
                gate_path,
                expected_strategy="new-era-kronos-matrix-v1",
                expected_symbol="XAUUSD",
            )
            self.assertEqual(parsed["action"] if parsed else None, "BUY")

            invalid_rows = (
                f"1,new-era-kronos-matrix-v1,EURUSD,BUY,0.84,{now},{now + 300}",
                f"1,unexpected-strategy,XAUUSD,BUY,0.84,{now},{now + 300}",
                f"2,new-era-kronos-matrix-v1,XAUUSD,BUY,0.84,{now},{now + 300}",
                f"1,new-era-kronos-matrix-v1,XAUUSD,BUY,1.20,{now},{now + 300}",
                f"1,new-era-kronos-matrix-v1,XAUUSD,BUY,0.84,{now - 500},{now - 1}",
            )
            for row in invalid_rows:
                with self.subTest(row=row):
                    write_gate(row)
                    self.assertIsNone(
                        orchestrator._read_gate(
                            gate_path,
                            expected_strategy="new-era-kronos-matrix-v1",
                            expected_symbol="XAUUSD",
                        )
                    )

    def evaluate(self, report: dict[str, object], *, tradingagents: dict[str, object] | None = None):
        return self.manager.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=step1_fixture(),
            kronos_report=report,
            tradingagents_report=tradingagents,
            l2_snapshot=l2_fixture(),
            mt5_meta={"bid": 100.0, "ask": 100.1, "atr_m5": 1.0, "digits": 2, "point": 0.01},
        )

    def test_matrix_abstain_with_sell_analogue_and_forecast_abstains(self):
        decision = self.evaluate(
            kronos_fixture(matrix="ABSTAIN", analogue="SELL", forecast="SELL")
        )

        self.assertEqual(decision.final_action, "ABSTAIN")
        self.assertEqual(decision.recommended_lot, 0.0)
        self.assertFalse(decision.step2_kronos_matrix["components_agree"])

    def test_step2_accepts_two_of_three_qualified_votes(self):
        decision = self.evaluate(
            kronos_fixture(matrix="BUY", analogue="BUY", forecast="SELL")
        )

        self.assertEqual(decision.final_action, "BUY")
        self.assertEqual(decision.step2_kronos_matrix["selected_direction"], "BUY")
        self.assertEqual(decision.step2_kronos_matrix["confluence_count"], 2)
        self.assertEqual(decision.step2_kronos_matrix["confluence_label"], "2-of-3")
        self.assertEqual(
            decision.step2_kronos_matrix["supporting_components"],
            ["Matrix", "Analogue"],
        )
        self.assertTrue(decision.step2_kronos_matrix["components_agree"])

    def test_step2_rejects_direction_contradicted_by_talib_trend(self):
        decision = self.evaluate(
            kronos_fixture(matrix="SELL", analogue="SELL", forecast="BUY")
        )

        self.assertEqual(decision.final_action, "ABSTAIN")
        self.assertEqual(decision.recommended_lot, 0.0)
        self.assertEqual(decision.step2_kronos_matrix["selected_direction"], "SELL")
        self.assertEqual(decision.step2_kronos_matrix["confluence_count"], 2)
        self.assertEqual(decision.step2_kronos_matrix["confluence_label"], "2-of-3")
        self.assertTrue(decision.step2_kronos_matrix["talib_trend_contradiction"])
        self.assertTrue(decision.talib_vetoed)
        self.assertFalse(decision.step2_kronos_matrix["components_agree"])

    def test_joey_risk_profile_caps_risk_reward_and_stop_distance(self):
        sizing = self.manager.compute_dynamic_risk_and_lot(
            99.0,
            l2_wall_support=True,
            equity=10000.0,
            atr=2.0,
            sl_atr_mult=4.0,
            tp_atr_mult=1.0,
            entry_price=100.0,
            side="BUY",
            digits=2,
            point=0.01,
            tick_value=1.0,
            tick_size=0.01,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
        )

        self.assertEqual(sizing["action"], "BUY")
        self.assertLessEqual(sizing["risk_pct"], 1.5)
        self.assertLessEqual(sizing["realized_risk_pct"], 1.5)
        self.assertLessEqual(sizing["sl_atr_multiple"], 2.5)
        self.assertLessEqual(sizing["sl_distance"], 2.5 * 2.0)
        self.assertGreaterEqual(sizing["reward_risk_ratio"], 1.5)
        self.assertGreaterEqual(sizing["tp_distance"], sizing["sl_distance"] * 1.5)

    def test_expired_or_symbol_mismatched_report_abstains(self):
        now = int(time.time())
        cases = (
            kronos_fixture(generated_epoch=now - 120, expires_epoch=now - 1),
            kronos_fixture(symbol="EURUSD"),
        )
        for report in cases:
            with self.subTest(report=report):
                decision = self.evaluate(report)
                self.assertEqual(decision.final_action, "ABSTAIN")
                self.assertEqual(decision.recommended_lot, 0.0)
                self.assertFalse(decision.research_evidence_valid)

    def test_stale_tradingagents_report_cannot_create_signal_from_fresh_kronos(self):
        now = int(time.time())
        stale_ta = {
            "user_symbol": "XAUUSD",
            "ai_action": "BUY",
            "generated_epoch": now - 120,
            "mt5_gate_expires_epoch": now - 1,
        }

        decision = self.evaluate(kronos_fixture(), tradingagents=stale_ta)

        self.assertEqual(decision.final_action, "ABSTAIN")
        self.assertEqual(decision.recommended_lot, 0.0)
        self.assertFalse(decision.research_evidence_valid)

    def test_failed_phase_marker_cannot_reuse_cached_report(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
            orchestrator = Orchestrator(settings)
            report_path = settings.report_dir / "kronos_matrix_latest.json"
            report_path.write_text(
                json.dumps(
                    kronos_fixture(
                        generated_epoch=now,
                        expires_epoch=now + 300,
                    )
                ),
                encoding="utf-8",
            )

            report = orchestrator._load_phase_research_report(
                [{"source": "kronos", "return_code": 2}],
                path=report_path,
                symbol="XAUUSD",
                source="kronos",
                minimum_generated_epoch=now - 1,
            )
            decision = self.evaluate(report or {})

        self.assertEqual(decision.final_action, "ABSTAIN")
        self.assertEqual(decision.recommended_lot, 0.0)
        self.assertFalse(decision.research_evidence_valid)

    def test_screener_does_not_share_one_symbol_report_with_another_pair(self):
        async def run_cycle() -> dict[str, Any]:
            now = int(time.time())
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
                orchestrator = Orchestrator(settings)
                orchestrator.screener_symbols = ["XAUUSD", "EURUSD"]
                orchestrator.get_open_positions = AsyncMock(return_value={"positions": []})
                orchestrator.MarketBookGet = AsyncMock(return_value=l2_fixture())

                settings.report_dir.joinpath("kronos_matrix_latest.json").write_text(
                    json.dumps(kronos_fixture(generated_epoch=now, expires_epoch=now + 300)),
                    encoding="utf-8",
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
                mt5_meta = {
                    "last_close": 100.0,
                    "bid": 100.0,
                    "ask": 100.1,
                    "atr_m5": 1.0,
                }
                with (
                    patch("newera_api.orchestrator.get_mt5_status", return_value={"account": {"equity": 1000.0}}),
                    patch("newera_api.orchestrator.fetch_mt5_bars_to_csv", return_value=mt5_meta),
                    patch.object(TALibTechnicalEngine, "evaluate_from_csv", return_value=step1_fixture()),
                ):
                    return await orchestrator.run_multi_pair_screener_cycle()

        payload = asyncio.run(run_cycle())
        results = {item["symbol"]: item for item in payload["results"]}
        self.assertEqual(results["XAUUSD"]["final_action"], "BUY")
        self.assertEqual(results["EURUSD"]["final_action"], "ABSTAIN")
        self.assertEqual(payload["auto_executed_count"], 0)

    def test_joey_allowlist_routes_non_core_auto_signal_to_shadow_mode(self):
        async def run_cycle() -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
            now = int(time.time())
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
                orchestrator = Orchestrator(settings)
                orchestrator.screener_symbols = ["EURUSD"]
                orchestrator.gemini_cio.autonomous_mode = True
                orchestrator.get_open_positions = AsyncMock(return_value={"positions": []})
                orchestrator.MarketBookGet = AsyncMock(return_value=l2_fixture())
                settings.report_dir.joinpath("kronos_matrix_latest.json").write_text(
                    json.dumps(kronos_fixture(symbol="EURUSD", generated_epoch=now, expires_epoch=now + 300)),
                    encoding="utf-8",
                )
                settings.report_dir.joinpath("tradingagents_latest.json").write_text(
                    json.dumps(
                        {
                            "user_symbol": "EURUSD",
                            "ai_action": "BUY",
                            "generated_epoch": now,
                            "mt5_gate_expires_epoch": now + 300,
                        }
                    ),
                    encoding="utf-8",
                )
                mt5_meta = {
                    "last_close": 100.0,
                    "bid": 100.0,
                    "ask": 100.1,
                    "atr_m5": 1.0,
                }
                execute = AsyncMock()
                final_review = AsyncMock(side_effect=AssertionError("shadow mode must not call live Gemini execution review"))
                with (
                    patch("newera_api.orchestrator.get_mt5_status", return_value={"account": {"equity": 1000.0}}),
                    patch("newera_api.orchestrator.fetch_mt5_bars_to_csv", return_value=mt5_meta),
                    patch.object(TALibTechnicalEngine, "evaluate_from_csv", return_value=step1_fixture()),
                    patch.object(orchestrator, "execute_demo_trade", new=execute),
                    patch.object(orchestrator, "_gemini_gate_before_auto_trade", new=final_review),
                ):
                    payload = await orchestrator.run_multi_pair_screener_cycle()
                return payload, execute, final_review

        payload, execute, final_review = asyncio.run(run_cycle())
        self.assertTrue(is_joey_live_auto_execution_symbol("XAUUSD"))
        self.assertFalse(is_joey_live_auto_execution_symbol("EURUSD"))
        self.assertEqual(build_joey_profile_comment("USTEC", {"confluence_count": 2}), "JoeyCIO_USTEC_2of3")
        self.assertEqual(payload["auto_executed_count"], 0)
        self.assertEqual(payload["shadow_logged_count"], 1)
        self.assertEqual(payload["results"][0]["execution_mode"], "SHADOW")
        self.assertFalse(payload["results"][0]["auto_execution_allowed"])
        self.assertEqual(payload["shadow_trades"][0]["comment"], "JoeyCIO_EURUSD_3of3")
        execute.assert_not_awaited()
        final_review.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
