from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.skill_visibility import (  # noqa: E402
    activate_overlay_scoped_skills,
    active_overlays,
    apply_skill_lifecycle_plan,
    attach_skill_evidence,
    _apply_lifecycle_unlink,
    _declared_skill_occurrences,
    _effective_occurrences,
    _install_path_state,
    _plan_skill_prune_actions,
    _plan_skill_removals,
    _prepare_lifecycle_link_destination,
    _project_categories_for_policy,
    _project_skill_roots,
    _scan_installed_root,
    _scope_filter_matches,
    _skill_destination_bases,
    _skill_repo_declared_names,
    _sync_wanted_skill_names,
    _target_states_for_skill,
    collect_skill_audit,
    collect_skill_visibility,
    compact_skill_visibility_payload,
    matched_skill_clients,
    print_skill_audit_text,
    print_skill_lifecycle_text,
    print_skill_visibility_text,
    skill_lifecycle_plan,
    set_overlay,
    toggle_overlay,
    unlink_overlay_scoped_skills,
)
from runtime_manager.errors import PRUNE_SKIPPED_PINNED  # noqa: E402
from runtime_manager.shared import directory_tree_sha256  # noqa: E402


class SkillVisibilityTests(unittest.TestCase):
    def test_overlay_state_file_merges_env_comments_and_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "overlays.txt"
            state_path.write_text("# comment\nexisting\n\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    "runtime_manager.skill_visibility.os.environ",
                    {
                        "SKILLBOX_OVERLAY_STATE": str(state_path),
                        "SKILLBOX_OVERLAYS": "ephemeral, existing",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(active_overlays(), {"existing", "ephemeral"})
                self.assertTrue(set_overlay("new", True))
                self.assertEqual(state_path.read_text(encoding="utf-8"), "existing\nnew\n")
                self.assertFalse(set_overlay("existing", False))
                self.assertEqual(state_path.read_text(encoding="utf-8"), "new\n")
                self.assertTrue(toggle_overlay("brand-new"))
                self.assertFalse(toggle_overlay("brand-new"))

    def test_lifecycle_link_destination_preparation_handles_conflicts_and_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            source = root / "source"
            source.mkdir()
            same_link = root / "same"
            same_link.symlink_to(source, target_is_directory=True)
            action: dict[str, str] = {}
            self.assertFalse(
                _prepare_lifecycle_link_destination(
                    action,
                    same_link,
                    source,
                    allow_directories=False,
                    force=False,
                )
            )
            self.assertEqual(action["status"], "ok")

            file_destination = root / "file"
            file_destination.write_text("content\n", encoding="utf-8")
            action = {}
            self.assertFalse(
                _prepare_lifecycle_link_destination(
                    action,
                    file_destination,
                    source,
                    allow_directories=False,
                    force=False,
                )
            )
            self.assertEqual(action["status"], "conflict_file")
            self.assertTrue(file_destination.is_file())

            action = {}
            self.assertTrue(
                _prepare_lifecycle_link_destination(
                    action,
                    file_destination,
                    source,
                    allow_directories=False,
                    force=True,
                )
            )
            self.assertFalse(file_destination.exists())

            directory_destination = root / "directory"
            directory_destination.mkdir()
            action = {}
            self.assertFalse(
                _prepare_lifecycle_link_destination(
                    action,
                    directory_destination,
                    source,
                    allow_directories=False,
                    force=True,
                )
            )
            self.assertEqual(action["status"], "conflict_directory")

            self.assertTrue(
                _prepare_lifecycle_link_destination(
                    {},
                    directory_destination,
                    source,
                    allow_directories=True,
                    force=True,
                )
            )
            self.assertFalse(directory_destination.exists())

    def test_lifecycle_unlink_and_install_path_state_cover_files_dirs_links_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            source.mkdir()
            same_link = root / "same-link"
            same_link.symlink_to(source, target_is_directory=True)
            other_source = root / "other-source"
            other_source.mkdir()
            different_link = root / "different-link"
            different_link.symlink_to(other_source, target_is_directory=True)
            file_path = root / "file"
            file_path.write_text("content\n", encoding="utf-8")
            directory_path = root / "directory"
            directory_path.mkdir()
            (directory_path / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(_install_path_state(root / "missing"), {"state": "missing"})
            self.assertEqual(_install_path_state(same_link, str(source))["state"], "same_link")
            self.assertEqual(_install_path_state(different_link, str(source))["state"], "different_link")
            self.assertEqual(_install_path_state(file_path)["state"], "file")
            self.assertEqual(_install_path_state(directory_path)["state"], "directory")
            self.assertTrue(_install_path_state(directory_path)["has_skill_md"])

            action: dict[str, str] = {}
            _apply_lifecycle_unlink(action, root / "missing", dry_run=True, allow_directories=False)
            self.assertEqual(action["status"], "missing")

            action = {}
            _apply_lifecycle_unlink(action, file_path, dry_run=True, allow_directories=False)
            self.assertEqual(action["status"], "would_unlink")

            action = {}
            _apply_lifecycle_unlink(action, file_path, dry_run=False, allow_directories=False)
            self.assertEqual(action["status"], "unlinked")
            self.assertFalse(file_path.exists())

            action = {}
            _apply_lifecycle_unlink(action, directory_path, dry_run=False, allow_directories=False)
            self.assertEqual(action["status"], "skipped_directory")
            self.assertTrue(directory_path.exists())

            action = {}
            _apply_lifecycle_unlink(action, directory_path, dry_run=False, allow_directories=True)
            self.assertEqual(action["status"], "removed_directory")
            self.assertFalse(directory_path.exists())

            pinned_path = root / "pinned-skill"
            pinned_path.mkdir()
            action = {"pinned": True}
            _apply_lifecycle_unlink(action, pinned_path, dry_run=False, allow_directories=True)
            self.assertEqual(action["status"], "skipped_pinned")
            self.assertEqual(action["code"], PRUNE_SKIPPED_PINNED)
            self.assertTrue(pinned_path.exists())

    def test_lifecycle_prune_sync_and_target_state_helpers_classify_visibility(self) -> None:
        visibility = {
            "issues": {
                "scope_violations": [
                    {"name": "alpha", "path": "/skills/alpha", "layer": "global:claude"},
                    {"name": "beta", "path": "/skills/beta", "layer": "global:codex"},
                ],
                "global_not_allowed": [{"name": "alpha", "path": "/global/alpha", "layer": "global:claude"}],
                "extra_global": [{"name": "alpha"}],
                "broken_global": [{"name": "alpha", "path": "/broken/alpha", "layer": "global:codex"}],
                "broken_project": [{"name": "gamma", "path": "/project/gamma", "layer": "project:claude"}],
                "missing_for_cwd": [{"name": "alpha"}, {"name": ""}, {}],
            }
        }

        alpha_actions = _plan_skill_prune_actions(visibility, "alpha")
        all_actions = _plan_skill_prune_actions(visibility, None)
        project_actions = _plan_skill_prune_actions(visibility, None, from_scope="project")

        self.assertEqual([action["skill"] for action in alpha_actions], ["alpha", "alpha", "alpha"])
        self.assertEqual([action["reason"] for action in alpha_actions], [
            "scope_violations",
            "global_not_allowed",
            "broken_global",
        ])
        self.assertEqual(len(all_actions), 5)
        self.assertEqual([action["skill"] for action in project_actions], ["gamma"])
        self.assertEqual(_sync_wanted_skill_names(visibility, "explicit"), ["explicit"])
        self.assertEqual(_sync_wanted_skill_names(visibility, None), ["alpha"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_root = root / "target"
            ok_dir = target_root / "ok-skill"
            stale_dir = target_root / "stale-skill"
            present_dir = target_root / "present-skill"
            ok_dir.mkdir(parents=True)
            stale_dir.mkdir()
            present_dir.mkdir()
            (ok_dir / "SKILL.md").write_text("# OK\n", encoding="utf-8")
            (stale_dir / "SKILL.md").write_text("# Stale\n", encoding="utf-8")
            (present_dir / "SKILL.md").write_text("# Present\n", encoding="utf-8")
            skillset = {"install_targets": [{"id": "codex", "host_path": str(target_root)}]}

            self.assertEqual(
                _target_states_for_skill(skillset, "missing-skill", {"install_tree_sha": "sha"})[0]["state"],
                "missing",
            )
            self.assertEqual(
                _target_states_for_skill(
                    skillset,
                    "ok-skill",
                    {"install_tree_sha": directory_tree_sha256(ok_dir)},
                )[0]["state"],
                "ok",
            )
            self.assertEqual(
                _target_states_for_skill(skillset, "stale-skill", {"install_tree_sha": "wrong"})[0]["state"],
                "stale",
            )
            self.assertEqual(
                _target_states_for_skill(skillset, "present-skill", {})[0]["state"],
                "present",
            )

    def test_scope_filters_removal_plans_and_compact_payload_helpers(self) -> None:
        self.assertTrue(_scope_filter_matches({"layer": "global:codex"}, "all"))
        self.assertTrue(_scope_filter_matches({"layer": "project:claude"}, "all"))
        self.assertFalse(_scope_filter_matches({"layer": "client:personal"}, "all"))
        self.assertTrue(_scope_filter_matches({"layer": "global:claude"}, "global"))
        self.assertFalse(_scope_filter_matches({"layer": "project:claude"}, "global"))
        self.assertTrue(_scope_filter_matches({"layer": "project:codex"}, "project"))
        self.assertFalse(_scope_filter_matches({"layer": "global:codex"}, "project"))
        self.assertTrue(_scope_filter_matches({"layer": "project:codex"}, "unknown"))

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            global_path = root / "global"
            project_path = root / "project"
            same_path = root / "same"
            for path in (global_path, project_path, same_path):
                path.mkdir()
            occurrences = [
                {"name": "alpha", "layer": "global:codex", "path": str(global_path), "source": "/src/alpha"},
                {"name": "alpha", "layer": "project:claude", "path": str(project_path), "source": "/src/alpha"},
                {"name": "alpha", "layer": "global:claude", "path": str(same_path), "source": "/src/alpha"},
                {"name": "alpha", "layer": "client:personal", "path": str(root / "client"), "source": "/src/alpha"},
            ]
            with mock.patch(
                "runtime_manager.skill_visibility._installed_occurrences_for_skill",
                return_value=occurrences,
            ):
                self.assertEqual(
                    _plan_skill_removals({}, "sync", "alpha", root, "all", []),
                    [],
                )
                remove_actions = _plan_skill_removals({}, "remove", "alpha", root, "project", [])
                move_actions = _plan_skill_removals(
                    {},
                    "move",
                    "alpha",
                    root,
                    "all",
                    [{"op": "link", "destination": str(same_path)}],
                )

        self.assertEqual([action["destination"] for action in remove_actions], [str(project_path)])
        self.assertEqual([action["reason"] for action in move_actions], ["move source cleanup", "move source cleanup"])
        self.assertNotIn(str(same_path), [action["destination"] for action in move_actions])

        compact = compact_skill_visibility_payload(
            {
                "cwd": "/repo",
                "active_clients": ["personal"],
                "active_profiles": ["core"],
                "matched_clients": [{"id": "personal"}],
                "matched_project_categories": [{"id": "frontend"}],
                "matched_scope_rules": [{"id": "frontend-rule"}],
                "summary": {"effective": 1},
                "effective": [
                    {
                        "name": "ui",
                        "layer": "project",
                        "state": "ok",
                        "source_bucket": "repo",
                        "source": "/repo/skills/ui",
                        "path": "/repo/.codex/skills/ui",
                        "unused": "drop",
                    }
                ],
                "issues": {"missing_for_cwd": [{"name": "seo"}], "extra": [{"name": "drop"}]},
                "recommendations": [{"action": "add_project_skill"}],
                "policy": {"global_allowlist": []},
                "source_roots": ["/repo/skills"],
                "undefined_sources": [{"name": "old"}],
                "next_actions": ["sync"],
            }
        )

        self.assertEqual(compact["effective"][0]["name"], "ui")
        self.assertNotIn("unused", compact["effective"][0])
        self.assertEqual(compact["issues"]["missing_for_cwd"], [{"name": "seo"}])
        self.assertNotIn("extra", compact["issues"])
        self.assertEqual(compact["source_roots"], ["/repo/skills"])

    def test_print_skill_visibility_text_renders_summary_layers_issues_and_limits(self) -> None:
        payload = {
            "cwd": "/repo/app",
            "active_clients": ["personal"],
            "active_profiles": ["core", "dev"],
            "matched_clients": [{"id": "personal", "match": "cwd"}],
            "matched_project_categories": [{"id": "frontend"}],
            "summary": {
                "effective": 3,
                "occurrences": 5,
                "undefined_sources": 2,
                "broken_global": 1,
                "broken_global_skills": 1,
                "broken_project": 1,
                "broken_project_skills": 1,
                "global_not_allowed": 1,
                "global_not_allowed_skills": 1,
                "extra_global": 2,
                "extra_global_skills": 1,
                "shadowed": 1,
                "archive_sources": 1,
                "archive_source_skills": 1,
                "scope_violations": 1,
                "scope_violation_skills": 1,
                "missing_for_cwd": 2,
                "missing_for_cwd_skills": 2,
            },
            "layers": [
                {
                    "id": "default",
                    "kind": "declared",
                    "skill_count": 2,
                    "healthy_targets": 1,
                    "target_count": 2,
                    "config_error": "bad config",
                    "lock_error": "bad lock",
                },
                {
                    "id": "global",
                    "kind": "installed",
                    "skill_count": 3,
                    "present": False,
                    "broken_count": 1,
                },
            ],
            "effective": [
                {
                    "name": "domain-planner",
                    "layer": "client:personal",
                    "state": "ok",
                    "source_bucket": "repo",
                    "shadowed_count": 1,
                },
                {
                    "name": "ui",
                    "layer": "project",
                    "availability": "declared",
                    "source_bucket": "project",
                },
            ],
            "issues": {
                "shadowed": [
                    {
                        "name": "domain-planner",
                        "winner_layer": "client:personal",
                        "shadowed_layers": ["default"],
                    }
                ],
                "scope_violations": [
                    {
                        "name": "ui",
                        "layer": "global",
                        "path": "/skills/ui",
                        "scope_rule": "frontend",
                        "allowed_paths": ["/repo/.claude/skills"],
                    }
                ],
                "global_not_allowed": [
                    {"name": "legacy", "layer": "global", "path": "/skills/legacy"}
                ],
                "missing_for_cwd": [
                    {
                        "name": "ga4",
                        "scope_rule": "frontend",
                        "categories": ["frontend"],
                        "allowed_paths": ["/repo/.claude/skills"],
                    },
                    {
                        "name": "seo",
                        "scope_rule": "frontend",
                        "categories": ["frontend"],
                        "allowed_paths": [],
                    },
                ],
            },
            "undefined_sources": [
                {"name": "unused", "source_bucket": "repo", "source": "/sources/unused"},
                {"name": "stale", "source_bucket": "archive", "source": "/archive/stale"},
            ],
            "source_roots": ["/sources", "/archive"],
            "next_actions": ["doctor --format json"],
            "recommendations": [
                {"action": "add_project_skill", "skill": "ga4", "hint": "frontend"},
                {"action": "prune_global", "skill": "legacy", "hint": "allowlist"},
            ],
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_skill_visibility_text(payload, show_shadowed=True, limit=1)
        output = buffer.getvalue()

        self.assertIn("skills: 3 effective, 5 occurrences, 2 undefined/not synced", output)
        self.assertIn("active: clients=personal profiles=core, dev", output)
        self.assertIn("pwd match: personal@cwd", output)
        self.assertIn("project categories: frontend", output)
        self.assertIn("  - default: 2 skills, 1/2 targets healthy, config error, lock error", output)
        self.assertIn("  - global: 3 skills, missing, 1 broken", output)
        self.assertIn("  - broken_global: 1 links / 1 skills", output)
        self.assertIn("  - scope_violations: 1 installs / 1 skills", output)
        self.assertIn("  - missing_for_cwd: 2 rules / 2 skills", output)
        self.assertIn("  - domain-planner: client:personal ok repo shadows=1", output)
        self.assertIn("  ... 1 more (rerun with --full)", output)
        self.assertIn("shadowed:\n  - domain-planner: winner=client:personal hidden=default", output)
        self.assertIn("scope_violations:\n  - ui: global at /skills/ui", output)
        self.assertIn("global_not_allowed:\n  - legacy: global at /skills/legacy", output)
        self.assertIn("missing_for_cwd:\n  - ga4: rule=frontend categories=frontend", output)
        self.assertIn("  ... 1 more missing cwd-scoped skills", output)
        self.assertIn("undefined / not synced (2 from 2 source roots):", output)
        self.assertIn("  - unused: repo /sources/unused", output)
        self.assertIn("  ... 1 more undefined source skills (rerun with --full)", output)
        self.assertIn("next_actions:\n  - doctor --format json", output)
        self.assertIn("recommendations:\n  - add_project_skill: ga4 (frontend)", output)
        self.assertIn("  ... 1 more recommendations", output)

    def test_print_skill_visibility_text_issues_only_omits_layers_and_effective(self) -> None:
        payload = {
            "cwd": "/repo",
            "summary": {"missing_for_cwd": 1, "missing_for_cwd_skills": 1},
            "issues": {
                "missing_for_cwd": [
                    {
                        "name": "ui",
                        "scope_rule": "frontend",
                        "categories": [],
                        "allowed_paths": [],
                    }
                ]
            },
        }

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_skill_visibility_text(payload, issues_only=True)
        output = buffer.getvalue()

        self.assertIn("skills: 0 effective, 0 occurrences", output)
        self.assertIn("issues:", output)
        self.assertIn("missing_for_cwd:\n  - ui: rule=frontend categories=(none) allowed=(none)", output)
        self.assertNotIn("layers:", output)
        self.assertNotIn("effective:", output)

    def test_print_skill_lifecycle_text_renders_noop_actions_and_activation_packet(self) -> None:
        no_actions = {
            "action": "sync",
            "skill": None,
            "dry_run": True,
            "cwd": "/repo",
            "resolved_to": "project",
            "actions": [],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_skill_lifecycle_text(no_actions)
        self.assertIn("actions: none", buffer.getvalue())

        payload = {
            "action": "activate",
            "skill": "domain-planner",
            "dry_run": False,
            "cwd": "/repo",
            "resolved_to": "client",
            "selected_source": {"source": "/sources/domain-planner"},
            "warnings": ["already linked elsewhere"],
            "actions": [
                {
                    "status": "linked",
                    "op": "link",
                    "skill": "domain-planner",
                    "destination": "/repo/.codex/skills/domain-planner",
                }
            ],
            "activation_packet": {
                "name": "domain-planner",
                "source": "/sources/domain-planner",
                "skill_md_sha256": "a" * 64,
                "skill_md": "# Domain Planner\n",
            },
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            print_skill_lifecycle_text(payload)
        output = buffer.getvalue()

        self.assertIn("skill activate: domain-planner (apply)", output)
        self.assertIn("source: /sources/domain-planner", output)
        self.assertIn("warning: already linked elsewhere", output)
        self.assertIn("  - linked: link domain-planner -> /repo/.codex/skills/domain-planner", output)
        self.assertIn("activation packet:", output)
        self.assertIn("skill_md_sha256: " + "a" * 64, output)
        self.assertIn("# Domain Planner", output)

    def test_installed_root_reports_broken_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skills"
            root.mkdir()
            good = root / "good"
            good.mkdir()
            (good / "SKILL.md").write_text("# Good\n", encoding="utf-8")
            (root / "broken").symlink_to(Path(tmpdir) / "missing")

            occurrences, summary = _scan_installed_root(
                root,
                layer="global:claude",
                label="global claude",
                rank=10,
            )

            by_name = {item["name"]: item for item in occurrences}
            self.assertEqual(summary["skill_count"], 2)
            self.assertEqual(summary["broken_count"], 1)
            self.assertEqual(by_name["good"]["state"], "ok")
            self.assertEqual(by_name["broken"]["state"], "broken")

    def test_collects_effective_highest_layer_and_shadowed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            clients_root = workspace / "clients" / "personal"
            workspace.mkdir(parents=True)
            clients_root.mkdir(parents=True)

            default_config = workspace / "skill-repos.yaml"
            default_config.write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./default-skills\n"
                "    pick: [shared, default-only]\n",
                encoding="utf-8",
            )
            client_config = clients_root / "skill-repos.yaml"
            client_config.write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [shared, client-only]\n",
                encoding="utf-8",
            )
            (workspace / "skill-repos.lock.json").write_text(
                json.dumps({
                    "version": 2,
                    "config_sha": "x",
                    "synced_at": "now",
                    "skills": [
                        {"name": "shared", "source_path": "./default-skills"},
                        {"name": "default-only", "source_path": "./default-skills"},
                    ],
                }),
                encoding="utf-8",
            )
            (clients_root / "skill-repos.lock.json").write_text(
                json.dumps({
                    "version": 2,
                    "config_sha": "x",
                    "synced_at": "now",
                    "skills": [
                        {"name": "shared", "source_path": "./skills"},
                        {"name": "client-only", "source_path": "./skills"},
                    ],
                }),
                encoding="utf-8",
            )

            model = {
                "active_clients": ["personal"],
                "active_profiles": ["core"],
                "clients": [
                    {
                        "id": "personal",
                        "label": "Personal",
                        "context": {"cwd_match": [str(root / "project")]},
                    }
                ],
                "skills": [
                    {
                        "id": "default-skills",
                        "kind": "skill-repo-set",
                        "skill_repos_config_host_path": str(default_config),
                        "lock_path_host_path": str(workspace / "skill-repos.lock.json"),
                        "install_targets": [],
                    },
                    {
                        "id": "personal-skills",
                        "kind": "skill-repo-set",
                        "skill_repos_config_host_path": str(client_config),
                        "lock_path_host_path": str(clients_root / "skill-repos.lock.json"),
                        "install_targets": [],
                    },
                ],
            }

            payload = collect_skill_visibility(
                model,
                cwd=str(root / "project" / "app"),
                include_global=False,
                include_project=False,
            )
            effective = {item["name"]: item for item in payload["effective"]}

            self.assertEqual(payload["matched_clients"][0]["id"], "personal")
            self.assertEqual(effective["shared"]["layer"], "client:personal")
            self.assertEqual(effective["shared"]["shadowed_count"], 1)
            self.assertEqual(effective["default-only"]["layer"], "default")
            self.assertEqual(effective["client-only"]["layer"], "client:personal")
            self.assertEqual(payload["summary"]["shadowed"], 1)

    def test_installed_global_beats_declared_defaults(self) -> None:
        effective, shadowed = _effective_occurrences([
            {
                "name": "domain-planner",
                "availability": "declared",
                "layer": "default",
                "layer_rank": 10,
                "state": "declared",
                "source": "/tmp/default/domain-planner",
            },
            {
                "name": "domain-planner",
                "availability": "installed",
                "layer": "global:claude",
                "layer_rank": 30,
                "state": "ok",
                "source": "/tmp/global/domain-planner",
            },
        ])

        self.assertEqual(effective[0]["layer"], "global:claude")
        self.assertEqual(effective[0]["shadowed_count"], 0)
        self.assertEqual(shadowed, [])

    def test_mirrored_surfaces_same_source_are_not_shadow_conflicts(self) -> None:
        effective, shadowed = _effective_occurrences([
            {
                "name": "ntm",
                "availability": "installed",
                "layer": "global:codex",
                "layer_rank": 30,
                "state": "ok",
                "source": "/tmp/skills/ntm",
            },
            {
                "name": "ntm",
                "availability": "installed",
                "layer": "global:claude",
                "layer_rank": 30,
                "state": "ok",
                "source": "/tmp/skills/ntm",
            },
            {
                "name": "ntm",
                "availability": "declared",
                "layer": "default",
                "layer_rank": 10,
                "state": "declared",
                "source": "/tmp/skills/ntm",
            },
        ])

        self.assertEqual(effective[0]["layer"], "global:claude")
        self.assertEqual(effective[0]["shadowed_count"], 0)
        self.assertEqual(shadowed, [])

    def test_collects_undefined_source_skills_from_policy_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            declared = source_root / "declared"
            unused = source_root / "unused"
            installed_elsewhere = source_root / "installed-elsewhere"
            install_root = root / "other-repo" / ".claude" / "skills"
            clients_root.mkdir()
            declared.mkdir(parents=True)
            unused.mkdir(parents=True)
            installed_elsewhere.mkdir(parents=True)
            install_root.mkdir(parents=True)
            (declared / "SKILL.md").write_text("# Declared\n", encoding="utf-8")
            (unused / "SKILL.md").write_text("# Unused\n", encoding="utf-8")
            (installed_elsewhere / "SKILL.md").write_text("# Installed elsewhere\n", encoding="utf-8")
            (install_root / "installed-elsewhere").symlink_to(installed_elsewhere)
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: []\n"
                f"skill_source_roots: [{source_root}]\n"
                f"skill_install_scan_roots: [{root}]\n",
                encoding="utf-8",
            )
            config = root / "skill-repos.yaml"
            config.write_text(
                "version: 2\n"
                "skill_repos:\n"
                f"  - path: {source_root}\n"
                "    pick: [declared]\n",
                encoding="utf-8",
            )

            payload = collect_skill_visibility(
                {
                    "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                    "clients": [],
                    "skills": [
                        {
                            "id": "default-skills",
                            "kind": "skill-repo-set",
                            "skill_repos_config_host_path": str(config),
                            "lock_path_host_path": str(root / "missing-lock.json"),
                            "install_targets": [],
                        }
                    ],
                },
                cwd=str(root),
                include_global=False,
                include_project=False,
                include_sources=True,
            )

            undefined = {
                item["name"]: item for item in payload["undefined_sources"]
                if item["source"].startswith(str(source_root.resolve()))
            }
            self.assertNotIn("declared", undefined)
            self.assertNotIn("installed-elsewhere", undefined)
            self.assertIn("unused", undefined)

    def test_source_scan_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            unused = source_root / "unused"
            clients_root.mkdir()
            unused.mkdir(parents=True)
            (unused / "SKILL.md").write_text("# Unused\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                f"skill_source_roots: [{source_root}]\n",
                encoding="utf-8",
            )

            payload = collect_skill_visibility(
                {
                    "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                    "clients": [],
                    "skills": [],
                },
                cwd=str(root),
                include_global=False,
                include_project=False,
            )

            self.assertEqual(payload["undefined_sources"], [])
            self.assertEqual(payload["summary"]["undefined_sources"], 0)

    def test_project_categories_expand_scope_rules_and_report_missing_for_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            project = root / "repos" / "web-app"
            clients_root.mkdir()
            project.mkdir(parents=True)
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "project_categories:\n"
                "  frontend:\n"
                f"    paths: [{project}]\n"
                "rules:\n"
                "  - id: frontend-local\n"
                "    skills: [ui, ga4]\n"
                "    categories: [frontend]\n",
                encoding="utf-8",
            )

            payload = collect_skill_visibility(
                {
                    "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                    "clients": [],
                    "skills": [],
                },
                cwd=str(project / "src"),
                include_global=False,
                include_project=False,
            )

            self.assertEqual(payload["matched_project_categories"][0]["id"], "frontend")
            self.assertEqual(payload["matched_scope_rules"][0]["id"], "frontend-local")
            missing = {item["name"]: item for item in payload["issues"]["missing_for_cwd"]}
            self.assertEqual(set(missing), {"ui", "ga4"})
            self.assertEqual(missing["ui"]["categories"], ["frontend"])
            actions = {item["action"] for item in payload["recommendations"]}
            self.assertIn("add_project_skill", actions)

    def test_collect_skill_audit_scans_configured_repos_and_global_drift_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            frontend = root / "repos" / "web-app"
            cli_repo = root / "repos" / "cli-tool"
            source_root = root / "source-skills"
            global_root = root / "home" / ".claude" / "skills"
            for path in (clients_root, frontend, cli_repo, source_root / "ui", source_root / "cli-ergonomics", global_root):
                path.mkdir(parents=True)
            for skill in ("ui", "cli-ergonomics", "extra-global"):
                skill_dir = source_root / skill
                skill_dir.mkdir(exist_ok=True)
                (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (global_root / "extra-global").symlink_to(source_root / "extra-global", target_is_directory=True)
            (frontend / ".git").mkdir()
            (cli_repo / ".git").mkdir()
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [always-global]\n"
                f"skill_source_roots: [{source_root}]\n"
                f"skill_install_scan_roots: [{root / 'repos'}]\n"
                "project_categories:\n"
                "  frontend:\n"
                f"    paths: [{frontend}]\n"
                "  cli:\n"
                f"    paths: [{cli_repo}]\n"
                "rules:\n"
                "  - id: frontend-local\n"
                "    skills: [ui]\n"
                "    categories: [frontend]\n"
                "  - id: cli-local\n"
                "    skills: [cli-ergonomics]\n"
                "    categories: [cli]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "active_clients": [],
                "active_profiles": ["core"],
                "clients": [],
                "skills": [],
            }

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=root / "home"):
                payload = collect_skill_audit(model, cwd=str(frontend), include_clean=False)

            self.assertEqual(payload["summary"]["candidate_repos"], 2)
            self.assertEqual(payload["summary"]["repos_with_issues"], 2)
            self.assertEqual(payload["summary"]["missing_for_cwd"], 2)
            self.assertEqual(payload["summary"]["global_not_allowed"], 1)
            by_path = {repo["path"]: repo for repo in payload["repos"]}
            self.assertEqual(by_path[str(frontend.resolve())]["missing_for_cwd"], ["ui"])
            self.assertEqual(by_path[str(cli_repo.resolve())]["missing_for_cwd"], ["cli-ergonomics"])
            self.assertIn("manage.py skill sync --cwd", payload["next_actions"][0])

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_skill_audit_text(payload, limit=10)
            output = buffer.getvalue()
            self.assertIn("skill audit: 2 candidate repos", output)
            self.assertIn("global: broken=0 not_allowed=1 extra=1", output)
            self.assertIn("missing=ui", output)

    def test_project_category_and_declared_name_helpers_cover_policy_shapes(self) -> None:
        policy = {
            "_policy_path": "/policy.yaml",
            "project_categories": {
                "frontend": {"paths": ["~/frontend"], "notes": "UI"},
                "backend": ["~/backend"],
                "": {"paths": ["ignored"]},
            },
        }
        categories = _project_categories_for_policy(policy)
        self.assertEqual([category["id"] for category in categories], ["frontend", "backend"])
        self.assertEqual(categories[0]["notes"], "UI")
        self.assertEqual(categories[1]["notes"], "")
        self.assertEqual(categories[0]["policy_path"], "/policy.yaml")

        list_policy = {
            "project_categories": [
                {"id": "mobile", "allowed_paths": ["~/ios"], "description": "apps"},
                {"name": "docs", "paths": ["~/docs"]},
                "ignored",
            ],
        }
        self.assertEqual(
            [(category["id"], category["notes"]) for category in _project_categories_for_policy(list_policy)],
            [("mobile", "apps"), ("docs", "")],
        )
        self.assertEqual(_skill_repo_declared_names({"pick": ["alpha", "", "beta"]}, "path", "/repo"), ["alpha", "beta"])
        self.assertEqual(_skill_repo_declared_names({}, "repo", "owner/gamma"), ["gamma"])
        self.assertEqual(_skill_repo_declared_names({}, "path", "/tmp/delta"), ["delta"])
        self.assertEqual(_skill_repo_declared_names({}, "distributor", "acme"), [])

    def test_declared_skill_occurrences_counts_lock_targets_and_lock_only_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = root / "skill-repos.yaml"
            config.write_text("skill_repos:\n  - pick: [alpha]\n", encoding="utf-8")
            install_root = root / "installed"
            alpha_install = install_root / "alpha"
            alpha_install.mkdir(parents=True)
            (alpha_install / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            alpha_tree = directory_tree_sha256(alpha_install)
            lock_path = root / "skill-repos.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "skills": [
                            {
                                "name": "alpha",
                                "source_path": str(root / "alpha-source"),
                                "declared_ref": "main",
                                "resolved_commit": "abc123",
                                "install_tree_sha": alpha_tree,
                            },
                            {
                                "name": "beta",
                                "repo": "owner/beta",
                                "declared_ref": "v1",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            skillset = {
                "id": "personal-skills",
                "kind": "skill-repo-set",
                "skill_repos_config_host_path": str(config),
                "lock_path_host_path": str(lock_path),
                "install_targets": [{"id": "codex", "host_path": str(install_root)}],
            }
            model = {
                "active_clients": ["personal"],
                "skills": [{"id": "plain"}, skillset],
            }

            with mock.patch(
                "runtime_manager.skill_visibility.load_skill_repos_config",
                return_value={"skill_repos": [{"path": str(root / "alpha-source"), "pick": ["alpha"]}]},
            ):
                occurrences, layers = _declared_skill_occurrences(model)

        by_name = {occurrence["name"]: occurrence for occurrence in occurrences}
        self.assertEqual(set(by_name), {"alpha", "beta"})
        self.assertEqual(by_name["alpha"]["scope"], "client")
        self.assertEqual(by_name["alpha"]["targets"][0]["state"], "ok")
        self.assertEqual(by_name["beta"]["source_kind"], "repo")
        self.assertEqual(by_name["beta"]["source_bucket"], "repo")
        self.assertEqual(layers[0]["skill_count"], 2)
        self.assertEqual(layers[0]["healthy_targets"], 1)
        self.assertEqual(layers[0]["target_count"], 2)

    def test_skill_destination_bases_cover_auto_category_and_project_fallbacks(self) -> None:
        cwd = Path("/repo/app")
        with mock.patch("runtime_manager.skill_visibility._global_install_allowed", return_value=True):
            self.assertEqual(
                _skill_destination_bases({}, "ui", cwd=cwd, to="auto", categories=[]),
                ("global", [{"scope": "global", "path": None, "category": None}], []),
            )

        with (
            mock.patch("runtime_manager.skill_visibility._global_install_allowed", return_value=False),
            mock.patch("runtime_manager.skill_visibility._matching_scope_rule", return_value=None),
            mock.patch("runtime_manager.skill_visibility._repo_root_for_skill_install", return_value=Path("/repo")),
        ):
            resolved_to, bases, warnings = _skill_destination_bases({}, "ui", cwd=cwd, to="category", categories=[])
        self.assertEqual(resolved_to, "project")
        self.assertEqual(bases, [{"scope": "project", "path": "/repo", "category": None}])
        self.assertIn("falling back to the current repo", warnings[0])

        def category_by_id(_model: dict, category_id: str) -> dict | None:
            if category_id == "frontend":
                return {"paths": ["/repo/a", "/repo/b"]}
            return None

        with mock.patch("runtime_manager.skill_visibility._category_by_id", side_effect=category_by_id):
            resolved_to, bases, warnings = _skill_destination_bases(
                {},
                "ui",
                cwd=cwd,
                to="category",
                categories=["frontend", "missing"],
            )
        self.assertEqual(resolved_to, "category")
        self.assertEqual([base["path"] for base in bases], ["/repo/a", "/repo/b"])
        self.assertEqual(warnings, ["Unknown project category: missing"])

        with (
            mock.patch("runtime_manager.skill_visibility._matching_scope_rule", return_value={"paths": ["/repo", "/repo/app"]}),
            mock.patch("runtime_manager.skill_visibility._repo_root_for_skill_install", return_value=Path("/fallback")),
        ):
            self.assertEqual(
                _skill_destination_bases({}, "ui", cwd=cwd, to="project", categories=[])[1],
                [{"scope": "project", "path": "/repo/app", "category": None}],
            )

    def test_skill_lifecycle_add_links_skill_to_category_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project_a = root / "repos" / "web-a"
            project_b = root / "repos" / "web-b"
            clients_root.mkdir()
            project_a.mkdir(parents=True)
            project_b.mkdir(parents=True)
            skill_dir = source_root / "ui"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# UI\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: []\n"
                f"skill_source_roots: [{source_root}]\n"
                "project_categories:\n"
                "  frontend:\n"
                f"    paths: [{project_a}, {project_b}]\n"
                "rules:\n"
                "  - id: frontend-local\n"
                "    skills: [ui]\n"
                "    categories: [frontend]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            plan = skill_lifecycle_plan(
                model,
                "add",
                skill_name="ui",
                cwd=str(project_a),
                to="category",
                categories=["frontend"],
                source=str(skill_dir),
            )
            result = apply_skill_lifecycle_plan(plan, dry_run=False)

            self.assertEqual(result["summary"]["link"], 4)
            for project in (project_a, project_b):
                for surface in ("claude", "codex"):
                    link = project / f".{surface}" / "skills" / "ui"
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), skill_dir.resolve())

    def test_skill_lifecycle_activate_links_both_surfaces_and_returns_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project = root / "repos" / "tool"
            clients_root.mkdir()
            project.mkdir(parents=True)
            skill_dir = source_root / "hot-skill"
            skill_dir.mkdir(parents=True)
            skill_md = "# Hot Skill\n\nUse immediately.\n"
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            plan = skill_lifecycle_plan(
                model,
                "activate",
                skill_name="hot-skill",
                cwd=str(project),
                to="project",
                source=str(skill_dir),
            )
            result = apply_skill_lifecycle_plan(plan, dry_run=False)

            self.assertEqual(result["summary"]["link"], 2)
            packet = result["activation_packet"]
            self.assertEqual(packet["name"], "hot-skill")
            self.assertEqual(packet["skill_md"], skill_md)
            self.assertEqual(
                packet["skill_md_sha256"],
                hashlib.sha256(skill_md.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(set(packet["surface_targets"]), {"claude", "codex"})
            for surface in ("claude", "codex"):
                link = project / f".{surface}" / "skills" / "hot-skill"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), skill_dir.resolve())
                self.assertIn(str(link.parent.resolve() / link.name), packet["surface_targets"][surface])

    def test_overlay_activation_defaults_to_project_scope_and_tracks_cwd_metamorphically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project_a = root / "repos" / "tool-a"
            project_b = root / "repos" / "tool-b"
            clients_root.mkdir()
            project_a.mkdir(parents=True)
            project_b.mkdir(parents=True)
            skill_dir = source_root / "hot-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Hot Skill\n\nUse immediately.\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: []\n"
                f"skill_source_roots: [{source_root}]\n"
                "rules:\n"
                "  - id: marketing-local\n"
                "    overlay: marketing\n"
                "    skills: [hot-skill]\n"
                f"    paths: [{root / 'repos'}]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            first = activate_overlay_scoped_skills(model, "marketing", project_a)
            second = activate_overlay_scoped_skills(model, "marketing", project_b)

            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(first[0]["summary"]["link"], 2)
            self.assertEqual(second[0]["summary"]["link"], 2)
            self.assertEqual(
                first[0]["activation_packet"]["skill_md_sha256"],
                second[0]["activation_packet"]["skill_md_sha256"],
            )
            for surface in ("claude", "codex"):
                first_target = first[0]["activation_packet"]["surface_targets"][surface][0]
                second_target = second[0]["activation_packet"]["surface_targets"][surface][0]
                self.assertEqual(second_target, first_target.replace(str(project_a.resolve()), str(project_b.resolve())))
                self.assertTrue(first_target.startswith(str(project_a.resolve())))
                self.assertTrue(second_target.startswith(str(project_b.resolve())))
                self.assertFalse(first_target.startswith(str(Path.home())))
                self.assertFalse(second_target.startswith(str(Path.home())))

    def test_overlay_activation_can_explicitly_target_global_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project = root / "repos" / "tool"
            clients_root.mkdir()
            project.mkdir(parents=True)
            skill_dir = source_root / "hot-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Hot Skill\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                f"skill_source_roots: [{source_root}]\n"
                "rules:\n"
                "  - id: marketing-local\n"
                "    overlay: marketing\n"
                "    skills: [hot-skill]\n"
                f"    paths: [{project}]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            activations = activate_overlay_scoped_skills(
                model,
                "marketing",
                project,
                to="global",
                dry_run=True,
            )

            destinations = {action["destination"] for action in activations[0]["actions"]}
            self.assertEqual(destinations, {
                str(Path.home() / ".claude" / "skills" / "hot-skill"),
                str(Path.home() / ".codex" / "skills" / "hot-skill"),
            })

    def test_overlay_unlink_scope_project_does_not_remove_global_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            project = root / "repos" / "tool"
            fake_home = root / "home"
            source = root / "source-skills" / "hot-skill"
            clients_root.mkdir()
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# Hot Skill\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "rules:\n"
                "  - id: marketing-local\n"
                "    overlay: marketing\n"
                "    skills: [hot-skill]\n",
                encoding="utf-8",
            )
            for base in (
                project / ".claude" / "skills",
                project / ".codex" / "skills",
                fake_home / ".claude" / "skills",
                fake_home / ".codex" / "skills",
            ):
                base.mkdir(parents=True)
                (base / "hot-skill").symlink_to(source)
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                removed = unlink_overlay_scoped_skills(model, "marketing", project, scope="project")

            self.assertEqual(set(removed), {
                str(project / ".claude" / "skills" / "hot-skill"),
                str(project / ".codex" / "skills" / "hot-skill"),
            })
            self.assertFalse((project / ".claude" / "skills" / "hot-skill").exists())
            self.assertFalse((project / ".codex" / "skills" / "hot-skill").exists())
            self.assertTrue((fake_home / ".claude" / "skills" / "hot-skill").is_symlink())
            self.assertTrue((fake_home / ".codex" / "skills" / "hot-skill").is_symlink())

    def test_skill_lifecycle_auto_uses_project_when_global_policy_disallows_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project = root / "repos" / "tool"
            clients_root.mkdir()
            project.mkdir(parents=True)
            skill_dir = source_root / "mcp-server-design"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# MCP\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [smart]\n"
                f"skill_source_roots: [{source_root}]\n"
                "rules:\n"
                "  - id: mcp-local\n"
                "    skills: [mcp-server-design]\n"
                f"    paths: [{project}]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            auto_plan = skill_lifecycle_plan(
                model,
                "add",
                skill_name="mcp-server-design",
                cwd=str(project),
                to="auto",
            )
            global_plan = skill_lifecycle_plan(
                model,
                "add",
                skill_name="mcp-server-design",
                cwd=str(project),
                to="global",
            )

            self.assertEqual(auto_plan["resolved_to"], "project")
            self.assertEqual({item["scope"] for item in auto_plan["actions"]}, {"project"})
            self.assertEqual(global_plan["summary"]["blocked"], 2)

    def test_skill_lifecycle_prefers_path_specific_rule_for_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            blocked_project = root / "repos" / "blocked"
            allowed_project = root / "repos" / "notes-grep"
            for path in (clients_root, blocked_project, allowed_project):
                path.mkdir(parents=True)
            skill_dir = source_root / "rust-crates-publishing"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Rust Crates Publishing\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [smart]\n"
                f"skill_source_roots: [{source_root}]\n"
                "rules:\n"
                "  - id: broad-rust-local\n"
                "    skills: [rust-crates-publishing]\n"
                f"    paths: [{blocked_project}]\n"
                "  - id: notes-grep-package-local\n"
                "    skills: [rust-crates-publishing]\n"
                f"    paths: [{allowed_project}]\n"
                "    default: off\n"
                "    activation: on-demand\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            plan = skill_lifecycle_plan(
                model,
                "activate",
                skill_name="rust-crates-publishing",
                cwd=str(allowed_project),
                to="auto",
            )

            self.assertEqual(plan["summary"]["blocked"], 0)
            self.assertEqual({item["scope"] for item in plan["actions"]}, {"project"})
            self.assertEqual(
                {Path(str(item["repo_path"])) for item in plan["actions"]},
                {allowed_project.resolve()},
            )

            apply_skill_lifecycle_plan(plan, dry_run=False)
            visibility = collect_skill_visibility(
                model,
                cwd=str(allowed_project),
                include_global=False,
                include_project=True,
            )
            self.assertEqual(visibility["summary"]["scope_violations"], 0)

    def test_skill_sync_links_repo_required_skill_even_when_global_install_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            project = root / "repos" / "ios-app"
            fake_home = root / "home"
            clients_root.mkdir()
            project.mkdir(parents=True)
            skill_dir = source_root / "recipe-ios-ui-catalog"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Recipe iOS UI Catalog\n", encoding="utf-8")
            global_root = fake_home / ".claude" / "skills"
            global_root.mkdir(parents=True)
            (global_root / "recipe-ios-ui-catalog").symlink_to(skill_dir, target_is_directory=True)
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: []\n"
                f"skill_source_roots: [{source_root}]\n"
                "project_categories:\n"
                "  ios:\n"
                f"    paths: [{project}]\n"
                "rules:\n"
                "  - id: ios-local\n"
                "    skills: [recipe-ios-ui-catalog]\n"
                "    categories: [ios]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=fake_home):
                plan = skill_lifecycle_plan(model, "sync", cwd=str(project), to="auto")

            self.assertEqual(plan["summary"]["link"], 2)
            self.assertEqual({item["scope"] for item in plan["actions"]}, {"project"})
            self.assertEqual(
                {Path(str(item["destination"])).parent.parent.name for item in plan["actions"]},
                {".claude", ".codex"},
            )

    def test_on_demand_rule_routes_explicit_activation_to_current_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            source_root = root / "source-skills"
            repos_root = root / "repos"
            project = repos_root / "app"
            skill_dir = source_root / "divide-and-conquer"
            for path in (clients_root, skill_dir, project):
                path.mkdir(parents=True)
            (project / ".git").mkdir()
            (skill_dir / "SKILL.md").write_text("# Divide and Conquer\n", encoding="utf-8")
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [smart, sbp]\n"
                f"skill_source_roots: [{source_root}]\n"
                "rules:\n"
                "  - id: dispatcher-global\n"
                "    skills: [smart, sbp]\n"
                "    allow_global: true\n"
                "  - id: routed-on-demand\n"
                "    skills: [divide-and-conquer]\n"
                f"    paths: [{repos_root}]\n"
                "    default: off\n"
                "    activation: on-demand\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            visibility = collect_skill_visibility(model, cwd=str(project), include_global=False)
            self.assertEqual(visibility["issues"]["missing_for_cwd"], [])

            plan = skill_lifecycle_plan(
                model,
                "activate",
                skill_name="divide-and-conquer",
                cwd=str(project),
                to="auto",
            )

            self.assertEqual(plan["resolved_to"], "project")
            self.assertEqual({item["scope"] for item in plan["actions"]}, {"project"})
            self.assertEqual(
                {Path(str(item["destination"])).parent.parent for item in plan["actions"]},
                {(project / ".claude").resolve(), (project / ".codex").resolve()},
            )
            self.assertIsNotNone(plan["activation_packet"])

    def test_scope_policy_flags_project_install_outside_allowed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            blocked = root / "blocked"
            allowed = root / "allowed"
            skill_dir = blocked / ".claude" / "skills" / "restricted-tool"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Restricted\n", encoding="utf-8")

            model = {
                "active_clients": ["personal"],
                "active_profiles": ["core"],
                "clients": [
                    {
                        "id": "personal",
                        "label": "Personal",
                        "context": {
                            "cwd_match": [str(blocked)],
                            "skill_scope": {
                                "rules": [
                                    {
                                        "id": "restricted-local",
                                        "skills": ["restricted-*"],
                                        "paths": [str(allowed)],
                                    }
                                ]
                            },
                        },
                    }
                ],
                "skills": [],
            }

            payload = collect_skill_visibility(
                model,
                cwd=str(blocked),
                include_global=False,
                include_project=True,
            )

            violations = payload["issues"]["scope_violations"]
            self.assertEqual(payload["summary"]["scope_violations"], 1)
            self.assertEqual(violations[0]["name"], "restricted-tool")
            self.assertEqual(violations[0]["scope_rule"], "restricted-local")

    def test_matched_clients_prefers_repo_specific_overlay_on_equal_prefix(self) -> None:
        cwd = Path("/tmp/repos/api_server")
        model = {
            "clients": [
                {
                    "id": "cca",
                    "label": "CCA",
                    "default_cwd": "/tmp/repos/example-website",
                    "context": {"cwd_match": ["/tmp/repos/api_server"]},
                },
                {
                    "id": "api",
                    "label": "API",
                    "default_cwd": "/tmp/repos/api",
                    "context": {"cwd_match": ["/tmp/repos/api_server"]},
                },
            ]
        }

        matches = matched_skill_clients(model, cwd)

        self.assertEqual(matches[0]["id"], "api")

    def test_collects_broken_project_links_as_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = root / "project"
            skills_root = project / ".codex" / "skills"
            skills_root.mkdir(parents=True)
            (skills_root / "local-only").symlink_to(root / "missing-skill")

            payload = collect_skill_visibility(
                {"clients": [], "skills": []},
                cwd=str(project),
                include_global=False,
                include_project=True,
            )

            self.assertEqual(payload["summary"]["broken_project"], 1)
            self.assertEqual(payload["issues"]["broken_project"][0]["name"], "local-only")

    def test_collects_beads_requirement_from_skill_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = root / "project"
            (project / ".git").mkdir(parents=True)
            source = root / "skills" / "smart"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\n"
                "name: smart\n"
                "metadata:\n"
                "  requires_beads: true\n"
                "---\n"
                "# Smart\n",
                encoding="utf-8",
            )
            install_root = project / ".codex" / "skills"
            install_root.mkdir(parents=True)
            (install_root / "smart").symlink_to(source, target_is_directory=True)

            with mock.patch("runtime_manager.skill_visibility.shutil.which", return_value="/usr/local/bin/br"):
                payload = collect_skill_visibility(
                    {"clients": [], "skills": []},
                    cwd=str(project),
                    include_global=False,
                    include_project=True,
                )

                beads = payload["beads"]
                self.assertTrue(beads["required"])
                self.assertEqual(beads["required_skills"][0]["name"], "smart")
                self.assertFalse(beads["initialized"])
                self.assertEqual(beads["issues"][0]["code"], "no_beads_dir")
                self.assertTrue(
                    any(action.startswith("sbp beads init") for action in payload["next_actions"])
                )
                self.assertEqual(payload["summary"]["beads_required_skills"], 1)
                self.assertEqual(payload["summary"]["beads_issues"], 1)

                (project / ".beads").mkdir()
                initialized_payload = collect_skill_visibility(
                    {"clients": [], "skills": []},
                    cwd=str(project),
                    include_global=False,
                    include_project=True,
                )
                self.assertTrue(initialized_payload["beads"]["ok"])
                self.assertEqual(initialized_payload["beads"]["issues"], [])

    def test_allow_global_rules_flag_unapproved_global_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            clients_root.mkdir()
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [always-global]\n"
                "rules:\n"
                "  - id: global\n"
                "    skills: [always-global]\n"
                "    allow_global: true\n",
                encoding="utf-8",
            )

            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            from runtime_manager.skill_visibility import _global_install_allowed  # noqa: PLC0415

            self.assertTrue(_global_install_allowed(model, "always-global"))
            self.assertFalse(_global_install_allowed(model, "too-broad"))

    def test_global_allowlist_snapshot_does_not_grant_global_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            clients_root.mkdir()
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [snapshot-only]\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            from runtime_manager.skill_visibility import _global_install_allowed  # noqa: PLC0415

            self.assertFalse(_global_install_allowed(model, "snapshot-only"))

    def test_allow_global_rules_prevent_extra_global_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            home = root / "home"
            allowed_source = root / "sources" / "always-global"
            extra_source = root / "sources" / "too-broad"
            global_root = home / ".codex" / "skills"
            for path in (clients_root, allowed_source, extra_source, global_root):
                path.mkdir(parents=True)
            (allowed_source / "SKILL.md").write_text("# Always\n", encoding="utf-8")
            (extra_source / "SKILL.md").write_text("# Extra\n", encoding="utf-8")
            (global_root / "always-global").symlink_to(allowed_source, target_is_directory=True)
            (global_root / "too-broad").symlink_to(extra_source, target_is_directory=True)
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [always-global]\n"
                "rules:\n"
                "  - id: global\n"
                "    skills: [always-global]\n"
                "    allow_global: true\n",
                encoding="utf-8",
            )
            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }

            with mock.patch("runtime_manager.skill_visibility.Path.home", return_value=home):
                payload = collect_skill_visibility(model, cwd=str(root), include_project=False)

            self.assertEqual(payload["issues"]["global_not_allowed"][0]["name"], "too-broad")
            self.assertEqual(payload["issues"]["extra_global"][0]["name"], "too-broad")
            self.assertNotIn(
                "always-global",
                {item["name"] for item in payload["issues"]["extra_global"]},
            )

    def test_project_skill_roots_stop_at_nearest_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            repo = parent / "repo"
            subdir = repo / "src"
            (parent / ".claude" / "skills").mkdir(parents=True)
            (repo / ".git").mkdir(parents=True)
            (repo / ".claude" / "skills").mkdir(parents=True)
            subdir.mkdir()

            roots = [path for _, path in _project_skill_roots(subdir)]

            self.assertIn((repo / ".claude" / "skills").resolve(), roots)
            self.assertNotIn((parent / ".claude" / "skills").resolve(), roots)


