from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.rch_adapter import build_rch_stage_plan, prepare_rch_stage  # noqa: E402


class RchAdapterTests(unittest.TestCase):
    def test_plan_is_safe_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_id="canary",
                command_parts=["--", "cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )

        self.assertTrue(plan["ok"])
        self.assertFalse(plan["mutates"])
        self.assertFalse(plan["deletes"])
        self.assertFalse(plan["remote_writes"])
        self.assertEqual(plan["wrappers"]["real_ssh"], "/bin/echo")
        self.assertIn("SKILLBOX_RCH_ADAPTER_ALLOW_DELETE", plan["env"])
        self.assertEqual(plan["env"]["SKILLBOX_RCH_ADAPTER_ALLOW_DELETE"], "0")
        self.assertIn("cargo", plan["command"]["exec_argv"])

    def test_prepare_copies_without_delete_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            (source / "src").mkdir(parents=True)
            (source / "src" / "lib.rs").write_text("pub fn ok() {}\n", encoding="utf-8")
            (source / ".git").mkdir()
            (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (source / "target").mkdir()
            (source / "target" / "debug.o").write_text("skip\n", encoding="utf-8")
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )

            result = prepare_rch_stage(plan)
            staged = Path(plan["stage"]["local_project_root"])

            self.assertTrue((staged / "src" / "lib.rs").is_file())
            self.assertFalse((staged / ".git").exists())
            self.assertFalse((staged / "target").exists())
            self.assertFalse(result["deletes"])
            self.assertEqual(result["copy"]["deleted_entries"], 0)
            self.assertTrue(Path(plan["wrappers"]["ssh"]).is_file())
            self.assertTrue(Path(plan["wrappers"]["rsync"]).is_file())
            manifest = json.loads(Path(plan["stage"]["manifest_path"]).read_text(encoding="utf-8"))
            self.assertFalse(manifest["deletes"])
            self.assertIn("manual_cleanup_commands", manifest)

    def test_ssh_wrapper_rewrites_only_remote_command_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )
            prepare_rch_stage(plan, copy_source=False)
            env = {**os.environ, **plan["env"]}
            local_key = f"{plan['stage']['local_projects_root']}/key"
            result = subprocess.run(
                [
                    plan["wrappers"]["ssh"],
                    "-i",
                    local_key,
                    "portfolio-devbox-rch-ossshd",
                    "sh",
                    "-c",
                    f"mkdir -p {plan['stage']['local_projects_root']} && cd /data/projects/repo",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(local_key, result.stdout)
        self.assertIn(plan["remote"]["projects_root"], result.stdout)
        self.assertNotIn("/data/projects/repo", result.stdout)

    def test_ssh_wrapper_preserves_dependency_preflight_report_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )
            prepare_rch_stage(plan, copy_source=False)
            env = {**os.environ, **plan["env"]}
            probe = (
                "missing=0; for required in /data/projects/repo/Cargo.toml; do "
                "if [ -f \"$required\" ]; then printf 'RCH_DEP_PRESENT:%s\\n' \"$required\"; "
                "else printf 'RCH_DEP_MISSING:%s\\n' \"$required\"; missing=1; fi; done"
            )
            result = subprocess.run(
                [
                    plan["wrappers"]["ssh"],
                    "portfolio-devbox-rch-ossshd",
                    f"sh -lc '{probe}'",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(plan["remote"]["projects_root"], result.stdout)
        self.assertIn("RCH_DEP_PRESENT:%s\\n", result.stdout)
        self.assertIn('"$required"', result.stdout)
        self.assertNotIn("RCH_DEP_PRESENT:%s\\n' \"$actual\"", result.stdout)
        self.assertIn("actual=\"/srv/skillbox/rch-adapter/canary/projects/${actual#/data/projects/}\"", result.stdout)

    def test_ssh_wrapper_rewrites_sh_s_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            real_ssh = root / "fake-ssh"
            real_ssh.write_text(
                "#!/usr/bin/env sh\n"
                "printf 'ARGS:'\n"
                "printf ' [%s]' \"$@\"\n"
                "printf '\\nSTDIN:\\n'\n"
                "cat\n",
                encoding="utf-8",
            )
            real_ssh.chmod(0o755)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh=str(real_ssh),
                real_rsync="/bin/echo",
            )
            prepare_rch_stage(plan, copy_source=False)
            env = {**os.environ, **plan["env"]}
            result = subprocess.run(
                [
                    plan["wrappers"]["ssh"],
                    "portfolio-devbox-rch-ossshd",
                    "sh",
                    "-s",
                ],
                input="touch /data/projects/repo && cd /data/projects/repo\n",
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARGS: [portfolio-devbox-rch-ossshd] [sh] [-s]", result.stdout)
        self.assertIn("touch /srv/skillbox/rch-adapter/canary/projects/repo", result.stdout)
        self.assertIn("cd /srv/skillbox/rch-adapter/canary/projects/repo", result.stdout)
        self.assertNotIn("/data/projects/repo", result.stdout)

    def test_rsync_wrapper_rewrites_remote_path_and_strips_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )
            prepare_rch_stage(plan, copy_source=False)
            env = {**os.environ, **plan["env"]}
            result = subprocess.run(
                [
                    plan["wrappers"]["rsync"],
                    "-az",
                    "--delete",
                    "--compress-choice=zstd",
                    "--rsync-path",
                    "mkdir -p /data/projects/repo && rsync",
                    "src/",
                    "skillbox@portfolio-devbox-rch-ossshd:/data/projects/repo",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--delete", result.stdout)
        self.assertNotIn("--compress-choice=zstd", result.stdout)
        self.assertIn(plan["remote"]["projects_root"], result.stdout)
        self.assertNotIn("/data/projects/repo", result.stdout)

    def test_prepare_replaces_dangling_alias_symlink(self) -> None:
        # Regression: a stale symlink pointing at a removed target raised
        # FileExistsError because Path.exists() follows symlinks and reported
        # False, while symlink_to() refused to overwrite the existing link.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )
            alias = Path(plan["stage"]["local_alias_root"])
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(Path(tmpdir) / "missing-target")

            prepare_rch_stage(plan, copy_source=False)

            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), plan["stage"]["local_projects_root"])

    def test_prepare_refuses_to_overwrite_real_alias_directory(self) -> None:
        # Regression: a real directory at the alias path used to silently pass
        # and then path translation diverged because the alias was no longer a
        # symlink to the projects root.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            plan = build_rch_stage_plan(
                root,
                source=source,
                stage_root=root / "stage",
                stage_id="canary",
                command_parts=["cargo", "check"],
                real_ssh="/bin/echo",
                real_rsync="/bin/echo",
            )
            alias = Path(plan["stage"]["local_alias_root"])
            alias.mkdir(parents=True)

            with self.assertRaises(RuntimeError) as ctx:
                prepare_rch_stage(plan, copy_source=False)

            self.assertIn("not a symlink", str(ctx.exception))

    def test_cli_dry_run_json_is_plan_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "repo"
            source.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    ".env-manager/manage.py",
                    "rch-stage",
                    "--source",
                    str(source),
                    "--stage-root",
                    str(root / "stage"),
                    "--stage-id",
                    "canary",
                    "--real-ssh",
                    "/bin/echo",
                    "--real-rsync",
                    "/bin/echo",
                    "--dry-run",
                    "--format",
                    "json",
                    "--",
                    "cargo",
                    "check",
                ],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertFalse(payload["mutates"])
        self.assertFalse(payload["remote_writes"])
        self.assertEqual(payload["command"]["argv"], ["cargo", "check"])


if __name__ == "__main__":
    unittest.main()
