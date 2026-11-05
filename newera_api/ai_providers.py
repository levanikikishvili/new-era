"""New Era 2.0 — Clean AI Provider Abstraction Layer.

Supports:
1. OpenAI API (`OpenAIProvider`) — Primary external AI provider.
   Reads `OPENAI_API_KEY` exclusively from backend environment / `.env`.
   Never exposes API keys or secrets in frontend payloads, logs, or error strings.
2. Google Gemini API (`GeminiProvider`) — Secondary / legacy external AI provider.
   Reads `GEMINI_API_KEY` or `GOOGLE_API_KEY` from backend environment / `.env`.
3. Local Ollama Provider (`LocalOllamaProvider`) — Optional local LLM provider.
   Disabled / offline by default so it never slows down the 16 GB RAM / AMD Radeon system.
4. Deterministic Quant Fallback (`DeterministicQuantProvider`) — Zero-latency,
   fail-closed quantitative reasoning engine when external AI is offline or disabled.

Safety Contract:
- External AI providers act ONLY as advisory / CIO sanity-check / co-pilot layers.
- An external AI provider can never bypass deterministic risk limits (`MAX_RISK_PCT = 1.5%`,
  `MIN_REWARD_RISK = 1.5`, `MAX_SL_ATR_MULT = 2.5`) or `DEMO ONLY` execution guards.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ROOT, load_workspace_dotenv


DEFAULT_OPENAI_MODELS: tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
)

DEFAULT_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
)

DEFAULT_LOCAL_OLLAMA_MODEL = "deepseek-r1:8b"
DEFAULT_LOCAL_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/generate"

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{16,}|Bearer\s+[A-Za-z0-9_\-\.]+)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Ensure API keys or bearer tokens never leak into logs, UI status, or exceptions."""
    if not text:
        return ""
    return _SECRET_PATTERN.sub("[REDACTED_SECRET]", str(text))


@dataclass
class AIProviderResponse:
    provider_id: str
    provider_name: str
    model: str
    ok: bool
    text: str = ""
    json_data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_public_dict(self) -> dict[str, Any]:
        """Return a sanitized dictionary safe for API/UI telemetry."""
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "model": self.model,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "error": redact_secrets(self.error)[:240] if self.error else "",
            "timestamp": self.timestamp,
        }


class AIProvider(ABC):
    """Abstract base class for all New Era 2.0 AI providers."""

    provider_id: str = "base"
    provider_name: str = "Base AI Provider"

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if credentials/endpoints are configured (without exposing secrets)."""

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Return True if this provider is enabled for runtime requests."""

    @abstractmethod
    def status_snapshot(self) -> dict[str, Any]:
        """Return non-secret status metadata for the UI Settings / Header."""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 18.0,
    ) -> AIProviderResponse:
        """Execute a synchronous generation request with strict timeout and error handling."""


