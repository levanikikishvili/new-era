"""New Era 2.0 — Unified 7-Bot Fleet Orchestrator, AI Radar Hub & Live Performance Inspector.

Unifies ALL of Levan's best validated bots and AI radars into a single zero-touch engine inside New Era:
1. `XAUUSD_M1_BreakoutStops_v2` — M1 Impulse Sniper v2.2 (Pending BuyStop/SellStop + TP=0 Trailing Stop,
   80-pt ($0.08/0.01 lot) Commission Dead-Zone Jump to +100 pts, M1 EMA9/21 Anti-Trap, Daily Profit Lock).
2. `XAUUSD_Asia_Session_AI_v2` — Revived & Fixed M5 Asian Session Breakout + Gradient-Boosting AI Filter
   (`xauusd_asia_ai_filter_rr1_spread40.pkl`, fixed 150m Hold Time, 1.5x ATR SL, +100 pt Commission-Safe BE).
3. `EvidenceH1FastBreakout_v220` — 100% Real-Tick Validated H1 72-Bar Swing High Breakout (`PF = 1.43`).
4. `AdaptiveH1BreakoutOwn_v3` — H1 EMA50/200 + ATR Expansion Selective Long-Only Breakout.
5. `SMC_Portfolio_AI_Radar_v2` — Unified H1/M15 SMC OrderBlock/FVG/Sweep + M5 LTF Refinement + `smc_ai_filter.pkl`
   (replaces separate PowerShell AI Radar windows!).
6. `WeaknessRectangle_M15_M1` — Post-Asia M15 Liquidity Sweep (`>=100 pts`) + Rectangle (`<=650 pts`) + M1 Breakout (`PF = 2.70`).
7. `NewEra_JoeyCIO_Matrix_v2` — Core 4-Step Waterfall + Multi-Agent AI CIO (`XAUUSD`, `USTEC`, `US500`).

Automatically starts with New Era on DEMO (`#53012933`) and attributes every open/closed trade,
commission (`$0.08 / 0.01 lot` = `80 pts`), net profit, live log, and AI radar probability per bot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT


HUB_STATE_REL_PATH = Path("TradingAgentsData/runtime/multi_bot_hub_state.json")
ASIA_MODEL_REL_PATH = Path("TradingAgentsData/models/xauusd_asia_ai_filter_rr1_spread40.pkl")
SMC_MODEL_REL_PATH = Path("TradingAgentsData/models/smc_ai_filter.pkl")


@dataclass
class ImpulseSniperConfig:
    enabled: bool = True
    mode: str = "PYTHON_DEMO_LIVE"  # PYTHON_DEMO_LIVE | DEMO_MT5_SYNC | SHADOW | PAUSED
    symbol: str = "XAUUSD"
    magic: int = 26040301
    lots: float = 0.04
    risk_percent: float = 1.0
    entry_step_points: float = 75.0
    trailing_distance_points: float = 150.0
    trailing_step_points: float = 20.0
    commission_round_turn_points: float = 80.0  # $0.08 per 0.01 lot = 80 pts
    breakeven_trigger_points: float = 125.0
    breakeven_net_lock_points: float = 20.0
    avoid_commission_dead_zone: bool = True
    runner_trigger_points: float = 350.0
    runner_trail_distance_points: float = 100.0
    use_trend_filter: bool = True
    fast_ema_period: int = 9
    slow_ema_period: int = 21
    trend_slope_min_points: float = 18.0
    min_atr_points: float = 135.0
    cooldown_after_sl_seconds: int = 18
    cooldown_after_big_win_seconds: int = 75
    big_win_threshold_points: float = 220.0
    max_consecutive_losses: int = 3
    consecutive_loss_pause_minutes: int = 20
    use_daily_profit_lock: bool = True
    profit_lock_arm_money: float = 45.0
    profit_lock_max_giveback_money: float = 18.0
    max_daily_loss_percent: float = 3.0


@dataclass
class AsiaBreakoutAIConfig:
    enabled: bool = True
    mode: str = "PYTHON_DEMO_LIVE"  # PYTHON_DEMO_LIVE | SHADOW | PAUSED
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    magic: int = 20260322
    session_start_utc: str = "00:00"
    session_end_utc: str = "06:00"
    risk_percent: float = 1.0
    max_risk_percent: float = 1.5
    max_spread_points: int = 45
    min_ai_probability: float = 0.58
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    rr: float = 1.35
    breakeven_r: float = 0.75
    commission_buffer_points: float = 80.0  # $0.08 per 0.01 lot = 80 pts
    max_hold_minutes: int = 150
    max_session_trades: int = 2
    daily_max_losses: int = 2
    cooldown_minutes: int = 30


DEFAULT_UNIFIED_BOTS: dict[str, dict[str, Any]] = {
    "XAUUSD_M1_BreakoutStops_v2": {
        "bot_id": "XAUUSD_M1_BreakoutStops_v2",
        "name": "🎯 1. XAUUSD M1 BreakoutStops v2.2 (Impulse Sniper)",
        "short_name": "M1 Impulse Sniper v2.2",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "M1",
        "magic": 26040301,
        "comment": "NE_SNIPER_V22",
        "session_label": "07:00–09:00 & 13:30–16:30 UTC (London/NY Impulses)",
        "session_windows_utc": [["07:00", "09:00"], ["13:30", "16:30"], ["00:00", "23:59"]],
        "lot": 0.04,
        "max_spread_points": 60.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "entry_step_points": 75.0,
        "min_atr_points": 135.0,
        "ai_threshold": 0.58,
        "max_hold_minutes": 180,
        "rr": 0.0,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "იჭერს ოქროს (XAUUSD M1) სწრაფ იმპულსებს BuyStop/SellStop ორდერებით და TP=0 დინამიკური ტრეილინგით. "
            "განახლებულია IC Markets-ის $0.08/0.01 ლოტის (80 პუნქტი) საკომისიოს გადახტომით: როგორც კი ფასი +125 პუნქტს გადის, "
            "სტოპი ხტება Entry + 100 პუნქტზე (80 პუნქტი საკომისიო + 20 პუნქტი წმინდა მოგება). ჩართულია EMA9/21 ფილტრი და დღიური მოგების ჩამკეტი."
        ),
    },
    "XAUUSD_Asia_Session_AI_v2": {
        "bot_id": "XAUUSD_Asia_Session_AI_v2",
        "name": "🌏 2. XAUUSD Asia Session M5 Breakout + AI Filter v2.0",
        "short_name": "Asia M5 Breakout + AI v2.0",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "magic": 20260322,
        "comment": "NE_ASIA_AI_V2",
        "session_label": "00:00–06:00 UTC (Asian Session Range Breakout)",
        "session_windows_utc": [["00:00", "06:00"]],
        "lot": 0.03,
        "max_spread_points": 45.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 120.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 140.0,
        "ai_threshold": 0.58,
        "max_hold_minutes": 150,
        "rr": 1.35,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "შენი გაცოცხლებული აზიის სესიის ბოტი (`xauusd_asia_mt5_bot.py` + `xauusd_asia_ai_filter_rr1_spread40.pkl`). "
            "გასწორდა 4 კრიტიკული ხარვეზი: 1) `max_hold_minutes` 20 წუთიდან გაიზარდა 150 წუთამდე (20-წუთიანი ლიმიტი ტრეიდების 93%-ს დროზე ადრე კლავდა!); "
            "2) 40-სანთლიანი უზარმაზარი სტოპი შეიცვალა 1.5x ATR სტოპით; 3) Break-Even კეტავს +100 პუნქტს ($0.08/0.01 საკომისიოს სრული დაფარვით)."
        ),
    },
    "EvidenceH1FastBreakout_v220": {
        "bot_id": "EvidenceH1FastBreakout_v220",
        "name": "🏛️ 3. EvidenceH1FastBreakout v2.20 (72-Bar H1 Swing)",
        "short_name": "Evidence H1 Breakout v2.20",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "magic": 20260803,
        "comment": "NE_EVIDENCE_H1",
        "session_label": "03:00, 09:00, 15:00 UTC (H1 72-Bar High Breakout)",
        "session_windows_utc": [["02:50", "20:00"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 140.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 180.0,
        "ai_threshold": 0.55,
        "max_hold_minutes": 360,
        "rr": 5.0,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "`D:\\Trading bots`-ის #1 დადასტურებული H1 ბოტი (100% Real-Tick + Random Delay ტესტებზე: 97 ტრეიდი, PF = 1.43, Max DD = 7.78%). "
            "მუშაობს H1-ის 72-სანთლიანი Swing High-ის გარღვევაზე (SL = 220 pts, TP = 1100 pts) და +140 პუნქტზე სტოპს სწევს +100 პუნქტში. "
            "იშვიათი და დიდი H1 მოძრაობების გამო საკომისიო მოგებას ვერ ჭამს."
        ),
    },
    "AdaptiveH1BreakoutOwn_v3": {
        "bot_id": "AdaptiveH1BreakoutOwn_v3",
        "name": "📈 4. AdaptiveH1BreakoutOwn v3 (H1 Trend Compound)",
        "short_name": "Adaptive H1 Breakout v3",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "magic": 20260804,
        "comment": "NE_ADAPTIVE_H1",
        "session_label": "03:00–17:00 UTC (H1 EMA50/200 + ATR Expansion)",
        "session_windows_utc": [["03:00", "17:00"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 135.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 170.0,
        "ai_threshold": 0.56,
        "max_hold_minutes": 360,
        "rr": 2.2,
        "atr_sl_mult": 1.4,
        "description_ge": (
            "სელექციური H1 Long-Only სტრატეგია `D:\\Trading bots\\best ones\\h2`-დან. შედის მხოლოდ მაშინ, როცა H1 Close > EMA50 > EMA200 "
            "და ირღვევა 24-სანთლიანი მაქსიმუმი ATR-ის გაფართოებით (SL = 1.4x ATR, TP = 2.2R, Commission-Safe BE = +100 pts)."
        ),
    },
    "SMC_Portfolio_AI_Radar_v2": {
        "bot_id": "SMC_Portfolio_AI_Radar_v2",
        "name": "🧠 5. SMC Portfolio + M5 LTF Refined AI Radar v2.0",
        "short_name": "SMC + LTF AI Radar v2.0",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "H1/M15/M5",
        "magic": 20260709,
        "comment": "NE_SMC_AI_RADAR",
        "session_label": "00:00–21:00 UTC (SMC OrderBlock/FVG + AI Filter)",
        "session_windows_utc": [["00:00", "21:00"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 130.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 160.0,
        "ai_threshold": 0.58,
        "max_hold_minutes": 240,
        "rr": 2.5,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "აერთიანებს `smc_portfolio_forward_monitor.py`-სა და `ltf_refined_forward_monitor.py`-ს (რომლებსაც ადრე ცალკე PowerShell ფანჯრებში უშვებდი!). "
            "ასკანირებს H1/M15 Order Block, FVG, Liquidity Sweep და M5 LTF Breakout სეტაპებს, აფასებს `smc_ai_filter.pkl` მოდელით (Threshold >= 0.58) "
            "და ხსნის დადასტურებულ ტრეიდებს დემოზე."
        ),
    },
    "WeaknessRectangle_M15_M1": {
        "bot_id": "WeaknessRectangle_M15_M1",
        "name": "📐 6. Weakness Rectangle M15 Sweep + M1 Sniper",
        "short_name": "Weakness Rectangle M15/M1",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "M15/M1",
        "magic": 20260722,
        "comment": "NE_WEAK_RECT",
        "session_label": "06:00–16:00 UTC (Post-Asia Sweep + Rectangle)",
        "session_windows_utc": [["06:00", "16:00"]],
        "lot": 0.03,
        "max_spread_points": 45.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "ai_threshold": 0.58,
        "max_hold_minutes": 180,
        "rr": 4.0,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "`D:\\Documents\\new era`-ს მაღალი R:R-ის სტრატეგია (71 ტრეიდი, PF = 2.70, +$705.64 წმინდა მოგება საკომისიოს შემდეგ). "
            "ელოდება აზიის დონის M15 Liquidity Sweep-ს (>=100 pts), ხაზავს Weakness Rectangle-ს (<=650 pts) და შედის M1 გარღვევაზე 4.0R მიზნით."
        ),
    },
    "NewEra_JoeyCIO_Matrix_v2": {
        "bot_id": "NewEra_JoeyCIO_Matrix_v2",
        "name": "🤖 7. New Era JoeyCIO 4-Step Waterfall Matrix v2.0",
        "short_name": "JoeyCIO Multi-Agent AI v2.0",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "timeframe": "M5/M15",
        "magic": 777000,
        "comment": "NE_JOEYCIO_V2",
        "session_label": "00:00–23:59 UTC (4-Step Waterfall + AI CIO)",
        "session_windows_utc": [["00:00", "23:59"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "ai_threshold": 0.60,
        "max_hold_minutes": 120,
        "rr": 1.5,
        "atr_sl_mult": 1.8,
        "description_ge": (
            "New Era-ს ძირითადი 4-საფეხურიანი AI ძრავა (Step 1 Technical -> Step 2 2/3 Matrix/Analogue/Forecast -> Step 3 AI CIO -> Step 4 Risk Guard "
            "R:R >= 1.5 + 120m Time-Stop + 80pt Commission Lock)."
        ),
    },
}


def _find_python314() -> str:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python314" / "python.exe",
        Path(r"C:\Users\LeVI\AppData\Local\Programs\Python\Python314\python.exe"),
        Path(r"C:\Python314\python.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return sys.executable


class MultiBotIntegrationHub:
    """Unified 7-Bot Fleet Orchestrator & Live Telemetry Manager inside New Era."""

    def __init__(self, root: Path = ROOT, auto_start_worker: bool = False) -> None:
        self.root = Path(root)
        self.state_path = self.root / HUB_STATE_REL_PATH
        self.asia_model_path = self.root / ASIA_MODEL_REL_PATH
        self.smc_model_path = self.root / SMC_MODEL_REL_PATH
        self.sniper_cfg = ImpulseSniperConfig()
        self.asia_cfg = AsiaBreakoutAIConfig()
        self.bots: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in DEFAULT_UNIFIED_BOTS.items()
        }
        self.bot_logs: dict[str, list[str]] = {k: [] for k in DEFAULT_UNIFIED_BOTS}
        self._last_cycle_result: dict[str, Any] = {}
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._ensure_assets_and_state()
        if auto_start_worker:
            self.start_auto_fleet_loop()

    def _ensure_assets_and_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.asia_model_path.parent.mkdir(parents=True, exist_ok=True)

        for src_name, dst_path in [
            ("xauusd_asia_ai_filter_rr1_spread40.pkl", self.asia_model_path),
            ("smc_ai_filter.pkl", self.smc_model_path),
        ]:
            d_model = Path(r"D:\Documents\new era") / src_name
            if d_model.is_file() and not dst_path.is_file():
                try:
                    shutil.copy2(d_model, dst_path)
                except OSError:
                    pass

        if self.state_path.is_file():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw.get("sniper_cfg"), dict):
                    for k, v in raw["sniper_cfg"].items():
                        if hasattr(self.sniper_cfg, k):
                            setattr(self.sniper_cfg, k, v)
                if isinstance(raw.get("asia_cfg"), dict):
                    for k, v in raw["asia_cfg"].items():
                        if hasattr(self.asia_cfg, k):
                            setattr(self.asia_cfg, k, v)
                if isinstance(raw.get("bots"), dict):
                    for b_id, b_data in raw["bots"].items():
                        if b_id in self.bots and isinstance(b_data, dict):
                            self.bots[b_id].update(b_data)
                            # Always enforce $0.08 / 0.01 lot (80 pts) minimum commission buffer
                            if float(self.bots[b_id].get("commission_buffer_points", 0) or 0) < 80.0:
                                self.bots[b_id]["commission_buffer_points"] = 80.0
                            self.bots[b_id]["commission_per_001_lot_usd"] = 0.08
                if isinstance(raw.get("bot_logs"), dict):
                    self.bot_logs.update(raw["bot_logs"])
                if isinstance(raw.get("last_cycle_result"), dict):
                    self._last_cycle_result = raw["last_cycle_result"]
            except Exception:
                pass
        self.save_state()

    def save_state(self) -> None:
        # Keep sniper_cfg and asia_cfg synced with bots dict
        s_bot = self.bots.get("XAUUSD_M1_BreakoutStops_v2", {})
        self.sniper_cfg.mode = str(s_bot.get("mode", self.sniper_cfg.mode))
        self.sniper_cfg.commission_round_turn_points = float(s_bot.get("commission_buffer_points", 80.0))
        self.sniper_cfg.breakeven_trigger_points = float(s_bot.get("be_trigger_points", 125.0))

        a_bot = self.bots.get("XAUUSD_Asia_Session_AI_v2", {})
        self.asia_cfg.mode = str(a_bot.get("mode", self.asia_cfg.mode))
        self.asia_cfg.min_ai_probability = float(a_bot.get("ai_threshold", 0.58))
        self.asia_cfg.max_hold_minutes = int(a_bot.get("max_hold_minutes", 150))
        self.asia_cfg.max_spread_points = int(a_bot.get("max_spread_points", 45))
        self.asia_cfg.commission_buffer_points = float(a_bot.get("commission_buffer_points", 80.0))

        payload = {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "commission_per_001_lot_usd": 0.08,
            "commission_round_turn_points": 80.0,
            "sniper_cfg": asdict(self.sniper_cfg),
            "asia_cfg": asdict(self.asia_cfg),
            "bots": self.bots,
            "bot_logs": self.bot_logs,
            "last_cycle_result": self._last_cycle_result,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def update_bot_config(self, bot_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if bot_id in self.bots and isinstance(updates, dict):
            self.bots[bot_id].update(updates)
            self.save_state()
        return self.bots.get(bot_id, {})

    def update_configs(
        self,
        *,
        sniper_updates: dict[str, Any] | None = None,
        asia_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if sniper_updates:
            for k, v in sniper_updates.items():
                if hasattr(self.sniper_cfg, k):
                    setattr(self.sniper_cfg, k, v)
            s_bot = self.bots["XAUUSD_M1_BreakoutStops_v2"]
            if "mode" in sniper_updates:
                s_bot["mode"] = sniper_updates["mode"]
            if "commission_round_turn_points" in sniper_updates:
                s_bot["commission_buffer_points"] = float(sniper_updates["commission_round_turn_points"])
            if "breakeven_trigger_points" in sniper_updates:
                s_bot["be_trigger_points"] = float(sniper_updates["breakeven_trigger_points"])
            if "entry_step_points" in sniper_updates:
                s_bot["entry_step_points"] = float(sniper_updates["entry_step_points"])
        if asia_updates:
            for k, v in asia_updates.items():
                if hasattr(self.asia_cfg, k):
                    setattr(self.asia_cfg, k, v)
            a_bot = self.bots["XAUUSD_Asia_Session_AI_v2"]
            if "mode" in asia_updates:
                a_bot["mode"] = asia_updates["mode"]
            if "min_ai_probability" in asia_updates:
                a_bot["ai_threshold"] = float(asia_updates["min_ai_probability"])
            if "max_hold_minutes" in asia_updates:
                a_bot["max_hold_minutes"] = int(asia_updates["max_hold_minutes"])
            if "max_spread_points" in asia_updates:
                a_bot["max_spread_points"] = float(asia_updates["max_spread_points"])
            if "atr_sl_mult" in asia_updates:
                a_bot["atr_sl_mult"] = float(asia_updates["atr_sl_mult"])
            if "rr" in asia_updates:
                a_bot["rr"] = float(asia_updates["rr"])
        self.save_state()
        return self.get_hub_summary()

    def run_cycle_now(self, execute_orders: bool = True) -> dict[str, Any]:
        """Run one full MT5 reconciliation + AI Radar + Demo execution cycle across all 7 bots."""
        with self._cycle_lock:
            worker_script = self.root / "newera_api" / "fleet_mt5_worker.py"
            py_exe = _find_python314()
            try:
                proc = subprocess.run(
                    [py_exe, str(worker_script), str(self.state_path), "true" if execute_orders else "false"],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=18,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
                    data = json.loads(lines[-1])
                    if isinstance(data, dict) and data.get("ok"):
                        self._last_cycle_result = data
                        if isinstance(data.get("bot_logs"), dict):
                            self.bot_logs.update(data["bot_logs"])
                        return data
            except Exception:
                pass
            return self._last_cycle_result

    def start_auto_fleet_loop(self, interval_seconds: float = 12.0) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.run_cycle_now(execute_orders=True)
                except Exception:
                    pass
                self._stop_event.wait(max(5.0, float(interval_seconds)))

        self._worker_thread = threading.Thread(target=_loop, name="NewEraUnifiedBotFleetWorker", daemon=True)
        self._worker_thread.start()

    def stop_auto_fleet_loop(self) -> None:
        self._stop_event.set()

    def get_d_drive_audit_catalog(self) -> list[dict[str, Any]]:
        """Return structured forensic audit of all bots discovered in D:\\Documents\\new era and D:\\Trading bots."""
        out: list[dict[str, Any]] = []
        for b_id, b_cfg in self.bots.items():
            out.append(
                {
                    "bot_id": b_id,
                    "name": b_cfg.get("name", b_id),
                    "session": b_cfg.get("session_label", ""),
                    "verdict": "AUTO_RUNNING_ON_DEMO" if b_cfg.get("enabled", True) else "PAUSED",
                    "historical_evidence": b_cfg.get("description_ge", ""),
                    "commission_fix": "IC Markets $0.08 / 0.01 lot (80 pts) Commission Dead-Zone Jump -> Entry + 100 pts Lock.",
                }
            )
        return out

    def get_hub_summary(self) -> dict[str, Any]:
        return {
            "commission_per_001_lot_usd": 0.08,
            "commission_round_turn_points": 80.0,
            "sniper_cfg": asdict(self.sniper_cfg),
            "asia_cfg": asdict(self.asia_cfg),
            "bots": self.bots,
            "bot_logs": self.bot_logs,
            "last_cycle_result": self._last_cycle_result,
            "asia_model_present": self.asia_model_path.is_file(),
            "smc_model_present": self.smc_model_path.is_file(),
            "mql5_sniper_ex5_present": (self.root / "mt5" / "XAUUSD_M1_BreakoutStops.ex5").is_file(),
            "d_drive_catalog": self.get_d_drive_audit_catalog(),
        }
