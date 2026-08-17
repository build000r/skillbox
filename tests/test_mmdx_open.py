from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

import runtime_manager as MODULE  # noqa: E402
from runtime_manager import mmdx_open as MMDX  # noqa: E402


def _write_mmdx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<!-- mmdx {"entry":"main"} -->\n\n'
        "## chart main Main\n"
        "```mermaid\n"
        "flowchart TD\n"
        "  A --> B\n"
        "```\n",
        encoding="utf-8",
    )


class MmdxOpenTests(unittest.TestCase):
    def test_split_path_query_resolves_exact_mmdx_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "docs" / "plans" / "draft" / "skill_review_realms" / "review.mmdx"
            _write_mmdx(target)

            payload, exit_code = MODULE.mmdx_open_payload(
                root_dir=ROOT_DIR,
                cwd=repo,
                query_parts=["docs/plans/draft/", "skill_review_realms/review.mmdx"],
                open_file=False,
            )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["action"], "resolved")
        self.assertEqual(payload["selected"]["path"], str(target.resolve()))
        self.assertEqual(payload["selected"]["score"], 1.5)

    def test_fuzzy_query_prefers_best_path_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            old = repo / "docs" / "other" / "review.mmdx"
            target = repo / "docs" / "plans" / "draft" / "skill_review_realms" / "review.mmdx"
            _write_mmdx(old)
            _write_mmdx(target)
            os.utime(old, (100, 100))
            os.utime(target, (200, 200))

            payload, exit_code = MODULE.mmdx_open_payload(
                root_dir=ROOT_DIR,
                cwd=repo,
                query_parts=["skill review realms"],
                open_file=False,
                limit=5,
            )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["action"], "resolved")
        self.assertEqual(payload["selected"]["path"], str(target.resolve()))
        self.assertGreaterEqual(payload["returned"], 2)

    def test_inventory_excludes_generated_roots_for_import_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            canonical = repo / "docs" / "diagrams" / "runtime-drift-demo.mmdx"
            generated_open_next = (
                repo
                / ".open-next"
                / "server-functions"
                / "default"
                / "docs"
                / "diagrams"
                / "runtime-drift-demo.mmdx"
            )
            generated_next = repo / ".next" / "server" / "docs" / "diagrams" / "other.mmdx"
            generated_coverage = repo / "coverage" / "docs" / "diagrams" / "coverage-copy.mmdx"
            _write_mmdx(canonical)
            _write_mmdx(generated_open_next)
            _write_mmdx(generated_next)
            _write_mmdx(generated_coverage)
            os.utime(canonical, (100, 100))
            os.utime(generated_open_next, (300, 300))
            os.utime(generated_next, (400, 400))
            os.utime(generated_coverage, (500, 500))

            payload, exit_code = MODULE.mmdx_open_payload(
                root_dir=ROOT_DIR,
                cwd=repo,
                query_parts=[],
                open_file=False,
                limit=10,
            )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["action"], "listed")
        self.assertEqual(payload["scanned"], 1)
        self.assertEqual(payload["returned"], 1)
        self.assertEqual(payload["matches"][0]["rel_path"], "docs/diagrams/runtime-drift-demo.mmdx")
        self.assertEqual(payload["matches"][0]["modified_at"], "1970-01-01T00:01:40Z")
        reported_paths = " ".join(match["rel_path"] for match in payload["matches"])
        self.assertNotIn(".open-next", reported_paths)
        self.assertNotIn(".next", reported_paths)
        self.assertNotIn("coverage", reported_paths)
        self.assertIn(".open-next", payload["excluded_generated_roots"])
        self.assertIn("canonical source files", payload["import_candidate_note"])

    def test_open_invokes_mmd_script_with_selected_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            target = repo / "diagram.mmdx"
            script = repo / "mmd.py"
            _write_mmdx(target)
            script.write_text("# fake\n", encoding="utf-8")

            completed = mock.Mock()
            completed.returncode = 0
            completed.stdout = "https://example.com/diagrams#pako:abc\n"
            completed.stderr = ""
            with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
                payload, exit_code = MODULE.mmdx_open_payload(
                    root_dir=ROOT_DIR,
                    cwd=repo,
                    query_parts=["diagram"],
                    open_file=True,
                    mmd_script=script,
                )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["action"], "opened")
        self.assertEqual(payload["viewer"]["url"], "https://example.com/diagrams#pako:abc")
        args = run.call_args.args[0]
        self.assertIn(str(script), args)
        self.assertIn(str(target.resolve()), args)
        self.assertIn("--open", args)
        self.assertIn("--no-parser-install", args)

    def test_low_confidence_query_returns_no_match_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_mmdx(repo / "docs" / "runtime-drift.mmd")

            with self.assertRaises(MODULE.MmdxOpenError) as ctx:
                MODULE.mmdx_open_payload(
                    root_dir=ROOT_DIR,
                    cwd=repo,
                    query_parts=["does not exist"],
                    open_file=False,
                )

        self.assertEqual(ctx.exception.error_type, "mmdx_no_match")
        self.assertEqual(ctx.exception.data["query"], "does not exist")
        self.assertEqual(ctx.exception.data["alternatives"][0]["rel_path"], "docs/runtime-drift.mmd")

    def test_error_payload_is_structured(self) -> None:
        exc = MODULE.MmdxOpenError(
            "mmdx_no_match",
            "No diagrams matched.",
            recovery_hint="Try another query.",
            next_actions=["mmdx --no-open"],
        )

        payload = MODULE.mmdx_error_payload(exc)

        self.assertEqual(payload["error"]["type"], "mmdx_no_match")
        self.assertTrue(payload["error"]["recoverable"])
        self.assertEqual(payload["error"]["next_actions"], ["mmdx --no-open"])


