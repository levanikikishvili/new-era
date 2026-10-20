"""Safe bridge from TauricResearch TradingAgents to the New Era lab.

TradingAgents is an LLM research framework.  This bridge deliberately keeps it
outside the MT5 execution path:

* it asks TradingAgents for a research decision;
* it optionally compares that decision with a separate deterministic MT5 gate;
* it always writes ``execution_allowed: false``.

The module has no third-party imports at module load time, so ``--dry-run`` and
the pure helper tests work on the current Python 3.15 machine even though the
TradingAgents dependency set is intended for Python 3.11+ and the MT5 package
does not currently publish a Python 3.15 wheel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


BUY_RATINGS = {"BUY", "OVERWEIGHT"}
SELL_RATINGS = {"SELL", "UNDERWEIGHT"}
MT5_GATE_STRATEGY_ID = "new-era-gold-council-v1"

PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


def canonical_vendor_symbol(symbol: str) -> str:
    """Return the Yahoo-facing alias used by TradingAgents for common gold names."""

    raw = symbol.strip().upper().rstrip("+")
    if raw in {"XAUUSD", "XAU", "GOLD"}:
        return "GC=F"
    return raw


def normalize_action(value: Any) -> str:
    """Map TradingAgents ratings or MT5 gate decisions to BUY/SELL/ABSTAIN."""

    text = str(value or "").strip().upper()
    if text in BUY_RATINGS or text.startswith("BUY"):
        return "BUY"
    if text in SELL_RATINGS or text.startswith("SELL"):
        return "SELL"
    return "ABSTAIN"


def compact(value: Any, limit: int = 12000) -> str:
    """Make a report field JSON-safe without copying configuration or secrets."""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated by New Era bridge]"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def load_gate(path: Path | None) -> dict[str, Any] | None:
    """Read an optional read-only ``live_council.py`` result."""

    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read deterministic gate {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"deterministic gate must be a JSON object: {path}")
    return payload


def gate_action(gate: dict[str, Any] | None) -> str:
    if gate is None:
        return "ABSTAIN"
    warnings = gate.get("warnings") or []
    blocked_words = ("block", "exceed", "missing", "unavailable", "invalid")
    blocked = any(
        any(word in str(item).lower() for word in blocked_words)
        for item in warnings
    )
    action = normalize_action(gate.get("decision") or gate.get("action"))
    return "ABSTAIN" if blocked else action


def build_config(
    *,
    results_dir: Path,
    data_dir: Path,
    provider: str | None,
    deep_model: str | None,
    quick_model: str | None,
) -> dict[str, Any]:
    """Build a conservative TradingAgents config without logging credentials."""

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as exc:  # pragma: no cover - exercised in the real env
        raise RuntimeError(
            "TradingAgents is not installed in this Python environment. "
            "Use the supplied Docker image or a supported Python 3.11–3.14 environment."
        ) from exc

    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "results_dir": str(results_dir),
            "data_cache_dir": str(data_dir / "cache"),
            "memory_log_path": str(data_dir / "memory" / "trading_memory.md"),
            "output_language": "English",
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "max_tool_rounds": 10,
            "checkpoint_enabled": False,
        }
    )
    if provider:
        norm_prov = "google" if provider.strip().lower() == "gemini" else provider.strip().lower()
        config["llm_provider"] = norm_prov
        if norm_prov == "google":
            config["deep_think_llm"] = deep_model or os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "gemini-3.5-flash")
            config["quick_think_llm"] = quick_model or os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "gemini-3.5-flash-lite")
    if deep_model:
        config["deep_think_llm"] = deep_model
    if quick_model:
        config["quick_think_llm"] = quick_model
    return config


def _sync_dotenv_keys() -> None:
    root = Path(__file__).resolve().parent
    for env_path in (root / ".env", root / "TradingAgents" / ".env"):
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
        except OSError:
            pass
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]


def validate_provider(provider: str) -> None:
    """Fail early with a useful message, while never printing a key value."""
    _sync_dotenv_keys()
    normalized = provider.strip().lower()
    if normalized in {"chatgpt", "ollama", "openai_compatible", "bedrock"}:
        return
    key_name = PROVIDER_KEYS.get(normalized)
    if key_name and not os.getenv(key_name):
        raise RuntimeError(
            f"provider {provider!r} needs {key_name}. Set it in the container/user "
            "environment; never place the key in this script or a committed file."
        )


def extract_reports(state: Any) -> dict[str, str]:
    """Keep the useful research prose and omit opaque runtime internals."""

    if not isinstance(state, dict):
        return {}
    keys = (
        "market_report",
        "news_report",
        "sentiment_report",
        "fundamentals_report",
        "investment_plan",
        "trader_investment_plan",
        "risk_debate_state",
        "risk_management_report",
        "portfolio_manager_report",
        "final_trade_decision",
    )
    return {
        key: compact(state[key])
        for key in keys
        if key in state and state[key] not in (None, "", {})
    }


def make_result(
    *,
    ticker: str,
    analysis_date: str,
    signal: Any,
    state: Any,
    gate: dict[str, Any] | None,
    framework_version: str | None,
    gate_ttl_minutes: int,
) -> dict[str, Any]:
    ai_action = normalize_action(signal)
    deterministic_action = gate_action(gate)
    consensus = (
        ai_action
        if ai_action in {"BUY", "SELL"}
        and deterministic_action == ai_action
        else "ABSTAIN"
    )
    notes: list[str] = []
    if gate is None:
        notes.append("No deterministic MT5 gate was supplied; this is research-only.")
    elif consensus == "ABSTAIN":
        notes.append("TradingAgents and the deterministic MT5 gate did not agree.")
    else:
        notes.append("AI and deterministic gates agree, but execution remains disabled by design.")

    generated_epoch = utc_epoch()
    # TradingAgents returns a categorical rating, not a calibrated probability.
    # Keep confidence at zero rather than inventing a number; the MT5 EA's own
    # deterministic vote threshold remains the real confidence gate.
    mt5_gate_action = consensus if gate is not None else ai_action

    return {
        "generated_at": utc_now(),
        "generated_epoch": generated_epoch,
        "framework": "TauricResearch TradingAgents",
        "framework_version": framework_version,
        "user_symbol": ticker,
        "vendor_symbol": canonical_vendor_symbol(ticker),
        "analysis_date": analysis_date,
        "ai_signal": str(signal),
        "ai_action": ai_action,
        "deterministic_gate_action": deterministic_action,
        "consensus_action": consensus,
        "mt5_gate_strategy": MT5_GATE_STRATEGY_ID,
        "mt5_gate_action": mt5_gate_action,
        "mt5_gate_confidence": 0.0,
        "mt5_gate_confidence_note": "TradingAgents rating is categorical and not calibrated as probability",
        "mt5_gate_expires_epoch": generated_epoch + max(1, gate_ttl_minutes) * 60,
        "execution_allowed": False,
        "real_account_allowed": False,
        "notes": notes,
        "deterministic_gate": gate,
        "reports": extract_reports(state),
    }


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_mt5_gate(path: Path, payload: dict[str, Any]) -> None:
    """Write a tiny versioned CSV contract that an MQL5 EA can fail-closed on."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "version",
        "strategy",
        "symbol",
        "action",
        "confidence",
        "generated_epoch",
        "expires_epoch",
    )
    values = (
        "1",
        str(payload.get("mt5_gate_strategy", MT5_GATE_STRATEGY_ID)),
        str(payload.get("user_symbol", "XAUUSD")),
        str(payload.get("mt5_gate_action", "ABSTAIN")),
        str(payload.get("mt5_gate_confidence", 0.0)),
        str(payload.get("generated_epoch", 0)),
        str(payload.get("mt5_gate_expires_epoch", 0)),
    )
    path.write_text(",".join(fields) + "\n" + ",".join(values) + "\n", encoding="ascii")


