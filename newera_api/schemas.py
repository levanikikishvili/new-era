"""Pydantic DTOs exposed to a local UI or future desktop client."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class ScanRequest(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    provider: str | None = Field(default=None, max_length=40)
    input_csv: str | None = Field(default=None, max_length=260)
    spread_points: float | None = Field(default=None, ge=0, le=10000)
    include_tradingagents: bool = True
    include_kronos: bool = True
    gate_ttl_minutes: int = Field(default=90, ge=1, le=1440)
    timeout_seconds: int | None = Field(default=None, ge=30, le=7200)

    @field_validator("symbol")
    @classmethod
    def safe_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
        if not value or any(character not in allowed for character in value):
            raise ValueError("symbol contains unsupported characters")
        return value

    @field_validator("provider")
    @classmethod
    def clean_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        return value or None


class ScanResponse(BaseModel):
    scan_id: str
    status: Literal["queued", "running", "completed", "failed"]
    mode: Literal["manual", "automatic"]
    message: str


class ScanStatus(BaseModel):
    scan_id: str
    status: Literal["queued", "running", "completed", "failed"]
    mode: Literal["manual", "automatic"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    last_event: dict[str, Any] | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class MemorySearchResponse(BaseModel):
    query: str
    results: list[dict[str, Any]]


class DemoOrderRequest(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    side: Literal["BUY", "SELL"] = "BUY"
    volume: float = Field(default=0.01, ge=0.01, le=50.0)
    sl: float = Field(default=0.0, ge=0.0)
    tp: float = Field(default=0.0, ge=0.0)
    comment: str = Field(default="NewEraQuantDemo", max_length=31)
    allow_paper_fallback: bool = False

    @field_validator("symbol")
    @classmethod
    def safe_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
        if not value or any(character not in allowed for character in value):
            raise ValueError("symbol contains unsupported characters")
        return value


class DemoCloseRequest(BaseModel):
    symbol: str | None = Field(default=None, max_length=32)
    ticket: int | None = Field(default=None, ge=1)


class DemoManageRequest(BaseModel):
    symbol: str | None = Field(default=None, max_length=32)
    price_override: float | None = Field(default=None, gt=0.0)
    atr_override: float | None = Field(default=None, gt=0.0)


class GeminiChatRequest(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    message: str = Field(min_length=1, max_length=2000)


class GeminiAnalyzeRequest(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    account_equity: float = Field(default=1000.0, gt=0.0)


class AIAnalyzeRequest(BaseModel):
    """Read-only local Ollama analysis request.

    The optional context fields are research inputs only. The backend bounds
    and sanitizes them again before they reach the model transport.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    context: dict[str, Any] = Field(default_factory=dict)
    ohlcv: list[dict[str, Any]] | None = None
    indicators: dict[str, Any] | None = None
    kronos_matrix: dict[str, Any] | None = None
    l2_dom: dict[str, Any] | None = None

    @field_validator("symbol")
    @classmethod
    def safe_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
        if not value or any(character not in allowed for character in value):
            raise ValueError("symbol contains unsupported characters")
        return value


class AutonomousModeRequest(BaseModel):
    autonomous_mode: bool = False
    dynamic_trailing_enabled: bool = True
    paused: bool = False


class ControlsUpdateRequest(BaseModel):
    """Partial runtime decision-control update.

    The fields accept the Phase 2 operator ranges.  The response also exposes
    the effective protected values; the demo-only execution boundary remains
    hard-capped even when a wider override is requested.
    """

    model_config = ConfigDict(extra="forbid")

    gemini_cio_enabled: StrictBool | None = None
    kronos_enabled: StrictBool | None = None
    l2_enabled: StrictBool | None = None
    confluence_mode: str | None = None
    risk_pct: float | None = Field(default=None, ge=0.5, le=5.0)
    min_rr: float | None = Field(default=None, ge=1.0, le=4.0)
    max_sl_atr: float | None = Field(default=None, ge=0.5, le=5.0)
    time_stop_minutes: int | None = Field(default=None, ge=30, le=240)

    @field_validator("confluence_mode")
    @classmethod
    def clean_confluence_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"2-of-3", "3-of-3", "raw"}:
            raise ValueError("confluence_mode must be one of: 2-of-3, 3-of-3, raw")
        return normalized


class BotProfileCreateRequest(BaseModel):
    """Create a research-only bot profile without executable artifacts."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    bot_id: str | None = Field(default=None, min_length=2, max_length=64)
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)


class BotEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class StrategyProfileRequest(BaseModel):
    """Research-only visual/code strategy profile payload."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str | None = Field(default=None, alias="strategy_id", min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    builder: Literal["visual", "code"] = "visual"
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    timeframe: str = Field(default="M5", min_length=1, max_length=20)
    description: str = Field(default="", max_length=2000)
    flow: dict[str, Any] = Field(default_factory=dict)
    code: dict[str, Any] | None = None
    module_path: str | None = Field(default=None, max_length=400)
    class_name: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] | None = None


class StrategySaveRequest(StrategyProfileRequest):
    """Alias-shaped DTO used by the save endpoint."""


class OHLCVBarRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: str | int | float | None = None
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    strategy_id: str | None = Field(default=None, min_length=2, max_length=64)
    profile: StrategyProfileRequest | None = None
    strategy: StrategyProfileRequest | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    timeframe: str | None = Field(default=None, min_length=1, max_length=20)
    bars: list[dict[str, Any]] | list[OHLCVBarRequest] | None = None
    ohlcv: list[dict[str, Any]] | list[OHLCVBarRequest] | None = None
    data: list[dict[str, Any]] | list[OHLCVBarRequest] | None = None
    data_path: str | None = Field(default=None, alias="csv_path", max_length=400)
    initial_cash: float = Field(default=10000.0, gt=0.0)
    position_size: float | None = Field(default=None, gt=0.0)
    commission: float = Field(default=0.0, ge=0.0)
    slippage: float = Field(default=0.0, ge=0.0)


class WalkForwardRequest(BacktestRequest):
    train_ratio: float = Field(default=0.7, ge=0.1, le=0.9)
    train_bars: int | None = Field(default=None, ge=2)


class StrategyLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str | None = Field(default=None, min_length=2, max_length=64)
    module_path: str | None = Field(default=None, max_length=400)
    class_name: str | None = Field(default=None, max_length=128)


# Friendly aliases for clients that use the Bot Strategy Lab naming.
BotStrategyProfileRequest = StrategyProfileRequest
BotBacktestRequest = BacktestRequest
BotWalkForwardRequest = WalkForwardRequest
BotStrategyLoadRequest = StrategyLoadRequest


class TelegramCommandRequest(BaseModel):
    command: str = Field(default="/status", min_length=1, max_length=120)
