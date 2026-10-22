from __future__ import annotations

import asyncio
import unittest

import main_api
from newera_api.schemas import ControlsUpdateRequest


class Phase2ApiContractTests(unittest.TestCase):
    def test_phase2_routes_are_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in main_api.app.routes}
        self.assertTrue(
            {
                "/controls",
                "/decision-history",
                "/decision-history/summary",
                "/bots",
                "/bots/status",
                "/bots/{bot_id}/enabled",
                "/bots/{bot_id}",
            }.issubset(paths)
        )

    def test_controls_endpoint_returns_requested_and_effective_values(self) -> None:
        original = main_api.orchestrator.get_controls()["controls"]
        try:
            updated = asyncio.run(
                main_api.controls_post(
                    ControlsUpdateRequest(
                        risk_pct=5.0,
                        min_rr=1.0,
                        max_sl_atr=5.0,
                        time_stop_minutes=240,
                    )
                )
            )
            self.assertEqual(updated["risk_pct"], 5.0)
            self.assertEqual(updated["min_rr"], 1.0)
            self.assertEqual(updated["effective_controls"]["risk_pct"], 1.5)
            self.assertEqual(updated["effective_controls"]["min_rr"], 1.5)
            self.assertEqual(updated["effective_controls"]["max_sl_atr"], 2.5)
            self.assertEqual(updated["effective_controls"]["time_stop_minutes"], 120)
        finally:
            main_api.orchestrator.controls.update(original)


if __name__ == "__main__":
    unittest.main()
