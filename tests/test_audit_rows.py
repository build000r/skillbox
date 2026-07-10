"""Provenance-completeness contract for every audit/recalibrate issue row.

The bead premise: *an audit row an agent can't act on without re-derivation is
half an audit.* The data (rule id, policy path, broken-link origin, exact fix
command) already exists internally; this suite proves it is no longer dropped at
the three serialization boundaries in ``skill_visibility``:

1. **Issue groups** -- ``collect_skill_visibility(...)["issues"]`` (the per-cwd
   ``sbp skills --issues-only`` payload). EVERY row in EVERY group must carry
   ``type`` / ``rule_id`` / ``policy_path`` / ``origin`` / ``fix_command``.
2. **Fleet rows** -- ``collect_skill_audit(...)`` per-repo and global rows. The
   provenance row lists (``scope_violation_rows``, ``missing_for_cwd_rows``,
   ``broken_*_links``, ``*_global_rows``) must carry the same fields.
3. **Suggestions** -- ``collect_skill_visibility(...)["recommendations"]`` must
   mirror the provenance (``issue_type`` / ``rule_id`` / ``policy_path`` /
   ``origin`` / ``fix_command``) and the overlay-on-vs-activate distinction.

Everything is proved against the reproducible ``fixture_fleet`` estate (a
miniature on-disk skill estate with healthy / cross-machine / dangling /
overlay-gated links), never the live operator estate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402


# The four fields that make a row act-without-re-derivation, plus ``type``.
ROW_PROVENANCE_KEYS = ("type", "rule_id", "policy_path", "origin", "fix_command")

# Issue groups whose rows are act-on-able installed/broken/missing skills. The
# advisory-only group ``shadowed`` is also covered because it now carries the
# fields, but it has no operator-resolvable scope rule.
ISSUE_GROUP_KEYS = (
    "broken_global",
    "broken_project",
    "global_not_allowed",
    "extra_global",
    "shadowed",
    "archive_sources",
    "scope_violations",
    "missing_for_cwd",
)


def _all_rows(payload: dict) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for group, items in (payload.get("issues") or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                rows.append((group, item))
    return rows


def _assert_row_is_actionable(group: str, row: dict) -> None:
    for key in ROW_PROVENANCE_KEYS:
        assert key in row, f"issue row in {group!r} is missing {key!r}: {row}"
    # The row's stamped type must equal the group it lives in.
    assert row["type"] == group, f"row.type {row['type']!r} != group {group!r}"
    # A fix command is never blank for an act-on-able row.
    assert str(row["fix_command"]).strip(), f"{group!r} row has empty fix_command: {row}"


# --- Site 1: issue groups (per-cwd visibility) ------------------------------


def test_every_issue_group_row_carries_full_provenance(fixture_fleet) -> None:
    """No row across any issue group lacks rule provenance or a fix command."""
    fleet = fixture_fleet
    # Resolve every fixture repo so we exercise broken/missing/scope rows.
    saw_any = False
    for repo_name in fleet.repos:
        payload = fleet.run_resolution(fleet.repo(repo_name), include_sources=True)
        rows = _all_rows(payload)
        for group, row in rows:
            saw_any = True
            _assert_row_is_actionable(group, row)
    assert saw_any, "fixture fleet produced no issue rows to assert on"


def test_broken_project_row_carries_origin_and_taxonomy_fix(fixture_fleet) -> None:
    """The dangling-link repo surfaces a broken row classified + healable."""
    fleet = fixture_fleet
    payload = fleet.run_resolution(fleet.repo("dangling"))
    broken = (payload.get("issues") or {}).get("broken_project") or []
    assert broken, "expected a broken project link in the dangling repo"
    row = broken[0]
    assert row["type"] == "broken_project"
    assert row["origin"] in sv.BROKEN_LINK_CLASSES
    # A dangling link prunes; a same-named source would relink. Either way the
    # taxonomy fix_command is concrete, not a placeholder.
    assert row["fix_command"]
    assert row.get("suggested_action") in sv.BROKEN_LINK_ACTIONS.values()


def test_cross_machine_link_classified_as_other_machine(fixture_fleet) -> None:
    """The /fake-mac-root link is an ``other-machine`` migrate, with a fix."""
    fleet = fixture_fleet
    payload = fleet.run_resolution(fleet.repo("other-machine"))
    broken = (payload.get("issues") or {}).get("broken_project") or []
    assert broken
    origins = {row["origin"] for row in broken}
    # On the devbox-like profile this is a cross-machine link; on a profile-less
    # box it degrades to dangling. Either is a valid taxonomy answer -- the row
    # must still carry origin + fix_command.
    assert origins <= set(sv.BROKEN_LINK_CLASSES)
    for row in broken:
        assert row["fix_command"]


# --- Site 2: fleet rows (cross-repo audit) ----------------------------------


def _fleet_provenance_rows(audit: dict) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for repo in audit.get("repos") or []:
        for key in ("scope_violation_rows", "missing_for_cwd_rows", "broken_project_links"):
            for row in repo.get(key) or []:
                rows.append((key, row))
    global_row = audit.get("global") or {}
    for key in (
        "global_not_allowed_rows",
        "extra_global_rows",
        "broken_global_links",
    ):
        for row in global_row.get(key) or []:
            rows.append((key, row))
    return rows


def test_fleet_audit_rows_carry_provenance(fixture_fleet) -> None:
    """Every fleet provenance row carries the four act-on-able fields."""
    fleet = fixture_fleet
    audit = fleet.run_audit(cwd=fleet.repo("overlay-repo"), include_clean=True)
    rows = _fleet_provenance_rows(audit)
    assert rows, "fixture fleet audit produced no provenance rows"
    for key, row in rows:
        for field in ("type", "rule_id", "policy_path", "origin", "fix_command"):
            assert field in row, f"fleet {key} row missing {field!r}: {row}"
        assert str(row["fix_command"]).strip(), f"fleet {key} row empty fix_command: {row}"


def test_fleet_audit_emits_both_names_and_provenance_rows(fixture_fleet) -> None:
    """Back-compat name lists survive alongside the new provenance rows."""
    fleet = fixture_fleet
    audit = fleet.run_audit(cwd=fleet.repo("overlay-repo"), include_clean=True)
    by_name = {Path(repo["path"]).name: repo for repo in audit.get("repos") or []}
    # The dangling repo keeps its compact name list AND the per-link rows.
    dangling = by_name.get("dangling")
    assert dangling is not None
    assert "broken_project" in dangling  # compact names (back-compat)
    assert "broken_project_links" in dangling  # full provenance rows
    for row in dangling["broken_project_links"]:
        assert row["type"] == "broken_project"
        assert row["origin"] in sv.BROKEN_LINK_CLASSES


# --- Site 3: suggestions (recommendations) ----------------------------------


def test_recommendations_mirror_row_provenance(fixture_fleet) -> None:
    """Every recommendation carries issue_type + rule provenance + fix command."""
    fleet = fixture_fleet
    saw_any = False
    for repo_name in fleet.repos:
        payload = fleet.run_resolution(fleet.repo(repo_name), include_sources=True)
        for rec in payload.get("recommendations") or []:
            saw_any = True
            for field in ("issue_type", "rule_id", "policy_path", "origin", "fix_command"):
                assert field in rec, f"recommendation missing {field!r}: {rec}"
            assert str(rec["fix_command"]).strip(), f"recommendation empty fix_command: {rec}"
    assert saw_any, "fixture fleet produced no recommendations to assert on"


def test_missing_for_cwd_hint_uses_durable_on_recovery() -> None:
    """The missing-skill fix command uses the durable `on` recovery.

    Driven directly off a synthetic ``missing_for_cwd`` issue group so the
    overlay-semantics contract is asserted deterministically (no fixture repo in
    the miniature estate currently emits a missing-skill row, since the
    overlay-gated rule is simply not-expected rather than missing when off).
    """
    issues = {
        "missing_for_cwd": [
            {"name": "wiki", "scope_rule": "wiki-local", "scope_policy_path": "/p/skill-scope.yaml"}
        ],
    }
    sv._enrich_issue_rows(issues)
    recs = sv._skill_visibility_recommendations(issues)
    add_recs = [rec for rec in recs if rec.get("action") == "add_project_skill"]
    assert add_recs, "expected an add_project_skill recommendation"
    rec = add_recs[0]
    hint = rec.get("hint") or ""
    # Post-unification semantics: activate = policy-evaluated/ephemeral,
    # on = persisted across sessions.
    assert "overlay activate" in hint
    assert "overlay on" in hint
    assert "ephemeral" in hint.lower() or "policy-evaluated" in hint.lower()
    assert "persist" in hint.lower()
    # The recommendation also carries the row provenance + a fix command.
    assert rec["issue_type"] == "missing_for_cwd"
    assert rec["rule_id"] == "wiki-local"
    assert rec["policy_path"] == "/p/skill-scope.yaml"
    assert rec["fix_command"] == "sbp skill on wiki --cwd $PWD"


# --- direct-unit coverage of the fix-command derivation ---------------------


@pytest.mark.parametrize(
    "issue_type,row,expected_substr",
    [
        ("missing_for_cwd", {"name": "wiki"}, "skill on wiki --cwd $PWD"),
        ("scope_violations", {"name": "wiki", "path": "/r/.claude/skills/wiki"}, "remove wiki --from project"),
        ("global_not_allowed", {"name": "wiki"}, "remove wiki --from global"),
        ("extra_global", {"name": "wiki"}, "remove wiki --from global"),
    ],
)
def test_issue_row_fix_command_derivation(issue_type, row, expected_substr) -> None:
    cmd = sv._issue_row_fix_command(issue_type, row)
    assert expected_substr in cmd, cmd


def test_broken_row_fix_command_prefers_taxonomy_command() -> None:
    """A pre-classified broken row keeps its taxonomy fix_command verbatim."""
    row = {"name": "wiki", "path": "/r/.claude/skills/wiki", "fix_command": "rm /r/.claude/skills/wiki  # prune"}
    assert sv._issue_row_fix_command("broken_project", row) == row["fix_command"]
