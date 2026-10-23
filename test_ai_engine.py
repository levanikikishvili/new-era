from __future__ import annotations

import asyncio
import json
import unittest

from newera_api.ai_engine import (
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    OllamaAIEngine,
    build_scan_context,
    build_scan_prompt,
    normalize_model_result,
    parse_json_stream,
)


VALID_DECISION = {
    "action": "BUY",
    "confidence": 0.82,
    "risk_pct": 1.0,
    "min_rr": 1.8,
    "max_sl_atr": 2.0,
    "time_stop_minutes": 60,
    "reasoning_summary": "Matrix and orderflow agree on a research-only long bias.",
    "evidence": [{"source": "kronos", "direction": "BUY"}],
    "bot_proposals": [
        {
            "id": "matrix-research",
            "name": "Matrix Research",
            "builder": "visual",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "flow": {"actions": [{"type": "enter_long"}]},
        }
    ],
}


class FakeResponse:
    def __init__(self, body: bytes | str, status: int = 200) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.closed = False

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, body: bytes | str) -> None:
        self.body = body
        self.calls = 0
        self.last_request = None
        self.last_timeout = None

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.last_request = request
        self.last_timeout = timeout
        return FakeResponse(self.body)


class AIEngineTests(unittest.TestCase):
    def test_defaults_context_prompt_and_sync_call_are_json_safe(self) -> None:
        opener = FakeOpener(json.dumps({"choices": [{"message": {"content": json.dumps(VALID_DECISION)}}]}))
        engine = OllamaAIEngine(opener=opener, timeout=4.5)

        self.assertEqual(engine.endpoint, DEFAULT_OLLAMA_ENDPOINT)
        self.assertEqual(engine.model, DEFAULT_OLLAMA_MODEL)
        context = build_scan_context(
            "xauusd",
            [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}],
            {"rsi": 55.0},
            {"direction": "BUY"},
            {"imbalance_ratio": 0.2},
        )
        prompt = build_scan_prompt(context)
        self.assertIn('"symbol":"XAUUSD"', prompt)
        self.assertEqual(context["kronos_matrix"]["direction"], "BUY")

        result = engine.safe_call(context)
        self.assertEqual(result.action, "BUY")
        self.assertFalse(result.blocked)
        self.assertFalse(result.executable)
        self.assertTrue(result.research_only)
        self.assertEqual(result.bot_proposals[0]["status"], "research_only")
        self.assertEqual(opener.calls, 1)
        self.assertAlmostEqual(opener.last_timeout, 4.5)
        request_payload = json.loads(opener.last_request.data.decode("utf-8"))
        self.assertEqual(request_payload["model"], DEFAULT_OLLAMA_MODEL)
        self.assertFalse(request_payload["stream"])

    def test_malformed_output_and_hidden_reasoning_fail_closed(self) -> None:
        malformed = normalize_model_result(
            {
                "action": "BUY",
                "confidence": "not-a-number",
                "risk_pct": 1.0,
                "min_rr": 1.5,
                "max_sl_atr": 2.0,
                "time_stop_minutes": 60,
                "reasoning_summary": "should not be accepted",
            },
            symbol="XAUUSD",
        )
        self.assertEqual(malformed.action, "ABSTAIN")
        self.assertEqual(malformed.status, "BLOCKED")
        self.assertFalse(malformed.executable)

        hidden = normalize_model_result(
            {
                "action": "BUY",
                "confidence": 0.8,
                "risk_pct": 1.0,
                "min_rr": 1.5,
                "max_sl_atr": 2.5,
                "time_stop_minutes": 120,
                "reasoning_summary": "<think>private chain of thought</think>concise rationale",
            },
            symbol="XAUUSD",
        )
        self.assertFalse(hidden.blocked)
        self.assertNotIn("private chain of thought", hidden.reasoning_summary)
        self.assertEqual(hidden.reasoning_summary, "concise rationale")

    def test_hard_risk_caps_block_unsafe_values(self) -> None:
        for field, value in (
            ("risk_pct", 1.51),
            ("min_rr", 1.49),
            ("max_sl_atr", 2.51),
            ("time_stop_minutes", 121),
        ):
            payload = dict(VALID_DECISION)
            payload["bot_proposals"] = []
            payload[field] = value
            result = normalize_model_result(payload, symbol="XAUUSD")
            self.assertEqual(result.action, "ABSTAIN", field)
            self.assertEqual(result.status, "BLOCKED", field)
            self.assertFalse(result.executable, field)

    def test_stream_chunks_are_reassembled_and_async_call_is_safe(self) -> None:
        fragments = [
            "<think>omit this</think>{\"action\":\"BUY\",\"confidence\":0.75,",
            "\"risk_pct\":0.5,\"min_rr\":1.5,\"max_sl_atr\":2.5,",
            "\"time_stop_minutes\":30,\"reasoning_summary\":\"streamed rationale\"}",
        ]
        chunks = [
            "data: " + json.dumps({"choices": [{"delta": {"content": fragment}}]}) + "\n\n"
            for fragment in fragments
        ] + ["data: [DONE]\n\n"]
        parsed = parse_json_stream(chunks)
        self.assertEqual(parsed["action"], "BUY")

        opener = FakeOpener("\n".join(chunks))
        engine = OllamaAIEngine(opener=opener, stream=True)
        result = asyncio.run(engine.safe_call_async({"symbol": "XAUUSD"}))
        self.assertEqual(result.action, "BUY")
        self.assertFalse(result.blocked)

    def test_offline_mode_never_calls_transport(self) -> None:
        opener = FakeOpener("{}").__call__
        engine = OllamaAIEngine(opener=opener, offline=True)
        result = engine.safe_call({"symbol": "EURUSD"})
        self.assertEqual(result.action, "ABSTAIN")
        self.assertEqual(result.status, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
