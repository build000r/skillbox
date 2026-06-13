"""Overlay-declaration registry + validation (repos-sbp-overlay-semantics-vq0.3).

Before this slice, overlay tags were enumerated from rules with NO declaration
point and NO validation: a typo (``overlay: marketng``) silently created a ghost
overlay that never matched and failed closed. This slice adds:

* a top-level ``overlays:`` registry in skill-scope.yaml (the declaration point),
* ``validate_overlay_declarations`` -- a lint asserting every rule ``overlay:``
  tag references a declared overlay,
* ``skill_visibility`` helpers that read the registry and flag overlay-state
  entries naming an UNDECLARED overlay as an AUDIT WARNING (they filter nothing).

This suite covers the lint (green on the live policy, red on a ghost tag, the
empty/unused/parse edges) and the visibility-layer helpers + audit warning.

TESTS ONLY -- never imports or edits runtime code beyond the public API.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.validation import (  # noqa: E402
    OVERLAY_DECLARATION_CODE,
    validate_overlay_declarations,
    validate_overlay_declarations_file,
)
from runtime_manager import skill_visibility as sv  # noqa: E402


def _real_skill_scope_path() -> Path:
    candidates = [
        ROOT_DIR.parent / "skillbox-config" / "skill-scope.yaml",
        ROOT_DIR.parent.parent / "skillbox-config" / "skill-scope.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _policy(overlays, rules) -> dict:
    return {"overlays": overlays, "rules": rules}


class OverlayDeclarationLintTests(unittest.TestCase):
    def _statuses(self, results) -> list[str]:
        return [r.status for r in results]

    def test_lint_green_on_real_skill_scope_yaml(self) -> None:
        """The live policy must already declare every overlay tag its rules use."""
        path = _real_skill_scope_path()
        self.assertTrue(path.is_file(), f"expected skill-scope.yaml at {path}")
        results = validate_overlay_declarations_file(path)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0].code, OVERLAY_DECLARATION_CODE)
        self.assertEqual(
            results[0].status,
            "pass",
            f"live skill-scope.yaml has an undeclared overlay tag: "
            f"{results[0].message} :: {results[0].details}",
        )
        # The live registry declares marketing, and the rules use exactly it.
        self.assertEqual(results[0].details["declared"], ["marketing"])
        self.assertEqual(results[0].details["rule_overlay_tags"], ["marketing"])

    def test_lint_green_when_every_rule_tag_is_declared(self) -> None:
        policy = _policy(
            overlays=[{"name": "marketing", "default": "off"}],
            rules=[
                {"id": "mk-frontend", "overlay": "marketing", "skills": ["seo"]},
                {"id": "no-overlay-rule", "skills": ["smart"]},
            ],
        )
        results = validate_overlay_declarations(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)

    def test_lint_red_on_undeclared_ghost_tag_names_offender_and_registry(self) -> None:
        """The typo case: a rule tags `marketng`, the registry declares `marketing`."""
        policy = _policy(
            overlays=[{"name": "marketing"}],
            rules=[{"id": "mk-typo", "overlay": "marketng", "skills": ["seo"]}],
        )
        results = validate_overlay_declarations(policy, policy_path="/fake/skill-scope.yaml")
        self.assertEqual(self._statuses(results), ["fail"], results)
        self.assertEqual(results[0].code, OVERLAY_DECLARATION_CODE)
        self.assertIn("marketng", results[0].details["undeclared"])
        # The failure names the offending tag, the rule id, and the declared list.
        blob = results[0].message + str(results[0].details)
        self.assertIn("marketng", blob)
        self.assertIn("mk-typo", blob)
        self.assertIn("marketing", blob)
        self.assertIn("/fake/skill-scope.yaml", results[0].message)
        self.assertEqual(results[0].details["offending_rules"]["marketng"], ["mk-typo"])

    def test_lint_groups_multiple_rules_under_one_ghost_tag(self) -> None:
        policy = _policy(
            overlays=[{"name": "marketing"}],
            rules=[
                {"id": "a", "overlay": "ghost", "skills": ["x"]},
                {"id": "b", "overlay": "ghost", "skills": ["y"]},
            ],
        )
        results = validate_overlay_declarations(policy)
        self.assertEqual(self._statuses(results), ["fail"])
        self.assertEqual(results[0].details["offending_rules"]["ghost"], ["a", "b"])

    def test_declared_but_unused_overlay_is_advisory_not_failure(self) -> None:
        """A registry entry declared ahead of its rules is a PASS (advisory only)."""
        policy = _policy(
            overlays=[{"name": "marketing"}, {"name": "future-mode"}],
            rules=[{"id": "mk", "overlay": "marketing", "skills": ["seo"]}],
        )
        results = validate_overlay_declarations(policy)
        self.assertEqual(self._statuses(results), ["pass"], results[0].details)
        self.assertEqual(results[0].details["declared_but_unused"], ["future-mode"])
        self.assertIn("future-mode", results[0].message)

    def test_empty_policy_is_pass(self) -> None:
        results = validate_overlay_declarations({"rules": []})
        self.assertEqual(self._statuses(results), ["pass"])
        self.assertIn("no overlay surface", results[0].message)

    def test_mapping_and_bare_string_declaration_shapes_accepted(self) -> None:
        # Mapping form.
        mapping_policy = {
            "overlays": {"marketing": {"description": "d"}},
            "rules": [{"id": "mk", "overlay": "marketing", "skills": ["seo"]}],
        }
        self.assertEqual(
            [r.status for r in validate_overlay_declarations(mapping_policy)], ["pass"]
        )
        # Bare-string list form.
        bare_policy = {
            "overlays": ["marketing"],
            "rules": [{"id": "mk", "overlay": "marketing", "skills": ["seo"]}],
        }
        self.assertEqual(
            [r.status for r in validate_overlay_declarations(bare_policy)], ["pass"]
        )

    def test_missing_file_is_pass_and_bad_mapping_is_fail(self) -> None:
        missing = validate_overlay_declarations_file("/nope/skill-scope.yaml")
        self.assertEqual([r.status for r in missing], ["pass"])
        not_mapping = validate_overlay_declarations(["not", "a", "mapping"])  # type: ignore[arg-type]
        self.assertEqual([r.status for r in not_mapping], ["pass"])


class OverlayRegistryVisibilityHelperTests(unittest.TestCase):
    """The skill_visibility readers that back the CLI list + audit warning."""

    def _model_with_clients_root(self, base: Path, policy_yaml: str) -> dict:
        clients_root = base / "config" / "clients"
        clients_root.mkdir(parents=True)
        (clients_root.parent / "skill-scope.yaml").write_text(policy_yaml, encoding="utf-8")
        return {
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
            "clients": [],
            "skills": [],
        }

    def test_declared_records_and_names_read_registry(self) -> None:
        import tempfile

        policy = (
            "version: 1\n"
            "overlays:\n"
            "  - name: marketing\n"
            "    description: GTM mode\n"
            "    default: off\n"
            "  - name: research\n"
            "    default: on\n"
            "rules:\n"
            "  - id: mk\n"
            "    overlay: marketing\n"
            "    skills: [seo]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model_with_clients_root(Path(tmp), policy)
            records = sv.declared_overlay_records(model)
            by_name = {r["name"]: r for r in records}
            self.assertEqual(sorted(by_name), ["marketing", "research"])
            self.assertEqual(by_name["marketing"]["description"], "GTM mode")
            self.assertTrue(by_name["marketing"]["default_off"])  # off
            self.assertFalse(by_name["research"]["default_off"])  # on
            self.assertEqual(sv.declared_overlays(model), {"marketing", "research"})
            self.assertEqual(sv.rule_overlay_tags(model), {"marketing"})

    def test_undeclared_active_overlay_is_flagged_when_registry_exists(self) -> None:
        import os
        import tempfile
        from unittest import mock

        policy = (
            "version: 1\n"
            "overlays:\n"
            "  - name: marketing\n"
            "rules:\n"
            "  - id: mk\n"
            "    overlay: marketing\n"
            "    skills: [seo]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model_with_clients_root(Path(tmp), policy)
            with mock.patch.dict(
                os.environ, {"SKILLBOX_OVERLAYS": "marketing,marketng"}, clear=False
            ):
                # marketing is declared; marketng (typo) is the ghost.
                self.assertEqual(sv.undeclared_active_overlays(model), ["marketng"])

    def test_no_registry_means_no_undeclared_warnings(self) -> None:
        """With no `overlays:` block there is nothing to validate against."""
        import os
        import tempfile
        from unittest import mock

        policy = (
            "version: 1\n"
            "rules:\n"
            "  - id: mk\n"
            "    overlay: marketing\n"
            "    skills: [seo]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            model = self._model_with_clients_root(Path(tmp), policy)
            with mock.patch.dict(
                os.environ, {"SKILLBOX_OVERLAYS": "anything,goes"}, clear=False
            ):
                self.assertEqual(sv.declared_overlays(model), set())
                self.assertEqual(sv.undeclared_active_overlays(model), [])


if __name__ == "__main__":
    unittest.main()
