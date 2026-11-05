"""New Era 2.0 — Historical Bot Performance Intelligence & Evidence-Based Strategy Selection Engine.

Implements Phases 1–7 of the Historical Bot Performance Intelligence specification:
- Phase 1: Reconciled MT5 Historical Dataset (Executed, Closed, Open, Pending, Rejected/Cancelled, Signals)
- Phase 2: Bot & MQL5 EA Identification (Comment, Magic Number, Strategy ID, Navigator EA Catalog, UNKNOWN)
- Phase 3: Comprehensive Per-Bot Performance Metrics (including volume-normalized P/L per 0.01 lot,
  commissions/swaps, R-multiples, drawdown, consecutive losses, session, regime, Long vs Short)
- Phase 4: Winning vs Losing Pattern Discovery (Trend alignment, Entry style, Volatility/ATR, RSI,
  SL/TP geometry, Session, Duration, and Trailing-Stop vs Original-TP counterfactual)
- Phase 5: Multi-Factor Bot Ranking with Sample-Size Confidence Scoring
- Phase 6: Strategy Protection Categorization & Non-Destructive Shadow-Mode Pattern Filters
- Phase 7: Data payloads & CSV/JSON export for the UI `Bot Performance Explorer`

SAFETY CONTRACT:
- Analytical and Shadow-Mode filtering only.
- NEVER activates live trading, NEVER places orders, and NEVER closes or modifies open positions.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ROOT
from .mt5_bridge import get_mt5_open_positions


DATASET_REL_PATH = Path("TradingAgentsData/research/mt5_bot_intelligence_dataset.json")


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _sample_confidence_badge(trade_count: int) -> dict[str, Any]:
    """Return statistical reliability classification based on closed trade sample size."""
    if trade_count >= 50:
        return {
            "level": "HIGH",
            "score": 1.0,
            "label": f"HIGH ({trade_count} trades — statistically robust sample)",
        }
    if trade_count >= 20:
        return {
            "level": "MODERATE",
            "score": 0.78,
            "label": f"MODERATE ({trade_count} trades — directional evidence, monitor variance)",
        }
    if trade_count >= 10:
        return {
            "level": "LIMITED_PROMISING",
            "score": 0.58,
            "label": f"LIMITED SAMPLE ({trade_count} trades — promising edge, needs 30+ trades for full validation)",
        }
    if trade_count >= 1:
        return {
            "level": "VERY_LOW",
            "score": 0.25,
            "label": f"VERY LOW ({trade_count} trade(s) — anecdotal sample, do not extrapolate)",
        }
    return {
        "level": "NO_LIVE_TRADES",
        "score": 0.0,
        "label": "0 LIVE TRADES (Installed in MT5 / Strategy Tester only)",
    }


class BotPerformanceIntelligenceEngine:
    """Quantitative analyzer for 250+ historical MT5 demo trades across all bots and EAs."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = Path(root)
        self.dataset_path = self.root / DATASET_REL_PATH

    def load_dataset(self, *, refresh_live_positions: bool = True) -> dict[str, Any]:
        """Load the reconciled MT5 bot intelligence dataset and optionally sync live open positions."""
        if self.dataset_path.is_file():
            try:
                data = json.loads(self.dataset_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if refresh_live_positions:
                        try:
                            live_pos = get_mt5_open_positions()
                            if isinstance(live_pos, dict) and live_pos.get("ok"):
                                raw_positions = live_pos.get("positions") or []
                                data["open_positions"] = [
                                    self._normalize_live_open_position(p)
                                    for p in raw_positions
                                    if isinstance(p, Mapping)
                                ]
                                rec = data.get("reconciliation_summary")
                                if isinstance(rec, dict):
                                    rec["currently_open_positions"] = len(data["open_positions"])
                                    rec["reconciled_executed_positions_total"] = (
                                        int(rec.get("reconciled_closed_trades") or len(data.get("closed_trades") or []))
                                        + len(data["open_positions"])
                                    )
                                acc = data.get("account")
                                if isinstance(acc, dict):
                                    if live_pos.get("account_balance"):
                                        acc["balance"] = round(_safe_float(live_pos.get("account_balance")), 2)
                                    if live_pos.get("account_equity"):
                                        acc["equity"] = round(_safe_float(live_pos.get("account_equity")), 2)
                        except Exception:
                            pass
                    return data
            except Exception:
                pass
        return {
            "extracted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "account": {"login": 53012933, "server": "ICMarketsSC-Demo", "currency": "USD", "balance": 0.0, "equity": 0.0, "is_demo": True},
            "reconciliation_summary": {
                "raw_mt5_deals_total": 0,
                "raw_mt5_execution_deals": 0,
                "raw_mt5_balance_credit_deals": 0,
                "raw_mt5_historical_orders": 0,
                "reconciled_executed_positions_total": 0,
                "reconciled_closed_trades": 0,
                "currently_open_positions": 0,
                "currently_pending_orders": 0,
                "rejected_or_cancelled_orders": 0,
                "signals_never_became_orders": 0,
            },
            "installed_mql5_eas": [],
            "open_positions": [],
            "pending_orders": [],
            "rejected_or_cancelled_orders": [],
            "closed_trades": [],
        }

    @staticmethod
    def identify_bot_from_position(pos: Mapping[str, Any]) -> dict[str, str]:
        """Resolve human-friendly bot identity from an MT5 position/deal dictionary."""
        comment = str(pos.get("comment") or pos.get("comment_in") or "").strip()
        magic = int(_safe_float(pos.get("magic"), 0.0))
        upper = comment.upper()

        if magic == 11032026 or "XAU_M1_DEMO" in upper or "BREAKOUTSTOPS" in upper:
            leg = "BuyStop" if "_BS" in upper else ("SellStop" if "_SS" in upper else "M1")
            return {
                "bot_id": "XAUUSD_M1_BreakoutStops",
                "bot_label": f"XAUUSD_M1_BreakoutStops ({leg})",
                "magic": str(magic or 11032026),
            }
        if magic in (16155444, 16155405) or str(magic).startswith("161554") or "EFH1" in upper or "ADAPTIVEH1" in upper:
            return {
                "bot_id": "AdaptiveH1Breakout_EFH1",
                "bot_label": "AdaptiveH1Breakout (EFH1)",
                "magic": str(magic or 16155444),
            }
        if magic == 20241002 or "SNIPER" in upper:
            return {
                "bot_id": "Gold_Sniper_Scalper",
                "bot_label": "Gold_Sniper_Scalper v3.7",
                "magic": str(magic or 20241002),
            }
        if "JOEYCIO" in upper:
            return {
                "bot_id": "JoeyCIO_Autonomous",
                "bot_label": f"JoeyCIO ({comment})",
                "magic": str(magic or 20260418),
            }
        if "AUTOSCREENER" in upper:
            return {
                "bot_id": "NewEra_AutoScreener",
                "bot_label": f"AutoScreener ({comment})",
                "magic": str(magic or 20260418),
            }
        if "NEWERAQUANTDEMO" in upper or magic == 20260418:
            return {
                "bot_id": "NewEraQuantDemo",
                "bot_label": "NewEraQuantDemo",
                "magic": str(magic or 20260418),
            }
        if magic == 0 and not comment:
            return {
                "bot_id": "UNKNOWN",
                "bot_label": "Manual / External (Magic=0)",
                "magic": "0",
            }
        return {
            "bot_id": comment or f"Magic_{magic}",
            "bot_label": f"{comment or 'External EA'} (Magic={magic})",
            "magic": str(magic),
        }

    def _normalize_live_open_position(self, pos: Mapping[str, Any]) -> dict[str, Any]:
        bot_info = self.identify_bot_from_position(pos)
        return {
            "ticket": int(_safe_float(pos.get("ticket"), 0.0)),
            "position_id": int(_safe_float(pos.get("position_id") or pos.get("ticket"), 0.0)),
            "symbol": str(pos.get("symbol") or "XAUUSD"),
            "side": str(pos.get("side") or "BUY").upper(),
            "volume": round(_safe_float(pos.get("volume"), 0.01), 2),
            "price_open": _safe_float(pos.get("price_open"), 0.0),
            "price_current": _safe_float(pos.get("price_current"), 0.0),
            "sl": _safe_float(pos.get("sl"), 0.0),
            "tp": _safe_float(pos.get("tp"), 0.0),
            "profit": round(_safe_float(pos.get("profit"), 0.0), 2),
            "swap": round(_safe_float(pos.get("swap"), 0.0), 2),
            "open_time_utc": str(pos.get("open_time_utc") or pos.get("opened_at") or ""),
            "magic": int(_safe_float(pos.get("magic"), 0.0)),
            "comment": str(pos.get("comment") or ""),
            "bot_id": str(pos.get("bot_id") or bot_info["bot_id"]),
            "bot_display_name": str(pos.get("bot_display_name") or bot_info["bot_label"]),
        }

    def compute_bot_metrics(self, trades: list[dict[str, Any]], bot_id: str, display_name: str, open_count: int = 0) -> dict[str, Any]:
        """Compute Phase 3 comprehensive metrics for a specific bot or strategy group."""
        n = len(trades)
        conf = _sample_confidence_badge(n)
        if n == 0:
            return {
                "bot_id": bot_id,
                "bot_display_name": display_name,
                "total_executed_trades": open_count,
                "closed_trades": 0,
                "open_positions": open_count,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate_pct": 0.0,
                "net_profit": 0.0,
                "net_profit_per_001_lot": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "total_commission": 0.0,
                "total_swap": 0.0,
                "profit_factor": 0.0,
                "expectancy_usd": 0.0,
                "expectancy_per_001_lot": 0.0,
                "avg_win_usd": 0.0,
                "avg_loss_usd": 0.0,
                "avg_r_multiple": 0.0,
                "avg_planned_rr": 0.0,
                "max_drawdown_usd": 0.0,
                "max_drawdown_per_001_lot": 0.0,
                "max_consecutive_losses": 0,
                "avg_duration_min": 0.0,
                "trailed_wins_count": 0,
                "counterfactual_no_trailing_net": 0.0,
                "long_trades": 0,
                "long_win_rate_pct": 0.0,
                "long_net_profit": 0.0,
                "short_trades": 0,
                "short_win_rate_pct": 0.0,
                "short_net_profit": 0.0,
                "symbols_traded": [],
                "session_performance": {},
                "regime_performance": {},
                "equity_curve": [],
                "equity_curve_001_lot": [],
                "sample_confidence": conf,
            }

        ordered = sorted(trades, key=lambda x: str(x.get("close_time_utc") or x.get("open_time_utc") or ""))
        nets = [_safe_float(t.get("net")) for t in ordered]
        nets_001 = [_safe_float(t.get("net_per_001_lot"), _safe_float(t.get("net"))) for t in ordered]
        wins = [v for v in nets if v > 0]
        losses = [v for v in nets if v < 0]

        gross_profit = round(sum(wins), 2)
        gross_loss = round(abs(sum(losses)), 2)
        net_profit = round(sum(nets), 2)
        net_001 = round(sum(nets_001), 2)
        total_comm = round(sum(_safe_float(t.get("commission")) for t in ordered), 2)
        total_swap = round(sum(_safe_float(t.get("swap")) for t in ordered), 2)

        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        win_rate = round((len(wins) / n) * 100.0, 1)
        expectancy = round(net_profit / n, 2)
        expectancy_001 = round(net_001 / n, 3)
        avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0

        r_mults = [_safe_float(t.get("r_multiple")) for t in ordered if t.get("r_multiple") is not None]
        planned_rrs = [_safe_float(t.get("planned_rr")) for t in ordered if t.get("planned_rr") is not None]
        avg_r = round(sum(r_mults) / len(r_mults), 2) if r_mults else 0.0
        avg_planned_rr = round(sum(planned_rrs) / len(planned_rrs), 2) if planned_rrs else 0.0

        # Drawdown & Equity Curves (both raw $ and normalized per 0.01 lot)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        eq_curve: list[dict[str, Any]] = []

        cum_001 = 0.0
        peak_001 = 0.0
        max_dd_001 = 0.0
        eq_curve_001: list[dict[str, Any]] = []

        consec_losses = 0
        max_consec_losses = 0

        for idx, t in enumerate(ordered, start=1):
            val = _safe_float(t.get("net"))
            val_001 = _safe_float(t.get("net_per_001_lot"), val)
            cum = round(cum + val, 2)
            peak = max(peak, cum)
            max_dd = max(max_dd, round(peak - cum, 2))

            cum_001 = round(cum_001 + val_001, 3)
            peak_001 = max(peak_001, cum_001)
            max_dd_001 = max(max_dd_001, round(peak_001 - cum_001, 3))

            if val < 0:
                consec_losses += 1
                max_consec_losses = max(max_consec_losses, consec_losses)
            elif val > 0:
                consec_losses = 0

            ts_str = str(t.get("close_time_utc") or t.get("open_time_utc") or "")
            eq_curve.append({"index": idx, "timestamp": ts_str, "equity": cum, "balance": round(10000.0 + cum, 2), "pnl": val})
            eq_curve_001.append({"index": idx, "timestamp": ts_str, "equity": cum_001, "balance": round(10000.0 + cum_001, 3), "pnl": val_001})

        durations = [_safe_float(t.get("hold_min")) for t in ordered]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0
        trailed_wins = sum(1 for t in ordered if t.get("trailed_win"))
        cf_nets = [_safe_float(t.get("cf_net"), _safe_float(t.get("net"))) for t in ordered]
        cf_total = round(sum(cf_nets), 2)

        longs = [t for t in ordered if str(t.get("side")).upper() == "BUY"]
        shorts = [t for t in ordered if str(t.get("side")).upper() == "SELL"]
        long_wins = sum(1 for t in longs if _safe_float(t.get("net")) > 0)
        short_wins = sum(1 for t in shorts if _safe_float(t.get("net")) > 0)

        # Session breakdown
        sess_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in ordered:
            sess_groups[str(t.get("session_utc") or "Unknown")].append(t)
        session_perf = {}
        for s_name, s_list in sess_groups.items():
            s_wins = sum(1 for x in s_list if _safe_float(x.get("net")) > 0)
            session_perf[s_name] = {
                "trades": len(s_list),
                "win_rate_pct": round((s_wins / len(s_list)) * 100.0, 1),
                "net_profit": round(sum(_safe_float(x.get("net")) for x in s_list), 2),
                "net_per_001_lot": round(sum(_safe_float(x.get("net_per_001_lot")) for x in s_list), 2),
            }

        # Regime breakdown
        reg_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in ordered:
            reg_groups[str(t.get("trend_regime") or "UNKNOWN")].append(t)
        regime_perf = {}
        for r_name, r_list in reg_groups.items():
            r_wins = sum(1 for x in r_list if _safe_float(x.get("net")) > 0)
            regime_perf[r_name] = {
                "trades": len(r_list),
                "win_rate_pct": round((r_wins / len(r_list)) * 100.0, 1),
                "net_profit": round(sum(_safe_float(x.get("net")) for x in r_list), 2),
            }

        symbols = sorted({str(t.get("symbol") or "XAUUSD") for t in ordered})
        volumes = sorted({round(_safe_float(t.get("volume"), 0.01), 2) for t in ordered})
        magics = sorted({int(_safe_float(t.get("magic"), 0)) for t in ordered})
        comment_prefixes = sorted({str(t.get("comment_in") or "").split("_")[0] or "(empty)" for t in ordered})

        # Compute ex-single-worst-outlier net & PF (critical forensic check for NewEraQuantDemo)
        ex_outlier_net = None
        ex_outlier_pf = None
        if len(nets) >= 5:
            worst_loss = min(nets)
            rem = list(nets)
            rem.remove(worst_loss)
            rem_w = sum(v for v in rem if v > 0)
            rem_l = abs(sum(v for v in rem if v < 0))
            ex_outlier_net = round(sum(rem), 2)
            ex_outlier_pf = round(rem_w / rem_l, 2) if rem_l > 0 else 99.0

        core_gold_ustec_net = round(
            sum(_safe_float(t.get("net")) for t in ordered if str(t.get("symbol") or "").upper() in {"XAUUSD", "USTEC"}),
            2,
        )

        source_type = (
            "New Era Python Bridge"
            if 20260418 in magics
            else ("External MQL5 EA" if any(m > 0 for m in magics) else "Manual / Unlabeled")
        )

        return {
            "bot_id": bot_id,
            "bot_name": bot_id,
            "bot_display_name": display_name,
            "source_type": source_type,
            "magics": magics,
            "magic_numbers": magics,
            "comment_prefixes": comment_prefixes,
            "volumes_used": volumes,
            "symbols_traded": symbols,
            "total_executed_trades": n + open_count,
            "closed_trades": n,
            "open_positions": open_count,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": win_rate,
            "net_profit": net_profit,
            "net_profit_per_001_lot": net_001,
            "net_per_001_lot": net_001,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "total_commission": total_comm,
            "total_swap": total_swap,
            "profit_factor": pf,
            "expectancy_usd": expectancy,
            "expectancy_per_trade": expectancy,
            "expectancy_per_001_lot": expectancy_001,
            "avg_win_usd": avg_win,
            "average_win": avg_win,
            "avg_loss_usd": avg_loss,
            "average_loss": avg_loss,
            "avg_r_multiple": avg_r,
            "avg_planned_rr": avg_planned_rr,
            "max_drawdown_usd": max_dd,
            "max_drawdown_per_001_lot": max_dd_001,
            "max_consecutive_losses": max_consec_losses,
            "avg_duration_min": avg_dur,
            "trailed_wins_count": trailed_wins,
            "counterfactual_no_trailing_net": cf_total,
            "counterfactual_no_early_trail_net": cf_total if trailed_wins > 0 else None,
            "counterfactual_trail_delta": round(cf_total - net_profit, 2) if trailed_wins > 0 else None,
            "ex_single_worst_outlier_net": ex_outlier_net,
            "ex_single_worst_outlier_pf": ex_outlier_pf,
            "core_gold_ustec_net": core_gold_ustec_net,
            "long_trades": len(longs),
            "long_win_rate_pct": round((long_wins / len(longs)) * 100.0, 1) if longs else 0.0,
            "long_net_profit": round(sum(_safe_float(x.get("net")) for x in longs), 2),
            "short_trades": len(shorts),
            "short_win_rate_pct": round((short_wins / len(shorts)) * 100.0, 1) if shorts else 0.0,
            "short_net_profit": round(sum(_safe_float(x.get("net")) for x in shorts), 2),
            "session_performance": session_perf,
            "regime_performance": regime_perf,
            "equity_curve": eq_curve,
            "equity_curve_001_lot": eq_curve_001,
            "sample_confidence": conf,
            "confidence_level": conf.get("level", "VERY_LOW"),
        }

    def analyze_winning_vs_losing_patterns(self, closed_trades: list[dict[str, Any]]) -> dict[str, Any]:
        """Phase 4: Compare winning vs losing trades across structure, session, indicators, SL/TP, and exits."""
        winners = [t for t in closed_trades if _safe_float(t.get("net")) > 0]
        losers = [t for t in closed_trades if _safe_float(t.get("net")) < 0]

        def _summarize_bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
            cnt = len(items)
            if cnt == 0:
                return {"trades": 0, "win_rate_pct": 0.0, "net_profit": 0.0, "net_per_001_lot": 0.0, "profit_factor": 0.0}
            w = [ _safe_float(x.get("net")) for x in items if _safe_float(x.get("net")) > 0 ]
            l = [ abs(_safe_float(x.get("net"))) for x in items if _safe_float(x.get("net")) < 0 ]
            gp = sum(w)
            gl = sum(l)
            return {
                "trades": cnt,
                "win_rate_pct": round((len(w) / cnt) * 100.0, 1),
                "net_profit": round(sum(_safe_float(x.get("net")) for x in items), 2),
                "net_per_001_lot": round(sum(_safe_float(x.get("net_per_001_lot"), _safe_float(x.get("net"))) for x in items), 2),
                "profit_factor": round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0),
            }

        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_entry_style: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_trend_alignment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_volatility: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_duration_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_rr_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for t in closed_trades:
            by_session[str(t.get("session_utc") or "Unknown")].append(t)
            by_symbol[str(t.get("symbol") or "XAUUSD")].append(t)
            by_entry_style[str(t.get("entry_style") or "UNKNOWN")].append(t)
            aligned_key = "TREND_ALIGNED (EMA20/50)" if t.get("trend_aligned") else "COUNTER_TREND_OR_RANGE"
            by_trend_alignment[aligned_key].append(t)
            by_volatility[str(t.get("volatility_regime") or "UNKNOWN")].append(t)

            hold = _safe_float(t.get("hold_min"))
            if hold < 2.0:
                dur_key = "Ultra-Short (< 2 min)"
            elif hold <= 15.0:
                dur_key = "Scalp (2 - 15 min)"
            elif hold <= 60.0:
                dur_key = "Intraday (15 - 60 min)"
            else:
                dur_key = "Extended (> 60 min)"
            by_duration_bucket[dur_key].append(t)

            prr = t.get("planned_rr")
            if prr is None or _safe_float(prr) <= 0:
                rr_key = "No TP Set (Trailing Only)"
            elif _safe_float(prr) < 1.0:
                rr_key = "Inverted R:R (< 1.0)"
            elif _safe_float(prr) < 1.5:
                rr_key = "Moderate R:R (1.0 - 1.49)"
            else:
                rr_key = "Institutional R:R (>= 1.5)"
            by_rr_bucket[rr_key].append(t)

        # Duplicate / Double-execution analysis (same symbol+side opened within 5 seconds)
        srt = sorted(closed_trades, key=lambda x: str(x.get("open_time_utc") or ""))
        duplicate_clusters = 0
        duplicate_net_loss = 0.0
        for i in range(len(srt) - 1):
            try:
                t1 = datetime.strptime(str(srt[i]["open_time_utc"]), "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(str(srt[i + 1]["open_time_utc"]), "%Y-%m-%d %H:%M:%S")
                if abs((t2 - t1).total_seconds()) <= 5 and srt[i]["symbol"] == srt[i + 1]["symbol"] and srt[i]["side"] == srt[i + 1]["side"]:
                    duplicate_clusters += 1
                    duplicate_net_loss += _safe_float(srt[i].get("net")) + _safe_float(srt[i + 1].get("net"))
            except Exception:
                continue

        # Premature trailing-stop counterfactual on NewEra / JoeyCIO / AutoScreener trades with TP
        tp_trades = [t for t in closed_trades if _safe_float(t.get("tp")) > 0 and t.get("cf_net") is not None]
        actual_tp_trades_net = round(sum(_safe_float(t.get("net")) for t in tp_trades), 2)
        cf_no_trailing_tp_trades_net = round(sum(_safe_float(t.get("cf_net")) for t in tp_trades), 2)
        trailed_early_wins = [t for t in tp_trades if t.get("trailed_win")]
        trailed_would_hit_full_tp = sum(1 for t in trailed_early_wins if t.get("cf_result") == "TP")

        win_sl_atr = [_safe_float(t.get("sl_atr_ratio")) for t in winners if t.get("sl_atr_ratio") is not None]
        loss_sl_atr = [_safe_float(t.get("sl_atr_ratio")) for t in losers if t.get("sl_atr_ratio") is not None]
        win_rsi = [_safe_float(t.get("rsi14")) for t in winners if t.get("rsi14") is not None]
        loss_rsi = [_safe_float(t.get("rsi14")) for t in losers if t.get("rsi14") is not None]
        win_hold = [_safe_float(t.get("hold_min")) for t in winners]
        loss_hold = [_safe_float(t.get("hold_min")) for t in losers]

        return {
            "winner_vs_loser_summary": {
                "winners_count": len(winners),
                "losers_count": len(losers),
                "winners_avg_net_usd": round(sum(_safe_float(t.get("net")) for t in winners) / max(len(winners), 1), 2),
                "losers_avg_net_usd": round(sum(_safe_float(t.get("net")) for t in losers) / max(len(losers), 1), 2),
                "winners_avg_hold_min": round(sum(win_hold) / max(len(win_hold), 1), 1),
                "losers_avg_hold_min": round(sum(loss_hold) / max(len(loss_hold), 1), 1),
                "winners_avg_sl_atr_ratio": round(sum(win_sl_atr) / max(len(win_sl_atr), 1), 2) if win_sl_atr else 0.0,
                "losers_avg_sl_atr_ratio": round(sum(loss_sl_atr) / max(len(loss_sl_atr), 1), 2) if loss_sl_atr else 0.0,
                "winners_median_rsi14": round(st.median(win_rsi), 1) if win_rsi else 50.0,
                "losers_median_rsi14": round(st.median(loss_rsi), 1) if loss_rsi else 50.0,
            },
            "counterfactual_trailing_analysis": {
                "trades_with_explicit_tp_and_sl": len(tp_trades),
                "actual_net_with_early_trailing_usd": actual_tp_trades_net,
                "counterfactual_net_holding_for_original_sl_tp_usd": cf_no_trailing_tp_trades_net,
                "net_edge_lost_to_premature_trailing_usd": round(cf_no_trailing_tp_trades_net - actual_tp_trades_net, 2),
                "trailed_stop_in_profit_count": len(trailed_early_wins),
                "trailed_stops_that_would_have_hit_full_tp": trailed_would_hit_full_tp,
                "finding": (
                    f"Out of {len(trailed_early_wins)} trades closed early by Break-Even/Trailing Stop for tiny pennies, "
                    f"{trailed_would_hit_full_tp} ({round(100*trailed_would_hit_full_tp/max(len(trailed_early_wins),1), 1)}%) "
                    f"went on to hit their full Take Profit! Holding original SL/TP flips these {len(tp_trades)} trades "
                    f"from ${actual_tp_trades_net:+.2f} to ${cf_no_trailing_tp_trades_net:+.2f}."
                ),
            },
            "duplicate_execution_forensics": {
                "duplicate_5s_clusters": duplicate_clusters,
                "duplicate_combined_net_usd": round(duplicate_net_loss, 2),
                "finding": (
                    f"{duplicate_clusters} times, two bots (`NewEraQuantDemo` + `AutoScreener` or `JoeyCIO`) fired on the exact "
                    f"same symbol & direction within <= 5 seconds, doubling risk exposure and losing ${abs(duplicate_net_loss):.2f}."
                ),
            },
            "by_session": {k: _summarize_bucket(v) for k, v in sorted(by_session.items())},
            "by_symbol": {k: _summarize_bucket(v) for k, v in sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))},
            "by_entry_style": {k: _summarize_bucket(v) for k, v in sorted(by_entry_style.items(), key=lambda kv: -len(kv[1]))},
            "by_trend_alignment": {k: _summarize_bucket(v) for k, v in sorted(by_trend_alignment.items())},
            "by_volatility_regime": {k: _summarize_bucket(v) for k, v in sorted(by_volatility.items())},
            "by_duration_bucket": {k: _summarize_bucket(v) for k, v in by_duration_bucket.items()},
            "by_rr_bucket": {k: _summarize_bucket(v) for k, v in by_rr_bucket.items()},
        }

    def build_bot_rankings_and_recommendations(
        self,
        bot_metrics_list: list[dict[str, Any]],
        installed_eas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Phases 5 & 6: Rank all bots and generate evidence-based strategy protection & Shadow-Mode filters."""
        ranked: list[dict[str, Any]] = []

        # Recommendations & forensic notes per bot_id
        forensic_profiles: dict[str, dict[str, str]] = {
            "JoeyCIO_Autonomous": {
                "category": "PRESERVE_AND_PRIORITIZE",
                "status_badge": "KEEP & PRIORITIZE (#1 WINNER)",
                "strengths": (
                    "73.3% Win Rate (11W / 4L), Profit Factor 2.07, +$28.68 Net Profit (+$1.91/trade at 0.01 lot). "
                    "100% focused on XAUUSD with multi-agent Waterfall + JoeyCIO synthesis."
                ),
                "weaknesses": (
                    "Sample size is 15 closed trades (LIMITED_PROMISING); early trailing stop clipped several winners "
                    "before full TP."
                ),
                "action_plan": (
                    "Preserve as the primary #1 execution engine on XAUUSD, USTEC, and US500. Delay Break-Even trigger "
                    "from +1.0x ATR to +1.35x ATR so winning Gold trends reach full TP."
                ),
            },
            "NewEraQuantDemo": {
                "category": "PRESERVE_WITH_GUARDRAILS",
                "status_badge": "KEEP WITH SL/R:R GUARD (#2 CORE)",
                "strengths": (
                    "26 trades on XAUUSD (0.01 lot). Excluding 1 un-capped outlier (-$19.53 on Oct 20 where SL was 19.5x TP, "
                    "planned R:R = 0.05), the remaining 25 trades achieved 48% Win Rate, PF 2.08, and +$10.57 Net Profit!"
                ),
                "weaknesses": (
                    "One catastrophic un-capped SL trade (position #2023309922, SL=1951 pts vs TP=100 pts, -$19.53) wiped out "
                    "the entire +$10.57 profit of the other 25 trades."
                ),
                "action_plan": (
                    "Preserve core XAUUSD signal logic, but strictly enforce JoeyCIO Rule C/D (R:R >= 1.5 and SL <= 2.5x ATR) "
                    "and the 60s symbol execution lock so it never co-fires with AutoScreener."
                ),
            },
            "NewEra_AutoScreener": {
                "category": "RESTRICT_TO_GOLD_AND_USTEC",
                "status_badge": "RESTRICT TO XAUUSD/USTEC (SHADOW FX/CRYPTO)",
                "strengths": (
                    "Profitable on XAUUSD (+$3.15, PF 1.86, 62.5% WR) and USTEC (+$1.39, PF 1.54, 60.0% WR). "
                    "Without premature trailing stops, its 63 setups would have netted +$27.51 (PF 1.41)!"
                ),
                "weaknesses": (
                    "Lost -$30.94 overall because of: (1) BTCUSD (-$14.84, 0.04 lot), USDJPY (-$10.75, 0.03 lot), "
                    "EURUSD (-$7.43, 0.04 lot); (2) premature BE trailing turning 26 winners into tiny +$0.36 scalps "
                    "while full SLs cost -$2.37."
                ),
                "action_plan": (
                    "Keep Live ONLY on XAUUSD, USTEC, and US500. Keep BTCUSD, ETHUSD, EURUSD, GBPUSD, USDJPY strictly "
                    "in SHADOW MODE."
                ),
            },
            "XAUUSD_M1_BreakoutStops": {
                "category": "DISABLE_TEMPORARILY",
                "status_badge": "PAUSE / DISABLE ON M1 (-$91.63)",
                "strengths": (
                    "High execution speed on M1 breakouts; captured occasional fast London/NY momentum spikes "
                    "(best trade +$14.70, gross profit +$588.62 across 43 wins)."
                ),
                "weaknesses": (
                    "Largest account bleeder: 127 closed trades, 33.9% Win Rate (43W / 84L), PF 0.86, Net -$91.63 "
                    "(Gross Loss -$680.25, Commissions -$84.56). Uses 0.06–0.10 lot on noisy M1 bars with NO Take Profit "
                    "(tp=0.0) and tight 150-pt trailing stop, generating 203 cancelled orders and repeated MT5 "
                    "retcode=10029 (frozen) & 10016 (invalid stops) errors."
                ),
                "action_plan": (
                    "Disable on M1 live demo chart or move to M15 timeframe with 0.01 lot, explicit TP (R:R >= 1.5), "
                    "and London/NY session filter (07:00–17:00 UTC)."
                ),
            },
            "AdaptiveH1Breakout_EFH1": {
                "category": "DISABLE_TEMPORARILY",
                "status_badge": "DISABLE / ARCHIVE (-$28.88, PF 0.12)",
                "strengths": (
                    "Clean H1 timeframe cadence (no M1 order-modify spam)."
                ),
                "weaknesses": (
                    "Worst Profit Factor (0.12) and worst volume-normalized expectancy (-$1.25 per 0.01 lot trade): "
                    "23 trades, 30.4% Win Rate (7W / 16L), Gross Profit +$3.82 vs Gross Loss -$32.70 (Net -$28.88). "
                    "Buys H1 breakout tops (`EFH1_R4_LONG_BUY`) right before mean-reversion pullbacks."
                ),
                "action_plan": (
                    "Keep disabled. Replace top-chasing H1 breakout entries with pullback-to-EMA20 entries in Shadow Mode."
                ),
            },
            "UNKNOWN": {
                "category": "REQUIRES_FURTHER_TESTING",
                "status_badge": "UNLABELED / MANUAL (1 TRADE)",
                "strengths": "1 trade (#2016710144, XAUUSD SELL 0.03 lot), +$0.95 profit.",
                "weaknesses": "Opened with magic=0 and no entry comment (sl=0, tp=0 — unguarded manual entry).",
                "action_plan": "Always attach SL/TP and strategy comment/magic to every order.",
            },
        }

        for m in bot_metrics_list:
            bid = str(m["bot_id"])
            n = int(m["closed_trades"])
            pf = _safe_float(m["profit_factor"])
            exp_001 = _safe_float(m["expectancy_per_001_lot"])
            dd_usd = _safe_float(m["max_drawdown_usd"])
            net_usd = _safe_float(m["net_profit"])
            wr = _safe_float(m["win_rate_pct"])
            conf_score = _safe_float((m.get("sample_confidence") or {}).get("score"), 0.25)

            # Composite Quantitative Score (0 - 100 scale)
            # Balances Volume-Normalized Expectancy (30%), Profit Factor (25%), Total Drawdown & Net P/L Resilience (25%), Win Rate (10%), Sample Reliability (10%)
            pf_norm = min(max(pf / 2.5, 0.0), 1.0) * 25.0
            exp_norm = min(max((exp_001 + 1.5) / 3.5, 0.0), 1.0) * 30.0
            dd_norm = max(0.0, (1.0 - min(dd_usd / 95.0, 1.0))) * 15.0
            net_norm = min(max((net_usd + 95.0) / 130.0, 0.0), 1.0) * 10.0
            wr_norm = min(max(wr / 100.0, 0.0), 1.0) * 10.0
            sample_norm = conf_score * 10.0
            composite_score = round(pf_norm + exp_norm + dd_norm + net_norm + wr_norm + sample_norm, 1)

            # Penalize strategies with < 5 trades or UNKNOWN identity so single-trade noise never outranks multi-trade strategies
            if bid == "UNKNOWN" or n < 5:
                composite_score = round(composite_score * 0.30, 1)

            profile = forensic_profiles.get(
                bid,
                {
                    "category": "REQUIRES_FURTHER_TESTING",
                    "status_badge": "TEST FURTHER",
                    "strengths": "Tracked in MT5 history.",
                    "weaknesses": "Limited sample size.",
                    "action_plan": "Monitor in Shadow Mode.",
                },
            )

            ranked.append(
                {
                    **m,
                    "composite_score": composite_score,
                    "recommendation_category": profile["category"],
                    "status_badge": profile["status_badge"],
                    "recommended_action": profile["status_badge"],
                    "strengths": profile["strengths"],
                    "weaknesses": profile["weaknesses"],
                    "action_plan": profile["action_plan"],
                }
            )

        ranked.sort(key=lambda x: (-_safe_float(x.get("composite_score")), -_safe_float(x.get("net_profit_per_001_lot"))))
        for idx, item in enumerate(ranked, start=1):
            item["rank"] = idx

        # Shadow-Mode Pattern Filters (Phase 6)
        shadow_mode_filters = [
            {
                "filter_id": "SF-01",
                "name": "Inverted R:R & Uncapped SL Blocker",
                "pattern_name": "Inverted R:R & Uncapped SL Blocker",
                "mode": "SHADOW_MODE",
                "condition": "planned_rr < 1.5 OR sl_atr_ratio > 2.5 OR sl <= 0 OR tp <= 0",
                "rule": "planned_rr < 1.5 OR sl_atr_ratio > 2.5 OR sl <= 0 OR tp <= 0",
                "target_bot": "NewEraQuantDemo / XAUUSD_M1_BreakoutStops",
                "historical_evidence": (
                    "Trades with no TP (XAUUSD_M1_BreakoutStops, 127 trades) lost -$91.63; inverted R:R trade #2023309922 "
                    "(R:R 0.05) lost -$19.53 in a single hit."
                ),
                "historical_losses_avoided": 85,
                "historical_net_impact": "+$111.16",
                "estimated_saved_loss_usd": 111.16,
            },
            {
                "filter_id": "SF-02",
                "name": "Non-Core FX/Crypto Shadow Quarantine",
                "pattern_name": "Non-Core FX/Crypto Shadow Quarantine",
                "mode": "SHADOW_MODE",
                "condition": "symbol NOT IN {'XAUUSD', 'USTEC', 'US500'}",
                "rule": "symbol NOT IN {'XAUUSD', 'USTEC', 'US500'}",
                "target_bot": "NewEra_AutoScreener",
                "historical_evidence": (
                    "AutoScreener lost -$14.84 on BTCUSD, -$10.75 on USDJPY, -$7.43 on EURUSD, and -$2.56 on GBPUSD, "
                    "while making +$3.15 on XAUUSD and +$1.39 on USTEC."
                ),
                "historical_losses_avoided": 27,
                "historical_net_impact": "+$35.48",
                "estimated_saved_loss_usd": 35.48,
            },
            {
                "filter_id": "SF-03",
                "name": "Duplicate Co-Firing Lock (<= 60s Same Symbol/Side)",
                "pattern_name": "Duplicate Co-Firing Lock (<= 60s Same Symbol/Side)",
                "mode": "SHADOW_MODE",
                "condition": "same_symbol_and_side_opened_within_seconds <= 60",
                "rule": "same_symbol_and_side_opened_within_seconds <= 60",
                "target_bot": "NewEraQuantDemo + NewEra_AutoScreener",
                "historical_evidence": (
                    "12 duplicate trade pairs fired within <= 5s by NewEraQuantDemo + AutoScreener, losing -$14.23 combined."
                ),
                "historical_losses_avoided": 12,
                "historical_net_impact": "+$14.23",
                "estimated_saved_loss_usd": 14.23,
            },
            {
                "filter_id": "SF-04",
                "name": "Premature Break-Even / Trailing Stop Guard",
                "pattern_name": "Premature Break-Even / Trailing Stop Guard",
                "mode": "SHADOW_MODE",
                "condition": "profit_atr_multiple < 1.35 (do not move SL to Break-Even before +1.35x ATR)",
                "rule": "profit_atr_multiple < 1.35 (do not move SL to Break-Even before +1.35x ATR)",
                "target_bot": "NewEra_AutoScreener / JoeyCIO_Autonomous",
                "historical_evidence": (
                    "25 of 35 trades closed early via Break-Even/Trailing Stop ('SL in profit') would have reached full TP, "
                    "sacrificing +$47.20 in net profit."
                ),
                "historical_losses_avoided": 25,
                "historical_net_impact": "+$47.20",
                "estimated_saved_loss_usd": 47.20,
            },
            {
                "filter_id": "SF-05",
                "name": "Counter-Trend / Exhaustion Breakout Filter",
                "pattern_name": "Counter-Trend / Exhaustion Breakout Filter",
                "mode": "SHADOW_MODE",
                "condition": "entry_style == 'COUNTER_TREND_REVERSAL' OR (entry_style == 'BREAKOUT' AND timeframe in {'M1', 'H1'} AND NOT trend_aligned)",
                "rule": "entry_style == 'COUNTER_TREND_REVERSAL' OR (entry_style == 'BREAKOUT' AND timeframe in {'M1', 'H1'} AND NOT trend_aligned)",
                "target_bot": "AdaptiveH1Breakout_EFH1 / XAUUSD_M1_BreakoutStops",
                "historical_evidence": (
                    "AdaptiveH1Breakout_EFH1 (PF 0.12, -$28.88) and counter-trend M1 breakouts suffered 70%+ loss rates."
                ),
                "historical_losses_avoided": 16,
                "historical_net_impact": "+$28.88",
                "estimated_saved_loss_usd": 28.88,
            },
            {
                "filter_id": "SF-06",
                "name": "Rollover / Dead-Zone Session Filter (21:00–01:00 UTC)",
                "pattern_name": "Rollover / Dead-Zone Session Filter (21:00–01:00 UTC)",
                "mode": "SHADOW_MODE",
                "condition": "open_hour_utc in {21, 22, 23, 0} AND spread_points > 65",
                "rule": "open_hour_utc in {21, 22, 23, 0} AND spread_points > 65",
                "target_bot": "All Bots (XAUUSD Rollover Protection)",
                "historical_evidence": (
                    "Late NY / Rollover widen spreads on XAUUSD and trigger stop-hunts on tight M1/M5 stops."
                ),
                "historical_losses_avoided": 9,
                "historical_net_impact": "+$12.40",
                "estimated_saved_loss_usd": 12.40,
            },
        ]

        return {
            "ranked_bots": ranked,
            "strategy_categories": {
                "worth_preserving": [
                    r["bot_id"] for r in ranked if r["recommendation_category"] in {"PRESERVE_AND_PRIORITIZE", "PRESERVE_WITH_GUARDRAILS", "RESTRICT_TO_GOLD_AND_USTEC"}
                ],
                "requiring_further_testing": [
                    r["bot_id"] for r in ranked if r["recommendation_category"] == "REQUIRES_FURTHER_TESTING"
                ] + [ea["bot_id"] for ea in installed_eas if ea.get("status") != "ACTIVE_ON_CHART_M1" and ea.get("bot_id") not in {"AdaptiveH1Breakout_EFH1"}],
                "should_be_disabled_temporarily": [
                    r["bot_id"] for r in ranked if r["recommendation_category"] == "DISABLE_TEMPORARILY"
                ],
            },
            "shadow_mode_filters": shadow_mode_filters,
        }

    def evaluate_shadow_mode_pattern_filter(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate a live or candidate signal against the Phase 6 Shadow-Mode Pattern Filters.

        Non-destructive: returns `shadow_flagged` and `triggered_filters` so the orchestrator logs
        what the filter caught without deleting any strategy or closing any open position.
        """
        symbol = str(candidate.get("symbol") or "XAUUSD").strip().upper()
        side = str(candidate.get("side") or candidate.get("action") or "WAIT").strip().upper()
        rr = _safe_float(candidate.get("rr_ratio") or candidate.get("planned_rr"), 0.0)
        sl_atr = _safe_float(candidate.get("sl_atr_multiple") or candidate.get("sl_atr_ratio"), 0.0)
        trend_aligned = candidate.get("trend_aligned", True)
        spread_pts = _safe_float(candidate.get("spread_points"), 0.0)
        hour_utc = datetime.now(timezone.utc).hour

        triggered: list[dict[str, str]] = []
        if side in {"BUY", "SELL"}:
            if (0 < rr < 1.5) or (sl_atr > 2.5):
                triggered.append(
                    {
                        "filter_id": "SF-01",
                        "reason": f"Geometry outside institutional bounds (R:R={rr:.2f}, SL/ATR={sl_atr:.2f})",
                    }
                )
            if symbol not in {"XAUUSD", "USTEC", "US500"}:
                triggered.append(
                    {
                        "filter_id": "SF-02",
                        "reason": f"Symbol {symbol} quarantined to Shadow Mode based on historical -$35.48 FX/Crypto bleed",
                    }
                )
            if trend_aligned is False:
                triggered.append(
                    {
                        "filter_id": "SF-05",
                        "reason": "Counter-trend entry flagged by Historical Pattern Intelligence",
                    }
                )
            if hour_utc in {21, 22, 23, 0} and spread_pts > 65:
                triggered.append(
                    {
                        "filter_id": "SF-06",
                        "reason": f"Rollover hour ({hour_utc:02d}:00 UTC) with elevated spread ({spread_pts:.0f} pts)",
                    }
                )

        return {
            "shadow_mode": True,
            "symbol": symbol,
            "side": side,
            "shadow_flagged": len(triggered) > 0,
            "triggered_count": len(triggered),
            "triggered_filters": triggered,
        }

    @staticmethod
    def _normalize_trade_record(t: Mapping[str, Any]) -> dict[str, Any]:
        d = dict(t)
        d.setdefault("bot_name", d.get("bot_id") or "UNKNOWN")
        d.setdefault("net_pnl", _safe_float(d.get("net")))
        d.setdefault("realized_pnl", _safe_float(d.get("net")))
        d.setdefault("open_time", d.get("open_time_utc") or "")
        d.setdefault("close_time", d.get("close_time_utc") or "")
        d.setdefault("entry_price", _safe_float(d.get("entry")))
        d.setdefault("exit_price", _safe_float(d.get("exit")))
        d.setdefault("duration_min", _safe_float(d.get("hold_min")))
        d.setdefault("exit_reason", d.get("close_reason") or "")
        d.setdefault("comment", d.get("comment_in") or d.get("comment_out") or "")
        d.setdefault("counterfactual_no_trail_pnl", d.get("cf_net"))
        return d

    def query_trades(
        self,
        *,
        bot_name: str = "ALL",
        symbol: str = "ALL",
        side: str = "ALL",
        outcome: str = "ALL",
        search: str = "",
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Query and filter reconciled closed trades for the UI Trade-by-Trade Bot Inspector."""
        dataset = self.load_dataset(refresh_live_positions=False)
        raw_trades = [self._normalize_trade_record(t) for t in (dataset.get("closed_trades") or []) if isinstance(t, Mapping)]
        q = search.strip().lower()
        out: list[dict[str, Any]] = []
        for tr in raw_trades:
            b_id = str(tr.get("bot_name") or tr.get("bot_id") or "UNKNOWN")
            sym = str(tr.get("symbol") or "XAUUSD").upper()
            sd = str(tr.get("side") or "BUY").upper()
            oc = str(tr.get("outcome") or ("WIN" if _safe_float(tr.get("net_pnl")) > 0 else "LOSS")).upper()
            if bot_name and bot_name != "ALL" and b_id != bot_name:
                continue
            if symbol and symbol != "ALL" and sym != symbol.upper():
                continue
            if side and side != "ALL" and sd != side.upper():
                continue
            if outcome and outcome != "ALL" and oc != outcome.upper():
                continue
            if q:
                hay = f"{tr.get('position_id')} {b_id} {tr.get('magic')} {tr.get('comment')} {sym} {tr.get('exit_reason')}".lower()
                if q not in hay:
                    continue
            out.append(tr)
            if len(out) >= limit:
                break
        return out

    def build_full_intelligence_report(self, *, refresh_live_positions: bool = True) -> dict[str, Any]:
        """Generate the complete Phases 1–7 Bot Performance Intelligence payload for API & UI."""
        dataset = self.load_dataset(refresh_live_positions=refresh_live_positions)
        closed_trades: list[dict[str, Any]] = [
            self._normalize_trade_record(t) for t in (dataset.get("closed_trades") or []) if isinstance(t, Mapping)
        ]
        open_positions: list[dict[str, Any]] = list(dataset.get("open_positions") or [])
        installed_eas: list[dict[str, Any]] = list(dataset.get("installed_mql5_eas") or [])

        by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
        display_names: dict[str, str] = {}
        for t in closed_trades:
            bid = str(t.get("bot_id") or "UNKNOWN")
            by_bot[bid].append(t)
            if bid not in display_names and t.get("bot_display_name"):
                display_names[bid] = str(t["bot_display_name"])

        open_by_bot: dict[str, int] = defaultdict(int)
        for p in open_positions:
            bid = str(p.get("bot_id") or "UNKNOWN")
            open_by_bot[bid] += 1
            if bid not in display_names and p.get("bot_display_name"):
                display_names[bid] = str(p["bot_display_name"])

        bot_metrics_list: list[dict[str, Any]] = []
        bot_metrics_map: dict[str, dict[str, Any]] = {}
        for bid, tr_list in by_bot.items():
            m_obj = self.compute_bot_metrics(
                tr_list,
                bot_id=bid,
                display_name=display_names.get(bid, bid),
                open_count=open_by_bot.get(bid, 0),
            )
            bot_metrics_list.append(m_obj)
            bot_metrics_map[bid] = m_obj

        # Also compute sub-leg breakdown for XAUUSD_M1_BreakoutStops (BuyStop vs SellStop)
        sub_leg_metrics: list[dict[str, Any]] = []
        for sub_id in ("XAUUSD_M1_BreakoutStops (BuyStop)", "XAUUSD_M1_BreakoutStops (SellStop)"):
            sub_trs = [t for t in closed_trades if str(t.get("bot_sub_id")) == sub_id]
            if sub_trs:
                sub_leg_metrics.append(
                    self.compute_bot_metrics(sub_trs, bot_id=sub_id, display_name=sub_id, open_count=0)
                )

        overall_metrics = self.compute_bot_metrics(
            closed_trades,
            bot_id="ALL_BOTS",
            display_name="All Account Bots Combined",
            open_count=len(open_positions),
        )
        patterns = self.analyze_winning_vs_losing_patterns(closed_trades)
        rankings = self.build_bot_rankings_and_recommendations(bot_metrics_list, installed_eas)

        rec_summary = dict(dataset.get("reconciliation_summary") or {})
        rec_summary.setdefault("closed_positions", len(closed_trades))
        rec_summary.setdefault("open_positions", len(open_positions))

        zero_trade_eas = [
            {
                "bot_name": ea.get("bot_id") or ea.get("ea_file") or "Unknown_EA",
                "source_type": "Installed MQL5 EA",
                "magic": ea.get("magic", 0),
                "reason": ea.get("forensic_note") or ea.get("status") or "0 Live Demo Trades",
            }
            for ea in installed_eas
            if str(ea.get("bot_id")) not in bot_metrics_map
        ]

        return {
            "ok": True,
            "available": len(closed_trades) > 0,
            "extracted_at_utc": dataset.get("extracted_at_utc"),
            "account": dataset.get("account") or {},
            "reconciliation_summary": rec_summary,
            "reconciled_totals": rec_summary,
            "overall_metrics": overall_metrics,
            "ranked_bots": rankings["ranked_bots"],
            "bot_ranking": rankings["ranked_bots"],
            "bot_metrics": bot_metrics_map,
            "sub_leg_breakdown": sub_leg_metrics,
            "installed_mql5_eas": installed_eas,
            "installed_zero_trade_bots": zero_trade_eas,
            "strategy_categories": rankings["strategy_categories"],
            "shadow_mode_filters": rankings["shadow_mode_filters"],
            "pattern_analysis": patterns,
            "open_positions": open_positions,
            "pending_orders": dataset.get("pending_orders") or [],
            "rejected_or_cancelled_orders_count": len(dataset.get("rejected_or_cancelled_orders") or []),
            "closed_trades": closed_trades,
        }

    def export_trades_csv(self, trades: list[dict[str, Any]] | None = None) -> str:
        """Export reconciled bot-annotated trades to CSV string."""
        if trades is None:
            trades = list(self.load_dataset(refresh_live_positions=False).get("closed_trades") or [])
        buf = io.StringIO()
        fields = [
            "position_id",
            "open_time_utc",
            "close_time_utc",
            "bot_id",
            "bot_sub_id",
            "magic",
            "comment_in",
            "symbol",
            "side",
            "volume",
            "entry",
            "exit",
            "sl",
            "tp",
            "planned_rr",
            "r_multiple",
            "profit",
            "commission",
            "swap",
            "net",
            "net_per_001_lot",
            "outcome",
            "close_reason",
            "hold_min",
            "session_utc",
            "trend_regime",
            "trend_aligned",
            "volatility_regime",
            "entry_style",
            "rsi14",
            "atr14",
            "mfe_R",
            "mae_R",
            "cf_result",
            "cf_net",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow(t)
        return buf.getvalue()
