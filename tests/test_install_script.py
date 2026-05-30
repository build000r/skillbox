from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = ROOT_DIR / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def test_help_lists_key_flags(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source-dir", result.stdout)
        self.assertIn("--private-path", result.stdout)
        self.assertIn("--client", result.stdout)
        self.assertIn("--offline", result.stdout)
        self.assertIn("--skip-first-box", result.stdout)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--skip-build", result.stdout)
        self.assertIn("--skip-up", result.stdout)
        self.assertIn("--install-wrappers", result.stdout)
        self.assertIn("--wrapper-bin-dir", result.stdout)

    def test_dry_run_does_not_create_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--dry-run",
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("source: planned", result.stdout)
            self.assertFalse(repo_dir.exists())
            self.assertFalse(private_dir.exists())

    def test_local_source_install_creates_private_repo_and_open_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo_dir / ".env").is_file())
            self.assertTrue((private_dir / ".git").exists())
            self.assertTrue((private_dir / "clients" / "personal" / "overlay.yaml").is_file())
            self.assertTrue((repo_dir / "sand" / "personal" / "CLAUDE.md").is_file())

    def test_existing_nonempty_target_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            note_path = repo_dir / "note.txt"
            note_path.write_text("keep me\n", encoding="utf-8")

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--no-gum",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Checkout target already exists", result.stderr)
            self.assertEqual(note_path.read_text(encoding="utf-8"), "keep me\n")

    def test_force_refuses_non_skillbox_target_and_preserves_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "not-skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            note_path = repo_dir / "sentinel.txt"
            note_path.write_text("do not delete\n", encoding="utf-8")

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--force",
                "--no-gum",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing --force replacement of non-Skillbox directory", result.stderr)
            self.assertEqual(note_path.read_text(encoding="utf-8"), "do not delete\n")

    def test_force_refuses_home_target_and_preserves_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            home = root / "home"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)
            home.mkdir(parents=True, exist_ok=True)
            note_path = home / "sentinel.txt"
            note_path.write_text("do not delete\n", encoding="utf-8")

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(home),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--force",
                "--no-gum",
                extra_env={"HOME": str(home)},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing --force replacement of protected install target", result.stderr)
            self.assertEqual(note_path.read_text(encoding="utf-8"), "do not delete\n")

    def test_force_allows_existing_skillbox_checkout_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)
            (repo_dir / ".env-manager").mkdir(parents=True, exist_ok=True)
            (repo_dir / ".env-manager" / "manage.py").write_text("# existing\n", encoding="utf-8")
            (repo_dir / "install.sh").write_text("# existing\n", encoding="utf-8")
            (repo_dir / "README.md").write_text("# existing\n", encoding="utf-8")
            old_file = repo_dir / "old.txt"
            old_file.write_text("replace me\n", encoding="utf-8")

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-first-box",
                "--skip-build",
                "--skip-up",
                "--force",
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo_dir / ".env").is_file())
            self.assertFalse(old_file.exists())

    def test_verify_runs_post_install_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-build",
                "--skip-up",
                "--verify",
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verify: ok", result.stdout)
            invocations = [
                json.loads(line)["argv"][0]
                for line in (repo_dir / "manage-invocations.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(invocations, ["first-box", "doctor", "status"])

    def test_skip_first_box_leaves_private_repo_uncreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            self._write_fake_source(source_dir)

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-first-box",
                "--skip-build",
                "--skip-up",
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo_dir / ".env").is_file())
            self.assertFalse(private_dir.exists())
            self.assertFalse((repo_dir / "sand" / "personal").exists())
            self.assertIn("first_box: skipped", result.stdout)

    def test_install_wrappers_creates_repo_owned_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            bin_dir = root / "bin"
            self._write_fake_source(source_dir)

            result = self._run(
                "--source-dir",
                str(source_dir),
                "--repo-dir",
                str(repo_dir),
                "--private-path",
                str(private_dir),
                "--client",
                "personal",
                "--skip-first-box",
                "--skip-build",
                "--skip-up",
                "--install-wrappers",
                "--wrapper-bin-dir",
                str(bin_dir),
                "--no-gum",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bin_dir / "sbp").is_symlink())
            self.assertTrue((bin_dir / "sbo").is_symlink())
            self.assertEqual((bin_dir / "sbp").resolve(), (repo_dir / "scripts" / "sbp").resolve())
            self.assertEqual((bin_dir / "sbo").resolve(), (repo_dir / "scripts" / "sbo").resolve())
            self.assertIn("wrappers: ok", result.stdout)

    def _write_fake_source(self, source_dir: Path) -> None:
        (source_dir / ".env-manager").mkdir(parents=True, exist_ok=True)
        (source_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (source_dir / "README.md").write_text("# Skillbox test fixture\n", encoding="utf-8")
        (source_dir / "install.sh").write_text("# test fixture\n", encoding="utf-8")
        (source_dir / ".env.example").write_text(
            "\n".join(
                [
                    "SKILLBOX_STATE_ROOT=./.skillbox-state",
                    "SKILLBOX_MONOSERVER_HOST_ROOT=${SKILLBOX_STATE_ROOT}/monoserver",
                    "SKILLBOX_CM_MCP_PORT=0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        for wrapper_name in ("sbp", "sbo"):
            wrapper_path = source_dir / "scripts" / wrapper_name
            wrapper_path.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"${1:-}\" == \"--help\" ]]; then echo help; exit 0; fi\n"
                "echo ok\n",
                encoding="utf-8",
            )
            wrapper_path.chmod(0o755)
        (source_dir / ".env-manager" / "manage.py").write_text(
            """from __future__ import annotations

import json
import sys
from pathlib import Path


def arg_value(flag: str) -> str:
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


root = Path.cwd()
argv = sys.argv[1:]
command = argv[0] if argv else ""
with (root / "manage-invocations.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv}) + "\\n")

if command == "first-box":
    client = argv[1] if len(argv) > 1 else "personal"
    output_dir = root / "sand" / client
    private_path = Path(arg_value("--private-path") or root.parent / "skillbox-config")
    output_dir.mkdir(parents=True, exist_ok=True)
    private_path.mkdir(parents=True, exist_ok=True)
    (output_dir / "CLAUDE.md").write_text("# test context\\n", encoding="utf-8")
    (private_path / ".git").mkdir(parents=True, exist_ok=True)
    client_dir = private_path / "clients" / client
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "overlay.yaml").write_text("client: test\\n", encoding="utf-8")
    print(json.dumps({
        "client_id": client,
        "output_dir": str(output_dir),
        "private_repo": {"target_dir": str(private_path)},
        "steps": [],
    }))
    raise SystemExit(0)

if command in {"doctor", "status"}:
    print(json.dumps({"ok": True, "command": command}))
    raise SystemExit(0)

print(json.dumps({"ok": False, "command": command}), file=sys.stderr)
raise SystemExit(2)
""",
            encoding="utf-8",
        )

    def _run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        if extra_env:
            env.update(extra_env)
        with tempfile.TemporaryDirectory() as lock_tmp:
            env["TMPDIR"] = lock_tmp
            return subprocess.run(
                ["bash", str(INSTALL_SCRIPT), *args],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )


class InstallScriptChecksumGateTests(unittest.TestCase):
    """install.sh must refuse to skip checksum verification by default.

    Added by the sec-hardening-20260516 slice (C2). Prior to the fix,
    verify_checksum() warned-and-returned-0 when the expected SHA was empty,
    silently allowing a curl|bash install of an unverified tarball.
    """

    @staticmethod
    def _extract_function(func_name: str) -> str:
        """Pull a single bash function body out of install.sh."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        marker = f"{func_name}() {{"
        start = text.index(marker)
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        raise ValueError(f"Could not find end of {func_name}() in install.sh")

    def _harness(self, body: str) -> str:
        """Bash snippet that defines verify_checksum + its deps, then runs body."""
        deps = "\n".join(
            self._extract_function(name)
            for name in ("have_cmd", "sha256_file", "verify_checksum")
        )
        # warn/err are tiny — provide stubs that just emit to stderr so we
        # don't have to drag in install.sh's color/QUIET/has_gum globals.
        return f"""
set -u
warn() {{ printf 'WARN: %s\\n' "$*" >&2; }}
err()  {{ printf 'ERROR: %s\\n' "$*" >&2; }}
{deps}

{body}
"""

    def test_help_advertises_allow_unverified_flag(self) -> None:
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--allow-unverified", result.stdout)

    def test_verify_checksum_fails_on_empty_expected_sha(self) -> None:
        """verify_checksum() must exit non-zero when expected SHA is empty."""
        with tempfile.NamedTemporaryFile("w", suffix=".bin", delete=False) as tf:
            tf.write("hello world")
            target = tf.name
        try:
            script = self._harness(f"ALLOW_UNVERIFIED=0\nverify_checksum {target} ''")
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertNotEqual(
                result.returncode, 0,
                f"verify_checksum returned 0 on empty SHA; stderr={result.stderr!r}",
            )
            combined = (result.stdout + result.stderr).lower()
            self.assertIn("allow-unverified", combined)
        finally:
            os.unlink(target)

    def test_verify_checksum_accepts_explicit_allow_unverified(self) -> None:
        """With ALLOW_UNVERIFIED=1, empty SHA should warn-and-pass."""
        with tempfile.NamedTemporaryFile("w", suffix=".bin", delete=False) as tf:
            tf.write("hello world")
            target = tf.name
        try:
            script = self._harness(
                f"ALLOW_UNVERIFIED=1\nverify_checksum {target} ''\necho exit=$?"
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
            self.assertIn("exit=0", result.stdout)
        finally:
            os.unlink(target)

    def test_verify_checksum_accepts_matching_sha(self) -> None:
        """Sanity: with a correct SHA, verify_checksum passes."""
        import hashlib as _hashlib
        with tempfile.NamedTemporaryFile("w", suffix=".bin", delete=False) as tf:
            tf.write("hello world")
            target = tf.name
        try:
            expected = _hashlib.sha256(b"hello world").hexdigest()
            script = self._harness(
                f"ALLOW_UNVERIFIED=0\nverify_checksum {target} {expected}\necho exit=$?"
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
            self.assertIn("exit=0", result.stdout)
        finally:
            os.unlink(target)


if __name__ == "__main__":
    unittest.main()
