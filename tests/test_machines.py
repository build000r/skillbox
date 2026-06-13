"""Self-contained unit tests for runtime_manager.machines.

Covers machine detection, the SKILLBOX_MACHINE env override, root translation
(both directions), foreign-path classification, and alias canonicalization.

These tests do NOT depend on the live machine identity: every case loads a
fixture machines.yaml written into a tempdir and supplies an explicit
``hostname=`` and ``env=`` so nothing reads the real host or process env.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"

if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

try:  # PyYAML is required to parse machines.yaml; skip cleanly if absent.
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

from runtime_manager import machines as m


FIXTURE_YAML = textwrap.dedent(
    """
    version: 1

    machines:
      mac-laptop:
        hostnames: [bs-macbook-air, macbook-pro]
        home: /Users/b
        repo_roots:
          - /Users/b/repos
          - ~/repos
        projects_roots:
          - /Users/b/projects

      portfolio-devbox:
        hostnames: [skillbox-portfolio-devbox, portfolio-devbox]
        home: /home/skillbox
        managed_home: /srv/skillbox/home
        repo_roots:
          - /srv/skillbox/repos
          - /srv/repos
        projects_roots:
          - /srv/skillbox/projects

    aliases:
      - alias: /srv/repos
        canonical: /srv/skillbox/repos
    """
).strip()


def _write_fixture(directory: Path) -> Path:
    path = directory / "machines.yaml"
    path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
    return path


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class MachinesLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.fixture = _write_fixture(self.tmpdir)
        self.config = m.load_machines_config(self.fixture)

    # -- config shape ----------------------------------------------------

    def test_load_parses_machines_and_aliases(self) -> None:
        self.assertEqual(
            sorted(self.config.machines), ["mac-laptop", "portfolio-devbox"]
        )
        devbox = self.config.require("portfolio-devbox")
        self.assertEqual(devbox.home, "/home/skillbox")
        self.assertEqual(devbox.managed_home, "/srv/skillbox/home")
        self.assertEqual(devbox.canonical_repo_root, "/srv/skillbox/repos")
        self.assertIn("/srv/repos", devbox.repo_roots)
        self.assertEqual(len(self.config.aliases), 1)
        self.assertEqual(self.config.aliases[0].alias, "/srv/repos")
        self.assertEqual(self.config.aliases[0].canonical, "/srv/skillbox/repos")

    def test_require_unknown_machine_raises(self) -> None:
        with self.assertRaises(m.MachinesConfigError):
            self.config.require("nope")

    def test_unsupported_version_raises(self) -> None:
        bad = self.tmpdir / "bad.yaml"
        bad.write_text("version: 999\nmachines: {}\n", encoding="utf-8")
        with self.assertRaises(m.MachinesConfigError):
            m.load_machines_config(bad)

    # -- machine detection ----------------------------------------------

    def test_detect_by_hostname_devbox(self) -> None:
        machine_id = self.config.detect_machine_id(
            hostname="skillbox-portfolio-devbox", env={}
        )
        self.assertEqual(machine_id, "portfolio-devbox")

    def test_detect_by_short_hostname_strips_domain(self) -> None:
        # A FQDN should match on its short form (the part before the first ".").
        machine_id = self.config.detect_machine_id(
            hostname="skillbox-portfolio-devbox.tail-scale.ts.net", env={}
        )
        self.assertEqual(machine_id, "portfolio-devbox")

    def test_detect_by_hostname_is_case_insensitive(self) -> None:
        machine_id = self.config.detect_machine_id(
            hostname="BS-MacBook-Air", env={}
        )
        self.assertEqual(machine_id, "mac-laptop")

    def test_detect_unknown_hostname_returns_none(self) -> None:
        machine_id = self.config.detect_machine_id(
            hostname="some-random-box", env={}
        )
        self.assertIsNone(machine_id)

    def test_current_profile_resolves_from_hostname(self) -> None:
        profile = self.config.current_profile(
            hostname="portfolio-devbox", env={}
        )
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.machine_id, "portfolio-devbox")

    # -- SKILLBOX_MACHINE env override ----------------------------------

    def test_env_override_wins_over_hostname(self) -> None:
        # Hostname says devbox, but the override forces mac-laptop.
        machine_id = self.config.detect_machine_id(
            hostname="skillbox-portfolio-devbox",
            env={m.MACHINE_ENV_VAR: "mac-laptop"},
        )
        self.assertEqual(machine_id, "mac-laptop")

    def test_env_override_selects_machine_with_no_hostname_match(self) -> None:
        machine_id = self.config.detect_machine_id(
            hostname="totally-unknown-host",
            env={m.MACHINE_ENV_VAR: "portfolio-devbox"},
        )
        self.assertEqual(machine_id, "portfolio-devbox")

    def test_env_override_unknown_machine_raises(self) -> None:
        with self.assertRaises(m.MachinesConfigError):
            self.config.detect_machine_id(
                hostname="skillbox-portfolio-devbox",
                env={m.MACHINE_ENV_VAR: "ghost-box"},
            )

    def test_blank_env_override_falls_back_to_hostname(self) -> None:
        machine_id = self.config.detect_machine_id(
            hostname="skillbox-portfolio-devbox",
            env={m.MACHINE_ENV_VAR: "   "},
        )
        self.assertEqual(machine_id, "portfolio-devbox")

    # -- alias canonicalization -----------------------------------------

    def test_alias_canonicalization_rewrites_to_real_tree(self) -> None:
        self.assertEqual(
            self.config.canonicalize_alias("/srv/repos/skillbox-config/x.yaml"),
            "/srv/skillbox/repos/skillbox-config/x.yaml",
        )

    def test_alias_canonicalization_of_bare_alias_root(self) -> None:
        self.assertEqual(
            self.config.canonicalize_alias("/srv/repos"),
            "/srv/skillbox/repos",
        )

    def test_alias_canonicalization_noop_for_non_alias_path(self) -> None:
        self.assertEqual(
            self.config.canonicalize_alias("/Users/b/repos/foo"),
            "/Users/b/repos/foo",
        )

    def test_alias_not_applied_to_lookalike_sibling(self) -> None:
        # "/srv/reposX" must NOT be treated as under the "/srv/repos" alias.
        self.assertEqual(
            self.config.canonicalize_alias("/srv/reposX/foo"),
            "/srv/reposX/foo",
        )

    # -- root translation (both directions) ------------------------------

    def test_translate_devbox_to_mac(self) -> None:
        out = self.config.translate_path(
            "/srv/skillbox/repos/skillbox-config/machines.yaml",
            "portfolio-devbox",
            "mac-laptop",
        )
        self.assertEqual(out, "/Users/b/repos/skillbox-config/machines.yaml")

    def test_translate_mac_to_devbox(self) -> None:
        out = self.config.translate_path(
            "/Users/b/repos/skillbox-config/machines.yaml",
            "mac-laptop",
            "portfolio-devbox",
        )
        self.assertEqual(
            out, "/srv/skillbox/repos/skillbox-config/machines.yaml"
        )

    def test_translate_round_trips(self) -> None:
        start = "/srv/skillbox/repos/opensource/skillbox/AGENTS.md"
        to_mac = self.config.translate_path(
            start, "portfolio-devbox", "mac-laptop"
        )
        back = self.config.translate_path(
            to_mac, "mac-laptop", "portfolio-devbox"
        )
        self.assertEqual(back, start)

    def test_translate_canonicalizes_alias_on_source_side(self) -> None:
        # An alias-form devbox path should translate as if canonical.
        out = self.config.translate_path(
            "/srv/repos/skillbox-config/machines.yaml",
            "portfolio-devbox",
            "mac-laptop",
        )
        self.assertEqual(out, "/Users/b/repos/skillbox-config/machines.yaml")

    def test_translate_projects_root(self) -> None:
        out = self.config.translate_path(
            "/srv/skillbox/projects/foo/bar",
            "portfolio-devbox",
            "mac-laptop",
        )
        self.assertEqual(out, "/Users/b/projects/foo/bar")

    def test_translate_returns_none_for_path_outside_src_roots(self) -> None:
        out = self.config.translate_path(
            "/etc/hosts", "portfolio-devbox", "mac-laptop"
        )
        self.assertIsNone(out)

    def test_translate_root_itself_maps_to_dst_root(self) -> None:
        out = self.config.translate_path(
            "/srv/skillbox/repos", "portfolio-devbox", "mac-laptop"
        )
        self.assertEqual(out, "/Users/b/repos")

    # -- foreign-path classification ------------------------------------

    def test_foreign_path_from_other_machine_is_foreign(self) -> None:
        self.assertTrue(
            self.config.is_foreign_path(
                "/Users/b/repos/foo", "portfolio-devbox"
            )
        )

    def test_own_path_is_not_foreign(self) -> None:
        self.assertFalse(
            self.config.is_foreign_path(
                "/srv/skillbox/repos/foo", "portfolio-devbox"
            )
        )

    def test_alias_path_is_not_foreign_for_owning_machine(self) -> None:
        # /srv/repos is the devbox's own tree via alias; not foreign to devbox.
        self.assertFalse(
            self.config.is_foreign_path("/srv/repos/foo", "portfolio-devbox")
        )

    def test_unrooted_path_is_not_foreign(self) -> None:
        # A path under no declared root makes no claim; it is not "foreign".
        self.assertFalse(
            self.config.is_foreign_path("/var/log/syslog", "portfolio-devbox")
        )

    def test_is_foreign_unknown_machine_raises(self) -> None:
        with self.assertRaises(m.MachinesConfigError):
            self.config.is_foreign_path("/srv/skillbox/repos/foo", "ghost")

    def test_classify_path_reports_machine_and_remainder(self) -> None:
        result = self.config.classify_path("/srv/repos/opensource/skillbox")
        self.assertEqual(result["machines"], ["portfolio-devbox"])
        self.assertEqual(result["canonical"], "/srv/skillbox/repos/opensource/skillbox")
        self.assertEqual(len(result["matches"]), 1)
        match = result["matches"][0]
        self.assertEqual(match["machine"], "portfolio-devbox")
        self.assertEqual(match["category"], "repos")
        self.assertEqual(match["root"], "/srv/skillbox/repos")
        self.assertEqual(match["remainder"], "opensource/skillbox")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class MachinesLocationTests(unittest.TestCase):
    """Locating machines.yaml: explicit path env override + candidate search."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_find_via_file_env_override(self) -> None:
        path = _write_fixture(self.tmpdir)
        found = m.find_machines_yaml(
            env={m.MACHINES_FILE_ENV_VAR: str(path)}
        )
        self.assertEqual(found, str(path))

    def test_find_via_config_root(self) -> None:
        config_dir = self.tmpdir / "skillbox-config"
        config_dir.mkdir()
        path = _write_fixture(config_dir)
        found = m.find_machines_yaml(config_root=str(config_dir), env={})
        self.assertEqual(os.path.realpath(found), os.path.realpath(str(path)))

    def test_find_relative_to_runtime_root_sibling_layout(self) -> None:
        # Simulate <root>/../skillbox-config/machines.yaml.
        root = self.tmpdir / "skillbox"
        root.mkdir()
        config_dir = self.tmpdir / "skillbox-config"
        config_dir.mkdir()
        path = _write_fixture(config_dir)
        found = m.find_machines_yaml(root_dir=str(root), env={})
        self.assertEqual(os.path.realpath(found), os.path.realpath(str(path)))

    def test_find_relative_to_nested_runtime_root_devbox_layout(self) -> None:
        # Simulate the devbox: <repos>/opensource/skillbox runtime root, with
        # skillbox-config a sibling of opensource (two levels up).
        root = self.tmpdir / "opensource" / "skillbox"
        root.mkdir(parents=True)
        config_dir = self.tmpdir / "skillbox-config"
        config_dir.mkdir()
        path = _write_fixture(config_dir)
        found = m.find_machines_yaml(root_dir=str(root), env={})
        self.assertEqual(os.path.realpath(found), os.path.realpath(str(path)))

    def test_missing_file_returns_none_and_load_raises(self) -> None:
        # Pin BOTH config_root and root_dir into the empty tempdir so the
        # candidate search cannot fall through to the real, on-disk
        # skillbox-config/machines.yaml. (find_machines_yaml deliberately tries
        # the runtime-root-relative candidates after config_root, so isolating
        # the search requires steering root_dir away from the live repo too.)
        empty = self.tmpdir / "empty"
        empty.mkdir()
        isolated = self.tmpdir / "isolated"
        isolated.mkdir()
        self.assertIsNone(
            m.find_machines_yaml(
                config_root=str(empty), root_dir=str(isolated), env={}
            )
        )
        with self.assertRaises(m.MachinesConfigError):
            m.load_machines_config(
                config_root=str(empty), root_dir=str(isolated), env={}
            )

    def test_module_level_detect_and_current_profile(self) -> None:
        path = _write_fixture(self.tmpdir)
        config = m.load_machines_config(path)
        self.assertEqual(
            m.detect_machine_id(config, hostname="portfolio-devbox", env={}),
            "portfolio-devbox",
        )
        profile = m.current_profile(config, hostname="bs-macbook-air", env={})
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.machine_id, "mac-laptop")


if __name__ == "__main__":
    unittest.main()
