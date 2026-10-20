"""Direct MetaTrader 5 bridge for account telemetry, symbols, ticks, L2 DOM, OHLCV bars, and Demo-Only execution.

Safety contract:
- Telemetry & market data (`initialize`, `account_info`, `symbols_get`, `symbol_info`,
  `symbol_info_tick`, `market_book_add`, `market_book_get`, `market_book_release`,
  `copy_rates_from_pos`, `positions_get`).
- Order placement (`execute_mt5_demo_order`) and position closing (`close_mt5_demo_positions`)
  are **strictly restricted to DEMO accounts** (`account_info.trade_mode == ACCOUNT_TRADE_MODE_DEMO`,
  i.e. `is_demo == True`). Any attempt to execute on a real/contest account raises `PermissionError`.
- Supports both in-process `MetaTrader5` (Python <= 3.14) and transparent out-of-process
  invocation via a local Python 3.11-3.14 interpreter when the API server runs under
  Python 3.15 (`.venv-api`).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEMO_POSITIONS_PATH = ROOT_DIR / "TradingAgentsData" / "research" / "demo_positions.json"
DEMO_HISTORY_PATH = ROOT_DIR / "TradingAgentsData" / "research" / "demo_history.json"
DEMO_MAGIC_NUMBER = 20260418
DEFAULT_MAX_OPEN_POSITIONS_PER_SYMBOL = 1
DEFAULT_MAX_DAILY_DEMO_TRADES = 10


def _read_safety_limit(name: str, default: int) -> int:
    """Read an integer safety limit, failing closed when its environment value is bad."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


# These are deliberately visible configuration points for operators and tests.
MAX_OPEN_POSITIONS_PER_SYMBOL = _read_safety_limit(
    "NEWERA_MAX_OPEN_POSITIONS_PER_SYMBOL",
    DEFAULT_MAX_OPEN_POSITIONS_PER_SYMBOL,
)
MAX_DAILY_DEMO_TRADES = _read_safety_limit(
    "NEWERA_MAX_DAILY_DEMO_TRADES",
    DEFAULT_MAX_DAILY_DEMO_TRADES,
)
_DEMO_EXECUTION_LOCK = threading.Lock()


def _configured_safety_limit(name: str, configured: int) -> int:
    """Allow a process environment override while treating malformed values as zero."""
    return _read_safety_limit(name, configured)


def _is_order_safety_rejection(mt5: Any, retcode: int) -> bool:
    """Identify broker responses for which a paper overlay must not hide a safety failure."""
    for name in (
        "TRADE_RETCODE_INVALID_STOPS",
        "TRADE_RETCODE_INVALID_PRICE",
        "TRADE_RETCODE_INVALID_VOLUME",
        "TRADE_RETCODE_INVALID_ORDER",
    ):
        code = getattr(mt5, name, None)
        if code is not None and int(code) == retcode:
            return True
    return False


def _validate_sl_tp_geometry(side: str, entry_price: float, sl: float, tp: float) -> None:
    """Reject missing or incorrectly ordered protective levels without repairing them."""
    norm_side = str(side).strip().upper()
    if norm_side not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported order side '{side}'; must be BUY or SELL.")

    try:
        entry = float(entry_price)
        stop_loss = float(sl)
        take_profit = float(tp)
    except (TypeError, ValueError) as exc:
        raise ValueError("SL, TP, and the current entry price must be numeric.") from exc

    if not all(math.isfinite(value) for value in (entry, stop_loss, take_profit)):
        raise ValueError("SL, TP, and the current entry price must be finite numbers.")
    if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
        raise ValueError(
            f"Invalid {norm_side} SL/TP geometry: both SL and TP must be positive and bracket entry {entry_price}."
        )

    valid = stop_loss < entry < take_profit if norm_side == "BUY" else take_profit < entry < stop_loss
    if not valid:
        if norm_side == "BUY":
            requirement = f"SL ({stop_loss}) < entry ({entry}) < TP ({take_profit})"
        else:
            requirement = f"TP ({take_profit}) < entry ({entry}) < SL ({stop_loss})"
        raise ValueError(f"Invalid {norm_side} SL/TP geometry: require {requirement}.")


# Public alias keeps the pure validation boundary easy to exercise without MT5.
validate_sl_tp_geometry = _validate_sl_tp_geometry