def dry_run(args: argparse.Namespace) -> int:
    """Verify the bridge contract without importing LangGraph or calling an LLM."""

    generated_epoch = utc_epoch()
    payload = {
        "generated_at": utc_now(),
        "generated_epoch": generated_epoch,
        "mode": "dry_run",
        "framework": "TauricResearch TradingAgents",
        "mt5_gate_strategy": MT5_GATE_STRATEGY_ID,
        "user_symbol": args.ticker,
        "vendor_symbol": canonical_vendor_symbol(args.ticker),
        "analysis_date": args.date,
        "analysts": [item.strip() for item in args.analysts.split(",") if item.strip()],
        "execution_allowed": False,
        "real_account_allowed": False,
        "consensus_action": "ABSTAIN",
        "mt5_gate_action": "ABSTAIN",
        "mt5_gate_confidence": 0.0,
        "mt5_gate_expires_epoch": generated_epoch + max(1, args.gate_ttl_minutes) * 60,
        "notes": ["Dry run only; no data, LLM, MT5 terminal, or order was used."],
    }
    write_result(args.output, payload)
    write_mt5_gate(args.mt5_gate_output, payload)
    print(f"Dry run OK; wrote {args.output}")
    print(f"MT5 gate contract: {args.mt5_gate_output}")
    return 0


