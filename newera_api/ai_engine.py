"""Safe local DeepSeek/Ollama decision adapter.

The local model is treated as an untrusted research assistant.  This module
keeps transport, parsing, and validation deliberately small so it can run in
the API-only environment without adding an SDK.  A model response never
becomes an executable order: validated responses are marked research-only and
malformed or unsafe responses fail closed to ``ABSTAIN``/``BLOCKED``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import urllib.request
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Iterable, Mapping


DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434/v1/chat/completions"
DEFAULT_OLLAMA_MODEL = "deepseek-r1:8b"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_REASONING_SUMMARY_LENGTH = 480
MAX_EVIDENCE_ITEMS = 32
MAX_BOT_PROPOSALS = 32

_SYMBOL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
_HIDDEN_REASONING_RE = re.compile(r"<(?:think|analysis)>.*?</(?:think|analysis)>", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


class UnsafeModelResult(ValueError):
    """Raised internally when a model payload cannot be made safe."""


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON-compatible values and reject non-finite or executable values."""

    if depth > 10:
        raise ValueError("JSON value is too deeply nested")
    if is_dataclass(value):
        value = asdict(value)  # type: ignore[arg-type]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if len(value) > 2000:
            raise ValueError("JSON object is too large")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 2000:
            raise ValueError("JSON array is too large")
        return [_json_safe(item, depth=depth + 1) for item in value]
    raise ValueError(f"unsupported non-JSON value: {type(value).__name__}")


def json_safe_copy(value: Any) -> Any:
    """Return a strict JSON-safe clone, including an allow-NaN check."""

    safe = _json_safe(value)
    return json.loads(json.dumps(safe, ensure_ascii=False, allow_nan=False))


