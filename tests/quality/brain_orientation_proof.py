#!/usr/bin/env python3
"""Standalone safe-orientation proof for the agent operations brain.

Skillbox could already prove that the brain answers *fast* and answers with the
*right shape*. Neither says anything about whether ``next``, ``explain``, and
``search`` orient an agent **safely**: a confidently wrong recommendation
passes every schema and latency gate ever written for it. Making the brain
faster or broader without this file just amplifies confident wrongness.

This proof replays a redacted scenario corpus
(``tests/goldens/agent_ops_orientation_scenarios.json``) through the real
in-process payload functions and scores each answer against rules the scenario
declares up front:

* **acceptable_actions** -- every action that is safe here. Several scenarios
  have several safe answers, so there is deliberately no exact-action oracle.
* **forbidden_actions** -- actions that must never appear, as ids or globs.
* **grounding** -- which evidence sources the answer may cite, which it must
  cite, which cautions must stay in the returned set, and which conflicts must
  be reported.
* **abstention** -- whether refusing to act is the correct answer.

Failures are split into two classes that must never be averaged together:

* ``false_safe``   -- the brain moved: it acted when it should have abstained,
  surfaced a forbidden action, emitted an unsafe command, or lost the grounding
  that would justify its answer. This is the failure that hurts.
* ``false_abstain``-- the brain stalled: it refused, or refused without a route
  forward, while a vetted safe action was available. This is the failure that
  makes the brain useless.

Everything here is deterministic: fixed fixtures, rule-based matching, no
LLM-as-judge, no network, no Docker, no subprocess, no filesystem reads beyond
the corpus itself. Timing fields are stripped before scoring so two runs on
different machines produce byte-identical scorecards.

Usage:
    python3 tests/quality/brain_orientation_proof.py [--json] [--out DIR]
                                                     [--strict] [--emit-baseline]

Exit codes:
    0  observed results match the baseline recorded in the corpus
    1  drift from the recorded baseline (either direction), or --strict with
       any false_safe finding
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.agent_decisions import explain_payload, next_action_payload  # noqa: E402
from runtime_manager.agent_search import search_payload  # noqa: E402

CORPUS_PATH = ROOT_DIR / "tests" / "goldens" / "agent_ops_orientation_scenarios.json"

CATEGORIES = (
    "healthy",
    "degraded",
    "conflicting",
    "missing_evidence",
    "unsafe_action",
    "abstention",
)
SURFACES = ("next", "explain", "search")

FALSE_SAFE = "false_safe"
FALSE_ABSTAIN = "false_abstain"

# Every rule is deterministic and maps to exactly one failure class. Rules are
# named so a scorecard says *why* an answer failed, not merely that it did.
RULE_CLASSES: dict[str, str] = {
    "acted_when_abstention_required": FALSE_SAFE,
    "forbidden_top_action": FALSE_SAFE,
    "unlisted_top_action": FALSE_SAFE,
    "forbidden_action_returned": FALSE_SAFE,
    "forbidden_command_emitted": FALSE_SAFE,
    "missing_required_surface": FALSE_SAFE,
    "unsurfaced_conflict": FALSE_SAFE,
    "ungrounded_evidence_source": FALSE_SAFE,
    "missing_required_evidence_source": FALSE_SAFE,
    "missing_required_reason": FALSE_SAFE,
    "uncited_recommendation": FALSE_SAFE,
    "abstained_when_action_available": FALSE_ABSTAIN,
    "no_recommendation_when_action_available": FALSE_ABSTAIN,
    "dead_end_abstention": FALSE_ABSTAIN,
}

REQUIRED_SCENARIO_FIELDS = (
    "id",
    "category",
    "surface",
    "summary",
    "evidence_inputs",
    "fixture",
    "acceptable_actions",
    "forbidden_actions",
    "grounding",
    "abstention",
)
REQUIRED_GROUNDING_FIELDS = (
    "allowed_evidence_sources",
    "required_evidence_sources",
    "required_present_actions",
    "required_disagreement_codes",
)
REQUIRED_ABSTENTION_FIELDS = ("required", "rationale")

# Values that must never enter a corpus that ships in a public repo. Secrets
# first, then the machine-specific strings this repo has historically baked
# into goldens.
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{16,}"),
    ("github_pat", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("openai_key", r"\bsk-[A-Za-z0-9]{20,}"),
    ("slack_token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("assigned_secret", r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|bearer)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{12,}"),
    ("long_hex_digest", r"\b[0-9a-f]{32,}\b"),
)
MACHINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("posix_home_path", r"/(?:home|Users|root)/[A-Za-z0-9._-]+"),
    ("srv_path", r"/srv/[A-Za-z0-9._/-]+"),
    ("windows_user_path", r"[A-Za-z]:\\\\?Users\\\\?"),
    ("tmp_machine_path", r"/var/folders/[A-Za-z0-9]"),
    ("ip_literal", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("private_hostname", r"\b[A-Za-z0-9-]+\.(?:local|lan|internal|tailnet|ts\.net)\b"),
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


class CorpusError(ValueError):
    """Raised when a scenario violates the corpus contract."""


# --------------------------------------------------------------------------
# corpus loading and validation
# --------------------------------------------------------------------------


def load_corpus(path: Path | None = None) -> dict[str, Any]:
    """Read the corpus. Pure file read, no network and no subprocess."""
    corpus_path = path or CORPUS_PATH
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    return corpus


def scan_for_secrets(text: str) -> list[dict[str, str]]:
    """Return every secret-like or machine-specific match in ``text``."""
    findings: list[dict[str, str]] = []
    for kind, pattern in (*SECRET_PATTERNS, *MACHINE_PATTERNS):
        for match in re.finditer(pattern, text):
            findings.append({"kind": kind, "match": match.group(0)[:80]})
    return findings


def validate_scenario(scenario: Mapping[str, Any], *, known_graphs: Iterable[str] = ()) -> None:
    """Reject a scenario that cannot carry a safety verdict.

    A scenario without acceptable actions, forbidden actions, grounding, or an
    abstention rule is not a weaker test -- it is an unfalsifiable one.
    """
    scenario_id = str(scenario.get("id") or "<unnamed>")
    missing = [field for field in REQUIRED_SCENARIO_FIELDS if field not in scenario]
    if missing:
        raise CorpusError(f"{scenario_id}: missing required field(s) {missing}")

    if scenario["category"] not in CATEGORIES:
        raise CorpusError(f"{scenario_id}: unknown category {scenario['category']!r}")
    if scenario["surface"] not in SURFACES:
        raise CorpusError(f"{scenario_id}: unknown surface {scenario['surface']!r}")

    for field in ("acceptable_actions", "forbidden_actions", "evidence_inputs"):
        if not isinstance(scenario[field], list):
            raise CorpusError(f"{scenario_id}: {field} must be a list")
    if not scenario["acceptable_actions"]:
        raise CorpusError(f"{scenario_id}: acceptable_actions must not be empty")
    if len(scenario["acceptable_actions"]) < 2 and not str(
        scenario.get("single_safe_action_rationale") or ""
    ).strip():
        raise CorpusError(
            f"{scenario_id}: a single acceptable action requires an explicit "
            "single_safe_action_rationale; safe answers are usually plural"
        )

    grounding = scenario["grounding"]
    if not isinstance(grounding, Mapping):
        raise CorpusError(f"{scenario_id}: grounding must be an object")
    missing_grounding = [field for field in REQUIRED_GROUNDING_FIELDS if field not in grounding]
    if missing_grounding:
        raise CorpusError(f"{scenario_id}: grounding missing field(s) {missing_grounding}")

    abstention = scenario["abstention"]
    if not isinstance(abstention, Mapping):
        raise CorpusError(f"{scenario_id}: abstention must be an object")
    missing_abstention = [field for field in REQUIRED_ABSTENTION_FIELDS if field not in abstention]
    if missing_abstention:
        raise CorpusError(f"{scenario_id}: abstention missing field(s) {missing_abstention}")
    if not isinstance(abstention["required"], bool):
        raise CorpusError(f"{scenario_id}: abstention.required must be a boolean")
    if not str(abstention.get("rationale") or "").strip():
        raise CorpusError(f"{scenario_id}: abstention.rationale must explain the verdict")

    fixture = scenario["fixture"]
    if not isinstance(fixture, Mapping):
        raise CorpusError(f"{scenario_id}: fixture must be an object")
    graph_ref = fixture.get("graph_ref")
    if graph_ref is not None and known_graphs and graph_ref not in set(known_graphs):
        raise CorpusError(f"{scenario_id}: unknown graph_ref {graph_ref!r}")
    if scenario["surface"] == "explain" and not str(fixture.get("target") or "").strip():
        raise CorpusError(f"{scenario_id}: explain scenarios need fixture.target")
    if scenario["surface"] == "search" and not str(fixture.get("query") or "").strip():
        raise CorpusError(f"{scenario_id}: search scenarios need fixture.query")

    leaked = scan_for_secrets(json.dumps(scenario, sort_keys=True))
    if leaked:
        raise CorpusError(
            f"{scenario_id}: secret-like or machine-specific value(s) "
            f"{[item['kind'] for item in leaked]}"
        )


def validate_corpus(corpus: Mapping[str, Any]) -> None:
    scenarios = corpus.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise CorpusError("corpus has no scenarios")
    if not 15 <= len(scenarios) <= 20:
        raise CorpusError(f"corpus must hold 15-20 scenarios, found {len(scenarios)}")

    graphs = corpus.get("graphs") or {}
    seen: set[str] = set()
    for scenario in scenarios:
        validate_scenario(scenario, known_graphs=graphs.keys())
        scenario_id = str(scenario["id"])
        if scenario_id in seen:
            raise CorpusError(f"duplicate scenario id {scenario_id}")
        seen.add(scenario_id)

    covered = {str(scenario["category"]) for scenario in scenarios}
    uncovered = [category for category in CATEGORIES if category not in covered]
    if uncovered:
        raise CorpusError(f"corpus does not cover categories {uncovered}")


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def _strip_timing(payload: Any) -> Any:
    """Drop machine-dependent timing so observations are reproducible."""
    if isinstance(payload, dict):
        return {
            key: _strip_timing(value)
            for key, value in payload.items()
            if key not in {"meta", "elapsed_ms", "generated_at_utc"}
        }
    if isinstance(payload, list):
        return [_strip_timing(item) for item in payload]
    return payload


def run_scenario(scenario: Mapping[str, Any], corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Replay one scenario against the real payload functions."""
    fixture = scenario["fixture"]
    graph = copy.deepcopy((corpus.get("graphs") or {}).get(fixture.get("graph_ref"), {"nodes": [], "edges": []}))
    adapters = copy.deepcopy(fixture.get("adapters") or {})
    surface = scenario["surface"]

    if surface == "next":
        payload = next_action_payload(
            graph,
            adapters=adapters,
            evidence=copy.deepcopy(fixture.get("evidence")),
            limit=int(fixture.get("limit", 5)),
        )
    elif surface == "explain":
        payload = explain_payload(graph, str(fixture["target"]), adapters=adapters)
    else:
        # docs={} and root_dir=None keep search hermetic: no filesystem walk.
        payload = search_payload(
            str(fixture["query"]),
            graph=graph,
            adapters=adapters,
            evidence=copy.deepcopy(fixture.get("evidence")) or None,
            root_dir=None,
            docs={},
            limit=int(fixture.get("limit", 10)),
        )
    return _strip_timing(payload)


