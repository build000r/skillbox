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

build_local_runtime_service_deferred_error = MANAGE_MODULE.build_local_runtime_service_deferred_error
classify_requested_surfaces = MANAGE_MODULE.classify_requested_surfaces
collect_deferred_log_entries = MANAGE_MODULE.collect_deferred_log_entries
doctor_results = MANAGE_MODULE.doctor_results
filter_model = MANAGE_MODULE.filter_model
local_runtime_focus_payload = MANAGE_MODULE.local_runtime_focus_payload
parity_ledger_deferred_surfaces = MANAGE_MODULE.parity_ledger_deferred_surfaces
reconcile_local_runtime_env = MANAGE_MODULE.reconcile_local_runtime_env
runtime_status = MANAGE_MODULE.runtime_status
select_local_runtime_services = MANAGE_MODULE.select_local_runtime_services
validate_env_file_target_paths = MANAGE_MODULE.validate_env_file_target_paths
validate_parity_ledger = MANAGE_MODULE.validate_parity_ledger
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


# ---------------------------------------------------------------------------
# WG-007: Full local-core graph + parity ledger test harness
# ---------------------------------------------------------------------------
#
# These tests drive reconcile_local_runtime_env / local_runtime_focus_payload /
# validate_env_file_target_paths against an in-memory six-service model that
# mirrors the personal overlay declared in
# /Users/b/repos/skillbox-config/clients/personal/overlay.yaml (shared.md US-1).
# We build the model as plain dicts rather than materialising overlay.yaml so
# the tests stay fast and hermetic.
from typing import Any

LOCAL_CORE_SERVICE_IDS: tuple[str, ...] = (
    "spaps",
    "htma_server",
    "ingredient_server",
    "approval_feedback_api",
    "cfo",
    "htma",
)


