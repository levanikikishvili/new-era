from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import main_api
from newera_api.bot_lab import StrategyLab, load_python_strategy, run_backtest
from newera_api.schemas import StrategyLoadRequest, StrategySaveRequest


def visual_profile() -> dict[str, object]:
    return {
        "id": "ma-research",
        "name": "Moving Average Research",
        "builder": "visual",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "flow": {
            "indicators": [{"id": "fast", "type": "sma", "source": "close", "period": 2}],
            "conditions": [
                {
                    "id": "go_long",
                    "left": {"indicator": "fast"},
                    "operator": ">",
                    "right": {"value": 100},
                }
            ],
            "actions": [{"type": "enter_long", "when": "go_long"}],
        },
    }


def bars(count: int = 10) -> list[dict[str, float | int]]:
    return [
        {
            "timestamp": index,
            "open": 100.0 + index,
            "high": 100.5 + index,
            "low": 99.5 + index,
            "close": 100.0 + index,
            "volume": 10.0,
        }
        for index in range(count)
    ]


class BotLabTests(unittest.TestCase):
    def test_visual_profile_save_load_is_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab = StrategyLab(Path(directory))
            saved = lab.save_profile(visual_profile())
            loaded = lab.load_profile("ma-research")

            self.assertEqual(saved, loaded)
            self.assertEqual(loaded["status"], "research_only")
            self.assertFalse(loaded["safety"]["real_account_allowed"])
            self.assertFalse(loaded["safety"]["order_execution_allowed"])
            self.assertEqual([item["id"] for item in lab.list_profiles()], ["ma-research"])

    def test_deterministic_visual_backtest_and_walk_forward_metrics(self) -> None:
        profile = visual_profile()
        first = run_backtest(profile, bars(), initial_cash=1000.0)
        second = run_backtest(profile, bars(), initial_cash=1000.0)
        self.assertEqual(first, second)
        self.assertEqual(first["metrics"]["trades"], 1)
        self.assertGreater(first["metrics"]["net_profit"], 0.0)
        self.assertEqual(first["execution"], "backtest_only")

        with tempfile.TemporaryDirectory() as directory:
            lab = StrategyLab(Path(directory))
            result = lab.walk_forward(profile, bars(12), train_ratio=0.5, initial_cash=1000.0)
            self.assertEqual(result["split"]["split_index"], 6)
            self.assertEqual(result["split"]["train_bars"], 6)
            self.assertEqual(result["split"]["validation_bars"], 6)
            self.assertFalse(result["real_account_allowed"])

    def test_workspace_local_python_strategy_loader_validates_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "strategies" / "sample_strategy.py"
            module.parent.mkdir(parents=True)
            module.write_text(
                "class SampleStrategy:\n"
                "    def on_bar(self, bar, context):\n"
                "        return 'BUY' if bar['close'] > 100 else None\n",
                encoding="utf-8",
            )
            loaded = load_python_strategy(module, "SampleStrategy", workspace_root=root)
            self.assertEqual(loaded.class_name, "SampleStrategy")
            self.assertTrue(loaded.metadata()["research_only"])
            self.assertFalse(loaded.metadata()["real_account_allowed"])

            profile = StrategyLab(root).save_profile(
                {
                    "id": "sample-code",
                    "name": "Sample Code Strategy",
                    "builder": "code",
                    "code": {"module_path": "strategies/sample_strategy.py", "class_name": "SampleStrategy"},
                }
            )
            code_result = StrategyLab(root).backtest("sample-code", bars(6))
            self.assertEqual(profile["builder"], "code")
            self.assertEqual(code_result["execution"], "backtest_only")
            self.assertFalse(code_result["real_account_allowed"])

            outside = Path(directory).parent / "outside_strategy.py"
            outside.write_text("class SampleStrategy: pass\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_python_strategy(outside, "SampleStrategy", workspace_root=root)

            invalid = root / "strategies" / "invalid.py"
            invalid.write_text("class Invalid: pass\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_python_strategy(invalid, "Invalid", workspace_root=root)

    def test_strategy_lab_routes_and_load_endpoint_contract(self) -> None:
        paths = {getattr(route, "path", "") for route in main_api.app.routes}
        self.assertTrue(
            {
                "/strategies",
                "/strategies/{strategy_id}",
                "/strategies/backtest",
                "/strategies/walk-forward",
                "/strategies/load",
            }.issubset(paths)
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "research_strategy.py"
            module.write_text(
                "class ResearchStrategy:\n"
                "    def on_bar(self, bar, context):\n"
                "        return None\n",
                encoding="utf-8",
            )
            original_lab = main_api.strategy_lab
            main_api.strategy_lab = StrategyLab(root)
            try:
                response = asyncio.run(
                    main_api.strategies_load(
                        StrategyLoadRequest(module_path="research_strategy.py", class_name="ResearchStrategy")
                    )
                )
                self.assertEqual(response["class_name"], "ResearchStrategy")
                self.assertFalse(response["order_execution_allowed"])

                saved = asyncio.run(main_api.strategies_save(StrategySaveRequest(**visual_profile())))
                self.assertEqual(saved["id"], "ma-research")
            finally:
                main_api.strategy_lab = original_lab


if __name__ == "__main__":
    unittest.main()
