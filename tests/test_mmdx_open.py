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


if __name__ == "__main__":
    unittest.main()
