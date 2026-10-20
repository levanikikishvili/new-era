import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from newera_api.config import ApiSettings
from newera_api.gemini_manager import GeminiPortfolioManager, Step1TALibResult, TALibTechnicalEngine
from newera_api.orchestrator import Orchestrator


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
    report: dict[str, object] = {
        "symbol": symbol,
        "action": matrix if matrix == analogue == forecast else "ABSTAIN",
        "confidence": 0.9,
        "decision": {
            "matrix": {"direction": matrix, "score": 6.5, "buy_score": 6.5, "sell_score": 1.0},
            "analog": {"direction": analogue, "win_rate": 0.875},
            "forecast": {"direction": forecast, "move_atr": 1.45, "path_agreement": 0.85},
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
        async def run_cycle() -> dict[str, object]:
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


if __name__ == "__main__":
    unittest.main()
