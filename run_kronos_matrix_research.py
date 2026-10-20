"""Run the Kronos + Matrix + historical-analogue research layer.

The command produces a JSON report and a small CSV gate for the reusable MT5
execution EA.  It never imports MetaTrader5 and never sends an order.  A missing
dependency, missing spread, stale input, model failure, or disagreement writes
an ABSTAIN gate instead of guessing.

Typical live-data flow:

1. export closed XAUUSD M5 candles from MT5 to CSV;
2. run this script in the Kronos Docker environment;
3. copy the resulting gate with ``sync_kronos_gate_to_mt5.ps1``;
4. attach the reusable EA with the Kronos Matrix preset on demo only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import timezone
from pathlib import Path
from typing import Any

from kronos_matrix_strategy import (
    MatrixConfig,
    STRATEGY_ID,
    atr_series,
    combine_decisions,
    decision_to_dict,
    matrix_signal,
    analog_signal,
    forecast_signal,
    write_gate_csv,
)


def epoch_now() -> int:
    return int(time.time())


def safe_abstain(args: argparse.Namespace, reason: str) -> int:
    generated = epoch_now()
    expires = generated + max(1, args.gate_ttl_minutes) * 60
    payload = {
        "generated_epoch": generated,
        "strategy": STRATEGY_ID,
        "symbol": args.symbol,
        "action": "ABSTAIN",
        "confidence": 0.0,
        "execution_allowed": False,
        "real_account_allowed": False,
        "safe_failure": True,
        "reason": reason,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    write_gate_csv(
        args.gate_output,
        symbol=args.symbol,
        action="ABSTAIN",
        confidence=0.0,
        generated_epoch=generated,
        expires_epoch=expires,
    )
    print(f"Kronos Matrix stopped safely: {reason}", file=sys.stderr)
    print(f"Wrote ABSTAIN gate: {args.gate_output}", file=sys.stderr)
    return 2


def parse_timestamp(series, pd):
    if getattr(series.dtype, "kind", "") in "iu":
        sample = float(series.dropna().iloc[0]) if not series.dropna().empty else 0.0
        unit = "ms" if sample > 10_000_000_000 else "s"
        return pd.to_datetime(series, unit=unit, utc=True)
    return pd.to_datetime(series, utc=True)


def load_ohlcv_csv(path: Path, pd):
    if not path.exists():
        raise RuntimeError(f"input CSV does not exist: {path}")
    frame = pd.read_csv(path)
    rename: dict[str, str] = {}
    for column in frame.columns:
        normalized = str(column).strip().lower()
        if normalized in {"time", "timestamp", "timestamps", "datetime", "date"}:
            rename[column] = "timestamp"
        elif normalized in {"open", "high", "low", "close", "volume", "amount"}:
            rename[column] = normalized
    frame = frame.rename(columns=rename)
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"input CSV is missing columns: {', '.join(missing)}")
    frame["timestamp"] = parse_timestamp(frame["timestamp"], pd)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp")
    frame = frame.set_index("timestamp")
    if len(frame) < 600:
        raise RuntimeError(f"need at least 600 closed bars, received {len(frame)}")
    return frame[["open", "high", "low", "close", "volume", "amount"]]


def resample_frame(frame, rule: str, pd, before=None):
    # MT5 exports candle-open timestamps.  A higher-timeframe bar whose start
    # equals the current M5 timestamp is still forming, so keep only groups
    # strictly before the current anchor.
    aggregated = frame.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum",
        }
    )
    aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])
    if before is not None:
        aggregated = aggregated[aggregated.index < before.floor(rule)]
    return aggregated


def records(frame) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for row in frame.reset_index().to_dict("records"):
        result.append(
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
                "amount": float(row.get("amount", 0.0) or 0.0),
            }
        )
    return result


def load_kronos_predictor(args: argparse.Namespace):
    try:
        import pandas as pd
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "Kronos runtime dependencies are missing. Use the Kronos Docker image "
            "or install its pinned requirements in a supported Python environment."
        ) from exc

    kronos_root = args.kronos_root.resolve()
    if not (kronos_root / "model" / "__init__.py").exists():
        raise RuntimeError(f"Kronos source was not found under {kronos_root}")
    sys.path.insert(0, str(kronos_root))
    try:
        from model import Kronos, KronosPredictor, KronosTokenizer
    except ImportError as exc:
        raise RuntimeError(f"could not import Kronos model from {kronos_root}: {exc}") from exc

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
    device = args.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
    tokenizer = KronosTokenizer.from_pretrained(args.tokenizer)
    model = Kronos.from_pretrained(args.model)
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=args.max_context)
    return predictor, pd, device


def kronos_forecast(frame, args: argparse.Namespace, pd):
    predictor, _, device = load_kronos_predictor(args)
    lookback = min(args.lookback, len(frame), args.max_context)
    x_df = frame.iloc[-lookback:][["open", "high", "low", "close", "volume", "amount"]]
    x_timestamp = x_df.index.to_series()
    interval = pd.Timedelta(minutes=args.bar_minutes)
    y_timestamp = pd.Series(
        [x_timestamp.iloc[-1] + interval * step for step in range(1, args.pred_len + 1)]
    )
    predicted = predictor.predict(
        df=x_df.reset_index(drop=True),
        x_timestamp=x_timestamp.reset_index(drop=True),
        y_timestamp=y_timestamp,
        pred_len=args.pred_len,
        T=args.temperature,
        top_p=args.top_p,
        sample_count=args.sample_count,
        verbose=args.verbose,
    )
    return predicted, device, lookback


def run(args: argparse.Namespace) -> int:
    if args.dry_run:
        safe_abstain(args, "dry-run: no market data or Kronos dependency was loaded")
        print("Kronos Matrix dry-run OK; no model or order was used.")
        return 0
    try:
        import pandas as pd
    except ImportError as exc:
        return safe_abstain(args, f"pandas unavailable: {exc}")

    try:
        base = load_ohlcv_csv(args.input_csv, pd)
        anchor = base.index[-1]
        frames = {
            "M5": base,
            "M15": resample_frame(base, "15min", pd, anchor),
            "H1": resample_frame(base, "1h", pd, anchor),
            "H4": resample_frame(base, "4h", pd, anchor),
        }
        config = MatrixConfig(
            breakout_window=args.breakout_window,
            analog_window=args.analog_window,
            analog_horizon=args.analog_horizon,
            analog_neighbors=args.analog_neighbors,
            analog_min_samples=args.analog_min_samples,
            analog_min_win_rate=args.analog_min_win_rate,
            forecast_min_move_atr=args.forecast_min_move_atr,
            forecast_min_agreement=args.forecast_min_agreement,
            gate_ttl_minutes=args.gate_ttl_minutes,
        )
        matrix = matrix_signal({key: records(value) for key, value in frames.items()}, config)
        m5_records = records(frames["M5"])
        analog = analog_signal(m5_records, config)
        m5_atr = atr_series(m5_records, config.atr_period)[-1]
        if args.dry_run:
            forecast_values = []
            device = "dry-run"
            lookback = 0
        else:
            predicted, device, lookback = kronos_forecast(frames["M5"], args, pd)
            forecast_values = [float(value) for value in predicted["close"].tolist()]
        current_close = float(frames["M5"]["close"].iloc[-1])
        forecast = forecast_signal(current_close, forecast_values, m5_atr, config)
        if args.dry_run:
            hard_blocks = ["dry-run: Kronos inference was not executed"]
        else:
            hard_blocks = []
        if args.spread_points is None:
            cost_ok = False
            hard_blocks.append("spread is missing")
        else:
            spread_price = args.spread_points * args.point
            cost_ok = args.spread_points <= args.max_spread_points and (
                m5_atr <= 0 or spread_price / m5_atr <= args.max_spread_to_atr
            )
            if not cost_ok:
                hard_blocks.append("spread exceeds configured cost gate")
        decision = combine_decisions(
            matrix,
            analog,
            forecast,
            cost_ok=cost_ok,
            hard_blocks=hard_blocks,
            config=config,
        )
        generated = epoch_now()
        expires = generated + max(1, args.gate_ttl_minutes) * 60
        payload: dict[str, Any] = {
            "generated_epoch": generated,
            "expires_epoch": expires,
            "strategy": STRATEGY_ID,
            "symbol": args.symbol,
            "data_source": str(args.input_csv),
            "last_bar": str(frames["M5"].index[-1]),
            "model": args.model,
            "tokenizer": args.tokenizer,
            "device": device,
            "lookback": lookback,
            "pred_len": args.pred_len,
            "execution_allowed": False,
            "real_account_allowed": False,
            "action": decision.action,
            "score": decision.score,
            "confidence": decision.confidence,
            "confidence_note": "heuristic confluence score, not a calibrated probability",
            "decision": decision_to_dict(decision),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        write_gate_csv(
            args.gate_output,
            symbol=args.symbol,
            action=decision.action,
            confidence=decision.confidence,
            generated_epoch=generated,
            expires_epoch=expires,
        )
        print(
            f"Kronos Matrix: action={decision.action} matrix={matrix.direction} "
            f"analog={analog.direction} forecast={forecast.direction} "
            f"confidence={decision.confidence:.3f}"
        )
        print(f"Wrote report: {args.output}")
        print(f"Wrote MT5 gate: {args.gate_output}")
        return 0 if decision.action == "ABSTAIN" else 0
    except Exception as exc:
        return safe_abstain(args, str(exc))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Kronos + historical analog + breakout matrix research signal")
    parser.add_argument("--input-csv", type=Path, default=root / "TradingAgentsData" / "market" / "xauusd_m5.csv")
    parser.add_argument("--kronos-root", type=Path, default=root / "Kronos")
    parser.add_argument("--output", type=Path, default=root / "TradingAgentsData" / "research" / "kronos_matrix_latest.json")
    parser.add_argument("--gate-output", type=Path, default=root / "TradingAgentsData" / "research" / "new_era_kronos_matrix_gate.csv")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--model", default="NeoQuasar/Kronos-small")
    parser.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    parser.add_argument("--device", default=None)
    parser.add_argument("--max-context", type=int, default=512)
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--pred-len", type=int, default=12)
    parser.add_argument("--bar-minutes", type=int, default=5)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--sample-count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--spread-points", type=float, default=None)
    parser.add_argument("--max-spread-points", type=float, default=180.0)
    parser.add_argument("--point", type=float, default=0.01)
    parser.add_argument("--max-spread-to-atr", type=float, default=0.45)
    parser.add_argument("--breakout-window", type=int, default=20)
    parser.add_argument("--analog-window", type=int, default=24)
    parser.add_argument("--analog-horizon", type=int, default=12)
    parser.add_argument("--analog-neighbors", type=int, default=8)
    parser.add_argument("--analog-min-samples", type=int, default=5)
    parser.add_argument("--analog-min-win-rate", type=float, default=0.55)
    parser.add_argument("--forecast-min-move-atr", type=float, default=0.15)
    parser.add_argument("--forecast-min-agreement", type=float, default=0.67)
    parser.add_argument("--gate-ttl-minutes", type=int, default=90)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
