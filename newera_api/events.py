"""Async event fan-out for WebSocket clients and future desktop UIs."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any


def event(event_type: str, *, scan_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "type": event_type,
        "scan_id": scan_id,
        "payload": payload,
    }


class EventBus:
    """Small in-process pub/sub bus.

    It is intentionally local and ephemeral. The durable artifacts remain JSON,
    CSV and ChromaDB; a reconnecting UI can fetch the latest state through HTTP.
    """

    def __init__(self, *, queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow UI cannot block the trading/research pipeline.
                continue

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
