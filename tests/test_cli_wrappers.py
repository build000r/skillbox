from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SBP = ROOT_DIR / "scripts" / "sbp"
SBO = ROOT_DIR / "scripts" / "sbo"


class CliWrapperTests(unittest.TestCase):
    def test_help_lists_mmdx_shortcut(self) -> None:
        result = self._run_wrapper(SBP, "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sbp mmdx QUERY", result.stdout)
        self.assertIn("Fuzzy-find and open .mmdx/.mmd", result.stdout)
        self.assertIn("sbp hire times", result.stdout)
        self.assertIn("sbp skills audit", result.stdout)
        self.assertIn("sbp recalibrate", result.stdout)
        self.assertIn("sbp mcp", result.stdout)
        self.assertIn("sbp beads", result.stdout)

    def test_sbp_skills_infers_client_from_downstream_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "skills",
                "--issues-only",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "skills",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--issues-only",
                ],
            )

    def test_sbp_skills_audit_maps_to_skill_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "skills",
                "audit",
                "--limit",
                "5",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "skill-audit",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--limit",
                    "5",
                ],
            )

    def test_sbp_recalibrate_defaults_to_cwd_sync_and_project_prune_dry_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("policy issues for this repo:", result.stdout)
            self.assertIn("add missing repo-local skills:", result.stdout)
            self.assertIn("remove repo-local policy violations:", result.stdout)
            self.assertIn("beads graph:", result.stdout)
            self.assertIn("beads: not required by currently effective skills", result.stdout)
            self.assertIn("mcp config parity:", result.stdout)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "mcp-audit",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                ],
            )

    def test_sbp_recalibrate_cwd_override_applies_to_closeout_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            invoke_cwd = root / "launcher"
            target_cwd = root / "target"
            invoke_cwd.mkdir()
            target_cwd.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--cwd",
                str(target_cwd),
                fake_root=fake_root,
                invoke_cwd=invoke_cwd,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"cwd: {target_cwd}", result.stdout)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "mcp-audit",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(target_cwd),
                ],
            )

    def test_sbp_mcp_maps_to_mcp_audit_for_downstream_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "mcp",
                "--config-root",
                str(downstream),
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "mcp-audit",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--config-root",
                    str(downstream),
                ],
            )

    def test_sbp_recalibrate_fleet_maps_to_skill_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--fleet",
                "--limit",
                "9",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("review dry-runs before applying:", result.stdout)
            self.assertIn("sbp skill sync --cwd <repo> --dry-run", result.stdout)
            self.assertNotIn("sbp skill sync <skill>", result.stdout)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "skill-audit",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--limit",
                    "9",
                ],
            )

    def test_sbp_mmdx_preserves_downstream_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream" / "docs" / "plans"
            downstream.mkdir(parents=True)
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "mmdx",
                "skill",
                "review",
                "--no-open",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(record["cwd"]).resolve(), fake_root.resolve())
            self.assertEqual(
                record["argv"],
                [
                    "mmdx",
                    "--cwd",
                    str(downstream),
                    "skill",
                    "review",
                    "--no-open",
                ],
            )

    def test_sbo_alias_uses_same_mmdx_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "repo"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBO,
                "mmd",
                "runtime",
                "drift",
                "c",
                "--no-open",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "mmdx",
                    "--cwd",
                    str(downstream),
                    "runtime",
                    "drift",
                    "c",
                    "--no-open",
                ],
            )

    def test_status_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "status",
                "core",
                fake_root=fake_root,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                ["status", "--client", "personal", "--profile", "local-core"],
            )

    def test_sbp_hire_maps_operator_booking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "hire",
                "times",
                "--limit",
                "3",
                fake_root=fake_root,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "operator-booking",
                    "--client",
                    "personal",
                    "--profile",
                    "local-all",
                    "times",
                    "--limit",
                    "3",
                ],
            )

    def test_make_wrappers_install_creates_checked_in_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / "bin"
            result = subprocess.run(
                ["make", "wrappers-install", f"WRAPPER_BIN_DIR={bin_dir}"],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bin_dir / "sbp").is_symlink())
            self.assertTrue((bin_dir / "sbo").is_symlink())
            self.assertEqual((bin_dir / "sbp").resolve(), SBP)
            self.assertEqual((bin_dir / "sbo").resolve(), SBO)
            self.assertIn("installed wrappers:", result.stdout)

    def _make_fake_skillbox(self, root: Path) -> Path:
        env_dir = root / ".env-manager"
        env_dir.mkdir(parents=True)
        (env_dir / "manage.py").write_text(
            textwrap.dedent(
                """\
                from __future__ import annotations

                import json
                import os
                import sys

                payload = {"argv": sys.argv[1:], "cwd": os.getcwd()}
                with open(os.environ["SKILLBOX_RECORD"], "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                print(json.dumps(payload))
                """
            ),
            encoding="utf-8",
        )
        return root

    def _run_wrapper(
        self,
        wrapper: Path,
        *args: str,
        fake_root: Path | None = None,
        invoke_cwd: Path | None = None,
        record_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        if fake_root is not None:
            env["SKILLBOX_ROOT"] = str(fake_root)
        if invoke_cwd is not None:
            env["SKILLBOX_INVOKE_CWD"] = str(invoke_cwd)
        if record_path is not None:
            env["SKILLBOX_RECORD"] = str(record_path)
        else:
            env["SKILLBOX_RECORD"] = os.devnull
        return subprocess.run(
            ["bash", str(wrapper), *args],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


if __name__ == "__main__":
    unittest.main()
