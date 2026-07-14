"""Fail-safe explanation for registered and unregistered terminal routes."""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA_VERSION = 1


def explain_fallback(facts: Mapping[str, Any]) -> dict[str, Any]:
    evidence: list[str] = []
    risks: list[str] = []
    if facts.get("registered"):
        evidence.append("exact route record exists")
        if (
            facts.get("generation_matches")
            and facts.get("pane_matches")
            and facts.get("client_matches")
        ):
            evidence.extend(("generation matches", "pane matches", "client matches"))
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "ready",
                "image_action": "registered_route",
                "text_action": "native_paste",
                "confidence": 1.0,
                "evidence": evidence,
                "risks": risks,
                "repair": None,
            }
        risks.append("registered route identity is stale or conflicting")
    else:
        risks.append("no exact route registration")

    for key, label in (
        ("title_only", "terminal title is not an identity"),
        ("detached", "pane is detached"),
        ("multi_hop", "multi-hop destination is ambiguous"),
        ("pane_reused", "pane identity was reused"),
        ("conflicting_targets", "multiple destination signals conflict"),
        ("stale", "route metadata is stale"),
    ):
        if facts.get(key):
            risks.append(label)
    if facts.get("process_tree_target"):
        evidence.append("SSH or mosh process tree names a target")
    if facts.get("host_alias_known"):
        evidence.append("target is present in the tracked host registry")
    confidence = 0.65 if len(evidence) == 2 and len(risks) == 1 else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "ambiguous" if risks else "unsupported",
        "image_action": "refuse_upload",
        "text_action": "native_paste",
        "confidence": confidence,
        "evidence": evidence,
        "risks": risks,
        "repair": "relaunch with d2 or d3 to register the exact pane",
    }
