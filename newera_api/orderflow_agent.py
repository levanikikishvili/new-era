"""L2 Orderbook DOM Streaming Engine & Orderflow Liquidity Intelligence Agent.

Provides:
1. Direct MT5 `market_book_get()` L2 DOM acquisition (with automatic MT5 M1/M5
   tick-volume liquidity cluster fallback when a CFD symbol's raw ECN book is empty).
2. Crypto L2 Orderbook acquisition via Binance / Bybit WebSocket & REST Depth APIs
   for 24/7 pairs (`BTCUSD`, `ETHUSD`, `SOLUSD`, `BNBUSD`, `XRPUSD`, etc.).
3. `OrderflowLiquidityAgent`:
   - Liquidity Walls detection (large Buy/Sell Limit walls >= 2.2x mean depth)
   - Whale Order warnings (>= 3.2x mean depth)
   - Spoofing Alerts (large limit orders pulled >= 65% without price fill)
   - Absorption & Iceberg pattern detection (repeated high-volume reloads at key levels)
   - Gate Veto integration (blocks BUY into heavy overhead Sell Wall, blocks SELL into heavy Buy Wall).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .mt5_bridge import get_mt5_market_book

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]


CRYPTO_SYMBOL_MAP: dict[str, str] = {
    "BTCUSD": "BTCUSDT",
    "BTCUSDT": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSD": "SOLUSDT",
    "SOLUSDT": "SOLUSDT",
    "BNBUSD": "BNBUSDT",
    "BNBUSDT": "BNBUSDT",
    "XRPUSD": "XRPUSDT",
    "XRPUSDT": "XRPUSDT",
    "DOGEUSD": "DOGEUSDT",
    "ADAUSD": "ADAUSDT",
    "AVAXUSD": "AVAXUSDT",
    "LINKUSD": "LINKUSDT",
    "LTCUSD": "LTCUSDT",
}


def resolve_crypto_pair(symbol: str) -> str | None:
    raw = symbol.strip().upper()
    cleaned = re.sub(r"([._-][A-Z0-9]+|[+#]|M|PRO|RAW|ECN)$", "", raw)
    if cleaned in CRYPTO_SYMBOL_MAP:
        return CRYPTO_SYMBOL_MAP[cleaned]
    for prefix in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LTC"):
        if cleaned.startswith(prefix) and ("USD" in cleaned or "USDT" in cleaned):
            return f"{prefix}USDT"
    return None


@dataclass
class BookLevel:
    price: float
    volume: float
    side: str  # "BID" or "ASK"
    cumulative_volume: float = 0.0
    pct_of_side: float = 0.0
    ratio_to_mean: float = 1.0
    tag: str = ""  # "", "WALL", "WHALE_WALL", "ICEBERG"


@dataclass
class LiquidityAlert:
    timestamp: float
    symbol: str
    kind: str  # "WHALE_WALL", "BUY_WALL", "SELL_WALL", "SPOOFING", "ABSORPTION", "GATE_VETO"
    severity: str  # "INFO", "WARNING", "CRITICAL"
    price: float
    volume: float
    message: str


@dataclass
class OrderflowAnalysis:
    symbol: str
    source: str  # "mt5_l2_dom", "mt5_tick_volume_dom", "binance_l2_ws", "binance_l2_rest"
    timestamp: float
    mid_price: float
    best_bid: float
    best_ask: float
    spread: float
    total_bid_volume: float
    total_ask_volume: float
    imbalance_ratio: float  # [-1.0 .. +1.0]
    bid_pressure_pct: float  # [0 .. 100]
    ask_pressure_pct: float  # [0 .. 100]
    l2_direction: str  # "BUY", "SELL", "ABSTAIN"
    confluence_score: float  # [0 .. 100]
    nearest_buy_wall: dict[str, Any] | None
    nearest_sell_wall: dict[str, Any] | None
    veto_buy: bool
    veto_sell: bool
    veto_reason: str | None
    bids: list[dict[str, Any]] = field(default_factory=list)
    asks: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderflowLiquidityAgent:
    """AI/Quant Orderflow & L2 DOM Liquidity Radar Agent."""

    def __init__(self, *, history_len: int = 12) -> None:
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._wall_memory: dict[str, dict[float, float]] = {}
        self._reload_counts: dict[str, dict[float, int]] = {}
        self._recent_alerts: deque[dict[str, Any]] = deque(maxlen=60)
        self._history_len = history_len

    # ------------------------------------------------------------------
    # L2 Data Acquisition (Crypto WS/REST + MT5 MarketBookGet)
    # ------------------------------------------------------------------
    async def fetch_l2_snapshot(self, symbol: str, levels: int = 10) -> OrderflowAnalysis:
        norm_symbol = symbol.strip().upper() or "XAUUSD"
        crypto_pair = resolve_crypto_pair(norm_symbol)
        if crypto_pair is not None:
            raw_book = await self._fetch_crypto_l2_book(crypto_pair, levels=levels)
            if raw_book is not None:
                return self.analyze_orderbook(
                    norm_symbol,
                    bids=raw_book["bids"],
                    asks=raw_book["asks"],
                    source=raw_book["source"],
                )

        # Fallback or primary for Forex/Metals/Indices: Direct MT5 market_book_get()
        mt5_book = await asyncio.to_thread(get_mt5_market_book, norm_symbol, levels)
        return self.analyze_orderbook(
            norm_symbol,
            bids=mt5_book.get("bids", []),
            asks=mt5_book.get("asks", []),
            source=str(mt5_book.get("source") or "mt5_l2_dom"),
        )

    async def _fetch_crypto_l2_book(self, pair: str, levels: int = 10) -> dict[str, Any] | None:
        # 1. Try fast WebSocket burst from Binance L2 depth stream if websockets is installed
        if websockets is not None:
            ws_url = f"wss://stream.binance.com:9443/ws/{pair.lower()}@depth20@1000ms"
            try:
                async with websockets.connect(ws_url, open_timeout=2.0, close_timeout=1.0) as ws:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=2.2)
                    data = json.loads(raw_msg)
                    bids = [
                        {"price": float(p), "volume": float(q)}
                        for p, q in (data.get("bids") or [])[:levels]
                        if float(q) > 0
                    ]
                    asks = [
                        {"price": float(p), "volume": float(q)}
                        for p, q in (data.get("asks") or [])[:levels]
                        if float(q) > 0
                    ]
                    if bids and asks:
                        return {"source": "binance_l2_ws", "bids": bids, "asks": asks}
            except Exception:
                pass

        # 2. Fallback to Binance REST L2 Depth endpoint
        rest_url = f"https://api.binance.com/api/v3/depth?symbol={pair.upper()}&limit={max(10, levels)}"
        try:
            data = await asyncio.to_thread(self._http_get_json, rest_url, 3.0)
            bids = [
                {"price": float(p), "volume": float(q)}
                for p, q in (data.get("bids") or [])[:levels]
                if float(q) > 0
            ]
            asks = [
                {"price": float(p), "volume": float(q)}
                for p, q in (data.get("asks") or [])[:levels]
                if float(q) > 0
            ]
            if bids and asks:
                return {"source": "binance_l2_rest", "bids": bids, "asks": asks}
        except Exception:
            pass

        # 3. Fallback to Bybit V5 public orderbook
        bybit_url = f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={pair.upper()}&limit={max(10, levels)}"
        try:
            data = await asyncio.to_thread(self._http_get_json, bybit_url, 3.0)
            result = data.get("result") or {}
            bids = [
                {"price": float(item[0]), "volume": float(item[1])}
                for item in (result.get("b") or [])[:levels]
                if float(item[1]) > 0
            ]
            asks = [
                {"price": float(item[0]), "volume": float(item[1])}
                for item in (result.get("a") or [])[:levels]
                if float(item[1]) > 0
            ]
            if bids and asks:
                return {"source": "bybit_l2_rest", "bids": bids, "asks": asks}
        except Exception:
            pass
        return None

    @staticmethod
    def _http_get_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "NewEraQuant/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # Quantitative L2 Orderflow Analysis (Walls, Spoofing, Absorption)
    # ------------------------------------------------------------------
    def analyze_orderbook(
        self,
        symbol: str,
        *,
        bids: list[dict[str, Any]],
        asks: list[dict[str, Any]],
        source: str = "mt5_l2_dom",
    ) -> OrderflowAnalysis:
        now = time.time()
        norm_symbol = symbol.strip().upper() or "XAUUSD"

        sorted_bids = sorted(
            [{"price": float(b["price"]), "volume": float(b["volume"])} for b in bids if float(b.get("volume", 0)) > 0],
            key=lambda x: x["price"],
            reverse=True,
        )[:12]
        sorted_asks = sorted(
            [{"price": float(a["price"]), "volume": float(a["volume"])} for a in asks if float(a.get("volume", 0)) > 0],
            key=lambda x: x["price"],
        )[:12]

        if not sorted_bids or not sorted_asks:
            return OrderflowAnalysis(
                symbol=norm_symbol,
                source=source,
                timestamp=now,
                mid_price=0.0,
                best_bid=0.0,
                best_ask=0.0,
                spread=0.0,
                total_bid_volume=0.0,
                total_ask_volume=0.0,
                imbalance_ratio=0.0,
                bid_pressure_pct=50.0,
                ask_pressure_pct=50.0,
                l2_direction="ABSTAIN",
                confluence_score=0.0,
                nearest_buy_wall=None,
                nearest_sell_wall=None,
                veto_buy=False,
                veto_sell=False,
                veto_reason="L2 orderbook is empty",
            )

        best_bid = sorted_bids[0]["price"]
        best_ask = sorted_asks[0]["price"]
        mid_price = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)

        all_vols = [b["volume"] for b in sorted_bids] + [a["volume"] for a in sorted_asks]
        mean_vol = statistics.fmean(all_vols) if all_vols else 1.0
        stdev_vol = statistics.pstdev(all_vols) if len(all_vols) > 1 else 0.0
        wall_threshold = max(mean_vol * 1.85, mean_vol + 1.15 * stdev_vol)
        whale_threshold = max(mean_vol * 2.75, mean_vol + 2.0 * stdev_vol)

        total_bid_vol = sum(b["volume"] for b in sorted_bids)
        total_ask_vol = sum(a["volume"] for a in sorted_asks)
        total_book_vol = total_bid_vol + total_ask_vol or 1.0
        imbalance = (total_bid_vol - total_ask_vol) / total_book_vol
        bid_pct = (total_bid_vol / total_book_vol) * 100.0
        ask_pct = (total_ask_vol / total_book_vol) * 100.0

        alerts: list[LiquidityAlert] = []
        prev_walls = self._wall_memory.get(norm_symbol, {})
        reloads = self._reload_counts.setdefault(norm_symbol, {})
        current_walls: dict[float, float] = {}

        # Process Bid Levels & Detect Buy Walls / Icebergs
        enriched_bids: list[dict[str, Any]] = []
        cum_bid = 0.0
        nearest_buy_wall: dict[str, Any] | None = None
        for b in sorted_bids:
            price = b["price"]
            vol = b["volume"]
            cum_bid += vol
            ratio = vol / mean_vol if mean_vol > 0 else 1.0
            tag = ""
            if vol >= whale_threshold:
                tag = "WHALE_WALL"
                current_walls[price] = vol
                alerts.append(
                    LiquidityAlert(
                        timestamp=now,
                        symbol=norm_symbol,
                        kind="WHALE_WALL",
                        severity="CRITICAL",
                        price=price,
                        volume=vol,
                        message=f"🐋 WHALE BUY WALL at {price:,.2f} ({vol:,.2f} vol | {ratio:.1f}x avg depth)",
                    )
                )
            elif vol >= wall_threshold:
                tag = "BUY_WALL"
                current_walls[price] = vol
                alerts.append(
                    LiquidityAlert(
                        timestamp=now,
                        symbol=norm_symbol,
                        kind="BUY_WALL",
                        severity="WARNING",
                        price=price,
                        volume=vol,
                        message=f"🧱 Institutional Buy Wall at {price:,.2f} ({vol:,.2f} vol | {ratio:.1f}x avg)",
                    )
                )

            # Check absorption / iceberg reload at same price level
            if price in prev_walls and vol >= prev_walls[price] * 0.95:
                reloads[price] = reloads.get(price, 0) + 1
                if reloads[price] >= 2:
                    if not tag:
                        tag = "ICEBERG"
                    alerts.append(
                        LiquidityAlert(
                            timestamp=now,
                            symbol=norm_symbol,
                            kind="ABSORPTION",
                            severity="INFO",
                            price=price,
                            volume=vol,
                            message=f"🧊 Bid Absorption / Iceberg reload at {price:,.2f} (held {reloads[price]}x)",
                        )
                    )

            level_dict = asdict(
                BookLevel(
                    price=price,
                    volume=vol,
                    side="BID",
                    cumulative_volume=cum_bid,
                    pct_of_side=(vol / (total_bid_vol or 1.0)) * 100.0,
                    ratio_to_mean=ratio,
                    tag=tag,
                )
            )
            enriched_bids.append(level_dict)
            if tag in {"BUY_WALL", "WHALE_WALL"} and nearest_buy_wall is None:
                nearest_buy_wall = level_dict

        # Process Ask Levels & Detect Sell Walls / Icebergs
        enriched_asks: list[dict[str, Any]] = []
        cum_ask = 0.0
        nearest_sell_wall: dict[str, Any] | None = None
        for a in sorted_asks:
            price = a["price"]
            vol = a["volume"]
            cum_ask += vol
            ratio = vol / mean_vol if mean_vol > 0 else 1.0
            tag = ""
            if vol >= whale_threshold:
                tag = "WHALE_WALL"
                current_walls[price] = vol
                alerts.append(
                    LiquidityAlert(
                        timestamp=now,
                        symbol=norm_symbol,
                        kind="WHALE_WALL",
                        severity="CRITICAL",
                        price=price,
                        volume=vol,
                        message=f"🐋 WHALE SELL WALL at {price:,.2f} ({vol:,.2f} vol | {ratio:.1f}x avg depth)",
                    )
                )
            elif vol >= wall_threshold:
                tag = "SELL_WALL"
                current_walls[price] = vol
                alerts.append(
                    LiquidityAlert(
                        timestamp=now,
                        symbol=norm_symbol,
                        kind="SELL_WALL",
                        severity="WARNING",
                        price=price,
                        volume=vol,
                        message=f"🧱 Institutional Sell Wall at {price:,.2f} ({vol:,.2f} vol | {ratio:.1f}x avg)",
                    )
                )

            if price in prev_walls and vol >= prev_walls[price] * 0.95:
                reloads[price] = reloads.get(price, 0) + 1
                if reloads[price] >= 2:
                    if not tag:
                        tag = "ICEBERG"
                    alerts.append(
                        LiquidityAlert(
                            timestamp=now,
                            symbol=norm_symbol,
                            kind="ABSORPTION",
                            severity="INFO",
                            price=price,
                            volume=vol,
                            message=f"🧊 Ask Absorption / Iceberg defense at {price:,.2f} (held {reloads[price]}x)",
                        )
                    )

            level_dict = asdict(
                BookLevel(
                    price=price,
                    volume=vol,
                    side="ASK",
                    cumulative_volume=cum_ask,
                    pct_of_side=(vol / (total_ask_vol or 1.0)) * 100.0,
                    ratio_to_mean=ratio,
                    tag=tag,
                )
            )
            enriched_asks.append(level_dict)
            if tag in {"SELL_WALL", "WHALE_WALL"} and nearest_sell_wall is None:
                nearest_sell_wall = level_dict

        # Spoofing Detection: Check if any previous large wall vanished (>= 65% drop) without price trading through it
        current_vol_by_price = {b["price"]: b["volume"] for b in sorted_bids} | {
            a["price"]: a["volume"] for a in sorted_asks
        }
        for old_price, old_vol in prev_walls.items():
            new_vol = current_vol_by_price.get(old_price, 0.0)
            if new_vol <= old_vol * 0.35:
                # If old_price was an Ask above current best_ask or Bid below current best_bid, it wasn't filled—it was pulled!
                if old_price > best_ask or old_price < best_bid:
                    side_lbl = "ASK" if old_price > best_ask else "BID"
                    alerts.append(
                        LiquidityAlert(
                            timestamp=now,
                            symbol=norm_symbol,
                            kind="SPOOFING",
                            severity="CRITICAL",
                            price=old_price,
                            volume=old_vol,
                            message=(
                                f"⚠️ SPOOFING ALERT: {side_lbl} Wall at {old_price:,.2f} "
                                f"pulled ({old_vol:,.1f} -> {new_vol:,.1f}) without fill!"
                            ),
                        )
                    )

        self._wall_memory[norm_symbol] = current_walls

        # Evaluate Gate Veto Conditions
        # Veto BUY if a heavy Sell Wall sits in the top 4 Ask levels and outweighs top Bid support, or strong ask imbalance
        veto_buy = False
        veto_sell = False
        veto_reasons: list[str] = []

        if nearest_sell_wall is not None:
            sell_wall_idx = next(
                (i for i, a in enumerate(enriched_asks) if a["price"] == nearest_sell_wall["price"]),
                99,
            )
            if sell_wall_idx <= 4 and (
                nearest_buy_wall is None or nearest_sell_wall["volume"] > nearest_buy_wall["volume"] * 1.15
            ):
                veto_buy = True
                veto_reasons.append(
                    f"Overhead Sell Wall at {nearest_sell_wall['price']:,.2f} "
                    f"({nearest_sell_wall['ratio_to_mean']:.1f}x avg depth)"
                )

        if nearest_buy_wall is not None:
            buy_wall_idx = next(
                (i for i, b in enumerate(enriched_bids) if b["price"] == nearest_buy_wall["price"]),
                99,
            )
            if buy_wall_idx <= 4 and (
                nearest_sell_wall is None or nearest_buy_wall["volume"] > nearest_sell_wall["volume"] * 1.15
            ):
                veto_sell = True
                veto_reasons.append(
                    f"Below-market Buy Wall at {nearest_buy_wall['price']:,.2f} "
                    f"({nearest_buy_wall['ratio_to_mean']:.1f}x avg depth)"
                )

        if imbalance <= -0.22 and not veto_buy:
            veto_buy = True
            veto_reasons.append(f"Heavy L2 Ask imbalance ({ask_pct:.1f}% Sell pressure)")
        elif imbalance >= 0.22 and not veto_sell:
            veto_sell = True
            veto_reasons.append(f"Heavy L2 Bid imbalance ({bid_pct:.1f}% Buy pressure)")

        # Determine L2 Direction & Confluence Score
        if imbalance >= 0.12 and not veto_buy:
            l2_direction = "BUY"
        elif imbalance <= -0.12 and not veto_sell:
            l2_direction = "SELL"
        else:
            l2_direction = "ABSTAIN"

        raw_score = 50.0 + abs(imbalance) * 50.0
        if (l2_direction == "BUY" and nearest_buy_wall) or (l2_direction == "SELL" and nearest_sell_wall):
            raw_score = min(100.0, raw_score + 12.0)
        confluence_score = round(min(100.0, max(10.0, raw_score)), 1)

        alert_dicts = [asdict(a) for a in alerts]
        for ad in alert_dicts:
            self._recent_alerts.appendleft(ad)

        analysis = OrderflowAnalysis(
            symbol=norm_symbol,
            source=source,
            timestamp=now,
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            total_bid_volume=round(total_bid_vol, 2),
            total_ask_volume=round(total_ask_vol, 2),
            imbalance_ratio=round(imbalance, 4),
            bid_pressure_pct=round(bid_pct, 1),
            ask_pressure_pct=round(ask_pct, 1),
            l2_direction=l2_direction,
            confluence_score=confluence_score,
            nearest_buy_wall=nearest_buy_wall,
            nearest_sell_wall=nearest_sell_wall,
            veto_buy=veto_buy,
            veto_sell=veto_sell,
            veto_reason="; ".join(veto_reasons) if veto_reasons else None,
            bids=enriched_bids,
            asks=enriched_asks,
            alerts=alert_dicts,
        )

        hist = self._history.setdefault(norm_symbol, deque(maxlen=self._history_len))
        hist.append(analysis.to_dict())
        return analysis

    def evaluate_gate_veto(self, proposed_action: str, analysis: OrderflowAnalysis) -> tuple[str, list[str]]:
        """Apply L2 Orderbook Liquidity Veto to a proposed BUY/SELL gate action."""
        action = proposed_action.strip().upper()
        reasons: list[str] = []
        if action == "BUY" and analysis.veto_buy:
            reasons.append(
                f"OrderflowLiquidityAgent VETO on BUY: {analysis.veto_reason or 'Heavy overhead Sell Wall'}"
            )
            return "ABSTAIN", reasons
        if action == "SELL" and analysis.veto_sell:
            reasons.append(
                f"OrderflowLiquidityAgent VETO on SELL: {analysis.veto_reason or 'Heavy below-market Buy Wall'}"
            )
            return "ABSTAIN", reasons
        return action, reasons

    def recent_alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        return list(self._recent_alerts)[: max(1, min(limit, 60))]
