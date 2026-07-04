"""Registry-id / category -> path resolution for skill-scope rules (bead y8w.3).

A scope rule may name its repos by registry id (``repos: [app_core, app_core-server]``)
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
    {"id": "app_core", "path": "~/repos/app_core", "bucket": "app"},
    {"id": "app_core-server", "path": "~/repos/api_server", "bucket": "backend"},
    {"id": "ingredient-server", "path": "~/repos/shared_service", "bucket": "backend"},
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
        # Devbox-style roots: the registry's ~/repos/app_core re-roots under each.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos", "/srv/repos")):
            entries = sv._load_registry_entries()
            paths, cats = sv._resolve_scope_rule_repos(["app_core", "app_core-server"], [], entries)
        self.assertEqual(cats, [])
        # Both the /srv/skillbox/repos and /srv/repos roots produce the repo's
        # remainder path; _expand_policy_path's resolve() folds the /srv/repos
        # symlink-alias into /srv/skillbox/repos when that alias is real on the
        # host (as on the devbox), so the canonical spelling is always present.
        self.assertIn("/srv/skillbox/repos/app_core", paths)
        self.assertIn("/srv/skillbox/repos/api_server", paths)
        # The /srv/repos spelling is present either as itself (no symlink) or
        # already collapsed into /srv/skillbox/repos (real symlink): the canonical
        # resolved form must cover it.
        self.assertIn(sv._expand_policy_path("/srv/repos/app_core"), paths)
        self.assertIn(sv._expand_policy_path("~/repos/app_core"), paths)

    def test_same_id_resolves_differently_per_machine(self) -> None:
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            devbox, _ = sv._resolve_scope_rule_repos(["app_core"], [], entries)
        with _ResolverHarness(repo_roots=("/Users/operator/repos",)):
            entries = sv._load_registry_entries()
            laptop, _ = sv._resolve_scope_rule_repos(["app_core"], [], entries)
        self.assertIn("/srv/skillbox/repos/app_core", devbox)
        self.assertIn("/Users/operator/repos/app_core", laptop)
        # Same registry id, machine-specific resolution.
        self.assertNotIn("/Users/operator/repos/app_core", devbox)
        self.assertNotIn("/srv/skillbox/repos/app_core", laptop)

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
                sv._resolve_scope_rule_repos(["app_coree"], [], entries)
        message = str(ctx.exception)
        self.assertIn("'app_coree' not in registry/repos.yaml", message)
        self.assertIn("did you mean 'app_core'", message)
        self.assertIn("declared ids:", message)
        # The full declared id list is part of the hint.
        self.assertIn("design-system-registry", message)

    def test_category_expands_via_registry_bucket(self) -> None:
        # categories: [backend] matches every repo whose registry bucket==backend.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            paths, cats = sv._resolve_scope_rule_repos([], ["backend"], entries)
        self.assertEqual(cats, ["backend"])
        self.assertIn("/srv/skillbox/repos/api_server", paths)
        self.assertIn("/srv/skillbox/repos/shared_service", paths)
        # app-bucket app_core is NOT a backend repo.
        self.assertNotIn("/srv/skillbox/repos/app_core", paths)

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
            sv._resolve_scope_rule_repos(["app_core"], [], [])
        self.assertEqual(sv._resolve_scope_rule_repos([], ["frontend"], []), ([], []))

    def test_scope_rule_from_raw_threads_repos_into_paths(self) -> None:
        raw_rule = {
            "id": "app_core-local",
            "skills": ["app_core-*"],
            "repos": ["app_core", "app_core-server"],
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
        self.assertEqual(rule["repos"], ["app_core", "app_core-server"])
        self.assertIn("/srv/skillbox/repos/app_core", rule["paths"])
        self.assertIn("/srv/skillbox/repos/api_server", rule["paths"])
        self.assertEqual(rule["unknown_categories"], [])

    def test_literal_paths_still_resolve_unchanged_alongside_repos(self) -> None:
        # ADDITIVE: a literal path and a registry id coexist; both land in paths.
        raw_rule = {
            "id": "mixed",
            "skills": ["x"],
            "repos": ["app_core"],
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
        self.assertIn("/srv/skillbox/repos/app_core", rule["paths"])
        self.assertIn(sv._expand_policy_path("~/repos/some-unregistered-repo"), rule["paths"])


class ScopeRulePathMatchTests(unittest.TestCase):
    def _model(self, rules: list[dict]) -> dict:
        return {
            "env": {},
            "active_clients": [],
            "active_profiles": ["core"],
            "clients": [
                {
                    "id": "c",
                    "context": {
                        "skill_scope": {"_policy_path": "inline:c", "rules": rules}
                    },
                }
            ],
            "skills": [],
        }

    def test_exact_rule_matches_root_but_not_child_repo(self) -> None:
        root = Path("/tmp/skillbox-portfolio-repos")
        child_repo = root / "cycle-chef"
        model = self._model(
            [
                {
                    "id": "portfolio-root-local",
                    "skills": ["make-indispensable"],
                    "paths": [str(root)],
                    "match": "exact",
                },
                {
                    "id": "prefix-local",
                    "skills": ["prefix-skill"],
                    "paths": [str(root)],
                },
            ]
        )

        parsed_rules = {rule["id"]: rule for rule in sv._scope_rules(model)}
        self.assertEqual(parsed_rules["portfolio-root-local"]["path_match"], "exact")
        self.assertEqual(parsed_rules["prefix-local"]["path_match"], "prefix")

        root_ids = [rule["id"] for rule in sv._matched_scope_rules_for_cwd(model, root)]
        child_ids = [rule["id"] for rule in sv._matched_scope_rules_for_cwd(model, child_repo)]

        self.assertIn("portfolio-root-local", root_ids)
        self.assertIn("prefix-local", root_ids)
        self.assertNotIn("portfolio-root-local", child_ids)
        self.assertIn("prefix-local", child_ids)


class MigratedRuleEquivalenceTests(unittest.TestCase):
    """Prove the migrated app_core-local/ui-local rules resolve to the SAME effective
    paths as their prior literal lists, on this machine, with no behavior change.
    """

    # Prior literal path lists, verbatim from skill-scope.yaml before migration.
    APP_CORE_LOCAL_BEFORE = [
        "~/repos/app_core", "/srv/repos/app_core", "/srv/skillbox/repos/app_core",
        "~/repos/api_server", "/srv/repos/api_server", "/srv/skillbox/repos/api_server",
    ]
    UI_LOCAL_REGISTRY_BEFORE = [
        "~/repos/design-system-registry",
        "/srv/repos/design-system-registry",
        "/srv/skillbox/repos/design-system-registry",
    ]

    def _literal_effective_set(self, paths: list[str]) -> set[str]:
        return {sv._expand_policy_path(p) for p in paths}

    def test_htma_local_after_equals_before_on_devbox_roots(self) -> None:
        before = self._literal_effective_set(self.APP_CORE_LOCAL_BEFORE)
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos", "/srv/repos")):
            entries = sv._load_registry_entries()
            after, _ = sv._resolve_scope_rule_repos(["app_core", "app_core-server"], [], entries)
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


class _UndetectedMachineHarness:
    """Like _ResolverHarness but reports the machine as UNDETECTED (None, None).

    Reproduces the failure mode where ``_machines_classifier`` swallowed every
    error (missing/broken machines.yaml, unmatched hostname, a renamed host, a
    worker container) to ``(None, None)``. The registry is still readable so an id
    is "known" — only re-rooting is impossible.
    """

    def __enter__(self) -> None:
        sv._registry_doctor_module_override = _fake_registry_doctor  # type: ignore[attr-defined]
        sv._machines_classifier_override = lambda: (None, None)  # type: ignore[attr-defined]
        self._prev_env = sv.os.environ.get(sv.REGISTRY_FILE_ENV_VAR)
        sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = __file__

    def __exit__(self, *_exc: object) -> None:
        sv.__dict__.pop("_registry_doctor_module_override", None)
        sv.__dict__.pop("_machines_classifier_override", None)
        if self._prev_env is None:
            sv.os.environ.pop(sv.REGISTRY_FILE_ENV_VAR, None)
        else:
            sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = self._prev_env


class UndetectedMachineFailLoudTests(unittest.TestCase):
    """BUG A: a registry-id rule used while the current machine is UNDETECTED must
    FAIL LOUD rather than silently collapse to the home-form-only spelling (which
    matches NO real ``/srv/skillbox/repos/<repo>`` cwd → skills silently vanish).
    """

    def test_repos_id_under_repos_root_raises_when_machine_undetected(self) -> None:
        with _UndetectedMachineHarness():
            entries = sv._load_registry_entries()
            with self.assertRaises(sv.RegistryResolutionError) as ctx:
                sv._resolve_scope_rule_repos(["app_core"], [], entries)
        message = str(ctx.exception)
        self.assertIn("current machine undetected", message)
        self.assertIn("machines.yaml", message)
        self.assertIn("SKILLBOX_MACHINE", message)

    def test_category_under_repos_root_raises_when_machine_undetected(self) -> None:
        # A registry-bucket category that expands to ~/repos repos is equally
        # unresolvable when the machine is undetected.
        with _UndetectedMachineHarness():
            entries = sv._load_registry_entries()
            with self.assertRaises(sv.RegistryResolutionError):
                sv._resolve_scope_rule_repos([], ["backend"], entries)

    def test_non_repos_root_id_still_resolves_when_machine_undetected(self) -> None:
        # mmd-pcb lives under ~/hard (NOT a repo root that needs re-rooting), so it
        # expands home-relative as-is even with the machine undetected — the
        # fail-loud guard fires ONLY for paths that actually need re-rooting.
        with _UndetectedMachineHarness():
            entries = sv._load_registry_entries()
            paths, _ = sv._resolve_scope_rule_repos(["mmd-pcb"], [], entries)
        self.assertEqual(paths, [sv._expand_policy_path("~/hard/mmd-pcb")])

    def test_detected_machine_still_resolves_unchanged(self) -> None:
        # The healthy path is unaffected: a detected machine re-roots normally.
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            entries = sv._load_registry_entries()
            paths, _ = sv._resolve_scope_rule_repos(["app_core"], [], entries)
        self.assertIn("/srv/skillbox/repos/app_core", paths)


class MalformedRegistryEntryTests(unittest.TestCase):
    """BUG B: a known registry id whose entry has a missing/empty ``path`` is a
    MALFORMED entry — it must raise (consistent with unknown-id), not silently
    resolve to [] and make the rule match nothing.
    """

    def _harness_with(self, repos: list[dict]) -> None:
        # Install a fake registry exposing the supplied repos + a detected machine.
        def fake() -> types.SimpleNamespace:
            return types.SimpleNamespace(
                load_registry=lambda _p: {"repos": [dict(r) for r in repos]},
                DEFAULT_REGISTRY="/fake/registry/repos.yaml",
            )

        sv._registry_doctor_module_override = fake  # type: ignore[attr-defined]
        config = _machines_config(repo_roots=("/srv/skillbox/repos",))
        sv._machines_classifier_override = lambda: (config, "test-machine")  # type: ignore[attr-defined]
        self._prev_env = sv.os.environ.get(sv.REGISTRY_FILE_ENV_VAR)
        sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = __file__

    def tearDown(self) -> None:
        sv.__dict__.pop("_registry_doctor_module_override", None)
        sv.__dict__.pop("_machines_classifier_override", None)
        prev = getattr(self, "_prev_env", None)
        if prev is None:
            sv.os.environ.pop(sv.REGISTRY_FILE_ENV_VAR, None)
        else:
            sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = prev

    def test_empty_path_entry_raises(self) -> None:
        self._harness_with([{"id": "broke", "path": "", "bucket": "app"}])
        entries = sv._load_registry_entries()
        with self.assertRaises(sv.RegistryResolutionError) as ctx:
            sv._resolve_scope_rule_repos(["broke"], [], entries)
        message = str(ctx.exception)
        self.assertIn("broke", message)
        self.assertIn("missing/empty", message)

    def test_missing_path_key_entry_raises(self) -> None:
        self._harness_with([{"id": "broke", "bucket": "app"}])
        entries = sv._load_registry_entries()
        with self.assertRaises(sv.RegistryResolutionError):
            sv._resolve_scope_rule_repos(["broke"], [], entries)

    def test_category_with_one_malformed_member_raises(self) -> None:
        # A bucket whose member has an empty path is equally malformed.
        self._harness_with([{"id": "broke", "path": "", "bucket": "backend"}])
        entries = sv._load_registry_entries()
        with self.assertRaises(sv.RegistryResolutionError):
            sv._resolve_scope_rule_repos([], ["backend"], entries)


class ScalarSkillsPatternTests(unittest.TestCase):
    """BUG D: a SCALAR ``skills:`` string must become a single-element pattern
    list, not be char-iterated (``skills: foo`` → ``['f','o','o']``).
    """

    def test_scalar_skills_string_is_single_pattern(self) -> None:
        self.assertEqual(sv._scope_rule_patterns({"skills": "foo"}), ["foo"])

    def test_list_skills_unchanged(self) -> None:
        self.assertEqual(
            sv._scope_rule_patterns({"skills": ["foo", "bar"]}), ["foo", "bar"]
        )

    def test_scalar_patterns_alias_is_single_pattern(self) -> None:
        self.assertEqual(sv._scope_rule_patterns({"patterns": "baz"}), ["baz"])

    def test_scalar_names_alias_is_single_pattern(self) -> None:
        self.assertEqual(sv._scope_rule_patterns({"names": "qux"}), ["qux"])

    def test_empty_skills_falls_through_to_patterns(self) -> None:
        # Back-compat: an empty/absent key still falls through to the next alias.
        self.assertEqual(
            sv._scope_rule_patterns({"skills": [], "patterns": ["p"]}), ["p"]
        )

    def test_scalar_skills_threads_through_scope_rule_from_raw(self) -> None:
        # End-to-end: a rule authored with a scalar skills string yields ONE
        # pattern (so the rule survives instead of becoming char-fragment patterns).
        raw_rule = {"id": "scalar", "skills": "app_core-deploy", "paths": ["~/repos/x"]}
        rule = sv._scope_rule_from_raw(
            raw_rule,
            index=0,
            policy={"_policy_path": "/p.yaml"},
            categories={},
            overlays_on=set(),
            registry_entries=[],
        )
        assert rule is not None
        self.assertEqual(rule["patterns"], ["app_core-deploy"])


class ResilientScopeRulesTests(unittest.TestCase):
    """BUG C: one bad rule must not nuke the WHOLE report. ``_scope_rules`` SKIPS
    the bad rule, keeps resolving the rest, and surfaces the error via
    ``last_scope_rule_errors`` so a doctor lint can report the typo.
    """

    def _model(self, rules: list[dict]) -> dict:
        return {
            "env": {},
            "active_clients": [],
            "active_profiles": ["core"],
            "clients": [
                {
                    "id": "c",
                    "context": {
                        "skill_scope": {"_policy_path": "inline:c", "rules": rules}
                    },
                }
            ],
            "skills": [],
        }

    def test_one_typo_does_not_drop_the_other_rules(self) -> None:
        good = {"id": "good", "skills": ["g-*"], "repos": ["app_core"]}
        typo = {"id": "typo", "skills": ["t-*"], "repos": ["app_coree"]}  # near app_core
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            rules = sv._scope_rules(self._model([typo, good]))
            errors = sv.last_scope_rule_errors()
        # The good rule survived; the typo'd rule was skipped.
        ids = [r["id"] for r in rules]
        self.assertIn("good", ids)
        self.assertNotIn("typo", ids)
        # The typo is surfaced with the self-healing hint.
        self.assertEqual([e["rule_id"] for e in errors], ["typo"])
        self.assertEqual(errors[0]["type"], "RegistryResolutionError")
        self.assertIn("'app_coree' not in registry/repos.yaml", errors[0]["error"])
        self.assertIn("did you mean 'app_core'", errors[0]["error"])

    def test_clean_pass_records_no_errors(self) -> None:
        good = {"id": "good", "skills": ["g-*"], "repos": ["app_core"]}
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            rules = sv._scope_rules(self._model([good]))
            errors = sv.last_scope_rule_errors()
        self.assertEqual([r["id"] for r in rules], ["good"])
        self.assertEqual(errors, [])

    def test_errors_are_reset_each_pass(self) -> None:
        typo = {"id": "typo", "skills": ["t-*"], "repos": ["htmaa"]}
        good = {"id": "good", "skills": ["g-*"], "repos": ["app_core"]}
        with _ResolverHarness(repo_roots=("/srv/skillbox/repos",)):
            sv._scope_rules(self._model([typo]))
            self.assertEqual(len(sv.last_scope_rule_errors()), 1)
            # A subsequent clean pass clears the prior pass's errors.
            sv._scope_rules(self._model([good]))
            self.assertEqual(sv.last_scope_rule_errors(), [])


if __name__ == "__main__":
    unittest.main()
