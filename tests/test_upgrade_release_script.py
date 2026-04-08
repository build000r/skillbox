from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UPGRADE_SCRIPT = ROOT_DIR / "scripts" / "06-upgrade-release.sh"


class UpgradeReleaseScriptTests(unittest.TestCase):
    def test_upgrade_release_preserves_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_dir = root / "skillbox"
            self._write_repo(repo_dir, version="old")
            self._write_runtime_state(repo_dir)

            archive_path = self._build_release_archive(root, version="new")
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

            env = dict(os.environ)
            env["SKILLBOX_TEST_EXPECT_PROFILE"] = "connectors"

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

            env = dict(os.environ)
            env["SKILLBOX_TEST_ACCEPTANCE_FAIL"] = "1"

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
                f"""\
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


if __name__ == "__main__":
    unittest.main()
