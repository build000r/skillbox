from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UPGRADE_SCRIPT = ROOT_DIR / "scripts" / "06-upgrade-release.sh"


FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_upgrade"
ASSERT_ROLLBACK = FIXTURES / "assert-rollback.sh"

# Managed DCG state, mirrored from scripts/06-upgrade-release.sh.
DCG_MANAGED_RELPATHS = (
    ".claude/settings.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".config/dcg/config.toml",
    ".config/dcg/skillbox-reconcile.json",
)
UNRELATED_RELPATH = ".config/unrelated/keep.txt"


class DcgHomeMixin(unittest.TestCase):
    """Every test pins SKILLBOX_DCG_HOME at a temp dir.

    Without this the upgrade script falls back to $HOME, and the rollback path
    would write to the operator's REAL ~/.claude and ~/.config/dcg during a unit
    test run. Tests must never be able to touch operator DCG state.
    """

    def _base_env(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["TMPDIR"] = str(root)
        env["SKILLBOX_DCG_HOME"] = str(self._dcg_home(root))
        env["SKILLBOX_DCG_BIN"] = str(self._dcg_home(root) / ".local" / "bin" / "dcg")
        return env

    def _dcg_home(self, root: Path) -> Path:
        return root / "dcg-home"

    def _seed_dcg_home(self, root: Path) -> Path:
        home = self._dcg_home(root)
        payloads = {
            ".claude/settings.json": '{"hooks":{"PreToolUse":["prior"]}}\n',
            ".codex/hooks.json": '{"hooks":{"PreToolUse":["prior"]}}\n',
            ".codex/config.toml": '[hooks.state."dcg"]\ntrusted_hash = "prior"\n',
            ".config/dcg/config.toml": "fail_closed = true\nprior = true\n",
            ".config/dcg/skillbox-reconcile.json": '{"backups":{"last":"prior"}}\n',
            UNRELATED_RELPATH: "unrelated operator file\n",
        }
        for relpath, body in payloads.items():
            target = home / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        binary = home / ".local" / "bin" / "dcg"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\necho v0.6.7\n", encoding="utf-8")
        binary.chmod(0o755)
        return home

    def _managed_digest(self, root: Path) -> str:
        home = self._dcg_home(root)
        material = ""
        for relpath in DCG_MANAGED_RELPATHS:
            target = home / relpath
            digest = (
                hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "absent"
            )
            material += f"{relpath}={digest}\n"
        binary = home / ".local" / "bin" / "dcg"
        digest = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else "absent"
        material += f"binary={digest}\n"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _unrelated_digest(self, root: Path) -> str:
        target = self._dcg_home(root) / UNRELATED_RELPATH
        if not target.is_file():
            return "absent"
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _assert_rollback_ok(self, root: Path, managed: str, unrelated: str) -> str:
        result = subprocess.run(
            ["bash", str(ASSERT_ROLLBACK), str(self._dcg_home(root)), managed, unrelated],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SKILLBOX_DCG_BIN": str(self._dcg_home(root) / ".local" / "bin" / "dcg")},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DCG_ROLLBACK_OK", result.stdout)
        return result.stdout


class UpgradeReleaseScriptTests(DcgHomeMixin):
    def test_upgrade_release_does_not_require_make_on_remote_host(self) -> None:
        script = UPGRADE_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("require_cmd make", script)
        self.assertIn("repo_lifecycle_target", script)
        self.assertIn('repo_dir="$(cd "${repo_dir}" && pwd -P)"', script)

    def test_upgrade_release_seeds_env_into_operator_state_not_repo_root(self) -> None:
        # skillbox-4c9s: a repo-root .env seed trips the secrets containment
        # doctor checks on fresh upgrades; the seed must target the operator dir.
        script = UPGRADE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('cp "${REPO_DIR}/.env.example" "${OPERATOR_ENV_DIR}/.env"', script)
        self.assertIn('chmod 600 "${OPERATOR_ENV_DIR}/.env"', script)
        self.assertNotIn('cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"', script)

    def test_upgrade_release_preserves_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            self._write_repo(repo_dir, version="old")
            self._write_runtime_state(repo_dir)

            archive_path = self._build_release_archive(root, version="new")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            env = self._base_env(root)
            env["SKILLBOX_TEST_EXPECT_PROFILE"] = "connectors"
            env["TMPDIR"] = tmpdir

            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADE_SCRIPT),
                    "--archive",
                    str(archive_path),
                    "--sha256",
                    archive_sha256,
                    "--repo-dir",
                    str(repo_dir),
                    "--client",
                    "personal",
                    "--profile",
                    "connectors",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo_dir / "VERSION.txt").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((repo_dir / ".build-version").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((repo_dir / ".up-version").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((repo_dir / ".env").read_text(encoding="utf-8"), "SECRET=1\n")
            self.assertEqual((repo_dir / ".mcp.json").read_text(encoding="utf-8"), '{"servers":["skillbox"]}\n')
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "clients" / "personal" / "context.yaml").read_text(encoding="utf-8"),
                "client: personal\n",
            )
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "home" / ".codex" / "skills" / "custom.md").read_text(encoding="utf-8"),
                "keep home\n",
            )
            self.assertEqual((repo_dir / ".skillbox-state" / "logs" / "api" / "api.log").read_text(encoding="utf-8"), "keep log\n")
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "monoserver" / "custom-skill" / "README.md").read_text(encoding="utf-8"),
                "keep monoserver\n",
            )
            self.assertEqual(
                (repo_dir / "workspace" / ".compose-overrides" / "docker-compose.client-personal.yml").read_text(encoding="utf-8"),
                "services: {}\n",
            )
            self.assertEqual((repo_dir / "workspace" / ".focus.json").read_text(encoding="utf-8"), '{"client_id":"personal"}\n')
            self.assertEqual(
                (repo_dir / "workspace" / "skill-repos" / "custom-skill" / "README.md").read_text(encoding="utf-8"),
                "keep skill repo\n",
            )
            self.assertFalse((repo_dir / "repos" / "client-a" / "README.md").exists())
            self.assertFalse((repo_dir / "sand" / "personal" / "report.txt").exists())
            self.assertFalse((repo_dir / "data" / "state.json").exists())
            self.assertFalse((root / "skillbox.rollback").exists())

    def test_upgrade_release_rolls_back_on_acceptance_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            self._write_repo(repo_dir, version="old")
            self._write_runtime_state(repo_dir)

            archive_path = self._build_release_archive(root, version="new")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            env = self._base_env(root)
            env["SKILLBOX_TEST_ACCEPTANCE_FAIL"] = "1"
            env["TMPDIR"] = tmpdir

            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADE_SCRIPT),
                    "--archive",
                    str(archive_path),
                    "--sha256",
                    archive_sha256,
                    "--repo-dir",
                    str(repo_dir),
                    "--client",
                    "personal",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((repo_dir / "VERSION.txt").read_text(encoding="utf-8"), "old\n")
            self.assertEqual((repo_dir / ".env").read_text(encoding="utf-8"), "SECRET=1\n")
            self.assertEqual((repo_dir / "repos" / "client-a" / "README.md").read_text(encoding="utf-8"), "keep repo\n")
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "clients" / "personal" / "context.yaml").read_text(encoding="utf-8"),
                "client: personal\n",
            )
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "home" / ".codex" / "skills" / "custom.md").read_text(encoding="utf-8"),
                "keep home\n",
            )
            self.assertEqual((repo_dir / ".skillbox-state" / "logs" / "api" / "api.log").read_text(encoding="utf-8"), "keep log\n")
            self.assertEqual(
                (repo_dir / ".skillbox-state" / "monoserver" / "custom-skill" / "README.md").read_text(encoding="utf-8"),
                "keep monoserver\n",
            )
            self.assertEqual(
                (repo_dir / "workspace" / ".compose-overrides" / "docker-compose.client-personal.yml").read_text(encoding="utf-8"),
                "services: {}\n",
            )
            self.assertEqual((repo_dir / "workspace" / ".focus.json").read_text(encoding="utf-8"), '{"client_id":"personal"}\n')
            self.assertEqual(
                (repo_dir / "workspace" / "skill-repos" / "custom-skill" / "README.md").read_text(encoding="utf-8"),
                "keep skill repo\n",
            )
            self.assertEqual((repo_dir / "sand" / "personal" / "report.txt").read_text(encoding="utf-8"), "keep sand\n")
            self.assertEqual((repo_dir / "data" / "state.json").read_text(encoding="utf-8"), '{"ready":true}\n')
            self.assertEqual((repo_dir / ".up-version").read_text(encoding="utf-8"), "old\n")
            self.assertFalse((repo_dir / ".build-version").exists())
            self.assertFalse((root / "skillbox.rollback").exists())

    def test_upgrade_blocks_when_install_lock_held_by_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            self._write_repo(repo_dir, version="old")

            archive_path = self._build_release_archive(root, version="new")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            lock_tmp = root / "tmp"
            lock_tmp.mkdir()
            lock_dir = lock_tmp / "skillbox-install.lock"
            lock_dir.mkdir()
            (lock_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

            env = self._base_env(root)
            env["TMPDIR"] = str(lock_tmp)

            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADE_SCRIPT),
                    "--archive",
                    str(archive_path),
                    "--sha256",
                    archive_sha256,
                    "--repo-dir",
                    str(repo_dir),
                    "--client",
                    "personal",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("appears to be running", result.stderr)
            # The existing checkout must be untouched and the foreign lock kept.
            self.assertEqual((repo_dir / "VERSION.txt").read_text(encoding="utf-8"), "old\n")
            self.assertTrue(lock_dir.is_dir())
            self.assertEqual(
                (lock_dir / "pid").read_text(encoding="utf-8").strip(),
                str(os.getpid()),
            )

    def _build_release_archive(self, root: Path, *, version: str) -> Path:
        source_root = root / "archive-src" / "skillbox"
        self._write_repo(source_root, version=version)
        archive_path = root / f"skillbox-{version}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname="skillbox")
        return archive_path

    def _write_repo(self, repo_dir: Path, *, version: str) -> None:
        (repo_dir / ".env-manager").mkdir(parents=True, exist_ok=True)
        (repo_dir / "VERSION.txt").write_text(f"{version}\n", encoding="utf-8")
        (repo_dir / ".env.example").write_text("DEFAULT=1\n", encoding="utf-8")
        (repo_dir / "Makefile").write_text(
            textwrap.dedent(
                """\
                build:
                \t@python3 -c "from pathlib import Path; Path('.build-version').write_text(Path('VERSION.txt').read_text(encoding='utf-8'), encoding='utf-8')"

                up:
                \t@python3 -c "from pathlib import Path; Path('.up-version').write_text(Path('VERSION.txt').read_text(encoding='utf-8'), encoding='utf-8')"

                down:
                \t@python3 -c "from pathlib import Path; Path('.down-version').write_text(Path('VERSION.txt').read_text(encoding='utf-8'), encoding='utf-8')"
                """
            ),
            encoding="utf-8",
        )
        (repo_dir / ".env-manager" / "manage.py").write_text(
            textwrap.dedent(
                """\
                import json
                import os
                import sys
                from pathlib import Path

                root = Path(__file__).resolve().parents[1]
                args = sys.argv[1:]

                # DCG lifecycle stub (skillbox-dcg-upgrade-rollback-n8lu).
                # Each probe fails a DIFFERENT stage of the guard so the
                # transaction can be proven to roll back from every one.
                if args and args[0] == "dcg-reconcile":
                    action = ""
                    idx = 1
                    while idx < len(args):
                        if args[idx] == "--action" and idx + 1 < len(args):
                            action = args[idx + 1]
                        idx += 1
                    home = Path(os.environ.get("SKILLBOX_DCG_HOME", "")).expanduser()

                    def _damage(relpath, body):
                        target = home / relpath
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(body, encoding="utf-8")

                    if action == "apply":
                        # A real converge rewrites managed state; do the same so
                        # rollback has something to actually undo.
                        _damage(".config/dcg/config.toml", "fail_closed = true\\nupgraded = true\\n")
                        _damage(".claude/settings.json", '{"hooks":{"PreToolUse":["upgraded"]}}\\n')
                        if os.environ.get("SKILLBOX_TEST_DCG_POLICY_FAIL") == "1":
                            print(json.dumps({"error": {"message": "policy render failed"}}))
                            raise SystemExit(1)
                        if os.environ.get("SKILLBOX_TEST_DCG_HOOK_FAIL") == "1":
                            print(json.dumps({"error": {"message": "hook write failed"}}))
                            raise SystemExit(1)
                        if os.environ.get("SKILLBOX_TEST_DCG_TRUST_FAIL") == "1":
                            print(json.dumps({"error": {"message": "codex trust absent"}}))
                            raise SystemExit(3)
                        print(json.dumps({"ok": True, "marker": "DCG_CHANGED"}))
                        raise SystemExit(0)

                    if action == "verify":
                        if os.environ.get("SKILLBOX_TEST_DCG_DOCTOR_FAIL") == "1":
                            print(json.dumps({"error": {"message": "dcg doctor failed"}}))
                            raise SystemExit(1)
                        print(json.dumps({"ok": True, "marker": "DCG_HEALTHY"}))
                        raise SystemExit(0)

                    print(json.dumps({"error": {"message": "unsupported action"}}))
                    raise SystemExit(1)

                if len(args) < 2 or args[0] != "acceptance":
                    print(json.dumps({"error": {"message": "unsupported"}}))
                    raise SystemExit(1)

                profiles = []
                idx = 2
                while idx < len(args):
                    if args[idx] == "--profile" and idx + 1 < len(args):
                        profiles.append(args[idx + 1])
                        idx += 2
                        continue
                    idx += 1

                expected = os.environ.get("SKILLBOX_TEST_EXPECT_PROFILE", "").strip()
                if expected and expected not in profiles:
                    print(json.dumps({"error": {"message": "missing expected profile"}}))
                    raise SystemExit(1)

                if os.environ.get("SKILLBOX_TEST_ACCEPTANCE_FAIL") == "1":
                    print(json.dumps({"error": {"message": "acceptance failed"}}))
                    raise SystemExit(1)

                print(json.dumps({
                    "ready": True,
                    "version": root.joinpath("VERSION.txt").read_text(encoding="utf-8").strip(),
                    "profiles": profiles,
                }))
                """
            ),
            encoding="utf-8",
        )

    def _write_runtime_state(self, repo_dir: Path) -> None:
        (repo_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (repo_dir / ".mcp.json").write_text('{"servers":["skillbox"]}\n', encoding="utf-8")
        (repo_dir / "repos" / "client-a").mkdir(parents=True, exist_ok=True)
        (repo_dir / "repos" / "client-a" / "README.md").write_text("keep repo\n", encoding="utf-8")
        (repo_dir / ".skillbox-state" / "clients" / "personal").mkdir(parents=True, exist_ok=True)
        (repo_dir / ".skillbox-state" / "clients" / "personal" / "context.yaml").write_text("client: personal\n", encoding="utf-8")
        (repo_dir / ".skillbox-state" / "home" / ".codex" / "skills").mkdir(parents=True, exist_ok=True)
        (repo_dir / ".skillbox-state" / "home" / ".codex" / "skills" / "custom.md").write_text("keep home\n", encoding="utf-8")
        (repo_dir / ".skillbox-state" / "logs" / "api").mkdir(parents=True, exist_ok=True)
        (repo_dir / ".skillbox-state" / "logs" / "api" / "api.log").write_text("keep log\n", encoding="utf-8")
        (repo_dir / ".skillbox-state" / "monoserver" / "custom-skill").mkdir(parents=True, exist_ok=True)
        (repo_dir / ".skillbox-state" / "monoserver" / "custom-skill" / "README.md").write_text("keep monoserver\n", encoding="utf-8")
        (repo_dir / "workspace" / ".compose-overrides").mkdir(parents=True, exist_ok=True)
        (repo_dir / "workspace" / ".compose-overrides" / "docker-compose.client-personal.yml").write_text("services: {}\n", encoding="utf-8")
        (repo_dir / "workspace" / ".focus.json").write_text('{"client_id":"personal"}\n', encoding="utf-8")
        (repo_dir / "workspace" / "skill-repos" / "custom-skill").mkdir(parents=True, exist_ok=True)
        (repo_dir / "workspace" / "skill-repos" / "custom-skill" / "README.md").write_text("keep skill repo\n", encoding="utf-8")
        (repo_dir / "sand" / "personal").mkdir(parents=True, exist_ok=True)
        (repo_dir / "sand" / "personal" / "report.txt").write_text("keep sand\n", encoding="utf-8")
        (repo_dir / "data").mkdir(parents=True, exist_ok=True)
        (repo_dir / "data" / "state.json").write_text('{"ready":true}\n', encoding="utf-8")


class DcgUpgradeTests(DcgHomeMixin):
    """DCG joins the upgrade transaction (skillbox-dcg-upgrade-rollback-n8lu).

    An upgrade that leaves DCG degraded is worse than one that fails: the host
    keeps working while nothing guards the agent's shell. So the guard is
    captured only after the archive verifies, re-validated before success, and
    fully restored on any failure.
    """

    # Reuse the fixture-repo builders from the sibling suite.
    _write_repo = UpgradeReleaseScriptTests._write_repo
    _write_runtime_state = UpgradeReleaseScriptTests._write_runtime_state
    _build_release_archive = UpgradeReleaseScriptTests._build_release_archive

    def _run_upgrade(
        self,
        root: Path,
        *,
        version: str = "new",
        prior: str = "old",
        sha_override: str | None = None,
        archive_override: Path | None = None,
        extra_env: dict[str, str] | None = None,
        receipt: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        repo_dir = root / "skillbox"
        if not repo_dir.exists():
            self._write_repo(repo_dir, version=prior)
            self._write_runtime_state(repo_dir)
        archive_path = archive_override or self._build_release_archive(root, version=version)
        sha256 = sha_override or hashlib.sha256(archive_path.read_bytes()).hexdigest()

        env = self._base_env(root)
        if extra_env:
            env.update(extra_env)

        argv = [
            "bash", str(UPGRADE_SCRIPT),
            "--archive", str(archive_path),
            "--sha256", sha256,
            "--repo-dir", str(repo_dir),
            "--client", "personal",
        ]
        if receipt is not None:
            argv += ["--receipt", str(receipt)]
        return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)

    # --- happy path + receipt ------------------------------------------------

    def test_successful_upgrade_writes_a_receipt_with_every_required_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            receipt = root / "receipt.json"

            result = self._run_upgrade(root, prior="v0.6.7", version="v0.6.8", receipt=receipt)
            self.assertEqual(0, result.returncode, result.stderr)

            payload = json.loads(receipt.read_text(encoding="utf-8"))
            for marker in (
                "before_version",
                "after_version",
                "binary_sha256",
                "policy_sha256",
                "hook_state_sha256",
                "rollback_bundle_sha256",
            ):
                self.assertIn(marker, payload, f"receipt is missing {marker}")
                self.assertTrue(str(payload[marker]).strip(), f"{marker} is empty")

            self.assertEqual("v0.6.7", payload["before_version"])
            self.assertEqual("v0.6.8", payload["after_version"])
            self.assertFalse(payload["unchanged"])

    def test_prior_pin_to_v067_is_recorded_as_the_before_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            receipt = root / "receipt.json"
            self._run_upgrade(root, prior="v0.6.7", version="v0.6.8", receipt=receipt)
            self.assertEqual(
                "v0.6.7", json.loads(receipt.read_text(encoding="utf-8"))["before_version"]
            )

    def test_same_version_rerun_is_marked_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            receipt = root / "receipt.json"
            result = self._run_upgrade(root, prior="v0.6.7", version="v0.6.7", receipt=receipt)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["before_version"], payload["after_version"])
            self.assertTrue(payload["unchanged"], "a same-version rerun must report unchanged")

    def test_dcg_is_converged_and_revalidated_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            result = self._run_upgrade(root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Converging DCG", result.stdout)
            self.assertIn("Re-validating DCG before declaring success", result.stdout)

    def test_capture_happens_after_archive_verification(self) -> None:
        """An unverified artifact must never get to touch the guard."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            result = self._run_upgrade(root, sha_override="0" * 64)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn("Captured DCG rollback bundle", result.stdout)

    # --- failure probes ------------------------------------------------------

    def _probe_rolls_back(self, probe_env: dict[str, str], *, corrupt_archive: bool = False,
                          bad_sha: bool = False) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            expected_managed = self._managed_digest(root)
            expected_unrelated = self._unrelated_digest(root)

            kwargs: dict = {"extra_env": probe_env}
            if bad_sha:
                kwargs["sha_override"] = "0" * 64
            if corrupt_archive:
                bogus = root / "corrupt.tar.gz"
                bogus.write_bytes(b"not a tarball")
                kwargs["archive_override"] = bogus
                kwargs["sha_override"] = hashlib.sha256(bogus.read_bytes()).hexdigest()

            result = self._run_upgrade(root, **kwargs)

            self.assertNotEqual(0, result.returncode, "probe must exit nonzero")
            self._assert_rollback_ok(root, expected_managed, expected_unrelated)
            # The checkout must be back on the prior release too.
            self.assertEqual(
                "old\n", (root / "skillbox" / "VERSION.txt").read_text(encoding="utf-8")
            )

    def test_probe_artifact_failure_rolls_back(self) -> None:
        self._probe_rolls_back({}, corrupt_archive=True)

    def test_probe_signature_failure_rolls_back(self) -> None:
        self._probe_rolls_back({}, bad_sha=True)

    def test_probe_policy_failure_rolls_back(self) -> None:
        self._probe_rolls_back({"SKILLBOX_TEST_DCG_POLICY_FAIL": "1"})

    def test_probe_hook_failure_rolls_back(self) -> None:
        self._probe_rolls_back({"SKILLBOX_TEST_DCG_HOOK_FAIL": "1"})

    def test_probe_trust_failure_rolls_back(self) -> None:
        self._probe_rolls_back({"SKILLBOX_TEST_DCG_TRUST_FAIL": "1"})

    def test_probe_doctor_failure_rolls_back(self) -> None:
        self._probe_rolls_back({"SKILLBOX_TEST_DCG_DOCTOR_FAIL": "1"})

    def test_probe_acceptance_failure_rolls_back(self) -> None:
        self._probe_rolls_back({"SKILLBOX_TEST_ACCEPTANCE_FAIL": "1"})

    # --- rollback quality ----------------------------------------------------

    def test_converge_really_does_damage_so_rollback_is_not_vacuous(self) -> None:
        """If apply changed nothing, every probe above would pass trivially."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed_dcg_home(root)
            before = self._managed_digest(root)
            self._run_upgrade(root)  # succeeds; converge mutates managed state
            self.assertNotEqual(
                before, self._managed_digest(root),
                "the stubbed converge must mutate managed state, or the probes prove nothing",
            )

    def test_rollback_restores_policy_and_hook_bytes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = self._seed_dcg_home(root)
            policy_before = (home / ".config/dcg/config.toml").read_bytes()
            hook_before = (home / ".claude/settings.json").read_bytes()

            result = self._run_upgrade(root, extra_env={"SKILLBOX_TEST_DCG_DOCTOR_FAIL": "1"})
            self.assertNotEqual(0, result.returncode)

            self.assertEqual(policy_before, (home / ".config/dcg/config.toml").read_bytes())
            self.assertEqual(hook_before, (home / ".claude/settings.json").read_bytes())

    def test_rollback_leaves_unrelated_operator_files_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = self._seed_dcg_home(root)
            unrelated = home / UNRELATED_RELPATH
            before = unrelated.read_bytes()
            self._run_upgrade(root, extra_env={"SKILLBOX_TEST_DCG_DOCTOR_FAIL": "1"})
            self.assertEqual(before, unrelated.read_bytes())

    def test_rollback_removes_a_file_that_did_not_exist_before(self) -> None:
        """A file the upgrade created must not survive rollback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = self._seed_dcg_home(root)
            created = home / ".codex" / "hooks.json"
            created.unlink()  # absent before the upgrade
            expected_managed = self._managed_digest(root)
            expected_unrelated = self._unrelated_digest(root)

            # apply writes managed state; doctor then fails.
            result = self._run_upgrade(root, extra_env={"SKILLBOX_TEST_DCG_DOCTOR_FAIL": "1"})
            self.assertNotEqual(0, result.returncode)
            self._assert_rollback_ok(root, expected_managed, expected_unrelated)
            self.assertFalse(created.exists(), "rollback must not resurrect an absent file")

    def test_assert_rollback_fixture_detects_managed_drift(self) -> None:
        """The fixture must be able to FAIL, or it proves nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = self._seed_dcg_home(root)
            managed = self._managed_digest(root)
            unrelated = self._unrelated_digest(root)
            (home / ".config/dcg/config.toml").write_text("tampered\n", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(ASSERT_ROLLBACK), str(home), managed, unrelated],
                capture_output=True, text=True, check=False,
                env={**os.environ, "SKILLBOX_DCG_BIN": str(home / ".local" / "bin" / "dcg")},
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("DCG_ROLLBACK_MANAGED_DRIFT", result.stderr)
            self.assertNotIn("DCG_ROLLBACK_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
