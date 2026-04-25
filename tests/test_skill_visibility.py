from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.skill_visibility import (  # noqa: E402
    apply_skill_lifecycle_plan,
    _effective_occurrences,
    _project_skill_roots,
    _scan_installed_root,
    collect_skill_visibility,
    matched_skill_clients,
    skill_lifecycle_plan,
)


class SkillVisibilityTests(unittest.TestCase):
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
        cwd = Path("/tmp/repos/htma_server")
        model = {
            "clients": [
                {
                    "id": "cca",
                    "label": "CCA",
                    "default_cwd": "/tmp/repos/cca-website",
                    "context": {"cwd_match": ["/tmp/repos/htma_server"]},
                },
                {
                    "id": "htma",
                    "label": "HTMA",
                    "default_cwd": "/tmp/repos/htma",
                    "context": {"cwd_match": ["/tmp/repos/htma_server"]},
                },
            ]
        }

        matches = matched_skill_clients(model, cwd)

        self.assertEqual(matches[0]["id"], "htma")

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

    def test_global_allowlist_flags_unapproved_global_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clients_root = root / "clients"
            clients_root.mkdir()
            (root / "skill-scope.yaml").write_text(
                "version: 1\n"
                "global_allowlist: [always-global]\n",
                encoding="utf-8",
            )

            model = {
                "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
                "clients": [],
                "skills": [],
            }
            occurrences = [
                {
                    "name": "always-global",
                    "availability": "installed",
                    "layer": "global:claude",
                    "state": "ok",
                    "path": str(root / "global" / "always-global"),
                },
                {
                    "name": "too-broad",
                    "availability": "installed",
                    "layer": "global:claude",
                    "state": "ok",
                    "path": str(root / "global" / "too-broad"),
                },
            ]

            from runtime_manager.skill_visibility import _global_install_allowed  # noqa: PLC0415

            self.assertTrue(_global_install_allowed(model, "always-global"))
            self.assertFalse(_global_install_allowed(model, "too-broad"))

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


if __name__ == "__main__":
    unittest.main()
