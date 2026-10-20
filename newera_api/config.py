"""Configuration for the local New Era API service.

Only local paths and non-secret runtime switches live here. Provider API keys
remain in the existing ``TradingAgents\\.env`` file or the process environment;
the API never accepts them in request bodies and never returns them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_workspace_dotenv(root: Path = ROOT) -> dict[str, str]:
    """Load key-value pairs from root `.env` and `TradingAgents/.env` into `os.environ`."""
    loaded: dict[str, str] = {}
    for env_path in (root / ".env", root / "TradingAgents" / ".env"):
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and val and not val.startswith("your_"):
                    loaded[key] = val
                    if not os.environ.get(key):
                        os.environ[key] = val
        except OSError:
            pass

    # Sync GEMINI_API_KEY <-> GOOGLE_API_KEY for TradingAgents LLM compatibility
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if gemini_key and not google_key:
        os.environ["GOOGLE_API_KEY"] = gemini_key
        loaded["GOOGLE_API_KEY"] = gemini_key
    elif google_key and not gemini_key:
        os.environ["GEMINI_API_KEY"] = google_key
        loaded["GEMINI_API_KEY"] = google_key
    return loaded


_DOTENV_VARS = load_workspace_dotenv(ROOT)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiSettings:
    root: Path = ROOT
    host: str = os.getenv("NEWERA_API_HOST", "127.0.0.1")
    port: int = int(os.getenv("NEWERA_API_PORT", "8787"))
    allow_origins: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "NEWERA_API_ALLOW_ORIGINS",
            "http://127.0.0.1:8787,http://localhost:8787",
        ).split(",")
        if item.strip()
    )
    scan_timeout_seconds: int = int(os.getenv("NEWERA_SCAN_TIMEOUT_SECONDS", "1800"))
    default_symbol: str = os.getenv("NEWERA_DEFAULT_SYMBOL", "XAUUSD")
    default_provider: str = os.getenv(
        "NEWERA_TRADINGAGENTS_PROVIDER",
        "google" if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") else "",
    )
    max_spread_points: float = float(os.getenv("NEWERA_MAX_SPREAD_POINTS", "180.0"))
    max_spread_to_atr: float = float(os.getenv("NEWERA_MAX_SPREAD_TO_ATR", "0.45"))
    memory_dir: Path = ROOT / "TradingAgentsData" / "memory"
    report_dir: Path = ROOT / "TradingAgentsData" / "research"
    chroma_enabled: bool = _bool("NEWERA_CHROMA_ENABLED", True)
    chroma_collection: str = os.getenv("NEWERA_CHROMA_COLLECTION", "newera_trade_cycles")
    process_shell: str = os.getenv("NEWERA_POWERSHELL", "powershell")
    safe_mode: bool = _bool("NEWERA_SAFE_MODE", True)

    @property
    def gemini_key_configured(self) -> bool:
        load_workspace_dotenv(self.root)
        return bool(os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip())

    @property
    def tradingagents_runner(self) -> Path:
        return self.root / "run_tradingagents_research.ps1"

    @property
    def kronos_runner(self) -> Path:
        return self.root / "run_kronos_matrix_research.ps1"

    @property
    def combine_runner(self) -> Path:
        return self.root / "combine_research_gates.py"

    @property
    def registry_path(self) -> Path:
        return self.root / "bot_registry.json"

    @property
    def kronos_gate_path(self) -> Path:
        return self.report_dir / "new_era_kronos_matrix_gate.csv"

    @property
    def ai_gate_path(self) -> Path:
        return self.report_dir / "new_era_ai_gate.csv"

    @property
    def combined_report_path(self) -> Path:
        return self.report_dir / "combined_gate_latest.json"

    def ensure_directories(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def resolve_workspace_path(self, value: str | Path | None, *, default: Path | None = None) -> Path:
        """Resolve a request path and reject traversal outside this workspace."""

        candidate = Path(value) if value else (default or self.root)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        root = self.root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"path must stay inside the NewEraTradingBot workspace: {value}") from exc
        return resolved


settings = ApiSettings()
