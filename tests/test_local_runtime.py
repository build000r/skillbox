"""Tests for local-runtime bridge, profile, and lifecycle features."""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

from scripts.lib import runtime_model

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
MANAGE_MODULE = SourceFileLoader(
    "skillbox_manage",
    str((ROOT_DIR / ".env-manager" / "manage.py").resolve()),
).load_module()

filter_model = MANAGE_MODULE.filter_model
normalize_active_clients = MANAGE_MODULE.normalize_active_clients
normalize_active_profiles = MANAGE_MODULE.normalize_active_profiles
bridge_expected_outputs = MANAGE_MODULE.bridge_expected_outputs
bridge_freshness = MANAGE_MODULE.bridge_freshness
bridge_id_map = MANAGE_MODULE.bridge_id_map
bridge_outputs_state = MANAGE_MODULE.bridge_outputs_state
local_runtime_error = MANAGE_MODULE.local_runtime_error
task_success_state = MANAGE_MODULE.task_success_state
validate_bridges = MANAGE_MODULE.validate_bridges
validate_local_runtime_profiles = MANAGE_MODULE.validate_local_runtime_profiles


def _write_local_runtime_fixture(repo: Path, *, with_bridge_outputs: bool = False) -> None:
    """Scaffold a minimal runtime fixture with local-runtime bridge declarations."""
    (repo / "workspace" / "clients" / "personal").mkdir(parents=True)
    (repo / "defaults").mkdir(exist_ok=True)
    (repo / ".env.example").write_text(
        textwrap.dedent(f"""\
            SKILLBOX_WORKSPACE_ROOT={repo / "workspace"}
            SKILLBOX_REPOS_ROOT={repo / "workspace" / "repos"}
            SKILLBOX_LOG_ROOT={repo / "workspace" / "logs"}
            SKILLBOX_HOME_ROOT={repo / "workspace" / "home"}
            SKILLBOX_CLIENTS_HOST_ROOT=./workspace/clients
            SKILLBOX_MONOSERVER_HOST_ROOT=..
            SKILLBOX_MONOSERVER_ROOT={repo}
        """),
        encoding="utf-8",
    )
    (repo / "workspace" / "runtime.yaml").write_text(
        textwrap.dedent(f"""\
            version: 2
            selection: {{}}
            core:
              repos:
                - id: skillbox-self
                  path: {repo / "workspace"}
                  required: true
                  profiles:
                    - core
              checks:
                - id: workspace-root
                  type: path_exists
                  path: {repo / "workspace"}
                  required: true
                  profiles:
                    - core
        """),
        encoding="utf-8",
    )
    (repo / "workspace" / "clients" / "personal" / "overlay.yaml").write_text(
        textwrap.dedent(f"""\
            client:
              id: personal
              label: Personal
              default_cwd: {repo}

              repos:
                - id: sweet-potato
                  kind: repo
                  repo_path: {repo}/sweet-potato
                  profiles:
                    - local-minimal

              bridges:
                - id: local-minimal-bridge
                  env_tier: local
                  legacy_targets:
                    - sweet-potato
                    - htma_server
                    - htma
                  output_root: {repo}/env-out
                  emit_stubs: false
                  profiles:
                    - local-minimal

              tasks:
                - id: env-bridge-local-minimal
                  kind: bootstrap
                  bridge_id: local-minimal-bridge
                  profiles:
                    - local-minimal
                  command: echo bridge-ran

              env_files:
                - id: sweet-potato-env
                  repo_id: sweet-potato
                  target_path: {repo}/sweet-potato/.env.local
                  required: true
                  profiles:
                    - local-minimal
                  source:
                    kind: file
                    source_path: {repo}/env-out/sweet-potato/local.env
                  sync:
                    mode: write

              services:
                - id: spaps
                  kind: http
                  repo_id: sweet-potato
                  profiles:
                    - local-minimal
                  depends_on: []
                  bootstrap_tasks:
                    - env-bridge-local-minimal
                  start_modes:
                    - reuse
                    - fresh
                  healthcheck:
                    type: http
                    url: http://localhost:3301/health

                - id: htma_server
                  kind: http
                  profiles:
                    - local-minimal
                  depends_on:
                    - spaps
                  bootstrap_tasks:
                    - env-bridge-local-minimal
                  start_modes:
                    - reuse
                  healthcheck:
                    type: http
                    url: http://localhost:8000/health

                - id: htma
                  kind: http
                  profiles:
                    - local-minimal
                  depends_on:
                    - spaps
                    - htma_server
                  bootstrap_tasks:
                    - env-bridge-local-minimal
                  start_modes:
                    - reuse
                  healthcheck:
                    type: http
                    url: http://localhost:3000

              checks:
                - id: personal-root
                  type: path_exists
                  path: {repo}
                  required: true
                  profiles:
                    - core
        """),
        encoding="utf-8",
    )

    if with_bridge_outputs:
        for target in ("sweet-potato", "htma_server", "htma"):
            out_dir = repo / "env-out" / target
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "local.env").write_text(f"# generated for {target}\n", encoding="utf-8")


