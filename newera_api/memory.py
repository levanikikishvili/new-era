"""ChromaDB-backed cycle memory with a safe JSONL fallback.

The service must still start when ChromaDB is not installed, so the fallback is
intentional rather than a hidden failure. Once ``chromadb`` is installed, the
same cycle records are stored in a persistent collection under
``TradingAgentsData/memory/chroma``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any


def _excerpt(value: Any, limit: int = 2400) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit] + ("…" if len(text) > limit else "")


class HashEmbeddingFunction:
    """Deterministic local embeddings so Chroma does not download a model."""

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def name(self) -> str:
        return "newera-hash-embedding-v1"

    def __call__(self, input: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in input:
            vector = [0.0] * self.dimensions
            tokens = re.findall(r"[a-z0-9_]+", text.lower())
            for token in tokens:
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(item * item for item in vector)) or 1.0
            result.append([item / norm for item in vector])
        return result


class MemoryStore:
    def __init__(self, directory: Path, *, enabled: bool = True, collection_name: str = "newera_trade_cycles") -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fallback_path = self.directory / "cycles.jsonl"
        self.backend = "jsonl"
        self.error: str | None = None
        self.collection = None
        self._embedding = HashEmbeddingFunction()
        if enabled:
            try:
                import chromadb

                client = chromadb.PersistentClient(path=str(self.directory / "chroma"))
                self.collection = client.get_or_create_collection(
                    name=collection_name,
                    embedding_function=self._embedding,
                    metadata={"description": "New Era research-cycle memory; never an order log"},
                )
                self.backend = "chromadb"
            except Exception as exc:  # pragma: no cover - depends on optional package
                self.error = str(exc)

    def _record(self, cycle: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        cycle_id = str(cycle.get("scan_id") or f"cycle-{int(time.time() * 1000)}")
        strategy = str(cycle.get("strategy") or "unknown")
        action = str(cycle.get("action") or "ABSTAIN")
        reasons = cycle.get("reasons") or []
        document = "\n".join(
            [
                f"strategy={strategy}",
                f"symbol={cycle.get('symbol', 'XAUUSD')}",
                f"action={action}",
                f"score={cycle.get('score', '')}",
                f"confidence={cycle.get('confidence', '')}",
                f"reasons={_excerpt(reasons)}",
                f"result={_excerpt(cycle.get('result', 'pending'))}",
                f"analysis={_excerpt(cycle.get('analysis', ''))}",
            ]
        )
        metadata = {
            "scan_id": cycle_id,
            "strategy": strategy,
            "symbol": str(cycle.get("symbol") or "XAUUSD"),
            "action": action,
            "score": float(cycle.get("score") or 0.0),
            "confidence": float(cycle.get("confidence") or 0.0),
            "created_at": float(cycle.get("created_at") or time.time()),
        }
        return cycle_id, document, metadata

    def store_cycle(self, cycle: dict[str, Any]) -> dict[str, Any]:
        cycle_id, document, metadata = self._record(cycle)
        if self.collection is not None:
            self.collection.upsert(
                ids=[cycle_id],
                documents=[document],
                metadatas=[metadata],
            )
            return {"stored": True, "backend": self.backend, "id": cycle_id}

        with self.fallback_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": cycle_id, "document": document, "metadata": metadata}, ensure_ascii=False) + "\n")
        return {"stored": True, "backend": self.backend, "id": cycle_id, "fallback_reason": self.error}

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        if self.collection is not None:
            result = self.collection.query(
                query_embeddings=self._embedding([query]),
                n_results=max(1, min(limit, 20)),
            )
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            return [
                {"document": document, "metadata": metadata, "distance": distance}
                for document, metadata, distance in zip(documents, metadatas, distances)
            ]

        if not self.fallback_path.exists():
            return []
        query_tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))
        scored: list[tuple[int, dict[str, Any]]] = []
        for line in self.fallback_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            tokens = set(re.findall(r"[a-z0-9_]+", str(record.get("document", "")).lower()))
            scored.append((len(query_tokens & tokens), record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record | {"score": score} for score, record in scored[: max(1, min(limit, 20))] if score > 0]

    def health(self) -> dict[str, Any]:
        count = None
        if self.collection is not None:
            try:
                count = self.collection.count()
            except Exception:
                count = None
        elif self.fallback_path.exists():
            count = len(self.fallback_path.read_text(encoding="utf-8").splitlines())
        return {"backend": self.backend, "count": count, "error": self.error}
