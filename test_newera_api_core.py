import tempfile
import unittest
from pathlib import Path

from kronos_matrix_strategy import STRATEGY_ID
from newera_api.config import ApiSettings
from newera_api.memory import HashEmbeddingFunction, MemoryStore
from newera_api.orchestrator import Orchestrator


class NewEraApiCoreTests(unittest.TestCase):
    def test_hash_embedding_is_local_and_deterministic(self):
        embedding = HashEmbeddingFunction(dimensions=16)
        self.assertEqual(embedding(["same text"]), embedding(["same text"]))
        self.assertEqual(len(embedding(["same text"])[0]), 16)

    def test_memory_store_has_safe_jsonl_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory), enabled=False)
            result = store.store_cycle(
                {
                    "scan_id": "scan-1",
                    "strategy": STRATEGY_ID,
                    "symbol": "XAUUSD",
                    "action": "ABSTAIN",
                    "score": 1.0,
                    "confidence": 0.0,
                    "reasons": ["spread blocked"],
                    "analysis": "matrix breakout was not confirmed",
                }
            )
            self.assertTrue(result["stored"])
            self.assertEqual(store.health()["backend"], "jsonl")
            self.assertTrue(store.search("spread blocked"))

    def test_orchestrator_rejects_workspace_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ApiSettings(root=root, memory_dir=root / "memory", report_dir=root / "reports")
            orchestrator = Orchestrator(config)
            with self.assertRaises(ValueError):
                config.resolve_workspace_path("..\\outside.csv")
            self.assertEqual(orchestrator._line_event_type("kronos", "forecast: BUY"), "kronos.forecast")


if __name__ == "__main__":
    unittest.main()
