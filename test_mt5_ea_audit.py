from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EA_SOURCE = (ROOT / "NewEraCouncilDemo.mq5").read_text(encoding="utf-8")


class Mt5EaAuditContractTests(unittest.TestCase):
    def test_market_order_requires_server_retcode_success(self) -> None:
        self.assertIn("const uint retcode = trade.ResultRetcode();", EA_SOURCE)
        self.assertIn("const string retcode_description = trade.ResultRetcodeDescription();", EA_SOURCE)
        self.assertIn("if(ok && SuccessfulTradeRetcode(retcode))", EA_SOURCE)
        self.assertIn('LogEvent("ORDER_SUCCESSFULLY_EXECUTED", result_details)', EA_SOURCE)
        self.assertIn('LogEvent("BROKER_REJECTED_ORDER", result_details)', EA_SOURCE)
        self.assertNotIn('LogEvent(ok ? "ORDER_SENT" : "ORDER_FAILED"', EA_SOURCE)

    def test_live_and_unsafe_parameter_paths_are_hard_blocked(self) -> None:
        self.assertIn("this EA is permanently demo-only", EA_SOURCE)
        self.assertIn("ACCOUNT_TRADE_MODE_DEMO", EA_SOURCE)
        self.assertIn("Attach this EA to an M5 chart", EA_SOURCE)
        self.assertIn("RewardRisk < 1.50", EA_SOURCE)
        self.assertIn("MaximumRiskPercent > 1.50", EA_SOURCE)
        self.assertIn("StopLossATRMultiplier > 2.50", EA_SOURCE)
        self.assertIn("MinimumVotes < 4", EA_SOURCE)

    def test_diagnostic_contract_contains_required_states_and_fields(self) -> None:
        for status in (
            "NO SIGNAL",
            "SIGNAL BLOCKED BY FILTER",
            "SIGNAL STALE",
            "AI GATE ABSTAIN",
            "ORDER VALIDATION FAILED",
            "BROKER REJECTED ORDER",
            "ORDER SUCCESSFULLY EXECUTED",
        ):
            self.assertIn(status, EA_SOURCE)
        for field in (
            '"spread_points"',
            '"h4"',
            '"h1"',
            '"m15"',
            '"m5"',
            '"ai_gate"',
            '"freshness"',
            '"risk_checks"',
            '"decision"',
            '"reason"',
        ):
            self.assertIn(field, EA_SOURCE)
        self.assertIn("Comment(\"NEW ERA MT5 EXECUTION AUDIT", EA_SOURCE)

    def test_presets_use_safe_reward_risk_and_diagnostic_files(self) -> None:
        for name in (
            "NewEraCouncilDemo.set",
            "NewEraCouncilDemo_AI_Gated.set",
            "NewEraKronosMatrixScalper.set",
        ):
            values = {
                key: value
                for key, value in (
                    line.split("=", 1)
                    for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
            }
            self.assertGreaterEqual(float(values["RewardRisk"]), 1.5, name)
            self.assertGreaterEqual(int(values["MinimumVotes"]), 4, name)
            self.assertTrue(values["DiagnosticCsvFile"], name)

    def test_python_research_contract_cannot_directly_place_orders(self) -> None:
        runner = (ROOT / "run_tradingagents_research.py").read_text(encoding="utf-8")
        combiner = (ROOT / "combine_research_gates.py").read_text(encoding="utf-8")
        self.assertIn('"execution_allowed": False', runner)
        self.assertIn('"real_account_allowed": False', runner)
        self.assertNotRegex(runner, r"\border_send\s*\(")
        self.assertNotRegex(combiner, r"\border_send\s*\(")


if __name__ == "__main__":
    unittest.main()
