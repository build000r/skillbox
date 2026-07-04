"""Golden baseline-capture test for the fixture-fleet harness.

This locks the CURRENT behavior of the three skill-visibility surfaces against
an UNCHANGED runtime, using the miniature estate from ``tests/fixture_fleet.py``:

* ``collect_skill_visibility`` (``fleet.run_resolution``)
* ``collect_skill_audit``      (``fleet.run_audit``)
* ``skill_lifecycle_plan`` + ``apply_skill_lifecycle_plan`` (``fleet.apply_plan``)

It is intentionally a *characterization* golden: an unexpected diff against
``tests/goldens/fixture_fleet_visibility.json`` means a visibility surface
moved. Regenerate the golden only when such a change is deliberate.

Run just this surface with::

    python3 -m pytest tests/ -k fixture_fleet -q
"""

from __future__ import annotations

import json
from pathlib import Path



ROOT_DIR = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT_DIR / "tests" / "goldens" / "fixture_fleet_visibility.json"


def _names(items: list[dict]) -> list[str]:
    return sorted({str(item.get("name") or "") for item in items if item.get("name")})


def _load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_fixture_fleet_factory_materializes_full_estate(fixture_fleet) -> None:
    """The factory builds every advertised piece of the miniature estate."""
    fleet = fixture_fleet

    # Two machine profiles + machines.yaml.
    assert fleet.machines_path.is_file()
    assert "mac-like" in fleet.machines_path.read_text()
    assert "devbox-like" in fleet.machines_path.read_text()

    # Two homes with both symlink-home variants.
    assert (fleet.os_home / ".claude" / "skills" / "tiny-ui").is_symlink()  # per-entry
    assert (fleet.managed_home / ".claude" / "skills").is_symlink()  # dir-symlink

    # Source roots + a requires_beads skill + the _shared chain.
    assert fleet.skills_root.is_dir() and fleet.skills_private_root.is_dir()
    assert (fleet.skill("needs-beads") / "SKILL.md").read_text().count("requires_beads") == 1
    shared = fleet.skills_private_root / "_shared"
    assert shared.is_symlink() and shared.resolve().is_dir()

    # registry/repos.yaml + skill-scope.yaml policy.
    assert fleet.registry_path.is_file()
    assert fleet.skill_scope_path.is_file()

    # Aliased repos root (symlink dir) + the four diverse repos.
    assert fleet.aliased_root.is_symlink()
    assert set(fleet.repos) == {"healthy", "other-machine", "dangling", "overlay-repo"}


def test_resolution_surface_matches_golden(fixture_fleet) -> None:
    fleet = fixture_fleet
    golden = _load_golden()["resolution"]

    overlay = fleet.run_resolution(fleet.repo("overlay-repo"))
    assert [r["id"] for r in overlay["matched_scope_rules"]] == golden["overlay-repo"]["matched_scope_rules"]
    assert [c["id"] for c in overlay["matched_project_categories"]] == golden["overlay-repo"]["matched_project_categories"]
    assert _names(overlay["effective"]) == golden["overlay-repo"]["effective"]
    assert _names(overlay["issues"].get("missing_for_cwd", [])) == golden["overlay-repo"]["missing_for_cwd"]
    assert _names(overlay["issues"].get("broken_project", [])) == golden["overlay-repo"]["broken_project"]

    healthy = fleet.run_resolution(fleet.repo("healthy"))
    assert [r["id"] for r in healthy["matched_scope_rules"]] == golden["healthy"]["matched_scope_rules"]
    assert [c["id"] for c in healthy["matched_project_categories"]] == golden["healthy"]["matched_project_categories"]
    assert _names(healthy["effective"]) == golden["healthy"]["effective"]

    # Dangling link surfaces as a broken project skill.
    dangling = fleet.run_resolution(fleet.repo("dangling"))
    assert _names(dangling["issues"].get("broken_project", [])) == golden["dangling"]["broken_project"]

    # Cross-machine link (target under /fake-mac-root) is broken on-box.
    other = fleet.run_resolution(fleet.repo("other-machine"))
    assert _names(other["issues"].get("broken_project", [])) == golden["other-machine"]["broken_project"]


def test_requires_beads_frontmatter_drives_beads_surface(fixture_fleet) -> None:
    fleet = fixture_fleet
    golden = _load_golden()["beads"]

    fleet.apply_plan(
        "activate",
        skill_name=golden["activated_skill"],
        cwd=fleet.repo(golden["repo"]),
        to="project",
        source=fleet.skill(golden["activated_skill"]),
        dry_run=False,
    )
    payload = fleet.run_resolution(fleet.repo(golden["repo"]))
    beads = payload["beads"]
    assert beads["required"] is golden["required"]
    assert _names(beads.get("required_skills", [])) == golden["required_skills"]
    assert beads.get("initialized") is golden["initialized"]


def test_fleet_audit_surface_matches_golden(fixture_fleet) -> None:
    fleet = fixture_fleet
    golden = _load_golden()["audit"]

    audit = fleet.run_audit(cwd=fleet.repo("overlay-repo"), include_clean=True)

    summary = audit["summary"]
    for key, expected in golden["summary"].items():
        assert summary[key] == expected, f"audit summary[{key}] drifted"

    by_name = {Path(repo["path"]).name: repo for repo in audit["repos"]}
    assert set(by_name) == set(golden["repos"])
    for name, expected in golden["repos"].items():
        assert by_name[name].get("categories", []) == expected["categories"], name
        assert by_name[name].get("broken_project", []) == expected["broken_project"], name


def test_apply_plan_links_and_scope_blocks_match_golden(fixture_fleet) -> None:
    fleet = fixture_fleet
    golden = _load_golden()["apply_plan"]

    # cli skill into the cli-category repo links cleanly.
    ok = fleet.apply_plan(
        "add",
        skill_name="tiny-cli",
        cwd=fleet.repo("healthy"),
        to="category",
        categories=["cli"],
        source=fleet.skill("tiny-cli"),
        dry_run=False,
    )
    exp = golden["add_cli_to_cli_repo"]["summary"]
    assert ok["summary"]["actions"] == exp["actions"]
    assert ok["summary"]["link"] == exp["link"]
    assert ok["summary"]["blocked"] == exp["blocked"]
    for surface in ("claude", "codex"):
        link = fleet.repo("healthy") / f".{surface}" / "skills" / "tiny-cli"
        assert link.is_symlink()
        assert link.resolve() == fleet.skill("tiny-cli").resolve()

    # Same skill into a frontend repo is scope-blocked (cli-local rule).
    blocked = fleet.apply_plan(
        "add",
        skill_name="tiny-cli",
        cwd=fleet.repo("overlay-repo"),
        to="project",
        source=fleet.skill("tiny-cli"),
        dry_run=False,
    )
    exp_blocked = golden["add_cli_to_frontend_repo_blocked"]["summary"]
    assert blocked["summary"]["actions"] == exp_blocked["actions"]
    assert blocked["summary"]["link"] == exp_blocked["link"]
    assert blocked["summary"]["blocked"] == exp_blocked["blocked"]