class OpenAIProvider(AIProvider):
    """Primary External AI Provider using the OpenAI Chat Completions REST API."""

    provider_id = "openai"
    provider_name = "OpenAI API"

    def __init__(self, root: Path = ROOT, *, enabled: bool = True) -> None:
        self.root = root
        self._enabled = bool(enabled)
        self.active_model: str | None = None
        self.last_ok_at: str | None = None
        self.last_error: str = ""
        self.last_latency_ms: float = 0.0
        self._dead_models: set[str] = set()

    def _get_api_key(self) -> str:
        load_workspace_dotenv(self.root)
        return os.getenv("OPENAI_API_KEY", "").strip()

    def _get_base_url(self) -> str:
        load_workspace_dotenv(self.root)
        raw = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        return raw or "https://api.openai.com/v1"

    @property
    def is_configured(self) -> bool:
        key = self._get_api_key()
        return bool(key and not key.startswith("your_") and len(key) >= 12)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def model_candidates(self) -> list[str]:
        load_workspace_dotenv(self.root)
        override = [m.strip() for m in os.getenv("OPENAI_MODEL", "").split(",") if m.strip()]
        ordered: list[str] = []
        for name in ([self.active_model] if self.active_model else []) + override + list(DEFAULT_OPENAI_MODELS):
            if name and name not in ordered and name not in self._dead_models:
                ordered.append(name)
        return ordered or list(DEFAULT_OPENAI_MODELS)

    def status_snapshot(self) -> dict[str, Any]:
        candidates = self.model_candidates()
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "role": "primary_external",
            "enabled": self.is_enabled,
            "configured": self.is_configured,
            "key_source": "backend_env:OPENAI_API_KEY",
            "key_present": self.is_configured,
            "live": bool(self.active_model and self.last_ok_at and not self.last_error),
            "active_model": self.active_model or (candidates[0] if candidates else "gpt-4o-mini"),
            "available_models": candidates,
            "last_ok_at": self.last_ok_at,
            "last_error": redact_secrets(self.last_error),
            "last_latency_ms": round(self.last_latency_ms, 1),
        }

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 18.0,
    ) -> AIProviderResponse:
        if not self.is_enabled:
            self.last_error = "OpenAI provider is disabled in settings"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.active_model or DEFAULT_OPENAI_MODELS[0],
                ok=False,
                error=self.last_error,
            )

        api_key = self._get_api_key()
        if not api_key or api_key.startswith("your_"):
            self.last_error = "OPENAI_API_KEY not configured in backend environment"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.active_model or DEFAULT_OPENAI_MODELS[0],
                ok=False,
                error=self.last_error,
            )

        base_url = self._get_base_url()
        deadline = time.monotonic() + max(3.0, float(timeout))
        for model_name in self.model_candidates():
            remaining = deadline - time.monotonic()
            if remaining < 1.5:
                self.last_error = self.last_error or "OpenAI request timeout budget exhausted"
                break

            payload: dict[str, Any] = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as http_exc:
                code = int(getattr(http_exc, "code", 0) or 0)
                self.last_error = f"{model_name}: HTTP {code}"
                if code == 404:
                    self._dead_models.add(model_name)
                    continue
                if code in {401, 403}:
                    self.last_error = f"OpenAI authentication failed (HTTP {code})"
                    break
                continue
            except Exception as exc:
                self.last_error = f"{model_name}: {type(exc).__name__}"
                continue

            latency_ms = (time.perf_counter() - t0) * 1000.0
            choices = data.get("choices") or []
            if not choices:
                self.last_error = f"{model_name}: empty choices"
                continue
            message = (choices[0].get("message") or {}) if isinstance(choices[0], Mapping) else {}
            content = str(message.get("content") or "").strip()
            if not content:
                self.last_error = f"{model_name}: empty message content"
                continue

            self.active_model = model_name
            self.last_ok_at = datetime.now(timezone.utc).isoformat()
            self.last_error = ""
            self.last_latency_ms = latency_ms
            parsed_json = _extract_json_object(content) if json_mode else {}
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=model_name,
                ok=True,
                text=content,
                json_data=parsed_json,
                latency_ms=latency_ms,
            )

        return AIProviderResponse(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            model=self.active_model or DEFAULT_OPENAI_MODELS[0],
            ok=False,
            error=redact_secrets(self.last_error or "OpenAI request failed"),
        )


