"""Dependency-free, read-only MCP-inspired JSON-RPC 2.0 facade.

The facade intentionally exposes research and audit helpers only.  Tool
handlers may be synchronous or asynchronous, but every registered tool is
explicitly read-only and every result is copied through strict JSON
validation.  There is no order-placement, shell, Python-evaluation, or other
arbitrary-command capability in this module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .ai_engine import json_safe_copy, normalize_research_strategy_profile


MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_TOOL_ITEMS = 100
MAX_PARALLEL_ANALYSES = 16
MAX_PARALLEL_CONCURRENCY = 8
_SYMBOL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ORDER_PLACEMENT_FRAGMENTS = (
    "place_order",
    "submit_order",
    "execute_order",
    "cancel_order",
    "modify_order",
    "create_order",
    "send_order",
    "order_placement",
    "trade_execution",
)


class MCPProtocolError(Exception):
    """JSON-RPC error with a protocol code and safe optional data."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = data


@dataclass(frozen=True)
class MCPTool:
    """Registered read-only tool and its non-JSON handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    read_only: bool = True
    annotations: dict[str, Any] | None = None

    def descriptor(self) -> dict[str, Any]:
        annotations = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        if self.annotations:
            annotations.update(json_safe_copy(self.annotations))
        if not bool(annotations.get("readOnlyHint", False)) or bool(annotations.get("destructiveHint", False)):
            raise ValueError("MCP tool annotations must remain explicitly read-only")
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": json_safe_copy(self.input_schema),
            "annotations": annotations,
        }


class ToolRegistry:
    """Small registry that accepts sync/async handlers but only read-only tools."""

    def __init__(self) -> None:
        self._tools: dict[str, MCPTool] = {}

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not _NAME_RE.fullmatch(normalized):
            raise ValueError("tool name must be a simple identifier")
        lowered = normalized.lower()
        if any(fragment in lowered for fragment in _ORDER_PLACEMENT_FRAGMENTS):
            raise ValueError("order-placement tools are not permitted")
        return normalized

    def register_tool(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        description: str = "",
        input_schema: Mapping[str, Any] | None = None,
        read_only: bool = True,
        annotations: Mapping[str, Any] | None = None,
    ) -> MCPTool:
        normalized_name = self._validate_name(name)
        if not read_only:
            raise ValueError("only read-only tools may be registered")
        if not callable(handler):
            raise ValueError("tool handler must be callable")
        schema = dict(input_schema or {"type": "object", "additionalProperties": True})
        tool = MCPTool(
            name=normalized_name,
            description=str(description or "Read-only research tool"),
            input_schema=json_safe_copy(schema),
            handler=handler,
            read_only=True,
            annotations=json_safe_copy(dict(annotations or {})),
        )
        # Validate the descriptor now so an unsafe annotation cannot be listed later.
        tool.descriptor()
        self._tools[normalized_name] = tool
        return tool

    register = register_tool

    def get(self, name: str) -> MCPTool | None:
        return self._tools.get(str(name))

    def names(self) -> list[str]:
        return list(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tools[name].descriptor() for name in self._tools]

    async def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        tool = self.get(name)
        if tool is None:
            raise MCPProtocolError(-32602, f"unknown tool: {name}")
        if not tool.read_only:
            raise MCPProtocolError(-32001, "tool is not read-only")
        args = dict(arguments or {})
        handler = tool.handler
        if inspect.iscoroutinefunction(handler):
            value = await handler(args)
        else:
            value = await asyncio.to_thread(handler, args)
            if inspect.isawaitable(value):
                value = await value
        return json_safe_copy(value)

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        return await self.invoke(name, arguments)


def _safe_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 32 or any(character not in _SYMBOL_CHARS for character in symbol):
        raise ValueError("symbol contains unsupported characters")
    return symbol


def _finite(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


async def _call_provider(provider: Callable[..., Any], arguments: dict[str, Any], symbol: str | None = None) -> Any:
    """Call a sync/async provider without exposing reflection or execution tools."""

    call_argument: Any = arguments
    try:
        signature = inspect.signature(provider)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        required = [parameter for parameter in positional if parameter.default is parameter.empty]
        if not required:
            call_argument = None
        elif len(positional) == 1 and positional[0].name.lower() in {"symbol", "ticker", "instrument"}:
            call_argument = symbol
    except (TypeError, ValueError):
        pass

    if call_argument is None:
        if inspect.iscoroutinefunction(provider):
            return await provider()
        result = await asyncio.to_thread(provider)
    elif inspect.iscoroutinefunction(provider):
        result = await provider(call_argument)
    else:
        result = await asyncio.to_thread(provider, call_argument)
    if inspect.isawaitable(result):
        result = await result
    return result


class MCPServer:
    """Read-only JSON-RPC server facade with safe default research tools."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        market_snapshot_provider: Callable[..., Any] | None = None,
        market_snapshot_handler: Callable[..., Any] | None = None,
        orderflow_provider: Callable[..., Any] | None = None,
        orderflow_handler: Callable[..., Any] | None = None,
        decision_audit_provider: Callable[..., Any] | None = None,
        decision_audit_handler: Callable[..., Any] | None = None,
        decision_records: Iterable[Mapping[str, Any]] | None = None,
        audit_records: Iterable[Mapping[str, Any]] | None = None,
        decision_audit_path: str | Path | None = None,
        strategy_proposals: Iterable[Mapping[str, Any]] | None = None,
        bot_proposals: Iterable[Mapping[str, Any]] | None = None,
        market_snapshots: Mapping[str, Any] | None = None,
        max_parallel: int = 4,
    ) -> None:
        self.registry = registry or ToolRegistry()
        self.max_parallel = max(1, min(int(max_parallel), MAX_PARALLEL_CONCURRENCY))
        self._market_snapshot_provider = market_snapshot_provider or market_snapshot_handler
        self._orderflow_provider = orderflow_provider or orderflow_handler
        self._decision_audit_provider = decision_audit_provider or decision_audit_handler
        self._decision_records = [json_safe_copy(dict(item)) for item in (decision_records or audit_records or [])]
        self._decision_audit_path = Path(decision_audit_path).resolve() if decision_audit_path else None
        self._market_snapshots = {
            _safe_symbol(key): json_safe_copy(value) for key, value in (market_snapshots or {}).items()
        }
        self._strategy_proposals: list[dict[str, Any]] = []
        for index, proposal in enumerate(strategy_proposals or bot_proposals or []):
            try:
                self._strategy_proposals.append(normalize_research_strategy_profile(proposal, index=index))
            except Exception:
                # Listing remains fail-closed: unsafe initial proposals are omitted.
                continue
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        defaults: list[tuple[str, str, Callable[[dict[str, Any]], Any], dict[str, Any]]] = [
            (
                "market_snapshot",
                "Read-only market snapshot lookup; never places orders.",
                self._market_snapshot_tool,
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}, "snapshot": {"type": "object"}},
                    "additionalProperties": True,
                },
            ),
            (
                "orderflow_analysis",
                "Read-only bid/ask and L2 orderflow analysis.",
                self._orderflow_tool,
                {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "bids": {"type": "array"},
                        "asks": {"type": "array"},
                    },
                    "additionalProperties": True,
                },
            ),
            (
                "decision_audit_lookup",
                "Read-only lookup of local decision audit records.",
                self._decision_audit_tool,
                {
                    "type": "object",
                    "properties": {
                        "scan_id": {"type": "string"},
                        "symbol": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_ITEMS},
                    },
                    "additionalProperties": True,
                },
            ),
            (
                "validate_strategy_proposal",
                "Validate a research-only strategy or bot proposal without loading or executing code.",
                self._validate_proposal_tool,
                {"type": "object", "additionalProperties": True},
            ),
            (
                "list_strategy_proposals",
                "List validated research-only strategy and bot proposals.",
                self._list_proposals_tool,
                {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_ITEMS}},
                    "additionalProperties": False,
                },
            ),
            (
                "run_parallel_analysis",
                "Run bounded parallel read-only analysis tools while preserving input order.",
                self._parallel_tool,
                {
                    "type": "object",
                    "properties": {
                        "analyses": {"type": "array", "maxItems": MAX_PARALLEL_ANALYSES},
                        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": MAX_PARALLEL_CONCURRENCY},
                    },
                    "required": ["analyses"],
                    "additionalProperties": False,
                },
            ),
        ]
        for name, description, handler, schema in defaults:
            if self.registry.get(name) is None:
                self.registry.register_tool(name, handler, description=description, input_schema=schema, read_only=True)

    async def _market_snapshot_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = _safe_symbol(arguments.get("symbol", "XAUUSD"))
        if self._market_snapshot_provider is not None:
            value = await _call_provider(self._market_snapshot_provider, arguments, symbol)
            return {"symbol": symbol, "available": value is not None, "source": "provider", "snapshot": value}
        if "snapshot" in arguments:
            return {"symbol": symbol, "available": True, "source": "request", "snapshot": arguments["snapshot"]}
        snapshot = self._market_snapshots.get(symbol)
        return {
            "symbol": symbol,
            "available": snapshot is not None,
            "source": "memory" if snapshot is not None else "offline",
            "snapshot": snapshot,
            "read_only": True,
        }

    @staticmethod
    def _normalise_levels(value: Any, *, name: str) -> list[dict[str, float]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list")
        if len(value) > MAX_TOOL_ITEMS:
            raise ValueError(f"{name} is too large")
        result: list[dict[str, float]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError(f"{name} levels must be objects")
            price = _finite(item.get("price"), name=f"{name}.price")
            volume = _finite(item.get("volume", item.get("size", 0.0)), name=f"{name}.volume")
            if price <= 0 or volume < 0:
                raise ValueError(f"{name} price must be positive and volume cannot be negative")
            result.append({"price": price, "volume": volume})
        return result

    async def _orderflow_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = _safe_symbol(arguments.get("symbol", "XAUUSD"))
        if self._orderflow_provider is not None:
            value = await _call_provider(self._orderflow_provider, arguments, symbol)
            return {"symbol": symbol, "available": value is not None, "source": "provider", "analysis": value}

        bids = self._normalise_levels(arguments.get("bids", []), name="bids")
        asks = self._normalise_levels(arguments.get("asks", []), name="asks")
        if not bids or not asks:
            return {
                "symbol": symbol,
                "available": False,
                "source": "offline",
                "best_bid": None,
                "best_ask": None,
                "mid_price": None,
                "spread": None,
                "imbalance_ratio": 0.0,
                "l2_direction": "ABSTAIN",
                "read_only": True,
            }
        best_bid = max(item["price"] for item in bids)
        best_ask = min(item["price"] for item in asks)
        total_bid = sum(item["volume"] for item in bids)
        total_ask = sum(item["volume"] for item in asks)
        denominator = total_bid + total_ask or 1.0
        imbalance = (total_bid - total_ask) / denominator
        direction = "BUY" if imbalance >= 0.12 else "SELL" if imbalance <= -0.12 else "ABSTAIN"
        return {
            "symbol": symbol,
            "available": True,
            "source": "request",
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": (best_bid + best_ask) / 2.0,
            "spread": max(0.0, best_ask - best_bid),
            "total_bid_volume": total_bid,
            "total_ask_volume": total_ask,
            "imbalance_ratio": imbalance,
            "l2_direction": direction,
            "bids": bids,
            "asks": asks,
            "read_only": True,
        }

    def _load_audit_records(self) -> list[dict[str, Any]]:
        records = list(self._decision_records)
        path = self._decision_audit_path
        if path is None or not path.is_file():
            return records
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return records
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                records.extend(json_safe_copy(item) for item in decoded if isinstance(item, Mapping))
            elif isinstance(decoded, Mapping):
                records.append(json_safe_copy(dict(decoded)))
            return records
        except json.JSONDecodeError:
            for line in text.splitlines():
                try:
                    decoded_line = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded_line, Mapping):
                    records.append(json_safe_copy(dict(decoded_line)))
        return records

    async def _decision_audit_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._decision_audit_provider is not None:
            provided = await _call_provider(self._decision_audit_provider, arguments)
            records = provided.get("records", []) if isinstance(provided, Mapping) else provided
        else:
            records = self._load_audit_records()
        if records is None:
            records = []
        if not isinstance(records, list):
            raise ValueError("decision audit provider must return a list or records object")
        symbol = str(arguments.get("symbol", "")).strip().upper()
        scan_id = str(arguments.get("scan_id", "")).strip()
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        limit = max(1, min(limit, MAX_TOOL_ITEMS))
        filtered: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if symbol and str(record.get("symbol", "")).upper() != symbol:
                continue
            if scan_id and str(record.get("scan_id", "")) != scan_id:
                continue
            filtered.append(json_safe_copy(dict(record)))
        return {"records": filtered[:limit], "count": min(len(filtered), limit), "read_only": True}

    async def _validate_proposal_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        proposal = arguments.get("proposal", arguments)
        try:
            normalized = normalize_research_strategy_profile(proposal)
        except Exception as exc:
            return {
                "valid": False,
                "research_only": True,
                "order_execution_allowed": False,
                "error": str(exc)[:240] or "invalid research proposal",
            }
        return {
            "valid": True,
            "research_only": True,
            "order_execution_allowed": False,
            "proposal": normalized,
        }

    async def _list_proposals_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            limit = int(arguments.get("limit", MAX_TOOL_ITEMS))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        limit = max(1, min(limit, MAX_TOOL_ITEMS))
        return {
            "proposals": json_safe_copy(self._strategy_proposals[:limit]),
            "count": min(len(self._strategy_proposals), limit),
            "research_only": True,
            "order_execution_allowed": False,
        }

    async def run_parallel_analysis(
        self,
        analyses: Iterable[Mapping[str, Any]],
        *,
        max_concurrency: int | None = None,
    ) -> dict[str, Any]:
        return await self._parallel_tool(
            {
                "analyses": list(analyses),
                "max_concurrency": max_concurrency if max_concurrency is not None else self.max_parallel,
            }
        )

    async def _parallel_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        analyses = arguments.get("analyses")
        if not isinstance(analyses, list):
            raise ValueError("analyses must be a list")
        if len(analyses) > MAX_PARALLEL_ANALYSES:
            raise ValueError(f"at most {MAX_PARALLEL_ANALYSES} analyses are allowed")
        try:
            requested_concurrency = int(arguments.get("max_concurrency", self.max_parallel))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_concurrency must be an integer") from exc
        concurrency = max(1, min(requested_concurrency, self.max_parallel, MAX_PARALLEL_CONCURRENCY))
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(index: int, item: Any) -> dict[str, Any]:
            if not isinstance(item, Mapping):
                return {"index": index, "tool": "", "ok": False, "error": "analysis item must be an object"}
            name = str(item.get("tool", item.get("name", ""))).strip()
            if name == "run_parallel_analysis":
                return {"index": index, "tool": name, "ok": False, "error": "nested parallel analysis is not allowed"}
            tool = self.registry.get(name)
            if tool is None:
                return {"index": index, "tool": name, "ok": False, "error": "unknown read-only tool"}
            if not tool.read_only:
                return {"index": index, "tool": name, "ok": False, "error": "tool is not read-only"}
            raw_arguments = item.get("arguments", {})
            if not isinstance(raw_arguments, Mapping):
                return {"index": index, "tool": name, "ok": False, "error": "arguments must be an object"}
            async with semaphore:
                try:
                    result = await self.registry.invoke(name, dict(raw_arguments))
                    return {"index": index, "tool": name, "ok": True, "result": result}
                except Exception as exc:
                    return {"index": index, "tool": name, "ok": False, "error": str(exc)[:240] or "analysis failed"}

        results = await asyncio.gather(*(run_one(index, item) for index, item in enumerate(analyses)))
        return {
            "results": results,
            "count": len(results),
            "max_concurrency": concurrency,
            "read_only": True,
            "order_execution_allowed": False,
        }

    @staticmethod
    def _success(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, error: MCPProtocolError) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            try:
                payload["data"] = json_safe_copy(error.data)
            except ValueError:
                payload["data"] = {"safe": False}
        return {"jsonrpc": "2.0", "id": request_id, "error": payload}

    async def handle_async(self, request: Mapping[str, Any] | str) -> dict[str, Any] | None:
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except json.JSONDecodeError:
                return self._error(None, MCPProtocolError(-32700, "parse error"))
        if not isinstance(request, Mapping) or request.get("jsonrpc") != "2.0":
            return self._error(None, MCPProtocolError(-32600, "invalid request"))
        request_id = request.get("id")
        is_notification = "id" not in request
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            error = self._error(request_id, MCPProtocolError(-32600, "invalid request"))
            return None if is_notification else error

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "newera-read-only-mcp", "version": "0.1.0"},
                    "instructions": "All tools are read-only research and audit helpers; no order placement is available.",
                }
            elif method in {"notifications/initialized", "initialized"}:
                result = {}
            elif method == "tools/list":
                result = {"tools": self.registry.list_tools()}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(name, str) or not name.strip() or not isinstance(arguments, Mapping):
                    raise MCPProtocolError(-32602, "tools/call requires a tool name and object arguments")
                tool_result = await self.registry.invoke(name.strip(), dict(arguments))
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(tool_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        }
                    ],
                    "structuredContent": tool_result,
                    "isError": False,
                }
            else:
                raise MCPProtocolError(-32601, f"method not found: {method}")
        except MCPProtocolError as exc:
            return None if is_notification else self._error(request_id, exc)
        except (ValueError, TypeError) as exc:
            error = MCPProtocolError(-32602, str(exc)[:240] or "invalid parameters")
            return None if is_notification else self._error(request_id, error)
        except Exception:
            error = MCPProtocolError(-32000, "tool execution failed")
            return None if is_notification else self._error(request_id, error)

        return None if is_notification else self._success(request_id, result)

    def handle(self, request: Mapping[str, Any] | str) -> dict[str, Any] | None:
        """Synchronously dispatch one request for local callers outside an event loop."""

        return asyncio.run(self.handle_async(request))

    def handle_json(self, payload: str) -> str:
        result = self.handle(payload)
        return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    dispatch = handle
    dispatch_async = handle_async


# Friendly aliases for callers using server/registry terminology.
MCPInspiredServer = MCPServer
JSONRPCServer = MCPServer


__all__ = [
    "JSONRPCServer",
    "MCPInspiredServer",
    "MCP_PROTOCOL_VERSION",
    "MCPProtocolError",
    "MCPServer",
    "MCPTool",
    "ToolRegistry",
]
