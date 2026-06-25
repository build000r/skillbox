from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (ROOT_DIR / "scripts").resolve()
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SKILL_BUNDLE_FILTER = types.ModuleType("lib.skill_bundle_filter")
SKILL_BUNDLE_FILTER.iter_included_skill_files = (
    lambda skill_path: (path for path in Path(skill_path).rglob("*") if path.is_file())
)
sys.modules.setdefault("lib.skill_bundle_filter", SKILL_BUNDLE_FILTER)

QUICK_VALIDATE = SourceFileLoader(
    "skillbox_quick_validate",
    str((SCRIPTS_DIR / "quick_validate.py").resolve()),
).load_module()
CM_BRIDGE = SourceFileLoader(
    "skillbox_cm_stdio_bridge",
    str((SCRIPTS_DIR / "cm_stdio_bridge.py").resolve()),
).load_module()
INGRESS = SourceFileLoader(
    "skillbox_ingress_proxy",
    str((SCRIPTS_DIR / "ingress_proxy.py").resolve()),
).load_module()
STUB_WEB = SourceFileLoader(
    "skillbox_stub_web",
    str((SCRIPTS_DIR / "stub_web.py").resolve()),
).load_module()
HERMES_CODEX = SourceFileLoader(
    "skillbox_hermes_codex_adapter",
    str((SCRIPTS_DIR / "hermes_codex_adapter.py").resolve()),
).load_module()
TAILNET_SMOKE = SourceFileLoader(
    "skillbox_tailnet_app_smoke",
    str((SCRIPTS_DIR / "tailnet_app_smoke.py").resolve()),
).load_module()


def _write_skill(root: Path, frontmatter: str, body: str = "\nUse this skill.\n") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