def _build_local_core_model(
    repo_root: Path,
    *,
    with_bridge_outputs: bool = True,
    parity_ledger_overrides: list[dict[str, Any]] | None = None,
    approval_env_filename: str = ".env",
    include_approval_bootstrap: bool = True,
) -> dict[str, Any]:
    """Return a minimal in-memory runtime model for the local-core graph.

    Mirrors the personal overlay contract from shared.md (WG-001/WG-002):
      * six services with declared mode commands (reuse/prod/fresh)
      * one env bridge with six legacy targets
      * two bootstrap tasks (env-bridge-local-core, approval-feedback-db-bootstrap)
      * approval_feedback_api env target is repo-local ``.env``
      * a parity ledger with one deferred surface (``buildooor``) + the
        bridge-only ``sync.sh env compilation`` seam, keyed by the six
        covered services as ``covered``.
    """
    bridge_root = repo_root / "env-out"
    if with_bridge_outputs:
        for target in (
            "sweet-potato",
            "htma_server",
            "ingredient_server",
            "unclawg-approval-feedback-api",
            "cfo",
            "htma",
        ):
            target_dir = bridge_root / target
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "local.env").write_text(f"# {target}\n", encoding="utf-8")

    def _repo(rid: str, sub: str) -> dict[str, Any]:
        host = repo_root / sub
        host.mkdir(parents=True, exist_ok=True)
        return {
            "id": rid,
            "path": str(host),
            "repo_path": str(host),
            "host_path": str(host),
            "kind": "repo",
            "profiles": ["local-core"],
        }

    repos = [
        _repo("sweet-potato", "sweet-potato"),
        _repo("htma_server", "htma_server"),
        _repo("ingredient_server", "ingredient_server"),
        _repo("approval-feedback-api", "unclawg/services/approval_feedback_api"),
        _repo("cfo", "cfo"),
        _repo("htma", "htma"),
    ]

    bridges = [
        {
            "id": "local-core-bridge",
            "client": "personal",
            "env_tier": "local",
            "legacy_targets": [
                "sweet-potato",
                "htma_server",
                "ingredient_server",
                "unclawg-approval-feedback-api",
                "cfo",
                "htma",
            ],
            "output_root": str(bridge_root),
            "output_root_host_path": str(bridge_root),
            "emit_stubs": False,
            "profiles": ["local-core"],
        }
    ]

    tasks: list[dict[str, Any]] = [
        {
            "id": "env-bridge-local-core",
            "kind": "bootstrap",
            "bridge_id": "local-core-bridge",
            "profiles": ["local-core"],
            "command": "sync.sh --emit",
        }
    ]
    if include_approval_bootstrap:
        tasks.append(
            {
                "id": "approval-feedback-db-bootstrap",
                "kind": "bootstrap",
                "repo_id": "approval-feedback-api",
                "profiles": ["local-core"],
                "command": "docker start unclawg-db-1 >/dev/null 2>&1 || true",
                "success": {
                    "type": "path_exists",
                    "host_path": str(repo_root / ".unclawg-db.ready"),
                },
            }
        )

    approval_target = (
        repo_root
        / "unclawg"
        / "services"
        / "approval_feedback_api"
        / approval_env_filename
    )
    approval_source = (
        bridge_root / "unclawg-approval-feedback-api" / "local.env"
    )
    # Pre-create the target so env_file_state reports state="ok" for
    # ensure_required_env_files_ready (WG-005 pre-mutation check).
    if with_bridge_outputs and approval_env_filename == ".env":
        approval_target.parent.mkdir(parents=True, exist_ok=True)
        if approval_source.is_file():
            approval_target.write_bytes(approval_source.read_bytes())
            # env_file_state enforces 0o600 for a matching target
            import os as _os
            _os.chmod(approval_target, 0o600)
    env_files = [
        {
            "id": "approval-feedback-env",
            "repo": "approval-feedback-api",
            "repo_id": "approval-feedback-api",
            "path": str(approval_target),
            "host_path": str(approval_target),
            "target_path": str(approval_target),
            "required": True,
            "profiles": ["local-core"],
            "mode": "0600",
            "source": {
                "kind": "file",
                "path": str(approval_source),
                "host_path": str(approval_source),
                "source_path": str(approval_source),
            },
            "sync": {"mode": "write"},
            "source_kind": "file",
            "source_path": str(approval_source),
            "source_host_path": str(approval_source),
        }
    ]

    def _svc(
        sid: str,
        repo_id: str,
        depends_on: list[str],
        health_url: str,
        *,
        bootstrap_tasks: list[str] | None = None,
        supports: tuple[str, ...] = ("reuse", "prod", "fresh"),
    ) -> dict[str, Any]:
        commands = {mode: f"make local-up-{mode} # {sid}" for mode in supports}
        return {
            "id": sid,
            "kind": "http",
            "repo": repo_id,
            "repo_id": repo_id,
            "profiles": ["local-core"],
            "depends_on": depends_on,
            "bootstrap_tasks": list(bootstrap_tasks or ["env-bridge-local-core"]),
            "commands": commands,
            "healthcheck": {"type": "http", "url": health_url},
            "health_type": "http",
            "health_target": health_url,
        }

    approval_bootstraps = ["env-bridge-local-core"]
    if include_approval_bootstrap:
        approval_bootstraps.append("approval-feedback-db-bootstrap")

    services = [
        _svc("spaps", "sweet-potato", [], "http://localhost:3301/health"),
        _svc("htma_server", "htma_server", ["spaps"], "http://localhost:8000/health"),
        _svc(
            "ingredient_server",
            "ingredient_server",
            ["spaps"],
            "http://localhost:8001/health",
        ),
        _svc(
            "approval_feedback_api",
            "approval-feedback-api",
            ["spaps"],
            "http://localhost:8010/health",
            bootstrap_tasks=approval_bootstraps,
        ),
        _svc("cfo", "cfo", ["spaps"], "http://localhost:8050/health"),
        _svc("htma", "htma", ["spaps", "htma_server"], "http://localhost:5173"),
    ]

    parity_ledger = [
        {
            "id": sid,
            "legacy_surface": sid,
            "surface_type": "service",
            "action": "declare",
            "ownership_state": "covered",
            "intended_profiles": ["local-core"],
            "bridge_dependency": None,
        }
        for sid in LOCAL_CORE_SERVICE_IDS
    ] + [
        {
            "id": "buildooor",
            "legacy_surface": "buildooor",
            "surface_type": "service",
            "action": "build",
            "ownership_state": "deferred",
            "intended_profiles": ["local-all"],
            "bridge_dependency": None,
            "request_error": "LOCAL_RUNTIME_SERVICE_DEFERRED",
            "notes": "follow-on slice",
        },
        {
            "id": "swimmers",
            "legacy_surface": "swimmers",
            "surface_type": "service",
            "action": "build",
            "ownership_state": "deferred",
            "intended_profiles": ["local-all"],
            "bridge_dependency": None,
            "request_error": "LOCAL_RUNTIME_SERVICE_DEFERRED",
        },
        {
            "id": "sync-sh-env-compilation",
            "legacy_surface": "sync.sh env compilation",
            "surface_type": "bridge",
            "action": "bridge",
            "ownership_state": "bridge-only",
            "intended_profiles": ["local-core"],
            "bridge_dependency": "local-core-bridge",
            "request_error": "LOCAL_RUNTIME_SERVICE_DEFERRED",
        },
    ]
    if parity_ledger_overrides is not None:
        parity_ledger = parity_ledger_overrides

    return {
        "root_dir": str(repo_root),
        "active_profiles": ["core", "local-core"],
        "active_clients": ["personal"],
        "selection": {},
        "clients": [
            {
                "id": "personal",
                "label": "Personal",
                "_overlay_path": str(repo_root / "overlay.yaml"),
            }
        ],
        "repos": repos,
        "artifacts": [],
        "env_files": env_files,
        "skills": [],
        "tasks": tasks,
        "services": services,
        "logs": [],
        "checks": [],
        "bridges": bridges,
        "parity_ledger": parity_ledger,
    }