class BridgeModelCompilationTests(unittest.TestCase):
    """WG-002: Bridge records compile into the runtime model."""

    def test_build_model_includes_bridges_from_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            _write_local_runtime_fixture(repo)
            model = runtime_model.build_runtime_model(repo)

            bridges = {b["id"]: b for b in model["bridges"]}
            self.assertIn("local-minimal-bridge", bridges)
            bridge = bridges["local-minimal-bridge"]
            self.assertEqual(bridge["env_tier"], "local")
            self.assertEqual(bridge["legacy_targets"], ["sweet-potato", "htma_server", "htma"])
            self.assertIn("output_root_host_path", bridge)
            self.assertEqual(bridge["client"], "personal")

    def test_filter_model_scopes_bridges_by_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            _write_local_runtime_fixture(repo)
            model = runtime_model.build_runtime_model(repo)

            # With local-minimal profile, bridge should be included
            active = normalize_active_profiles(["local-minimal"])
            clients = normalize_active_clients(model, ["personal"])
            filtered = filter_model(model, active, clients)
            self.assertEqual(len(filtered["bridges"]), 1)

            # With only core profile, bridge should be excluded
            active_core = normalize_active_profiles([])
            filtered_core = filter_model(model, active_core, clients)
            self.assertEqual(len(filtered_core["bridges"]), 0)

    def test_filter_model_includes_local_services_for_local_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            _write_local_runtime_fixture(repo)
            model = runtime_model.build_runtime_model(repo)

            active = normalize_active_profiles(["local-minimal"])
            clients = normalize_active_clients(model, ["personal"])
            filtered = filter_model(model, active, clients)

            service_ids = {s["id"] for s in filtered["services"]}
            self.assertEqual(service_ids, {"spaps", "htma_server", "htma"})

    def test_active_profiles_includes_core_plus_local(self) -> None:
        active = normalize_active_profiles(["local-minimal"])
        self.assertIn("core", active)
        self.assertIn("local-minimal", active)


class BridgeFreshnessTests(unittest.TestCase):
    """WG-003: Bridge freshness checking."""

    def test_bridge_expected_outputs_generates_correct_paths(self) -> None:
        bridge = {
            "output_root_host_path": "/env-out",
            "legacy_targets": ["sweet-potato", "htma_server"],
            "env_tier": "local",
        }
        outputs = bridge_expected_outputs(bridge)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0], Path("/env-out/sweet-potato/local.env"))
        self.assertEqual(outputs[1], Path("/env-out/htma_server/local.env"))

    def test_bridge_outputs_state_ok_when_all_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for target in ("sweet-potato", "htma_server"):
                (root / target).mkdir()
                (root / target / "local.env").write_text("ok\n")
            bridge = {
                "output_root_host_path": str(root),
                "legacy_targets": ["sweet-potato", "htma_server"],
                "env_tier": "local",
            }
            state = bridge_outputs_state(bridge)
            self.assertEqual(state["state"], "ok")

    def test_bridge_outputs_state_missing_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "sweet-potato").mkdir()
            (root / "sweet-potato" / "local.env").write_text("ok\n")
            bridge = {
                "output_root_host_path": str(root),
                "legacy_targets": ["sweet-potato", "htma_server"],
                "env_tier": "local",
            }
            state = bridge_outputs_state(bridge)
            self.assertEqual(state["state"], "missing")
            self.assertEqual(len(state["missing"]), 1)
            self.assertIn("htma_server", state["missing"][0])

    def test_bridge_freshness_returns_fresh_when_outputs_newer_than_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overlay = root / "overlay.yaml"
            overlay.write_text("old\n")

            import time
            time.sleep(0.05)

            for target in ("sp",):
                (root / target).mkdir()
                (root / target / "local.env").write_text("new\n")

            bridge = {
                "output_root_host_path": str(root),
                "legacy_targets": ["sp"],
                "env_tier": "local",
            }
            result = bridge_freshness(bridge, str(overlay))
            self.assertTrue(result["fresh"])

    def test_bridge_freshness_returns_stale_when_overlay_newer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "sp").mkdir()
            (root / "sp" / "local.env").write_text("old\n")

            import time
            time.sleep(0.05)

            overlay = root / "overlay.yaml"
            overlay.write_text("new overlay\n")

            bridge = {
                "output_root_host_path": str(root),
                "legacy_targets": ["sp"],
                "env_tier": "local",
            }
            result = bridge_freshness(bridge, str(overlay))
            self.assertFalse(result["fresh"])
            self.assertEqual(result["reason"], "stale")


