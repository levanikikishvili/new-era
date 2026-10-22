"""Validated runtime controls for the New Era decision waterfall.

The controls are intentionally kept independent from the UI and from provider
configuration.  They describe the current operator-requested decision profile;
the effective values are clamped again before the protected demo execution
path and the MT5 bridge remains the final account/risk-safety boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


CONFLUENCE_MODES: tuple[str, ...] = ("2-of-3", "3-of-3", "raw")

# These are the ranges exposed by the Phase 2 control panel.  Values outside
# them are rejected by the API instead of being silently coerced.
CONTROL_RANGES: dict[str, tuple[float, float]] = {
    "risk_pct": (0.5, 5.0),
    "min_rr": (1.0, 4.0),
    "max_sl_atr": (0.5, 5.0),
    "time_stop_minutes": (30.0, 240.0),
}

# The original JoeyCIO profile remains the effective execution envelope.  A
# person may enter a wider value as an explicit demo-profile override, but the
# actual sizing/bridge path reports and applies these hard safety ceilings.
HARD_SAFETY_LIMITS: dict[str, float] = {
    "max_risk_pct": 1.5,
    "min_reward_risk": 1.5,
    "max_sl_atr": 2.5,
    "max_time_stop_minutes": 120.0,
}

DEFAULT_RUNTIME_CONTROLS: dict[str, Any] = {
    "gemini_cio_enabled": True,
    "kronos_enabled": True,
    "l2_enabled": True,
    "confluence_mode": "2-of-3",
    "risk_pct": 1.5,
    "min_rr": 1.5,
    "max_sl_atr": 2.5,
    "time_stop_minutes": 120,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _validate_float(name: str, value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return round(parsed, 6)


def _validate_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed != value and not isinstance(value, str):
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def validate_control_value(name: str, value: Any) -> Any:
    """Normalize one control and enforce the fail-closed safety envelope."""

    if name in {"gemini_cio_enabled", "kronos_enabled", "l2_enabled"}:
        return _validate_bool(name, value)
    if name == "confluence_mode":
        normalized = str(value or "").strip().lower()
        if normalized not in CONFLUENCE_MODES:
            raise ValueError(f"confluence_mode must be one of: {', '.join(CONFLUENCE_MODES)}")
        return normalized
    if name == "risk_pct":
        return _validate_float(name, value, minimum=CONTROL_RANGES[name][0], maximum=CONTROL_RANGES[name][1])
    if name == "min_rr":
        return _validate_float(name, value, minimum=CONTROL_RANGES[name][0], maximum=CONTROL_RANGES[name][1])
    if name == "max_sl_atr":
        return _validate_float(name, value, minimum=CONTROL_RANGES[name][0], maximum=CONTROL_RANGES[name][1])
    if name == "time_stop_minutes":
        return _validate_int(
            name,
            value,
            minimum=int(CONTROL_RANGES[name][0]),
            maximum=int(CONTROL_RANGES[name][1]),
        )
    raise ValueError(f"unsupported runtime control: {name}")


@dataclass
class RuntimeControls:
    """Mutable, validated decision controls with explicit override metadata."""

    gemini_cio_enabled: bool = True
    kronos_enabled: bool = True
    l2_enabled: bool = True
    confluence_mode: str = "2-of-3"
    risk_pct: float = 1.5
    min_rr: float = 1.5
    max_sl_atr: float = 2.5
    time_stop_minutes: int = 120
    _updated_at: str = field(default_factory=_utc_now_iso, repr=False)
    _override_fields: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        current = self.values()
        normalized = {name: validate_control_value(name, value) for name, value in current.items()}
        for name, value in normalized.items():
            setattr(self, name, value)
        self._override_fields = {
            name for name, value in normalized.items() if value != DEFAULT_RUNTIME_CONTROLS[name]
        }

    def values(self) -> dict[str, Any]:
        return {
            "gemini_cio_enabled": self.gemini_cio_enabled,
            "kronos_enabled": self.kronos_enabled,
            "l2_enabled": self.l2_enabled,
            "confluence_mode": self.confluence_mode,
            "risk_pct": self.risk_pct,
            "min_rr": self.min_rr,
            "max_sl_atr": self.max_sl_atr,
            "time_stop_minutes": self.time_stop_minutes,
        }

    def execution_values(self) -> dict[str, Any]:
        """Return the effective values used by the protected execution path."""

        values = self.values()
        return {
            **values,
            "risk_pct": min(float(values["risk_pct"]), HARD_SAFETY_LIMITS["max_risk_pct"]),
            "min_rr": max(float(values["min_rr"]), HARD_SAFETY_LIMITS["min_reward_risk"]),
            "max_sl_atr": min(float(values["max_sl_atr"]), HARD_SAFETY_LIMITS["max_sl_atr"]),
            "time_stop_minutes": min(
                int(values["time_stop_minutes"]),
                int(HARD_SAFETY_LIMITS["max_time_stop_minutes"]),
            ),
        }

    @property
    def safety_clamped_fields(self) -> list[str]:
        requested = self.values()
        effective = self.execution_values()
        return sorted(name for name in requested if requested[name] != effective[name])

    def update(self, updates: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Apply a partial update and return normalized values.

        Updates are validated as one candidate state before mutating the live
        object, so an invalid request cannot leave a partially applied profile.
        """

        requested: dict[str, Any] = {}
        if updates:
            requested.update(dict(updates))
        requested.update(kwargs)
        unknown = sorted(set(requested) - set(DEFAULT_RUNTIME_CONTROLS))
        if unknown:
            raise ValueError(f"unsupported runtime control(s): {', '.join(unknown)}")

        candidate = self.values()
        for name, value in requested.items():
            candidate[name] = validate_control_value(name, value)
        for name, value in candidate.items():
            setattr(self, name, value)
        self._override_fields = {
            name for name, value in candidate.items() if value != DEFAULT_RUNTIME_CONTROLS[name]
        }
        if requested:
            self._updated_at = _utc_now_iso()
        return self.values()

    @property
    def override_active(self) -> bool:
        return bool(self._override_fields)

    @property
    def override_fields(self) -> list[str]:
        return sorted(self._override_fields)

    def snapshot(self) -> dict[str, Any]:
        """Return the stable API/UI contract for the current runtime state."""

        values = self.values()
        effective = self.execution_values()
        metadata = {
            "override_active": self.override_active,
            "override_fields": self.override_fields,
            "updated_at": self._updated_at,
            "source": "explicit_override" if self.override_active else "safe_defaults",
            "defaults": dict(DEFAULT_RUNTIME_CONTROLS),
            "requested_ranges": {
                name: {"min": minimum, "max": maximum}
                for name, (minimum, maximum) in CONTROL_RANGES.items()
            },
            "allowed_confluence_modes": list(CONFLUENCE_MODES),
            "demo_only": True,
            "real_account_allowed": False,
            "safety_limits": dict(HARD_SAFETY_LIMITS),
            "effective_controls": dict(effective),
            "safety_clamped_fields": self.safety_clamped_fields,
        }
        return {
            **values,
            "controls": dict(values),
            "effective_controls": dict(effective),
            "override_active": self.override_active,
            "override_fields": self.override_fields,
            "metadata": metadata,
        }


__all__ = [
    "CONFLUENCE_MODES",
    "CONTROL_RANGES",
    "DEFAULT_RUNTIME_CONTROLS",
    "HARD_SAFETY_LIMITS",
    "RuntimeControls",
    "validate_control_value",
]