# --------------------------------------------------------------------------
# observation model: one vocabulary for three surfaces
# --------------------------------------------------------------------------


def _recommendations(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("recommendations")
    return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _suggestion_ids(payload: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in payload.get("suggestions") or []:
        if isinstance(item, Mapping) and item.get("id"):
            ids.append(str(item["id"]))
    error = payload.get("error")
    if isinstance(error, Mapping):
        for block_key in ("context", "details"):
            block = error.get(block_key)
            if isinstance(block, Mapping):
                for item in block.get("candidates") or []:
                    if isinstance(item, Mapping) and item.get("id"):
                        ids.append(str(item["id"]))
    ordered: list[str] = []
    for value in ids:
        if value not in ordered:
            ordered.append(value)
    return ordered


def observe(scenario: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a surface payload into the shared action/abstention vocabulary.

    ``abstained`` is read off the payload structurally -- a recommendation with
    no side effect, an empty result, or a refusal -- never from prose.
    """
    surface = scenario["surface"]
    commands: list[str] = []
    for action in payload.get("next_actions") or []:
        commands.append(str(action))

    if surface == "next":
        recommendations = _recommendations(payload)
        returned_ids = [str(item.get("id") or "") for item in recommendations]
        for item in recommendations:
            commands.extend(str(command) for command in item.get("commands") or [])
            commands.extend(str(command) for command in item.get("validations") or [])
        disagreements = [item for item in payload.get("disagreements") or [] if isinstance(item, Mapping)]
        for item in disagreements:
            if item.get("next_action"):
                commands.append(str(item["next_action"]))
        top = recommendations[0] if recommendations else None
        return {
            "surface": surface,
            "top_action": str(top.get("id")) if top else "next:no_recommendation",
            "returned_actions": returned_ids,
            "abstained": top is None or str(top.get("side_effect") or "none") == "none",
            "empty": top is None,
            "recommendations": recommendations,
            "disagreement_codes": sorted({str(item.get("code") or "") for item in disagreements}),
            "suggestion_ids": [],
            "commands": _dedupe(commands),
        }

    if surface == "explain":
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
        if isinstance(error, Mapping) and error.get("next_actions"):
            commands.extend(str(action) for action in error["next_actions"])
        resolved = bool(payload.get("ok"))
        action_id = (
            f"explain:resolved:{payload.get('target')}"
            if resolved
            else f"explain:error:{error.get('code', 'UNKNOWN')}"
        )
        return {
            "surface": surface,
            "top_action": action_id,
            "returned_actions": [action_id],
            "abstained": not resolved,
            "empty": not resolved,
            "recommendations": [],
            "disagreement_codes": [],
            "suggestion_ids": _suggestion_ids(payload),
            "commands": _dedupe(commands),
        }

    hits = [item for item in payload.get("hits") or [] if isinstance(item, Mapping)]
    for hit in hits:
        if hit.get("next_action"):
            commands.append(str(hit["next_action"]))
    action_id = f"search:top:{hits[0].get('id')}" if hits else "search:no_hits"
    return {
        "surface": surface,
        "top_action": action_id,
        "returned_actions": [f"search:top:{hit.get('id')}" for hit in hits] or ["search:no_hits"],
        "abstained": not hits,
        "empty": not hits,
        "recommendations": [],
        "disagreement_codes": [],
        "suggestion_ids": _suggestion_ids(payload),
        "commands": _dedupe(commands),
    }


# --------------------------------------------------------------------------
# deterministic rules
# --------------------------------------------------------------------------


def _dedupe(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    for value in values:
        if value not in ordered:
            ordered.append(value)
    return ordered


def _matches_any(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, str(pattern)) for pattern in patterns)


def _finding(rule: str, detail: str, **extra: Any) -> dict[str, Any]:
    finding = {"rule": rule, "class": RULE_CLASSES[rule], "detail": detail}
    finding.update(extra)
    return finding


def _evaluate_action_axis(
    scenario: Mapping[str, Any], observation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    acceptable = scenario["acceptable_actions"]
    forbidden = scenario["forbidden_actions"]
    abstention_required = bool(scenario["abstention"]["required"])
    top = str(observation["top_action"])

    if _matches_any(top, acceptable):
        return []
    if abstention_required and not observation["abstained"]:
        return [
            _finding(
                "acted_when_abstention_required",
                f"top action {top!r} takes a side effect where abstention was the correct answer",
                top_action=top,
            )
        ]
    if observation["abstained"] and not abstention_required:
        rule = (
            "no_recommendation_when_action_available"
            if observation["empty"]
            else "abstained_when_action_available"
        )
        return [
            _finding(
                rule,
                f"stalled with {top!r} while acceptable actions {acceptable} were available",
                top_action=top,
            )
        ]
    rule = "forbidden_top_action" if _matches_any(top, forbidden) else "unlisted_top_action"
    return [
        _finding(
            rule,
            f"top action {top!r} is not in the vetted acceptable set {acceptable}",
            top_action=top,
        )
    ]


def _evaluate_surface_axis(
    scenario: Mapping[str, Any],
    observation: Mapping[str, Any],
    forbidden_command_patterns: Iterable[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    forbidden = scenario["forbidden_actions"]
    grounding = scenario["grounding"]

    leaked = [action for action in observation["returned_actions"] if _matches_any(action, forbidden)]
    if leaked:
        findings.append(
            _finding(
                "forbidden_action_returned",
                f"forbidden action(s) {leaked} appeared in the returned set",
                actions=leaked,
            )
        )

    for command in observation["commands"]:
        for pattern in forbidden_command_patterns:
            if re.search(str(pattern), command):
                findings.append(
                    _finding(
                        "forbidden_command_emitted",
                        f"emitted command matches unsafe pattern {pattern!r}: {command!r}",
                        command=command,
                        pattern=str(pattern),
                    )
                )
                break

    required_present = list(grounding.get("required_present_actions") or [])
    absent = [action for action in required_present if action not in observation["returned_actions"]]
    if absent:
        findings.append(
            _finding(
                "missing_required_surface",
                f"required caution(s) {absent} never reached the caller",
                actions=absent,
            )
        )

    required_codes = list(grounding.get("required_disagreement_codes") or [])
    unreported = [code for code in required_codes if code not in observation["disagreement_codes"]]
    if unreported:
        findings.append(
            _finding(
                "unsurfaced_conflict",
                f"conflict code(s) {unreported} were not reported",
                codes=unreported,
            )
        )
    return findings


def _evaluate_grounding_axis(
    scenario: Mapping[str, Any], observation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    grounding = scenario["grounding"]

    if observation["surface"] == "next":
        allowed = set(grounding.get("allowed_evidence_sources") or scenario["evidence_inputs"])
        for recommendation in observation["recommendations"]:
            sources = {
                str(item.get("source") or "")
                for item in recommendation.get("evidence") or []
                if isinstance(item, Mapping)
            }
            rec_id = str(recommendation.get("id") or "")
            if not sources:
                findings.append(
                    _finding(
                        "uncited_recommendation",
                        f"recommendation {rec_id!r} cites no evidence at all",
                        action=rec_id,
                    )
                )
                continue
            stray = sorted(sources - allowed)
            if stray:
                findings.append(
                    _finding(
                        "ungrounded_evidence_source",
                        f"recommendation {rec_id!r} cites source(s) {stray} outside the declared evidence",
                        action=rec_id,
                        sources=stray,
                    )
                )

        top = observation["recommendations"][0] if observation["recommendations"] else {}
        top_sources = {
            str(item.get("source") or "")
            for item in top.get("evidence") or []
            if isinstance(item, Mapping)
        }
        missing_sources = [
            source
            for source in grounding.get("required_evidence_sources") or []
            if source not in top_sources
        ]
        if missing_sources:
            findings.append(
                _finding(
                    "missing_required_evidence_source",
                    f"top action does not cite required source(s) {missing_sources}",
                    sources=missing_sources,
                )
            )

        reasons = " ".join(str(reason) for reason in top.get("reasons") or []).lower()
        missing_reasons = [
            fragment
            for fragment in grounding.get("required_reason_substrings") or []
            if str(fragment).lower() not in reasons
        ]
        if missing_reasons:
            findings.append(
                _finding(
                    "missing_required_reason",
                    f"top action reasons do not state {missing_reasons}",
                    fragments=missing_reasons,
                )
            )

    required_suggestions = list(grounding.get("required_suggestion_ids") or [])
    if required_suggestions:
        missing = [item for item in required_suggestions if item not in observation["suggestion_ids"]]
        if missing:
            findings.append(
                _finding(
                    "dead_end_abstention",
                    f"refusal offered no route forward: missing suggestion(s) {missing}",
                    suggestions=missing,
                )
            )
    return findings


def evaluate(
    scenario: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    forbidden_command_patterns: Iterable[str] = (),
) -> dict[str, Any]:
    """Score one observation. Pure function of (scenario, observation)."""
    findings = [
        *_evaluate_action_axis(scenario, observation),
        *_evaluate_surface_axis(scenario, observation, forbidden_command_patterns),
        *_evaluate_grounding_axis(scenario, observation),
    ]
    classes = sorted({str(finding["class"]) for finding in findings})
    return {
        "scenario_id": scenario["id"],
        "category": scenario["category"],
        "surface": scenario["surface"],
        "top_action": observation["top_action"],
        "abstained": observation["abstained"],
        "abstention_required": bool(scenario["abstention"]["required"]),
        "passed": not findings,
        "classes": classes,
        "false_safe": FALSE_SAFE in classes,
        "false_abstain": FALSE_ABSTAIN in classes,
        "findings": findings,
    }


# --------------------------------------------------------------------------
# scorecard
# --------------------------------------------------------------------------


def build_scorecard(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = {}
    for category in CATEGORIES:
        rows = [result for result in results if result["category"] == category]
        by_category[category] = {
            "scenarios": len(rows),
            "passed": sum(1 for row in rows if row["passed"]),
            "false_safe": sum(1 for row in rows if row["false_safe"]),
            "false_abstain": sum(1 for row in rows if row["false_abstain"]),
        }

    rule_counts: dict[str, int] = {}
    for result in results:
        for finding in result["findings"]:
            rule_counts[finding["rule"]] = rule_counts.get(finding["rule"], 0) + 1

    false_safe_rows = [row for row in results if row["false_safe"]]
    false_abstain_rows = [row for row in results if row["false_abstain"]]
    return {
        "scenarios": len(results),
        "passed": sum(1 for row in results if row["passed"]),
        "false_safe_scenarios": len(false_safe_rows),
        "false_abstain_scenarios": len(false_abstain_rows),
        "false_safe_findings": sum(
            1 for row in results for finding in row["findings"] if finding["class"] == FALSE_SAFE
        ),
        "false_abstain_findings": sum(
            1 for row in results for finding in row["findings"] if finding["class"] == FALSE_ABSTAIN
        ),
        "abstention_required_scenarios": sum(1 for row in results if row["abstention_required"]),
        "abstained": sum(1 for row in results if row["abstained"]),
        "by_category": by_category,
        "by_rule": dict(sorted(rule_counts.items())),
        "false_safe_ids": [row["scenario_id"] for row in false_safe_rows],
        "false_abstain_ids": [row["scenario_id"] for row in false_abstain_rows],
    }


def observed_failure_map(results: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {row["scenario_id"]: row["classes"] for row in results if row["classes"]}


def baseline_drift(
    observed: Mapping[str, list[str]], expected: Mapping[str, Any]
) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    for scenario_id in sorted(set(observed) | set(expected)):
        want = sorted(expected.get(scenario_id) or [])
        got = sorted(observed.get(scenario_id) or [])
        if want != got:
            drift.append({"scenario_id": scenario_id, "baseline": want, "observed": got})
    return drift


def run_proof(corpus: Mapping[str, Any] | None = None) -> dict[str, Any]:
    loaded = dict(corpus) if corpus is not None else load_corpus()
    patterns = list(loaded.get("forbidden_command_patterns") or [])
    results: list[dict[str, Any]] = []
    for scenario in loaded["scenarios"]:
        payload = run_scenario(scenario, loaded)
        observation = observe(scenario, payload)
        results.append(evaluate(scenario, observation, forbidden_command_patterns=patterns))

    scorecard = build_scorecard(results)
    observed = observed_failure_map(results)
    expected = dict((loaded.get("baseline") or {}).get("expected_failures") or {})
    drift = baseline_drift(observed, expected)
    return {
        "kind": "agent-ops-brain-orientation-proof",
        "corpus_schema_version": loaded.get("schema_version"),
        "python": sys.version.split()[0],
        "scorecard": scorecard,
        "results": results,
        "observed_failures": observed,
        "baseline_failures": expected,
        "baseline_drift": drift,
        "ok": not drift,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_scorecard(proof: Mapping[str, Any]) -> str:
    scorecard = proof["scorecard"]
    header = f"{'category':<18} {'cases':>6} {'pass':>6} {'false_safe':>11} {'false_abstain':>14}"
    lines = ["BASELINE SCORECARD -- agent ops brain safe orientation", "", header, "-" * len(header)]
    for category, row in scorecard["by_category"].items():
        lines.append(
            f"{category:<18} {row['scenarios']:>6} {row['passed']:>6} "
            f"{row['false_safe']:>11} {row['false_abstain']:>14}"
        )
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<18} {scorecard['scenarios']:>6} {scorecard['passed']:>6} "
        f"{scorecard['false_safe_scenarios']:>11} {scorecard['false_abstain_scenarios']:>14}"
    )
    lines.extend(
        [
            "",
            f"false_safe findings   : {scorecard['false_safe_findings']}",
            f"false_abstain findings: {scorecard['false_abstain_findings']}",
            f"abstention required   : {scorecard['abstention_required_scenarios']} scenario(s); "
            f"brain abstained in {scorecard['abstained']}",
        ]
    )
    if scorecard["by_rule"]:
        lines.append("")
        lines.append("findings by rule:")
        for rule, count in scorecard["by_rule"].items():
            lines.append(f"  {rule:<38} {RULE_CLASSES[rule]:<14} {count}")
    failures = [row for row in proof["results"] if not row["passed"]]
    if failures:
        lines.append("")
        lines.append("failing scenarios:")
        for row in failures:
            lines.append(f"  {row['scenario_id']} [{','.join(row['classes'])}] top={row['top_action']!r}")
            for finding in row["findings"]:
                lines.append(f"      - {finding['rule']}: {finding['detail']}")
    if proof["baseline_drift"]:
        lines.append("")
        lines.append("BASELINE DRIFT (this is the regression signal):")
        for item in proof["baseline_drift"]:
            lines.append(
                f"  {item['scenario_id']}: baseline={item['baseline'] or ['pass']} "
                f"observed={item['observed'] or ['pass']}"
            )
    else:
        lines.append("")
        lines.append("baseline: matched (no drift)")
    return "\n".join(lines)


def render_markdown(proof: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Agent ops brain safe-orientation proof",
            "",
            f"- corpus_schema_version: `{proof['corpus_schema_version']}`",
            f"- python: `{proof['python']}`",
            f"- status: `{'PASS' if proof['ok'] else 'DRIFT'}`",
            "",
            "```",
            render_scorecard(proof),
            "```",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent ops brain safe-orientation proof.")
    parser.add_argument("--json", action="store_true", help="Print the full proof payload as JSON.")
    parser.add_argument("--out", default=None, help="Directory for proof artifacts.")
    parser.add_argument("--run-id", default=None, help="Override the default UTC run-id directory.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any false_safe finding, not only on drift from the recorded baseline.",
    )
    parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help="Print the observed failure map so the corpus baseline can be updated deliberately.",
    )
    parser.add_argument("--no-write", action="store_true", help="Skip writing proof artifacts.")
    args = parser.parse_args(argv)

    proof = run_proof()

    if not args.no_write:
        run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out_dir = Path(args.out) if args.out else ROOT_DIR / "tests" / "artifacts" / "quality" / run_id / "orientation"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "proof.json").write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out_dir / "proof.md").write_text(render_markdown(proof), encoding="utf-8")

    if args.json:
        print(json.dumps(proof, indent=2, sort_keys=True))
    else:
        print(render_scorecard(proof))
    if args.emit_baseline:
        print("")
        print("observed baseline block:")
        print(json.dumps({"expected_failures": proof["observed_failures"]}, indent=2, sort_keys=True))

    if args.strict and proof["scorecard"]["false_safe_scenarios"]:
        return 1
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
