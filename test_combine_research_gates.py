import tempfile
import time
import unittest
from pathlib import Path

import combine_research_gates as combo


class CombinedGateTests(unittest.TestCase):
    def test_disagreement_abstains(self):
        action, confidence, reasons, _ = combo.combine(
            {"action": "BUY", "confidence": "0.8", "expires_epoch": str(int(time.time()) + 100)},
            {"action": "SELL", "confidence": "0.7", "expires_epoch": str(int(time.time()) + 100)},
        )
        self.assertEqual(action, "ABSTAIN")
        self.assertEqual(confidence, 0.0)
        self.assertTrue(reasons)

    def test_agreement_requires_both_positive_actions(self):
        action, confidence, reasons, _ = combo.combine(
            {"action": "BUY", "confidence": "0.8", "expires_epoch": str(int(time.time()) + 100)},
            {"action": "BUY", "confidence": "0.7", "expires_epoch": str(int(time.time()) + 100)},
        )
        self.assertEqual(action, "BUY")
        self.assertEqual(confidence, 0.7)
        self.assertFalse(reasons)

    def test_main_writes_a_fail_closed_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ai = root / "ai.csv"
            kronos = root / "kronos.csv"
            out = root / "out.csv"
            report = root / "report.json"
            header = "version,strategy,symbol,action,confidence,generated_epoch,expires_epoch\n"
            row = f"1,test,XAUUSD,ABSTAIN,0.0,{int(time.time())},{int(time.time()) + 100}\n"
            ai.write_text(header + row, encoding="ascii")
            kronos.write_text(header + row, encoding="ascii")
            self.assertEqual(
                combo.main(["--ai-gate", str(ai), "--kronos-gate", str(kronos), "--output", str(out), "--report", str(report)]),
                0,
            )
            self.assertIn(",ABSTAIN,", out.read_text(encoding="ascii"))


if __name__ == "__main__":
    unittest.main()
