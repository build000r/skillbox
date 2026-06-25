"""Golden tests for ``sbp explain <skill>`` skill-visibility provenance.

Exercises :func:`runtime_manager.skill_visibility.explain_skill_visibility`
plus the CLI routing in :func:`runtime_manager.cli._handle_explain`. Every case
is built on the reproducible ``fixture_fleet`` estate (two homes, mini source
roots, a layered ``skill-scope.yaml`` with an overlay-gated rule, and four fake
repos exhibiting healthy / cross-machine / dangling / overlay-gated links) so
the provenance answer is provable in one ``pytest`` run, never against the live
operator estate.

The five required cases:

* ``visible``               -- linked and effective at the cwd,
* ``invisible_activatable``  -- a source exists and can be durably turned on,
* ``invisible_no_source``    -- no occurrence and no source anywhere,
* ``overlay_gated``          -- only "expected" when an overlay is active,
* ``shadowed``               -- a lower-layer occurrence loses to a higher one.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402

GOLDEN = json.loads(
    (ROOT_DIR / "tests" / "goldens" / "explain_skill_provenance.json").read_text()
)
CASES = GOLDEN["cases"]


def _explain(fleet, skill: str, repo: str) -> dict:
    with fleet._home_patched():
        return sv.explain_skill_visibility(fleet.model(), skill, cwd=str(fleet.repo(repo)))


def _remediation_kinds(payload: dict) -> list[str]:
    return [step["kind"] for step in payload.get("remediation") or []]


def _assert_single_winning_layer(payload: dict) -> None:
    winners = [layer for layer in payload.get("layers") or [] if layer.get("wins")]
    if payload.get("winner") is None:
        assert winners == []
        assert payload.get("winning_layer") is None
        return
    assert len(winners) == 1
    assert winners[0]["layer"] == payload["winning_layer"]
    assert winners[0]["layer"] == payload["winner"]["layer"]


# --------------------------------------------------------------------------
# Case 1: visible
# --------------------------------------------------------------------------

def test_visible_skill_reports_winning_layer(fixture_fleet):
    case = CASES["visible"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["schema_version"] == GOLDEN["schema_version"]
    assert payload["visible"] is True
    assert payload["layer_family"] == case["layer_family"]
    assert case["reason_contains"] in payload["reason"]
    assert _remediation_kinds(payload) == case["remediation_kinds"]
    # The winner is flagged exactly once across occurrences.
    assert sum(1 for occ in payload["occurrences"] if occ.get("won")) == 1
    assert payload["winner"] is not None
    assert payload["winner"]["won"] is True
    assert payload["winner"]["wins"] is True
    _assert_single_winning_layer(payload)
    assert payload["next_actions"] == ["already visible; no action needed"]


# --------------------------------------------------------------------------
# Case 1b: repo-local pin_on override
# --------------------------------------------------------------------------

def test_pinned_on_local_skill_reports_override_winning_layer(fixture_fleet):
    case = CASES["pinned_on_local"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is case["visible"]
    assert payload["layer"] == case["layer"]
    assert payload["winning_layer"] == case["layer"]
    assert payload["layer_family"] == case["layer_family"]
    assert payload["winner"]["state"] == case["winner_state"]
    assert payload["winner"]["override_action"] == "pin_on"
    _assert_single_winning_layer(payload)


# --------------------------------------------------------------------------
# Case 1c: repo-local opt_out_global override
# --------------------------------------------------------------------------

def test_opt_out_global_skill_reports_disabled_override_layer(fixture_fleet):
    case = CASES["opt_out_global"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is case["visible"]
    assert payload["layer"] == case["layer"]
    assert payload["winning_layer"] == case["layer"]
    assert payload["layer_family"] == case["layer_family"]
    assert payload["winner"]["state"] == case["winner_state"]
    assert payload["winner"]["override_action"] == "opt_out_global"
    assert case["reason_contains"] in payload["reason"]
    _assert_single_winning_layer(payload)


# --------------------------------------------------------------------------
# Case 2: invisible but activatable
# --------------------------------------------------------------------------

def test_invisible_activatable_skill_ranks_activate_first(fixture_fleet):
    case = CASES["invisible_activatable"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is False
    assert payload["layer_family"] is None
    assert _remediation_kinds(payload) == case["remediation_kinds"]
    assert payload["remediation"][0]["kind"] == case["top_remediation_kind"]
    assert case["reason_contains"] in payload["reason"]
    assert bool(payload["source_options"]) is case["has_source_options"]
    # The durable command is exact and cwd-scoped by shell $PWD, while the
    # resolved command records the absolute cwd for machine consumers.
    turn_on = payload["remediation"][0]
    assert turn_on["command"] == f"sbp skill on {case['skill']} --cwd $PWD"
    assert turn_on["resolved_command"] == f"sbp skill on {case['skill']} --cwd {payload['cwd']}"
    assert payload["next_actions"][0] == turn_on["command"]
    _assert_single_winning_layer(payload)


# --------------------------------------------------------------------------
# Case 3: invisible, no source anywhere
# --------------------------------------------------------------------------

def test_invisible_no_source_skill_recommends_source_restore(fixture_fleet):
    case = CASES["invisible_no_source"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is False
    assert payload["layer_family"] is None
    assert _remediation_kinds(payload) == case["remediation_kinds"]
    assert case["reason_contains"] in payload["reason"]
    assert bool(payload["source_options"]) is case["has_source_options"]
    assert payload["occurrences"] == []
    # No activate path because there is nothing to link.
    assert "activate" not in _remediation_kinds(payload)
    _assert_single_winning_layer(payload)


# --------------------------------------------------------------------------
# Case 4: overlay-gated (invisible until the overlay is flipped on)
# --------------------------------------------------------------------------

def test_overlay_gated_skill_surfaces_overlay_flip(fixture_fleet, monkeypatch):
    case = CASES["overlay_gated"]
    monkeypatch.delenv(sv.OVERLAY_ENV_VAR, raising=False)
    # Start from the no-link state so the overlay gate (not an existing link) is
    # what controls visibility.
    link = fixture_fleet.repo(case["repo"]) / ".claude" / "skills" / case["skill"]
    if link.is_symlink() or link.exists():
        link.unlink()

    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is False
    kinds = _remediation_kinds(payload)
    assert set(case["remediation_kinds"]) <= set(kinds)
    # The overlay-gated rule is filtered from the live scope_rules (overlay off)
    # but surfaced as an inactive_overlay_rule so the fix is discoverable.
    assert payload["scope_rules"] == []
    inactive = payload["inactive_overlay_rules"]
    assert any(rule["id"] == case["inactive_overlay_rule_id"] for rule in inactive)
    overlay_steps = [s for s in payload["remediation"] if s["kind"] == "overlay_flip"]
    assert overlay_steps
    assert overlay_steps[0]["command"] == (
        f"sbp overlay activate {case['overlay_flip_overlay']} --cwd {payload['cwd']}"
    )
    _assert_single_winning_layer(payload)


def test_overlay_gated_rule_appears_in_scope_rules_when_overlay_active(
    fixture_fleet, monkeypatch
):
    case = CASES["overlay_gated"]
    monkeypatch.setenv(sv.OVERLAY_ENV_VAR, case["overlay_flip_overlay"])
    payload = _explain(fixture_fleet, case["skill"], case["repo"])
    # With the overlay active the rule is a live scope rule (no longer filtered).
    assert any(
        rule["id"] == case["inactive_overlay_rule_id"] and rule["overlay"]
        for rule in payload["scope_rules"]
    )


# --------------------------------------------------------------------------
# Case 5: shadowed (lower-layer occurrence loses to a higher one)
# --------------------------------------------------------------------------

def test_shadowed_skill_reports_loser_and_reason(fixture_fleet):
    case = CASES["shadowed"]
    payload = _explain(fixture_fleet, case["skill"], case["repo"])

    assert payload["visible"] is case["visible"]
    assert payload["layer_family"] == case["winner_layer_family"]
    assert payload["winner"]["state"] == case["winner_state"]
    lost_families = [item["layer_family"] for item in payload["lost"]]
    assert lost_families == case["lost_layer_families"]
    assert payload["lost"]
    assert case["lost_reason_contains"] in payload["lost"][0]["lost_reason"]
    _assert_single_winning_layer(payload)


# --------------------------------------------------------------------------
# Forward-compatible fields + scope-rule provenance
# --------------------------------------------------------------------------

def test_payload_carries_forward_compatible_blocks(fixture_fleet):
    payload = _explain(fixture_fleet, "tiny-cli", "healthy")
    # machine + registry blocks are always present (stable-keyed) for future
    # machine-routing and registry-id consumers.
    assert "machine" in payload and isinstance(payload["machine"], dict)
    assert "resolved" in payload["machine"]
    assert payload["registry"] == {"skill_id": None, "registry_ids": []}


def test_scope_rule_carries_id_pattern_and_policy_source(fixture_fleet):
    # tiny-cli has a literal `cli-local` rule pinned to the `healthy` repo.
    payload = _explain(fixture_fleet, "tiny-cli", "healthy")
    rule_ids = {rule["id"] for rule in payload["scope_rules"]}
    assert "cli-local" in rule_ids
    cli_rule = next(r for r in payload["scope_rules"] if r["id"] == "cli-local")
    assert cli_rule["matched_pattern"] == "tiny-cli"
    assert cli_rule["matches_cwd"] is True
    assert cli_rule["policy_path"].endswith("skill-scope.yaml")


# --------------------------------------------------------------------------
# CLI routing: `manage.py explain <skill>` -> skill provenance (JSON + text)
# --------------------------------------------------------------------------

def _run_cli(args: list[str], fleet) -> tuple[int, str]:
    from runtime_manager import cli

    buf = io.StringIO()
    with fleet._home_patched(), mock.patch.object(
        cli, "_filtered_model_for_args", return_value=fleet.model()
    ), redirect_stdout(buf):
        code = cli.main(args)
    return code, buf.getvalue()


def test_cli_explain_skill_json_routes_to_provenance(fixture_fleet):
    code, out = _run_cli(
        ["explain", "tiny-cli", "--cwd", str(fixture_fleet.repo("healthy")), "--format", "json"],
        fixture_fleet,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["skill"] == "tiny-cli"
    assert payload["visible"] is True
    assert payload["schema_version"] == GOLDEN["schema_version"]


def test_cli_explain_invisible_skill_text_lists_ranked_paths(fixture_fleet):
    code, out = _run_cli(
        ["explain", "needs-beads", "--cwd", str(fixture_fleet.repo("overlay-repo")), "--format", "text"],
        fixture_fleet,
    )
    assert code == 0
    assert "NOT VISIBLE" in out
    assert "paths to visibility (ranked)" in out
    assert "skill on needs-beads --cwd $PWD" in out


def test_cli_skill_why_json_routes_to_provenance_without_unknown_fallback(fixture_fleet):
    code, out = _run_cli(
        [
            "skill",
            "why",
            "totally-unknown-skill",
            "--cwd",
            str(fixture_fleet.repo("healthy")),
            "--format",
            "json",
        ],
        fixture_fleet,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["schema_version"] == GOLDEN["schema_version"]
    assert payload["skill"] == "totally-unknown-skill"
    assert payload["visible"] is False
    assert [step["kind"] for step in payload["remediation"]] == ["rule_edit", "source_restore"]


def test_cli_explain_brain_target_still_routes_to_brain(fixture_fleet):
    # A dotted brain id keeps the legacy graph/registry explainer.
    code, out = _run_cli(
        [
            "explain",
            "brain.next",
            "--no-adapters",
            "--cwd",
            str(fixture_fleet.repo("healthy")),
            "--format",
            "json",
        ],
        fixture_fleet,
    )
    payload = json.loads(out)
    # brain explain payloads carry a `kind`, never the skill `schema_version`.
    assert payload.get("schema_version") != GOLDEN["schema_version"]
    assert "kind" in payload or "error" in payload


def test_cli_explain_skill_flag_forces_skill_mode(fixture_fleet):
    # `--skill` forces provenance even for a dotted/ambiguous target.
    code, out = _run_cli(
        [
            "explain",
            "tiny-cli",
            "--skill",
            "--cwd",
            str(fixture_fleet.repo("healthy")),
            "--format",
            "json",
        ],
        fixture_fleet,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["schema_version"] == GOLDEN["schema_version"]