class GeminiProvider(AIProvider):
    """Secondary / Legacy External AI Provider (Google Gemini REST API)."""

    provider_id = "gemini"
    provider_name = "Google Gemini API"

    def __init__(self, root: Path = ROOT, *, enabled: bool = True) -> None:
        self.root = root
        self._enabled = bool(enabled)
        self.active_model: str | None = None
        self.last_ok_at: str | None = None
        self.last_error: str = ""
        self.last_latency_ms: float = 0.0
        self._dead_models: set[str] = set()

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

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def model_candidates(self) -> list[str]:
        load_workspace_dotenv(self.root)
        override = [m.strip() for m in os.getenv("GEMINI_MODEL", "").split(",") if m.strip()]
        ordered: list[str] = []
        for name in ([self.active_model] if self.active_model else []) + override + list(DEFAULT_GEMINI_MODELS):
            if name and name not in ordered and name not in self._dead_models:
                ordered.append(name)
        return ordered or list(DEFAULT_GEMINI_MODELS)

    def status_snapshot(self) -> dict[str, Any]:
        candidates = self.model_candidates()
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "role": "secondary_external",
            "enabled": self.is_enabled,
            "configured": self.is_configured,
            "key_source": "backend_env:GEMINI_API_KEY",
            "key_present": self.is_configured,
            "live": bool(self.active_model and self.last_ok_at and not self.last_error),
            "active_model": self.active_model or (candidates[0] if candidates else "gemini-2.5-flash"),
            "available_models": candidates,
            "last_ok_at": self.last_ok_at,
            "last_error": redact_secrets(self.last_error),
            "last_latency_ms": round(self.last_latency_ms, 1),
        }

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 18.0,
    ) -> AIProviderResponse:
        if not self.is_enabled:
            self.last_error = "Gemini provider is disabled in settings"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.active_model or DEFAULT_GEMINI_MODELS[0],
                ok=False,
                error=self.last_error,
            )

        api_key = self._get_api_key()
        if not api_key or api_key.startswith("your_"):
            self.last_error = "GEMINI_API_KEY not configured"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.active_model or DEFAULT_GEMINI_MODELS[0],
                ok=False,
                error=self.last_error,
            )

        gen_cfg: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        deadline = time.monotonic() + max(3.0, float(timeout))
        for model_name in self.model_candidates():
            remaining = deadline - time.monotonic()
            if remaining < 1.5:
                self.last_error = self.last_error or "timeout budget exhausted"
                break
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                data=raw_body,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=remaining) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as http_exc:
                code = int(getattr(http_exc, "code", 0) or 0)
                self.last_error = f"{model_name}: HTTP {code}"
                if code == 404:
                    self._dead_models.add(model_name)
                    continue
                if code in {401, 403}:
                    break
                continue
            except Exception as exc:
                self.last_error = f"{model_name}: {type(exc).__name__}"
                continue

            latency_ms = (time.perf_counter() - t0) * 1000.0
            candidates = data.get("candidates") or []
            if not candidates:
                self.last_error = f"{model_name}: empty response"
                continue
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip()
            if not text:
                self.last_error = f"{model_name}: no text"
                continue
            self.active_model = model_name
            self.last_ok_at = datetime.now(timezone.utc).isoformat()
            self.last_error = ""
            self.last_latency_ms = latency_ms
            parsed_json = _extract_json_object(text) if json_mode else {}
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=model_name,
                ok=True,
                text=text,
                json_data=parsed_json,
                latency_ms=latency_ms,
            )

        return AIProviderResponse(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            model=self.active_model or DEFAULT_GEMINI_MODELS[0],
            ok=False,
            error=redact_secrets(self.last_error or "Gemini request failed"),
        )


class LocalOllamaProvider(AIProvider):
    """Optional Local LLM Provider (disabled/offline by default to protect RAM/GPU)."""

    provider_id = "local_ollama"
    provider_name = "Local AI (Ollama — Optional)"

    def __init__(
        self,
        *,
        enabled: bool = False,
        model: str = DEFAULT_LOCAL_OLLAMA_MODEL,
        endpoint: str = DEFAULT_LOCAL_OLLAMA_ENDPOINT,
    ) -> None:
        self._enabled = bool(enabled)
        self.model = model or DEFAULT_LOCAL_OLLAMA_MODEL
        self.endpoint = endpoint or DEFAULT_LOCAL_OLLAMA_ENDPOINT
        self.last_ok_at: str | None = None
        self.last_error: str = ""
        self.last_latency_ms: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.model)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "role": "optional_local",
            "enabled": self.is_enabled,
            "configured": self.is_configured,
            "default_state": "disabled_by_default",
            "live": bool(self.is_enabled and self.last_ok_at and not self.last_error),
            "active_model": self.model,
            "endpoint": self.endpoint,
            "last_ok_at": self.last_ok_at,
            "last_error": redact_secrets(self.last_error),
            "last_latency_ms": round(self.last_latency_ms, 1),
        }

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        timeout: float = 10.0,
    ) -> AIProviderResponse:
        if not self.is_enabled:
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.model,
                ok=False,
                error="Local Ollama provider is disabled by default (enable in Settings -> AI Providers)",
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": f"{system_prompt}\n\n{user_prompt}",
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            self.last_error = f"Local Ollama unreachable: {type(exc).__name__}"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.model,
                ok=False,
                error=self.last_error,
            )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        text = str(data.get("response") or "").strip()
        if not text:
            self.last_error = "Local Ollama returned empty response"
            return AIProviderResponse(
                provider_id=self.provider_id,
                provider_name=self.provider_name,
                model=self.model,
                ok=False,
                error=self.last_error,
            )
        self.last_ok_at = datetime.now(timezone.utc).isoformat()
        self.last_error = ""
        self.last_latency_ms = latency_ms
        return AIProviderResponse(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            model=self.model,
            ok=True,
            text=text,
            json_data=_extract_json_object(text) if json_mode else {},
            latency_ms=latency_ms,
        )


