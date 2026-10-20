from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from ui.desktop_app import (
    NewEraDesktopTerminal,
    build_tradingview_html,
    map_mt5_to_tradingview_symbol,
)


class DesktopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ui_contract_does_not_import_subprocess_or_mt5(self) -> None:
        source = (Path(__file__).resolve().parent / "ui" / "desktop_app.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import MetaTrader5", source)

    def test_tradingview_symbol_mapping_and_html(self) -> None:
        self.assertEqual(map_mt5_to_tradingview_symbol("XAUUSD"), "OANDA:XAUUSD")
        self.assertEqual(map_mt5_to_tradingview_symbol("EURUSD.a"), "FX:EURUSD")
        self.assertEqual(map_mt5_to_tradingview_symbol("BTCUSDm"), "COINBASE:BTCUSD")
        html = build_tradingview_html("OANDA:XAUUSD", "5")
        self.assertIn("TradingView.widget", html)
        self.assertIn("OANDA:XAUUSD", html)

    def test_gate_refresh_never_bypasses_backend_autonomous_execution(self) -> None:
        from unittest.mock import patch

        window = NewEraDesktopTerminal(
            api_url="http://127.0.0.1:59999",
            ws_url="ws://127.0.0.1:59999/ws/logs",
        )
        try:
            window.btn_auto_execute_demo.blockSignals(True)
            window.btn_auto_execute_demo.setChecked(True)
            window.btn_auto_execute_demo.blockSignals(False)

            with patch.object(window, "trigger_execute_demo_trade") as manual_execute:
                window._update_gates_and_prediction_ui(
                    {
                        "combined_report": {
                            "symbol": "XAUUSD",
                            "action": "BUY",
                            "confidence": 0.84,
                            "scan_id": "scan-1",
                        },
                        "kronos_matrix": {
                            "symbol": "XAUUSD",
                            "action": "BUY",
                            "confidence": "0.84",
                        },
                        "waterfall": {
                            "symbol": "XAUUSD",
                            "final_action": "BUY",
                            "risk_tier": "STANDARD_RISK",
                        },
                    }
                )

            self.assertEqual(window._latest_gate_action, "BUY")
            manual_execute.assert_not_called()

            window._update_gates_and_prediction_ui(
                {
                    "combined_report": {
                        "symbol": "EURUSD",
                        "action": "SELL",
                        "confidence": 0.91,
                        "scan_id": "foreign-scan",
                    }
                }
            )
            self.assertEqual(window._latest_gate_action, "ABSTAIN")
        finally:
            window.close()

    def test_terminal_renders_and_updates_mt5_calculators_and_confluence(self) -> None:
        window = NewEraDesktopTerminal(
            api_url="http://127.0.0.1:59999",
            ws_url="ws://127.0.0.1:59999/ws/logs",
        )
        try:
            self.assertEqual(window.ticker_combo.currentText(), "XAUUSD")
            window._update_health_ui(
                {
                    "status": "ok",
                    "version": "0.1.0",
                    "active_scan_id": None,
                    "memory": {"backend": "jsonl", "count": 4},
                }
            )
            self.assertIn("ONLINE", window.api_status_badge.text())

            window._update_mt5_status_ui(
                {
                    "connected": True,
                    "account": {
                        "login": 53012933,
                        "company": "Raw Trading Ltd",
                        "server": "ICMarketsSC-Demo",
                        "name": "Levani Kikishvili",
                        "currency": "USD",
                        "balance": 171.12,
                        "equity": 171.12,
                        "margin_free": 171.12,
                        "leverage": 5000,
                        "trade_mode": "DEMO",
                    },
                    "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD"],
                }
            )
            self.assertIn("Raw Trading Ltd", window.mt5_broker_value.text())
            self.assertIn("53012933", window.mt5_account_value.text())
            self.assertIn("171.12", window.mt5_balance_value.text())

            window._update_mt5_symbol_ui(
                {
                    "symbol": "XAUUSD",
                    "digits": 2,
                    "point": 0.01,
                    "bid": 4165.76,
                    "ask": 4166.16,
                    "spread_points": 40.0,
                    "atr_m5": 2.17,
                    "trade_tick_value": 1.0,
                    "trade_tick_size": 0.01,
                    "volume_min": 0.01,
                    "volume_max": 100.0,
                    "volume_step": 0.01,
                }
            )
            self.assertIn("4165.76", window.live_quote_badge.text())
            self.assertIn("MT5 LOT SIZE:", window.calc_lot_result_label.text())
            self.assertIn("Full Kelly:", window.calc_kelly_result_label.text())

            window._on_ws_event_received(
                {
                    "type": "tradingagents.debate",
                    "timestamp": 1792320907.0,
                    "payload": {"source": "tradingagents", "message": "Bull vs Bear debate round 1"},
                }
            )
            window._on_ws_event_received(
                {
                    "type": "kronos.forecast",
                    "timestamp": 1792320908.0,
                    "payload": {
                        "source": "kronos",
                        "message": "Kronos Matrix: action=BUY matrix=BUY analog=BUY forecast=BUY confidence=0.820",
                    },
                }
            )
            self.assertIn("Bull vs Bear", window.log_debate.toPlainText())
            self.assertEqual(window.kronos_dir_label.text(), "BUY")
            self.assertEqual(window.kronos_conf_bar.value(), 82)

            # Verify L2 Orderbook & Liquidity Radar UI update
            window._update_l2_orderbook_ui(
                {
                    "symbol": "XAUUSD",
                    "source": "mt5_l2_dom",
                    "digits": 2,
                    "best_bid": 4165.70,
                    "best_ask": 4166.10,
                    "mid_price": 4165.90,
                    "spread": 0.40,
                    "total_bid_volume": 450.0,
                    "total_ask_volume": 1200.0,
                    "bid_pressure_pct": 27.3,
                    "ask_pressure_pct": 72.7,
                    "imbalance_ratio": 0.375,
                    "l2_direction": "SELL",
                    "confluence_score": 0.78,
                    "veto_buy": True,
                    "veto_sell": False,
                    "veto_reason": "Heavy L2 SELL WALL at 4166.50 (vol=650.0, 4.2x avg) blocks BUY",
                    "strongest_buy_wall": {"price": 4164.00, "volume": 120.0},
                    "strongest_sell_wall": {"price": 4166.50, "volume": 650.0},
                    "asks": [
                        {
                            "side": "ASK",
                            "price": 4166.50,
                            "volume": 650.0,
                            "cumulative_volume": 650.0,
                            "volume_ratio": 4.2,
                            "is_wall": True,
                            "is_whale": True,
                            "tag": "WHALE_WALL",
                        }
                    ],
                    "bids": [
                        {
                            "side": "BID",
                            "price": 4165.70,
                            "volume": 120.0,
                            "cumulative_volume": 120.0,
                            "volume_ratio": 1.1,
                            "is_wall": False,
                            "is_whale": False,
                            "tag": "NORMAL",
                        }
                    ],
                    "alerts": [
                        {
                            "symbol": "XAUUSD",
                            "alert_type": "WHALE_WALL",
                            "side": "ASK",
                            "price": 4166.50,
                            "volume": 650.0,
                            "severity": "CRITICAL",
                            "message": "[XAUUSD] WHALE SELL WALL detected at 4166.50 (Vol: 650.0)",
                        }
                    ],
                }
            )
            self.assertIn("VETO BUY", window.l2_veto_badge.text())
            self.assertIn("SELL", window.l2_direction_badge.text())
            self.assertIn("WHALE SELL WALL", window.l2_alerts_feed.toPlainText())
            self.assertEqual(window.l2_dom_table.rowCount(), 21)

            # A delayed event for a different pair must not replace the active pair's L2 state.
            window._on_ws_event_received(
                {
                    "type": "l2.snapshot",
                    "payload": {
                        "symbol": "EURUSD",
                        "source": "mt5_l2_dom",
                        "best_bid": 1.0,
                        "best_ask": 1.1,
                    },
                }
            )
            self.assertEqual(window._latest_l2_snapshot.get("symbol"), "XAUUSD")
        finally:
            window.close()

    def test_orderflow_liquidity_agent_walls_spoofing_and_veto(self) -> None:
        from newera_api.orderflow_agent import OrderflowLiquidityAgent

        agent = OrderflowLiquidityAgent()
        # Snapshot 1: Huge Ask Whale Wall at 4166.50
        res1 = agent.analyze_orderbook(
            "XAUUSD",
            bids=[{"price": 4165.50 - i * 0.5, "volume": 50.0} for i in range(6)],
            asks=[
                {"price": 4166.00, "volume": 40.0},
                {"price": 4166.50, "volume": 500.0},  # Whale Sell Wall
                {"price": 4167.00, "volume": 45.0},
                {"price": 4167.50, "volume": 40.0},
                {"price": 4168.00, "volume": 50.0},
                {"price": 4168.50, "volume": 45.0},
            ],
            source="mt5_l2_dom",
        )
        self.assertTrue(res1.veto_buy)
        self.assertEqual(res1.l2_direction, "SELL")
        self.assertIsNotNone(res1.nearest_sell_wall)
        action_after_veto, veto_reasons = agent.evaluate_gate_veto("BUY", res1)
        self.assertEqual(action_after_veto, "ABSTAIN")
        self.assertTrue(len(veto_reasons) > 0)

        # Snapshot 2: Price did not reach 4166.50, but the 500.0 wall vanished -> SPOOFING alert!
        res2 = agent.analyze_orderbook(
            "XAUUSD",
            bids=[{"price": 4165.50 - i * 0.5, "volume": 50.0} for i in range(6)],
            asks=[
                {"price": 4166.00, "volume": 40.0},
                {"price": 4166.50, "volume": 20.0},  # Pulled >65% without fill
                {"price": 4167.00, "volume": 45.0},
                {"price": 4167.50, "volume": 40.0},
                {"price": 4168.00, "volume": 50.0},
                {"price": 4168.50, "volume": 45.0},
            ],
            source="mt5_l2_dom",
        )
        alert_kinds = {a.get("kind") for a in res2.alerts}
        self.assertIn("SPOOFING", alert_kinds)

    def test_tradelocker_overlay_sessions_theme_and_demo_controls(self) -> None:
        from datetime import datetime, timezone
        from PyQt6.QtCore import Qt
        from newera_api.config import settings
        from ui.desktop_app import ThemeManager, compute_trading_sessions_utc

        # 1. Verify Trading Sessions & Killzone Calculator
        dt_overlap = datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)
        sess = compute_trading_sessions_utc(dt_overlap)
        self.assertTrue(sess["overlap"])
        self.assertIn("LONDON × NEW YORK OVERLAP", sess["headline"])
        self.assertTrue(sess["sessions"]["london"]["active"])
        self.assertTrue(sess["sessions"]["new_york"]["active"])
        self.assertTrue(sess["sessions"]["new_york"]["killzone"])

        # 2. Verify Clean TradingView HTML (TradeLocker Overlay & internal banners completely removed)
        positions = [
            {
                "ticket": 9001001,
                "symbol": "XAUUSD",
                "side": "BUY",
                "volume": 0.05,
                "price_open": 4165.00,
                "price_current": 4167.50,
                "sl": 4161.75,
                "tp": 4170.42,
                "profit": 12.50,
                "pnl_pct": 0.125,
                "points_distance": 250.0,
                "pips_distance": 25.0,
                "risk_usd": 16.25,
                "reward_usd": 27.10,
                "sl_pips": 32.5,
                "tp_pips": 54.2,
                "digits": 2,
            }
        ]
        html = build_tradingview_html("OANDA:XAUUSD", "5", theme="dark")
        self.assertNotIn("window.updateTradeLockerOverlay", html)
        self.assertNotIn("tradelocker_overlay", html)
        self.assertNotIn("tv_session_hud", html)
        self.assertIn("TradingView.widget", html)
        self.assertIn("contextmenu", html)

        # 3. Verify ThemeManager & i18n
        self.assertIn("Bloomberg Dark", ThemeManager.THEMES)
        self.assertIn("Institutional Light", ThemeManager.THEMES)
        self.assertIn("Cyber Matrix", ThemeManager.THEMES)
        qss_light = ThemeManager.get_qss("Institutional Light")
        self.assertIn("#f1f5f9", qss_light)
        self.assertIn("დემო", ThemeManager.tr("exec_demo_btn", "KA"))

        # 4. Verify Spread Cost Gate & .env Sync Settings
        self.assertGreaterEqual(settings.max_spread_points, 180.0)
        self.assertGreaterEqual(settings.max_spread_to_atr, 0.45)
        self.assertTrue(settings.gemini_key_configured)

        # 5. Verify Header Live PnL, Quick Chart Buy/Sell Buttons, Open Positions Table & Trade History Panel
        window = NewEraDesktopTerminal(
            api_url="http://127.0.0.1:59999",
            ws_url="ws://127.0.0.1:59999/ws/logs",
        )
        try:
            if window.web_chart is not None:
                self.assertEqual(
                    window.web_chart.contextMenuPolicy(),
                    Qt.ContextMenuPolicy.DefaultContextMenu,
                )
            # Direct Quick Buy / Sell buttons on top of the chart
            self.assertIn("BUY MARKET", window.btn_quick_buy_market.text())
            self.assertIn("SELL MARKET", window.btn_quick_sell_market.text())

            # Header AUTO-TRADE ON/OFF button & synchronization with control-panel and Joey-tab
            self.assertTrue(hasattr(window, "btn_header_auto_trade"))
            self.assertIn("AUTO-TRADE: OFF", window.btn_header_auto_trade.text())
            self.assertFalse(window.btn_header_auto_trade.isChecked())

            # Toggle ON from Header -> all 3 controls sync to ON
            window.btn_header_auto_trade.setChecked(True)
            self.assertTrue(window.btn_auto_execute_demo.isChecked())
            self.assertTrue(window.chk_joey_autonomous.isChecked())
            self.assertIn("ACTIVE (DEMO)", window.btn_header_auto_trade.text())

            # Toggle OFF from Joey Tab -> all 3 controls sync to OFF
            window.chk_joey_autonomous.setChecked(False)
            self.assertFalse(window.btn_header_auto_trade.isChecked())
            self.assertFalse(window.btn_auto_execute_demo.isChecked())
            self.assertIn("AUTO-TRADE: OFF", window.btn_header_auto_trade.text())

            # Update open positions -> populates Open Positions table & Header Live PnL
            window._update_positions_ui(
                {
                    "positions": positions,
                    "total_floating_pnl": 12.50,
                    "floating_pnl_pct": 0.125,
                    "account_balance": 10000.0,
                }
            )
            self.assertEqual(window.open_positions_table.rowCount(), 1)
            self.assertEqual(window.open_positions_table.item(0, 0).text(), "#9001001")
            self.assertEqual(window.open_positions_table.item(0, 1).text(), "XAUUSD")
            self.assertEqual(window.open_positions_table.item(0, 2).text(), "BUY")
            self.assertIn("+$12.50", window.open_positions_table.item(0, 8).text())
            self.assertIsNotNone(window.open_positions_table.cellWidget(0, 9))
            self.assertIn("+$12.50", window.mt5_live_pnl_value.text())

            # Negative Live PnL turns red
            window._update_header_live_pnl_label(-15.0, -0.15)
            self.assertIn("-$15.00", window.mt5_live_pnl_value.text())
            self.assertIn("#ef4444", window.mt5_live_pnl_value.styleSheet())

            # Closing positions clears Open Positions table
            window._update_positions_ui({"positions": [], "floating_pnl": 0.0, "floating_pnl_pct": 0.0})
            self.assertEqual(window.open_positions_table.rowCount(), 0)
            self.assertIn("+$0.00", window.mt5_live_pnl_value.text())

            # Trade History & Stats Panel
            window._update_trade_history_ui(
                {
                    "trades": [
                        {
                            "ticket": 88001,
                            "symbol": "XAUUSD",
                            "side": "BUY",
                            "volume": 0.10,
                            "open_time": "2026-10-18 14:00:00",
                            "close_time": "2026-10-18 14:25:00",
                            "entry_price": 4160.00,
                            "exit_price": 4168.50,
                            "realized_pnl": 85.00,
                            "digits": 2,
                        }
                    ],
                    "summary": {
                        "total_realized_pnl": 85.00,
                        "win_rate_pct": 100.0,
                        "profit_factor": 99.99,
                        "total_trades": 1,
                        "winning_trades": 1,
                        "losing_trades": 0,
                    },
                }
            )
            self.assertEqual(window.trade_history_table.rowCount(), 1)
            self.assertEqual(window.trade_history_table.item(0, 0).text(), "#88001")
            self.assertIn("+$85.00", window.trade_history_table.item(0, 6).text())
            self.assertIn("+$85.00", window.hist_total_pnl_label.text())
            self.assertIn("100.0%", window.hist_win_rate_label.text())
            self.assertIn("1W / 0L", window.hist_win_rate_label.text())
            self.assertIn("Total Trades: 1", window.hist_total_trades_label.text())
        finally:
            window.close()

    def test_waterfall_gemini_cio_dynamic_position_and_telegram(self) -> None:
        import asyncio
        from newera_api.gemini_manager import (
            GeminiPortfolioManager,
            TALibTechnicalEngine,
        )
        from newera_api.mt5_bridge import compute_dynamic_position_adjustment
        from newera_api.telegram_bot import TelegramRemoteController

        # 1. Step 1 (TA-Lib Technical Filter)
        candles = []
        base_price = 4150.0
        for i in range(60):
            # Oscillating upward trend so RSI stays in healthy 55-75 range (<82 exhaustion threshold)
            wave = 0.6 if (i % 3 != 0) else -0.45
            c = base_price + i * 0.25 + wave
            candles.append(
                {
                    "open": c - 0.2,
                    "high": c + 0.8,
                    "low": c - 0.7,
                    "close": c,
                    "volume": 100.0 + i * 2.0,
                }
            )
        step1 = TALibTechnicalEngine.evaluate(candles, symbol="XAUUSD", timeframe="M5")
        self.assertEqual(step1.trend_bias, "BUY")
        self.assertGreater(step1.atr_14, 0.0)
        self.assertGreater(step1.rsi_14, 50.0)

        # 2. GeminiPortfolioManager Dynamic Risk Tiers (<65%, 65-80%, >80% + L2 Wall Support)
        cio = GeminiPortfolioManager()

        res_low = cio.compute_dynamic_risk_and_lot(
            58.0,
            l2_wall_support=True,
            equity=10000.0,
            atr=3.0,
            entry_price=4175.0,
            side="BUY",
        )
        self.assertEqual(res_low["action"], "ABSTAIN")
        self.assertEqual(res_low["risk_pct"], 0.0)
        self.assertEqual(res_low["lot_size"], 0.0)

        res_std = cio.compute_dynamic_risk_and_lot(
            72.5,
            l2_wall_support=False,
            equity=10000.0,
            atr=3.0,
            entry_price=4175.0,
            side="BUY",
        )
        self.assertIn("STANDARD_RISK", res_std["risk_tier"])
        self.assertGreaterEqual(res_std["risk_pct"], 0.25)
        self.assertLessEqual(res_std["risk_pct"], 0.50)
        self.assertGreater(res_std["lot_size"], 0.0)

        res_inc = cio.compute_dynamic_risk_and_lot(
            88.0,
            l2_wall_support=True,
            equity=10000.0,
            atr=3.0,
            entry_price=4175.0,
            side="BUY",
        )
        self.assertIn("DYNAMIC_INCREASED_RISK", res_inc["risk_tier"])
        self.assertGreaterEqual(res_inc["risk_pct"], 1.00)
        self.assertLessEqual(res_inc["risk_pct"], 1.25)
        self.assertGreater(res_inc["lot_size"], res_std["lot_size"])

        # 2b. Gemini CIO Trade Review safety contract & status
        status = cio.gemini_status()
        self.assertTrue(status["configured"])
        self.assertTrue(status["review_required"])
        self.assertIn("gemini-3.5-flash", cio._model_candidates())

        # When offline / simulated LLM returns APPROVE with 0.8x risk multiplier
        orig_call = cio._call_gemini_rest
        try:
            cio._call_gemini_rest = lambda *a, **kw: '{"verdict":"APPROVE","confidence_pct":88,"risk_multiplier":0.75,"reason_ka":"დადასტურებულია"}'
            rev_ok = cio.review_trade_with_gemini({"symbol": "XAUUSD", "final_action": "BUY", "recommended_lot": 0.20})
            self.assertEqual(rev_ok["verdict"], "APPROVE")
            self.assertEqual(rev_ok["risk_multiplier"], 0.75)
            self.assertEqual(rev_ok["source"], "gemini_api")

            # When Gemini returns REJECT -> trade is rejected
            cio._call_gemini_rest = lambda *a, **kw: '{"verdict":"REJECT","confidence_pct":40,"risk_multiplier":0.0,"reason_ka":"წინააღმდეგობაა"}'
            rev_no = cio.review_trade_with_gemini({"symbol": "XAUUSD", "final_action": "BUY", "recommended_lot": 0.20})
            self.assertEqual(rev_no["verdict"], "REJECT")
            self.assertEqual(rev_no["risk_multiplier"], 0.0)

            # Fail-safe: if Gemini API is unreachable (returns None) -> REJECT
            cio._call_gemini_rest = lambda *a, **kw: None
            rev_fail = cio.review_trade_with_gemini({"symbol": "XAUUSD", "final_action": "BUY", "recommended_lot": 0.20})
            self.assertEqual(rev_fail["verdict"], "REJECT")
            self.assertEqual(rev_fail["source"], "gemini_unavailable")
        finally:
            cio._call_gemini_rest = orig_call

        # 3. Full 4-Step Waterfall Decision Pipeline evaluation
        wf = cio.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=step1,
            kronos_report={
                "action": "BUY",
                "confidence": 0.84,
                "score": 6.5,
                "decision": {
                    "matrix": {"direction": "BUY", "score": 6.5, "buy_score": 6.5, "sell_score": 1.0},
                    "analog": {"direction": "BUY", "win_rate": 0.875},
                    "forecast": {"direction": "BUY", "move_atr": 1.45, "path_agreement": 0.85},
                },
            },
            tradingagents_report={"ai_action": "BUY", "confidence": 0.78},
            l2_snapshot={
                "l2_direction": "BUY",
                "confluence_score": 82.0,
                "veto_buy": False,
                "veto_sell": False,
                "nearest_buy_wall": {"price": 4170.0, "volume": 120.0},
            },
            mt5_meta={"bid": 4175.0, "ask": 4175.2, "atr_m5": 3.0, "digits": 2, "point": 0.01},
            account_equity=10000.0,
        )
        self.assertEqual(wf.final_action, "BUY")
        self.assertGreater(wf.win_probability_pct, 80.0)
        self.assertIn("DYNAMIC_INCREASED_RISK", wf.risk_tier)
        self.assertGreaterEqual(wf.risk_pct, 1.00)

        # 4. Dynamic Position Management (+1.0x ATR Break-Even & +2.0x ATR 50% Partial Close)
        pos_be = {
            "ticket": 991001,
            "symbol": "XAUUSD",
            "side": "BUY",
            "volume": 0.20,
            "price_open": 4160.00,
            "price_current": 4163.50,  # +3.50 profit (> +1.0x ATR of 3.0)
            "sl": 4155.50,
            "tp": 4169.00,
            "digits": 2,
            "point": 0.01,
            "break_even_active": False,
            "partial_closed_50": False,
        }
        adj1 = compute_dynamic_position_adjustment(pos_be, atr=3.0)
        self.assertTrue(adj1["sl_changed"])
        self.assertTrue(adj1["break_even_active"])
        self.assertFalse(adj1["do_partial_50"])
        self.assertGreaterEqual(adj1["new_sl"], 4160.00)

        pos_partial = {
            **pos_be,
            "price_current": 4166.50,  # +6.50 profit (> +2.0x ATR of 3.0)
            "sl": 4160.02,
            "break_even_active": True,
            "partial_closed_50": False,
        }
        adj2 = compute_dynamic_position_adjustment(pos_partial, atr=3.0)
        self.assertTrue(adj2["do_partial_50"])
        self.assertAlmostEqual(adj2["partial_close_volume"], 0.10, places=2)

        # 5. Telegram Remote Controller commands (/status, /pause, /resume)
        tg = TelegramRemoteController()
        async def _mock_handler(cmd: str) -> str:
            return f"EXEC:{cmd.upper()}"
        tg.set_command_handler(_mock_handler)
        reply_pause = asyncio.run(tg.handle_command_text("/pause"))
        self.assertEqual(reply_pause, "EXEC:/PAUSE")

        # 6. UI Gemini Quant Co-Pilot ("ჯოი") Tab & Waterfall Status Update
        window = NewEraDesktopTerminal(
            api_url="http://127.0.0.1:59999",
            ws_url="ws://127.0.0.1:59999/ws/logs",
        )
        try:
            tab_titles = [window.right_tabs.tabText(i) for i in range(window.right_tabs.count())]
            self.assertTrue(any("ჯოი" in t for t in tab_titles))
            window._update_waterfall_ui(wf.to_dict())
            self.assertIn("DYNAMIC_INCREASED_RISK", window.wf_step4_label.text())
            self.assertIn("WIN-PROB:", window.wf_prob_badge.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
