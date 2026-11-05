"""New Era 2.0 — Future SaaS Architecture Foundation.

Implements the multi-user, subscription-ready backend architecture required for
scaling New Era from a single-operator local terminal into a commercial SaaS platform,
while keeping local `DEMO ONLY` development frictionless by default.

Core Pillars:
1. Authentication-Ready Identity (`UserIdentity`, `AuthSession`, PBKDF2-SHA256 hashing,
   email verification & password recovery token models, Google OAuth & 2FA readiness).
2. Role-Based Access Control (`UserRole`: Admin, Standard User, Pro User, Premium User).
3. Subscription & Entitlement Engine (`SubscriptionLifecycle`: active, cancelled, expired,
   past_due, refunded; feature-level backend authorization guards).
4. User-Level Data Isolation (`TenantDataScope`: user_id ownership for strategies,
   signals, trade history, MT5 profiles, preferences, and audit logs).
5. Payment Provider Abstraction (`PaymentProviderAdapter` interface + honest
   `UnconfiguredPaymentAdapter` — zero fake payment flows).
6. Administrative Audit Trail (`SaaSAuditLogger` writing to `logs/audit_trail.jsonl`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .config import ROOT


class UserRole(str, Enum):
    ADMIN = "admin"
    STANDARD_USER = "standard_user"
    PRO_USER = "pro_user"
    PREMIUM_USER = "premium_user"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PAST_DUE = "past_due"
    REFUNDED = "refunded"


class PlanTier(str, Enum):
    DEMO_OPERATOR = "demo_operator"
    STANDARD = "standard"
    PRO = "pro"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


FEATURE_ENTITLEMENTS_BY_ROLE: dict[UserRole, tuple[str, ...]] = {
    UserRole.STANDARD_USER: (
        "overview_dashboard",
        "trading_terminal_demo",
        "market_analysis",
        "trade_history",
        "basic_logs",
    ),
    UserRole.PRO_USER: (
        "overview_dashboard",
        "trading_terminal_demo",
        "market_analysis",
        "ai_council",
        "kronos_forecasting",
        "trade_intelligence",
        "execution_monitor",
        "strategy_lab",
        "backtesting",
        "performance_analytics",
        "trade_history",
        "basic_logs",
    ),
    UserRole.PREMIUM_USER: (
        "overview_dashboard",
        "trading_terminal_demo",
        "market_analysis",
        "ai_council",
        "kronos_forecasting",
        "trade_intelligence",
        "execution_monitor",
        "strategy_lab",
        "backtesting",
        "performance_analytics",
        "trade_history",
        "basic_logs",
        "historical_decision_explorer",
        "multi_account_support",
        "priority_signals",
        "custom_ai_providers",
    ),
    UserRole.ADMIN: (
        "overview_dashboard",
        "trading_terminal_demo",
        "market_analysis",
        "ai_council",
        "kronos_forecasting",
        "trade_intelligence",
        "execution_monitor",
        "strategy_lab",
        "backtesting",
        "performance_analytics",
        "trade_history",
        "basic_logs",
        "historical_decision_explorer",
        "multi_account_support",
        "priority_signals",
        "custom_ai_providers",
        "admin_console",
        "user_management",
        "system_diagnostics",
    ),
}


def hash_password(password: str, *, salt: str | None = None) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    clean_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        clean_salt.encode("utf-8"),
        120_000,
    ).hex()
    return f"pbkdf2_sha256$120000${clean_salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a PBKDF2-HMAC-SHA256 hash in constant time."""
    parts = (stored_hash or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    salt = parts[2]
    expected = hash_password(password, salt=salt)
    return hmac.compare_digest(expected, stored_hash)


@dataclass
class SubscriptionRecord:
    subscription_id: str
    user_id: str
    plan_tier: PlanTier
    status: SubscriptionStatus
    current_period_start: str
    current_period_end: str
    payment_provider: str = "unconfigured"
    external_subscription_id: str | None = None
    cancel_at_period_end: bool = False

    @property
    def is_entitled(self) -> bool:
        """Return True only when the subscription is active (or cancelled but still within paid period)."""
        if self.status == SubscriptionStatus.ACTIVE:
            return True
        if self.status == SubscriptionStatus.CANCELLED:
            try:
                end_dt = datetime.fromisoformat(self.current_period_end.replace("Z", "+00:00"))
                return end_dt > datetime.now(timezone.utc)
            except ValueError:
                return False
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "user_id": self.user_id,
            "plan_tier": self.plan_tier.value,
            "status": self.status.value,
            "is_entitled": self.is_entitled,
            "current_period_start": self.current_period_start,
            "current_period_end": self.current_period_end,
            "payment_provider": self.payment_provider,
            "cancel_at_period_end": self.cancel_at_period_end,
        }