def _extract_json_object(raw: str) -> dict[str, Any]:
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


class AIProviderRouter:
    """Central AI Provider Router for New Era 2.0.

    Priority order:
    1. Selected primary provider (`openai` by default)
    2. Secondary configured cloud provider (`gemini` if enabled and configured)
    3. Optional local provider (`local_ollama` only if explicitly enabled by user)
    4. Deterministic Quant Fallback (always available, fail-closed)
    """

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        load_workspace_dotenv(self.root)
        env_primary = os.getenv("NEWERA_AI_PROVIDER", "openai").strip().lower()
        self.primary_provider_id: str = env_primary if env_primary in {"openai", "gemini", "local_ollama"} else "openai"
        self.openai = OpenAIProvider(root=root, enabled=True)
        self.gemini = GeminiProvider(root=root, enabled=True)
        local_opt_in = os.getenv("NEWERA_LOCAL_AI_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        self.local_ollama = LocalOllamaProvider(enabled=local_opt_in)

    def get_provider(self, provider_id: str) -> AIProvider | None:
        norm = (provider_id or "").strip().lower()
        if norm == "openai":
            return self.openai
        if norm == "gemini":
            return self.gemini
        if norm in {"local_ollama", "ollama", "local"}:
            return self.local_ollama
        return None

    def ordered_providers(self) -> list[AIProvider]:
        all_map: dict[str, AIProvider] = {
            "openai": self.openai,
            "gemini": self.gemini,
            "local_ollama": self.local_ollama,
        }
        ordered: list[AIProvider] = []
        primary = all_map.get(self.primary_provider_id)
        if primary is not None:
            ordered.append(primary)
        for pid in ("openai", "gemini", "local_ollama"):
            prov = all_map[pid]
            if prov not in ordered:
                ordered.append(prov)
        return ordered

    def configure(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Update non-secret runtime provider preferences."""
        if "primary_provider" in payload:
            cand = str(payload.get("primary_provider") or "").strip().lower()
            if cand in {"openai", "gemini", "local_ollama"}:
                self.primary_provider_id = cand
        if "openai_enabled" in payload:
            self.openai.set_enabled(bool(payload.get("openai_enabled")))
        if "gemini_enabled" in payload:
            self.gemini.set_enabled(bool(payload.get("gemini_enabled")))
        if "local_ollama_enabled" in payload:
            self.local_ollama.set_enabled(bool(payload.get("local_ollama_enabled")))
        if "local_ollama_model" in payload and str(payload.get("local_ollama_model") or "").strip():
            self.local_ollama.model = str(payload["local_ollama_model"]).strip()
        return self.status_snapshot()

    def status_snapshot(self) -> dict[str, Any]:
        openai_st = self.openai.status_snapshot()
        gemini_st = self.gemini.status_snapshot()
        ollama_st = self.local_ollama.status_snapshot()
        any_cloud_configured = bool(openai_st["configured"] or gemini_st["configured"])
        active_prov = self.primary_provider_id
        if active_prov == "openai" and not openai_st["configured"] and gemini_st["configured"]:
            active_prov = "gemini"
        return {
            "primary_provider": self.primary_provider_id,
            "effective_provider": active_prov if any_cloud_configured else "deterministic_quant_fallback",
            "any_external_configured": any_cloud_configured,
            "local_ai_optional_and_disabled_by_default": True,
            "secret_handling": "backend_env_only_never_exposed",
            "providers": {
                "openai": openai_st,
                "gemini": gemini_st,
                "local_ollama": ollama_st,
                "deterministic_quant": {
                    "provider_id": "deterministic_quant",
                    "provider_name": "Deterministic Quant Engine (Fail-Closed Fallback)",
                    "role": "built_in_fallback",
                    "enabled": True,
                    "configured": True,
                    "live": True,
                },
            },
        }

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 18.0,
    ) -> AIProviderResponse:
        """Try enabled & configured providers in priority order."""
        last_resp: AIProviderResponse | None = None
        for provider in self.ordered_providers():
            if not provider.is_enabled or not provider.is_configured:
                continue
            resp = provider.generate(
                system_prompt,
                user_prompt,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            if resp.ok:
                return resp
            last_resp = resp

        return last_resp or AIProviderResponse(
            provider_id="none",
            provider_name="No External AI Provider Configured",
            model="quant-fallback",
            ok=False,
            error="No external AI provider is configured or reachable",
        )
