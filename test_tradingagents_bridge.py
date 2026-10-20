import unittest
import tempfile
from pathlib import Path

import run_tradingagents_research as bridge


class TradingAgentsBridgeTests(unittest.TestCase):
    def test_gold_alias_is_normalized_for_vendor_data(self):
        self.assertEqual(bridge.canonical_vendor_symbol("XAUUSD"), "GC=F")
        self.assertEqual(bridge.canonical_vendor_symbol("GOLD+"), "GC=F")

    def test_rating_mapping_is_conservative(self):
        self.assertEqual(bridge.normalize_action("Buy"), "BUY")
        self.assertEqual(bridge.normalize_action("Underweight"), "SELL")
        self.assertEqual(bridge.normalize_action("Hold"), "ABSTAIN")
        self.assertEqual(bridge.normalize_action("unrecognised text"), "ABSTAIN")

    def test_blocked_deterministic_gate_abstains(self):
        gate = {"decision": "BUY", "warnings": ["spread exceeds configured ceiling"]}
        self.assertEqual(bridge.gate_action(gate), "ABSTAIN")

    def test_consensus_never_enables_execution(self):
        result = bridge.make_result(
            ticker="XAUUSD",
            analysis_date="2026-10-17",
            signal="Buy",
            state={"final_trade_decision": "research text"},
            gate={"decision": "BUY", "warnings": []},
            framework_version="0.7.0",
            gate_ttl_minutes=90,
        )
        self.assertEqual(result["consensus_action"], "BUY")
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["real_account_allowed"])

    def test_mt5_gate_is_a_small_versioned_csv_contract(self):
        payload = {
            "mt5_gate_strategy": "new-era-gold-council-v1",
            "user_symbol": "XAUUSD",
            "mt5_gate_action": "BUY",
            "mt5_gate_confidence": 0.0,
            "generated_epoch": 100,
            "mt5_gate_expires_epoch": 200,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.csv"
            bridge.write_mt5_gate(path, payload)
            self.assertEqual(path.read_text(encoding="ascii").splitlines()[1].split(",")[3], "BUY")


if __name__ == "__main__":
    unittest.main()
