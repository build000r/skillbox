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
        self.assertIn("sbp capabilities --json", result.stdout)
        self.assertIn("sbp mmdx QUERY", result.stdout)
        self.assertIn("Fuzzy-find and open .mmdx/.mmd", result.stdout)
        self.assertIn("sbp hire times", result.stdout)
        self.assertIn("sbp skills audit", result.stdout)
        self.assertIn("sbp candidates", result.stdout)
        self.assertIn("sbp recalibrate", result.stdout)
        self.assertIn("sbp mcp", result.stdout)
        self.assertIn("sbp beads", result.stdout)
        self.assertIn("sbp launch", result.stdout)
        self.assertIn("Alias for launch", result.stdout)

    def test_sbo_help_uses_sbo_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")

            result = self._run_wrapper(SBO, "--help", fake_root=fake_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sbo - personal skillbox runtime and skill helper", result.stdout)
        self.assertIn("sbo capabilities --json", result.stdout)

    def test_sbp_capabilities_and_robot_triage_are_parseable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")

            result = self._run_wrapper(SBP, "capabilities", "--json", fake_root=fake_root)
            triage = self._run_wrapper(SBP, "--robot-triage", fake_root=fake_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tool"], "skillbox-sbp")
        self.assertIn("stdout_stderr_contract", payload)
        self.assertTrue(any(command["name"] == "candidates" for command in payload["commands"]))
        launch = next(command for command in payload["commands"] if command["name"] == "launch")
        bulk = next(command for command in payload["commands"] if command["name"] == "bulk")
        self.assertEqual(launch["aliases"], ["bulk"])
        self.assertEqual(bulk["alias_for"], "launch")
        self.assertIn("sbp down <profile> <service> --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp bulk <dir> <dir> --request '<prompt>' --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", payload["next_actions"])
        triage_payload = json.loads(triage.stdout)
        self.assertEqual(triage_payload["tool"], "skillbox-sbp")
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", triage_payload["quick_ref"])
        self.assertTrue(any(item["id"] == "preview-launch" for item in triage_payload["recommendations"]))

    def test_sbp_home_surfaces_batch_launcher_safe_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()

            result = self._run_wrapper(SBP, fake_root=fake_root, invoke_cwd=downstream)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", result.stdout)
        self.assertIn("preview Swimmers batch launch (bulk alias)", result.stdout)

    def test_sbp_robot_docs_names_bulk_alias_and_prompt_quoting(self) -> None:
        result = self._run_wrapper(SBP, "robot-docs", "guide")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sbp bulk <dir> <dir> --request 'Audit auth drift' --dry-run --json", result.stdout)
        self.assertIn("bulk is an alias for launch", result.stdout)
        self.assertIn("Use single quotes when prompts contain $smart", result.stdout)

    def test_sbp_launch_maps_to_swimmers_launch_without_profile_consuming_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "launcher"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "launch",
                "core",
                "../api",
                "--request",
                "Audit auth drift",
                "--dry-run",
                "--json",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "swimmers-launch",
                    "--invoke-cwd",
                    str(downstream),
                    "core",
                    "../api",
                    "--request",
                    "Audit auth drift",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

    def test_sbp_status_json_alias_keeps_stdout_parseable_and_warns_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "status",
                "core",
                "--jsno",
                fake_root=fake_root,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
            self.assertIn("Interpreting --jsno as --format json", result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                ["status", "--cwd", str(ROOT_DIR), "--profile", "local-core", "--format", "json"],
            )

    def test_sbp_up_dry_run_is_not_treated_as_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "up",
                "backend",
                "spaps",
                "--dry-run",
                "--json",
                fake_root=fake_root,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "up",
                    "--cwd",
                    str(ROOT_DIR),
                    "--profile",
                    "local-backend",
                    "--mode",
                    "reuse",
                    "--dry-run",
                    "--format",
                    "json",
                    "--service",
                    "spaps",
                ],
            )

    def test_sbp_up_dry_run_survives_system_bash_empty_arrays(self) -> None:
        system_bash = Path("/bin/bash")
        if not system_bash.exists():
            self.skipTest("/bin/bash is not available on this platform")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")

            for json_flag in ("--json", "--jason"):
                with self.subTest(json_flag=json_flag):
                    record_path = root / f"record-{json_flag[2:]}.json"
                    result = self._run_wrapper(
                        SBP,
                        "up",
                        "--dry-run",
                        json_flag,
                        fake_root=fake_root,
                        record_path=record_path,
                        bash_path=system_bash,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        record["argv"],
                        [
                            "up",
                            "--cwd",
                            str(ROOT_DIR),
                            "--profile",
                            "local-all",
                            "--mode",
                            "reuse",
                            "--dry-run",
                            "--format",
                            "json",
                        ],
                    )
                    if json_flag != "--json":
                        self.assertIn("Interpreting --jason as --format json", result.stderr)

    def test_sbp_unknown_and_logs_errors_name_exact_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")

            unknown = self._run_wrapper(SBP, "statu", fake_root=fake_root)
            logs = self._run_wrapper(SBP, "logs", fake_root=fake_root)

        self.assertEqual(unknown.returncode, 2)
        self.assertEqual(unknown.stdout, "")
        self.assertIn("Did you mean: sbp status --json", unknown.stderr)
        self.assertIn("sbp capabilities --json", unknown.stderr)
        self.assertEqual(logs.returncode, 2)
        self.assertIn("Exact command: sbp logs <profile> <service> --json", logs.stderr)
        self.assertIn("List services first: sbp status --json", logs.stderr)

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

    def test_sbp_candidates_maps_to_full_source_inventory_without_global_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "candidates",
                "--json",
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
                    "--show-sources",
                    "--full",
                    "--no-global",
                    "--format",
                    "json",
                ],
            )

    def test_sbp_skills_candidates_alias_uses_same_candidate_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "skills",
                "candidates",
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
                    "skills",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--show-sources",
                    "--full",
                    "--no-global",
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
            self.assertIn("sbp candidates --json", result.stdout)
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
                ["status", "--cwd", str(ROOT_DIR), "--profile", "local-core"],
            )

    def test_sbp_status_prefers_canonical_operator_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            home = root / "home"
            clients_root = home / "repos" / "skillbox-config" / "clients"
            clients_root.mkdir(parents=True)
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "status",
                fake_root=fake_root,
                home=home,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["env"]["SKILLBOX_CLIENTS_HOST_ROOT"],
                str(clients_root.resolve()),
            )
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_ROOT"], str(home / "repos"))
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_HOST_ROOT"], str(home / "repos"))

    def test_sbp_status_preserves_explicit_clients_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            home = root / "home"
            (home / "repos" / "skillbox-config" / "clients").mkdir(parents=True)
            custom_clients_root = root / "custom-clients"
            custom_clients_root.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "status",
                fake_root=fake_root,
                home=home,
                extra_env={"SKILLBOX_CLIENTS_HOST_ROOT": str(custom_clients_root)},
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["env"]["SKILLBOX_CLIENTS_HOST_ROOT"],
                str(custom_clients_root),
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
                    "--cwd",
                    str(ROOT_DIR),
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

                payload = {
                    "argv": sys.argv[1:],
                    "cwd": os.getcwd(),
                    "env": {
                        "SKILLBOX_CLIENTS_HOST_ROOT": os.environ.get("SKILLBOX_CLIENTS_HOST_ROOT"),
                        "SKILLBOX_MONOSERVER_HOST_ROOT": os.environ.get("SKILLBOX_MONOSERVER_HOST_ROOT"),
                        "SKILLBOX_MONOSERVER_ROOT": os.environ.get("SKILLBOX_MONOSERVER_ROOT"),
                    },
                }
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
        home: Path | None = None,
        invoke_cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        record_path: Path | None = None,
        bash_path: str | Path = "bash",
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        if fake_root is not None:
            env["SKILLBOX_ROOT"] = str(fake_root)
        if home is not None:
            env["HOME"] = str(home)
        if invoke_cwd is not None:
            env["SKILLBOX_INVOKE_CWD"] = str(invoke_cwd)
        if extra_env:
            env.update(extra_env)
        if record_path is not None:
            env["SKILLBOX_RECORD"] = str(record_path)
        else:
            env["SKILLBOX_RECORD"] = os.devnull
        return subprocess.run(
            [str(bash_path), str(wrapper), *args],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


if __name__ == "__main__":
    unittest.main()
