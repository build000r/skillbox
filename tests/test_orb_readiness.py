import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts/orb/orb_readiness.py"
SPEC = importlib.util.spec_from_file_location("orb_readiness", COLLECTOR)
assert SPEC is not None and SPEC.loader is not None
ORB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORB)


class OrbReadinessTests(unittest.TestCase):
    def test_receipt_has_exact_schema_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            subprocess.run([str(COLLECTOR), "collect", "--context", "manual", "--output", str(output)], check=True)
            receipt = json.loads(output.read_text())
            self.assertEqual(
                set(receipt),
                {
                    "schema_version",
                    "project_alias",
                    "context",
                    "state",
                    "reason_code",
                    "network_attempted",
                    "external_readiness_claimed",
                    "capabilities",
                },
            )
            self.assertEqual(receipt["schema_version"], "skillbox.amp-project-orb.readiness/1")
            self.assertEqual(receipt["project_alias"], "build000r/skillbox")
            self.assertIn(receipt["state"], {"ready", "configured", "degraded", "blocked", "forbidden"})
            self.assertFalse(receipt["network_attempted"])
            self.assertFalse(receipt["external_readiness_claimed"])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            classes = {item["class"] for item in receipt["capabilities"]}
            self.assertEqual(classes, {"required_local", "optional_presence", "forbidden_authority"})
            self.assertTrue(all(set(item) == {"id", "class", "state", "reason_code"}
                                for item in receipt["capabilities"]))

    def test_presence_only_configuration_never_emits_values(self):
        secret = "value-that-must-never-appear"
        receipt = ORB.collect(
            "manual",
            env={
                "SBP_REMOTE": secret,
                "SBP_PROJECT_ALIAS": "build000r/skillbox",
                "SPAPS_REMOTE_READ_URL": secret,
            },
        )
        by_id = {item["id"]: item for item in receipt["capabilities"]}
        self.assertEqual(by_id["sbpd.remote_read"]["state"], "configured")
        self.assertEqual(by_id["sweet_potato.spaps_read"]["state"], "configured")
        self.assertNotIn(secret, json.dumps(receipt))

    def test_resume_identity_is_stable_private_and_not_in_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "orb-resume-id"
            ORB.ensure_identity(identity)
            first = identity.read_text(encoding="ascii")
            ORB.ensure_identity(identity)
            self.assertEqual(identity.read_text(encoding="ascii"), first)
            self.assertEqual(stat.S_IMODE(identity.stat().st_mode), 0o600)
            self.assertNotIn(first.strip(), json.dumps(ORB.collect("manual", env={})))

    def test_hook_preparation_replaces_stale_files_privately_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            logs = root / "logs"
            state.mkdir(mode=0o755)
            logs.mkdir(mode=0o755)
            status = state / "setup-status.json"
            status.write_text('{"status":"stale-success"}\n', encoding="utf-8")
            log = logs / "setup.log"
            log.write_text("stale private path\n", encoding="utf-8")
            ORB.prepare_hook(state, logs, "setup")
            self.assertFalse(status.exists())
            self.assertEqual(log.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

            target = root / "target.log"
            target.write_text("operator-owned\n", encoding="utf-8")
            log.unlink()
            log.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular files"):
                ORB.prepare_hook(state, logs, "setup")
            self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned\n")

    def test_missing_timeout_emits_current_typed_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["PATH"] = directory
            result = subprocess.run(
                ["/bin/bash", str(ROOT / ".agents/resume")],
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        self.assertEqual(result.returncode, 20)
        marker = "AGENT_RESUME_RESULT_JSON "
        receipt = json.loads(result.stderr.split(marker, 1)[1])
        self.assertEqual(receipt["reason_code"], "timeout_command_missing")
        self.assertEqual(receipt["failure_class"], "dependency")

    def test_resume_readiness_timeout_is_bounded_and_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(AGENT_STATE_DIR=directory, AGENT_LOG_DIR=directory,
                       SKILLBOX_ORB_RESUME_MIN_FREE_GB="0",
                       SKILLBOX_ORB_RESUME_READINESS_TIMEOUT_SECONDS="1",
                       SKILLBOX_ORB_TEST_READINESS_DELAY_SECONDS="5")
            start = time.monotonic()
            result = subprocess.run([str(ROOT / ".agents/resume")], env=env, capture_output=True,
                                    text=True, timeout=5, check=False)
            self.assertLess(time.monotonic() - start, 4)
            self.assertEqual(result.returncode, 50)
            status = json.loads((Path(directory) / "resume-status.json").read_text())
            self.assertEqual((status["failure_class"], status["reason_code"]), ("validation", "readiness_timeout"))
            self.assertEqual(stat.S_IMODE((Path(directory) / "resume-status.json").stat().st_mode), 0o600)

    def test_resume_does_not_invoke_network_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            marker = root / "network-attempted"
            for name in ("amp", "curl", "pip", "pip3", "ssh", "tailscale", "wget"):
                planted = binaries / name
                planted.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' {name} >>'{marker}'\nexit 99\n",
                    encoding="utf-8",
                )
                planted.chmod(0o755)
            state = root / "state"
            logs = root / "logs"
            env = os.environ.copy()
            env.update(
                PATH=f"{binaries}:{env['PATH']}",
                AGENT_STATE_DIR=str(state),
                AGENT_LOG_DIR=str(logs),
                SKILLBOX_ORB_RESUME_MIN_FREE_GB="0",
            )
            result = subprocess.run(
                [str(ROOT / ".agents/resume")],
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            receipt = json.loads((state / "orb-readiness.json").read_text())
            network_attempted = marker.exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(network_attempted)
        self.assertFalse(receipt["network_attempted"])

    def test_setup_compile_timeout_is_bounded_and_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "python3"
            real_python = os.path.realpath(os.environ.get("PYTHON", "/usr/bin/python3"))
            fake.write_text(f"#!/bin/sh\ncase \"$*\" in *compileall*) sleep 5;; *) exec {real_python} \"$@\";; esac\n")
            fake.chmod(0o755)
            state = Path(directory) / "state"
            logs = Path(directory) / "logs"
            env = os.environ.copy()
            env.update(PATH=f"{directory}:{env['PATH']}", AGENT_STATE_DIR=str(state), AGENT_LOG_DIR=str(logs),
                       SKILLBOX_ORB_MIN_FREE_GB="0", SKILLBOX_ORB_COMPILE_TIMEOUT_SECONDS="1")
            start = time.monotonic()
            result = subprocess.run([str(ROOT / ".agents/setup")], env=env, capture_output=True,
                                    text=True, timeout=6, check=False)
            self.assertLess(time.monotonic() - start, 5)
            self.assertEqual(result.returncode, 50)
            status = json.loads((state / "setup-status.json").read_text())
            self.assertEqual((status["failure_class"], status["reason_code"]), ("validation", "compileall_timeout"))


if __name__ == "__main__":
    unittest.main()