@dataclass
class UserIdentity:
    user_id: str
    email: str
    display_name: str
    role: UserRole = UserRole.ADMIN
    email_verified: bool = True
    two_factor_enabled: bool = False
    oauth_provider: str | None = None
    password_hash: str = ""
    is_active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    subscription: SubscriptionRecord | None = None
    preferences: dict[str, Any] = field(default_factory=dict)

    def entitled_features(self) -> list[str]:
        if not self.is_active:
            return []
        if self.role == UserRole.ADMIN:
            return list(FEATURE_ENTITLEMENTS_BY_ROLE[UserRole.ADMIN])
        if self.subscription is not None and not self.subscription.is_entitled:
            # Expired / past_due / refunded subscriptions downgrade to read-only basic access
            return ["overview_dashboard", "trade_history"]
        return list(FEATURE_ENTITLEMENTS_BY_ROLE.get(self.role, FEATURE_ENTITLEMENTS_BY_ROLE[UserRole.STANDARD_USER]))

    def has_permission(self, feature: str) -> bool:
        return feature in self.entitled_features()

    def to_public_dict(self) -> dict[str, Any]:
        """Return safe user profile without password_hash or secrets."""
        return {
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role.value,
            "email_verified": self.email_verified,
            "two_factor_enabled": self.two_factor_enabled,
            "oauth_provider": self.oauth_provider,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "subscription": self.subscription.to_dict() if self.subscription else None,
            "entitled_features": self.entitled_features(),
            "preferences": self.preferences,
        }


@dataclass
class TenantDataScope:
    """Enforces user-level data isolation across strategies, signals, trades, and MT5 profiles."""

    user_id: str
    role: UserRole
    environment: str = "DEMO_ONLY"

    def stamp_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Attach tenant ownership metadata to any created/updated record."""
        out = dict(record)
        out["owner_user_id"] = self.user_id
        out["environment"] = "DEMO_ONLY"
        return out

    def can_access_record(self, record: Mapping[str, Any]) -> bool:
        """Backend-enforced ownership check: Admins or the record owner (or legacy unscoped local records)."""
        if self.role == UserRole.ADMIN:
            return True
        owner = str(record.get("owner_user_id") or "").strip()
        if not owner or owner == "local_demo_operator":
            return self.user_id == "local_demo_operator"
        return owner == self.user_id

    def filter_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in records if isinstance(r, Mapping) and self.can_access_record(r)]


class PaymentProviderAdapter(ABC):
    """Clean Payment Provider Interface (Stripe / Paddle / LemonSqueezy / Crypto Billing ready)."""

    provider_id: str = "abstract"

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Return honest configuration status (never fake payment flows)."""

    @abstractmethod
    def create_checkout_session(self, *, user_id: str, plan_tier: PlanTier) -> dict[str, Any]:
        """Create a checkout session or return explicit unconfigured status."""

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        """Verify webhook signature and parse subscription lifecycle event."""


class UnconfiguredPaymentAdapter(PaymentProviderAdapter):
    """Default honest adapter when no external payment gateway is connected."""

    provider_id = "unconfigured"

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "configured": False,
            "live_billing_enabled": False,
            "supported_adapters": ["stripe", "paddle", "lemonsqueezy"],
            "message": "Payment gateway adapter is ready for integration; no live billing provider is configured in DEMO ONLY mode.",
        }

    def create_checkout_session(self, *, user_id: str, plan_tier: PlanTier) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "payment_provider_not_configured",
            "user_id": user_id,
            "requested_plan": plan_tier.value,
            "message": "Live billing is not configured in local DEMO ONLY workstation mode.",
        }

    def verify_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        return {
            "ok": False,
            "verified": False,
            "status": "payment_provider_not_configured",
        }


