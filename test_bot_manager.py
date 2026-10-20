import tempfile
import unittest
from pathlib import Path

import bot_manager


class BotManagerTests(unittest.TestCase):
    def test_registry_and_active_artifacts_are_valid(self):
        registry = bot_manager.load_registry()
        self.assertEqual(bot_manager.validate_registry(registry), [])
        self.assertEqual(bot_manager.validate_active_artifacts(registry), [])

    def test_gate_contract_accepts_versioned_row(self):
        content = (
            "version,strategy,symbol,action,confidence,generated_epoch,expires_epoch\n"
            "1,new-era-gold-council-v1,XAUUSD,ABSTAIN,0.0,100,200\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.csv"
            path.write_text(content, encoding="ascii")
            self.assertEqual(bot_manager.validate_gate(path), [])

    def test_gate_contract_rejects_expired_order_signal(self):
        content = (
            "version,strategy,symbol,action,confidence,generated_epoch,expires_epoch\n"
            "1,new-era-gold-council-v1,XAUUSD,BUY,0.0,200,100\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.csv"
            path.write_text(content, encoding="ascii")
            self.assertTrue(bot_manager.validate_gate(path))

    def test_kronos_gate_uses_its_own_strategy_id(self):
        content = (
            "version,strategy,symbol,action,confidence,generated_epoch,expires_epoch\n"
            "1,new-era-kronos-matrix-v1,XAUUSD,ABSTAIN,0.0,100,200\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kronos_gate.csv"
            path.write_text(content, encoding="ascii")
            self.assertEqual(bot_manager.validate_gate(path, "new-era-kronos-matrix-v1"), [])


if __name__ == "__main__":
    unittest.main()
