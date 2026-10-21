import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        return SimpleNamespace(
            login=12345,
            trade_mode=self.trade_mode,
            balance=10000.0,
            equity=10000.0,
            profit=0.0,
            margin=0.0,
            margin_free=10000.0,
            leverage=100,
            company="Fake Broker",
            server="Fake Demo",
            name="Fake Account",
            currency="USD",
        )

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
        return [
            {
                "time": 1_700_000_000 + i * 300,
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
            }
            for i in range(max(16, count))
        ]

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
    def setUp(self) -> None:
        mt5_bridge.clear_execution_locks()

    def _call_bridge(
        self,
        fake_mt5: FakeMetaTrader5,
        *,
        symbol: str = "XAUUSD",
        side: str = "BUY",
        sl: float = 99.0,
        tp: float = 103.0,
        comment: str = "NewEraQuantDemo",
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
                        "NEWERA_MAX_OPEN_POSITIONS_ACCOUNT": "2",
                        "NEWERA_MAX_DAILY_DEMO_TRADES": "10",
                    },
                    clear=False,
                ),
                ):
                return mt5_bridge._inprocess_execute_demo_order(
                    symbol,
                    side,
                    0.01,
                    sl=sl,
                    tp=tp,
                    comment=comment,
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

    def test_bridge_enforces_joey_risk_reward_and_atr_limits_before_order_send(self) -> None:
        cases = (
            (99.0, 101.0, "reward:risk"),
            (96.0, 110.0, "SL distance"),
        )
        for sl, tp, message in cases:
            with self.subTest(sl=sl, tp=tp):
                fake_mt5 = FakeMetaTrader5()
                with self.assertRaisesRegex(ValueError, message):
                    self._call_bridge(fake_mt5, sl=sl, tp=tp)
                self.assertEqual(fake_mt5.order_send_calls, [])

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
                        tp=103.0,
                        allow_paper_fallback=True,
                    )
        self.assertEqual(fake_mt5.order_send_calls, [])

    def test_account_wide_cap_blocks_a_new_symbol(self) -> None:
        fake_mt5 = FakeMetaTrader5(
            positions=[
                SimpleNamespace(symbol="XAUUSD"),
                SimpleNamespace(symbol="EURUSD"),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "account-wide open-position cap"):
            self._call_bridge(fake_mt5, symbol="GBPUSD")
        self.assertEqual(fake_mt5.order_send_calls, [])

    def test_pure_safety_helpers_are_deterministic(self) -> None:
        lock_state: dict[str, float] = {}
        self.assertEqual(
            mt5_bridge.acquire_execution_lock(
                lock_state,
                "xauusd",
                now=100.0,
                lock_seconds=60.0,
            ),
            0.0,
        )
        self.assertEqual(
            mt5_bridge.execution_lock_remaining(
                lock_state,
                "XAUUSD",
                now=101.5,
                lock_seconds=60.0,
            ),
            58.5,
        )
        self.assertEqual(
            mt5_bridge.count_open_positions_account_wide([{"symbol": "XAUUSD"}], [{"symbol": "EURUSD"}]),
            2,
        )
        opened = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
        now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(mt5_bridge.position_age_minutes({"opened_at": opened}, now=now), 120.0)
        self.assertTrue(mt5_bridge.is_position_time_stop_due({"opened_at": opened}, now=now))
        self.assertEqual(mt5_bridge.prepare_order_comment("x" * 40), "x" * 31)

    def test_per_symbol_execution_lock_covers_check_and_send(self) -> None:
        fake_mt5 = FakeMetaTrader5(order_retcode=FakeMetaTrader5.TRADE_RETCODE_DONE)
        first = self._call_bridge(fake_mt5, comment="JoeyCIO_88pct")
        self.assertIsInstance(first, dict)
        with self.assertRaisesRegex(RuntimeError, "execution lock is active"):
            self._call_bridge(fake_mt5, comment="JoeyCIO_88pct")
        self.assertEqual(len(fake_mt5.order_send_calls), 1)
        self.assertEqual(fake_mt5.positions_calls, 1)

    def test_order_comment_is_passed_through_and_returned_for_profiling(self) -> None:
        fake_mt5 = FakeMetaTrader5(order_retcode=FakeMetaTrader5.TRADE_RETCODE_DONE)
        result = self._call_bridge(fake_mt5, comment="JoeyCIO_91pct")
        assert isinstance(result, dict)
        self.assertEqual(fake_mt5.order_send_calls[0]["comment"], "JoeyCIO_91pct")
        self.assertEqual(result["order_comment"], "JoeyCIO_91pct")
        self.assertEqual(result["position"]["comment"], "JoeyCIO_91pct")

    def test_time_stop_closes_old_terminal_position_before_dynamic_management(self) -> None:
        now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        old_position = SimpleNamespace(
            ticket=77,
            symbol="XAUUSD",
            type=FakeMetaTrader5.ORDER_TYPE_BUY,
            volume=0.10,
            price_open=100.0,
            price_current=100.1,
            sl=99.0,
            tp=101.0,
            profit=1.0,
            swap=0.0,
            time=(now - timedelta(minutes=121)).timestamp(),
            comment="JoeyCIO_91pct",
        )
        fake_mt5 = FakeMetaTrader5(
            positions=[old_position],
            order_retcode=FakeMetaTrader5.TRADE_RETCODE_DONE,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(sys.modules, {"MetaTrader5": fake_mt5}),
                patch.object(mt5_bridge, "DEMO_POSITIONS_PATH", root / "demo_positions.json"),
                patch.object(mt5_bridge, "DEMO_HISTORY_PATH", root / "demo_history.json"),
                patch.dict("os.environ", {"NEWERA_TIME_STOP_MINUTES": "120"}, clear=False),
            ):
                result = mt5_bridge._inprocess_manage_demo_positions(
                    now_override=now,
                    atr_override=0.01,
                )

        self.assertEqual(result["time_stop_closed_tickets"], [77])
        self.assertEqual(result["modifications"][0]["action_type"], "TIME_STOP")
        self.assertEqual(len(fake_mt5.order_send_calls), 1)
        self.assertEqual(fake_mt5.order_send_calls[0]["comment"], "NewEraTimeStop")
        self.assertTrue(all(request["action"] != getattr(fake_mt5, "TRADE_ACTION_SLTP", 3) for request in fake_mt5.order_send_calls))

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
                        tp=103.0,
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
