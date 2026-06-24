"""Lint coverage for the registry-path-duplication doctor check (bead y8w.3).

Raw ``paths:`` stay fully supported for back-compat, but a literal path that a
registry id already covers EXACTLY is redundant: the repo could be named ONCE via
``repos: [<id>]`` and have its per-machine path derived from
registry/repos.yaml + machines.yaml. ``validate_registry_path_duplication``
WARNS (never FAILS) on such a path so the duplication is visibly discouraged.

Crucially the warn is gated on EQUALITY, not membership (the y8w fix): a registry
id resolves to a SET of spellings on the current machine (its home form PLUS every
re-rooting under the machine's repo roots), so a single home-form literal like
``~/repos/example-app`` is a strict SUBSET of the id's resolved set. Swapping that
one literal for ``repos: [example-app]`` would WIDEN the rule's match set to the
whole superset — a real behavior change — so the lint must NOT recommend it. It
only warns when the rule's literals enumerate the id's FULL resolved set, making
the swap a no-op on every machine.

These tests inject a fake registry + fake machine config via the
``skill_visibility`` override hooks so they assert the lint contract hermetically.
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
from runtime_manager.validation import (  # noqa: E402
    REGISTRY_PATH_DUPLICATION_CODE,
    validate_registry_path_duplication,
)


FAKE_REGISTRY_REPOS = [
    {"id": "app_core", "path": "~/repos/app_core", "bucket": "app"},
    {"id": "app_core-server", "path": "~/repos/api_server", "bucket": "backend"},
]


def _fake_registry_doctor() -> types.SimpleNamespace:
    def load_registry(_path: object) -> dict:
        return {"repos": [dict(item) for item in FAKE_REGISTRY_REPOS]}

    return types.SimpleNamespace(
        load_registry=load_registry, DEFAULT_REGISTRY="/fake/repos.yaml"
    )


class _Harness:
    def __init__(self, repo_roots: tuple[str, ...]) -> None:
        self._repo_roots = repo_roots

    def __enter__(self) -> None:
        sv._registry_doctor_module_override = _fake_registry_doctor  # type: ignore[attr-defined]
        profile = machines_mod.MachineProfile(
            machine_id="test-machine", hostnames=("h",), repo_roots=self._repo_roots
        )
        config = machines_mod.MachinesConfig(machines={"test-machine": profile})
        sv._machines_classifier_override = lambda: (config, "test-machine")  # type: ignore[attr-defined]
        self._prev = sv.os.environ.get(sv.REGISTRY_FILE_ENV_VAR)
        sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = __file__

    def __exit__(self, *_exc: object) -> None:
        sv.__dict__.pop("_registry_doctor_module_override", None)
        sv.__dict__.pop("_machines_classifier_override", None)
        if self._prev is None:
            sv.os.environ.pop(sv.REGISTRY_FILE_ENV_VAR, None)
        else:
            sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = self._prev


class RegistryPathDuplicationTests(unittest.TestCase):
    def test_warns_when_literals_enumerate_the_full_resolved_set(self) -> None:
        # On a `~/repos`-rooted box, the `app_core` id resolves to BOTH the home form
        # and the /srv re-rooting. A rule whose literal paths enumerate that FULL
        # set is a true no-op-replaceable duplicate, so the lint WARNS.
        policy = {
            "rules": [
                {
                    "id": "app_core-old",
                    "skills": ["x"],
                    "paths": ["~/repos/app_core", "/srv/skillbox/repos/app_core"],
                },
            ]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.code, REGISTRY_PATH_DUPLICATION_CODE)
        self.assertIn("app_core-old", result.message)
        self.assertIn("'app_core'", result.message)
        self.assertIn("repos: [<id>]", result.message)
        redundant = result.details["redundant"]
        self.assertEqual(redundant[0]["rule"], "app_core-old")
        self.assertEqual(redundant[0]["registry_id"], "app_core")

    def test_warns_on_single_spelling_id_with_no_repo_root_rerooting(self) -> None:
        # With NO machine repo roots, `app_core` resolves to exactly its home form, so
        # a lone `~/repos/app_core` literal already equals the FULL set -> WARN. This is
        # the genuine no-op case the lint is meant to discourage.
        policy = {
            "rules": [{"id": "app_core-old", "skills": ["x"], "paths": ["~/repos/app_core"]}]
        }
        with _Harness(repo_roots=()):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(results[0].status, "warn")
        self.assertEqual(results[0].details["redundant"][0]["registry_id"], "app_core")

    def test_does_not_warn_when_literal_is_a_strict_subset_of_resolved_set(self) -> None:
        # BUG 1 regression: a single home-form literal on a `~/repos`-rooted box is a
        # strict SUBSET of the id's resolved set (which also includes the /srv
        # re-rooting). Swapping it for `repos: [app_core]` would WIDEN the match set, so
        # the lint must STAY SILENT — keeping the literal is intentional.
        policy = {
            "rules": [{"id": "app_core-narrow", "skills": ["x"], "paths": ["~/repos/app_core"]}]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(
            results[0].status,
            "pass",
            f"subset literal must not be flagged: {results[0].details}",
        )

    def test_srv_only_literal_is_a_subset_and_not_flagged(self) -> None:
        # Mirrors the live cloudflare-local/web-analytics-local/saas-audit-local
        # case: a single /srv-form spelling is still only ONE member of the id's
        # two-spelling resolved set, so it is a subset -> no warn.
        policy = {
            "rules": [
                {"id": "srv-only", "skills": ["x"], "paths": ["/srv/skillbox/repos/app_core"]}
            ]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(results[0].status, "pass", results[0].details)

    def test_passes_when_rule_uses_repos_not_literal_paths(self) -> None:
        # The migrated form: no literal paths -> nothing to flag.
        policy = {
            "rules": [
                {"id": "app_core-local", "skills": ["x"], "repos": ["app_core", "app_core-server"]},
            ]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(results[0].status, "pass")

    def test_does_not_flag_unregistered_literal_path(self) -> None:
        policy = {
            "rules": [
                {"id": "misc", "skills": ["x"], "paths": ["~/repos/not-in-registry"]},
            ]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(results[0].status, "pass")

    def test_warn_is_advisory_not_a_failure(self) -> None:
        # A WARN must never be a FAIL: raw paths stay supported. Use a genuine
        # full-enumeration warn case (the no-repo-root single-spelling id).
        policy = {"rules": [{"id": "r", "skills": ["x"], "paths": ["~/repos/app_core"]}]}
        with _Harness(repo_roots=()):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(results[0].status, "warn")
        self.assertNotEqual(results[0].status, "fail")

    def test_no_registry_is_a_pass(self) -> None:
        # No override installed + an env file that yields no entries -> pass.
        sv._registry_doctor_module_override = lambda: None  # type: ignore[attr-defined]
        try:
            policy = {"rules": [{"id": "r", "skills": ["x"], "paths": ["/srv/skillbox/repos/app_core"]}]}
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        finally:
            sv.__dict__.pop("_registry_doctor_module_override", None)
        self.assertEqual(results[0].status, "pass")


if __name__ == "__main__":
    unittest.main()
