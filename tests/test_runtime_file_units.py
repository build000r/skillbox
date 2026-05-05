from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import shared as SHARED  # noqa: E402


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RuntimeFileUnitTests(unittest.TestCase):
    def test_upsert_env_file_values_updates_appends_and_detects_noops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "# keep comment\n"
                "SKILLBOX_CLIENTS_HOST_ROOT=./workspace/clients\n"
                "OTHER = untouched\n"
                "\n",
                encoding="utf-8",
            )

            changed = SHARED.upsert_env_file_values(
                env_path,
                {
                    "SKILLBOX_CLIENTS_HOST_ROOT": "./../skillbox-config/clients",
                    "NEW_VALUE": "enabled",
                },
            )
            unchanged = SHARED.upsert_env_file_values(
                env_path,
                {
                    "SKILLBOX_CLIENTS_HOST_ROOT": "./../skillbox-config/clients",
                    "NEW_VALUE": "enabled",
                },
            )

            self.assertTrue(changed)
            self.assertFalse(unchanged)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "# keep comment\n"
                "SKILLBOX_CLIENTS_HOST_ROOT=./../skillbox-config/clients\n"
                "OTHER = untouched\n"
                "\n"
                "NEW_VALUE=enabled\n",
            )

    def test_bundle_members_validates_roots_paths_and_expected_skill_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            valid_bundle = root / "skill.zip"
            with zipfile.ZipFile(valid_bundle, "w") as archive:
                archive.writestr("my-skill/", "")
                archive.writestr("my-skill/SKILL.md", "# Skill\n")
                archive.writestr("my-skill/references/guide.md", "guide\n")

            archive_root, members = SHARED.bundle_members(valid_bundle, expected_skill_name="my-skill")
            metadata = SHARED.bundle_metadata(valid_bundle, expected_skill_name="my-skill")

            self.assertEqual(archive_root, "my-skill")
            self.assertEqual([rel for rel, _digest in members], ["SKILL.md", "references/guide.md"])
            self.assertEqual(metadata["archive_root"], "my-skill")
            self.assertEqual(metadata["file_count"], 2)

            wrong_root = root / "wrong-root.zip"
            with zipfile.ZipFile(wrong_root, "w") as archive:
                archive.writestr("other-skill/SKILL.md", "# Other\n")
            with self.assertRaises(RuntimeError) as wrong_name:
                SHARED.bundle_members(wrong_root, expected_skill_name="my-skill")
            self.assertIn("expected skill root", str(wrong_name.exception))

            multi_root = root / "multi-root.zip"
            with zipfile.ZipFile(multi_root, "w") as archive:
                archive.writestr("one/SKILL.md", "# One\n")
                archive.writestr("two/SKILL.md", "# Two\n")
            with self.assertRaises(RuntimeError) as multiple:
                SHARED.bundle_members(multi_root)
            self.assertIn("exactly one top-level", str(multiple.exception))

            invalid_member = root / "invalid.zip"
            with zipfile.ZipFile(invalid_member, "w") as archive:
                archive.writestr("../SKILL.md", "# Invalid\n")
            with self.assertRaises(RuntimeError) as invalid:
                SHARED.bundle_members(invalid_member)
            self.assertIn("Invalid bundle member", str(invalid.exception))

            empty_bundle = root / "empty.zip"
            with zipfile.ZipFile(empty_bundle, "w"):
                pass
            with self.assertRaises(RuntimeError) as empty:
                SHARED.bundle_members(empty_bundle)
            self.assertIn("is empty", str(empty.exception))

    def test_connector_csv_and_projection_sanitizers_normalize_inputs(self) -> None:
        self.assertEqual(SHARED.split_csv_values(None), [])
        self.assertEqual(SHARED.split_csv_values("chat, files, ,admin"), ["chat", "files", "admin"])
        self.assertEqual(SHARED.split_csv_values(["chat,files", 42, ""]), ["chat", "files", "42"])
        self.assertEqual(SHARED.split_csv_values(7), ["7"])

        entries, issues = SHARED.normalize_client_connector_entries(
            [
                "slack",
                "",
                {"id": " github ", "capabilities": ["issues,pulls"], "scopes": {"orgs": ["acme"]}},
                {"id": "bad-caps", "capabilities": "issues"},
                {"id": "bad-scopes", "scopes": "orgs"},
                {"capabilities": []},
                123,
            ],
            client_id="personal",
        )

        self.assertEqual([entry["id"] for entry in entries], ["slack", "github", "bad-caps", "bad-scopes"])
        self.assertEqual(entries[1]["capabilities"], ["issues", "pulls"])
        self.assertTrue(any("connectors[2] is empty" in issue for issue in issues))
        self.assertTrue(any("capabilities must be a list" in issue for issue in issues))
        self.assertTrue(any("scopes must be a mapping" in issue for issue in issues))
        self.assertTrue(any("missing id" in issue for issue in issues))
        self.assertTrue(any("got int" in issue for issue in issues))
        self.assertEqual(SHARED.normalize_client_connector_entries("slack,github", client_id="personal")[0], [{"id": "slack"}, {"id": "github"}])
        self.assertTrue(SHARED.normalize_client_connector_entries({"bad": True}, client_id="personal")[1])

        sanitized_source = SHARED.sanitize_projection_source(
            {
                "kind": "file",
                "path": "/host/secret.txt",
                "host_path": "/host/secret.txt",
                "url": "https://example.test/skill.zip",
                "token": "secret",
                "nested": {"_private": True, "host_path": "/host", "safe": "yes"},
            }
        )
        self.assertEqual(sanitized_source["kind"], "file")
        self.assertEqual(sanitized_source["url"], "https://example.test/skill.zip")
        self.assertEqual(sanitized_source["nested"], {"safe": "yes"})
        self.assertNotIn("path", sanitized_source)
        self.assertNotIn("host_path", sanitized_source)
        self.assertNotIn("token", sanitized_source)

        self.assertTrue(SHARED.artifact_source_configured({"source": {"kind": "url", "url": "https://example.test/a.zip"}}))
        self.assertTrue(SHARED.artifact_source_configured({"source": {"kind": "file", "host_path": "/tmp/a"}}))
        self.assertFalse(SHARED.artifact_source_configured({"source": {"kind": "manual"}}))
        self.assertFalse(SHARED.artifact_source_configured({"source": {"kind": "url", "url": ""}}))

    def test_parse_duplicates_blueprints_and_git_dirty_helpers(self) -> None:
        self.assertEqual(
            SHARED.parse_key_value_assignments(["SERVICE_ID=api", "EMPTY="], "--set"),
            [("SERVICE_ID", "api"), ("EMPTY", "")],
        )
        for raw_assignment, expected_message in (
            ("SERVICE_ID", "expects KEY=VALUE"),
            ("bad-key=value", "is invalid"),
            ("SERVICE_ID=api", "Duplicate"),
        ):
            assignments = [raw_assignment]
            if expected_message == "Duplicate":
                assignments = ["SERVICE_ID=api", raw_assignment]
            with self.subTest(raw_assignment=raw_assignment):
                with self.assertRaises(RuntimeError) as ctx:
                    SHARED.parse_key_value_assignments(assignments, "--set")
                self.assertIn(expected_message, str(ctx.exception))

        self.assertEqual(
            SHARED.find_duplicates(
                [
                    {"name": "ask-cascade"},
                    {"name": ""},
                    {"name": "describe"},
                    {"name": "ask-cascade"},
                    {"name": "ask-cascade"},
                ],
                "name",
            ),
            ["ask-cascade"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            blueprint_path = root / "empty.yaml"
            blueprint_path.write_text("", encoding="utf-8")
            empty_blueprint = SHARED.load_client_blueprint(blueprint_path)
            self.assertEqual(empty_blueprint["id"], "empty")
            self.assertEqual(empty_blueprint["variables"], [])
            self.assertEqual(empty_blueprint["client"], {})

            invalid_cases = [
                ("list.yaml", "- nope\n", "Expected a YAML object"),
                ("client.yaml", "version: 1\nclient: nope\n", "Expected `client`"),
                ("scaffold.yaml", "version: 1\nscaffold: nope\n", "Expected `scaffold`"),
                ("variable.yaml", "version: 1\nvariables:\n  - name: bad-name\n", "Invalid client blueprint variable"),
                ("variable-entry.yaml", "version: 1\nvariables:\n  - nope\n", "Expected every variable entry"),
            ]
            for file_name, text, expected_message in invalid_cases:
                with self.subTest(file_name=file_name):
                    path = root / file_name
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(RuntimeError) as ctx:
                        SHARED.load_client_blueprint(path)
                    self.assertIn(expected_message, str(ctx.exception))

            with self.assertRaises(RuntimeError) as missing:
                SHARED.load_client_blueprint(root / "missing.yaml")
            self.assertIn("not found", str(missing.exception))

        with mock.patch.object(
            SHARED,
            "run_command",
            return_value=_completed(stdout=" M file.txt\nR  old.txt -> new.txt\n?? untracked.txt\n\n"),
        ):
            self.assertEqual(SHARED.git_dirty_paths(Path("/repo")), ["file.txt", "new.txt", "untracked.txt"])
        with mock.patch.object(SHARED, "run_command", return_value=_completed(1, stderr="not a repo")):
            self.assertEqual(SHARED.git_dirty_paths(Path("/repo")), [])

    def test_git_and_private_repo_helpers_are_deterministic_with_mocked_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "private"

            with mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": True}):
                self.assertFalse(SHARED.ensure_git_repo(repo_path))

            commands: list[list[str]] = []

            def fake_run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                del cwd
                commands.append(args)
                return _completed()

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch.object(SHARED, "run_command", side_effect=fake_run_command),
            ):
                self.assertTrue(SHARED.ensure_git_repo(repo_path))
            self.assertEqual(commands, [["git", "init"], ["git", "branch", "-M", "main"]])

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch.object(SHARED, "run_command", return_value=_completed(1, stderr="git init failed")),
            ):
                with self.assertRaises(RuntimeError) as init_failed:
                    SHARED.ensure_git_repo(root / "bad-private")
            self.assertIn("git init failed", str(init_failed.exception))

            with (
                mock.patch("runtime_manager.runtime_ops.git_repo_state", return_value={"git": False}),
                mock.patch.object(
                    SHARED,
                    "run_command",
                    side_effect=[_completed(), _completed(1, stderr="branch failed")],
                ),
            ):
                with self.assertRaises(RuntimeError) as branch_failed:
                    SHARED.ensure_git_repo(root / "bad-branch")
            self.assertIn("branch failed", str(branch_failed.exception))

    def test_private_repo_inference_and_client_migration_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_clients = root / "workspace" / "clients"
            source_clients.mkdir(parents=True)
            (source_clients / "personal").mkdir()
            (source_clients / "personal" / "overlay.yaml").write_text("version: 1\n", encoding="utf-8")
            (source_clients / "_shared").mkdir()
            (source_clients / "_shared" / "README.md").write_text("shared\n", encoding="utf-8")
            (root / "default-skills" / "clients" / "personal" / "bundle").mkdir(parents=True)
            (root / "default-skills" / "clients" / "personal" / "bundle" / "SKILL.md").write_text("bundle\n", encoding="utf-8")
            (root / "skills" / "clients" / "personal").mkdir(parents=True)
            (root / "skills" / "clients" / "personal" / "skill.md").write_text("skill\n", encoding="utf-8")

            with (
                mock.patch.object(SHARED, "client_configs_host_root", return_value=(root / "workspace" / "clients")),
                mock.patch.object(SHARED, "compile_persistence_summary", side_effect=RuntimeError("bad persistence")),
            ):
                self.assertIsNone(SHARED.inferred_private_target_dir(root, {"SKILLBOX_CLIENTS_HOST_ROOT": "./workspace/clients"}))

            private_clients = (root / ".." / "skillbox-config" / "clients").resolve()
            with (
                mock.patch.object(SHARED, "client_configs_host_root", return_value=private_clients),
                mock.patch.object(SHARED, "compile_persistence_summary", return_value={"state_root": str(root / ".state")}),
                mock.patch.object(SHARED, "storage_binding_by_id", return_value={"relative_path": "clients"}),
            ):
                self.assertEqual(SHARED.inferred_private_target_dir(root, {}), private_clients.parent)

            missing_actions = SHARED.migrate_client_subtree(
                root,
                root / "missing",
                root / "target-clients",
                subdir_name="skills",
            )
            self.assertEqual(missing_actions, [])

            target_clients = root / "target-clients"
            (target_clients / "personal" / "skills" / "skill.md").mkdir(parents=True)
            subtree_actions = SHARED.migrate_client_subtree(
                root,
                root / "skills" / "clients",
                target_clients,
                subdir_name="skills",
            )
            self.assertTrue(any(action.startswith("skip-client-skills-entry-existing:") for action in subtree_actions))

            with (
                mock.patch.object(SHARED, "load_runtime_env", return_value={}),
                mock.patch.object(SHARED, "client_configs_host_root", return_value=source_clients),
                mock.patch.object(SHARED, "ensure_git_repo", return_value=True),
                mock.patch.object(SHARED, "normalize_client_overlay_shape", return_value=["normalize-client: personal"]),
                mock.patch.object(SHARED, "upsert_env_file_values", return_value=True),
            ):
                payload = SHARED.init_private_repo(root, target_dir_arg="private-config")

            target_dir = root / "private-config"
            expected_clients_host_root = SHARED.normalize_host_rel_path(root, (target_dir / "clients").resolve())
            self.assertEqual(payload["target_dir"], str(target_dir.resolve()))
            self.assertEqual(payload["migrated_clients"], ["_shared", "personal"])
            self.assertEqual(payload["env_updates"]["SKILLBOX_CLIENTS_HOST_ROOT"], expected_clients_host_root)
            self.assertTrue(any(action.startswith("git-init: ") and action.endswith("private-config") for action in payload["actions"]))
            self.assertTrue(any(action.startswith("copy-client: ") and action.endswith("private-config/clients/_shared") for action in payload["actions"]))
            self.assertIn("normalize-client: personal", payload["actions"])
            self.assertTrue((target_dir / "clients" / "_shared" / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
