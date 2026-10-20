"""Point-in-time walk-forward backtest for the Kronos Matrix candidate.

The default ``combined`` mode runs Kronos at each historical decision point,
then evaluates only future candles after that point.  ``matrix-analog`` is a
cheaper ablation that tests the deterministic pattern hypothesis before model
download/inference is available.

This is deliberately not an MT5 executor.  It reports research metrics and
never writes a BUY/SELL gate for trading.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kronos_matrix_strategy import (
    MatrixConfig,
    analog_signal,
    atr_series,
    combine_decisions,
    forecast_signal,
    matrix_signal,
)
from run_kronos_matrix_research import (
    load_kronos_predictor,
    load_ohlcv_csv,
    records as frame_records,
    resample_frame,
)


def make_predictor_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        kronos_root=args.kronos_root,
        seed=args.seed,
        device=args.device,
        tokenizer=args.tokenizer,
        model=args.model,
        max_context=args.max_context,
    )


def forecast_at_anchor(frame, predictor, pd, args):
    lookback = min(args.lookback, len(frame), args.max_context)
    x_df = frame.iloc[-lookback:][["open", "high", "low", "close", "volume", "amount"]]
    x_timestamp = x_df.index.to_series().reset_index(drop=True)
    interval = pd.Timedelta(minutes=args.bar_minutes)
    y_timestamp = pd.Series(
        [x_timestamp.iloc[-1] + interval * step for step in range(1, args.pred_len + 1)]
    )
    predicted = predictor.predict(
        df=x_df.reset_index(drop=True),
        x_timestamp=x_timestamp,
        y_timestamp=y_timestamp,
        pred_len=args.pred_len,
        T=args.temperature,
        top_p=args.top_p,
        sample_count=args.sample_count,
        verbose=False,
    )
    return [float(value) for value in predicted["close"].tolist()]


def cost_adjusted_return(
    entry: float,
    exit_price: float,
    action: str,
    *,
    spread_points: float,
    slippage_points: float,
    point: float,
) -> float:
    if action not in {"BUY", "SELL"} or entry <= 0:
        return 0.0
    gross = (exit_price - entry) / entry if action == "BUY" else (entry - exit_price) / entry
    round_turn_cost = 2.0 * (spread_points + slippage_points) * point / entry
    return gross - round_turn_cost


def choose_matrix_analog_action(matrix, analog, cost_ok: bool) -> str:
    if not cost_ok:
        return "ABSTAIN"
    if matrix.direction in {"BUY", "SELL"} and matrix.direction == analog.direction:
        return matrix.direction
    return "ABSTAIN"


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["net_return"]) for item in trades if item["action"] in {"BUY", "SELL"}]
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    return {
        "decision_points": len(trades),
        "trades": len(returns),
        "wins": sum(value > 0 for value in returns),
        "win_rate": (sum(value > 0 for value in returns) / len(returns)) if returns else None,
        "net_return": sum(returns),
        "profit_factor": (gains / losses) if losses > 0 else (None if gains == 0 else float("inf")),
        "average_return": (sum(returns) / len(returns)) if returns else None,
        "max_loss": min(returns) if returns else None,
    }


def run(args: argparse.Namespace) -> int:
    try:
        import pandas as pd
    except ImportError as exc:
        print(f"pandas is required for the backtest: {exc}", file=sys.stderr)
        return 2

    try:
        base = load_ohlcv_csv(args.input_csv, pd)
        start = max(args.start_index, 3000)
        end = min(args.end_index or (len(base) - args.horizon - 1), len(base) - args.horizon - 1)
        if end <= start:
            raise RuntimeError(f"not enough data for backtest range {start}:{end}")
        config = MatrixConfig(
            breakout_window=args.breakout_window,
            analog_window=args.analog_window,
            analog_horizon=args.horizon,
            analog_neighbors=args.analog_neighbors,
            analog_min_samples=args.analog_min_samples,
            analog_min_win_rate=args.analog_min_win_rate,
            forecast_min_move_atr=args.forecast_min_move_atr,
            forecast_min_agreement=args.forecast_min_agreement,
        )
        predictor = None
        device = "matrix-analog-only"
        if args.mode == "combined":
            predictor, _, device = load_kronos_predictor(make_predictor_args(args))
        cost_ok = args.spread_points <= args.max_spread_points
        if args.spread_points * args.point > 0:
            # ATR is checked again at each anchor below; this initial check only
            # prevents nonsensical command-line settings.
            cost_ok = cost_ok and args.spread_points >= 0

        trades: list[dict[str, Any]] = []
        for anchor_index in range(start, end + 1, max(1, args.step)):
            history = base.iloc[: anchor_index + 1]
            anchor_timestamp = history.index[-1]
            frames = {
                "M5": history,
                "M15": resample_frame(history, "15min", pd, anchor_timestamp),
                "H1": resample_frame(history, "1h", pd, anchor_timestamp),
                "H4": resample_frame(history, "4h", pd, anchor_timestamp),
            }
            frame_map = {key: frame_records(value) for key, value in frames.items()}
            matrix = matrix_signal(frame_map, config)
            analog = analog_signal(frame_map["M5"], config)
            m5_atr = atr_series(frame_map["M5"], config.atr_period)[-1]
            spread_to_atr = (args.spread_points * args.point / m5_atr) if m5_atr > 0 else math.inf
            anchor_cost_ok = cost_ok and spread_to_atr <= args.max_spread_to_atr
            if args.mode == "combined":
                forecast_values = forecast_at_anchor(history, predictor, pd, args)
                forecast = forecast_signal(frame_map["M5"][-1]["close"], forecast_values, m5_atr, config)
                decision = combine_decisions(matrix, analog, forecast, cost_ok=anchor_cost_ok, config=config)
                action = decision.action
                confidence = decision.confidence
                forecast_direction = forecast.direction
            else:
                action = choose_matrix_analog_action(matrix, analog, anchor_cost_ok)
                confidence = 0.0
                forecast_direction = "NOT_RUN"

            entry = float(base["close"].iloc[anchor_index])
            exit_price = float(base["close"].iloc[anchor_index + args.horizon])
            net_return = cost_adjusted_return(
                entry,
                exit_price,
                action,
                spread_points=args.spread_points,
                slippage_points=args.slippage_points,
                point=args.point,
            )
            trades.append(
                {
                    "anchor_index": anchor_index,
                    "timestamp": str(anchor_timestamp),
                    "action": action,
                    "confidence": confidence,
                    "matrix_direction": matrix.direction,
                    "matrix_score": matrix.score,
                    "analog_direction": analog.direction,
                    "analog_win_rate": analog.win_rate,
                    "forecast_direction": forecast_direction,
                    "spread_to_atr": spread_to_atr,
                    "entry": entry,
                    "exit": exit_price,
                    "net_return": net_return,
                }
            )

        payload = {
            "strategy": "new-era-kronos-matrix-v1",
            "mode": args.mode,
            "symbol": args.symbol,
            "input_csv": str(args.input_csv),
            "device": device,
            "point_in_time": True,
            "future_prices_used_for_decision": False,
            "cost_assumptions": {
                "spread_points": args.spread_points,
                "slippage_points_per_side": args.slippage_points,
                "point": args.point,
            },
            "summary": summarize(trades),
            "trades": trades,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.trades_output:
            args.trades_output.parent.mkdir(parents=True, exist_ok=True)
            with args.trades_output.open("w", newline="", encoding="utf-8") as handle:
                if trades:
                    writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
                    writer.writeheader()
                    writer.writerows(trades)
        print(json.dumps(payload["summary"], indent=2))
        print(f"Wrote backtest: {args.output}")
        return 0
    except Exception as exc:
        print(f"Kronos Matrix backtest stopped: {exc}", file=sys.stderr)
        return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Point-in-time Kronos Matrix walk-forward backtest")
    parser.add_argument("--input-csv", type=Path, default=root / "TradingAgentsData/market/xauusd_m5.csv")
    parser.add_argument("--output", type=Path, default=root / "TradingAgentsData/research/kronos_matrix_backtest.json")
    parser.add_argument("--trades-output", type=Path, default=root / "TradingAgentsData/research/kronos_matrix_backtest_trades.csv")
    parser.add_argument("--mode", choices=("combined", "matrix-analog"), default="combined")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--kronos-root", type=Path, default=root / "Kronos")
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
    parser.add_argument("--start-index", type=int, default=3000)
    parser.add_argument("--end-index", type=int, default=0)
    parser.add_argument("--step", type=int, default=12)
    parser.add_argument("--horizon", type=int, default=12)
    parser.add_argument("--spread-points", type=float, default=25.0)
    parser.add_argument("--slippage-points", type=float, default=10.0)
    parser.add_argument("--point", type=float, default=0.01)
    parser.add_argument("--max-spread-points", type=float, default=50.0)
    parser.add_argument("--max-spread-to-atr", type=float, default=0.08)
    parser.add_argument("--breakout-window", type=int, default=20)
    parser.add_argument("--analog-window", type=int, default=24)
    parser.add_argument("--analog-neighbors", type=int, default=8)
    parser.add_argument("--analog-min-samples", type=int, default=5)
    parser.add_argument("--analog-min-win-rate", type=float, default=0.55)
    parser.add_argument("--forecast-min-move-atr", type=float, default=0.15)
    parser.add_argument("--forecast-min-agreement", type=float, default=0.67)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
