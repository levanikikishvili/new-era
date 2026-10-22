import json
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

    def test_research_only_profile_lifecycle_is_atomic_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_registry.json"
            path.write_text(json.dumps(bot_manager.load_registry()), encoding="utf-8")

            created = bot_manager.add_bot_profile(
                "Telemetry Research Bot",
                bot_id="telemetry-research-bot",
                symbol="USTEC",
                path=path,
            )
            self.assertEqual(created["status"], "research_only")
            self.assertFalse(created["safety"]["real_account_allowed"])

            disabled = bot_manager.set_bot_enabled("telemetry-research-bot", False, path=path)
            self.assertFalse(disabled["enabled"])
            self.assertEqual(disabled["status"], "disabled")

            enabled = bot_manager.set_bot_enabled("telemetry-research-bot", True, path=path)
            self.assertTrue(enabled["enabled"])
            self.assertEqual(enabled["status"], "research_only")

            removed = bot_manager.remove_bot_profile("telemetry-research-bot", path=path)
            self.assertTrue(removed["removed"])
            self.assertEqual(
                bot_manager.validate_registry(json.loads(path.read_text(encoding="utf-8"))),
                [],
            )


if __name__ == "__main__":
    unittest.main()
