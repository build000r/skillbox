"""Claude <-> Codex global skill-surface parity tests.

sbp's core promise is that BOTH agents see the same world. That promise was
historically audited only for MCP servers (``mcp_visibility.collect_mcp_audit``);
the skill surfaces had no parity check, so a managed home where ``.codex/skills``
carried entries ``.claude/skills`` did not (or vice versa) drifted silently.

These tests lock the new skill-parity audit
(``skill_visibility.collect_skill_parity``), its wiring into the audit payloads
(``collect_skill_visibility`` / ``collect_skill_audit`` / the compact payload),
and the DRY-RUN symmetric-layout relink planner
(``relink_global_homes_to_symmetric_layout``). The relink planner must never
mutate the filesystem — that is an operator-reviewed apply step, not something
the audit does autonomously.

The fixture fleet (``tests/fixture_fleet.py``) models BOTH home-link variants the
scanner must tolerate: an OS-style ``os_home`` with a per-entry-symlink
``.claude/skills`` and a ``managed_home`` whose ``.claude/skills`` dir is itself a
symlink. Reusing it keeps the parity tests deterministic and off the operator's
real homes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "tests"))

import runtime_manager.skill_visibility as sv  # noqa: E402
from fixture_fleet import build_fixture_fleet  # noqa: E402


def _seed_skill(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


class SkillParityDiffTests(unittest.TestCase):
    """``collect_skill_parity`` mirrors the MCP parity block shape."""

    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _patch(self, os_home: Path, managed: Path | None = None):
        env = {} if managed is None else {sv.GLOBAL_HOME_ROOT_ENV: str(managed)}
        return (
            mock.patch.object(sv.Path, "home", return_value=os_home),
            mock.patch.dict(os.environ, env, clear=False),
        )

    def test_parity_reports_claude_only_and_codex_only(self) -> None:
        os_home = self._tmp / "os-home"
        claude = os_home / ".claude" / "skills"
        codex = os_home / ".codex" / "skills"
        # shared in both, plus one claude-only and one codex-only.
        _seed_skill(claude, "shared-skill")
        _seed_skill(codex, "shared-skill")
        _seed_skill(claude, "claude-only-skill")
        _seed_skill(codex, "codex-only-skill")

        patch_home, patch_env = self._patch(os_home)
        with patch_home, patch_env:
            os.environ.pop(sv.GLOBAL_HOME_ROOT_ENV, None)
            parity = sv.collect_skill_parity()

        self.assertEqual(parity["claude_only"], ["claude-only-skill"])
        self.assertEqual(parity["codex_only"], ["codex-only-skill"])
        self.assertEqual(parity["shared"], ["shared-skill"])
        self.assertFalse(parity["in_sync"])
        self.assertEqual(parity["summary"]["divergent"], 2)
        # Same field names as the MCP parity block so jq pipelines are uniform.
        for key in ("claude_only", "codex_only", "shared"):
            self.assertIn(key, parity)

    def test_in_sync_when_surfaces_match(self) -> None:
        os_home = self._tmp / "os-home"
        for surface in ("claude", "codex"):
            _seed_skill(os_home / f".{surface}" / "skills", "same-skill")
        patch_home, patch_env = self._patch(os_home)
        with patch_home, patch_env:
            os.environ.pop(sv.GLOBAL_HOME_ROOT_ENV, None)
            parity = sv.collect_skill_parity()
        self.assertEqual(parity["claude_only"], [])
        self.assertEqual(parity["codex_only"], [])
        self.assertTrue(parity["in_sync"])
        self.assertEqual(parity["summary"]["divergent"], 0)

    def test_shared_payload_link_is_ignored_not_drift(self) -> None:
        # `_shared` is the cross-root payload link, not a real skill; it must not
        # show up as claude-only drift even when only one surface carries it.
        os_home = self._tmp / "os-home"
        _seed_skill(os_home / ".claude" / "skills", "_shared")
        _seed_skill(os_home / ".claude" / "skills", "real-skill")
        _seed_skill(os_home / ".codex" / "skills", "real-skill")
        patch_home, patch_env = self._patch(os_home)
        with patch_home, patch_env:
            os.environ.pop(sv.GLOBAL_HOME_ROOT_ENV, None)
            parity = sv.collect_skill_parity()
        self.assertNotIn("_shared", parity["claude_only"])
        self.assertIn("_shared", parity["ignored"])
        self.assertTrue(parity["in_sync"])

    def test_parity_unions_across_os_and_managed_homes(self) -> None:
        # Divergence must be detected across BOTH resolved homes: an entry that
        # exists only in the managed codex surface is codex-only even if the OS
        # home claude surface carries it.
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        _seed_skill(os_home / ".claude" / "skills", "from-os-claude")
        _seed_skill(managed / ".codex" / "skills", "from-managed-codex")
        patch_home, patch_env = self._patch(os_home, managed)
        with patch_home, patch_env:
            parity = sv.collect_skill_parity()
        self.assertIn("from-os-claude", parity["claude_only"])
        self.assertIn("from-managed-codex", parity["codex_only"])

    def test_symlinked_homes_collapse_and_do_not_double_count(self) -> None:
        # OS home and managed home realpath-equivalent -> one surface set, so a
        # skill present in the shared dir is NOT reported as one-sided drift.
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        os_home.mkdir()
        managed.symlink_to(os_home, target_is_directory=True)
        for surface in ("claude", "codex"):
            _seed_skill(os_home / f".{surface}" / "skills", "shared-skill")
        patch_home, patch_env = self._patch(os_home, managed)
        with patch_home, patch_env:
            parity = sv.collect_skill_parity()
        self.assertTrue(parity["in_sync"])
        self.assertEqual(parity["shared"], ["shared-skill"])


class SkillParityWiringTests(unittest.TestCase):
    """The parity block surfaces in the audit payloads agents read."""

    def test_collect_skill_visibility_includes_parity_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fleet = build_fixture_fleet(td)
            with mock.patch.object(sv.Path, "home", return_value=fleet.os_home), \
                 mock.patch.dict(
                     os.environ,
                     {sv.GLOBAL_HOME_ROOT_ENV: str(fleet.managed_home)},
                     clear=False,
                 ):
                payload = sv.collect_skill_visibility(
                    fleet.model(), cwd=str(fleet.repo("healthy")),
                )
                compact = sv.compact_skill_visibility_payload(payload)
        # Full payload carries parity at top level...
        self.assertIn("parity", payload)
        # ...and so does the compact (agent-facing) payload, so
        # `sbp skills --format json | jq '.parity'` works without --full.
        self.assertIn("parity", compact)
        self.assertEqual(payload["parity"], compact["parity"])
        # Fixture: tiny-ui (os claude) + tiny-cli (managed claude) are claude-only.
        self.assertEqual(sorted(payload["parity"]["claude_only"]), ["tiny-cli", "tiny-ui"])
        self.assertEqual(payload["parity"]["codex_only"], [])
        self.assertEqual(
            payload["summary"]["parity_divergent"],
            payload["parity"]["summary"]["divergent"],
        )

    def test_parity_absent_when_global_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fleet = build_fixture_fleet(td)
            with mock.patch.object(sv.Path, "home", return_value=fleet.os_home):
                payload = sv.collect_skill_visibility(
                    fleet.model(),
                    cwd=str(fleet.repo("healthy")),
                    include_global=False,
                )
        self.assertEqual(payload["parity"], {})

    def test_collect_skill_audit_includes_parity_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fleet = build_fixture_fleet(td)
            with mock.patch.object(sv.Path, "home", return_value=fleet.os_home), \
                 mock.patch.dict(
                     os.environ,
                     {sv.GLOBAL_HOME_ROOT_ENV: str(fleet.managed_home)},
                     clear=False,
                 ):
                audit = sv.collect_skill_audit(
                    fleet.model(), scan_roots=[str(fleet.aliased_root)],
                )
        self.assertIn("parity", audit)
        self.assertEqual(sorted(audit["parity"]["claude_only"]), ["tiny-cli", "tiny-ui"])
        self.assertEqual(
            audit["summary"]["parity_divergent"],
            audit["parity"]["summary"]["divergent"],
        )

    def test_parity_next_actions_name_divergent_skills_and_point_at_dry_run(self) -> None:
        parity = {"claude_only": ["a"], "codex_only": ["b"]}
        actions = sv.skill_parity_next_actions(parity)
        joined = " ".join(actions)
        self.assertIn("a", joined)
        self.assertIn("b", joined)
        self.assertTrue(any("DRY RUN" in action for action in actions))
        # In-sync surfaces produce no parity chatter.
        self.assertEqual(sv.skill_parity_next_actions({"claude_only": [], "codex_only": []}), [])


class SymmetricRelinkDryRunTests(unittest.TestCase):
    """The relink planner describes work but NEVER mutates live homes."""

    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_dry_run_plans_relink_for_divergent_per_entry_surface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fleet = build_fixture_fleet(td)
            with mock.patch.object(sv.Path, "home", return_value=fleet.os_home), \
                 mock.patch.dict(
                     os.environ,
                     {sv.GLOBAL_HOME_ROOT_ENV: str(fleet.managed_home)},
                     clear=False,
                 ):
                plan = sv.relink_global_homes_to_symmetric_layout()
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["target_layout"], sv.SKILL_HOME_CANONICAL_LAYOUT)
        # The os-home per-entry .claude/skills is targeted for relink to managed.
        os_claude = str(fleet.os_home / ".claude" / "skills")
        relinked = {a["link"]: a for a in plan["actions"]}
        self.assertIn(os_claude, relinked)
        self.assertEqual(relinked[os_claude]["current_layout"], "per-entry")
        self.assertEqual(
            relinked[os_claude]["would_point_to"],
            str(fleet.managed_home / ".claude" / "skills"),
        )

    def test_dry_run_does_not_mutate_the_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fleet = build_fixture_fleet(td)
            os_claude = fleet.os_home / ".claude" / "skills"
            # Snapshot: a real per-entry dir (NOT a symlink) before planning.
            self.assertFalse(os_claude.is_symlink())
            before = sorted(p.name for p in os_claude.iterdir())
            with mock.patch.object(sv.Path, "home", return_value=fleet.os_home), \
                 mock.patch.dict(
                     os.environ,
                     {sv.GLOBAL_HOME_ROOT_ENV: str(fleet.managed_home)},
                     clear=False,
                 ):
                sv.relink_global_homes_to_symmetric_layout()
            # Still a real dir with identical entries: nothing was relinked.
            self.assertFalse(os_claude.is_symlink())
            self.assertEqual(sorted(p.name for p in os_claude.iterdir()), before)

    def test_already_canonical_dir_symlink_is_left_alone(self) -> None:
        # A surface that is already a dir-symlink into the managed home is the
        # canonical layout; the planner must not propose relinking it.
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        managed_claude = managed / ".claude" / "skills"
        managed_claude.mkdir(parents=True)
        (os_home / ".claude").mkdir(parents=True)
        (os_home / ".claude" / "skills").symlink_to(managed_claude, target_is_directory=True)
        with mock.patch.object(sv.Path, "home", return_value=os_home), \
             mock.patch.dict(
                 os.environ, {sv.GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False,
             ):
            plan = sv.relink_global_homes_to_symmetric_layout()
        links = {a["link"] for a in plan["actions"]}
        self.assertNotIn(str(os_home / ".claude" / "skills"), links)

    def test_no_managed_home_blocks_relink_with_explicit_reason(self) -> None:
        os_home = self._tmp / "os-home"
        _seed_skill(os_home / ".claude" / "skills", "tiny")  # per-entry-ish real dir
        with mock.patch.object(sv.Path, "home", return_value=os_home), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sv.GLOBAL_HOME_ROOT_ENV, None)
            plan = sv.relink_global_homes_to_symmetric_layout()
        self.assertIsNone(plan["managed_home"])
        # Real-dir surfaces are flagged but blocked: no shared target to pick.
        blocked = [a for a in plan["actions"] if a["would_point_to"] is None]
        self.assertTrue(blocked)
        self.assertEqual(
            plan["summary"]["blocked_no_managed_home"], len(blocked),
        )

    def test_rejects_unsupported_target_layout(self) -> None:
        with self.assertRaises(RuntimeError):
            sv.relink_global_homes_to_symmetric_layout(target_layout="per-entry-symlink")


if __name__ == "__main__":
    unittest.main()
