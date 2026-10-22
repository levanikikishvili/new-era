"""Reusable Qt widgets for the Phase 2/3 New Era desktop workspace.

This module deliberately contains only widget construction, local state, and
signals.  It does not import the API application, create threads, or perform
HTTP/WebSocket work at import time.  The desktop window owns the transport
workers and connects their results to these widgets.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


PHASE2_TAB_LABELS: tuple[str, ...] = (
    "Live Execution & Interactive Chart",
    "Quant Gate & Confluence Control",
    "Bot & Strategy Fleet Manager",
    "Live Decision Audit & Analytics",
)

DEFAULT_CONTROL_STATE: dict[str, Any] = {
    "gemini_cio_enabled": True,
    "kronos_enabled": True,
    "l2_enabled": True,
    "confluence_mode": "2-of-3",
    "risk_pct": 1.5,
    "min_rr": 1.5,
    "max_sl_atr": 2.5,
    "time_stop_minutes": 120,
}

CONTROL_FIELDS: tuple[str, ...] = (
    "gemini_cio_enabled",
    "kronos_enabled",
    "l2_enabled",
    "confluence_mode",
    "risk_pct",
    "min_rr",
    "max_sl_atr",
    "time_stop_minutes",
)


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_control_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the flat UI control state from either API shape.

    The API returns the fields at the top level and also under ``controls``.
    Accepting both forms keeps the widget useful with fixtures and older
    server responses while preserving the backend field names in POST payloads.
    """

    source: Mapping[str, Any] = state or {}
    nested = source.get("controls")
    if isinstance(nested, Mapping):
        source = {**nested, **source}
    normalized = dict(DEFAULT_CONTROL_STATE)
    for field in CONTROL_FIELDS:
        if field not in source:
            continue
        value = source[field]
        if field in {"gemini_cio_enabled", "kronos_enabled", "l2_enabled"}:
            normalized[field] = bool(value)
        elif field == "confluence_mode":
            mode = _safe_text(value, normalized[field]).lower()
            normalized[field] = mode if mode in {"2-of-3", "3-of-3", "raw"} else normalized[field]
        elif field == "time_stop_minutes":
            try:
                normalized[field] = int(value)
            except (TypeError, ValueError):
                pass
        else:
            parsed = _safe_float(value, _safe_float(normalized[field]))
            normalized[field] = parsed
    return normalized


