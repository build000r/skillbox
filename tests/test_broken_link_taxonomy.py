"""Broken-link triage taxonomy for the skill audit.

A broken installed skill symlink is one of exactly four things, and the audit
must say which so "N broken links" collapses into the ~3 decisions they really
are. This file proves the classifier on
:func:`runtime_manager.skill_visibility.collect_skill_visibility` /
``collect_skill_audit`` using the shared fixture fleet (``tests/fixture_fleet.py``),
which already materializes healthy / other-machine / dangling links.

The four classes and how each is detected:

* ``other-machine`` — target under a root that ``machines.yaml`` maps to a
  DIFFERENT machine profile. Detected via ``runtime_manager.machines``
  (``is_foreign_path``). Action: ``migrate``.
* ``moved`` — a same-named skill still exists under a current
  ``skill_source_root`` (a relink candidate). Action: ``relink``.
* ``dangling`` — no source anywhere; the target is not foreign. Action:
  ``prune``.
* ``unreadable`` — the link itself cannot be read (permission error / symlink
  loop). Action: ``investigate``.

The fixture's ``machines.yaml`` uses an estate-shape schema the canonical
``machines.py`` loader does not parse, so these tests inject a canonical-schema
:class:`MachinesConfig` through the module's ``_machines_classifier_override``
hook. That keeps the cross-machine cases host-independent (no reliance on the
live box identity) exactly like ``tests/test_machines.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
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


def _devbox_config(*, mac_root: Path, devbox_root: Path) -> m.MachinesConfig:
    """Canonical-schema config: ``mac-like`` vs ``devbox-like`` repo roots.

    ``fake-mac-root`` (where the fixture's other-machine link points) becomes the
    mac's repo root; the fixture's real repos tree becomes the devbox root. From
    ``devbox-like`` the mac target is therefore foreign.
    """
    return m.MachinesConfig(
        machines={
            "mac-like": m.MachineProfile(
                machine_id="mac-like",
                hostnames=("mac-like",),
                repo_roots=(str(mac_root),),
            ),
            "devbox-like": m.MachineProfile(
                machine_id="devbox-like",
                hostnames=("devbox-like",),
                repo_roots=(str(devbox_root),),
            ),
        }
    )


@contextmanager
def _inject_machines(config: m.MachinesConfig | None, machine_id: str | None) -> Iterator[None]:
    """Patch the classifier's machines resolution for the duration of a test."""
    sv._machines_classifier_override = lambda: (config, machine_id)  # type: ignore[attr-defined]
    try:
        yield
    finally:
        sv._machines_classifier_override = None  # type: ignore[attr-defined]


class BrokenLinkTaxonomyUnitTests(unittest.TestCase):
    """Direct unit coverage of the classifier on hand-built occurrences."""

    def test_unreadable_wins_even_when_target_would_classify(self) -> None:
        # A resolve error -> unreadable, regardless of where the target points.
        occ = {
            "name": "x",
            "availability": "installed",
            "state": "broken",
            "path": "/repo/.claude/skills/x",
            "link_target": "/Users/b/repos/skills/x",
            "link_target_abs": "/Users/b/repos/skills/x",
            "broken_reason": "unreadable",
        }
        result = sv._classify_broken_link(
            occ, {"clients": [], "skills": []}, machines_config=None, machine_id=None
        )
        self.assertEqual(result["origin"], "unreadable")
        self.assertEqual(result["suggested_action"], "investigate")
        self.assertIn("investigate", result["fix_command"])

    def test_other_machine_beats_moved(self) -> None:
        # Foreign target classifies as other-machine even when a same-named
        # source exists (precedence is deliberate: migration, not a mystery).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mac_root = root / "Users" / "b" / "repos"
            devbox_root = root / "srv" / "repos"
            (mac_root).mkdir(parents=True)
            (devbox_root).mkdir(parents=True)
            config = m.MachinesConfig(
                machines={
                    "mac": m.MachineProfile(machine_id="mac", repo_roots=(str(mac_root),)),
                    "devbox": m.MachineProfile(machine_id="devbox", repo_roots=(str(devbox_root),)),
                }
            )
            target = str(mac_root / "skills" / "x")
            occ = {
                "name": "x",
                "availability": "installed",
                "state": "broken",
                "path": "/repo/.claude/skills/x",
                "link_target": target,
                "link_target_abs": target,
                "broken_reason": "missing-target",
            }
            # _skill_source_options would normally be consulted for moved; force
            # a non-empty result to prove other-machine still wins.
            with mock.patch.object(
                sv, "_skill_source_options", return_value=[{"source": "/some/src/x"}]
            ):
                result = sv._classify_broken_link(
                    occ,
                    {"clients": [], "skills": []},
                    machines_config=config,
                    machine_id="devbox",
                )
            self.assertEqual(result["origin"], "other-machine")
            self.assertEqual(result["suggested_action"], "migrate")
            self.assertIn("rm /repo/.claude/skills/x", result["fix_command"])
            self.assertIn("another machine", result["fix_command"])

    def test_moved_relinks_to_live_source(self) -> None:
        occ = {
            "name": "x",
            "availability": "installed",
            "state": "broken",
            "path": "/repo/.claude/skills/x",
            "link_target": "/old/place/x",
            "link_target_abs": "/old/place/x",
            "broken_reason": "missing-target",
        }
        with mock.patch.object(
            sv, "_skill_source_options", return_value=[{"source": "/new/src/x"}]
        ):
            result = sv._classify_broken_link(
                occ, {"clients": [], "skills": []}, machines_config=None, machine_id=None
            )
        self.assertEqual(result["origin"], "moved")
        self.assertEqual(result["suggested_action"], "relink")
        self.assertEqual(result["fix_command"], "ln -sfn /new/src/x /repo/.claude/skills/x")

    def test_dangling_prunes(self) -> None:
        occ = {
            "name": "ghost",
            "availability": "installed",
            "state": "broken",
            "path": "/repo/.claude/skills/ghost",
            "link_target": "/gone/ghost",
            "link_target_abs": "/gone/ghost",
            "broken_reason": "missing-target",
        }
        with mock.patch.object(sv, "_skill_source_options", return_value=[]):
            result = sv._classify_broken_link(
                occ, {"clients": [], "skills": []}, machines_config=None, machine_id=None
            )
        self.assertEqual(result["origin"], "dangling")
        self.assertEqual(result["suggested_action"], "prune")
        self.assertIn("rm /repo/.claude/skills/ghost", result["fix_command"])

    def test_class_counts_default_unenriched_to_dangling(self) -> None:
        counts = sv.broken_link_class_counts(
            [
                {"origin": "moved"},
                {"origin": "moved"},
                {"origin": "other-machine"},
                {},  # never enriched -> bucketed as dangling, never dropped
            ]
        )
        self.assertEqual(counts["moved"], 2)
        self.assertEqual(counts["other-machine"], 1)
        self.assertEqual(counts["dangling"], 1)
        self.assertEqual(counts["unreadable"], 0)
        self.assertEqual(set(counts), set(sv.BROKEN_LINK_CLASSES))


class BrokenLinkTaxonomyFleetTests(unittest.TestCase):
    """End-to-end taxonomy on the shared fixture fleet's broken links."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = build_fixture_fleet(self._tmp.name)
        self.mac_root = self.fleet.root / "fake-mac-root"
        self.config = _devbox_config(
            mac_root=self.mac_root, devbox_root=self.fleet.repos_real
        )

    def _resolution(self, repo_name: str) -> dict:
        with _inject_machines(self.config, "devbox-like"):
            return self.fleet.run_resolution(self.fleet.repo(repo_name))

    def _broken_items(self, payload: dict) -> list[dict]:
        return list(payload["issues"]["broken_project"])

    def test_other_machine_link_is_classified_as_migrate(self) -> None:
        # The other-machine repo links tiny-ui at /fake-mac-root/... AND tiny-ui
        # has a live source — proving other-machine wins over moved end to end.
        payload = self._resolution("other-machine")
        broken = self._broken_items(payload)
        self.assertEqual(len(broken), 1)
        row = broken[0]
        self.assertEqual(row["name"], "tiny-ui")
        self.assertEqual(row["origin"], "other-machine")
        self.assertEqual(row["suggested_action"], "migrate")
        self.assertTrue(row["fix_command"].startswith("rm "))
        self.assertIn("another machine", row["fix_command"])

    def test_other_machine_falls_back_to_moved_without_machines_config(self) -> None:
        # No machines config -> not foreign -> tiny-ui's live source makes it
        # a moved/relink candidate instead.
        with _inject_machines(None, None):
            payload = self.fleet.run_resolution(self.fleet.repo("other-machine"))
        row = payload["issues"]["broken_project"][0]
        self.assertEqual(row["origin"], "moved")
        self.assertEqual(row["suggested_action"], "relink")
        self.assertTrue(row["fix_command"].startswith("ln -sfn "))
        self.assertIn(str(self.fleet.skill("tiny-ui")), row["fix_command"])

    def test_dangling_link_is_classified_as_prune(self) -> None:
        # The dangling repo links ghost -> a deleted source (no source anywhere).
        payload = self._resolution("dangling")
        broken = self._broken_items(payload)
        self.assertEqual(len(broken), 1)
        row = broken[0]
        self.assertEqual(row["name"], "ghost")
        self.assertEqual(row["origin"], "dangling")
        self.assertEqual(row["suggested_action"], "prune")
        self.assertTrue(row["fix_command"].startswith("rm "))

    def test_unreadable_link_is_classified_as_investigate(self) -> None:
        # A link whose resolution raises (symlink loop) is flagged unreadable by
        # the scanner (broken_reason="unreadable"); the enrichment pass — the
        # production region under test — then classifies it as investigate. We
        # exercise the scanner + enrichment directly to keep the assertion on the
        # taxonomy region rather than unrelated downstream passes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            root.mkdir()
            loop = root / "loopy"
            os.symlink(str(loop), str(loop))  # self-referential -> resolve loops
            occurrences, summary = sv._scan_installed_root(
                root, layer="project:claude:/x", label="project", rank=20
            )
            self.assertEqual(summary["broken_count"], 1)
            broken = [o for o in occurrences if o.get("state") == "broken"]
            self.assertEqual(len(broken), 1)
            self.assertEqual(broken[0]["broken_reason"], "unreadable")

            with _inject_machines(self.config, "devbox-like"):
                sv._enrich_broken_links(self.fleet.model(), occurrences)
            row = broken[0]
            self.assertEqual(row["origin"], "unreadable")
            self.assertEqual(row["suggested_action"], "investigate")
            self.assertIn("investigate", row["fix_command"])

    def test_summary_counts_by_class_present_per_repo(self) -> None:
        payload = self._resolution("dangling")
        counts = payload["summary"]["broken_by_class"]
        self.assertEqual(set(counts), set(sv.BROKEN_LINK_CLASSES))
        self.assertEqual(counts["dangling"], 1)
        self.assertEqual(sum(counts.values()), 1)

    def test_fleet_audit_rows_and_summary_carry_taxonomy(self) -> None:
        with _inject_machines(self.config, "devbox-like"):
            audit = self.fleet.run_audit()

        # Fleet summary has counts-by-class: the other-machine + dangling repos.
        summary = audit["summary"]
        self.assertIn("broken_by_class", summary)
        by_class = summary["broken_by_class"]
        self.assertEqual(set(by_class), set(sv.BROKEN_LINK_CLASSES))
        self.assertEqual(by_class["other-machine"], 1)
        self.assertEqual(by_class["dangling"], 1)
        self.assertEqual(summary["broken_links"], sum(by_class.values()))

        # Each affected repo row carries per-link classified rows + counts.
        rows_by_path = {repo["path"]: repo for repo in audit["repos"]}
        om_repo = rows_by_path[str(self.fleet.repo("other-machine"))]
        links = om_repo["broken_project_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["origin"], "other-machine")
        self.assertEqual(links[0]["suggested_action"], "migrate")
        self.assertEqual(links[0]["fix_command"], om_repo_fix := links[0]["fix_command"])
        self.assertTrue(om_repo_fix.startswith("rm "))
        self.assertEqual(om_repo["broken_project_by_class"]["other-machine"], 1)

        dangling_repo = rows_by_path[str(self.fleet.repo("dangling"))]
        self.assertEqual(dangling_repo["broken_project_links"][0]["origin"], "dangling")
        self.assertEqual(dangling_repo["broken_project_by_class"]["dangling"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
