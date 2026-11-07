"""New Era 2.0 — Unified 10-Bot Multi-Asset Fleet Orchestrator, AI Radar Hub & Live Performance Inspector.

Unifies Levan's validated bots, multi-pair research setups, and top GitHub quantitative strategies
into a single zero-touch multi-asset engine inside New Era across 11 liquid instruments
(`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`, `USTEC`, `US500`, `BTCUSD`, `ETHUSD`):

1. `XAUUSD_M1_BreakoutStops_v2` — M1 Impulse Sniper v2.2 (`XAUUSD`, Pending BuyStop/SellStop + TP=0 Trailing Stop,
   80-pt ($0.08/0.01 lot) Commission Dead-Zone Jump to +100 pts, M1 EMA9/21 Anti-Trap, Daily Profit Lock).
2. `XAUUSD_Asia_Session_AI_v2` — 7-Pair Multi-Asset Asian Session Breakout & Fakeout + AI Filter v2.2
   (`XAUUSD`, `USDJPY`, `AUDUSD`, `NZDUSD`, `EURUSD`, `GBPUSD`, `USDCAD`).
3. `EvidenceH1FastBreakout_v220` — 100% Real-Tick Validated H1 Swing High Breakout (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`).
4. `AdaptiveH1BreakoutOwn_v3` — H1 EMA50/200 + ATR Expansion Trend Breakout (`XAUUSD`, `USTEC`, `US500`, `EURUSD`, `USDJPY`).
5. `SMC_Portfolio_AI_Radar_v2` — Multi-Pair H1/M15 SMC OrderBlock/FVG/Sweep + M5 LTF Refinement (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `USTEC`, `US500`)
   with 15m per-symbol anti-cluster cooldown and capped SL.
6. `WeaknessRectangle_M15_M1` — Post-Asia M15 Liquidity Sweep + M1 Tight Rectangle Breakout (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`).
7. `NewEra_JoeyCIO_Matrix_v2` — Core 4-Step Waterfall + Multi-Agent AI CIO (`XAUUSD`, `USTEC`, `US500`, `EURUSD`, `GBPUSD`, `BTCUSD`).
8. `NY_ORB_Index_Sniper_v1` — GitHub Institutional London/NY Opening Range Breakout (ORB) + Displacement (`USTEC`, `US500`, `XAUUSD`, `GBPUSD`).
9. `Quant_ZScore_MeanReversion_v1` — GitHub Statistical Arbitrage 20-Bar Z-Score (`|Z|>=1.95`) + Bollinger/RSI Mean Reversion across 7 FX Pairs (`EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`).
10. `Crypto_FX_Compression_Breakout_v1` — #1 OOS Ranked Volatility Squeeze (`PF=3.49`, `OOS WR=90.9%`) + H1 Trend Breakout (`BTCUSD`, `ETHUSD`, `XAUUSD`, `EURUSD`, `USDJPY`, `USTEC`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
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
    commission_round_turn_points: float = 80.0  # $0.08 per 0.01 lot = 80 pts on XAUUSD
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
    symbol: str = "XAUUSD,USDJPY,AUDUSD,NZDUSD,EURUSD,GBPUSD,USDCAD"
    timeframe: str = "M5"
    magic: int = 20260322
    session_start_utc: str = "00:00"
    session_end_utc: str = "08:00"
    risk_percent: float = 1.0
    max_risk_percent: float = 1.5
    max_spread_points: int = 45
    min_ai_probability: float = 0.58
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    rr: float = 1.50
    breakeven_r: float = 0.75
    commission_buffer_points: float = 80.0
    max_hold_minutes: int = 150
    max_session_trades: int = 4
    daily_max_losses: int = 3
    cooldown_minutes: int = 20


DEFAULT_UNIFIED_BOTS: dict[str, dict[str, Any]] = {
    "XAUUSD_M1_BreakoutStops_v2": {
        "bot_id": "XAUUSD_M1_BreakoutStops_v2",
        "name": "🎯 1. XAUUSD M1 BreakoutStops v2.2 (Impulse Sniper)",
        "short_name": "M1 Impulse Sniper v2.2",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD",
        "symbols": ["XAUUSD"],
        "timeframe": "M1",
        "magic": 26040301,
        "comment": "NE_SNIPER_V22",
        "session_label": "00:00–23:59 UTC [XAUUSD] (M1 Impulse Sniper)",
        "session_windows_utc": [["00:00", "23:59"]],
        "lot": 0.04,
        "max_spread_points": 60.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "entry_step_points": 75.0,
        "min_atr_points": 135.0,
        "cooldown_minutes": 10,
        "max_open_positions": 1,
        "ai_threshold": 0.58,
        "max_hold_minutes": 180,
        "rr": 0.0,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "პარასკევის #1 ჩემპიონი ბოტი (+$43.56 სუფთა მოგება 5 სნაიპერულ თრეიდში!). იჭერს XAUUSD M1 სწრაფ იმპულსებს "
            "BuyStop/SellStop ორდერებით და TP=0 დინამიკური ტრეილინგით. ჩართულია $0.08/0.01 ლოტის (80pt) საკომისიოს გადახტომა "
            "(Entry + 100pt Lock), M1 ATR >= 135pt ფილტრი და დღიური მოგების ჩამკეტი."
        ),
    },
    "XAUUSD_Asia_Session_AI_v2": {
        "bot_id": "XAUUSD_Asia_Session_AI_v2",
        "name": "🌏 2. Multi-Pair Asia Session 7-Pairs + AI Filter v2.2",
        "short_name": "Asia 7-Pairs + AI v2.2",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,USDJPY,AUDUSD,NZDUSD,EURUSD,GBPUSD,USDCAD",
        "symbols": ["XAUUSD", "USDJPY", "AUDUSD", "NZDUSD", "EURUSD", "GBPUSD", "USDCAD"],
        "timeframe": "M5",
        "magic": 20260322,
        "comment": "NE_ASIA_7PAIR",
        "session_label": "00:00–08:30 UTC [7 Pairs: XAU/JPY/AUD/NZD/EUR/GBP/CAD]",
        "session_windows_utc": [["00:00", "08:30"]],
        "lot": 0.03,
        "max_spread_points": 45.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 120.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 140.0,
        "cooldown_minutes": 20,
        "max_open_positions": 3,
        "ai_threshold": 0.58,
        "max_hold_minutes": 150,
        "rr": 1.5,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "შენი აზიის სესიის ბოტი გაფართოებული 7 წყვილზე (`XAUUSD`, `USDJPY`, `AUDUSD`, `NZDUSD`, `EURUSD`, `GBPUSD`, `USDCAD`)! "
            "გასწორდა ოქროს რეინჯის ფილტრი (2200 პუნქტამდე) და თითოეული ვალუტისთვის ავტომატურად ითვლება სპრედის, ATR-ის და "
            "საკომისიოს ($0.08/0.01 lot) ზუსტი პუნქტები. მუშაობს როგორც აზიის რეინჯის გარღვევაზე (Breakout), ისე ცრუ გარღვევის შემობრუნებაზე (Sweep Reclaim)."
        ),
    },
    "EvidenceH1FastBreakout_v220": {
        "bot_id": "EvidenceH1FastBreakout_v220",
        "name": "🏛️ 3. EvidenceH1FastBreakout v2.20 (Multi-Pair H1 Swing)",
        "short_name": "Evidence H1 Multi-Pair",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,EURUSD,GBPUSD,USDJPY",
        "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"],
        "timeframe": "H1",
        "magic": 20260803,
        "comment": "NE_EVIDENCE_H1",
        "session_label": "02:30–20:00 UTC [XAUUSD/EURUSD/GBPUSD/USDJPY]",
        "session_windows_utc": [["02:30", "20:00"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 140.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 180.0,
        "cooldown_minutes": 30,
        "max_open_positions": 2,
        "ai_threshold": 0.56,
        "max_hold_minutes": 360,
        "rr": 3.5,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "`D:\\Trading bots`-ის #1 Real-Tick ვალიდირებული H1 Swing Breakout სტრატეგია, ახლა უკვე 4 წყვილზე (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`). "
            "იჭერს H1-ის სტრუქტურული მწვერვალის/ფსკერის (36/72-სანთლიანი Swing High/Low) იმპულსურ გარღვევას ტრენდის მიმართულებით."
        ),
    },
    "AdaptiveH1BreakoutOwn_v3": {
        "bot_id": "AdaptiveH1BreakoutOwn_v3",
        "name": "📈 4. AdaptiveH1BreakoutOwn v3 (Metals & Indices H1 Trend)",
        "short_name": "Adaptive H1 Trend v3",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,USTEC,US500,EURUSD,USDJPY",
        "symbols": ["XAUUSD", "USTEC", "US500", "EURUSD", "USDJPY"],
        "timeframe": "H1",
        "magic": 20260804,
        "comment": "NE_ADAPTIVE_H1",
        "session_label": "03:00–19:00 UTC [XAUUSD/USTEC/US500/EURUSD/USDJPY]",
        "session_windows_utc": [["03:00", "19:00"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 135.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 170.0,
        "cooldown_minutes": 30,
        "max_open_positions": 2,
        "ai_threshold": 0.58,
        "max_hold_minutes": 360,
        "rr": 2.2,
        "atr_sl_mult": 1.4,
        "description_ge": (
            "სელექციური H1/M15 Trend Breakout სტრატეგია ოქროზე, ამერიკულ ინდექსებსა (`USTEC`, `US500`) და მთავარ ფორექს წყვილებზე (`EURUSD`, `USDJPY`). "
            "შედის როგორც BUY, ისე SELL მიმართულებით, როცა H1 EMA50/200 ტრენდი და ATR-ის გაფართოება ემთხვევა 20-სანთლიანი დონის გარღვევას."
        ),
    },
    "SMC_Portfolio_AI_Radar_v2": {
        "bot_id": "SMC_Portfolio_AI_Radar_v2",
        "name": "🧠 5. SMC Portfolio 6-Pairs + M5 LTF AI Radar v2.2 (90% WR)",
        "short_name": "SMC 6-Pairs AI Radar v2.2",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,EURUSD,GBPUSD,USDJPY,USTEC,US500",
        "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USTEC", "US500"],
        "timeframe": "H1/M15/M5",
        "magic": 20260709,
        "comment": "NE_SMC_AI_RADAR",
        "session_label": "00:00–21:30 UTC [XAU/EUR/GBP/JPY/USTEC/US500]",
        "session_windows_utc": [["00:00", "21:30"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "cooldown_minutes": 18,
        "max_open_positions": 3,
        "ai_threshold": 0.60,
        "max_hold_minutes": 240,
        "rr": 2.4,
        "atr_sl_mult": 1.4,
        "description_ge": (
            "5–6 ნოემბერს დადო 90.0% Win Rate (20 თრეიდიდან 18 მოგება = +$84.57!). ახლა დაემატა: "
            "1) 18-წუთიანი Cooldown თითოეულ წყვილზე (რომ ერთსა და იმავე M15 სანთელზე 5-ჯერ არ შევიდეს და მწვერვალზე არ წამოეგოს); "
            "2) მაქსიმალური SL-ის შეზღუდვა (ოქროზე max 360pt = ~$10 ნაცვლად -$38-ისა!); "
            "3) სრული 6-წყვილიანი სკანირება (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `USTEC`, `US500`) BUY და SELL მიმართულებით."
        ),
    },
    "WeaknessRectangle_M15_M1": {
        "bot_id": "WeaknessRectangle_M15_M1",
        "name": "📐 6. Weakness Rectangle M15 Sweep + M1 Sniper (5 Pairs)",
        "short_name": "Weakness Rect 5-Pairs",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,EURUSD,GBPUSD,USDJPY,AUDUSD",
        "symbols": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
        "timeframe": "M15/M1",
        "magic": 20260722,
        "comment": "NE_WEAK_RECT",
        "session_label": "06:00–17:00 UTC [XAU/EUR/GBP/JPY/AUD]",
        "session_windows_utc": [["06:00", "17:00"]],
        "lot": 0.03,
        "max_spread_points": 45.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "cooldown_minutes": 20,
        "max_open_positions": 2,
        "ai_threshold": 0.58,
        "max_hold_minutes": 180,
        "rr": 3.5,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "`D:\\Documents\\new era`-ს მაღალი R:R-ის სტრატეგია (PF = 2.70). ჩართულია პირდაპირი ავტომატური ვაჭრობა (`ready_to_fire = True`) "
            "5 წყვილზე (`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`): იჭერს M15 Liquidity Sweep-ს + M1 კონსოლიდაციის მართკუთხედის გარღვევას."
        ),
    },
    "NewEra_JoeyCIO_Matrix_v2": {
        "bot_id": "NewEra_JoeyCIO_Matrix_v2",
        "name": "🤖 7. New Era JoeyCIO 4-Step Waterfall Matrix v2.2 (6 Pairs)",
        "short_name": "JoeyCIO Waterfall 6-Pairs",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,USTEC,US500,EURUSD,GBPUSD,BTCUSD",
        "symbols": ["XAUUSD", "USTEC", "US500", "EURUSD", "GBPUSD", "BTCUSD"],
        "timeframe": "M5/M15",
        "magic": 777000,
        "comment": "NE_JOEYCIO_V2",
        "session_label": "00:00–23:59 UTC [XAU/USTEC/US500/EUR/GBP/BTC]",
        "session_windows_utc": [["00:00", "23:59"]],
        "lot": 0.03,
        "max_spread_points": 50.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "cooldown_minutes": 20,
        "max_open_positions": 2,
        "ai_threshold": 0.62,
        "max_hold_minutes": 120,
        "rr": 1.8,
        "atr_sl_mult": 1.6,
        "description_ge": (
            "New Era-ს ძირითადი 4-საფეხურიანი AI ძრავა 6 ინსტრუმენტზე (`XAUUSD`, `USTEC`, `US500`, `EURUSD`, `GBPUSD`, `BTCUSD`). "
            "ჩართულია პირდაპირი Execution, როცა Step 1 (EMA/RSI/MACD) + Step 2 (2/3 Matrix) + Step 3 (AI CIO Conf >= 62%) + Step 4 (Risk Guard) სრულდება."
        ),
    },
    "NY_ORB_Index_Sniper_v1": {
        "bot_id": "NY_ORB_Index_Sniper_v1",
        "name": "⚡ 8. GitHub London/NY ORB Index & Gold Sniper v1.0",
        "short_name": "London/NY ORB Sniper",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "USTEC,US500,XAUUSD,GBPUSD",
        "symbols": ["USTEC", "US500", "XAUUSD", "GBPUSD"],
        "timeframe": "M5/M15",
        "magic": 20260901,
        "comment": "NE_ORB_SNIPER",
        "session_label": "07:30–11:00 & 13:30–17:30 UTC [USTEC/US500/XAU/GBP]",
        "session_windows_utc": [["07:30", "11:00"], ["13:30", "17:30"]],
        "lot": 0.03,
        "max_spread_points": 120.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "cooldown_minutes": 25,
        "max_open_positions": 2,
        "ai_threshold": 0.60,
        "max_hold_minutes": 180,
        "rr": 2.2,
        "atr_sl_mult": 1.4,
        "description_ge": (
            "GitHub-ის #1 ინსტიტუციონალური ინდექსების და ოქროს სტრატეგია: **Opening Range Breakout (ORB) + Volume Displacement**. "
            "იჭერს ლონდონის (07:00 UTC) და ნიუ-იორკის (13:30 UTC) გახსნის დიაპაზონის (Opening Range) ძლიერ იმპულსურ გარღვევას "
            "`USTEC` (Nasdaq 100), `US500` (S&P 500), `XAUUSD` და `GBPUSD`-ზე."
        ),
    },
    "Quant_ZScore_MeanReversion_v1": {
        "bot_id": "Quant_ZScore_MeanReversion_v1",
        "name": "📊 9. GitHub Quant Z-Score Mean Reversion (All 7 FX Pairs)",
        "short_name": "Quant Z-Score 7-FX",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD",
        "symbols": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"],
        "timeframe": "M15/M5",
        "magic": 20260902,
        "comment": "NE_ZSCORE_FX7",
        "session_label": "00:00–22:00 UTC [7 FX: EUR/GBP/JPY/AUD/CAD/CHF/NZD]",
        "session_windows_utc": [["00:00", "22:00"]],
        "lot": 0.03,
        "max_spread_points": 45.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 110.0,
        "be_net_lock_points": 15.0,
        "trail_distance_points": 120.0,
        "cooldown_minutes": 25,
        "max_open_positions": 3,
        "ai_threshold": 0.60,
        "max_hold_minutes": 180,
        "rr": 1.6,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "კლასიკური კვანტური სტატისტიკური არბიტრაჟის (Mean Reversion) ბოტი GitHub-ის `quant-trading` არქიტექტურიდან შენს 7-ვე სავალუტო წყვილზე "
            "(`EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`). ითვლის 20-სანთლიან Z-Score-ს ($Z = (Close - Mean) / StdDev$) და RSI(14)-ს: "
            "როცა ფასი სტატისტიკურ ექსტრემუმს აღწევს ($|Z| >= 1.95$) და აკეთებს Wick Rejection-ს, შედის საშუალო წირისკენ (Mean) დაბრუნებაზე მაღალი Win Rate-ით."
        ),
    },
    "Crypto_FX_Compression_Breakout_v1": {
        "bot_id": "Crypto_FX_Compression_Breakout_v1",
        "name": "🚀 10. Volatility Squeeze & Crypto/FX Breakout v1.0 (PF 3.49)",
        "short_name": "Volatility Squeeze 24/7",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "BTCUSD,ETHUSD,XAUUSD,EURUSD,USDJPY,USTEC",
        "symbols": ["BTCUSD", "ETHUSD", "XAUUSD", "EURUSD", "USDJPY", "USTEC"],
        "timeframe": "M30/M15",
        "magic": 20260903,
        "comment": "NE_SQUEEZE_BRK",
        "session_label": "00:00–23:59 UTC 24/7 [BTC/ETH/XAU/EUR/JPY/USTEC]",
        "session_windows_utc": [["00:00", "23:59"]],
        "lot": 0.03,
        "max_spread_points": 600.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 130.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 160.0,
        "cooldown_minutes": 25,
        "max_open_positions": 2,
        "ai_threshold": 0.60,
        "max_hold_minutes": 240,
        "rr": 2.0,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "შენი `mtf_deep_fx_crypto_coarse_top.csv` კვლევის #1 საუკეთესო სტრატეგია (`compression_breakout_h1_trend`: Profit Factor = 3.49, "
            "Out-of-Sample Win Rate = 90.9%!) გაერთიანებული GitHub-ის TTM Volatility Squeeze-თან. მუშაობს 24/7 რეჟიმში კრიპტოზე (`BTCUSD`, `ETHUSD`), "
            "ოქროზე (`XAUUSD`), ფორექსსა (`EURUSD`, `USDJPY`) და `USTEC`-ზე: პოულობს დაბალი ვოლატილობის შეკუმშვას (Squeeze) და შედის აფეთქებაზე H1 ტრენდის მიმართულებით."
        ),
    },
    "Grok_TradingAgents_Debate_v1": {
        "bot_id": "Grok_TradingAgents_Debate_v1",
        "name": "🔥 11. Grok xAI + TradingAgents Multi-Agent Debate Desk",
        "short_name": "Grok + TradingAgents Desk",
        "enabled": True,
        "mode": "PYTHON_DEMO_LIVE",
        "symbol": "XAUUSD,USTEC,US500,EURUSD,GBPUSD,USDJPY,BTCUSD",
        "symbols": ["XAUUSD", "USTEC", "US500", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"],
        "timeframe": "H1/M15/M5",
        "magic": 20260904,
        "comment": "NE_GROK_AGENTS",
        "session_label": "00:00–23:59 UTC [Grok xAI + Tauric TradingAgents Debate]",
        "session_windows_utc": [["00:00", "23:59"]],
        "lot": 0.03,
        "max_spread_points": 500.0,
        "commission_per_001_lot_usd": 0.08,
        "commission_buffer_points": 80.0,
        "be_trigger_points": 125.0,
        "be_net_lock_points": 20.0,
        "trail_distance_points": 150.0,
        "cooldown_minutes": 20,
        "max_open_positions": 2,
        "ai_threshold": 0.64,
        "max_hold_minutes": 210,
        "rr": 2.2,
        "atr_sl_mult": 1.5,
        "description_ge": (
            "აერთიანებს GitHub-ის ვირუსულ **Grok Trading Bot (`AI-Agent-Grok-Trading-System`)**-ს და ჩვენს პროექტში ჩაშენებულ "
            "**`TauricResearch/TradingAgents`** მულტი-აგენტურ არქიტექტურას: 1) **Cross-Asset Macro Analyst** ითვლის დოლარის რეალურ ძალას (USD Basket) "
            "7 ფორექს წყვილზე და Risk-On/Off ინდექსს (`USTEC`/`US500`/`BTCUSD`); 2) **Bull Researcher vs Bear Researcher** მართავენ დებატებს (0–100 ქულა); "
            "3) **Risk Committee + Self-Reflection** ამოწმებს წინა თრეიდების მეხსიერებას და ხსნის პოზიციას მხოლოდ მაშინ, როცა ერთი მხარე >=25 ქულით იმარჯვებს!"
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
    """Unified 10-Bot Multi-Asset Fleet Orchestrator & Live Telemetry Manager inside New Era."""

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
                            # Preserve user runtime toggles (enabled, mode, lot) while upgrading multi-symbol definitions
                            default_def = dict(DEFAULT_UNIFIED_BOTS[b_id])
                            for user_key in (
                                "enabled",
                                "mode",
                                "lot",
                                "commission_buffer_points",
                                "be_trigger_points",
                                "be_net_lock_points",
                                "trail_distance_points",
                            ):
                                if user_key in b_data:
                                    default_def[user_key] = b_data[user_key]
                            self.bots[b_id] = default_def
                            if float(self.bots[b_id].get("commission_buffer_points", 0) or 0) < 80.0:
                                self.bots[b_id]["commission_buffer_points"] = 80.0
                            self.bots[b_id]["commission_per_001_lot_usd"] = 0.08
                if isinstance(raw.get("bot_logs"), dict):
                    for k, v in raw["bot_logs"].items():
                        if isinstance(v, list):
                            self.bot_logs[k] = v
                if isinstance(raw.get("last_cycle_result"), dict):
                    self._last_cycle_result = raw["last_cycle_result"]
            except Exception:
                pass
        self.save_state()

    def save_state(self) -> None:
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
        """Run one full MT5 reconciliation + Multi-Asset AI Radar + Demo execution cycle across all 10 bots."""
        with self._cycle_lock:
            worker_script = self.root / "newera_api" / "fleet_mt5_worker.py"
            py_exe = _find_python314()
            try:
                sinfo = None
                if hasattr(subprocess, "STARTUPINFO"):
                    sinfo = subprocess.STARTUPINFO()
                    sinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                    sinfo.wShowWindow = 0
                proc = subprocess.run(
                    [py_exe, str(worker_script), str(self.state_path), "true" if execute_orders else "false"],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=25,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    startupinfo=sinfo,
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
        """Return structured forensic audit of all 10 Multi-Asset bots in the Unified Fleet."""
        out: list[dict[str, Any]] = []
        for b_id, b_cfg in self.bots.items():
            out.append(
                {
                    "bot_id": b_id,
                    "name": b_cfg.get("name", b_id),
                    "symbols": b_cfg.get("symbols", [b_cfg.get("symbol", "XAUUSD")]),
                    "session": b_cfg.get("session_label", ""),
                    "verdict": "AUTO_RUNNING_ON_DEMO" if b_cfg.get("enabled", True) else "PAUSED",
                    "historical_evidence": b_cfg.get("description_ge", ""),
                    "commission_fix": "IC Markets $0.08 / 0.01 lot Commission Dead-Zone Jump + Per-Symbol Point Calibration.",
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
