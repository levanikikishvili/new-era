"""Small, dependency-free registry and safety validator for the bot workspace."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "bot_registry.json"
BOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
BOT_MUTABLE_STATUSES = {"research_only", "demo_only", "disabled"}


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


def _normalize_bot_id(value: str) -> str:
    candidate = str(value or "").strip().lower().replace(" ", "-")
    if not BOT_ID_PATTERN.fullmatch(candidate):
        raise ValueError("bot id must be 2-64 characters using letters, numbers, '.', '_' or '-'")
    return candidate


def save_registry(registry: dict[str, Any], path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Validate and atomically persist a registry without touching artifacts."""

    errors = validate_registry(registry)
    if errors:
        raise ValueError("invalid bot registry: " + "; ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return registry


def add_bot_profile(
    display_name: str,
    *,
    bot_id: str | None = None,
    symbol: str = "XAUUSD",
    path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    """Add a safe research-only profile; no executable artifact is created."""

    registry = load_json(path)
    clean_name = str(display_name or "").strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("display_name must be 1-120 characters")
    normalized_id = _normalize_bot_id(bot_id or clean_name)
    existing_ids = {
        str(item.get("id"))
        for item in registry.get("bots", [])
        if isinstance(item, dict) and item.get("id")
    }
    if normalized_id in existing_ids:
        raise ValueError(f"bot is already registered: {normalized_id}")
    normalized_symbol = str(symbol or "XAUUSD").strip().upper()
    if not normalized_symbol or any(character not in set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-") for character in normalized_symbol):
        raise ValueError("symbol contains unsupported characters")
    bot = {
        "id": normalized_id,
        "display_name": clean_name,
        "status": "research_only",
        "enabled": True,
        "symbol": normalized_symbol,
        "execution": {"kind": "research_profile", "source": None},
        "research": {"kind": "local_profile", "output_directory": "TradingAgentsData/research"},
        "safety": {"real_account_allowed": False, "direct_python_orders": False},
    }
    bots = registry.setdefault("bots", [])
    if not isinstance(bots, list):
        raise ValueError("registry bots must be a list")
    bots.append(bot)
    save_registry(registry, path)
    return bot


def set_bot_enabled(bot_id: str, enabled: bool, *, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_json(path)
    bot = bot_by_id(registry, _normalize_bot_id(bot_id))
    bot["enabled"] = bool(enabled)
    if not bot["enabled"]:
        bot["status"] = "disabled"
    elif str(bot.get("status") or "").lower() == "disabled":
        bot["status"] = "research_only"
    save_registry(registry, path)
    return bot


def remove_bot_profile(bot_id: str, *, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_json(path)
    normalized_id = _normalize_bot_id(bot_id)
    if str(registry.get("active_bot")) == normalized_id:
        raise ValueError("active bot cannot be removed; activate another profile first")
    bots = registry.get("bots")
    if not isinstance(bots, list) or len(bots) <= 1:
        raise ValueError("the registry must keep at least one bot")
    remaining = [item for item in bots if not (isinstance(item, dict) and item.get("id") == normalized_id)]
    if len(remaining) == len(bots):
        raise ValueError(f"bot is not registered: {normalized_id}")
    registry["bots"] = remaining
    save_registry(registry, path)
    return {"id": normalized_id, "removed": True}


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
