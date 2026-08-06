from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/orb/deploy_preflight.py"
SPEC = importlib.util.spec_from_file_location("deploy_preflight", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class OrbDeployPreflightTests(unittest.TestCase):
    def _manifest(self, root: Path, name: str, source_commit: str) -> Path:
        directory = root / name
        directory.mkdir()
        archive = directory / "skillbox.tar.gz"
        archive.write_bytes(f"{name} exact archive\n".encode())
        manifest = directory / "deploy.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "client_id": "project-orb-test",
                    "source_commit": source_commit,
                    "payload_tree_sha256": hashlib.sha256(f"{name}-tree".encode()).hexdigest(),
                    "archive": archive.name,
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "active_profiles": ["core"],
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_exact_artifact_preflight_is_deterministic_private_and_apply_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._manifest(root, "current", "a" * 40)
            previous = self._manifest(root, "previous", "b" * 40)
            secret = "agent-socket-value-that-must-not-appear"
            first = DEPLOY.collect(
                DEPLOY.DEFAULT_OVERLAY,
                current,
                box_id="project-orb-test",
                previous_deploy_manifest=previous,
                env={"SSH_AUTH_SOCK": secret},
            )
            second = DEPLOY.collect(
                DEPLOY.DEFAULT_OVERLAY,
                current,
                box_id="project-orb-test",
                previous_deploy_manifest=previous,
                env={"SSH_AUTH_SOCK": secret},
            )
            receipt = root / "receipts/preflight.json"
            DEPLOY.write_receipt(receipt, first)

            self.assertEqual(first, second)
            self.assertEqual(first["state"], "configured")
            self.assertEqual(first["artifact"]["source_commit"], "a" * 40)
            self.assertEqual(first["production_apply"], "forbidden")
            by_id = {step["id"]: step for step in first["steps"]}
            self.assertEqual(by_id["health"]["state"], "planned")
            self.assertEqual(by_id["rollback"]["artifact"]["source_commit"], "b" * 40)
            self.assertEqual(by_id["apply"]["authority"], "operator_only")
            self.assertNotIn(secret, json.dumps(first))
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(receipt.parent.stat().st_mode), 0o700)

    def test_missing_credential_and_rollback_are_typed_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._manifest(root, "current", "a" * 40)
            previous = self._manifest(root, "previous", "b" * 40)
            missing_rollback = DEPLOY.collect(
                DEPLOY.DEFAULT_OVERLAY,
                current,
                box_id="project-orb-test",
                env={"SSH_AUTH_SOCK": "configured"},
            )
            with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "ambient-value"}, clear=True):
                missing_credential = DEPLOY.collect(
                    DEPLOY.DEFAULT_OVERLAY,
                    current,
                    box_id="project-orb-test",
                    previous_deploy_manifest=previous,
                    env={},
                )

        self.assertEqual(missing_rollback["reason_code"], "ROLLBACK_UNPROVEN")
        self.assertEqual(missing_credential["reason_code"], "CREDENTIAL_UNAVAILABLE")

    def test_rejects_short_commit_and_overlay_authority_widening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            short = self._manifest(root, "short", "abc123")
            with self.assertRaisesRegex(RuntimeError, "full source_commit"):
                DEPLOY.collect(
                    DEPLOY.DEFAULT_OVERLAY,
                    short,
                    box_id="project-orb-test",
                    env={},
                )

            widened = json.loads(DEPLOY.DEFAULT_OVERLAY.read_text())
            widened["authority"]["ordinary_project_orb"] = "production_apply"
            widened_path = root / "widened.json"
            widened_path.write_text(json.dumps(widened), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "violates"):
                DEPLOY._load_overlay(widened_path)

    def test_artifact_archive_cannot_escape_or_symlink_outside_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(root, "current", "a" * 40)
            outside = root / "outside.tar.gz"
            outside.write_bytes(b"outside\n")
            payload = json.loads(manifest.read_text())
            payload["archive"] = "../outside.tar.gz"
            payload["archive_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "stay beside"):
                DEPLOY.collect(DEPLOY.DEFAULT_OVERLAY, manifest, box_id="project-orb-test")

            link = manifest.parent / "linked.tar.gz"
            link.symlink_to(outside)
            payload["archive"] = link.name
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot be a symlink"):
                DEPLOY.collect(DEPLOY.DEFAULT_OVERLAY, manifest, box_id="project-orb-test")

    def test_receipt_destination_is_confined_to_declared_private_store(self) -> None:
        overlay = DEPLOY._load_overlay(DEPLOY.DEFAULT_OVERLAY)
        payload = {"artifact": {"archive_sha256": "a" * 64}}
        expected_root = (ROOT / overlay["receipt_store"]["root"]).resolve()
        self.assertEqual(
            DEPLOY.receipt_destination(overlay, payload, None),
            expected_root / f"preflight-{'a' * 64}.json",
        )
        with self.assertRaisesRegex(ValueError, "declared receipt store"):
            DEPLOY.receipt_destination(overlay, payload, Path("/tmp/escape.json"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("operator owned\n", encoding="utf-8")
            symlink = root / "receipt.json"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                DEPLOY.write_receipt(symlink, payload)
            self.assertEqual(target.read_text(encoding="utf-8"), "operator owned\n")

    def test_receipt_store_rejects_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            linked_store = root / "receipt-store"
            linked_store.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "ancestors must be real directories"):
                DEPLOY.write_receipt(
                    linked_store / "preflight.json",
                    {"ok": True},
                    store_root=linked_store,
                )
            self.assertFalse((outside / "preflight.json").exists())

    def test_receipt_serialization_failure_opens_no_store_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "receipt-store"
            with (
                mock.patch.object(DEPLOY, "_open_receipt_parent") as open_parent,
                self.assertRaises(TypeError),
            ):
                DEPLOY.write_receipt(
                    store / "preflight.json",
                    {"not_json": object()},
                    store_root=store,
                )
            open_parent.assert_not_called()

    def test_receipt_write_remains_confined_when_store_path_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "receipt-store"
            store.mkdir(mode=0o700)
            moved_store = root / "receipt-store-opened"
            outside = root / "outside"
            outside.mkdir()
            destination = store / "preflight.json"
            real_replace = os.replace

            def replace_after_swap(
                source: str,
                target: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                store.rename(moved_store)
                store.symlink_to(outside, target_is_directory=True)
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with mock.patch.object(DEPLOY.os, "replace", side_effect=replace_after_swap):
                DEPLOY.write_receipt(destination, {"ok": True}, store_root=store)

            self.assertFalse((outside / destination.name).exists())
            self.assertEqual(
                json.loads((moved_store / destination.name).read_text(encoding="utf-8")),
                {"ok": True},
            )
            self.assertEqual(stat.S_IMODE((moved_store / destination.name).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
