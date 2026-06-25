"""Small timing helpers for agent-facing JSON payloads."""
from __future__ import annotations

import time
from typing import Any


def timer_start() -> float:
    """Return a monotonic start marker for elapsed payload metadata."""
    return time.perf_counter()


def elapsed_ms(start: float) -> float:
    """Return elapsed milliseconds rounded for compact JSON output."""
    return round((time.perf_counter() - start) * 1000.0, 3)


def attach_elapsed(payload: dict[str, Any], start: float) -> dict[str, Any]:
    """Attach ``meta.elapsed_ms`` to a JSON payload and return it."""
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        payload["meta"] = meta
    meta["elapsed_ms"] = elapsed_ms(start)
    return payload


__all__ = ["attach_elapsed", "elapsed_ms", "timer_start"]
