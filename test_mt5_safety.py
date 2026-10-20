import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from newera_api import mt5_bridge
from newera_api.schemas import DemoOrderRequest


class FakeMetaTrader5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 2
    ORDER_FILLING_RETURN = 3
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_MARKET_CLOSED = 10018
    TRADE_RETCODE_INVALID_STOPS = 10016
    TIMEFRAME_M5 = 5

    def __init__(
        self,
        *,
        positions: list[object] | None = None,
        history: list[object] | None = None,
        order_retcode: int = TRADE_RETCODE_MARKET_CLOSED,
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
    ) -> None:
        self.positions = positions or []
        self.history = history or []
        self.order_retcode = order_retcode
        self.trade_mode = trade_mode
        self.order_send_calls: list[dict[str, object]] = []
        self.positions_calls = 0

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> str:
        return "fake mt5 error"

    def account_info(self) -> object:
        return SimpleNamespace(login=12345, trade_mode=self.trade_mode)

    def symbols_get(self) -> list[object]:
        return [SimpleNamespace(name="XAUUSD")]

    def symbol_info(self, symbol: str) -> object:
        return SimpleNamespace(
            name=symbol,
            digits=1,
            point=0.1,
            trade_contract_size=100.0,
            trade_tick_value=1.0,
            trade_tick_size=0.1,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
        )

    def symbol_select(self, symbol: str, selected: bool) -> bool:
        return True

    def symbol_info_tick(self, symbol: str) -> object:
        return SimpleNamespace(ask=100.1, bid=99.9)

    def positions_get(self, *, symbol: str | None = None) -> list[object]:
        self.positions_calls += 1
        return list(self.positions)

    def history_deals_get(self, start: object, end: object) -> list[object]:
        return list(self.history)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> list[object]:
        return []

    def order_send(self, request: dict[str, object]) -> object:
        self.order_send_calls.append(request)
        return SimpleNamespace(
            retcode=self.order_retcode,
            price=0.0,
            order=0,
            deal=0,
            comment="market closed",
        )


