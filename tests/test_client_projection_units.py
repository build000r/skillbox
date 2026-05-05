from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import shared as SHARED  # noqa: E402


def _load_yaml_text(text: str) -> dict[str, object]:
    raw = SHARED.require_yaml("parse test yaml").safe_load(text)
    if not isinstance(raw, dict):
        raise AssertionError("expected YAML mapping")
    return raw


def _write_projection_root(root: Path) -> None:
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / ".env.example").write_text(
        "\n".join(
            [
                "SKILLBOX_WORKSPACE_ROOT=/workspace",
                "SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients",
                "SKILLBOX_CLIENTS_HOST_ROOT=./clients",
                "SKILLBOX_LOG_ROOT=/workspace/logs",
                "SKILLBOX_HOME_ROOT=/home/sandbox",
                "SKILLBOX_MONOSERVER_ROOT=/monoserver",
                "SKILLBOX_STATE_ROOT=./.skillbox-state",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "workspace" / "runtime.yaml").write_text(
        "version: 2\n"
        "selection: {}\n"
        "clients:\n"
        "  - id: personal\n"
        "    default_cwd: /monoserver\n"
        "  - id: other\n"
        "    default_cwd: /other\n"
        "core:\n"
        "  repos: []\n"
        "  artifacts: []\n"
        "  skills: []\n"
        "  services: []\n"
        "  logs: []\n"
        "  checks: []\n",
        encoding="utf-8",
    )
    (root / "workspace" / "persistence.yaml").write_text(
        "version: 1\n"
        "state_root_env: SKILLBOX_STATE_ROOT\n"
        "targets:\n"
        "  local:\n"
        "    provider: local\n"
        "    default_state_root: ./.skillbox-state\n"
        "bindings: []\n",
        encoding="utf-8",
    )


def _write_client_overlay(root: Path, client_id: str = "personal") -> Path:
    client_dir = root / "clients" / client_id
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "overlay.yaml").write_text(
        "version: 1\n"
        "client:\n"
        f"  id: {client_id}\n"
        "  label: Personal\n"
        "  default_cwd: /monoserver\n"
        "  context:\n"
        "    cwd_match: [/monoserver]\n",
        encoding="utf-8",
    )
    (client_dir / "skill-repos.lock.json").write_text('{"skills": []}\n', encoding="utf-8")
    (client_dir / "plans").mkdir()
    (client_dir / "plans" / "INDEX.md").write_text("# Plans\n", encoding="utf-8")
    (client_dir / "skills" / "personal-skill").mkdir(parents=True)
    (client_dir / "skills" / "personal-skill" / "SKILL.md").write_text("# Personal Skill\n", encoding="utf-8")
    return client_dir


def _projection_model(root: Path) -> dict[str, object]:
    skill_config = root / "workspace" / "skill-repos.yaml"
    skill_lock = root / "workspace" / "skill-repos.lock.json"
    skill_config.write_text("version: 2\nskill_repos: []\n", encoding="utf-8")
    skill_lock.write_text('{"version": 1, "skills": []}\n', encoding="utf-8")
    return {
        "active_clients": ["personal"],
        "active_profiles": ["core"],
        "selection": {"default_client": "personal"},
        "storage": {
            "state_root": str(root / ".skillbox-state"),
            "raw_state_root": "./.skillbox-state",
        },
        "env": {
            "SKILLBOX_CLIENTS_HOST_ROOT": str(root / "clients"),
            "SKILLBOX_SWIMMERS_AUTH_TOKEN": "redacted",
        },
        "clients": [
            {
                "id": "personal",
                "default_cwd": "/monoserver",
                "_host_path": str(root / "clients" / "personal"),
            }
        ],
        "repos": [],
        "artifacts": [],
        "env_files": [],
        "services": [],
        "logs": [],
        "checks": [],
        "skills": [
            {
                "id": "default-skills",
                "kind": "skill-repo-set",
                "skill_repos_config": "/workspace/workspace/skill-repos.yaml",
                "skill_repos_config_host_path": str(skill_config),
                "lock_path": "/workspace/workspace/skill-repos.lock.json",
                "lock_path_host_path": str(skill_lock),
            }
        ],
    }


class ClientProjectionUnitTests(unittest.TestCase):
    def test_collect_client_projection_files_includes_overlay_and_sanitized_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            _write_client_overlay(root)

            files, overlay_mode = SHARED.collect_client_projection_files(root, _projection_model(root), "personal")

            self.assertEqual(overlay_mode, "overlay")
            self.assertIn("workspace/runtime.yaml", files)
            self.assertIn(".env.example", files)
            self.assertIn("workspace/persistence.yaml", files)
            self.assertIn("workspace/clients/personal/overlay.yaml", files)
            self.assertIn("workspace/clients/personal/skill-repos.lock.json", files)
            self.assertIn("workspace/clients/personal/plans/INDEX.md", files)
            self.assertIn("workspace/clients/personal/skills/personal-skill/SKILL.md", files)
            self.assertIn("workspace/skill-repos.yaml", files)
            self.assertIn("workspace/skill-repos.lock.json", files)
            self.assertIn("runtime-model.json", files)

            runtime_text = files["workspace/runtime.yaml"]["content"]
            self.assertEqual(_load_yaml_text(runtime_text)["selection"]["default_client"], "personal")
            model_text = files["runtime-model.json"]["content"]
            model_payload = json.loads(model_text)
            self.assertEqual(model_payload["storage"]["state_root"], "./.skillbox-state")
            self.assertNotIn("SKILLBOX_CLIENTS_HOST_ROOT", model_payload["env"])
            self.assertNotIn("SKILLBOX_SWIMMERS_AUTH_TOKEN", model_text)
            self.assertNotIn("_host_path", model_text)

    def test_collect_client_projection_files_without_overlay_keeps_inline_client_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            model = _projection_model(root)
            model["skills"] = []

            files, overlay_mode = SHARED.collect_client_projection_files(root, model, "personal")

            self.assertEqual(overlay_mode, "inline")
            runtime_doc = _load_yaml_text(files["workspace/runtime.yaml"]["content"])
            self.assertEqual([client["id"] for client in runtime_doc["clients"]], ["personal"])

    def test_collect_client_projection_files_includes_packaged_skill_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            manifest_path = root / "workspace" / "manifest.txt"
            sources_config_path = root / "workspace" / "sources.yaml"
            bundle_dir = root / "workspace" / "bundles"
            bundle_path = bundle_dir / "alpha.zip"
            manifest_path.write_text("alpha\n", encoding="utf-8")
            sources_config_path.write_text("sources: []\n", encoding="utf-8")
            bundle_dir.mkdir()
            (bundle_dir / "README.md").write_text("# Bundles\n", encoding="utf-8")
            bundle_path.write_text("zip bytes do not matter for projection copy\n", encoding="utf-8")
            model = _projection_model(root)
            model["skills"] = [
                {
                    "id": "bundled",
                    "kind": "packaged-skill-set",
                    "manifest": "/workspace/workspace/manifest.txt",
                    "manifest_host_path": str(manifest_path),
                    "sources_config": "/workspace/workspace/sources.yaml",
                    "sources_config_host_path": str(sources_config_path),
                    "bundle_dir": "/workspace/workspace/bundles",
                    "bundle_dir_host_path": str(bundle_dir),
                }
            ]

            with mock.patch(
                "runtime_manager.validation.collect_skill_inventory",
                return_value={
                    "expected_skills": ["alpha"],
                    "bundles": {"alpha": {"filename": "alpha.zip", "host_path": str(bundle_path)}},
                },
            ):
                files, overlay_mode = SHARED.collect_client_projection_files(root, model, "personal")

            self.assertEqual(overlay_mode, "inline")
            self.assertIn("workspace/manifest.txt", files)
            self.assertIn("workspace/sources.yaml", files)
            self.assertIn("workspace/bundles/README.md", files)
            self.assertIn("workspace/bundles/alpha.zip", files)
            runtime_model = json.loads(files["runtime-model.json"]["content"])
            self.assertEqual(runtime_model["skills"][0]["kind"], "packaged-skill-set")

            with mock.patch(
                "runtime_manager.validation.collect_skill_inventory",
                return_value={"expected_skills": ["alpha"], "bundles": {}},
            ):
                with self.assertRaises(RuntimeError) as missing_bundle:
                    SHARED.collect_client_projection_files(root, model, "personal")
            self.assertIn("missing bundles", str(missing_bundle.exception))

    def test_project_client_bundle_writes_projection_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "projection"
            runtime_model_file = {
                "type": "text",
                "destination_rel": "runtime-model.json",
                "content": "{}\n",
            }
            filtered_model = {
                "active_profiles": ["core"],
                "active_clients": ["personal"],
                "selection": {"default_client": "personal"},
            }

            with (
                mock.patch.object(SHARED, "build_runtime_model", return_value={"clients": []}),
                mock.patch("runtime_manager.validation.normalize_active_profiles", return_value=["core"]),
                mock.patch("runtime_manager.validation.normalize_active_clients", return_value=["personal"]),
                mock.patch("runtime_manager.validation.filter_model", return_value=filtered_model),
                mock.patch.object(
                    SHARED,
                    "collect_client_projection_files",
                    return_value=({"runtime-model.json": runtime_model_file}, "inline"),
                ),
            ):
                payload = SHARED.project_client_bundle(
                    root,
                    "personal",
                    profiles=["core"],
                    output_dir_arg=str(output_dir),
                    dry_run=False,
                    force=False,
                )

            metadata_path = output_dir / "projection.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["client_id"], "personal")
            self.assertEqual(payload["overlay_mode"], "inline")
            self.assertEqual(payload["file_count"], 1)
            self.assertEqual(payload["payload_tree_sha256"], metadata["payload_tree_sha256"])
            self.assertTrue((output_dir / "runtime-model.json").is_file())
            self.assertTrue(any(action.endswith("projection/projection.json") for action in payload["actions"]))

    def test_materialize_and_load_projection_bundle_validates_payload_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            _write_client_overlay(root)
            files, _overlay_mode = SHARED.collect_client_projection_files(root, _projection_model(root), "personal")
            output_dir = root / "bundle"

            actions, entries = SHARED.materialize_client_projection(
                root,
                output_dir,
                files,
                dry_run=False,
                force=False,
            )
            tree_digest = SHARED.tree_hash(entries)
            projection_payload = {
                "client_id": "personal",
                "payload_tree_sha256": tree_digest,
                "runtime_manifest": "workspace/runtime.yaml",
                "runtime_model": "runtime-model.json",
                "files": [
                    {"path": rel_path, "sha256": digest}
                    for rel_path, digest in sorted(entries)
                ],
            }
            SHARED.write_json_file(output_dir / "projection.json", projection_payload)

            bundle = SHARED.load_client_projection_bundle(output_dir, expected_client_id="personal")

            self.assertTrue(any(action.startswith("copy-file:") for action in actions))
            self.assertEqual(bundle["client_id"], "personal")
            self.assertEqual(bundle["payload_tree_sha256"], tree_digest)
            self.assertEqual(bundle["runtime_manifest_rel"], "workspace/runtime.yaml")
            self.assertEqual(bundle["runtime_model_rel"], "runtime-model.json")

            with self.assertRaises(RuntimeError) as wrong_client:
                SHARED.load_client_projection_bundle(output_dir, expected_client_id="other")
            self.assertIn("not 'other'", str(wrong_client.exception))

            (output_dir / "runtime-model.json").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as bad_hash:
                SHARED.load_client_projection_bundle(output_dir, expected_client_id="personal")
            self.assertIn("hash mismatch", str(bad_hash.exception))

    def test_prepare_client_projection_output_dir_protects_existing_non_projection_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            existing = root / "custom-output"
            existing.mkdir()
            (existing / "note.txt").write_text("manual\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as conflict:
                SHARED.prepare_client_projection_output_dir(root, existing, dry_run=False, force=False)
            self.assertIn("already exists", str(conflict.exception))

            with self.assertRaises(RuntimeError) as protected:
                SHARED.prepare_client_projection_output_dir(root, root.resolve(), dry_run=True, force=True)
            self.assertIn("protected output directory", str(protected.exception))

            (existing / "projection.json").write_text("{}\n", encoding="utf-8")
            actions = SHARED.prepare_client_projection_output_dir(root, existing, dry_run=False, force=True)
            self.assertIn("remove-output-dir: custom-output", actions)
            self.assertTrue(existing.is_dir())
            self.assertFalse((existing / "note.txt").exists())

    def test_load_and_build_blueprinted_client_scaffold_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            blueprint_path = root / "workspace" / "client-blueprints" / "git-service.yaml"
            blueprint_path.parent.mkdir(parents=True, exist_ok=True)
            blueprint_path.write_text(
                "version: 1\n"
                "description: Clone a service repo.\n"
                "variables:\n"
                "  - name: SERVICE_ID\n"
                "    required: true\n"
                "  - name: SERVICE_PATH\n"
                "    default: ${CLIENT_ROOT}/${SERVICE_ID}\n"
                "scaffold:\n"
                "  pack: hybrid\n"
                "client:\n"
                "  default_cwd: ${SERVICE_PATH}\n"
                "  connectors: [slack]\n"
                "  repos:\n"
                "    - id: ${SERVICE_ID}\n"
                "      kind: repo\n"
                "      path: ${SERVICE_PATH}\n"
                "      required: true\n"
                "      profiles: [core]\n"
                "      source: {kind: git, url: https://example.test/repo.git}\n"
                "      sync: {mode: clone-if-missing}\n",
                encoding="utf-8",
            )

            blueprint = SHARED.load_client_blueprint(blueprint_path)
            target_files, scaffold_pack = SHARED.build_blueprinted_client_scaffold_files(
                root_dir=root,
                env_values=SHARED.load_runtime_env(root),
                client_id="acme",
                client_label="Acme",
                client_root="/monoserver/acme",
                client_default_cwd="/monoserver/acme",
                explicit_label=False,
                explicit_default_cwd=False,
                blueprint=blueprint,
                blueprint_assignments=[
                    ("SERVICE_ID", "app"),
                    ("SLACK_CAPABILITIES", "chat,files"),
                    ("SLACK_CHANNELS", "ops,alerts"),
                ],
            )

            self.assertEqual(scaffold_pack, "hybrid")
            overlay_text = target_files[(root / "clients" / "acme" / "overlay.yaml").resolve()]
            overlay_doc = _load_yaml_text(overlay_text)
            client = overlay_doc["client"]
            self.assertEqual(client["default_cwd"], "/monoserver/acme/app")
            self.assertEqual(client["repos"][0]["id"], "app")
            self.assertEqual(client["connectors"][0]["capabilities"], ["chat", "files"])
            self.assertEqual(client["connectors"][0]["scopes"]["channels"], ["ops", "alerts"])
            self.assertIn((root / "clients" / "acme" / "plans" / "INDEX.md").resolve(), target_files)
            self.assertIn((root / "clients" / "acme" / "workflows" / "INDEX.md").resolve(), target_files)

    def test_load_client_blueprint_rejects_invalid_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_path = root / "bad.yaml"

            bad_path.write_text("version: 2\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as bad_version:
                SHARED.load_client_blueprint(bad_path)
            self.assertIn("Unsupported client blueprint version", str(bad_version.exception))

            bad_path.write_text("version: 1\nvariables: nope\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as bad_variables:
                SHARED.load_client_blueprint(bad_path)
            self.assertIn("variables", str(bad_variables.exception))

            bad_path.write_text(
                "version: 1\n"
                "variables:\n"
                "  - name: SERVICE\n"
                "  - name: SERVICE\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as duplicate:
                SHARED.load_client_blueprint(bad_path)
            self.assertIn("Duplicate", str(duplicate.exception))

            with self.assertRaises(RuntimeError) as missing_required:
                SHARED.build_blueprinted_client_scaffold_files(
                    root_dir=root,
                    env_values={"SKILLBOX_CLIENTS_HOST_ROOT": str(root / "clients")},
                    client_id="acme",
                    client_label="Acme",
                    client_root="/monoserver/acme",
                    client_default_cwd="/monoserver/acme",
                    explicit_label=False,
                    explicit_default_cwd=False,
                    blueprint={
                        "variables": [{"name": "REQUIRED_VALUE", "required": True}],
                        "client": {},
                        "scaffold": {},
                    },
                    blueprint_assignments=[],
                )
            self.assertIn("missing required values", str(missing_required.exception))

    def test_scaffold_client_overlay_creates_default_pack_and_rejects_existing_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)

            actions, blueprint = SHARED.scaffold_client_overlay(
                root,
                client_id="acme",
                label=None,
                default_cwd=None,
                root_path="/monoserver/acme",
                blueprint_name=None,
                blueprint_assignments=[],
                dry_run=False,
                force=False,
            )

            overlay_path = root / "clients" / "acme" / "overlay.yaml"
            self.assertIsNone(blueprint)
            self.assertTrue(overlay_path.is_file())
            self.assertTrue((root / "clients" / "acme" / "skill-repos.yaml").is_file())
            self.assertTrue((root / "clients" / "acme" / "plans" / "INDEX.md").is_file())
            self.assertTrue((root / "clients" / "_shared" / "skills" / "domain-planner" / "SKILL.md").is_file())
            self.assertTrue(any(action.startswith("copy-skill-template:") for action in actions))

            with self.assertRaises(RuntimeError) as existing:
                SHARED.scaffold_client_overlay(
                    root,
                    client_id="acme",
                    label=None,
                    default_cwd=None,
                    root_path="/monoserver/acme",
                    blueprint_name=None,
                    blueprint_assignments=[],
                    dry_run=True,
                    force=False,
                )
            self.assertIn("already exists", str(existing.exception))

            with self.assertRaises(RuntimeError) as set_without_blueprint:
                SHARED.scaffold_client_overlay(
                    root,
                    client_id="new-client",
                    label=None,
                    default_cwd=None,
                    root_path="/monoserver/new-client",
                    blueprint_name=None,
                    blueprint_assignments=[("SERVICE_ID", "app")],
                    dry_run=True,
                    force=False,
                )
            self.assertIn("--blueprint", str(set_without_blueprint.exception))

    def test_scaffold_client_overlay_uses_blueprint_metadata_and_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            blueprint_path = root / "workspace" / "client-blueprints" / "service.yaml"
            blueprint_path.parent.mkdir(parents=True, exist_ok=True)
            blueprint_path.write_text(
                "version: 1\n"
                "description: Service blueprint.\n"
                "variables:\n"
                "  - name: SERVICE_ID\n"
                "    required: true\n"
                "scaffold:\n"
                "  pack: skill-builder\n"
                "client:\n"
                "  default_cwd: ${CLIENT_ROOT}/${SERVICE_ID}\n",
                encoding="utf-8",
            )

            actions, blueprint = SHARED.scaffold_client_overlay(
                root,
                client_id="service-client",
                label="Service Client",
                default_cwd=None,
                root_path="/monoserver/service-client",
                blueprint_name="service",
                blueprint_assignments=[("SERVICE_ID", "api")],
                dry_run=False,
                force=False,
            )

            self.assertEqual(blueprint["id"], "service")
            overlay_path = root / "clients" / "service-client" / "overlay.yaml"
            overlay = _load_yaml_text(overlay_path.read_text(encoding="utf-8"))["client"]
            self.assertEqual(overlay["default_cwd"], "/monoserver/service-client/api")
            self.assertIn("workflow_builder", overlay["context"])
            self.assertNotIn("plans", overlay["context"])
            self.assertTrue((root / "clients" / "service-client" / "skills" / "skill-issue" / "SKILL.md").is_file())
            self.assertTrue(any(action.startswith("write-file:") for action in actions))

            force_actions, force_blueprint = SHARED.scaffold_client_overlay(
                root,
                client_id="service-client",
                label="Service Client",
                default_cwd=None,
                root_path="/monoserver/service-client",
                blueprint_name="service",
                blueprint_assignments=[("SERVICE_ID", "api")],
                dry_run=True,
                force=True,
            )
            self.assertEqual(force_blueprint["id"], "service")
            self.assertTrue(any("overlay.yaml" in action for action in force_actions))

    def test_normalize_client_overlay_shape_fills_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_projection_root(root)
            overlay_dir = root / "clients" / "minimal"
            overlay_dir.mkdir(parents=True)
            (overlay_dir / "overlay.yaml").write_text(
                "version: 1\n"
                "client:\n"
                "  id: minimal\n"
                "  default_cwd: /monoserver/minimal\n"
                "  scaffold:\n"
                "    pack: skill-builder\n",
                encoding="utf-8",
            )

            actions = SHARED.normalize_client_overlay_shape(root, overlay_dir)

            overlay = _load_yaml_text((overlay_dir / "overlay.yaml").read_text(encoding="utf-8"))["client"]
            self.assertEqual(overlay["label"], "Minimal")
            self.assertIn("/monoserver/minimal", overlay["context"]["cwd_match"])
            self.assertIn("workflow_builder", overlay["context"])
            self.assertNotIn("plans", overlay["context"])
            self.assertTrue((overlay_dir / "skill-repos.yaml").is_file())
            self.assertTrue((overlay_dir / "skills" / "skill-issue" / "SKILL.md").is_file())
            self.assertTrue(any(action.startswith("normalize-overlay:") for action in actions))

            bad_dir = root / "clients" / "bad"
            bad_dir.mkdir()
            (bad_dir / "overlay.yaml").write_text(
                "version: 1\n"
                "client:\n"
                "  id: bad\n"
                "  context: nope\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError) as bad_context:
                SHARED.normalize_client_overlay_shape(root, bad_dir)
            self.assertIn("client.context", str(bad_context.exception))


if __name__ == "__main__":
    unittest.main()
