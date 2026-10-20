"""Research-only signal contract for the New Era Kronos Matrix strategy.

This module intentionally has no pandas, NumPy, PyTorch, MetaTrader5, or model
imports.  The pure functions are therefore testable on the current machine and
can also be reused by a future data adapter.

The strategy is not a claim that markets literally repeat.  It tests a narrower
hypothesis:

1. a multi-timeframe breakout matrix identifies the current structure;
2. nearest historical windows with similar normalized returns provide an
   empirical analogue check, without using future bars at the decision point;
3. Kronos supplies a short-horizon OHLCV forecast;
4. cost/risk/news gates can veto the idea.

Only the combination of all required checks may produce BUY or SELL.  Every
other state is ABSTAIN.  This is a research signal, never an order request.
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence


UP = "BUY"
DOWN = "SELL"
FLAT = "ABSTAIN"
STRATEGY_ID = "new-era-kronos-matrix-v1"
GATE_VERSION = "1"


@dataclass(frozen=True)
class MatrixConfig:
    breakout_window: int = 20
    breakout_atr_fraction: float = 0.10
    trend_fast: int = 20
    trend_slow: int = 50
    rsi_period: int = 14
    buy_rsi: float = 52.0
    sell_rsi: float = 48.0
    atr_period: int = 14
    # H4 + H1 + M15 trend alignment is 5 points.  A minimum of 6 therefore
    # requires the closed M5 range breakout as well; trend alone cannot trade.
    minimum_matrix_score: int = 6
    analog_window: int = 24
    analog_horizon: int = 12
    analog_neighbors: int = 8
    analog_min_samples: int = 5
    analog_min_win_rate: float = 0.55
    analog_min_distance_ratio: float = 0.0
    forecast_min_move_atr: float = 0.15
    forecast_min_agreement: float = 0.67
    gate_ttl_minutes: int = 90


@dataclass
class MatrixDecision:
    direction: str
    score: float
    buy_score: float
    sell_score: float
    reasons: list[str]
    metrics: dict[str, float | str | None]


@dataclass
class AnalogDecision:
    direction: str
    sample_count: int
    win_rate: float | None
    median_forward_return: float | None
    p25_forward_return: float | None
    p75_forward_return: float | None
    median_distance: float | None
    reasons: list[str]


@dataclass
class ForecastDecision:
    direction: str
    move_atr: float | None
    final_return: float | None
    path_agreement: float
    forecast_bars: int
    reasons: list[str]


@dataclass
class CombinedDecision:
    strategy: str
    action: str
    score: float
    confidence: float
    reasons: list[str]
    matrix: MatrixDecision
    analog: AnalogDecision
    forecast: ForecastDecision
    hard_blocks: list[str]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _field(bar: Mapping[str, Any], name: str) -> float | None:
    return _number(bar.get(name))


def closes(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    return [value for bar in bars if (value := _field(bar, "close")) is not None]


def _aligned_values(bars: Sequence[Mapping[str, Any]], field: str) -> list[float]:
    values: list[float] = []
    for bar in bars:
        value = _field(bar, field)
        if value is None:
            raise ValueError(f"bar is missing finite {field!r}")
        values.append(value)
    return values


def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def true_ranges(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    highs = _aligned_values(bars, "high")
    lows = _aligned_values(bars, "low")
    closes_values = _aligned_values(bars, "close")
    result: list[float] = []
    previous = closes_values[0] if closes_values else 0.0
    for high, low, close in zip(highs, lows, closes_values):
        result.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    return result


def rolling_mean(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("rolling period must be positive")
    output: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        output.append(running / min(index + 1, period))
    return output


def atr_series(bars: Sequence[Mapping[str, Any]], period: int) -> list[float]:
    return rolling_mean(true_ranges(bars), period)


def rsi_series(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("RSI period must be positive")
    if not values:
        return []
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    average_gain = rolling_mean(gains, period)
    average_loss = rolling_mean(losses, period)
    result: list[float] = []
    for gain, loss in zip(average_gain, average_loss):
        if loss <= 1e-12:
            result.append(100.0 if gain > 0 else 50.0)
        else:
            relative = gain / loss
            result.append(100.0 - (100.0 / (1.0 + relative)))
    return result


def _directional_trend(
    bars: Sequence[Mapping[str, Any]], config: MatrixConfig
) -> tuple[int, float, str]:
    values = _aligned_values(bars, "close")
    required = max(config.trend_slow, 3)
    if len(values) < required:
        return 0, 0.0, "insufficient trend bars"
    fast = ema(values, config.trend_fast)[-1]
    slow = ema(values, config.trend_slow)[-1]
    close = values[-1]
    if close > fast > slow:
        return 1, 1.0, "trend aligned bullish"
    if close < fast < slow:
        return -1, 1.0, "trend aligned bearish"
    return 0, 0.0, "trend is mixed"


def _breakout_vote(
    bars: Sequence[Mapping[str, Any]], config: MatrixConfig
) -> tuple[int, float, dict[str, float | None], str]:
    if len(bars) < config.breakout_window + config.atr_period + 2:
        return 0, 0.0, {}, "insufficient breakout bars"
    highs = _aligned_values(bars, "high")
    lows = _aligned_values(bars, "low")
    close = _aligned_values(bars, "close")[-1]
    atr = atr_series(bars, config.atr_period)[-1]
    previous_high = max(highs[-config.breakout_window - 1 : -1])
    previous_low = min(lows[-config.breakout_window - 1 : -1])
    buffer = max(0.0, atr * config.breakout_atr_fraction)
    metrics = {
        "close": close,
        "atr": atr,
        "range_high": previous_high,
        "range_low": previous_low,
        "breakout_distance_atr": (close - previous_high) / atr if atr > 0 else None,
        "breakdown_distance_atr": (previous_low - close) / atr if atr > 0 else None,
    }
    if close > previous_high + buffer:
        return 1, 2.0, metrics, "closed-bar upside range breakout"
    if close < previous_low - buffer:
        return -1, 2.0, metrics, "closed-bar downside range breakout"
    return 0, 0.0, metrics, "no closed-bar range breakout"


def matrix_signal(
    frames: Mapping[str, Sequence[Mapping[str, Any]]],
    config: MatrixConfig | None = None,
) -> MatrixDecision:
    """Score the H4/H1/M15/M5 breakout matrix using only closed bars."""

    config = config or MatrixConfig()
    reasons: list[str] = []
    metrics: dict[str, float | str | None] = {}
    buy_score = 0.0
    sell_score = 0.0

    for timeframe, weight in (("H4", 2.0), ("H1", 2.0)):
        vote, _, reason = _directional_trend(frames.get(timeframe, []), config)
        reasons.append(f"{timeframe}: {reason}")
        if vote > 0:
            buy_score += weight
        elif vote < 0:
            sell_score += weight

    m15 = frames.get("M15", [])
    m15_values = _aligned_values(m15, "close") if m15 else []
    if len(m15_values) >= max(config.trend_fast, config.rsi_period) + 1:
        m15_ema = ema(m15_values, config.trend_fast)[-1]
        m15_rsi = rsi_series(m15_values, config.rsi_period)[-1]
        metrics.update({"m15_ema": m15_ema, "m15_rsi": m15_rsi})
        if m15_values[-1] > m15_ema and m15_rsi >= config.buy_rsi:
            buy_score += 1.0
            reasons.append("M15: momentum aligned bullish")
        elif m15_values[-1] < m15_ema and m15_rsi <= config.sell_rsi:
            sell_score += 1.0
            reasons.append("M15: momentum aligned bearish")
        else:
            reasons.append("M15: momentum mixed")
    else:
        reasons.append("M15: insufficient momentum bars")

    vote, weight, breakout_metrics, reason = _breakout_vote(frames.get("M5", []), config)
    metrics.update({f"m5_{key}": value for key, value in breakout_metrics.items()})
    reasons.append(f"M5: {reason}")
    if vote > 0:
        buy_score += weight
    elif vote < 0:
        sell_score += weight

    score = max(buy_score, sell_score)
    if score < config.minimum_matrix_score or buy_score == sell_score:
        direction = FLAT
    else:
        direction = UP if buy_score > sell_score else DOWN
    return MatrixDecision(direction, score, buy_score, sell_score, reasons, metrics)


def _log_return_vector(
    bars: Sequence[Mapping[str, Any]], end_index: int, window: int
) -> list[float] | None:
    if window < 3 or end_index >= len(bars) or end_index - window + 1 < 0:
        return None
    values = _aligned_values(bars, "close")[end_index - window + 1 : end_index + 1]
    returns = [math.log(current / previous) for previous, current in zip(values, values[1:]) if previous > 0 and current > 0]
    if len(returns) < window - 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    scale = math.sqrt(variance) or 1.0
    return [(value - mean) / scale for value in returns]


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return float("inf")
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)) / len(left))


def analog_signal(
    bars: Sequence[Mapping[str, Any]],
    config: MatrixConfig | None = None,
) -> AnalogDecision:
    """Find past, non-overlapping normalized return windows and score outcomes."""

    config = config or MatrixConfig()
    n = len(bars)
    current = _log_return_vector(bars, n - 1, config.analog_window)
    if current is None:
        return AnalogDecision(FLAT, 0, None, None, None, None, None, ["insufficient analog context"])

    candidates: list[tuple[float, float]] = []
    last_allowed = n - config.analog_horizon - 1
    # Do not use the most recent windows that overlap the live pattern. This
    # reduces leakage from nearly identical adjacent candles.
    earliest_end = config.analog_window - 1
    latest_end = last_allowed - config.analog_window
    for end in range(earliest_end, max(earliest_end, latest_end) + 1):
        if end >= n - config.analog_horizon - config.analog_window:
            continue
        vector = _log_return_vector(bars, end, config.analog_window)
        if vector is None:
            continue
        start_close = _field(bars[end], "close")
        future_close = _field(bars[end + config.analog_horizon], "close")
        if start_close is None or future_close is None or start_close <= 0:
            continue
        candidates.append((_distance(current, vector), future_close / start_close - 1.0))

    candidates.sort(key=lambda item: item[0])
    selected = candidates[: max(1, config.analog_neighbors)]
    if len(selected) < config.analog_min_samples:
        return AnalogDecision(
            FLAT,
            len(selected),
            None,
            None,
            None,
            None,
            median([item[0] for item in selected]) if selected else None,
            [f"only {len(selected)} usable analogs; need {config.analog_min_samples}"],
        )

    returns = [item[1] for item in selected]
    forward_positive = sum(value > 0 for value in returns) / len(returns)
    forward_negative = sum(value < 0 for value in returns) / len(returns)
    median_return = median(returns)
    ordered = sorted(returns)
    p25 = ordered[max(0, int((len(ordered) - 1) * 0.25))]
    p75 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.75))]
    if forward_positive >= config.analog_min_win_rate and median_return > 0:
        direction = UP
        win_rate = forward_positive
    elif forward_negative >= config.analog_min_win_rate and median_return < 0:
        direction = DOWN
        win_rate = forward_negative
    else:
        direction = FLAT
        win_rate = max(forward_positive, forward_negative)
    return AnalogDecision(
        direction,
        len(returns),
        win_rate,
        median_return,
        p25,
        p75,
        median([item[0] for item in selected]),
        [f"nearest analogs: {len(returns)}", f"positive/negative rates: {forward_positive:.2f}/{forward_negative:.2f}"],
    )


def forecast_signal(
    current_close: float,
    predicted_closes: Sequence[float],
    atr: float,
    config: MatrixConfig | None = None,
) -> ForecastDecision:
    """Turn a Kronos path into a direction without pretending it is a probability."""

    config = config or MatrixConfig()
    values = [float(value) for value in predicted_closes if math.isfinite(float(value))]
    if not values or current_close <= 0 or atr <= 0:
        return ForecastDecision(FLAT, None, None, 0.0, len(values), ["forecast or ATR unavailable"])
    final_return = values[-1] / current_close - 1.0
    move_atr = (values[-1] - current_close) / atr
    signs: list[int] = []
    for value in values:
        delta = value - current_close
        signs.append(1 if delta > 0 else -1 if delta < 0 else 0)
    if signs:
        path_agreement = max(sum(sign > 0 for sign in signs), sum(sign < 0 for sign in signs)) / len(signs)
    else:
        path_agreement = 0.0
    if move_atr >= config.forecast_min_move_atr and path_agreement >= config.forecast_min_agreement:
        direction = UP
    elif move_atr <= -config.forecast_min_move_atr and path_agreement >= config.forecast_min_agreement:
        direction = DOWN
    else:
        direction = FLAT
    return ForecastDecision(
        direction,
        move_atr,
        final_return,
        path_agreement,
        len(values),
        [f"forecast move={move_atr:.2f} ATR", f"path agreement={path_agreement:.2f}"],
    )


def combine_decisions(
    matrix: MatrixDecision,
    analog: AnalogDecision,
    forecast: ForecastDecision,
    *,
    cost_ok: bool = True,
    hard_blocks: Sequence[str] = (),
    config: MatrixConfig | None = None,
) -> CombinedDecision:
    """Require structure, analog and forecast agreement before producing a gate."""

    config = config or MatrixConfig()
    blocks = list(hard_blocks)
    reasons = [*matrix.reasons, *analog.reasons, *forecast.reasons]
    if not cost_ok:
        blocks.append("execution cost gate failed")
    if matrix.direction == FLAT:
        blocks.append("matrix has no decisive breakout alignment")
    if analog.direction == FLAT:
        blocks.append("analog pattern has no sufficiently strong direction")
    if forecast.direction == FLAT:
        blocks.append("Kronos forecast has no sufficiently strong direction")
    if matrix.direction != FLAT and analog.direction != FLAT and matrix.direction != analog.direction:
        blocks.append("matrix and analog directions disagree")
    if matrix.direction != FLAT and forecast.direction != FLAT and matrix.direction != forecast.direction:
        blocks.append("matrix and Kronos directions disagree")

    action = FLAT
    if not blocks:
        action = matrix.direction
        reasons.append("all required matrix, analog, Kronos and cost gates agree")

    matrix_component = min(1.0, matrix.score / max(config.minimum_matrix_score, 1))
    analog_component = analog.win_rate if analog.win_rate is not None else 0.0
    forecast_component = min(1.0, forecast.path_agreement)
    confidence = round((0.45 * matrix_component) + (0.30 * analog_component) + (0.25 * forecast_component), 4)
    score = round(matrix.score + (analog_component * 2.0) + forecast_component, 4)
    return CombinedDecision(
        STRATEGY_ID,
        action,
        score,
        confidence,
        reasons,
        matrix,
        analog,
        forecast,
        blocks,
    )


def decision_to_dict(decision: CombinedDecision) -> dict[str, Any]:
    return asdict(decision)


def write_gate_csv(
    path: Path,
    *,
    symbol: str,
    action: str,
    confidence: float,
    generated_epoch: int,
    expires_epoch: int,
    strategy: str = STRATEGY_ID,
) -> None:
    """Write the exact fail-closed contract read by the reusable MT5 EA."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="ascii") as handle:
        writer = csv.writer(handle)
        writer.writerow(("version", "strategy", "symbol", "action", "confidence", "generated_epoch", "expires_epoch"))
        writer.writerow((GATE_VERSION, strategy, symbol, action, f"{confidence:.4f}", generated_epoch, expires_epoch))