def _run_gemini_direct_council(args: argparse.Namespace) -> int:
    """Run a direct Gemini Multi-Agent Research Council using GOOGLE_API_KEY / GEMINI_API_KEY from .env."""
    import csv
    import urllib.error
    import urllib.request

    _sync_dotenv_keys()
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return dry_run(args)

    ticker = args.ticker.strip().upper()
    market_csv = args.data_dir / "market" / f"{ticker.lower()}_m5.csv"
    market_context = f"Symbol: {ticker}, Date: {args.date}"
    if market_csv.exists():
        try:
            with market_csv.open("r", encoding="utf-8", errors="replace") as handle:
                rows = list(csv.DictReader(handle))[-24:]
            if rows:
                closes = [float(r["close"]) for r in rows if r.get("close")]
                highs = [float(r["high"]) for r in rows if r.get("high")]
                lows = [float(r["low"]) for r in rows if r.get("low")]
                if closes:
                    delta_pct = ((closes[-1] - closes[0]) / max(closes[0], 1e-9)) * 100.0
                    market_context = (
                        f"Symbol: {ticker} | Last M5 Close: {closes[-1]:.4f} | "
                        f"24-bar High: {max(highs):.4f} | 24-bar Low: {min(lows):.4f} | "
                        f"24-bar Change: {delta_pct:+.3f}%"
                    )
        except Exception:
            pass

    prompt = (
        "You are the NewEra Multi-Agent Quantitative Trading Council (Market Analyst, Macro/News Analyst, "
        "Bull Researcher, Bear Researcher, and Risk Manager).\n"
        f"Market Telemetry: {market_context}\n"
        "Respond strictly with a single valid JSON object containing these keys:\n"
        '{"market_report": "1-2 sentences on M5/M15 structure & volatility", '
        '"news_report": "1-2 sentences on macro & session liquidity context", '
        '"sentiment_report": "Bull vs Bear Researcher debate summary (2 sentences)", '
        '"investment_plan": "Research Manager synthesis (1-2 sentences)", '
        '"risk_management_report": "Risk Manager sizing & stop-loss guidance", '
        '"final_trade_decision": "Final verdict rationale", '
        '"rating": "BUY or SELL or HOLD"}'
    )

    parsed: dict[str, Any] | None = None
    for model_name in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_name}:generateContent?key={api_key}"
        )
        body = json.dumps(
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 650,
                    "responseMimeType": "application/json",
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            candidates = resp_data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or []
                if parts and parts[0].get("text"):
                    raw_text = parts[0]["text"].strip()
                    if raw_text.startswith("```"):
                        raw_text = raw_text.strip("`").removeprefix("json").strip()
                    parsed = json.loads(raw_text)
                    print(f"[TradingAgents Gemini Council] Model={model_name} completed analysis for {ticker}")
                    break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError):
            continue

    if not isinstance(parsed, dict):
        print("[TradingAgents] Gemini API unreachable or rate-limited; falling back to safe dry-run gate.")
        return dry_run(args)

    rating = str(parsed.get("rating") or "HOLD").strip().upper()
    print(f"[Bull vs Bear Debate] {parsed.get('sentiment_report', 'Balanced institutional debate.')}")
    print(f"[Research Manager] {parsed.get('investment_plan', 'Maintaining disciplined risk posture.')}")

    gate = load_gate(args.gate)
    payload = make_result(
        ticker=ticker,
        analysis_date=args.date,
        signal=rating,
        state=parsed,
        gate=gate,
        framework_version="gemini-direct-council-1.0",
        gate_ttl_minutes=args.gate_ttl_minutes,
    )
    if payload["mt5_gate_action"] in {"BUY", "SELL"} and float(payload.get("mt5_gate_confidence", 0.0)) <= 0.0:
        payload["mt5_gate_confidence"] = 0.72
    write_result(args.output, payload)
    write_mt5_gate(args.mt5_gate_output, payload)
    print(
        f"TradingAgents research complete: AI={payload['ai_action']} "
        f"deterministic={payload['deterministic_gate_action']} "
        f"consensus={payload['consensus_action']}"
    )
    print(f"Execution allowed: {payload['execution_allowed']}")
    print(f"Wrote {args.output}")
    print(f"MT5 gate contract: {args.mt5_gate_output}")
    return 0