class LocalCoreFocusUS1Tests(unittest.TestCase):
    """WG-007 / US-1: Full local-core focus coverage."""

    def test_local_core_focus_resolves_six_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            services = select_local_runtime_services(model, "local-core")
            self.assertEqual(
                [s["id"] for s in services], list(LOCAL_CORE_SERVICE_IDS)
            )

    def test_local_core_focus_payload_shape_matches_us1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            result = reconcile_local_runtime_env(
                model, "local-core", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "ready", result)
            payload = local_runtime_focus_payload(
                model, result, client_id="personal",
            )
            self.assertEqual(payload["client_id"], "personal")
            self.assertIn("core", payload["active_profiles"])
            self.assertIn("local-core", payload["active_profiles"])
            self.assertEqual(payload["local_runtime"]["profile"], "local-core")
            self.assertEqual(
                payload["local_runtime"]["default_mode"], "reuse"
            )
            env_bridge = payload["local_runtime"]["env_bridge"]
            self.assertEqual(env_bridge["id"], "local-core-bridge")
            self.assertEqual(env_bridge["status"], "ready")
            service_ids = [
                entry["id"]
                for entry in payload["local_runtime"]["services"]
            ]
            self.assertEqual(service_ids, list(LOCAL_CORE_SERVICE_IDS))
            self.assertTrue(payload["next_actions"][0].startswith("manage.py up"))

    def test_unknown_profile_returns_profile_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            result = reconcile_local_runtime_env(
                model, "local-nonexistent", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["error"]["type"], "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            )

    def test_empty_profile_returns_profile_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            result = reconcile_local_runtime_env(
                model, "", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["error"]["type"], "LOCAL_RUNTIME_PROFILE_UNKNOWN",
            )

    def test_missing_bridge_output_returns_env_output_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo, with_bridge_outputs=True)
            # Nuke one bridge output to simulate an incomplete bridge run.
            missing_path = (
                repo / "env-out" / "unclawg-approval-feedback-api" / "local.env"
            )
            missing_path.unlink()
            # Drop the bridge task so reconciliation cannot repair it.
            model["tasks"] = [
                t for t in model["tasks"]
                if t["id"] != "env-bridge-local-core"
            ]
            result = reconcile_local_runtime_env(
                model, "local-core", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["error"]["type"], "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            )

    def test_bridge_non_zero_exit_returns_env_bridge_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo, with_bridge_outputs=False)
            # Monkey-patch run_tasks so it raises inside the reconciliation
            # path, simulating sync.sh exiting non-zero.
            import runtime_manager.runtime_ops as runtime_ops_mod
            original = runtime_ops_mod.run_tasks

            def _boom(*args: object, **kwargs: object) -> None:
                raise RuntimeError("sync.sh exited 1 for targets: sweet-potato")

            runtime_ops_mod.run_tasks = _boom  # type: ignore[assignment]
            try:
                result = reconcile_local_runtime_env(
                    model,
                    "local-core",
                    overlay_path=None,
                    dry_run=False,
                )
            finally:
                runtime_ops_mod.run_tasks = original  # type: ignore[assignment]

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["error"]["type"], "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            )
            self.assertIn("sync.sh", result["error"]["detail"])

    def test_approval_feedback_env_target_must_be_dot_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            good = _build_local_core_model(repo)
            self.assertEqual(
                validate_env_file_target_paths(good["env_files"], good),
                [],
            )
            bad = _build_local_core_model(repo, approval_env_filename=".env.local")
            violations = validate_env_file_target_paths(bad["env_files"], bad)
            self.assertEqual(len(violations), 1)
            self.assertIn("approval_feedback_api", violations[0])
            self.assertIn(".env", violations[0])

    def test_wrong_env_target_path_surfaces_as_env_output_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(
                repo, approval_env_filename=".env.local",
            )
            result = reconcile_local_runtime_env(
                model, "local-core", overlay_path=None, dry_run=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["error"]["type"], "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
            )


