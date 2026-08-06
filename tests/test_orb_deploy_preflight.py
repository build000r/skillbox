from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
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
            missing_credential = DEPLOY.collect(
                DEPLOY.DEFAULT_OVERLAY,
                current,
                box_id="project-orb-test",
                previous_deploy_manifest=previous,
                env={},
            )

        self.assertEqual(missing_rollback["reason_code"], "ROLLBACK_UNPROVEN")
        self.assertEqual(missing_credential["reason_code"], "CREDENTIAL_UNAVAILABLE")

    def test_explicit_empty_env_does_not_fall_back_to_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._manifest(root, "current", "a" * 40)
            previous = self._manifest(root, "previous", "b" * 40)
            prior = os.environ.get("SSH_AUTH_SOCK")
            os.environ["SSH_AUTH_SOCK"] = "/configured/in-parent-process"
            try:
                explicit_empty = DEPLOY.collect(
                    DEPLOY.DEFAULT_OVERLAY,
                    current,
                    box_id="project-orb-test",
                    previous_deploy_manifest=previous,
                    env={},
                )
            finally:
                if prior is None:
                    os.environ.pop("SSH_AUTH_SOCK", None)
                else:
                    os.environ["SSH_AUTH_SOCK"] = prior

        self.assertEqual(explicit_empty["reason_code"], "CREDENTIAL_UNAVAILABLE")
        self.assertEqual(
            explicit_empty["credential_preflight"],
            [{"name": "SSH_AUTH_SOCK", "configured": False}],
        )

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
        traversal = expected_root / "nested" / ".." / ".." / "outside" / "receipt.json"
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            DEPLOY.receipt_destination(overlay, payload, traversal)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("operator owned\n", encoding="utf-8")
            symlink = root / "receipt.json"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                DEPLOY.write_receipt(symlink, payload)
            self.assertEqual(target.read_text(encoding="utf-8"), "operator owned\n")

    def test_receipt_writer_rejects_symlinked_ancestor_and_broad_parent(self) -> None:
        payload = {"state": "configured"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                DEPLOY.write_receipt(linked / "receipt.json", payload)
            self.assertFalse((outside / "receipt.json").exists())

            broad = root / "broad"
            broad.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                DEPLOY.write_receipt(broad / "receipt.json", payload)
            self.assertFalse((broad / "receipt.json").exists())

            private = root / "private"
            private.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "parent traversal"):
                DEPLOY.write_receipt(private / "nested" / ".." / "receipt.json", payload)
            self.assertFalse((private / "receipt.json").exists())

    def test_receipt_mode_is_exact_under_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipts" / "receipt.json"
            program = (
                "import importlib.util,json,os,stat,sys;"
                "spec=importlib.util.spec_from_file_location('deploy_preflight',sys.argv[1]);"
                "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
                "os.umask(0o777);module.write_receipt(module.Path(sys.argv[2]),{'state':'ready'});"
                "print(oct(stat.S_IMODE(os.stat(sys.argv[2]).st_mode)))"
            )
            result = subprocess.run(
                [sys.executable, "-c", program, str(MODULE_PATH), str(receipt)],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.stdout.strip(), "0o600")

    def test_receipt_writer_keeps_open_directory_when_path_is_swapped(self) -> None:
        payload = {"state": "configured"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "receipts"
            moved = root / "receipts-opened"
            outside = root / "outside"
            parent.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            real_replace = os.replace

            def swap_then_replace(source, destination, *, src_dir_fd, dst_dir_fd):
                parent.rename(moved)
                parent.symlink_to(outside, target_is_directory=True)
                return real_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with mock.patch.object(DEPLOY.os, "replace", side_effect=swap_then_replace):
                DEPLOY.write_receipt(parent / "receipt.json", payload)

            self.assertEqual(
                json.loads((moved / "receipt.json").read_text(encoding="utf-8")),
                payload,
            )
            self.assertFalse((outside / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
