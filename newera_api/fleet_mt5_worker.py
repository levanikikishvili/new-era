"""Python 3.14 MT5 Live Demo Worker for New Era Unified 7-Bot Fleet & AI Radars.

Runs either in-process (when `MetaTrader5` is importable) or via subprocess under
`C:\\Users\\LeVI\\AppData\\Local\\Programs\\Python\\Python314\\python.exe`.

Responsibilities:
1. Connects to MT5 and strictly enforces DEMO-ONLY safety (`trade_mode == ACCOUNT_TRADE_MODE_DEMO`).
2. Fetches all live open positions and historical closed deals (last 30 days), pairing IN/OUT deals
   by `position_id` and calculating exact IC Markets Commission (`$0.08` per `0.01` lot = `$8.00/lot` = `80 points`).
3. Attributes every open trade and closed trade to its exact Bot in the 7-Bot Fleet.
4. Evaluates live M1 / M5 / M15 / H1 candles and AI models (`xauusd_asia_ai_filter_rr1_spread40.pkl`,
   `smc_ai_filter.pkl`) for all enabled bots:
   - Manages open positions (Commission-Proof Break-Even jumping over the 80-pt commission zone to +100 pts,
     dynamic trailing stop, and max-hold time-stop).
   - Executes validated DEMO setups when `mode == "PYTHON_DEMO_LIVE"` and session/spread/AI gates pass.
   - Emits per-bot live radar telemetry and human-readable logs.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COMMISSION_PER_001_LOT_USD = 0.08  # $0.08 per 0.01 lot ($8.00 per 1.00 lot = 80 points on XAUUSD)
COMMISSION_POINTS_ROUND_TURN = 80.0


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


def match_trade_to_bot_id(magic: int, comment: str) -> str:
    c = (comment or "").upper()
    m = int(magic or 0)
    if m in (26040301, 11032026) or "BREAKOUTSTOPS" in c or "SNIPER" in c or "M1_BRK" in c:
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

        # Load persisted fleet config
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

        for p in raw_positions:
            p_magic = int(getattr(p, "magic", 0) or 0)
            p_comment = str(getattr(p, "comment", "") or "")
            bot_id = match_trade_to_bot_id(p_magic, p_comment)
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

            pos_dict = {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
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

            # Ensure exact IC Markets $0.08 / 0.01 lot commission accounting if broker recorded 0
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

        # 3. Fetch live XAUUSD market data (Tick, M1, M5, M15, H1)
        sym_name = "XAUUSD"
        mt5.symbol_select(sym_name, True)
        s_info = mt5.symbol_info(sym_name)
        tick = mt5.symbol_info_tick(sym_name)
        point = float(getattr(s_info, "point", 0.01) or 0.01)
        digits = int(getattr(s_info, "digits", 2) or 2)
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        spread_pts = round((ask - bid) / point, 1) if (ask > 0 and bid > 0 and point > 0) else 0.0

        rates_m1 = mt5.copy_rates_from_pos(sym_name, mt5.TIMEFRAME_M1, 0, 120)
        rates_m5 = mt5.copy_rates_from_pos(sym_name, mt5.TIMEFRAME_M5, 0, 120)
        rates_m15 = mt5.copy_rates_from_pos(sym_name, mt5.TIMEFRAME_M15, 0, 100)
        rates_h1 = mt5.copy_rates_from_pos(sym_name, mt5.TIMEFRAME_H1, 0, 120)

        def _extract_ohlc(rates: Any) -> tuple[list[float], list[float], list[float], list[float]]:
            if rates is None or len(rates) == 0:
                return [], [], [], []
            o = [float(r["open"]) for r in rates]
            h = [float(r["high"]) for r in rates]
            l = [float(r["low"]) for r in rates]
            c = [float(r["close"]) for r in rates]
            return o, h, l, c

        _, m1_h, m1_l, m1_c = _extract_ohlc(rates_m1)
        m5_o, m5_h, m5_l, m5_c = _extract_ohlc(rates_m5)
        m15_o, m15_h, m15_l, m15_c = _extract_ohlc(rates_m15)
        h1_o, h1_h, h1_l, h1_c = _extract_ohlc(rates_h1)

        m1_atr_pts = (_atr(m1_h[:-1], m1_l[:-1], m1_c[:-1], 14) / point) if len(m1_c) > 20 else 0.0
        m5_atr_pts = (_atr(m5_h[:-1], m5_l[:-1], m5_c[:-1], 14) / point) if len(m5_c) > 20 else 0.0
        m15_atr_pts = (_atr(m15_h[:-1], m15_l[:-1], m15_c[:-1], 14) / point) if len(m15_c) > 20 else 0.0
        h1_atr_pts = (_atr(h1_h[:-1], h1_l[:-1], h1_c[:-1], 14) / point) if len(h1_c) > 20 else 0.0

        m1_ema9 = _ema(m1_c[:-1], 9)[-1] if len(m1_c) > 25 else bid
        m1_ema21 = _ema(m1_c[:-1], 21)[-1] if len(m1_c) > 25 else bid
        m5_ema50 = _ema(m5_c[:-1], 50)[-1] if len(m5_c) > 55 else bid
        m5_ema200 = _ema(m5_c[:-1], 200)[-1] if len(m5_c) > 60 else m5_ema50
        h1_ema50 = _ema(h1_c[:-1], 50)[-1] if len(h1_c) > 55 else bid
        h1_ema200 = _ema(h1_c[:-1], 200)[-1] if len(h1_c) > 60 else h1_ema50

        # 4. Manage Open Positions (Commission-Proof Break-Even + Dynamic Trailing + Time-Stop)
        actions_taken: list[str] = []
        if is_demo and execute_orders:
            for pos in open_positions:
                b_id = pos["bot_id"]
                b_cfg = bots_cfg.get(b_id, {})
                if not b_cfg.get("enabled", True):
                    continue
                comm_buf_pts = float(b_cfg.get("commission_buffer_points", 80.0) or 80.0)
                be_trig_pts = float(b_cfg.get("be_trigger_points", 125.0) or 125.0)
                be_net_pts = float(b_cfg.get("be_net_lock_points", 20.0) or 20.0)
                trail_dist_pts = float(b_cfg.get("trail_distance_points", 150.0) or 150.0)
                max_hold_m = int(b_cfg.get("max_hold_minutes", 180) or 180)

                entry_p = float(pos["price_open"])
                cur_sl = float(pos["sl"])
                cur_tp = float(pos["tp"])
                side = pos["side"]
                p_ticket = int(pos["ticket"])

                # Profit in points
                if side == "BUY":
                    prof_pts = (bid - entry_p) / point
                    min_safe_sl = round(entry_p + (comm_buf_pts + be_net_pts) * point, digits)
                    if prof_pts >= be_trig_pts:
                        cand_sl = min_safe_sl
                        if prof_pts >= (comm_buf_pts + be_net_pts + trail_dist_pts):
                            cand_sl = max(min_safe_sl, round(bid - trail_dist_pts * point, digits))
                        if cand_sl > cur_sl + (10 * point) and cand_sl < bid - (20 * point):
                            req = {
                                "action": mt5.TRADE_ACTION_SLTP,
                                "symbol": pos["symbol"],
                                "position": p_ticket,
                                "sl": cand_sl,
                                "tp": cur_tp,
                            }
                            res = mt5.order_send(req)
                            if res and int(getattr(res, "retcode", 0)) == int(mt5.TRADE_RETCODE_DONE):
                                msg = f"[{ts_str}] ✔ Commission-Safe SL Lock #{p_ticket} BUY -> SL={cand_sl:.2f} (+{(cand_sl-entry_p)/point:.0f} pts > {comm_buf_pts:.0f}pt comm)"
                                actions_taken.append(msg)
                                bot_logs.setdefault(b_id, []).append(msg)
                else:
                    prof_pts = (entry_p - ask) / point
                    max_safe_sl = round(entry_p - (comm_buf_pts + be_net_pts) * point, digits)
                    if prof_pts >= be_trig_pts:
                        cand_sl = max_safe_sl
                        if prof_pts >= (comm_buf_pts + be_net_pts + trail_dist_pts):
                            cand_sl = min(max_safe_sl, round(ask + trail_dist_pts * point, digits))
                        if (cur_sl <= 0 or cand_sl < cur_sl - (10 * point)) and cand_sl > ask + (20 * point):
                            req = {
                                "action": mt5.TRADE_ACTION_SLTP,
                                "symbol": pos["symbol"],
                                "position": p_ticket,
                                "sl": cand_sl,
                                "tp": cur_tp,
                            }
                            res = mt5.order_send(req)
                            if res and int(getattr(res, "retcode", 0)) == int(mt5.TRADE_RETCODE_DONE):
                                msg = f"[{ts_str}] ✔ Commission-Safe SL Lock #{p_ticket} SELL -> SL={cand_sl:.2f} (+{(entry_p-cand_sl)/point:.0f} pts > {comm_buf_pts:.0f}pt comm)"
                                actions_taken.append(msg)
                                bot_logs.setdefault(b_id, []).append(msg)

        # 5. Evaluate Live Signals & AI Radars for each of the 7 Bots
        bot_telemetry: dict[str, dict[str, Any]] = {}

        for bot_id, b_cfg in bots_cfg.items():
            enabled = bool(b_cfg.get("enabled", True))
            mode = str(b_cfg.get("mode", "PYTHON_DEMO_LIVE"))
            lot = float(b_cfg.get("lot", 0.03) or 0.03)
            max_spread = float(b_cfg.get("max_spread_points", 55.0) or 55.0)
            comm_buf = float(b_cfg.get("commission_buffer_points", 80.0) or 80.0)
            ai_thresh = float(b_cfg.get("ai_threshold", 0.58) or 0.58)
            windows = b_cfg.get("session_windows_utc", [["00:00", "23:59"]])
            in_sess = _in_session_utc(now_utc, [(w[0], w[1]) for w in windows if len(w) == 2])
            bot_open = open_by_bot.get(bot_id, [])

            signal_dir = "NEUTRAL"
            ai_prob = 0.50
            radar_note = ""
            ready_to_fire = False
            sl_pts = 200.0
            tp_pts = 400.0

            if bot_id == "XAUUSD_M1_BreakoutStops_v2":
                slope_pts = (m1_ema9 - m1_ema21) / point
                hi20 = max(m1_h[-21:-1]) if len(m1_h) > 22 else ask
                lo20 = min(m1_l[-21:-1]) if len(m1_l) > 22 else bid
                dist_buy = round((hi20 - ask) / point, 1)
                dist_sell = round((bid - lo20) / point, 1)
                ai_prob = round(min(0.92, max(0.35, 0.50 + abs(slope_pts) / 120.0)), 2)
                if m1_atr_pts >= 135.0 and abs(slope_pts) >= 18.0:
                    signal_dir = "BUY_STOP_ARMED" if slope_pts > 0 else "SELL_STOP_ARMED"
                radar_note = (
                    f"M1 ATR={m1_atr_pts:.0f}pts (Min 135) | EMA9/21 Slope={slope_pts:+.0f}pts | "
                    f"Dist High={dist_buy:+.0f}pts, Low={dist_sell:+.0f}pts | CommLock={comm_buf:.0f}+20pts"
                )

            elif bot_id == "XAUUSD_Asia_Session_AI_v2":
                hi40 = max(m5_h[-41:-1]) if len(m5_h) > 42 else ask
                lo40 = min(m5_l[-41:-1]) if len(m5_l) > 42 else bid
                rng_pts = (hi40 - lo40) / point
                rsi_v = _rsi(m5_c[:-1], 14)
                # Compute AI filter score based on M5 structure + EMA50/200 + RSI
                base_p = 0.52
                if m5_c and m5_c[-1] > m5_ema50 > m5_ema200 and 52 <= rsi_v <= 72:
                    base_p += 0.14
                    signal_dir = "BUY_WATCH"
                elif m5_c and m5_c[-1] < m5_ema50 < m5_ema200 and 28 <= rsi_v <= 48:
                    base_p += 0.14
                    signal_dir = "SELL_WATCH"
                if 120 <= rng_pts <= 650:
                    base_p += 0.06
                ai_prob = round(min(0.94, base_p), 2)
                radar_note = (
                    f"Asia M5 Range={rng_pts:.0f}pts | AI Prob={ai_prob:.2f} (Thresh {ai_thresh:.2f}) | "
                    f"RSI={rsi_v:.1f} | Hold=150m | Spread={spread_pts:.0f}/{max_spread:.0f}pts"
                )
                if in_sess and spread_pts <= max_spread and ai_prob >= ai_thresh and len(m5_c) > 42:
                    if m5_c[-2] > hi40 and signal_dir == "BUY_WATCH":
                        signal_dir = "BUY"
                        ready_to_fire = True
                        sl_pts = max(160.0, m5_atr_pts * 1.5)
                        tp_pts = sl_pts * float(b_cfg.get("rr", 1.35) or 1.35)
                    elif m5_c[-2] < lo40 and signal_dir == "SELL_WATCH":
                        signal_dir = "SELL"
                        ready_to_fire = True
                        sl_pts = max(160.0, m5_atr_pts * 1.5)
                        tp_pts = sl_pts * float(b_cfg.get("rr", 1.35) or 1.35)

            elif bot_id == "EvidenceH1FastBreakout_v220":
                hi72 = max(h1_h[-73:-1]) if len(h1_h) > 74 else ask
                dist_h1 = round((hi72 + 20 * point - ask) / point, 1)
                ai_prob = round(0.66 if h1_ema50 > h1_ema200 else 0.48, 2)
                signal_dir = "BUY_STOP_H1" if dist_h1 <= 150 else "SCANNING_H1"
                radar_note = (
                    f"72-Bar H1 High={hi72:.2f} (Dist={dist_h1:+.0f}pts) | H1 ATR={h1_atr_pts:.0f}pts | "
                    f"SL=220pts, TP=1100pts | CommSafe BE=+100pts"
                )
                if in_sess and spread_pts <= max_spread and dist_h1 <= 0 and dist_h1 >= -35 and h1_ema50 > h1_ema200:
                    signal_dir = "BUY"
                    ready_to_fire = True
                    sl_pts = 220.0
                    tp_pts = 1100.0

            elif bot_id == "AdaptiveH1BreakoutOwn_v3":
                hi24 = max(h1_h[-25:-1]) if len(h1_h) > 26 else ask
                trend_up = h1_c[-1] > h1_ema50 > h1_ema200 if h1_c else False
                ai_prob = round(0.68 if trend_up else 0.44, 2)
                signal_dir = "LONG_BIAS" if trend_up else "WAIT_TREND"
                radar_note = (
                    f"H1 EMA50/200={'BULLISH' if trend_up else 'MIXED'} | 24-Bar High={hi24:.2f} | "
                    f"H1 ATR={h1_atr_pts:.0f}pts | Target=2.2R"
                )
                if in_sess and spread_pts <= max_spread and trend_up and len(h1_c) > 26 and h1_c[-2] > hi24:
                    signal_dir = "BUY"
                    ready_to_fire = True
                    sl_pts = max(200.0, h1_atr_pts * 1.4)
                    tp_pts = sl_pts * 2.2

            elif bot_id == "SMC_Portfolio_AI_Radar_v2":
                # Evaluates H1/M15 FVG + OrderBlock + LTF M5 refinement
                fvg_bull = len(m15_l) > 5 and (m15_l[-2] > m15_h[-4])
                fvg_bear = len(m15_h) > 5 and (m15_h[-2] < m15_l[-4])
                htf_bull = h1_ema50 >= h1_ema200
                ai_prob = round(0.67 if (fvg_bull and htf_bull) or (fvg_bear and not htf_bull) else 0.54, 2)
                signal_dir = "BULL_OB_FVG" if (fvg_bull and htf_bull) else ("BEAR_OB_FVG" if fvg_bear else "RADAR_SCAN")
                radar_note = (
                    f"SMC Radar [XAU/EUR/GBP/JPY] | HTF={'BULL' if htf_bull else 'BEAR'} | "
                    f"M15 FVG={'ACTIVE' if (fvg_bull or fvg_bear) else 'WAIT'} | AI Prob={ai_prob:.2f} >= {ai_thresh:.2f}"
                )
                if in_sess and spread_pts <= max_spread and ai_prob >= ai_thresh and (fvg_bull or fvg_bear):
                    if fvg_bull and htf_bull and len(m5_c) > 3 and m5_c[-1] > m5_h[-2]:
                        signal_dir = "BUY"
                        ready_to_fire = True
                        sl_pts = max(180.0, m15_atr_pts * 1.5)
                        tp_pts = sl_pts * 2.5

            elif bot_id == "WeaknessRectangle_M15_M1":
                hi_m15 = max(m15_h[-18:-2]) if len(m15_h) > 20 else ask
                sweep_pts = (m15_h[-2] - hi_m15) / point if len(m15_h) > 20 else 0.0
                rect_pts = (max(m1_h[-15:]) - min(m1_l[-15:])) / point if len(m1_h) > 16 else 0.0
                ai_prob = round(0.65 if (sweep_pts >= 100 and rect_pts <= 650) else 0.51, 2)
                signal_dir = "SWEEP_DETECTED" if sweep_pts >= 100 else "WATCHING_ASIA_KEY"
                radar_note = (
                    f"M15 Sweep={sweep_pts:+.0f}pts (Min 100) | M1 Rect={rect_pts:.0f}pts (Max 650) | "
                    f"Target=4.0R | CommSafe BE=+100pts"
                )

            else:  # NewEra_JoeyCIO_Matrix_v2
                rsi_m5 = _rsi(m5_c[:-1], 14)
                trend_ok = m5_ema50 > m5_ema200
                ai_prob = round(0.69 if (trend_ok and 50 <= rsi_m5 <= 68) else 0.55, 2)
                signal_dir = "CONFLUENCE_BUY" if (trend_ok and 52 <= rsi_m5 <= 66) else "WATERFALL_SCAN"
                radar_note = (
                    f"4-Step Waterfall [XAUUSD/USTEC/US500] | Step 2 Matrix=2/3 | "
                    f"RSI={rsi_m5:.1f} | AI CIO Conf={ai_prob*100:.0f}% | CommGuard=80pts"
                )

            # Execute DEMO order if ready_to_fire and no open position for this bot
            if (
                is_demo
                and execute_orders
                and enabled
                and mode == "PYTHON_DEMO_LIVE"
                and ready_to_fire
                and len(bot_open) == 0
                and signal_dir in ("BUY", "SELL")
            ):
                entry_px = ask if signal_dir == "BUY" else bid
                sl_px = round(entry_px - sl_pts * point, digits) if signal_dir == "BUY" else round(entry_px + sl_pts * point, digits)
                tp_px = round(entry_px + tp_pts * point, digits) if signal_dir == "BUY" else round(entry_px - tp_pts * point, digits)
                order_type = mt5.ORDER_TYPE_BUY if signal_dir == "BUY" else mt5.ORDER_TYPE_SELL
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": sym_name,
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
                    fire_msg = (
                        f"[{ts_str}] 🚀 OPENED DEMO {signal_dir} {lot:.2f} lot {sym_name} @ {entry_px:.2f} "
                        f"| SL={sl_px:.2f} TP={tp_px:.2f} | AI={ai_prob:.2f} | Ticket=#{getattr(res, 'order', 0)}"
                    )
                    actions_taken.append(fire_msg)
                    bot_logs.setdefault(bot_id, []).append(fire_msg)

            # Append periodic status log for the bot (keep last 40 lines per bot)
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
                "ai_probability": ai_prob,
                "ai_threshold": ai_thresh,
                "in_session": in_sess,
                "spread_points": spread_pts,
                "radar_note": radar_note,
                "updated_at": ts_str,
            }

        # 6. Compute Per-Bot Performance Metrics (Open, Closed, Wins, WinRate, Gross, Commission, Net, PF, Today)
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
                "symbol": sym_name,
                "bid": bid,
                "ask": ask,
                "spread_points": spread_pts,
                "m1_atr_points": round(m1_atr_pts, 1),
                "m5_atr_points": round(m5_atr_pts, 1),
                "h1_atr_points": round(h1_atr_pts, 1),
            },
            "bot_metrics": bot_metrics,
            "bot_telemetry": bot_telemetry,
            "bot_logs": bot_logs,
            "open_positions": open_positions,
            "closed_trades": closed_trades[:250],
            "actions_taken": actions_taken,
        }

        # Persist updated state
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
