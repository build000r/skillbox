from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / "scripts" / "e2e-smoke.sh"


class E2ESmokeScriptTests(unittest.TestCase):
    def _write_executable(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _make_fake_root(self, root: Path) -> Path:
        (root / "workspace").mkdir()
        (root / ".skillbox-state").mkdir()
        (root / "logs").mkdir()
        (root / "home").mkdir()
        (root / "repos").mkdir()
        (root / "skills").mkdir()
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (root / "docker-compose.monoserver.yml").write_text("services: {}\n", encoding="utf-8")
        (root / "README.md").write_text("fake\n", encoding="utf-8")
        (root / "Makefile").write_text("fake:\n\t@true\n", encoding="utf-8")

        self._write_executable(
            root / "scripts" / "04-reconcile.py",
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            command = sys.argv[1]
            if command == "render":
                if os.environ.get("SKILLBOX_FAKE_RENDER_FAIL") == "1":
                    print("bad runtime fixture", file=sys.stderr)
                    sys.exit(1)
                print(json.dumps({
                    "sandbox": {},
                    "runtime_manager": {},
                    "expected_files": [],
                    "expected_mounts": [],
                }))
            elif command == "doctor":
                if os.environ.get("SKILLBOX_FAKE_DOCTOR_FAIL") == "1":
                    print(json.dumps([{"status": "fail", "code": "planted-breakage"}]))
                    sys.exit(1)
                print(json.dumps([{"status": "pass", "code": "ok"}]))
            else:
                raise SystemExit(f"unexpected reconcile command: {command}")
            """,
        )
        self._write_executable(
            root / ".env-manager" / "manage.py",
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            root = Path(__file__).resolve().parents[1]
            command = sys.argv[1]
            if command == "render":
                print(json.dumps({
                    "root_dir": str(root),
                    "repos": [],
                    "skills": [],
                    "services": [],
                    "logs": [],
                    "checks": [],
                }))
            elif command == "sync":
                if os.environ.get("SKILLBOX_FAKE_MUTATE") == "1":
                    (root / "workspace" / "mutated.txt").write_text("bad", encoding="utf-8")
                print(json.dumps({"dry_run": True, "actions": []}))
            elif command == "capabilities":
                print(json.dumps({"ok": True, "commands": [], "registry": {}}))
            elif command == "next":
                print(json.dumps({"ok": True, "recommendations": []}))
            else:
                raise SystemExit(f"unexpected manage command: {command}")
            """,
        )
        self._write_executable(
            root / "scripts" / "stub_api.py",
            """
            #!/usr/bin/env python3
            import json
            import os
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            if os.environ.get("SKILLBOX_FAKE_STUB_IMPORT_FAIL") == "1":
                raise RuntimeError("broken stub import")

            host = os.environ["SKILLBOX_API_HOST"]
            port = int(os.environ["SKILLBOX_API_PORT"])

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def do_GET(self):
                    if self.path == "/health":
                        payload = {"ok": True, "service": "skillbox-api"}
                    elif self.path == "/v1/sandbox":
                        payload = {"name": "skillbox", "runtime_manager": {}}
                    elif self.path == "/v1/runtime":
                        payload = {
                            "manifest": "workspace/runtime.yaml",
                            "repos": [],
                            "skills": [],
                            "services": [],
                            "logs": [],
                            "checks": [],
                        }
                    else:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            ThreadingHTTPServer((host, port), Handler).serve_forever()
            """,
        )
        self._write_executable(
            root / "scripts" / "stub_web.py",
            """
            #!/usr/bin/env python3
            import os
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            host = os.environ["SKILLBOX_WEB_HOST"]
            port = int(os.environ["SKILLBOX_WEB_PORT"])

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def do_GET(self):
                    if self.path != "/":
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = b"<html><body><h1>skillbox</h1></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            ThreadingHTTPServer((host, port), Handler).serve_forever()
            """,
        )
        return root

    def _run_smoke(
        self,
        fake_root: Path,
        *args: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        bin_dir = fake_root / "bin"
        self._write_executable(
            bin_dir / "docker",
            """
            #!/usr/bin/env bash
            set -u
            if [[ "${1:-}" == "compose" ]]; then
              shift
              for arg in "$@"; do
                if [[ "$arg" == "config" ]]; then
                  exit 0
                fi
              done
            fi
            echo "unexpected docker args: $*" >&2
            exit 1
            """,
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env.get('PATH', '')}",
                "PYTHON": sys.executable,
                "SKILLBOX_E2E_ROOT_DIR": str(fake_root),
                "SKILLBOX_E2E_STEP_TIMEOUT_SECONDS": "3",
                "SKILLBOX_E2E_STUB_TIMEOUT_SECONDS": "1",
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_json_mode_passes_and_reports_no_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(fake_root, "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["fail"], 0)
        self.assertTrue(payload["state_mutation"]["ok"])
        self.assertEqual(payload["ports"]["host"], "127.0.0.1")
        self.assertIn("stub-api", {step["name"] for step in payload["steps"]})

    def test_doctor_failure_warns_unless_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(
                fake_root,
                "--format",
                "json",
                extra_env={"SKILLBOX_FAKE_DOCTOR_FAIL": "1"},
            )
            strict = self._run_smoke(
                fake_root,
                "--format",
                "json",
                "--strict",
                extra_env={"SKILLBOX_FAKE_DOCTOR_FAIL": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        doctor = next(step for step in payload["steps"] if step["name"] == "doctor")
        self.assertEqual(doctor["status"], "WARN")
        self.assertNotEqual(strict.returncode, 0)
        strict_payload = json.loads(strict.stdout)
        strict_doctor = next(step for step in strict_payload["steps"] if step["name"] == "doctor")
        self.assertEqual(strict_doctor["status"], "FAIL")

    def test_occupied_declared_api_port_fails_stub_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            fake_root = self._make_fake_root(Path(tmpdir))
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = str(sock.getsockname()[1])
            result = self._run_smoke(
                fake_root,
                "--format",
                "json",
                extra_env={"SKILLBOX_E2E_API_PORT": port},
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        api_step = next(step for step in payload["steps"] if step["name"] == "stub-api")
        self.assertEqual(api_step["status"], "FAIL")

    def test_render_breakage_fails_render_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(
                fake_root,
                "--format",
                "json",
                extra_env={"SKILLBOX_FAKE_RENDER_FAIL": "1"},
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        render_step = next(step for step in payload["steps"] if step["name"] == "render")
        self.assertEqual(render_step["status"], "FAIL")

    def test_broken_stub_import_fails_api_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(
                fake_root,
                "--format",
                "json",
                extra_env={"SKILLBOX_FAKE_STUB_IMPORT_FAIL": "1"},
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        api_step = next(step for step in payload["steps"] if step["name"] == "stub-api")
        self.assertEqual(api_step["status"], "FAIL")

    def test_mtime_mutation_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(
                fake_root,
                "--format",
                "json",
                extra_env={"SKILLBOX_FAKE_MUTATE": "1"},
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["state_mutation"]["ok"])
        changed = {entry["path"] for entry in payload["state_mutation"]["changed"]}
        self.assertIn("workspace/mutated.txt", changed)

    def test_human_mode_prints_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = self._make_fake_root(Path(tmpdir))
            result = self._run_smoke(fake_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STEP", result.stdout)
        self.assertIn("STATUS", result.stdout)
        self.assertIn("result: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
