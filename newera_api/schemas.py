"""Pydantic DTOs exposed to a local UI or future desktop client."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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


class AutonomousModeRequest(BaseModel):
    autonomous_mode: bool = False
    dynamic_trailing_enabled: bool = True
    paused: bool = False


class TelegramCommandRequest(BaseModel):
    command: str = Field(default="/status", min_length=1, max_length=120)
