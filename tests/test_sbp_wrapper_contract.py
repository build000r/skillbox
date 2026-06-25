from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SBP = ROOT_DIR / "scripts" / "sbp"


def _run_sbp(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SBP), *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "SKILLBOX_ROOT": str(ROOT_DIR),
            "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
            "PYTHONPATH": str(ROOT_DIR / ".env-manager"),
        },
    )


def _make_skill_source(root: Path, name: str = "demo-skill") -> Path:
    source = root / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return source


class SbpWrapperContractTests(unittest.TestCase):
    def test_robot_docs_safe_first_try_is_parseable_json(self) -> None:
        capabilities = _run_sbp("capabilities", "--json")
        self.assertEqual(capabilities.returncode, 0, capabilities.stderr)
        payload = json.loads(capabilities.stdout)
        commands = {command["name"]: command for command in payload["commands"]}
        self.assertEqual(
            commands["robot-docs"]["safe_first_try"],
            "sbp robot-docs guide --json",
        )

        robot_docs = _run_sbp("robot-docs", "guide", "--json")
        self.assertEqual(robot_docs.returncode, 0, robot_docs.stderr)
        docs_payload = json.loads(robot_docs.stdout)
        self.assertEqual(docs_payload["ok"], True)
        self.assertEqual(docs_payload["topic"], "guide")
        self.assertIn("Skillbox wrapper agent guide", docs_payload["guide"])

    def test_recalibrate_auto_fix_json_rejects_with_structured_guidance(self) -> None:
        result = _run_sbp("recalibrate", "--auto-fix", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "UNSUPPORTED_JSON_AUTO_FIX")
        self.assertIn("sbp recalibrate --json", payload["error"]["next_actions"])
        self.assertIn("fixes[].command", payload["error"]["next_actions"][1])

    def test_legacy_sync_requires_preview_or_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = _make_skill_source(Path(tmpdir))
            result = _run_sbp("sync", str(source))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("requires --dry-run to preview or --yes to apply", result.stderr)
        self.assertIn("sbp skill on <skill>", result.stderr)

    def test_legacy_sync_dry_run_forwards_to_skill_add_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = _make_skill_source(Path(tmpdir))
            result = _run_sbp("sync", str(source), "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skill add: demo-skill (dry-run)", result.stdout)
        self.assertIn(f"source: {source}", result.stdout)

    def test_recalibrate_auto_fix_returns_nonzero_when_heal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script_dir = root / "scripts"
            script_dir.mkdir()
            wrapper = script_dir / "sbp"
            shutil.copy2(SBP, wrapper)
            wrapper.chmod(0o755)

            env_manager = root / ".env-manager"
            env_manager.mkdir()
            manage = env_manager / "manage.py"
            manage.write_text(
                textwrap.dedent(
                    """\
                    from __future__ import annotations

                    import json
                    import sys

                    args = sys.argv[1:]
                    if args[:1] == ["skills"] and "--format" in args:
                        print(json.dumps({
                            "issues": {"missing_for_cwd": [{"name": "needs-heal"}]},
                            "beads": {"required": False},
                        }))
                    elif args[:2] == ["skill", "heal"]:
                        print("heal failed for needs-heal", file=sys.stderr)
                        sys.exit(9)
                    else:
                        print("stub ok")
                    """
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(wrapper), "recalibrate", "--auto-fix"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "SKILLBOX_ROOT": str(root),
                    "SKILLBOX_INVOKE_CWD": str(root),
                    "SKILLBOX_CONFIG_ROOT": str(root / "skillbox-config"),
                    "SKILLBOX_MONOSERVER_ROOT": str(root),
                },
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("heal failed for needs-heal", result.stderr)
        self.assertIn("one or more heal commands failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
