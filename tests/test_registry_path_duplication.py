"""Lint coverage for the registry-path-duplication doctor check (bead y8w.3).

Raw ``paths:`` stay fully supported for back-compat, but a literal path that a
registry id already covers is redundant: the repo could be named ONCE via
``repos: [<id>]`` and have its per-machine path derived from
registry/repos.yaml + machines.yaml. ``validate_registry_path_duplication``
WARNS (never FAILS) on such a path so the duplication is visibly discouraged.

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
    {"id": "htma", "path": "~/repos/htma", "bucket": "app"},
    {"id": "htma-server", "path": "~/repos/htma_server", "bucket": "backend"},
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
    def test_warns_on_literal_path_covered_by_registry_id(self) -> None:
        policy = {
            "rules": [
                {"id": "htma-old", "skills": ["x"], "paths": ["/srv/skillbox/repos/htma"]},
            ]
        }
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.code, REGISTRY_PATH_DUPLICATION_CODE)
        self.assertIn("htma-old", result.message)
        self.assertIn("'htma'", result.message)
        self.assertIn("repos: [<id>]", result.message)
        redundant = result.details["redundant"]
        self.assertEqual(redundant[0]["rule"], "htma-old")
        self.assertEqual(redundant[0]["registry_id"], "htma")

    def test_passes_when_rule_uses_repos_not_literal_paths(self) -> None:
        # The migrated form: no literal paths -> nothing to flag.
        policy = {
            "rules": [
                {"id": "htma-local", "skills": ["x"], "repos": ["htma", "htma-server"]},
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
        # A WARN must never be a FAIL: raw paths stay supported.
        policy = {"rules": [{"id": "r", "skills": ["x"], "paths": ["/srv/skillbox/repos/htma"]}]}
        with _Harness(repo_roots=("/srv/skillbox/repos",)):
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        self.assertNotEqual(results[0].status, "fail")

    def test_no_registry_is_a_pass(self) -> None:
        # No override installed + an env file that yields no entries -> pass.
        sv._registry_doctor_module_override = lambda: None  # type: ignore[attr-defined]
        try:
            policy = {"rules": [{"id": "r", "skills": ["x"], "paths": ["/srv/skillbox/repos/htma"]}]}
            results = validate_registry_path_duplication(policy, policy_path="/p.yaml")
        finally:
            sv.__dict__.pop("_registry_doctor_module_override", None)
        self.assertEqual(results[0].status, "pass")


if __name__ == "__main__":
    unittest.main()