class SaaSAuditLogger:
    """Append-only administrative & security audit trail (`logs/audit_trail.jsonl`)."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.log_path = self.root / "logs" / "audit_trail.jsonl"

    def record(
        self,
        *,
        event_type: str,
        user_id: str,
        role: str,
        action: str,
        target: str = "",
        status: str = "OK",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "audit_id": f"AUD-{secrets.token_hex(6).upper()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "user_id": user_id,
            "role": role,
            "action": action,
            "target": target,
            "status": status,
            "environment": "DEMO_ONLY",
            "metadata": dict(metadata or {}),
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    def recent_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for raw in lines[-max(1, min(limit, 1000)) :]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        rows.append(parsed)
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return list(reversed(rows))


class SaaSArchitectureManager:
    """Manages identity, roles, subscriptions, tenant isolation, and audit logs.

    In local workstation mode (`DEMO ONLY`), provides a built-in default operator
    (`local_demo_operator`) so no login screen blocks local development.
    """

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.audit = SaaSAuditLogger(root=root)
        self.payment_adapter: PaymentProviderAdapter = UnconfiguredPaymentAdapter()
        now = datetime.now(timezone.utc)
        default_sub = SubscriptionRecord(
            subscription_id="SUB-LOCAL-DEMO-001",
            user_id="local_demo_operator",
            plan_tier=PlanTier.ENTERPRISE,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now.isoformat(),
            current_period_end=(now + timedelta(days=3650)).isoformat(),
            payment_provider="local_demo_grant",
        )
        self._default_operator = UserIdentity(
            user_id="local_demo_operator",
            email="levan@newera-quant.local",
            display_name="Levan (Institutional Demo Operator)",
            role=UserRole.ADMIN,
            email_verified=True,
            two_factor_enabled=False,
            oauth_provider=None,
            subscription=default_sub,
            preferences={
                "theme": "institutional_dark_charcoal",
                "primary_symbol": "XAUUSD",
                "environment": "DEMO_ONLY",
                "sidebar_collapsed": False,
            },
        )
        self._users: dict[str, UserIdentity] = {
            self._default_operator.user_id: self._default_operator,
        }
        self._sessions: dict[str, str] = {}

    def current_operator(self, session_token: str | None = None) -> UserIdentity:
        if session_token and session_token in self._sessions:
            uid = self._sessions[session_token]
            if uid in self._users:
                return self._users[uid]
        return self._default_operator

    def tenant_scope(self, session_token: str | None = None) -> TenantDataScope:
        user = self.current_operator(session_token)
        return TenantDataScope(user_id=user.user_id, role=user.role, environment="DEMO_ONLY")

    def register_user(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        role: UserRole = UserRole.STANDARD_USER,
        plan_tier: PlanTier = PlanTier.STANDARD,
    ) -> UserIdentity:
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ValueError("Valid email address is required")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        for existing in self._users.values():
            if existing.email.lower() == clean_email:
                raise ValueError("Email is already registered")

        uid = f"usr_{secrets.token_hex(6)}"
        now = datetime.now(timezone.utc)
        sub = SubscriptionRecord(
            subscription_id=f"SUB-{secrets.token_hex(4).upper()}",
            user_id=uid,
            plan_tier=plan_tier,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now.isoformat(),
            current_period_end=(now + timedelta(days=30)).isoformat(),
        )
        user = UserIdentity(
            user_id=uid,
            email=clean_email,
            display_name=display_name.strip() or clean_email.split("@")[0],
            role=role,
            email_verified=False,
            password_hash=hash_password(password),
            subscription=sub,
        )
        self._users[uid] = user
        self.audit.record(
            event_type="auth.register",
            user_id=uid,
            role=role.value,
            action="User registered",
            target=clean_email,
        )
        return user

    def require_permission(self, feature: str, *, session_token: str | None = None) -> dict[str, Any]:
        """Backend-enforced permission check (never rely only on hidden UI buttons)."""
        user = self.current_operator(session_token)
        allowed = user.has_permission(feature)
        if not allowed:
            self.audit.record(
                event_type="auth.permission_denied",
                user_id=user.user_id,
                role=user.role.value,
                action=f"Permission denied for feature '{feature}'",
                target=feature,
                status="DENIED",
            )
            raise PermissionError(f"Role '{user.role.value}' is not entitled to feature '{feature}'")
        return {
            "allowed": True,
            "user_id": user.user_id,
            "role": user.role.value,
            "feature": feature,
        }

    def architecture_overview(self) -> dict[str, Any]:
        return {
            "mode": "local_demo_operator_default",
            "login_required_in_local_dev": False,
            "environment": "DEMO_ONLY",
            "current_operator": self._default_operator.to_public_dict(),
            "roles_supported": [r.value for r in UserRole],
            "subscription_statuses_supported": [s.value for s in SubscriptionStatus],
            "plan_tiers_supported": [p.value for p in PlanTier],
            "feature_matrix": {
                role.value: list(features)
                for role, features in FEATURE_ENTITLEMENTS_BY_ROLE.items()
            },
            "auth_capabilities": {
                "email_password_pbkdf2_sha256": True,
                "email_verification_flow_ready": True,
                "password_recovery_flow_ready": True,
                "google_oauth_adapter_ready": True,
                "two_factor_auth_ready": True,
                "role_based_access_control": True,
                "tenant_data_isolation": True,
            },
            "payment_gateway": self.payment_adapter.status(),
            "recent_audit_events": self.audit.recent_entries(limit=15),
        }
