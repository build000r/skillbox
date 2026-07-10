from __future__ import annotations

import json
import os
import re
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
        verbs = payload["skill_verbs"]
        expected_verb_fields = {
            "purpose",
            "mutates",
            "links_disk",
            "returns_packet",
            "scope",
            "survives_recalibrate",
            "when_to_use",
            "do_NOT",
        }
        required_decision_verbs = {
            "recalibrate",
            "activate",
            "sync",
            "prune",
            "on",
            "off",
            "default",
            "heal",
            "why",
        }
        self.assertLessEqual(required_decision_verbs, set(verbs))
        for name, row in verbs.items():
            with self.subTest(skill_verb=name):
                self.assertEqual(set(row), expected_verb_fields)
        self.assertTrue(verbs["on"]["returns_packet"])
        self.assertEqual(verbs["recalibrate"]["mutates"], "none")
        self.assertEqual(verbs["activate"]["mutates"], "cwd-ephemeral")
        self.assertIn("default", verbs)
        self.assertEqual(verbs["default"]["mutates"], "repo_or_operator_policy")
        self.assertFalse(verbs["default"]["links_disk"])
        self.assertIn(
            "sbp skill default on <skill> --repo --dry-run --format json",
            payload["safety"]["dry_run_first"],
        )
        help_result = subprocess.run(
            ["python3", ".env-manager/manage.py", "skill", "--help"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        match = re.search(r"\{([^}]+)\}", help_result.stdout)
        self.assertIsNotNone(match, help_result.stdout)
        dispatched_skill_verbs = set(match.group(1).split(",")) if match else set()
        self.assertLessEqual(dispatched_skill_verbs, set(verbs))
        launch = next(command for command in payload["commands"] if command["name"] == "launch")
        bulk = next(command for command in payload["commands"] if command["name"] == "bulk")
        recalibrate = next(command for command in payload["commands"] if command["name"] == "recalibrate")
        self.assertEqual(launch["aliases"], ["bulk"])
        self.assertEqual(bulk["alias_for"], "launch")
        self.assertTrue(recalibrate["json"])
        self.assertEqual(recalibrate["safe_first_try"], "sbp recalibrate --json")
        self.assertIn("sbp down <profile> <service> --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp bulk <dir> <dir> --request '<prompt>' --dry-run --json", payload["safety"]["dry_run_first"])
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", payload["next_actions"])
        triage_payload = json.loads(triage.stdout)
        self.assertEqual(triage_payload["tool"], "skillbox-sbp")
        self.assertIn("sbp launch <dir> <dir> --request '<prompt>' --dry-run --json", triage_payload["quick_ref"])
        self.assertTrue(any(item["id"] == "preview-launch" for item in triage_payload["recommendations"]))

    def test_sbp_skill_default_forwards_to_runtime_skill_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "skill",
                "default",
                "on",
                "alpha",
                "--repo",
                "--dry-run",
                "--format",
                "json",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "skill",
                    "default",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "on",
                    "alpha",
                    "--repo",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            )

    def test_sbp_recalibrate_auto_fix_yes_repairs_missing_skill_in_one_outer_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            source_root = root / "sources"
            clients_root = root / "clients"
            repo.mkdir()
            (repo / ".git").mkdir()
            (source_root / "alpha").mkdir(parents=True)
            clients_root.mkdir()
            (source_root / "alpha" / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
            policy_path = root / "skill-scope.yaml"
            policy_path.write_text(
                "version: 1\n"
                "skill_source_roots:\n"
                f"  - {source_root}\n"
                "rules:\n"
                "  - id: alpha-local\n"
                "    skills: [alpha]\n"
                f"    paths: [{repo}]\n",
                encoding="utf-8",
            )
            env = {
                "SKILLBOX_SKILL_SCOPE_FILE": str(policy_path),
                "SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root),
            }

            def embedded_json_objects(text: str) -> list[dict[str, object]]:
                decoder = json.JSONDecoder()
                objects: list[dict[str, object]] = []
                for index, character in enumerate(text):
                    if character != "{":
                        continue
                    try:
                        item, _ = decoder.raw_decode(text[index:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        objects.append(item)
                return objects

            before = self._run_wrapper(
                SBP,
                "skills",
                "--issues-only",
                "--no-global",
                "--format",
                "json",
                invoke_cwd=repo,
                extra_env=env,
            )
            recovery = self._run_wrapper(
                SBP,
                "recalibrate",
                "--auto-fix",
                "--yes",
                invoke_cwd=repo,
                extra_env=env,
            )
            after = self._run_wrapper(
                SBP,
                "skills",
                "--issues-only",
                "--no-global",
                "--format",
                "json",
                invoke_cwd=repo,
                extra_env=env,
            )
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(recovery.returncode, 0, recovery.stderr)
            self.assertEqual(after.returncode, 0, after.stderr)
            before_payload = json.loads(before.stdout)
            heal_payloads = [
                item for item in embedded_json_objects(recovery.stdout)
                if item.get("action") == "heal" and item.get("skill") == "alpha"
            ]
            after_payload = json.loads(after.stdout)
            recovery_commands = ["sbp recalibrate --auto-fix --yes"]
            ceremony_log = {
                "steps": [
                    {
                        "name": "detect_missing",
                        "command": "sbp skills --issues-only --no-global --format json",
                        "missing_for_cwd": [
                            item.get("name")
                            for item in (before_payload.get("issues") or {}).get("missing_for_cwd") or []
                        ],
                    },
                    {
                        "name": "recover",
                        "command": recovery_commands[0],
                        "tool_call_count": len(recovery_commands),
                        "returned_activation_packet": bool(
                            heal_payloads and heal_payloads[0].get("activation_packet")
                        ),
                    },
                    {
                        "name": "verify_effective",
                        "command": "sbp skills --issues-only --no-global --format json",
                        "missing_for_cwd": [
                            item.get("name")
                            for item in (after_payload.get("issues") or {}).get("missing_for_cwd") or []
                        ],
                    },
                ],
            }

            self.assertEqual(ceremony_log["steps"][0]["missing_for_cwd"], ["alpha"], ceremony_log)
            self.assertEqual(recovery_commands, ["sbp recalibrate --auto-fix --yes"], ceremony_log)
            self.assertEqual(ceremony_log["steps"][1]["tool_call_count"], 1, ceremony_log)
            self.assertEqual(len(heal_payloads), 1, ceremony_log)
            heal_payload = heal_payloads[0]
            self.assertTrue(ceremony_log["steps"][1]["returned_activation_packet"], ceremony_log)
            self.assertEqual(heal_payload["action"], "heal", ceremony_log)
            self.assertEqual(heal_payload["activation_packet"]["name"], "alpha", ceremony_log)
            self.assertIn("skill_md", heal_payload["activation_packet"], ceremony_log)
            self.assertEqual(ceremony_log["steps"][2]["missing_for_cwd"], [], ceremony_log)
            self.assertIn("alpha", [item.get("name") for item in after_payload.get("effective") or []], ceremony_log)
            self.assertTrue((repo / ".claude" / "skills" / "alpha").is_symlink(), ceremony_log)
            self.assertTrue((repo / ".codex" / "skills" / "alpha").is_symlink(), ceremony_log)

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
        self.assertIn("Skill verb contract:", result.stdout)
        self.assertIn("sbp capabilities --json | jq .skill_verbs", result.stdout)
        self.assertIn("activate (mutates=cwd-ephemeral, returns_packet=true)", result.stdout)

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

    def test_sbp_overlay_dry_run_keeps_policy_sync_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "overlay",
                "on",
                "marketing",
                "--dry-run",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "skill",
                    "sync",
                    "--profile",
                    "local-all",
                    "--cwd",
                    str(downstream),
                    "--dry-run",
                ],
            )

    def test_sbp_overlay_list_does_not_policy_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "overlay",
                "list",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("applying policy for this cwd", result.stdout)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["argv"],
                [
                    "overlay",
                    "--cwd",
                    str(downstream),
                    "list",
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

    def test_sbp_recalibrate_json_emits_parseable_machine_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            generator = fake_root / "scripts" / "gen_output_schemas.py"
            generator.parent.mkdir(parents=True)
            generator.write_text(
                textwrap.dedent(
                    """\
                    from __future__ import annotations

                    import json
                    import sys

                    if "--recalibrate-json" not in sys.argv:
                        raise SystemExit(2)
                    cwd = sys.argv[sys.argv.index("--cwd") + 1]
                    print(json.dumps({
                        "cwd": cwd,
                        "issues": {"missing_for_cwd": [{"name": "alpha"}]},
                        "fixes": [{
                            "problem": "missing_for_cwd",
                            "skill": "alpha",
                            "command": "sbp skill on alpha --cwd $PWD",
                            "links": [],
                            "dry_run_preview": {"dry_run": True},
                            "packet_on_apply": None,
                        }],
                    }))
                    """
                ),
                encoding="utf-8",
            )

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--json",
                fake_root=fake_root,
                invoke_cwd=downstream,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("policy issues for this repo:", result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["cwd"], str(downstream))
            self.assertEqual(payload["fixes"][0]["skill"], "alpha")
            self.assertEqual(payload["fixes"][0]["command"], "sbp skill on alpha --cwd $PWD")

    def test_sbp_recalibrate_auto_fix_previews_heal_for_missing_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox_with_missing_skills(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--auto-fix",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto-fix missing repo-local skills:", result.stdout)
            self.assertIn("mode: dry-run (pass --yes to apply)", result.stdout)
            calls = json.loads(record_path.read_text(encoding="utf-8"))
            heal_calls = [
                item["argv"] for item in calls
                if item["argv"][:2] == ["skill", "heal"]
            ]
            self.assertEqual(
                heal_calls,
                [
                    [
                        "skill",
                        "heal",
                        "alpha",
                        "--profile",
                        "local-all",
                        "--cwd",
                        str(downstream),
                        "--dry-run",
                        "--format",
                        "json",
                    ],
                    [
                        "skill",
                        "heal",
                        "beta",
                        "--profile",
                        "local-all",
                        "--cwd",
                        str(downstream),
                        "--dry-run",
                        "--format",
                        "json",
                    ],
                ],
            )

    def test_sbp_recalibrate_auto_fix_yes_applies_heal_for_missing_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox_with_missing_skills(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--auto-fix",
                "--yes",
                fake_root=fake_root,
                invoke_cwd=downstream,
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode: apply (--yes)", result.stdout)
            self.assertIn('"activation_packet"', result.stdout)
            calls = json.loads(record_path.read_text(encoding="utf-8"))
            heal_calls = [
                item["argv"] for item in calls
                if item["argv"][:2] == ["skill", "heal"]
            ]
            self.assertEqual(len(heal_calls), 2)
            self.assertTrue(all("--dry-run" not in call for call in heal_calls))
            self.assertEqual([call[2] for call in heal_calls], ["alpha", "beta"])

    def test_sbp_recalibrate_auto_fix_rejects_fleet_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            downstream = root / "downstream"
            downstream.mkdir()

            result = self._run_wrapper(
                SBP,
                "recalibrate",
                "--fleet",
                "--auto-fix",
                fake_root=fake_root,
                invoke_cwd=downstream,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("--auto-fix is cwd-only", result.stderr)

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

    def test_sbp_status_prefers_operator_repo_roots_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_root = self._make_fake_skillbox(root / "skillbox")
            home = root / "home"
            operator_repos = root / "srv" / "skillbox" / "repos"
            clients_root = operator_repos / "skillbox-config" / "clients"
            clients_root.mkdir(parents=True)
            record_path = root / "record.json"

            result = self._run_wrapper(
                SBP,
                "status",
                fake_root=fake_root,
                home=home,
                extra_env={"SKILLBOX_OPERATOR_REPOS_ROOT": str(operator_repos)},
                record_path=record_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["env"]["SKILLBOX_CONFIG_ROOT"], str((operator_repos / "skillbox-config").resolve()))
            self.assertEqual(record["env"]["SKILLBOX_CLIENTS_HOST_ROOT"], str(clients_root.resolve()))
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_ROOT"], str(operator_repos.resolve()))
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_HOST_ROOT"], str(operator_repos.resolve()))

    def test_sbp_status_infers_operator_roots_from_explicit_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            operator_repos = root / "srv" / "skillbox" / "repos"
            fake_root = self._make_fake_skillbox(operator_repos / "opensource" / "skillbox")
            home = root / "home"
            clients_root = operator_repos / "skillbox-config" / "clients"
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
            self.assertEqual(record["env"]["SKILLBOX_CONFIG_ROOT"], str((operator_repos / "skillbox-config").resolve()))
            self.assertEqual(record["env"]["SKILLBOX_CLIENTS_HOST_ROOT"], str(clients_root.resolve()))
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_ROOT"], str(operator_repos.resolve()))
            self.assertEqual(record["env"]["SKILLBOX_MONOSERVER_HOST_ROOT"], str(operator_repos.resolve()))

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
                        "SKILLBOX_CONFIG_ROOT": os.environ.get("SKILLBOX_CONFIG_ROOT"),
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

    def _make_fake_skillbox_with_missing_skills(self, root: Path) -> Path:
        env_dir = root / ".env-manager"
        env_dir.mkdir(parents=True)
        (env_dir / "manage.py").write_text(
            textwrap.dedent(
                """\
                from __future__ import annotations

                import json
                import os
                import sys
                from pathlib import Path

                record_path = Path(os.environ["SKILLBOX_RECORD"])
                if record_path.exists() and record_path.read_text(encoding="utf-8"):
                    rows = json.loads(record_path.read_text(encoding="utf-8"))
                else:
                    rows = []
                rows.append({"argv": sys.argv[1:], "cwd": os.getcwd()})
                record_path.write_text(json.dumps(rows), encoding="utf-8")

                argv = sys.argv[1:]
                payload = {
                    "issues": {
                        "missing_for_cwd": [
                            {"name": "alpha"},
                            {"name": "beta"},
                            {"name": "alpha"},
                        ]
                    },
                    "beads": {"required": False},
                    "summary": {},
                }
                if argv[:1] == ["skills"] and "--format" in argv and "json" in argv:
                    print(json.dumps(payload))
                elif argv[:2] == ["skill", "heal"]:
                    skill = argv[2]
                    print(json.dumps({
                        "action": "heal",
                        "skill": skill,
                        "activation_packet": {"name": skill, "skill_md_sha256": "sha"},
                    }))
                else:
                    print("ok")
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