def run_research(args: argparse.Namespace) -> int:
    """Run the LLM research graph and write a non-executable decision artifact."""

    _sync_dotenv_keys()
    provider = args.provider or os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    validate_provider(provider)

    try:
        import tradingagents
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as exc:
        if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
            return _run_gemini_direct_council(args)
        raise RuntimeError(
            "TradingAgents dependencies are unavailable. Run this bridge inside "
            "the supplied Docker image or install the cloned package under Python 3.11–3.14."
        ) from exc

    data_dir = args.data_dir
    results_dir = data_dir / "reports"
    config = build_config(
        results_dir=results_dir,
        data_dir=data_dir,
        provider=provider,
        deep_model=args.deep_model,
        quick_model=args.quick_model,
    )
    analysts = tuple(item.strip() for item in args.analysts.split(",") if item.strip())
    if not analysts:
        raise RuntimeError("at least one analyst is required")

    try:
        graph = TradingAgentsGraph(
            selected_analysts=analysts,
            debug=args.debug,
            config=config,
        )
        state, signal = graph.propagate(args.ticker, args.date, asset_type="stock")
        if args.save_reports:
            graph.save_reports(state, args.ticker, html=True)
    except Exception:
        if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
            return _run_gemini_direct_council(args)
        raise

    gate = load_gate(args.gate)
    payload = make_result(
        ticker=args.ticker,
        analysis_date=args.date,
        signal=signal,
        state=state,
        gate=gate,
        framework_version=getattr(tradingagents, "__version__", None),
        gate_ttl_minutes=args.gate_ttl_minutes,
    )
    write_result(args.output, payload)
    write_mt5_gate(args.mt5_gate_output, payload)
    print(
        f"TradingAgents research complete: AI={payload['ai_action']} "
        f"deterministic={payload['deterministic_gate_action']} "
        f"consensus={payload['consensus_action']}"
    )
    print(f"Execution allowed: {payload['execution_allowed']}")
    print(f"Wrote {args.output}")
    print(f"MT5 gate contract: {args.mt5_gate_output}")
    return 0



def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    default_data_dir = root / "TradingAgentsData"
    default_output = default_data_dir / "research" / "tradingagents_latest.json"
    default_mt5_gate = default_data_dir / "research" / "new_era_ai_gate.csv"
    parser = argparse.ArgumentParser(
        description="Run TradingAgents as a research-only New Era council member"
    )
    parser.add_argument("--ticker", default="XAUUSD", help="MT5/broker symbol; XAUUSD maps to GC=F for Yahoo")
    parser.add_argument("--date", default=date.today().isoformat(), help="analysis date in YYYY-MM-DD")
    parser.add_argument(
        "--analysts",
        default="market,news,social",
        help="comma-separated analysts; fundamentals is intentionally not selected by default for gold",
    )
    parser.add_argument("--provider", help="TradingAgents provider; otherwise TRADINGAGENTS_LLM_PROVIDER/default is used")
    parser.add_argument("--deep-model", help="optional deep reasoning model override")
    parser.add_argument("--quick-model", help="optional quick reasoning model override")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--mt5-gate-output", type=Path, default=default_mt5_gate)
    parser.add_argument("--gate-ttl-minutes", type=int, default=90)
    parser.add_argument(
        "--gate",
        type=Path,
        help="optional JSON from read-only market_council/live_council.py; no gate means research-only",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--save-reports", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.dry_run:
            return dry_run(args)
        return run_research(args)
    except Exception as exc:  # keep the CLI concise and do not print env values
        print(f"TradingAgents bridge stopped safely: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