class SkillEvidenceAnnotationTests(unittest.TestCase):
    """OPTIONAL per-candidate evidence annotation on candidate rows."""

    def _payload(self):
        return {
            "effective": [
                {"name": "sbp", "availability": "installed", "layer": "global:claude"},
                {"name": "ui", "availability": "installed", "layer": "project:claude:/r"},
            ],
            "undefined_sources": [{"name": "ga4"}],
        }

    def test_attach_annotates_only_skills_with_evidence(self) -> None:
        index = {
            "sbp": {"invocations_in_repo": 4, "last_used": "2026-06-05T10:00:00.000Z", "fleet_wide_count": 12},
            "ga4": {"invocations_in_repo": 0, "last_used": None, "fleet_wide_count": 3},
        }
        payload = attach_skill_evidence(self._payload(), index)
        effective = {row["name"]: row for row in payload["effective"]}
        # sbp has evidence; ui does not -> ui keeps no evidence field.
        self.assertEqual(effective["sbp"]["evidence"]["invocations_in_repo"], 4)
        self.assertEqual(effective["sbp"]["evidence"]["fleet_wide_count"], 12)
        self.assertNotIn("evidence", effective["ui"])
        # undefined_sources (source-backed candidates) get annotated too.
        self.assertEqual(payload["undefined_sources"][0]["evidence"]["fleet_wide_count"], 3)

    def test_attach_is_noop_when_index_empty_or_none(self) -> None:
        # Cass down / no provider: NOTHING attached, rows keep their shape.
        for empty in (None, {}, []):
            payload = attach_skill_evidence(self._payload(), empty)
            for row in payload["effective"]:
                self.assertNotIn("evidence", row)
            for row in payload["undefined_sources"]:
                self.assertNotIn("evidence", row)

    def test_collect_visibility_provider_failure_does_not_crash(self) -> None:
        # A throwing provider must be swallowed: visibility still returns.
        def _boom():
            raise RuntimeError("cass exploded")

        model = {"skillsets": [], "clients": [], "active_clients": []}
        with tempfile.TemporaryDirectory() as tmp:
            payload = collect_skill_visibility(
                model, cwd=tmp, include_global=False, evidence_provider=_boom
            )
        self.assertIn("effective", payload)

    def test_collect_visibility_provider_annotates_effective(self) -> None:
        model = {"skillsets": [], "clients": [], "active_clients": []}
        with tempfile.TemporaryDirectory() as tmp:
            project_skills = Path(tmp) / ".claude" / "skills"
            project_skills.mkdir(parents=True)
            source = Path(tmp) / "src" / "demo"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            (project_skills / "demo").symlink_to(source)

            def _provider():
                return {"demo": {"invocations_in_repo": 7, "last_used": "2026-06-05T00:00:00Z", "fleet_wide_count": 9}}

            payload = collect_skill_visibility(
                model, cwd=tmp, include_global=False, evidence_provider=_provider
            )
        effective = {row.get("name"): row for row in payload["effective"]}
        self.assertIn("demo", effective)
        self.assertEqual(effective["demo"]["evidence"]["invocations_in_repo"], 7)
        # Compaction preserves the evidence annotation.
        compact = compact_skill_visibility_payload(payload)
        compact_demo = {row.get("name"): row for row in compact["effective"]}["demo"]
        self.assertEqual(compact_demo["evidence"]["fleet_wide_count"], 9)


if __name__ == "__main__":
    unittest.main()
