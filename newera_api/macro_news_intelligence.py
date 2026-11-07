"""New Era 2.0 — Global Macro, NFP/CPI/FOMC Probability Forecaster (%), Polymarket & Live News/X-Pulse Hub.

Unifies into one engine:
1. Official Federal Reserve (FRED) Macroeconomic Series (`PAYEMS` NFP, `UNRATE` Unemployment,
   `ICSA` Jobless Claims, `CPIAUCSL` CPI, `CPILFESL` Core CPI, `T10YIE` Breakeven Inflation,
   `FEDFUNDS` Fed Rate, `DGS2` 2Y Yield, `DGS10` 10Y Yield, `T10Y2Y` Yield Curve, `VIXCLS` VIX).
2. Quantitative Macro Event Probability Forecaster (%) — calculates exact pre-event probabilities
   (Beat / Hawkish % vs Inline % vs Miss / Dovish %) for NFP, CPI, FOMC, Jobless Claims, and
   GDP/Risk Regime based on leading indicators, Treasury yield spreads, Polymarket odds, and
   live MT5 Cross-Asset USD Basket flows.
3. Polymarket Real-Money Prediction Market Odds (`gamma-api.polymarket.com`).
4. Real-Time Global Financial News, Central Bank & Trump/Geopolitical Pulse Aggregator (RSS + NLP
   impact scoring for XAUUSD, FX, USTEC/US500, BTCUSD/ETHUSD).
5. Full 12-Instrument Multi-Asset Live Terminal Matrix (Gold, 7 FX Majors, 2 US Indices, 2 Crypto).
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT, load_workspace_dotenv


ALL_12_TERMINAL_SYMBOLS: tuple[dict[str, str], ...] = (
    {"symbol": "XAUUSD", "category": "METAL / SAFE HAVEN", "name_ge": "ოქრო (Gold Spot)"},
    {"symbol": "EURUSD", "category": "FX MAJOR", "name_ge": "ევრო / დოლარი"},
    {"symbol": "GBPUSD", "category": "FX MAJOR", "name_ge": "ფუნტი / დოლარი"},
    {"symbol": "USDJPY", "category": "FX MAJOR", "name_ge": "დოლარი / იენა"},
    {"symbol": "AUDUSD", "category": "FX COMMODITY", "name_ge": "ავსტრალიური / დოლარი"},
    {"symbol": "USDCAD", "category": "FX COMMODITY", "name_ge": "დოლარი / კანადური"},
    {"symbol": "USDCHF", "category": "FX SAFE HAVEN", "name_ge": "დოლარი / ფრანკი"},
    {"symbol": "NZDUSD", "category": "FX COMMODITY", "name_ge": "ზელანდიური / დოლარი"},
    {"symbol": "USTEC", "category": "US INDEX", "name_ge": "Nasdaq 100 (Tech Index)"},
    {"symbol": "US500", "category": "US INDEX", "name_ge": "S&P 500 Index"},
    {"symbol": "BTCUSD", "category": "CRYPTO", "name_ge": "Bitcoin / USD"},
    {"symbol": "ETHUSD", "category": "CRYPTO", "name_ge": "Ethereum / USD"},
)

FRED_KEY_SERIES: dict[str, dict[str, str]] = {
    "PAYEMS": {"title": "US Non-Farm Payrolls (NFP)", "unit": "K Jobs"},
    "UNRATE": {"title": "US Unemployment Rate", "unit": "%"},
    "ICSA": {"title": "Weekly Initial Jobless Claims", "unit": "Claims"},
    "CPIAUCSL": {"title": "US Headline CPI Index", "unit": "Index"},
    "CPILFESL": {"title": "US Core CPI Index", "unit": "Index"},
    "T10YIE": {"title": "10Y Breakeven Inflation Rate", "unit": "%"},
    "FEDFUNDS": {"title": "Effective Fed Funds Rate", "unit": "%"},
    "DGS2": {"title": "US 2-Year Treasury Yield", "unit": "%"},
    "DGS10": {"title": "US 10-Year Treasury Yield", "unit": "%"},
    "T10Y2Y": {"title": "10Y-2Y Yield Curve Spread", "unit": "%"},
    "VIXCLS": {"title": "CBOE VIX Volatility Index", "unit": "pts"},
    "DTWEXBGS": {"title": "Broad US Dollar Index (DXY Proxy)", "unit": "Index"},
    "RSAFS": {"title": "US Advance Retail Sales", "unit": "$M"},
}

LIVE_NEWS_FEEDS: tuple[tuple[str, str], ...] = (
    ("FX & Macro Live", "https://www.investing.com/rss/news_1.rss"),
    ("Commodities & Gold", "https://www.investing.com/rss/news_11.rss"),
    ("Economic Indicators", "https://www.investing.com/rss/news_95.rss"),
    ("Crypto & Bitcoin", "https://www.investing.com/rss/news_301.rss"),
    ("US Markets & Fed", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
)


def _http_get_json(url: str, timeout: float = 6.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewEra2MacroTerminal/2.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _http_get_text(url: str, timeout: float = 5.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewEra2MacroTerminal/2.0",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


class MacroNewsIntelligenceHub:
    """Centralized Global Macro, NFP/CPI/FOMC Probability Forecaster & Multi-Asset News Terminal."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.cache_dir = self.root / "TradingAgentsData" / "research"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "macro_news_intelligence_cache.json"
        load_workspace_dotenv(self.root)
        self._memory_cache: dict[str, Any] = self._load_disk_cache()

    def _load_disk_cache(self) -> dict[str, Any]:
        if self.cache_path.is_file():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_disk_cache(self, payload: dict[str, Any]) -> None:
        try:
            self.cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 1. FRED Macroeconomic Data Fetcher
    # ------------------------------------------------------------------
    def fetch_fred_macro_snapshot(self, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        now_ts = time.time()
        cached_fred = self._memory_cache.get("fred_snapshot", {})
        cached_ts = float(self._memory_cache.get("fred_ts", 0.0) or 0.0)
        # FRED macro series update daily/weekly/monthly, so 15-minute cache is optimal
        if cached_fred and not force_refresh and (now_ts - cached_ts) < 900:
            return cached_fred

        fred_key = (os.environ.get("FRED_API_KEY") or "").strip()
        results: dict[str, dict[str, Any]] = {}

        if fred_key:
            for series_id, meta in FRED_KEY_SERIES.items():
                try:
                    params = urllib.parse.urlencode(
                        {
                            "series_id": series_id,
                            "api_key": fred_key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": 14,
                        }
                    )
                    url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
                    data = _http_get_json(url, timeout=4.5)
                    obs_raw = data.get("observations", [])
                    valid_vals: list[tuple[str, float]] = []
                    for item in obs_raw:
                        if not isinstance(item, dict):
                            continue
                        val_str = str(item.get("value", ".")).strip()
                        if val_str in {".", "", "nan"}:
                            continue
                        try:
                            valid_vals.append((str(item.get("date", "")), float(val_str)))
                        except ValueError:
                            continue
                    if valid_vals:
                        latest_date, latest_val = valid_vals[0]
                        prev_val = valid_vals[1][1] if len(valid_vals) > 1 else latest_val
                        prev2_val = valid_vals[2][1] if len(valid_vals) > 2 else prev_val
                        yoy_val = valid_vals[min(12, len(valid_vals) - 1)][1]
                        delta = latest_val - prev_val
                        delta_prev = prev_val - prev2_val
                        pct_change = ((latest_val - prev_val) / prev_val * 100.0) if prev_val else 0.0
                        yoy_pct = ((latest_val - yoy_val) / yoy_val * 100.0) if yoy_val else 0.0
                        results[series_id] = {
                            "series_id": series_id,
                            "title": meta["title"],
                            "unit": meta["unit"],
                            "latest_date": latest_date,
                            "latest": round(latest_val, 3),
                            "previous": round(prev_val, 3),
                            "prev2": round(prev2_val, 3),
                            "delta": round(delta, 3),
                            "delta_prev": round(delta_prev, 3),
                            "pct_change": round(pct_change, 3),
                            "yoy_pct": round(yoy_pct, 2),
                            "history": [round(v[1], 3) for v in valid_vals[:6]],
                        }
                except Exception:
                    continue

        if results:
            self._memory_cache["fred_snapshot"] = results
            self._memory_cache["fred_ts"] = now_ts
            return results
        return cached_fred if isinstance(cached_fred, dict) else {}

    # ------------------------------------------------------------------
    # 2. Polymarket Live Prediction Market Odds
    # ------------------------------------------------------------------
    def fetch_polymarket_odds(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        now_ts = time.time()
        cached_pm = self._memory_cache.get("polymarket_odds", [])
        cached_ts = float(self._memory_cache.get("polymarket_ts", 0.0) or 0.0)
        if cached_pm and not force_refresh and (now_ts - cached_ts) < 300:
            return cached_pm

        topics = ("Fed rate", "Inflation", "Recession", "Bitcoin", "Gold", "Tariffs")
        seen_questions: set[str] = set()
        markets_out: list[dict[str, Any]] = []

        for topic in topics:
            try:
                params = urllib.parse.urlencode({"q": topic, "limit_per_type": 6})
                url = f"https://gamma-api.polymarket.com/public-search?{params}"
                data = _http_get_json(url, timeout=4.5)
                for ev in data.get("events", []) or []:
                    if not isinstance(ev, dict):
                        continue
                    for m in ev.get("markets", []) or []:
                        if not isinstance(m, dict) or m.get("closed"):
                            continue
                        question = str(m.get("question") or ev.get("title") or "").strip()
                        if not question or question in seen_questions:
                            continue
                        outcomes_raw = m.get("outcomes")
                        prices_raw = m.get("outcomePrices")
                        try:
                            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
                            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
                        except Exception:
                            continue
                        if not outcomes or not prices or len(outcomes) != len(prices):
                            continue
                        yes_prob = round(float(prices[0]) * 100.0, 1)
                        no_prob = round(float(prices[1]) * 100.0, 1) if len(prices) > 1 else round(100.0 - yes_prob, 1)
                        vol = float(m.get("volumeNum") or m.get("volume") or 0.0)
                        seen_questions.add(question)
                        markets_out.append(
                            {
                                "topic": topic,
                                "question": question,
                                "yes_label": str(outcomes[0]),
                                "yes_pct": yes_prob,
                                "no_label": str(outcomes[1]) if len(outcomes) > 1 else "No",
                                "no_pct": no_prob,
                                "volume_usd": round(vol, 0),
                            }
                        )
            except Exception:
                continue

        markets_out.sort(key=lambda x: float(x.get("volume_usd", 0.0)), reverse=True)
        top_markets = markets_out[:14]
        if top_markets:
            self._memory_cache["polymarket_odds"] = top_markets
            self._memory_cache["polymarket_ts"] = now_ts
            return top_markets
        return cached_pm if isinstance(cached_pm, list) else []

    # ------------------------------------------------------------------
    # 3. Live News, X/Macro Pulse & Sentiment Scorer
    # ------------------------------------------------------------------
    def _score_headline_impact(self, title: str, category: str) -> dict[str, Any]:
        low = title.lower()
        impact = "MEDIUM"
        high_keywords = (
            "nfp", "payroll", "cpi", "inflation", "fed ", "fomc", "powell", "rate cut", "rate hike",
            "tariff", "trump", "war", "missile", "sanction", "central bank", "ecb", "boj", "boe",
            "gdp", "recession", "unemployment", "jobless", "gold", "xau", "bitcoin", "etf", "crash", "surge",
        )
        if any(k in low for k in high_keywords):
            impact = "HIGH 🔥"

        # Score directional bias for USD, Gold (XAUUSD), Indices (USTEC), and Crypto (BTCUSD)
        usd_score = 0
        gold_score = 0
        risk_score = 0

        if any(
            w in low
            for w in (
                "hawkish", "hot cpi", "inflation rises", "strong jobs", "blowout payroll",
                "yields rise", "lift yields", "rate hike", "beat", "dollar holds firm",
                "dollar rises", "dollar gains", "stronger dollar",
            )
        ):
            usd_score += 2
            gold_score -= 1
            risk_score -= 1
        if any(
            w in low
            for w in (
                "dovish", "rate cut", "cool cpi", "inflation slows", "weak jobs",
                "jobless claims rise", "miss", "dollar slips", "dollar falls", "weaker dollar",
            )
        ):
            usd_score -= 2
            gold_score += 2
            risk_score += 1
        if any(
            w in low
            for w in (
                "war", "geopolit", "middle east", "iran", "sanction", "tariff",
                "safe haven", "safe-haven", "uncertainty", "gold rises", "central bank buying",
                "bullion",
            )
        ):
            gold_score += 2
            risk_score -= 1
        if any(
            w in low
            for w in (
                "rally", "record high", "bull", "surges", "ai boom", "nasdaq",
                "bitcoin rises", "inflow", "stocks gain", "wall street rises",
            )
        ):
            risk_score += 2
        if any(
            w in low
            for w in (
                "drop", "falls", "plunge", "selloff", "rout", "fear",
                "crash", "recession", "slips", "slump",
            )
        ):
            risk_score -= 2
            gold_score += 1

        affected: list[str] = []
        if "gold" in low or "xau" in low or gold_score != 0:
            affected.append("XAUUSD")
        if any(w in low for w in ("dollar", "usd", "fed", "fomc", "cpi", "nfp", "payroll", "yield")):
            affected.extend(["EURUSD", "USDJPY", "GBPUSD"])
        if any(w in low for w in ("btc", "bitcoin", "crypto", "eth")):
            affected.extend(["BTCUSD", "ETHUSD"])
        if any(w in low for w in ("nasdaq", "s&p", "wall street", "stocks", "tech", "ai ")):
            affected.extend(["USTEC", "US500"])
        if not affected:
            if "Crypto" in category:
                affected = ["BTCUSD", "ETHUSD"]
            elif "Commodities" in category:
                affected = ["XAUUSD", "USDCAD"]
            else:
                affected = ["XAUUSD", "EURUSD", "USTEC"]

        # Deduplicate preserving order
        affected_unique = list(dict.fromkeys(affected))[:4]

        if gold_score >= 2:
            sentiment_tag = "🟢 BULLISH GOLD (XAUUSD ↑)"
            bias_ge = "ოქროს ზრდის ფაქტორი (Safe-Haven / Dovish)"
        elif usd_score >= 2:
            sentiment_tag = "🔵 BULLISH USD (DXY ↑ / FX ↓)"
            bias_ge = "დოლარის გამყარების სიგნალი (Hawkish)"
        elif usd_score <= -2:
            sentiment_tag = "🟠 BEARISH USD (EUR/GBP/XAU ↑)"
            bias_ge = "დოლარის დასუსტების სიგნალი (Dovish)"
        elif risk_score >= 2:
            sentiment_tag = "🟢 RISK-ON (USTEC / BTC ↑)"
            bias_ge = "პოზიტიური იმპულსი ინდექსებისა და კრიპტოსთვის"
        elif risk_score <= -2:
            sentiment_tag = "🔴 RISK-OFF (USTEC ↓ / XAU ↑)"
            bias_ge = "რისკისგან გაქცევა (ინდექსებზე წნეხი, ოქროზე მოთხოვნა)"
        else:
            sentiment_tag = "⚪ MACRO WATCH"
            bias_ge = "საბაზრო მონიტორინგი"

        return {
            "impact": impact,
            "sentiment_tag": sentiment_tag,
            "bias_ge": bias_ge,
            "affected_symbols": ", ".join(affected_unique),
            "usd_score": usd_score,
            "gold_score": gold_score,
            "risk_score": risk_score,
        }

    def fetch_live_news_stream(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        now_ts = time.time()
        cached_news = self._memory_cache.get("news_stream", [])
        cached_ts = float(self._memory_cache.get("news_ts", 0.0) or 0.0)
        if cached_news and not force_refresh and (now_ts - cached_ts) < 180:
            return cached_news

        items_out: list[dict[str, Any]] = []
        seen_titles: set[str] = set()

        for feed_name, feed_url in LIVE_NEWS_FEEDS:
            try:
                xml_text = _http_get_text(feed_url, timeout=4.0)
                root_el = ET.fromstring(xml_text)
                for item_el in root_el.findall(".//item")[:8]:
                    title = (item_el.findtext("title") or "").strip()
                    pub_date = (item_el.findtext("pubDate") or "").strip()
                    link = (item_el.findtext("link") or "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    scored = self._score_headline_impact(title, feed_name)
                    items_out.append(
                        {
                            "source": feed_name,
                            "published": pub_date[:25] if pub_date else datetime.now(timezone.utc).strftime("%H:%M UTC"),
                            "title": title,
                            "link": link,
                            **scored,
                        }
                    )
            except Exception:
                continue

        # Sort HIGH impact first
        items_out.sort(key=lambda x: (0 if "HIGH" in str(x.get("impact", "")) else 1))
        top_news = items_out[:30]
        if top_news:
            self._memory_cache["news_stream"] = top_news
            self._memory_cache["news_ts"] = now_ts
            return top_news
        return cached_news if isinstance(cached_news, list) else []

    # ------------------------------------------------------------------
    # 4. Quantitative Macro Event Probability Forecaster (%)
    #    (NFP, CPI, FOMC, Jobless Claims, Yield Curve / VIX Regime)
    # ------------------------------------------------------------------
    def compute_macro_event_probabilities(
        self,
        fred: dict[str, dict[str, Any]],
        polymarket: list[dict[str, Any]],
        usd_basket_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Calculates % probabilities for major macro releases based on leading indicators."""
        forecasts: list[dict[str, Any]] = []

        # --- EVENT 1: US Non-Farm Payrolls (NFP) & Labor Market ---
        payems = fred.get("PAYEMS", {})
        icsa = fred.get("ICSA", {})
        unrate = fred.get("UNRATE", {})
        dgs2 = fred.get("DGS2", {})

        last_nfp_add = float(payems.get("delta", 150.0) or 150.0)  # in thousands (e.g. +165K)
        prev_nfp_add = float(payems.get("delta_prev", 110.0) or 110.0)
        claims_latest = float(icsa.get("latest", 220000.0) or 220000.0)
        claims_delta = float(icsa.get("delta", 0.0) or 0.0)
        unemp_latest = float(unrate.get("latest", 4.1) or 4.1)
        unemp_delta = float(unrate.get("delta", 0.0) or 0.0)

        # Quantitative scoring for NFP Beat (Strong Jobs) vs Miss (Weak Jobs)
        nfp_bull_usd_score = 50.0
        if last_nfp_add > prev_nfp_add:
            nfp_bull_usd_score += 8.0
        else:
            nfp_bull_usd_score -= 7.0
        if claims_latest < 225000:
            nfp_bull_usd_score += 7.0
        elif claims_latest > 240000:
            nfp_bull_usd_score -= 10.0
        if claims_delta < 0:
            nfp_bull_usd_score += 5.0
        elif claims_delta > 5000:
            nfp_bull_usd_score -= 6.0
        if unemp_delta <= 0:
            nfp_bull_usd_score += 4.0
        else:
            nfp_bull_usd_score -= 6.0
        nfp_bull_usd_score += usd_basket_score * 8.0

        nfp_beat_pct = max(15.0, min(78.0, round(nfp_bull_usd_score * 0.78, 1)))
        nfp_inline_pct = 20.0
        nfp_miss_pct = max(10.0, round(100.0 - nfp_beat_pct - nfp_inline_pct, 1))

        if nfp_beat_pct >= nfp_miss_pct + 8:
            nfp_verdict = f"🔵 STRONG NFP BIAS ({nfp_beat_pct:.0f}% Beat vs {nfp_miss_pct:.0f}% Miss)"
            nfp_impact_ge = (
                f"წინმსწრები ინდიკატორები (NFP +{last_nfp_add:.0f}K, უმუშევრობა {unemp_latest:.1f}%, Claims {claims_latest:,.0f}) "
                f"აჩვენებს შრომის ბაზრის სიმყარეს ({nfp_beat_pct:.0f}% Beat). ეს დოლარისთვის (USD) დადებითია, ხოლო ოქროზე (XAUUSD) პირველ რეაქციაზე ქმნის SELL წნეხს."
            )
        elif nfp_miss_pct >= nfp_beat_pct + 8:
            nfp_verdict = f"🟢 WEAK NFP / GOLD BULLISH ({nfp_miss_pct:.0f}% Miss vs {nfp_beat_pct:.0f}% Beat)"
            nfp_impact_ge = (
                f"შრომის ბაზრის შენელების ნიშნები ({nfp_miss_pct:.0f}% სუსტი მონაცემის ალბათობა). "
                f"სუსტი NFP ასუსტებს დოლარს და იწვევს ოქროს (XAUUSD) და Nasdaq-ის (USTEC) ძლიერ BUY იმპულსს."
            )
        else:
            nfp_verdict = f"⚖️ BALANCED NFP SETUP ({nfp_beat_pct:.0f}% Beat / {nfp_inline_pct:.0f}% Inline / {nfp_miss_pct:.0f}% Miss)"
            nfp_impact_ge = "NFP-ის წინ ბაზარი დაბალანსებულია — საუკეთესოა BreakoutStops v2.2-ით ორივე მხარეს იმპულსის დაჭერა."

        forecasts.append(
            {
                "event_code": "NFP_PAYROLLS",
                "event_name": "🇺🇸 US Non-Farm Payrolls (NFP) & Unemployment",
                "current_state": f"Last: +{last_nfp_add:.0f}K (Prior: +{prev_nfp_add:.0f}K) | Unemp: {unemp_latest:.1f}% | Claims: {claims_latest:,.0f}",
                "beat_hawkish_pct": nfp_beat_pct,
                "inline_neutral_pct": nfp_inline_pct,
                "miss_dovish_pct": nfp_miss_pct,
                "verdict": nfp_verdict,
                "xauusd_bias": "SELL on Beat / BUY on Miss" if abs(nfp_beat_pct - nfp_miss_pct) < 10 else ("BEARISH DIP -> BUY REVERSAL" if nfp_beat_pct > nfp_miss_pct else "BULLISH BREAKOUT ↑"),
                "usd_bias": "BULLISH USD ↑" if nfp_beat_pct >= nfp_miss_pct else "BEARISH USD ↓",
                "explanation_ge": nfp_impact_ge,
            }
        )

        # --- EVENT 2: US CPI & Core CPI Inflation ---
        cpi = fred.get("CPIAUCSL", {})
        core_cpi = fred.get("CPILFESL", {})
        breakeven10 = fred.get("T10YIE", {})

        cpi_yoy = float(cpi.get("yoy_pct", 2.6) or 2.6)
        cpi_mom = float(cpi.get("pct_change", 0.2) or 0.2)
        core_yoy = float(core_cpi.get("yoy_pct", 3.2) or 3.2)
        be_infl = float(breakeven10.get("latest", 2.3) or 2.3)
        be_delta = float(breakeven10.get("delta", 0.0) or 0.0)

        cpi_hot_score = 48.0
        if cpi_mom > 0.22:
            cpi_hot_score += 10.0
        elif cpi_mom < 0.15:
            cpi_hot_score -= 10.0
        if be_infl > 2.35:
            cpi_hot_score += 8.0
        elif be_infl < 2.15:
            cpi_hot_score -= 8.0
        if be_delta > 0:
            cpi_hot_score += 4.0

        cpi_hot_pct = max(15.0, min(75.0, round(cpi_hot_score * 0.76, 1)))
        cpi_inline_pct = 24.0
        cpi_cool_pct = max(10.0, round(100.0 - cpi_hot_pct - cpi_inline_pct, 1))

        forecasts.append(
            {
                "event_code": "US_CPI_INFLATION",
                "event_name": "🔥 US CPI & Core CPI (ინფლაციის პროგნოზი)",
                "current_state": f"CPI YoY: {cpi_yoy:.2f}% (MoM {cpi_mom:+.2f}%) | Core YoY: {core_yoy:.2f}% | 10Y Breakeven: {be_infl:.2f}%",
                "beat_hawkish_pct": cpi_hot_pct,
                "inline_neutral_pct": cpi_inline_pct,
                "miss_dovish_pct": cpi_cool_pct,
                "verdict": (
                    f"🔴 STICKY/HOT CPI RISK ({cpi_hot_pct:.0f}% Hot vs {cpi_cool_pct:.0f}% Cool)"
                    if cpi_hot_pct > cpi_cool_pct
                    else f"🟢 DISINFLATION / COOL CPI ({cpi_cool_pct:.0f}% Cool vs {cpi_hot_pct:.0f}% Hot)"
                ),
                "xauusd_bias": "VOLATILE SPIKE" if cpi_hot_pct > cpi_cool_pct else "BULLISH XAUUSD & USTEC ↑",
                "usd_bias": "BULLISH USD ↑" if cpi_hot_pct > cpi_cool_pct else "BEARISH USD ↓",
                "explanation_ge": (
                    f"ბაზრის ინფლაციური მოლოდინი (10Y Breakeven: {be_infl:.2f}%, Core CPI: {core_yoy:.2f}%). "
                    f"მაღალი CPI-ს ალბათობაა {cpi_hot_pct:.0f}%, ხოლო დაბალი/შენელებული CPI-ს — {cpi_cool_pct:.0f}%."
                ),
            }
        )

        # --- EVENT 3: FOMC / Federal Reserve Interest Rate Decision ---
        fed_rate = float((fred.get("FEDFUNDS", {}) or {}).get("latest", 4.58) or 4.58)
        y2_rate = float(dgs2.get("latest", 4.20) or 4.20)
        yield_gap = y2_rate - fed_rate  # Negative gap means bond market prices Fed Rate Cuts!

        # Check if Polymarket has a specific Fed market
        pm_fed_note = ""
        for pm in polymarket:
            if "fed" in str(pm.get("question", "")).lower() or "rate" in str(pm.get("question", "")).lower():
                pm_fed_note = f" | Polymarket: {pm['question'][:42]}... ({pm['yes_pct']:.0f}%)"
                break

        if yield_gap < -0.25:
            cut_dovish_pct = min(82.0, round(52.0 + abs(yield_gap) * 35.0, 1))
            hold_pct = round(max(12.0, 92.0 - cut_dovish_pct), 1)
            hike_hawkish_pct = round(max(4.0, 100.0 - cut_dovish_pct - hold_pct), 1)
        elif yield_gap > 0.15:
            hike_hawkish_pct = min(65.0, round(40.0 + yield_gap * 30.0, 1))
            hold_pct = 35.0
            cut_dovish_pct = round(max(8.0, 100.0 - hike_hawkish_pct - hold_pct), 1)
        else:
            hold_pct = 54.0
            cut_dovish_pct = 32.0
            hike_hawkish_pct = 14.0

        forecasts.append(
            {
                "event_code": "FOMC_RATE_DECISION",
                "event_name": "🏦 FOMC / Fed Interest Rate & Powell Guidance",
                "current_state": f"Fed Funds: {fed_rate:.2f}% vs 2Y Treasury: {y2_rate:.2f}% (Spread: {yield_gap:+.2f}%){pm_fed_note}",
                "beat_hawkish_pct": hike_hawkish_pct,
                "inline_neutral_pct": hold_pct,
                "miss_dovish_pct": cut_dovish_pct,
                "verdict": (
                    f"🟢 DOVISH / RATE CUT CYCLE ({cut_dovish_pct:.0f}% Cut/Dovish vs {hold_pct:.0f}% Hold)"
                    if cut_dovish_pct >= hold_pct
                    else f"🟡 FED HOLD / PAUSE ({hold_pct:.0f}% Hold vs {cut_dovish_pct:.0f}% Cut)"
                ),
                "xauusd_bias": "STRUCTURAL BULLISH GOLD ↑" if yield_gap < 0 else "RANGE / USD SUPPORTED",
                "usd_bias": "DOVISH MEDIUM-TERM" if yield_gap < -0.2 else "HAWKISH HOLD",
                "explanation_ge": (
                    f"2-წლიანი სახაზინო ობლიგაციების სპრედი Fed-ის განაკვეთთან არის {yield_gap:+.2f}%. "
                    f"როცა 2Y Yield ({y2_rate:.2f}%) Fed-ის განაკვეთზე ({fed_rate:.2f}%) დაბალია, ბონდების ბაზარი ითვლის განაკვეთის დაწევას ({cut_dovish_pct:.0f}% Dovish), რაც გრძელვადიანად ოქროს (XAUUSD) მთავარი საწვავია!"
                ),
            }
        )

        # --- EVENT 4: Global Risk Regime (VIX Volatility & 10Y-2Y Yield Curve) ---
        vix_val = float((fred.get("VIXCLS", {}) or {}).get("latest", 18.0) or 18.0)
        yc_val = float((fred.get("T10Y2Y", {}) or {}).get("latest", 0.15) or 0.15)

        risk_off_pct = min(80.0, max(15.0, round((vix_val - 12.0) * 3.2 + (15.0 if yc_val < 0 else 0.0), 1)))
        neutral_regime_pct = 22.0
        risk_on_pct = max(10.0, round(100.0 - risk_off_pct - neutral_regime_pct, 1))

        forecasts.append(
            {
                "event_code": "MACRO_RISK_REGIME",
                "event_name": "🌍 Wall Street Risk Regime (VIX & 10Y-2Y Yield Curve)",
                "current_state": f"VIX Index: {vix_val:.2f} | 10Y-2Y Yield Spread: {yc_val:+.2f}% | USD Live Basket: {usd_basket_score:+.2f}",
                "beat_hawkish_pct": risk_off_pct,
                "inline_neutral_pct": neutral_regime_pct,
                "miss_dovish_pct": risk_on_pct,
                "verdict": (
                    f"🟢 RISK-ON REGIME ({risk_on_pct:.0f}% Bullish Equities/Crypto)"
                    if risk_on_pct >= risk_off_pct
                    else f"🔴 RISK-OFF / HIGH VOLATILITY ({risk_off_pct:.0f}% Safe-Haven Gold Flow)"
                ),
                "xauusd_bias": "SAFE-HAVEN DEMAND ↑" if risk_off_pct > 45 else "TECHNICAL FLOW",
                "usd_bias": f"LIVE USD BASKET: {usd_basket_score:+.2f}",
                "explanation_ge": (
                    f"VIX შიშის ინდექსი არის {vix_val:.2f}-ზე, ხოლო 10Y-2Y სპრედი {yc_val:+.2f}%-ზე. "
                    f"Risk-On (ინდექსებისა და კრიპტოს ზრდის) ალბათობაა {risk_on_pct:.0f}%, ხოლო Risk-Off (ოქროსა და დოლარში თავშესაფრის ძებნა) — {risk_off_pct:.0f}%."
                ),
            }
        )

        # --- EVENT 5: Weekly Initial Jobless Claims (ყოველ ხუთშაბათს 16:30/17:30) ---
        claims_beat_pct = max(18.0, min(74.0, round(52.0 - (claims_delta / 1000.0) * 1.5, 1)))
        claims_inline_pct = 20.0
        claims_miss_pct = max(12.0, round(100.0 - claims_beat_pct - claims_inline_pct, 1))
        forecasts.append(
            {
                "event_code": "JOBLESS_CLAIMS",
                "event_name": "📅 Weekly Initial Jobless Claims (ხუთშაბათის უმუშევრობის განაცხადები)",
                "current_state": f"Latest Claims: {claims_latest:,.0f} (Weekly Change: {claims_delta:+,.0f})",
                "beat_hawkish_pct": claims_beat_pct,
                "inline_neutral_pct": claims_inline_pct,
                "miss_dovish_pct": claims_miss_pct,
                "verdict": (
                    f"🔵 LOW CLAIMS / STRONG LABOR ({claims_beat_pct:.0f}% USD Bullish)"
                    if claims_beat_pct >= claims_miss_pct
                    else f"🟢 RISING CLAIMS / GOLD BULLISH ({claims_miss_pct:.0f}% XAUUSD Bullish)"
                ),
                "xauusd_bias": "SELL SPIKE / RANGE" if claims_beat_pct >= claims_miss_pct else "BUY BREAKOUT ↑",
                "usd_bias": "BULLISH USD ↑" if claims_beat_pct >= claims_miss_pct else "BEARISH USD ↓",
                "explanation_ge": (
                    f"ყოველკვირეული უმუშევრობის განაცხადები არის {claims_latest:,.0f} ({claims_delta:+,.0f}). "
                    f"დაბალი განაცხადების (ძლიერი დოლარის) ალბათობაა {claims_beat_pct:.0f}%, ხოლო გაზრდილი განაცხადების (ოქროს ზრდის) — {claims_miss_pct:.0f}%."
                ),
            }
        )

        # --- EVENT 6: US Retail Sales & Consumer Strength ---
        rsafs = fred.get("RSAFS", {})
        ret_mom = float(rsafs.get("pct_change", 0.3) or 0.3)
        ret_yoy = float(rsafs.get("yoy_pct", 2.8) or 2.8)
        ret_beat_pct = max(20.0, min(72.0, round(46.0 + ret_mom * 18.0, 1)))
        ret_inline_pct = 22.0
        ret_miss_pct = max(12.0, round(100.0 - ret_beat_pct - ret_inline_pct, 1))
        forecasts.append(
            {
                "event_code": "US_RETAIL_SALES",
                "event_name": "🛒 US Retail Sales (საცალო გაყიდვები და სამომხმარებლო აქტივობა)",
                "current_state": f"MoM Change: {ret_mom:+.2f}% | YoY Growth: {ret_yoy:+.2f}%",
                "beat_hawkish_pct": ret_beat_pct,
                "inline_neutral_pct": ret_inline_pct,
                "miss_dovish_pct": ret_miss_pct,
                "verdict": (
                    f"🔵 STRONG CONSUMER ({ret_beat_pct:.0f}% Beat vs {ret_miss_pct:.0f}% Miss)"
                    if ret_beat_pct >= ret_miss_pct
                    else f"🟠 CONSUMER SLOWDOWN ({ret_miss_pct:.0f}% Miss vs {ret_beat_pct:.0f}% Beat)"
                ),
                "xauusd_bias": "MODERATE SELL PRESSURE" if ret_beat_pct >= ret_miss_pct else "BULLISH XAUUSD ↑",
                "usd_bias": "BULLISH USD & USTEC ↑" if ret_beat_pct >= ret_miss_pct else "BEARISH USD ↓",
                "explanation_ge": (
                    f"აშშ-ის საცალო გაყიდვების ტემპი არის {ret_mom:+.2f}% (თვიური) და {ret_yoy:+.2f}% (წლიური). "
                    f"პროგნოზზე უკეთესი შედეგის (Beat) ალბათობაა {ret_beat_pct:.0f}%, ხოლო სუსტი შედეგის (Miss) — {ret_miss_pct:.0f}%."
                ),
            }
        )

        return forecasts

    # ------------------------------------------------------------------
    # 5. Live 12-Instrument Multi-Asset Matrix (MT5 Direct)
    # ------------------------------------------------------------------
    def fetch_12_asset_terminal_matrix(self, force_refresh: bool = False) -> tuple[list[dict[str, Any]], float]:
        """Pulls live MT5 stats across all 12 instruments (Gold, 7 FX, 2 Indices, 2 Crypto)."""
        now_ts = time.time()
        last_snap = self._memory_cache.get("last_full_snapshot", {})
        cached_matrix = last_snap.get("terminal_12_matrix", []) if isinstance(last_snap, dict) else []
        cached_usd = float(last_snap.get("usd_basket_score", 0.0) or 0.0) if isinstance(last_snap, dict) else 0.0
        matrix_ts = float(self._memory_cache.get("matrix_ts", 0.0) or 0.0)

        try:
            import MetaTrader5 as mt5

            mt5_ok = mt5.initialize()
        except Exception:
            mt5 = None
            mt5_ok = False

        if not mt5_ok or mt5 is None:
            # If called from Python 3.15 UI (.venv-api), return cached 12-asset matrix if valid
            if cached_matrix and any(float(r.get("price", 0.0) or 0.0) > 0 for r in cached_matrix):
                return cached_matrix, cached_usd
            # Otherwise invoke Python 3.14 silently via pythonw.exe (zero console window)
            pyw = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python314" / "pythonw.exe"
            if not pyw.is_file():
                pyw = Path(r"C:\Users\LeVI\AppData\Local\Programs\Python\Python314\pythonw.exe")
            if pyw.is_file():
                try:
                    import subprocess

                    sinfo = None
                    if hasattr(subprocess, "STARTUPINFO"):
                        sinfo = subprocess.STARTUPINFO()
                        sinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                        sinfo.wShowWindow = 0
                    subprocess.run(
                        [
                            str(pyw),
                            "-c",
                            "from newera_api.macro_news_intelligence import MacroNewsIntelligenceHub; MacroNewsIntelligenceHub().get_full_intelligence_snapshot(force_refresh=False)",
                        ],
                        cwd=str(self.root),
                        capture_output=True,
                        timeout=12,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        startupinfo=sinfo,
                    )
                    reloaded = self._load_disk_cache().get("last_full_snapshot", {})
                    if isinstance(reloaded, dict) and reloaded.get("terminal_12_matrix"):
                        return reloaded["terminal_12_matrix"], float(reloaded.get("usd_basket_score", 0.0) or 0.0)
                except Exception:
                    pass
            return cached_matrix, cached_usd

        rows: list[dict[str, Any]] = []
        usd_scores: list[float] = []
        for item in ALL_12_TERMINAL_SYMBOLS:
            sym = item["symbol"]
            cat = item["category"]
            name_ge = item["name_ge"]

            if not mt5_ok or mt5 is None:
                rows.append(
                    {
                        "symbol": sym,
                        "category": cat,
                        "name_ge": name_ge,
                        "price": 0.0,
                        "spread_pts": 0.0,
                        "change_1h_pct": 0.0,
                        "change_24h_pct": 0.0,
                        "rsi_14": 50.0,
                        "atr_pts": 0.0,
                        "z_score": 0.0,
                        "bull_score": 50,
                        "bear_score": 50,
                        "verdict": "WAITING MT5",
                    }
                )
                continue

            try:
                mt5.symbol_select(sym, True)
                info = mt5.symbol_info(sym)
                tick = mt5.symbol_info_tick(sym)
                rates_m15 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 100)
                if info is None or tick is None or rates_m15 is None or len(rates_m15) < 30:
                    continue

                point = float(info.point or 0.00001)
                digits = int(info.digits or 2)
                bid = float(tick.bid or 0.0)
                ask = float(tick.ask or 0.0)
                spread_pts = round((ask - bid) / point, 1) if point > 0 else 0.0

                closes = [float(r["close"]) for r in rates_m15]
                highs = [float(r["high"]) for r in rates_m15]
                lows = [float(r["low"]) for r in rates_m15]

                c_now = closes[-1]
                c_1h = closes[-5] if len(closes) >= 5 else closes[0]
                c_24h = closes[-96] if len(closes) >= 96 else closes[0]

                chg_1h = ((c_now - c_1h) / c_1h * 100.0) if c_1h else 0.0
                chg_24h = ((c_now - c_24h) / c_24h * 100.0) if c_24h else 0.0

                # Track USD basket from the 7 FX pairs
                if sym in {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}:
                    usd_scores.append(-1.0 if chg_1h > 0.03 else (1.0 if chg_1h < -0.03 else 0.0))
                elif sym in {"USDJPY", "USDCAD", "USDCHF"}:
                    usd_scores.append(1.0 if chg_1h > 0.03 else (-1.0 if chg_1h < -0.03 else 0.0))

                # RSI(14)
                gains = 0.0
                losses = 0.0
                for i in range(-14, 0):
                    diff = closes[i] - closes[i - 1]
                    if diff > 0:
                        gains += diff
                    else:
                        losses -= diff
                rs = (gains / 14.0) / max(losses / 14.0, 1e-9)
                rsi_14 = 100.0 - (100.0 / (1.0 + rs))

                # ATR(14) in points
                trs = []
                for i in range(-14, 0):
                    tr = max(
                        highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]),
                    )
                    trs.append(tr)
                atr_pts = (sum(trs) / len(trs)) / point if point > 0 else 0.0

                # Z-Score(20)
                w20 = closes[-20:]
                mean20 = sum(w20) / 20.0
                std20 = math.sqrt(sum((x - mean20) ** 2 for x in w20) / 20.0) or 1e-9
                z_score = (c_now - mean20) / std20

                # Bull vs Bear Score
                ema20 = sum(closes[-20:]) / 20.0
                ema50 = sum(closes[-50:]) / 50.0
                bull = 50
                bear = 50
                if c_now > ema20 > ema50:
                    bull += 25
                    bear -= 15
                elif c_now < ema20 < ema50:
                    bear += 25
                    bull -= 15
                if chg_1h > 0.08:
                    bull += 15
                elif chg_1h < -0.08:
                    bear += 15
                if rsi_14 > 55 and rsi_14 < 72:
                    bull += 10
                elif rsi_14 < 45 and rsi_14 > 28:
                    bear += 10

                bull = max(5, min(98, bull))
                bear = max(5, min(98, bear))

                if bull - bear >= 20:
                    verdict = f"🟢 STRONG BUY (B:{bull}/S:{bear})"
                elif bear - bull >= 20:
                    verdict = f"🔴 STRONG SELL (B:{bull}/S:{bear})"
                else:
                    verdict = f"⚪ RANGE / NEUTRAL (B:{bull}/S:{bear})"

                rows.append(
                    {
                        "symbol": sym,
                        "category": cat,
                        "name_ge": name_ge,
                        "digits": digits,
                        "price": round(bid, digits),
                        "spread_pts": round(spread_pts, 1),
                        "change_1h_pct": round(chg_1h, 2),
                        "change_24h_pct": round(chg_24h, 2),
                        "rsi_14": round(rsi_14, 1),
                        "atr_pts": round(atr_pts, 0),
                        "z_score": round(z_score, 2),
                        "bull_score": bull,
                        "bear_score": bear,
                        "verdict": verdict,
                    }
                )
            except Exception:
                continue

        usd_basket = round(sum(usd_scores) / len(usd_scores), 2) if usd_scores else 0.0
        return rows, usd_basket

    # ------------------------------------------------------------------
    # 6. Full Unified Snapshot for UI & Bot Fleet
    # ------------------------------------------------------------------
    def get_full_intelligence_snapshot(self, force_refresh: bool = False) -> dict[str, Any]:
        matrix_12, usd_basket = self.fetch_12_asset_terminal_matrix()
        fred_data = self.fetch_fred_macro_snapshot(force_refresh=force_refresh)
        polymarket_data = self.fetch_polymarket_odds(force_refresh=force_refresh)
        news_stream = self.fetch_live_news_stream(force_refresh=force_refresh)
        event_forecasts = self.compute_macro_event_probabilities(
            fred=fred_data,
            polymarket=polymarket_data,
            usd_basket_score=usd_basket,
        )

        # Aggregate overall News Sentiment for Gold and USD
        gold_news_net = sum(int(n.get("gold_score", 0)) for n in news_stream[:12])
        usd_news_net = sum(int(n.get("usd_score", 0)) for n in news_stream[:12])
        risk_news_net = sum(int(n.get("risk_score", 0)) for n in news_stream[:12])

        snapshot = {
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "usd_basket_score": usd_basket,
            "news_sentiment_summary": {
                "gold_net_score": gold_news_net,
                "usd_net_score": usd_news_net,
                "risk_net_score": risk_news_net,
                "gold_bias_label": "BULLISH GOLD ↑" if gold_news_net > 1 else ("BEARISH GOLD ↓" if gold_news_net < -1 else "NEUTRAL"),
                "usd_bias_label": "BULLISH USD ↑" if usd_news_net > 1 else ("BEARISH USD ↓" if usd_news_net < -1 else "NEUTRAL"),
            },
            "macro_event_forecasts": event_forecasts,
            "fred_series": fred_data,
            "polymarket_odds": polymarket_data,
            "live_news_stream": news_stream,
            "terminal_12_matrix": matrix_12,
        }
        self._memory_cache["last_full_snapshot"] = snapshot
        self._save_disk_cache(self._memory_cache)
        return snapshot
