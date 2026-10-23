from __future__ import annotations

import asyncio
import unittest

from newera_api.mcp_server import MCPServer, ToolRegistry


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MCPServer(
            market_snapshots={"XAUUSD": {"close": 2000.0, "atr": 4.0}},
            decision_records=[
                {"scan_id": "scan-1", "symbol": "XAUUSD", "action": "ABSTAIN", "status": "BLOCKED"},
                {"scan_id": "scan-2", "symbol": "EURUSD", "action": "BUY", "status": "SHADOW"},
            ],
            strategy_proposals=[
                {
                    "id": "range-study",
                    "name": "Range Study",
                    "builder": "visual",
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "flow": {},
                }
            ],
            max_parallel=2,
        )

    @staticmethod
    def request(request_id: int, method: str, params: dict | None = None) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        response = self.server.handle(self.request(1, "tools/call", {"name": name, "arguments": arguments or {}}))
        self.assertIsNotNone(response)
        self.assertNotIn("error", response)
        return response["result"]["structuredContent"]

    def test_initialize_and_tools_list_are_read_only_without_order_tool(self) -> None:
        initialized = self.server.handle(self.request(1, "initialize", {"protocolVersion": "2024-11-05"}))
        self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", initialized["result"]["capabilities"])

        listed = self.server.handle(self.request(2, "tools/list"))
        tools = listed["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue(
            {
                "market_snapshot",
                "orderflow_analysis",
                "decision_audit_lookup",
                "validate_strategy_proposal",
                "list_strategy_proposals",
                "run_parallel_analysis",
            }.issubset(names)
        )
        self.assertNotIn("place_order", names)
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_safe_tools_snapshot_orderflow_audit_and_proposal_validation(self) -> None:
        snapshot = self.call_tool("market_snapshot", {"symbol": "XAUUSD"})
        self.assertEqual(snapshot["snapshot"]["close"], 2000.0)

        orderflow = self.call_tool(
            "orderflow_analysis",
            {
                "symbol": "XAUUSD",
                "bids": [{"price": 1999.0, "volume": 4.0}],
                "asks": [{"price": 2001.0, "volume": 1.0}],
            },
        )
        self.assertEqual(orderflow["l2_direction"], "BUY")
        self.assertTrue(orderflow["read_only"])

        audit = self.call_tool("decision_audit_lookup", {"symbol": "XAUUSD"})
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["records"][0]["scan_id"], "scan-1")

        validation = self.call_tool(
            "validate_strategy_proposal",
            {"proposal": {"name": "New Study", "builder": "visual", "flow": {}}},
        )
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["proposal"]["safety"]["order_execution_allowed"])
        listed = self.call_tool("list_strategy_proposals")
        self.assertEqual(listed["count"], 1)

    def test_json_rpc_errors_and_registry_rejects_unsafe_tools(self) -> None:
        unknown_method = self.server.handle(self.request(4, "unknown/method"))
        self.assertEqual(unknown_method["error"]["code"], -32601)
        unknown_tool = self.server.handle(
            self.request(5, "tools/call", {"name": "place_order", "arguments": {}})
        )
        self.assertEqual(unknown_tool["error"]["code"], -32602)
        invalid_json = self.server.handle_json("not-json")
        self.assertIn('"code":-32700', invalid_json)

        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register_tool("place_order", lambda args: {})
        with self.assertRaises(ValueError):
            registry.register_tool("unsafe_tool", lambda args: {}, read_only=False)

    def test_parallel_analysis_supports_sync_async_and_preserves_order(self) -> None:
        async def async_read(args: dict) -> dict:
            await asyncio.sleep(0.01)
            return {"value": args["value"], "kind": "async"}

        def sync_read(args: dict) -> dict:
            return {"value": args["value"], "kind": "sync"}

        self.server.registry.register_tool("async_read", async_read, description="async read")
        self.server.registry.register_tool("sync_read", sync_read, description="sync read")
        result = self.call_tool(
            "run_parallel_analysis",
            {
                "max_concurrency": 2,
                "analyses": [
                    {"tool": "async_read", "arguments": {"value": 1}},
                    {"tool": "sync_read", "arguments": {"value": 2}},
                ],
            },
        )
        self.assertEqual(result["max_concurrency"], 2)
        self.assertEqual([item["result"]["value"] for item in result["results"]], [1, 2])
        self.assertTrue(all(item["ok"] for item in result["results"]))
        self.assertTrue(result["read_only"])

        direct = asyncio.run(
            self.server.run_parallel_analysis(
                [{"tool": "market_snapshot", "arguments": {"symbol": "XAUUSD"}}],
                max_concurrency=20,
            )
        )
        self.assertEqual(direct["max_concurrency"], 2)


if __name__ == "__main__":
    unittest.main()