class LocalCoreParityLedgerUS4Tests(unittest.TestCase):
    """WG-007 / US-4: Parity ledger enforcement and doctor drift."""

    def test_classify_requested_surfaces_marks_deferred_buildooor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            classification = classify_requested_surfaces(model, ["buildooor"])
            self.assertEqual(classification["covered"], [])
            self.assertEqual(classification["unknown"], [])
            self.assertEqual(len(classification["deferred"]), 1)
            sid, item = classification["deferred"][0]
            self.assertEqual(sid, "buildooor")
            self.assertEqual(item["ownership_state"], "deferred")

    def test_classify_requested_surfaces_marks_bridge_only_seam_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            classification = classify_requested_surfaces(
                model, ["sync-sh-env-compilation"]
            )
            self.assertEqual(len(classification["deferred"]), 1)
            _, item = classification["deferred"][0]
            self.assertEqual(item["ownership_state"], "bridge-only")

    def test_build_local_runtime_service_deferred_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            _, item = classify_requested_surfaces(model, ["buildooor"])["deferred"][0]
            err = build_local_runtime_service_deferred_error(
                item,
                client_id="personal",
                profile="local-core",
                requested_mode="reuse",
                surface_id="buildooor",
            )
            self.assertEqual(
                err["error"]["type"], "LOCAL_RUNTIME_SERVICE_DEFERRED",
            )
            self.assertIn("buildooor", err["error"]["blocked_services"])
            self.assertEqual(err["error"]["ownership_state"], "deferred")
            self.assertEqual(err["error"]["requested_mode"], "reuse")

    def test_logs_against_deferred_surface_returns_service_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            deferred_pairs = classify_requested_surfaces(
                model, ["buildooor"]
            )["deferred"]
            entries = collect_deferred_log_entries(
                deferred_pairs, client_id="personal", profile="local-core",
            )
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["id"], "buildooor")
            self.assertTrue(entry["deferred"])
            self.assertEqual(entry["ownership_state"], "deferred")
            self.assertTrue(entry["next_action"])

    def test_status_with_blocked_prerequisites_exits_observationally(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            status = runtime_status(model)
            # observational: the key exists and the payload carries the
            # parity-ledger section; we don't assert exit code here because
            # runtime_status is the pre-exit data shape
            self.assertIn("blocked_services", status)
            self.assertIsInstance(status["blocked_services"], list)
            # services in the fixture are not actually running, so status
            # surfaces them through the ownership-state annotated listing
            service_ids_in_status = {s["id"] for s in status["services"]}
            self.assertEqual(service_ids_in_status, set(LOCAL_CORE_SERVICE_IDS))
            for service in status["services"]:
                self.assertEqual(service["ownership_state"], "covered")
            deferred = status["parity_ledger"]["deferred_surfaces"]
            self.assertIn("buildooor", deferred)
            self.assertIn("sync.sh env compilation", deferred)

    def test_doctor_emits_parity_ledger_subject_with_deferred_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            results = validate_parity_ledger(model)
            self.assertEqual(len(results), 1)
            check = results[0]
            self.assertEqual(check.status, "pass", check)
            self.assertEqual(check.code, "parity-ledger")
            details = check.details or {}
            self.assertIn("deferred_surfaces", details)
            self.assertIn("buildooor", details["deferred_surfaces"])

    def test_parity_ledger_drift_deferred_with_service_fires_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            # Drift: mark spaps as deferred while the service graph still
            # declares it (runtime claims coverage the ledger denies).
            for item in model["parity_ledger"]:
                if item.get("id") == "spaps":
                    item["ownership_state"] = "deferred"
                    break
            results = validate_parity_ledger(model)
            self.assertEqual(results[0].status, "fail")
            self.assertEqual(results[0].code, "LOCAL_RUNTIME_COVERAGE_GAP")

    def test_parity_ledger_drift_covered_without_service_fires_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            model = _build_local_core_model(repo)
            model["parity_ledger"].append(
                {
                    "id": "ghost_service",
                    "legacy_surface": "ghost_service",
                    "surface_type": "service",
                    "action": "declare",
                    "ownership_state": "covered",
                    "intended_profiles": ["local-core"],
                    "bridge_dependency": None,
                }
            )
            results = validate_parity_ledger(model)
            self.assertEqual(results[0].status, "fail")
            self.assertEqual(results[0].code, "LOCAL_RUNTIME_COVERAGE_GAP")


if __name__ == "__main__":
    unittest.main()
