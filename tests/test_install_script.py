from __future__ import annotations

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
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--skip-build", result.stdout)
        self.assertIn("--skip-up", result.stdout)

    def test_dry_run_does_not_create_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"

            result = self._run(
                "--source-dir",
                str(ROOT_DIR),
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
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"

            result = self._run(
                "--source-dir",
                str(ROOT_DIR),
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
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"
            repo_dir.mkdir(parents=True, exist_ok=True)
            note_path = repo_dir / "note.txt"
            note_path.write_text("keep me\n", encoding="utf-8")

            result = self._run(
                "--source-dir",
                str(ROOT_DIR),
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

    def test_verify_runs_post_install_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            private_dir = root / "skillbox-config"

            result = self._run(
                "--source-dir",
                str(ROOT_DIR),
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

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        return subprocess.run(
            ["bash", str(INSTALL_SCRIPT), *args],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )


if __name__ == "__main__":
    unittest.main()
