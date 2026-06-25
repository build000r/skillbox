"""Tests for repo-local .skillbox/skill-overrides.yaml inputs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import multiprocessing as mp
import os
import random
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import policy_eval as POLICY_EVAL  # noqa: E402
from runtime_manager import cli as RUNTIME_CLI  # noqa: E402
from runtime_manager import skill_visibility as SKILL_VISIBILITY  # noqa: E402
from runtime_manager._skill_common import DISPATCHER_CORE  # noqa: E402
from runtime_manager.errors import (  # noqa: E402
    OVERRIDE_PARSE_ERROR,
    OVERRIDE_REFUSED_FLOOR,
    OVERRIDE_REFUSED_GLOBAL_ESCALATION,
    OVERRIDE_SKILL_UNKNOWN,
    PRUNE_SKIPPED_PINNED,
    ValidationError,
)
from runtime_manager.policy_eval import (  # noqa: E402
    OverrideWriteLockTimeout,
    _global_override_refusal_context,
    _repo_override_policy,
    update_repo_override_policy,
)
from runtime_manager.skill_visibility import (  # noqa: E402
    active_overlays,
    apply_skill_lifecycle_plan,
    collect_skill_visibility,
    explain_skill_visibility,
    skill_lifecycle_plan,
)
from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _append_pin(policy: dict[str, object], name: str) -> dict[str, object]:
    pins = list(policy.get("pin_on") or [])
    if name not in pins:
        pins.append(name)
    policy["pin_on"] = pins
    return policy


def _write_override(repo: Path, text: str) -> None:
    (repo / ".skillbox").mkdir(exist_ok=True)
    (repo / ".skillbox" / "skill-overrides.yaml").write_text(text, encoding="utf-8")


def _write_project_skill(repo: Path, name: str) -> None:
    skill_dir = repo / ".codex" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def _write_source_skill(root: Path, name: str) -> Path:
    source = root / name
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return source


def _install_project_skill(repo: Path, name: str, source: Path, *, surface: str = "claude") -> Path:
    install_root = repo / f".{surface}" / "skills"
    install_root.mkdir(parents=True, exist_ok=True)
    link = install_root / name
    link.symlink_to(source, target_is_directory=True)
    return link


def _write_scope_policy(root: Path, source_root: Path, skill_name: str, allowed_path: Path) -> dict[str, object]:
    clients_root = root / "clients"
    clients_root.mkdir(exist_ok=True)
    (root / "skill-scope.yaml").write_text(
        "version: 1\n"
        "skill_source_roots:\n"
        f"  - {source_root}\n"
        "rules:\n"
        "  - id: skill-scope\n"
        f"    skills: [{skill_name}]\n"
        f"    paths: [{allowed_path}]\n",
        encoding="utf-8",
    )
    return {
        "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
        "clients": [],
        "skills": [],
    }


def _write_multi_scope_policy(root: Path, source_root: Path, skill_names: list[str], allowed_path: Path) -> dict[str, object]:
    clients_root = root / "clients"
    clients_root.mkdir(exist_ok=True)
    skill_rows = "\n".join(f"      - {name}" for name in skill_names)
    (root / "skill-scope.yaml").write_text(
        "version: 1\n"
        "skill_source_roots:\n"
        f"  - {source_root}\n"
        "rules:\n"
        "  - id: generated-scope\n"
        "    skills:\n"
        f"{skill_rows}\n"
        f"    paths: [{allowed_path}]\n",
        encoding="utf-8",
    )
    return {
        "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
        "clients": [],
        "skills": [],
    }


def _write_global_floor_policy(root: Path, source_root: Path, skill_name: str, allowed_path: Path) -> dict[str, object]:
    clients_root = root / "clients"
    clients_root.mkdir(exist_ok=True)
    (root / "skill-scope.yaml").write_text(
        "version: 1\n"
        "skill_source_roots:\n"
        f"  - {source_root}\n"
        "global_allowlist: [smart, sbp]\n"
        "rules:\n"
        "  - id: dispatcher-global\n"
        "    skills: [smart, sbp]\n"
        "    allow_global: true\n"
        "  - id: local-default-off\n"
        f"    skills: [{skill_name}]\n"
        "    default: off\n"
        f"    paths: [{allowed_path}]\n",
        encoding="utf-8",
    )
    return {
        "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
        "clients": [],
        "skills": [],
    }


def _skill_toggle_args(repo: Path, action: str, name: str, **overrides: object) -> Namespace:
    values = {
        "skill_action": action,
        "skill_name": name,
        "cwd": str(repo),
        "to": "project",
        "category": [],
        "source": None,
        "from_scope": "project",
        "force": False,
        "allow_directories": False,
        "verify": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _write_seeded_override(repo: Path, *, pin_on: list[str], pin_off: list[str]) -> None:
    lines = ["version: 1"]
    if pin_on:
        lines.append("pin_on:")
        lines.extend(f"  - {name}" for name in pin_on)
    if pin_off:
        lines.append("pin_off:")
        lines.extend(f"  - {name}" for name in pin_off)
    if len(lines) > 1:
        _write_override(repo, "\n".join(lines) + "\n")


def _plan_action_signature(plan: dict[str, object]) -> list[tuple[str, str, str, str]]:
    return sorted(
        (
            str(action.get("op") or ""),
            str(action.get("skill") or ""),
            str(action.get("destination") or ""),
            str(action.get("reason") or ""),
        )
        for action in plan.get("actions") or []
    )


def _override_writer_worker(repo_path: str, name: str) -> None:
    update_repo_override_policy(Path(repo_path), lambda policy: _append_pin(policy, name))


def _run_runtime_cli(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = RUNTIME_CLI.main(argv)
    return code, output.getvalue()


class RepoSkillOverridePolicyTests(unittest.TestCase):
    def test_reads_fixture_repo_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)

            policy = _repo_override_policy(fleet.repo("healthy"))

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], ["needs-beads"])
        self.assertEqual(policy["pin_off"], ["tiny-marketing"])
        self.assertEqual(policy["opt_out_global"], ["fixture-global-optout"])
        self.assertEqual(policy["overlays"]["enable"], ["marketing"])
        self.assertEqual(policy["overlays"]["disable"], ["swarm"])
        self.assertEqual(policy["defaults"], ["tiny-ui"])
        self.assertEqual(policy["reason"], "fixture override")
        self.assertTrue(policy["_policy_path"].endswith(".skillbox/skill-overrides.yaml"))

    def test_fixture_overlay_repo_materializes_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)

            policy = _repo_override_policy(fleet.repo("overlay-repo"))

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_off"], ["tiny-cli"])
        self.assertEqual(policy["reason"], "fixture overlay override")
        self.assertTrue(policy["_policy_path"].endswith(".skillbox/skill-overrides.yaml"))

    def test_subdir_invocation_resolves_to_git_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [wiki]\n",
                encoding="utf-8",
            )
            subdir = repo / "src" / "pkg"
            subdir.mkdir(parents=True)

            policy = _repo_override_policy(subdir)

        self.assertEqual(policy["pin_on"], ["wiki"])
        self.assertEqual(policy["_repo_root"], str(repo))

    def test_unknown_top_level_key_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\nsurprise: true\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertEqual(policy["errors"][0]["key"], "surprise")
        self.assertIn("surprise", policy["errors"][0]["message"])

    def test_wrong_overlay_shape_is_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\noverlays: []\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["errors"][0]["key"], "overlays")
        self.assertIn("mapping", policy["errors"][0]["message"])

    def test_malformed_yaml_fails_safe_with_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            (repo / ".skillbox").mkdir()
            (repo / ".skillbox" / "skill-overrides.yaml").write_text(
                "version: 1\npin_on: [unterminated\n",
                encoding="utf-8",
            )

            policy = _repo_override_policy(repo)

        self.assertFalse(policy["ok"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["overlays"], {"enable": [], "disable": []})
        self.assertEqual(policy["errors"][0]["code"], OVERRIDE_PARSE_ERROR)
        self.assertIn("Failed to parse", policy["errors"][0]["message"])

    def test_missing_override_file_is_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            policy = _repo_override_policy(repo)

        self.assertTrue(policy["ok"], policy["errors"])
        self.assertEqual(policy["pin_on"], [])
        self.assertEqual(policy["pin_off"], [])
        self.assertEqual(policy["defaults"], [])

    def test_missing_override_file_is_visibility_noop_from_repo_and_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            subdir = repo / "src" / "pkg"
            subdir.mkdir(parents=True)
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            _install_project_skill(repo, "alpha", source, surface="claude")
            _install_project_skill(repo, "alpha", source, surface="codex")
            model = _write_scope_policy(root, source_root, "alpha", repo)

            for cwd in (repo, subdir):
                with self.subTest(cwd=cwd):
                    self.assertFalse((repo / ".skillbox" / "skill-overrides.yaml").exists())
                    normal = collect_skill_visibility(
                        model,
                        cwd=str(cwd),
                        include_global=False,
                        include_project=True,
                    )
                    with mock.patch(
                        "runtime_manager.skill_visibility._repo_override_visibility",
                        return_value=([], []),
                    ):
                        baseline = collect_skill_visibility(
                            model,
                            cwd=str(cwd),
                            include_global=False,
                            include_project=True,
                        )

                    self.assertEqual(
                        json.dumps(normal, sort_keys=True),
                        json.dumps(baseline, sort_keys=True),
                    )
                    self.assertNotIn(
                        "repo-override-file",
                        [str(layer.get("id") or "") for layer in normal["layers"]],
                    )
                    self.assertEqual(
                        [item["name"] for item in normal["effective"]],
                        ["alpha"],
                    )

    def test_override_visibility_is_portable_without_skillbox_config_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            _install_project_skill(repo, "alpha", source, surface="claude")
            _write_override(repo, "version: 1\npin_on: [alpha]\n")
            model = _write_scope_policy(root, source_root, "alpha", repo)
            fixture_br = root / "bin" / "br"
            fixture_br.parent.mkdir()
            fixture_br.write_text("#!/bin/sh\n", encoding="utf-8")

            self.assertFalse((root / "skillbox-config").exists())
            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch("runtime_manager.skill_visibility.shutil.which", return_value=str(fixture_br)),
            ):
                payload = collect_skill_visibility(
                    model,
                    cwd=str(repo),
                    include_global=False,
                    include_project=True,
                )

        rendered = json.dumps(payload, sort_keys=True)
        external_config_roots = [
            str((ROOT_DIR.parent / "skillbox-config").resolve()),
            str((ROOT_DIR.parent.parent / "skillbox-config").resolve()),
        ]
        for forbidden in [str(Path.home().resolve()), *external_config_roots]:
            self.assertNotIn(forbidden, rendered)
        effective = {item["name"]: item for item in payload["effective"]}
        self.assertEqual(effective["alpha"]["layer"], "repo-override-file")
        self.assertTrue(effective["alpha"]["policy_path"].startswith(str(root)))

    def test_skill_why_cli_is_portable_under_temp_clients_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            clients_root = root / "clients"
            clients_root.mkdir()
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            _write_override(repo, "version: 1\npin_on: [alpha]\n")
            machines_file = root / "machines.yaml"
            machines_file.write_text(
                "version: 1\n"
                "machines:\n"
                "  fixture-box:\n"
                "    hostnames: [fixture-box]\n"
                f"    home: {fake_home}\n"
                f"    repo_roots: [{root}]\n",
                encoding="utf-8",
            )
            model = _write_scope_policy(root, source_root, "alpha", repo)
            model["env"] = {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)}

            self.assertFalse((root / "skillbox-config").exists())
            env = {
                "SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root),
                "SKILLBOX_MACHINES_FILE": str(machines_file),
                "SKILLBOX_MACHINE": "fixture-box",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                code, text = _run_runtime_cli([
                    "skill",
                    "why",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

        self.assertEqual(code, 0)
        rendered = json.dumps(json.loads(text), sort_keys=True)
        external_config_roots = [
            str((ROOT_DIR.parent / "skillbox-config").resolve()),
            str((ROOT_DIR.parent.parent / "skillbox-config").resolve()),
        ]
        for forbidden in [str(Path.home().resolve()), *external_config_roots, "/srv/skillbox"]:
            self.assertNotIn(forbidden, rendered)
        self.assertIn(str(root), rendered)

    def test_override_writer_preserves_sequential_read_modify_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            first = update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))
            second = update_repo_override_policy(repo, lambda policy: _append_pin(policy, "beta"))
            policy = _repo_override_policy(repo)

        self.assertTrue(first["changed"])
        self.assertTrue(second["changed"])
        self.assertEqual(policy["pin_on"], ["alpha", "beta"])

    def test_override_writer_serializes_concurrent_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            ctx = mp.get_context("spawn")
            names = ["alpha", "beta", "gamma"]
            procs = [
                ctx.Process(target=_override_writer_worker, args=(str(repo), name))
                for name in names
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=10)
                self.assertIsNotNone(proc.exitcode, "writer process hung")
                self.assertEqual(proc.exitcode, 0, "writer process failed")

            policy = _repo_override_policy(repo)

        self.assertEqual(policy["pin_on"], names)

    def test_override_writer_failed_replace_leaves_old_file_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))
            policy_path = repo / ".skillbox" / "skill-overrides.yaml"
            before = policy_path.read_text(encoding="utf-8")

            with mock.patch.object(POLICY_EVAL.os, "replace", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    update_repo_override_policy(repo, lambda policy: _append_pin(policy, "beta"))

            leftovers = [
                path.name for path in policy_path.parent.iterdir()
                if path.name.startswith(".skill-overrides.yaml.") and path.suffix == ".tmp"
            ]
            after = policy_path.read_text(encoding="utf-8")

        self.assertEqual(after, before)
        self.assertEqual(leftovers, [])

    def test_override_writer_fsyncs_file_and_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))

            with mock.patch.object(POLICY_EVAL.os, "fsync") as fsync_mock:
                update_repo_override_policy(repo, lambda policy: _append_pin(policy, "alpha"))

        self.assertGreaterEqual(fsync_mock.call_count, 2)

    def test_override_writer_lock_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            policy_dir = repo / ".skillbox"
            policy_dir.mkdir()
            lock_path = policy_dir / "skill-overrides.yaml.lock"
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                with self.assertRaises(OverrideWriteLockTimeout) as ctx:
                    update_repo_override_policy(
                        repo,
                        lambda policy: _append_pin(policy, "blocked"),
                        timeout=0.05,
                    )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

        self.assertEqual(ctx.exception.lock_path, lock_path)

    def test_repo_override_pin_on_and_pin_off_are_effective_layers_shared_by_explain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            _write_project_skill(repo, "alpha")
            _write_project_skill(repo, "beta")
            _write_override(repo, "version: 1\npin_on: [alpha]\npin_off: [beta]\n")

            payload = collect_skill_visibility(
                {},
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )
            explanation = explain_skill_visibility(
                {},
                "alpha",
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )
            disabled_explanation = explain_skill_visibility(
                {},
                "beta",
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )

        effective = {item["name"]: item for item in payload["effective"]}
        decisions = {item["name"]: item for item in payload["visibility_decisions"]}
        self.assertEqual(effective["alpha"]["layer"], "repo-override-file")
        self.assertEqual(effective["alpha"]["winning_layer"], "repo-override-file")
        self.assertEqual(effective["alpha"]["state"], "pinned")
        self.assertNotIn("beta", effective)
        self.assertEqual(decisions["beta"]["layer"], "repo-override-file")
        self.assertEqual(decisions["beta"]["winning_layer"], "repo-override-file")
        self.assertEqual(decisions["beta"]["state"], "disabled")
        self.assertEqual(explanation["layer"], "repo-override-file")
        self.assertEqual(explanation["winning_layer"], "repo-override-file")
        self.assertTrue(explanation["visible"])
        self.assertEqual(
            [layer["layer"] for layer in explanation["layers"] if layer.get("wins")],
            ["repo-override-file"],
        )
        self.assertEqual(disabled_explanation["layer"], "repo-override-file")
        self.assertEqual(disabled_explanation["winning_layer"], "repo-override-file")
        self.assertFalse(disabled_explanation["visible"])
        self.assertEqual(disabled_explanation["winner"]["state"], "disabled")
        self.assertEqual(disabled_explanation["winner"]["override_action"], "pin_off")
        self.assertEqual(
            [layer["layer"] for layer in disabled_explanation["layers"] if layer.get("wins")],
            ["repo-override-file"],
        )
        self.assertIn("disabled by the OVERRIDE layer", disabled_explanation["reason"])

    def test_pin_on_beats_default_off_and_why_reports_override_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)
            _write_override(repo, "version: 1\npin_on: [alpha]\n")

            explanation = explain_skill_visibility(
                model,
                "alpha",
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )

        self.assertTrue(explanation["visible"])
        self.assertEqual(explanation["winning_layer"], "repo-override-file")
        self.assertEqual(explanation["layer"], "repo-override-file")
        self.assertEqual(explanation["winner"]["override_action"], "pin_on")
        self.assertEqual(
            [layer["layer"] for layer in explanation["layers"] if layer.get("wins")],
            ["repo-override-file"],
        )

    def test_repo_override_pin_off_cannot_disable_dispatcher_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            _write_project_skill(repo, "sbp")
            _write_override(repo, "version: 1\npin_off: [sbp]\n")

            payload = collect_skill_visibility(
                {},
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )

        effective = {item["name"]: item for item in payload["effective"]}
        self.assertNotEqual(effective["sbp"]["layer"], "repo-override-file")
        self.assertEqual(effective["sbp"]["state"], "ok")
        override_layer = [
            layer for layer in payload["layers"]
            if layer.get("id") == "repo-override-file"
        ][0]
        self.assertEqual(override_layer["vetoed_floor"], ["sbp"])

    def test_explain_floor_skill_pin_off_veto_has_non_winning_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            _write_project_skill(repo, "sbp")
            _write_override(repo, "version: 1\npin_off: [sbp]\n")

            explanation = explain_skill_visibility(
                {},
                "sbp",
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )

        self.assertTrue(explanation["visible"])
        self.assertEqual(explanation["winning_layer"], explanation["winner"]["layer"])
        winning_layers = [layer for layer in explanation["layers"] if layer.get("wins")]
        self.assertEqual(len(winning_layers), 1)
        self.assertEqual(winning_layers[0]["layer"], explanation["winning_layer"])
        veto_layers = [
            layer for layer in explanation["layers"]
            if layer.get("state") == "vetoed_floor"
        ]
        self.assertEqual(len(veto_layers), 1)
        self.assertEqual(veto_layers[0]["layer"], "repo-override-file")
        self.assertFalse(veto_layers[0]["wins"])
        self.assertIn("cannot be disabled", veto_layers[0]["lost_reason"])

    def test_repo_override_pin_on_without_source_is_not_effective(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            _write_override(repo, "version: 1\npin_on: [ghost]\n")

            payload = collect_skill_visibility(
                {},
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )
            explanation = explain_skill_visibility(
                {},
                "ghost",
                cwd=str(repo),
                include_global=False,
                include_project=True,
            )

        effective = {item["name"]: item for item in payload["effective"]}
        decisions = {item["name"]: item for item in payload["visibility_decisions"]}
        self.assertNotIn("ghost", effective)
        self.assertEqual(decisions["ghost"]["layer"], "repo-override-file")
        self.assertEqual(decisions["ghost"]["winning_layer"], "repo-override-file")
        self.assertEqual(decisions["ghost"]["state"], "broken")
        self.assertEqual(decisions["ghost"]["broken_reason"], "override_source_missing")
        self.assertEqual(explanation["layer"], "repo-override-file")
        self.assertEqual(explanation["winning_layer"], "repo-override-file")
        self.assertFalse(explanation["visible"])
        self.assertEqual(explanation["winner"]["state"], "broken")
        self.assertIn("no installed occurrence or source was found", explanation["reason"])

    def test_skill_why_cli_explains_absence_without_brain_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fleet = build_fixture_fleet(tmpdir)
            buf = io.StringIO()

            with fleet._home_patched(), mock.patch.object(
                RUNTIME_CLI,
                "_filtered_model_for_args",
                return_value=fleet.model(),
            ), contextlib.redirect_stdout(buf):
                code = RUNTIME_CLI.main([
                    "skill",
                    "why",
                    "needs-beads",
                    "--cwd",
                    str(fleet.repo("overlay-repo")),
                    "--format",
                    "json",
                ])

        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["skill"], "needs-beads")
        self.assertFalse(payload["visible"])
        self.assertEqual(payload["remediation"][0]["kind"], "on")
        self.assertEqual(payload["remediation"][0]["command"], "sbp skill on needs-beads --cwd $PWD")
        self.assertEqual(payload["next_actions"][0], "sbp skill on needs-beads --cwd $PWD")

    def test_prune_firewall_skips_pinned_override_at_plan_construction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            allowed = root / "allowed"
            source_root = root / "sources"
            allowed.mkdir()
            source = _write_source_skill(source_root, "alpha")
            link = _install_project_skill(repo, "alpha", source)
            _write_override(repo, "version: 1\npin_on: [alpha]\n")
            model = _write_scope_policy(root, source_root, "alpha", allowed)

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                dry_plan = skill_lifecycle_plan(model, "prune", cwd=str(repo), from_scope="project")
                applied_plan = skill_lifecycle_plan(model, "prune", cwd=str(repo), from_scope="project")
                dry_plan_json = json.dumps(dry_plan, sort_keys=True)
                applied_plan_json = json.dumps(applied_plan, sort_keys=True)
                dry = apply_skill_lifecycle_plan(dry_plan, dry_run=True)
                applied = apply_skill_lifecycle_plan(applied_plan, dry_run=False)

            self.assertEqual(dry_plan_json, applied_plan_json)
            self.assertEqual(dry["actions"], [])
            self.assertEqual(applied["actions"], [])
            self.assertEqual(dry["skipped"], applied["skipped"])
            self.assertEqual(dry["skipped"][0]["name"], "alpha")
            self.assertEqual(dry["skipped"][0]["reason"], "pinned")
            self.assertEqual(dry["skipped"][0]["code"], PRUNE_SKIPPED_PINNED)
            self.assertEqual(dry["summary"]["skipped"], 1)
            self.assertTrue(link.is_symlink())

    def test_prune_firewall_unlinks_pin_off_even_without_visibility_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            link = _install_project_skill(repo, "alpha", source)
            _write_override(repo, "version: 1\npin_off: [alpha]\n")
            model = _write_scope_policy(root, source_root, "alpha", repo)

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                dry_plan = skill_lifecycle_plan(model, "prune", cwd=str(repo), from_scope="project")
                applied_plan = skill_lifecycle_plan(model, "prune", cwd=str(repo), from_scope="project")
                dry = apply_skill_lifecycle_plan(dry_plan, dry_run=True)
                applied = apply_skill_lifecycle_plan(applied_plan, dry_run=False)

            self.assertEqual(dry["skipped"], [])
            self.assertEqual(len(dry["actions"]), 1)
            self.assertEqual(dry["actions"][0]["reason"], "pin_off")
            self.assertEqual(dry["actions"][0]["destination"], str(link))
            self.assertEqual(applied["actions"][0]["status"], "unlinked")
            self.assertFalse(link.exists())

    def test_seeded_prune_firewall_property_over_generated_overrides(self) -> None:
        skill_names = ["alpha", "beta", "gamma", "delta"]
        for seed in range(16):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    repo = _make_repo(root)
                    fake_home = root / "home"
                    fixture_br = root / "bin" / "br"
                    fixture_br.parent.mkdir()
                    fixture_br.write_text("#!/bin/sh\n", encoding="utf-8")
                    source_root = root / "sources"
                    sources = {
                        name: _write_source_skill(source_root, name)
                        for name in skill_names
                    }
                    for name in skill_names:
                        if rng.choice([True, False]):
                            _install_project_skill(repo, name, sources[name], surface="claude")

                    pin_on = sorted(name for name in skill_names if rng.random() < 0.35)
                    remaining = [name for name in skill_names if name not in pin_on]
                    pin_off = sorted(name for name in remaining if rng.random() < 0.35)
                    _write_seeded_override(repo, pin_on=pin_on, pin_off=pin_off)
                    model = _write_multi_scope_policy(root, source_root, skill_names, repo)

                    with (
                        mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                        mock.patch("runtime_manager.skill_visibility.shutil.which", return_value=str(fixture_br)),
                    ):
                        sync_first = apply_skill_lifecycle_plan(
                            skill_lifecycle_plan(model, "sync", cwd=str(repo), to="project"),
                            dry_run=False,
                        )
                        prune_dry_plan = skill_lifecycle_plan(
                            model,
                            "prune",
                            cwd=str(repo),
                            from_scope="project",
                        )
                        prune_dry = apply_skill_lifecycle_plan(prune_dry_plan, dry_run=True)
                        prune_apply_plan = skill_lifecycle_plan(
                            model,
                            "prune",
                            cwd=str(repo),
                            from_scope="project",
                        )
                        prune_apply = apply_skill_lifecycle_plan(prune_apply_plan, dry_run=False)
                        sync_fixed = apply_skill_lifecycle_plan(
                            skill_lifecycle_plan(model, "sync", cwd=str(repo), to="project"),
                            dry_run=True,
                        )
                        visibility = collect_skill_visibility(
                            model,
                            cwd=str(repo),
                            include_global=False,
                            include_project=True,
                        )

                self.assertEqual(_plan_action_signature(prune_dry), _plan_action_signature(prune_apply))
                self.assertFalse(
                    {
                        str(action.get("skill") or "")
                        for action in prune_dry["actions"]
                        if action.get("op") == "unlink"
                    } & set(pin_on)
                )
                self.assertFalse(
                    {
                        item["name"]
                        for item in visibility["effective"]
                    } & set(pin_off)
                )
                self.assertEqual(sync_fixed["actions"], [])
                rendered = json.dumps(
                    {
                        "sync_first": sync_first,
                        "prune_dry": prune_dry,
                        "prune_apply": prune_apply,
                        "sync_fixed": sync_fixed,
                        "visibility": visibility,
                    },
                    sort_keys=True,
                )
                self.assertNotIn(str(Path.home().resolve()), rendered)
                self.assertNotIn("/srv/skillbox", rendered)
                for name in pin_on + pin_off:
                    self.assertIn(name, skill_names)

    def test_skill_on_is_idempotent_and_returns_activation_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_scope_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "on", "alpha", verify=True)

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                first = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)
                second = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)
                third = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            policy = _repo_override_policy(repo)
            self.assertEqual(policy["pin_on"], ["alpha"])
            self.assertEqual(policy["pin_off"], [])
            self.assertTrue(first["changed"])
            self.assertFalse(first["noop"])
            self.assertEqual(first["summary"]["link"], 2)
            self.assertTrue(first["activation_packet"]["skill_md_sha256"])
            self.assertTrue(first["verification"]["verified"])
            self.assertIn("alpha", [item["name"] for item in first["verification"]["effective_now"]])
            self.assertFalse(second["changed"])
            self.assertTrue(second["noop"])
            self.assertEqual(second["actions"], [])
            self.assertEqual(
                json.dumps(second, sort_keys=True),
                json.dumps(third, sort_keys=True),
            )

    def test_skill_on_and_off_cli_are_idempotent_after_first_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_scope_policy(root, source_root, "alpha", repo)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                first_on_code, _first_on_text = _run_runtime_cli([
                    "skill",
                    "on",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                second_on_code, second_on_text = _run_runtime_cli([
                    "skill",
                    "on",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                third_on_code, third_on_text = _run_runtime_cli([
                    "skill",
                    "on",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                first_off_code, _first_off_text = _run_runtime_cli([
                    "skill",
                    "off",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                second_off_code, second_off_text = _run_runtime_cli([
                    "skill",
                    "off",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                third_off_code, third_off_text = _run_runtime_cli([
                    "skill",
                    "off",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

        second_on = json.loads(second_on_text)
        second_off = json.loads(second_off_text)
        self.assertEqual(first_on_code, 0)
        self.assertEqual(second_on_code, 0)
        self.assertEqual(third_on_code, 0)
        self.assertEqual(second_on["actions"], [])
        self.assertTrue(second_on["noop"])
        self.assertEqual(second_on_text, third_on_text)
        self.assertEqual(first_off_code, 0)
        self.assertEqual(second_off_code, 0)
        self.assertEqual(third_off_code, 0)
        self.assertEqual(second_off["actions"], [])
        self.assertTrue(second_off["noop"])
        self.assertEqual(second_off_text, third_off_text)

    def test_skill_on_dry_run_does_not_write_or_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_scope_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "on", "alpha")

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                dry = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=True)

            self.assertTrue(dry["override"]["would_change"])
            self.assertFalse(dry["changed"])
            self.assertFalse((repo / ".skillbox" / "skill-overrides.yaml").exists())
            self.assertFalse((repo / ".claude" / "skills" / "alpha").exists())
            self.assertFalse((repo / ".codex" / "skills" / "alpha").exists())

    def test_skill_on_text_prints_activation_packet_without_link_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            _install_project_skill(repo, "alpha", source, surface="claude")
            _install_project_skill(repo, "alpha", source, surface="codex")
            model = _write_scope_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "on", "alpha")

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                payload = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            self.assertEqual(payload["actions"], [])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                RUNTIME_CLI.print_skill_lifecycle_text(payload)
            text = output.getvalue()
            self.assertIn("actions: none", text)
            self.assertIn("activation packet:", text)
            self.assertIn("skill_md_sha256:", text)

    def test_skill_on_verify_requires_activation_packet_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            clients_root = root / "clients"
            clients_root.mkdir()
            source = _write_source_skill(root / "unlisted-sources", "alpha")
            _install_project_skill(repo, "alpha", source, surface="claude")
            _install_project_skill(repo, "alpha", source, surface="codex")
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }
            args = _skill_toggle_args(repo, "on", "alpha", verify=True)

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                payload = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            self.assertIsNone(payload["activation_packet"])
            self.assertFalse(payload["verification"]["verified"])
            self.assertIsNone(payload["verification"]["skill_md_sha256"])
            self.assertIn("alpha", [item["name"] for item in payload["verification"]["effective_now"]])
            self.assertTrue(payload["verification"]["symlink_resolved"])

    def test_skill_off_writes_pin_off_and_unlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            claude_link = _install_project_skill(repo, "alpha", source, surface="claude")
            codex_link = _install_project_skill(repo, "alpha", source, surface="codex")
            _write_override(repo, "version: 1\npin_on: [alpha]\n")
            model = _write_scope_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "off", "alpha")

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                dry = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=True)
                applied = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            policy = _repo_override_policy(repo)
            self.assertEqual(policy["pin_on"], [])
            self.assertEqual(policy["pin_off"], ["alpha"])
            self.assertTrue(dry["override"]["would_change"])
            self.assertEqual({action["status"] for action in dry["actions"]}, {"would_unlink"})
            self.assertTrue(applied["changed"])
            self.assertEqual(dry["requested_to"], "project")
            self.assertEqual(dry["resolved_to"], "project")
            self.assertEqual(applied["requested_to"], "project")
            self.assertEqual(applied["resolved_to"], "project")
            self.assertEqual({action["status"] for action in applied["actions"]}, {"unlinked"})
            self.assertFalse(claude_link.exists())
            self.assertFalse(codex_link.exists())

    def test_skill_off_refuses_dispatcher_floor_before_write(self) -> None:
        for floor_skill in DISPATCHER_CORE:
            with self.subTest(floor_skill=floor_skill):
                with tempfile.TemporaryDirectory() as tmpdir:
                    repo = _make_repo(Path(tmpdir))
                    _write_override(repo, "version: 1\npin_on: [alpha]\n")
                    before = (repo / ".skillbox" / "skill-overrides.yaml").read_bytes()
                    args = _skill_toggle_args(repo, "off", floor_skill)

                    with self.assertRaises(ValidationError) as caught:
                        RUNTIME_CLI._handle_skill_toggle(args, {}, dry_run=False)

                    self.assertEqual(caught.exception.code, OVERRIDE_REFUSED_FLOOR)
                    self.assertEqual(
                        (repo / ".skillbox" / "skill-overrides.yaml").read_bytes(),
                        before,
                    )

    def test_skill_off_cli_refuses_dispatcher_floor_with_nonzero_exit_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir))
            _write_override(repo, "version: 1\npin_on: [alpha]\n")
            before = (repo / ".skillbox" / "skill-overrides.yaml").read_bytes()

            with mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value={}):
                code, text = _run_runtime_cli([
                    "skill",
                    "off",
                    "sbp",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

            payload = json.loads(text)
            after = (repo / ".skillbox" / "skill-overrides.yaml").read_bytes()

            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error_code"], OVERRIDE_REFUSED_FLOOR)
            self.assertEqual(payload["error"]["code"], OVERRIDE_REFUSED_FLOOR)
            self.assertEqual(after, before)

    def test_skill_on_global_refuses_non_global_skill_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "on", "alpha", to="global")

            with self.assertRaises(ValidationError) as caught:
                RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            self.assertEqual(caught.exception.code, OVERRIDE_REFUSED_GLOBAL_ESCALATION)
            self.assertEqual(caught.exception.context["scope_rule"], "local-default-off")
            self.assertEqual(caught.exception.context["scope_rule_allow_global"], False)
            self.assertEqual(caught.exception.context["allowed_global_patterns"], ["sbp", "smart"])
            self.assertFalse((repo / ".skillbox" / "skill-overrides.yaml").exists())

    def test_skill_on_local_default_off_skill_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)
            args = _skill_toggle_args(repo, "on", "alpha")

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                payload = RUNTIME_CLI._handle_skill_toggle(args, model, dry_run=False)

            policy = _repo_override_policy(repo)
            self.assertEqual(policy["pin_on"], ["alpha"])
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["summary"]["link"], 2)

    def test_skill_heal_pins_links_and_returns_same_packet_on_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "alpha")
            expected_sha = hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()
            model = _write_global_floor_policy(root, source_root, "alpha", repo)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                first_code, first_text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])
                second_code, second_text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

            first = json.loads(first_text)
            second = json.loads(second_text)
            policy = _repo_override_policy(repo)
            claude_linked = (repo / ".claude" / "skills" / "alpha").is_symlink()
            codex_linked = (repo / ".codex" / "skills" / "alpha").is_symlink()

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(policy["pin_on"], ["alpha"])
        self.assertTrue(policy["reason"].startswith("heal:alpha "))
        self.assertTrue(claude_linked)
        self.assertTrue(codex_linked)
        self.assertEqual(first["action"], "heal")
        self.assertTrue(first["changed"])
        self.assertEqual(first["summary"]["link"], 2)
        self.assertEqual(first["activation_packet"]["skill_md_sha256"], expected_sha)
        self.assertEqual(second["action"], "heal")
        self.assertFalse(second["changed"])
        self.assertTrue(second["noop"])
        self.assertEqual(second["actions"], [])
        self.assertEqual(second["activation_packet"], first["activation_packet"])

    def test_skill_heal_unknown_refuses_without_writing_dangling_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                code, text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "alpah",
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

            payload = json.loads(text)

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], OVERRIDE_SKILL_UNKNOWN)
        self.assertIn("alpha", payload["error"]["context"]["suggestions"])
        self.assertFalse((repo / ".skillbox" / "skill-overrides.yaml").exists())

    def test_skill_heal_explicit_source_mismatch_refuses_without_writing_dangling_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            source = _write_source_skill(source_root, "sbp")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                code, text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "madeup-skill-name",
                    "--source",
                    str(source),
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

            payload = json.loads(text)
            override_exists = (repo / ".skillbox" / "skill-overrides.yaml").exists()
            claude_link_exists = (repo / ".claude" / "skills" / "madeup-skill-name").exists()
            codex_link_exists = (repo / ".codex" / "skills" / "madeup-skill-name").exists()

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], OVERRIDE_SKILL_UNKNOWN)
        self.assertIn("sbp", payload["error"]["context"]["suggestions"])
        self.assertFalse(override_exists)
        self.assertFalse(claude_link_exists)
        self.assertFalse(codex_link_exists)

    def test_skill_heal_missing_explicit_source_reports_unknown_without_writing_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                code, text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "alpha",
                    "--source",
                    str(root / "missing"),
                    "--cwd",
                    str(repo),
                    "--format",
                    "json",
                ])

            payload = json.loads(text)
            override_exists = (repo / ".skillbox" / "skill-overrides.yaml").exists()

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], OVERRIDE_SKILL_UNKNOWN)
        self.assertFalse(override_exists)

    def test_skill_heal_verify_fails_when_any_surface_target_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            fake_home = root / "home"
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)
            conflict = repo / ".claude" / "skills" / "alpha"
            conflict.mkdir(parents=True)

            with (
                mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home),
                mock.patch.object(RUNTIME_CLI, "_filtered_model_for_args", return_value=model),
            ):
                code, text = _run_runtime_cli([
                    "skill",
                    "heal",
                    "alpha",
                    "--cwd",
                    str(repo),
                    "--verify",
                    "--format",
                    "json",
                ])

            payload = json.loads(text)
            policy = _repo_override_policy(repo)

        self.assertEqual(code, 0)
        self.assertEqual(policy["pin_on"], ["alpha"])
        self.assertFalse(payload["verification"]["verified"])
        statuses = {action["destination"]: action["status"] for action in payload["actions"]}
        self.assertEqual(statuses[str(conflict)], "conflict_directory")
        expected_targets = {
            row["path"]: row
            for row in payload["verification"]["expected_targets"]
        }
        self.assertFalse(expected_targets[str(conflict)]["ok"])
        self.assertFalse(expected_targets[str(conflict)]["is_symlink"])

    def test_skill_on_parser_accepts_global_alias(self) -> None:
        parser = RUNTIME_CLI._build_parser()
        args = parser.parse_args(["skill", "on", "alpha", "--global"])
        self.assertEqual(args.to, "global")

    def test_global_override_refusal_context_uses_allow_global_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            source_root = root / "sources"
            _write_source_skill(source_root, "alpha")
            model = _write_global_floor_policy(root, source_root, "alpha", repo)

            alpha_context = _global_override_refusal_context(model, "alpha")
            self.assertEqual(alpha_context["scope_rule"], "local-default-off")
            self.assertEqual(alpha_context["scope_policy_path"], str(root / "skill-scope.yaml"))
            self.assertEqual(alpha_context["allowed_global_patterns"], ["sbp", "smart"])
            self.assertIsNone(_global_override_refusal_context(model, "sbp"))

    def test_overlay_precedence_cli_env_repo_operator_base_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = _make_repo(root)
            state_path = root / "overlays.txt"
            state_path.write_text("marketing\n", encoding="utf-8")
            _write_override(
                repo,
                "version: 1\n"
                "overlays:\n"
                "  disable: [marketing]\n",
            )
            model = {
                "active_clients": ["personal"],
                "clients": [
                    {
                        "id": "personal",
                        "context": {
                            "skill_scope": {
                                "overlays": [{"name": "marketing"}],
                                "rules": [
                                    {
                                        "id": "marketing-rule",
                                        "skills": ["promo"],
                                        "overlay": "marketing",
                                        "paths": [str(repo)],
                                    }
                                ],
                            }
                        },
                    }
                ],
                "skills": [],
            }

            base_env = {
                "SKILLBOX_OVERLAY_STATE": str(state_path),
                "SKILLBOX_OVERLAYS": "",
                "SKILLBOX_CLI_OVERLAYS": "",
            }
            with mock.patch.dict(SKILL_VISIBILITY.os.environ, base_env, clear=False):
                self.assertNotIn("marketing", active_overlays(repo))
                disabled = collect_skill_visibility(
                    model,
                    cwd=str(repo),
                    include_global=False,
                    include_project=False,
                )
                self.assertEqual(disabled["matched_scope_rules"], [])

            env_on = dict(base_env)
            env_on["SKILLBOX_OVERLAYS"] = "marketing"
            with mock.patch.dict(SKILL_VISIBILITY.os.environ, env_on, clear=False):
                self.assertIn("marketing", active_overlays(repo))
                enabled = collect_skill_visibility(
                    model,
                    cwd=str(repo),
                    include_global=False,
                    include_project=False,
                )
                self.assertEqual(enabled["matched_scope_rules"][0]["id"], "marketing-rule")

            cli_off = dict(env_on)
            cli_off["SKILLBOX_CLI_OVERLAYS"] = "!marketing"
            with mock.patch.dict(SKILL_VISIBILITY.os.environ, cli_off, clear=False):
                self.assertNotIn("marketing", active_overlays(repo))


if __name__ == "__main__":
    unittest.main()
