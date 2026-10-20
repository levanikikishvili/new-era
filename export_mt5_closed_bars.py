"""Read-only MT5 candle exporter for the Kronos Matrix research runner."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path


def resolve_symbol(mt5, requested: str) -> str:
    symbols = mt5.symbols_get() or []
    exact = [item.name for item in symbols if item.name.upper() == requested.upper()]
    if exact:
        return exact[0]
    candidates = [item.name for item in symbols if "XAU" in item.name.upper() or "GOLD" in item.name.upper()]
    if not candidates:
        raise RuntimeError(f"could not resolve gold symbol for {requested}")
    return sorted(candidates, key=len)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export closed MT5 candles; never sends orders")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--output", type=Path, default=Path("TradingAgentsData/market/xauusd_m5.csv"))
    args = parser.parse_args(argv)
    if args.bars < 600:
        parser.error("--bars must be at least 600")

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        print(
            "MetaTrader5 is not installed in this Python environment. Use supported "
            "64-bit Python 3.11-3.14 with the MT5 terminal.",
            file=sys.stderr,
        )
        return 2

    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}", file=sys.stderr)
        return 2
    try:
        symbol = resolve_symbol(mt5, args.symbol)
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"could not select {symbol}: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, args.bars + 1)
        if rates is None or len(rates) < args.bars:
            raise RuntimeError(f"not enough M5 bars for {symbol}: {mt5.last_error()}")
        # Position 0 is the currently forming candle. Exclude it so research
        # and backtests never observe an unfinished bar.
        rows = list(rates[:-1])[-args.bars :]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("timestamp", "open", "high", "low", "close", "volume", "amount"))
            for row in rows:
                timestamp = datetime.fromtimestamp(int(row["time"]), UTC).isoformat()
                volume = float(row["tick_volume"])
                close = float(row["close"])
                writer.writerow(
                    (
                        timestamp,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        close,
                        volume,
                        volume * close,
                    )
                )
        print(f"Exported {len(rows)} closed M5 bars for {symbol} to {args.output}")
        return 0
    except Exception as exc:
        print(f"MT5 export stopped safely: {exc}", file=sys.stderr)
        return 2
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
