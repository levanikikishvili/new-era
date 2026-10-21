"""Gemini Autonomous Portfolio Manager ("ჯოი" / Joey CIO) & TA-Lib Technical Engine.

Implements:
1. Step 1 (TA-Lib Technical Filter Engine): Fast RSI(14), ATR(14), EMA(20/50/200),
   MACD(12,26,9), Bollinger Bands(20,2), and ADX(14) calculation (using C-based
   `talib` when installed, or exact TA-Lib mathematical equivalents).
2. Waterfall Decision Pipeline & Veto Hierarchy (Steps 1 -> 2 -> 3 -> 4):
   - Step 1 (TA-Lib): Fast Technical Trend & Exhaustion Filter
   - Step 2 (Kronos + Matrix): Primary Direction & Base Probability Engine
   - Step 3 (L2 DOM Orderflow): Safety Gate / Veto Check (opposing walls only + wall support detection)
   - Step 4 (Gemini CIO - "ჯოი"): Final Win-Probability (0-100%), Dynamic Risk Sizing & Lot Calculation:
     * < 65%: ABSTAIN / Pass (0% risk)
     * 65% - 80%: Standard Risk (0.25% - 0.50% equity risk)
     * > 80% + L2 DOM Wall Support: Dynamic Increased Risk (1.00% - 1.25% equity risk, hard cap 1.50%)
3. Interactive Georgian Co-Pilot Chat & One-Click Setup Analyzer ("ჯოი").
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT, load_workspace_dotenv

try:
    import talib  # type: ignore[import-untyped]
    import numpy as np  # type: ignore[import-untyped]

    HAS_TALIB = True
except ImportError:
    talib = None
    np = None
    HAS_TALIB = False


# Joey CIO profile safety limits.  Keep these deterministic and local to the
# sizing/pipeline layer so a research report can never widen the execution
# envelope by itself.
MAX_RISK_PCT = 1.5
MIN_REWARD_RISK = 1.5
MAX_SL_ATR_MULT = 2.5


# ---------------------------------------------------------------------------
# Step 1: TA-Lib Technical Filter Engine
# ---------------------------------------------------------------------------
@dataclass
class Step1TALibResult:
    symbol: str
    passed: bool
    trend_bias: str  # "BUY", "SELL", "NEUTRAL"
    filter_score: float  # 0.0 .. 100.0
    close: float
    rsi_14: float
    atr_14: float
    ema_20: float
    ema_50: float
    ema_200: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    adx_14: float
    veto_buy: bool
    veto_sell: bool
    veto_reason: str
    engine: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "passed": self.passed,
            "trend_bias": self.trend_bias,
            "filter_score": round(self.filter_score, 1),
            "close": round(self.close, 5),
            "rsi_14": round(self.rsi_14, 2),
            "atr_14": round(self.atr_14, 5),
            "ema_20": round(self.ema_20, 5),
            "ema_50": round(self.ema_50, 5),
            "ema_200": round(self.ema_200, 5),
            "macd_line": round(self.macd_line, 5),
            "macd_signal": round(self.macd_signal, 5),
            "macd_hist": round(self.macd_hist, 5),
            "bb_upper": round(self.bb_upper, 5),
            "bb_middle": round(self.bb_middle, 5),
            "bb_lower": round(self.bb_lower, 5),
            "adx_14": round(self.adx_14, 2),
            "veto_buy": self.veto_buy,
            "veto_sell": self.veto_sell,
            "veto_reason": self.veto_reason,
            "engine": self.engine,
            "summary": self.summary,
        }


class TALibTechnicalEngine:
    """Fast Step-1 Technical Filter computing RSI, ATR, EMA(20/50/200), MACD, Bollinger & ADX."""

    @staticmethod
    def _ema_series(values: list[float], period: int) -> list[float]:
        if not values:
            return []
        if len(values) < period:
            avg = sum(values) / len(values)
            return [avg for _ in values]
        k = 2.0 / (period + 1.0)
        ema_vals = [0.0] * len(values)
        seed = sum(values[:period]) / float(period)
        for i in range(period):
            ema_vals[i] = seed
        prev = seed
        for i in range(period, len(values)):
            prev = (values[i] - prev) * k + prev
            ema_vals[i] = prev
        return ema_vals

    @staticmethod
    def _wilder_rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) <= period:
            return 50.0
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0.0, diff))
            losses.append(max(0.0, -diff))
        avg_gain = sum(gains[:period]) / float(period)
        avg_loss = sum(losses[:period]) / float(period)
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / float(period)
            avg_loss = (avg_loss * (period - 1) + losses[i]) / float(period)
        if avg_loss <= 1e-12:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _wilder_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
        n = min(len(highs), len(lows), len(closes))
        if n < 2:
            return 0.0
        trs: list[float] = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if len(trs) <= period:
            return sum(trs) / float(len(trs))
        atr = sum(trs[:period]) / float(period)
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / float(period)
        return atr

    @staticmethod
    def _wilder_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
        n = min(len(highs), len(lows), len(closes))
        if n <= period * 2:
            return 25.0
        plus_dm: list[float] = []
        minus_dm: list[float] = []
        trs: list[float] = []
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
        tr_s = sum(trs[:period])
        p_s = sum(plus_dm[:period])
        m_s = sum(minus_dm[:period])
        dx_list: list[float] = []
        for i in range(period, len(trs)):
            tr_s = tr_s - (tr_s / period) + trs[i]
            p_s = p_s - (p_s / period) + plus_dm[i]
            m_s = m_s - (m_s / period) + minus_dm[i]
            if tr_s <= 1e-12:
                continue
            p_di = 100.0 * (p_s / tr_s)
            m_di = 100.0 * (m_s / tr_s)
            denom = p_di + m_di
            dx_list.append(100.0 * abs(p_di - m_di) / denom if denom > 1e-12 else 0.0)
        if not dx_list:
            return 20.0
        adx = sum(dx_list[:period]) / float(min(period, len(dx_list)))
        for val in dx_list[period:]:
            adx = (adx * (period - 1) + val) / float(period)
        return adx

    @classmethod
    def evaluate(
        cls,
        candles: list[dict[str, Any]],
        *,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        fallback_close: float = 0.0,
        fallback_atr: float = 0.0,
    ) -> Step1TALibResult:
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        for row in candles:
            if not isinstance(row, dict):
                continue
            try:
                h = float(row.get("high") or 0.0)
                l = float(row.get("low") or 0.0)
                c = float(row.get("close") or 0.0)
                if c > 0:
                    highs.append(h)
                    lows.append(l)
                    closes.append(c)
            except (TypeError, ValueError):
                continue
        return cls._evaluate_arrays(
            symbol=symbol,
            highs=highs,
            lows=lows,
            closes=closes,
            fallback_close=fallback_close,
            fallback_atr=fallback_atr,
        )

    @classmethod
    def evaluate_from_csv(
        cls,
        symbol: str,
        csv_path: Path | None,
        *,
        fallback_close: float = 0.0,
        fallback_atr: float = 0.0,
    ) -> Step1TALibResult:
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []

        if csv_path is not None and csv_path.exists():
            try:
                with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        try:
                            h = float(row.get("high") or 0.0)
                            l = float(row.get("low") or 0.0)
                            c = float(row.get("close") or 0.0)
                            if c > 0:
                                highs.append(h)
                                lows.append(l)
                                closes.append(c)
                        except (TypeError, ValueError):
                            continue
            except OSError:
                pass

        return cls._evaluate_arrays(
            symbol=symbol,
            highs=highs,
            lows=lows,
            closes=closes,
            fallback_close=fallback_close,
            fallback_atr=fallback_atr,
        )

    @classmethod
    def _evaluate_arrays(
        cls,
        *,
        symbol: str,
        highs: list[float],
        lows: list[float],
        closes: list[float],
        fallback_close: float = 0.0,
        fallback_atr: float = 0.0,
    ) -> Step1TALibResult:
        # Keep last 350 bars for fast evaluation
        if len(closes) > 350:
            highs = highs[-350:]
            lows = lows[-350:]
            closes = closes[-350:]

        if len(closes) < 30:
            px = fallback_close if fallback_close > 0 else (closes[-1] if closes else 0.0)
            atr = fallback_atr if fallback_atr > 0 else max(px * 0.001, 0.5)
            return Step1TALibResult(
                symbol=symbol,
                passed=False,
                trend_bias="NEUTRAL",
                filter_score=0.0,
                close=px,
                rsi_14=50.0,
                atr_14=atr,
                ema_20=px,
                ema_50=px,
                ema_200=px,
                macd_line=0.0,
                macd_signal=0.0,
                macd_hist=0.0,
                bb_upper=px + atr * 2.0,
                bb_middle=px,
                bb_lower=px - atr * 2.0,
                adx_14=22.0,
                veto_buy=False,
                veto_sell=False,
                veto_reason="",
                engine="talib-fallback",
                summary=f"[Step 1 TA-Lib] {symbol}: Insufficient candles for technical evidence (ATR={atr:.4f})",
            )

        last_close = closes[-1]
        if HAS_TALIB and talib is not None and np is not None:
            arr_h = np.asarray(highs, dtype=float)
            arr_l = np.asarray(lows, dtype=float)
            arr_c = np.asarray(closes, dtype=float)
            rsi_val = float(talib.RSI(arr_c, timeperiod=14)[-1])
            atr_val = float(talib.ATR(arr_h, arr_l, arr_c, timeperiod=14)[-1])
            ema20_val = float(talib.EMA(arr_c, timeperiod=20)[-1])
            ema50_val = float(talib.EMA(arr_c, timeperiod=50)[-1])
            ema200_val = float(talib.EMA(arr_c, timeperiod=min(200, len(closes) - 1))[-1])
            macd_l, macd_s, macd_h = talib.MACD(arr_c, fastperiod=12, slowperiod=26, signalperiod=9)
            macd_line = float(macd_l[-1]) if not math.isnan(float(macd_l[-1])) else 0.0
            macd_sig = float(macd_s[-1]) if not math.isnan(float(macd_s[-1])) else 0.0
            macd_hist = float(macd_h[-1]) if not math.isnan(float(macd_h[-1])) else 0.0
            bb_u, bb_m, bb_lo = talib.BBANDS(arr_c, timeperiod=20, nbdevup=2, nbdevdn=2)
            bb_upper = float(bb_u[-1])
            bb_mid = float(bb_m[-1])
            bb_lower = float(bb_lo[-1])
            adx_raw = float(talib.ADX(arr_h, arr_l, arr_c, timeperiod=14)[-1])
            adx_val = adx_raw if not math.isnan(adx_raw) else 25.0
            engine_name = "TA-Lib C-Engine"
        else:
            rsi_val = cls._wilder_rsi(closes, 14)
            atr_val = cls._wilder_atr(highs, lows, closes, 14)
            ema20_series = cls._ema_series(closes, 20)
            ema50_series = cls._ema_series(closes, 50)
            ema200_series = cls._ema_series(closes, min(200, len(closes)))
            ema20_val = ema20_series[-1]
            ema50_val = ema50_series[-1]
            ema200_val = ema200_series[-1]

            ema12 = cls._ema_series(closes, 12)
            ema26 = cls._ema_series(closes, 26)
            macd_series = [a - b for a, b in zip(ema12, ema26)]
            sig_series = cls._ema_series(macd_series, 9)
            macd_line = macd_series[-1]
            macd_sig = sig_series[-1]
            macd_hist = macd_line - macd_sig

            w20 = closes[-20:]
            bb_mid = sum(w20) / float(len(w20))
            variance = sum((x - bb_mid) ** 2 for x in w20) / float(len(w20))
            std20 = math.sqrt(max(0.0, variance))
            bb_upper = bb_mid + 2.0 * std20
            bb_lower = bb_mid - 2.0 * std20
            adx_val = cls._wilder_adx(highs, lows, closes, 14)
            engine_name = "TA-Lib Native Quant Engine"

        if fallback_atr > 0 and atr_val <= 0:
            atr_val = fallback_atr

        # Determine EMA + MACD Trend Bias
        bull_points = 0.0
        bear_points = 0.0
        if last_close > ema20_val:
            bull_points += 1.0
        else:
            bear_points += 1.0
        if ema20_val > ema50_val:
            bull_points += 1.5
        else:
            bear_points += 1.5
        if last_close > ema200_val:
            bull_points += 1.0
        else:
            bear_points += 1.0
        if macd_hist > 0:
            bull_points += 1.0
        elif macd_hist < 0:
            bear_points += 1.0
        if 52.0 <= rsi_val <= 78.0:
            bull_points += 0.8
        elif 22.0 <= rsi_val <= 48.0:
            bear_points += 0.8

        if bull_points >= bear_points + 1.0:
            trend_bias = "BUY"
        elif bear_points >= bull_points + 1.0:
            trend_bias = "SELL"
        else:
            trend_bias = "NEUTRAL"

        # Step 1 only vetoes extreme RSI exhaustion (>82 for BUY, <18 for SELL) or dead volatility
        veto_buy = rsi_val > 82.0
        veto_sell = rsi_val < 18.0
        veto_reason = ""
        if veto_buy:
            veto_reason = f"TA-Lib RSI(14)={rsi_val:.1f} > 82.0 (Extreme Overbought Exhaustion blocks BUY)"
        elif veto_sell:
            veto_reason = f"TA-Lib RSI(14)={rsi_val:.1f} < 18.0 (Extreme Oversold Exhaustion blocks SELL)"

        dominance = abs(bull_points - bear_points) / 5.3
        adx_boost = min(20.0, max(0.0, (adx_val - 15.0) * 0.8))
        filter_score = max(35.0, min(98.0, 52.0 + dominance * 30.0 + adx_boost))

        summary = (
            f"[Step 1 TA-Lib] Bias={trend_bias} | RSI(14)={rsi_val:.1f} | ATR(14)={atr_val:.2f} | "
            f"EMA20={ema20_val:.2f} / EMA50={ema50_val:.2f} / EMA200={ema200_val:.2f} | "
            f"MACD Hist={macd_hist:+.4f} | ADX={adx_val:.1f}"
        )
        return Step1TALibResult(
            symbol=symbol,
            passed=atr_val > 0,
            trend_bias=trend_bias,
            filter_score=filter_score,
            close=last_close,
            rsi_14=rsi_val,
            atr_14=atr_val,
            ema_20=ema20_val,
            ema_50=ema50_val,
            ema_200=ema200_val,
            macd_line=macd_line,
            macd_signal=macd_sig,
            macd_hist=macd_hist,
            bb_upper=bb_upper,
            bb_middle=bb_mid,
            bb_lower=bb_lower,
            adx_14=adx_val,
            veto_buy=veto_buy,
            veto_sell=veto_sell,
            veto_reason=veto_reason,
            engine=engine_name,
            summary=summary,
        )


# ---------------------------------------------------------------------------
# Step 4: Gemini Autonomous Portfolio Manager ("ჯოი" / Joey — Quant CIO)
# ---------------------------------------------------------------------------
@dataclass
class WaterfallDecision:
    symbol: str
    final_action: str  # "BUY", "SELL", "ABSTAIN"
    win_probability_pct: float  # 0.0 .. 100.0
    risk_tier: str  # "ABSTAIN (<65%)", "STANDARD (0.25%-0.50%)", "DYNAMIC_HIGH (1.00%-1.25%, cap 1.50%)"
    risk_pct: float  # e.g. 0.0, 0.38, 1.15
    recommended_lot: float
    sl_price: float
    tp_price: float
    be_trigger_price: float  # +1.0x ATR
    partial_close_price: float  # +2.0x ATR
    trailing_distance: float  # 1.0x ATR
    l2_wall_support: bool
    l2_vetoed: bool
    talib_vetoed: bool
    step1_talib: dict[str, Any]
    step2_kronos_matrix: dict[str, Any]
    step3_l2_safety: dict[str, Any]
    step4_gemini_cio: dict[str, Any]
    rationale_ka: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    research_evidence_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "final_action": self.final_action,
            "win_probability_pct": round(self.win_probability_pct, 1),
            "confidence": round(self.win_probability_pct / 100.0, 4),
            "risk_tier": self.risk_tier,
            "risk_pct": round(self.risk_pct, 3),
            "recommended_lot": round(self.recommended_lot, 2),
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "be_trigger_price": self.be_trigger_price,
            "partial_close_price": self.partial_close_price,
            "trailing_distance": self.trailing_distance,
            "l2_wall_support": self.l2_wall_support,
            "l2_vetoed": self.l2_vetoed,
            "talib_vetoed": self.talib_vetoed,
            "steps": {
                "step1_talib": self.step1_talib,
                "step2_kronos_matrix": self.step2_kronos_matrix,
                "step3_l2_safety": self.step3_l2_safety,
                "step4_gemini_cio": self.step4_gemini_cio,
            },
            "rationale_ka": self.rationale_ka,
            "research_evidence_valid": self.research_evidence_valid,
            "created_at": self.created_at,
        }


class GeminiPortfolioManager:
    """Autonomous Portfolio Manager & Dynamic Risk CIO ("ჯოი") powered by Gemini."""

    PERSONA_NAME = "ჯოი (Gemini Quant CIO)"
    MAX_RISK_PCT = MAX_RISK_PCT
    MIN_REWARD_RISK = MIN_REWARD_RISK
    MAX_SL_ATR_MULT = MAX_SL_ATR_MULT
    # NOTE: gemini-2.5-flash / 2.0-flash / 1.5-flash now return HTTP 404 for this key.
    # Order = fastest first. Override with GEMINI_MODEL=model_a,model_b in .env.
    DEFAULT_MODELS = ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-3.1-flash-lite")
    MODELS = DEFAULT_MODELS

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.autonomous_mode: bool = False
        self.dynamic_trailing_enabled: bool = True
        # Fail-safe: in autonomous mode every trade must be APPROVED by the live Gemini API.
        self.gemini_review_required: bool = True
        self._last_decision: dict[str, Any] | None = None
        self._chat_history: list[dict[str, str]] = []
        self._dead_models: set[str] = set()
        self.active_model: str | None = None
        self.last_gemini_error: str = ""
        self.last_gemini_ok_at: str | None = None
        self.last_cio_review: dict[str, Any] | None = None

    def _get_api_key(self) -> str:
        load_workspace_dotenv(self.root)
        return (
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )

    @property
    def is_configured(self) -> bool:
        key = self._get_api_key()
        return bool(key and not key.startswith("your_"))

    def _model_candidates(self) -> list[str]:
        override = [m.strip() for m in os.getenv("GEMINI_MODEL", "").split(",") if m.strip()]
        ordered: list[str] = []
        for name in ([self.active_model] if self.active_model else []) + override + list(self.DEFAULT_MODELS):
            if name and name not in ordered and name not in self._dead_models:
                ordered.append(name)
        return ordered

    def gemini_status(self) -> dict[str, Any]:
        """Cheap (no network) status snapshot for /health and the UI header."""
        return {
            "configured": self.is_configured,
            "live": bool(self.active_model and self.last_gemini_ok_at),
            "active_model": self.active_model,
            "last_ok_at": self.last_gemini_ok_at,
            "last_error": self.last_gemini_error,
            "review_required": self.gemini_review_required,
            "last_cio_review": self.last_cio_review,
        }

    def ping_gemini(self, timeout: float = 15.0) -> dict[str, Any]:
        """Live connectivity check against the Gemini API (used at startup and by GET /gemini/status?live=true)."""
        reply = self._call_gemini_rest(
            "You are a health-check endpoint. Reply with JSON only.",
            'Return exactly {"ok": true}',
            timeout=timeout,
            json_mode=True,
            max_tokens=1024,
            temperature=0.0,
        )
        status = self.gemini_status()
        status["ping_ok"] = reply is not None
        return status

    @staticmethod
    def compute_dynamic_risk_and_lot(
        win_probability_pct: float,
        *,
        l2_wall_support: bool,
        equity: float,
        atr: float,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 2.5,
        entry_price: float = 0.0,
        side: str = "BUY",
        digits: int = 2,
        point: float = 0.01,
        tick_value: float = 1.0,
        tick_size: float = 0.01,
        contract_size: float = 100.0,
        volume_min: float = 0.01,
        volume_max: float = 50.0,
        volume_step: float = 0.01,
    ) -> dict[str, Any]:
        """Compute risk tier, exact risk %, lot size, and SL/TP/Break-Even/Partial-Close prices.

        Risk Hierarchy:
        - < 65%: ABSTAIN / Pass (0.0% risk, 0.00 lot)
        - 65% - 80%: Standard Risk (0.25% - 0.50% Lot Size)
        - > 80% + L2 DOM Wall Support: Dynamic Increased Risk (1.00% - 1.25% Lot Size, hard cap 1.50%)
        - > 80% without L2 Wall Support: Top of Standard Risk (0.50% Lot Size)
        """
        prob = max(0.0, min(100.0, float(win_probability_pct)))
        norm_side = side.strip().upper()

        if prob < 65.0 or norm_side not in {"BUY", "SELL"}:
            return {
                "action": "ABSTAIN",
                "risk_tier": "ABSTAIN (<65%)",
                "risk_pct": 0.0,
                "risk_usd": 0.0,
                "lot_size": 0.0,
                "sl_price": 0.0,
                "tp_price": 0.0,
                "be_trigger_price": 0.0,
                "partial_close_price": 0.0,
                "trailing_distance": round(max(atr, point * 10), digits),
                "reward_risk_ratio": 0.0,
                "rr_ratio": 0.0,
                "sl_distance": 0.0,
                "tp_distance": 0.0,
                "sl_atr_multiple": 0.0,
                "tp_atr_multiple": 0.0,
                "max_risk_pct": MAX_RISK_PCT,
                "risk_constraint_status": "ABSTAIN",
            }

        if prob > 80.0 and l2_wall_support:
            # Scale linearly from 1.00% at 80% prob up to 1.25% at 95%+ prob
            t = min(1.0, max(0.0, (prob - 80.0) / 15.0))
            risk_pct = round(1.00 + t * 0.25, 3)
            risk_tier = "DYNAMIC_INCREASED_RISK (1.00% - 1.25% + L2 Wall Support)"
        else:
            # Standard Risk: scale linearly from 0.25% at 65% prob up to 0.50% at 80%+ prob
            t = min(1.0, max(0.0, (prob - 65.0) / 15.0))
            risk_pct = round(0.25 + t * 0.25, 3)
            risk_tier = "STANDARD_RISK (0.25% - 0.50%)"

        # Hard cap the profile even if a future tier or caller supplies a
        # larger value.  The current tiers remain intentionally below this
        # ceiling, preserving their existing behavior.
        risk_pct = min(MAX_RISK_PCT, max(0.0, risk_pct))
        safe_equity = max(10.0, float(equity or 1000.0))
        risk_usd = round(min(safe_equity * (MAX_RISK_PCT / 100.0), safe_equity * (risk_pct / 100.0)), 2)

        try:
            raw_atr = float(atr or 0.0)
        except (TypeError, ValueError):
            raw_atr = 0.0
        if not math.isfinite(raw_atr) or raw_atr < 0.0:
            raw_atr = 0.0
        # When ATR evidence is present, use that measured ATR directly so the
        # 2.5x ceiling is meaningful in the instrument's actual volatility
        # units.  Only an unavailable/non-positive ATR uses the historical
        # conservative fallback.
        safe_atr = (
            raw_atr
            if raw_atr > 0.0
            else max(point * 20.0, entry_price * 0.0008 if entry_price > 0 else 0.5)
        )

        try:
            requested_sl_mult = float(sl_atr_mult)
        except (TypeError, ValueError):
            requested_sl_mult = 1.5
        if not math.isfinite(requested_sl_mult):
            requested_sl_mult = 1.5
        effective_sl_mult = min(MAX_SL_ATR_MULT, max(0.5, requested_sl_mult))

        try:
            requested_tp_mult = float(tp_atr_mult)
        except (TypeError, ValueError):
            requested_tp_mult = 2.5
        if not math.isfinite(requested_tp_mult):
            requested_tp_mult = 2.5
        effective_tp_mult = max(0.8, requested_tp_mult, effective_sl_mult * MIN_REWARD_RISK)

        sl_dist = safe_atr * effective_sl_mult
        tp_dist = safe_atr * effective_tp_mult
        reward_risk_ratio = tp_dist / sl_dist if sl_dist > 1e-9 else 0.0
        be_dist = safe_atr * 1.0  # +1.0x ATR Break-Even trigger
        partial_dist = safe_atr * 2.0  # +2.0x ATR 50% Partial Close trigger

        if tick_value > 0 and tick_size > 0:
            loss_per_lot = (sl_dist / tick_size) * tick_value
        else:
            loss_per_lot = sl_dist * (contract_size if contract_size > 0 else 100.0)

        raw_lot = (risk_usd / loss_per_lot) if loss_per_lot > 1e-9 else 0.0
        step = volume_step if volume_step > 0 else 0.01
        # Round down to the broker step so the realized stop-loss exposure
        # cannot exceed the profile budget.  If even the broker minimum would
        # exceed it, fail closed instead of forcing a too-large minimum lot.
        if raw_lot <= 0.0 or volume_min <= 0.0 or volume_min * loss_per_lot > risk_usd + 1e-9:
            final_lot = 0.0
        else:
            stepped_lot = math.floor((raw_lot + 1e-12) / step) * step
            clamped_lot = min(volume_max, max(volume_min, stepped_lot))
            # Keep the final two-decimal presentation from rounding back up
            # over the already-safe stepped amount.
            final_lot = math.floor(clamped_lot * 100.0 + 1e-12) / 100.0
            if final_lot < volume_min:
                final_lot = 0.0

        realized_risk_usd = round(final_lot * loss_per_lot, 2)
        realized_risk_pct = round((realized_risk_usd / safe_equity) * 100.0, 4) if safe_equity > 0 else 0.0

        if entry_price > 0:
            if norm_side == "BUY":
                sl_px = round(entry_price - sl_dist, digits)
                tp_px = round(entry_price + tp_dist, digits)
                be_px = round(entry_price + be_dist, digits)
                part_px = round(entry_price + partial_dist, digits)
            else:
                sl_px = round(entry_price + sl_dist, digits)
                tp_px = round(entry_price - tp_dist, digits)
                be_px = round(entry_price - be_dist, digits)
                part_px = round(entry_price - partial_dist, digits)
        else:
            sl_px = 0.0
            tp_px = 0.0
            be_px = 0.0
            part_px = 0.0

        return {
            "action": norm_side if final_lot > 0.0 else "ABSTAIN",
            "risk_tier": risk_tier,
            "risk_pct": risk_pct,
            "risk_usd": risk_usd,
            "realized_risk_usd": realized_risk_usd,
            "realized_risk_pct": realized_risk_pct,
            "lot_size": final_lot,
            "sl_price": sl_px,
            "tp_price": tp_px,
            "be_trigger_price": be_px,
            "partial_close_price": part_px,
            "trailing_distance": round(safe_atr, digits),
            "sl_distance": sl_dist,
            "tp_distance": tp_dist,
            "sl_atr_multiple": effective_sl_mult,
            "tp_atr_multiple": effective_tp_mult,
            "reward_risk_ratio": reward_risk_ratio,
            "rr_ratio": reward_risk_ratio,
            "max_risk_pct": MAX_RISK_PCT,
            "min_reward_risk": MIN_REWARD_RISK,
            "max_sl_atr_multiple": MAX_SL_ATR_MULT,
            "risk_constraint_status": "PASS" if final_lot > 0.0 else "ABSTAIN_MIN_LOT_EXCEEDS_RISK_CAP",
        }

    @staticmethod
    def build_fast_step2_matrix_report(
        symbol: str,
        csv_path: Path | None,
        step1_talib: Step1TALibResult,
    ) -> dict[str, Any]:
        """Fast in-process Step-2 Matrix Breakout + 8-NN Analogue + Trajectory Forecast evaluation."""
        from kronos_matrix_strategy import (
            analog_signal,
            combine_decisions,
            decision_to_dict,
            forecast_signal,
            matrix_signal,
        )

        m5_bars: list[dict[str, float]] = []
        if csv_path is not None and csv_path.exists():
            try:
                with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        try:
                            o = float(row.get("open") or row.get("close") or 0.0)
                            h = float(row.get("high") or 0.0)
                            l = float(row.get("low") or 0.0)
                            c = float(row.get("close") or 0.0)
                            if c > 0 and h > 0 and l > 0:
                                m5_bars.append({"open": o, "high": h, "low": l, "close": c})
                        except (TypeError, ValueError):
                            continue
            except OSError:
                pass

        if len(m5_bars) < 80:
            return {
                "symbol": symbol,
                "action": "ABSTAIN",
                "score": 0.0,
                "confidence": 0.0,
                "safe_failure": True,
                "reason": "insufficient M5 bars for Matrix, analogue, and forecast evidence",
                "decision": {
                    "matrix": {
                        "direction": "ABSTAIN",
                        "score": 0.0,
                        "buy_score": 0.0,
                        "sell_score": 0.0,
                    },
                    "analog": {
                        "direction": "ABSTAIN",
                        "win_rate": 0.0,
                    },
                    "forecast": {
                        "direction": "ABSTAIN",
                        "path_agreement": 0.0,
                        "move_atr": 0.0,
                    },
                    "hard_blocks": ["insufficient M5 bars"],
                },
            }

        recent_m5 = m5_bars[-2400:]

        def _resample(bars: list[dict[str, float]], factor: int) -> list[dict[str, float]]:
            out: list[dict[str, float]] = []
            for idx in range(0, len(bars) - factor + 1, factor):
                chunk = bars[idx : idx + factor]
                out.append(
                    {
                        "open": chunk[0]["open"],
                        "high": max(b["high"] for b in chunk),
                        "low": min(b["low"] for b in chunk),
                        "close": chunk[-1]["close"],
                    }
                )
            return out

        frames = {
            "M5": recent_m5[-300:],
            "M15": _resample(recent_m5, 3),
            "H1": _resample(recent_m5, 12),
            "H4": _resample(recent_m5, 48),
        }
        mat_dec = matrix_signal(frames)
        ana_dec = analog_signal(recent_m5[-900:])

        last_close = recent_m5[-1]["close"]
        atr_val = max(step1_talib.atr_14, last_close * 0.0005, 1e-6)
        slope = (step1_talib.ema_20 - step1_talib.ema_50) / 10.0
        if abs(slope) < atr_val * 0.02 and len(recent_m5) >= 6:
            slope = (recent_m5[-1]["close"] - recent_m5[-6]["close"]) / 5.0
        predicted_closes = [last_close + slope * (step_i * 0.45) for step_i in range(1, 13)]
        fc_dec = forecast_signal(last_close, predicted_closes, atr_val)
        combined = combine_decisions(mat_dec, ana_dec, fc_dec, cost_ok=True)
        return {
            "symbol": symbol,
            "action": combined.action,
            "score": combined.score,
            "confidence": combined.confidence,
            "decision": decision_to_dict(combined),
        }

    @staticmethod
    def _coerce_report_timestamp(value: Any) -> float | None:
        """Return a report timestamp as UTC epoch seconds, or ``None`` if invalid."""

        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, datetime):
            parsed_dt = value
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            return parsed_dt.timestamp()

        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None:
            if not math.isfinite(numeric) or numeric <= 0:
                return None
            # Accept millisecond epochs emitted by a few report producers.
            return numeric / 1000.0 if numeric > 100_000_000_000 else numeric

        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt.timestamp()

    @staticmethod
    def _report_identity_symbols(report: dict[str, Any]) -> list[str]:
        identities: list[str] = []
        for key in ("symbol", "user_symbol", "requested_symbol", "ticker"):
            value = str(report.get(key) or "").strip().upper()
            if value:
                identities.append(value)
        return identities

    @classmethod
    def _validate_research_report(
        cls,
        report: dict[str, Any] | None,
        requested_symbol: str,
        *,
        require_symbol: bool = False,
        now_epoch: float | None = None,
    ) -> tuple[bool, str]:
        """Validate report identity and any supplied freshness contract.

        ``require_symbol`` is intentionally optional for the direct public
        waterfall API, whose callers historically supplied in-memory reports
        without metadata.  The orchestrator enables it for every cached file,
        so a disk report can never silently become evidence for another pair.
        """

        if not isinstance(report, dict) or not report:
            return False, "research report is missing"
        if report.get("_evidence_invalid"):
            return False, str(report.get("reason") or "research phase failed")

        safe_failure = str(report.get("safe_failure") or "").strip().lower()
        if report.get("safe_failure") is True or safe_failure in {"1", "true", "yes", "on"}:
            return False, str(report.get("reason") or "research phase reported a safe failure")

        identities = cls._report_identity_symbols(report)
        normalized_symbol = requested_symbol.strip().upper()
        if identities and any(item != normalized_symbol for item in identities):
            return False, "research report symbol does not match requested symbol"
        has_freshness_metadata = any(
            key in report and report.get(key) not in (None, "")
            for key in (
                "generated_epoch",
                "generated_at",
                "created_at",
                "timestamp",
                "updated_at",
                "expires_epoch",
                "mt5_gate_expires_epoch",
                "expires_at",
            )
        )
        if (require_symbol or has_freshness_metadata) and not identities:
            return False, "research report has no symbol identity"

        now = float(now_epoch if now_epoch is not None else time.time())
        generated_values: list[float] = []
        expiry_values: list[float] = []
        for key in ("generated_epoch", "generated_at", "created_at", "timestamp", "updated_at"):
            if key in report and report.get(key) not in (None, ""):
                parsed = cls._coerce_report_timestamp(report.get(key))
                if parsed is None:
                    return False, f"research report timestamp is invalid: {key}"
                generated_values.append(parsed)
        for key in ("expires_epoch", "mt5_gate_expires_epoch", "expires_at"):
            if key in report and report.get(key) not in (None, ""):
                parsed = cls._coerce_report_timestamp(report.get(key))
                if parsed is None:
                    return False, f"research report expiry is invalid: {key}"
                expiry_values.append(parsed)

        if any(expiry <= now for expiry in expiry_values):
            return False, "research report is expired"
        if any(generated > now + 60.0 for generated in generated_values):
            return False, "research report timestamp is in the future"
        if generated_values and not expiry_values and min(generated_values) <= now - (90.0 * 60.0):
            return False, "research report is stale"
        if generated_values and expiry_values and min(expiry_values) <= max(generated_values):
            return False, "research report expires before it was generated"
        return True, ""

    @staticmethod
    def _safe_report_float(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) else default

    def evaluate_waterfall_pipeline(
        self,
        *,
        symbol: str,
        step1_talib: Step1TALibResult,
        kronos_report: dict[str, Any] | None,
        tradingagents_report: dict[str, Any] | None,
        l2_snapshot: dict[str, Any] | None,
        mt5_meta: dict[str, Any] | None = None,
        account_equity: float = 1000.0,
        market_csv: Path | None = None,
    ) -> WaterfallDecision:
        """Execute the 4-Step Waterfall Decision Pipeline & Veto Hierarchy without module deadlocks.

        Hierarchy:
        - Step 1 (TA-Lib): Fast Technical Filter (RSI/ATR/EMA Trend).
        - Step 2 (Kronos + Matrix): Primary Direction & Probability Generation.
        - Step 3 (L2 DOM Orderflow): Safety Gate / Veto Check (checks opposing walls only).
        - Step 4 (Gemini CIO - "ჯოი"): Synthesizes all steps and determines Win-Prob (0-100%),
          Risk Tier (<65% ABSTAIN, 65-80% 0.25-0.50%, >80% + L2 Wall 1.00-1.25%, hard cap 1.50%), and Lot Size.
        """
        normalized_symbol = symbol.strip().upper()
        kr_supplied = isinstance(kronos_report, dict) and bool(kronos_report)
        kr = kronos_report if kr_supplied else {}
        kr_valid, kr_reason = self._validate_research_report(
            kr,
            normalized_symbol,
            # Direct callers may still provide a metadata-free in-memory
            # report.  The orchestrator requires identity for disk reports.
            require_symbol=False,
        )
        if not kr_valid:
            kr = {}

        ta_supplied = isinstance(tradingagents_report, dict) and bool(tradingagents_report)
        ta = tradingagents_report if ta_supplied else {}
        ta_valid, ta_reason = self._validate_research_report(
            ta,
            normalized_symbol,
            require_symbol=False,
        ) if ta_supplied else (True, "")
        if ta_valid and ta_supplied and not any(
            key in ta for key in ("ai_action", "consensus_action", "mt5_gate_action", "action")
        ):
            ta_valid = False
            ta_reason = "TradingAgents report has no action field"

        # A failed, stale, mismatched, or otherwise missing research phase is
        # never allowed to fall through to the last cached BUY/SELL.
        research_evidence_valid = bool(kr_valid and kr) and bool(ta_valid)
        decision_obj = kr.get("decision") if isinstance(kr.get("decision"), dict) else {}
        matrix_obj = decision_obj.get("matrix") if isinstance(decision_obj.get("matrix"), dict) else {}
        analog_obj = decision_obj.get("analog") if isinstance(decision_obj.get("analog"), dict) else {}
        forecast_obj = decision_obj.get("forecast") if isinstance(decision_obj.get("forecast"), dict) else {}

        # --- STEP 2: Kronos + Matrix Primary Direction & Base Probability ---
        strict_action = str(kr.get("action") or "ABSTAIN").upper()
        matrix_dir = str(matrix_obj.get("direction") or "ABSTAIN").upper()
        forecast_dir = str(forecast_obj.get("direction") or "ABSTAIN").upper()
        analog_dir = str(analog_obj.get("direction") or "ABSTAIN").upper()

        matrix_score = self._safe_report_float(matrix_obj.get("score"), 0.0)
        buy_score = self._safe_report_float(matrix_obj.get("buy_score"), 0.0)
        sell_score = self._safe_report_float(matrix_obj.get("sell_score"), 0.0)
        analog_wr = self._safe_report_float(analog_obj.get("win_rate"), 0.0)
        path_agree = self._safe_report_float(forecast_obj.get("path_agreement"), 0.0)
        forecast_move_atr = self._safe_report_float(forecast_obj.get("move_atr"), 0.0)
        move_atr = abs(forecast_move_atr)
        kr_conf = self._safe_report_float(kr.get("confidence"), 0.0)
        hard_blocks = decision_obj.get("hard_blocks") if isinstance(decision_obj.get("hard_blocks"), list) else []

        # Step 2 uses qualified component votes rather than requiring all
        # three research modules to agree.  A component only counts when its
        # direction is backed by the evidence that module already reports.
        component_support = {
            "Matrix": matrix_dir in {"BUY", "SELL"}
            and matrix_score > 0.0
            and (
                (matrix_dir == "BUY" and buy_score > sell_score)
                or (matrix_dir == "SELL" and sell_score > buy_score)
            ),
            "Analogue": analog_dir in {"BUY", "SELL"} and analog_wr >= 0.55,
            "Forecast": forecast_dir in {"BUY", "SELL"}
            and path_agree >= 0.67
            and (
                (forecast_dir == "BUY" and forecast_move_atr > 0.0)
                or (forecast_dir == "SELL" and forecast_move_atr < 0.0)
            ),
        }
        component_directions = {
            "Matrix": matrix_dir,
            "Analogue": analog_dir,
            "Forecast": forecast_dir,
        }
        direction_support = {
            direction: [
                name
                for name, component_direction in component_directions.items()
                if component_support[name] and component_direction == direction
            ]
            for direction in ("BUY", "SELL")
        }
        buy_support = len(direction_support["BUY"])
        sell_support = len(direction_support["SELL"])
        if buy_support >= 2 and sell_support < 2:
            selected_direction = "BUY"
        elif sell_support >= 2 and buy_support < 2:
            selected_direction = "SELL"
        else:
            selected_direction = "ABSTAIN"
        confluence_count = max(buy_support, sell_support)
        confluence_label = f"{confluence_count}-of-3"

        # The upstream research gate historically reports ABSTAIN whenever a
        # component disagrees.  ABSTAIN is therefore tolerated here, but an
        # explicit opposite BUY/SELL action is treated as an inconsistent
        # report and remains fail-closed.
        strict_action_conflict = (
            strict_action in {"BUY", "SELL"}
            and selected_direction in {"BUY", "SELL"}
            and strict_action != selected_direction
        )
        talib_trend = str(getattr(step1_talib, "trend_bias", "NEUTRAL") or "NEUTRAL").upper()
        talib_trend_contradiction = (
            selected_direction in {"BUY", "SELL"}
            and talib_trend in {"BUY", "SELL"}
            and talib_trend != selected_direction
        )

        # These blocks describe the one dissenting component or the ordinary
        # pairwise disagreement that is expected under a 2-of-3 profile.  All
        # other hard blocks (cost, data integrity, news, etc.) still veto.
        blocking_hard_blocks = [
            str(block)
            for block in hard_blocks
            if not (
                str(block).strip().lower().startswith("matrix has no decisive breakout alignment")
                or str(block).strip().lower().startswith("analog pattern has no sufficiently strong direction")
                or str(block).strip().lower().startswith("kronos forecast has no sufficiently strong direction")
                or str(block).strip().lower().startswith("matrix and analog directions disagree")
                or str(block).strip().lower().startswith("matrix and kronos directions disagree")
            )
        ]
        step2_agrees = (
            selected_direction in {"BUY", "SELL"}
            and confluence_count >= 2
            and not strict_action_conflict
            and not talib_trend_contradiction
            and kr_conf > 0.0
            and not blocking_hard_blocks
        )

        evidence_reasons: list[str] = []
        if not kr_valid or not kr:
            evidence_reasons.append(kr_reason or "Kronos/Matrix report is missing or invalid")
        if not ta_valid:
            evidence_reasons.append(ta_reason or "TradingAgents report is missing or invalid")
        if confluence_count < 2:
            evidence_reasons.append("Step 2 requires at least 2-of-3 qualified Matrix/Analogue/Forecast votes")
        elif strict_action_conflict:
            evidence_reasons.append("Step 2 report action contradicts the selected component direction")
        elif talib_trend_contradiction:
            evidence_reasons.append(
                f"TA-Lib trend {talib_trend} contradicts the selected Step 2 direction {selected_direction}"
            )
        elif blocking_hard_blocks:
            evidence_reasons.extend(blocking_hard_blocks[:2])
        elif not step2_agrees and kr_conf <= 0.0:
            evidence_reasons.append("Step 2 confidence evidence is unavailable")
        if not getattr(step1_talib, "passed", False):
            evidence_reasons.append("TA-Lib Step 1 evidence is unavailable")
        if not isinstance(l2_snapshot, dict) or not l2_snapshot:
            evidence_reasons.append("L2 Step 3 evidence is unavailable")

        # Step 2 is a qualified 2-of-3 confluence gate, never a weighted vote.
        # Step 1 and Step 3 may veto/support that direction but may not
        # manufacture one.
        step1_available = bool(getattr(step1_talib, "passed", False))
        l2_available = False
        if isinstance(l2_snapshot, dict) and l2_snapshot:
            try:
                mid_price = float(l2_snapshot.get("mid_price") or 0.0)
                best_bid = float(l2_snapshot.get("best_bid") or 0.0)
                best_ask = float(l2_snapshot.get("best_ask") or 0.0)
            except (TypeError, ValueError):
                mid_price = best_bid = best_ask = 0.0
            empty_book = str(l2_snapshot.get("veto_reason") or "").strip().lower() == "l2 orderbook is empty"
            has_price_evidence = mid_price > 0.0 and best_bid > 0.0 and best_ask > 0.0
            has_book_signal = bool(
                str(l2_snapshot.get("l2_direction") or "").upper() in {"BUY", "SELL"}
                or l2_snapshot.get("nearest_buy_wall")
                or l2_snapshot.get("nearest_sell_wall")
                or l2_snapshot.get("strongest_buy_wall")
                or l2_snapshot.get("strongest_sell_wall")
                or self._safe_report_float(l2_snapshot.get("confluence_score"), 0.0) > 0.0
            )
            l2_available = not empty_book and (has_price_evidence or has_book_signal)
        candidate_dir = (
            selected_direction
            if step2_agrees and research_evidence_valid and step1_available and l2_available
            else "ABSTAIN"
        )

        # Base Win Probability from the validated Step-2 report.
        base_prob = max(68.0, min(92.0, kr_conf * 100.0)) if candidate_dir in {"BUY", "SELL"} else 45.0

        # Step 1 (TA-Lib) synergy or Exhaustion Veto
        talib_vetoed = bool(talib_trend_contradiction)
        if candidate_dir == "BUY":
            if step1_talib.veto_buy:
                talib_vetoed = True
                base_prob = min(base_prob, 48.0)
            elif step1_talib.trend_bias == "BUY":
                base_prob += 5.5
        elif candidate_dir == "SELL":
            if step1_talib.veto_sell:
                talib_vetoed = True
                base_prob = min(base_prob, 48.0)
            elif step1_talib.trend_bias == "SELL":
                base_prob += 5.5

        # --- STEP 3: L2 DOM Orderflow Safety Gate (Veto Check & Wall Support) ---
        l2 = l2_snapshot if isinstance(l2_snapshot, dict) else {}
        l2_dir = str(l2.get("l2_direction") or "NEUTRAL").upper()
        l2_conf = self._safe_report_float(l2.get("confluence_score"), 0.0)
        if l2_conf <= 1.0:
            l2_conf_pct = l2_conf * 100.0
        else:
            l2_conf_pct = l2_conf
        veto_buy_l2 = bool(l2.get("veto_buy", False))
        veto_sell_l2 = bool(l2.get("veto_sell", False))
        l2_veto_reason = str(l2.get("veto_reason") or "")

        l2_vetoed = False
        l2_wall_support = False
        if candidate_dir == "BUY":
            if veto_buy_l2:
                l2_vetoed = True
                base_prob = min(base_prob, 42.0)
            else:
                has_buy_wall = bool(l2.get("nearest_buy_wall") or l2.get("strongest_buy_wall"))
                if l2_dir == "BUY" and (has_buy_wall or l2_conf_pct >= 62.0):
                    l2_wall_support = True
                    base_prob += 7.5
        elif candidate_dir == "SELL":
            if veto_sell_l2:
                l2_vetoed = True
                base_prob = min(base_prob, 42.0)
            else:
                has_sell_wall = bool(l2.get("nearest_sell_wall") or l2.get("strongest_sell_wall"))
                if l2_dir == "SELL" and (has_sell_wall or l2_conf_pct >= 62.0):
                    l2_wall_support = True
                    base_prob += 7.5

        # Optional TradingAgents Macro/Sentiment bonus.  A supplied but stale
        # or mismatched report already made ``research_evidence_valid`` false.
        ta_act = str(ta.get("ai_action") or ta.get("consensus_action") or "ABSTAIN").upper()
        if ta_act == candidate_dir and candidate_dir in {"BUY", "SELL"}:
            base_prob += 4.5

        win_prob_pct = round(max(5.0, min(97.0, base_prob)), 1)
        if talib_vetoed or l2_vetoed or candidate_dir not in {"BUY", "SELL"}:
            win_prob_pct = min(win_prob_pct, 58.0)

        # Extract symbol pricing & contract specs
        meta = mt5_meta if isinstance(mt5_meta, dict) else {}
        digits = int(meta.get("digits") or l2.get("digits") or 2)
        point = float(meta.get("point") or l2.get("point") or 0.01)
        bid = float(meta.get("bid") or l2.get("best_bid") or step1_talib.close or 0.0)
        ask = float(meta.get("ask") or l2.get("best_ask") or step1_talib.close or 0.0)
        entry_px = ask if candidate_dir == "BUY" else bid
        if entry_px <= 0:
            entry_px = step1_talib.close
        atr_val = float(meta.get("atr_m5") or step1_talib.atr_14 or max(point * 30, entry_px * 0.001))

        sizing = self.compute_dynamic_risk_and_lot(
            win_prob_pct,
            l2_wall_support=l2_wall_support,
            equity=account_equity,
            atr=atr_val,
            entry_price=entry_px,
            side=candidate_dir,
            digits=digits,
            point=point,
            tick_value=float(meta.get("trade_tick_value") or 1.0),
            tick_size=float(meta.get("trade_tick_size") or point),
            contract_size=float(meta.get("trade_contract_size") or 100.0),
            volume_min=float(meta.get("volume_min") or 0.01),
            volume_max=float(meta.get("volume_max") or 50.0),
            volume_step=float(meta.get("volume_step") or 0.01),
        )

        final_action = str(sizing["action"])
        effective_research_evidence_valid = bool(
            research_evidence_valid and step2_agrees and step1_available and l2_available
        )
        step2_summary = {
            "candidate_direction": candidate_dir,
            "selected_direction": selected_direction,
            "strict_gate_action": strict_action,
            "matrix_direction": matrix_dir,
            "matrix_score": round(matrix_score, 2),
            "buy_score": round(buy_score, 1),
            "sell_score": round(sell_score, 1),
            "analog_direction": analog_dir,
            "analog_win_rate_pct": round(analog_wr * 100.0, 1),
            "forecast_direction": forecast_dir,
            "forecast_path_agreement_pct": round(path_agree * 100.0, 1),
            "forecast_move_atr": round(move_atr, 2),
            "confluence_count": confluence_count,
            "confluence_label": confluence_label,
            "confluence": confluence_label,
            "supporting_components": direction_support.get(selected_direction, []),
            "component_support": component_support,
            "talib_trend": talib_trend,
            "talib_trend_contradiction": talib_trend_contradiction,
            "components_agree": step2_agrees,
            "evidence_valid": effective_research_evidence_valid,
            "evidence_reasons": evidence_reasons,
        }
        step3_summary = {
            "l2_direction": l2_dir,
            "confluence_score_pct": round(l2_conf_pct, 1),
            "veto_buy": veto_buy_l2,
            "veto_sell": veto_sell_l2,
            "vetoed": l2_vetoed,
            "l2_wall_support": l2_wall_support,
            "veto_reason": l2_veto_reason if l2_vetoed else "",
        }

        if final_action in {"BUY", "SELL"}:
            rationale_ka = (
                f"🤖 ჯოი (Gemini Quant CIO): Waterfall იერარქია წარმატებით დადასტურდა -> {final_action} ({symbol}) | "
                f"Win-Probability: {win_prob_pct:.1f}% | რისკის რეჟიმი: {sizing['risk_tier']} ({sizing['risk_pct']:.2f}% = ${sizing['risk_usd']:.2f}) | "
                f"რეკომენდებული ლოტი: {sizing['lot_size']:.2f} | SL: {sizing['sl_price']} | TP: {sizing['tp_price']} | "
                f"Break-Even (+1x ATR): {sizing['be_trigger_price']} | 50% Partial Close (+2x ATR): {sizing['partial_close_price']}."
            )
        elif l2_vetoed:
            rationale_ka = (
                f"🛑 ჯოი (Gemini Quant CIO): Step 3 L2 DOM Safety Gate-მა დაბლოკა {candidate_dir} სიგნალი — "
                f"წინააღმდეგობაზე დგას მსხვილი ლიკვიდობის კედელი ({l2_veto_reason}). საბოლოო გადაწყვეტილება: ABSTAIN."
            )
        elif talib_trend_contradiction:
            rationale_ka = (
                f"🛑 ჯოი (Gemini Quant CIO): TA-Lib ტრენდი ({talib_trend}) ეწინააღმდეგება "
                f"Step 2-ის {selected_direction} მიმართულებას. საბოლოო გადაწყვეტილება: ABSTAIN."
            )
        elif talib_vetoed:
            rationale_ka = (
                f"🛑 ჯოი (Gemini Quant CIO): Step 1 TA-Lib ფილტრმა დააფიქსირა RSI გადახურება ({step1_talib.veto_reason}). "
                f"საბოლოო გადაწყვეტილება: ABSTAIN."
            )
        elif evidence_reasons:
            rationale_ka = (
                f"🛑 ჯოი (Gemini Quant CIO): კვლევის მტკიცებულება არასაკმარისი ან არავალიდურია "
                f"({'; '.join(evidence_reasons[:3])}). საბოლოო გადაწყვეტილება: ABSTAIN."
            )
        else:
            rationale_ka = (
                f"⏳ ჯოი (Gemini Quant CIO): მიმდინარე Win-Probability ({win_prob_pct:.1f}%) ნაკლებია 65%-იან ზღვარზე "
                f"(კანდიდატი: {candidate_dir}, TA-Lib: {step1_talib.trend_bias}, Kronos: {forecast_dir}, Matrix: {matrix_dir}). "
                f"საბოლოო გადაწყვეტილება: ABSTAIN (უსაფრთხო მოლოდინი)."
            )

        step4_summary = {
            "cio_agent": self.PERSONA_NAME,
            "final_action": final_action,
            "win_probability_pct": win_prob_pct,
            "risk_tier": sizing["risk_tier"],
            "risk_pct": sizing["risk_pct"],
            "risk_usd": sizing["risk_usd"],
            "realized_risk_usd": sizing.get("realized_risk_usd", 0.0),
            "realized_risk_pct": sizing.get("realized_risk_pct", 0.0),
            "max_risk_pct": sizing.get("max_risk_pct", MAX_RISK_PCT),
            "recommended_lot": sizing["lot_size"],
            "sl_price": sizing["sl_price"],
            "tp_price": sizing["tp_price"],
            "sl_distance": sizing.get("sl_distance", 0.0),
            "tp_distance": sizing.get("tp_distance", 0.0),
            "sl_atr_multiple": sizing.get("sl_atr_multiple", 0.0),
            "tp_atr_multiple": sizing.get("tp_atr_multiple", 0.0),
            "reward_risk_ratio": sizing.get("reward_risk_ratio", 0.0),
            "rr_ratio": sizing.get("rr_ratio", 0.0),
            "risk_constraint_status": sizing.get("risk_constraint_status", "ABSTAIN"),
            "be_trigger_price": sizing["be_trigger_price"],
            "partial_close_price": sizing["partial_close_price"],
            "trailing_distance": sizing["trailing_distance"],
            "autonomous_mode": self.autonomous_mode,
            "rationale_ka": rationale_ka,
        }

        decision = WaterfallDecision(
            symbol=symbol,
            final_action=final_action,
            win_probability_pct=win_prob_pct,
            risk_tier=str(sizing["risk_tier"]),
            risk_pct=float(sizing["risk_pct"]),
            recommended_lot=float(sizing["lot_size"]),
            sl_price=float(sizing["sl_price"]),
            tp_price=float(sizing["tp_price"]),
            be_trigger_price=float(sizing["be_trigger_price"]),
            partial_close_price=float(sizing["partial_close_price"]),
            trailing_distance=float(sizing["trailing_distance"]),
            l2_wall_support=l2_wall_support,
            l2_vetoed=l2_vetoed,
            talib_vetoed=talib_vetoed,
            step1_talib=step1_talib.to_dict(),
            step2_kronos_matrix=step2_summary,
            step3_l2_safety=step3_summary,
            step4_gemini_cio=step4_summary,
            rationale_ka=rationale_ka,
            research_evidence_valid=effective_research_evidence_valid,
        )
        self._last_decision = decision.to_dict()
        return decision

    def _call_gemini_rest(
        self,
        system_instruction: str,
        user_prompt: str,
        timeout: float = 18.0,
        *,
        json_mode: bool = False,
        max_tokens: int = 4096,
        temperature: float = 0.25,
    ) -> str | None:
        """Call the Gemini REST API, falling through working models. Returns None on any failure."""
        api_key = self._get_api_key()
        if not api_key or api_key.startswith("your_"):
            self.last_gemini_error = "GEMINI_API_KEY not configured"
            return None

        gen_cfg: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        deadline = time.monotonic() + max(3.0, float(timeout))
        for model_name in self._model_candidates():
            remaining = deadline - time.monotonic()
            if remaining < 2.0:
                self.last_gemini_error = self.last_gemini_error or "timeout budget exhausted"
                break
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                data=raw_body,
                # Key in header (not URL) so it never leaks into logs / tracebacks.
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as http_exc:
                code = int(getattr(http_exc, "code", 0) or 0)
                self.last_gemini_error = f"{model_name}: HTTP {code}"
                if code == 404:
                    self._dead_models.add(model_name)
                    if self.active_model == model_name:
                        self.active_model = None
                    continue
                if code in {401, 403}:
                    # Unauthorized key -> no other model will work either.
                    return None
                if code == 400:
                    try:
                        body_txt = http_exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        body_txt = ""
                    if "API key" in body_txt or "API_KEY" in body_txt:
                        self.last_gemini_error = "GEMINI_API_KEY invalid (HTTP 400)"
                        return None
                continue  # 400 model-specific / 429 quota / 5xx -> try next model
            except Exception as exc:  # timeout, DNS, SSL...
                self.last_gemini_error = f"{model_name}: {type(exc).__name__}"
                continue

            candidates = data.get("candidates") or []
            if not candidates:
                self.last_gemini_error = f"{model_name}: empty response"
                continue
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip()
            if not text:
                self.last_gemini_error = f"{model_name}: no text ({candidates[0].get('finishReason')})"
                continue
            self.active_model = model_name
            self.last_gemini_ok_at = datetime.now(timezone.utc).isoformat()
            self.last_gemini_error = ""
            return text
        return None

    @staticmethod
    def _parse_json_reply(raw: str) -> dict[str, Any]:
        txt = (raw or "").strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            if txt.lower().startswith("json"):
                txt = txt[4:]
        start, end = txt.find("{"), txt.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(txt[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def review_trade_with_gemini(
        self,
        decision: "WaterfallDecision | dict[str, Any]",
        context: dict[str, Any] | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        """Step 4b — real Gemini CIO review of a quant signal before any autonomous execution.

        Safety contract:
        - Gemini can only APPROVE or REJECT, and can only REDUCE size (risk_multiplier 0.25-1.0), never increase it.
        - If Gemini is unreachable / returns garbage -> REJECT (Default Safe Action = ABSTAIN).
        """
        wf = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision or {})
        symbol = str(wf.get("symbol") or "").upper()
        action = str(wf.get("final_action") or "ABSTAIN").upper()
        review: dict[str, Any] = {
            "symbol": symbol,
            "action": action,
            "verdict": "REJECT",
            "risk_multiplier": 0.0,
            "confidence_pct": 0.0,
            "reason_ka": "",
            "model": None,
            "source": "",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }

        if action not in {"BUY", "SELL"}:
            review.update(source="no_signal", reason_ka="კვანტურ სიგნალს არ აქვს მიმართულება (ABSTAIN) — Gemini-ს განხილვა არ სჭირდება.")
            self.last_cio_review = review
            return review

        if not self.is_configured:
            if self.gemini_review_required:
                review.update(source="no_key", reason_ka="GEMINI_API_KEY არ არის კონფიგურირებული — უსაფრთხოებისთვის ABSTAIN.")
            else:
                review.update(verdict="APPROVE", risk_multiplier=1.0, source="quant_only", reason_ka="Gemini review გამორთულია — მხოლოდ კვანტური სიგნალი.")
            self.last_cio_review = review
            return review

        system_prompt = (
            "You are 'Joey' (ჯოი), the Chief Risk Officer of the NewEra AI Quant trading desk. "
            "A deterministic quant pipeline (TA-Lib -> Kronos/Matrix + 8-NN analogues -> L2 DOM veto -> risk sizing) "
            "has produced a trade signal for a DEMO account. Your job is a final, skeptical sanity review.\n"
            "Rules:\n"
            "- You may only APPROVE or REJECT. You may only REDUCE size via risk_multiplier in [0.25, 1.0]; never increase.\n"
            "- REJECT if: modules conflict (e.g. TA-Lib trend vs signal), RSI exhausted in signal direction, "
            "L2 veto/opposing wall, SL/TP geometry is illogical (BUY needs SL < entry < TP; SELL the reverse), "
            "reward:risk < 1.5, SL distance > 2.5 ATR, risk > 1.5% of equity, spread too wide vs ATR, "
            "or the evidence is weak/ambiguous.\n"
            "- When in doubt, REJECT. Capital preservation beats activity.\n"
            "Return ONLY JSON: {\"verdict\": \"APPROVE\"|\"REJECT\", \"confidence_pct\": 0-100, "
            "\"risk_multiplier\": 0.25-1.0, \"reason_ka\": \"<= 2 short sentences in Georgian\"}"
        )
        user_prompt = json.dumps(
            {"signal": wf, "market_context": context or {}},
            ensure_ascii=False,
            default=str,
        )
        raw = self._call_gemini_rest(
            system_prompt,
            user_prompt,
            timeout=timeout,
            json_mode=True,
            max_tokens=4096,
            temperature=0.1,
        )
        if raw is None:
            review.update(
                source="gemini_unavailable",
                reason_ka=f"Gemini API მიუწვდომელია ({self.last_gemini_error or 'unknown'}) — უსაფრთხოებისთვის ABSTAIN.",
            )
            self.last_cio_review = review
            return review

        parsed = self._parse_json_reply(raw)
        verdict = str(parsed.get("verdict") or "").strip().upper()
        try:
            conf = max(0.0, min(100.0, float(parsed.get("confidence_pct") or 0.0)))
        except (TypeError, ValueError):
            conf = 0.0
        try:
            mult = float(parsed.get("risk_multiplier") if parsed.get("risk_multiplier") is not None else 1.0)
        except (TypeError, ValueError):
            mult = 0.0
        reason = str(parsed.get("reason_ka") or "").strip()[:400]

        if verdict == "APPROVE" and mult > 0:
            review.update(verdict="APPROVE", risk_multiplier=round(max(0.25, min(1.0, mult)), 2))
        else:
            review.update(verdict="REJECT", risk_multiplier=0.0)
            if verdict not in {"APPROVE", "REJECT"}:
                reason = reason or "Gemini-ს პასუხი ვერ წავიკითხე (არავალიდური JSON) — უსაფრთხოებისთვის ABSTAIN."
        review.update(
            confidence_pct=round(conf, 1),
            reason_ka=reason or ("დადასტურებულია." if review["verdict"] == "APPROVE" else "უარყოფილია."),
            model=self.active_model,
            source="gemini_api",
        )
        self.last_cio_review = review
        return review

    def analyze_current_setup(self, context: dict[str, Any]) -> dict[str, Any]:
        """One-click comprehensive setup analysis in Georgian ('ჯოი' — Gemini Quant Co-Pilot)."""
        symbol = str(context.get("symbol") or "XAUUSD").upper()
        waterfall = context.get("waterfall") if isinstance(context.get("waterfall"), dict) else (self._last_decision or {})
        l2 = context.get("l2_orderflow") if isinstance(context.get("l2_orderflow"), dict) else {}
        kronos = context.get("kronos_report") if isinstance(context.get("kronos_report"), dict) else {}
        mt5_sym = context.get("mt5_symbol") if isinstance(context.get("mt5_symbol"), dict) else {}
        positions = context.get("positions") if isinstance(context.get("positions"), list) else []

        system_prompt = (
            "შენ ხარ 'ჯოი' (Joey) — NewEra Trading Bot-ის Chief Investment Officer (CIO) და კვანტური პორტფელის მენეჯერი. "
            "უპასუხე ქართულ ენაზე, ლაკონურად, სტრუქტურირებულად და პროფესიონალური კვანტ-ტრეიდერის ტონით. "
            "გააანალიზე 4-საფეხურიანი Waterfall Pipeline:\n"
            "1. Step 1 (TA-Lib ტექნიკური ფილტრი: RSI, ATR, EMA ტრენდი, MACD, ADX)\n"
            "2. Step 2 (Kronos Transformer + Matrix Breakout + 8-NN Historical Analogue)\n"
            "3. Step 3 (L2 Orderbook DOM & Liquidity Radar: Bid/Ask იმბალანსი, ვეშაპის კედლები, Veto სტატუსი)\n"
            "4. Step 4 (ჯოის საბოლოო ვერდიქტი: BUY/SELL/ABSTAIN, Win-Probability %, რისკის ზომა 0.25%-1.50% cap-ით, Lot Size, "
            "SL, TP, +1x ATR Break-Even და +2x ATR 50% Partial Close გეგმა)."
        )
        compact_ctx = {
            "symbol": symbol,
            "quote": {
                "bid": mt5_sym.get("bid") or l2.get("best_bid"),
                "ask": mt5_sym.get("ask") or l2.get("best_ask"),
                "spread_points": mt5_sym.get("spread_points"),
                "atr_m5": mt5_sym.get("atr_m5"),
            },
            "waterfall_decision": waterfall,
            "l2_dom_summary": {
                "l2_direction": l2.get("l2_direction"),
                "bid_pressure_pct": l2.get("bid_pressure_pct"),
                "ask_pressure_pct": l2.get("ask_pressure_pct"),
                "confluence_score": l2.get("confluence_score"),
                "veto_buy": l2.get("veto_buy"),
                "veto_sell": l2.get("veto_sell"),
                "veto_reason": l2.get("veto_reason"),
                "strongest_buy_wall": l2.get("strongest_buy_wall"),
                "strongest_sell_wall": l2.get("strongest_sell_wall"),
            },
            "kronos_summary": {
                "action": kronos.get("action"),
                "score": kronos.get("score"),
                "confidence": kronos.get("confidence"),
                "decision": kronos.get("decision"),
            },
            "open_positions": positions,
        }
        user_prompt = (
            f"გთხოვ გააანალიზო {symbol}-ის მიმდინარე სეტაპი რეალურ დროში:\n"
            f"{json.dumps(compact_ctx, ensure_ascii=False, indent=2)}"
        )

        llm_reply = self._call_gemini_rest(system_prompt, user_prompt, timeout=22.0)
        from_gemini = bool(llm_reply)
        if not llm_reply:
            llm_reply = self._build_deterministic_georgian_analysis(symbol, compact_ctx)

        entry = {
            "role": "assistant",
            "speaker": self.PERSONA_NAME,
            "symbol": symbol,
            "text": llm_reply,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "gemini_api" if from_gemini else "quant_fallback",
            "model": self.active_model if from_gemini else "quant-fallback (Gemini offline)",
        }
        self._chat_history.append({"role": "assistant", "text": llm_reply})
        return entry

    def chat(self, message: str, context: dict[str, Any]) -> dict[str, Any]:
        """Interactive Co-Pilot conversation with 'ჯოი' in Georgian."""
        clean_msg = message.strip()
        if not clean_msg:
            return {
                "role": "assistant",
                "speaker": self.PERSONA_NAME,
                "text": "გისმენ ლევან! მკითხე ნებისმიერ წყვილზე, L2 Orderbook კედლებზე, Kronos-ის პროგნოზზე ან რისკ-მენეჯმენტზე.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        self._chat_history.append({"role": "user", "text": clean_msg})
        recent_history = self._chat_history[-6:]

        symbol = str(context.get("symbol") or "XAUUSD").upper()
        system_prompt = (
            "შენ ხარ 'ჯოი' (Joey) — NewEra Trading Bot-ის AI Quant Co-Pilot და პორტფელის მენეჯერი. "
            "მომხმარებელია ლევანი (პლატფორმის არქიტექტორი). უპასუხე ქართულად, მეგობრულად და ზუსტი კვანტური არგუმენტებით. "
            "ყოველთვის დაეყრდენი მოწოდებულ Live MT5, TA-Lib, Kronos+Matrix და L2 DOM მონაცემებს."
        )
        prompt_text = (
            f"მიმდინარე აქტივი: {symbol}\n"
            f"ბაზრის და Waterfall Pipeline-ის კონტექსტი:\n{json.dumps(context, ensure_ascii=False)}\n\n"
            f"დიალოგის ისტორია:\n{json.dumps(recent_history, ensure_ascii=False)}\n\n"
            f"ლევანის შეკითხვა: {clean_msg}"
        )
        llm_reply = self._call_gemini_rest(system_prompt, prompt_text, timeout=22.0)
        from_gemini = bool(llm_reply)
        if not llm_reply:
            llm_reply = self._build_deterministic_georgian_analysis(symbol, context, user_question=clean_msg)

        self._chat_history.append({"role": "assistant", "text": llm_reply})
        return {
            "role": "assistant",
            "speaker": self.PERSONA_NAME,
            "symbol": symbol,
            "text": llm_reply,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "gemini_api" if from_gemini else "quant_fallback",
            "model": self.active_model if from_gemini else "quant-fallback (Gemini offline)",
        }

    def _build_deterministic_georgian_analysis(
        self,
        symbol: str,
        ctx: dict[str, Any],
        *,
        user_question: str = "",
    ) -> str:
        wf = ctx.get("waterfall_decision") or ctx.get("waterfall") or self._last_decision or {}
        steps = wf.get("steps") if isinstance(wf.get("steps"), dict) else {}
        s1 = steps.get("step1_talib") if isinstance(steps.get("step1_talib"), dict) else {}
        s2 = steps.get("step2_kronos_matrix") if isinstance(steps.get("step2_kronos_matrix"), dict) else {}
        s3 = steps.get("step3_l2_safety") if isinstance(steps.get("step3_l2_safety"), dict) else {}
        l2 = ctx.get("l2_dom_summary") or ctx.get("l2_orderflow") or {}
        quote = ctx.get("quote") or ctx.get("mt5_symbol") or {}

        act = wf.get("final_action", "ABSTAIN")
        prob = wf.get("win_probability_pct", 50.0)
        tier = wf.get("risk_tier", "ABSTAIN (<65%)")
        lot = wf.get("recommended_lot", 0.01)
        sl = wf.get("sl_price", 0.0)
        tp = wf.get("tp_price", 0.0)
        be = wf.get("be_trigger_price", 0.0)
        part = wf.get("partial_close_price", 0.0)

        lines = [
            f"🤖 **ჯოი (Gemini Quant CIO) — {symbol} სეტაპის სრული კვანტური ანალიზი**",
        ]
        if user_question:
            lines.append(f"💬 *შენი კითხვა:* \"{user_question}\"\n")

        lines.extend(
            [
                f"1️⃣ **Step 1 — TA-Lib ტექნიკური ფილტრი:**",
                f"   • ტრენდის იმპულსი: **{s1.get('trend_bias', 'NEUTRAL')}** | RSI(14): **{s1.get('rsi_14', 50.0)}** | ATR(14): **{s1.get('atr_14', quote.get('atr_m5', '--'))}**",
                f"   • EMA(20/50/200): `{s1.get('ema_20', '--')}` / `{s1.get('ema_50', '--')}` / `{s1.get('ema_200', '--')}` | ADX: `{s1.get('adx_14', '--')}`",
                "",
                f"2️⃣ **Step 2 — Kronos Transformer & Matrix Breakout:**",
                f"   • კანდიდატი მიმართულება: **{s2.get('candidate_direction', 'ABSTAIN')}** | "
                f"კონფლუენსი: **{s2.get('confluence_label', '--')}** | Matrix ქულა: **{s2.get('matrix_score', 0.0)}/8.0**",
                f"   • 8-NN ისტორიული ანალოგების Win-Rate: **{s2.get('analog_win_rate_pct', 50.0)}%** | Kronos Path Agreement: **{s2.get('forecast_path_agreement_pct', 50.0)}%**",
                "",
                f"3️⃣ **Step 3 — L2 DOM Orderflow & Liquidity Safety Gate:**",
                f"   • ჭიქის (DOM) წნეხი: Bid **{l2.get('bid_pressure_pct', 50.0)}%** vs Ask **{l2.get('ask_pressure_pct', 50.0)}%** (მიმართულება: **{s3.get('l2_direction', l2.get('l2_direction', 'NEUTRAL'))}**)",
                f"   • L2 Wall Support: **{'კი ✅' if wf.get('l2_wall_support') else 'არა'}** | Veto ბლოკი: **{'აქტიურია 🛑 (' + str(s3.get('veto_reason')) + ')' if wf.get('l2_vetoed') else 'სუფთაა ✅'}**",
                "",
                f"4️⃣ **Step 4 — ჯოის (CIO) საბოლოო გადაწყვეტილება და რისკ-გეგმა:**",
                f"   • საბოლოო ვერდიქტი: **{act}** | Win-Probability: **{prob}%**",
                f"   • რისკის კატეგორია: **{tier}** | რეკომენდებული მოცულობა: **{lot} Lot**",
                f"   • Stop Loss: `{sl}` | Take Profit: `{tp}`",
                f"   • დინამიკური მართვა: **Break-Even (+1.0x ATR)** -> `{be}` | **50% Partial Close (+2.0x ATR)** -> `{part}`.",
            ]
        )
        return "\n".join(lines)