class LiveExecutionTab(QWidget):
    """Phase 2 execution surface with a symbol selector and content host."""

    symbol_changed = pyqtSignal(str)
    refresh_requested = pyqtSignal(str)

    SYMBOLS: tuple[str, ...] = ("XAUUSD", "USTEC", "US500")

    def __init__(self, content_widget: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase2LiveExecutionTab")
        self._build_ui(content_widget)

    def _build_ui(self, content_widget: QWidget | None) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QFrame(self)
        header.setObjectName("SubCard")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        title = QLabel("LIVE EXECUTION & INTERACTIVE CHART")
        title.setObjectName("SectionHeader")
        header_layout.addWidget(title)

        symbol_label = QLabel("SYMBOL:")
        symbol_label.setObjectName("MetricTitle")
        self.symbol_combo = QComboBox(header)
        self.symbol_combo.setObjectName("Phase2SymbolSelector")
        self.symbol_combo.setEditable(True)
        self.symbol_combo.addItems(self.SYMBOLS)
        self.symbol_combo.setCurrentText("XAUUSD")
        self.symbol_combo.setMinimumWidth(105)
        self.symbol_combo.currentTextChanged.connect(self._emit_symbol_changed)

        self.quote_status_label = QLabel("Live quote: waiting for MT5/API state")
        self.quote_status_label.setObjectName("MetricValue")
        self.quote_status_label.setMinimumWidth(260)

        self.refresh_button = QPushButton("↻ Refresh Live State", header)
        self.refresh_button.setObjectName("ChipButton")
        self.refresh_button.clicked.connect(
            lambda: self.refresh_requested.emit(self.symbol_combo.currentText().strip().upper() or "XAUUSD")
        )

        header_layout.addWidget(symbol_label)
        header_layout.addWidget(self.symbol_combo)
        header_layout.addWidget(self.quote_status_label, stretch=1)
        header_layout.addWidget(self.refresh_button)
        layout.addWidget(header)

        if content_widget is not None:
            self.chart_host = content_widget
            layout.addWidget(content_widget, stretch=1)
            self.chart_placeholder = None
            self.live_stream_placeholder = None
        else:
            host = QFrame(self)
            host.setObjectName("CardPanel")
            host_layout = QVBoxLayout(host)
            host_layout.setContentsMargins(10, 10, 10, 10)
            placeholder = QLabel(
                "Interactive chart host ready\n"
                "Embed the existing TradingView/MT5 chart widget here."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setObjectName("Phase2ChartPlaceholder")
            stream = QTextEdit(host)
            stream.setReadOnly(True)
            stream.setPlaceholderText("Live stream placeholder — WebSocket events are delivered by the desktop transport.")
            stream.setMaximumHeight(110)
            host_layout.addWidget(placeholder, stretch=1)
            host_layout.addWidget(stream)
            self.chart_host = host
            self.chart_placeholder = placeholder
            self.live_stream_placeholder = stream
            layout.addWidget(host, stretch=1)

    def _emit_symbol_changed(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if normalized:
            self.symbol_changed.emit(normalized)

    def set_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper() or "XAUUSD"
        self.symbol_combo.blockSignals(True)
        try:
            self.symbol_combo.setCurrentText(normalized)
        finally:
            self.symbol_combo.blockSignals(False)

    def set_quote_status(self, text: str) -> None:
        self.quote_status_label.setText(text)


class QuantGateConfluenceTab(QWidget):
    """Runtime decision controls for the backend ``/controls`` contract."""

    apply_requested = pyqtSignal(dict)
    refresh_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase2QuantGateControlTab")
        self._build_ui()
        self.set_state(DEFAULT_CONTROL_STATE)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("QUANT GATE & CONFLUENCE CONTROL")
        title.setObjectName("SectionHeader")
        self.state_badge = QLabel("SAFE DEFAULTS")
        self.state_badge.setObjectName("Phase2ControlStateBadge")
        self.refresh_button = QPushButton("↻ Refresh", self)
        self.refresh_button.setObjectName("ChipButton")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.refresh_controls_button = self.refresh_button
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.state_badge)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        layer_card = QFrame(self)
        layer_card.setObjectName("SubCard")
        layer_layout = QGridLayout(layer_card)
        layer_layout.setContentsMargins(12, 10, 12, 10)
        layer_layout.setHorizontalSpacing(14)
        layer_layout.setVerticalSpacing(8)

        self.gemini_cio_toggle = QCheckBox("Gemini CIO veto", layer_card)
        self.gemini_cio_toggle.setObjectName("GeminiCioToggle")
        self.gemini_cio_check = self.gemini_cio_toggle
        self.kronos_toggle = QCheckBox("Kronos Transformer", layer_card)
        self.kronos_toggle.setObjectName("KronosToggle")
        self.kronos_check = self.kronos_toggle
        self.l2_toggle = QCheckBox("L2 Radar", layer_card)
        self.l2_toggle.setObjectName("L2RadarToggle")
        self.l2_check = self.l2_toggle
        layer_layout.addWidget(self.gemini_cio_toggle, 0, 0)
        layer_layout.addWidget(self.kronos_toggle, 0, 1)
        layer_layout.addWidget(self.l2_toggle, 0, 2)

        layer_layout.addWidget(QLabel("Confluence mode:"), 1, 0)
        self.confluence_combo = QComboBox(layer_card)
        self.confluence_combo.setObjectName("ConfluenceModeSelector")
        self.confluence_combo.addItems(["2-of-3", "3-of-3", "raw"])
        self.confluence_selector = self.confluence_combo
        layer_layout.addWidget(self.confluence_combo, 1, 1, 1, 2)
        layout.addWidget(layer_card)

        risk_card = QFrame(self)
        risk_card.setObjectName("SubCard")
        risk_grid = QGridLayout(risk_card)
        risk_grid.setContentsMargins(12, 10, 12, 10)
        risk_grid.setHorizontalSpacing(12)
        risk_grid.setVerticalSpacing(8)

        risk_grid.addWidget(QLabel("Risk %:"), 0, 0)
        self.risk_pct_slider = QSlider(Qt.Orientation.Horizontal, risk_card)
        self.risk_pct_slider.setObjectName("RiskPctSlider")
        self.risk_pct_slider.setRange(5, 50)
        self.risk_pct_slider.setSingleStep(1)
        self.risk_pct_slider.setPageStep(5)
        self.risk_pct_spin = QDoubleSpinBox(risk_card)
        self.risk_pct_spin.setObjectName("RiskPctSpinBox")
        self.risk_pct_spin.setRange(0.5, 5.0)
        self.risk_pct_spin.setDecimals(1)
        self.risk_pct_spin.setSingleStep(0.1)
        self.risk_pct_spin.setSuffix(" %")
        self.risk_spinbox = self.risk_pct_spin
        self.risk_slider = self.risk_pct_slider
        self.risk_pct_slider.valueChanged.connect(lambda value: self.risk_pct_spin.setValue(value / 10.0))
        self.risk_pct_spin.valueChanged.connect(lambda value: self.risk_pct_slider.setValue(round(value * 10.0)))
        risk_grid.addWidget(self.risk_pct_slider, 0, 1)
        risk_grid.addWidget(self.risk_pct_spin, 0, 2)

        risk_grid.addWidget(QLabel("Minimum R:R:"), 1, 0)
        self.min_rr_spin = QDoubleSpinBox(risk_card)
        self.min_rr_spin.setObjectName("MinRrSpinBox")
        self.min_rr_spin.setRange(1.0, 4.0)
        self.min_rr_spin.setDecimals(1)
        self.min_rr_spin.setSingleStep(0.1)
        self.min_rr_spin.setSuffix(" R")
        self.min_rr = self.min_rr_spin
        risk_grid.addWidget(self.min_rr_spin, 1, 1, 1, 2)

        risk_grid.addWidget(QLabel("Maximum SL:"), 2, 0)
        self.max_sl_atr_spin = QDoubleSpinBox(risk_card)
        self.max_sl_atr_spin.setObjectName("MaxSlAtrSpinBox")
        # The requested Phase 2 range is broader than the original JoeyCIO
        # hard ceiling.  The backend accepts the explicit override and returns
        # the effective protected value separately.
        self.max_sl_atr_spin.setRange(0.5, 5.0)
        self.max_sl_atr_spin.setDecimals(1)
        self.max_sl_atr_spin.setSingleStep(0.1)
        self.max_sl_atr_spin.setSuffix(" x ATR")
        self.max_sl_atr = self.max_sl_atr_spin
        risk_grid.addWidget(self.max_sl_atr_spin, 2, 1, 1, 2)

        risk_grid.addWidget(QLabel("Time stop:"), 3, 0)
        self.time_stop_spin = QSpinBox(risk_card)
        self.time_stop_spin.setObjectName("TimeStopMinutesSpinBox")
        self.time_stop_spin.setRange(30, 240)
        self.time_stop_spin.setSingleStep(5)
        self.time_stop_spin.setSuffix(" min")
        self.time_stop_minutes = self.time_stop_spin
        risk_grid.addWidget(self.time_stop_spin, 3, 1, 1, 2)
        layout.addWidget(risk_card)

        action_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply Controls", self)
        self.apply_button.setObjectName("PrimaryScanButton")
        self.apply_button.clicked.connect(self._emit_apply)
        self.apply_controls_button = self.apply_button
        self.status_label = QLabel("Controls are local until Apply is sent to the API.")
        self.status_label.setObjectName("Phase2ControlStatus")
        action_row.addWidget(self.apply_button)
        action_row.addWidget(self.status_label, stretch=1)
        layout.addLayout(action_row)
        layout.addStretch(1)

    def values(self) -> dict[str, Any]:
        return {
            "gemini_cio_enabled": self.gemini_cio_toggle.isChecked(),
            "kronos_enabled": self.kronos_toggle.isChecked(),
            "l2_enabled": self.l2_toggle.isChecked(),
            "confluence_mode": self.confluence_combo.currentText(),
            "risk_pct": round(self.risk_pct_spin.value(), 1),
            "min_rr": round(self.min_rr_spin.value(), 1),
            "max_sl_atr": round(self.max_sl_atr_spin.value(), 1),
            "time_stop_minutes": int(self.time_stop_spin.value()),
        }

    def set_state(self, state: Mapping[str, Any] | None) -> None:
        normalized = normalize_control_state(state)
        self.gemini_cio_toggle.setChecked(normalized["gemini_cio_enabled"])
        self.kronos_toggle.setChecked(normalized["kronos_enabled"])
        self.l2_toggle.setChecked(normalized["l2_enabled"])
        self.confluence_combo.setCurrentText(normalized["confluence_mode"])
        self.risk_pct_spin.setValue(min(5.0, max(0.5, _safe_float(normalized["risk_pct"], 1.5))))
        self.min_rr_spin.setValue(min(4.0, max(1.0, _safe_float(normalized["min_rr"], 1.5))))
        self.max_sl_atr_spin.setValue(min(5.0, max(0.5, _safe_float(normalized["max_sl_atr"], 2.5))))
        try:
            time_stop = int(normalized["time_stop_minutes"])
        except (TypeError, ValueError):
            time_stop = 120
        self.time_stop_spin.setValue(min(240, max(30, time_stop)))
        state_map = state or {}
        override_active = bool(state_map.get("override_active", False))
        metadata = state_map.get("metadata") if isinstance(state_map.get("metadata"), Mapping) else {}
        clamped_fields = metadata.get("safety_clamped_fields") if isinstance(metadata, Mapping) else None
        if isinstance(clamped_fields, list) and clamped_fields:
            self.state_badge.setText("OVERRIDE ACTIVE / HARD CAP")
        else:
            self.state_badge.setText("OVERRIDE ACTIVE" if override_active else "SAFE DEFAULTS")

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {'#ef4444' if error else '#94a3b8'};")

    def _emit_apply(self) -> None:
        values = self.values()
        self.set_status("Applying runtime controls…")
        self.apply_requested.emit(values)


class BotFleetManagerTab(QWidget):
    """Fleet view with safe local feedback and optional backend persistence."""

    action_requested = pyqtSignal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase2BotFleetTab")
        self._bots: dict[str, dict[str, Any]] = {}
        self._local_enabled: dict[str, bool] = {}
        self._hidden_bot_ids: set[str] = set()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("BOT & STRATEGY FLEET MANAGER")
        title.setObjectName("SectionHeader")
        self.active_bot_label = QLabel("Active bot: --")
        self.active_bot_label.setObjectName("MetricValue")
        self.registry_status_label = QLabel("Fleet: waiting for /bots/status")
        self.registry_status_label.setObjectName("Phase2FleetStatus")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.active_bot_label)
        header.addWidget(self.registry_status_label)
        layout.addLayout(header)

        self.bot_table = QTableWidget(0, 5, self)
        self.bot_table.setObjectName("BotFleetTable")
        self.bot_table.setHorizontalHeaderLabels(["Bot ID", "Profile", "Symbol", "Server Status", "UI State"])
        self.bot_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.bot_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.bot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bot_table.verticalHeader().setVisible(False)
        self.bot_table.horizontalHeader().setStretchLastSection(True)
        self.bots_table = self.bot_table
        layout.addWidget(self.bot_table, stretch=1)

        actions = QHBoxLayout()
        self.new_bot_id_input = QLineEdit(self)
        self.new_bot_id_input.setObjectName("NewLocalBotIdInput")
        self.new_bot_id_input.setPlaceholderText("Optional local bot id")
        self.enable_button = QPushButton("Enable", self)
        self.enable_button.setObjectName("ChipButton")
        self.disable_button = QPushButton("Disable", self)
        self.disable_button.setObjectName("ChipButton")
        self.add_button = QPushButton("Add Profile", self)
        self.add_button.setObjectName("ChipButton")
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.setObjectName("ChipButton")
        self.enable_button.clicked.connect(lambda: self._set_selected_enabled(True))
        self.disable_button.clicked.connect(lambda: self._set_selected_enabled(False))
        self.add_button.clicked.connect(self._add_local_bot)
        self.remove_button.clicked.connect(self._remove_selected_from_view)
        actions.addWidget(self.new_bot_id_input, stretch=1)
        actions.addWidget(self.enable_button)
        actions.addWidget(self.disable_button)
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)

        self.help_label = QLabel(
            "Fleet actions sync with the backend registry when the API is online; new profiles start research-only."
        )
        self.help_label.setObjectName("Phase2FleetHelp")
        layout.addWidget(self.help_label)

    def set_status(self, data: Mapping[str, Any] | None) -> None:
        payload: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        active = _safe_text(payload.get("active_bot"), "--")
        self.active_bot_label.setText(f"Active bot: {active}")
        validation_errors = payload.get("validation_errors")
        if validation_errors:
            self.registry_status_label.setText(f"Fleet: registry warnings ({len(validation_errors)})")
        else:
            bot_value = payload.get("bots")
            self.registry_status_label.setText(f"Fleet: {len(bot_value) if isinstance(bot_value, list) else 0} server bot(s)")
        bot_value = payload.get("bots")
        raw_bots: list[Any] = []
        if isinstance(bot_value, list):
            raw_bots.extend(bot_value)
        server_ids: set[str] = set()
        for raw in raw_bots:
            if not isinstance(raw, Mapping):
                continue
            bot = dict(raw)
            bot_id = _safe_text(bot.get("id"), "unknown")
            bot["id"] = bot_id
            server_ids.add(bot_id)
            self._bots[bot_id] = bot
            server_enabled = bot.get("enabled")
            if isinstance(server_enabled, bool):
                self._local_enabled[bot_id] = server_enabled
            else:
                self._local_enabled.setdefault(
                    bot_id,
                    _safe_text(bot.get("status"), "enabled").lower() not in {"disabled", "off"},
                )
        for bot_id in list(self._bots):
            if not self._bots[bot_id].get("_local") and bot_id not in server_ids:
                self._bots.pop(bot_id, None)
                self._local_enabled.pop(bot_id, None)
        self._render_table()

    update_bots = set_status

    def set_action_status(self, text: str, *, error: bool = False) -> None:
        self.registry_status_label.setText(text)
        self.registry_status_label.setStyleSheet(
            f"color: {'#ef4444' if error else '#38bdf8'};"
        )

    def _selected_bot_id(self) -> str:
        rows = self.bot_table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.bot_table.item(rows[0].row(), 0)
        return item.text().strip() if item is not None else ""

    def _set_selected_enabled(self, enabled: bool) -> None:
        bot_id = self._selected_bot_id()
        if not bot_id:
            self.registry_status_label.setText("Fleet: select a bot first")
            return
        self._local_enabled[bot_id] = enabled
        self.registry_status_label.setText(f"Fleet: syncing {'enabled' if enabled else 'disabled'} {bot_id}")
        self._render_table(select_id=bot_id)
        self.action_requested.emit("set_enabled", {"bot_id": bot_id, "enabled": enabled})

    def _add_local_bot(self) -> None:
        requested = self.new_bot_id_input.text().strip()
        if not requested:
            requested = f"local-bot-{len([key for key in self._bots if key.startswith('local-bot-')]) + 1}"
        bot_id = requested.lower().replace(" ", "-")
        self._bots[bot_id] = {
            "id": bot_id,
            "display_name": bot_id,
            "symbol": "XAUUSD",
            "status": "local",
            "profile": "LOCAL UI PROFILE",
            "_local": True,
        }
        self._local_enabled[bot_id] = True
        self._hidden_bot_ids.discard(bot_id)
        self.new_bot_id_input.clear()
        self.registry_status_label.setText(f"Fleet: syncing new research-only profile {bot_id}")
        self._render_table(select_id=bot_id)
        self.action_requested.emit(
            "add",
            {"bot_id": bot_id, "display_name": bot_id, "symbol": "XAUUSD"},
        )

    def _remove_selected_from_view(self) -> None:
        bot_id = self._selected_bot_id()
        if not bot_id:
            self.registry_status_label.setText("Fleet: select a bot first")
            return
        self.registry_status_label.setText(f"Fleet: requesting removal of {bot_id}")
        self.action_requested.emit("remove", {"bot_id": bot_id})

    def _render_table(self, *, select_id: str | None = None) -> None:
        visible = [bot for bot_id, bot in self._bots.items() if bot_id not in self._hidden_bot_ids]
        self.bot_table.setRowCount(len(visible))
        for row, bot in enumerate(visible):
            bot_id = _safe_text(bot.get("id"), "unknown")
            server_status = _safe_text(bot.get("status"), "unknown").upper()
            ui_enabled = self._local_enabled.get(bot_id, True)
            ui_state = "ENABLED" if ui_enabled else "DISABLED"
            if bot.get("_local"):
                ui_state += " (LOCAL)"
            values = (
                bot_id,
                _safe_text(bot.get("profile") or bot.get("display_name"), "--"),
                _safe_text(bot.get("symbol"), "--"),
                server_status,
                ui_state,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 4:
                    item.setForeground(QColor("#10b981" if ui_enabled else "#f59e0b"))
                self.bot_table.setItem(row, column, item)
        if select_id:
            for row in range(self.bot_table.rowCount()):
                if self.bot_table.item(row, 0) and self.bot_table.item(row, 0).text() == select_id:
                    self.bot_table.selectRow(row)
                    break


class DecisionAuditTab(QWidget):
    """Decision history table, filters, summary counts, and reason details."""

    refresh_requested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase2DecisionAuditTab")
        self._records: list[dict[str, Any]] = []
        self._summary: dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("LIVE DECISION AUDIT & ANALYTICS")
        title.setObjectName("SectionHeader")
        self.total_label = QLabel("TOTAL: 0")
        self.live_label = QLabel("LIVE: 0")
        self.shadow_label = QLabel("SHADOW: 0")
        self.blocked_label = QLabel("BLOCKED: 0")
        for label in (self.total_label, self.live_label, self.shadow_label, self.blocked_label):
            label.setObjectName("MetricValue")
            header.addWidget(label)
        header.insertWidget(0, title)
        header.insertStretch(1, 1)
        layout.addLayout(header)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Symbol:"))
        self.symbol_filter = QComboBox(self)
        self.symbol_filter.setObjectName("AuditSymbolFilter")
        self.symbol_filter.setEditable(True)
        self.symbol_filter.addItem("ALL")
        filters.addWidget(self.symbol_filter)
        filters.addWidget(QLabel("Status:"))
        self.status_filter = QComboBox(self)
        self.status_filter.setObjectName("AuditStatusFilter")
        self.status_filter.addItems(["ALL", "LIVE", "SHADOW", "BLOCKED"])
        filters.addWidget(self.status_filter)
        filters.addWidget(QLabel("Decision:"))
        self.decision_filter = QComboBox(self)
        self.decision_filter.setObjectName("AuditDecisionFilter")
        self.decision_filter.addItems(["ALL", "BUY", "SELL", "ABSTAIN"])
        filters.addWidget(self.decision_filter)
        self.history_limit_spin = QSpinBox(self)
        self.history_limit_spin.setObjectName("AuditHistoryLimit")
        self.history_limit_spin.setRange(1, 500)
        self.history_limit_spin.setValue(100)
        self.history_limit_spin.setSuffix(" rows")
        filters.addWidget(self.history_limit_spin)
        self.refresh_button = QPushButton("↻ Refresh History", self)
        self.refresh_button.setObjectName("ChipButton")
        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit(int(self.history_limit_spin.value())))
        self.refresh_history_button = self.refresh_button
        self.clear_button = QPushButton("Clear View", self)
        self.clear_button.setObjectName("ChipButton")
        self.clear_button.clicked.connect(self.clear_view)
        filters.addWidget(self.refresh_button)
        filters.addWidget(self.clear_button)
        layout.addLayout(filters)

        self.decision_table = QTableWidget(0, 7, self)
        self.decision_table.setObjectName("DecisionAuditTable")
        self.decision_table.setHorizontalHeaderLabels(
            ["Time", "Symbol", "Signal", "Action", "Outcome", "Reason", "Record ID"]
        )
        self.decision_table.setAlternatingRowColors(True)
        self.decision_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.decision_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.decision_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.decision_table.verticalHeader().setVisible(False)
        self.decision_table.horizontalHeader().setStretchLastSection(True)
        self.decision_table.itemSelectionChanged.connect(self._render_selected_detail)
        self.audit_table = self.decision_table
        layout.addWidget(self.decision_table, stretch=1)

        self.detail_view = QTextEdit(self)
        self.detail_view.setObjectName("DecisionAuditDetail")
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("Select a decision to inspect its human-readable reason and layer status.")
        self.detail_view.setMaximumHeight(155)
        self.reason_view = self.detail_view
        layout.addWidget(self.detail_view)

        for control in (self.symbol_filter, self.status_filter, self.decision_filter):
            control.currentTextChanged.connect(self._render_table)

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def update_records(self, data: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None) -> None:
        raw_records: list[Any]
        if isinstance(data, Mapping):
            source = dict(cast(Mapping[str, Any], data))
            records_value = source.get("records")
            raw_records = list(records_value) if isinstance(records_value, list) else []
            summary_value = source.get("summary")
            summary = summary_value if isinstance(summary_value, Mapping) else None
            if summary is not None:
                self.set_summary(summary)
        elif isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
            raw_records = list(data)
        else:
            raw_records = []
        self._records = [dict(record) for record in raw_records if isinstance(record, Mapping)]
        self._update_symbol_filter_options()
        if not self._summary:
            self.set_summary(self._derive_summary(self._records))
        self._render_table()

    def append_record(self, record: Mapping[str, Any] | None) -> None:
        if not isinstance(record, Mapping):
            return
        payload = record.get("record")
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else record
        normalized = dict(payload_map)
        record_id = _safe_text(normalized.get("record_id"))
        if record_id and any(_safe_text(item.get("record_id")) == record_id for item in self._records):
            return
        self._records.insert(0, normalized)
        self._records = self._records[: max(1, int(self.history_limit_spin.value()))]
        self._summary = self._derive_summary(self._records)
        self._update_symbol_filter_options()
        self._render_table()

    def set_summary(self, summary: Mapping[str, Any] | None) -> None:
        summary_map: Mapping[str, Any] = summary if isinstance(summary, Mapping) else {}
        self._summary = dict(summary_map)
        mode_value = self._summary.get("execution_mode")
        mode_counts = mode_value if isinstance(mode_value, Mapping) else {}
        total = self._summary.get("total")
        if total is None:
            total = sum(int(value or 0) for value in mode_counts.values())
        self.total_label.setText(f"TOTAL: {int(total or 0)}")
        self.live_label.setText(f"LIVE: {int(mode_counts.get('LIVE', 0) or 0)}")
        self.shadow_label.setText(f"SHADOW: {int(mode_counts.get('SHADOW', 0) or 0)}")
        self.blocked_label.setText(f"BLOCKED: {int(mode_counts.get('BLOCKED', 0) or 0)}")

    def clear_view(self) -> None:
        self._records = []
        self._summary = {}
        self.decision_table.setRowCount(0)
        self.detail_view.clear()
        self.set_summary({"total": 0, "execution_mode": {}})

    @staticmethod
    def _derive_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        modes: dict[str, int] = {}
        decisions: dict[str, int] = {}
        total = 0
        for record in records:
            total += 1
            mode = _safe_text(record.get("execution_mode"), "BLOCKED").upper()
            decision = _safe_text(record.get("final_decision") or record.get("action"), "ABSTAIN").upper()
            modes[mode] = modes.get(mode, 0) + 1
            decisions[decision] = decisions.get(decision, 0) + 1
        return {"total": total, "execution_mode": modes, "final_decision": decisions}

    def _update_symbol_filter_options(self) -> None:
        current = self.symbol_filter.currentText() or "ALL"
        symbols = sorted({_safe_text(record.get("symbol")).upper() for record in self._records if _safe_text(record.get("symbol"))})
        self.symbol_filter.blockSignals(True)
        try:
            self.symbol_filter.clear()
            self.symbol_filter.addItem("ALL")
            self.symbol_filter.addItems(symbols)
            self.symbol_filter.setCurrentText(current if current in {"ALL", *symbols} else "ALL")
        finally:
            self.symbol_filter.blockSignals(False)

    def _filtered_records(self) -> list[dict[str, Any]]:
        symbol = self.symbol_filter.currentText().strip().upper()
        mode = self.status_filter.currentText().strip().upper()
        decision = self.decision_filter.currentText().strip().upper()
        return [
            record
            for record in self._records
            if (symbol in {"", "ALL"} or _safe_text(record.get("symbol")).upper() == symbol)
            and (mode in {"", "ALL"} or _safe_text(record.get("execution_mode"), "BLOCKED").upper() == mode)
            and (
                decision in {"", "ALL"}
                or _safe_text(record.get("final_decision") or record.get("action"), "ABSTAIN").upper() == decision
            )
        ]

    def _render_table(self) -> None:
        records = self._filtered_records()
        self.decision_table.setRowCount(len(records))
        for row, record in enumerate(records):
            timestamp = _safe_text(record.get("timestamp"), "--")
            if "T" in timestamp:
                timestamp = timestamp.replace("T", " ").replace("+00:00", " UTC")[:25]
            symbol = _safe_text(record.get("symbol"), "--").upper()
            signal = _safe_text(record.get("signal"), "ABSTAIN").upper()
            action = _safe_text(record.get("final_decision") or record.get("action"), "ABSTAIN").upper()
            mode = _safe_text(record.get("execution_mode"), "BLOCKED").upper()
            reason = _safe_text(record.get("reason") or record.get("block_reason"), "No reason supplied")
            record_id = _safe_text(record.get("record_id"), "--")
            values = [timestamp, symbol, signal, action, mode, reason, record_id[:12]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value if column in {5, 6} else "")
                if column in {3, 4}:
                    color = "#10b981" if value in {"BUY", "LIVE"} else ("#ef4444" if value in {"SELL", "BLOCKED"} else "#f59e0b")
                    item.setForeground(QColor(color))
                self.decision_table.setItem(row, column, item)
        if records:
            self.decision_table.selectRow(0)
        else:
            self.detail_view.clear()

    def _render_selected_detail(self) -> None:
        selected = self.decision_table.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        records = self._filtered_records()
        if 0 <= row < len(records):
            self.detail_view.setPlainText(self.render_record(records[row]))

    @classmethod
    def render_record(cls, record: Mapping[str, Any]) -> str:
        """Return a human-readable decision explanation for table/detail views."""

        symbol = _safe_text(record.get("symbol"), "UNKNOWN").upper()
        action = _safe_text(record.get("final_decision") or record.get("action"), "ABSTAIN").upper()
        mode = _safe_text(record.get("execution_mode"), "BLOCKED").upper()
        reason = _safe_text(record.get("reason"), "No human-readable reason supplied.")
        block_reason = _safe_text(record.get("block_reason"))
        lines = [f"{symbol}: {action} / {mode}", f"Reason: {reason}"]
        if block_reason:
            lines.append(f"Block/shadow reason: {block_reason}")
        layer_reasons = record.get("waterfall_layer_reasons")
        layer_statuses = record.get("waterfall_layer_statuses")
        if isinstance(layer_statuses, Mapping) or isinstance(layer_reasons, Mapping):
            lines.append("Layers:")
            names = [
                "step1_talib",
                "step2_kronos_matrix",
                "step3_l2_safety",
                "step4_gemini_cio",
            ]
            for name in names:
                status = _safe_text(layer_statuses.get(name) if isinstance(layer_statuses, Mapping) else "", "UNKNOWN")
                layer_reason = _safe_text(layer_reasons.get(name) if isinstance(layer_reasons, Mapping) else "", "")
                suffix = f" — {layer_reason}" if layer_reason else ""
                lines.append(f"  • {name}: {status}{suffix}")
        return "\n".join(lines)


