r"""New Era Trading Bot — Enterprise AI Quant Desktop Platform (PyQt6 + TradingView + MT5).

Connects to the local FastAPI orchestrator (`http://127.0.0.1:8787`) and WebSocket
stream (`ws://127.0.0.1:8787/ws/logs`) with:
1. Direct MetaTrader 5 live account telemetry & dynamic broker symbol discovery (`/mt5/status`, `/mt5/symbol/{symbol}`).
2. Embedded Interactive TradingView Charting Engine (`QWebEngineView`) with full indicators, drawing tools, and timeframe controls.
3. Quantitative Calculators Suite (Position Sizing Lot Calculator, Kelly Criterion & R:R, ATR Volatility & Cost Gate, and 4-Layer Confluence Visualizer).
4. Dynamic Multi-Asset Scanning (`/scan/manual` & `/scan/automatic`) pulling live OHLCV candles directly from MT5.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from newera_api.gui import BotStrategyLabPanel, Phase2WorkspaceTabs

try:
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover
    QWebEngineSettings = None  # type: ignore[assignment,misc]
    QWebEngineView = None  # type: ignore[assignment,misc]

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]


DEFAULT_HOST = os.getenv("NEWERA_API_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("NEWERA_API_PORT", "8787"))
DEFAULT_API_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_WS_URL = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}/ws/logs"
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Trading Sessions & Killzones Calculator (UTC)
# ---------------------------------------------------------------------------
def compute_trading_sessions_utc(
    now_utc: datetime | None = None,
    display_offset_hours: int = 0,
) -> dict[str, Any]:
    """Compute session state in UTC and display the clock in a selected offset.

    Session activity is always calculated from UTC so daylight-saving changes do
    not alter the trading rules.  ``display_offset_hours`` only changes the
    labels shown to the operator (Georgia is UTC+4 by default in the desktop
    UI).
    """
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    display_dt = dt + timedelta(hours=int(display_offset_hours))
    hour_float = dt.hour + (dt.minute / 60.0)
    is_weekend = dt.weekday() >= 5

    offset = int(display_offset_hours)
    offset_label = "UTC" if offset == 0 else f"UTC{offset:+d}"

    def display_window(start_hour: int, end_hour: int) -> str:
        start = (start_hour + offset) % 24
        end = (end_hour + offset) % 24
        return f"{start:02d}:00–{end:02d}:00 {offset_label}"

    # Standard Institutional FX/Gold UTC Windows:
    # - Asian (Tokyo/Sydney): 00:00 - 09:00 UTC (Killzone: 00:00 - 04:00 UTC)
    # - London (European Open): 07:00 - 16:00 UTC (Killzone: 07:00 - 10:00 UTC)
    # - New York (US Open): 12:00 - 21:00 UTC (Killzone: 12:00 - 15:00 UTC)
    asia_active = (0.0 <= hour_float < 9.0) and not is_weekend
    asia_kz = (0.0 <= hour_float < 4.0) and not is_weekend

    london_active = (7.0 <= hour_float < 16.0) and not is_weekend
    london_kz = (7.0 <= hour_float < 10.0) and not is_weekend

    ny_active = (12.0 <= hour_float < 21.0) and not is_weekend
    ny_kz = (12.0 <= hour_float < 15.0) and not is_weekend

    ldn_ny_overlap = london_active and ny_active

    active_names: list[str] = []
    if asia_active:
        active_names.append("ASIA" + (" [KILLZONE]" if asia_kz else ""))
    if london_active:
        active_names.append("LONDON" + (" [KILLZONE]" if london_kz else ""))
    if ny_active:
        active_names.append("NEW YORK" + (" [KILLZONE]" if ny_kz else ""))

    if is_weekend:
        headline = "MARKET CLOSED (WEEKEND) — CRYPTO 24/7 ACTIVE"
    elif ldn_ny_overlap:
        headline = "🔥 LONDON × NEW YORK OVERLAP (PEAK INSTITUTIONAL LIQUIDITY)"
    elif active_names:
        headline = "ACTIVE: " + " + ".join(active_names)
    else:
        headline = "SYDNEY / ROLLOVER QUIET WINDOW (21:00–00:00 UTC)"

    return {
        "utc_time": dt.strftime("%H:%M:%S UTC"),
        "display_time": display_dt.strftime(f"%H:%M:%S {offset_label}"),
        "display_offset_hours": offset,
        "display_timezone": offset_label,
        "is_weekend": is_weekend,
        "headline": headline,
        "overlap": ldn_ny_overlap,
        "sessions": {
            "asia": {
                "name": "ASIA (TOKYO)",
                "hours": display_window(0, 9),
                "active": asia_active,
                "killzone": asia_kz,
            },
            "london": {
                "name": "LONDON",
                "hours": display_window(7, 16),
                "active": london_active,
                "killzone": london_kz,
            },
            "new_york": {
                "name": "NEW YORK",
                "hours": display_window(12, 21),
                "active": ny_active,
                "killzone": ny_kz,
            },
        },
    }


# ---------------------------------------------------------------------------
# TradingView Symbol & HTML Generator (with TradeLocker Overlay & Sessions)
# ---------------------------------------------------------------------------
TV_SYMBOL_OVERRIDES: dict[str, str] = {
    "XAUUSD": "OANDA:XAUUSD",
    "GOLD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
    "SILVER": "OANDA:XAGUSD",
    "EURUSD": "FX:EURUSD",
    "GBPUSD": "FX:GBPUSD",
    "USDJPY": "FX:USDJPY",
    "USDCHF": "FX:USDCHF",
    "USDCAD": "FX:USDCAD",
    "AUDUSD": "FX:AUDUSD",
    "NZDUSD": "FX:NZDUSD",
    "EURGBP": "FX:EURGBP",
    "EURJPY": "FX:EURJPY",
    "GBPJPY": "FX:GBPJPY",
    "BTCUSD": "COINBASE:BTCUSD",
    "BTCUSDT": "BINANCE:BTCUSDT",
    "ETHUSD": "COINBASE:ETHUSD",
    "ETHUSDT": "BINANCE:ETHUSDT",
    "SOLUSD": "COINBASE:SOLUSD",
    "BNBUSD": "BINANCE:BNBUSDT",
    "XRPUSD": "COINBASE:XRPUSD",
    "DOGEUSD": "COINBASE:DOGEUSD",
    "ADAUSD": "COINBASE:ADAUSD",
    "US500": "OANDA:SPX500USD",
    "SPX500": "OANDA:SPX500USD",
    "USTEC": "OANDA:NAS100USD",
    "NAS100": "OANDA:NAS100USD",
    "US30": "OANDA:US30USD",
    "DE40": "OANDA:DE30EUR",
    "WTI": "TVC:USOIL",
    "USOIL": "TVC:USOIL",
    "BRENT": "TVC:UKOIL",
}


def map_mt5_to_tradingview_symbol(mt5_symbol: str) -> str:
    """Convert a broker MT5 symbol (e.g. `XAUUSD`, `EURUSD.a`, `BTCUSDm`) to a TradingView symbol."""
    raw = mt5_symbol.strip().upper()
    if raw in TV_SYMBOL_OVERRIDES:
        return TV_SYMBOL_OVERRIDES[raw]
    # Strip common broker suffixes (.a, .r, .pro, .ecn, .raw, m, +, #)
    cleaned = re.sub(r"([._-][A-Z0-9]+|[+#]|M|PRO|RAW|ECN)$", "", raw)
    if cleaned in TV_SYMBOL_OVERRIDES:
        return TV_SYMBOL_OVERRIDES[cleaned]
    if len(cleaned) == 6 and cleaned.isalpha():
        return f"FX:{cleaned}"
    return cleaned or "OANDA:XAUUSD"


def build_tradingview_html(tv_symbol: str, interval: str = "5", theme: str = "dark") -> str:
    """Generate clean, fast, standard interactive TradingView Advanced Chart HTML.

    No custom DOM overlays or internal chart banners are injected so timeframe
    and symbol switching remains instant and never hangs.
    """
    safe_symbol = json.dumps(tv_symbol)
    safe_interval = json.dumps(interval)
    tv_theme = "light" if theme.lower() == "light" else "dark"
    safe_theme = json.dumps(tv_theme)
    bg_color = "#f8fafc" if tv_theme == "light" else "#090d14"
    toolbar_bg = "#e2e8f0" if tv_theme == "light" else "#0f1623"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<style>
    html, body {{
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
        background-color: {bg_color};
        overflow: hidden;
        font-family: "Consolas", "Segoe UI", monospace;
    }}
    #tv_chart_container {{
        width: 100%;
        height: 100%;
    }}
</style>
</head>
<body>
<div id="tv_chart_container"></div>
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script type="text/javascript">
    window.addEventListener("contextmenu", function(e) {{
        // Allow TradingView / Chromium native context menu
    }}, false);

    new TradingView.widget({{
        "autosize": true,
        "symbol": {safe_symbol},
        "interval": {safe_interval},
        "timezone": "Etc/UTC",
        "theme": {safe_theme},
        "style": "1",
        "locale": "en",
        "toolbar_bg": "{toolbar_bg}",
        "enable_publishing": false,
        "hide_top_toolbar": false,
        "hide_side_toolbar": false,
        "allow_symbol_change": true,
        "save_image": true,
        "withdateranges": true,
        "details": true,
        "hotlist": false,
        "calendar": false,
        "studies": [
            "MAExp@tv-basicstudies",
            "RSI@tv-basicstudies"
        ],
        "container_id": "tv_chart_container"
    }});
</script>
</body>
</html>
"""



# ---------------------------------------------------------------------------
# Bloomberg / TradingView Dark Theme Stylesheet
# ---------------------------------------------------------------------------
BLOOMBERG_DARK_QSS = """
QMainWindow, QWidget {
    background-color: #090d14;
    color: #e2e8f0;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}

QMenuBar {
    background-color: #070a0f;
    color: #cbd5e1;
    border-bottom: 1px solid #1e293b;
    padding: 2px 4px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    font-weight: bold;
}

QMenuBar::item {
    background-color: transparent;
    padding: 5px 10px;
    margin: 1px;
}

QMenuBar::item:selected, QMenu::item:selected {
    background-color: #1e3a5f;
    color: #f8fafc;
}

QMenu {
    background-color: #0f1623;
    color: #e2e8f0;
    border: 1px solid #2b3b55;
    padding: 4px;
}

QMenu::item {
    padding: 5px 22px 5px 10px;
}

QDockWidget {
    color: #cbd5e1;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    font-weight: bold;
}

QDockWidget::title {
    background-color: #111827;
    color: #38bdf8;
    border: 1px solid #1e293b;
    padding: 5px 8px;
    text-align: left;
}

QLabel#Phase3WorkspaceHint, QLabel#Phase3StrategyStatus {
    color: #94a3b8;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
}

QFrame#HeaderPanel, QFrame#ControlPanel, QFrame#CardPanel {
    background-color: #0f1623;
    border: 1px solid #1e293b;
    border-radius: 6px;
}

QFrame#SubCard {
    background-color: #131c2e;
    border: 1px solid #233047;
    border-radius: 5px;
}

QLabel#TerminalTitle {
    color: #f8fafc;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 16px;
    font-weight: bold;
    letter-spacing: 0.8px;
}

QLabel#TerminalSubtitle {
    color: #64748b;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 10px;
}

QLabel#SectionHeader {
    color: #94a3b8;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}

QLabel#MetricTitle {
    color: #64748b;
    font-size: 10px;
    font-weight: bold;
    text-transform: uppercase;
}

QLabel#MetricValue {
    color: #f1f5f9;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 13px;
    font-weight: bold;
}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #141d2f;
    color: #f8fafc;
    border: 1px solid #2b3b55;
    border-radius: 4px;
    padding: 4px 8px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 12px;
    selection-background-color: #0284c7;
}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #38bdf8;
    background-color: #172238;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QPushButton {
    background-color: #1e293b;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 5px 12px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    font-weight: bold;
}

QPushButton:hover {
    background-color: #28374f;
    border-color: #475569;
}

QPushButton:pressed {
    background-color: #0f172a;
}

QPushButton:disabled {
    background-color: #0f1623;
    color: #475569;
    border-color: #1e293b;
}

QPushButton#PrimaryScanButton {
    background-color: #0284c7;
    color: #ffffff;
    border: 1px solid #38bdf8;
    padding: 6px 16px;
    font-size: 12px;
}

QPushButton#PrimaryScanButton:hover {
    background-color: #0369a1;
    border-color: #7dd3fc;
}

QPushButton#PrimaryScanButton:disabled {
    background-color: #0c2a40;
    color: #475569;
    border-color: #163b56;
}

QPushButton#AutoScanButton {
    background-color: #172133;
    color: #94a3b8;
    border: 1px solid #2b3b55;
}

QPushButton#AutoScanButton:checked {
    background-color: #064e3b;
    color: #34d399;
    border: 1px solid #10b981;
}

QPushButton#ChipButton, QPushButton#TfButton {
    background-color: #131c2e;
    color: #94a3b8;
    border: 1px solid #233047;
    border-radius: 3px;
    padding: 3px 8px;
    font-size: 11px;
}

QPushButton#ChipButton:hover, QPushButton#TfButton:hover {
    background-color: #1e293b;
    color: #38bdf8;
    border-color: #38bdf8;
}

QPushButton#TfButton:checked {
    background-color: #0284c7;
    color: #ffffff;
    border-color: #38bdf8;
}

QCheckBox {
    color: #cbd5e1;
    spacing: 5px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
}

QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid #334155;
    background-color: #141d2f;
}

QCheckBox::indicator:checked {
    background-color: #0284c7;
    border-color: #38bdf8;
}

QTabWidget::pane {
    border: 1px solid #1e293b;
    background-color: #0b101b;
    border-radius: 4px;
}

QTabBar::tab {
    background-color: #111827;
    color: #94a3b8;
    border: 1px solid #1e293b;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 12px;
    margin-right: 2px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    font-weight: bold;
}

QTabBar::tab:selected {
    background-color: #0b101b;
    color: #38bdf8;
    border-top: 2px solid #38bdf8;
}

QTabBar::tab:hover:!selected {
    background-color: #192338;
    color: #e2e8f0;
}

QTextEdit {
    background-color: #070a10;
    color: #d1d5db;
    border: 1px solid #192234;
    border-radius: 4px;
    font-family: "Consolas", "Cascadia Mono", "Courier New", monospace;
    font-size: 11px;
    padding: 6px;
}

QProgressBar {
    background-color: #141d2f;
    border: 1px solid #26354d;
    border-radius: 4px;
    text-align: center;
    color: #f8fafc;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-weight: bold;
    font-size: 10px;
    height: 16px;
}

QProgressBar::chunk {
    background-color: #0284c7;
    border-radius: 3px;
}

QTableWidget {
    background-color: #080c14;
    alternate-background-color: #0c121e;
    color: #e2e8f0;
    border: 1px solid #1e293b;
    border-radius: 4px;
    gridline-color: #162032;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
    selection-background-color: #1e3a5f;
    selection-color: #ffffff;
}

QHeaderView::section {
    background-color: #111827;
    color: #94a3b8;
    border: none;
    border-right: 1px solid #1e293b;
    border-bottom: 1px solid #1e293b;
    padding: 4px 6px;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 10px;
    font-weight: bold;
    text-transform: uppercase;
}

QScrollArea {
    border: none;
    background-color: transparent;
}

QSplitter::handle {
    background-color: #1e293b;
}

QSplitter::handle:horizontal {
    width: 3px;
}

QSplitter::handle:vertical {
    height: 3px;
}

QStatusBar {
    background-color: #070a0f;
    color: #64748b;
    border-top: 1px solid #1e293b;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 11px;
}

QPushButton#ExecuteDemoButton {
    background-color: #059669;
    color: #ffffff;
    border: 1px solid #10b981;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: bold;
}

QPushButton#ExecuteDemoButton:hover {
    background-color: #047857;
    border-color: #34d399;
}

QPushButton#ExecuteDemoButton:disabled {
    background-color: #062e22;
    color: #475569;
    border-color: #1e293b;
}

QPushButton#AutoExecDemoButton {
    background-color: #172133;
    color: #94a3b8;
    border: 1px solid #2b3b55;
    padding: 5px 10px;
}

QPushButton#AutoExecDemoButton:checked {
    background-color: #3b0764;
    color: #e9d5ff;
    border: 1px solid #a855f7;
}

QPushButton#CloseDemoButton {
    background-color: #450a0a;
    color: #fca5a5;
    border: 1px solid #ef4444;
    padding: 5px 10px;
}

QPushButton#CloseDemoButton:hover {
    background-color: #7f1d1d;
    color: #ffffff;
}
"""

INSTITUTIONAL_LIGHT_QSS = (
    BLOOMBERG_DARK_QSS.replace("#e2e8f0", "#0f172a")
    .replace("#f8fafc", "#0f172a")
    .replace("#f1f5f9", "#1e293b")
    .replace("#cbd5e1", "#334155")
    .replace("#d1d5db", "#1e293b")
    .replace("#1e293b", "#cbd5e1")
    .replace("#090d14", "#f1f5f9")
    .replace("#0f1623", "#ffffff")
    .replace("#131c2e", "#f8fafc")
    .replace("#0b101b", "#ffffff")
    .replace("#070a10", "#f8fafc")
    .replace("#080c14", "#ffffff")
    .replace("#0c121e", "#f1f5f9")
    .replace("#111827", "#e2e8f0")
    .replace("#141d2f", "#f1f5f9")
)

CYBER_MATRIX_QSS = (
    BLOOMBERG_DARK_QSS.replace("#090d14", "#04080e")
    .replace("#0f1623", "#07101c")
    .replace("#131c2e", "#0a1728")
    .replace("#0284c7", "#0d9488")
    .replace("#38bdf8", "#2dd4bf")
)


