"""Combine TradingAgents and Kronos Matrix gates without granting execution rights."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

from kronos_matrix_strategy import STRATEGY_ID, write_gate_csv


def read_gate(path: Path) -> dict[str, str]:
    with path.open("r", newline="", encoding="ascii") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise RuntimeError(f"gate must contain one data row: {path}")
    return rows[0]


def combine(ai: dict[str, str], kronos: dict[str, str]) -> tuple[str, float, list[str], int]:
    reasons: list[str] = []
    if ai.get("action") not in {"BUY", "SELL"}:
        reasons.append(f"TradingAgents action is {ai.get('action', 'missing')}")
    if kronos.get("action") not in {"BUY", "SELL"}:
        reasons.append(f"Kronos Matrix action is {kronos.get('action', 'missing')}")
    if not reasons and ai["action"] != kronos["action"]:
        reasons.append("TradingAgents and Kronos Matrix directions disagree")
    try:
        confidence = min(float(ai.get("confidence", 0.0)), float(kronos.get("confidence", 0.0)))
        expires = min(int(ai.get("expires_epoch", "0")), int(kronos.get("expires_epoch", "0")))
    except ValueError as exc:
        reasons.append(f"gate numeric field invalid: {exc}")
        confidence = 0.0
        expires = int(time.time()) + 60
    if expires <= int(time.time()):
        reasons.append("one input gate is expired")
    return ("ABSTAIN" if reasons else ai["action"], confidence if not reasons else 0.0, reasons, expires)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Require TradingAgents and Kronos agreement")
    parser.add_argument("--ai-gate", type=Path, default=root / "TradingAgentsData/research/new_era_ai_gate.csv")
    parser.add_argument("--kronos-gate", type=Path, default=root / "TradingAgentsData/research/new_era_kronos_matrix_gate.csv")
    parser.add_argument("--output", type=Path, default=root / "TradingAgentsData/research/new_era_kronos_matrix_gate.csv")
    parser.add_argument("--report", type=Path, default=root / "TradingAgentsData/research/combined_gate_latest.json")
    parser.add_argument("--symbol", default="XAUUSD")
    args = parser.parse_args(argv)
    try:
        ai = read_gate(args.ai_gate)
        kronos = read_gate(args.kronos_gate)
        action, confidence, reasons, expires = combine(ai, kronos)
        generated = int(time.time())
        expires = max(generated + 60, expires)
        write_gate_csv(
            args.output,
            symbol=args.symbol,
            action=action,
            confidence=confidence,
            generated_epoch=generated,
            expires_epoch=expires,
            strategy=STRATEGY_ID,
        )
        report: dict[str, Any] = {
            "strategy": STRATEGY_ID,
            "symbol": args.symbol,
            "action": action,
            "confidence": confidence,
            "execution_allowed": False,
            "real_account_allowed": False,
            "reasons": reasons,
            "inputs": {"tradingagents": ai, "kronos_matrix": kronos},
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Combined research gate: {action}; reasons={len(reasons)}")
        print(f"Wrote {args.output}")
        return 0
    except Exception as exc:
        print(f"Gate combination stopped safely: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
