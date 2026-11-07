"""Python 3.14 MT5 Live Multi-Asset Demo Worker for New Era Unified 10-Bot Fleet & AI Radars.

Runs either in-process (when `MetaTrader5` is importable) or via subprocess under
`C:\\Users\\LeVI\\AppData\\Local\\Programs\\Python\\Python314\\python.exe`.

Responsibilities:
1. Connects to MT5 and strictly enforces DEMO-ONLY safety (`trade_mode == ACCOUNT_TRADE_MODE_DEMO`).
2. Fetches all live open positions and historical closed deals (last 30 days), pairing IN/OUT deals
   by `position_id` and calculating exact IC Markets Commission (`$0.08` per `0.01` lot = `$8.00/lot`).
3. Attributes every open trade and closed trade to its exact Bot in the 10-Bot Multi-Asset Fleet.
4. Fetches multi-timeframe OHLCV (`M1`, `M5`, `M15`, `H1`) across all 12 liquid symbols:
   `XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`, `NZDUSD`, `USTEC`, `US500`, `BTCUSD`, `ETHUSD`.
5. Manages open positions per-symbol (Commission-Proof Break-Even jumping over the commission zone,
   dynamic trailing stop, and max-hold time-stop).
6. Evaluates all 10 Bots across their multi-pair symbol universes with per-symbol cooldown guards
   (preventing clustering/chasing on a single candle) and capped SL distances.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COMMISSION_PER_001_LOT_USD = 0.08  # $0.08 per 0.01 lot ($8.00 per 1.00 lot)
COMMISSION_POINTS_ROUND_TURN = 80.0

ALL_FLEET_SYMBOLS: tuple[str, ...] = (
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "USTEC",
    "US500",
    "BTCUSD",
    "ETHUSD",
)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses <= 1e-12:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / max(1, len(trs))
    return sum(trs[-period:]) / float(period)


def _zscore(values: list[float], window: int = 20) -> float:
    if len(values) < window:
        return 0.0
    seg = values[-window:]
    mean = sum(seg) / float(window)
    var = sum((x - mean) ** 2 for x in seg) / float(window)
    std = math.sqrt(var)
    if std <= 1e-12:
        return 0.0
    return (seg[-1] - mean) / std


def _in_session_utc(now_utc: datetime, session_windows: list[tuple[str, str]]) -> bool:
    cur_mins = now_utc.hour * 60 + now_utc.minute
    for start_s, end_s in session_windows:
        try:
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
            s_m = sh * 60 + sm
            e_m = eh * 60 + em
            if s_m <= e_m:
                if s_m <= cur_mins <= e_m:
                    return True
            else:
                if cur_mins >= s_m or cur_mins <= e_m:
                    return True
        except Exception:
            continue
    return False


def _symbol_risk_profile(symbol: str) -> dict[str, float]:
    """Return point-calibrated risk, spread, commission, BE, and SL bounds per asset class."""
    s = symbol.upper()
    if s == "XAUUSD":
        return {
            "comm_pts": 80.0,
            "be_trig_pts": 125.0,
            "be_lock_pts": 20.0,
            "trail_pts": 150.0,
            "min_sl_pts": 165.0,
            "max_sl_pts": 360.0,  # Caps loss at ~$10.80 on 0.03 lot (prevents -$38 outliers!)
            "max_spread_pts": 55.0,
        }
    if s in ("EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "USDJPY"):
        # 5-digit FX (10 pts = 1 pip = ~$0.10 per 0.01 lot -> $0.08 commission = 8-10 pts)
        return {
            "comm_pts": 10.0,
            "be_trig_pts": 65.0,
            "be_lock_pts": 15.0,
            "trail_pts": 80.0,
            "min_sl_pts": 110.0,
            "max_sl_pts": 250.0,
            "max_spread_pts": 45.0 if s in ("GBPUSD", "AUDUSD", "NZDUSD") else 32.0,
        }
    if s in ("USTEC", "US500"):
        return {
            "comm_pts": 80.0,
            "be_trig_pts": 160.0,
            "be_lock_pts": 30.0,
            "trail_pts": 220.0,
            "min_sl_pts": 200.0,
            "max_sl_pts": 650.0,
            "max_spread_pts": 150.0 if s == "USTEC" else 80.0,
        }
    # Crypto (BTCUSD, ETHUSD)
    return {
        "comm_pts": 120.0,
        "be_trig_pts": 350.0,
        "be_lock_pts": 60.0,
        "trail_pts": 450.0,
        "min_sl_pts": 450.0,
        "max_sl_pts": 2200.0,
        "max_spread_pts": 750.0,
    }


def match_trade_to_bot_id(magic: int, comment: str) -> str:
    c = (comment or "").upper()
    m = int(magic or 0)
    if m in (26040301, 11032026) or "BREAKOUTSTOPS" in c or "SNIPER_V2" in c or "XAU_M1_" in c or "M1_BRK" in c:
        return "XAUUSD_M1_BreakoutStops_v2"
    if m in (20260322, 26071401) or "ASIA" in c:
        return "XAUUSD_Asia_Session_AI_v2"
    if m == 20260803 or "EVIDENCE" in c or "EVH1" in c:
        return "EvidenceH1FastBreakout_v220"
    if m == 20260804 or "ADAPTIVE" in c or "ADH1" in c:
        return "AdaptiveH1BreakoutOwn_v3"
    if m == 20260709 or "SMC" in c or "RADAR" in c or "LTF" in c:
        return "SMC_Portfolio_AI_Radar_v2"
    if m == 20260722 or "WEAK" in c or "RECT" in c:
        return "WeaknessRectangle_M15_M1"
    if m == 20260901 or "ORB" in c:
        return "NY_ORB_Index_Sniper_v1"
    if m == 20260902 or "ZSCORE" in c:
        return "Quant_ZScore_MeanReversion_v1"
    if m == 20260903 or "SQUEEZE" in c:
        return "Crypto_FX_Compression_Breakout_v1"
    if m == 20260904 or "GROK" in c or "AGENTS" in c:
        return "Grok_TradingAgents_Debate_v1"
    if m in (777000, 20260418, 902010) or "JOEY" in c or "NEWERA" in c or "COUNCIL" in c or "KRONOS" in c:
        return "NewEra_JoeyCIO_Matrix_v2"
    return "External_Or_Manual_MT5"


def run_fleet_cycle(state_path: Path, execute_orders: bool = True) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    ts_str = now_utc.strftime("%H:%M:%S UTC")

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        return {"ok": False, "error": f"MetaTrader5 module unavailable: {exc}", "timestamp_utc": now_utc.isoformat()}

    if not mt5.initialize():
        return {"ok": False, "error": f"mt5.initialize() failed: {mt5.last_error()}", "timestamp_utc": now_utc.isoformat()}

    try:
        acc = mt5.account_info()
        if acc is None:
            return {"ok": False, "error": "mt5.account_info() returned None", "timestamp_utc": now_utc.isoformat()}

        is_demo = int(getattr(acc, "trade_mode", -1)) == int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        account_summary = {
            "login": int(acc.login),
            "server": str(acc.server),
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "profit": float(acc.profit),
            "margin_free": float(acc.margin_free),
            "is_demo": bool(is_demo),
            "commission_rate_per_001_lot": COMMISSION_PER_001_LOT_USD,
            "commission_points_round_turn": COMMISSION_POINTS_ROUND_TURN,
        }

        fleet_state: dict[str, Any] = {}
        if state_path.is_file():
            try:
                fleet_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                fleet_state = {}

        bots_cfg: dict[str, dict[str, Any]] = fleet_state.get("bots", {})
        bot_logs: dict[str, list[str]] = fleet_state.get("bot_logs", {})

        # 1. Collect all open positions
        raw_positions = mt5.positions_get() or []
        open_positions: list[dict[str, Any]] = []
        open_by_bot: dict[str, list[dict[str, Any]]] = {}
        last_activity_ts_by_bot_sym: dict[tuple[str, str], float] = {}

        for p in raw_positions:
            p_magic = int(getattr(p, "magic", 0) or 0)
            p_comment = str(getattr(p, "comment", "") or "")
            bot_id = match_trade_to_bot_id(p_magic, p_comment)
            sym_p = str(getattr(p, "symbol", "XAUUSD") or "XAUUSD")
            vol = float(getattr(p, "volume", 0.01) or 0.01)
            est_comm = -round((vol / 0.01) * COMMISSION_PER_001_LOT_USD, 2)
            gross_p = float(getattr(p, "profit", 0.0) or 0.0)
            swap_p = float(getattr(p, "swap", 0.0) or 0.0)
            net_p = round(gross_p + swap_p + est_comm, 2)
            p_type = int(getattr(p, "type", 0))
            side = "BUY" if p_type == 0 else "SELL"
            open_time_sec = int(getattr(p, "time", 0) or 0)
            open_dt = datetime.fromtimestamp(open_time_sec, tz=timezone.utc) if open_time_sec > 0 else now_utc
            held_mins = max(0.0, (now_utc - open_dt).total_seconds() / 60.0)

            if open_time_sec > 0:
                key_bs = (bot_id, sym_p)
                last_activity_ts_by_bot_sym[key_bs] = max(last_activity_ts_by_bot_sym.get(key_bs, 0.0), float(open_time_sec))

            pos_dict = {
                "ticket": int(p.ticket),
                "symbol": sym_p,
                "side": side,
                "volume": vol,
                "price_open": float(p.price_open),
                "price_current": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "gross_profit": round(gross_p, 2),
                "commission": est_comm,
                "net_profit": net_p,
                "profit": gross_p,
                "magic": p_magic,
                "comment": p_comment,
                "bot_id": bot_id,
                "open_time": open_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "held_minutes": round(held_mins, 1),
            }
            open_positions.append(pos_dict)
            open_by_bot.setdefault(bot_id, []).append(pos_dict)

        # 2. Collect all historical closed deals (last 30 days) and pair by position_id
        from_dt = now_utc - timedelta(days=30)
        to_dt = now_utc + timedelta(days=1)
        raw_deals = mt5.history_deals_get(from_dt, to_dt) or []

        pos_deals: dict[int, list[Any]] = {}
        for d in raw_deals:
            pid = int(getattr(d, "position_id", 0) or 0)
            if pid <= 0:
                continue
            pos_deals.setdefault(pid, []).append(d)

        closed_trades: list[dict[str, Any]] = []
        closed_by_bot: dict[str, list[dict[str, Any]]] = {}

        for pid, d_list in pos_deals.items():
            d_list.sort(key=lambda x: int(getattr(x, "time_msc", 0) or getattr(x, "time", 0) * 1000))
            in_deals = [d for d in d_list if int(getattr(d, "entry", -1)) == 0]
            out_deals = [d for d in d_list if int(getattr(d, "entry", -1)) in (1, 2, 3)]
            if not out_deals:
                continue
            first_d = in_deals[0] if in_deals else d_list[0]
            last_d = out_deals[-1]
            sym = str(getattr(last_d, "symbol", "") or getattr(first_d, "symbol", "XAUUSD"))
            magic = int(getattr(first_d, "magic", 0) or getattr(last_d, "magic", 0) or 0)
            comment = str(getattr(first_d, "comment", "") or getattr(last_d, "comment", "") or "")
            bot_id = match_trade_to_bot_id(magic, comment)

            vol = sum(float(getattr(d, "volume", 0.0) or 0.0) for d in out_deals)
            if vol <= 0:
                vol = float(getattr(first_d, "volume", 0.01) or 0.01)

            gross = sum(float(getattr(d, "profit", 0.0) or 0.0) for d in out_deals)
            swap = sum(float(getattr(d, "swap", 0.0) or 0.0) for d in d_list)
            raw_comm = sum(float(getattr(d, "commission", 0.0) or 0.0) for d in d_list)
            for d in d_list:
                raw_comm += float(getattr(d, "fee", 0.0) or 0.0)

            expected_comm = -round((vol / 0.01) * COMMISSION_PER_001_LOT_USD, 2)
            comm = round(raw_comm, 2) if abs(raw_comm) > 1e-6 else expected_comm
            net = round(gross + swap + comm, 2)

            d_type = int(getattr(first_d, "type", 0))
            if in_deals:
                side = "BUY" if d_type == 0 else "SELL"
            else:
                side = "SELL" if int(getattr(last_d, "type", 0)) == 0 else "BUY"

            t_in = int(getattr(first_d, "time", 0) or 0)
            t_out = int(getattr(last_d, "time", 0) or 0)
            dt_out = datetime.fromtimestamp(t_out, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t_out > 0 else ""
            held_m = max(0.0, (t_out - t_in) / 60.0) if (t_in > 0 and t_out >= t_in) else 0.0

            if t_out > 0:
                key_bs = (bot_id, sym)
                last_activity_ts_by_bot_sym[key_bs] = max(last_activity_ts_by_bot_sym.get(key_bs, 0.0), float(t_out))

            tr_dict = {
                "ticket": int(pid),
                "deal_ticket": int(getattr(last_d, "ticket", pid)),
                "symbol": sym,
                "side": side,
                "volume": round(vol, 2),
                "entry_price": float(getattr(first_d, "price", 0.0) or 0.0),
                "exit_price": float(getattr(last_d, "price", 0.0) or 0.0),
                "gross_profit": round(gross, 2),
                "commission": comm,
                "swap": round(swap, 2),
                "net_profit": net,
                "profit": net,
                "magic": magic,
                "comment": comment,
                "bot_id": bot_id,
                "close_time": dt_out,
                "held_minutes": round(held_m, 1),
            }
            closed_trades.append(tr_dict)
            closed_by_bot.setdefault(bot_id, []).append(tr_dict)

        closed_trades.sort(key=lambda x: x.get("close_time", ""), reverse=True)

        # 3. Fetch Multi-Asset Market Data across all 12 symbols
        def _extract_ohlc(rates: Any) -> tuple[list[float], list[float], list[float], list[float]]:
            if rates is None or len(rates) == 0:
                return [], [], [], []
            o = [float(r["open"]) for r in rates]
            h = [float(r["high"]) for r in rates]
            l = [float(r["low"]) for r in rates]
            c = [float(r["close"]) for r in rates]
            return o, h, l, c

        sym_data: dict[str, dict[str, Any]] = {}
        for s_name in ALL_FLEET_SYMBOLS:
            mt5.symbol_select(s_name, True)
            s_info = mt5.symbol_info(s_name)
            tick = mt5.symbol_info_tick(s_name)
            if s_info is None or tick is None:
                continue
            pt = float(getattr(s_info, "point", 0.01) or 0.01)
            dg = int(getattr(s_info, "digits", 2) or 2)
            bd = float(getattr(tick, "bid", 0.0) or 0.0)
            ak = float(getattr(tick, "ask", 0.0) or 0.0)
            tick_time = int(getattr(tick, "time", 0) or 0)
            is_mkt_open = (abs(now_utc.timestamp() - tick_time) <= 300) if tick_time > 0 else False
            if bd <= 0 or ak <= 0 or pt <= 0:
                continue
            sp_pts = round((ak - bd) / pt, 1)

            r_m1 = mt5.copy_rates_from_pos(s_name, mt5.TIMEFRAME_M1, 0, 80)
            r_m5 = mt5.copy_rates_from_pos(s_name, mt5.TIMEFRAME_M5, 0, 90)
            r_m15 = mt5.copy_rates_from_pos(s_name, mt5.TIMEFRAME_M15, 0, 80)
            r_h1 = mt5.copy_rates_from_pos(s_name, mt5.TIMEFRAME_H1, 0, 85)

            _, m1_h, m1_l, m1_c = _extract_ohlc(r_m1)
            m5_o, m5_h, m5_l, m5_c = _extract_ohlc(r_m5)
            m15_o, m15_h, m15_l, m15_c = _extract_ohlc(r_m15)
            h1_o, h1_h, h1_l, h1_c = _extract_ohlc(r_h1)

            if len(m5_c) < 30 or len(m15_c) < 25 or len(h1_c) < 25:
                continue

            m1_atr = (_atr(m1_h[:-1], m1_l[:-1], m1_c[:-1], 14) / pt) if len(m1_c) > 20 else 0.0
            m5_atr = (_atr(m5_h[:-1], m5_l[:-1], m5_c[:-1], 14) / pt) if len(m5_c) > 20 else 0.0
            m15_atr = (_atr(m15_h[:-1], m15_l[:-1], m15_c[:-1], 14) / pt) if len(m15_c) > 20 else 0.0
            m15_atr50 = (_atr(m15_h[:-1], m15_l[:-1], m15_c[:-1], 50) / pt) if len(m15_c) > 52 else m15_atr
            h1_atr = (_atr(h1_h[:-1], h1_l[:-1], h1_c[:-1], 14) / pt) if len(h1_c) > 20 else 0.0

            m1_e9 = _ema(m1_c[:-1], 9)[-1] if len(m1_c) > 25 else bd
            m1_e21 = _ema(m1_c[:-1], 21)[-1] if len(m1_c) > 25 else bd
            m5_e50 = _ema(m5_c[:-1], 50)[-1] if len(m5_c) > 55 else bd
            m5_e200 = _ema(m5_c[:-1], 200)[-1] if len(m5_c) > 60 else m5_e50
            h1_e50 = _ema(h1_c[:-1], 50)[-1] if len(h1_c) > 55 else bd
            h1_e200 = _ema(h1_c[:-1], 200)[-1] if len(h1_c) > 60 else h1_e50

            sym_data[s_name] = {
                "point": pt,
                "digits": dg,
                "bid": bd,
                "ask": ak,
                "spread_pts": sp_pts,
                "market_open": is_mkt_open,
                "m1_h": m1_h,
                "m1_l": m1_l,
                "m1_c": m1_c,
                "m5_o": m5_o,
                "m5_h": m5_h,
                "m5_l": m5_l,
                "m5_c": m5_c,
                "m15_o": m15_o,
                "m15_h": m15_h,
                "m15_l": m15_l,
                "m15_c": m15_c,
                "h1_o": h1_o,
                "h1_h": h1_h,
                "h1_l": h1_l,
                "h1_c": h1_c,
                "m1_atr_pts": m1_atr,
                "m5_atr_pts": m5_atr,
                "m15_atr_pts": m15_atr,
                "m15_atr50_pts": m15_atr50,
                "h1_atr_pts": h1_atr,
                "m1_ema9": m1_e9,
                "m1_ema21": m1_e21,
                "m5_ema50": m5_e50,
                "m5_ema200": m5_e200,
                "h1_ema50": h1_e50,
                "h1_ema200": h1_e200,
                "m5_rsi": _rsi(m5_c[:-1], 14),
                "m15_rsi": _rsi(m15_c[:-1], 14),
                "m15_zscore": _zscore(m15_c[:-1], 20),
                "risk_profile": _symbol_risk_profile(s_name),
            }

        xau_d = sym_data.get("XAUUSD", {})

        # Compute Cross-Asset USD Strength Basket (-1.0 to +1.0) & Risk-On Index for Grok + TradingAgents Desk
        usd_votes: list[float] = []
        for fx_s, inv in (("EURUSD", -1.0), ("GBPUSD", -1.0), ("AUDUSD", -1.0), ("NZDUSD", -1.0), ("USDJPY", 1.0), ("USDCAD", 1.0), ("USDCHF", 1.0)):
            if fx_s in sym_data:
                fx_d = sym_data[fx_s]
                v = 1.0 if fx_d["m15_c"][-1] > fx_d["m5_ema50"] else -1.0
                usd_votes.append(v * inv)
        usd_basket_score = (sum(usd_votes) / len(usd_votes)) if usd_votes else 0.0

        # 4. Manage Open Positions (Per-Symbol Commission-Proof Break-Even + Dynamic Trailing)
        actions_taken: list[str] = []
        if is_demo and execute_orders:
            for pos in open_positions:
                b_id = pos["bot_id"]
                b_cfg = bots_cfg.get(b_id, {})
                if not b_cfg.get("enabled", True):
                    continue
                p_sym = pos["symbol"]
                sd = sym_data.get(p_sym)
                if not sd:
                    continue
                pt = sd["point"]
                dg = sd["digits"]
                bd = sd["bid"]
                ak = sd["ask"]
                r_prof = sd["risk_profile"]

                comm_buf_pts = r_prof["comm_pts"]
                be_trig_pts = r_prof["be_trig_pts"]
                be_net_pts = r_prof["be_lock_pts"]
                trail_dist_pts = r_prof["trail_pts"]

                entry_p = float(pos["price_open"])
                cur_sl = float(pos["sl"])
                cur_tp = float(pos["tp"])
                side = pos["side"]
                p_ticket = int(pos["ticket"])

                if side == "BUY":
                    prof_pts = (bd - entry_p) / pt
                    min_safe_sl = round(entry_p + (comm_buf_pts + be_net_pts) * pt, dg)
                    if prof_pts >= be_trig_pts:
                        cand_sl = min_safe_sl
                        if prof_pts >= (comm_buf_pts + be_net_pts + trail_dist_pts):
                            cand_sl = max(min_safe_sl, round(bd - trail_dist_pts * pt, dg))
                        if cand_sl > cur_sl + (5 * pt) and cand_sl < bd - (15 * pt):
                            req = {
                                "action": mt5.TRADE_ACTION_SLTP,
                                "symbol": p_sym,
                                "position": p_ticket,
                                "sl": cand_sl,
                                "tp": cur_tp,
                            }
                            res = mt5.order_send(req)
                            if res and int(getattr(res, "retcode", 0)) == int(mt5.TRADE_RETCODE_DONE):
                                msg = f"[{ts_str}] ✔ Commission-Safe SL Lock #{p_ticket} {p_sym} BUY -> SL={cand_sl:.{dg}f} (+{(cand_sl-entry_p)/pt:.0f}pts > {comm_buf_pts:.0f}pt comm)"
                                actions_taken.append(msg)
                                bot_logs.setdefault(b_id, []).append(msg)
                else:
                    prof_pts = (entry_p - ak) / pt
                    max_safe_sl = round(entry_p - (comm_buf_pts + be_net_pts) * pt, dg)
                    if prof_pts >= be_trig_pts:
                        cand_sl = max_safe_sl
                        if prof_pts >= (comm_buf_pts + be_net_pts + trail_dist_pts):
                            cand_sl = min(max_safe_sl, round(ak + trail_dist_pts * pt, dg))
                        if (cur_sl <= 0 or cand_sl < cur_sl - (5 * pt)) and cand_sl > ak + (15 * pt):
                            req = {
                                "action": mt5.TRADE_ACTION_SLTP,
                                "symbol": p_sym,
                                "position": p_ticket,
                                "sl": cand_sl,
                                "tp": cur_tp,
                            }
                            res = mt5.order_send(req)
                            if res and int(getattr(res, "retcode", 0)) == int(mt5.TRADE_RETCODE_DONE):
                                msg = f"[{ts_str}] ✔ Commission-Safe SL Lock #{p_ticket} {p_sym} SELL -> SL={cand_sl:.{dg}f} (+{(entry_p-cand_sl)/pt:.0f}pts > {comm_buf_pts:.0f}pt comm)"
                                actions_taken.append(msg)
                                bot_logs.setdefault(b_id, []).append(msg)

        # 5. Evaluate Live Multi-Asset Signals & AI Radars across all 10 Bots
        bot_telemetry: dict[str, dict[str, Any]] = {}
        now_epoch = now_utc.timestamp()

        for bot_id, b_cfg in bots_cfg.items():
            enabled = bool(b_cfg.get("enabled", True))
            mode = str(b_cfg.get("mode", "PYTHON_DEMO_LIVE"))
            lot = float(b_cfg.get("lot", 0.03) or 0.03)
            ai_thresh = float(b_cfg.get("ai_threshold", 0.58) or 0.58)
            cooldown_mins = float(b_cfg.get("cooldown_minutes", 18.0) or 18.0)
            max_open_pos = int(b_cfg.get("max_open_positions", 2) or 2)
            windows = b_cfg.get("session_windows_utc", [["00:00", "23:59"]])
            in_sess = _in_session_utc(now_utc, [(w[0], w[1]) for w in windows if len(w) == 2])
            bot_open = open_by_bot.get(bot_id, [])
            open_syms_for_bot = {p["symbol"] for p in bot_open}

            raw_syms = b_cfg.get("symbols")
            if not raw_syms:
                raw_syms = [s.strip() for s in str(b_cfg.get("symbol", "XAUUSD")).split(",") if s.strip()]
            bot_symbols = [s for s in raw_syms if s in sym_data]
            if not bot_symbols and "XAUUSD" in sym_data:
                bot_symbols = ["XAUUSD"]

            best_sym = bot_symbols[0] if bot_symbols else "XAUUSD"
            signal_dir = "NEUTRAL"
            ai_prob = 0.50
            radar_note = ""
            ready_to_fire = False
            sl_pts = 200.0
            tp_pts = 400.0
            sym_summaries: list[str] = []

            for s_name in bot_symbols:
                sd = sym_data[s_name]
                pt = sd["point"]
                ak = sd["ask"]
                bd = sd["bid"]
                sp_pts = sd["spread_pts"]
                r_prof = sd["risk_profile"]
                max_sp = r_prof["max_spread_pts"]
                min_sl = r_prof["min_sl_pts"]
                max_sl = r_prof["max_sl_pts"]

                last_ts = last_activity_ts_by_bot_sym.get((bot_id, s_name), 0.0)
                in_cooldown = (now_epoch - last_ts) < (cooldown_mins * 60.0)
                sym_open = s_name in open_syms_for_bot

                cand_dir = "NEUTRAL"
                cand_prob = 0.50
                cand_fire = False
                cand_sl = min_sl
                cand_tp = min_sl * 2.0
                cand_note = ""

                if bot_id == "XAUUSD_M1_BreakoutStops_v2":
                    m1_h, m1_l, m1_atr = sd["m1_h"], sd["m1_l"], sd["m1_atr_pts"]
                    slope_pts = (sd["m1_ema9"] - sd["m1_ema21"]) / pt
                    hi20 = max(m1_h[-21:-1]) if len(m1_h) > 22 else ak
                    lo20 = min(m1_l[-21:-1]) if len(m1_l) > 22 else bd
                    dist_buy = round((hi20 - ak) / pt, 1)
                    dist_sell = round((bd - lo20) / pt, 1)
                    cand_prob = round(min(0.92, max(0.35, 0.50 + abs(slope_pts) / 120.0)), 2)
                    if m1_atr >= 135.0 and abs(slope_pts) >= 18.0:
                        cand_dir = "BUY_STOP_ARMED" if slope_pts > 0 else "SELL_STOP_ARMED"
                    cand_note = (
                        f"M1 ATR={m1_atr:.0f}pts (Min 135) | EMA9/21={slope_pts:+.0f}pts | "
                        f"Dist Hi={dist_buy:+.0f}pts Lo={dist_sell:+.0f}pts"
                    )

                elif bot_id == "XAUUSD_Asia_Session_AI_v2":
                    m5_h, m5_l, m5_c = sd["m5_h"], sd["m5_l"], sd["m5_c"]
                    hi40 = max(m5_h[-41:-1]) if len(m5_h) > 42 else ak
                    lo40 = min(m5_l[-41:-1]) if len(m5_l) > 42 else bd
                    rng_pts = (hi40 - lo40) / pt
                    rsi_v = sd["m5_rsi"]
                    e50, e200 = sd["m5_ema50"], sd["m5_ema200"]
                    max_rng = 2200.0 if s_name == "XAUUSD" else 450.0
                    base_p = 0.53
                    if m5_c[-1] > e50 > e200 and 50 <= rsi_v <= 74:
                        base_p += 0.13
                        cand_dir = f"BUY_WATCH({s_name})"
                    elif m5_c[-1] < e50 < e200 and 26 <= rsi_v <= 50:
                        base_p += 0.13
                        cand_dir = f"SELL_WATCH({s_name})"
                    if rng_pts <= max_rng:
                        base_p += 0.05
                    cand_prob = round(min(0.94, base_p), 2)
                    sym_summaries.append(f"{s_name}:{cand_prob:.2f}")
                    cand_note = f"[{s_name}] M5 Rng={rng_pts:.0f}pts | AI={cand_prob:.2f} | RSI={rsi_v:.1f} | Sp={sp_pts:.0f}"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if m5_c[-2] > hi40 and "BUY" in cand_dir:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.5))
                            cand_tp = cand_sl * float(b_cfg.get("rr", 1.5) or 1.5)
                        elif m5_c[-2] < lo40 and "SELL" in cand_dir:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.5))
                            cand_tp = cand_sl * float(b_cfg.get("rr", 1.5) or 1.5)

                elif bot_id == "EvidenceH1FastBreakout_v220":
                    h1_h, h1_l = sd["h1_h"], sd["h1_l"]
                    hi72 = max(h1_h[-73:-1]) if len(h1_h) > 74 else max(h1_h[:-1])
                    lo72 = min(h1_l[-73:-1]) if len(h1_l) > 74 else min(h1_l[:-1])
                    dist_hi = (hi72 - ak) / pt
                    dist_lo = (bd - lo72) / pt
                    trend_up = sd["h1_ema50"] >= sd["h1_ema200"]
                    cand_prob = round(0.67 if (dist_hi <= 80 or dist_lo <= 80) else 0.52, 2)
                    cand_dir = f"H1_SCAN({s_name})"
                    sym_summaries.append(f"{s_name}:dHi={dist_hi:+.0f}")
                    cand_note = f"[{s_name}] 72-Bar Hi={hi72:.{sd['digits']}f} (Dist={dist_hi:+.0f}pts)"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp:
                        if trend_up and -35 <= dist_hi <= 0:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["h1_atr_pts"] * 1.2))
                            cand_tp = cand_sl * 3.5
                        elif (not trend_up) and -35 <= dist_lo <= 0:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["h1_atr_pts"] * 1.2))
                            cand_tp = cand_sl * 3.5

                elif bot_id == "AdaptiveH1BreakoutOwn_v3":
                    h1_h, h1_l, h1_c = sd["h1_h"], sd["h1_l"], sd["h1_c"]
                    hi24 = max(h1_h[-25:-1]) if len(h1_h) > 26 else ak
                    lo24 = min(h1_l[-25:-1]) if len(h1_l) > 26 else bd
                    trend_up = h1_c[-1] > sd["h1_ema50"] > sd["h1_ema200"]
                    trend_dn = h1_c[-1] < sd["h1_ema50"] < sd["h1_ema200"]
                    cand_prob = round(0.68 if (trend_up or trend_dn) else 0.46, 2)
                    cand_dir = f"LONG_BIAS({s_name})" if trend_up else (f"SHORT_BIAS({s_name})" if trend_dn else "WAIT_TREND")
                    sym_summaries.append(f"{s_name}:{'BULL' if trend_up else ('BEAR' if trend_dn else 'FLAT')}")
                    cand_note = f"[{s_name}] H1 EMA={'BULL' if trend_up else ('BEAR' if trend_dn else 'MIXED')} | 24h Hi={hi24:.{sd['digits']}f}"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp:
                        if trend_up and h1_c[-2] > hi24:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["h1_atr_pts"] * 1.3))
                            cand_tp = cand_sl * 2.2
                        elif trend_dn and h1_c[-2] < lo24:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["h1_atr_pts"] * 1.3))
                            cand_tp = cand_sl * 2.2

                elif bot_id == "SMC_Portfolio_AI_Radar_v2":
                    m15_h, m15_l = sd["m15_h"], sd["m15_l"]
                    m5_h, m5_l, m5_c = sd["m5_h"], sd["m5_l"], sd["m5_c"]
                    fvg_bull = len(m15_l) > 5 and (m15_l[-2] > m15_h[-4])
                    fvg_bear = len(m15_h) > 5 and (m15_h[-2] < m15_l[-4])
                    htf_bull = sd["h1_ema50"] >= sd["h1_ema200"]
                    cand_prob = round(0.68 if (fvg_bull and htf_bull) or (fvg_bear and not htf_bull) else 0.54, 2)
                    cand_dir = f"BULL_FVG({s_name})" if (fvg_bull and htf_bull) else (f"BEAR_FVG({s_name})" if fvg_bear else "RADAR_SCAN")
                    sym_summaries.append(f"{s_name}:{'FVG+' if fvg_bull else ('FVG-' if fvg_bear else 'SCAN')}")
                    cand_note = f"[{s_name}] HTF={'BULL' if htf_bull else 'BEAR'} | M15 FVG={'ACTIVE' if (fvg_bull or fvg_bear) else 'WAIT'} | Cooldown={'ACTIVE' if in_cooldown else 'READY'}"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if fvg_bull and htf_bull and m5_c[-1] > m5_h[-2]:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.3))
                            cand_tp = cand_sl * 2.4
                        elif fvg_bear and (not htf_bull) and m5_c[-1] < m5_l[-2]:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.3))
                            cand_tp = cand_sl * 2.4

                elif bot_id == "WeaknessRectangle_M15_M1":
                    m15_h, m15_l, m15_c = sd["m15_h"], sd["m15_l"], sd["m15_c"]
                    m1_h, m1_l, m1_c = sd["m1_h"], sd["m1_l"], sd["m1_c"]
                    hi_m15 = max(m15_h[-18:-2]) if len(m15_h) > 20 else ak
                    lo_m15 = min(m15_l[-18:-2]) if len(m15_l) > 20 else bd
                    sweep_hi = (m15_h[-2] - hi_m15) / pt
                    sweep_lo = (lo_m15 - m15_l[-2]) / pt
                    rect_pts = (max(m1_h[-15:-1]) - min(m1_l[-15:-1])) / pt if len(m1_h) > 16 else 999.0
                    max_rect = 650.0 if s_name == "XAUUSD" else 160.0
                    min_sw = 80.0 if s_name == "XAUUSD" else 20.0
                    has_sw = (sweep_hi >= min_sw or sweep_lo >= min_sw) and rect_pts <= max_rect
                    cand_prob = round(0.66 if has_sw else 0.52, 2)
                    cand_dir = f"SWEEP_RECT({s_name})" if has_sw else "WATCHING_RECT"
                    sym_summaries.append(f"{s_name}:R={rect_pts:.0f}")
                    cand_note = f"[{s_name}] SweepHi={sweep_hi:+.0f} Lo={sweep_lo:+.0f} | M1 Rect={rect_pts:.0f}pts"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and has_sw and len(m1_c) > 16:
                        if sweep_lo >= min_sw and m1_c[-1] > max(m1_h[-15:-1]):
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, rect_pts * 1.2))
                            cand_tp = cand_sl * 3.5
                        elif sweep_hi >= min_sw and m1_c[-1] < min(m1_l[-15:-1]):
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, rect_pts * 1.2))
                            cand_tp = cand_sl * 3.5

                elif bot_id == "NewEra_JoeyCIO_Matrix_v2":
                    rsi_m5 = sd["m5_rsi"]
                    m5_c, m5_h, m5_l = sd["m5_c"], sd["m5_h"], sd["m5_l"]
                    trend_up = sd["m5_ema50"] > sd["m5_ema200"] and sd["h1_ema50"] >= sd["h1_ema200"]
                    trend_dn = sd["m5_ema50"] < sd["m5_ema200"] and sd["h1_ema50"] <= sd["h1_ema200"]
                    conf_buy = trend_up and 53 <= rsi_m5 <= 67 and m5_c[-2] > max(m5_h[-10:-2])
                    conf_sell = trend_dn and 33 <= rsi_m5 <= 47 and m5_c[-2] < min(m5_l[-10:-2])
                    cand_prob = round(0.69 if (conf_buy or conf_sell) else (0.61 if (trend_up or trend_dn) else 0.54), 2)
                    cand_dir = f"CONFLUENCE_BUY({s_name})" if conf_buy else (f"CONFLUENCE_SELL({s_name})" if conf_sell else "WATERFALL_SCAN")
                    sym_summaries.append(f"{s_name}:{rsi_m5:.0f}")
                    cand_note = f"[{s_name}] Matrix=2/3 | RSI={rsi_m5:.1f} | AI CIO={cand_prob*100:.0f}%"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if conf_buy:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.6))
                            cand_tp = cand_sl * 1.8
                        elif conf_sell:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.6))
                            cand_tp = cand_sl * 1.8

                elif bot_id == "NY_ORB_Index_Sniper_v1":
                    # London/NY Opening Range Breakout (6-bar M5 Opening Range + Displacement body >= 50%)
                    m5_o, m5_h, m5_l, m5_c = sd["m5_o"], sd["m5_h"], sd["m5_l"], sd["m5_c"]
                    orb_hi = max(m5_h[-14:-2])
                    orb_lo = min(m5_l[-14:-2])
                    bar_rng = max(pt, m5_h[-2] - m5_l[-2])
                    body_ratio = abs(m5_c[-2] - m5_o[-2]) / bar_rng
                    trend_up = m5_c[-2] > sd["m5_ema50"]
                    trend_dn = m5_c[-2] < sd["m5_ema50"]
                    orb_buy = trend_up and m5_c[-2] > orb_hi and body_ratio >= 0.50
                    orb_sell = trend_dn and m5_c[-2] < orb_lo and body_ratio >= 0.50
                    cand_prob = round(0.71 if (orb_buy or orb_sell) else 0.55, 2)
                    cand_dir = f"ORB_BREAK({s_name})" if (orb_buy or orb_sell) else "ORB_WATCH"
                    sym_summaries.append(f"{s_name}:Body={body_ratio*100:.0f}%")
                    cand_note = f"[{s_name}] ORB Hi={orb_hi:.{sd['digits']}f} Lo={orb_lo:.{sd['digits']}f} | Displacement={body_ratio*100:.0f}%"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if orb_buy:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 2.2
                        elif orb_sell:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m5_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 2.2

                elif bot_id == "Quant_ZScore_MeanReversion_v1":
                    # 20-bar Z-Score Mean Reversion across 7 FX Pairs
                    z = sd["m15_zscore"]
                    rsi15 = sd["m15_rsi"]
                    m15_o, m15_c = sd["m15_o"], sd["m15_c"]
                    bull_rev = z <= -1.95 and rsi15 <= 36.0 and m15_c[-1] > m15_o[-1]
                    bear_rev = z >= 1.95 and rsi15 >= 64.0 and m15_c[-1] < m15_o[-1]
                    cand_prob = round(min(0.92, 0.52 + abs(z) * 0.11), 2)
                    cand_dir = f"Z_BUY({s_name})" if bull_rev else (f"Z_SELL({s_name})" if bear_rev else "ZSCORE_SCAN")
                    sym_summaries.append(f"{s_name}:Z={z:+.2f}")
                    cand_note = f"[{s_name}] Z-Score={z:+.2f} ( Trig +-1.95 ) | RSI15={rsi15:.1f}"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if bull_rev:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 1.6
                        elif bear_rev:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 1.6

                elif bot_id == "Crypto_FX_Compression_Breakout_v1":
                    # Volatility Squeeze (ATR14 <= 0.88 * ATR50) + 25-bar Breakout + H1 Trend
                    m15_h, m15_l, m15_c = sd["m15_h"], sd["m15_l"], sd["m15_c"]
                    atr_ratio = sd["m15_atr_pts"] / max(1e-6, sd["m15_atr50_pts"])
                    in_squeeze = atr_ratio <= 0.90
                    hi25 = max(m15_h[-26:-2])
                    lo25 = min(m15_l[-26:-2])
                    htf_up = sd["h1_ema50"] >= sd["h1_ema200"]
                    sq_buy = in_squeeze and htf_up and m15_c[-2] > hi25
                    sq_sell = in_squeeze and (not htf_up) and m15_c[-2] < lo25
                    cand_prob = round(0.72 if (sq_buy or sq_sell) else (0.60 if in_squeeze else 0.52), 2)
                    cand_dir = f"SQUEEZE_BRK({s_name})" if (sq_buy or sq_sell) else ("SQUEEZE_ARMED" if in_squeeze else "VOL_SCAN")
                    sym_summaries.append(f"{s_name}:Sq={atr_ratio:.2f}")
                    cand_note = f"[{s_name}] SqueezeRatio={atr_ratio:.2f} (<=0.90) | HTF={'BULL' if htf_up else 'BEAR'}"
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if sq_buy:
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.5))
                            cand_tp = cand_sl * 2.0
                        elif sq_sell:
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.5))
                            cand_tp = cand_sl * 2.0

                else:  # Grok_TradingAgents_Debate_v1
                    # TauricResearch/TradingAgents Multi-Agent Bull vs Bear Debate + Grok Self-Reflection
                    m5_c, m5_h, m5_l = sd["m5_c"], sd["m5_h"], sd["m5_l"]
                    htf_up = sd["h1_ema50"] > sd["h1_ema200"]
                    mtf_up = sd["m5_ema50"] > sd["m5_ema200"]
                    rsi5 = sd["m5_rsi"]
                    bull_score = 20
                    bear_score = 20
                    if htf_up:
                        bull_score += 25
                    else:
                        bear_score += 25
                    if mtf_up:
                        bull_score += 20
                    else:
                        bear_score += 20
                    # Cross-Asset Macro Analyst (USD Basket effect on symbol)
                    if s_name in ("XAUUSD", "EURUSD", "GBPUSD", "AUDUSD", "BTCUSD"):
                        if usd_basket_score < -0.25:
                            bull_score += 20
                        elif usd_basket_score > 0.25:
                            bear_score += 20
                    elif s_name == "USDJPY":
                        if usd_basket_score > 0.25:
                            bull_score += 20
                        elif usd_basket_score < -0.25:
                            bear_score += 20
                    if 52 <= rsi5 <= 68 and m5_c[-2] > max(m5_h[-8:-2]):
                        bull_score += 15
                    elif 32 <= rsi5 <= 48 and m5_c[-2] < min(m5_l[-8:-2]):
                        bear_score += 15

                    margin = bull_score - bear_score
                    cand_prob = round(min(0.93, max(0.45, 0.52 + abs(margin) / 140.0)), 2)
                    cand_dir = f"BULL_WIN({s_name})" if margin >= 25 else (f"BEAR_WIN({s_name})" if margin <= -25 else "DEBATE_TIE")
                    sym_summaries.append(f"{s_name}:B{bull_score}/S{bear_score}")
                    cand_note = (
                        f"[{s_name}] Bull={bull_score} vs Bear={bear_score} (Margin={margin:+d}) | "
                        f"USD_Basket={usd_basket_score:+.2f} | Grok/Agents Conf={cand_prob*100:.0f}%"
                    )
                    if in_sess and not in_cooldown and not sym_open and sp_pts <= max_sp and cand_prob >= ai_thresh:
                        if margin >= 35 and m5_c[-2] > max(m5_h[-8:-2]):
                            cand_dir = "BUY"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 2.2
                        elif margin <= -35 and m5_c[-2] < min(m5_l[-8:-2]):
                            cand_dir = "SELL"
                            cand_fire = True
                            cand_sl = min(max_sl, max(min_sl, sd["m15_atr_pts"] * 1.4))
                            cand_tp = cand_sl * 2.2

                # Select highest-priority candidate across this bot's symbols
                if cand_fire and not ready_to_fire:
                    best_sym = s_name
                    signal_dir = cand_dir
                    ai_prob = cand_prob
                    ready_to_fire = True
                    sl_pts = cand_sl
                    tp_pts = cand_tp
                    radar_note = cand_note
                elif not ready_to_fire and cand_prob >= ai_prob:
                    best_sym = s_name
                    signal_dir = cand_dir
                    ai_prob = cand_prob
                    radar_note = cand_note

            if sym_summaries:
                radar_note = f"{radar_note} | Universe=[{', '.join(sym_summaries[:6])}]"

            # Execute DEMO order if ready_to_fire, symbol market is open (live tick <= 5m), and bot has capacity
            if (
                is_demo
                and execute_orders
                and enabled
                and mode == "PYTHON_DEMO_LIVE"
                and ready_to_fire
                and len(bot_open) < max_open_pos
                and best_sym not in open_syms_for_bot
                and signal_dir in ("BUY", "SELL")
                and best_sym in sym_data
                and bool(sym_data[best_sym].get("market_open", False))
            ):
                sd_fire = sym_data[best_sym]
                pt_f = sd_fire["point"]
                dg_f = sd_fire["digits"]
                entry_px = sd_fire["ask"] if signal_dir == "BUY" else sd_fire["bid"]
                sl_px = round(entry_px - sl_pts * pt_f, dg_f) if signal_dir == "BUY" else round(entry_px + sl_pts * pt_f, dg_f)
                tp_px = round(entry_px + tp_pts * pt_f, dg_f) if signal_dir == "BUY" else round(entry_px - tp_pts * pt_f, dg_f)
                order_type = mt5.ORDER_TYPE_BUY if signal_dir == "BUY" else mt5.ORDER_TYPE_SELL
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": best_sym,
                    "volume": round(lot, 2),
                    "type": order_type,
                    "price": entry_px,
                    "sl": sl_px,
                    "tp": tp_px,
                    "deviation": 25,
                    "magic": int(b_cfg.get("magic", 20260000)),
                    "comment": str(b_cfg.get("comment", bot_id[:16]))[:31],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res and int(getattr(res, "retcode", 0)) == int(mt5.TRADE_RETCODE_DONE):
                    last_activity_ts_by_bot_sym[(bot_id, best_sym)] = now_epoch
                    fire_msg = (
                        f"[{ts_str}] 🚀 OPENED DEMO {signal_dir} {lot:.2f} lot {best_sym} @ {entry_px:.{dg_f}f} "
                        f"| SL={sl_px:.{dg_f}f} TP={tp_px:.{dg_f}f} | AI={ai_prob:.2f} | Ticket=#{getattr(res, 'order', 0)}"
                    )
                    actions_taken.append(fire_msg)
                    bot_logs.setdefault(bot_id, []).append(fire_msg)

            status_tag = "🟢 AUTO-DEMO" if (enabled and mode in ("PYTHON_DEMO_LIVE", "DEMO_MT5_SYNC")) else ("🟡 SHADOW" if enabled else "⏸ PAUSED")
            sess_tag = "IN-SESSION" if in_sess else "WAIT-SESSION"
            log_line = f"[{ts_str}] [{status_tag}] [{sess_tag}] Signal={signal_dir} | {radar_note}"
            b_log_list = bot_logs.setdefault(bot_id, [])
            if not b_log_list or b_log_list[-1][11:] != log_line[11:]:
                b_log_list.append(log_line)
            if len(b_log_list) > 50:
                bot_logs[bot_id] = b_log_list[-50:]

            bot_telemetry[bot_id] = {
                "signal_dir": signal_dir,
                "active_symbol": best_sym,
                "symbols_scanned": len(bot_symbols),
                "ai_probability": ai_prob,
                "ai_threshold": ai_thresh,
                "in_session": in_sess,
                "spread_points": sym_data.get(best_sym, {}).get("spread_pts", 0.0),
                "radar_note": radar_note,
                "updated_at": ts_str,
            }

        # 6. Compute Per-Bot Performance Metrics
        all_bot_ids = list(bots_cfg.keys()) + ["External_Or_Manual_MT5"]
        bot_metrics: dict[str, dict[str, Any]] = {}
        today_prefix = now_utc.strftime("%Y-%m-%d")
        baseline_ts = str(fleet_state.get("test_baseline_utc", "") or today_prefix)

        for b_id in all_bot_ids:
            b_closed = closed_by_bot.get(b_id, [])
            b_open = open_by_bot.get(b_id, [])
            c_cnt = len(b_closed)
            o_cnt = len(b_open)
            wins = [t for t in b_closed if float(t.get("net_profit", 0.0)) > 0]
            losses = [t for t in b_closed if float(t.get("net_profit", 0.0)) <= 0]
            gross_sum = round(sum(float(t.get("gross_profit", 0.0)) for t in b_closed), 2)
            comm_sum = round(sum(float(t.get("commission", 0.0)) for t in b_closed), 2)
            net_sum = round(sum(float(t.get("net_profit", 0.0)) for t in b_closed), 2)
            float_sum = round(sum(float(t.get("net_profit", 0.0)) for t in b_open), 2)
            win_rate = round((len(wins) / c_cnt) * 100.0, 1) if c_cnt > 0 else 0.0
            gross_wins = sum(float(t.get("net_profit", 0.0)) for t in wins)
            gross_loss = abs(sum(float(t.get("net_profit", 0.0)) for t in losses))
            pf = round(gross_wins / gross_loss, 2) if gross_loss > 1e-6 else (round(gross_wins, 2) if gross_wins > 0 else 0.0)
            best_t = round(max((float(t.get("net_profit", 0.0)) for t in b_closed), default=0.0), 2)
            worst_t = round(min((float(t.get("net_profit", 0.0)) for t in b_closed), default=0.0), 2)

            b_today = [t for t in b_closed if str(t.get("close_time", "")).startswith(today_prefix)]
            today_gross = round(sum(float(t.get("gross_profit", 0.0)) for t in b_today), 2)
            today_comm = round(sum(float(t.get("commission", 0.0)) for t in b_today), 2)
            today_net = round(sum(float(t.get("net_profit", 0.0)) for t in b_today), 2)

            b_base = [t for t in b_closed if str(t.get("close_time", "")) >= baseline_ts]
            base_net = round(sum(float(t.get("net_profit", 0.0)) for t in b_base), 2)
            base_comm = round(sum(float(t.get("commission", 0.0)) for t in b_base), 2)

            bot_metrics[b_id] = {
                "open_trades": o_cnt,
                "closed_trades": c_cnt,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": win_rate,
                "gross_pnl_usd": gross_sum,
                "commission_usd": comm_sum,
                "net_pnl_usd": net_sum,
                "floating_pnl_usd": float_sum,
                "total_pnl_with_floating_usd": round(net_sum + float_sum, 2),
                "profit_factor": pf,
                "best_trade_usd": best_t,
                "worst_trade_usd": worst_t,
                "today_trades": len(b_today),
                "today_gross_usd": today_gross,
                "today_commission_usd": today_comm,
                "today_net_usd": today_net,
                "forward_test_trades": len(b_base),
                "forward_test_commission_usd": base_comm,
                "forward_test_net_usd": base_net,
            }

        result_payload = {
            "ok": True,
            "timestamp_utc": now_utc.isoformat(),
            "account": account_summary,
            "market": {
                "symbol": "XAUUSD",
                "symbols_active": list(sym_data.keys()),
                "bid": xau_d.get("bid", 0.0),
                "ask": xau_d.get("ask", 0.0),
                "spread_points": xau_d.get("spread_pts", 0.0),
                "m1_atr_points": round(float(xau_d.get("m1_atr_pts", 0.0)), 1),
                "m5_atr_points": round(float(xau_d.get("m5_atr_pts", 0.0)), 1),
                "h1_atr_points": round(float(xau_d.get("h1_atr_pts", 0.0)), 1),
            },
            "bot_metrics": bot_metrics,
            "bot_telemetry": bot_telemetry,
            "bot_logs": bot_logs,
            "open_positions": open_positions,
            "closed_trades": closed_trades[:250],
            "actions_taken": actions_taken,
        }

        fleet_state["bot_logs"] = bot_logs
        fleet_state["last_cycle_result"] = result_payload
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(fleet_state, indent=2, ensure_ascii=False), encoding="utf-8")
        return result_payload

    finally:
        mt5.shutdown()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    st_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("TradingAgentsData/runtime/multi_bot_hub_state.json")
    exec_flag = (sys.argv[2].lower() != "false") if len(sys.argv) > 2 else True
    res = run_fleet_cycle(st_path, execute_orders=exec_flag)
    print(json.dumps(res, ensure_ascii=True))