class BridgeBackedTaskTests(unittest.TestCase):
    """WG-002/WG-004: Bridge-backed task success state."""

    def test_task_success_state_ok_when_bridge_outputs_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for target in ("sweet-potato", "htma_server"):
                (root / target).mkdir()
                (root / target / "local.env").write_text("ok\n")
            model = {
                "bridges": [{
                    "id": "local-minimal-bridge",
                    "output_root_host_path": str(root),
                    "legacy_targets": ["sweet-potato", "htma_server"],
                    "env_tier": "local",
                }],
            }
            task = {"id": "env-bridge", "bridge_id": "local-minimal-bridge"}
            state = task_success_state(task, model)
            self.assertEqual(state["state"], "ok")
            self.assertEqual(state["target"], "bridge:local-minimal-bridge")

    def test_task_success_state_down_when_bridge_outputs_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = {
                "bridges": [{
                    "id": "local-minimal-bridge",
                    "output_root_host_path": str(root),
                    "legacy_targets": ["sweet-potato"],
                    "env_tier": "local",
                }],
            }
            task = {"id": "env-bridge", "bridge_id": "local-minimal-bridge"}
            state = task_success_state(task, model)
            self.assertEqual(state["state"], "down")

    def test_task_success_state_unknown_when_bridge_not_found(self) -> None:
        model = {"bridges": []}
        task = {"id": "env-bridge", "bridge_id": "nonexistent"}
        state = task_success_state(task, model)
        self.assertEqual(state["state"], "unknown")

    def test_task_success_state_falls_through_without_bridge_id(self) -> None:
        task = {"id": "regular-task", "success": {"type": "path_exists", "host_path": "/nonexistent"}}
        state = task_success_state(task)
        self.assertEqual(state["state"], "down")


class LocalRuntimeProfileValidationTests(unittest.TestCase):
    """WG-002/WG-004: Profile validation for local-* profiles."""

    def test_valid_local_profile_with_services_returns_no_errors(self) -> None:
        model = {
            "active_profiles": ["core", "local-minimal"],
            "services": [{"id": "spaps", "profiles": ["local-minimal"]}],
        }
        errors = validate_local_runtime_profiles(model)
        self.assertEqual(len(errors), 0)

    def test_local_profile_without_services_returns_profile_unknown(self) -> None:
        model = {
            "active_profiles": ["core", "local-nonexistent"],
            "services": [{"id": "spaps", "profiles": ["local-minimal"]}],
        }
        errors = validate_local_runtime_profiles(model)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"]["type"], "LOCAL_RUNTIME_PROFILE_UNKNOWN")
        self.assertIn("local-minimal", errors[0]["error"]["available_profiles"])

    def test_no_local_profiles_returns_no_errors(self) -> None:
        model = {
            "active_profiles": ["core"],
            "services": [{"id": "api"}],
        }
        errors = validate_local_runtime_profiles(model)
        self.assertEqual(len(errors), 0)