class Mt5SafetyTests(unittest.TestCase):
    def _call_bridge(
        self,
        fake_mt5: FakeMetaTrader5,
        *,
        side: str = "BUY",
        sl: float = 99.0,
        tp: float = 101.0,
        allow_paper_fallback: bool = False,
    ) -> object:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(sys.modules, {"MetaTrader5": fake_mt5}),
                patch.object(mt5_bridge, "DEMO_POSITIONS_PATH", root / "demo_positions.json"),
                patch.object(mt5_bridge, "DEMO_HISTORY_PATH", root / "demo_history.json"),
                patch.dict(
                    "os.environ",
                    {
                        "NEWERA_MAX_OPEN_POSITIONS_PER_SYMBOL": "1",
                        "NEWERA_MAX_DAILY_DEMO_TRADES": "10",
                    },
                    clear=False,
                ),
            ):
                return mt5_bridge._inprocess_execute_demo_order(
                    "XAUUSD",
                    side,
                    0.01,
                    sl=sl,
                    tp=tp,
                    allow_paper_fallback=allow_paper_fallback,
                )

    def test_invalid_buy_and_sell_geometry_never_reaches_order_send_or_fallback(self) -> None:
        cases = (
            ("BUY", 101.0, 102.0),  # both levels are above the ask
            ("SELL", 98.0, 99.0),  # TP is not below the bid and SL is not above it
        )
        for side, sl, tp in cases:
            with self.subTest(side=side):
                fake_mt5 = FakeMetaTrader5()
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    self._call_bridge(
                        fake_mt5,
                        side=side,
                        sl=sl,
                        tp=tp,
                        allow_paper_fallback=True,
                    )
                self.assertEqual(fake_mt5.order_send_calls, [])
                self.assertEqual(fake_mt5.positions_calls, 0)

    def test_duplicate_symbol_guard_includes_paper_positions(self) -> None:
        fake_mt5 = FakeMetaTrader5()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            positions_path = root / "demo_positions.json"
            positions_path.write_text(
                json.dumps(
                    [
                        {
                            "ticket": 7,
                            "symbol": "XAUUSD",
                            "opened_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(sys.modules, {"MetaTrader5": fake_mt5}),
                patch.object(mt5_bridge, "DEMO_POSITIONS_PATH", positions_path),
                patch.object(mt5_bridge, "DEMO_HISTORY_PATH", root / "demo_history.json"),
                patch.dict(
                    "os.environ",
                    {
                        "NEWERA_MAX_OPEN_POSITIONS_PER_SYMBOL": "1",
                        "NEWERA_MAX_DAILY_DEMO_TRADES": "10",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "already has 1 open"):
                    mt5_bridge._inprocess_execute_demo_order(
                        "XAUUSD",
                        "BUY",
                        0.01,
                        sl=99.0,
                        tp=101.0,
                        allow_paper_fallback=True,
                    )
        self.assertEqual(fake_mt5.order_send_calls, [])

    def test_daily_cap_blocks_before_order_send(self) -> None:
        now = datetime.now(timezone.utc)
        fake_mt5 = FakeMetaTrader5(history=[{"time": now.timestamp()}])
        with self.assertRaisesRegex(RuntimeError, "daily Demo trade cap reached"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    patch.dict(sys.modules, {"MetaTrader5": fake_mt5}),
                    patch.object(mt5_bridge, "DEMO_POSITIONS_PATH", root / "demo_positions.json"),
                    patch.object(mt5_bridge, "DEMO_HISTORY_PATH", root / "demo_history.json"),
                    patch.dict(
                        "os.environ",
                        {
                            "NEWERA_MAX_OPEN_POSITIONS_PER_SYMBOL": "1",
                            "NEWERA_MAX_DAILY_DEMO_TRADES": "1",
                        },
                        clear=False,
                    ),
                ):
                    mt5_bridge._inprocess_execute_demo_order(
                        "XAUUSD",
                        "BUY",
                        0.01,
                        sl=99.0,
                        tp=101.0,
                        allow_paper_fallback=True,
                    )
        self.assertEqual(fake_mt5.order_send_calls, [])

    def test_daily_trade_counter_is_deterministic_and_fail_closed(self) -> None:
        fixed_now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        records = [
            {"time": datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc).timestamp()},
            {"time": datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc).timestamp()},
            {"malformed": True},
        ]
        self.assertEqual(mt5_bridge.count_daily_demo_trades(records, now=fixed_now), 2)

    def test_fallback_is_disabled_by_default_and_explicit_fallback_is_unambiguous(self) -> None:
        fake_mt5 = FakeMetaTrader5()
        with self.assertRaisesRegex(RuntimeError, "fallback is disabled"):
            self._call_bridge(fake_mt5)
        self.assertEqual(len(fake_mt5.order_send_calls), 1)

        explicit_fake_mt5 = FakeMetaTrader5()
        result = self._call_bridge(explicit_fake_mt5, allow_paper_fallback=True)
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["execution_source"], "mt5_demo_paper_fallback")
        self.assertTrue(result["demo_overlay"])
        self.assertTrue(result["paper_fallback"])
        self.assertTrue(result["fallback_requested"])

    def test_explicit_fallback_does_not_mask_broker_invalid_stops(self) -> None:
        fake_mt5 = FakeMetaTrader5(order_retcode=FakeMetaTrader5.TRADE_RETCODE_INVALID_STOPS)
        with self.assertRaisesRegex(RuntimeError, "paper fallback was not activated"):
            self._call_bridge(fake_mt5, allow_paper_fallback=True)
        self.assertEqual(len(fake_mt5.order_send_calls), 1)

    def test_schema_defaults_fallback_off_and_keeps_volume_validation(self) -> None:
        request = DemoOrderRequest(symbol="XAUUSD")
        self.assertFalse(request.allow_paper_fallback)
        with self.assertRaises(ValueError):
            DemoOrderRequest(symbol="XAUUSD", volume=0.001)

    def test_real_account_hard_block_is_preserved(self) -> None:
        fake_mt5 = FakeMetaTrader5(trade_mode=2)
        with self.assertRaises(PermissionError):
            self._call_bridge(fake_mt5)
        self.assertEqual(fake_mt5.order_send_calls, [])


if __name__ == "__main__":
    unittest.main()
