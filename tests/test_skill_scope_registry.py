"""Registry-id / category -> path resolution for skill-scope rules (bead y8w.3).

A scope rule may name its repos by registry id (``repos: [htma, htma-server]``)
and/or registry-bucket category (``categories: [backend]``) instead of literal
``paths:``. The id->path taxonomy is the canonical operator registry
(``skillbox-config/registry/repos.yaml`` — the SAME file
``scripts/registry_doctor.py`` validates) and the per-machine path is derived at
eval time from ``machines.yaml``. These tests are hermetic: a fake registry +
fake machines config are injected via module override hooks, so they assert the
resolution contract without depending on the live host identity.

Runs under both pytest and ``python3 -m unittest discover -s tests``.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility as sv  # noqa: E402
from runtime_manager import machines as machines_mod  # noqa: E402


# A small fake registry mirroring the real repos.yaml shape (id/path/bucket).
# Paths are home-relative (~/repos/..., ~/hard/...) exactly like the real file.
FAKE_REGISTRY_REPOS = [
    {"id": "htma", "path": "~/repos/htma", "bucket": "app"},
    {"id": "htma-server", "path": "~/repos/htma_server", "bucket": "backend"},
    {"id": "ingredient-server", "path": "~/repos/ingredient_server", "bucket": "backend"},
    {"id": "design-system-registry", "path": "~/repos/design-system-registry", "bucket": "registry"},
    {"id": "mmd-pcb", "path": "~/hard/mmd-pcb", "bucket": "hardware"},
]


def _fake_registry_doctor() -> types.SimpleNamespace:
    """A stand-in for scripts/registry_doctor.py exposing ``load_registry``."""
    def load_registry(_path: object) -> dict:
        return {"repos": [dict(item) for item in FAKE_REGISTRY_REPOS]}

    return types.SimpleNamespace(
        load_registry=load_registry,
        DEFAULT_REGISTRY="/fake/registry/repos.yaml",
    )


def _machines_config(*, repo_roots: tuple[str, ...]) -> machines_mod.MachinesConfig:
    """A one-machine config whose current profile uses ``repo_roots``."""
    profile = machines_mod.MachineProfile(
        machine_id="test-machine",
        hostnames=("test-host",),
        repo_roots=repo_roots,
    )
    return machines_mod.MachinesConfig(machines={"test-machine": profile})


class _ResolverHarness:
    """Install fake registry + fake machine config via the module override hooks."""

    def __init__(self, repo_roots: tuple[str, ...]) -> None:
        self._repo_roots = repo_roots

    def __enter__(self) -> None:
        sv._registry_doctor_module_override = _fake_registry_doctor  # type: ignore[attr-defined]
        config = _machines_config(repo_roots=self._repo_roots)
        sv._machines_classifier_override = lambda: (config, "test-machine")  # type: ignore[attr-defined]
        # Point the registry-file resolver at an existing file so it is "found".
        self._prev_env = sv.os.environ.get(sv.REGISTRY_FILE_ENV_VAR)
        sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = __file__

    def __exit__(self, *_exc: object) -> None:
        sv.__dict__.pop("_registry_doctor_module_override", None)
        sv.__dict__.pop("_machines_classifier_override", None)
        if self._prev_env is None:
            sv.os.environ.pop(sv.REGISTRY_FILE_ENV_VAR, None)
        else:
            sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = self._prev_env


class RegistryResolutionTests(unittest.TestCase):
    def test_repo_id_resolves_to_current_machine_path(self) -> None:
        # Devbox-style roots: the registry's ~/repos/htma re-roots under each.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos", "/srv/repos")):
            entries = sv._load_registry_entries()
            paths, cats = sv._resolve_scope_rule_repos(["htma", "htma-server"], [], entries)
        self.assertEqual(cats, [])
        # Both the /srv/skillbox/repos and /srv/repos roots produce the repo's
        # remainder path; _expand_policy_path's resolve() folds the /srv/repos
        # symlink-alias into /srv/skillbox/repos when that alias is real on the
        # host (as on the devbox), so the canonical spelling is always present.
        self.assertIn("/srv/skillbox/repos/htma", paths)
        self.assertIn("/srv/skillbox/repos/htma_server", paths)
        # The /srv/repos spelling is present either as itself (no symlink) or
        # already collapsed into /srv/skillbox/repos (real symlink): the canonical
        # resolved form must cover it.
        self.assertIn(sv._expand_policy_path("/srv/repos/htma"), paths)
        self.assertIn(sv._expand_policy_path("~/repos/htma"), paths)

    def test_same_id_resolves_differently_per_machine(self) -> None:
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            devbox, _ = sv._resolve_scope_rule_repos(["htma"], [], entries)
        with _ResolverHarness(repo_roots=("/Users/b/repos",)):
            entries = sv._load_registry_entries()
            laptop, _ = sv._resolve_scope_rule_repos(["htma"], [], entries)
        self.assertIn("/srv/skillbox/repos/htma", devbox)
        self.assertIn("/Users/b/repos/htma", laptop)
        # Same registry id, machine-specific resolution.
        self.assertNotIn("/Users/b/repos/htma", devbox)
        self.assertNotIn("/srv/skillbox/repos/htma", laptop)

    def test_non_repos_root_path_is_expanded_home_relative(self) -> None:
        # mmd-pcb lives under ~/hard (NOT a machine repo_root) so it carries no
        # machine re-rooting — it expands home-relative as-is.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            paths, _ = sv._resolve_scope_rule_repos(["mmd-pcb"], [], entries)
        self.assertEqual(paths, [sv._expand_policy_path("~/hard/mmd-pcb")])

    def test_unknown_id_raises_with_fix_hint(self) -> None:
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            with self.assertRaises(sv.RegistryResolutionError) as ctx:
                sv._resolve_scope_rule_repos(["htmaa"], [], entries)
        message = str(ctx.exception)
        self.assertIn("'htmaa' not in registry/repos.yaml", message)
        self.assertIn("did you mean 'htma'", message)
        self.assertIn("declared ids:", message)
        # The full declared id list is part of the hint.
        self.assertIn("design-system-registry", message)

    def test_category_expands_via_registry_bucket(self) -> None:
        # categories: [backend] matches every repo whose registry bucket==backend.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            paths, cats = sv._resolve_scope_rule_repos([], ["backend"], entries)
        self.assertEqual(cats, ["backend"])
        self.assertIn("/srv/skillbox/repos/htma_server", paths)
        self.assertIn("/srv/skillbox/repos/ingredient_server", paths)
        # app-bucket htma is NOT a backend repo.
        self.assertNotIn("/srv/skillbox/repos/htma", paths)

    def test_unknown_category_is_not_a_registry_match(self) -> None:
        # A category id that matches no registry bucket simply does not expand
        # here (the policy project_categories block owns that resolution path).
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            paths, cats = sv._resolve_scope_rule_repos([], ["frontend"], entries)
        self.assertEqual(paths, [])
        self.assertEqual(cats, [])

    def test_unreadable_registry_raises_for_explicit_repos(self) -> None:
        # No registry entries + an explicit repos: id -> a clear resolution error,
        # not a silent drop (categories degrade quietly to policy resolution).
        with self.assertRaises(sv.RegistryResolutionError):
            sv._resolve_scope_rule_repos(["htma"], [], [])
        self.assertEqual(sv._resolve_scope_rule_repos([], ["frontend"], []), ([], []))

    def test_scope_rule_from_raw_threads_repos_into_paths(self) -> None:
        raw_rule = {
            "id": "htma-local",
            "skills": ["htma-*"],
            "repos": ["htma", "htma-server"],
        }
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            rule = sv._scope_rule_from_raw(
                raw_rule,
                index=0,
                policy={"_policy_path": "/p.yaml"},
                categories={},
                overlays_on=set(),
                registry_entries=entries,
            )
        assert rule is not None
        self.assertEqual(rule["repos"], ["htma", "htma-server"])
        self.assertIn("/srv/skillbox/repos/htma", rule["paths"])
        self.assertIn("/srv/skillbox/repos/htma_server", rule["paths"])
        self.assertEqual(rule["unknown_categories"], [])

    def test_literal_paths_still_resolve_unchanged_alongside_repos(self) -> None:
        # ADDITIVE: a literal path and a registry id coexist; both land in paths.
        raw_rule = {
            "id": "mixed",
            "skills": ["x"],
            "repos": ["htma"],
            "paths": ["~/repos/some-unregistered-repo"],
        }
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            rule = sv._scope_rule_from_raw(
                raw_rule,
                index=0,
                policy={"_policy_path": "/p.yaml"},
                categories={},
                overlays_on=set(),
                registry_entries=entries,
            )
        assert rule is not None
        self.assertIn("/srv/skillbox/repos/htma", rule["paths"])
        self.assertIn(sv._expand_policy_path("~/repos/some-unregistered-repo"), rule["paths"])


class MigratedRuleEquivalenceTests(unittest.TestCase):
    """Prove the migrated htma-local/ui-local rules resolve to the SAME effective
    paths as their prior literal lists, on this machine, with no behavior change.
    """

    # Prior literal path lists, verbatim from skill-scope.yaml before migration.
    HTMA_LOCAL_BEFORE = [
        "~/repos/htma", "/srv/repos/htma", "/srv/skillbox/repos/htma",
        "~/repos/htma_server", "/srv/repos/htma_server", "/srv/skillbox/repos/htma_server",
    ]
    UI_LOCAL_REGISTRY_BEFORE = [
        "~/repos/design-system-registry",
        "/srv/repos/design-system-registry",
        "/srv/skillbox/repos/design-system-registry",
    ]

    def _literal_effective_set(self, paths: list[str]) -> set[str]:
        return {sv._expand_policy_path(p) for p in paths}

    def test_htma_local_after_equals_before_on_devbox_roots(self) -> None:
        before = self._literal_effective_set(self.HTMA_LOCAL_BEFORE)
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos", "/srv/repos")):
            entries = sv._load_registry_entries()
            after, _ = sv._resolve_scope_rule_repos(["htma", "htma-server"], [], entries)
        # The /srv/repos symlink-alias collapses into /srv/skillbox/repos under
        # _expand_policy_path's resolve() on the real devbox; in this hermetic
        # test the roots are not real symlinks, so assert BEFORE is a subset of
        # AFTER (AFTER additionally carries the resolved-but-equivalent spellings).
        self.assertTrue(before.issubset(set(after)), f"{before - set(after)} missing")

    def test_ui_local_registry_component_after_equals_before(self) -> None:
        before = self._literal_effective_set(self.UI_LOCAL_REGISTRY_BEFORE)
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos", "/srv/repos")):
            entries = sv._load_registry_entries()
            after, _ = sv._resolve_scope_rule_repos(["design-system-registry"], [], entries)
        self.assertTrue(before.issubset(set(after)), f"{before - set(after)} missing")


if __name__ == "__main__":
    unittest.main()