def _field_value(row: Any, field: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(field, default)
    return getattr(row, field, default)


def count_open_positions_for_symbol(
    terminal_positions: Any,
    paper_positions: Any,
    symbol: str,
    *,
    resolved_symbol: str | None = None,
) -> int:
    """Count all open terminal/overlay positions matching the requested broker symbol."""
    symbols = {
        str(candidate).strip().upper()
        for candidate in (symbol, resolved_symbol)
        if candidate and str(candidate).strip()
    }
    if not symbols:
        return 0

    count = 0
    for row in list(terminal_positions or []) + list(paper_positions or []):
        row_symbol = str(_field_value(row, "symbol", "") or "").strip().upper()
        if row_symbol in symbols:
            count += 1
    return count


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _record_timestamps(row: Any) -> list[datetime]:
    timestamps: list[datetime] = []
    for field in ("opened_at", "open_time", "time", "timestamp", "close_time"):
        parsed = _coerce_utc_datetime(_field_value(row, field))
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


def count_daily_demo_trades(records: Any, *, now: datetime | None = None) -> int:
    """Count records in the current UTC day; unknown timestamps count conservatively."""
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    for row in list(records or []):
        timestamps = _record_timestamps(row)
        # The caller supplies a bounded MT5 history query. A malformed record must
        # not be used to bypass the cap, so count it as current-day activity.
        if not timestamps or any(day_start <= timestamp <= current for timestamp in timestamps):
            count += 1
    return count



PRIORITY_SYMBOLS: tuple[str, ...] = (
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "BTCUSD",
    "ETHUSD",
    "XAGUSD",
    "US500",
    "USTEC",
    "US30",
    "DE40",
    "UK100",
    "JP225",
    "WTI",
    "BRENT",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
)

TIMEFRAME_MAP: dict[str, str] = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


def find_mt5_python() -> str | None:
    """Locate a Python interpreter that has `MetaTrader5` installed."""
    try:
        import MetaTrader5  # noqa: F401

        return sys.executable
    except ImportError:
        pass

    candidates = [
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python314" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe",
        Path(r"C:\Python314\python.exe"),
        Path(r"C:\Python313\python.exe"),
        Path(r"C:\Python312\python.exe"),
        Path(r"C:\Python311\python.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    py_launcher = shutil.which("py")
    if py_launcher:
        for ver in ("-3.14", "-3.13", "-3.12", "-3.11"):
            try:
                proc = subprocess.run(
                    [py_launcher, ver, "-c", "import sys; print(sys.executable)"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip()
            except Exception:
                continue
    return None


def _resolve_broker_symbol(mt5: Any, requested: str) -> str:
    req = requested.strip()
    if not req:
        req = "XAUUSD"
    info = mt5.symbol_info(req)
    if info is not None:
        return info.name

    symbols = mt5.symbols_get() or []
    upper_req = req.upper()
    exact = [s.name for s in symbols if s.name.upper() == upper_req]
    if exact:
        return exact[0]

    prefix_matches = [s.name for s in symbols if s.name.upper().startswith(upper_req)]
    if prefix_matches:
        return sorted(prefix_matches, key=len)[0]

    if "XAU" in upper_req or "GOLD" in upper_req:
        gold = [s.name for s in symbols if "XAU" in s.name.upper() or "GOLD" in s.name.upper()]
        if gold:
            return sorted(gold, key=len)[0]

    contains_matches = [s.name for s in symbols if upper_req in s.name.upper()]
    if contains_matches:
        return sorted(contains_matches, key=len)[0]

    raise RuntimeError(f"Symbol '{requested}' not found in MT5 broker symbols")


def _compute_atr(rows: list[Any], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    trs: list[float] = []
    prev_close = float(rows[0]["close"])
    for row in rows[1:]:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def _inprocess_status(max_symbols: int = 250) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        return {
            "connected": False,
            "error": f"MT5 initialize failed: {mt5.last_error()}",
            "account": None,
            "symbols": list(PRIORITY_SYMBOLS[:10]),
            "visible_symbols": [],
            "total_broker_symbols": 0,
        }
    try:
        acc = mt5.account_info()
        all_syms = mt5.symbols_get() or []
        total_count = len(all_syms)

        by_upper = {s.name.upper(): s.name for s in all_syms}
        visible = [s.name for s in all_syms if getattr(s, "visible", False)]

        ordered: list[str] = []
        seen: set[str] = set()

        for p_sym in PRIORITY_SYMBOLS:
            actual = by_upper.get(p_sym)
            if actual and actual not in seen:
                ordered.append(actual)
                seen.add(actual)

        for v_sym in visible:
            if v_sym not in seen:
                ordered.append(v_sym)
                seen.add(v_sym)

        for s_obj in all_syms:
            if len(ordered) >= max_symbols:
                break
            name = s_obj.name
            if name in seen:
                continue
            path_upper = str(getattr(s_obj, "path", "")).upper()
            if len(name) <= 10 and any(
                tag in path_upper for tag in ("FOREX", "METAL", "CRYPTO", "INDIC", "CASH", "MAJOR", "MINOR", "SPOT")
            ):
                ordered.append(name)
                seen.add(name)

        trade_mode_raw = getattr(acc, "trade_mode", 0) if acc else 0
        trade_mode_label = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(trade_mode_raw, "UNKNOWN")

        account_dict = None
        if acc is not None:
            bal = float(acc.balance)
            mt5_pnl = float(acc.profit)
            # Also include any active paper fallback positions' floating PnL if in demo mode
            paper_pnl = 0.0
            if trade_mode_raw == 0:
                try:
                    for p_item in _load_paper_demo_positions():
                        paper_pnl += float(p_item.get("profit") or 0.0)
                except Exception:
                    pass
            live_pnl = round(mt5_pnl + paper_pnl, 2)
            live_pnl_pct = round((live_pnl / bal) * 100.0, 2) if bal > 0 else 0.0
            account_dict = {
                "login": int(acc.login),
                "company": str(acc.company),
                "server": str(acc.server),
                "name": str(getattr(acc, "name", "")),
                "currency": str(acc.currency),
                "balance": bal,
                "equity": round(float(acc.equity) + paper_pnl, 2),
                "margin": float(acc.margin),
                "margin_free": float(acc.margin_free),
                "profit": live_pnl,
                "live_floating_pnl": live_pnl,
                "live_floating_pnl_pct": live_pnl_pct,
                "leverage": int(acc.leverage),
                "trade_mode": trade_mode_label,
                "is_demo": trade_mode_raw == 0,
            }

        return {
            "connected": acc is not None,
            "error": None if acc is not None else "MT5 account_info returned None",
            "account": account_dict,
            "symbols": ordered or list(PRIORITY_SYMBOLS[:10]),
            "visible_symbols": visible,
            "total_broker_symbols": total_count,
        }
    finally:
        mt5.shutdown()


def _inprocess_symbol_snapshot(symbol: str) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        resolved = _resolve_broker_symbol(mt5, symbol)
        mt5.symbol_select(resolved, True)
        info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        if info is None:
            raise RuntimeError(f"Could not read symbol_info for {resolved}")

        point = float(info.point) if info.point else 0.01
        digits = int(info.digits)
        bid = float(tick.bid) if tick and tick.bid else float(getattr(info, "bid", 0.0))
        ask = float(tick.ask) if tick and tick.ask else float(getattr(info, "ask", 0.0))
        if ask > 0 and bid > 0 and point > 0:
            spread_points = round((ask - bid) / point, 1)
        else:
            spread_points = float(getattr(info, "spread", 0.0))

        rates_m5 = mt5.copy_rates_from_pos(resolved, mt5.TIMEFRAME_M5, 0, 120)
        closed_m5 = list(rates_m5[:-1]) if rates_m5 is not None and len(rates_m5) > 2 else []
        atr_m5 = _compute_atr(closed_m5, 14) if closed_m5 else 0.0
        window_20 = closed_m5[-20:] if len(closed_m5) >= 20 else closed_m5
        range_20_low = min(float(r["low"]) for r in window_20) if window_20 else bid
        range_20_high = max(float(r["high"]) for r in window_20) if window_20 else ask
        last_close = float(closed_m5[-1]["close"]) if closed_m5 else bid
        last_bar_time = (
            datetime.fromtimestamp(int(closed_m5[-1]["time"]), timezone.utc).isoformat()
            if closed_m5
            else None
        )

        return {
            "requested_symbol": symbol,
            "symbol": resolved,
            "description": str(getattr(info, "description", resolved)),
            "currency_base": str(getattr(info, "currency_base", "")),
            "currency_profit": str(getattr(info, "currency_profit", "USD")),
            "digits": digits,
            "point": point,
            "bid": bid,
            "ask": ask,
            "last_close": last_close,
            "spread_points": spread_points,
            "trade_contract_size": float(getattr(info, "trade_contract_size", 100.0) or 100.0),
            "trade_tick_value": float(getattr(info, "trade_tick_value", 1.0) or 1.0),
            "trade_tick_size": float(getattr(info, "trade_tick_size", point) or point),
            "volume_min": float(getattr(info, "volume_min", 0.01) or 0.01),
            "volume_max": float(getattr(info, "volume_max", 100.0) or 100.0),
            "volume_step": float(getattr(info, "volume_step", 0.01) or 0.01),
            "atr_m5": atr_m5,
            "atr_m5_points": round(atr_m5 / point, 1) if point > 0 else 0.0,
            "range_20_low": range_20_low,
            "range_20_high": range_20_high,
            "last_bar_time": last_bar_time,
        }
    finally:
        mt5.shutdown()


def _inprocess_market_book(symbol: str, levels: int = 10) -> dict[str, Any]:
    """Read L2 DOM via `mt5.market_book_get(symbol)`, falling back to M1/M5 volume profile DOM."""
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        resolved = _resolve_broker_symbol(mt5, symbol)
        mt5.symbol_select(resolved, True)
        info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        point = float(info.point) if info and info.point else 0.01
        digits = int(info.digits) if info else 2
        bid = float(tick.bid) if tick and tick.bid else float(getattr(info, "bid", 0.0) if info else 0.0)
        ask = float(tick.ask) if tick and tick.ask else float(getattr(info, "ask", 0.0) if info else 0.0)

        # 1. Try native MT5 ECN Level 2 Market Book (`market_book_add` + `market_book_get`)
        book_added = mt5.market_book_add(resolved)
        raw_book = None
        if book_added:
            try:
                time.sleep(0.08)
                raw_book = mt5.market_book_get(resolved)
            finally:
                mt5.market_book_release(resolved)

        if raw_book and len(raw_book) > 0:
            bids: list[dict[str, float]] = []
            asks: list[dict[str, float]] = []
            sell_types = {
                getattr(mt5, "BOOK_TYPE_SELL", 1),
                getattr(mt5, "BOOK_TYPE_SELL_MARKET", 3),
            }
            buy_types = {
                getattr(mt5, "BOOK_TYPE_BUY", 2),
                getattr(mt5, "BOOK_TYPE_BUY_MARKET", 4),
            }
            for item in raw_book:
                b_type = int(getattr(item, "type", 0))
                price = round(float(getattr(item, "price", 0.0)), digits)
                vol = float(getattr(item, "volume_real", 0.0) or getattr(item, "volume_dbl", 0.0) or getattr(item, "volume", 0.0))
                if vol <= 0:
                    continue
                if b_type in sell_types:
                    asks.append({"price": price, "volume": vol})
                elif b_type in buy_types:
                    bids.append({"price": price, "volume": vol})
            if bids and asks:
                return {
                    "symbol": resolved,
                    "source": "mt5_l2_dom",
                    "digits": digits,
                    "point": point,
                    "bids": sorted(bids, key=lambda x: x["price"], reverse=True)[:levels],
                    "asks": sorted(asks, key=lambda x: x["price"])[:levels],
                }

        # 2. Synthesize institutional Volume-Profile & Liquidity Pool DOM from MT5 M1/M5 bars
        rates = mt5.copy_rates_from_pos(resolved, mt5.TIMEFRAME_M5, 0, 240)
        if rates is None or len(rates) < 10:
            rates = mt5.copy_rates_from_pos(resolved, mt5.TIMEFRAME_H1, 0, 120)
        rows = list(rates) if rates is not None else []
        if bid <= 0 and rows:
            bid = float(rows[-1]["close"])
            ask = round(bid + max(point * 10, 0.01), digits)

        atr = _compute_atr(rows[-60:], 14) if len(rows) >= 15 else max(point * 50, bid * 0.0008)
        step = max(point * 5, round(atr / max(6, levels), digits))
        if step <= 0:
            step = point * 10

        # Aggregate historical tick volume around each ladder price bucket + swing highs/lows
        swing_highs = [float(r["high"]) for r in rows[-80:]]
        swing_lows = [float(r["low"]) for r in rows[-80:]]

        bids_out: list[dict[str, float]] = []
        asks_out: list[dict[str, float]] = []

        for lvl in range(levels):
            bid_p = round(bid - lvl * step, digits)
            ask_p = round(ask + lvl * step, digits)

            bid_vol = 18.0 + (levels - lvl) * 3.5
            ask_vol = 18.0 + (levels - lvl) * 3.5

            for r in rows[-120:]:
                r_low = float(r["low"])
                r_high = float(r["high"])
                r_close = float(r["close"])
                r_open = float(r["open"])
                r_tv = float(r["tick_volume"])
                if r_low - step <= bid_p <= r_high + step:
                    weight = 1.35 if r_close >= r_open else 0.85
                    bid_vol += (r_tv / 35.0) * weight
                if r_low - step <= ask_p <= r_high + step:
                    weight = 1.35 if r_close < r_open else 0.85
                    ask_vol += (r_tv / 35.0) * weight

            # Boost liquidity wall where multiple swing highs/lows cluster
            near_lows = sum(1 for sl in swing_lows if abs(sl - bid_p) <= step * 0.8)
            near_highs = sum(1 for sh in swing_highs if abs(sh - ask_p) <= step * 0.8)
            if near_lows >= 2:
                bid_vol *= 1.0 + min(2.2, near_lows * 0.48)
            if near_highs >= 2:
                ask_vol *= 1.0 + min(2.2, near_highs * 0.48)

            bids_out.append({"price": bid_p, "volume": round(bid_vol, 2)})
            asks_out.append({"price": ask_p, "volume": round(ask_vol, 2)})

        return {
            "symbol": resolved,
            "source": "mt5_tick_volume_dom",
            "digits": digits,
            "point": point,
            "bids": bids_out,
            "asks": asks_out,
        }
    finally:
        mt5.shutdown()


def _inprocess_export_bars(
    symbol: str,
    output_csv: Path,
    *,
    timeframe: str = "M5",
    bars: int = 5000,
) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        resolved = _resolve_broker_symbol(mt5, symbol)
        if not mt5.symbol_select(resolved, True):
            raise RuntimeError(f"Could not select {resolved} in MT5: {mt5.last_error()}")

        tf_attr = TIMEFRAME_MAP.get(timeframe.upper(), "TIMEFRAME_M5")
        tf_val = getattr(mt5, tf_attr, mt5.TIMEFRAME_M5)

        rates = mt5.copy_rates_from_pos(resolved, tf_val, 0, bars + 1)
        if rates is None or len(rates) < 601:
            raise RuntimeError(
                f"Not enough {timeframe} bars from MT5 for {resolved} "
                f"(received {0 if rates is None else len(rates)}, need >= 600): {mt5.last_error()}"
            )

        rows = list(rates[:-1])[-bars:]
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        with output_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("timestamp", "open", "high", "low", "close", "volume", "amount"))
            for row in rows:
                ts = datetime.fromtimestamp(int(row["time"]), timezone.utc).isoformat()
                vol = float(row["tick_volume"])
                close = float(row["close"])
                writer.writerow(
                    (
                        ts,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        close,
                        vol,
                        vol * close,
                    )
                )

        info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        point = float(info.point) if info and info.point else 0.01
        bid = float(tick.bid) if tick and tick.bid else float(rows[-1]["close"])
        ask = float(tick.ask) if tick and tick.ask else bid
        spread_points = (
            round((ask - bid) / point, 1)
            if (ask > 0 and bid > 0 and point > 0)
            else float(getattr(info, "spread", 0.0) if info else 0.0)
        )
        atr_14 = _compute_atr(rows[-60:], 14)
        w20 = rows[-20:]

        return {
            "ok": True,
            "requested_symbol": symbol,
            "resolved_symbol": resolved,
            "timeframe": timeframe.upper(),
            "bars_exported": len(rows),
            "output_csv": str(output_csv),
            "last_bar": datetime.fromtimestamp(int(rows[-1]["time"]), timezone.utc).isoformat(),
            "last_close": float(rows[-1]["close"]),
            "bid": bid,
            "ask": ask,
            "point": point,
            "digits": int(info.digits) if info else 2,
            "spread_points": spread_points,
            "atr_m5": atr_14,
            "range_20_low": min(float(r["low"]) for r in w20),
            "range_20_high": max(float(r["high"]) for r in w20),
        }
    finally:
        mt5.shutdown()


def _assert_demo_account(mt5: Any) -> Any:
    """Strictly verify that the connected MT5 account is a DEMO account (`is_demo == True`)."""
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"MT5 account_info() returned None: {mt5.last_error()}")
    if int(info.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO):
        raise PermissionError(
            f"SAFETY BLOCK: Order execution is strictly limited to DEMO accounts "
            f"(current login={info.login}, trade_mode={info.trade_mode})."
        )
    return info


def _load_paper_demo_positions(*, fail_closed: bool = False) -> list[dict[str, Any]]:
    if not DEMO_POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(DEMO_POSITIONS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            if fail_closed and any(
                not isinstance(row, dict) or not str(row.get("symbol") or "").strip() for row in data
            ):
                raise RuntimeError("SAFETY BLOCK: Demo overlay positions contain malformed records.")
            return [row for row in data if isinstance(row, dict)]
        if fail_closed:
            raise RuntimeError("SAFETY BLOCK: Demo overlay positions file is not a list.")
    except (OSError, json.JSONDecodeError) as exc:
        if fail_closed:
            raise RuntimeError("SAFETY BLOCK: Could not inspect Demo overlay positions safely.") from exc
    if fail_closed:
        raise RuntimeError("SAFETY BLOCK: Could not inspect Demo overlay positions safely.")
    return []


def _save_paper_demo_positions(positions: list[dict[str, Any]]) -> bool:
    try:
        DEMO_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEMO_POSITIONS_PATH.write_text(json.dumps(positions, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def _load_paper_demo_history(*, fail_closed: bool = False) -> list[dict[str, Any]]:
    if not DEMO_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(DEMO_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            if fail_closed and any(not isinstance(row, dict) for row in data):
                raise RuntimeError("SAFETY BLOCK: Demo overlay history contains malformed records.")
            return [row for row in data if isinstance(row, dict)]
        if fail_closed:
            raise RuntimeError("SAFETY BLOCK: Demo overlay history file is not a list.")
    except (OSError, json.JSONDecodeError) as exc:
        if fail_closed:
            raise RuntimeError("SAFETY BLOCK: Could not inspect Demo overlay history safely.") from exc
    if fail_closed:
        raise RuntimeError("SAFETY BLOCK: Could not inspect Demo overlay history safely.")
    return []


def _append_paper_demo_history(entry: dict[str, Any]) -> None:
    try:
        history = _load_paper_demo_history()
        history.append(entry)
        if len(history) > 500:
            history = history[-500:]
        DEMO_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEMO_HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _enrich_position_metrics(
    raw: dict[str, Any],
    *,
    point: float,
    digits: int,
    contract_size: float,
    tick_value: float,
    tick_size: float,
    account_balance: float = 0.0,
) -> dict[str, Any]:
    side = str(raw.get("side") or "BUY").upper()
    volume = max(float(raw.get("volume") or 0.01), 0.01)
    price_open = float(raw.get("price_open") or 0.0)
    price_current = float(raw.get("price_current") or price_open)
    sl = float(raw.get("sl") or 0.0)
    tp = float(raw.get("tp") or 0.0)
    safe_point = point if point > 0 else 0.01

    if tick_value > 0 and tick_size > 0:
        usd_per_price_unit = (tick_value / tick_size) * volume
    else:
        usd_per_price_unit = (contract_size if contract_size > 0 else 100.0) * volume

    signed_diff = (price_current - price_open) if side == "BUY" else (price_open - price_current)
    points_distance = round(signed_diff / safe_point, 1) if safe_point > 0 else 0.0
    pips_distance = round(points_distance / 10.0, 1)

    sl_points = round(abs(price_open - sl) / safe_point, 1) if sl > 0 else 0.0
    tp_points = round(abs(tp - price_open) / safe_point, 1) if tp > 0 else 0.0
    sl_pips = round(sl_points / 10.0, 1)
    tp_pips = round(tp_points / 10.0, 1)

    risk_usd = round(abs(price_open - sl) * usd_per_price_unit, 2) if sl > 0 else 0.0
    reward_usd = round(abs(tp - price_open) * usd_per_price_unit, 2) if tp > 0 else 0.0
    rr_ratio = round(reward_usd / risk_usd, 2) if risk_usd > 0 else 0.0

    if raw.get("profit") is not None and raw.get("source") == "mt5_terminal":
        floating_pnl = round(float(raw["profit"]), 2)
    else:
        floating_pnl = round(signed_diff * usd_per_price_unit, 2)

    pnl_pct = round((floating_pnl / account_balance) * 100.0, 2) if account_balance > 0 else 0.0

    return {
        "ticket": int(raw.get("ticket") or 0),
        "symbol": str(raw.get("symbol") or "XAUUSD"),
        "side": side,
        "volume": round(volume, 2),
        "price_open": round(price_open, digits),
        "price_current": round(price_current, digits),
        "sl": round(sl, digits) if sl > 0 else 0.0,
        "tp": round(tp, digits) if tp > 0 else 0.0,
        "profit": floating_pnl,
        "pnl_pct": pnl_pct,
        "swap": round(float(raw.get("swap") or 0.0), 2),
        "points_distance": points_distance,
        "pips_distance": pips_distance,
        "sl_points": sl_points,
        "tp_points": tp_points,
        "sl_pips": sl_pips,
        "tp_pips": tp_pips,
        "risk_usd": risk_usd,
        "reward_usd": reward_usd,
        "rr_ratio": rr_ratio,
        "point": safe_point,
        "digits": digits,
        "break_even_active": bool(raw.get("break_even_active", False)),
        "trailing_active": bool(raw.get("trailing_active", False)),
        "partial_closed_50": bool(raw.get("partial_closed_50", False)),
        "realized_partial_profit": round(float(raw.get("realized_partial_profit") or 0.0), 2),
        "opened_at": str(raw.get("opened_at") or datetime.now(timezone.utc).isoformat()),
        "comment": str(raw.get("comment") or "NewEraQuantDemo"),
        "source": str(raw.get("source") or "mt5_terminal"),
    }


def compute_dynamic_position_adjustment(
    pos: dict[str, Any],
    *,
    atr: float,
    point: float = 0.01,
    digits: int = 2,
    be_atr_trigger: float = 1.0,
    partial_close_atr_trigger: float = 2.0,
    trail_atr_distance: float = 1.0,
) -> dict[str, Any]:
    """Pure quantitative rule evaluator for Dynamic Position Management:
    - +1.0x ATR profit -> Move SL to Break-Even (open_price +/- 2 points buffer)
    - > +1.0x ATR profit -> Dynamic Trailing Stop (1.0x ATR behind current price)
    - +2.0x ATR profit -> Partial Close 50% of position volume (if not already partially closed)
    """
    side = str(pos.get("side") or "BUY").upper()
    open_px = float(pos.get("price_open") or 0.0)
    cur_px = float(pos.get("price_current") or open_px)
    cur_sl = float(pos.get("sl") or 0.0)
    vol = float(pos.get("volume") or 0.01)
    already_partial = bool(pos.get("partial_closed_50", False))
    safe_atr = max(float(atr or 0.0), point * 10.0)
    buffer_px = point * 2.0

    profit_dist = (cur_px - open_px) if side == "BUY" else (open_px - cur_px)
    atr_multiple = (profit_dist / safe_atr) if safe_atr > 0 else 0.0

    new_sl = cur_sl
    be_triggered = bool(pos.get("break_even_active", False))
    trailing_triggered = bool(pos.get("trailing_active", False))
    do_partial_50 = False
    partial_close_vol = 0.0
    remaining_vol = vol
    actions_taken: list[str] = []

    # 1. Break-Even check at +1.0x ATR
    if atr_multiple >= be_atr_trigger:
        if side == "BUY":
            be_sl = round(open_px + buffer_px, digits)
            if cur_sl < be_sl:
                new_sl = be_sl
                be_triggered = True
                actions_taken.append(f"BREAK_EVEN (+{atr_multiple:.2f}x ATR -> SL moved to {be_sl})")
        else:
            be_sl = round(open_px - buffer_px, digits)
            if cur_sl <= 0 or cur_sl > be_sl:
                new_sl = be_sl
                be_triggered = True
                actions_taken.append(f"BREAK_EVEN (+{atr_multiple:.2f}x ATR -> SL moved to {be_sl})")

        # 2. Dynamic Trailing Stop (ratchets behind current price by 1.0x ATR once beyond Break-Even)
        if side == "BUY":
            candidate_trail = round(cur_px - (safe_atr * trail_atr_distance), digits)
            if candidate_trail > new_sl and candidate_trail > open_px:
                new_sl = candidate_trail
                trailing_triggered = True
                actions_taken.append(f"TRAILING_STOP (+{atr_multiple:.2f}x ATR -> SL trailed to {new_sl})")
        else:
            candidate_trail = round(cur_px + (safe_atr * trail_atr_distance), digits)
            if (new_sl <= 0 or candidate_trail < new_sl) and candidate_trail < open_px:
                new_sl = candidate_trail
                trailing_triggered = True
                actions_taken.append(f"TRAILING_STOP (+{atr_multiple:.2f}x ATR -> SL trailed to {new_sl})")

    # 3. 50% Partial Close at +2.0x ATR
    if atr_multiple >= partial_close_atr_trigger and not already_partial and vol >= 0.02:
        close_half = round(math.floor((vol * 0.5) / 0.01) * 0.01, 2)
        if close_half < 0.01:
            close_half = 0.01
        if vol - close_half >= 0.01:
            do_partial_50 = True
            partial_close_vol = close_half
            remaining_vol = round(vol - close_half, 2)
            actions_taken.append(
                f"PARTIAL_CLOSE_50 (+{atr_multiple:.2f}x ATR -> Closed 50% = {close_half:.2f} lot, Remaining = {remaining_vol:.2f} lot)"
            )

    sl_changed = abs(new_sl - cur_sl) >= (point * 0.5) and new_sl > 0
    return {
        "modified": bool(sl_changed or do_partial_50),
        "sl_changed": sl_changed,
        "new_sl": round(new_sl, digits) if new_sl > 0 else cur_sl,
        "do_partial_50": do_partial_50,
        "partial_close_volume": partial_close_vol,
        "remaining_volume": remaining_vol,
        "break_even_active": be_triggered,
        "trailing_active": trailing_triggered,
        "partial_closed_50": already_partial or do_partial_50,
        "atr_multiple": round(atr_multiple, 2),
        "actions": actions_taken,
    }


def _inprocess_positions(symbol: str | None = None) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        return {
            "ok": False,
            "connected": False,
            "is_demo": False,
            "error": f"MT5 initialize failed: {mt5.last_error()}",
            "positions": [],
            "total_floating_pnl": 0.0,
            "floating_pnl_pct": 0.0,
            "account_balance": 0.0,
            "account_equity": 0.0,
        }
    try:
        acc = mt5.account_info()
        is_demo = bool(acc is not None and int(acc.trade_mode) == int(mt5.ACCOUNT_TRADE_MODE_DEMO))
        acc_balance = float(acc.balance) if acc is not None else 0.0
        acc_equity = float(acc.equity) if acc is not None else 0.0
        resolved_filter = _resolve_broker_symbol(mt5, symbol) if symbol else None

        mt5_positions = mt5.positions_get(symbol=resolved_filter) if resolved_filter else mt5.positions_get()
        raw_list: list[dict[str, Any]] = []
        seen_tickets: set[int] = set()

        if mt5_positions:
            for pos in mt5_positions:
                side = "BUY" if int(pos.type) == int(mt5.ORDER_TYPE_BUY) else "SELL"
                ticket_id = int(pos.ticket)
                seen_tickets.add(ticket_id)
                raw_list.append(
                    {
                        "ticket": ticket_id,
                        "symbol": str(pos.symbol),
                        "side": side,
                        "volume": float(pos.volume),
                        "price_open": float(pos.price_open),
                        "price_current": float(pos.price_current),
                        "sl": float(pos.sl),
                        "tp": float(pos.tp),
                        "profit": float(pos.profit),
                        "swap": float(getattr(pos, "swap", 0.0)),
                        "opened_at": datetime.fromtimestamp(int(pos.time), timezone.utc).isoformat(),
                        "comment": str(getattr(pos, "comment", "") or "NewEraQuantDemo"),
                        "source": "mt5_terminal",
                    }
                )

        # Also include any active Demo Paper Fallback positions (e.g., placed when broker market was closed)
        if is_demo:
            paper_list = _load_paper_demo_positions()
            for item in paper_list:
                t_id = int(item.get("ticket") or 0)
                if t_id in seen_tickets:
                    continue
                item_sym = str(item.get("symbol") or "XAUUSD")
                if resolved_filter and item_sym.upper() not in {resolved_filter.upper(), (symbol or "").upper()}:
                    continue
                raw_list.append(item)

        enriched: list[dict[str, Any]] = []
        for raw_pos in raw_list:
            pos_sym = str(raw_pos.get("symbol") or "XAUUSD")
            resolved_sym = _resolve_broker_symbol(mt5, pos_sym)
            info = mt5.symbol_info(resolved_sym)
            tick = mt5.symbol_info_tick(resolved_sym)
            point = float(info.point) if info and info.point else float(raw_pos.get("point") or 0.01)
            digits = int(info.digits) if info else int(raw_pos.get("digits") or 2)
            contract_size = float(getattr(info, "trade_contract_size", 100.0) if info else 100.0)
            tick_value = float(getattr(info, "trade_tick_value", 0.0) if info else 0.0)
            tick_size = float(getattr(info, "trade_tick_size", 0.0) if info else 0.0)

            if raw_pos.get("source") != "mt5_terminal" and tick is not None and not raw_pos.get("lock_simulated_price"):
                side = str(raw_pos.get("side") or "BUY").upper()
                live_px = float(tick.bid if side == "BUY" else tick.ask)
                if live_px > 0:
                    raw_pos = dict(raw_pos)
                    raw_pos["price_current"] = live_px

            enriched.append(
                _enrich_position_metrics(
                    raw_pos,
                    point=point,
                    digits=digits,
                    contract_size=contract_size,
                    tick_value=tick_value,
                    tick_size=tick_size,
                    account_balance=acc_balance,
                )
            )

        total_pnl = round(sum(float(p["profit"]) for p in enriched), 2)
        floating_pnl_pct = round((total_pnl / acc_balance) * 100.0, 2) if acc_balance > 0 else 0.0
        return {
            "ok": True,
            "connected": True,
            "is_demo": is_demo,
            "account_login": int(acc.login) if acc else 0,
            "account_balance": acc_balance,
            "account_equity": acc_equity,
            "positions": enriched,
            "count": len(enriched),
            "total_floating_pnl": total_pnl,
            "floating_pnl_pct": floating_pnl_pct,
        }
    finally:
        mt5.shutdown()


def _inprocess_manage_demo_positions(
    symbol: str | None = None,
    *,
    price_override: float | None = None,
    atr_override: float | None = None,
) -> dict[str, Any]:
    """Run Dynamic Position Management on all open Demo positions:
    - +1.0x ATR profit -> Break-Even SL
    - > +1.0x ATR profit -> Dynamic Trailing Stop
    - +2.0x ATR profit -> 50% Partial Close
    """
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        acc = _assert_demo_account(mt5)
        resolved_filter = _resolve_broker_symbol(mt5, symbol) if symbol else None
        modifications: list[dict[str, Any]] = []

        # 1. Manage live MT5 terminal positions on Demo account
        mt5_positions = mt5.positions_get(symbol=resolved_filter) if resolved_filter else mt5.positions_get()
        if mt5_positions:
            for pos in mt5_positions:
                pos_sym = str(pos.symbol)
                info = mt5.symbol_info(pos_sym)
                tick = mt5.symbol_info_tick(pos_sym)
                point = float(info.point) if info and info.point else 0.01
                digits = int(info.digits) if info else 2
                rates = mt5.copy_rates_from_pos(pos_sym, mt5.TIMEFRAME_M5, 0, 60)
                rows = list(rates[:-1]) if rates is not None and len(rates) > 15 else []
                atr_val = float(atr_override) if (atr_override and atr_override > 0) else (
                    _compute_atr(rows, 14) if rows else max(point * 40, float(pos.price_open) * 0.001)
                )
                side = "BUY" if int(pos.type) == int(mt5.ORDER_TYPE_BUY) else "SELL"
                cur_px = float(price_override) if (price_override and price_override > 0) else (
                    float(tick.bid if side == "BUY" else tick.ask) if tick else float(pos.price_current)
                )
                comment_str = str(getattr(pos, "comment", "") or "")
                raw_dict = {
                    "ticket": int(pos.ticket),
                    "symbol": pos_sym,
                    "side": side,
                    "volume": float(pos.volume),
                    "price_open": float(pos.price_open),
                    "price_current": cur_px,
                    "sl": float(pos.sl),
                    "tp": float(pos.tp),
                    "partial_closed_50": "PART50" in comment_str.upper(),
                }
                adj = compute_dynamic_position_rules_alias(
                    raw_dict,
                    atr=atr_val,
                    point=point,
                    digits=digits,
                )
                if not adj["modified"]:
                    continue

                if adj["do_partial_50"] and adj["partial_close_volume"] >= 0.01:
                    close_type = mt5.ORDER_TYPE_SELL if side == "BUY" else mt5.ORDER_TYPE_BUY
                    for filling_mode in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                        deal_req = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "position": int(pos.ticket),
                            "symbol": pos_sym,
                            "volume": float(adj["partial_close_volume"]),
                            "type": close_type,
                            "price": cur_px,
                            "deviation": 35,
                            "magic": DEMO_MAGIC_NUMBER,
                            "comment": "NewEra_PART50",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": filling_mode,
                        }
                        res = mt5.order_send(deal_req)
                        if res is not None and int(res.retcode) in {
                            int(mt5.TRADE_RETCODE_DONE),
                            int(mt5.TRADE_RETCODE_DONE_PARTIAL),
                        }:
                            break

                if adj["sl_changed"]:
                    sltp_req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": int(pos.ticket),
                        "symbol": pos_sym,
                        "sl": float(adj["new_sl"]),
                        "tp": float(pos.tp),
                    }
                    mt5.order_send(sltp_req)

                modifications.append(
                    {
                        "ticket": int(pos.ticket),
                        "symbol": pos_sym,
                        "side": side,
                        "action_type": "PARTIAL_50_AND_SL" if adj["do_partial_50"] else ("TRAILING_SL" if adj["trailing_active"] else "BREAK_EVEN"),
                        "new_sl": adj["new_sl"],
                        "new_volume": adj["remaining_volume"],
                        "atr_multiple": adj["atr_multiple"],
                        "description": " | ".join(adj["actions"]),
                    }
                )

        # 2. Manage Demo Paper Fallback positions (24/7 overlay positions)
        paper_positions = _load_paper_demo_positions()
        updated_paper: list[dict[str, Any]] = []
        paper_changed = False
        for item in paper_positions:
            item_sym = str(item.get("symbol") or "XAUUSD")
            if symbol and item_sym.upper() not in {symbol.strip().upper(), (resolved_filter or "").upper()}:
                updated_paper.append(item)
                continue

            resolved_sym = _resolve_broker_symbol(mt5, item_sym)
            info = mt5.symbol_info(resolved_sym)
            tick = mt5.symbol_info_tick(resolved_sym)
            point = float(info.point) if info and info.point else float(item.get("point") or 0.01)
            digits = int(info.digits) if info else int(item.get("digits") or 2)
            rates = mt5.copy_rates_from_pos(resolved_sym, mt5.TIMEFRAME_M5, 0, 60)
            rows = list(rates[:-1]) if rates is not None and len(rates) > 15 else []
            atr_val = float(atr_override) if (atr_override and atr_override > 0) else (
                _compute_atr(rows, 14) if rows else max(point * 40, float(item.get("price_open") or 2000.0) * 0.001)
            )
            side = str(item.get("side") or "BUY").upper()
            if price_override and price_override > 0:
                cur_px = float(price_override)
                item["price_current"] = cur_px
                item["lock_simulated_price"] = True
            elif tick is not None and not item.get("lock_simulated_price"):
                live_px = float(tick.bid if side == "BUY" else tick.ask)
                cur_px = live_px if live_px > 0 else float(item.get("price_current") or item.get("price_open") or 0.0)
                item["price_current"] = cur_px
            else:
                cur_px = float(item.get("price_current") or item.get("price_open") or 0.0)

            adj = compute_dynamic_position_adjustment(
                item,
                atr=atr_val,
                point=point,
                digits=digits,
            )
            if adj["modified"]:
                paper_changed = True
                item["sl"] = adj["new_sl"]
                item["volume"] = adj["remaining_volume"]
                item["break_even_active"] = adj["break_even_active"]
                item["trailing_active"] = adj["trailing_active"]
                item["partial_closed_50"] = adj["partial_closed_50"]
                modifications.append(
                    {
                        "ticket": int(item.get("ticket") or 0),
                        "symbol": item_sym,
                        "side": side,
                        "action_type": "PARTIAL_50_AND_SL" if adj["do_partial_50"] else ("TRAILING_SL" if adj["trailing_active"] else "BREAK_EVEN"),
                        "new_sl": adj["new_sl"],
                        "new_volume": adj["remaining_volume"],
                        "atr_multiple": adj["atr_multiple"],
                        "description": " | ".join(adj["actions"]),
                    }
                )
            updated_paper.append(item)

        if paper_changed:
            _save_paper_demo_positions(updated_paper)

        pos_snapshot = _inprocess_positions(symbol=symbol)
        return {
            "ok": True,
            "is_demo": True,
            "account_login": int(acc.login),
            "modified_count": len(modifications),
            "modifications": modifications,
            "positions": pos_snapshot.get("positions", []),
        }
    finally:
        mt5.shutdown()


compute_dynamic_position_rules_alias = compute_dynamic_position_adjustment


def _inprocess_execute_demo_order_unlocked(
    symbol: str,
    side: str,
    volume: float,
    *,
    sl: float = 0.0,
    tp: float = 0.0,
    comment: str = "NewEraQuantDemo",
    allow_paper_fallback: bool = False,
) -> dict[str, Any]:
    import MetaTrader5 as mt5

    norm_side = side.strip().upper()
    if norm_side not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported order side '{side}'; must be BUY or SELL.")

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        # Strict Demo Safety Gate: raises PermissionError if account is not DEMO
        acc = _assert_demo_account(mt5)
        resolved = _resolve_broker_symbol(mt5, symbol)
        mt5.symbol_select(resolved, True)

        info = mt5.symbol_info(resolved)
        tick = mt5.symbol_info_tick(resolved)
        digits = int(info.digits) if info else 2
        point = float(info.point) if info and info.point else 0.01
        contract_size = float(getattr(info, "trade_contract_size", 100.0) if info else 100.0)
        tick_value = float(getattr(info, "trade_tick_value", 0.0) if info else 0.0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) if info else 0.0)

        vol_min = float(getattr(info, "volume_min", 0.01) if info else 0.01)
        vol_max = float(getattr(info, "volume_max", 100.0) if info else 100.0)
        vol_step = float(getattr(info, "volume_step", 0.01) if info else 0.01)
        safe_vol = max(vol_min, min(vol_max, float(volume)))
        if vol_step > 0:
            safe_vol = round(round(safe_vol / vol_step) * vol_step, 2)

        ask = float(tick.ask) if tick and tick.ask else 0.0
        bid = float(tick.bid) if tick and tick.bid else 0.0
        if ask <= 0 or bid <= 0:
            rates = mt5.copy_rates_from_pos(resolved, mt5.TIMEFRAME_M5, 0, 2)
            fallback_px = float(rates[-1]["close"]) if rates is not None and len(rates) > 0 else 0.0
            ask = ask or fallback_px
            bid = bid or fallback_px

        raw_entry_price = ask if norm_side == "BUY" else bid
        try:
            raw_sl = float(sl)
            raw_tp = float(tp)
        except (TypeError, ValueError) as exc:
            raise ValueError("SL and TP must be numeric prices.") from exc

        # Check the user-supplied levels against the unrounded live quote first,
        # then check the exact rounded prices that will be sent to MT5.
        _validate_sl_tp_geometry(norm_side, raw_entry_price, raw_sl, raw_tp)
        entry_price = round(raw_entry_price, digits)
        sl_price = round(raw_sl, digits) if raw_sl > 0 else 0.0
        tp_price = round(raw_tp, digits) if raw_tp > 0 else 0.0

        # This is intentionally before any order_send and before the paper
        # fallback branch. Bad user levels are rejected, never repaired.
        _validate_sl_tp_geometry(norm_side, entry_price, sl_price, tp_price)

        terminal_positions = mt5.positions_get(symbol=resolved) or []
        paper_positions = _load_paper_demo_positions(fail_closed=True)
        open_count = count_open_positions_for_symbol(
            terminal_positions,
            paper_positions,
            symbol,
            resolved_symbol=resolved,
        )
        max_open = _configured_safety_limit(
            "NEWERA_MAX_OPEN_POSITIONS_PER_SYMBOL",
            MAX_OPEN_POSITIONS_PER_SYMBOL,
        )
        if open_count >= max_open:
            raise RuntimeError(
                f"SAFETY BLOCK: {resolved} already has {open_count} open position(s); "
                f"the limit is {max_open} per symbol."
            )

        history_deals_get = getattr(mt5, "history_deals_get", None)
        if not callable(history_deals_get):
            raise RuntimeError("SAFETY BLOCK: MT5 trade history cannot be inspected.")
        now_utc = datetime.now(timezone.utc)
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            terminal_history = history_deals_get(day_start, now_utc + timedelta(seconds=1))
        except Exception as exc:
            raise RuntimeError("SAFETY BLOCK: Could not inspect MT5 trade history safely.") from exc
        if terminal_history is None:
            raise RuntimeError("SAFETY BLOCK: MT5 trade history could not be read safely.")
        daily_count = count_daily_demo_trades(terminal_history, now=now_utc)
        daily_count += count_daily_demo_trades(terminal_positions, now=now_utc)
        daily_count += count_daily_demo_trades(paper_positions, now=now_utc)
        daily_count += count_daily_demo_trades(_load_paper_demo_history(fail_closed=True), now=now_utc)
        max_daily = _configured_safety_limit("NEWERA_MAX_DAILY_DEMO_TRADES", MAX_DAILY_DEMO_TRADES)
        if daily_count >= max_daily:
            raise RuntimeError(
                f"SAFETY BLOCK: daily Demo trade cap reached ({daily_count}/{max_daily}); "
                "no new order was sent."
            )

        order_type = mt5.ORDER_TYPE_BUY if norm_side == "BUY" else mt5.ORDER_TYPE_SELL
        filling_Candidates = [
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]
        last_result = None
        for filling_mode in filling_Candidates:
            req: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": resolved,
                "volume": safe_vol,
                "type": order_type,
                "price": entry_price,
                "deviation": 35,
                "magic": DEMO_MAGIC_NUMBER,
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode,
            }
            if sl_price > 0:
                req["sl"] = sl_price
            if tp_price > 0:
                req["tp"] = tp_price

            res = mt5.order_send(req)
            last_result = res
            if res is not None and int(res.retcode) in {
                int(mt5.TRADE_RETCODE_DONE),
                int(mt5.TRADE_RETCODE_PLACED),
                int(mt5.TRADE_RETCODE_DONE_PARTIAL),
            }:
                exec_price = float(res.price) if float(res.price) > 0 else entry_price
                ticket_id = int(res.order or res.deal or int(time.time()))
                pos_payload = _enrich_position_metrics(
                    {
                        "ticket": ticket_id,
                        "symbol": resolved,
                        "side": norm_side,
                        "volume": safe_vol,
                        "price_open": exec_price,
                        "price_current": bid if norm_side == "BUY" else ask,
                        "sl": sl_price,
                        "tp": tp_price,
                        "profit": 0.0,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "comment": comment,
                        "source": "mt5_terminal",
                    },
                    point=point,
                    digits=digits,
                    contract_size=contract_size,
                    tick_value=tick_value,
                    tick_size=tick_size,
                )
                return {
                    "ok": True,
                    "is_demo": True,
                    "account_login": int(acc.login),
                    "execution_source": "mt5_demo_terminal",
                    "demo_overlay": False,
                    "paper_fallback": False,
                    "fallback_requested": bool(allow_paper_fallback),
                    "retcode": int(res.retcode),
                    "retcode_comment": str(getattr(res, "comment", "Executed on MT5 Demo")),
                    "position": pos_payload,
                }
            if res is not None and _is_order_safety_rejection(mt5, int(res.retcode)):
                break
            # If market is closed (10018), no need to retry other filling modes
            if res is not None and int(res.retcode) == int(getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)):
                break

        retcode = int(last_result.retcode) if last_result is not None else -1
        ret_comment = str(getattr(last_result, "comment", "") if last_result is not None else mt5.last_error())

        if _is_order_safety_rejection(mt5, retcode):
            raise RuntimeError(
                f"MT5 Demo order_send rejected a safety check (retcode={retcode}, comment={ret_comment}); "
                "paper fallback was not activated."
            )

        if allow_paper_fallback:
            ticket_id = int(time.time() * 1000) % 900000000 + 100000000
            raw_paper = {
                "ticket": ticket_id,
                "symbol": resolved,
                "side": norm_side,
                "volume": safe_vol,
                "price_open": entry_price,
                "price_current": bid if norm_side == "BUY" else ask,
                "sl": sl_price,
                "tp": tp_price,
                "profit": 0.0,
                "point": point,
                "digits": digits,
                "break_even_active": False,
                "trailing_active": False,
                "partial_closed_50": False,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "comment": f"{comment} (DemoOverlay)",
                "source": "mt5_demo_paper_fallback",
            }
            existing = paper_positions
            existing.append(raw_paper)
            if not _save_paper_demo_positions(existing):
                raise RuntimeError("SAFETY BLOCK: Demo paper fallback could not be persisted safely.")

            pos_payload = _enrich_position_metrics(
                raw_paper,
                point=point,
                digits=digits,
                contract_size=contract_size,
                tick_value=tick_value,
                tick_size=tick_size,
            )
            return {
                "ok": True,
                "is_demo": True,
                "account_login": int(acc.login),
                "execution_source": "mt5_demo_paper_fallback",
                "demo_overlay": True,
                "paper_fallback": True,
                "fallback_requested": True,
                "retcode": retcode,
                "retcode_comment": (
                    f"MT5 Demo broker returned {retcode} ({ret_comment}); "
                    f"activated explicit Demo paper fallback overlay position #{ticket_id}"
                ),
                "position": pos_payload,
            }

        raise RuntimeError(
            f"MT5 Demo order_send rejected (retcode={retcode}, comment={ret_comment}); "
            "paper fallback is disabled unless allow_paper_fallback=true."
        )
    finally:
        mt5.shutdown()


def _inprocess_execute_demo_order(
    symbol: str,
    side: str,
    volume: float,
    *,
    sl: float = 0.0,
    tp: float = 0.0,
    comment: str = "NewEraQuantDemo",
    allow_paper_fallback: bool = False,
) -> dict[str, Any]:
    """Serialize direct execution so duplicate checks cover check-and-open as one action."""
    with _DEMO_EXECUTION_LOCK:
        return _inprocess_execute_demo_order_unlocked(
            symbol=symbol,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            comment=comment,
            allow_paper_fallback=allow_paper_fallback,
        )


def _inprocess_close_demo_positions(
    symbol: str | None = None,
    ticket: int | None = None,
) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        acc = _assert_demo_account(mt5)
        resolved_filter = _resolve_broker_symbol(mt5, symbol) if symbol else None
        closed_tickets: list[int] = []
        errors: list[str] = []

        # 1. Close matching MT5 terminal demo positions
        mt5_positions = mt5.positions_get(symbol=resolved_filter) if resolved_filter else mt5.positions_get()
        if mt5_positions:
            for pos in mt5_positions:
                pos_ticket = int(pos.ticket)
                if ticket is not None and pos_ticket != int(ticket):
                    continue
                pos_sym = str(pos.symbol)
                tick = mt5.symbol_info_tick(pos_sym)
                is_buy = int(pos.type) == int(mt5.ORDER_TYPE_BUY)
                close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
                close_price = (
                    float(tick.bid if is_buy else tick.ask)
                    if tick is not None
                    else float(pos.price_current)
                )
                closed_this = False
                for filling_mode in (
                    mt5.ORDER_FILLING_IOC,
                    mt5.ORDER_FILLING_FOK,
                    mt5.ORDER_FILLING_RETURN,
                ):
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "position": pos_ticket,
                        "symbol": pos_sym,
                        "volume": float(pos.volume),
                        "type": close_type,
                        "price": close_price,
                        "deviation": 35,
                        "magic": DEMO_MAGIC_NUMBER,
                        "comment": "NewEraCloseDemo",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": filling_mode,
                    }
                    res = mt5.order_send(req)
                    if res is not None and int(res.retcode) in {
                        int(mt5.TRADE_RETCODE_DONE),
                        int(mt5.TRADE_RETCODE_DONE_PARTIAL),
                    }:
                        closed_tickets.append(pos_ticket)
                        closed_this = True
                        break
                if not closed_this:
                    errors.append(f"Could not close MT5 ticket #{pos_ticket}")

        # 2. Close matching Demo Paper Fallback positions
        paper_positions = _load_paper_demo_positions()
        remaining_paper: list[dict[str, Any]] = []
        for item in paper_positions:
            item_ticket = int(item.get("ticket") or 0)
            item_sym = str(item.get("symbol") or "XAUUSD")
            match_ticket = ticket is None or item_ticket == int(ticket)
            # If a specific ticket is provided, close that ticket regardless of symbol filter
            match_symbol = (
                ticket is not None
                or symbol is None
                or item_sym.upper() == symbol.strip().upper()
                or (resolved_filter is not None and item_sym.upper() == resolved_filter.upper())
            )
            if match_ticket and match_symbol:
                closed_tickets.append(item_ticket)
                try:
                    resolved_sym = _resolve_broker_symbol(mt5, item_sym)
                    info = mt5.symbol_info(resolved_sym)
                    tick = mt5.symbol_info_tick(resolved_sym)
                    point = float(info.point) if info and info.point else float(item.get("point") or 0.01)
                    digits = int(info.digits) if info else int(item.get("digits") or 2)
                    contract_size = float(getattr(info, "trade_contract_size", 100.0) if info else 100.0)
                    tick_value = float(getattr(info, "trade_tick_value", 0.0) if info else 0.0)
                    tick_size = float(getattr(info, "trade_tick_size", 0.0) if info else 0.0)
                    side_str = str(item.get("side") or "BUY").upper()
                    exit_px = (
                        float(tick.bid if side_str == "BUY" else tick.ask)
                        if tick is not None and not item.get("lock_simulated_price")
                        else float(item.get("price_current") or item.get("price_open") or 0.0)
                    )
                    if exit_px > 0:
                        item["price_current"] = exit_px
                    enriched_closed = _enrich_position_metrics(
                        item,
                        point=point,
                        digits=digits,
                        contract_size=contract_size,
                        tick_value=tick_value,
                        tick_size=tick_size,
                    )
                    _append_paper_demo_history(
                        {
                            "ticket": item_ticket,
                            "deal_id": item_ticket,
                            "symbol": item_sym,
                            "side": side_str,
                            "volume": float(enriched_closed.get("volume") or 0.01),
                            "entry_price": float(enriched_closed.get("price_open") or 0.0),
                            "exit_price": float(enriched_closed.get("price_current") or 0.0),
                            "open_time": str(item.get("opened_at") or datetime.now(timezone.utc).isoformat()),
                            "close_time": datetime.now(timezone.utc).isoformat(),
                            "realized_pnl": float(enriched_closed.get("profit") or 0.0),
                            "digits": digits,
                            "comment": str(item.get("comment") or "NewEraDemoClosed"),
                            "source": "mt5_demo_paper_fallback",
                        }
                    )
                except Exception:
                    pass
            else:
                remaining_paper.append(item)

        if len(remaining_paper) != len(paper_positions):
            _save_paper_demo_positions(remaining_paper)

        return {
            "ok": True,
            "is_demo": True,
            "account_login": int(acc.login),
            "closed_tickets": closed_tickets,
            "closed_count": len(closed_tickets),
            "errors": errors,
        }
    finally:
        mt5.shutdown()


def _inprocess_trade_history(days: int = 30, limit: int = 150) -> dict[str, Any]:
    """Fetch closed trades from MT5 (`history_deals_get`) + closed Demo Overlay trades and compute stats."""
    import MetaTrader5 as mt5

    if not mt5.initialize():
        return {
            "ok": False,
            "connected": False,
            "error": f"MT5 initialize failed: {mt5.last_error()}",
            "trades": [],
            "summary": {
                "total_realized_pnl": 0.0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        }
    try:
        acc = mt5.account_info()
        now_dt = datetime.now(timezone.utc)
        from_dt = now_dt - timedelta(days=max(1, int(days)))
        deals = mt5.history_deals_get(from_dt, now_dt + timedelta(days=1))

        entry_by_pos: dict[int, dict[str, Any]] = {}
        closed_trades: list[dict[str, Any]] = []
        seen_tickets: set[int] = set()

        deal_entry_in = int(getattr(mt5, "DEAL_ENTRY_IN", 0))
        deal_entry_out = int(getattr(mt5, "DEAL_ENTRY_OUT", 1))
        deal_entry_inout = int(getattr(mt5, "DEAL_ENTRY_INOUT", 2))
        deal_entry_out_by = int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3))
        deal_type_buy = int(getattr(mt5, "DEAL_TYPE_BUY", 0))
        deal_type_sell = int(getattr(mt5, "DEAL_TYPE_SELL", 1))

        if deals:
            for d in deals:
                d_type = int(getattr(d, "type", -1))
                if d_type not in {deal_type_buy, deal_type_sell}:
                    continue
                pos_id = int(getattr(d, "position_id", 0) or getattr(d, "order", 0) or getattr(d, "ticket", 0))
                d_entry = int(getattr(d, "entry", -1))
                if d_entry == deal_entry_in and pos_id > 0:
                    entry_by_pos[pos_id] = {
                        "price": float(getattr(d, "price", 0.0)),
                        "time": datetime.fromtimestamp(int(getattr(d, "time", 0)), timezone.utc).isoformat(),
                        "side": "BUY" if d_type == deal_type_buy else "SELL",
                        "volume": float(getattr(d, "volume", 0.01)),
                    }

            for d in deals:
                d_type = int(getattr(d, "type", -1))
                if d_type not in {deal_type_buy, deal_type_sell}:
                    continue
                d_entry = int(getattr(d, "entry", -1))
                if d_entry not in {deal_entry_out, deal_entry_inout, deal_entry_out_by}:
                    continue
                pos_id = int(getattr(d, "position_id", 0) or getattr(d, "order", 0) or getattr(d, "ticket", 0))
                deal_ticket = int(getattr(d, "ticket", 0))
                sym = str(getattr(d, "symbol", "XAUUSD") or "XAUUSD")
                info = mt5.symbol_info(sym)
                digits = int(info.digits) if info else 2

                entry_meta = entry_by_pos.get(pos_id, {})
                # Note: an OUT deal of type SELL closes a BUY position, and vice versa
                orig_side = str(entry_meta.get("side") or ("BUY" if d_type == deal_type_sell else "SELL"))
                exit_px = float(getattr(d, "price", 0.0))
                entry_px = float(entry_meta.get("price") or exit_px)
                close_iso = datetime.fromtimestamp(int(getattr(d, "time", 0)), timezone.utc).isoformat()
                open_iso = str(entry_meta.get("time") or close_iso)
                realized = (
                    float(getattr(d, "profit", 0.0))
                    + float(getattr(d, "swap", 0.0))
                    + float(getattr(d, "commission", 0.0))
                    + float(getattr(d, "fee", 0.0))
                )
                seen_tickets.add(deal_ticket)
                seen_tickets.add(pos_id)
                closed_trades.append(
                    {
                        "ticket": pos_id or deal_ticket,
                        "deal_id": deal_ticket,
                        "symbol": sym,
                        "side": orig_side,
                        "volume": round(float(getattr(d, "volume", 0.01)), 2),
                        "entry_price": round(entry_px, digits),
                        "exit_price": round(exit_px, digits),
                        "open_time": open_iso,
                        "close_time": close_iso,
                        "realized_pnl": round(realized, 2),
                        "digits": digits,
                        "comment": str(getattr(d, "comment", "") or ""),
                        "source": "mt5_terminal",
                    }
                )

        # Merge closed Demo Paper Overlay trades
        for p_hist in _load_paper_demo_history():
            t_id = int(p_hist.get("ticket") or p_hist.get("deal_id") or 0)
            if t_id in seen_tickets and t_id > 0:
                continue
            closed_trades.append(p_hist)

        closed_trades.sort(key=lambda x: str(x.get("close_time") or ""), reverse=True)
        if limit > 0:
            closed_trades = closed_trades[:limit]

        total_trades = len(closed_trades)
        total_realized_pnl = round(sum(float(t.get("realized_pnl") or 0.0) for t in closed_trades), 2)
        winning_trades = sum(1 for t in closed_trades if float(t.get("realized_pnl") or 0.0) > 0)
        losing_trades = sum(1 for t in closed_trades if float(t.get("realized_pnl") or 0.0) < 0)
        win_rate_pct = round((winning_trades / total_trades) * 100.0, 1) if total_trades > 0 else 0.0
        gross_profit = round(
            sum(float(t.get("realized_pnl") or 0.0) for t in closed_trades if float(t.get("realized_pnl") or 0.0) > 0),
            2,
        )
        gross_loss = round(
            abs(sum(float(t.get("realized_pnl") or 0.0) for t in closed_trades if float(t.get("realized_pnl") or 0.0) < 0)),
            2,
        )
        if gross_loss > 0:
            profit_factor = round(gross_profit / gross_loss, 2)
        elif gross_profit > 0:
            profit_factor = round(gross_profit, 2)
        else:
            profit_factor = 0.0

        return {
            "ok": True,
            "connected": True,
            "account_login": int(acc.login) if acc else 0,
            "trades": closed_trades,
            "summary": {
                "total_realized_pnl": total_realized_pnl,
                "win_rate_pct": win_rate_pct,
                "profit_factor": profit_factor,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "gross_profit": gross_profit,
                "gross_loss": gross_loss,
            },
        }
    finally:
        mt5.shutdown()


def _call_bridge_cli(args: list[str], timeout: float = 15.0) -> dict[str, Any]:
    mt5_py = find_mt5_python()
    if not mt5_py:
        raise RuntimeError(
            "MetaTrader5 Python environment not found. Install MetaTrader5 in Python 3.11-3.14."
        )
    proc = subprocess.run(
        [mt5_py, str(Path(__file__).resolve()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(err_msg)
    return json.loads(proc.stdout)


def get_mt5_status(max_symbols: int = 250) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_status(max_symbols=max_symbols)
    except ImportError:
        try:
            return _call_bridge_cli(["--action", "status", "--max-symbols", str(max_symbols)])
        except Exception as exc:
            return {
                "connected": False,
                "error": str(exc),
                "account": None,
                "symbols": list(PRIORITY_SYMBOLS[:10]),
                "visible_symbols": [],
                "total_broker_symbols": 0,
            }


def get_mt5_symbol_snapshot(symbol: str) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_symbol_snapshot(symbol)
    except ImportError:
        return _call_bridge_cli(["--action", "symbol", "--symbol", symbol])


def get_mt5_market_book(symbol: str, levels: int = 10) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_market_book(symbol, levels=levels)
    except ImportError:
        return _call_bridge_cli(["--action", "market-book", "--symbol", symbol, "--levels", str(levels)])


def get_mt5_open_positions(symbol: str | None = None) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_positions(symbol=symbol)
    except ImportError:
        cli_args = ["--action", "positions"]
        if symbol:
            cli_args.extend(["--symbol", symbol])
        try:
            return _call_bridge_cli(cli_args)
        except Exception as exc:
            return {
                "ok": False,
                "connected": False,
                "is_demo": False,
                "error": str(exc),
                "positions": [],
                "count": 0,
                "total_floating_pnl": 0.0,
                "floating_pnl_pct": 0.0,
            }


def get_mt5_trade_history(days: int = 30, limit: int = 150) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_trade_history(days=days, limit=limit)
    except ImportError:
        try:
            return _call_bridge_cli(["--action", "history", "--days", str(days), "--limit", str(limit)])
        except Exception as exc:
            return {
                "ok": False,
                "connected": False,
                "error": str(exc),
                "trades": [],
                "summary": {
                    "total_realized_pnl": 0.0,
                    "win_rate_pct": 0.0,
                    "profit_factor": 0.0,
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                },
            }


def execute_mt5_demo_order(
    symbol: str,
    side: str,
    volume: float,
    *,
    sl: float = 0.0,
    tp: float = 0.0,
    comment: str = "NewEraQuantDemo",
    allow_paper_fallback: bool = False,
) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_execute_demo_order(
            symbol=symbol,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            comment=comment,
            allow_paper_fallback=allow_paper_fallback,
        )
    except ImportError:
        cli_args = [
            "--action",
            "execute-demo",
            "--symbol",
            symbol,
            "--side",
            side,
            "--volume",
            str(volume),
            "--sl",
            str(sl),
            "--tp",
            str(tp),
            "--comment",
            comment,
        ]
        if allow_paper_fallback:
            cli_args.append("--paper-fallback")
        else:
            cli_args.append("--no-paper-fallback")
        return _call_bridge_cli(cli_args, timeout=15.0)


def close_mt5_demo_positions(
    symbol: str | None = None,
    ticket: int | None = None,
) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_close_demo_positions(symbol=symbol, ticket=ticket)
    except ImportError:
        cli_args = ["--action", "close-demo"]
        if symbol:
            cli_args.extend(["--symbol", symbol])
        if ticket is not None:
            cli_args.extend(["--ticket", str(ticket)])
        return _call_bridge_cli(cli_args, timeout=15.0)


def manage_mt5_demo_positions(
    symbol: str | None = None,
    *,
    price_override: float | None = None,
    atr_override: float | None = None,
) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_manage_demo_positions(
            symbol=symbol,
            price_override=price_override,
            atr_override=atr_override,
        )
    except ImportError:
        cli_args = ["--action", "manage-demo"]
        if symbol:
            cli_args.extend(["--symbol", symbol])
        if price_override is not None:
            cli_args.extend(["--price-override", str(price_override)])
        if atr_override is not None:
            cli_args.extend(["--atr-override", str(atr_override)])
        return _call_bridge_cli(cli_args, timeout=15.0)


def fetch_mt5_bars_to_csv(
    symbol: str,
    output_csv: Path,
    *,
    timeframe: str = "M5",
    bars: int = 5000,
) -> dict[str, Any]:
    try:
        import MetaTrader5  # noqa: F401

        return _inprocess_export_bars(symbol, output_csv, timeframe=timeframe, bars=bars)
    except ImportError:
        return _call_bridge_cli(
            [
                "--action",
                "export-bars",
                "--symbol",
                symbol,
                "--timeframe",
                timeframe,
                "--bars",
                str(bars),
                "--output",
                str(output_csv),
            ],
            timeout=25.0,
        )


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal MT5 JSON bridge (Demo-only execution)")
    parser.add_argument(
        "--action",
        choices=[
            "status",
            "symbol",
            "market-book",
            "export-bars",
            "positions",
            "history",
            "execute-demo",
            "close-demo",
            "manage-demo",
        ],
        required=True,
    )
    parser.add_argument("--symbol", default="")
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--max-symbols", type=int, default=250)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--output", type=Path, default=Path("TradingAgentsData/market/xauusd_m5.csv"))
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--volume", type=float, default=0.01)
    parser.add_argument("--sl", type=float, default=0.0)
    parser.add_argument("--tp", type=float, default=0.0)
    parser.add_argument("--ticket", type=int, default=None)
    parser.add_argument("--comment", default="NewEraQuantDemo")
    parser.add_argument("--paper-fallback", dest="paper_fallback", action="store_true")
    # Keep the old internal flag accepted while making the CLI default fail closed.
    parser.add_argument("--no-paper-fallback", dest="paper_fallback", action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(paper_fallback=False)
    parser.add_argument("--price-override", type=float, default=None)
    parser.add_argument("--atr-override", type=float, default=None)
    args = parser.parse_args(argv)

    try:
        sym = args.symbol.strip() or None
        if args.action == "status":
            payload = _inprocess_status(max_symbols=args.max_symbols)
        elif args.action == "symbol":
            payload = _inprocess_symbol_snapshot(sym or "XAUUSD")
        elif args.action == "market-book":
            payload = _inprocess_market_book(sym or "XAUUSD", levels=args.levels)
        elif args.action == "positions":
            payload = _inprocess_positions(symbol=sym)
        elif args.action == "history":
            payload = _inprocess_trade_history(days=args.days, limit=args.limit)
        elif args.action == "execute-demo":
            payload = _inprocess_execute_demo_order(
                symbol=sym or "XAUUSD",
                side=args.side,
                volume=args.volume,
                sl=args.sl,
                tp=args.tp,
                comment=args.comment,
                allow_paper_fallback=args.paper_fallback,
            )
        elif args.action == "close-demo":
            payload = _inprocess_close_demo_positions(symbol=sym, ticket=args.ticket)
        elif args.action == "manage-demo":
            payload = _inprocess_manage_demo_positions(
                symbol=sym,
                price_override=args.price_override,
                atr_override=args.atr_override,
            )
        else:
            payload = _inprocess_export_bars(
                sym or "XAUUSD",
                args.output,
                timeframe=args.timeframe,
                bars=args.bars,
            )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())
