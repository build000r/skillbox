"""Tests for WG-004 lockfile schema enrichment."""
from __future__ import annotations

import os
import sys
import unittest

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from runtime_manager.distribution.lockfile import (  # noqa: E402
    DEFAULT_SKILL_SOURCE,
    DistributorManifestLockEntry,
    LockfileSchemaError,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from runtime_manager.validation import lock_skill_map  # noqa: E402


class TestLockfileSchema(unittest.TestCase):
    def test_emit_parse_round_trip_with_distributor_data(self) -> None:
        entries = [
            SkillLockEntry(
                name="audit-plans",
                source="repo",
                repo="build000r/skills",
                declared_ref="main",
                resolved_commit="abc123",
                install_tree_sha="tree-audit",
            ),
            SkillLockEntry(
                name="deploy",
                source="distributor",
                distributor_id="acme-skills",
                version=7,
                bundle_sha256="bundle-deploy",
                install_tree_sha="tree-deploy",
                pinned_by="distributor",
                pin_reason="rollback issue in v8",
            ),
        ]
        distributor_manifests = {
            "acme-skills": DistributorManifestLockEntry(
                manifest_version=14,
                fetched_at="2026-04-21T10:00:00Z",
                signature_verified=True,
            )
        }

        payload = emit_lockfile(
            entries,
            distributor_manifests,
            version=3,
            config_sha="cfg123",
            synced_at="2026-04-21T10:01:00Z",
        )

        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["config_sha"], "cfg123")
        self.assertIn("distributor_manifests", payload)
        self.assertEqual(
            payload["distributor_manifests"]["acme-skills"]["manifest_version"],
            14,
        )
        self.assertEqual(payload["skills"][1]["source"], "distributor")
        self.assertEqual(payload["skills"][1]["distributor_id"], "acme-skills")
        self.assertEqual(payload["skills"][1]["version"], 7)

        parsed = parse_lockfile(payload)
        self.assertEqual(parsed.version, 3)
        self.assertEqual(parsed.skills[0].source, "repo")
        self.assertEqual(parsed.skills[1].source, "distributor")
        self.assertEqual(parsed.skills[1].pinned_by, "distributor")
        self.assertEqual(parsed.distributor_manifests["acme-skills"].signature_verified, True)
        self.assertEqual(parsed.to_dict(), payload)

    def test_parse_legacy_lockfile_defaults_source_to_repo(self) -> None:
        legacy = {
            "version": 2,
            "config_sha": "legacy-sha",
            "synced_at": "2026-04-20T15:00:00Z",
            "skills": [
                {
                    "name": "deploy",
                    "repo": "build000r/skills",
                    "declared_ref": "main",
                    "resolved_commit": "deadbeef",
                    "install_tree_sha": "legacy-tree",
                }
            ],
        }

        parsed = parse_lockfile(legacy)
        self.assertEqual(parsed.version, 2)
        self.assertEqual(len(parsed.skills), 1)
        self.assertEqual(parsed.skills[0].name, "deploy")
        self.assertEqual(parsed.skills[0].source, DEFAULT_SKILL_SOURCE)
        self.assertEqual(parsed.distributor_manifests, {})

        emitted = parsed.to_dict()
        self.assertEqual(emitted["skills"][0]["source"], "repo")
        self.assertEqual(emitted["skills"][0]["repo"], "build000r/skills")

    def test_parse_mixed_lockfile_payload(self) -> None:
        raw = {
            "version": 3,
            "config_sha": "mixed-sha",
            "synced_at": "2026-04-21T09:00:00Z",
            "distributor_manifests": {
                "acme-skills": {
                    "manifest_version": 14,
                    "fetched_at": "2026-04-21T08:59:00Z",
                    "signature_verified": True,
                }
            },
            "skills": [
                {
                    "name": "audit-plans",
                    "repo": "build000r/skills",
                    "declared_ref": "main",
                    "resolved_commit": "abc",
                    "install_tree_sha": "tree-audit",
                },
                {
                    "name": "deploy",
                    "source": "distributor",
                    "distributor_id": "acme-skills",
                    "version": 7,
                    "bundle_sha256": "bundle-deploy",
                    "install_tree_sha": "tree-deploy",
                    "pinned_by": "manifest_floor",
                    "pin_reason": "CVE-2026-xxxx",
                },
            ],
        }

        parsed = parse_lockfile(raw)
        self.assertEqual(len(parsed.skills), 2)
        self.assertEqual(parsed.skills[0].source, "repo")
        self.assertEqual(parsed.skills[1].source, "distributor")
        self.assertEqual(parsed.skills[1].version, 7)
        self.assertEqual(parsed.skills[1].pin_reason, "CVE-2026-xxxx")
        self.assertEqual(parsed.distributor_manifests["acme-skills"].manifest_version, 14)

    def test_legacy_payload_remains_compatible_with_validation_helpers(self) -> None:
        legacy = {
            "version": 2,
            "skills": [
                {
                    "name": "deploy",
                    "repo": "build000r/skills",
                    "declared_ref": "main",
                    "resolved_commit": "deadbeef",
                    "install_tree_sha": "legacy-tree",
                }
            ],
        }
        mapping = lock_skill_map(legacy)
        self.assertIn("deploy", mapping)

    def test_invalid_distributor_manifest_types_raise(self) -> None:
        with self.assertRaises(LockfileSchemaError):
            parse_lockfile(
                {
                    "version": 3,
                    "config_sha": "sha",
                    "synced_at": "2026-04-21T10:00:00Z",
                    "skills": [],
                    "distributor_manifests": {
                        "acme-skills": {
                            "manifest_version": "14",
                            "fetched_at": "2026-04-21T10:00:00Z",
                            "signature_verified": True,
                        }
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
