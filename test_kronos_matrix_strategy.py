import tempfile
import unittest
from pathlib import Path

from kronos_matrix_strategy import (
    MatrixConfig,
    STRATEGY_ID,
    analog_signal,
    combine_decisions,
    forecast_signal,
    matrix_signal,
    write_gate_csv,
)


def make_bars(values):
    bars = []
    for value in values:
        bars.append(
            {
                "open": value - 0.02,
                "high": value + 0.10,
                "low": value - 0.10,
                "close": value,
                "volume": 1.0,
                "amount": value,
            }
        )
    return bars


class KronosMatrixStrategyTests(unittest.TestCase):
    def test_forecast_requires_a_meaningful_move_and_path_agreement(self):
        self.assertEqual(forecast_signal(100.0, [100.02, 100.04], 1.0).direction, "ABSTAIN")
        self.assertEqual(forecast_signal(100.0, [100.2, 100.4, 100.6], 1.0).direction, "BUY")
        self.assertEqual(forecast_signal(100.0, [99.8, 99.6, 99.4], 1.0).direction, "SELL")

    def test_matrix_does_not_trade_without_a_closed_breakout(self):
        values = [100.0 + (i * 0.01) for i in range(120)]
        bars = make_bars(values)
        decision = matrix_signal({"H4": bars, "H1": bars, "M15": bars, "M5": bars})
        self.assertEqual(decision.direction, "ABSTAIN")

    def test_gate_requires_all_three_agreement_layers(self):
        bars = make_bars([100.0 + i * 0.1 for i in range(100)])
        matrix = matrix_signal({"H4": bars, "H1": bars, "M15": bars, "M5": bars})
        analog = analog_signal(bars)
        forecast = forecast_signal(110.0, [111.0, 112.0], 1.0)
        combined = combine_decisions(matrix, analog, forecast)
        self.assertEqual(combined.action, "ABSTAIN")
        self.assertTrue(combined.hard_blocks)

    def test_gate_writer_is_versioned_and_uses_strategy_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.csv"
            write_gate_csv(
                path,
                symbol="XAUUSD",
                action="ABSTAIN",
                confidence=0.0,
                generated_epoch=100,
                expires_epoch=200,
            )
            lines = path.read_text(encoding="ascii").splitlines()
            self.assertEqual(lines[0].split(",")[0], "version")
            self.assertIn(STRATEGY_ID, lines[1])
            self.assertIn(",ABSTAIN,", lines[1])


if __name__ == "__main__":
    unittest.main()