class ThemeManager:
    """Centralized Theme & Localization (i18n) Manager for the New Era Desktop Platform."""

    THEMES: dict[str, dict[str, str]] = {
        "Bloomberg Dark": {"qss": BLOOMBERG_DARK_QSS, "tv_theme": "dark"},
        "Institutional Light": {"qss": INSTITUTIONAL_LIGHT_QSS, "tv_theme": "light"},
        "Cyber Matrix": {"qss": CYBER_MATRIX_QSS, "tv_theme": "dark"},
    }

    TRANSLATIONS: dict[str, dict[str, str]] = {
        "EN": {
            "scan_btn": "▶ SCAN SYMBOL (MT5 → AI)",
            "exec_demo_btn": "⚡ EXECUTE DEMO TRADE",
            "auto_exec_off": "🤖 AUTO-EXEC DEMO: OFF",
            "auto_exec_on": "🤖 AUTO-EXEC DEMO: ON",
            "close_demo_btn": "✖ CLOSE DEMO",
        },
        "KA": {
            "scan_btn": "▶ სიმბოლოს სკანირება (MT5 → AI)",
            "exec_demo_btn": "⚡ დემო ტრეიდის გახსნა",
            "auto_exec_off": "🤖 ავტო-დემო: გამორთულია",
            "auto_exec_on": "🤖 ავტო-დემო: ჩართულია",
            "close_demo_btn": "✖ პოზიციის დახურვა",
        },
    }

    @classmethod
    def get_qss(cls, theme_name: str) -> str:
        entry = cls.THEMES.get(theme_name) or cls.THEMES["Bloomberg Dark"]
        return entry["qss"]

    @classmethod
    def get_tv_theme(cls, theme_name: str) -> str:
        entry = cls.THEMES.get(theme_name) or cls.THEMES["Bloomberg Dark"]
        return entry["tv_theme"]

    @classmethod
    def load_custom_qss(cls, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return BLOOMBERG_DARK_QSS

    @classmethod
    def tr(cls, key: str, lang: str = "EN") -> str:
        lang_map = cls.TRANSLATIONS.get(lang.upper()) or cls.TRANSLATIONS["EN"]
        return lang_map.get(key, key)


# ---------------------------------------------------------------------------
# Background QThread Workers (HTTP & WebSocket)
# ---------------------------------------------------------------------------
class HttpTaskThread(QThread):
    """Non-blocking HTTP worker thread."""

    task_succeeded = pyqtSignal(str, object)
    task_failed = pyqtSignal(str, str)

    def __init__(
        self,
        task_name: str,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 12.0,
        include_local_kronos_snapshot: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_name = task_name
        self.method = method.upper()
        self.url = url
        self.payload = payload
        self.timeout = timeout
        self.include_local_kronos_snapshot = include_local_kronos_snapshot

    def run(self) -> None:
        try:
            data = self._execute_http()
            if self.include_local_kronos_snapshot and isinstance(data, dict):
                if not isinstance(data.get("kronos_report"), dict):
                    data["_kronos_report_detail"] = self._read_kronos_report_snapshot()
                else:
                    data["_kronos_report_detail"] = data["kronos_report"]
                data["_market_price_range"] = self._read_latest_market_range()
            self.task_succeeded.emit(self.task_name, data)
        except Exception as exc:
            self.task_failed.emit(self.task_name, str(exc))

    def _execute_http(self) -> Any:
        headers = {"Accept": "application/json"}
        body_bytes: bytes | None = None
        if self.payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(self.payload).encode("utf-8")

        if requests is not None:
            response = requests.request(
                self.method,
                self.url,
                headers=headers,
                data=body_bytes,
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                detail = response.text
                try:
                    err_json = response.json()
                    if isinstance(err_json, dict) and "detail" in err_json:
                        detail = str(err_json["detail"])
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {response.status_code}: {detail}")
            return response.json()

        req = urllib.request.Request(self.url, data=body_bytes, headers=headers, method=self.method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(raw_err)
                if isinstance(err_json, dict) and "detail" in err_json:
                    raw_err = str(err_json["detail"])
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {raw_err}") from exc

    @staticmethod
    def _read_kronos_report_snapshot() -> dict[str, Any] | None:
        report_path = WORKSPACE_ROOT / "TradingAgentsData" / "research" / "kronos_matrix_latest.json"
        try:
            if report_path.exists():
                parsed = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            pass
        return None

    @staticmethod
    def _read_latest_market_range() -> dict[str, float | str] | None:
        csv_path = WORKSPACE_ROOT / "TradingAgentsData" / "market" / "xauusd_m5.csv"
        try:
            if not csv_path.exists():
                return None
            with csv_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192), os.SEEK_SET)
                tail_lines = handle.read().decode("utf-8", errors="replace").splitlines()
            rows: list[tuple[str, float, float, float]] = []
            for line in tail_lines[-25:]:
                parts = [item.strip() for item in line.split(",")]
                if len(parts) < 5:
                    continue
                try:
                    rows.append((parts[0], float(parts[2]), float(parts[3]), float(parts[4])))
                except ValueError:
                    continue
            if not rows:
                return None
            window = rows[-20:]
            last_ts, last_high, last_low, last_close = window[-1]
            return {
                "timestamp": last_ts,
                "close": last_close,
                "bar_low": last_low,
                "bar_high": last_high,
                "range_20_low": min(item[2] for item in window),
                "range_20_high": max(item[1] for item in window),
            }
        except Exception:
            return None


class WebSocketStreamThread(QThread):
    """Dedicated QThread running an asyncio loop for `ws://127.0.0.1:8787/ws/logs`."""

    event_received = pyqtSignal(dict)
    ws_status_changed = pyqtSignal(bool, str)
    reconnected = pyqtSignal()

    def __init__(self, ws_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ws_url = ws_url
        self._running = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        task = self._task
        if loop is not None and loop.is_running() and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def run(self) -> None:
        if websockets is None:
            self.ws_status_changed.emit(False, "WS MISSING")
            return

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._consume_forever())
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.ws_status_changed.emit(False, f"WS ERROR: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for p_task in pending:
                    p_task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._loop = None
            self._task = None

    async def _consume_forever(self) -> None:
        backoff = 1.5
        while self._running:
            try:
                self.ws_status_changed.emit(False, "WS CONNECTING...")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                ) as ws:
                    backoff = 1.5
                    self.ws_status_changed.emit(True, "WS LIVE STREAM")
                    self.reconnected.emit()
                    async for raw_message in ws:
                        if not self._running:
                            break
                        try:
                            payload = json.loads(raw_message)
                            if isinstance(payload, dict):
                                self.event_received.emit(payload)
                        except json.JSONDecodeError:
                            self.event_received.emit(
                                {
                                    "type": "process.log",
                                    "timestamp": time.time(),
                                    "payload": {"message": str(raw_message)},
                                }
                            )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                self.ws_status_changed.emit(False, f"WS RECONNECTING ({type(exc).__name__})")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(backoff * 1.6, 10.0)


# ---------------------------------------------------------------------------
# Main Enterprise AI Quant Desktop Platform Window
# ---------------------------------------------------------------------------
class NewEraDesktopTerminal(QMainWindow):
    """Enterprise AI Quant Trading Platform with Direct MT5, TradingView Chart & Quant Calculators."""

    def __init__(
        self,
        *,
        api_url: str = DEFAULT_API_URL,
        ws_url: str = DEFAULT_WS_URL,
    ) -> None:
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.ws_url = ws_url
        self._active_threads: set[QThread] = set()
        self._memory_records: list[dict[str, Any]] = []
        self._recent_scans: list[dict[str, Any]] = []
        self._open_positions: list[dict[str, Any]] = []
        self._event_count = 0
        self._active_scan_id: str | None = None
        self._current_tv_interval = "5"
        self._current_tv_symbol = "XAUUSD"
        self._current_theme_name = "Bloomberg Dark"
        self._updating_symbol_combo = False
        self._syncing_phase2_symbol = False
        self._ui_initialized = False
        self._l2_alerts_seen: set[str] = set()
        self._l2_alert_count = 0
        self._latest_l2_snapshot: dict[str, Any] = {}
        self._latest_mt5_account: dict[str, Any] = {}
        self._latest_gate_action: str = "ABSTAIN"
        self._latest_ai_direction: str = "BUY"
        self._computed_lot_size: float = 0.01
        self._computed_long_sl: float = 0.0
        self._computed_long_tp: float = 0.0
        self._computed_short_sl: float = 0.0
        self._computed_short_tp: float = 0.0
        self._latest_symbol_spec: dict[str, Any] = {
            "symbol": "XAUUSD",
            "point": 0.01,
            "digits": 2,
            "bid": 4165.0,
            "ask": 4165.4,
            "spread_points": 40.0,
            "atr_m5": 2.17,
            "trade_contract_size": 100.0,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.01,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        self._workspace_settings = QSettings("NewEra", "NewEraTradingBot")
        self.workspace_settings = self._workspace_settings
        self._workspace_layout_version = 3
        self._ws_connected = False
        self._ws_status_text = "WS CONNECTING"
        self._ws_ping_ms: float | None = None
        self._last_ws_event_monotonic: float | None = None
        self._last_ws_ping_sample_monotonic: float | None = None
        self._active_bot_count = 0

        self.setWindowTitle(
            "New Era Trading Bot — Enterprise AI Quant Platform [8-Pair Auto-Screener • MT5 L2 DOM • TradingView • Kronos • Gemini CIO]"
        )
        self.resize(1680, 980)
        self.setMinimumSize(1280, 780)
        self.setStyleSheet(ThemeManager.get_qss(self._current_theme_name))

        self._build_ui()
        self._setup_timers_and_streams()
        self.refresh_all_state()
        self._ui_initialized = True

    # -----------------------------------------------------------------------
    # UI Construction
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 8, 10, 6)
        root_layout.setSpacing(6)

        # 1. Header Panel (System Status + Hero Gate Banner + Live MT5 Account Telemetry + Live Floating PnL)
        root_layout.addWidget(self._build_header_panel())

        # 2. Control & Symbol Bar (Dynamic MT5 Symbols + One-Click/Auto Demo Execution + Scan Actions)
        root_layout.addWidget(self._build_control_panel())

        # 3. Phase 2 surfaces remain the source of truth, but Phase 3 hosts
        # each surface in a native, independently movable QDockWidget.
        self.phase2_tabs = Phase2WorkspaceTabs(
            execution_widget=self._build_execution_workspace_content(),
            parent=self,
        )
        self.main_workspace_tabs = self.phase2_tabs
        self.workspace_tabs = self.phase2_tabs
        self.main_tabs = self.phase2_tabs
        self.phase2_live_tab = self.phase2_tabs.live_execution_tab
        self.phase2_controls_tab = self.phase2_tabs.quant_gate_tab
        self.phase2_fleet_tab = self.phase2_tabs.bot_fleet_tab
        self.phase2_audit_tab = self.phase2_tabs.decision_audit_tab
        self.controls_tab = self.phase2_controls_tab
        self.bot_fleet_tab = self.phase2_fleet_tab
        self.audit_tab = self.phase2_audit_tab
        self.phase2_live_tab.symbol_changed.connect(self._on_phase2_symbol_changed)
        self.phase2_live_tab.refresh_requested.connect(self._on_phase2_live_refresh_requested)
        self.phase2_controls_tab.apply_requested.connect(self._apply_phase2_controls)
        self.phase2_controls_tab.refresh_requested.connect(self._request_controls_state)
        self.phase2_fleet_tab.action_requested.connect(self._handle_bot_fleet_action)
        self.phase2_audit_tab.refresh_requested.connect(self._request_decision_history)
        self.ticker_combo.currentTextChanged.connect(self._sync_phase2_symbol_selector)
        self._sync_phase2_symbol_selector(self.ticker_combo.currentText())

        phase2_widgets = self.phase2_tabs.detach_tabs_for_docks()
        self._build_phase3_docks(phase2_widgets)

        workspace_canvas = QFrame(central)
        workspace_canvas.setObjectName("CardPanel")
        canvas_layout = QVBoxLayout(workspace_canvas)
        canvas_layout.setContentsMargins(18, 18, 18, 18)
        canvas_layout.setSpacing(8)
        canvas_title = QLabel("NATIVE QUANT WORKSPACE")
        canvas_title.setObjectName("SectionHeader")
        canvas_copy = QLabel(
            "Phase 3 panels are dockable, floatable, movable, and closable. "
            "Use View to restore a hidden panel or Bot Lab to open Strategy Lab."
        )
        canvas_copy.setWordWrap(True)
        canvas_copy.setObjectName("Phase3WorkspaceHint")
        canvas_layout.addWidget(canvas_title)
        canvas_layout.addWidget(canvas_copy)
        canvas_layout.addStretch(1)
        root_layout.addWidget(workspace_canvas, stretch=1)

        self._build_status_bar()
        self._build_phase3_menus()
        self._restore_workspace_layout()

    def _build_status_bar(self) -> None:
        status_bar = QStatusBar(self)
        status_bar.setObjectName("Phase3StatusBar")
        self.setStatusBar(status_bar)
        self.status_message_label = QLabel("Initializing New Era Enterprise AI Quant Platform...")
        self.status_safety_label = QLabel(
            "MT5 DEMO EXECUTION: ACTIVE (DEMO ONLY)  |  8-PAIR AUTO-SCREENER: ON  |  SPREAD GATE: <= 180 PTS"
        )
        self.status_safety_label.setStyleSheet("color: #f59e0b; font-weight: bold; padding-right: 8px;")

        self.status_api_label = QLabel("API: OFFLINE")
        self.status_api_label.setObjectName("StatusApiLabel")
        self.status_ws_ping_label = QLabel("WS PING: --")
        self.status_ws_ping_label.setObjectName("StatusWsPingLabel")
        self.status_active_bot_label = QLabel("ACTIVE BOTS: 0")
        self.status_active_bot_label.setObjectName("StatusActiveBotLabel")
        self.status_mt5_health_label = QLabel("MT5 HEALTH: CONNECTING")
        self.status_mt5_health_label.setObjectName("StatusMt5HealthLabel")

        # Reader-friendly aliases keep status widgets discoverable to Phase 3
        # integrations without removing any Phase 2 header badges.
        self.api_status_status_widget = self.status_api_label
        self.ws_ping_status_widget = self.status_ws_ping_label
        self.active_bot_status_widget = self.status_active_bot_label
        self.mt5_health_status_widget = self.status_mt5_health_label
        self.api_status_label = self.status_api_label
        self.ws_ping_latency_label = self.status_ws_ping_label
        self.active_bot_count_label = self.status_active_bot_label
        self.mt5_health_label = self.status_mt5_health_label
        self.ws_ping_label = self.status_ws_ping_label

        for label in (
            self.status_api_label,
            self.status_ws_ping_label,
            self.status_active_bot_label,
            self.status_mt5_health_label,
        ):
            label.setStyleSheet(
                "background-color: #131c2e; color: #94a3b8; border: 1px solid #233047; "
                "border-radius: 3px; padding: 2px 6px; font-family: 'Consolas', monospace; "
                "font-size: 10px; font-weight: bold;"
            )

        status_bar.addWidget(self.status_message_label, stretch=1)
        status_bar.addPermanentWidget(self.status_api_label)
        status_bar.addPermanentWidget(self.status_ws_ping_label)
        status_bar.addPermanentWidget(self.status_active_bot_label)
        status_bar.addPermanentWidget(self.status_mt5_health_label)
        status_bar.addPermanentWidget(self.status_safety_label)

    def _create_phase3_dock(
        self,
        key: str,
        title: str,
        widget: QWidget,
        area: Qt.DockWidgetArea,
    ) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(key)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        dock.setMinimumSize(280, 180)
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        return dock

    def _build_phase3_docks(self, phase2_widgets: tuple[QWidget, ...]) -> None:
        if len(phase2_widgets) != 4:
            raise RuntimeError("Phase 2 workspace did not provide four dockable surfaces")

        self.strategy_lab_panel = BotStrategyLabPanel(self)
        self.bot_strategy_lab_panel = self.strategy_lab_panel
        self.strategy_lab_widget = self.strategy_lab_panel
        self.strategy_backtester_panel = self.strategy_lab_panel
        self.strategy_lab_panel.tool_requested.connect(self._on_strategy_lab_tool_requested)
        self.strategy_lab_panel.strategy_save_requested.connect(self._request_strategy_save)
        self.strategy_lab_panel.strategy_backtest_requested.connect(self._request_strategy_backtest)
        self.strategy_lab_panel.strategy_walk_forward_requested.connect(self._request_strategy_walk_forward)
        self.strategy_lab_panel.strategy_load_requested.connect(self._request_strategy_load)

        self.live_chart_dock = self._create_phase3_dock(
            "LiveChartExecutionDock",
            "Live Chart / Execution",
            phase2_widgets[0],
            Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.decision_audit_dock = self._create_phase3_dock(
            "DecisionAuditDock",
            "Decision Audit",
            phase2_widgets[3],
            Qt.DockWidgetArea.BottomDockWidgetArea,
        )
        self.quant_controls_dock = self._create_phase3_dock(
            "QuantControlsDock",
            "Quant Controls",
            phase2_widgets[1],
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.l2_orderbook_panel = QTextEdit(self)
        self.l2_orderbook_panel.setObjectName("Phase3L2OrderbookPanel")
        self.l2_orderbook_panel.setReadOnly(True)
        self.l2_orderbook_panel.setPlaceholderText(
            "L2 Orderbook telemetry will appear here when the MT5/DOM stream is available."
        )
        self.l2_orderbook_dock = self._create_phase3_dock(
            "L2OrderbookDock",
            "L2 Orderbook",
            self.l2_orderbook_panel,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.bot_fleet_dock = self._create_phase3_dock(
            "BotFleetDock",
            "Bot Fleet",
            phase2_widgets[2],
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.strategy_lab_dock = self._create_phase3_dock(
            "BotStrategyLabDock",
            "Bot Strategy Lab / Backtester",
            self.strategy_lab_panel,
            Qt.DockWidgetArea.BottomDockWidgetArea,
        )

        self.phase3_docks: dict[str, QDockWidget] = {
            "live_chart_execution": self.live_chart_dock,
            "decision_audit": self.decision_audit_dock,
            "quant_controls": self.quant_controls_dock,
            "l2_orderbook": self.l2_orderbook_dock,
            "bot_fleet": self.bot_fleet_dock,
            "strategy_lab": self.strategy_lab_dock,
        }
        self.dock_widgets = self.phase3_docks
        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
        )

        # Keep the main chart/execution surface prominent on first launch. A
        # saved QSettings state overrides these defaults below.
        if not self._workspace_settings.contains("ui/state"):
            self.resizeDocks(
                [self.live_chart_dock, self.quant_controls_dock, self.bot_fleet_dock],
                [860, 360, 300],
                Qt.Orientation.Horizontal,
            )
            self.resizeDocks(
                [self.decision_audit_dock, self.strategy_lab_dock],
                [280, 300],
                Qt.Orientation.Vertical,
            )

    def _build_phase3_menus(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.setObjectName("Phase3MenuBar")
        menu_bar.setNativeMenuBar(False)

        self.file_menu = menu_bar.addMenu("File")
        self.view_menu = menu_bar.addMenu("View")
        self.insert_menu = menu_bar.addMenu("Insert")
        self.strategy_tools_menu = menu_bar.addMenu("Strategy Tools")
        self.bot_lab_menu = menu_bar.addMenu("Bot Lab")
        self.help_menu = menu_bar.addMenu("Help")

        self.export_audit_json_action = QAction("Export Audit Telemetry JSON", self)
        self.export_audit_json_action.triggered.connect(lambda: self.export_audit_telemetry_json())
        self.export_audit_csv_action = QAction("Export Audit Telemetry CSV", self)
        self.export_audit_csv_action.triggered.connect(lambda: self.export_audit_telemetry_csv())
        self.save_workspace_layout_action = QAction("Save Workspace Layout", self)
        self.save_workspace_layout_action.triggered.connect(lambda: self.save_workspace_layout())
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(lambda: self.close())
        self.file_menu.addAction(self.export_audit_json_action)
        self.file_menu.addAction(self.export_audit_csv_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.save_workspace_layout_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)

        self.view_actions: dict[str, QAction] = {}
        for key, dock in self.phase3_docks.items():
            action = dock.toggleViewAction()
            action.setText(dock.windowTitle())
            self.view_actions[key] = action
            self.view_menu.addAction(action)
        self.view_menu.addSeparator()
        self.show_all_panels_action = QAction("Show All Panels", self)
        self.show_all_panels_action.triggered.connect(lambda: self.show_all_phase3_panels())
        self.hide_all_panels_action = QAction("Hide All Panels", self)
        self.hide_all_panels_action.triggered.connect(lambda: self.hide_all_phase3_panels())
        self.restore_workspace_layout_action = QAction("Restore Workspace Layout", self)
        self.restore_workspace_layout_action.triggered.connect(lambda: self.restore_workspace_layout())
        self.view_menu.addAction(self.show_all_panels_action)
        self.view_menu.addAction(self.hide_all_panels_action)
        self.view_menu.addAction(self.restore_workspace_layout_action)

        self.insert_actions: dict[str, QAction] = {}
        for label in ("Indicator: RSI", "Indicator: EMA", "Overlay: VWAP", "Custom Signal Widget"):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, item=label: self._insert_phase3_placeholder(item))
            self.insert_menu.addAction(action)
            self.insert_actions[label] = action
        self.insert_menu.addSeparator()
        self.indicators_menu = self.insert_menu.addMenu("Indicators")
        for label in ("RSI", "EMA", "ATR"):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, item=f"Indicator: {label}": self._insert_phase3_placeholder(item))
            self.indicators_menu.addAction(action)
        self.overlays_menu = self.insert_menu.addMenu("Overlays")
        for label in ("VWAP", "Session Bands", "Liquidity Levels"):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, item=f"Overlay: {label}": self._insert_phase3_placeholder(item))
            self.overlays_menu.addAction(action)
        self.custom_signal_widgets_menu = self.insert_menu.addMenu("Custom Signal Widgets")
        self.custom_signal_widget_action = QAction("New Signal Widget", self)
        self.custom_signal_widget_action.triggered.connect(
            lambda: self._insert_phase3_placeholder("Custom Signal Widget")
        )
        self.custom_signal_widgets_menu.addAction(self.custom_signal_widget_action)

        self.strategy_backtester_action = QAction("Strategy Backtester", self)
        self.strategy_backtester_action.triggered.connect(lambda: self.show_strategy_backtester())
        self.manual_risk_override_action = QAction("Manual Risk Override", self)
        self.manual_risk_override_action.triggered.connect(lambda: self.show_manual_risk_override())
        self.github_quant_module_search_action = QAction("GitHub Quant Module Search", self)
        self.github_quant_module_search_action.triggered.connect(lambda: self.show_github_quant_module_search())
        self.strategy_tools_menu.addAction(self.strategy_backtester_action)
        self.strategy_tools_menu.addAction(self.manual_risk_override_action)
        self.strategy_tools_menu.addAction(self.github_quant_module_search_action)
        self.tools_menu = self.strategy_tools_menu.addMenu("Tools")
        self.tools_menu.addAction(self.strategy_backtester_action)
        self.tools_menu.addAction(self.manual_risk_override_action)
        self.tools_menu.addAction(self.github_quant_module_search_action)

        self.open_strategy_lab_action = QAction("Open Strategy Lab", self)
        self.open_strategy_lab_action.triggered.connect(lambda: self.show_strategy_lab())
        self.bot_lab_menu.addAction(self.open_strategy_lab_action)

        self.about_action = QAction("About New Era Trading Bot", self)
        self.about_action.triggered.connect(
            lambda: self.status_message_label.setText(
                "New Era Trading Bot Phase 3 native workspace — safe research and audit tooling."
            )
        )
        self.phase3_help_action = QAction("Phase 3 UI Guide", self)
        self.phase3_help_action.triggered.connect(
            lambda: self.status_message_label.setText(
                "Use View to toggle docks, Bot Lab for Strategy Lab, and File to export audit telemetry."
            )
        )
        self.help_menu.addAction(self.about_action)
        self.help_menu.addAction(self.phase3_help_action)

    def _on_strategy_lab_tool_requested(self, tool: str) -> None:
        self.status_message_label.setText(f"Strategy Lab: {tool.replace('.', ' ').replace('_', ' ')}")
        if tool == "strategy.list":
            self._spawn_http_task("strategies_list", "GET", "/strategies", timeout=12.0)
        if tool == "risk.override":
            controls = self.phase2_controls_tab.values()
            controls["risk_pct"] = round(float(self.strategy_lab_panel.risk_override_spin.value()), 1)
            self._apply_phase2_controls(controls)

    def _insert_phase3_placeholder(self, label: str) -> None:
        self.strategy_lab_panel.insert_placeholder(label)
        self._show_dock(self.strategy_lab_dock)

    def _show_dock(self, dock: QDockWidget) -> None:
        dock.show()
        dock.raise_()

    def show_strategy_lab(self) -> None:
        self._show_dock(self.strategy_lab_dock)
        self.strategy_lab_panel.open_strategy_lab()

    def show_strategy_backtester(self) -> None:
        self._show_dock(self.strategy_lab_dock)
        self.strategy_lab_panel.open_backtester()

    def show_manual_risk_override(self) -> None:
        self._show_dock(self.strategy_lab_dock)
        self.strategy_lab_panel.open_manual_risk_override()

    def show_github_quant_module_search(self) -> None:
        self._show_dock(self.strategy_lab_dock)
        self.strategy_lab_panel.open_github_module_search()

    @staticmethod
    def _strategy_data_path(symbol: str) -> str:
        """Map a visible symbol to an existing workspace-local OHLCV CSV."""

        normalized = symbol.strip().upper()
        aliases = {
            "GOLD": "xauusd",
            "XAUUSD": "xauusd",
            "EURUSD": "eurusd",
            "GBPUSD": "gbpusd",
            "USDJPY": "usdjpy",
            "USDCAD": "usdcad",
            "US500": "us500",
            "USTEC": "ustec",
            "NAS100": "ustec",
            "BTCUSD": "btcusd",
            "ETHUSD": "ethusd",
            "VIX": "vix_x6",
        }
        stem = aliases.get(normalized, re.sub(r"[^A-Z0-9]+", "", normalized).lower() or "xauusd")
        candidate = Path("TradingAgentsData") / "market" / f"{stem}_m5.csv"
        if not (WORKSPACE_ROOT / candidate).is_file():
            candidate = Path("TradingAgentsData") / "market" / "xauusd_m5.csv"
        return candidate.as_posix()

    def _request_strategy_save(self, payload: dict[str, Any]) -> None:
        self._spawn_http_task("strategy_save", "POST", "/strategies", payload=dict(payload), timeout=15.0)

    def _request_strategy_load(self, payload: dict[str, Any]) -> None:
        self._spawn_http_task("strategy_load", "POST", "/strategies/load", payload=dict(payload), timeout=15.0)

    def _request_strategy_backtest(self, payload: dict[str, Any]) -> None:
        request = dict(payload)
        symbol = str(request.pop("symbol", "XAUUSD")).strip().upper() or "XAUUSD"
        request.pop("timeframe", None)
        request.setdefault("data_path", self._strategy_data_path(symbol))
        self._spawn_http_task("strategy_backtest", "POST", "/strategies/backtest", payload=request, timeout=60.0)

    def _request_strategy_walk_forward(self, payload: dict[str, Any]) -> None:
        request = dict(payload)
        symbol = str(request.pop("symbol", "XAUUSD")).strip().upper() or "XAUUSD"
        request.pop("timeframe", None)
        request.setdefault("data_path", self._strategy_data_path(symbol))
        self._spawn_http_task("strategy_walk_forward", "POST", "/strategies/walk-forward", payload=request, timeout=60.0)

    def show_all_phase3_panels(self) -> None:
        for dock in self.phase3_docks.values():
            dock.show()
        self.status_message_label.setText("All Phase 3 workspace panels shown.")

    def hide_all_phase3_panels(self) -> None:
        for dock in self.phase3_docks.values():
            dock.hide()
        self.status_message_label.setText("All Phase 3 workspace panels hidden; use View to restore one.")

    def save_workspace_layout(self) -> None:
        self._workspace_settings.setValue("ui/geometry", self.saveGeometry())
        self._workspace_settings.setValue(
            "ui/state",
            self.saveState(self._workspace_layout_version),
        )
        self._workspace_settings.setValue("ui/theme", self._current_theme_name)
        self._workspace_settings.sync()
        if hasattr(self, "status_message_label"):
            self.status_message_label.setText("Workspace layout saved to QSettings.")

    def restore_workspace_layout(self) -> bool:
        restored = False
        geometry = self._workspace_settings.value("ui/geometry")
        if geometry is not None:
            restored = bool(self.restoreGeometry(geometry)) or restored
        state = self._workspace_settings.value("ui/state")
        if state is not None:
            restored = bool(self.restoreState(state, self._workspace_layout_version)) or restored
        if hasattr(self, "status_message_label"):
            self.status_message_label.setText(
                "Workspace layout restored from QSettings." if restored else "No saved workspace layout found."
            )
        return restored

    def _restore_workspace_layout(self) -> None:
        # Do not change the current theme during construction; the theme combo
        # remains the user-controlled source of truth. Layout restoration is
        # safe even when QSettings contains data from an older build.
        self.restore_workspace_layout()

    def _audit_telemetry_payload(self) -> dict[str, Any]:
        records = self.phase2_audit_tab.records if hasattr(self, "phase2_audit_tab") else []
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "event_count": int(self._event_count),
            "ws_status": self._ws_status_text,
            "ws_ping_ms": self._ws_ping_ms,
            "active_bot_count": int(self._active_bot_count),
            "mt5_connected": bool(self._latest_mt5_account),
            "controls": self.phase2_controls_tab.values() if hasattr(self, "phase2_controls_tab") else {},
            "summary": dict(getattr(self.phase2_audit_tab, "_summary", {}) or {}),
            "records": records,
        }

    @staticmethod
    def _resolve_export_path(
        parent: QWidget,
        path: str | Path | None,
        title: str,
        file_filter: str,
        suffix: str,
    ) -> Path | None:
        if path is None:
            selected, _ = QFileDialog.getSaveFileName(parent, title, f"audit_telemetry{suffix}", file_filter)
            if not selected:
                return None
            output = Path(selected)
        else:
            output = Path(str(path))
        if output.suffix.lower() != suffix.lower():
            output = output.with_suffix(suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        return output

    def export_audit_telemetry_json(self, path: str | Path | None = None) -> Path | None:
        output = self._resolve_export_path(
            self,
            path,
            "Export Audit Telemetry JSON",
            "JSON files (*.json)",
            ".json",
        )
        if output is None:
            return None
        output.write_text(json.dumps(self._audit_telemetry_payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        self.status_message_label.setText(f"Audit telemetry exported to {output.name}.")
        return output

    def export_audit_telemetry_csv(self, path: str | Path | None = None) -> Path | None:
        output = self._resolve_export_path(
            self,
            path,
            "Export Audit Telemetry CSV",
            "CSV files (*.csv)",
            ".csv",
        )
        if output is None:
            return None
        records = self._audit_telemetry_payload()["records"]
        fieldnames = [
            "record_id",
            "timestamp",
            "symbol",
            "signal",
            "action",
            "final_decision",
            "execution_mode",
            "reason",
            "block_reason",
        ]
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                if isinstance(record, dict):
                    writer.writerow({name: record.get(name, "") for name in fieldnames})
        self.status_message_label.setText(f"Audit telemetry exported to {output.name}.")
        return output

    def _build_execution_workspace_content(self) -> QSplitter:
        """Build the unchanged chart/MT5 command surface for Phase 2 Tab 1."""

        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_splitter.addWidget(self._build_tradingview_chart_panel())
        main_splitter.addWidget(self._build_right_command_tabs())
        main_splitter.setStretchFactor(0, 56)
        main_splitter.setStretchFactor(1, 44)
        main_splitter.setSizes([930, 720])
        return main_splitter

    def _build_header_panel(self) -> QFrame:
        header = QFrame(self)
        header.setObjectName("HeaderPanel")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        # Left Column: Brand + Connection Pills + Theme Selector
        left_col = QVBoxLayout()
        left_col.setSpacing(4)

        brand_row = QHBoxLayout()
        title_lbl = QLabel("NEW ERA // AI QUANT PLATFORM")
        title_lbl.setObjectName("TerminalTitle")
        brand_row.addWidget(title_lbl)
        brand_row.addStretch(1)

        theme_lbl = QLabel("THEME:")
        theme_lbl.setObjectName("MetricTitle")
        self.theme_combo = QComboBox(header)
        self.theme_combo.addItems(list(ThemeManager.THEMES.keys()))
        self.theme_combo.setCurrentText(self._current_theme_name)
        self.theme_combo.setMinimumWidth(135)
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        brand_row.addWidget(theme_lbl)
        brand_row.addWidget(self.theme_combo)
        left_col.addLayout(brand_row)

        subtitle_lbl = QLabel(
            "8-PAIR AUTONOMOUS SCREENER  •  MT5 L2 DOM & CRYPTO WS  •  GEMINI CIO (ჯოი)  •  KRONOS"
        )
        subtitle_lbl.setObjectName("TerminalSubtitle")
        left_col.addWidget(subtitle_lbl)

        badges_row = QHBoxLayout()
        badges_row.setSpacing(6)
        self.api_status_badge = QLabel("● API OFFLINE")
        self._apply_pill_style(self.api_status_badge, "offline")
        self.mt5_status_badge = QLabel("● MT5 CONNECTING...")
        self._apply_pill_style(self.mt5_status_badge, "warning")
        self.ws_status_badge = QLabel("● WS CONNECTING")
        self._apply_pill_style(self.ws_status_badge, "warning")
        self.gemini_status_badge = QLabel("● GEMINI .ENV")
        self._apply_pill_style(self.gemini_status_badge, "info")
        self.memory_status_badge = QLabel("MEMORY: --")
        self._apply_pill_style(self.memory_status_badge, "info")

        badges_row.addWidget(self.api_status_badge)
        badges_row.addWidget(self.mt5_status_badge)
        badges_row.addWidget(self.ws_status_badge)
        badges_row.addWidget(self.gemini_status_badge)
        badges_row.addWidget(self.memory_status_badge)

        self.btn_header_auto_trade = QPushButton("🤖 AUTO-TRADE: OFF", header)
        self.btn_header_auto_trade.setObjectName("HeaderAutoTradeButton")
        self.btn_header_auto_trade.setCheckable(True)
        self.btn_header_auto_trade.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_header_auto_trade.setToolTip(
            "100% Autonomous Trading (MT5 Demo only):\n"
            "Every signal (Win-Prob >= 68%) is reviewed by live Gemini API before opening.\n"
            "Click to toggle ON / OFF."
        )
        self.btn_header_auto_trade.toggled.connect(self.on_toggle_auto_execute_demo)
        self._style_header_auto_trade_btn(False)
        badges_row.addWidget(self.btn_header_auto_trade)

        badges_row.addStretch(1)
        left_col.addLayout(badges_row)
        layout.addLayout(left_col, stretch=34)

        # Center Column: Hero AI Gate Verdict
        gate_frame = QFrame(header)
        gate_frame.setObjectName("SubCard")
        gate_layout = QVBoxLayout(gate_frame)
        gate_layout.setContentsMargins(14, 6, 14, 6)
        gate_layout.setSpacing(2)

        gate_caption = QLabel("MULTI-LAYER AI ALIGNMENT VERDICT (WATERFALL + L2 VETO)")
        gate_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gate_caption.setObjectName("MetricTitle")

        self.gate_hero_label = QLabel("GATE: ABSTAIN (0.0%)")
        self.gate_hero_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gate_hero_label.setFont(QFont("Consolas", 18, QFont.Weight.Bold))

        self.gate_sub_label = QLabel("Waiting for scan... Select any symbol to stream L2 DOM & scan")
        self.gate_sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gate_sub_label.setStyleSheet(
            "color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 11px;"
        )

        gate_layout.addWidget(gate_caption)
        gate_layout.addWidget(self.gate_hero_label)
        gate_layout.addWidget(self.gate_sub_label)
        self._style_gate_hero("ABSTAIN", 0.0)
        layout.addWidget(gate_frame, stretch=32)

        # Right Column: Live MetaTrader 5 Account Telemetry Card + Real-Time Floating PnL
        mt5_frame = QFrame(header)
        mt5_frame.setObjectName("SubCard")
        mt5_grid = QGridLayout(mt5_frame)
        mt5_grid.setContentsMargins(12, 5, 12, 5)
        mt5_grid.setHorizontalSpacing(10)
        mt5_grid.setVerticalSpacing(2)

        lbl_broker = QLabel("MT5 BROKER:")
        lbl_broker.setObjectName("MetricTitle")
        self.mt5_broker_value = QLabel("Connecting to MT5...")
        self.mt5_broker_value.setObjectName("MetricValue")

        lbl_acc = QLabel("ACCOUNT ID:")
        lbl_acc.setObjectName("MetricTitle")
        self.mt5_account_value = QLabel("--")
        self.mt5_account_value.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )

        lbl_bal = QLabel("BALANCE / EQUITY:")
        lbl_bal.setObjectName("MetricTitle")
        self.mt5_balance_value = QLabel("$0.00  |  Eq: $0.00")
        self.mt5_balance_value.setStyleSheet(
            "color: #10b981; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )

        lbl_live_pnl = QLabel("LIVE FLOATING PnL:")
        lbl_live_pnl.setObjectName("MetricTitle")
        self.mt5_live_pnl_value = QLabel("Live PnL: +$0.00 (+0.00%)")
        self.mt5_live_pnl_value.setStyleSheet(
            "color: #10b981; background-color: #062e22; border: 1px solid #059669; "
            "border-radius: 4px; padding: 1px 6px; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )

        lbl_margin = QLabel("FREE MARGIN / LEV:")
        lbl_margin.setObjectName("MetricTitle")
        self.mt5_margin_value = QLabel("Free: $0.00  |  1:--")
        self.mt5_margin_value.setStyleSheet(
            "color: #f59e0b; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )

        mt5_grid.addWidget(lbl_broker, 0, 0)
        mt5_grid.addWidget(self.mt5_broker_value, 0, 1)
        mt5_grid.addWidget(lbl_acc, 1, 0)
        mt5_grid.addWidget(self.mt5_account_value, 1, 1)
        mt5_grid.addWidget(lbl_bal, 2, 0)
        mt5_grid.addWidget(self.mt5_balance_value, 2, 1)
        mt5_grid.addWidget(lbl_live_pnl, 3, 0)
        mt5_grid.addWidget(self.mt5_live_pnl_value, 3, 1)
        mt5_grid.addWidget(lbl_margin, 4, 0)
        mt5_grid.addWidget(self.mt5_margin_value, 4, 1)

        # Hidden compatibility labels for existing tests
        self.active_bot_value = QLabel("New Era Gold Council v1")
        self.bot_mode_value = QLabel("DEMO_ONLY [SAFE DEMO]")
        self.scan_engine_value = QLabel("READY / IDLE")

        layout.addWidget(mt5_frame, stretch=34)
        return header

    def _update_header_live_pnl_label(self, pnl_usd: float, pnl_pct: float | None = None) -> None:
        if pnl_pct is None:
            bal = float(self._latest_mt5_account.get("balance") or self.calc_balance_spin.value() or 1000.0)
            pnl_pct = (pnl_usd / bal) * 100.0 if bal > 0 else 0.0
        sign = "+" if pnl_usd >= 0 else "-"
        pct_sign = "+" if pnl_pct >= 0 else "-"
        self.mt5_live_pnl_value.setText(
            f"Live PnL: {sign}${abs(pnl_usd):,.2f} ({pct_sign}{abs(pnl_pct):.2f}%)"
        )
        if pnl_usd >= 0:
            self.mt5_live_pnl_value.setStyleSheet(
                "color: #10b981; background-color: #062e22; border: 1px solid #059669; "
                "border-radius: 4px; padding: 1px 6px; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
            )
        else:
            self.mt5_live_pnl_value.setStyleSheet(
                "color: #ef4444; background-color: #311016; border: 1px solid #dc2626; "
                "border-radius: 4px; padding: 1px 6px; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
            )

    def _style_header_auto_trade_btn(self, enabled: bool) -> None:
        if not hasattr(self, "btn_header_auto_trade"):
            return
        if enabled:
            self.btn_header_auto_trade.setText("🤖 AUTO-TRADE: ACTIVE (DEMO)")
            self.btn_header_auto_trade.setStyleSheet(
                "QPushButton#HeaderAutoTradeButton {"
                "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #059669);"
                "  color: #ffffff; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
                "  padding: 3px 10px; border-radius: 4px; border: 1px solid #a78bfa;"
                "}"
                "QPushButton#HeaderAutoTradeButton:hover {"
                "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8b5cf6, stop:1 #10b981);"
                "  border: 1px solid #c4b5fd;"
                "}"
            )
        else:
            self.btn_header_auto_trade.setText("🤖 AUTO-TRADE: OFF")
            self.btn_header_auto_trade.setStyleSheet(
                "QPushButton#HeaderAutoTradeButton {"
                "  background-color: #1e1e2d; color: #94a3b8;"
                "  font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
                "  padding: 3px 10px; border-radius: 4px; border: 1px solid #334155;"
                "}"
                "QPushButton#HeaderAutoTradeButton:hover {"
                "  background-color: #27273a; color: #cbd5e1; border: 1px solid #475569;"
                "}"
            )

    def _build_control_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("ControlPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        # Dynamic Broker & Crypto Symbol Selector
        lbl_ticker = QLabel("PAIR:")
        lbl_ticker.setObjectName("SectionHeader")
        self.ticker_combo = QComboBox(panel)
        self.ticker_combo.setEditable(True)
        self.ticker_combo.addItems(
            [
                "XAUUSD",
                "EURUSD",
                "GBPUSD",
                "USDJPY",
                "BTCUSD",
                "ETHUSD",
                "SOLUSD",
                "USDCHF",
                "USDCAD",
                "AUDUSD",
                "NZDUSD",
                "US500",
                "USTEC",
            ]
        )
        self.ticker_combo.setCurrentText("XAUUSD")
        self.ticker_combo.setMinimumWidth(110)
        self.ticker_combo.currentTextChanged.connect(self._on_symbol_changed)

        # Live Quote & Spread Badge
        self.live_quote_badge = QLabel("BID: --  |  ASK: --  |  ATR: --")
        self._apply_pill_style(self.live_quote_badge, "info")

        # Spread Points Input (auto-synced from MT5, editable)
        self.spread_enabled_check = QCheckBox("SPREAD:", panel)
        self.spread_enabled_check.setChecked(True)
        self.spread_spin = QDoubleSpinBox(panel)
        self.spread_spin.setRange(0.0, 10000.0)
        self.spread_spin.setDecimals(1)
        self.spread_spin.setSingleStep(1.0)
        self.spread_spin.setValue(25.0)
        self.spread_spin.setSuffix(" pts")
        self.spread_spin.setMinimumWidth(78)
        self.spread_enabled_check.toggled.connect(self.spread_spin.setEnabled)

        # Gate TTL
        lbl_ttl = QLabel("TTL:")
        lbl_ttl.setObjectName("SectionHeader")
        self.ttl_spin = QSpinBox(panel)
        self.ttl_spin.setRange(1, 1440)
        self.ttl_spin.setValue(90)
        self.ttl_spin.setSuffix("m")
        self.ttl_spin.setMinimumWidth(56)

        # Layer Checkboxes
        self.chk_tradingagents = QCheckBox("TradingAgents", panel)
        self.chk_tradingagents.setChecked(True)
        self.chk_kronos = QCheckBox("Kronos+Matrix", panel)
        self.chk_kronos.setChecked(True)
        self.chk_scan_on_switch = QCheckBox("Auto-Scan Pair", panel)
        self.chk_scan_on_switch.setChecked(True)
        self.chk_scan_on_switch.setToolTip(
            "Automatically start L2 DOM stream and trigger M5/M15 AI scan when switching pairs"
        )

        # Auto-Scan Interval
        self.auto_interval_spin = QSpinBox(panel)
        self.auto_interval_spin.setRange(1, 240)
        self.auto_interval_spin.setValue(15)
        self.auto_interval_spin.setSuffix("m")
        self.auto_interval_spin.setMinimumWidth(54)

        # Scan Action Buttons
        self.btn_manual_scan = QPushButton("▶ SCAN SYMBOL (MT5 → AI)", panel)
        self.btn_manual_scan.setObjectName("PrimaryScanButton")
        self.btn_manual_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual_scan.clicked.connect(self.trigger_manual_scan)

        self.btn_run_screener = QPushButton("🔎 SCAN TOP 8 PAIRS", panel)
        self.btn_run_screener.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run_screener.setToolTip(
            "Run 4-Step Waterfall Screener across Top 8 pairs (XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, ETHUSD, US500, USTEC)"
        )
        self.btn_run_screener.clicked.connect(self.trigger_multi_pair_screener)

        self.btn_auto_scan = QPushButton("⟳ AUTO-SCAN: OFF", panel)
        self.btn_auto_scan.setObjectName("AutoScanButton")
        self.btn_auto_scan.setCheckable(True)
        self.btn_auto_scan.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_scan.toggled.connect(self.on_toggle_auto_scan)

        # One-Click & Auto MT5 Demo Execution Controls
        self.demo_side_combo = QComboBox(panel)
        self.demo_side_combo.addItems(["AI GATE", "BUY (LONG)", "SELL (SHORT)"])
        self.demo_side_combo.setMinimumWidth(98)
        self.demo_side_combo.setToolTip("Select order direction for Demo Execution (AI GATE uses AI signal)")

        self.btn_execute_demo = QPushButton("⚡ EXECUTE DEMO TRADE", panel)
        self.btn_execute_demo.setObjectName("ExecuteDemoButton")
        self.btn_execute_demo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_execute_demo.setToolTip(
            "Send exact Calculator Lot Size, ATR SL & ATR TP to MT5 Demo Account"
        )
        self.btn_execute_demo.clicked.connect(self.trigger_execute_demo_trade)

        self.btn_auto_execute_demo = QPushButton("🤖 AUTO-EXEC DEMO: OFF", panel)
        self.btn_auto_execute_demo.setObjectName("AutoExecDemoButton")
        self.btn_auto_execute_demo.setCheckable(True)
        self.btn_auto_execute_demo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_execute_demo.setToolTip(
            "100% Autonomous Mode: automatically open Demo Trades when Waterfall Win-Prob >= 68%"
        )
        self.btn_auto_execute_demo.toggled.connect(self.on_toggle_auto_execute_demo)

        self.btn_close_demo = QPushButton("✖ CLOSE DEMO", panel)
        self.btn_close_demo.setObjectName("CloseDemoButton")
        self.btn_close_demo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close_demo.setToolTip("Close open Demo position(s) for active symbol")
        self.btn_close_demo.clicked.connect(self.trigger_close_demo_positions)

        self.btn_refresh = QPushButton("↻ SYNC", panel)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh_all_state)

        layout.addWidget(lbl_ticker)
        layout.addWidget(self.ticker_combo)
        layout.addWidget(self.live_quote_badge)
        layout.addWidget(self.spread_enabled_check)
        layout.addWidget(self.spread_spin)
        layout.addWidget(lbl_ttl)
        layout.addWidget(self.ttl_spin)
        layout.addWidget(self.chk_tradingagents)
        layout.addWidget(self.chk_kronos)
        layout.addWidget(self.chk_scan_on_switch)
        layout.addStretch(1)
        layout.addWidget(self.auto_interval_spin)
        layout.addWidget(self.btn_manual_scan)
        layout.addWidget(self.btn_run_screener)
        layout.addWidget(self.btn_auto_scan)
        layout.addWidget(self.demo_side_combo)
        layout.addWidget(self.btn_execute_demo)
        layout.addWidget(self.btn_auto_execute_demo)
        layout.addWidget(self.btn_close_demo)
        layout.addWidget(self.btn_refresh)
        return panel

    # -----------------------------------------------------------------------
    # Left Workspace: Clean TradingView Chart + Quick Buy/Sell + Positions & History Panel
    # -----------------------------------------------------------------------
    def _build_tradingview_chart_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("CardPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)

        # 1. Chart Header Bar with Direct Quick Buy / Sell Market Buttons & Timeframe Switcher
        top_bar = QHBoxLayout()
        top_bar.setSpacing(6)
        self.chart_title_label = QLabel("TRADINGVIEW INTERACTIVE CHART  //  OANDA:XAUUSD (M5)")
        self.chart_title_label.setObjectName("SectionHeader")
        top_bar.addWidget(self.chart_title_label)
        top_bar.addStretch(1)

        # Direct Quick Chart Trading Buttons ([🟢 BUY MARKET] / [🔴 SELL MARKET])
        self.btn_quick_buy_market = QPushButton("🟢 BUY MARKET", panel)
        self.btn_quick_buy_market.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quick_buy_market.setToolTip(
            "One-Click BUY Market on MT5 Demo using Calculator Lot Size, ATR SL & ATR TP"
        )
        self.btn_quick_buy_market.setStyleSheet(
            "background-color: #059669; color: #ffffff; border: 1px solid #10b981; "
            "border-radius: 4px; padding: 4px 12px; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.btn_quick_buy_market.clicked.connect(lambda: self.trigger_execute_demo_trade(forced_side="BUY"))

        self.btn_quick_sell_market = QPushButton("🔴 SELL MARKET", panel)
        self.btn_quick_sell_market.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quick_sell_market.setToolTip(
            "One-Click SELL Market on MT5 Demo using Calculator Lot Size, ATR SL & ATR TP"
        )
        self.btn_quick_sell_market.setStyleSheet(
            "background-color: #dc2626; color: #ffffff; border: 1px solid #ef4444; "
            "border-radius: 4px; padding: 4px 12px; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.btn_quick_sell_market.clicked.connect(lambda: self.trigger_execute_demo_trade(forced_side="SELL"))

        top_bar.addWidget(self.btn_quick_buy_market)
        top_bar.addWidget(self.btn_quick_sell_market)

        tf_label = QLabel("TIMEFRAME:")
        tf_label.setObjectName("MetricTitle")
        top_bar.addWidget(tf_label)

        self._tf_buttons: dict[str, QPushButton] = {}
        for tf_name, tv_code in (("M1", "1"), ("M5", "5"), ("M15", "15"), ("H1", "60"), ("H4", "240"), ("D1", "D")):
            btn = QPushButton(tf_name, panel)
            btn.setObjectName("TfButton")
            btn.setCheckable(True)
            btn.setChecked(tv_code == "5")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, code=tv_code: self._set_tradingview_timeframe(code))
            self._tf_buttons[tv_code] = btn
            top_bar.addWidget(btn)

        layout.addLayout(top_bar)

        # 2. Trading Sessions Indicator Bar (Asian / London / New York Killzones)
        self.session_banner_frame = QFrame(panel)
        self.session_banner_frame.setObjectName("SubCard")
        sess_layout = QHBoxLayout(self.session_banner_frame)
        sess_layout.setContentsMargins(8, 4, 8, 4)
        sess_layout.setSpacing(8)

        self.session_timezone_label = QLabel("SESSIONS (UTC+4):")
        self.session_timezone_label.setObjectName("MetricTitle")
        self.session_timezone_combo = QComboBox(panel)
        self.session_timezone_combo.setObjectName("ChipButton")
        for label, offset in (
            ("UTC+4 (Georgia)", 4),
            ("UTC", 0),
            ("UTC+1", 1),
            ("UTC+2", 2),
            ("UTC+3", 3),
            ("UTC+5", 5),
            ("UTC-5", -5),
            ("UTC-8", -8),
        ):
            self.session_timezone_combo.addItem(label, offset)
        self.session_timezone_combo.setCurrentIndex(0)
        self.session_timezone_combo.currentIndexChanged.connect(
            lambda _index: self._update_session_indicator_ui()
        )
        self.session_asia_badge = QLabel("🌏 ASIA 00:00–09:00")
        self.session_london_badge = QLabel("🇬🇧 LONDON 07:00–16:00")
        self.session_ny_badge = QLabel("🗽 NEW YORK 12:00–21:00")
        self.session_headline_label = QLabel("Syncing UTC Session Clock...")
        self.session_headline_label.setStyleSheet(
            "color: #fbbf24; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )

        sess_layout.addWidget(self.session_timezone_label)
        sess_layout.addWidget(self.session_timezone_combo)
        sess_layout.addWidget(self.session_asia_badge)
        sess_layout.addWidget(self.session_london_badge)
        sess_layout.addWidget(self.session_ny_badge)
        sess_layout.addStretch(1)
        sess_layout.addWidget(self.session_headline_label)
        layout.addWidget(self.session_banner_frame)
        self._update_session_indicator_ui()

        # 3. Vertical Splitter: Clean TradingView Chart (Top) + Live Positions & Account History Tabs (Bottom)
        chart_splitter = QSplitter(Qt.Orientation.Vertical, panel)

        chart_container = QWidget(chart_splitter)
        chart_vbox = QVBoxLayout(chart_container)
        chart_vbox.setContentsMargins(0, 0, 0, 0)
        chart_vbox.setSpacing(0)

        if QWebEngineView is not None and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self.web_chart: QWebEngineView | None = QWebEngineView(chart_container)
            self.web_chart.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
            if QWebEngineSettings is not None:
                web_settings = self.web_chart.settings()
                web_settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
                web_settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
                web_settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
                web_settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
                web_settings.setAttribute(QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, True)
            self.web_chart.setStyleSheet("background-color: #090d14;")
            chart_vbox.addWidget(self.web_chart, stretch=1)
            self._reload_tradingview_chart("XAUUSD", "5")
        else:
            self.web_chart = None
            fallback = QLabel(
                "Clean Interactive TradingView Chart Engine Ready\n"
                "(Active in GUI mode via PyQt6-WebEngine; offscreen mode detected)"
            )
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fallback.setStyleSheet("color: #64748b; font-family: 'Consolas', monospace; font-size: 13px;")
            chart_vbox.addWidget(fallback, stretch=1)

        chart_splitter.addWidget(chart_container)
        chart_splitter.addWidget(self._build_bottom_positions_and_history_tabs())
        chart_splitter.setStretchFactor(0, 66)
        chart_splitter.setStretchFactor(1, 34)
        chart_splitter.setSizes([540, 270])
        layout.addWidget(chart_splitter, stretch=1)

        return panel

    def _build_bottom_positions_and_history_tabs(self) -> QTabWidget:
        self.bottom_positions_tabs = QTabWidget(self)

        # --- TAB 1: 📊 ACTIVE OPEN POSITIONS ---
        open_tab = QWidget()
        open_layout = QVBoxLayout(open_tab)
        open_layout.setContentsMargins(6, 6, 6, 6)
        open_layout.setSpacing(4)

        open_hdr = QHBoxLayout()
        open_hdr.setSpacing(8)
        self.open_pos_summary_label = QLabel("ACTIVE OPEN POSITIONS: 0  |  LIVE FLOATING PnL: +$0.00 (+0.00%)")
        self.open_pos_summary_label.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        btn_refresh_pos = QPushButton("↻ Refresh Positions", open_tab)
        btn_refresh_pos.setObjectName("ChipButton")
        btn_refresh_pos.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh_pos.clicked.connect(lambda: self._request_open_positions())

        btn_close_all_pos = QPushButton("✖ CLOSE ALL OPEN", open_tab)
        btn_close_all_pos.setObjectName("CloseDemoButton")
        btn_close_all_pos.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close_all_pos.clicked.connect(self.trigger_close_all_demo_positions)

        open_hdr.addWidget(self.open_pos_summary_label)
        open_hdr.addStretch(1)
        open_hdr.addWidget(btn_refresh_pos)
        open_hdr.addWidget(btn_close_all_pos)
        open_layout.addLayout(open_hdr)

        self.open_positions_table = QTableWidget(0, 10, open_tab)
        self.open_positions_table.setAlternatingRowColors(True)
        self.open_positions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.open_positions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.open_positions_table.verticalHeader().setVisible(False)
        self.open_positions_table.setHorizontalHeaderLabels(
            [
                "Ticket ID",
                "Symbol",
                "Type",
                "Volume (Lot)",
                "Open Price",
                "Current Price",
                "SL",
                "TP",
                "Live PnL ($ / %)",
                "Close",
            ]
        )
        open_h = self.open_positions_table.horizontalHeader()
        open_h.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        open_h.setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)
        open_layout.addWidget(self.open_positions_table, stretch=1)

        # --- TAB 2: 📜 TRADE HISTORY & STATS ---
        hist_tab = QWidget()
        hist_layout = QVBoxLayout(hist_tab)
        hist_layout.setContentsMargins(6, 6, 6, 6)
        hist_layout.setSpacing(4)

        stats_bar = QFrame(hist_tab)
        stats_bar.setObjectName("SubCard")
        stats_row = QHBoxLayout(stats_bar)
        stats_row.setContentsMargins(8, 4, 8, 4)
        stats_row.setSpacing(12)

        self.hist_total_pnl_label = QLabel("Total Realized PnL: +$0.00")
        self.hist_total_pnl_label.setStyleSheet(
            "color: #10b981; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )
        self.hist_win_rate_label = QLabel("Win Rate: 0.0% (0W / 0L)")
        self.hist_win_rate_label.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.hist_profit_factor_label = QLabel("Profit Factor: 0.00")
        self.hist_profit_factor_label.setStyleSheet(
            "color: #f59e0b; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.hist_total_trades_label = QLabel("Total Trades: 0")
        self.hist_total_trades_label.setStyleSheet(
            "color: #e2e8f0; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.btn_refresh_history = QPushButton("↻ Refresh History", stats_bar)
        self.btn_refresh_history.setObjectName("ChipButton")
        self.btn_refresh_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_history.clicked.connect(self._request_trade_history)

        stats_row.addWidget(self.hist_total_pnl_label)
        stats_row.addWidget(self.hist_win_rate_label)
        stats_row.addWidget(self.hist_profit_factor_label)
        stats_row.addWidget(self.hist_total_trades_label)
        stats_row.addStretch(1)
        stats_row.addWidget(self.btn_refresh_history)
        hist_layout.addWidget(stats_bar)

        self.trade_history_table = QTableWidget(0, 7, hist_tab)
        self.trade_history_table.setAlternatingRowColors(True)
        self.trade_history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.trade_history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.trade_history_table.verticalHeader().setVisible(False)
        self.trade_history_table.setHorizontalHeaderLabels(
            [
                "Ticket ID",
                "Symbol",
                "Open / Close Time",
                "Buy / Sell",
                "Volume",
                "Entry → Exit Price",
                "Realized Profit/Loss ($)",
            ]
        )
        hist_h = self.trade_history_table.horizontalHeader()
        hist_h.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hist_layout.addWidget(self.trade_history_table, stretch=1)

        self.bottom_positions_tabs.addTab(open_tab, "📊 ACTIVE OPEN POSITIONS (0)")
        self.bottom_positions_tabs.addTab(hist_tab, "📜 TRADE HISTORY & STATS")
        return self.bottom_positions_tabs

    def apply_theme(self, theme_name: str) -> None:
        if theme_name not in ThemeManager.THEMES:
            return
        self._current_theme_name = theme_name
        self.setStyleSheet(ThemeManager.get_qss(theme_name))
        symbol = self.ticker_combo.currentText().strip() or "XAUUSD"
        self._reload_tradingview_chart(symbol, self._current_tv_interval)
        self.status_message_label.setText(f"Switched UI & TradingView theme to '{theme_name}'.")

    def _reload_tradingview_chart(self, mt5_symbol: str, interval: str | None = None) -> None:
        if interval is not None:
            self._current_tv_interval = interval
        tv_symbol = map_mt5_to_tradingview_symbol(mt5_symbol)
        tf_display = {"1": "M1", "5": "M5", "15": "M15", "60": "H1", "240": "H4", "D": "D1"}.get(
            self._current_tv_interval, self._current_tv_interval
        )
        self.chart_title_label.setText(f"TRADINGVIEW INTERACTIVE CHART  //  {tv_symbol} ({tf_display})")
        if self.web_chart is not None:
            tv_theme = ThemeManager.get_tv_theme(self._current_theme_name)
            html = build_tradingview_html(tv_symbol, self._current_tv_interval, theme=tv_theme)
            self.web_chart.setHtml(html, QUrl("https://s3.tradingview.com/"))

    def _set_tradingview_timeframe(self, tv_code: str) -> None:
        for code, btn in self._tf_buttons.items():
            btn.setChecked(code == tv_code)
        symbol = self.ticker_combo.currentText().strip() or "XAUUSD"
        self._reload_tradingview_chart(symbol, tv_code)

    # -----------------------------------------------------------------------
    # Phase 2 Modular Workspace Bridges
    # -----------------------------------------------------------------------
    def _sync_phase2_symbol_selector(self, symbol: str) -> None:
        if not hasattr(self, "phase2_live_tab"):
            return
        normalized = symbol.strip().upper() or "XAUUSD"
        self.phase2_live_tab.set_symbol(normalized)

    def _on_phase2_symbol_changed(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized:
            return
        if self.ticker_combo.currentText().strip().upper() == normalized:
            return
        self._syncing_phase2_symbol = True
        try:
            self.ticker_combo.setCurrentText(normalized)
        finally:
            self._syncing_phase2_symbol = False

    def _on_phase2_live_refresh_requested(self, symbol: str) -> None:
        normalized = symbol.strip().upper() or "XAUUSD"
        self._spawn_http_task("mt5_symbol", "GET", f"/mt5/symbol/{urllib.parse.quote(normalized)}")
        self._request_open_positions(normalized)
        self._request_l2_book(normalized, publish=False)

    def _apply_phase2_controls(self, controls: dict[str, Any]) -> None:
        if hasattr(self, "phase2_controls_tab"):
            self.phase2_controls_tab.set_status("Applying controls to API…")
        self.status_message_label.setText("Applying Quant Gate runtime controls…")
        self._spawn_http_task(
            "controls_update",
            "POST",
            "/controls",
            payload=dict(controls),
            timeout=8.0,
        )

    def _request_controls_state(self) -> None:
        self._spawn_http_task("controls_get", "GET", "/controls", timeout=8.0)

    def _request_decision_history(self, limit: int | None = None) -> None:
        requested = int(limit or self.phase2_audit_tab.history_limit_spin.value())
        requested = max(1, min(500, requested))
        self._spawn_http_task(
            "decision_history",
            "GET",
            f"/decision-history?limit={requested}",
            timeout=10.0,
        )
        self._spawn_http_task("decision_history_summary", "GET", "/decision-history/summary", timeout=10.0)

    def _handle_bot_fleet_action(self, action: str, payload: dict[str, Any]) -> None:
        """Persist a fleet action through the safe bot-registry API."""

        bot_id = str(payload.get("bot_id") or "").strip()
        if action == "add":
            self._spawn_http_task("bots_add", "POST", "/bots", payload=payload, timeout=8.0)
            return
        if not bot_id:
            self.phase2_fleet_tab.set_action_status("Fleet action needs a bot id.", error=True)
            return
        encoded_id = urllib.parse.quote(bot_id, safe="")
        if action == "set_enabled":
            self._spawn_http_task(
                "bot_enabled",
                "POST",
                f"/bots/{encoded_id}/enabled",
                payload={"enabled": bool(payload.get("enabled"))},
                timeout=8.0,
            )
        elif action == "remove":
            self._spawn_http_task("bot_remove", "DELETE", f"/bots/{encoded_id}", timeout=8.0)


    # -----------------------------------------------------------------------
    # Right Workspace: 4-Tab Quant Command Center
    # -----------------------------------------------------------------------
    def _build_right_command_tabs(self) -> QTabWidget:
        self.right_tabs = QTabWidget(self)
        self.right_tabs.addTab(self._build_ai_signal_and_confluence_tab(), "🎯 AI SIGNAL & CONFLUENCE")
        self.right_tabs.addTab(self._build_l2_orderbook_radar_tab(), "🔥 L2 ORDERBOOK & RADAR")
        self.right_tabs.addTab(self._build_gemini_copilot_tab(), "🤖 GEMINI QUANT CO-PILOT (ჯოი)")
        self.right_tabs.addTab(self._build_quant_calculators_tab(), "🧮 QUANT CALCULATORS")
        self.right_tabs.addTab(self._build_live_stream_and_memory_tab(), "📡 LIVE STREAM & MEMORY")
        return self.right_tabs

    # -----------------------------------------------------------------------
    # Tab: 🤖 GEMINI QUANT CO-PILOT ("ჯოი") + Waterfall Pipeline + Telegram
    # -----------------------------------------------------------------------
    def _build_gemini_copilot_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # 1. Waterfall Decision Pipeline & Veto Hierarchy Card
        wf_card = QFrame(container)
        wf_card.setObjectName("SubCard")
        wf_layout = QVBoxLayout(wf_card)
        wf_layout.setContentsMargins(10, 8, 10, 8)
        wf_layout.setSpacing(4)

        wf_hdr = QHBoxLayout()
        wf_title = QLabel("WATERFALL DECISION PIPELINE & VETO HIERARCHY  //  GEMINI CIO ('ჯოი')")
        wf_title.setObjectName("SectionHeader")
        self.wf_prob_badge = QLabel("WIN-PROB: --%")
        self._apply_pill_style(self.wf_prob_badge, "info")
        self.wf_risk_badge = QLabel("RISK TIER: ABSTAIN (<65%)")
        self._apply_pill_style(self.wf_risk_badge, "warning")
        wf_hdr.addWidget(wf_title)
        wf_hdr.addStretch(1)
        wf_hdr.addWidget(self.wf_prob_badge)
        wf_hdr.addWidget(self.wf_risk_badge)
        wf_layout.addLayout(wf_hdr)

        self.wf_step1_label = QLabel("STEP 1 [TA-Lib Filter]: RSI(14)=-- | ATR(14)=-- | EMA(20/50/200) Trend=--")
        self.wf_step1_label.setStyleSheet("color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 11px;")
        self.wf_step2_label = QLabel("STEP 2 [Kronos + Matrix]: Direction=-- | Matrix Score=--/8.0 | 8-NN Analogue=--%")
        self.wf_step2_label.setStyleSheet("color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 11px;")
        self.wf_step3_label = QLabel("STEP 3 [L2 DOM Safety Gate]: Opposing Wall Veto=CLEAR | L2 Wall Support=--")
        self.wf_step3_label.setStyleSheet("color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 11px;")
        self.wf_step4_label = QLabel(
            "STEP 4 [Gemini CIO 'ჯოი']: Verdict=ABSTAIN | Lot=0.00 | BE(+1x ATR)=-- | 50% Partial(+2x ATR)=--"
        )
        self.wf_step4_label.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )

        wf_layout.addWidget(self.wf_step1_label)
        wf_layout.addWidget(self.wf_step2_label)
        wf_layout.addWidget(self.wf_step3_label)
        wf_layout.addWidget(self.wf_step4_label)
        layout.addWidget(wf_card)

        # 2. Action Bar: Analyze Setup + Autonomous Mode + Dynamic Trailing + Telegram Controls
        ctrl_card = QFrame(container)
        ctrl_card.setObjectName("SubCard")
        ctrl_layout = QVBoxLayout(ctrl_card)
        ctrl_layout.setContentsMargins(10, 6, 10, 6)
        ctrl_layout.setSpacing(6)

        row_top = QHBoxLayout()
        row_top.setSpacing(8)
        self.btn_joey_analyze = QPushButton("🔍 Analyze Current Setup (ჯოი)", ctrl_card)
        self.btn_joey_analyze.setObjectName("PrimaryScanButton")
        self.btn_joey_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_joey_analyze.setToolTip(
            "One-Click: reads Live L2 DOM, Kronos+Matrix, TA-Lib & MT5 and outputs detailed Georgian analysis from ჯოი"
        )
        self.btn_joey_analyze.clicked.connect(self.trigger_joey_analyze_setup)

        self.chk_joey_autonomous = QCheckBox("🤖 100% Autonomous CIO Auto-Trade (Demo)", ctrl_card)
        self.chk_joey_autonomous.setChecked(False)
        self.chk_joey_autonomous.toggled.connect(self._on_joey_autonomous_toggled)

        self.chk_dynamic_trailing = QCheckBox("🛡️ Auto Break-Even (+1x ATR) & 50% Partial (+2x ATR)", ctrl_card)
        self.chk_dynamic_trailing.setChecked(True)
        self.chk_dynamic_trailing.toggled.connect(self._sync_autonomous_settings)

        row_top.addWidget(self.btn_joey_analyze)
        row_top.addWidget(self.chk_joey_autonomous)
        row_top.addWidget(self.chk_dynamic_trailing)
        row_top.addStretch(1)
        ctrl_layout.addLayout(row_top)

        row_tg = QHBoxLayout()
        row_tg.setSpacing(6)
        tg_lbl = QLabel("📱 TELEGRAM REMOTE:")
        tg_lbl.setObjectName("MetricTitle")
        self.telegram_status_badge = QLabel("● TELEGRAM: READY")
        self._apply_pill_style(self.telegram_status_badge, "info")
        row_tg.addWidget(tg_lbl)
        row_tg.addWidget(self.telegram_status_badge)

        for tg_cmd in ("/status", "/analyze", "/pause", "/resume", "/close_all"):
            btn_tg = QPushButton(tg_cmd, ctrl_card)
            btn_tg.setObjectName("ChipButton")
            btn_tg.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_tg.clicked.connect(lambda checked=False, c=tg_cmd: self.trigger_telegram_command(c))
            row_tg.addWidget(btn_tg)
        row_tg.addStretch(1)
        ctrl_layout.addLayout(row_tg)
        layout.addWidget(ctrl_card)

        # 3. Interactive Chat Transcript & Prompt Box with "ჯოი"
        chat_card = QFrame(container)
        chat_card.setObjectName("CardPanel")
        chat_layout = QVBoxLayout(chat_card)
        chat_layout.setContentsMargins(10, 8, 10, 8)
        chat_layout.setSpacing(6)

        chat_hdr = QHBoxLayout()
        chat_title = QLabel("🤖 GEMINI QUANT CO-PILOT ('ჯოი')  //  LIVE GEORGIAN QUANTITATIVE CHAT")
        chat_title.setObjectName("SectionHeader")
        btn_clear_chat = QPushButton("Clear Chat", chat_card)
        btn_clear_chat.setObjectName("ChipButton")
        btn_clear_chat.clicked.connect(lambda: self.joey_chat_view.clear())
        chat_hdr.addWidget(chat_title)
        chat_hdr.addStretch(1)
        chat_hdr.addWidget(btn_clear_chat)
        chat_layout.addLayout(chat_hdr)

        self.joey_chat_view = QTextEdit(chat_card)
        self.joey_chat_view.setReadOnly(True)
        self.joey_chat_view.setHtml(
            '<div style="color:#38bdf8; font-weight:bold;">[ჯოი — Gemini Quant CIO]:</div>'
            '<div style="color:#e2e8f0; margin-bottom:8px;">'
            "გამარჯობა ლევან! მე ვარ <b>ჯოი</b> — NewEra Trading Bot-ის კვანტური პორტფელის მენეჯერი. "
            "დააჭირე <b>„🔍 Analyze Current Setup (ჯოი)“</b> ღილაკს ან მომწერე ნებისმიერი შეკითხვა მიმდინარე წყვილზე, "
            "L2 DOM კედლებზე, TA-Lib ინდიკატორებზე და ოპტიმალურ ლოტის ზომაზე!"
            "</div>"
        )
        chat_layout.addWidget(self.joey_chat_view, stretch=1)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        for q_prompt in (
            "რა ხდება ჭიქაში (L2 DOM)?",
            "რატომ მიიღო Waterfall-მა ეს ვერდიქტი?",
            "რა ლოტით და რა SL/TP-ით შევიდე?",
        ):
            q_btn = QPushButton(q_prompt, chat_card)
            q_btn.setObjectName("ChipButton")
            q_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            q_btn.clicked.connect(lambda checked=False, p=q_prompt: self._send_quick_joey_prompt(p))
            quick_row.addWidget(q_btn)
        quick_row.addStretch(1)
        chat_layout.addLayout(quick_row)

        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self.joey_chat_input = QLineEdit(chat_card)
        self.joey_chat_input.setPlaceholderText(
            "ჰკითხე ჯოის ქართულად (მაგ: გააანალიზე XAUUSD-ის L2 კედლები და მითხარი რისკი)..."
        )
        self.joey_chat_input.returnPressed.connect(self.trigger_joey_chat)

        self.btn_joey_send = QPushButton("💬 გაგზავნა (Send)", chat_card)
        self.btn_joey_send.setObjectName("PrimaryScanButton")
        self.btn_joey_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_joey_send.clicked.connect(self.trigger_joey_chat)

        input_row.addWidget(self.joey_chat_input, stretch=1)
        input_row.addWidget(self.btn_joey_send)
        chat_layout.addLayout(input_row)

        layout.addWidget(chat_card, stretch=1)
        return container

    # -----------------------------------------------------------------------
    # Tab 1: AI Signal, Multi-Layer Confluence Visualizer & Kronos Card
    # -----------------------------------------------------------------------
    def _build_ai_signal_and_confluence_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Top Section: Kronos Matrix Visual Forecast Card
        fc_header = QHBoxLayout()
        fc_title = QLabel("KRONOS MATRIX & MULTI-LAYER PREDICTION CARD")
        fc_title.setObjectName("SectionHeader")
        self.fc_model_badge = QLabel("NeoQuasar/Kronos-small [cpu]")
        self._apply_pill_style(self.fc_model_badge, "info")
        fc_header.addWidget(fc_title)
        fc_header.addStretch(1)
        fc_header.addWidget(self.fc_model_badge)
        layout.addLayout(fc_header)

        hero_grid = QGridLayout()
        hero_grid.setSpacing(8)

        # Box 1: Direction
        dir_box = QFrame(container)
        dir_box.setObjectName("SubCard")
        dir_l = QVBoxLayout(dir_box)
        dir_l.setContentsMargins(10, 8, 10, 8)
        dir_t = QLabel("KRONOS DIRECTION")
        dir_t.setObjectName("MetricTitle")
        self.kronos_dir_label = QLabel("ABSTAIN")
        self.kronos_dir_label.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        self.kronos_dir_label.setStyleSheet("color: #f59e0b;")
        dir_l.addWidget(dir_t)
        dir_l.addWidget(self.kronos_dir_label)

        # Box 2: Confidence % & Confluence Score
        conf_box = QFrame(container)
        conf_box.setObjectName("SubCard")
        conf_l = QVBoxLayout(conf_box)
        conf_l.setContentsMargins(10, 8, 10, 8)
        conf_t = QLabel("CONFIDENCE / CONFLUENCE SCORE")
        conf_t.setObjectName("MetricTitle")
        self.kronos_conf_label = QLabel("0.0%  (Score: 0.00)")
        self.kronos_conf_label.setObjectName("MetricValue")
        self.kronos_conf_bar = QProgressBar(conf_box)
        self.kronos_conf_bar.setRange(0, 100)
        self.kronos_conf_bar.setValue(0)
        self.kronos_conf_bar.setFormat("%p%")
        conf_l.addWidget(conf_t)
        conf_l.addWidget(self.kronos_conf_label)
        conf_l.addWidget(self.kronos_conf_bar)

        # Box 3: Price Range / M5 Box
        range_box = QFrame(container)
        range_box.setObjectName("SubCard")
        range_l = QVBoxLayout(range_box)
        range_l.setContentsMargins(10, 8, 10, 8)
        range_t = QLabel("PRICE RANGE / 20-BAR M5 BOX")
        range_t.setObjectName("MetricTitle")
        self.kronos_range_label = QLabel("--")
        self.kronos_range_label.setObjectName("MetricValue")
        self.kronos_range_sub = QLabel("Last Bar: --")
        self.kronos_range_sub.setStyleSheet("color: #94a3b8; font-family: 'Consolas', monospace; font-size: 10px;")
        range_l.addWidget(range_t)
        range_l.addWidget(self.kronos_range_label)
        range_l.addWidget(self.kronos_range_sub)

        hero_grid.addWidget(dir_box, 0, 0)
        hero_grid.addWidget(conf_box, 0, 1)
        hero_grid.addWidget(range_box, 0, 2)
        hero_grid.setColumnStretch(0, 25)
        hero_grid.setColumnStretch(1, 35)
        hero_grid.setColumnStretch(2, 40)
        layout.addLayout(hero_grid)

        # Middle Section: Multi-Layer Confluence Score Visualizer (5 Visual Bars)
        conf_vis_frame = QFrame(container)
        conf_vis_frame.setObjectName("SubCard")
        cv_layout = QVBoxLayout(conf_vis_frame)
        cv_layout.setContentsMargins(12, 10, 12, 10)
        cv_layout.setSpacing(5)

        cv_title = QLabel("5-LAYER CONFLUENCE SCORE VISUALIZER (MATRIX + ANALOGUE + KRONOS + TA + L2 ORDERBOOK)")
        cv_title.setObjectName("SectionHeader")
        cv_layout.addWidget(cv_title)

        self.layer_matrix_val = QLabel("ABSTAIN (Score: --)")
        self.layer_matrix_bar = QProgressBar()
        self.layer_matrix_bar.setRange(0, 100)

        self.layer_analog_val = QLabel("ABSTAIN (WinRate: --)")
        self.layer_analog_bar = QProgressBar()
        self.layer_analog_bar.setRange(0, 100)

        self.layer_forecast_val = QLabel("ABSTAIN (Move ATR: --)")
        self.layer_forecast_bar = QProgressBar()
        self.layer_forecast_bar.setRange(0, 100)

        self.layer_ta_val = QLabel("ABSTAIN (Conf: 0.0%)")
        self.layer_ta_bar = QProgressBar()
        self.layer_ta_bar.setRange(0, 100)

        self.layer_l2_val = QLabel("NEUTRAL (L2 Confluence: 50%)")
        self.layer_l2_bar = QProgressBar()
        self.layer_l2_bar.setRange(0, 100)
        self.layer_l2_bar.setValue(50)

        for title_txt, val_lbl, prog_bar in (
            ("1. M5 BREAKOUT MATRIX (H4/H1/M15/M5 — Min 6/8 pts):", self.layer_matrix_val, self.layer_matrix_bar),
            ("2. HISTORICAL ANALOGUE (24-Bar / 8-NN — Min 55% Win):", self.layer_analog_val, self.layer_analog_bar),
            ("3. KRONOS TRANSFORMER (12-Bar OHLCV — Min 67% Agree):", self.layer_forecast_val, self.layer_forecast_bar),
            ("4. TRADINGAGENTS & SPREAD COST GATE (<= 180 pts / 45% ATR):", self.layer_ta_val, self.layer_ta_bar),
            ("5. L2 ORDERBOOK & LIQUIDITY RADAR (Walls / Spoofing / Veto):", self.layer_l2_val, self.layer_l2_bar),
        ):
            row_top = QHBoxLayout()
            lbl = QLabel(title_txt)
            lbl.setObjectName("MetricTitle")
            val_lbl.setStyleSheet("color: #e2e8f0; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;")
            row_top.addWidget(lbl)
            row_top.addStretch(1)
            row_top.addWidget(val_lbl)
            cv_layout.addLayout(row_top)
            cv_layout.addWidget(prog_bar)

        layout.addWidget(conf_vis_frame)

        # Bottom Section: Plain-Language Explanation & Gate Diagnostics
        diag_title = QLabel("PLAIN-LANGUAGE EXPLANATION & GATE DIAGNOSTICS (რატომ მიიღო ბოტმა ეს გადაწყვეტილება):")
        diag_title.setObjectName("MetricTitle")
        self.gate_reasons_view = QTextEdit(container)
        self.gate_reasons_view.setReadOnly(True)
        self.gate_reasons_view.setPlaceholderText(
            "Click '▶ SCAN SYMBOL (MT5 → AI)' above to pull 5000 live M5 candles + L2 Orderbook DOM and run the 5-layer AI analysis..."
        )
        layout.addWidget(diag_title)
        layout.addWidget(self.gate_reasons_view, stretch=1)

        return container

    # -----------------------------------------------------------------------
    # Tab 2: L2 Orderbook Heatmap & Liquidity Radar Panel
    # -----------------------------------------------------------------------
    def _build_l2_orderbook_radar_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # 1. Header Bar: Title + Source Pill + Auto-Stream Toggle + Manual Refresh
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        title_lbl = QLabel("L2 ORDERBOOK HEATMAP & LIQUIDITY RADAR")
        title_lbl.setObjectName("SectionHeader")

        self.l2_source_badge = QLabel("SOURCE: MT5 L2 DOM")
        self._apply_pill_style(self.l2_source_badge, "info")

        self.chk_l2_live_stream = QCheckBox("Live DOM Stream (2.5s)", container)
        self.chk_l2_live_stream.setChecked(True)

        self.btn_refresh_l2 = QPushButton("↻ REFRESH DOM", container)
        self.btn_refresh_l2.setObjectName("ChipButton")
        self.btn_refresh_l2.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_l2.clicked.connect(self._request_current_l2_book)

        top_row.addWidget(title_lbl)
        top_row.addWidget(self.l2_source_badge)
        top_row.addStretch(1)
        top_row.addWidget(self.chk_l2_live_stream)
        top_row.addWidget(self.btn_refresh_l2)
        layout.addLayout(top_row)

        # 2. Level 2 Confluence Indicator & Orderflow Veto Card
        conf_card = QFrame(container)
        conf_card.setObjectName("SubCard")
        cc_layout = QVBoxLayout(conf_card)
        cc_layout.setContentsMargins(10, 8, 10, 8)
        cc_layout.setSpacing(5)

        badges_row = QHBoxLayout()
        badges_row.setSpacing(8)
        self.l2_direction_badge = QLabel("L2 FLOW: NEUTRAL")
        self._apply_pill_style(self.l2_direction_badge, "warning")

        self.l2_confluence_badge = QLabel("L2 CONFLUENCE: 50.0%")
        self._apply_pill_style(self.l2_confluence_badge, "info")

        self.l2_veto_badge = QLabel("GATE VETO: CLEAR")
        self._apply_pill_style(self.l2_veto_badge, "online")

        self.l2_walls_summary_label = QLabel("Buy Wall: --  |  Sell Wall: --")
        self.l2_walls_summary_label.setStyleSheet(
            "color: #cbd5e1; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )

        badges_row.addWidget(self.l2_direction_badge)
        badges_row.addWidget(self.l2_confluence_badge)
        badges_row.addWidget(self.l2_veto_badge)
        badges_row.addStretch(1)
        badges_row.addWidget(self.l2_walls_summary_label)
        cc_layout.addLayout(badges_row)

        pressure_row = QHBoxLayout()
        self.l2_imbalance_label = QLabel(
            "BID LIQUIDITY: 50.0% (0.0)   ◄═══ TUG OF WAR ═══►   ASK LIQUIDITY: 50.0% (0.0)"
        )
        self.l2_imbalance_label.setStyleSheet(
            "color: #e2e8f0; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        pressure_row.addWidget(self.l2_imbalance_label)
        cc_layout.addLayout(pressure_row)

        self.l2_pressure_bar = QProgressBar(conf_card)
        self.l2_pressure_bar.setRange(0, 100)
        self.l2_pressure_bar.setValue(50)
        self.l2_pressure_bar.setFormat("BID PRESSURE: %p%")
        self.l2_pressure_bar.setStyleSheet(
            "QProgressBar { background-color: #3b1219; border: 1px solid #ef4444; border-radius: 4px; "
            "text-align: center; color: #f8fafc; font-weight: bold; height: 16px; } "
            "QProgressBar::chunk { background-color: #10b981; border-radius: 3px; }"
        )
        cc_layout.addWidget(self.l2_pressure_bar)
        layout.addWidget(conf_card)

        # 3. Vertical Splitter: Visual DOM Depth Ladder (Top 65%) + Real-Time Liquidity Alerts Feed (Bottom 35%)
        l2_splitter = QSplitter(Qt.Orientation.Vertical, container)

        # Top: Visual DOM / Orderbook Depth Ladder
        dom_frame = QFrame(l2_splitter)
        dom_frame.setObjectName("CardPanel")
        dom_layout = QVBoxLayout(dom_frame)
        dom_layout.setContentsMargins(8, 6, 8, 6)
        dom_layout.setSpacing(4)

        dom_hdr_row = QHBoxLayout()
        self.l2_ladder_title = QLabel("VISUAL DOM / ORDERBOOK DEPTH LADDER (10 ASK × 10 BID LEVELS)")
        self.l2_ladder_title.setObjectName("SectionHeader")
        self.l2_spread_info_label = QLabel("MID: --  |  SPREAD: --")
        self.l2_spread_info_label.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        dom_hdr_row.addWidget(self.l2_ladder_title)
        dom_hdr_row.addStretch(1)
        dom_hdr_row.addWidget(self.l2_spread_info_label)
        dom_layout.addLayout(dom_hdr_row)

        self.l2_dom_table = QTableWidget(21, 6, dom_frame)
        self.l2_dom_table.setHorizontalHeaderLabels(
            ["SIDE / LVL", "PRICE", "VOLUME", "CUM VOL", "HEATMAP DEPTH BAR", "RADAR TAG"]
        )
        self.l2_dom_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.l2_dom_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.l2_dom_table.verticalHeader().setVisible(False)
        self.l2_dom_table.verticalHeader().setDefaultSectionSize(21)
        dom_hdr = self.l2_dom_table.horizontalHeader()
        dom_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dom_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dom_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dom_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        dom_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        dom_hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        dom_layout.addWidget(self.l2_dom_table, stretch=1)

        # Bottom: Real-Time Liquidity Alerts Feed
        alerts_frame = QFrame(l2_splitter)
        alerts_frame.setObjectName("CardPanel")
        alerts_layout = QVBoxLayout(alerts_frame)
        alerts_layout.setContentsMargins(8, 6, 8, 6)
        alerts_layout.setSpacing(4)

        alerts_hdr_row = QHBoxLayout()
        alerts_title = QLabel("REAL-TIME LIQUIDITY ALERTS FEED (WHALE ORDERS • WALLS • SPOOFING • ICEBERGS)")
        alerts_title.setObjectName("SectionHeader")
        self.l2_alerts_count_label = QLabel("ALERTS: 0")
        self.l2_alerts_count_label.setStyleSheet(
            "color: #f59e0b; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        btn_clear_l2_alerts = QPushButton("Clear", alerts_frame)
        btn_clear_l2_alerts.setObjectName("ChipButton")
        btn_clear_l2_alerts.clicked.connect(self._clear_l2_alerts_feed)

        alerts_hdr_row.addWidget(alerts_title)
        alerts_hdr_row.addStretch(1)
        alerts_hdr_row.addWidget(self.l2_alerts_count_label)
        alerts_hdr_row.addWidget(btn_clear_l2_alerts)
        alerts_layout.addLayout(alerts_hdr_row)

        self.l2_alerts_feed = self._create_log_console(
            "Monitoring L2 Orderbook for Whale Limit Orders, Buy/Sell Liquidity Walls, Spoofing (Pulled Orders), and Iceberg Absorption..."
        )
        alerts_layout.addWidget(self.l2_alerts_feed, stretch=1)

        l2_splitter.addWidget(dom_frame)
        l2_splitter.addWidget(alerts_frame)
        l2_splitter.setStretchFactor(0, 66)
        l2_splitter.setStretchFactor(1, 34)
        layout.addWidget(l2_splitter, stretch=1)
        return container

    # -----------------------------------------------------------------------
    # Tab 3: Quantitative Calculators Panel (Lot Size, Kelly & R:R, ATR Volatility)
    # -----------------------------------------------------------------------
    def _build_quant_calculators_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Top Sync Bar
        sync_row = QHBoxLayout()
        sync_lbl = QLabel("QUANTITATIVE RISK & POSITION CALCULATORS (SYNCED WITH LIVE MT5)")
        sync_lbl.setObjectName("SectionHeader")
        btn_sync_mt5_calc = QPushButton("⚡ PULL LIVE MT5 BALANCE & ATR", content)
        btn_sync_mt5_calc.setObjectName("ChipButton")
        btn_sync_mt5_calc.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_sync_mt5_calc.clicked.connect(self._sync_calculators_from_mt5)
        sync_row.addWidget(sync_lbl)
        sync_row.addStretch(1)
        sync_row.addWidget(btn_sync_mt5_calc)
        layout.addLayout(sync_row)

        # Calculator 1: Position Sizing Calculator (MT5 Lot Size)
        lot_card = QFrame(content)
        lot_card.setObjectName("SubCard")
        lot_grid = QGridLayout(lot_card)
        lot_grid.setContentsMargins(12, 10, 12, 10)
        lot_grid.setHorizontalSpacing(12)
        lot_grid.setVerticalSpacing(6)

        lot_title = QLabel("1. POSITION SIZING CALCULATOR (EXACT MT5 LOT SIZE)")
        lot_title.setObjectName("SectionHeader")
        lot_grid.addWidget(lot_title, 0, 0, 1, 4)

        self.calc_balance_spin = QDoubleSpinBox(lot_card)
        self.calc_balance_spin.setRange(1.0, 100_000_000.0)
        self.calc_balance_spin.setDecimals(2)
        self.calc_balance_spin.setValue(171.12)
        self.calc_balance_spin.setPrefix("$ ")

        self.calc_risk_pct_spin = QDoubleSpinBox(lot_card)
        self.calc_risk_pct_spin.setRange(0.05, 20.0)
        self.calc_risk_pct_spin.setDecimals(2)
        self.calc_risk_pct_spin.setSingleStep(0.05)
        self.calc_risk_pct_spin.setValue(0.50)
        self.calc_risk_pct_spin.setSuffix(" %")

        self.calc_sl_points_spin = QDoubleSpinBox(lot_card)
        self.calc_sl_points_spin.setRange(1.0, 1_000_000.0)
        self.calc_sl_points_spin.setDecimals(1)
        self.calc_sl_points_spin.setSingleStep(10.0)
        self.calc_sl_points_spin.setValue(325.0)
        self.calc_sl_points_spin.setSuffix(" pts")

        self.calc_point_val_spin = QDoubleSpinBox(lot_card)
        self.calc_point_val_spin.setRange(0.0001, 10000.0)
        self.calc_point_val_spin.setDecimals(4)
        self.calc_point_val_spin.setValue(1.0000)
        self.calc_point_val_spin.setPrefix("$ ")
        self.calc_point_val_spin.setToolTip("Dollar value of 1 point move per 1.00 standard lot")

        lot_grid.addWidget(self._metric_label("Account Balance:"), 1, 0)
        lot_grid.addWidget(self.calc_balance_spin, 1, 1)
        lot_grid.addWidget(self._metric_label("Risk per Trade (%):"), 1, 2)
        lot_grid.addWidget(self.calc_risk_pct_spin, 1, 3)

        lot_grid.addWidget(self._metric_label("Stop Loss (Points):"), 2, 0)
        lot_grid.addWidget(self.calc_sl_points_spin, 2, 1)
        lot_grid.addWidget(self._metric_label("$ / Point (per 1 Lot):"), 2, 2)
        lot_grid.addWidget(self.calc_point_val_spin, 2, 3)

        self.calc_lot_result_label = QLabel("RECOMMENDED LOT: 0.01 Lots  |  Risk: $0.86  |  3-Trade Daily Cap: $2.57")
        self.calc_lot_result_label.setStyleSheet(
            "background-color: #062e22; color: #10b981; border: 1px solid #059669; "
            "border-radius: 4px; padding: 6px 10px; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )
        lot_grid.addWidget(self.calc_lot_result_label, 3, 0, 1, 4)
        layout.addWidget(lot_card)

        # Calculator 2: Kelly Criterion & Risk/Reward Ratio Calculator
        kelly_card = QFrame(content)
        kelly_card.setObjectName("SubCard")
        kelly_grid = QGridLayout(kelly_card)
        kelly_grid.setContentsMargins(12, 10, 12, 10)
        kelly_grid.setHorizontalSpacing(12)
        kelly_grid.setVerticalSpacing(6)

        kelly_title = QLabel("2. KELLY CRITERION & RISK/REWARD RATIO CALCULATOR")
        kelly_title.setObjectName("SectionHeader")
        kelly_grid.addWidget(kelly_title, 0, 0, 1, 4)

        self.calc_winrate_spin = QDoubleSpinBox(kelly_card)
        self.calc_winrate_spin.setRange(1.0, 99.0)
        self.calc_winrate_spin.setDecimals(1)
        self.calc_winrate_spin.setValue(75.0)
        self.calc_winrate_spin.setSuffix(" %")

        self.calc_tp_points_spin = QDoubleSpinBox(kelly_card)
        self.calc_tp_points_spin.setRange(1.0, 1_000_000.0)
        self.calc_tp_points_spin.setDecimals(1)
        self.calc_tp_points_spin.setSingleStep(10.0)
        self.calc_tp_points_spin.setValue(650.0)
        self.calc_tp_points_spin.setSuffix(" pts")

        kelly_grid.addWidget(self._metric_label("Win Probability (Analogue):"), 1, 0)
        kelly_grid.addWidget(self.calc_winrate_spin, 1, 1)
        kelly_grid.addWidget(self._metric_label("Take Profit (Points):"), 1, 2)
        kelly_grid.addWidget(self.calc_tp_points_spin, 1, 3)

        self.calc_kelly_result_label = QLabel("R:R = 1 : 2.00  |  Full Kelly: --%  |  Half-Kelly: --%  |  EV: --")
        self.calc_kelly_result_label.setStyleSheet(
            "background-color: #082438; color: #38bdf8; border: 1px solid #0284c7; "
            "border-radius: 4px; padding: 6px 10px; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        kelly_grid.addWidget(self.calc_kelly_result_label, 2, 0, 1, 4)
        layout.addWidget(kelly_card)

        # Calculator 3: ATR Volatility & Execution Cost Calculator
        atr_card = QFrame(content)
        atr_card.setObjectName("SubCard")
        atr_grid = QGridLayout(atr_card)
        atr_grid.setContentsMargins(12, 10, 12, 10)
        atr_grid.setHorizontalSpacing(12)
        atr_grid.setVerticalSpacing(6)

        atr_title = QLabel("3. ATR VOLATILITY, STOP/TARGET LEVELS & SPREAD COST GATE")
        atr_title.setObjectName("SectionHeader")
        atr_grid.addWidget(atr_title, 0, 0, 1, 4)

        self.calc_price_spin = QDoubleSpinBox(atr_card)
        self.calc_price_spin.setRange(0.0001, 10_000_000.0)
        self.calc_price_spin.setDecimals(4)
        self.calc_price_spin.setValue(4165.76)

        self.calc_atr_spin = QDoubleSpinBox(atr_card)
        self.calc_atr_spin.setRange(0.0001, 100_000.0)
        self.calc_atr_spin.setDecimals(4)
        self.calc_atr_spin.setValue(2.1671)

        self.calc_atr_sl_mult = QDoubleSpinBox(atr_card)
        self.calc_atr_sl_mult.setRange(0.2, 20.0)
        self.calc_atr_sl_mult.setDecimals(2)
        self.calc_atr_sl_mult.setSingleStep(0.25)
        self.calc_atr_sl_mult.setValue(1.50)
        self.calc_atr_sl_mult.setSuffix("x ATR")

        self.calc_atr_tp_mult = QDoubleSpinBox(atr_card)
        self.calc_atr_tp_mult.setRange(0.2, 40.0)
        self.calc_atr_tp_mult.setDecimals(2)
        self.calc_atr_tp_mult.setSingleStep(0.25)
        self.calc_atr_tp_mult.setValue(3.00)
        self.calc_atr_tp_mult.setSuffix("x ATR")

        atr_grid.addWidget(self._metric_label("Current Price (MT5):"), 1, 0)
        atr_grid.addWidget(self.calc_price_spin, 1, 1)
        atr_grid.addWidget(self._metric_label("M5 ATR (14):"), 1, 2)
        atr_grid.addWidget(self.calc_atr_spin, 1, 3)

        atr_grid.addWidget(self._metric_label("ATR Stop Multiplier:"), 2, 0)
        atr_grid.addWidget(self.calc_atr_sl_mult, 2, 1)
        atr_grid.addWidget(self._metric_label("ATR Target Multiplier:"), 2, 2)
        atr_grid.addWidget(self.calc_atr_tp_mult, 2, 3)

        self.calc_atr_result_label = QLabel("LONG -> SL: -- | TP: --   ||   SHORT -> SL: -- | TP: --")
        self.calc_atr_result_label.setStyleSheet(
            "background-color: #141d2f; color: #e2e8f0; border: 1px solid #2b3b55; "
            "border-radius: 4px; padding: 6px 10px; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        atr_grid.addWidget(self.calc_atr_result_label, 3, 0, 1, 4)
        layout.addWidget(atr_card)
        layout.addStretch(1)

        # Wire live calculator updates
        for spin in (
            self.calc_balance_spin,
            self.calc_risk_pct_spin,
            self.calc_sl_points_spin,
            self.calc_point_val_spin,
            self.calc_winrate_spin,
            self.calc_tp_points_spin,
            self.calc_price_spin,
            self.calc_atr_spin,
            self.calc_atr_sl_mult,
            self.calc_atr_tp_mult,
            self.spread_spin,
        ):
            spin.valueChanged.connect(self._recompute_all_calculators)

        self._recompute_all_calculators()
        scroll.setWidget(content)
        return scroll

    @staticmethod
    def _metric_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("MetricTitle")
        return lbl

    # -----------------------------------------------------------------------
    # Tab 3: Live Feed Stream & Persistent Memory Search
    # -----------------------------------------------------------------------
    def _build_live_stream_and_memory_tab(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top Half: Live WebSocket Feed
        feed_frame = QFrame(splitter)
        feed_frame.setObjectName("CardPanel")
        feed_layout = QVBoxLayout(feed_frame)
        feed_layout.setContentsMargins(10, 8, 10, 8)
        feed_layout.setSpacing(6)

        header_row = QHBoxLayout()
        title_lbl = QLabel("LIVE RESEARCH STREAM  //  WEBSOCKET (/ws/logs)")
        title_lbl.setObjectName("SectionHeader")
        self.event_counter_label = QLabel("EVENTS: 0")
        self.event_counter_label.setStyleSheet(
            "color: #38bdf8; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.chk_autoscroll = QCheckBox("Auto-Scroll", feed_frame)
        self.chk_autoscroll.setChecked(True)
        btn_clear = QPushButton("Clear", feed_frame)
        btn_clear.setObjectName("ChipButton")
        btn_clear.clicked.connect(self._clear_live_logs)

        header_row.addWidget(title_lbl)
        header_row.addStretch(1)
        header_row.addWidget(self.event_counter_label)
        header_row.addWidget(self.chk_autoscroll)
        header_row.addWidget(btn_clear)
        feed_layout.addLayout(header_row)

        self.stream_tabs = QTabWidget(feed_frame)
        self.log_all = self._create_log_console("Live events from ws://127.0.0.1:8787/ws/logs ...")
        self.log_debate = self._create_log_console("TradingAgents debate stream (`tradingagents.debate`)...")
        self.log_kronos = self._create_log_console("Kronos forecast & MT5 sync logs (`kronos.forecast`)...")
        self.log_system = self._create_log_console("System & Gate logs (`gate.changed`, `memory.stored`)...")

        self.stream_tabs.addTab(self.log_all, "ALL STREAM")
        self.stream_tabs.addTab(self.log_debate, "DEBATE")
        self.stream_tabs.addTab(self.log_kronos, "KRONOS & MT5")
        self.stream_tabs.addTab(self.log_system, "GATE & SYSTEM")
        feed_layout.addWidget(self.stream_tabs, stretch=1)

        # Bottom Half: Persistent Memory Search (`/memory/search`)
        memory_card = QFrame(splitter)
        memory_card.setObjectName("CardPanel")
        mem_layout = QVBoxLayout(memory_card)
        mem_layout.setContentsMargins(10, 8, 10, 8)
        mem_layout.setSpacing(6)

        mem_header = QHBoxLayout()
        mem_title = QLabel("PERSISTENT CYCLE MEMORY SEARCH  //  GET /memory/search")
        mem_title.setObjectName("SectionHeader")
        self.mem_count_label = QLabel("RESULTS: 0")
        self.mem_count_label.setStyleSheet("color: #94a3b8; font-family: 'Consolas', monospace; font-size: 11px;")
        mem_header.addWidget(mem_title)
        mem_header.addStretch(1)
        mem_header.addWidget(self.mem_count_label)
        mem_layout.addLayout(mem_header)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.memory_query_input = QLineEdit(memory_card)
        self.memory_query_input.setPlaceholderText("Search past cycles (XAUUSD, EURUSD, ABSTAIN, BUY, spread)...")
        self.memory_query_input.setText("XAUUSD")
        self.memory_query_input.returnPressed.connect(self.trigger_memory_search)

        self.memory_limit_spin = QSpinBox(memory_card)
        self.memory_limit_spin.setRange(1, 20)
        self.memory_limit_spin.setValue(8)

        self.btn_search_memory = QPushButton("SEARCH", memory_card)
        self.btn_search_memory.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search_memory.clicked.connect(self.trigger_memory_search)

        search_row.addWidget(self.memory_query_input, stretch=1)
        search_row.addWidget(self.memory_limit_spin)
        search_row.addWidget(self.btn_search_memory)
        mem_layout.addLayout(search_row)

        chips_row = QHBoxLayout()
        chips_row.setSpacing(5)
        for token in ("XAUUSD", "EURUSD", "ABSTAIN", "BUY", "SELL", "kronos"):
            chip_btn = QPushButton(token, memory_card)
            chip_btn.setObjectName("ChipButton")
            chip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            chip_btn.clicked.connect(lambda checked=False, q=token: self._run_quick_memory_query(q))
            chips_row.addWidget(chip_btn)
        chips_row.addStretch(1)
        mem_layout.addLayout(chips_row)

        self.memory_table = QTableWidget(0, 6, memory_card)
        self.memory_table.setHorizontalHeaderLabels(
            ["SCAN ID", "SYMBOL", "ACTION", "CONF %", "SCORE", "CREATED / MATCH"]
        )
        self.memory_table.setAlternatingRowColors(True)
        self.memory_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.memory_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.memory_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.memory_table.verticalHeader().setVisible(False)
        hdr = self.memory_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.memory_table.itemSelectionChanged.connect(self._on_memory_row_selected)
        mem_layout.addWidget(self.memory_table, stretch=1)

        self.memory_detail_view = QTextEdit(memory_card)
        self.memory_detail_view.setReadOnly(True)
        self.memory_detail_view.setMaximumHeight(95)
        self.memory_detail_view.setPlaceholderText("Select a memory cycle row above to inspect full details...")
        mem_layout.addWidget(self.memory_detail_view)

        splitter.addWidget(feed_frame)
        splitter.addWidget(memory_card)
        splitter.setStretchFactor(0, 50)
        splitter.setStretchFactor(1, 50)
        return splitter

    @staticmethod
    def _create_log_console(placeholder: str) -> QTextEdit:
        console = QTextEdit()
        console.setReadOnly(True)
        console.setPlaceholderText(placeholder)
        return console

    # -----------------------------------------------------------------------
    # Quantitative Calculator Logic
    # -----------------------------------------------------------------------
    def _sync_calculators_from_mt5(self) -> None:
        acc = self._latest_mt5_account
        spec = self._latest_symbol_spec
        if acc.get("balance"):
            self.calc_balance_spin.setValue(float(acc["balance"]))
        if spec.get("bid"):
            self.calc_price_spin.setValue(float(spec["bid"]))
        if spec.get("atr_m5") and float(spec["atr_m5"]) > 0:
            atr_v = float(spec["atr_m5"])
            self.calc_atr_spin.setValue(atr_v)
            point = float(spec.get("point") or 0.01)
            if point > 0:
                sl_pts = round((1.5 * atr_v) / point, 1)
                tp_pts = round((3.0 * atr_v) / point, 1)
                self.calc_sl_points_spin.setValue(max(1.0, sl_pts))
                self.calc_tp_points_spin.setValue(max(1.0, tp_pts))
        tick_val = float(spec.get("trade_tick_value") or 1.0)
        tick_size = float(spec.get("trade_tick_size") or spec.get("point") or 0.01)
        point = float(spec.get("point") or 0.01)
        if tick_size > 0 and point > 0:
            point_dollar_val = tick_val * (point / tick_size)
            self.calc_point_val_spin.setValue(max(0.0001, point_dollar_val))
        self._recompute_all_calculators()
        self.status_message_label.setText("Synced Quant Calculators with live MT5 account balance & symbol specs.")

    def _recompute_all_calculators(self) -> None:
        # 1. Position Sizing Calculator
        balance = float(self.calc_balance_spin.value())
        risk_pct = float(self.calc_risk_pct_spin.value())
        sl_points = max(0.1, float(self.calc_sl_points_spin.value()))
        point_val = max(0.0001, float(self.calc_point_val_spin.value()))

        risk_dollars = balance * (risk_pct / 100.0)
        raw_lots = risk_dollars / (sl_points * point_val)
        vol_min = float(self._latest_symbol_spec.get("volume_min") or 0.01)
        vol_max = float(self._latest_symbol_spec.get("volume_max") or 100.0)
        vol_step = float(self._latest_symbol_spec.get("volume_step") or 0.01)
        stepped_lots = max(vol_min, min(vol_max, math.floor(raw_lots / vol_step) * vol_step))
        if raw_lots < vol_min:
            stepped_lots = vol_min
        self._computed_lot_size = round(stepped_lots, 2)
        actual_risk = stepped_lots * sl_points * point_val
        daily_cap = risk_dollars * 3.0

        self.calc_lot_result_label.setText(
            f"MT5 LOT SIZE: {stepped_lots:.2f} Lots (Raw: {raw_lots:.3f})  |  "
            f"Risk: ${risk_dollars:.2f} (Actual: ${actual_risk:.2f})  |  3-Trade Cap: ${daily_cap:.2f}"
        )

        # 2. Kelly Criterion & Risk/Reward Ratio
        win_prob = float(self.calc_winrate_spin.value()) / 100.0
        loss_prob = 1.0 - win_prob
        tp_points = max(0.1, float(self.calc_tp_points_spin.value()))
        rr_ratio = tp_points / sl_points
        breakeven_wr = (1.0 / (1.0 + rr_ratio)) * 100.0
        kelly_f = win_prob - (loss_prob / rr_ratio) if rr_ratio > 0 else 0.0
        full_kelly_pct = max(0.0, kelly_f * 100.0)
        half_kelly_pct = full_kelly_pct * 0.5
        quarter_kelly_pct = full_kelly_pct * 0.25
        ev_r = (win_prob * rr_ratio) - loss_prob

        self.calc_kelly_result_label.setText(
            f"R:R = 1 : {rr_ratio:.2f} (BE: {breakeven_wr:.1f}%)  |  "
            f"Full Kelly: {full_kelly_pct:.1f}%  •  Half-Kelly: {half_kelly_pct:.1f}%  •  "
            f"Quarter: {quarter_kelly_pct:.1f}%  |  EV: {ev_r:+.2f}R"
        )

        # 3. ATR Volatility & Stop/Target Levels (with relaxed Spread Cost Gate <= 180 pts / <= 45% ATR)
        price = float(self.calc_price_spin.value())
        atr = float(self.calc_atr_spin.value())
        sl_mult = float(self.calc_atr_sl_mult.value())
        tp_mult = float(self.calc_atr_tp_mult.value())
        point = float(self._latest_symbol_spec.get("point") or 0.01)
        digits = int(self._latest_symbol_spec.get("digits") or 2)
        spread_pts = float(self.spread_spin.value())
        spread_price = spread_pts * point
        spread_to_atr_pct = (spread_price / atr * 100.0) if atr > 0 else 0.0
        cost_status = "PASS (<=45%)" if (spread_to_atr_pct <= 45.0 and spread_pts <= 180.0) else "COST VETO"

        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult
        long_sl = round(price - sl_dist, digits)
        long_tp = round(price + tp_dist, digits)
        short_sl = round(price + sl_dist, digits)
        short_tp = round(price - tp_dist, digits)

        self._computed_long_sl = long_sl
        self._computed_long_tp = long_tp
        self._computed_short_sl = short_sl
        self._computed_short_tp = short_tp

        self.calc_atr_result_label.setText(
            f"LONG [SL: {long_sl:.{digits}f} | TP: {long_tp:.{digits}f}]  •  "
            f"SHORT [SL: {short_sl:.{digits}f} | TP: {short_tp:.{digits}f}]  •  "
            f"Spread/ATR: {spread_to_atr_pct:.1f}% [{cost_status}]"
        )

    # -----------------------------------------------------------------------
    # Trading Sessions Indicator & TradeLocker Chart Overlay Sync
    # -----------------------------------------------------------------------
    def _update_session_indicator_ui(self) -> None:
        offset = 0
        if hasattr(self, "session_timezone_combo"):
            try:
                offset = int(self.session_timezone_combo.currentData() or 0)
            except (TypeError, ValueError):
                offset = 0
        sess_info = compute_trading_sessions_utc(display_offset_hours=offset)
        sessions = sess_info.get("sessions", {})
        asia = sessions.get("asia", {})
        london = sessions.get("london", {})
        ny = sessions.get("new_york", {})

        for badge, s_obj, icon in (
            (self.session_asia_badge, asia, "🌏"),
            (self.session_london_badge, london, "🇬🇧"),
            (self.session_ny_badge, ny, "🗽"),
        ):
            name = s_obj.get("name", "")
            hrs = s_obj.get("hours", "")
            active = bool(s_obj.get("active", False))
            kz = bool(s_obj.get("killzone", False))
            status_tag = " [KILLZONE 🔥]" if kz else (" [OPEN ●]" if active else " [CLOSED]")
            badge.setText(f"{icon} {name} ({hrs}){status_tag}")
            self._apply_pill_style(badge, "warning" if kz else ("online" if active else "info"))

        if hasattr(self, "session_timezone_label"):
            self.session_timezone_label.setText(f"SESSIONS ({sess_info.get('display_timezone', 'UTC')}):")
        self.session_headline_label.setText(
            f"🕒 {sess_info.get('display_time', sess_info.get('utc_time', ''))}  |  {sess_info.get('headline', '')}"
        )

    def _request_open_positions(self, symbol: str | None = None) -> None:
        path = f"/mt5/positions?symbol={urllib.parse.quote(symbol.upper())}" if symbol else "/mt5/positions"
        self._spawn_http_task(
            "mt5_positions",
            "GET",
            path,
            timeout=8.0,
        )

    def _request_trade_history(self) -> None:
        self._spawn_http_task(
            "mt5_history",
            "GET",
            "/mt5/history?days=30&limit=150",
            timeout=10.0,
        )

    def trigger_multi_pair_screener(self) -> None:
        self.btn_run_screener.setEnabled(False)
        self.btn_run_screener.setText("⏳ SCREENING 8 PAIRS...")
        self.status_message_label.setText(
            "Running 4-Step Waterfall Screener across Top 8 pairs (XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, ETHUSD, US500, USTEC)..."
        )
        self._spawn_http_task("screener_run", "POST", "/screener/run", payload={}, timeout=45.0)

    def trigger_execute_demo_trade(self, *, forced_side: str | None = None) -> None:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        self._recompute_all_calculators()

        if forced_side in {"BUY", "SELL"}:
            side = forced_side
        else:
            sel_mode = self.demo_side_combo.currentText().upper()
            if "BUY" in sel_mode:
                side = "BUY"
            elif "SELL" in sel_mode:
                side = "SELL"
            else:
                if self._latest_gate_action in {"BUY", "SELL"}:
                    side = self._latest_gate_action
                elif self._latest_ai_direction in {"BUY", "SELL"}:
                    side = self._latest_ai_direction
                else:
                    side = "BUY"

        volume = max(0.01, float(self._computed_lot_size))
        sl_price = self._computed_long_sl if side == "BUY" else self._computed_short_sl
        tp_price = self._computed_long_tp if side == "BUY" else self._computed_short_tp

        payload = {
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "sl": sl_price,
            "tp": tp_price,
            "comment": "NewEraQuantDemo",
            # A broker rejection must not silently become a simulated
            # position.  The explicit overlay mode is backend-controlled and
            # is not used by the normal trading buttons.
            "allow_paper_fallback": False,
        }
        self.btn_execute_demo.setEnabled(False)
        self.btn_execute_demo.setText(f"⏳ EXECUTING {side} {volume:.2f}...")
        self.status_message_label.setText(
            f"Sending Demo {side} {volume:.2f} lot order for {symbol} (SL={sl_price}, TP={tp_price}) to MT5 Demo..."
        )
        self._spawn_http_task("demo_execute", "POST", "/mt5/demo/execute", payload=payload, timeout=12.0)

    def on_toggle_auto_execute_demo(self, checked: bool) -> None:
        if hasattr(self, "btn_header_auto_trade") and self.btn_header_auto_trade.isChecked() != checked:
            self.btn_header_auto_trade.blockSignals(True)
            self.btn_header_auto_trade.setChecked(checked)
            self.btn_header_auto_trade.blockSignals(False)
        if hasattr(self, "btn_auto_execute_demo") and self.btn_auto_execute_demo.isChecked() != checked:
            self.btn_auto_execute_demo.blockSignals(True)
            self.btn_auto_execute_demo.setChecked(checked)
            self.btn_auto_execute_demo.blockSignals(False)
        if hasattr(self, "chk_joey_autonomous") and self.chk_joey_autonomous.isChecked() != checked:
            self.chk_joey_autonomous.blockSignals(True)
            self.chk_joey_autonomous.setChecked(checked)
            self.chk_joey_autonomous.blockSignals(False)

        self._style_header_auto_trade_btn(checked)
        self._sync_autonomous_settings()
        if checked:
            if hasattr(self, "btn_auto_execute_demo"):
                self.btn_auto_execute_demo.setText("🤖 AUTO-EXEC DEMO: ON")
            self.status_message_label.setText(
                "100% Autonomous Mode ENABLED (Demo Account Only): 8-Pair Screener & Waterfall signals (>=68%) will auto-execute with Gemini CIO approval."
            )
            self._append_log_entry(
                self.log_all,
                "AUTO.DEMO",
                "100% Autonomous Mode enabled (strictly restricted to MT5 DEMO accounts; Gemini CIO review active).",
                "#a855f7",
            )
        else:
            if hasattr(self, "btn_auto_execute_demo"):
                self.btn_auto_execute_demo.setText("🤖 AUTO-EXEC DEMO: OFF")
            self.status_message_label.setText("Auto-Execute Demo disabled.")

    def _on_joey_autonomous_toggled(self, checked: bool) -> None:
        self.on_toggle_auto_execute_demo(checked)

    def _apply_autonomous_ui_state(self, enabled: bool) -> None:
        """Apply state received from backend (/health, /screener/status, or autonomous.changed WS event)."""
        if hasattr(self, "btn_header_auto_trade") and self.btn_header_auto_trade.isChecked() != enabled:
            self.btn_header_auto_trade.blockSignals(True)
            self.btn_header_auto_trade.setChecked(enabled)
            self.btn_header_auto_trade.blockSignals(False)
        if hasattr(self, "btn_auto_execute_demo") and self.btn_auto_execute_demo.isChecked() != enabled:
            self.btn_auto_execute_demo.blockSignals(True)
            self.btn_auto_execute_demo.setChecked(enabled)
            self.btn_auto_execute_demo.blockSignals(False)
        if hasattr(self, "chk_joey_autonomous") and self.chk_joey_autonomous.isChecked() != enabled:
            self.chk_joey_autonomous.blockSignals(True)
            self.chk_joey_autonomous.setChecked(enabled)
            self.chk_joey_autonomous.blockSignals(False)
        self._style_header_auto_trade_btn(enabled)
        if hasattr(self, "btn_auto_execute_demo"):
            self.btn_auto_execute_demo.setText(f"🤖 AUTO-EXEC DEMO: {'ON' if enabled else 'OFF'}")

    def _sync_autonomous_settings(self) -> None:
        auto_on = bool(
            (hasattr(self, "btn_header_auto_trade") and self.btn_header_auto_trade.isChecked())
            or (hasattr(self, "btn_auto_execute_demo") and self.btn_auto_execute_demo.isChecked())
            or (hasattr(self, "chk_joey_autonomous") and self.chk_joey_autonomous.isChecked())
        )
        trail_on = bool(not hasattr(self, "chk_dynamic_trailing") or self.chk_dynamic_trailing.isChecked())
        self._spawn_http_task(
            "gemini_autonomous",
            "POST",
            "/gemini/autonomous",
            payload={
                "autonomous_mode": auto_on,
                "dynamic_trailing_enabled": trail_on,
                "paused": False,
            },
            timeout=8.0,
        )

    def trigger_joey_analyze_setup(self) -> None:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        equity = float(self.calc_balance_spin.value() or 1000.0)
        self.btn_joey_analyze.setEnabled(False)
        self.btn_joey_analyze.setText(f"⏳ ჯოი აანალიზებს {symbol}-ს...")
        self._append_joey_chat_message(
            "ლევანი",
            f"🔍 ჯოი, გააანალიზე {symbol}-ის მიმდინარე სეტაპი (TA-Lib + Kronos + L2 DOM + რისკ-გეგმა).",
            is_user=True,
        )
        self._spawn_http_task(
            "joey_analyze",
            "POST",
            "/gemini/analyze",
            payload={"symbol": symbol, "account_equity": equity},
            timeout=25.0,
        )

    def _send_quick_joey_prompt(self, prompt_text: str) -> None:
        self.joey_chat_input.setText(prompt_text)
        self.trigger_joey_chat()

    def trigger_joey_chat(self) -> None:
        msg = self.joey_chat_input.text().strip()
        if not msg:
            return
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        self.joey_chat_input.clear()
        self.btn_joey_send.setEnabled(False)
        self._append_joey_chat_message("ლევანი", msg, is_user=True)
        self._spawn_http_task(
            "joey_chat",
            "POST",
            "/gemini/chat",
            payload={"symbol": symbol, "message": msg},
            timeout=25.0,
        )

    def _append_joey_chat_message(self, speaker: str, text: str, *, is_user: bool = False) -> None:
        if not hasattr(self, "joey_chat_view"):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        color = "#34d399" if is_user else "#38bdf8"
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        html = (
            f'<div style="margin-top:6px;">'
            f'<span style="color:#64748b;">[{stamp}]</span> '
            f'<span style="color:{color}; font-weight:bold;">[{speaker}]:</span><br>'
            f'<span style="color:#f1f5f9;">{escaped}</span>'
            f"</div>"
        )
        self.joey_chat_view.append(html)
        self.joey_chat_view.moveCursor(QTextCursor.MoveOperation.End)

    def trigger_telegram_command(self, command: str) -> None:
        self._spawn_http_task(
            "telegram_command",
            "POST",
            "/telegram/command",
            payload={"command": command},
            timeout=15.0,
        )

    def trigger_close_demo_positions(self) -> None:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        self.btn_close_demo.setEnabled(False)
        self.status_message_label.setText(f"Closing Demo position(s) for {symbol}...")
        self._spawn_http_task(
            "demo_close",
            "POST",
            "/mt5/demo/close",
            payload={"symbol": symbol},
            timeout=12.0,
        )

    def trigger_close_all_demo_positions(self) -> None:
        self.btn_close_demo.setEnabled(False)
        self.status_message_label.setText("Closing ALL open Demo positions across all symbols...")
        self._spawn_http_task(
            "demo_close",
            "POST",
            "/mt5/demo/close",
            payload={},
            timeout=12.0,
        )

    def trigger_close_single_position(self, ticket: int) -> None:
        self.status_message_label.setText(f"Closing Demo position Ticket #{ticket}...")
        self._spawn_http_task(
            "demo_close",
            "POST",
            "/mt5/demo/close",
            payload={"ticket": int(ticket)},
            timeout=12.0,
        )

    def _update_positions_ui(self, data: dict[str, Any]) -> None:
        positions = data.get("positions") if isinstance(data.get("positions"), list) else []
        self._open_positions = [p for p in positions if isinstance(p, dict)]

        total_pnl = float(
            data.get("total_floating_pnl")
            if data.get("total_floating_pnl") is not None
            else sum(float(p.get("profit") or 0.0) for p in self._open_positions)
        )
        bal = float(
            data.get("account_balance")
            or self._latest_mt5_account.get("balance")
            or self.calc_balance_spin.value()
            or 1000.0
        )
        total_pnl_pct = (
            float(data["floating_pnl_pct"])
            if data.get("floating_pnl_pct") is not None
            else ((total_pnl / bal) * 100.0 if bal > 0 else 0.0)
        )

        # 1. Sync Header Telemetry Live Floating PnL
        self._update_header_live_pnl_label(total_pnl, total_pnl_pct)

        # 2. Update Bottom Active Open Positions Tab Header & Table
        count = len(self._open_positions)
        pnl_sign = "+" if total_pnl >= 0 else "-"
        pct_sign = "+" if total_pnl_pct >= 0 else "-"
        summary_color = "#10b981" if total_pnl >= 0 else "#ef4444"
        self.open_pos_summary_label.setText(
            f"ACTIVE OPEN POSITIONS: {count}  |  "
            f"LIVE FLOATING PnL: {pnl_sign}${abs(total_pnl):,.2f} ({pct_sign}{abs(total_pnl_pct):.2f}%)"
        )
        self.open_pos_summary_label.setStyleSheet(
            f"color: {summary_color if count > 0 else '#94a3b8'}; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )
        self.bottom_positions_tabs.setTabText(0, f"📊 ACTIVE OPEN POSITIONS ({count})")

        self.open_positions_table.setRowCount(count)
        for row_idx, pos in enumerate(self._open_positions):
            ticket = int(pos.get("ticket") or 0)
            sym = str(pos.get("symbol") or "XAUUSD")
            side = str(pos.get("side") or "BUY").upper()
            vol = float(pos.get("volume") or 0.01)
            digits = int(pos.get("digits") or 2)
            open_px = float(pos.get("price_open") or 0.0)
            cur_px = float(pos.get("price_current") or open_px)
            sl_px = float(pos.get("sl") or 0.0)
            tp_px = float(pos.get("tp") or 0.0)
            pnl = float(pos.get("profit") or 0.0)
            pos_pct = (
                float(pos["pnl_pct"])
                if pos.get("pnl_pct") is not None
                else ((pnl / bal) * 100.0 if bal > 0 else 0.0)
            )

            p_sign = "+" if pnl >= 0 else "-"
            pp_sign = "+" if pos_pct >= 0 else "-"
            pnl_str = f"{p_sign}${abs(pnl):.2f} ({pp_sign}{abs(pos_pct):.2f}%)"

            cells = [
                QTableWidgetItem(f"#{ticket}"),
                QTableWidgetItem(sym),
                QTableWidgetItem(side),
                QTableWidgetItem(f"{vol:.2f}"),
                QTableWidgetItem(f"{open_px:.{digits}f}"),
                QTableWidgetItem(f"{cur_px:.{digits}f}"),
                QTableWidgetItem(f"{sl_px:.{digits}f}" if sl_px > 0 else "--"),
                QTableWidgetItem(f"{tp_px:.{digits}f}" if tp_px > 0 else "--"),
                QTableWidgetItem(pnl_str),
            ]
            cells[2].setForeground(QColor("#10b981" if side == "BUY" else "#ef4444"))
            cells[8].setForeground(QColor("#10b981" if pnl >= 0 else "#ef4444"))

            for col_idx, cell_item in enumerate(cells):
                cell_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.open_positions_table.setItem(row_idx, col_idx, cell_item)

            close_btn = QPushButton("✖ CLOSE", self.open_positions_table)
            close_btn.setObjectName("CloseDemoButton")
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.clicked.connect(lambda checked=False, t=ticket: self.trigger_close_single_position(t))
            self.open_positions_table.setCellWidget(row_idx, 9, close_btn)

    def _update_trade_history_ui(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        trades = data.get("trades") if isinstance(data.get("trades"), list) else []
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}

        total_realized = float(summary.get("total_realized_pnl") or 0.0)
        win_rate = float(summary.get("win_rate_pct") or 0.0)
        pf = float(summary.get("profit_factor") or 0.0)
        total_trades = int(summary.get("total_trades") or len(trades))
        wins = int(summary.get("winning_trades") or 0)
        losses = int(summary.get("losing_trades") or 0)

        r_sign = "+" if total_realized >= 0 else "-"
        r_color = "#10b981" if total_realized >= 0 else "#ef4444"
        self.hist_total_pnl_label.setText(f"Total Realized PnL: {r_sign}${abs(total_realized):,.2f}")
        self.hist_total_pnl_label.setStyleSheet(
            f"color: {r_color}; font-family: 'Consolas', monospace; font-size: 12px; font-weight: bold;"
        )
        self.hist_win_rate_label.setText(f"Win Rate: {win_rate:.1f}% ({wins}W / {losses}L)")
        self.hist_profit_factor_label.setText(f"Profit Factor: {pf:.2f}")
        self.hist_total_trades_label.setText(f"Total Trades: {total_trades}")
        self.bottom_positions_tabs.setTabText(1, f"📜 TRADE HISTORY & STATS ({total_trades})")

        self.trade_history_table.setRowCount(len(trades))
        for row_idx, tr in enumerate(trades):
            if not isinstance(tr, dict):
                continue
            ticket = tr.get("ticket", "--")
            sym = str(tr.get("symbol") or "XAUUSD")
            side = str(tr.get("side") or "BUY").upper()
            vol = float(tr.get("volume") or 0.01)
            open_t = str(tr.get("open_time") or "--")[-8:]
            close_t = str(tr.get("close_time") or "--")
            if len(close_t) > 16:
                close_t = close_t[:16].replace("T", " ")
            time_span = f"{open_t} → {close_t}"
            entry_p = float(tr.get("entry_price") or 0.0)
            exit_p = float(tr.get("exit_price") or 0.0)
            net_pnl = float(tr.get("realized_pnl") if tr.get("realized_pnl") is not None else (tr.get("profit") or 0.0))

            n_sign = "+" if net_pnl >= 0 else "-"
            items = [
                QTableWidgetItem(f"#{ticket}"),
                QTableWidgetItem(sym),
                QTableWidgetItem(time_span),
                QTableWidgetItem(side),
                QTableWidgetItem(f"{vol:.2f}"),
                QTableWidgetItem(f"{entry_p:.2f} → {exit_p:.2f}"),
                QTableWidgetItem(f"{n_sign}${abs(net_pnl):.2f}"),
            ]
            items[3].setForeground(QColor("#10b981" if side == "BUY" else "#ef4444"))
            items[6].setForeground(QColor("#10b981" if net_pnl >= 0 else "#ef4444"))
            for col_idx, cell_item in enumerate(items):
                cell_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.trade_history_table.setItem(row_idx, col_idx, cell_item)

    # -----------------------------------------------------------------------
    # Timers, Background WebSocket Stream & HTTP Orchestration
    # -----------------------------------------------------------------------
    def _setup_timers_and_streams(self) -> None:
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(5000)
        self.poll_timer.timeout.connect(self._poll_periodic_state)
        self.poll_timer.start()

        self.l2_timer = QTimer(self)
        self.l2_timer.setInterval(2500)
        self.l2_timer.timeout.connect(self._poll_l2_stream)
        self.l2_timer.start()

        self.pair_switch_scan_timer = QTimer(self)
        self.pair_switch_scan_timer.setSingleShot(True)
        self.pair_switch_scan_timer.timeout.connect(self._on_pair_switch_auto_scan)

        self.auto_scan_timer = QTimer(self)
        self.auto_scan_timer.timeout.connect(self._trigger_scheduled_auto_scan)

        self.ws_ping_timer = QTimer(self)
        self.ws_ping_timer.setInterval(1000)
        self.ws_ping_timer.timeout.connect(self._refresh_ws_ping_ui)
        self.ws_ping_timer.start()

        self.ws_thread = WebSocketStreamThread(self.ws_url, self)
        self.ws_thread.ws_status_changed.connect(self._on_ws_status_changed)
        self.ws_thread.event_received.connect(self._on_ws_event_received)
        self.ws_thread.reconnected.connect(self.refresh_all_state)
        self.ws_thread.start()

    def _spawn_http_task(
        self,
        task_name: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 12.0,
        include_local_kronos_snapshot: bool = False,
    ) -> None:
        url = f"{self.api_url}{path}"
        worker = HttpTaskThread(
            task_name,
            method,
            url,
            payload=payload,
            timeout=timeout,
            include_local_kronos_snapshot=include_local_kronos_snapshot,
            parent=self,
        )
        self._active_threads.add(worker)
        worker.task_succeeded.connect(self._on_http_succeeded)
        worker.task_failed.connect(self._on_http_failed)
        worker.finished.connect(lambda w=worker: self._cleanup_thread(w))
        worker.start()

    def _cleanup_thread(self, worker: QThread) -> None:
        self._active_threads.discard(worker)
        worker.deleteLater()

    def refresh_all_state(self) -> None:
        self._update_session_indicator_ui()
        self._request_controls_state()
        self._request_decision_history()
        self._spawn_http_task("health", "GET", "/health")
        self._spawn_http_task("mt5_status", "GET", "/mt5/status")
        symbol = self.ticker_combo.currentText().strip() or "XAUUSD"
        self._spawn_http_task("mt5_symbol", "GET", f"/mt5/symbol/{urllib.parse.quote(symbol)}")
        self._request_open_positions()
        self._request_trade_history()
        self._request_l2_book(symbol, publish=False)
        self._spawn_http_task("bots_status", "GET", "/bots/status")
        self._spawn_http_task("strategies_list", "GET", "/strategies", timeout=12.0)
        self._spawn_http_task(
            "gates_latest",
            "GET",
            "/gates/latest",
            include_local_kronos_snapshot=True,
        )
        self._spawn_http_task("scans_list", "GET", "/scans")
        if not self._memory_records and self.memory_query_input.text().strip():
            self.trigger_memory_search()

    def _poll_periodic_state(self) -> None:
        self._update_session_indicator_ui()
        self._request_controls_state()
        self._request_decision_history()
        self._spawn_http_task("health", "GET", "/health")
        self._spawn_http_task("mt5_status", "GET", "/mt5/status")
        symbol = self.ticker_combo.currentText().strip() or "XAUUSD"
        self._spawn_http_task("mt5_symbol", "GET", f"/mt5/symbol/{urllib.parse.quote(symbol)}")
        self._request_open_positions()
        self._request_trade_history()
        self._spawn_http_task(
            "gates_latest",
            "GET",
            "/gates/latest",
            include_local_kronos_snapshot=True,
        )
        if self._active_scan_id:
            self._spawn_http_task("scan_detail", "GET", f"/scans/{self._active_scan_id}")

    def _poll_l2_stream(self) -> None:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        self._request_open_positions()
        if not self.chk_l2_live_stream.isChecked():
            return
        self._request_l2_book(symbol, publish=False)

    def _request_current_l2_book(self) -> None:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        self._request_l2_book(symbol, publish=True)

    def _request_l2_book(self, symbol: str, *, publish: bool = False) -> None:
        sym = symbol.strip().upper() or "XAUUSD"
        pub_flag = "true" if publish else "false"
        self._spawn_http_task(
            "l2_book",
            "GET",
            f"/l2/book/{urllib.parse.quote(sym)}?levels=10&publish={pub_flag}",
            timeout=10.0,
        )

    def _on_symbol_changed(self, new_symbol: str) -> None:
        if self._updating_symbol_combo:
            return
        sym = new_symbol.strip().upper()
        if not sym or len(sym) < 3:
            return
        if sym != self._current_tv_symbol:
            self._current_tv_symbol = sym
            self._reload_tradingview_chart(sym)
            self._spawn_http_task("mt5_symbol", "GET", f"/mt5/symbol/{urllib.parse.quote(sym)}")
            self._request_open_positions()
            self._request_l2_book(sym, publish=True)
            self.status_message_label.setText(
                f"Switched active pair to {sym} -> L2 Orderbook DOM stream & TradingView chart synced."
            )
            if self._ui_initialized and self.chk_scan_on_switch.isChecked():
                self.pair_switch_scan_timer.start(450)

    def _on_pair_switch_auto_scan(self) -> None:
        if self._active_scan_id is not None:
            return
        if not self.chk_scan_on_switch.isChecked():
            return
        self.trigger_manual_scan()

    # -----------------------------------------------------------------------
    # Scan Actions & Memory Search
    # -----------------------------------------------------------------------
    def _build_scan_payload(self) -> dict[str, Any]:
        symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        include_ta = self.chk_tradingagents.isChecked()
        include_kronos = self.chk_kronos.isChecked()
        if not include_ta and not include_kronos:
            include_kronos = True
            self.chk_kronos.setChecked(True)

        payload: dict[str, Any] = {
            "symbol": symbol,
            "include_tradingagents": include_ta,
            "include_kronos": include_kronos,
            "gate_ttl_minutes": int(self.ttl_spin.value()),
        }
        if self.spread_enabled_check.isChecked():
            payload["spread_points"] = float(self.spread_spin.value())
        return payload

    def trigger_manual_scan(self) -> None:
        payload = self._build_scan_payload()
        self.btn_manual_scan.setEnabled(False)
        self.btn_manual_scan.setText(f"⏳ SCANNING {payload['symbol']}...")
        self.status_message_label.setText(
            f"Pulling M5/M15 candles + L2 DOM for {payload['symbol']} and running Kronos + Matrix + TradingAgents + Orderflow Radar..."
        )
        self._append_log_entry(
            self.log_all,
            "UI.SCAN",
            f"Started Multi-Layer + L2 DOM Scan for {payload['symbol']} (spread={payload.get('spread_points', 'auto')} pts)",
            "#38bdf8",
        )
        self._request_l2_book(payload["symbol"], publish=True)
        self._spawn_http_task("scan_manual", "POST", "/scan/manual", payload=payload, timeout=15.0)

    def on_toggle_auto_scan(self, checked: bool) -> None:
        if checked:
            interval_min = int(self.auto_interval_spin.value())
            self.btn_auto_scan.setText(f"⟳ AUTO-SCAN: ON ({interval_min}m)")
            self.auto_scan_timer.start(interval_min * 60 * 1000)
            self._trigger_scheduled_auto_scan()
        else:
            self.auto_scan_timer.stop()
            self.btn_auto_scan.setText("⟳ AUTO-SCAN: OFF")
            self.status_message_label.setText("Auto-Scan disabled.")

    def _trigger_scheduled_auto_scan(self) -> None:
        if self._active_scan_id is not None:
            return
        payload = self._build_scan_payload()
        self._spawn_http_task("scan_automatic", "POST", "/scan/automatic", payload=payload, timeout=15.0)

    def trigger_memory_search(self) -> None:
        query = self.memory_query_input.text().strip()
        if not query:
            return
        limit = int(self.memory_limit_spin.value())
        encoded_q = urllib.parse.quote(query)
        self.btn_search_memory.setEnabled(False)
        self._spawn_http_task("memory_search", "GET", f"/memory/search?q={encoded_q}&limit={limit}")

    def _run_quick_memory_query(self, query: str) -> None:
        self.memory_query_input.setText(query)
        self.trigger_memory_search()

    # -----------------------------------------------------------------------
    # HTTP Response Handlers
    # -----------------------------------------------------------------------
    def _on_http_succeeded(self, task_name: str, data: object) -> None:
        if task_name == "health" and isinstance(data, dict):
            self._update_health_ui(data)
        elif task_name == "controls_get" and isinstance(data, dict):
            self._update_phase2_controls_ui(data)
        elif task_name == "controls_update" and isinstance(data, dict):
            self._update_phase2_controls_ui(data)
            self.phase2_controls_tab.set_status("Runtime controls applied.")
            self.status_message_label.setText("Quant Gate runtime controls applied.")
        elif task_name == "decision_history" and isinstance(data, dict):
            self._update_decision_history_ui(data)
        elif task_name == "decision_history_summary" and isinstance(data, dict):
            self.phase2_audit_tab.set_summary(data)
        elif task_name == "mt5_status" and isinstance(data, dict):
            self._update_mt5_status_ui(data)
        elif task_name == "mt5_symbol" and isinstance(data, dict):
            self._update_mt5_symbol_ui(data)
        elif task_name == "mt5_positions" and isinstance(data, dict):
            self._update_positions_ui(data)
        elif task_name == "mt5_history" and isinstance(data, dict):
            self._update_trade_history_ui(data)
        elif task_name == "screener_run" and isinstance(data, dict):
            self.btn_run_screener.setEnabled(True)
            self.btn_run_screener.setText("🔎 SCAN TOP 8 PAIRS")
            best = data.get("best_setup") if isinstance(data.get("best_setup"), dict) else {}
            opened_cnt = int(data.get("auto_executed_count") or 0)
            self.status_message_label.setText(
                f"8-Pair Screener Complete: Best={best.get('symbol', '--')} {best.get('final_action', 'ABSTAIN')} "
                f"({best.get('win_probability_pct', 0)}%) | Auto-Opened: {opened_cnt}"
            )
            self._request_open_positions()
            self._request_trade_history()
        elif task_name == "demo_execute" and isinstance(data, dict):
            self.btn_execute_demo.setEnabled(True)
            self.btn_execute_demo.setText("⚡ EXECUTE DEMO TRADE")
            pos = data.get("position") if isinstance(data.get("position"), dict) else {}
            src = data.get("execution_source", "mt5_demo")
            is_paper_overlay = bool(
                data.get("paper_fallback")
                or data.get("demo_overlay")
                or src == "mt5_demo_paper_fallback"
            )
            execution_label = "LOCAL PAPER OVERLAY" if is_paper_overlay else "MT5 DEMO LIVE"
            self.status_message_label.setText(
                f"Demo Trade Opened [{execution_label} | {src}]: {pos.get('side')} {pos.get('volume')} lot {pos.get('symbol')} @ {pos.get('price_open')} "
                f"| SL={pos.get('sl')} (-${pos.get('risk_usd')}) | TP={pos.get('tp')} (+${pos.get('reward_usd')})"
            )
            self._request_open_positions()
        elif task_name == "demo_close" and isinstance(data, dict):
            self.btn_close_demo.setEnabled(True)
            cnt = int(data.get("closed_count", 0))
            self.status_message_label.setText(f"Closed {cnt} Demo position(s).")
            self._request_open_positions()
            self._request_trade_history()
        elif task_name == "l2_book" and isinstance(data, dict):
            self._update_l2_orderbook_ui(data)
        elif task_name == "bots_status" and isinstance(data, dict):
            self._update_bots_ui(data)
        elif task_name == "strategies_list" and isinstance(data, dict):
            self.strategy_lab_panel.set_strategy_profiles(data)
        elif task_name in {"strategy_save", "strategy_load", "strategy_backtest", "strategy_walk_forward"} and isinstance(data, dict):
            self.strategy_lab_panel.set_backend_result(task_name, data)
            if task_name == "strategy_save":
                self._spawn_http_task("strategies_list", "GET", "/strategies", timeout=12.0)
            self.status_message_label.setText(f"Strategy Lab {task_name.replace('_', ' ')} completed.")
        elif task_name in {"bots_add", "bot_enabled", "bot_remove"}:
            self.phase2_fleet_tab.set_action_status("Fleet action applied; registry refreshed.")
            self._spawn_http_task("bots_status", "GET", "/bots/status", timeout=8.0)
        elif task_name == "gates_latest" and isinstance(data, dict):
            self._update_gates_and_prediction_ui(data)
        elif task_name in {"scan_manual", "scan_automatic"} and isinstance(data, dict):
            scan_id = str(data.get("scan_id", ""))
            mode = str(data.get("mode", "manual")).upper()
            self._active_scan_id = scan_id or None
            self.status_message_label.setText(f"Scan running [{mode}]: {scan_id[:8]}...")
        elif task_name == "scans_list" and isinstance(data, list):
            self._recent_scans = [item for item in data if isinstance(item, dict)]
            if self._recent_scans:
                latest_job = self._recent_scans[0]
                if latest_job.get("status") in {"queued", "running"}:
                    self._active_scan_id = str(latest_job.get("scan_id") or "")
        elif task_name == "scan_detail" and isinstance(data, dict):
            status = str(data.get("status", ""))
            if status in {"completed", "failed"}:
                self._active_scan_id = None
                self.btn_manual_scan.setEnabled(True)
                self.btn_manual_scan.setText("▶ SCAN SYMBOL (MT5 → AI)")
                self._spawn_http_task(
                    "gates_latest",
                    "GET",
                    "/gates/latest",
                    include_local_kronos_snapshot=True,
                )
        elif task_name == "memory_search" and isinstance(data, dict):
            self.btn_search_memory.setEnabled(True)
            self._update_memory_ui(data)
        elif task_name == "joey_analyze" and isinstance(data, dict):
            self.btn_joey_analyze.setEnabled(True)
            self.btn_joey_analyze.setText("🔍 Analyze Current Setup (ჯოი)")
            wf = data.get("waterfall") if isinstance(data.get("waterfall"), dict) else {}
            if wf:
                self._update_waterfall_ui(wf)
            reply_txt = str(data.get("text") or data.get("analysis_text") or wf.get("rationale_ka") or "")
            if reply_txt:
                self._append_joey_chat_message(
                    "🤖 ჯოი (Gemini Quant CIO — Setup Analysis)",
                    reply_txt,
                    is_user=False,
                )
            auto_exec = data.get("autonomous_execution")
            if isinstance(auto_exec, dict) and auto_exec.get("executed"):
                self._append_joey_chat_message(
                    "⚡ 100% Autonomous Mode Execution",
                    f"ჯოიმ ავტომატურად გახსნა დემო პოზიცია: {auto_exec.get('side')} {auto_exec.get('volume')} ლოტი "
                    f"(Ticket #{auto_exec.get('ticket')}) @ {auto_exec.get('entry_price')}",
                    is_user=False,
                )
                self._request_open_positions()
        elif task_name == "joey_chat" and isinstance(data, dict):
            self.btn_joey_send.setEnabled(True)
            self.btn_joey_send.setText("📤 გაგზავნა (Send to ჯოი)")
            wf = data.get("waterfall") if isinstance(data.get("waterfall"), dict) else {}
            if wf:
                self._update_waterfall_ui(wf)
            reply_txt = str(data.get("text") or data.get("reply") or "")
            if reply_txt:
                self._append_joey_chat_message(
                    f"🤖 ჯოი ({data.get('model') or 'Gemini CIO'})",
                    reply_txt,
                    is_user=False,
                )
        elif task_name == "gemini_autonomous" and isinstance(data, dict):
            en = bool(data.get("autonomous_mode", False))
            self.status_message_label.setText(
                f"Gemini CIO 100% Autonomous Mode: {'ENABLED' if en else 'DISABLED'}"
            )
        elif task_name == "telegram_command" and isinstance(data, dict):
            reply_txt = str(data.get("reply") or "")
            cmd = str(data.get("command") or "/status")
            if reply_txt:
                self._append_joey_chat_message(
                    f"📱 Telegram Remote ({cmd})",
                    reply_txt,
                    is_user=False,
                )
            self._request_open_positions()
        elif task_name == "demo_manage" and isinstance(data, dict):
            actions = data.get("actions_taken") if isinstance(data.get("actions_taken"), list) else []
            if actions:
                for act in actions:
                    if isinstance(act, dict):
                        self._append_log_entry(
                            self.log_all,
                            f"POS.{act.get('event_type', 'MANAGED')}",
                            str(act.get("reason") or ""),
                            "#38bdf8",
                        )

    def _on_http_failed(self, task_name: str, error_text: str) -> None:
        if task_name == "health":
            self.api_status_badge.setText("● API OFFLINE")
            self._apply_pill_style(self.api_status_badge, "offline")
            self.status_api_label.setText("API: OFFLINE")
            self.status_message_label.setText(f"API unreachable ({self.api_url}): {error_text}")
        elif task_name in {"strategies_list", "strategy_save", "strategy_load", "strategy_backtest", "strategy_walk_forward"}:
            self.strategy_lab_panel.set_backend_error(task_name, error_text)
            self.status_message_label.setText(f"Strategy Lab request failed: {error_text}")
        elif task_name in {"controls_get", "controls_update"}:
            self.phase2_controls_tab.set_status(f"Controls API offline: {error_text}", error=True)
            if task_name == "controls_update":
                self.status_message_label.setText("Quant Gate controls kept as local UI state; API is offline.")
        elif task_name in {"decision_history", "decision_history_summary"}:
            self.status_message_label.setText("Decision audit API offline; showing the last local view.")
        elif task_name in {"bots_add", "bot_enabled", "bot_remove"}:
            self.phase2_fleet_tab.set_action_status(f"Fleet API action failed: {error_text}", error=True)
            self.status_message_label.setText(f"Fleet action failed: {error_text}")
        elif task_name in {"scan_manual", "scan_automatic"}:
            self.btn_manual_scan.setEnabled(True)
            self.btn_manual_scan.setText("▶ SCAN SYMBOL (MT5 → AI)")
            self.status_message_label.setText(f"Scan request rejected: {error_text}")
            self._append_log_entry(self.log_all, "SCAN.ERROR", error_text, "#ef4444")
        elif task_name == "screener_run":
            self.btn_run_screener.setEnabled(True)
            self.btn_run_screener.setText("🔎 SCAN TOP 8 PAIRS")
            self.status_message_label.setText(f"8-Pair Screener error: {error_text}")
        elif task_name == "demo_execute":
            self.btn_execute_demo.setEnabled(True)
            self.btn_execute_demo.setText("⚡ EXECUTE DEMO TRADE")
            self.status_message_label.setText(f"Demo Trade Execution blocked/failed: {error_text}")
            self._append_log_entry(self.log_all, "DEMO.ERROR", error_text, "#ef4444")
        elif task_name == "demo_close":
            self.btn_close_demo.setEnabled(True)
            self.status_message_label.setText(f"Demo Close failed: {error_text}")
        elif task_name == "memory_search":
            self.btn_search_memory.setEnabled(True)
        elif task_name == "joey_analyze":
            self.btn_joey_analyze.setEnabled(True)
            self.btn_joey_analyze.setText("🔍 Analyze Current Setup (ჯოი)")
            self._append_joey_chat_message("⚠️ System", f"Setup analysis error: {error_text}", is_user=False)
        elif task_name == "joey_chat":
            self.btn_joey_send.setEnabled(True)
            self.btn_joey_send.setText("📤 გაგზავნა (Send to ჯოი)")
            self._append_joey_chat_message("⚠️ System", f"Chat error: {error_text}", is_user=False)

    # -----------------------------------------------------------------------
    # UI State Updaters
    # -----------------------------------------------------------------------
    def _update_health_ui(self, data: dict[str, Any]) -> None:
        version = data.get("version", "0.1.0")
        self.api_status_badge.setText(f"● API ONLINE v{version}")
        self._apply_pill_style(self.api_status_badge, "online")
        self.status_api_label.setText(f"API: ONLINE v{version}")

        gemini_obj = data.get("gemini") if isinstance(data.get("gemini"), dict) else {}
        if gemini_obj.get("live"):
            active_m = gemini_obj.get("active_model") or "live"
            self.gemini_status_badge.setText(f"● GEMINI: {active_m}")
            self._apply_pill_style(self.gemini_status_badge, "online")
        elif data.get("gemini_key_configured"):
            self.gemini_status_badge.setText("● GEMINI .ENV: READY")
            self._apply_pill_style(self.gemini_status_badge, "online")
        else:
            self.gemini_status_badge.setText("● GEMINI .ENV: DRY-RUN")
            self._apply_pill_style(self.gemini_status_badge, "warning")

        if "autonomous_mode" in data:
            self._apply_autonomous_ui_state(bool(data["autonomous_mode"]))

        active_scan = data.get("active_scan_id")
        self._active_scan_id = str(active_scan) if active_scan else None
        if self._active_scan_id:
            self.scan_engine_value.setText(f"ACTIVE ({self._active_scan_id[:8]})")
            self.btn_manual_scan.setEnabled(False)
            self.btn_manual_scan.setText(f"⏳ SCANNING ({self._active_scan_id[:6]})...")
        else:
            self.scan_engine_value.setText("READY / IDLE")
            self.btn_manual_scan.setEnabled(True)
            self.btn_manual_scan.setText("▶ SCAN SYMBOL (MT5 → AI)")

        memory = data.get("memory") or {}
        if isinstance(memory, dict):
            backend = str(memory.get("backend", "jsonl")).upper()
            count = memory.get("count")
            count_str = f" ({count})" if count is not None else ""
            self.memory_status_badge.setText(f"MEMORY: {backend}{count_str}")
            self._apply_pill_style(self.memory_status_badge, "info")

        controls = data.get("controls")
        if isinstance(controls, dict):
            self._update_phase2_controls_ui(controls)

    def _update_phase2_controls_ui(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self.phase2_controls_tab.set_state(data)
        source = data.get("metadata", {}).get("source") if isinstance(data.get("metadata"), dict) else None
        if source:
            self.phase2_controls_tab.set_status(f"Loaded controls from API ({source}).")

    def _update_decision_history_ui(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self.phase2_audit_tab.update_records(data)


    def _update_mt5_status_ui(self, data: dict[str, Any]) -> None:
        connected = bool(data.get("connected", False))
        acc = data.get("account") if isinstance(data.get("account"), dict) else None
        if connected and acc is not None:
            self._latest_mt5_account = acc
            server = str(acc.get("server") or "MT5")
            company = str(acc.get("company") or "Broker")
            login = acc.get("login", "--")
            trade_mode = str(acc.get("trade_mode") or "DEMO")
            currency = str(acc.get("currency") or "USD")
            balance = float(acc.get("balance") or 0.0)
            equity = float(acc.get("equity") or 0.0)
            free_margin = float(acc.get("margin_free") or 0.0)
            leverage = int(acc.get("leverage") or 0)
            floating_pnl = float(
                acc.get("live_floating_pnl")
                if acc.get("live_floating_pnl") is not None
                else (acc.get("profit") or (equity - balance))
            )
            floating_pct = float(
                acc.get("live_floating_pnl_pct")
                if acc.get("live_floating_pnl_pct") is not None
                else ((floating_pnl / balance) * 100.0 if balance > 0 else 0.0)
            )

            self.mt5_status_badge.setText(f"● MT5: {server}")
            self._apply_pill_style(self.mt5_status_badge, "online")
            self.status_mt5_health_label.setText(f"MT5 HEALTH: OK ({server})")
            self.mt5_broker_value.setText(f"{company} ({server})")
            self.mt5_account_value.setText(f"#{login} [{trade_mode}] — {acc.get('name', '')}")
            self.mt5_balance_value.setText(f"{currency} {balance:,.2f}  |  Eq: {equity:,.2f}")
            self._update_header_live_pnl_label(floating_pnl, floating_pct)
            self.mt5_margin_value.setText(f"Free: {currency} {free_margin:,.2f}  |  1:{leverage}")

            # Populate dynamic broker symbols while preserving flagship Crypto L2 pairs
            broker_symbols = data.get("symbols")
            if isinstance(broker_symbols, list) and len(broker_symbols) > 0:
                current_items = [self.ticker_combo.itemText(i) for i in range(self.ticker_combo.count())]
                if len(current_items) <= 16 and len(broker_symbols) > len(current_items):
                    current_sel = self.ticker_combo.currentText().strip() or "XAUUSD"
                    merged_symbols: list[str] = []
                    seen_syms: set[str] = set()
                    for s in ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD", "SOLUSD", *broker_symbols]:
                        s_str = str(s).strip()
                        if s_str and s_str not in seen_syms:
                            seen_syms.add(s_str)
                            merged_symbols.append(s_str)
                    self._updating_symbol_combo = True
                    try:
                        self.ticker_combo.clear()
                        self.ticker_combo.addItems(merged_symbols)
                        self.ticker_combo.setCurrentText(current_sel)
                    finally:
                        self._updating_symbol_combo = False
        else:
            err = str(data.get("error") or "Offline")
            self.mt5_status_badge.setText("● MT5 OFFLINE")
            self._apply_pill_style(self.mt5_status_badge, "offline")
            self.status_mt5_health_label.setText("MT5 HEALTH: OFFLINE")
            self.mt5_broker_value.setText(f"MT5 Not Connected ({err[:36]})")

    def _update_mt5_symbol_ui(self, data: dict[str, Any]) -> None:
        first_sync = self._latest_symbol_spec.get("_synced") is not True
        data["_synced"] = True
        self._latest_symbol_spec = data

        sym = str(data.get("symbol") or "XAUUSD")
        digits = int(data.get("digits") or 2)
        bid = float(data.get("bid") or 0.0)
        ask = float(data.get("ask") or 0.0)
        spread = float(data.get("spread_points") or 0.0)
        atr_m5 = float(data.get("atr_m5") or 0.0)

        self.live_quote_badge.setText(
            f"{sym}  •  BID: {bid:.{digits}f}  |  ASK: {ask:.{digits}f}  |  "
            f"SPREAD: {spread:.1f} pts  |  M5 ATR: {atr_m5:.{digits}f}"
        )
        if hasattr(self, "phase2_live_tab"):
            self.phase2_live_tab.set_quote_status(
                f"{sym}  BID {bid:.{digits}f}  |  ASK {ask:.{digits}f}  |  ATR {atr_m5:.{digits}f}"
            )
        if spread > 0 and not self.spread_spin.hasFocus():
            self.spread_spin.setValue(spread)

        if first_sync:
            self._sync_calculators_from_mt5()

    def _update_l2_orderbook_ui(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        symbol = str(data.get("symbol") or "").strip().upper()
        active_symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
        if not symbol or symbol != active_symbol:
            return
        self._latest_l2_snapshot = data
        source = str(data.get("source") or "mt5_l2_dom").upper()
        digits = int(data.get("digits") or 2)
        best_bid = self._safe_float(data.get("best_bid", 0.0))
        best_ask = self._safe_float(data.get("best_ask", 0.0))
        mid_price = self._safe_float(data.get("mid_price", (best_bid + best_ask) / 2.0))
        spread = self._safe_float(data.get("spread", max(0.0, best_ask - best_bid)))
        total_bid_vol = self._safe_float(data.get("total_bid_volume", 0.0))
        total_ask_vol = self._safe_float(data.get("total_ask_volume", 0.0))
        bid_pct = self._safe_float(data.get("bid_pressure_pct", 50.0))
        ask_pct = self._safe_float(data.get("ask_pressure_pct", 50.0))
        imbalance_ratio = self._safe_float(data.get("imbalance_ratio", 1.0))
        l2_dir = str(data.get("l2_direction") or "NEUTRAL").upper()
        conf_raw = self._safe_float(data.get("confluence_score", 50.0))
        conf_score = conf_raw * 100.0 if 0.0 < conf_raw <= 1.0 else conf_raw
        veto_buy = bool(data.get("veto_buy", False))
        veto_sell = bool(data.get("veto_sell", False))
        veto_reason = str(data.get("veto_reason") or "")

        # Update top badges in L2 tab
        self.l2_source_badge.setText(f"SOURCE: {source}")
        self._apply_pill_style(self.l2_source_badge, "online" if "WS" in source or "DOM" in source else "info")

        self.l2_direction_badge.setText(f"L2 FLOW: {l2_dir}")
        if l2_dir == "BUY":
            self._apply_pill_style(self.l2_direction_badge, "online")
        elif l2_dir == "SELL":
            self._apply_pill_style(self.l2_direction_badge, "offline")
        else:
            self._apply_pill_style(self.l2_direction_badge, "warning")

        self.l2_confluence_badge.setText(f"L2 CONFLUENCE: {conf_score:.1f}%")
        self._apply_pill_style(self.l2_confluence_badge, "online" if conf_score >= 65.0 else "info")

        if veto_buy or veto_sell:
            veto_tag = "VETO BUY (SELL WALL)" if veto_buy and not veto_sell else (
                "VETO SELL (BUY WALL)" if veto_sell and not veto_buy else "VETO BUY & SELL"
            )
            self.l2_veto_badge.setText(f"⛔ {veto_tag}")
            self.l2_veto_badge.setToolTip(veto_reason)
            self._apply_pill_style(self.l2_veto_badge, "offline")
        else:
            self.l2_veto_badge.setText("✅ GATE VETO: CLEAR")
            self.l2_veto_badge.setToolTip("No opposing L2 liquidity wall blocking execution")
            self._apply_pill_style(self.l2_veto_badge, "online")

        raw_bw = data.get("nearest_buy_wall") or data.get("strongest_buy_wall")
        raw_sw = data.get("nearest_sell_wall") or data.get("strongest_sell_wall")
        buy_wall = raw_bw if isinstance(raw_bw, dict) else None
        sell_wall = raw_sw if isinstance(raw_sw, dict) else None
        bw_txt = f"{float(buy_wall['price']):.{digits}f} ({float(buy_wall['volume']):,.1f})" if buy_wall else "None"
        sw_txt = f"{float(sell_wall['price']):.{digits}f} ({float(sell_wall['volume']):,.1f})" if sell_wall else "None"
        self.l2_walls_summary_label.setText(f"🧱 Buy Wall: {bw_txt}  |  🧱 Sell Wall: {sw_txt}")

        self.l2_imbalance_label.setText(
            f"BID LIQUIDITY: {bid_pct:.1f}% ({total_bid_vol:,.1f})   ◄═══ IMBALANCE {imbalance_ratio:+.2f} ═══►   "
            f"ASK LIQUIDITY: {ask_pct:.1f}% ({total_ask_vol:,.1f})"
        )
        self.l2_pressure_bar.setValue(int(round(max(0.0, min(100.0, bid_pct)))))
        self.l2_pressure_bar.setFormat(f"BID {bid_pct:.1f}%  vs  ASK {ask_pct:.1f}%")

        # Also update 5th layer bar on Tab 1
        veto_short = "VETO ACTIVE" if (veto_buy or veto_sell) else "CLEAR"
        self.layer_l2_val.setText(
            f"{l2_dir}  (Bid:{bid_pct:.0f}%/Ask:{ask_pct:.0f}% | Conf:{conf_score:.0f}% | {veto_short})"
        )
        self._colorize_inline_status(self.layer_l2_val, "BUY" if l2_dir == "BUY" else ("SELL" if l2_dir == "SELL" else "ABSTAIN"))
        self.layer_l2_bar.setValue(int(round(max(0.0, min(100.0, conf_score)))))

        # Update Header quote badge if crypto pair is active
        if best_bid > 0 and best_ask > 0 and symbol.upper() != str(self._latest_symbol_spec.get("symbol", "")).upper():
            self.live_quote_badge.setText(
                f"{symbol}  •  BID: {best_bid:.{digits}f}  |  ASK: {best_ask:.{digits}f}  |  "
                f"SPREAD: {spread:.{digits}f}  |  SOURCE: {source}"
            )

        # Render Visual DOM / Orderbook Depth Ladder (21 rows)
        self.l2_ladder_title.setText(f"VISUAL DOM / ORDERBOOK DEPTH LADDER — {symbol} ({source})")
        self.l2_spread_info_label.setText(
            f"BEST BID: {best_bid:.{digits}f}  |  MID: {mid_price:.{digits}f}  |  BEST ASK: {best_ask:.{digits}f}  |  SPREAD: {spread:.{digits}f}"
        )

        asks_raw = data.get("asks") if isinstance(data.get("asks"), list) else []
        bids_raw = data.get("bids") if isinstance(data.get("bids"), list) else []
        asks_sorted = sorted(
            [a for a in asks_raw if isinstance(a, dict)],
            key=lambda x: float(x.get("price", 0.0)),
        )[:10]
        bids_sorted = sorted(
            [b for b in bids_raw if isinstance(b, dict)],
            key=lambda x: float(x.get("price", 0.0)),
            reverse=True,
        )[:10]

        all_vols = [
            float(item.get("volume", 0.0))
            for item in [*asks_sorted, *bids_sorted]
            if float(item.get("volume", 0.0)) > 0
        ]
        max_vol = max(all_vols) if all_vols else 1.0

        # Pad to 10 levels each so the ladder is always 21 fixed rows
        while len(asks_sorted) < 10:
            asks_sorted.append({})
        while len(bids_sorted) < 10:
            bids_sorted.append({})

        # Top 10 rows: Farthest Ask (L10) down to Nearest Ask (L1)
        asks_display = list(reversed(asks_sorted))
        for row_idx, ask_lvl in enumerate(asks_display):
            lvl_num = 10 - row_idx
            self._populate_dom_row(row_idx, f"ASK L{lvl_num}", ask_lvl, is_bid=False, digits=digits, max_vol=max_vol)

        # Row 10: Center Spread / Mid-Price Divider
        spread_items = [
            QTableWidgetItem("◆ SPREAD / MID"),
            QTableWidgetItem(f"{mid_price:,.{digits}f}"),
            QTableWidgetItem(f"Δ {spread:.{digits}f}"),
            QTableWidgetItem(f"Imb: {imbalance_ratio:+.2f}"),
            QTableWidgetItem(f"◄══ BID {bid_pct:.0f}%  |  ASK {ask_pct:.0f}% ══►"),
            QTableWidgetItem(f"{l2_dir} ({conf_score:.0f}%)"),
        ]
        divider_bg = QColor("#17253b")
        divider_fg = QColor("#38bdf8")
        for col_idx, cell in enumerate(spread_items):
            cell.setBackground(divider_bg)
            cell.setForeground(divider_fg)
            cell.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            if col_idx in {1, 2, 3, 4}:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.l2_dom_table.setItem(10, col_idx, cell)

        # Bottom 10 rows (11..20): Nearest Bid (L1) down to Farthest Bid (L10)
        for idx, bid_lvl in enumerate(bids_sorted):
            row_idx = 11 + idx
            lvl_num = idx + 1
            self._populate_dom_row(row_idx, f"BID L{lvl_num}", bid_lvl, is_bid=True, digits=digits, max_vol=max_vol)

        # Process real-time alerts from snapshot
        alerts_list = data.get("alerts") if isinstance(data.get("alerts"), list) else []
        for alert_item in alerts_list:
            if isinstance(alert_item, dict):
                self._append_l2_alert(alert_item)
        self._update_phase3_l2_dock(data, symbol=symbol, digits=digits)

    def _update_phase3_l2_dock(self, data: dict[str, Any], *, symbol: str, digits: int) -> None:
        """Render a compact HFT-style L2 telemetry view in the dock panel."""

        panel = getattr(self, "l2_orderbook_panel", None)
        if panel is None:
            return
        best_bid = self._safe_float(data.get("best_bid", 0.0))
        best_ask = self._safe_float(data.get("best_ask", 0.0))
        mid_price = self._safe_float(data.get("mid_price", (best_bid + best_ask) / 2.0))
        spread = self._safe_float(data.get("spread", max(0.0, best_ask - best_bid)))
        bid_pct = self._safe_float(data.get("bid_pressure_pct", 50.0))
        ask_pct = self._safe_float(data.get("ask_pressure_pct", 50.0))
        imbalance = self._safe_float(data.get("imbalance_ratio", 1.0))
        direction = str(data.get("l2_direction") or "NEUTRAL").upper()
        source = str(data.get("source") or "mt5_l2_dom")
        veto = bool(data.get("veto_buy") or data.get("veto_sell"))
        veto_reason = str(data.get("veto_reason") or "CLEAR")
        buy_wall = data.get("nearest_buy_wall") or data.get("strongest_buy_wall") or {}
        sell_wall = data.get("nearest_sell_wall") or data.get("strongest_sell_wall") or {}
        alerts = data.get("alerts") if isinstance(data.get("alerts"), list) else []
        alert_lines = [
            str(item.get("message") or item.get("alert_type") or "alert")
            for item in alerts[-5:]
            if isinstance(item, dict)
        ]
        lines = [
            f"L2 ORDERBOOK / DOM TELEMETRY — {symbol}",
            f"SOURCE: {source}    DIRECTION: {direction}    GATE: {'VETO' if veto else 'CLEAR'}",
            f"BEST BID: {best_bid:.{digits}f}    MID: {mid_price:.{digits}f}    BEST ASK: {best_ask:.{digits}f}",
            f"SPREAD: {spread:.{digits}f}    IMBALANCE: {imbalance:+.2f}",
            f"BID PRESSURE: {bid_pct:.1f}%    ASK PRESSURE: {ask_pct:.1f}%",
            f"BUY WALL: {buy_wall.get('price', '--')} / {buy_wall.get('volume', '--')}",
            f"SELL WALL: {sell_wall.get('price', '--')} / {sell_wall.get('volume', '--')}",
            f"VETO REASON: {veto_reason}",
        ]
        if alert_lines:
            lines.append("RECENT ALERTS:")
            lines.extend(f"  • {line}" for line in alert_lines)
        panel.setPlainText("\n".join(lines))

    def _populate_dom_row(
        self,
        row_idx: int,
        side_label: str,
        level_data: dict[str, Any],
        *,
        is_bid: bool,
        digits: int,
        max_vol: float,
    ) -> None:
        if not level_data or not level_data.get("price"):
            for col_idx in range(6):
                empty_item = QTableWidgetItem("--" if col_idx > 0 else side_label)
                empty_item.setForeground(QColor("#475569"))
                self.l2_dom_table.setItem(row_idx, col_idx, empty_item)
            return

        price = float(level_data.get("price", 0.0))
        vol = float(level_data.get("volume", 0.0))
        cum_vol = float(level_data.get("cumulative_volume") or level_data.get("cumulative") or vol)
        vol_ratio = float(level_data.get("volume_ratio") or level_data.get("ratio_to_mean") or 1.0)
        tag = str(level_data.get("tag") or "NORMAL").upper()
        is_whale = bool(level_data.get("is_whale", False)) or ("WHALE" in tag)
        is_wall = bool(level_data.get("is_wall", False)) or ("WALL" in tag)

        rel_pct = max(0.0, min(1.0, vol / max(max_vol, 1e-9)))
        bar_blocks = max(1, int(round(rel_pct * 16)))
        bar_str = ("█" * bar_blocks) + ("░" * (16 - bar_blocks)) + f"  {rel_pct * 100:4.0f}% ({vol_ratio:.1f}x)"

        if is_whale:
            tag_display = "🐋 WHALE WALL"
        elif is_wall:
            tag_display = "🧱 BUY WALL" if is_bid else "🧱 SELL WALL"
        elif "ICEBERG" in tag:
            tag_display = "🧊 ICEBERG"
        else:
            tag_display = "NORMAL"

        # Heatmap color intensity based on relative volume
        alpha_boost = int(22 + rel_pct * 75)
        if is_whale:
            row_bg = QColor(6, 78, 59, 175) if is_bid else QColor(127, 29, 29, 175)
        elif is_wall:
            row_bg = QColor(6, 95, 70, 135) if is_bid else QColor(153, 27, 27, 135)
        else:
            row_bg = QColor(16, 185, 129, alpha_boost) if is_bid else QColor(239, 68, 68, alpha_boost)

        side_fg = QColor("#34d399") if is_bid else QColor("#f87171")
        price_fg = QColor("#ecfdf5") if is_bid else QColor("#fef2f2")
        tag_fg = QColor("#fbbf24") if (is_whale or is_wall or "ICEBERG" in tag) else QColor("#94a3b8")

        cells = [
            QTableWidgetItem(side_label),
            QTableWidgetItem(f"{price:,.{digits}f}"),
            QTableWidgetItem(f"{vol:,.2f}"),
            QTableWidgetItem(f"{cum_vol:,.2f}"),
            QTableWidgetItem(bar_str),
            QTableWidgetItem(tag_display),
        ]
        for col_idx, cell in enumerate(cells):
            cell.setBackground(row_bg)
            if col_idx == 0:
                cell.setForeground(side_fg)
                cell.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            elif col_idx == 1:
                cell.setForeground(price_fg)
                cell.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            elif col_idx in {2, 3}:
                cell.setForeground(QColor("#e2e8f0"))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            elif col_idx == 4:
                cell.setForeground(side_fg)
                cell.setFont(QFont("Consolas", 10))
            elif col_idx == 5:
                cell.setForeground(tag_fg)
                if is_whale or is_wall or "ICEBERG" in tag:
                    cell.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            self.l2_dom_table.setItem(row_idx, col_idx, cell)

    def _append_l2_alert(self, alert: dict[str, Any]) -> None:
        symbol = str(alert.get("symbol") or "XAUUSD")
        alert_type = str(alert.get("alert_type") or alert.get("kind") or "WALL")
        side = str(alert.get("side") or "")
        price = self._safe_float(alert.get("price", 0.0))
        vol = self._safe_float(alert.get("volume", 0.0))
        severity = str(alert.get("severity") or "INFO").upper()
        msg = str(alert.get("message") or "")

        dedup_key = f"{symbol}:{alert_type}:{side}:{price:.2f}:{int(vol)}"
        if dedup_key in self._l2_alerts_seen:
            return
        self._l2_alerts_seen.add(dedup_key)
        if len(self._l2_alerts_seen) > 300:
            self._l2_alerts_seen.clear()

        self._l2_alert_count += 1
        self.l2_alerts_count_label.setText(f"ALERTS: {self._l2_alert_count}")

        color = "#ef4444" if severity == "CRITICAL" else ("#f59e0b" if severity == "WARNING" else "#38bdf8")
        tag = f"L2.{alert_type}"
        self._append_log_entry(self.l2_alerts_feed, tag, msg, color)

    def _clear_l2_alerts_feed(self) -> None:
        self.l2_alerts_feed.clear()
        self._l2_alerts_seen.clear()
        self._l2_alert_count = 0
        self.l2_alerts_count_label.setText("ALERTS: 0")

    def _update_bots_ui(self, data: dict[str, Any]) -> None:
        active_id = str(data.get("active_bot") or "unknown")
        bots = data.get("bots") or []
        active_count = 0
        for bot in bots:
            if not isinstance(bot, dict):
                continue
            enabled_value = bot.get("enabled")
            if isinstance(enabled_value, bool):
                enabled = enabled_value
            else:
                enabled = str(bot.get("status") or "enabled").lower() not in {"disabled", "off"}
            if enabled:
                active_count += 1
        if active_count == 0 and not bots and active_id != "unknown":
            active_count = 1
        self._active_bot_count = active_count
        self.status_active_bot_label.setText(f"ACTIVE BOTS: {active_count}")
        display_name = active_id
        bot_status = "demo_only"
        for bot in bots:
            if isinstance(bot, dict) and bot.get("id") == active_id:
                display_name = str(bot.get("display_name") or active_id)
                bot_status = str(bot.get("status") or "demo_only")
                break
        self.active_bot_value.setText(display_name)
        self.bot_mode_value.setText(f"{bot_status.upper()} [SAFE DEMO]")
        if hasattr(self, "phase2_fleet_tab"):
            self.phase2_fleet_tab.set_status(data)

    def _update_gates_and_prediction_ui(self, data: dict[str, Any]) -> None:
        active_symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"

        def active_payload(value: object) -> dict[str, Any]:
            if not isinstance(value, dict):
                return {}
            identity = str(
                value.get("symbol")
                or value.get("user_symbol")
                or value.get("requested_symbol")
                or value.get("ticker")
                or ""
            ).strip().upper()
            return value if identity == active_symbol else {}

        kronos_gate = active_payload(data.get("kronos_matrix"))
        ta_gate = active_payload(data.get("tradingagents"))
        l2_orderflow = active_payload(data.get("l2_orderflow"))
        waterfall_obj = active_payload(data.get("waterfall"))
        tg_status = (
            data.get("telegram_status")
            if isinstance(data.get("telegram_status"), dict)
            else (data.get("telegram") if isinstance(data.get("telegram"), dict) else {})
        )
        combined_report = active_payload(data.get("combined_report"))
        kronos_detail = active_payload(data.get("kronos_report")) or active_payload(data.get("_kronos_report_detail"))
        market_range = (
            data.get("_market_price_range")
            if isinstance(data.get("_market_price_range"), dict)
            else {}
        )

        if l2_orderflow and not self._latest_l2_snapshot:
            self._update_l2_orderbook_ui(l2_orderflow)
        if waterfall_obj:
            self._update_waterfall_ui(waterfall_obj)
        if tg_status:
            if tg_status.get("configured"):
                self.telegram_status_badge.setText("● TELEGRAM: ONLINE")
                self._apply_pill_style(self.telegram_status_badge, "online")
            else:
                self.telegram_status_badge.setText("● TELEGRAM: LOCAL SIM (.env optional)")
                self._apply_pill_style(self.telegram_status_badge, "info")

        primary_action = str(
            combined_report.get("action")
            or kronos_gate.get("action")
            or ta_gate.get("action")
            or "ABSTAIN"
        ).upper()
        primary_conf = self._safe_float(
            combined_report.get(
                "confidence",
                kronos_detail.get("confidence", kronos_gate.get("confidence", 0.0)),
            )
        )
        ta_action = str(ta_gate.get("action") or "ABSTAIN").upper()
        ta_conf = self._safe_float(ta_gate.get("confidence", 0.0))
        km_action = str(kronos_detail.get("action") or kronos_gate.get("action") or "ABSTAIN").upper()
        km_conf = self._safe_float(kronos_detail.get("confidence", kronos_gate.get("confidence", 0.0)))

        # Hero Gate Update
        self._style_gate_hero(primary_action, max(primary_conf, km_conf))

        # Sub-layer objects
        decision_obj = kronos_detail.get("decision") if isinstance(kronos_detail.get("decision"), dict) else {}
        matrix_obj = decision_obj.get("matrix") if isinstance(decision_obj.get("matrix"), dict) else {}
        analog_obj = decision_obj.get("analog") if isinstance(decision_obj.get("analog"), dict) else {}
        forecast_obj = decision_obj.get("forecast") if isinstance(decision_obj.get("forecast"), dict) else {}

        # Update Kronos Prediction Card
        f_dir_hero = str(forecast_obj.get("direction") or km_action).upper()
        self._style_direction_label(self.kronos_dir_label, km_action if km_action != "ABSTAIN" else f_dir_hero)
        km_score = self._safe_float(kronos_detail.get("score", 0.0))
        conf_pct = max(0.0, min(100.0, km_conf * 100.0))
        self.kronos_conf_label.setText(f"{conf_pct:.1f}%  (Score: {km_score:.2f})")
        self.kronos_conf_bar.setValue(int(round(conf_pct)))

        model_name = str(kronos_detail.get("model") or "NeoQuasar/Kronos-small")
        device_name = str(kronos_detail.get("device") or "cpu")
        self.fc_model_badge.setText(f"{model_name} [{device_name}]")

        # 1. Matrix Layer Bar
        if matrix_obj:
            m_dir = str(matrix_obj.get("direction", "ABSTAIN")).upper()
            m_score = self._safe_float(matrix_obj.get("score", 0.0))
            b_score = self._safe_float(matrix_obj.get("buy_score", 0.0))
            s_score = self._safe_float(matrix_obj.get("sell_score", 0.0))
            self.layer_matrix_val.setText(f"{m_dir}  (Score: {m_score:.1f}/8.0 | Buy:{b_score:.0f} Sell:{s_score:.0f})")
            self._colorize_inline_status(self.layer_matrix_val, m_dir)
            self.layer_matrix_bar.setValue(int(min(100.0, (m_score / 8.0) * 100.0)))
        else:
            self.layer_matrix_val.setText(f"{km_action}  (Awaiting scan)")
            self._colorize_inline_status(self.layer_matrix_val, km_action)
            self.layer_matrix_bar.setValue(0)

        # 2. Historical Analogue Layer Bar
        if analog_obj:
            a_dir = str(analog_obj.get("direction", "ABSTAIN")).upper()
            a_samples = int(self._safe_float(analog_obj.get("sample_count", 0)))
            a_wr = analog_obj.get("win_rate")
            wr_pct = float(a_wr) * 100.0 if a_wr is not None else 0.0
            self.layer_analog_val.setText(f"{a_dir}  (Neighbors: {a_samples}/8 | WinRate: {wr_pct:.1f}%)")
            self._colorize_inline_status(self.layer_analog_val, a_dir)
            self.layer_analog_bar.setValue(int(min(100.0, wr_pct)))
            if wr_pct > 0 and not self.calc_winrate_spin.hasFocus():
                self.calc_winrate_spin.setValue(wr_pct)
        else:
            self.layer_analog_val.setText("ABSTAIN  (24-bar / 8-NN)")
            self._colorize_inline_status(self.layer_analog_val, "ABSTAIN")
            self.layer_analog_bar.setValue(0)

        # 3. Kronos Transformer Layer Bar
        if forecast_obj:
            f_dir = str(forecast_obj.get("direction", "ABSTAIN")).upper()
            f_atr = forecast_obj.get("move_atr")
            f_agree = self._safe_float(forecast_obj.get("path_agreement", 0.0)) * 100.0
            atr_str = f"{float(f_atr):+.2f} ATR" if f_atr is not None else "n/a"
            self.layer_forecast_val.setText(f"{f_dir}  (Move: {atr_str} | Path Agree: {f_agree:.0f}%)")
            self._colorize_inline_status(self.layer_forecast_val, f_dir)
            self.layer_forecast_bar.setValue(int(min(100.0, f_agree)))
        else:
            self.layer_forecast_val.setText(f"{km_action}  (12-bar K-line)")
            self._colorize_inline_status(self.layer_forecast_val, km_action)
            self.layer_forecast_bar.setValue(0)

        # 4. TradingAgents & Cost Gate Bar
        hard_blocks = decision_obj.get("hard_blocks") if isinstance(decision_obj.get("hard_blocks"), list) else []
        cost_blocked = any("cost" in str(b).lower() or "spread" in str(b).lower() for b in hard_blocks)
        cost_txt = "COST VETO" if cost_blocked else "COST OK"
        self.layer_ta_val.setText(f"TA: {ta_action} ({ta_conf * 100:.0f}%)  |  {cost_txt}")
        self._colorize_inline_status(self.layer_ta_val, ta_action if not cost_blocked else "SELL")
        self.layer_ta_bar.setValue(100 if (ta_action in {"BUY", "SELL"} and not cost_blocked) else (50 if not cost_blocked else 15))

        # Price Range Calculation
        metrics = matrix_obj.get("metrics") if isinstance(matrix_obj.get("metrics"), dict) else {}
        m5_low = metrics.get("m5_range_low") or self._latest_symbol_spec.get("range_20_low") or market_range.get("range_20_low")
        m5_high = metrics.get("m5_range_high") or self._latest_symbol_spec.get("range_20_high") or market_range.get("range_20_high")
        m5_close = metrics.get("m5_close") or self._latest_symbol_spec.get("last_close") or market_range.get("close")

        if m5_low is not None and m5_high is not None:
            low_f = float(m5_low)
            high_f = float(m5_high)
            close_f = float(m5_close) if m5_close is not None else (low_f + high_f) / 2.0
            self.kronos_range_label.setText(f"{low_f:,.2f} – {high_f:,.2f}")
            self.kronos_range_sub.setText(
                f"Close: {close_f:,.2f}  |  Bar: {kronos_detail.get('last_bar') or '--'}"
            )
        else:
            symbol_txt = str(kronos_gate.get("symbol") or "XAUUSD")
            self.kronos_range_label.setText(f"{symbol_txt} [READY]")
            self.kronos_range_sub.setText("Click 'SCAN SYMBOL' to compute M5 breakout box")

        # Subtitle & Plain-Language Diagnostics
        a_dir_txt = str(analog_obj.get("direction", "ABSTAIN")).upper()
        f_dir_txt = str(forecast_obj.get("direction", "ABSTAIN")).upper()
        m_dir_txt = str(matrix_obj.get("direction", "ABSTAIN")).upper()
        l2_snap = l2_orderflow or self._latest_l2_snapshot
        l2_dir_txt = str(l2_snap.get("l2_direction", "NEUTRAL")).upper()
        wf_tier = str(waterfall_obj.get("risk_tier") or "--")
        self.gate_sub_label.setText(
            f"MATRIX: {m_dir_txt}  •  ANALOG: {a_dir_txt}  •  KRONOS: {f_dir_txt}  •  TA: {ta_action}  •  L2 DOM: {l2_dir_txt}  •  CIO: {wf_tier}"
        )

        diag_lines: list[str] = []
        if primary_action in {"BUY", "SELL"}:
            diag_lines.append(f"✅ სრული თანხვედრა ({primary_action}): 4-Step Waterfall + Gemini CIO (ჯოი) ადასტურებს სიგნალს!")
        else:
            diag_lines.append(
                f"⏳ უსაფრთხო მოლოდინის რეჟიმი (ABSTAIN) — კონფლუენს ქულა: {km_score:.1f} (AI Confidence: {conf_pct:.0f}%)"
            )
            if f_dir_txt in {"BUY", "SELL"} or a_dir_txt in {"BUY", "SELL"}:
                diag_lines.append(
                    f"💡 AI ხედვა: Kronos = {f_dir_txt}, Analogue = {a_dir_txt}, Matrix = {m_dir_txt}, L2 Orderflow = {l2_dir_txt}"
                )

        if waterfall_obj:
            cio_ka = str(
                waterfall_obj.get("rationale_ka")
                or waterfall_obj.get("cio_reasoning_ka")
                or ""
            )
            if cio_ka:
                diag_lines.append(f"\n🤖 ჯოი (Gemini Quant CIO):\n  {cio_ka}")

        if l2_snap:
            v_reason = str(l2_snap.get("veto_reason") or "")
            if v_reason:
                diag_lines.append(f"🛑 L2 Orderbook Gate Veto: {v_reason}")

        if hard_blocks:
            diag_lines.append("\n🔒 რატომ დაბლოკა Gate-მა ორდერის გახსნა (Safety Blocks):")
            for block in hard_blocks:
                diag_lines.append(f"  • {block}")

        dec_reasons = decision_obj.get("reasons")
        if isinstance(dec_reasons, list) and dec_reasons:
            diag_lines.append("\n📊 ფენების დეტალური ტელემეტრია:")
            for r_item in dec_reasons:
                diag_lines.append(f"  - {r_item}")

        self.gate_reasons_view.setPlainText("\n".join(diag_lines))

        # Store latest gate & AI direction for the explicit manual execution controls.
        self._latest_gate_action = primary_action
        if primary_action in {"BUY", "SELL"}:
            self._latest_ai_direction = primary_action
        elif f_dir_txt in {"BUY", "SELL"}:
            self._latest_ai_direction = f_dir_txt
        elif m_dir_txt in {"BUY", "SELL"}:
            self._latest_ai_direction = m_dir_txt

        # Autonomous execution is owned by the backend waterfall path.  A gate
        # refresh is display state only: triggering the manual demo endpoint
        # here would bypass the backend Gemini review and could duplicate a
        # backend-approved order (or execute one the backend rejected).

    def _update_waterfall_ui(self, wf: dict[str, Any]) -> None:
        if not isinstance(wf, dict):
            return
        self._latest_waterfall = wf
        steps = wf.get("steps") if isinstance(wf.get("steps"), dict) else wf
        s1 = steps.get("step1_talib") if isinstance(steps.get("step1_talib"), dict) else {}
        s2 = steps.get("step2_kronos_matrix") if isinstance(steps.get("step2_kronos_matrix"), dict) else {}
        s3 = (
            steps.get("step3_l2_safety")
            if isinstance(steps.get("step3_l2_safety"), dict)
            else (
                steps.get("step3_l2_orderflow")
                if isinstance(steps.get("step3_l2_orderflow"), dict)
                else {}
            )
        )

        # Step 1: TA-Lib
        if s1:
            t_dir = str(s1.get("trend_bias") or s1.get("trend_direction") or "NEUTRAL").upper()
            rsi = float(s1.get("rsi_14") or 50.0)
            atr = float(s1.get("atr_14") or 0.0)
            adx = float(s1.get("adx_14") or 20.0)
            t_score = float(s1.get("filter_score") or s1.get("technical_score") or 50.0)
            src = str(s1.get("engine") or s1.get("engine_source") or "TA-Lib")
            self.wf_step1_label.setText(
                f"STEP 1 [TA-Lib Filter]: {t_dir} | RSI(14)={rsi:.1f} | ATR(14)={atr:.2f} | ADX={adx:.1f} | Score={t_score:.0f}/100 [{src}]"
            )
            self._colorize_inline_status(
                self.wf_step1_label,
                "BUY" if t_dir == "BUY" else ("SELL" if t_dir == "SELL" else "ABSTAIN"),
            )

        # Step 2: Kronos + Matrix
        if s2:
            p_dir = str(s2.get("candidate_direction") or s2.get("primary_direction") or "ABSTAIN").upper()
            m_dir = str(s2.get("matrix_direction") or "ABSTAIN").upper()
            a_dir = str(s2.get("analog_direction") or "ABSTAIN").upper()
            f_dir = str(s2.get("forecast_direction") or "ABSTAIN").upper()
            m_sc = float(s2.get("matrix_score") or 0.0)
            a_wr = float(s2.get("analog_win_rate_pct") or 50.0)
            self.wf_step2_label.setText(
                f"STEP 2 [Kronos + Matrix]: {p_dir} | Matrix={m_dir} ({m_sc:.1f}/8.0) | 8-NN Analogue={a_dir} ({a_wr:.1f}%) | Kronos={f_dir}"
            )
            self._colorize_inline_status(self.wf_step2_label, p_dir)

        # Step 3: L2 DOM Safety Gate
        if s3:
            veto_active = bool(s3.get("vetoed", s3.get("veto_active", False)))
            wall_support = bool(s3.get("l2_wall_support", False))
            l2_dir = str(s3.get("l2_direction") or "NEUTRAL").upper()
            if veto_active:
                self.wf_step3_label.setText(
                    f"STEP 3 [L2 DOM Safety Gate]: ⛔ VETO ACTIVE ({s3.get('veto_reason') or 'Opposing L2 Wall'})"
                )
                self._colorize_inline_status(self.wf_step3_label, "SELL")
            elif wall_support:
                self.wf_step3_label.setText(
                    f"STEP 3 [L2 DOM Safety Gate]: ✅ CLEAR + 🧱 L2 WALL SUPPORT ({l2_dir})"
                )
                self._colorize_inline_status(self.wf_step3_label, "BUY")
            else:
                self.wf_step3_label.setText(
                    f"STEP 3 [L2 DOM Safety Gate]: ✅ CLEAR (L2 Flow={l2_dir} | Wall Support=No)"
                )
                self._colorize_inline_status(self.wf_step3_label, "BUY")

        # Step 4: Gemini CIO Final Decision
        final_act = str(wf.get("final_action") or "ABSTAIN").upper()
        win_prob = float(wf.get("win_probability_pct") or 0.0)
        risk_tier = str(wf.get("risk_tier") or "ABSTAIN (<65%)")
        risk_pct = float(wf.get("risk_pct") or wf.get("recommended_risk_pct") or 0.0)
        rec_lot = float(wf.get("recommended_lot") or wf.get("recommended_lot_size") or 0.0)
        sl_p = float(wf.get("sl_price") or wf.get("recommended_sl_price") or 0.0)
        tp_p = float(wf.get("tp_price") or wf.get("recommended_tp_price") or 0.0)
        be_p = float(wf.get("be_trigger_price") or 0.0)
        part_p = float(wf.get("partial_close_price") or 0.0)

        self.wf_prob_badge.setText(f"WIN-PROB: {win_prob:.1f}%")
        self._apply_pill_style(
            self.wf_prob_badge,
            "online" if win_prob >= 65.0 else "warning",
        )
        self.wf_risk_badge.setText(f"RISK TIER: {risk_tier}")
        self._apply_pill_style(
            self.wf_risk_badge,
            "online" if final_act in {"BUY", "SELL"} else "warning",
        )

        self.wf_step4_label.setText(
            f"STEP 4 [Gemini CIO 'ჯოი']: Verdict={final_act} | Win-Prob={win_prob:.1f}% | Tier={risk_tier} | "
            f"Risk={risk_pct:.2f}% ({rec_lot:.2f} Lot) | SL={sl_p:.2f} | TP={tp_p:.2f} | BE(+1x ATR)={be_p:.2f} | 50%(+2x ATR)={part_p:.2f}"
        )
        self._colorize_inline_status(self.wf_step4_label, final_act)

    def _update_memory_ui(self, data: dict[str, Any]) -> None:
        results = data.get("results")
        if not isinstance(results, list):
            results = []
        self._memory_records = [r for r in results if isinstance(r, dict)]
        self.mem_count_label.setText(f"RESULTS: {len(self._memory_records)}")

        self.memory_table.setRowCount(len(self._memory_records))
        for row_idx, record in enumerate(self._memory_records):
            meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            scan_id = str(meta.get("scan_id") or record.get("id") or "--")
            symbol = str(meta.get("symbol") or "XAUUSD")
            action = str(meta.get("action") or "ABSTAIN").upper()
            conf = self._safe_float(meta.get("confidence", 0.0)) * 100.0
            score = self._safe_float(meta.get("score", record.get("score", 0.0)))
            created_at = meta.get("created_at")
            if isinstance(created_at, (int, float)) and created_at > 0:
                dt_txt = datetime.fromtimestamp(float(created_at), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            else:
                dt_txt = "--"

            items = [
                QTableWidgetItem(scan_id[:10]),
                QTableWidgetItem(symbol),
                QTableWidgetItem(action),
                QTableWidgetItem(f"{conf:.1f}%"),
                QTableWidgetItem(f"{score:.2f}"),
                QTableWidgetItem(dt_txt),
            ]
            items[0].setToolTip(scan_id)
            if action == "BUY":
                items[2].setForeground(QColor("#10b981"))
            elif action == "SELL":
                items[2].setForeground(QColor("#ef4444"))
            else:
                items[2].setForeground(QColor("#f59e0b"))

            for col_idx, table_item in enumerate(items):
                self.memory_table.setItem(row_idx, col_idx, table_item)

        if self._memory_records:
            self.memory_table.selectRow(0)

    def _on_memory_row_selected(self) -> None:
        selected = self.memory_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        if 0 <= row < len(self._memory_records):
            record = self._memory_records[row]
            doc = str(record.get("document") or "")
            meta = record.get("metadata") or {}
            formatted = (
                f"=== CYCLE METADATA ===\n{json.dumps(meta, indent=2, ensure_ascii=False)}\n\n"
                f"=== STORED REFLECTION DOCUMENT ===\n{doc}"
            )
            self.memory_detail_view.setPlainText(formatted)

    # -----------------------------------------------------------------------
    # WebSocket Event Stream Handling
    # -----------------------------------------------------------------------
    def _record_ws_latency(self, message: dict[str, Any]) -> None:
        """Record a safe UI-side latency sample from an incoming WS event.

        The API already stamps stream events.  Comparing that stamp with the
        local receipt time gives a dependency-free telemetry estimate and keeps
        the UI responsive when no extra network ping task is available.
        """

        self._last_ws_event_monotonic = time.monotonic()
        timestamp = message.get("timestamp")
        try:
            age_ms = (time.time() - float(timestamp)) * 1000.0
        except (TypeError, ValueError):
            age_ms = 0.0
        if 0.0 <= age_ms <= 60_000.0:
            self._ws_ping_ms = round(age_ms, 1)
            self._last_ws_ping_sample_monotonic = time.monotonic()
        else:
            self._ws_ping_ms = None
            self._last_ws_ping_sample_monotonic = None
        self._refresh_ws_ping_ui()

    def _refresh_ws_ping_ui(self) -> None:
        if not hasattr(self, "status_ws_ping_label"):
            return
        if not self._ws_connected:
            self.status_ws_ping_label.setText("WS PING: --")
            return
        sample_age = (
            time.monotonic() - self._last_ws_ping_sample_monotonic
            if self._last_ws_ping_sample_monotonic is not None
            else None
        )
        if self._ws_ping_ms is not None and (sample_age is None or sample_age <= 10.0):
            self.status_ws_ping_label.setText(f"WS PING: {self._ws_ping_ms:.0f} ms")
        elif self._last_ws_event_monotonic is None:
            self.status_ws_ping_label.setText("WS PING: waiting")
        else:
            self.status_ws_ping_label.setText("WS PING: stale")

    @property
    def ws_ping_latency_ms(self) -> float | None:
        """Latest UI-side WebSocket latency estimate in milliseconds."""

        return self._ws_ping_ms

    def _on_ws_status_changed(self, connected: bool, status_text: str) -> None:
        self._ws_connected = bool(connected)
        self._ws_status_text = str(status_text or ("WS LIVE STREAM" if connected else "WS OFFLINE"))
        if not connected:
            self._ws_ping_ms = None
            self._last_ws_ping_sample_monotonic = None
        self.ws_status_badge.setText(f"● {status_text}")
        self._apply_pill_style(self.ws_status_badge, "online" if connected else "warning")
        self._refresh_ws_ping_ui()

    def _on_ws_event_received(self, message: dict[str, Any]) -> None:
        self._record_ws_latency(message)
        self._event_count += 1
        self.event_counter_label.setText(f"EVENTS: {self._event_count}")

        event_type = str(message.get("type") or "process.log")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        ts = message.get("timestamp")
        time_str = (
            datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
            if isinstance(ts, (int, float))
            else datetime.now().strftime("%H:%M:%S")
        )

        if event_type == "trade.decision":
            decision_record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
            if isinstance(decision_record, dict):
                self.phase2_audit_tab.append_record(decision_record)
                action = str(
                    decision_record.get("final_decision")
                    or decision_record.get("action")
                    or payload.get("final_decision")
                    or "ABSTAIN"
                ).upper()
                mode = str(decision_record.get("execution_mode") or payload.get("execution_mode") or "BLOCKED").upper()
                reason = str(decision_record.get("reason") or decision_record.get("block_reason") or "decision recorded")
                color = "#10b981" if mode == "LIVE" else ("#f59e0b" if mode == "SHADOW" else "#ef4444")
                self._append_log_entry(
                    self.log_all,
                    "TRADE.DECISION",
                    f"{decision_record.get('symbol', '--')} {action} [{mode}] — {reason}",
                    color,
                    time_str=time_str,
                )
                self._append_log_entry(
                    self.log_system,
                    "AUDIT",
                    f"{decision_record.get('symbol', '--')} {action} [{mode}] — {reason}",
                    color,
                    time_str=time_str,
                )
            return
        if event_type == "controls.changed":
            controls = payload.get("controls") if isinstance(payload.get("controls"), dict) else payload
            if isinstance(controls, dict):
                self._update_phase2_controls_ui(controls)
            self._append_log_entry(
                self.log_all,
                "CONTROLS",
                str(payload.get("message") or "Runtime controls changed"),
                "#38bdf8",
                time_str=time_str,
            )
            return
        if event_type == "l2.snapshot":
            orderflow = payload.get("orderflow") if isinstance(payload.get("orderflow"), dict) else payload
            if isinstance(orderflow, dict):
                if not orderflow.get("symbol") and payload.get("symbol"):
                    orderflow = {**orderflow, "symbol": payload["symbol"]}
                self._update_l2_orderbook_ui(orderflow)
            return
        if event_type == "l2.alert":
            alert_obj = payload.get("alert") if isinstance(payload.get("alert"), dict) else payload
            if not isinstance(alert_obj, dict):
                return
            if not alert_obj.get("symbol") and payload.get("symbol"):
                alert_obj = {**alert_obj, "symbol": payload["symbol"]}
            active_symbol = self.ticker_combo.currentText().strip().upper() or "XAUUSD"
            alert_symbol = str(alert_obj.get("symbol") or "").strip().upper()
            if not alert_symbol or alert_symbol != active_symbol:
                return
            self._append_l2_alert(alert_obj)
            self._append_log_entry(
                self.log_all,
                "L2.ALERT",
                str(alert_obj.get("message") or ""),
                "#f59e0b",
                time_str=time_str,
            )
            return
        if event_type in {"trade.opened", "trade.closed", "trade.managed"}:
            self._request_open_positions()
            if event_type == "trade.closed":
                self._request_trade_history()
            self._append_log_entry(
                self.log_all,
                event_type.upper(),
                json.dumps(payload, ensure_ascii=False),
                "#10b981" if event_type == "trade.opened" else ("#38bdf8" if event_type == "trade.managed" else "#f59e0b"),
                time_str=time_str,
            )
            return
        if event_type == "screener.completed":
            self._request_open_positions()
            self._request_trade_history()
            self._append_log_entry(
                self.log_all,
                "SCREENER",
                json.dumps(payload, ensure_ascii=False),
                "#34d399",
                time_str=time_str,
            )
            return
        if event_type == "autonomous.changed":
            if "autonomous_mode" in payload:
                self._apply_autonomous_ui_state(bool(payload["autonomous_mode"]))
            self._append_log_entry(
                self.log_all,
                "AUTO-TRADE",
                str(payload.get("message") or ""),
                "#a855f7",
                time_str=time_str,
            )
            return
        if event_type == "gemini.review":
            review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
            verdict = str(review.get("verdict") or "REVIEW")
            color = "#10b981" if verdict == "APPROVE" else "#ef4444"
            msg = str(payload.get("message") or "")
            self._append_log_entry(self.log_all, f"GEMINI.{verdict}", msg, color, time_str=time_str)
            self._append_log_entry(self.log_debate, f"GEMINI.{verdict}", msg, color, time_str=time_str)
            return

        text_line = self._format_ws_payload(event_type, payload)
        color = self._event_color(event_type)

        self._append_log_entry(self.log_all, event_type.upper(), text_line, color, time_str=time_str)

        if event_type == "tradingagents.debate":
            self._append_log_entry(self.log_debate, "DEBATE", text_line, "#a78bfa", time_str=time_str)
        elif event_type == "kronos.forecast":
            self._append_log_entry(self.log_kronos, "KRONOS", text_line, "#38bdf8", time_str=time_str)
            self._parse_live_kronos_line(text_line)
        else:
            self._append_log_entry(self.log_system, event_type.upper(), text_line, color, time_str=time_str)

        if event_type in {"gate.changed", "scan.completed", "scan.failed", "scan.cancelled", "memory.stored"}:
            self._spawn_http_task(
                "gates_latest",
                "GET",
                "/gates/latest",
                include_local_kronos_snapshot=True,
            )
            self._spawn_http_task("health", "GET", "/health")
            if event_type in {"memory.stored", "scan.completed"} and self.memory_query_input.text().strip():
                self.trigger_memory_search()

    @staticmethod
    def _format_ws_payload(event_type: str, payload: dict[str, Any]) -> str:
        if "message" in payload:
            src = payload.get("source")
            prefix = f"[{src}] " if src else ""
            return f"{prefix}{payload['message']}"
        if event_type == "gate.changed" and isinstance(payload.get("gate"), dict):
            gate = payload["gate"]
            return (
                f"Gate updated -> symbol={gate.get('symbol')} action={gate.get('action')} "
                f"confidence={gate.get('confidence')} strategy={gate.get('strategy')}"
            )
        if event_type == "scan.completed":
            return f"Scan completed with status={payload.get('status')}"
        if event_type == "memory.stored":
            return f"Stored cycle memory id={payload.get('id')} backend={payload.get('backend')}"
        return json.dumps(payload, ensure_ascii=False)

    def _parse_live_kronos_line(self, line: str) -> None:
        match = re.search(
            r"action=(?P<action>\w+)\s+matrix=(?P<matrix>\w+)\s+analog=(?P<analog>\w+)\s+"
            r"forecast=(?P<forecast>\w+)\s+confidence=(?P<conf>[0-9.]+)",
            line,
        )
        if not match:
            return
        act = match.group("action").upper()
        conf_val = self._safe_float(match.group("conf"))
        self._style_direction_label(self.kronos_dir_label, act)
        pct = max(0.0, min(100.0, conf_val * 100.0))
        self.kronos_conf_label.setText(f"{pct:.1f}%  (Live Stream)")
        self.kronos_conf_bar.setValue(int(round(pct)))

    def _append_log_entry(
        self,
        widget: QTextEdit,
        tag: str,
        message: str,
        color: str,
        *,
        time_str: str | None = None,
    ) -> None:
        stamp = time_str or datetime.now().strftime("%H:%M:%S")
        escaped_msg = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html_line = (
            f'<span style="color:#64748b;">[{stamp}]</span> '
            f'<span style="color:{color}; font-weight:bold;">[{tag}]</span> '
            f'<span style="color:#e2e8f0;">{escaped_msg}</span>'
        )
        widget.append(html_line)
        if self.chk_autoscroll.isChecked():
            widget.moveCursor(QTextCursor.MoveOperation.End)

    def _clear_live_logs(self) -> None:
        self.log_all.clear()
        self.log_debate.clear()
        self.log_kronos.clear()
        self.log_system.clear()
        self._event_count = 0
        self.event_counter_label.setText("EVENTS: 0")

    # -----------------------------------------------------------------------
    # Visual Styling Helpers
    # -----------------------------------------------------------------------
    @staticmethod
    def _apply_pill_style(label: QLabel, variant: str) -> None:
        palettes = {
            "online": ("#062e22", "#10b981", "#059669"),
            "offline": ("#311016", "#ef4444", "#dc2626"),
            "warning": ("#2e2108", "#f59e0b", "#d97706"),
            "info": ("#082438", "#38bdf8", "#0284c7"),
        }
        bg, fg, border = palettes.get(variant, palettes["info"])
        label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 4px; padding: 3px 8px; font-family: 'Consolas', monospace; "
            f"font-size: 11px; font-weight: bold;"
        )

    def _style_gate_hero(self, action: str, confidence: float) -> None:
        action = action.upper()
        self.gate_hero_label.setText(f"GATE: {action}  ({confidence * 100:.1f}%)")
        if action == "BUY":
            self.gate_hero_label.setStyleSheet(
                "background-color: #062e22; color: #10b981; border: 2px solid #10b981; "
                "border-radius: 5px; padding: 3px 10px;"
            )
        elif action == "SELL":
            self.gate_hero_label.setStyleSheet(
                "background-color: #311016; color: #ef4444; border: 2px solid #ef4444; "
                "border-radius: 5px; padding: 3px 10px;"
            )
        else:
            self.gate_hero_label.setStyleSheet(
                "background-color: #2e2108; color: #f59e0b; border: 2px solid #f59e0b; "
                "border-radius: 5px; padding: 3px 10px;"
            )

    @staticmethod
    def _style_direction_label(label: QLabel, direction: str) -> None:
        direction = direction.upper()
        label.setText(direction)
        if direction == "BUY":
            label.setStyleSheet("color: #10b981; font-weight: bold;")
        elif direction == "SELL":
            label.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            label.setStyleSheet("color: #f59e0b; font-weight: bold;")

    @staticmethod
    def _colorize_inline_status(label: QLabel, direction: str) -> None:
        direction = direction.upper()
        if direction == "BUY":
            color = "#10b981"
        elif direction == "SELL":
            color = "#ef4444"
        else:
            color = "#f59e0b"
        label.setStyleSheet(
            f"color: {color}; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold;"
        )

    @staticmethod
    def _event_color(event_type: str) -> str:
        if event_type == "tradingagents.debate":
            return "#a78bfa"
        if event_type == "kronos.forecast":
            return "#38bdf8"
        if event_type in {"gate.changed", "l2.alert"}:
            return "#f59e0b"
        if event_type in {"scan.completed", "memory.stored", "websocket.connected"}:
            return "#10b981"
        if "failed" in event_type or "timeout" in event_type:
            return "#ef4444"
        return "#94a3b8"

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # -----------------------------------------------------------------------
    # Clean Shutdown
    # -----------------------------------------------------------------------
    def closeEvent(self, event_obj) -> None:  # type: ignore[override]
        self.save_workspace_layout()
        self.poll_timer.stop()
        self.l2_timer.stop()
        self.pair_switch_scan_timer.stop()
        self.auto_scan_timer.stop()
        self.ws_ping_timer.stop()
        if hasattr(self, "ws_thread") and self.ws_thread.isRunning():
            self.ws_thread.stop()
            self.ws_thread.wait(2500)
        for worker in list(self._active_threads):
            if worker.isRunning():
                worker.wait(1000)
        super().closeEvent(event_obj)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="New Era Trading Bot — Enterprise AI Quant Platform")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Local FastAPI base URL")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="Local WebSocket logs URL")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Initialize UI, verify widgets/threads, and exit automatically after 1.5 seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.smoke_test and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setApplicationName("New Era AI Quant Platform")
    window = NewEraDesktopTerminal(api_url=args.api_url, ws_url=args.ws_url)
    window.show()

    if args.smoke_test:
        QTimer.singleShot(1500, window.close)
        QTimer.singleShot(1600, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