class BinaryPushScriptTests(unittest.TestCase):
    SCRIPT = SCRIPTS_DIR / "07-build-and-push-binary.sh"

    def _run_validate_only(
        self,
        src_dir: Path,
        bin_name: str,
        target: str = "skillbox-host",
        package: str | None = None,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["SKILLBOX_BUILD_PUSH_VALIDATE_ONLY"] = "1"
        if extra_env:
            env.update(extra_env)
        args = ["bash", str(self.SCRIPT), str(src_dir), bin_name, target]
        if package is not None:
            args.append(package)
        return subprocess.run(
            args,
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_build_push_rejects_shell_metacharacter_identifiers_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir)
            result = self._run_validate_only(src, "bad;touch")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid binary name", result.stderr)

    def test_build_push_rejects_shell_metacharacter_paths_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir)
            result = self._run_validate_only(
                src,
                "fwc",
                extra_env={"SKILLBOX_TARGET_BIN_DIR": "/tmp/bin;touch-pwned"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid target bin dir", result.stderr)

    def test_build_push_validates_operator_inputs_without_docker_or_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir)
            result = self._run_validate_only(
                src,
                "fwc",
                package="flywheel-connectors",
                extra_env={
                    "SKILLBOX_BUILD_APT_DEPS": "libdbus-1-dev pkg-config libssl-dev",
                    "SKILLBOX_BUILD_EXTRA_MOUNTS": f"{src}:/dp/source:ro",
                    "SKILLBOX_TARGET_BIN_DIR": "/home/skillbox/.local/bin",
                    "SKILLBOX_SYMLINK_DIR": "/usr/local/bin",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validation: ok", result.stdout)


class TailnetAppSmokeTests(unittest.TestCase):
    def test_smoke_accepts_tailnet_direct_service_and_same_origin_assets(self) -> None:
        def status_loader(_root: Path, client: str, profile: str) -> dict[str, object]:
            self.assertEqual(client, "haas")
            self.assertEqual(profile, "local-all")
            return {
                "services": [
                    {
                        "id": "haas-web",
                        "state": "running",
                        "endpoint_url": "http://tailnet.test:8787/",
                        "exposure": "tailnet-direct",
                        "viewable_from_tailnet": True,
                    }
                ]
            }

        def fetcher(url: str, _timeout: float) -> object:
            if url == "http://tailnet.test:8787/":
                return TAILNET_SMOKE.FetchResult(
                    200,
                    "text/html",
                    b'<link rel="stylesheet" href="/app.css"><script src="/app.js"></script>',
                )
            return TAILNET_SMOKE.FetchResult(200, "application/javascript", b"ok")

        payload = TAILNET_SMOKE.run_smoke(
            root_dir=ROOT_DIR,
            clients=["haas"],
            profile="local-all",
            timeout=0.1,
            asset_limit=8,
            status_loader=status_loader,
            fetcher=fetcher,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service_count"], 1)
        self.assertEqual(payload["asset_count"], 2)
        self.assertEqual(payload["results"][0]["url"], "http://tailnet.test:8787/")

    def test_smoke_rejects_ingress_routed_service_for_port_per_app_contract(self) -> None:
        payload = TAILNET_SMOKE.run_smoke(
            root_dir=ROOT_DIR,
            clients=["haas"],
            profile="local-all",
            timeout=0.1,
            asset_limit=8,
            status_loader=lambda _root, _client, _profile: {
                "services": [
                    {
                        "id": "haas-web",
                        "state": "running",
                        "endpoint_url": "http://tailnet.test:9080/",
                        "exposure": "ingress-routed",
                        "viewable_from_tailnet": True,
                    }
                ]
            },
            fetcher=lambda _url, _timeout: TAILNET_SMOKE.FetchResult(200, "text/html", b""),
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["error"], "service is not tailnet-direct")


class GuardDevPortScriptTests(unittest.TestCase):
    SCRIPT = SCRIPTS_DIR / "guard-dev-port.sh"
    SHIM = SCRIPTS_DIR / "skillbox-dev-shim.sh"

    def _copy_scripts(self, root: Path) -> tuple[Path, Path]:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(parents=True)
        guard = scripts_dir / "guard-dev-port.sh"
        shim = scripts_dir / "skillbox-dev-shim.sh"
        guard.write_text(self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        shim.write_text(self.SHIM.read_text(encoding="utf-8"), encoding="utf-8")
        guard.chmod(0o755)
        shim.chmod(0o755)
        return guard, shim

    def _model(self, root: Path, app: Path) -> dict[str, object]:
        return {
            "root_dir": str(root),
            "repos": [{"id": "app", "host_path": str(app)}],
            "services": [
                {
                    "id": "app-web",
                    "client": "acme",
                    "profiles": ["local-all"],
                    "repo_id": "app",
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:5173/health"},
                }
            ],
        }

    def _run_guard(
        self,
        guard: Path,
        *,
        command: str,
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        root = guard.parent.parent
        env = os.environ.copy()
        env["SKILLBOX_ROOT"] = str(root)
        env["SKILLBOX_PORT_GUARD_MODEL_JSON"] = json.dumps(self._model(root, cwd if cwd.name == "app" else root / "app"))
        if extra_env:
            env.update(extra_env)
        payload = {"tool_name": "Bash", "tool_input": {"command": command, "cwd": str(cwd)}}
        return subprocess.run(
            ["bash", str(guard)],
            cwd=root,
            input=json.dumps(payload),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_guard_dev_port_blocks_direct_dev_in_covered_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            app.mkdir()
            guard, _shim = self._copy_scripts(root)

            result = self._run_guard(guard, command="npm run dev", cwd=app)
            cd_result = self._run_guard(guard, command="cd app && npm run dev", cwd=root)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(cd_result.returncode, 2)
            self.assertLessEqual(len(result.stderr.strip().splitlines()), 10)
            self.assertIn("service: app-web", result.stderr)
            self.assertIn("declared port: 5173", result.stderr)
            self.assertIn(
                "python3 .env-manager/manage.py up --client acme --profile local-all --service app-web",
                result.stderr,
            )
            runtime_log = root / "logs" / "runtime" / "runtime.log"
            self.assertIn("port_guard block", runtime_log.read_text(encoding="utf-8"))

    def test_guard_dev_port_allows_non_dev_uncovered_and_bypass_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            other = root / "other"
            app.mkdir()
            other.mkdir()
            guard, _shim = self._copy_scripts(root)

            install = self._run_guard(guard, command="npm install", cwd=app)
            vite_build = self._run_guard(guard, command="vite build", cwd=app)
            uncovered = self._run_guard(guard, command="npm run dev", cwd=other)
            managed = self._run_guard(
                guard,
                command="pnpm dev",
                cwd=app,
                extra_env={"SKILLBOX_MANAGED_RUN": "1"},
            )
            bypass = self._run_guard(
                guard,
                command="next dev",
                cwd=app,
                extra_env={"SKILLBOX_PORT_GUARD": "off"},
            )

            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual(vite_build.returncode, 0, vite_build.stderr)
            self.assertEqual(uncovered.returncode, 0, uncovered.stderr)
            self.assertEqual(managed.returncode, 0, managed.stderr)
            self.assertEqual(bypass.returncode, 0, bypass.stderr)
            runtime_log = root / "logs" / "runtime" / "runtime.log"
            content = runtime_log.read_text(encoding="utf-8")
            self.assertIn("port_guard bypass_uncovered", content)
            self.assertIn("port_guard bypass_managed_run", content)
            self.assertIn("port_guard bypass_operator", content)

    def test_guard_dev_port_blocks_when_detected_dev_cannot_be_evaluated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            app.mkdir()
            guard, _shim = self._copy_scripts(root)
            env = os.environ.copy()
            env["SKILLBOX_ROOT"] = str(root)
            env["SKILLBOX_PORT_GUARD_MODEL_JSON"] = "{not-json"
            payload = {"tool_name": "Bash", "tool_input": {"command": "npm run dev", "cwd": str(app)}}

            result = subprocess.run(
                ["bash", str(guard)],
                cwd=root,
                input=json.dumps(payload),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("could not evaluate", result.stderr)
            self.assertIn("SKILLBOX_PORT_GUARD=off", result.stderr)

    def test_dev_shim_blocks_before_exec_and_allows_managed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            shims = root / "shims"
            realbin = root / "realbin"
            app.mkdir()
            shims.mkdir()
            realbin.mkdir()
            _guard, shim = self._copy_scripts(root)
            (shims / "npm").symlink_to(shim)
            marker = root / "real-npm.txt"
            real = realbin / "npm"
            real.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'real npm %s\\n' \"$*\" > \"$REAL_NPM_MARKER\"\n",
                encoding="utf-8",
            )
            real.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PATH": f"{shims}:{realbin}:{env.get('PATH', '')}",
                "SKILLBOX_ROOT": str(root),
                "SKILLBOX_PORT_GUARD_MODEL_JSON": json.dumps(self._model(root, app)),
                "REAL_NPM_MARKER": str(marker),
            })

            blocked = subprocess.run(
                [str(shims / "npm"), "run", "dev"],
                cwd=app,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertFalse(marker.exists())

            allowed_env = dict(env)
            allowed_env["SKILLBOX_MANAGED_RUN"] = "1"
            allowed = subprocess.run(
                [str(shims / "npm"), "run", "dev"],
                cwd=app,
                env=allowed_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "real npm run dev\n")

    def test_dev_shims_install_target_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shims = Path(tmpdir) / "shims"

            first = subprocess.run(
                ["make", "dev-shims-install", f"DEV_SHIM_BIN_DIR={shims}"],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            second = subprocess.run(
                ["make", "dev-shims-install", f"DEV_SHIM_BIN_DIR={shims}"],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            for name in ("npm", "pnpm", "yarn", "vite", "next", "astro"):
                self.assertTrue((shims / name).exists(), name)


class GuardDestructiveOpScriptTests(unittest.TestCase):
    SCRIPT = SCRIPTS_DIR / "guard-destructive-op.sh"

    def _copy_guard(self, root: Path) -> Path:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(parents=True)
        target = scripts_dir / "guard-destructive-op.sh"
        target.write_text(self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
        return target

    def _run_guard(
        self,
        script: Path,
        *,
        tool_name: str = "mcp__skillbox-operator__operator_compose_down",
        tool_input: dict[str, object] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        root = script.parent.parent
        mono_root = root / "empty-mono"
        mono_root.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["SKILLBOX_MONOSERVER_HOST_ROOT"] = str(mono_root)
        if extra_env:
            env.update(extra_env)
        payload = {
            "tool_name": tool_name,
            "tool_input": tool_input or {"dry_run": False},
        }
        return subprocess.run(
            ["bash", str(script)],
            input=json.dumps(payload),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_guard_destructive_gates_underscore_operator_mcp_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = self._copy_guard(Path(tmpdir))

            result = self._run_guard(
                script,
                tool_name="mcp__skillbox_operator__operator_compose_down",
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKED: operator_compose_down", result.stderr)

    def test_guard_destructive_finds_nested_workspace_client_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = self._copy_guard(root)
            nested_repo = root / "workspace" / "clients" / "acme" / "app"
            nested_repo.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=nested_repo, check=True, capture_output=True, text=True)
            (nested_repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

            result = self._run_guard(script)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Uncommitted changes in:", result.stderr)
        self.assertIn(str(nested_repo), result.stderr)

    def test_guard_destructive_teardown_local_scans_local_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = self._copy_guard(root)
            nested_repo = root / "workspace" / "clients" / "local" / "app"
            nested_repo.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=nested_repo, check=True, capture_output=True, text=True)
            (nested_repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

            result = self._run_guard(
                script,
                tool_name="mcp__skillbox-operator__operator_teardown",
                tool_input={"dry_run": False, "box_id": "local"},
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Uncommitted changes in:", result.stderr)
        self.assertIn(str(nested_repo), result.stderr)

    def test_guard_destructive_default_ttl_matches_operator_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = self._copy_guard(Path(tmpdir))

            result = self._run_guard(script)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Configured marker TTL: 600s", result.stderr)
        self.assertIn("Observed marker age: unavailable", result.stderr)

    def test_guard_destructive_env_ttl_reports_observed_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = self._copy_guard(root)
            marker = root / ".skillbox-state" / "dryrun-markers" / ".skillbox-dryrun-operator_compose_down-local"
            marker.parent.mkdir(parents=True)
            marker.write_text("dry-run completed\n", encoding="utf-8")
            old = marker.stat().st_mtime - 10
            os.utime(marker, (old, old))

            result = self._run_guard(script, extra_env={"SKILLBOX_DRYRUN_MARKER_TTL_SECONDS": "5"})

        self.assertEqual(result.returncode, 1)
        self.assertIn("Configured marker TTL: 5s", result.stderr)
        self.assertRegex(result.stderr, r"Observed marker age: [0-9]+s")

    # ------------------------------------------------------------------
    # Fail-closed matrix: any internal error while evaluating a *gated*
    # tool must BLOCK (non-zero exit) and name the failing stage, while a
    # NON-gated tool always passes and the happy path (clean+pushed+valid
    # marker) still exits 0. See skillbox-safety-trust-boundary-epic-lzz1.3.
    # ------------------------------------------------------------------

    # Both dash- and underscore-namespaced forms of each gated tool. The matrix
    # runs against both compose_down and teardown.
    GATED_COMPOSE_DOWN = "mcp__skillbox-operator__operator_compose_down"
    GATED_TEARDOWN = "mcp__skillbox-operator__operator_teardown"

    def _run_guard_raw(
        self,
        script: Path,
        stdin_text: str,
        *,
        extra_env: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run the guard with arbitrary (possibly malformed) stdin."""
        root = script.parent.parent
        mono_root = root / "empty-mono"
        mono_root.mkdir(exist_ok=True)
        clients_root = root / "empty-clients"
        clients_root.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["SKILLBOX_MONOSERVER_HOST_ROOT"] = str(mono_root)
        env["SKILLBOX_CLIENTS_HOST_ROOT"] = str(clients_root)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(script)],
            input=stdin_text,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def _make_python_shim_dir(self, root: Path, *, mode: str) -> Path:
        """Create a PATH dir with a python3 shim.

        mode="missing": directory has the usual shell utils but NO python3, so
            the very first python3 call (tool_name parse) fails closed.
        mode="repo_scan_crash": python3 delegates to the real interpreter for
            the small inline parsers but exits 99 for the big repo-cleanliness
            scan (detected by the unique ``discover_git_roots`` token), so the
            scan-crash branch is exercised without breaking JSON parsing.
        """
        shim_dir = root / f"shim-{mode}"
        shim_dir.mkdir(parents=True, exist_ok=True)
        # Symlink the shell utilities the guard relies on so a restricted PATH
        # still has bash/git/etc. (we only want to control python3).
        for tool in (
            "bash", "sh", "cat", "date", "mkdir", "printf", "tr", "rm",
            "mktemp", "dirname", "pwd", "git", "timeout", "ssh", "sleep", "env",
        ):
            src = shutil.which(tool)
            if src:
                link = shim_dir / tool
                if not link.exists():
                    link.symlink_to(src)
        if mode == "missing":
            # No python3 at all in this dir.
            return shim_dir
        if mode == "repo_scan_crash":
            real_python = shutil.which("python3") or sys.executable
            shim = shim_dir / "python3"
            shim.write_text(
                "#!/usr/bin/env bash\n"
                "for a in \"$@\"; do\n"
                "  case \"$a\" in\n"
                "    *discover_git_roots*)\n"
                "      echo 'simulated repo-scan crash' >&2; exit 99 ;;\n"
                "  esac\n"
                "done\n"
                f'exec "{real_python}" "$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)
            return shim_dir
        raise ValueError(f"unknown shim mode: {mode}")

    def test_guard_malformed_hook_input_blocks_naming_stage(self) -> None:
        for tool in (self.GATED_COMPOSE_DOWN, self.GATED_TEARDOWN):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                script = self._copy_guard(Path(tmpdir))
                # Malformed JSON cannot be parsed even to learn the tool name.
                result = self._run_guard_raw(script, "{bad json")
            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED", result.stderr)
            self.assertIn("hook input (tool_name)", result.stderr)
            self.assertIn("blocking by default", result.stderr)

    def test_guard_malformed_tool_input_blocks_on_dry_run_stage(self) -> None:
        # tool_name parses, but tool_input is a non-object that cannot yield a
        # trustworthy dry_run decision -> block naming dry_run.
        for tool in (self.GATED_COMPOSE_DOWN, self.GATED_TEARDOWN):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                script = self._copy_guard(Path(tmpdir))
                stdin_text = json.dumps({"tool_name": tool, "tool_input": ["not", "an", "object"]})
                result = self._run_guard_raw(script, stdin_text)
            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED", result.stderr)
            self.assertIn("dry_run", result.stderr)
            self.assertIn("blocking by default", result.stderr)

    def test_guard_missing_python3_blocks(self) -> None:
        # python3 absent from PATH: the first python3 call (tool_name parse)
        # fails, so the guard fails closed instead of passing on empty output.
        for tool in (self.GATED_COMPOSE_DOWN, self.GATED_TEARDOWN):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                script = self._copy_guard(root)
                shim_dir = self._make_python_shim_dir(root, mode="missing")
                result = self._run_guard(
                    script,
                    tool_name=tool,
                    extra_env={"PATH": str(shim_dir)},
                )
            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED", result.stderr)
            self.assertIn("blocking by default", result.stderr)

    def test_guard_repo_scan_crash_blocks_naming_repo_cleanliness(self) -> None:
        # The big repo-cleanliness scan crashes (exit != 0/1) while the small
        # JSON parsers still work. The guard must block and name the repo
        # cleanliness stage rather than treat empty output as "clean".
        for tool in (self.GATED_COMPOSE_DOWN, self.GATED_TEARDOWN):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                script = self._copy_guard(root)
                shim_dir = self._make_python_shim_dir(root, mode="repo_scan_crash")
                result = self._run_guard(
                    script,
                    tool_name=tool,
                    tool_input={"dry_run": False, "box_id": "local"},
                    extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
                )
            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED", result.stderr)
            self.assertIn("repo cleanliness", result.stderr)
            self.assertIn("blocking by default", result.stderr)

    def test_guard_ssh_timeout_during_remote_verification_blocks(self) -> None:
        # A hanging SSH on the teardown remote path must be killed by the
        # overall timeout and reported as "could not verify", not allowed.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = self._copy_guard(root)
            # Inventory with a box that has a reachable address so we take the
            # SSH branch.
            (root / "workspace").mkdir(parents=True, exist_ok=True)
            (root / "workspace" / "boxes.json").write_text(
                json.dumps(
                    {"boxes": [{"id": "box-remote", "tailscale_ip": "100.64.0.9", "ssh_user": "skillbox"}]}
                ),
                encoding="utf-8",
            )
            ssh_stub = root / "ssh-hang"
            ssh_stub.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
            ssh_stub.chmod(0o755)

            result = self._run_guard(
                script,
                tool_name=self.GATED_TEARDOWN,
                tool_input={"dry_run": False, "box_id": "box-remote"},
                extra_env={
                    "SKILLBOX_GUARD_SSH_BIN": str(ssh_stub),
                    "SKILLBOX_GUARD_SSH_TIMEOUT_SECONDS": "2",
                },
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertIn("could not verify remote repos", result.stderr)

    def test_guard_non_gated_tool_fast_path_exits_zero(self) -> None:
        # Non-destructive tools must always pass without evaluation.
        for tool in ("mcp__skillbox-operator__operator_boxes", "Read", "some_other_tool"):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                script = self._copy_guard(Path(tmpdir))
                result = self._run_guard(script, tool_name=tool)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")

    def test_guard_dry_run_true_passes(self) -> None:
        for tool in (self.GATED_COMPOSE_DOWN, self.GATED_TEARDOWN):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir:
                script = self._copy_guard(Path(tmpdir))
                result = self._run_guard(
                    script,
                    tool_name=tool,
                    tool_input={"dry_run": True},
                )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_happy_path_clean_pushed_repo_with_valid_marker_passes(self) -> None:
        # Clean + pushed repo with a fresh dry-run marker must PASS (exit 0).
        for tool, friendly in (
            (self.GATED_COMPOSE_DOWN, "operator_compose_down"),
            (self.GATED_TEARDOWN, "operator_teardown"),
        ):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as baredir:
                root = Path(tmpdir)
                script = self._copy_guard(root)

                bare = Path(baredir) / "upstream.git"
                subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True, text=True)

                # Make the guard's REPO_ROOT (root) a clean, pushed git repo.
                self._git_init_clean_pushed(root, bare)

                # Empty workspace scan roots so only `root` is inspected.
                (root / "empty-clients").mkdir(exist_ok=True)
                (root / "empty-mono").mkdir(exist_ok=True)

                # Fresh dry-run marker for this tool/box.
                marker = (
                    root / ".skillbox-state" / "dryrun-markers"
                    / f".skillbox-dryrun-{friendly}-local"
                )
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("dry-run completed\n", encoding="utf-8")

                result = self._run_guard(
                    script,
                    tool_name=tool,
                    tool_input={"dry_run": False, "box_id": "local"},
                    extra_env={
                        "SKILLBOX_MONOSERVER_HOST_ROOT": str(root / "empty-mono"),
                        "SKILLBOX_CLIENTS_HOST_ROOT": str(root / "empty-clients"),
                    },
                )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_default_scan_does_not_walk_repo_parent(self) -> None:
        # Scope-inflation guard: by default the scan must NOT reach a dirty
        # sibling repo in the parent of REPO_ROOT (the old repo_root/.. default).
        with tempfile.TemporaryDirectory() as parentdir, tempfile.TemporaryDirectory() as baredir:
            parent = Path(parentdir)
            root = parent / "skillbox"
            root.mkdir()
            script = self._copy_guard(root)

            # Dirty sibling repo next to REPO_ROOT.
            sibling = parent / "unrelated-sibling"
            sibling.mkdir()
            subprocess.run(["git", "init", "-q", str(sibling)], check=True, capture_output=True, text=True)
            (sibling / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

            # REPO_ROOT itself is clean+pushed with a valid marker.
            bare = Path(baredir) / "upstream.git"
            subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True, text=True)
            self._git_init_clean_pushed(root, bare)
            (root / "empty-clients").mkdir(exist_ok=True)
            marker = (
                root / ".skillbox-state" / "dryrun-markers"
                / ".skillbox-dryrun-operator_compose_down-local"
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("dry-run completed\n", encoding="utf-8")

            # No SKILLBOX_MONOSERVER_HOST_ROOT -> bounded default, sibling ignored.
            env = os.environ.copy()
            env.pop("SKILLBOX_MONOSERVER_HOST_ROOT", None)
            env["SKILLBOX_CLIENTS_HOST_ROOT"] = str(root / "empty-clients")
            allowed = subprocess.run(
                ["bash", str(script)],
                input=json.dumps(
                    {
                        "tool_name": self.GATED_COMPOSE_DOWN,
                        "tool_input": {"dry_run": False},
                    }
                ),
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertNotIn("unrelated-sibling", allowed.stderr)

            # Explicit opt-in (operator monorepo layout) DOES scan the parent.
            env["SKILLBOX_MONOSERVER_HOST_ROOT"] = str(parent)
            blocked = subprocess.run(
                ["bash", str(script)],
                input=json.dumps(
                    {
                        "tool_name": self.GATED_COMPOSE_DOWN,
                        "tool_input": {"dry_run": False},
                    }
                ),
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("unrelated-sibling", blocked.stderr)

    def _git_init_clean_pushed(self, repo: Path, bare: Path) -> None:
        """Initialise ``repo`` as a clean working tree pushed to ``bare``."""
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        git("config", "user.email", "guard-test@example.com")
        git("config", "user.name", "guard-test")
        git("config", "commit.gpgsign", "false")
        # Ignore the volatile state dir so creating the marker/log keeps the
        # tree clean.
        (repo / ".gitignore").write_text(
            ".skillbox-state/\nempty-clients/\nempty-mono/\n", encoding="utf-8"
        )
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        git("remote", "add", "origin", str(bare))
        git("push", "-q", "-u", "origin", "HEAD")


class QuickValidateScriptTests(unittest.TestCase):
    def test_validate_skill_reports_document_frontmatter_name_description_and_todo_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            self.assertEqual(
                QUICK_VALIDATE.validate_skill(root / "missing"),
                (False, "SKILL.md not found"),
            )

            no_frontmatter = root / "no-frontmatter"
            no_frontmatter.mkdir()
            (no_frontmatter / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(no_frontmatter),
                (False, "No YAML frontmatter found"),
            )

            bad_yaml = root / "bad-yaml"
            _write_skill(bad_yaml, "name: [unterminated")
            self.assertFalse(QUICK_VALIDATE.validate_skill(bad_yaml)[0])
            self.assertIn("Invalid YAML", QUICK_VALIDATE.validate_skill(bad_yaml)[1])

            scalar_frontmatter = root / "scalar-frontmatter"
            _write_skill(scalar_frontmatter, "just-a-string")
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(scalar_frontmatter),
                (False, "Frontmatter must be a YAML dictionary"),
            )

            unexpected = root / "unexpected"
            _write_skill(
                unexpected,
                "name: valid-name\n"
                "description: This description is long enough to avoid the short warning.\n"
                "surprise: true",
            )
            self.assertFalse(QUICK_VALIDATE.validate_skill(unexpected)[0])
            self.assertIn("Unexpected key", QUICK_VALIDATE.validate_skill(unexpected)[1])

            missing_name = root / "missing-name"
            _write_skill(missing_name, "description: This description is long enough to validate.")
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(missing_name),
                (False, "Missing 'name' in frontmatter"),
            )

            missing_description = root / "missing-description"
            _write_skill(missing_description, "name: valid-name")
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(missing_description),
                (False, "Missing 'description' in frontmatter"),
            )

            empty_name = root / "empty-name"
            _write_skill(
                empty_name,
                'name: "   "\n'
                "description: This description is long enough to validate.",
            )
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(empty_name),
                (False, "Name cannot be empty"),
            )

            empty_description = root / "empty-description"
            _write_skill(empty_description, 'name: valid-name\ndescription: "   "')
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(empty_description),
                (False, "Description cannot be empty"),
            )

            bad_name = root / "bad-name"
            _write_skill(
                bad_name,
                "name: Bad_Name\n"
                "description: This description is long enough to validate.",
            )
            self.assertFalse(QUICK_VALIDATE.validate_skill(bad_name)[0])
            self.assertIn("hyphen-case", QUICK_VALIDATE.validate_skill(bad_name)[1])

            bad_description = root / "bad-description"
            _write_skill(bad_description, "name: valid-name\ndescription: contains <html>")
            self.assertEqual(
                QUICK_VALIDATE.validate_skill(bad_description),
                (False, "Description cannot contain angle brackets (< or >)"),
            )

            todo = root / "todo"
            _write_skill(
                todo,
                "name: valid-name\n"
                "description: This description is long enough to validate.",
                "\n[TODO: finish this]\n",
            )
            self.assertFalse(QUICK_VALIDATE.validate_skill(todo)[0])
            self.assertIn("Incomplete skill", QUICK_VALIDATE.validate_skill(todo)[1])

    def test_validate_skill_collects_warning_modes_and_main_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = root / "valid"
            _write_skill(
                skill,
                "name: valid-name\n"
                "description: short",
            )
            (skill / "references").mkdir()
            key = "tok" + "en"
            value = "abcd" * 4
            (skill / "notes.txt").write_text(f'{key} = "{value}"\n', encoding="utf-8")

            valid, message = QUICK_VALIDATE.validate_skill(skill)
            self.assertTrue(valid)
            self.assertIn("Description is short", message)
            self.assertIn("Possible secret", message)
            self.assertIn("Empty directory: references/", message)

            strict_valid, strict_message = QUICK_VALIDATE.validate_skill(skill, strict=True)
            self.assertFalse(strict_valid)
            self.assertIn("Strict mode failed", strict_message)

            long_skill = root / "long"
            _write_skill(
                long_skill,
                "name: long-name\n"
                "description: This description is long enough to validate.",
                "\n".join(f"line {index}" for index in range(505)),
            )
            self.assertIn("recommended max: 500", QUICK_VALIDATE.validate_skill(long_skill)[1])

            with (
                mock.patch.object(sys, "argv", ["quick_validate.py"]),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(QUICK_VALIDATE.main(), 1)
            self.assertIn("Usage: python quick_validate.py", stdout.getvalue())

            with (
                mock.patch.object(sys, "argv", ["quick_validate.py", str(skill)]),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(QUICK_VALIDATE.main(), 0)
            self.assertIn("Skill is valid with warnings", stdout.getvalue())

    def test_validate_name_and_description_reject_empty_required_values(self) -> None:
        self.assertEqual(QUICK_VALIDATE._skill_body("---\nunterminated"), "")  # noqa: SLF001
        self.assertEqual(
            QUICK_VALIDATE._parse_frontmatter("---\nname: valid-name"),  # noqa: SLF001
            (False, "Invalid frontmatter format"),
        )

        class UnreadableFile:
            def relative_to(self, _root: Path) -> Path:
                return Path("notes.txt")

            def read_text(self, **_kwargs: object) -> str:
                raise OSError("blocked")

        warnings: list[str] = []
        with mock.patch.object(QUICK_VALIDATE, "iter_included_skill_files", return_value=[UnreadableFile()]):
            QUICK_VALIDATE._collect_privacy_warnings(Path("/skill"), warnings)  # noqa: SLF001
        self.assertEqual(warnings, [])

        self.assertEqual(QUICK_VALIDATE._validate_name(""), (False, "Name cannot be empty"))  # noqa: SLF001
        self.assertEqual(QUICK_VALIDATE._validate_name("  "), (False, "Name cannot be empty"))  # noqa: SLF001
        self.assertFalse(QUICK_VALIDATE._validate_name(123)[0])  # noqa: SLF001
        self.assertFalse(QUICK_VALIDATE._validate_name("-bad")[0])  # noqa: SLF001
        self.assertFalse(QUICK_VALIDATE._validate_name("a" * 65)[0])  # noqa: SLF001
        warnings: list[str] = []
        self.assertEqual(
            QUICK_VALIDATE._validate_description("", warnings),  # noqa: SLF001
            (False, "Description cannot be empty"),
        )
        self.assertEqual(
            QUICK_VALIDATE._validate_description("  ", warnings),  # noqa: SLF001
            (False, "Description cannot be empty"),
        )
        self.assertFalse(QUICK_VALIDATE._validate_description(123, warnings)[0])  # noqa: SLF001
        self.assertFalse(QUICK_VALIDATE._validate_description("x" * 1025, warnings)[0])  # noqa: SLF001


class HermesCodexAdapterTests(unittest.TestCase):
    def _write_task(self, root: Path) -> tuple[Path, Path]:
        task_path = root / "task.json"
        result_path = root / "result.json"
        task_path.write_text(
            json.dumps(
                {
                    "task_spec": {
                        "task_class": "analysis",
                        "instruction": "Inspect the repo and report readiness.",
                    },
                    "resolved_context": {
                        "client_id": "skillbox",
                        "repo_id": "skillbox",
                        "effective_cwd": str(root),
                    },
                }
            ),
            encoding="utf-8",
        )
        return task_path, result_path

    def test_missing_codex_writes_terminal_worker_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_path, result_path = self._write_task(root)
            env = {
                "SKILLBOX_ROOT_DIR": str(root),
                "SKILLBOX_WORKER_RUN_ID": "wr_test",
                "SKILLBOX_WORKER_TASK_PATH": str(task_path),
                "SKILLBOX_WORKER_RESULT_PATH": str(result_path),
            }

            with (
                mock.patch.dict(HERMES_CODEX.os.environ, env, clear=True),
                mock.patch.object(HERMES_CODEX.shutil, "which", return_value=None),
            ):
                self.assertEqual(HERMES_CODEX.main(), 0)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "failed")
            self.assertIn("Codex CLI is not installed", result["summary"])
            self.assertEqual(result["next_action"], "Install codex or set SKILLBOX_HERMES_CODEX_BIN.")

    def test_success_invokes_codex_exec_read_only_and_persists_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_path, result_path = self._write_task(root)
            captured: dict[str, object] = {}

            def fake_run(command: list[str], *_args: object, **kwargs: object) -> object:
                captured["command"] = command
                captured["cwd"] = kwargs.get("cwd")
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text("Adapter dogfood report.", encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            env = {
                "SKILLBOX_ROOT_DIR": str(root),
                "SKILLBOX_WORKER_RUN_ID": "wr_test",
                "SKILLBOX_WORKER_TASK_PATH": str(task_path),
                "SKILLBOX_WORKER_RESULT_PATH": str(result_path),
                "SKILLBOX_HERMES_CODEX_BIN": "/opt/bin/codex",
                "SKILLBOX_HERMES_CODEX_MODEL": "gpt-test",
            }
            with (
                mock.patch.dict(HERMES_CODEX.os.environ, env, clear=True),
                mock.patch.object(HERMES_CODEX, "_run_codex", side_effect=fake_run),
            ):
                self.assertEqual(HERMES_CODEX.main(), 0)

            command = captured["command"]
            self.assertIsInstance(command, list)
            self.assertEqual(command[:2], ["/opt/bin/codex", "exec"])
            self.assertIn("read-only", command)
            self.assertIn("gpt-test", command)
            self.assertIn("Inspect the repo", command[-1])
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["summary"], "Adapter dogfood report.")
            self.assertEqual(result["actions_taken"], ["Ran codex exec through the Skillbox Hermes adapter."])

    def test_codex_nonzero_exit_becomes_worker_failure_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_path, result_path = self._write_task(root)
            env = {
                "SKILLBOX_ROOT_DIR": str(root),
                "SKILLBOX_WORKER_RUN_ID": "wr_test",
                "SKILLBOX_WORKER_TASK_PATH": str(task_path),
                "SKILLBOX_WORKER_RESULT_PATH": str(result_path),
                "SKILLBOX_HERMES_CODEX_BIN": "/opt/bin/codex",
            }
            with (
                mock.patch.dict(HERMES_CODEX.os.environ, env, clear=True),
                mock.patch.object(
                    HERMES_CODEX,
                    "_run_codex",
                    return_value=types.SimpleNamespace(returncode=17, stdout="", stderr="auth failed"),
                ),
            ):
                self.assertEqual(HERMES_CODEX.main(), 0)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "failed")
            self.assertIn("code 17", result["summary"])
            self.assertIn("auth failed", result["summary"])


class CmStdioBridgeScriptTests(unittest.TestCase):
    def test_tcp_ready_post_json_wait_and_emit_helpers_cover_success_and_failures(self) -> None:
        connection = mock.Mock()
        connection.__enter__ = mock.Mock(return_value=connection)
        connection.__exit__ = mock.Mock(return_value=None)
        with mock.patch.object(CM_BRIDGE.socket, "create_connection", return_value=connection):
            self.assertTrue(CM_BRIDGE.tcp_ready("127.0.0.1", 3222))
        with mock.patch.object(CM_BRIDGE.socket, "create_connection", side_effect=OSError("closed")):
            self.assertFalse(CM_BRIDGE.tcp_ready("127.0.0.1", 3222))

        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.body

        with mock.patch.object(CM_BRIDGE, "urlopen", return_value=Response(b'{"ok": true}')) as urlopen:
            self.assertEqual(CM_BRIDGE.post_json("http://cm", {"id": 1}, 3), {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")

        with mock.patch.object(CM_BRIDGE, "urlopen", return_value=Response(b"")):
            self.assertIsNone(CM_BRIDGE.post_json("http://cm", {"id": 1}, 3))
        with mock.patch.object(CM_BRIDGE, "urlopen", return_value=Response(b"not-json")):
            with self.assertRaisesRegex(RuntimeError, "non-JSON response"):
                CM_BRIDGE.post_json("http://cm", {"id": 1}, 3)
        with mock.patch.object(CM_BRIDGE, "urlopen", return_value=Response(b'["bad"]')):
            with self.assertRaisesRegex(RuntimeError, "unexpected response type"):
                CM_BRIDGE.post_json("http://cm", {"id": 1}, 3)

        http_error = urllib.error.HTTPError(
            "http://cm",
            503,
            "unavailable",
            hdrs={},
            fp=io.BytesIO(b"down"),
        )
        with mock.patch.object(CM_BRIDGE, "urlopen", side_effect=http_error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503: down"):
                CM_BRIDGE.post_json("http://cm", {"id": 1}, 3)
        with mock.patch.object(CM_BRIDGE, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                CM_BRIDGE.post_json("http://cm", {"id": 1}, 3)

        with (
            mock.patch.object(CM_BRIDGE, "tcp_ready", side_effect=[False, True]),
            mock.patch.object(CM_BRIDGE.time, "sleep") as sleep,
        ):
            CM_BRIDGE.wait_for_server("127.0.0.1", 3222, 1.0, None)
        sleep.assert_called_once()

        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.returncode = 7
        with mock.patch.object(CM_BRIDGE, "tcp_ready", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cm exited with code 7"):
                CM_BRIDGE.wait_for_server("127.0.0.1", 3222, 1.0, proc)
        with (
            mock.patch.object(CM_BRIDGE, "tcp_ready", return_value=False),
            mock.patch.object(CM_BRIDGE.time, "monotonic", side_effect=[1.0, 1.0]),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out waiting"):
                CM_BRIDGE.wait_for_server("127.0.0.1", 3222, 0.0, None)

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            CM_BRIDGE.emit_error(None, "ignored")
            CM_BRIDGE.emit_error(3, "bad")
            CM_BRIDGE.emit_result(None, {"ignored": True})
            CM_BRIDGE.emit_result(4, {"ok": True})
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["message"], "bad")
        self.assertEqual(lines[1]["result"], {"ok": True})
        self.assertEqual(CM_BRIDGE.initialize_result()["serverInfo"]["name"], "cm")
        self.assertEqual(
            CM_BRIDGE.normalize_response(9, {"result": {"tools": []}}),
            {"jsonrpc": "2.0", "id": 9, "result": {"tools": []}},
        )
        self.assertEqual(CM_BRIDGE.normalize_response(10, {"tools": []})["result"], {"tools": []})

    def test_main_bridges_stdio_requests_without_spawning_when_http_server_is_ready(self) -> None:
        args = types.SimpleNamespace(
            cm_command="cm",
            host="127.0.0.1",
            port=3222,
            startup_timeout=1.0,
            request_timeout=2.0,
        )
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
            '{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n'
            'not-json\n'
            '[]\n'
        )

        with (
            mock.patch.object(CM_BRIDGE, "parse_args", return_value=args),
            mock.patch.object(CM_BRIDGE, "tcp_ready", return_value=True),
            mock.patch.object(CM_BRIDGE, "wait_for_server") as wait_for_server,
            mock.patch.object(CM_BRIDGE, "post_json", return_value={"result": {"tools": []}}),
            mock.patch.object(CM_BRIDGE.atexit, "register"),
            mock.patch.object(CM_BRIDGE.signal, "signal"),
            mock.patch("sys.stdin", stdin),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(CM_BRIDGE.main(), 0)

        wait_for_server.assert_called_once_with("127.0.0.1", 3222, 1.0, None)
        messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(messages[0]["result"]["serverInfo"]["name"], "cm")
        self.assertEqual(messages[1]["result"], {})
        self.assertEqual(messages[2]["result"], {"tools": []})

    def test_main_reports_startup_failure_and_parse_args_reads_cli_values(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "cm_stdio_bridge.py",
                "--cm-command",
                "cm-dev",
                "--host",
                "0.0.0.0",
                "--port",
                "3333",
                "--startup-timeout",
                "4",
                "--request-timeout",
                "5",
            ],
        ):
            args = CM_BRIDGE.parse_args()
        self.assertEqual(args.cm_command, "cm-dev")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 3333)
        self.assertEqual(args.startup_timeout, 4.0)
        self.assertEqual(args.request_timeout, 5.0)

        main_args = types.SimpleNamespace(
            cm_command="cm",
            host="127.0.0.1",
            port=3222,
            startup_timeout=1.0,
            request_timeout=2.0,
        )
        with (
            mock.patch.object(CM_BRIDGE, "parse_args", return_value=main_args),
            mock.patch.object(CM_BRIDGE, "tcp_ready", return_value=True),
            mock.patch.object(CM_BRIDGE, "wait_for_server", side_effect=RuntimeError("no server")),
            mock.patch.object(CM_BRIDGE.atexit, "register"),
            mock.patch.object(CM_BRIDGE.signal, "signal"),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(CM_BRIDGE.main(), 1)
        self.assertIn("no server", stdout.getvalue())


class _UpstreamHandler(BaseHTTPRequestHandler):
    seen_headers: dict[str, str] = {}
    seen_body = b""

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(body=False)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        type(self).seen_body = self.rfile.read(length)
        self._respond()

    def _respond(self, *, body: bool = True) -> None:
        type(self).seen_headers = {key: value for key, value in self.headers.items()}
        payload = json.dumps({"path": self.path, "method": self.command}).encode("utf-8")
        self.send_response(202, "Accepted")
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_threaded_server(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class IngressProxyScriptTests(unittest.TestCase):
    def test_route_store_sorting_matching_health_and_reload_error_paths(self) -> None:
        routes = [
            {"id": "prefix-short", "path": "/api", "match": "prefix"},
            {"id": "exact", "path": "/api/users", "match": "exact"},
            {"id": "prefix-long", "path": "/api/users", "match": "prefix"},
        ]
        self.assertEqual(
            [route["id"] for route in INGRESS.sort_routes(routes)],
            ["exact", "prefix-long", "prefix-short"],
        )
        self.assertTrue(INGRESS.path_matches({"path": "/api/users"}, "/api/users"))
        self.assertFalse(INGRESS.path_matches({"path": "/api/users"}, "/api/users/1"))
        self.assertTrue(INGRESS.path_matches({"path": "/api", "match": "prefix"}, "/api/users"))
        self.assertTrue(INGRESS.path_matches({"path_prefix": "/haas", "match": "prefix"}, "/haas/assets/app.js"))
        self.assertTrue(INGRESS.path_matches({"path": "/", "match": "prefix"}, "/anything"))
        self.assertEqual(
            [route["id"] for route in INGRESS.sort_routes([
                {"id": "prefix-short", "path": "/api", "match": "prefix"},
                {"id": "prefix-alias", "path_prefix": "/api/reports", "match": "prefix"},
            ])],
            ["prefix-alias", "prefix-short"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            route_file = Path(tmpdir) / "routes.json"
            store = INGRESS.RouteStore(route_file)
            self.assertEqual(store.routes_for("public"), [])
            self.assertTrue(store.health_payload()["ok"])

            route_file.write_text("{not-json", encoding="utf-8")
            self.assertEqual(store.routes_for("public"), [])
            self.assertFalse(store.health_payload()["ok"])

            route_file.write_text(
                json.dumps(
                    {
                        "routes": [
                            {"id": "private", "listener": "private", "path": "/private"},
                            {"id": "public", "path": "/public"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store._mtime_ns = None  # noqa: SLF001
            self.assertEqual([route["id"] for route in store.routes_for("private")], ["private"])
            self.assertEqual(store.health_payload()["routes"]["public"], 1)

    def test_proxy_handler_serves_health_errors_and_successful_upstream_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            route_file = Path(tmpdir) / "routes.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
            upstream_thread = _start_threaded_server(upstream)
            self.addCleanup(upstream.shutdown)
            self.addCleanup(upstream.server_close)
            self.addCleanup(upstream_thread.join, 1.0)
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"

            route_file.write_text(
                json.dumps(
                    {
                        "routes": [
                            {
                                "id": "api",
                                "listener": "public",
                                "path": "/api",
                                "match": "prefix",
                                "origin_url": upstream_url,
                            },
                            {"id": "empty", "listener": "public", "path": "/empty", "origin_url": ""},
                            {"id": "bad", "listener": "public", "path": "/bad", "origin_url": "ftp://bad"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = INGRESS.RouteStore(route_file)
            proxy = INGRESS.IngressServer(
                ("127.0.0.1", 0),
                INGRESS.ProxyHandler,
                listener_name="public",
                route_store=store,
            )
            proxy_thread = _start_threaded_server(proxy)
            self.addCleanup(proxy.shutdown)
            self.addCleanup(proxy.server_close)
            self.addCleanup(proxy_thread.join, 1.0)
            base_url = f"http://127.0.0.1:{proxy.server_port}"

            health = json.loads(urllib.request.urlopen(f"{base_url}/__skillbox/health").read().decode("utf-8"))
            self.assertTrue(health["ok"])

            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"{base_url}/missing")
            self.assertEqual(missing.exception.code, 404)
            self.assertIn("No ingress route", missing.exception.read().decode("utf-8"))

            for path, expected in (("/empty", "has no upstream"), ("/bad", "invalid upstream")):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as exc:
                        urllib.request.urlopen(f"{base_url}{path}")
                    self.assertEqual(exc.exception.code, 502)
                    self.assertIn(expected, exc.exception.read().decode("utf-8"))

            request = urllib.request.Request(
                f"{base_url}/api/users?active=1",
                headers={"X-Forwarded-For": "10.0.0.1", "Connection": "close"},
            )
            response = urllib.request.urlopen(request)
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 202)
            self.assertEqual(payload["path"], "/api/users?active=1")
            self.assertEqual(_UpstreamHandler.seen_headers["X-Forwarded-For"].split(", ")[0], "10.0.0.1")
            self.assertEqual(_UpstreamHandler.seen_headers["X-Skillbox-Ingress"], "public")
            self.assertNotIn("Connection", _UpstreamHandler.seen_headers)

            post = urllib.request.Request(
                f"{base_url}/api/events",
                data=b'{"ok": true}',
                method="POST",
            )
            self.assertEqual(urllib.request.urlopen(post).status, 202)
            self.assertEqual(_UpstreamHandler.seen_body, b'{"ok": true}')

            head = urllib.request.Request(f"{base_url}/api/head", method="HEAD")
            self.assertEqual(urllib.request.urlopen(head).status, 202)

    def test_parse_args_and_main_wire_two_servers_and_signal_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            routes = Path(tmpdir) / "routes.json"
            routes.write_text('{"routes":[]}', encoding="utf-8")
            with mock.patch.object(sys, "argv", ["ingress_proxy.py", "--routes-file", str(routes)]):
                args = INGRESS.parse_args()
            self.assertEqual(args.routes_file, str(routes))
            self.assertEqual(args.public_host, "127.0.0.1")

        class FakeServer:
            def __init__(self, address, _handler, *, listener_name, route_store) -> None:
                self.server_address = address
                self.listener_name = listener_name
                self.route_store = route_store
                self.shutdown = mock.Mock()
                self.server_close = mock.Mock()

            def serve_forever(self, **_kwargs) -> None:
                return

        class FakeThread:
            def __init__(self, *, target, kwargs, daemon) -> None:
                self.target = target
                self.kwargs = kwargs
                self.daemon = daemon

            def start(self) -> None:
                self.target(**self.kwargs)

            def join(self, timeout: float | None = None) -> None:
                return

        handlers: list[object] = []

        def signal_capture(_sig: int, handler: object) -> None:
            handlers.append(handler)

        stop_event = mock.Mock()
        stop_event.wait.side_effect = lambda: handlers[0](15, None)
        main_args = types.SimpleNamespace(
            routes_file="/tmp/routes.json",
            public_host="127.0.0.1",
            public_port=18080,
            private_host="127.0.0.1",
            private_port=19080,
        )
        with (
            mock.patch.object(INGRESS, "parse_args", return_value=main_args),
            mock.patch.object(INGRESS, "IngressServer", side_effect=FakeServer) as server_factory,
            mock.patch.object(INGRESS.threading, "Event", return_value=stop_event),
            mock.patch.object(INGRESS.threading, "Thread", side_effect=FakeThread),
            mock.patch.object(INGRESS.signal, "signal", side_effect=signal_capture),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(INGRESS.main(), 0)
        self.assertEqual(server_factory.call_count, 2)
        self.assertEqual(server_factory.call_args_list[0].kwargs["listener_name"], "public")
        self.assertEqual(server_factory.call_args_list[1].kwargs["listener_name"], "private")


class StubWebScriptTests(unittest.TestCase):
    def test_stub_web_serves_index_and_not_found(self) -> None:
        server = STUB_WEB.ThreadingHTTPServer(("127.0.0.1", 0), STUB_WEB.Handler)
        thread = _start_threaded_server(server)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1.0)
        base_url = f"http://127.0.0.1:{server.server_port}"

        index = urllib.request.urlopen(f"{base_url}/").read().decode("utf-8")
        self.assertIn("<h1>skillbox</h1>", index)
        self.assertIn("workspace/runtime.yaml", index)

        index_html = urllib.request.urlopen(f"{base_url}/index.html").read().decode("utf-8")
        self.assertIn("Internal Runtime Manager", index_html)

        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base_url}/missing")
        self.assertEqual(exc.exception.code, 404)
        self.assertEqual(exc.exception.read(), b"not found")

    def test_stub_web_main_starts_threading_server(self) -> None:
        server = mock.Mock()
        with (
            mock.patch.object(STUB_WEB, "ThreadingHTTPServer", return_value=server) as server_class,
            mock.patch("sys.stdout"),
        ):
            STUB_WEB.main()
        server_class.assert_called_once_with(("0.0.0.0", STUB_WEB.PORT), STUB_WEB.Handler)
        server.serve_forever.assert_called_once()


if __name__ == "__main__":
    unittest.main()
