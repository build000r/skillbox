"""Redacted latency summaries for smart-paste receipts."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class MetricsError(ValueError):
    """A receipt set cannot produce a trustworthy summary."""


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise MetricsError("no successful latency samples")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize(
    receipts: Iterable[Mapping[str, Any]],
    *,
    image_p50_budget_ms: float = 500.0,
    image_p95_budget_ms: float = 1500.0,
    included_outcomes: frozenset[str] = frozenset({"image_path", "document_path"}),
) -> dict[str, Any]:
    latencies: list[float] = []
    outcomes: dict[str, int] = {}
    routes: set[str] = set()
    rejected = 0
    ignored = 0
    for receipt in receipts:
        if receipt.get("schema_version") != 1:
            raise MetricsError("unknown smart-paste receipt schema")
        if receipt.get("ok") is not True:
            rejected += 1
            continue
        outcome = str(receipt.get("outcome", "unknown"))
        if outcome not in included_outcomes:
            ignored += 1
            continue
        latency = receipt.get("latency_ms")
        if not isinstance(latency, (int, float)) or isinstance(latency, bool):
            raise MetricsError("successful receipt lacks numeric latency_ms")
        if latency < 0:
            raise MetricsError("receipt latency cannot be negative")
        latencies.append(float(latency))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if receipt.get("route_id"):
            routes.add(str(receipt["route_id"]))
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    return {
        "schema_version": SCHEMA_VERSION,
        "sample_count": len(latencies),
        "rejected_count": rejected,
        "ignored_count": ignored,
        "route_count": len(routes),
        "outcomes": dict(sorted(outcomes.items())),
        "latency_ms": {
            "min": round(min(latencies), 3),
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "max": round(max(latencies), 3),
        },
        "budget": {
            "image_p50_ms": image_p50_budget_ms,
            "image_p95_ms": image_p95_budget_ms,
            "p50_pass": p50 <= image_p50_budget_ms,
            "p95_pass": p95 <= image_p95_budget_ms,
            "enough_for_rollout": len(latencies) >= 20,
        },
        "redaction": "clipboard bytes, text, paths, targets, and credentials omitted",
    }


def load_receipts(paths: Iterable[Path]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MetricsError(f"invalid receipt file: {path.name}") from exc
        if not isinstance(payload, dict):
            raise MetricsError(f"receipt is not an object: {path.name}")
        result.append(payload)
    return result
