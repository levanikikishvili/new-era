"""Unit & integration tests for NEW ERA 2.0 — Complete Product Transformation & Future SaaS Architecture."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from newera_api.ai_providers import AIProviderRouter, LocalOllamaProvider, OpenAIProvider, redact_secrets
from newera_api.bot_performance_intelligence import BotPerformanceIntelligenceEngine
from newera_api.gemini_manager import GeminiPortfolioManager, TALibTechnicalEngine
from newera_api.historical_intelligence import (
    EXECUTION_PIPELINE_STAGES,
    LOG_CATEGORIES,
    SIGNAL_LIFECYCLE_STATUSES,
    HistoricalIntelligenceEngine,
)
from newera_api.saas_architecture import (
    PlanTier,
    SaaSArchitectureManager,
    SubscriptionStatus,
    TenantDataScope,
    UserRole,
    hash_password,
    verify_password,
)
from newera_api.ui_v2 import NAVIGATION_SECTIONS, SETTINGS_SUBPAGES, NewEra2WorkspaceShell
from ui.desktop_app import NewEraDesktopTerminal


_APP = QApplication.instance() or QApplication([])


class NewEraV2TransformationTests(unittest.TestCase):
    def test_historical_intelligence_extracts_real_signals_and_provenance(self) -> None:
        engine = HistoricalIntelligenceEngine()
        queried = engine.query_signals(limit=100)
        self.assertGreater(queried["total_indexed"], 50)
        self.assertIn("logs/decision_history.json", queried["sources_inventory"])
        self.assertTrue(queried["sources_inventory"]["logs/decision_history.json"]["exists"])

        first = queried["signals"][0]
        self.assertRegex(first["signal_id"], r"^NE-\d{4}-\d{5}-[A-F0-9]{4}$")
        self.assertIn(first["lifecycle_status"], SIGNAL_LIFECYCLE_STATUSES)
        self.assertTrue(first["source_file"])

        # Signal Inspector returns 7 structured condition checks
        insp = engine.get_signal_inspector(first["signal_id"])
        self.assertTrue(insp["found"])
        self.assertEqual(insp["total_conditions"], 7)
        self.assertEqual(len(insp["conditions"]), 7)

        # CSV & JSON exports work cleanly
        csv_txt = engine.export_signals(queried["signals"][:5], fmt="csv")
        self.assertIn("signal_id,timestamp,symbol", csv_txt)
        json_txt = engine.export_signals(queried["signals"][:5], fmt="json")
        self.assertIn(first["signal_id"], json_txt)

    def test_why_no_trade_diagnostics_and_execution_pipeline(self) -> None:
        engine = HistoricalIntelligenceEngine()
        sample_status = {
            "mt5": {"connected": True, "is_demo": True, "account": {"login": 53012933, "server": "ICMarketsSC-Demo"}},
            "waterfall": {
                "symbol": "XAUUSD",
                "final_action": "ABSTAIN",
                "win_probability_pct": 68.0,
                "recommended_lot": 0.0,
                "rationale_ka": "ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP",
                "steps": {
                    "step1_talib": {"status": "PASS", "trend_bias": "NEUTRAL", "close": 3931.5, "atr_14": 2.87},
                    "step2_kronos_matrix": {"status": "PASS", "confluence_label": "2-of-3", "selected_direction": "SELL"},
                    "step3_l2_safety": {"status": "PASS", "l2_direction": "NEUTRAL"},
                    "step4_gemini_cio": {"status": "BLOCKED", "risk_constraint_status": "ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP"},
                },
            },
        }
        why = engine.build_why_no_trade_diagnostics(sample_status)
        self.assertEqual(why["latest_cycle"]["blocking_category"], "RISK_CAP_OR_MIN_LOT")
        self.assertTrue(why["historical_blocking_breakdown"])

        pipe = engine.build_execution_pipeline(sample_status)
        self.assertEqual(pipe["environment"], "DEMO_ONLY")
        self.assertFalse(pipe["real_account_allowed"])
        self.assertEqual(len(pipe["stages"]), len(EXECUTION_PIPELINE_STAGES))

        council = engine.build_ai_council_view(sample_status)
        self.assertEqual(len(council["agents"]), 6)

    def test_step4_min_lot_exceeds_risk_cap_rationale_is_accurate(self) -> None:
        cio = GeminiPortfolioManager()
        candles = [
            {"open": 3930.0 + i * 0.05, "high": 3933.5, "low": 3928.5, "close": 3931.0}
            for i in range(60)
        ]
        s1 = TALibTechnicalEngine.evaluate(candles, symbol="XAUUSD")
        kronos_report = {
            "symbol": "XAUUSD",
            "action": "SELL",
            "confidence": 0.68,
            "decision": {
                "matrix": {"direction": "ABSTAIN", "score": 2.0, "buy_score": 0.0, "sell_score": 2.0},
                "analog": {"direction": "SELL", "win_rate": 0.625},
                "forecast": {"direction": "SELL", "path_agreement": 1.0, "move_atr": -1.8},
                "hard_blocks": [],
            },
        }
        l2_snap = {"symbol": "XAUUSD", "mid_price": 3931.0, "best_bid": 3930.9, "best_ask": 3931.1, "l2_direction": "NEUTRAL"}
        # Equity = $500 so 0.30% risk = $1.50, while 0.01 lot SL costs ~$7.50 -> triggers ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP
        dec = cio.evaluate_waterfall_pipeline(
            symbol="XAUUSD",
            step1_talib=s1,
            kronos_report=kronos_report,
            tradingagents_report={"symbol": "XAUUSD", "action": "ABSTAIN"},
            l2_snapshot=l2_snap,
            account_equity=500.0,
        )
        self.assertEqual(dec.final_action, "ABSTAIN")
        self.assertEqual(dec.step4_gemini_cio["risk_constraint_status"], "ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP")
        self.assertIn("ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP", dec.rationale_ka)
        self.assertNotIn("ნაკლებია 65%-იან ზღვარზე", dec.rationale_ka)

    def test_verified_performance_analytics_and_unified_logs(self) -> None:
        engine = HistoricalIntelligenceEngine()
        perf = engine.build_performance_analytics()
        self.assertTrue(perf["verified_data_available"])
        self.assertGreater(perf["total_trades"], 0)
        self.assertTrue(perf["equity_curve"])
        self.assertIn("Asian (00-08 UTC)", perf["session_performance"])

        logs = engine.build_unified_logs(category="ALL", limit=50)
        self.assertEqual(tuple(logs["categories"]), LOG_CATEGORIES)
        self.assertGreater(logs["returned_entries"], 0)

    def test_ai_provider_router_and_secret_redaction(self) -> None:
        leaked = "Error with key sk-1234567890abcdefghijklmnop and AIzaSyD1234567890abcdefghijk"
        cleaned = redact_secrets(leaked)
        self.assertNotIn("sk-123456", cleaned)
        self.assertNotIn("AIzaSyD", cleaned)
        self.assertIn("[REDACTED_SECRET]", cleaned)

        router = AIProviderRouter()
        snap = router.status_snapshot()
        self.assertEqual(snap["secret_handling"], "backend_env_only_never_exposed")
        self.assertTrue(snap["local_ai_optional_and_disabled_by_default"])
        self.assertFalse(router.local_ollama.is_enabled)

    def test_saas_architecture_roles_subscriptions_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SaaSArchitectureManager(root=Path(tmp))
            overview = mgr.architecture_overview()
            self.assertEqual(overview["environment"], "DEMO_ONLY")
            self.assertFalse(overview["login_required_in_local_dev"])
            self.assertFalse(overview["payment_gateway"]["configured"])

            # Password hashing
            h = hash_password("InstitutionalPass#2026")
            self.assertTrue(verify_password("InstitutionalPass#2026", h))
            self.assertFalse(verify_password("WrongPass", h))

            # Register Standard User and verify RBAC & Tenant Isolation
            std_user = mgr.register_user(
                email="trader@example.com",
                password="SecurePassword123",
                display_name="Standard Trader",
                role=UserRole.STANDARD_USER,
                plan_tier=PlanTier.STANDARD,
            )
            self.assertTrue(std_user.has_permission("overview_dashboard"))
            self.assertFalse(std_user.has_permission("admin_console"))
            self.assertFalse(std_user.has_permission("historical_decision_explorer"))

            # Expired subscription downgrades access
            assert std_user.subscription is not None
            std_user.subscription.status = SubscriptionStatus.EXPIRED
            self.assertFalse(std_user.subscription.is_entitled)
            self.assertNotIn("trading_terminal_demo", std_user.entitled_features())

            # Tenant isolation check
            std_scope = TenantDataScope(user_id=std_user.user_id, role=std_user.role)
            own_rec = std_scope.stamp_record({"strategy_id": "strat-1"})
            other_rec = {"strategy_id": "strat-2", "owner_user_id": "other_user"}
            filtered = std_scope.filter_records([own_rec, other_rec])
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["strategy_id"], "strat-1")

    def test_newera2_workspace_shell_13_sections_and_single_window_layout(self) -> None:
        window = NewEraDesktopTerminal(
            api_url="http://127.0.0.1:59999",
            ws_url="ws://127.0.0.1:59999/ws/logs",
        )
        try:
            self.assertTrue(hasattr(window, "workspace_shell"))
            shell: NewEra2WorkspaceShell = window.workspace_shell
            self.assertEqual(shell.sidebar_nav.count(), 13)
            self.assertEqual(shell.main_viewport_stack.count(), 13)
            self.assertEqual(shell.settings_subnav.count(), len(SETTINGS_SUBPAGES))

            # Perimeter docks are hidden by default so there is zero dock clutter
            for dock in window.phase3_docks.values():
                self.assertFalse(dock.isVisible())

            # Navigate through all 13 sections cleanly
            for idx, (sec_id, _title, _sub) in enumerate(NAVIGATION_SECTIONS):
                shell.navigate_to(sec_id)
                self.assertEqual(shell.main_viewport_stack.currentIndex(), idx)

            # Sidebar collapse/expand toggle works
            shell.toggle_sidebar()
            self.assertTrue(shell._sidebar_collapsed)
            shell.toggle_sidebar()
            self.assertFalse(shell._sidebar_collapsed)

            # Verify live open positions table, bot ranking table, shadow filters, backtest table, and interactive settings
            self.assertGreaterEqual(shell.pa_bot_ranking_table.rowCount(), 6)
            self.assertEqual(shell.pa_shadow_filters_table.rowCount(), 6)
            self.assertEqual(shell.pa_trade_inspector_table.rowCount(), 255)
            self.assertGreater(shell.bt_trades_table.rowCount(), 0)
            self.assertGreater(len(shell._settings_controls), 25)
            shell._save_interactive_settings()
        finally:
            window.close()

    def test_bot_performance_intelligence_engine_reconciliation_and_ranking(self) -> None:
        engine = BotPerformanceIntelligenceEngine()
        report = engine.build_full_intelligence_report()
        self.assertTrue(report["available"])
        self.assertEqual(report["reconciled_totals"]["closed_positions"], 255)

        rankings = report["bot_ranking"]
        self.assertEqual(len(rankings), 6)
        self.assertEqual(rankings[0]["bot_name"], "JoeyCIO_Autonomous")
        self.assertAlmostEqual(rankings[0]["net_profit"], 28.68, places=2)
        self.assertEqual(rankings[0]["closed_trades"], 15)

        # Verify NewEraQuantDemo outlier discovery & AutoScreener net profit
        quant_m = report["bot_metrics"]["NewEraQuantDemo"]
        self.assertAlmostEqual(quant_m["ex_single_worst_outlier_net"], 10.57, places=2)
        screener_m = report["bot_metrics"]["NewEra_AutoScreener"]
        self.assertAlmostEqual(screener_m["net_profit"], -30.94, places=2)

        # Verify 6 Shadow-Mode filters exist and are non-destructive
        self.assertEqual(len(report["shadow_mode_filters"]), 6)
        for sf in report["shadow_mode_filters"]:
            self.assertEqual(sf["mode"], "SHADOW_MODE")


if __name__ == "__main__":
    unittest.main()
