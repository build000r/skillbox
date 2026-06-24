"""Realpath/alias canonicalization + dedup for the cross-repo fleet audit.

``/srv/repos`` is a symlink alias of ``/srv/skillbox/repos`` on the devbox: the
same tree under two names. Before this canonicalization, the fleet audit could
enumerate a repo TWICE -- once per alias spelling -- inflating the candidate
count and double-reporting the same issue (and a link healthy via one alias
could look foreign via the other). The audit must:

1. Canonicalize every scan root and candidate repo path (folding declared
   aliases to their canonical tree) and dedup by the canonical path BEFORE
   auditing, so each repo is enumerated ONCE.
2. Report each repo under its single canonical ``path`` with an ``aliases``
   array listing every other spelling it answers to.
3. Merge the ``sources`` of the alias spellings onto the one canonical row, so
   an issue surfaced via either alias is reported once, not twice.

Canonicalization uses ``runtime_manager.machines.canonicalize_alias`` (string
prefix, machine-agnostic) -- strictly stronger than ``Path.resolve()`` because
it folds an alias even when the alias symlink is NOT resolvable on the current
box. To keep these tests host-independent (no reliance on the live devbox having
a ``/srv/repos`` symlink) we inject a canonical-schema :class:`MachinesConfig`
through ``skill_visibility._machines_classifier_override`` exactly like
``tests/test_broken_link_taxonomy.py``.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from runtime_manager import machines as m  # noqa: E402
from runtime_manager import skill_visibility as sv  # noqa: E402

from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


@contextmanager
def _inject_machines(config: m.MachinesConfig | None, machine_id: str | None) -> Iterator[None]:
    """Patch the classifier's machines resolution for the duration of a test."""
    sv._machines_classifier_override = lambda: (config, machine_id)  # type: ignore[attr-defined]
    try:
        yield
    finally:
        sv._machines_classifier_override = None  # type: ignore[attr-defined]


def _alias_config(*, alias_root: Path, canonical_root: Path) -> m.MachinesConfig:
    """A config declaring ``alias_root`` is the same tree as ``canonical_root``.

    Mirrors the live devbox machines.yaml: ``/srv/repos`` -> ``/srv/skillbox/repos``.
    """
    return m.MachinesConfig(
        machines={
            "devbox": m.MachineProfile(
                machine_id="devbox",
                hostnames=("devbox",),
                repo_roots=(str(canonical_root),),
            ),
        },
        aliases=(m.MachineAlias(alias=str(alias_root), canonical=str(canonical_root)),),
    )


# --------------------------------------------------------------------------- #
# Unit: the candidate collector folds alias spellings to one canonical row.
# --------------------------------------------------------------------------- #


def test_candidate_collector_folds_unresolvable_alias_to_one_row() -> None:
    """Two alias spellings of one repo collapse to a single canonical candidate.

    Uses paths that DO NOT exist on disk so ``Path.resolve()`` cannot collapse
    them -- proving the fold comes from ``canonicalize_alias`` (string prefix),
    not from live-filesystem resolution. The historic double-count is exactly
    this case: an alias path the box could not resolve.
    """
    config = _alias_config(
        alias_root=Path("/nope/srv/repos"),
        canonical_root=Path("/nope/srv/skillbox/repos"),
    )
    with _inject_machines(config, "devbox"):
        candidates: dict[str, dict] = {}
        # Same repo, two spellings, two different sources.
        sv._skill_audit_candidate_from_path(
            candidates, "/nope/srv/repos/app_core", source="category:app_core"
        )
        sv._skill_audit_candidate_from_path(
            candidates,
            "/nope/srv/skillbox/repos/app_core",
            source="scan_root:/nope/srv/skillbox/repos",
        )

    # ONE canonical row, not two.
    assert list(candidates.keys()) == ["/nope/srv/skillbox/repos/app_core"]
    row = candidates["/nope/srv/skillbox/repos/app_core"]
    assert row["path"] == "/nope/srv/skillbox/repos/app_core"
    # The alias spelling is recorded under the canonical row.
    assert row["aliases"] == ["/nope/srv/repos/app_core"]
    # Sources from BOTH spellings merge onto the one row (issue reported once).
    assert sorted(row["sources"]) == [
        "category:app_core",
        "scan_root:/nope/srv/skillbox/repos",
    ]


def test_candidate_collector_no_alias_leaves_aliases_empty() -> None:
    """A repo named only by its canonical path carries an empty alias array."""
    config = _alias_config(
        alias_root=Path("/nope/srv/repos"),
        canonical_root=Path("/nope/srv/skillbox/repos"),
    )
    with _inject_machines(config, "devbox"):
        candidates: dict[str, dict] = {}
        sv._skill_audit_candidate_from_path(
            candidates,
            "/nope/srv/skillbox/repos/solo",
            source="scan_root:/nope/srv/skillbox/repos",
        )
    row = candidates["/nope/srv/skillbox/repos/solo"]
    assert row["aliases"] == []


