"""Small, dependency-free registry and safety validator for the bot workspace."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "bot_registry.json"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def load_registry() -> dict[str, Any]:
    return load_json(REGISTRY_PATH)


def bot_by_id(registry: dict[str, Any], bot_id: str) -> dict[str, Any]:
    for bot in registry.get("bots", []):
        if isinstance(bot, dict) and bot.get("id") == bot_id:
            return bot
    raise RuntimeError(f"bot is not registered: {bot_id}")


def artifact_paths(bot: dict[str, Any]) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    execution = bot.get("execution") or {}
    research = bot.get("research") or {}
    charts = bot.get("charts") or {}
    for label, value in (
        ("execution.source", execution.get("source")),
        ("execution.preset", execution.get("preset")),
        ("execution.ai_gated_preset", execution.get("ai_gated_preset")),
        ("research.runner", research.get("runner")),
        ("research.config", research.get("config")),
        ("charts.tradingview_script", charts.get("tradingview_script")),
    ):
        if not value:
            continue
        raw = Path(str(value))
        paths.append((label, raw if raw.is_absolute() else ROOT / raw))
    return paths


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if registry.get("schema_version") != 1:
        errors.append("registry schema_version must be 1")
    bots = registry.get("bots")
    if not isinstance(bots, list) or not bots:
        return errors + ["registry must contain at least one bot"]

    ids: set[str] = set()
    for bot in bots:
        if not isinstance(bot, dict):
            errors.append("each bot entry must be an object")
            continue
        bot_id = bot.get("id")
        if not isinstance(bot_id, str) or not bot_id:
            errors.append("each bot needs a non-empty id")
            continue
        if bot_id in ids:
            errors.append(f"duplicate bot id: {bot_id}")
        ids.add(bot_id)
        for field in ("display_name", "status", "symbol", "execution", "safety"):
            if field not in bot:
                errors.append(f"{bot_id} is missing {field}")
        safety = bot.get("safety") or {}
        if safety.get("real_account_allowed") is not False:
            errors.append(f"{bot_id} must keep real_account_allowed=false")

    active = registry.get("active_bot")
    if active not in ids:
        errors.append(f"active_bot is not registered: {active}")
    return errors


def validate_active_artifacts(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    active = bot_by_id(registry, str(registry.get("active_bot")))
    for label, path in artifact_paths(active):
        if not path.exists():
            errors.append(f"missing active artifact {label}: {path}")

    config_path = ROOT / "configs" / f"{active['id']}.json"
    if not config_path.exists():
        errors.append(f"missing strategy config: {config_path}")
    else:
        config = load_json(config_path)
        if config.get("strategy_id") != active["id"]:
            errors.append(f"config strategy_id does not match active bot: {config_path}")
        safety = config.get("safety") or {}
        if safety.get("real_account_allowed") is not False:
            errors.append("strategy config must keep real_account_allowed=false")
    return errors


def validate_gate(path: Path, expected_strategy: str = "new-era-gold-council-v1") -> list[str]:
    errors: list[str] = []
    try:
        with path.open("r", newline="", encoding="ascii") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        return [f"cannot read gate {path}: {exc}"]
    if len(rows) != 1:
        return [f"gate must contain exactly one data row: {path}"]
    row = rows[0]
    required = {"version", "strategy", "symbol", "action", "confidence", "generated_epoch", "expires_epoch"}
    missing = sorted(required - set(row))
    if missing:
        errors.append(f"gate missing fields: {', '.join(missing)}")
        return errors
    if row["version"] != "1":
        errors.append(f"unsupported gate version: {row['version']}")
    if row["strategy"] != expected_strategy:
        errors.append(f"unexpected gate strategy: {row['strategy']} (expected {expected_strategy})")
    if row["action"] not in {"BUY", "SELL", "ABSTAIN"}:
        errors.append(f"unexpected gate action: {row['action']}")
    try:
        generated = int(row["generated_epoch"])
        expires = int(row["expires_epoch"])
        float(row["confidence"])
    except ValueError:
        errors.append("gate numeric fields are invalid")
    else:
        if expires <= generated:
            errors.append("gate expires before or at generated time")
    return errors


def print_list(registry: dict[str, Any]) -> int:
    print(f"Workspace: {ROOT}")
    print(f"Active bot: {registry.get('active_bot')}")
    for bot in registry.get("bots", []):
        print(f"- {bot.get('id')}: {bot.get('status')} | {bot.get('display_name')}")
    return 0


def print_status(registry: dict[str, Any]) -> int:
    print_list(registry)
    active = bot_by_id(registry, str(registry.get("active_bot")))
    print("\nActive artifacts:")
    for label, path in artifact_paths(active):
        print(f"- {'OK' if path.exists() else 'MISSING'}: {label} -> {path}")
    gate = ROOT / "TradingAgentsData" / "research" / "new_era_ai_gate.csv"
    print(f"- {'OK' if gate.exists() else 'NOT_CREATED'}: AI gate -> {gate}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="New Era bot registry manager")
    parser.add_argument(
        "action",
        choices=("list", "status", "validate", "validate-gate", "validate-kronos-gate"),
    )
    parser.add_argument("--gate", type=Path, default=ROOT / "TradingAgentsData" / "research" / "new_era_ai_gate.csv")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        if args.action == "list":
            return print_list(registry)
        if args.action == "status":
            return print_status(registry)
        if args.action == "validate-gate":
            errors = validate_gate(args.gate)
        elif args.action == "validate-kronos-gate":
            errors = validate_gate(args.gate, "new-era-kronos-matrix-v1")
        else:
            errors = validate_registry(registry)
            if not errors and args.action == "validate":
                errors.extend(validate_active_artifacts(registry))
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"{args.action}: OK")
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