def _finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise UnsafeModelResult(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UnsafeModelResult(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise UnsafeModelResult(f"{name} must be finite")
    return number


def _normalise_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 32 or any(character not in _SYMBOL_CHARS for character in symbol):
        raise ValueError("symbol contains unsupported characters")
    return symbol


def _compact_text(value: Any, *, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise UnsafeModelResult(f"{name} must be a string")
    text = re.sub(r"\s+", " ", _HIDDEN_REASONING_RE.sub("", text)).strip()
    if required and not text:
        raise UnsafeModelResult(f"{name} is required")
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


@dataclass(frozen=True)
class SafeDecision:
    """Validated, JSON-safe research decision returned by :class:`OllamaAIEngine`."""

    action: str = "ABSTAIN"
    confidence: float = 0.0
    risk_pct: float = 0.0
    min_rr: float = 1.5
    max_sl_atr: float = 2.5
    time_stop_minutes: int = 120
    reasoning_summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    bot_proposals: list[dict[str, Any]] = field(default_factory=list)
    status: str = "BLOCKED"
    block_reason: str = ""
    symbol: str = ""
    research_only: bool = True
    executable: bool = False
    execution_allowed: bool = False

    @property
    def reasoning(self) -> str:
        """Compatibility alias for callers that use ``reasoning``."""

        return self.reasoning_summary

    @property
    def blocked(self) -> bool:
        return self.status == "BLOCKED"

    @property
    def valid(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["blocked"] = self.blocked
        result["valid"] = self.valid
        result["reasoning"] = self.reasoning_summary
        return json_safe_copy(result)

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _blocked_decision(reason: str, *, symbol: str = "") -> SafeDecision:
    return SafeDecision(
        action="ABSTAIN",
        confidence=0.0,
        risk_pct=0.0,
        min_rr=1.5,
        max_sl_atr=2.5,
        time_stop_minutes=120,
        reasoning_summary="Model result blocked; no executable decision was produced.",
        evidence=[{"type": "safety", "status": "BLOCKED", "reason": reason[:240]}],
        bot_proposals=[],
        status="BLOCKED",
        block_reason=reason[:240],
        symbol=symbol,
        research_only=True,
        executable=False,
        execution_allowed=False,
    )


def _normalise_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsafeModelResult("evidence must be a list")
    if len(value) > MAX_EVIDENCE_ITEMS:
        raise UnsafeModelResult("evidence list is too large")
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            result.append({"summary": _compact_text(item, name="evidence.summary", limit=300)})
            continue
        if not isinstance(item, Mapping):
            raise UnsafeModelResult("each evidence item must be an object or string")
        safe = json_safe_copy(dict(item))
        result.append({str(key): item_value for key, item_value in safe.items()})
    return result


_UNSAFE_PROFILE_KEYS = frozenset(
    {
        "real_account_allowed",
        "order_execution_allowed",
        "direct_python_orders",
        "live_trading",
        "can_trade",
        "place_orders",
        "execute_orders",
    }
)

_UNSAFE_MODEL_KEYS = frozenset(
    {
        "place_order",
        "submit_order",
        "execute_order",
        "cancel_order",
        "modify_order",
        "order_placement",
        "live_trading",
        "real_account_allowed",
        "order_execution_allowed",
        "direct_python_orders",
        "shell",
        "command",
        "python",
        "script",
        "tool_call",
        "function_call",
    }
)


def _assert_false_safety_flags(value: Mapping[str, Any], *, prefix: str = "") -> None:
    for key, item in value.items():
        key_text = str(key).strip().lower()
        if key_text in _UNSAFE_PROFILE_KEYS and item not in (False, None, 0, "false", "False"):
            raise UnsafeModelResult(f"{prefix}{key} must remain false for research-only profiles")
        if isinstance(item, Mapping):
            _assert_false_safety_flags(item, prefix=f"{prefix}{key}.")


def _assert_no_unsafe_model_intent(value: Mapping[str, Any], *, prefix: str = "") -> None:
    for key, item in value.items():
        key_text = str(key).strip().lower()
        if key_text in _UNSAFE_MODEL_KEYS and item not in (False, None, 0, "false", "False", ""):
            raise UnsafeModelResult(f"{prefix}{key} is not allowed in a model result")
        if isinstance(item, Mapping):
            _assert_no_unsafe_model_intent(item, prefix=f"{prefix}{key}.")


def normalize_research_strategy_profile(profile: Mapping[str, Any], *, index: int = 0) -> dict[str, Any]:
    """Normalize one model-suggested strategy without creating an executable artifact."""

    if not isinstance(profile, Mapping):
        raise UnsafeModelResult(f"bot proposal {index} must be an object")
    raw = json_safe_copy(dict(profile))
    _assert_false_safety_flags(raw)

    name_value = raw.get("name", raw.get("display_name"))
    name = _compact_text(name_value, name="bot_proposal.name", limit=120, required=True)
    proposal_id = raw.get("id", raw.get("strategy_id"))
    if proposal_id is None:
        proposal_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.lower()).strip("-._")[:64]
    proposal_id = _compact_text(proposal_id, name="bot_proposal.id", limit=64, required=True).lower()
    if len(proposal_id) < 2 or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in proposal_id):
        raise UnsafeModelResult("bot_proposal.id is invalid")

    symbol = _normalise_symbol(raw.get("symbol", "XAUUSD"))
    timeframe = _compact_text(raw.get("timeframe", "M5"), name="bot_proposal.timeframe", limit=20, required=True).upper()
    description = _compact_text(raw.get("description", ""), name="bot_proposal.description", limit=1000)
    flow = raw.get("flow", raw.get("rules", {}))
    if not isinstance(flow, Mapping):
        raise UnsafeModelResult("bot_proposal.flow must be an object")
    code = raw.get("code")
    if code is not None and not isinstance(code, Mapping):
        raise UnsafeModelResult("bot_proposal.code must be an object when supplied")

    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise UnsafeModelResult("bot_proposal.metadata must be an object")
    normalized = {
        "id": proposal_id,
        "name": name,
        "builder": _compact_text(raw.get("builder", "visual"), name="bot_proposal.builder", limit=20, required=True).lower(),
        "symbol": symbol,
        "timeframe": timeframe,
        "description": description,
        "flow": json_safe_copy(dict(flow)),
        "code": json_safe_copy(dict(code)) if code is not None else None,
        "metadata": json_safe_copy(dict(metadata)),
        "status": "research_only",
        "research_only": True,
        "safety": {
            "research_only": True,
            "real_account_allowed": False,
            "order_execution_allowed": False,
            "direct_python_orders": False,
        },
    }
    if normalized["builder"] not in {"visual", "code"}:
        raise UnsafeModelResult("bot_proposal.builder must be visual or code")
    return json_safe_copy(normalized)


def _normalise_bot_proposals(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsafeModelResult("bot_proposals must be a list")
    if len(value) > MAX_BOT_PROPOSALS:
        raise UnsafeModelResult("too many bot proposals")
    return [normalize_research_strategy_profile(item, index=index) for index, item in enumerate(value)]


def _strip_hidden_reasoning(text: str) -> str:
    return _HIDDEN_REASONING_RE.sub("", text).strip()


def _content_from_payload(payload: Mapping[str, Any]) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, Mapping):
            message = choice.get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    pieces: list[str] = []
                    for item in content:
                        if isinstance(item, str):
                            pieces.append(item)
                        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                            pieces.append(str(item["text"]))
                    return "".join(pieces)
            text = choice.get("text")
            if isinstance(text, str):
                return text
            delta = choice.get("delta")
            if isinstance(delta, Mapping) and isinstance(delta.get("content"), str):
                return str(delta["content"])
    message = payload.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return str(message["content"])
    for key in ("response", "output", "content", "text"):
        if isinstance(payload.get(key), str):
            return str(payload[key])
    return None


def _extract_json_object(text: str) -> Any:
    cleaned = _strip_hidden_reasoning(text).strip()
    fenced = _FENCE_RE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise UnsafeModelResult("model output did not contain a JSON object")


def extract_model_result(payload: Any) -> Mapping[str, Any]:
    """Extract a decision object from an OpenAI/Ollama envelope or JSON text."""

    if isinstance(payload, SafeDecision):
        return payload.to_dict()
    if isinstance(payload, Mapping):
        if any(key in payload for key in ("action", "confidence", "risk_pct", "min_rr", "max_sl_atr", "time_stop_minutes")):
            return payload
        for key in ("decision", "result"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return nested
        content = _content_from_payload(payload)
        if content is not None:
            return extract_model_result(content)
        raise UnsafeModelResult("model object did not contain a decision")
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", errors="replace")
    if isinstance(payload, str):
        decoded = _extract_json_object(payload)
        if not isinstance(decoded, Mapping):
            raise UnsafeModelResult("model JSON result must be an object")
        return extract_model_result(decoded)
    raise UnsafeModelResult("model result must be a JSON object or text")


def normalize_model_result(payload: Any, *, symbol: str = "") -> SafeDecision:
    """Strictly normalize a model result; all failures return a blocked decision."""

    try:
        normalized_symbol = _normalise_symbol(symbol) if symbol else ""
        raw = dict(extract_model_result(payload))
        _assert_false_safety_flags(raw)
        _assert_no_unsafe_model_intent(raw)
        action_value = raw.get("action")
        if not isinstance(action_value, str):
            raise UnsafeModelResult("action is required")
        action = action_value.strip().upper()
        if action not in {"BUY", "SELL", "ABSTAIN"}:
            raise UnsafeModelResult("action must be BUY, SELL, or ABSTAIN")

        confidence_value = raw.get("confidence", 0.0 if action == "ABSTAIN" else None)
        if confidence_value is None:
            raise UnsafeModelResult("confidence is required")
        confidence = _finite_number(confidence_value, name="confidence")
        if not 0.0 <= confidence <= 1.0:
            raise UnsafeModelResult("confidence must be between 0 and 1")

        if action == "ABSTAIN":
            risk_pct = _finite_number(raw.get("risk_pct", 0.0), name="risk_pct")
            min_rr = _finite_number(raw.get("min_rr", 1.5), name="min_rr")
            max_sl_atr = _finite_number(raw.get("max_sl_atr", 2.5), name="max_sl_atr")
            time_stop_raw = raw.get("time_stop_minutes", 120)
        else:
            required = ("risk_pct", "min_rr", "max_sl_atr", "time_stop_minutes")
            missing = [key for key in required if key not in raw]
            if missing:
                raise UnsafeModelResult(f"missing required risk fields: {', '.join(missing)}")
            risk_pct = _finite_number(raw["risk_pct"], name="risk_pct")
            min_rr = _finite_number(raw["min_rr"], name="min_rr")
            max_sl_atr = _finite_number(raw["max_sl_atr"], name="max_sl_atr")
            time_stop_raw = raw["time_stop_minutes"]
        if risk_pct < 0.0 or risk_pct > 1.5:
            raise UnsafeModelResult("risk_pct exceeds the 1.5% hard cap")
        if min_rr < 1.5:
            raise UnsafeModelResult("min_rr is below the 1.5 minimum")
        if max_sl_atr <= 0.0 or max_sl_atr > 2.5:
            raise UnsafeModelResult("max_sl_atr is outside the safe range")
        if isinstance(time_stop_raw, bool):
            raise UnsafeModelResult("time_stop_minutes must be an integer")
        time_stop_value: Any = time_stop_raw
        try:
            time_stop_minutes = int(time_stop_value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise UnsafeModelResult("time_stop_minutes must be an integer") from exc
        if float(time_stop_minutes) != float(time_stop_value) or time_stop_minutes < 1 or time_stop_minutes > 120:
            raise UnsafeModelResult("time_stop_minutes exceeds the 120-minute hard cap")

        summary_value = raw.get("reasoning_summary", raw.get("summary", raw.get("reasoning", "")))
        reasoning_summary = _compact_text(
            summary_value,
            name="reasoning_summary",
            limit=MAX_REASONING_SUMMARY_LENGTH,
            required=action != "ABSTAIN",
        )
        evidence = _normalise_evidence(raw.get("evidence", []))
        proposals = _normalise_bot_proposals(raw.get("bot_proposals", raw.get("strategy_profiles", [])))
        return SafeDecision(
            action=action,
            confidence=round(confidence, 6),
            risk_pct=round(risk_pct, 6),
            min_rr=round(min_rr, 6),
            max_sl_atr=round(max_sl_atr, 6),
            time_stop_minutes=time_stop_minutes,
            reasoning_summary=reasoning_summary,
            evidence=evidence,
            bot_proposals=proposals,
            status="RESEARCH_ONLY",
            block_reason="",
            symbol=normalized_symbol,
            research_only=True,
            executable=False,
            execution_allowed=False,
        )
    except Exception as exc:
        reason = str(exc).strip() or "unsafe model result"
        try:
            safe_symbol = _normalise_symbol(symbol) if symbol else ""
        except ValueError:
            safe_symbol = ""
        return _blocked_decision(reason, symbol=safe_symbol)


def parse_json_stream(chunks: Iterable[Any]) -> Mapping[str, Any]:
    """Parse OpenAI SSE, Ollama NDJSON, or fragmented JSON into one result object."""

    text_fragments: list[str] = []
    direct_candidate: Mapping[str, Any] | None = None

    for chunk in chunks:
        if isinstance(chunk, Mapping):
            fragment = _content_from_payload(chunk)
            if fragment is not None:
                text_fragments.append(fragment)
            elif any(key in chunk for key in ("action", "confidence", "risk_pct", "decision")):
                direct_candidate = extract_model_result(chunk)
            continue
        if isinstance(chunk, (bytes, bytearray)):
            chunk_text = bytes(chunk).decode("utf-8", errors="replace")
        else:
            chunk_text = str(chunk)
        lines = chunk_text.splitlines() or [chunk_text]
        for line in lines:
            item = line.strip()
            if item.startswith("data:"):
                item = item[5:].strip()
            if not item or item == "[DONE]":
                continue
            try:
                decoded = json.loads(item)
            except json.JSONDecodeError:
                text_fragments.append(item)
                continue
            if isinstance(decoded, Mapping):
                fragment = _content_from_payload(decoded)
                if fragment is not None:
                    text_fragments.append(fragment)
                elif any(key in decoded for key in ("action", "confidence", "risk_pct", "decision")):
                    direct_candidate = extract_model_result(decoded)
            elif isinstance(decoded, str):
                text_fragments.append(decoded)

    if text_fragments:
        return extract_model_result("".join(text_fragments))
    if direct_candidate is not None:
        return direct_candidate
    raise UnsafeModelResult("JSON stream did not contain a model result")


def build_scan_context(
    symbol: str,
    ohlcv: Any = None,
    indicators: Any = None,
    kronos_matrix: Any = None,
    l2_dom: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a bounded JSON-safe scan context for a local model prompt."""

    normalized_symbol = _normalise_symbol(symbol)
    context: dict[str, Any] = {
        "symbol": normalized_symbol,
        "ohlcv": json_safe_copy([] if ohlcv is None else ohlcv),
        "indicators": json_safe_copy({} if indicators is None else indicators),
        "kronos_matrix": json_safe_copy({} if kronos_matrix is None else kronos_matrix),
        "l2_dom": json_safe_copy({} if l2_dom is None else l2_dom),
        "research_only": True,
    }
    for key, value in extra.items():
        if key in context:
            continue
        context[str(key)] = json_safe_copy(value)
    return json_safe_copy(context)


def build_scan_prompt(context: Mapping[str, Any]) -> str:
    """Render a deterministic prompt that requests only concise safe output."""

    safe_context = json_safe_copy(dict(context))
    return (
        "You are a local research-only market analysis assistant. Do not place, "
        "modify, or cancel orders. Do not reveal hidden chain-of-thought. Return "
        "one JSON object only with action BUY, SELL, or ABSTAIN; confidence 0..1; "
        "risk_pct <= 1.5; min_rr >= 1.5; max_sl_atr <= 2.5; "
        "time_stop_minutes <= 120; a concise reasoning_summary; structured evidence; "
        "and optional bot_proposals that are research-only profiles. Unsafe or "
        "uncertain conditions must use ABSTAIN.\n\nSCAN_CONTEXT=\n"
        + json.dumps(safe_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


class OllamaAIEngine:
    """Minimal OpenAI-compatible Ollama client with fail-closed validation."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        stream: bool = False,
        offline: bool | None = None,
        opener: Callable[..., Any] | None = None,
        transport: Callable[..., Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.endpoint = str(
            endpoint
            or os.getenv("NEWERA_OLLAMA_ENDPOINT")
            or os.getenv("OLLAMA_ENDPOINT")
            or DEFAULT_OLLAMA_ENDPOINT
        ).strip()
        self.model = str(
            model
            or os.getenv("NEWERA_OLLAMA_MODEL")
            or os.getenv("OLLAMA_MODEL")
            or DEFAULT_OLLAMA_MODEL
        ).strip()
        try:
            timeout_value = float(timeout if timeout is not None else os.getenv("NEWERA_OLLAMA_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_value = DEFAULT_TIMEOUT_SECONDS
        self.timeout = max(0.1, min(timeout_value, 300.0))
        self.stream = bool(stream)
        if offline is None:
            offline = str(os.getenv("NEWERA_AI_OFFLINE", os.getenv("OLLAMA_OFFLINE", ""))).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self.offline = bool(offline)
        selected_opener: Any = opener or transport or urllib.request.urlopen
        if not callable(selected_opener):
            selected_opener = getattr(selected_opener, "open", None) or getattr(selected_opener, "urlopen", None)
        if not callable(selected_opener):
            selected_opener = urllib.request.urlopen
        self._opener = selected_opener
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "NewEraQuant-LocalAI/1.0",
        }
        if headers:
            self.headers.update({str(key): str(value) for key, value in headers.items()})

    @staticmethod
    def parse_stream(chunks: Iterable[Any]) -> Mapping[str, Any]:
        return parse_json_stream(chunks)

    @staticmethod
    def normalize_result(payload: Any, *, symbol: str = "") -> SafeDecision:
        return normalize_model_result(payload, symbol=symbol)

    @staticmethod
    def build_scan_context(
        symbol: str,
        ohlcv: Any = None,
        indicators: Any = None,
        kronos_matrix: Any = None,
        l2_dom: Any = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return build_scan_context(symbol, ohlcv, indicators, kronos_matrix, l2_dom, **extra)

    @staticmethod
    def build_prompt(context: Mapping[str, Any]) -> str:
        return build_scan_prompt(context)

    def _request(self, context: Mapping[str, Any], *, prompt: str | None, stream: bool) -> Any:
        safe_context = json_safe_copy(dict(context))
        request_prompt = prompt or build_scan_prompt(safe_context)
        if not isinstance(request_prompt, str):
            raise ValueError("prompt must be a string")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return concise JSON only. This is research-only and never executable.",
                },
                {"role": "user", "content": request_prompt},
            ],
            "temperature": 0,
            "stream": bool(stream),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=dict(self.headers), method="POST")
        try:
            return self._opener(request, timeout=self.timeout)
        except TypeError:
            # Small fake transports often accept only the request object.
            return self._opener(request)

    @staticmethod
    def _response_status(response: Any) -> int | None:
        for name in ("status", "status_code"):
            value = getattr(response, name, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            try:
                getcode_callable: Any = getcode
                return int(getcode_callable())
            except (TypeError, ValueError, OSError):
                return None
        return None

    @staticmethod
    def _read_response(response: Any, *, stream: bool) -> Any:
        status = OllamaAIEngine._response_status(response)
        if status is not None and status >= 400:
            raise OSError(f"local model returned HTTP {status}")
        read = getattr(response, "read", None)
        if callable(read):
            raw = read()
            if stream and isinstance(raw, (list, tuple)):
                return parse_json_stream(raw)
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8", errors="replace")
            if stream:
                return parse_json_stream([raw])
            if isinstance(raw, str):
                return json.loads(raw)
            return raw
        if stream:
            return parse_json_stream(response)
        if isinstance(response, (bytes, bytearray, str)):
            raw_text = bytes(response).decode("utf-8", errors="replace") if isinstance(response, (bytes, bytearray)) else response
            return json.loads(raw_text)
        return response

    def safe_call(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        prompt: str | None = None,
        stream: bool | None = None,
        symbol: str | None = None,
    ) -> SafeDecision:
        """Call Ollama and always return a safe decision, including offline failures."""

        context_value = context or {}
        stream_value = self.stream if stream is None else bool(stream)
        try:
            safe_context = json_safe_copy(dict(context_value))
            context_symbol = str(symbol or safe_context.get("symbol") or "").strip().upper()
            if self.offline:
                return _blocked_decision("offline mode is enabled", symbol=context_symbol)
            response = self._request(safe_context, prompt=prompt, stream=stream_value)
            payload = self._read_response(response, stream=stream_value)
            return normalize_model_result(payload, symbol=context_symbol)
        except Exception as exc:
            context_symbol = ""
            try:
                context_symbol = _normalise_symbol(str(symbol or (context or {}).get("symbol") or ""))
            except (ValueError, AttributeError):
                pass
            return _blocked_decision(f"local model unavailable or unsafe: {exc}", symbol=context_symbol)

    def call(self, context: Mapping[str, Any] | None = None, **kwargs: Any) -> SafeDecision:
        return self.safe_call(context, **kwargs)

    def analyze(self, context: Mapping[str, Any] | None = None, **kwargs: Any) -> SafeDecision:
        return self.safe_call(context, **kwargs)

    def safe_call_dict(self, context: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.safe_call(context, **kwargs).to_dict()

    async def safe_call_async(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        prompt: str | None = None,
        stream: bool | None = None,
        symbol: str | None = None,
    ) -> SafeDecision:
        return await asyncio.to_thread(
            self.safe_call,
            context,
            prompt=prompt,
            stream=stream,
            symbol=symbol,
        )

    async def call_async(self, context: Mapping[str, Any] | None = None, **kwargs: Any) -> SafeDecision:
        return await self.safe_call_async(context, **kwargs)

    async def analyze_async(self, context: Mapping[str, Any] | None = None, **kwargs: Any) -> SafeDecision:
        return await self.safe_call_async(context, **kwargs)


# Friendly aliases for callers that use different local-engine names.
AIEngine = OllamaAIEngine
DeepSeekOllamaClient = OllamaAIEngine
ModelDecision = SafeDecision
DecisionResult = SafeDecision
parse_stream_json = parse_json_stream
normalize_decision = normalize_model_result


__all__ = [
    "AIEngine",
    "DEFAULT_OLLAMA_ENDPOINT",
    "DEFAULT_OLLAMA_MODEL",
    "DecisionResult",
    "DeepSeekOllamaClient",
    "ModelDecision",
    "OllamaAIEngine",
    "SafeDecision",
    "UnsafeModelResult",
    "build_scan_context",
    "build_scan_prompt",
    "extract_model_result",
    "json_safe_copy",
    "normalize_decision",
    "normalize_model_result",
    "normalize_research_strategy_profile",
    "parse_json_stream",
    "parse_stream_json",
]