# --------------------------------------------------------------------------- #
# Scan-root expansion: dual-alias scan-root list collapses to ONE root.
# --------------------------------------------------------------------------- #


def test_scan_root_expansion_dedups_dual_alias_roots() -> None:
    """``[<alias>, <canonical>]`` scan roots collapse to a single resolved root.

    The live skill-scope.yaml lists BOTH ``/srv/repos`` and
    ``/srv/skillbox/repos`` as ``skill_install_scan_roots``; expanding that pair
    must yield one root so the fleet walk runs once, not twice.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real = root / "repos_real"
        real.mkdir()
        alias = root / "aliased"
        alias.symlink_to(real, target_is_directory=True)
        config = _alias_config(alias_root=alias, canonical_root=real)
        with _inject_machines(config, "devbox"):
            roots = sv._expand_skill_source_patterns([str(alias), str(real)])
        # One root after dedup (both resolve/canonicalize to the real tree).
        assert [str(p) for p in roots] == [str(real)]


# --------------------------------------------------------------------------- #
# End-to-end: the fixture fleet audited via BOTH alias spellings enumerates once.
# --------------------------------------------------------------------------- #


def test_fleet_audit_enumerates_each_repo_once_across_aliases() -> None:
    """The cross-repo audit reports each fixture repo ONCE under its canonical path.

    The fixture fleet already materializes an aliased root (``aliased`` ->
    ``repos_real``) with four repos. Auditing with BOTH the alias root and the
    real root as scan roots must NOT double the candidate count, must report
    every repo under its canonical (``repos_real``) path, and must surface the
    alias spelling in the per-repo ``aliases`` array.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fleet = build_fixture_fleet(Path(tmp))
        config = _alias_config(
            alias_root=fleet.aliased_root, canonical_root=fleet.repos_real
        )
        # Audit via BOTH spellings of the same repos tree.
        both_roots = [str(fleet.aliased_root), str(fleet.repos_real)]
        with _inject_machines(config, "devbox"), mock.patch.object(
            sv.Path, "home", return_value=fleet.os_home
        ):
            payload = sv.collect_skill_audit(
                fleet.model(),
                scan_roots=both_roots,
                include_clean=True,
            )

        repos = payload["repos"]
        paths = [r["path"] for r in repos]

        # Single enumeration: four repos, four UNIQUE canonical paths.
        assert payload["summary"]["candidate_repos"] == 4
        assert len(paths) == 4
        assert len(set(paths)) == 4

        # Every reported repo lives under the canonical (real) tree, never the
        # alias tree -- so a repo is never double-listed under both names.
        for path in paths:
            assert path.startswith(str(fleet.repos_real)), path
            assert not path.startswith(str(fleet.aliased_root) + "/"), path

        # Scan roots in the payload collapsed to ONE (the dual-alias pair deduped).
        assert payload["scan_roots"] == [str(fleet.repos_real)]

        # No double-reported issues: the broken repos appear once each.
        by_name = {Path(r["path"]).name: r for r in repos}
        broken_names = [
            name
            for name, r in by_name.items()
            if r.get("broken_project")
        ]
        assert sorted(broken_names) == ["dangling", "other-machine"]
        # Each row exposes an aliases array (the contract), even if empty.
        for r in repos:
            assert "aliases" in r
            assert isinstance(r["aliases"], list)


def test_fleet_audit_alias_array_populated_for_alias_pinned_repo() -> None:
    """A repo pinned by its ALIAS path is reported once with the alias recorded.

    We add a project-category pin that names a repo via the alias root spelling.
    That repo must collapse onto its canonical row and carry the alias spelling
    in ``aliases`` -- proving the alias-array contract end to end, independent of
    whether the box can resolve the alias symlink.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fleet = build_fixture_fleet(Path(tmp))
        config = _alias_config(
            alias_root=fleet.aliased_root, canonical_root=fleet.repos_real
        )
        # Inject a candidate directly via the alias spelling of the 'healthy' repo,
        # alongside the canonical scan-root discovery of the same repo.
        with _inject_machines(config, "devbox"):
            candidates: dict[str, dict] = {}
            # Discovered by scan-root walk (canonical spelling).
            sv._skill_audit_candidate_from_path(
                candidates,
                str(fleet.repos_real / "healthy"),
                source="scan_root:canonical",
            )
            # Pinned by a category using the ALIAS spelling.
            sv._skill_audit_candidate_from_path(
                candidates,
                str(fleet.aliased_root / "healthy"),
                source="category:cli",
            )

        canonical_path = str(fleet.repos_real / "healthy")
        # ONE row keyed by the canonical path.
        assert list(candidates.keys()) == [canonical_path]
        row = candidates[canonical_path]
        # The alias spelling is recorded.
        assert str(fleet.aliased_root / "healthy") in row["aliases"]
        # Both sources merged (issue reported once).
        assert sorted(row["sources"]) == ["category:cli", "scan_root:canonical"]
