"""`family/v1` manifest and `family-snapshot/v1` receipt schema contracts.

Two disciplines carry most of these tests. **Never lie**: a receipt may not
claim evidence it does not carry, within a document and across the pair. **L3 is
structurally impossible**: identity material cannot appear in either document at
any depth, so a resumed family necessarily comes up with a fresh identity rather
than impersonating its ancestor.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import family_schema as FS  # noqa: E402

SCHEMA_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "family_schema.py"
SWEET_POTATO = ROOT_DIR / "workspace" / "families" / "sweet-potato.yaml"

COMMIT = "a" * 40
DIGEST = "b" * 64
SNAPSHOT_ID = "0" * 32


def manifest_document(**overrides: object) -> dict:
    document = {
        "schema": FS.FAMILY_MANIFEST_SCHEMA,
        "name": "sweet-potato",
        "members": [
            {
                "repo": "sweet-potato",
                "path": "repos/sweet-potato",
                "branch": "main",
                "commit": COMMIT,
            }
        ],
        "services": [
            {"id": "postgres", "kind": "database", "quiesce": "drain", "version": "16.3"}
        ],
        "data_mounts": [{"id": "pgdata", "path": "/srv/data/postgres"}],
        "enrollments": [
            {"id": "tailscale", "kind": "tailscale", "revoke_on_pause": True}
        ],
    }
    document.update(overrides)
    return document


def receipt_document(**overrides: object) -> dict:
    document = {
        "schema": FS.FAMILY_SNAPSHOT_SCHEMA,
        "family": "sweet-potato",
        "snapshot_id": SNAPSHOT_ID,
        "created_at": "2026-08-16T19:00:00Z",
        "members": [
            {"repo": "sweet-potato", "commit": COMMIT, "dirty": False, "capsule_digest": None}
        ],
        "services": [{"id": "postgres", "version": "16.3"}],
        "volume_snapshots": [
            {"mount_id": "pgdata", "snapshot_id": "snap-abc", "size_bytes": 1024}
        ],
        "enrollments_revoked": ["tailscale"],
        "resumed_from": None,
    }
    document.update(overrides)
    return document


class SchemaTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> FS.FamilySchemaError:
        with self.assertRaises(FS.FamilySchemaError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class ManifestTests(SchemaTestCase):
    """What a family declares."""

    def test_a_complete_manifest_round_trips(self) -> None:
        manifest = FS.FamilyManifest.from_mapping(manifest_document())
        self.assertEqual("sweet-potato", manifest.name)
        self.assertEqual(("pgdata",), manifest.mount_ids)
        again = FS.FamilyManifest.from_mapping(manifest.to_payload())
        self.assertEqual(manifest, again)

    def test_unknown_and_missing_fields_are_refused(self) -> None:
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(manifest_document(region="nyc3")),
        )
        trimmed = manifest_document()
        del trimmed["data_mounts"]
        self.assert_refused(
            "manifest_invalid", lambda: FS.FamilyManifest.from_mapping(trimmed)
        )

    def test_a_family_must_declare_a_member(self) -> None:
        # A family with no repos is not a family; it is a machine.
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(manifest_document(members=[])),
        )

    def test_the_wrong_schema_is_refused(self) -> None:
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(manifest_document(schema="family/v2")),
        )

    def test_identifiers_are_pattern_checked(self) -> None:
        for override in (
            {"name": "Sweet_Potato"},
            {"name": ""},
            {"members": [{"repo": "ok", "path": "p", "branch": "main", "commit": "abc"}]},
            {"members": [{"repo": "ok", "path": "p", "branch": "", "commit": None}]},
        ):
            self.assert_refused(
                "manifest_invalid",
                lambda override=override: FS.FamilyManifest.from_mapping(
                    manifest_document(**override)
                ),
            )

    def test_an_unpinned_member_is_allowed(self) -> None:
        # `commit: null` means "track the branch"; pinning happens at snapshot.
        manifest = FS.FamilyManifest.from_mapping(
            manifest_document(
                members=[
                    {"repo": "a", "path": "repos/a", "branch": "main", "commit": None}
                ]
            )
        )
        self.assertIsNone(manifest.members[0].commit)

    def test_quiesce_none_must_be_declared_from_the_closed_set(self) -> None:
        for mode in FS.QUIESCE_MODES:
            FS.FamilyManifest.from_mapping(
                manifest_document(
                    services=[
                        {"id": "s", "kind": "database", "quiesce": mode, "version": None}
                    ]
                )
            )
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(
                    services=[
                        {"id": "s", "kind": "database", "quiesce": "maybe", "version": None}
                    ]
                )
            ),
        )

    def test_enrollment_kinds_are_closed(self) -> None:
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(
                    enrollments=[
                        {"id": "x", "kind": "carrier-pigeon", "revoke_on_pause": True}
                    ]
                )
            ),
        )

    def test_duplicate_identities_are_refused(self) -> None:
        member = {"repo": "a", "path": "repos/a", "branch": "main", "commit": None}
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(members=[member, dict(member)])
            ),
        )
        mount = {"id": "pgdata", "path": "/srv/data/postgres"}
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(data_mounts=[mount, dict(mount)])
            ),
        )

    def test_collections_are_bounded(self) -> None:
        many = [
            {"repo": f"r{index}", "path": f"repos/r{index}", "branch": "main", "commit": None}
            for index in range(FS.MAX_MEMBERS + 1)
        ]
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(manifest_document(members=many)),
        )

    def test_a_data_mount_must_be_an_absolute_path(self) -> None:
        self.assert_refused(
            "manifest_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(data_mounts=[{"id": "d", "path": "relative/data"}])
            ),
        )


class SecretPathTests(SchemaTestCase):
    """Declared paths are screened with the same table capsules use."""

    def test_a_secret_shaped_component_anywhere_is_refused(self) -> None:
        for path in (
            "repos/secrets/app",
            "repos/app/private_key",
            "repos/app/api-key",
            "credentials/app",
        ):
            self.assert_refused(
                "secret_shaped_path",
                lambda path=path: FS.FamilyManifest.from_mapping(
                    manifest_document(
                        members=[
                            {"repo": "a", "path": path, "branch": "main", "commit": None}
                        ]
                    )
                ),
            )

    def test_a_secret_shaped_data_mount_is_refused(self) -> None:
        self.assert_refused(
            "secret_shaped_path",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(
                    data_mounts=[{"id": "d", "path": "/srv/secrets/postgres"}]
                )
            ),
        )

    def test_path_traversal_is_refused(self) -> None:
        self.assert_refused(
            "document_invalid",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(
                    members=[
                        {"repo": "a", "path": "repos/../etc", "branch": "main", "commit": None}
                    ]
                )
            ),
        )

    def test_the_screen_is_the_shared_capsule_implementation(self) -> None:
        # One implementation of "what looks like a secret", shared with
        # sbp_test_capsule rather than re-stated here.
        from runtime_manager import sbp_test_capsule

        self.assertIs(FS.secret_shaped_paths, sbp_test_capsule.secret_shaped_paths)


class IdentityMaterialTests(SchemaTestCase):
    """L3 must be impossible to write down, not merely omitted."""

    def test_identity_keys_are_refused_in_a_manifest(self) -> None:
        for key in ("machine_id", "node_key", "tailscale_key", "auth_key", "session"):
            self.assert_refused(
                "identity_material_forbidden",
                lambda key=key: FS.FamilyManifest.from_mapping(
                    manifest_document(**{key: "x"})
                ),
            )

    def test_identity_keys_are_refused_at_depth(self) -> None:
        # An allowlist protects the shapes it knows; this protects the ones a
        # future field might introduce.
        nested = manifest_document()
        nested["members"][0]["node_key"] = "nkey-abc"
        self.assert_refused(
            "identity_material_forbidden",
            lambda: FS.FamilyManifest.from_mapping(nested),
        )

    def test_key_matching_ignores_case_and_separators(self) -> None:
        for spelling in ("Machine-ID", "MACHINE_ID", "machineId", "machineid"):
            self.assert_refused(
                "identity_material_forbidden",
                lambda spelling=spelling: FS.FamilyManifest.from_mapping(
                    manifest_document(**{spelling: "x"})
                ),
            )

    def test_secret_shaped_keys_are_refused(self) -> None:
        for key in ("api_token", "db_password", "access_key"):
            self.assert_refused(
                "identity_material_forbidden",
                lambda key=key: FS.FamilyManifest.from_mapping(
                    manifest_document(**{key: "x"})
                ),
            )

    def test_a_receipt_is_screened_too(self) -> None:
        self.assert_refused(
            "identity_material_forbidden",
            lambda: FS.FamilySnapshotReceipt.from_mapping(
                receipt_document(machine_id="abc")
            ),
        )

    def test_an_over_deep_document_is_refused(self) -> None:
        document: object = "leaf"
        for _ in range(12):
            document = {"nest": document}
        self.assert_refused(
            "document_too_deep", lambda: FS.assert_no_identity_material(document)
        )


class ReceiptTests(SchemaTestCase):
    """What a snapshot attests, and what it may not claim."""

    def test_a_receipt_round_trips_byte_stably(self) -> None:
        receipt = FS.FamilySnapshotReceipt.from_mapping(receipt_document())
        again = FS.FamilySnapshotReceipt.from_mapping(receipt.to_payload())
        self.assertEqual(receipt, again)
        self.assertEqual(receipt.canonical_bytes(), again.canonical_bytes())
        self.assertEqual(receipt.digest(), again.digest())
        self.assertRegex(receipt.digest(), r"^[0-9a-f]{64}$")

    def test_the_digest_changes_with_the_content(self) -> None:
        first = FS.FamilySnapshotReceipt.from_mapping(receipt_document())
        second = FS.FamilySnapshotReceipt.from_mapping(
            receipt_document(created_at="2026-08-16T19:00:01Z")
        )
        self.assertNotEqual(first.digest(), second.digest())

    def test_a_dirty_member_without_a_capsule_is_refused(self) -> None:
        # The receipt would be asserting a working tree was captured when
        # nothing was.
        self.assert_refused(
            "receipt_incomplete",
            lambda: FS.FamilySnapshotReceipt.from_mapping(
                receipt_document(
                    members=[
                        {
                            "repo": "sweet-potato",
                            "commit": COMMIT,
                            "dirty": True,
                            "capsule_digest": None,
                        }
                    ]
                )
            ),
        )

    def test_a_clean_member_carrying_a_capsule_is_refused(self) -> None:
        # The mirror image: describing work that did not happen.
        self.assert_refused(
            "receipt_invalid",
            lambda: FS.FamilySnapshotReceipt.from_mapping(
                receipt_document(
                    members=[
                        {
                            "repo": "sweet-potato",
                            "commit": COMMIT,
                            "dirty": False,
                            "capsule_digest": DIGEST,
                        }
                    ]
                )
            ),
        )

    def test_a_dirty_member_with_a_capsule_is_accepted(self) -> None:
        receipt = FS.FamilySnapshotReceipt.from_mapping(
            receipt_document(
                members=[
                    {
                        "repo": "sweet-potato",
                        "commit": COMMIT,
                        "dirty": True,
                        "capsule_digest": DIGEST,
                    }
                ]
            )
        )
        self.assertTrue(receipt.members[0].dirty)
        self.assertEqual(DIGEST, receipt.members[0].capsule_digest)

    def test_a_receipt_cannot_be_resumed_from_itself(self) -> None:
        self.assert_refused(
            "receipt_invalid",
            lambda: FS.FamilySnapshotReceipt.from_mapping(
                receipt_document(resumed_from=SNAPSHOT_ID)
            ),
        )

    def test_a_resume_link_is_carried(self) -> None:
        receipt = FS.FamilySnapshotReceipt.from_mapping(
            receipt_document(resumed_from="1" * 32)
        )
        self.assertEqual("1" * 32, receipt.resumed_from)

    def test_identifiers_and_timestamps_are_pattern_checked(self) -> None:
        for override in (
            {"snapshot_id": "nope"},
            {"created_at": "2026-08-16 19:00:00"},
            {"created_at": "2026-08-16T19:00:00+00:00"},
            {"members": [{"repo": "a", "commit": "abc", "dirty": False, "capsule_digest": None}]},
        ):
            self.assert_refused(
                "receipt_invalid",
                lambda override=override: FS.FamilySnapshotReceipt.from_mapping(
                    receipt_document(**override)
                ),
            )

    def test_a_receipt_must_attest_a_member(self) -> None:
        self.assert_refused(
            "receipt_incomplete",
            lambda: FS.FamilySnapshotReceipt.from_mapping(receipt_document(members=[])),
        )

    def test_volume_sizes_are_bounded(self) -> None:
        for size in (-1, True, "1024", 2**60):
            self.assert_refused(
                "receipt_invalid",
                lambda size=size: FS.FamilySnapshotReceipt.from_mapping(
                    receipt_document(
                        volume_snapshots=[
                            {"mount_id": "pgdata", "snapshot_id": "s", "size_bytes": size}
                        ]
                    )
                ),
            )


class CoverageTests(SchemaTestCase):
    """The never-lie rule across the pair of documents."""

    def manifest(self, **overrides: object) -> FS.FamilyManifest:
        return FS.FamilyManifest.from_mapping(manifest_document(**overrides))

    def receipt(self, **overrides: object) -> FS.FamilySnapshotReceipt:
        return FS.FamilySnapshotReceipt.from_mapping(receipt_document(**overrides))

    def test_a_covering_receipt_is_accepted(self) -> None:
        FS.verify_receipt_covers_manifest(self.manifest(), self.receipt())

    def test_a_receipt_for_another_family_is_refused(self) -> None:
        self.assert_refused(
            "receipt_invalid",
            lambda: FS.verify_receipt_covers_manifest(
                self.manifest(), self.receipt(family="other-family")
            ),
        )

    def test_an_omitted_data_mount_is_refused(self) -> None:
        # On resume that mount comes back empty, and the receipt gave no warning.
        manifest = self.manifest(
            data_mounts=[
                {"id": "pgdata", "path": "/srv/data/postgres"},
                {"id": "uploads", "path": "/srv/data/uploads"},
            ]
        )
        self.assert_refused(
            "receipt_incomplete",
            lambda: FS.verify_receipt_covers_manifest(manifest, self.receipt()),
        )

    def test_an_undeclared_data_mount_is_refused(self) -> None:
        receipt = self.receipt(
            volume_snapshots=[
                {"mount_id": "pgdata", "snapshot_id": "s1", "size_bytes": 1},
                {"mount_id": "ghost", "snapshot_id": "s2", "size_bytes": 1},
            ]
        )
        self.assert_refused(
            "receipt_invalid",
            lambda: FS.verify_receipt_covers_manifest(self.manifest(), receipt),
        )

    def test_an_omitted_member_is_refused(self) -> None:
        manifest = self.manifest(
            members=[
                {"repo": "sweet-potato", "path": "repos/a", "branch": "main", "commit": None},
                {"repo": "htma", "path": "repos/htma", "branch": "main", "commit": None},
            ]
        )
        self.assert_refused(
            "receipt_incomplete",
            lambda: FS.verify_receipt_covers_manifest(manifest, self.receipt()),
        )

    def test_an_unrevoked_enrollment_is_refused(self) -> None:
        # Pause means destroy; an enrollment left live outlives the family and
        # lets a restored machine inherit reachability.
        self.assert_refused(
            "receipt_incomplete",
            lambda: FS.verify_receipt_covers_manifest(
                self.manifest(), self.receipt(enrollments_revoked=[])
            ),
        )

    def test_an_enrollment_not_marked_for_revocation_need_not_appear(self) -> None:
        manifest = self.manifest(
            enrollments=[{"id": "registry", "kind": "registry", "revoke_on_pause": False}]
        )
        FS.verify_receipt_covers_manifest(
            manifest, self.receipt(enrollments_revoked=[])
        )


class YamlAndFixtureTests(SchemaTestCase):
    """The shipped sweet-potato family is the first real fixture."""

    def test_the_sweet_potato_family_validates(self) -> None:
        self.assertTrue(SWEET_POTATO.is_file(), SWEET_POTATO)
        manifest = FS.load_manifest(SWEET_POTATO)
        self.assertEqual("sweet-potato", manifest.name)
        self.assertEqual(
            ["cycle-chef", "htma", "htma_server", "recipe-ios", "sweet-potato"],
            sorted(member.repo for member in manifest.members),
        )
        self.assertEqual(("pgdata", "uploads"), manifest.mount_ids)
        self.assertTrue(all(e.revoke_on_pause for e in manifest.enrollments))

    def test_the_fixture_carries_no_absolute_operator_path(self) -> None:
        # Sanitized for a public remote: repo-relative member paths, no host or
        # home directory.
        text = SWEET_POTATO.read_text(encoding="utf-8")
        for leak in ("/Users/", "/home/", "ts.net", "100.", "@"):
            self.assertNotIn(leak, text, leak)

    def test_a_manifest_parses_from_text(self) -> None:
        manifest = FS.load_manifest_text(SWEET_POTATO.read_text(encoding="utf-8"))
        self.assertEqual("sweet-potato", manifest.name)

    def test_malformed_yaml_is_one_refusal(self) -> None:
        self.assert_refused(
            "manifest_invalid", lambda: FS.load_manifest_text("name: [unclosed")
        )

    def test_a_missing_file_is_refused(self) -> None:
        self.assert_refused(
            "manifest_invalid", lambda: FS.load_manifest(ROOT_DIR / "no-such-family.yaml")
        )

    def test_the_yaml_entry_point_says_so_when_pyyaml_is_absent(self) -> None:
        with mock.patch.object(FS, "yaml", None):
            self.assert_refused(
                "yaml_unavailable", lambda: FS.load_manifest_text("schema: family/v1")
            )

    def test_a_mapping_can_be_validated_without_pyyaml(self) -> None:
        # Standard-library-first: PyYAML gates the YAML door only.
        with mock.patch.object(FS, "yaml", None):
            manifest = FS.FamilyManifest.from_mapping(manifest_document())
        self.assertEqual("sweet-potato", manifest.name)


class ContractTests(SchemaTestCase):
    """Invariants, including that this module stays schema-only."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\(\s*"([a-z_]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - FS.REFUSAL_CODES)

    def test_the_module_executes_nothing(self) -> None:
        # The brief scopes this bead to schema work; snapshot/pause/resume
        # execution belongs to sibling beads.
        source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        for banned in ("subprocess", "socket", "os.system", "popen", "doctl", "shutil"):
            self.assertNotIn(banned, source, banned)

    def test_the_schema_names_match_the_bead(self) -> None:
        self.assertEqual("family/v1", FS.FAMILY_MANIFEST_SCHEMA)
        self.assertEqual("family-snapshot/v1", FS.FAMILY_SNAPSHOT_SCHEMA)

    def test_a_receipt_payload_is_json_serializable(self) -> None:
        receipt = FS.FamilySnapshotReceipt.from_mapping(receipt_document())
        json.dumps(receipt.to_payload())

    def test_refusals_render_a_machine_readable_payload(self) -> None:
        error = self.assert_refused(
            "secret_shaped_path",
            lambda: FS.FamilyManifest.from_mapping(
                manifest_document(
                    members=[
                        {"repo": "a", "path": "repos/secrets/x", "branch": "main", "commit": None}
                    ]
                )
            ),
        )
        payload = error.to_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual("secret_shaped_path", payload["error_code"])
        self.assertEqual(["repos/secrets/x"], payload["paths"])


if __name__ == "__main__":
    unittest.main()