class MmdxTmuxParserInstallGateTests(unittest.TestCase):
    """`--tmux` must not smuggle an npm install past a refused consent.

    The skill honours `--no-parser-install` in the foreground command and
    nowhere else: `--tmux` spawns a detached handoff server whose argv omits the
    flag, and that server's preflight handlers call ``preflight_source_code``
    without ``auto_install``, so it defaults to True. The install would land in
    a grandchild that outlives the command, for the life of the handoff channel.
    Neither half is fixable from this repo, so the combination is refused here.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill = Path(self._tmp.name) / "mmdx" / "scripts"
        self.skill.mkdir(parents=True)
        self.script = self.skill / "mmd.py"
        self.script.write_text("# fixture\n", encoding="utf-8")

    def install_parser(self) -> Path:
        module = MODULE.mmdx_parser_module_path(self.script)
        module.mkdir(parents=True)
        return module

    def open_selected(self, **overrides: object):
        options: dict[str, object] = {
            "root_dir": Path(self._tmp.name),
            "tmux": True,
            "tmux_submit": False,
            "allow_parser_install": False,
            "mmd_script": self.script,
        }
        options.update(overrides)
        selected = {"path": str(self.skill / "diagram.mmdx"), "rel_path": "diagram.mmdx"}
        return MMDX._open_selected_mmdx(selected, **options)  # type: ignore[arg-type]

    def test_parser_module_path_matches_the_skill_layout(self) -> None:
        self.assertEqual(
            MODULE.mmdx_parser_module_path(self.script),
            self.skill / "node_modules" / "mermaid",
        )
        self.assertFalse(MODULE.mmdx_parser_installed(self.script))
        self.install_parser()
        self.assertTrue(MODULE.mmdx_parser_installed(self.script))

    def test_tmux_without_consent_is_refused_when_the_parser_is_absent(self) -> None:
        with self.assertRaises(MODULE.MmdxOpenError) as ctx:
            MODULE.assert_tmux_parser_install_consent(
                self.script, allow_parser_install=False
            )
        self.assertEqual(ctx.exception.error_type, "mmdx_tmux_parser_install_unconsented")
        self.assertTrue(ctx.exception.recoverable)
        self.assertIn("--setup-parser", ctx.exception.recovery_hint)
        self.assertIn("--allow-parser-install", ctx.exception.recovery_hint)
        self.assertEqual(
            ctx.exception.data["parser_module"],
            str(self.skill / "node_modules" / "mermaid"),
        )

    def test_explicit_consent_is_honoured(self) -> None:
        MODULE.assert_tmux_parser_install_consent(
            self.script, allow_parser_install=True
        )

    def test_an_already_installed_parser_makes_tmux_safe(self) -> None:
        """The skill short-circuits before installing, so there is nothing to refuse."""
        self.install_parser()
        MODULE.assert_tmux_parser_install_consent(
            self.script, allow_parser_install=False
        )

    def test_an_unanswerable_check_refuses_rather_than_allowing(self) -> None:
        """Only positive evidence may relax the gate."""
        missing = Path(self._tmp.name) / "gone" / "scripts" / "mmd.py"
        with self.assertRaises(MODULE.MmdxOpenError):
            MODULE.assert_tmux_parser_install_consent(
                missing, allow_parser_install=False
            )

    def test_open_refuses_before_spawning_anything(self) -> None:
        with mock.patch.object(MMDX.subprocess, "run") as run:
            with self.assertRaises(MODULE.MmdxOpenError) as ctx:
                self.open_selected()
        self.assertEqual(ctx.exception.error_type, "mmdx_tmux_parser_install_unconsented")
        run.assert_not_called()

    def test_a_non_tmux_open_is_untouched_and_still_declines_installs(self) -> None:
        """The gate is narrow: only --tmux can reach the detached child."""
        with mock.patch.object(MMDX.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = self.open_selected(tmux=False)
        command = result["command"]
        self.assertIn("--no-parser-install", command)
        self.assertNotIn("--tmux", command)
        self.assertNotIn("--handoff-ttl", command)

    def test_tmux_submit_alone_does_not_trip_the_gate(self) -> None:
        """Only --tmux sets the skill's tmux_handoff dest, which spawns the child."""
        with mock.patch.object(MMDX.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = self.open_selected(tmux=False, tmux_submit=True)
        self.assertIn("--tmux-submit", result["command"])
        self.assertNotIn("--tmux", result["command"])

    def test_an_allowed_tmux_open_states_the_handoff_ttl(self) -> None:
        with mock.patch.object(MMDX.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = self.open_selected(allow_parser_install=True)
        command = result["command"]
        self.assertIn("--tmux", command)
        self.assertEqual(
            command[command.index("--handoff-ttl") + 1],
            str(MODULE.MMDX_HANDOFF_TTL_SECONDS),
        )
        self.assertNotIn("--no-parser-install", command)

    def test_the_declared_lease_bound_is_what_gets_passed(self) -> None:
        """The state-mutation manifest claims a bounded 600s detached tail."""
        self.assertEqual(MODULE.MMDX_HANDOFF_TTL_SECONDS, 600)

    def test_an_installed_parser_tmux_open_still_declines_installs(self) -> None:
        self.install_parser()
        with mock.patch.object(MMDX.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = self.open_selected()
        command = result["command"]
        self.assertIn("--tmux", command)
        self.assertIn("--no-parser-install", command)
        self.assertIn("--handoff-ttl", command)


if __name__ == "__main__":
    unittest.main()
