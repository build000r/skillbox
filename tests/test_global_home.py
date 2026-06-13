"""Unit tests for canonical global-home resolution.

These cover ``resolve_global_homes`` / ``_default_global_roots`` /
``global_home_surfaces_report`` (the single resolution that honors
``SKILLBOX_HOME_ROOT``) and the model-defaults install targets in
``shared.managed_home_install_targets``.

The two-homes bug being guarded against: the auditor used to scan only
``Path.home()``, so a MANAGED home's installs were invisible. After
centralization both homes are surfaces, realpath-equivalent dirs collapse to
one, and the audit reports os-only vs managed-only entries per surface.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.shared import (  # noqa: E402
    GLOBAL_HOME_ROOT_ENV,
    GLOBAL_HOME_SURFACES,
    managed_home_install_targets,
)
from runtime_manager.skill_visibility import (  # noqa: E402
    _default_global_roots,
    global_home_surfaces_report,
    resolve_global_homes,
)


def _surface_roots(home: Path) -> dict[str, Path]:
    return {surface: home / f".{surface}" / "skills" for surface in GLOBAL_HOME_SURFACES}


def _seed_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


class ResolveGlobalHomesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def _patch_home(self, home: Path):
        return mock.patch(
            "runtime_manager.skill_visibility.Path.home",
            return_value=home,
        )

    def _clear_env(self):
        return mock.patch.dict(os.environ, {}, clear=False)

    # --- env unset -------------------------------------------------------
    def test_env_unset_yields_only_os_home(self) -> None:
        os_home = self._tmp / "os-home"
        os_home.mkdir()
        with self._patch_home(os_home), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(GLOBAL_HOME_ROOT_ENV, None)
            homes = resolve_global_homes()
        self.assertEqual([h["origin"] for h in homes], ["os-home"])
        self.assertEqual(Path(homes[0]["home"]), os_home)

    def test_env_unset_roots_are_os_home_only(self) -> None:
        os_home = self._tmp / "os-home"
        for root in _surface_roots(os_home).values():
            root.mkdir(parents=True)
        with self._patch_home(os_home), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(GLOBAL_HOME_ROOT_ENV, None)
            roots = _default_global_roots()
        self.assertEqual(
            sorted((s, str(r)) for s, r in roots),
            sorted((s, str(p)) for s, p in _surface_roots(os_home).items()),
        )

    # --- env set, divergent homes ---------------------------------------
    def test_env_set_divergent_includes_both_homes(self) -> None:
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        os_home.mkdir()
        managed.mkdir()
        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            homes = resolve_global_homes()
        self.assertEqual([h["origin"] for h in homes], ["os-home", "managed-home"])
        self.assertEqual(Path(homes[0]["home"]), os_home)
        self.assertEqual(Path(homes[1]["home"]), managed)

    def test_env_set_divergent_roots_span_both_homes(self) -> None:
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        for home in (os_home, managed):
            for root in _surface_roots(home).values():
                root.mkdir(parents=True)
        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            roots = {str(r) for _s, r in _default_global_roots()}
        for home in (os_home, managed):
            for root in _surface_roots(home).values():
                self.assertIn(str(root), roots, f"{root} should be a scanned surface")

    # --- env set, equal homes -------------------------------------------
    def test_equal_homes_collapse_to_single_surface(self) -> None:
        os_home = self._tmp / "os-home"
        os_home.mkdir()
        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(os_home)}, clear=False
        ):
            homes = resolve_global_homes()
            roots = _default_global_roots()
        self.assertEqual([h["origin"] for h in homes], ["both"])
        # One root per surface, not duplicated across "two" homes.
        self.assertEqual(len(roots), len(GLOBAL_HOME_SURFACES))

    # --- symlinked homes (realpath collapse) ----------------------------
    def test_symlinked_managed_home_collapses_via_realpath(self) -> None:
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        os_home.mkdir()
        # managed-home is a symlink to os-home -> same realpath -> one surface.
        managed.symlink_to(os_home, target_is_directory=True)
        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            homes = resolve_global_homes()
            roots = _default_global_roots()
        self.assertEqual([h["origin"] for h in homes], ["both"])
        self.assertEqual(len(roots), len(GLOBAL_HOME_SURFACES))

    def test_symlinked_skills_dir_collapses_to_one_root(self) -> None:
        # OS-home .claude/skills -> managed-home .claude/skills (the real
        # two-homes layout). Distinct home dirs, but the skills dirs share a
        # realpath so they must collapse to a single scanned root.
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        managed_claude = managed / ".claude" / "skills"
        managed_claude.mkdir(parents=True)
        os_claude_parent = os_home / ".claude"
        os_claude_parent.mkdir(parents=True)
        (os_claude_parent / "skills").symlink_to(managed_claude, target_is_directory=True)
        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            roots = _default_global_roots()
        claude_roots = [r for s, r in roots if s == "claude"]
        claude_realpaths = {os.path.realpath(r) for r in claude_roots}
        self.assertEqual(
            len(claude_realpaths),
            1,
            f"symlinked claude skills dirs must collapse to one surface, got {claude_roots}",
        )


class GlobalHomeSurfacesReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def _patch_home(self, home: Path):
        return mock.patch(
            "runtime_manager.skill_visibility.Path.home",
            return_value=home,
        )

    def test_report_lists_each_surface_with_realpath_and_partition(self) -> None:
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        os_claude = os_home / ".claude" / "skills"
        managed_claude = managed / ".claude" / "skills"
        os_claude.mkdir(parents=True)
        managed_claude.mkdir(parents=True)
        # os-home-only skill and managed-home-only skill.
        _seed_skill(os_claude, "os-only-skill")
        _seed_skill(managed_claude, "managed-only-skill")

        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            report = global_home_surfaces_report()

        self.assertEqual(len(report), 1)
        block = report[0]
        origins = [h["origin"] for h in block["homes"]]
        self.assertEqual(origins, ["os-home", "managed-home"])

        # Every reported surface carries its realpath.
        for home_block in block["homes"]:
            for surface in home_block["surfaces"]:
                self.assertIn("realpath", surface)
                self.assertEqual(
                    surface["realpath"], os.path.realpath(surface["root"])
                )

        os_only_names = {item["name"] for item in block["os_only"]}
        managed_only_names = {item["name"] for item in block["managed_only"]}
        self.assertIn("os-only-skill", os_only_names)
        self.assertIn("managed-only-skill", managed_only_names)
        self.assertNotIn("managed-only-skill", os_only_names)
        self.assertNotIn("os-only-skill", managed_only_names)

    def test_report_collapsed_surface_attributes_to_both(self) -> None:
        # Symlinked-equivalent homes -> single "both" surface, no spurious
        # os-only/managed-only split for the shared entries.
        os_home = self._tmp / "os-home"
        managed = self._tmp / "managed-home"
        os_home.mkdir()
        managed.symlink_to(os_home, target_is_directory=True)
        shared_claude = os_home / ".claude" / "skills"
        shared_claude.mkdir(parents=True)
        _seed_skill(shared_claude, "shared-skill")

        with self._patch_home(os_home), mock.patch.dict(
            os.environ, {GLOBAL_HOME_ROOT_ENV: str(managed)}, clear=False
        ):
            report = global_home_surfaces_report()

        block = report[0]
        self.assertEqual([h["origin"] for h in block["homes"]], ["both"])
        os_only_names = {item["name"] for item in block["os_only"]}
        managed_only_names = {item["name"] for item in block["managed_only"]}
        self.assertNotIn("shared-skill", os_only_names)
        self.assertNotIn("shared-skill", managed_only_names)


class ManagedHomeInstallTargetsTests(unittest.TestCase):
    def test_targets_derive_from_canonical_env_and_surfaces(self) -> None:
        targets = managed_home_install_targets()
        self.assertEqual([t["id"] for t in targets], list(GLOBAL_HOME_SURFACES))
        for surface, target in zip(GLOBAL_HOME_SURFACES, targets):
            self.assertEqual(
                target["path"],
                f"${{{GLOBAL_HOME_ROOT_ENV}}}/.{surface}/skills",
            )

    def test_targets_reference_the_managed_home_env_var(self) -> None:
        for target in managed_home_install_targets():
            self.assertIn(GLOBAL_HOME_ROOT_ENV, target["path"])


if __name__ == "__main__":
    unittest.main()
