from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import cli as CLI  # noqa: E402
from runtime_manager.forge import FORGE_NO_SIGNAL, CRON_MARKER, forge_init, forge_status  # noqa: E402


class ForgeInitTests(unittest.TestCase):
    def test_init_creates_session_hook_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            scoring_script = Path(tmpdir) / "score-session.sh"
            scoring_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            run_py = Path(tmpdir) / "codex-tmux" / "scripts" / "run.py"
            run_py.parent.mkdir(parents=True)
            run_py.write_text(
                'print("before")\n        echo "Result written to: $RESULT_FILE"\nprint("after")\n',
                encoding="utf-8",
            )

            first = forge_init(home=home, scoring_script=scoring_script, codex_tmux_run_py=run_py)
            second = forge_init(home=home, scoring_script=scoring_script, codex_tmux_run_py=run_py)

            self.assertTrue(first["ok"])
            self.assertEqual(first["settings"]["action"], "added")
            self.assertEqual(first["codex_tmux"]["action"], "patched")
            self.assertEqual(second["settings"]["action"], "already_present")
            self.assertEqual(second["codex_tmux"]["action"], "already_present")

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            session_end = settings["hooks"]["SessionEnd"]
            commands = [
                hook["command"]
                for entry in session_end
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(len(commands), 1)
            self.assertIn(str(scoring_script), commands[0])
            self.assertIn("--source claude", commands[0])
            self.assertEqual(run_py.read_text(encoding="utf-8").count("skillbox-forge-scoring-start"), 1)

    def test_init_with_cron_adds_single_marker_entry(self) -> None:
        calls: list[tuple[list[str], str | None]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs.get("input") if isinstance(kwargs.get("input"), str) else None))
            if command == ["crontab", "-l"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            scoring_script = Path(tmpdir) / "score-session.sh"
            scoring_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            payload = forge_init(
                home=home,
                scoring_script=scoring_script,
                codex_tmux_run_py=Path(tmpdir) / "missing.py",
                with_cron=True,
                subprocess_run=fake_run,
            )

        self.assertEqual(payload["cron"]["action"], "added")
        written_crontab = next(item[1] for item in calls if item[0] == ["crontab", "-"])
        self.assertIsNotNone(written_crontab)
        self.assertIn(CRON_MARKER, written_crontab or "")
        self.assertIn("--source both", written_crontab or "")

    def test_init_rejects_invalid_settings_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{not json", encoding="utf-8")
            scoring_script = Path(tmpdir) / "score-session.sh"
            scoring_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            with self.assertRaisesRegex(Exception, "not valid JSON"):
                forge_init(
                    home=home,
                    scoring_script=scoring_script,
                    codex_tmux_run_py=Path(tmpdir) / "missing.py",
                )

            self.assertEqual(settings.read_text(encoding="utf-8"), "{not json")

    def test_manage_py_forge_init_outputs_json(self) -> None:
        emitted: list[dict[str, object]] = []
        payload = {
            "ok": True,
            "settings": {"action": "added"},
            "codex_tmux": {"action": "missing"},
            "cron": {"action": "skipped"},
            "warnings": [],
        }
        with (
            mock.patch.object(CLI, "forge_init", return_value=payload) as forge_mock,
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            exit_code = CLI.main(["forge", "init", "--with-cron", "--format", "json"])

        self.assertEqual(exit_code, CLI.EXIT_OK)
        self.assertEqual(emitted[-1], payload)
        forge_mock.assert_called_once()
        self.assertTrue(forge_mock.call_args.kwargs["with_cron"])


class ForgeStatusTests(unittest.TestCase):
    def _append_history(
        self,
        history_path: Path,
        *,
        skill: str,
        timestamp: str,
        metrics: dict[str, float],
    ) -> None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": timestamp,
            "skill": skill,
            "source": "claude",
            "invocations": 1,
            "metrics": metrics,
        }
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _metrics(
        self,
        *,
        ack_rate: float = 0.9,
        validation_rate: float = 0.9,
        checkpoint_rate: float = 0.0,
        risk_gating_rate: float = 0.0,
        correction_rate: float = 0.0,
        completion_rate: float = 0.9,
    ) -> dict[str, float]:
        return {
            "ack_rate": ack_rate,
            "validation_rate": validation_rate,
            "checkpoint_rate": checkpoint_rate,
            "risk_gating_rate": risk_gating_rate,
            "correction_rate": correction_rate,
            "completion_rate": completion_rate,
        }

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    def _create_pending_branch(self, root: Path, skill: str) -> None:
        repo = root / "workspace" / "skill-repos" / "fixture-skills"
        repo.mkdir(parents=True)
        self._git(repo, "init", "-q")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(
            repo,
            "-c",
            "user.email=forge@example.test",
            "-c",
            "user.name=Forge Test",
            "commit",
            "-q",
            "-m",
            "init",
        )
        self._git(repo, "checkout", "-q", "-b", f"forge/{skill}")

    def test_status_no_signal_returns_informational_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            payload = forge_status(home=Path(tmpdir) / "home", root_dir=root)

        self.assertEqual(payload["code"], FORGE_NO_SIGNAL)
        self.assertEqual(payload["message"], "no signal yet")
        self.assertEqual(payload["skills"], [])
        self.assertEqual(payload["total_sessions_scored"], 0)

    def test_status_aggregates_counts_thresholds_and_pending_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            home = Path(tmpdir) / "home"
            history = home / ".claude" / "skill-review-history.jsonl"
            self._create_pending_branch(root, "ask-cascade")
            weak_metrics = self._metrics(
                ack_rate=0.4,
                validation_rate=0.35,
                correction_rate=0.4,
                completion_rate=0.45,
            )
            for index in range(6):
                self._append_history(
                    history,
                    skill="ask-cascade",
                    timestamp=f"2026-06-{index + 1:02d}T12:00:00Z",
                    metrics=weak_metrics,
                )
            for index in range(4):
                self._append_history(
                    history,
                    skill="describe",
                    timestamp=f"2026-06-{index + 1:02d}T13:00:00Z",
                    metrics=self._metrics(),
                )

            payload = forge_status(home=home, root_dir=root)

        by_name = {item["name"]: item for item in payload["skills"]}
        self.assertEqual(payload["total_sessions_scored"], 10)
        self.assertEqual(by_name["ask-cascade"]["sessions_scored"], 6)
        self.assertTrue(by_name["ask-cascade"]["proposal_pending"])
        self.assertEqual(
            by_name["ask-cascade"]["thresholds_crossed"],
            [
                "ack_rate < 0.5",
                "validation_rate < 0.4",
                "correction_rate > 0.3",
                "completion_rate < 0.5",
            ],
        )
        self.assertEqual(by_name["describe"]["sessions_scored"], 4)
        self.assertFalse(by_name["describe"]["proposal_pending"])
        self.assertEqual(by_name["describe"]["thresholds_crossed"], [])

    def test_status_detects_declining_three_session_average(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            home = Path(tmpdir) / "home"
            history = home / ".claude" / "skill-review-history.jsonl"
            for index in range(3):
                self._append_history(
                    history,
                    skill="ask-cascade",
                    timestamp=f"2026-06-{index + 1:02d}T12:00:00Z",
                    metrics=self._metrics(validation_rate=0.9),
                )
            for index in range(3):
                self._append_history(
                    history,
                    skill="ask-cascade",
                    timestamp=f"2026-06-{index + 4:02d}T12:00:00Z",
                    metrics=self._metrics(validation_rate=0.1),
                )

            payload = forge_status(home=home, root_dir=root)

        self.assertEqual(payload["skills"][0]["trend"], "declining")
        self.assertIn("validation_rate < 0.4", payload["skills"][0]["thresholds_crossed"])

    def test_status_filters_to_single_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            home = Path(tmpdir) / "home"
            history = home / ".claude" / "skill-review-history.jsonl"
            self._append_history(
                history,
                skill="ask-cascade",
                timestamp="2026-06-01T12:00:00Z",
                metrics=self._metrics(),
            )
            self._append_history(
                history,
                skill="describe",
                timestamp="2026-06-01T13:00:00Z",
                metrics=self._metrics(),
            )

            payload = forge_status(skill="describe", home=home, root_dir=root)

        self.assertEqual([item["name"] for item in payload["skills"]], ["describe"])
        self.assertEqual(payload["total_sessions_scored"], 1)

    def test_status_cli_outputs_json(self) -> None:
        emitted: list[dict[str, object]] = []
        payload = {
            "skills": [{"name": "ask-cascade", "sessions_scored": 1}],
            "total_sessions_scored": 1,
            "scoring_hook_installed": False,
            "unscored_sessions": 0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            expected_root = root.resolve()
            with (
                mock.patch.object(CLI, "forge_status", return_value=payload) as status_mock,
                mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
            ):
                exit_code = CLI.main(
                    [
                        "--root-dir",
                        str(root),
                        "forge",
                        "status",
                        "--format",
                        "json",
                        "--skill",
                        "ask-cascade",
                    ]
                )

        self.assertEqual(exit_code, CLI.EXIT_OK)
        self.assertEqual(emitted[-1], payload)
        status_mock.assert_called_once_with(skill="ask-cascade", root_dir=expected_root)


class ScoreSessionScriptTests(unittest.TestCase):
    def test_score_session_dry_run_without_skill_issue_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {**os.environ, "HOME": str(Path(tmpdir) / "home")}
            result = subprocess.run(
                ["bash", "scripts/score-session.sh", "--source", "claude", "--dry-run"],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skill-issue not installed", result.stderr)

    def test_score_session_saves_review_for_invoked_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            skill_issue = home / ".claude" / "skills" / "skill-issue"
            scripts = skill_issue / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "review_skill_usage.py").write_text(
                textwrap.dedent(
                    """\
                    import json
                    import sys
                    skill = sys.argv[sys.argv.index("--skill") + 1]
                    print(json.dumps({"skill": skill, "source": "claude", "invocations_found": 1}))
                    """
                ),
                encoding="utf-8",
            )
            (scripts / "save_skill_review.py").write_text(
                textwrap.dedent(
                    """\
                    import pathlib
                    pathlib.Path("saved.txt").write_text("saved", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            installed_skill = home / ".claude" / "skills" / "ask-cascade"
            installed_skill.mkdir(parents=True)
            (installed_skill / "SKILL.md").write_text("name: ask-cascade\n", encoding="utf-8")

            env = {**os.environ, "HOME": str(home), "PYTHONPATH": ""}
            result = subprocess.run(
                ["bash", "scripts/score-session.sh", "--source", "claude"],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviewed=1 saved=1", result.stderr)
        self.assertEqual((ROOT_DIR / "saved.txt").read_text(encoding="utf-8"), "saved")
        (ROOT_DIR / "saved.txt").unlink()

    def test_score_session_no_new_invocations_exits_zero_without_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            skill_issue = home / ".claude" / "skills" / "skill-issue"
            scripts = skill_issue / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "review_skill_usage.py").write_text(
                'import json; print(json.dumps({"skill": "ask-cascade", "invocations_found": 0}))\n',
                encoding="utf-8",
            )
            marker = root / "save-called"
            (scripts / "save_skill_review.py").write_text(
                f'import pathlib; pathlib.Path({str(marker)!r}).write_text("called", encoding="utf-8")\n',
                encoding="utf-8",
            )
            installed_skill = home / ".claude" / "skills" / "ask-cascade"
            installed_skill.mkdir(parents=True)
            (installed_skill / "SKILL.md").write_text("name: ask-cascade\n", encoding="utf-8")

            env = {**os.environ, "HOME": str(home)}
            result = subprocess.run(
                ["bash", "scripts/score-session.sh", "--source", "claude"],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviewed=1 saved=0", result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