class BotStrategyLabPanel(QWidget):
    """Safe Phase 3 strategy research and backtester surface.

    The panel intentionally exposes configuration and preview actions only.  It
    never imports a code runner, executes user supplied Python, or writes
    strategy files.  The desktop window can later connect ``tool_requested`` to
    a reviewed backend capability without changing this reusable widget.
    """

    tool_requested = pyqtSignal(str)
    strategy_save_requested = pyqtSignal(dict)
    strategy_backtest_requested = pyqtSignal(dict)
    strategy_walk_forward_requested = pyqtSignal(dict)
    strategy_load_requested = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase3BotStrategyLabPanel")
        self._last_tool = ""
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("BOT STRATEGY LAB // SAFE RESEARCH WORKSPACE")
        title.setObjectName("SectionHeader")
        self.status_label = QLabel("Ready for a metadata-only strategy preview.")
        self.status_label.setObjectName("Phase3StrategyStatus")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.lab_tabs = QTabWidget(self)
        self.strategy_lab_tabs = self.lab_tabs

        strategy_tab = QWidget(self.lab_tabs)
        strategy_layout = QVBoxLayout(strategy_tab)
        strategy_layout.setContentsMargins(8, 8, 8, 8)
        strategy_layout.setSpacing(8)

        config_card = QFrame(strategy_tab)
        config_card.setObjectName("SubCard")
        config_grid = QGridLayout(config_card)
        config_grid.setContentsMargins(10, 8, 10, 8)
        config_grid.setHorizontalSpacing(10)
        config_grid.setVerticalSpacing(8)

        config_grid.addWidget(QLabel("Strategy name:"), 0, 0)
        self.strategy_name_input = QLineEdit(config_card)
        self.strategy_name_input.setObjectName("StrategyNameInput")
        self.strategy_name_input.setText("New Era Confluence Preview")
        config_grid.addWidget(self.strategy_name_input, 0, 1, 1, 2)

        config_grid.addWidget(QLabel("Symbol:"), 1, 0)
        self.strategy_symbol_combo = QComboBox(config_card)
        self.strategy_symbol_combo.setObjectName("StrategySymbolSelector")
        self.strategy_symbol_combo.addItems(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "US500", "USTEC"])
        config_grid.addWidget(self.strategy_symbol_combo, 1, 1)

        config_grid.addWidget(QLabel("Timeframe:"), 1, 2)
        self.strategy_timeframe_combo = QComboBox(config_card)
        self.strategy_timeframe_combo.setObjectName("StrategyTimeframeSelector")
        self.strategy_timeframe_combo.addItems(["M5", "M15", "H1", "H4", "D1"])
        config_grid.addWidget(self.strategy_timeframe_combo, 1, 3)

        config_grid.addWidget(QLabel("Builder:"), 2, 0)
        self.builder_mode_combo = QComboBox(config_card)
        self.builder_mode_combo.setObjectName("StrategyBuilderModeSelector")
        self.builder_mode_combo.addItems(["Visual Rules", "Python Code"])
        config_grid.addWidget(self.builder_mode_combo, 2, 1)

        config_grid.addWidget(QLabel("Strategy ID:"), 2, 2)
        self.strategy_id_input = QLineEdit(config_card)
        self.strategy_id_input.setObjectName("StrategyIdInput")
        self.strategy_id_input.setPlaceholderText("optional-slug")
        config_grid.addWidget(self.strategy_id_input, 2, 3)

        config_grid.addWidget(QLabel("Python module:"), 3, 0)
        self.module_path_input = QLineEdit(config_card)
        self.module_path_input.setObjectName("StrategyModulePathInput")
        self.module_path_input.setPlaceholderText("strategies/my_strategy.py")
        config_grid.addWidget(self.module_path_input, 3, 1)

        config_grid.addWidget(QLabel("Class:"), 3, 2)
        self.class_name_input = QLineEdit(config_card)
        self.class_name_input.setObjectName("StrategyClassNameInput")
        self.class_name_input.setPlaceholderText("MyStrategy")
        config_grid.addWidget(self.class_name_input, 3, 3)

        strategy_layout.addWidget(config_card)

        flow_card = QFrame(strategy_tab)
        flow_card.setObjectName("SubCard")
        flow_layout = QVBoxLayout(flow_card)
        flow_layout.setContentsMargins(10, 8, 10, 8)
        flow_layout.setSpacing(6)
        flow_title = QLabel("VISUAL RULE FLOW / STRATEGY SPECIFICATION (JSON)")
        flow_title.setObjectName("MetricTitle")
        self.flow_editor = QTextEdit(flow_card)
        self.flow_editor.setObjectName("StrategyFlowEditor")
        self.flow_editor.setMinimumHeight(150)
        self.flow_editor.setPlainText(
            json.dumps(
                {
                    "indicators": [
                        {"id": "fast", "type": "sma", "source": "close", "period": 5},
                        {"id": "slow", "type": "sma", "source": "close", "period": 20},
                    ],
                    "conditions": [
                        {
                            "id": "go_long",
                            "left": {"indicator": "fast"},
                            "operator": ">",
                            "right": {"indicator": "slow"},
                        },
                        {
                            "id": "go_short",
                            "left": {"indicator": "fast"},
                            "operator": "<",
                            "right": {"indicator": "slow"},
                        },
                    ],
                    "actions": [
                        {"type": "enter_long", "when": "go_long"},
                        {"type": "enter_short", "when": "go_short"},
                    ],
                    "settings": {"allow_short": True, "default_size": 1.0},
                },
                indent=2,
            )
        )
        flow_layout.addWidget(flow_title)
        flow_layout.addWidget(self.flow_editor)
        strategy_layout.addWidget(flow_card)

        signal_card = QFrame(strategy_tab)
        signal_card.setObjectName("SubCard")
        signal_layout = QVBoxLayout(signal_card)
        signal_layout.setContentsMargins(10, 8, 10, 8)
        signal_layout.setSpacing(6)
        signal_title = QLabel("CUSTOM SIGNAL WIDGETS / OVERLAYS")
        signal_title.setObjectName("MetricTitle")
        self.signal_widget_placeholder = QLabel(
            "No custom widgets inserted. Use Insert → Indicators, Overlays, or Custom Signal Widgets."
        )
        self.signal_widget_placeholder.setObjectName("Phase3SignalWidgetPlaceholder")
        self.signal_widget_placeholder.setWordWrap(True)
        signal_layout.addWidget(signal_title)
        signal_layout.addWidget(self.signal_widget_placeholder)
        strategy_layout.addWidget(signal_card)

        risk_card = QFrame(strategy_tab)
        risk_card.setObjectName("SubCard")
        risk_layout = QHBoxLayout(risk_card)
        risk_layout.setContentsMargins(10, 8, 10, 8)
        risk_layout.setSpacing(8)
        risk_layout.addWidget(QLabel("Manual risk override:"))
        self.risk_override_spin = QDoubleSpinBox(risk_card)
        self.risk_override_spin.setObjectName("StrategyRiskOverrideSpinBox")
        self.risk_override_spin.setRange(0.1, 5.0)
        self.risk_override_spin.setDecimals(1)
        self.risk_override_spin.setSingleStep(0.1)
        self.risk_override_spin.setValue(1.5)
        self.risk_override_spin.setSuffix(" %")
        risk_layout.addWidget(self.risk_override_spin)
        self.risk_override_button = QPushButton("Apply Safe Risk Override", risk_card)
        self.risk_override_button.setObjectName("ChipButton")
        self.risk_override_button.clicked.connect(self._apply_risk_override)
        risk_layout.addWidget(self.risk_override_button)
        risk_layout.addStretch(1)
        strategy_layout.addWidget(risk_card)

        action_row = QHBoxLayout()
        self.new_strategy_button = QPushButton("New Strategy", strategy_tab)
        self.new_strategy_button.setObjectName("ChipButton")
        self.new_strategy_button.clicked.connect(self._new_strategy)
        self.save_strategy_button = QPushButton("Save Profile", strategy_tab)
        self.save_strategy_button.setObjectName("PrimaryScanButton")
        self.save_strategy_button.clicked.connect(self.save_strategy_profile)
        self.load_strategy_button = QPushButton("Validate Python Strategy", strategy_tab)
        self.load_strategy_button.setObjectName("ChipButton")
        self.load_strategy_button.clicked.connect(self.load_python_strategy)
        self.open_backtester_button = QPushButton("Open Backtester", strategy_tab)
        self.open_backtester_button.setObjectName("PrimaryScanButton")
        self.open_backtester_button.clicked.connect(self.open_backtester)
        self.github_search_button = QPushButton("Search GitHub Quant Modules", strategy_tab)
        self.github_search_button.setObjectName("ChipButton")
        self.github_search_button.clicked.connect(self.open_github_module_search)
        action_row.addWidget(self.new_strategy_button)
        action_row.addWidget(self.save_strategy_button)
        action_row.addWidget(self.load_strategy_button)
        action_row.addWidget(self.open_backtester_button)
        action_row.addWidget(self.github_search_button)
        action_row.addStretch(1)
        strategy_layout.addLayout(action_row)
        strategy_layout.addStretch(1)

        backtester_tab = QWidget(self.lab_tabs)
        backtest_layout = QVBoxLayout(backtester_tab)
        backtest_layout.setContentsMargins(8, 8, 8, 8)
        backtest_layout.setSpacing(8)

        backtest_card = QFrame(backtester_tab)
        backtest_card.setObjectName("SubCard")
        backtest_grid = QGridLayout(backtest_card)
        backtest_grid.setContentsMargins(10, 8, 10, 8)
        backtest_grid.setHorizontalSpacing(10)
        backtest_grid.setVerticalSpacing(8)
        backtest_grid.addWidget(QLabel("Replay symbol:"), 0, 0)
        self.backtest_symbol_combo = QComboBox(backtest_card)
        self.backtest_symbol_combo.setObjectName("BacktestSymbolSelector")
        self.backtest_symbol_combo.addItems(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "US500", "USTEC"])
        backtest_grid.addWidget(self.backtest_symbol_combo, 0, 1)
        backtest_grid.addWidget(QLabel("Replay timeframe:"), 0, 2)
        self.backtest_timeframe_combo = QComboBox(backtest_card)
        self.backtest_timeframe_combo.setObjectName("BacktestTimeframeSelector")
        self.backtest_timeframe_combo.addItems(["M5", "M15", "H1", "H4", "D1"])
        backtest_grid.addWidget(self.backtest_timeframe_combo, 0, 3)
        backtest_grid.addWidget(QLabel("Saved profile:"), 1, 0)
        self.saved_strategy_combo = QComboBox(backtest_card)
        self.saved_strategy_combo.setObjectName("SavedStrategySelector")
        self.saved_strategy_combo.addItem("Unsaved draft")
        backtest_grid.addWidget(self.saved_strategy_combo, 1, 1, 1, 2)
        self.refresh_strategies_button = QPushButton("Refresh Profiles", backtest_card)
        self.refresh_strategies_button.setObjectName("ChipButton")
        self.refresh_strategies_button.clicked.connect(self.refresh_strategy_profiles)
        backtest_grid.addWidget(self.refresh_strategies_button, 1, 3)
        self.run_backtest_button = QPushButton("Run Safe Backtest Preview", backtest_card)
        self.run_backtest_button.setObjectName("PrimaryScanButton")
        self.run_backtest_button.clicked.connect(self.run_backtest)
        backtest_grid.addWidget(self.run_backtest_button, 2, 0, 1, 2)
        self.run_walk_forward_button = QPushButton("Run Walk-forward Validation", backtest_card)
        self.run_walk_forward_button.setObjectName("ChipButton")
        self.run_walk_forward_button.clicked.connect(self.run_walk_forward)
        backtest_grid.addWidget(self.run_walk_forward_button, 2, 2, 1, 2)
        backtest_layout.addWidget(backtest_card)

        self.backtester_output = QTextEdit(backtester_tab)
        self.backtester_output.setObjectName("StrategyBacktesterOutput")
        self.backtester_output.setReadOnly(True)
        self.backtester_output.setPlaceholderText(
            "Backtester output will appear here. The Phase 3 preview does not execute arbitrary strategy code."
        )
        backtest_layout.addWidget(self.backtester_output, stretch=1)

        self.lab_tabs.addTab(strategy_tab, "Strategy Lab")
        self.lab_tabs.addTab(backtester_tab, "Backtester")
        self.strategy_tab = strategy_tab
        self.backtester_tab = backtester_tab
        self.strategy_lab_tab = strategy_tab
        self.backtest_tab = backtester_tab
        layout.addWidget(self.lab_tabs, stretch=1)

    @property
    def last_tool(self) -> str:
        return self._last_tool

    def _builder_type(self) -> str:
        return "code" if self.builder_mode_combo.currentText().strip().lower().startswith("python") else "visual"

    def _flow_payload(self) -> dict[str, Any]:
        raw = self.flow_editor.toPlainText().strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Flow JSON is invalid: {exc.msg} at line {exc.lineno}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Flow specification must be a JSON object")
        return payload

    def strategy_profile_payload(self) -> dict[str, Any]:
        """Build the backend StrategySaveRequest from the visible lab fields."""

        name = self.strategy_name_input.text().strip() or "New Era Research Strategy"
        profile: dict[str, Any] = {
            "name": name,
            "builder": self._builder_type(),
            "symbol": self.strategy_symbol_combo.currentText().strip().upper() or "XAUUSD",
            "timeframe": self.strategy_timeframe_combo.currentText().strip().upper() or "M5",
            "flow": self._flow_payload(),
            "metadata": {"source": "desktop_strategy_lab", "research_only": True},
        }
        strategy_id = self.strategy_id_input.text().strip()
        if strategy_id:
            profile["strategy_id"] = strategy_id
        if profile["builder"] == "code":
            profile["module_path"] = self.module_path_input.text().strip()
            profile["class_name"] = self.class_name_input.text().strip()
        return profile

    def _backtest_target_payload(self) -> dict[str, Any]:
        selected = self.saved_strategy_combo.currentData()
        if isinstance(selected, str) and selected.strip():
            return {"strategy_id": selected.strip()}
        return {"profile": self.strategy_profile_payload()}

    def save_strategy_profile(self) -> None:
        try:
            payload = self.strategy_profile_payload()
        except ValueError as exc:
            self._set_tool_status("strategy.validation", str(exc))
            return
        self._set_tool_status("strategy.save", "Saving research-only strategy profile…")
        self.strategy_save_requested.emit(payload)

    def load_python_strategy(self) -> None:
        module_path = self.module_path_input.text().strip()
        class_name = self.class_name_input.text().strip()
        if not module_path or not class_name:
            self._set_tool_status("strategy.load.validation", "Python module path and class name are required.")
            return
        self._set_tool_status("strategy.load", f"Validating {class_name} from workspace-local module…")
        self.strategy_load_requested.emit({"module_path": module_path, "class_name": class_name})

    def run_backtest(self) -> None:
        try:
            payload = self._backtest_target_payload()
        except ValueError as exc:
            self._set_tool_status("backtester.validation", str(exc))
            return
        payload.update(
            {
                "initial_cash": 10_000.0,
                "position_size": 1.0,
                "commission": 0.0,
                "slippage": 0.0,
            }
        )
        symbol = self.backtest_symbol_combo.currentText().strip().upper() or "XAUUSD"
        timeframe = self.backtest_timeframe_combo.currentText().strip().upper() or "M5"
        payload["symbol"] = symbol
        payload["timeframe"] = timeframe
        self.backtester_output.setPlainText(
            f"BACKTEST REQUEST QUEUED\nStrategy: {self.strategy_name_input.text().strip() or 'saved profile'}\n"
            f"Symbol: {symbol}\nTimeframe: {timeframe}\n\nWaiting for backend metrics…"
        )
        self._set_tool_status("backtester.run", f"Backtest requested for {symbol} {timeframe}.")
        self.strategy_backtest_requested.emit(payload)

    def run_walk_forward(self) -> None:
        try:
            payload = self._backtest_target_payload()
        except ValueError as exc:
            self._set_tool_status("walk_forward.validation", str(exc))
            return
        payload.update(
            {
                "initial_cash": 10_000.0,
                "position_size": 1.0,
                "commission": 0.0,
                "slippage": 0.0,
                "train_ratio": 0.7,
            }
        )
        symbol = self.backtest_symbol_combo.currentText().strip().upper() or "XAUUSD"
        timeframe = self.backtest_timeframe_combo.currentText().strip().upper() or "M5"
        payload["symbol"] = symbol
        payload["timeframe"] = timeframe
        self.backtester_output.setPlainText(
            f"WALK-FORWARD REQUEST QUEUED\nStrategy: {self.strategy_name_input.text().strip() or 'saved profile'}\n"
            f"Symbol: {symbol}\nTimeframe: {timeframe}\n\nWaiting for train/validation metrics…"
        )
        self._set_tool_status("walk_forward.run", f"Walk-forward validation requested for {symbol} {timeframe}.")
        self.strategy_walk_forward_requested.emit(payload)

    def refresh_strategy_profiles(self) -> None:
        self._set_tool_status("strategy.list", "Refreshing saved strategy profiles…")
        self.tool_requested.emit("strategy.list")

    def set_strategy_profiles(self, data: Mapping[str, Any] | None) -> None:
        payload = data if isinstance(data, Mapping) else {}
        raw_profiles = payload.get("strategies")
        profiles = [item for item in raw_profiles if isinstance(item, Mapping)] if isinstance(raw_profiles, list) else []
        current = self.saved_strategy_combo.currentData()
        self.saved_strategy_combo.blockSignals(True)
        try:
            self.saved_strategy_combo.clear()
            self.saved_strategy_combo.addItem("Unsaved draft")
            for profile in profiles:
                strategy_id = _safe_text(profile.get("id"))
                if strategy_id:
                    self.saved_strategy_combo.addItem(
                        f"{strategy_id} — {_safe_text(profile.get('name'), strategy_id)}",
                        strategy_id,
                    )
            if isinstance(current, str) and current:
                index = self.saved_strategy_combo.findData(current)
                if index >= 0:
                    self.saved_strategy_combo.setCurrentIndex(index)
        finally:
            self.saved_strategy_combo.blockSignals(False)
        self.status_label.setText(f"Loaded {len(profiles)} research-only strategy profile(s).")

    def set_backend_result(self, task_name: str, data: Mapping[str, Any] | None) -> None:
        payload = dict(data) if isinstance(data, Mapping) else {}
        if task_name == "strategy_save":
            strategy_id = _safe_text(payload.get("id"), "saved-strategy")
            self.strategy_id_input.setText(strategy_id)
            self.status_label.setText(f"Saved research-only profile: {strategy_id}")
            self._last_tool = "strategy.save.complete"
            return
        if task_name == "strategy_load":
            self.status_label.setText(
                f"Validated {payload.get('class_name', 'strategy class')} — research-only loader accepted."
            )
            self._last_tool = "strategy.load.complete"
            return
        if task_name in {"strategy_backtest", "strategy_walk_forward"}:
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
            self.backtester_output.setPlainText(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            if task_name == "strategy_walk_forward":
                validation = metrics.get("validation") if isinstance(metrics, Mapping) else None
                trade_count = validation.get("trades", "--") if isinstance(validation, Mapping) else "--"
            else:
                trade_count = metrics.get("trades", "--") if isinstance(metrics, Mapping) else "--"
            self.status_label.setText(
                f"{('Walk-forward' if task_name == 'strategy_walk_forward' else 'Backtest')} complete — "
                f"trades={trade_count}"
            )
            self._last_tool = f"{task_name}.complete"

    def set_backend_error(self, task_name: str, message: str) -> None:
        if task_name in {"strategy_backtest", "strategy_walk_forward"}:
            self.backtester_output.setPlainText(f"{task_name.upper()} ERROR\n{message}")
        self.status_label.setText(f"{task_name}: {message}")

    def _set_tool_status(self, tool: str, message: str) -> None:
        self._last_tool = tool
        self.status_label.setText(message)
        self.tool_requested.emit(tool)

    def _new_strategy(self) -> None:
        self.strategy_name_input.setText("New Era Confluence Preview")
        self.signal_widget_placeholder.setText(
            "New safe strategy draft created. Add indicators, overlays, or custom signal placeholders from Insert."
        )
        self.open_strategy_lab()
        self._set_tool_status("strategy.new", "New safe strategy draft ready.")

    def _apply_risk_override(self) -> None:
        self.open_manual_risk_override()
        self._set_tool_status(
            "risk.override",
            f"Safe risk override requested at {self.risk_override_spin.value():.1f}%; backend hard caps remain active.",
        )

    def _run_backtest_preview(self) -> None:
        symbol = self.backtest_symbol_combo.currentText().strip() or "XAUUSD"
        timeframe = self.backtest_timeframe_combo.currentText().strip() or "M5"
        strategy = self.strategy_name_input.text().strip() or "Unnamed strategy"
        self.backtester_output.setPlainText(
            f"SAFE BACKTEST PREVIEW\n"
            f"Strategy: {strategy}\n"
            f"Symbol: {symbol}\n"
            f"Timeframe: {timeframe}\n\n"
            "Preview queued. No arbitrary code, shell command, or external module was executed."
        )
        self._set_tool_status("backtester.run", f"Backtester preview queued for {symbol} {timeframe}.")

    def open_strategy_lab(self) -> None:
        self.lab_tabs.setCurrentWidget(self.strategy_tab)
        self._set_tool_status("strategy.lab", "Strategy Lab is ready for safe configuration.")

    def open_backtester(self) -> None:
        self.lab_tabs.setCurrentWidget(self.backtester_tab)
        self._set_tool_status("backtester.open", "Backtester preview is ready; execution is sandbox-free.")

    def open_manual_risk_override(self) -> None:
        self.lab_tabs.setCurrentWidget(self.strategy_tab)
        self.risk_override_spin.setFocus()
        self._set_tool_status("risk.override.open", "Manual Risk Override is bounded by backend JoeyCIO hard caps.")

    def open_github_module_search(self) -> None:
        self.lab_tabs.setCurrentWidget(self.strategy_tab)
        self.signal_widget_placeholder.setText(
            "GitHub Quant Module Search placeholder opened. Review metadata-only results manually; no repository code is executed."
        )
        self._set_tool_status("github.quant.search", "GitHub Quant Module Search is metadata-only.")

    def insert_placeholder(self, widget_kind: str) -> None:
        label = _safe_text(widget_kind, "Custom Signal Widget")
        self.lab_tabs.setCurrentWidget(self.strategy_tab)
        self.signal_widget_placeholder.setText(
            f"Inserted safe placeholder: {label}. Configure it in Strategy Lab before any reviewed backend run."
        )
        self._set_tool_status(f"insert.{label.lower().replace(' ', '_')}", f"Inserted {label} placeholder.")


class Phase2WorkspaceTabs(QTabWidget):
    """Top-level four-tab Phase 2 workspace.

    ``execution_widget`` is an optional existing terminal surface.  Passing it
    lets the legacy chart, MT5, and stream panels remain intact while the tab
    layer supplies the new modular navigation.
    """

    def __init__(self, execution_widget: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Phase2WorkspaceTabs")
        self._detached_phase2_widgets: tuple[QWidget, ...] | None = None
        self.live_execution_tab = LiveExecutionTab(execution_widget, self)
        self.quant_gate_tab = QuantGateConfluenceTab(self)
        self.bot_fleet_tab = BotFleetManagerTab(self)
        self.decision_audit_tab = DecisionAuditTab(self)

        # Short aliases make the layer convenient for callers and tests.
        self.live_tab = self.live_execution_tab
        self.controls_tab = self.quant_gate_tab
        self.fleet_tab = self.bot_fleet_tab
        self.audit_tab = self.decision_audit_tab

        self.addTab(self.live_execution_tab, PHASE2_TAB_LABELS[0])
        self.addTab(self.quant_gate_tab, PHASE2_TAB_LABELS[1])
        self.addTab(self.bot_fleet_tab, PHASE2_TAB_LABELS[2])
        self.addTab(self.decision_audit_tab, PHASE2_TAB_LABELS[3])

    def detach_tabs_for_docks(self) -> tuple[QWidget, ...]:
        """Detach the Phase 2 surfaces while preserving the public tab labels.

        Phase 2 callers can continue to inspect ``phase2_tabs`` and its labels,
        while Phase 3 places the real surfaces in independent dock widgets.
        Compatibility pages are intentionally inert and retain the original
        labels for older integrations/tests.
        """

        if self._detached_phase2_widgets is not None:
            return self._detached_phase2_widgets

        entries = [(self.tabText(index), self.widget(index)) for index in range(self.count())]
        for index in range(self.count() - 1, -1, -1):
            self.removeTab(index)

        detached: list[QWidget] = []
        for index, (label, widget) in enumerate(entries):
            if widget is None:
                raise RuntimeError(f"Missing Phase 2 widget at index {index}")
            detached.append(widget)
            placeholder = QWidget(self)
            placeholder.setObjectName(f"Phase2CompatibilityPage{index}")
            placeholder.setProperty("phase2_source_object", widget.objectName())
            self.addTab(placeholder, label)
        self._detached_phase2_widgets = tuple(detached)
        self.hide()
        return self._detached_phase2_widgets

    def widget(self, index: int) -> QWidget | None:  # type: ignore[override]
        """Return the real Phase 2 surface after Phase 3 docking.

        The visible tab pages become compatibility placeholders once the real
        widgets move into docks.  Returning the detached surface here keeps the
        original ``phase2_tabs.widget(index)`` contract useful to integrations.
        """

        if self._detached_phase2_widgets is not None and 0 <= index < len(self._detached_phase2_widgets):
            return self._detached_phase2_widgets[index]
        return super().widget(index)

    def currentWidget(self) -> QWidget | None:  # type: ignore[override]
        if self._detached_phase2_widgets is not None:
            index = self.currentIndex()
            if 0 <= index < len(self._detached_phase2_widgets):
                return self._detached_phase2_widgets[index]
        return super().currentWidget()


def build_phase2_tabs(execution_widget: QWidget | None = None, parent: QWidget | None = None) -> Phase2WorkspaceTabs:
    """Build a reusable Phase 2 workspace without starting any transport."""

    return Phase2WorkspaceTabs(execution_widget=execution_widget, parent=parent)


def build_phase3_strategy_lab(parent: QWidget | None = None) -> BotStrategyLabPanel:
    """Build the safe Phase 3 Strategy Lab/backtester surface."""

    return BotStrategyLabPanel(parent=parent)


# Backwards/reader-friendly aliases for callers that prefer the requested tab
# terminology over the shorter implementation names.
QuantGateControlTab = QuantGateConfluenceTab
BotStrategyFleetTab = BotFleetManagerTab
LiveDecisionAuditTab = DecisionAuditTab
StrategyLabPanel = BotStrategyLabPanel
BotBacktesterPanel = BotStrategyLabPanel


__all__ = [
    "CONTROL_FIELDS",
    "DEFAULT_CONTROL_STATE",
    "PHASE2_TAB_LABELS",
    "BotFleetManagerTab",
    "BotStrategyFleetTab",
    "BotBacktesterPanel",
    "BotStrategyLabPanel",
    "DecisionAuditTab",
    "LiveDecisionAuditTab",
    "LiveExecutionTab",
    "Phase2WorkspaceTabs",
    "QuantGateConfluenceTab",
    "QuantGateControlTab",
    "build_phase2_tabs",
    "build_phase3_strategy_lab",
    "normalize_control_state",
    "StrategyLabPanel",
]