class LocalRuntimeErrorCodeTests(unittest.TestCase):
    """WG-004: Stable error code shapes."""

    def test_env_bridge_failed_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            "sync.sh exited 1 for targets: sweet-potato",
            recoverable=True,
            next_action="re-run sync.sh manually to diagnose",
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_ENV_BRIDGE_FAILED")
        self.assertTrue(err["error"]["recoverable"])
        self.assertIn("next_action", err["error"])

    def test_env_output_missing_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            "sweet-potato/local.env missing after bridge",
            recoverable=True,
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_ENV_OUTPUT_MISSING")

    def test_profile_unknown_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            "profile 'core' is a box-level profile, not a local-* profile",
            recoverable=False,
            available_profiles=["local-minimal"],
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_PROFILE_UNKNOWN")
        self.assertFalse(err["error"]["recoverable"])
        self.assertEqual(err["error"]["available_profiles"], ["local-minimal"])

    def test_start_blocked_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_START_BLOCKED",
            "htma_server health check timed out after 30s",
            blocked_services=["htma_server", "htma"],
            next_action="manage.py status --client personal --profile local-minimal",
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_START_BLOCKED")
        self.assertEqual(err["error"]["blocked_services"], ["htma_server", "htma"])

    def test_service_deferred_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_SERVICE_DEFERRED",
            "ingredient_server is not yet covered by the overlay runtime",
            recoverable=True,
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_SERVICE_DEFERRED")

    def test_mode_unsupported_error_shape(self) -> None:
        err = local_runtime_error(
            "LOCAL_RUNTIME_MODE_UNSUPPORTED",
            "start mode 'prod' is not supported for htma",
            recoverable=False,
        )
        self.assertEqual(err["error"]["type"], "LOCAL_RUNTIME_MODE_UNSUPPORTED")


class BridgeDoctorValidationTests(unittest.TestCase):
    """WG-004: Doctor validates bridge references."""

    def test_validate_bridges_warns_on_missing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model = {
                "bridges": [{
                    "id": "test-bridge",
                    "output_root_host_path": str(root),
                    "legacy_targets": ["sp"],
                    "env_tier": "local",
                }],
                "tasks": [],
            }
            results = validate_bridges(model)
            codes = [r.code for r in results]
            self.assertIn("bridge_outputs_missing", codes)

    def test_validate_bridges_passes_when_outputs_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "sp").mkdir()
            (root / "sp" / "local.env").write_text("ok\n")
            model = {
                "bridges": [{
                    "id": "test-bridge",
                    "output_root_host_path": str(root),
                    "legacy_targets": ["sp"],
                    "env_tier": "local",
                }],
                "tasks": [],
            }
            results = validate_bridges(model)
            codes = [r.code for r in results]
            self.assertIn("bridge_outputs_present", codes)
            self.assertNotIn("bridge_outputs_missing", codes)

    def test_validate_bridges_fails_on_missing_bridge_reference(self) -> None:
        model = {
            "bridges": [],
            "tasks": [{"id": "env-bridge", "bridge_id": "nonexistent"}],
        }
        results = validate_bridges(model)
        codes = [r.code for r in results]
        self.assertIn("bridge_reference_missing", codes)


class FullModelIntegrationTests(unittest.TestCase):
    """WG-004: End-to-end model compilation with local-runtime overlay."""

    def test_full_model_with_local_minimal_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            _write_local_runtime_fixture(repo, with_bridge_outputs=True)
            model = runtime_model.build_runtime_model(repo)

            active = normalize_active_profiles(["local-minimal"])
            clients = normalize_active_clients(model, ["personal"])
            filtered = filter_model(model, active, clients)

            # Bridges present
            self.assertEqual(len(filtered["bridges"]), 1)
            self.assertEqual(filtered["bridges"][0]["id"], "local-minimal-bridge")

            # Services present with correct dependency order
            service_ids = [s["id"] for s in filtered["services"]]
            self.assertIn("spaps", service_ids)
            self.assertIn("htma_server", service_ids)
            self.assertIn("htma", service_ids)

            # Tasks present
            task_ids = {t["id"] for t in filtered["tasks"]}
            self.assertIn("env-bridge-local-minimal", task_ids)

            # Bridge task has bridge_id
            bridge_task = next(t for t in filtered["tasks"] if t["id"] == "env-bridge-local-minimal")
            self.assertEqual(bridge_task["bridge_id"], "local-minimal-bridge")

            # active_profiles includes core + local-minimal
            self.assertIn("core", filtered["active_profiles"])
            self.assertIn("local-minimal", filtered["active_profiles"])

    def test_full_model_core_only_excludes_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            _write_local_runtime_fixture(repo)
            model = runtime_model.build_runtime_model(repo)

            active = normalize_active_profiles([])
            clients = normalize_active_clients(model, ["personal"])
            filtered = filter_model(model, active, clients)

            # No bridges, no local services
            self.assertEqual(len(filtered["bridges"]), 0)
            local_services = [s for s in filtered["services"] if "local-minimal" in (s.get("profiles") or [])]
            self.assertEqual(len(local_services), 0)


if __name__ == "__main__":
    unittest.main()
