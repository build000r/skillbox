from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
GOLDEN_DIR = ROOT_DIR / "tests" / "goldens" / "skill_pull"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.skill_pull import (  # noqa: E402
    MAX_ENTRY_BYTES,
    SkillPullError,
    build_resolution_request,
    pull_host_skill,
    resolve_host_skills,
    validate_resolution_request,
)
from runtime_manager import skill_pull as SKILL_PULL  # noqa: E402
from runtime_manager import cli as CLI  # noqa: E402
from runtime_manager.shared import directory_tree_sha256  # noqa: E402
from runtime_manager.skill_visibility import collect_skill_visibility  # noqa: E402


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _initialize_git_repo(repo: Path, *, allow_empty_commit: bool = True) -> None:
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "fixture@example.invalid")
    _run_git(repo, "config", "user.name", "Skill Pull Fixture")
    if allow_empty_commit:
        _run_git(repo, "commit", "--allow-empty", "-q", "-m", "fixture")


class HostSkillFixture:
    def __init__(
        self,
        root: Path,
        *,
        skill_name: str = "sbp",
        real_git: bool = False,
    ) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        if real_git:
            _initialize_git_repo(self.repo)
        else:
            git_dir = self.repo / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text("1" * 40 + "\n", encoding="ascii")

        self.config = root / "config"
        (self.config / "clients").mkdir(parents=True)
        self.skills_root = root / "skill-sources"
        self.skill = self.skills_root / skill_name
        (self.skill / "references").mkdir(parents=True)
        self.entry = self.skill / "SKILL.md"
        self.entry.write_text(f"# {skill_name}\n\nUse live policy.\n", encoding="utf-8")
        self.reference = self.skill / "references" / "model.md"
        self.reference.write_text("model v1\n", encoding="utf-8")
        (self.config / "skill-scope.yaml").write_text(
            "\n".join(
                [
                    "policy_epoch: 7",
                    "skill_source_roots:",
                    f"  - {self.skills_root}",
                    "rules: []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.skill_name = skill_name
        self.model = {
            "repos": [{"id": "fixture-host", "host_path": str(self.repo)}],
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(self.config / "clients")},
            "active_clients": [],
            "active_profiles": [],
            "clients": [],
            "skills": [],
            "_skill_visibility_simulation": {
                "repo_path": str(self.repo),
                "installed_occurrences": [
                    {
                        "name": skill_name,
                        "availability": "installed",
                        "layer": "what-if:planned-project",
                        "layer_label": "fixture project",
                        "layer_rank": 100,
                        "scope": "installed",
                        "source_kind": "directory",
                        "source": str(self.skill),
                        "source_bucket": "host-fixture",
                        "path": str(self.skill),
                        "state": "ok",
                    }
                ],
            },
        }

    def add_skill(self, name: str, entry_text: str | None = None) -> Path:
        skill = self.skills_root / name
        skill.mkdir(parents=True)
        if entry_text is not None:
            (skill / "SKILL.md").write_text(entry_text, encoding="utf-8")
        self.model["_skill_visibility_simulation"]["installed_occurrences"].append(
            {
                "name": name,
                "availability": "installed",
                "layer": "what-if:planned-project",
                "layer_label": "fixture project",
                "layer_rank": 100,
                "scope": "installed",
                "source_kind": "directory",
                "source": str(skill),
                "source_bucket": "host-fixture",
                "path": str(skill),
                "state": "ok",
            }
        )
        return skill

    def pin_off(self) -> None:
        self.model["_skill_visibility_simulation"]["installed_occurrences"][0]["layer_rank"] = 40
        self.model["_skill_visibility_simulation"]["repo_override_policy"] = {
            "ok": True,
            "version": 1,
            "pin_on": [],
            "pin_off": [self.skill_name],
            "opt_out_global": [],
            "overlays": {"enable": [], "disable": []},
            "defaults": [],
            "reason": "fixture",
            "errors": [],
            "_repo_root": str(self.repo),
            "_policy_path": str(self.repo / ".skillbox" / "skill-overrides.yaml"),
            "_simulated": True,
        }


def _snapshot_tree(root: Path) -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for path in sorted([root, *root.rglob("*")]):
        rel = path.relative_to(root).as_posix() if path != root else "."
        mode = path.lstat().st_mode
        kind = (
            "symlink"
            if stat.S_ISLNK(mode)
            else "directory"
            if stat.S_ISDIR(mode)
            else "file"
            if stat.S_ISREG(mode)
            else "other"
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else ""
        rows.append((rel, kind, stat.S_IMODE(mode), digest))
    return rows


def _normalize_closed_record(value: object) -> object:
    if isinstance(value, list):
        return [_normalize_closed_record(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, object] = {}
    logical_source_id = value.get("logical_source_id")
    for key, item in value.items():
        if key == "policy_sha256" and item is not None:
            normalized[key] = "<policy-sha256>"
        elif key == "receipt_sha256" and item is not None:
            normalized[key] = "<receipt-sha256>"
        elif key == "sha256" and logical_source_id and item is not None:
            normalized[key] = f"<{logical_source_id}-sha256>"
        else:
            normalized[key] = _normalize_closed_record(item)
    return normalized


class SkillPullContractTests(unittest.TestCase):
    def test_resolution_is_deterministic_except_invocation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            first = resolve_host_skills(fixture.model, cwd=fixture.repo, explicit_skills=["sbp"])
            second = resolve_host_skills(fixture.model, cwd=fixture.repo, explicit_skills=["sbp"])

        self.assertEqual(first["schema_version"], "skill-resolution-receipt/v1")
        self.assertEqual(first["repository"], {
            "repository_id": "fixture-host",
            "base_sha": "1" * 40,
            "cwd_relative": ".",
        })
        self.assertEqual(first["policy"]["policy_epoch"], 7)
        self.assertEqual(first["selected_names"], ["sbp"])
        self.assertEqual(first["totals"]["candidate_count"], 1)
        self.assertEqual(first["skills"][0]["reason_code"], "DISPATCHER_FLOOR")
        for key in ("policy_sha256",):
            self.assertEqual(first["policy"][key], second["policy"][key])
        for key in ("tree_sha256", "entry_sha256", "entry_bytes"):
            self.assertEqual(first["skills"][0][key], second["skills"][0][key])
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first["resolution_id"], second["resolution_id"])
        self.assertNotEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertRegex(first["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_policy_dirty_observes_only_exact_file_backed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            _initialize_git_repo(fixture.config, allow_empty_commit=False)
            _run_git(fixture.config, "add", "skill-scope.yaml")
            _run_git(fixture.config, "commit", "-q", "-m", "policy")

            (fixture.config / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            clean = resolve_host_skills(
                fixture.model,
                cwd=fixture.repo,
                explicit_skills=["sbp"],
            )
            self.assertFalse(clean["policy"]["dirty"])

            policy_path = fixture.config / "skill-scope.yaml"
            policy_path.write_text(
                policy_path.read_text(encoding="utf-8") + "# modified\n",
                encoding="utf-8",
            )
            dirty = resolve_host_skills(
                fixture.model,
                cwd=fixture.repo,
                explicit_skills=["sbp"],
            )

        self.assertTrue(dirty["policy"]["dirty"])

    def test_untracked_repo_override_is_dirty_and_probe_is_narrow_read_only_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir), real_git=True)
            override = fixture.repo / ".skillbox" / "skill-overrides.yaml"
            override.parent.mkdir()
            override.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "pin_on: []",
                        "pin_off: []",
                        "opt_out_global: []",
                        "overlays:",
                        "  enable: []",
                        "  disable: []",
                        "defaults: []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            original_run = subprocess.run
            with patch.object(SKILL_PULL.subprocess, "run", wraps=original_run) as run:
                receipt = resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )

        self.assertTrue(receipt["policy"]["dirty"])
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "git",
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
                "--",
                ".skillbox/skill-overrides.yaml",
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(run.call_args.kwargs["cwd"], fixture.repo)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_policy_dirty_changes_receipt_not_effective_policy_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            _initialize_git_repo(fixture.config, allow_empty_commit=False)
            _run_git(fixture.config, "add", "skill-scope.yaml")
            _run_git(fixture.config, "commit", "-q", "-m", "policy")
            stable_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
            with (
                patch.object(SKILL_PULL.uuid, "uuid4", return_value=stable_uuid),
                patch.object(SKILL_PULL, "_utc_now", return_value="2026-07-28T00:00:00.000000Z"),
            ):
                clean = resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )
                _run_git(fixture.config, "update-index", "--chmod=+x", "skill-scope.yaml")
                dirty = resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )

        self.assertFalse(clean["policy"]["dirty"])
        self.assertTrue(dirty["policy"]["dirty"])
        self.assertEqual(clean["policy"]["policy_sha256"], dirty["policy"]["policy_sha256"])
        self.assertNotEqual(clean["receipt_sha256"], dirty["receipt_sha256"])

    def test_policy_checkout_observation_failure_is_environment_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            _initialize_git_repo(fixture.config, allow_empty_commit=False)
            _run_git(fixture.config, "add", "skill-scope.yaml")
            _run_git(fixture.config, "commit", "-q", "-m", "policy")
            failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout=b"", stderr=b"failure")
            with (
                patch.object(SKILL_PULL.subprocess, "run", return_value=failed),
                self.assertRaises(SkillPullError) as raised,
            ):
                resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )

        self.assertEqual(raised.exception.error_code, "SBP_ENVIRONMENT_UNSUPPORTED")

    def test_malformed_policy_checkout_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            (fixture.config / ".git").mkdir()
            with self.assertRaises(SkillPullError) as raised:
                resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )

        self.assertEqual(raised.exception.error_code, "SBP_ENVIRONMENT_UNSUPPORTED")

    def test_dirty_global_policy_does_not_skip_override_observation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir), real_git=True)
            override = fixture.repo / ".skillbox" / "skill-overrides.yaml"
            override.parent.mkdir()
            override.write_text(
                "version: 1\npin_on: []\npin_off: []\nopt_out_global: []\n",
                encoding="utf-8",
            )
            with (
                patch.object(
                    SKILL_PULL,
                    "_policy_source_dirty",
                    side_effect=[
                        True,
                        SkillPullError(
                            "SBP_ENVIRONMENT_UNSUPPORTED",
                            "override observation failed",
                        ),
                    ],
                ) as observe,
                self.assertRaises(SkillPullError) as raised,
            ):
                resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )

        self.assertEqual(observe.call_count, 2)
        self.assertEqual(raised.exception.error_code, "SBP_ENVIRONMENT_UNSUPPORTED")

    def test_source_roots_are_resolved_once_per_receipt_not_once_per_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            names = ["sbp"]
            for index in range(25):
                name = f"fixture-{index:02d}"
                fixture.add_skill(name, f"# fixture {index}\n")
                names.append(name)
            original = SKILL_PULL._skill_source_roots  # noqa: SLF001
            with patch.object(
                SKILL_PULL,
                "_skill_source_roots",
                wraps=original,
            ) as source_roots:
                receipt = resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=names,
                )

        self.assertEqual(receipt["totals"]["admitted_count"], 26)
        # One shared source-root boundary feeds both policy identity and source
        # selection. This must not grow with candidate count.
        self.assertEqual(source_roots.call_count, 1)

    def test_resolution_visibility_uses_same_canonical_winners_as_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            fixture.add_skill("alpha", "# alpha\n")
            fixture.add_skill("missing-entry")
            audit = collect_skill_visibility(
                fixture.model,
                cwd=str(fixture.repo),
                include_global=True,
                include_project=True,
                include_sources=False,
            )
            resolution = SKILL_PULL._collect_resolution_visibility(  # noqa: SLF001
                fixture.model,
                fixture.repo,
            )

        keys = ("name", "state", "source", "layer", "override_action")
        audit_winners = [
            {key: row.get(key) for key in keys}
            for row in audit["visibility_decisions"]
        ]
        resolution_winners = [
            {key: row.get(key) for key in keys}
            for row in resolution["visibility_decisions"]
        ]
        self.assertEqual(resolution_winners, audit_winners)

    def test_pull_returns_exact_verified_entry_and_three_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            result = pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)

        self.assertEqual(
            result,
            {
                "ok": True,
                "schema_version": "skill-pull-result/v1",
                "name": "sbp",
                "lifecycle": "active",
                "entry_text": "# sbp\n\nUse live policy.\n",
                "tree_sha256": result["tree_sha256"],
                "entry_sha256": result["entry_sha256"],
                "receipt_sha256": result["receipt_sha256"],
                "source_classification": "host-canonical",
                "instructions": "use this content immediately in the current session",
            },
        )
        for key in ("tree_sha256", "entry_sha256", "receipt_sha256"):
            self.assertRegex(result[key], r"^[0-9a-f]{64}$")

    def test_changed_reference_fails_tree_drift_without_entry_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(
                    fixture.model,
                    "sbp",
                    cwd=fixture.repo,
                    after_resolve=lambda _receipt: fixture.reference.write_text(
                        "model v2\n",
                        encoding="utf-8",
                    ),
                )

        envelope = raised.exception.envelope()
        self.assertEqual(envelope["error_code"], "SKILL_TREE_DRIFT")
        self.assertTrue(envelope["retryable"])
        self.assertNotIn("entry_text", envelope)

    def test_deleted_entry_after_resolution_is_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(
                    fixture.model,
                    "sbp",
                    cwd=fixture.repo,
                    after_resolve=lambda _receipt: fixture.entry.unlink(),
                )

        self.assertEqual(raised.exception.error_code, "SKILL_SOURCE_MISSING")
        self.assertNotIn("entry_text", raised.exception.envelope())

    def test_invalid_utf8_entry_is_withheld(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            fixture.entry.write_bytes(b"\xff\xfe")
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)

        self.assertEqual(raised.exception.error_code, "SKILL_ENTRY_INVALID_UTF8")
        self.assertNotIn("entry_text", raised.exception.envelope())

    def test_symlink_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            outside = fixture.root / "outside.md"
            outside.write_text("private\n", encoding="utf-8")
            (fixture.skill / "escape.md").symlink_to(outside)
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)

        self.assertEqual(raised.exception.error_code, "SKILL_SOURCE_MISSING")
        self.assertNotIn(str(outside), str(raised.exception))

    def test_internal_file_and_directory_symlinks_match_canonical_tree_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            (fixture.skill / "linked-model.md").symlink_to("references/model.md")
            (fixture.skill / "linked-references").symlink_to(
                "references",
                target_is_directory=True,
            )

            result = pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)

            self.assertEqual(result["tree_sha256"], directory_tree_sha256(fixture.skill))

    def test_catalog_omits_invalid_candidates_without_poisoning_valid_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            fixture.add_skill("cass-memory")
            mmdx = fixture.add_skill("mmdx", "# mmdx\n")
            (mmdx / "linked.md").symlink_to(fixture.reference)
            fixture.add_skill(
                "retired-skill",
                "---\nlifecycle: retired\n---\n# retired\n",
            )
            fixture.add_skill(
                "runtime-skill",
                "---\nruntime_requirements:\n  - definitely-not-an-installed-runtime\n---\n# runtime\n",
            )
            fixture.add_skill(
                "conflict-skill",
                "---\nconflicts:\n  - sbp\n---\n# conflict\n",
            )
            budget = fixture.add_skill("budget-skill")
            (budget / "SKILL.md").write_bytes(b"x" * (MAX_ENTRY_BYTES + 1))

            receipt = resolve_host_skills(fixture.model, cwd=fixture.repo)

        self.assertEqual(receipt["schema_version"], "skill-resolution-receipt/v1")
        self.assertIn("sbp", receipt["selected_names"])
        decisions = {row["name"]: row for row in receipt["skills"]}
        expected_omissions = {
            "budget-skill": "BUDGET",
            "cass-memory": "SOURCE_MISSING",
            "conflict-skill": "CONFLICT",
            "mmdx": "SOURCE_MISSING",
            "retired-skill": "RETIRED",
            "runtime-skill": "RUNTIME_MISSING",
        }
        self.assertEqual(
            {name: decisions[name]["reason_code"] for name in expected_omissions},
            expected_omissions,
        )
        self.assertEqual(decisions["sbp"]["admission"], "admitted")
        for name in expected_omissions:
            decision = decisions[name]
            self.assertEqual(decision["admission"], "omitted")
            for field in ("tree_sha256", "entry_sha256", "entry_bytes", "estimated_entry_tokens"):
                self.assertIsNone(decision[field])

    def test_runtime_then_composition_then_budget_precedence_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            runtime_skill = fixture.add_skill("runtime-budget")
            runtime_skill.joinpath("SKILL.md").write_bytes(
                (
                    "---\nruntime_requirements:\n"
                    "  - definitely-not-an-installed-runtime\n---\n"
                ).encode()
                + b"x" * (MAX_ENTRY_BYTES + 1)
            )
            conflict_skill = fixture.add_skill("conflict-budget")
            conflict_skill.joinpath("SKILL.md").write_bytes(
                "---\nconflicts:\n  - sbp\n---\n".encode()
                + b"x" * (MAX_ENTRY_BYTES + 1)
            )
            budget_skill = fixture.add_skill("budget-only")
            budget_skill.joinpath("SKILL.md").write_bytes(b"x" * (MAX_ENTRY_BYTES + 1))

            cases = (
                ("runtime-budget", "SKILL_RUNTIME_REQUIREMENT_MISSING"),
                ("conflict-budget", "SKILL_COMPOSITION_CONFLICT"),
                ("budget-only", "SKILL_CONTEXT_BUDGET_EXCEEDED"),
            )
            for name, expected_code in cases:
                with self.subTest(name=name):
                    with self.assertRaises(SkillPullError) as raised:
                        pull_host_skill(fixture.model, name, cwd=fixture.repo)
                    self.assertEqual(raised.exception.error_code, expected_code)
                    self.assertNotIn("entry_text", raised.exception.envelope())

    def test_retired_explicit_pull_emits_lifecycle_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            fixture.add_skill(
                "retired-skill",
                "---\nlifecycle: retired\n---\n# retired\n",
            )
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(fixture.model, "retired-skill", cwd=fixture.repo)

        self.assertEqual(raised.exception.error_code, "SKILL_LIFECYCLE_RETIRED")
        self.assertNotIn("entry_text", raised.exception.envelope())

    def test_explicit_pull_preserves_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            fixture.add_skill("cass-memory")
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(fixture.model, "cass-memory", cwd=fixture.repo)

        self.assertEqual(raised.exception.error_code, "SKILL_SOURCE_MISSING")

    def test_non_floor_repo_pin_off_is_not_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir), skill_name="alpha")
            fixture.pin_off()
            with self.assertRaises(SkillPullError) as raised:
                pull_host_skill(fixture.model, "alpha", cwd=fixture.repo)

        self.assertEqual(raised.exception.error_code, "SKILL_NOT_ADMITTED")
        self.assertFalse(raised.exception.envelope()["retryable"])

    def test_request_is_closed_and_has_fixed_host_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            request = build_resolution_request(
                fixture.model,
                cwd=fixture.repo,
                explicit_skills=["sbp"],
                request_id="request-1",
            )
            self.assertEqual(
                {
                    "schema_version": request["schema_version"],
                    "request_id": request["request_id"],
                    "mode": request["mode"],
                    "surface": request["surface"],
                    "explicit_skills": request["explicit_skills"],
                    "max_entry_bytes": request["max_entry_bytes"],
                },
                {
                    "schema_version": "skill-resolution-request/v1",
                    "request_id": "request-1",
                    "mode": "host",
                    "surface": "host-cli",
                    "explicit_skills": ["sbp"],
                    "max_entry_bytes": MAX_ENTRY_BYTES,
                },
            )
            invalid = dict(request)
            invalid["source_path"] = str(fixture.skill)
            with self.assertRaises(SkillPullError) as raised:
                validate_resolution_request(invalid)

        self.assertEqual(raised.exception.error_code, "SKILL_REQUEST_INVALID")

    def test_request_rejects_wrong_scalar_types_and_non_normalized_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            request = build_resolution_request(fixture.model, cwd=fixture.repo)
            invalid_values = (
                ("request_id", 7),
                ("max_entry_bytes", True),
                ("explicit_skills", [7]),
                ("repository.repository_id", 7),
                ("repository.base_sha", b"1" * 40),
                ("repository.cwd_relative", "./child"),
            )
            for field, value in invalid_values:
                with self.subTest(field=field):
                    invalid = dict(request)
                    if field.startswith("repository."):
                        invalid["repository"] = dict(request["repository"])
                        invalid["repository"][field.split(".", 1)[1]] = value
                    else:
                        invalid[field] = value
                    with self.assertRaises(SkillPullError) as raised:
                        validate_resolution_request(invalid)
                    self.assertEqual(raised.exception.error_code, "SKILL_REQUEST_INVALID")

    def test_closed_record_and_complete_error_matrix_match_committed_golden(self) -> None:
        error_codes = (
            "SBP_ENVIRONMENT_UNSUPPORTED",
            "SKILL_REQUEST_INVALID",
            "SKILL_NOT_ADMITTED",
            "SKILL_SOURCE_MISSING",
            "SKILL_ENTRY_INVALID_UTF8",
            "SKILL_TREE_DRIFT",
            "SKILL_LIFECYCLE_RETIRED",
            "SKILL_RUNTIME_REQUIREMENT_MISSING",
            "SKILL_COMPOSITION_CONFLICT",
            "SKILL_CONTEXT_BUDGET_EXCEEDED",
            "PERFORMANCE_FIXTURE_INVALID",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            request = build_resolution_request(
                fixture.model,
                cwd=fixture.repo,
                explicit_skills=["sbp"],
                request_id="golden-request",
            )
            stable_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
            with (
                patch.object(SKILL_PULL.uuid, "uuid4", return_value=stable_uuid),
                patch.object(SKILL_PULL, "_utc_now", return_value="2026-07-28T00:00:00.000000Z"),
            ):
                receipt = resolve_host_skills(
                    fixture.model,
                    cwd=fixture.repo,
                    explicit_skills=["sbp"],
                )
                pull = pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)
            omitted = SKILL_PULL._omitted_decision("omitted-skill", "DEFAULT_OFF")  # noqa: SLF001
            actual = {
                "schema_version": "skill-pull-closed-record-goldens/v1",
                "request": request,
                "nested_records": {
                    "repository_identity": request["repository"],
                    "policy_identity": receipt["policy"],
                    "policy_source_digests": receipt["policy"]["sources"],
                    "admitted_skill_decision": receipt["skills"][0],
                    "omitted_skill_decision": omitted,
                    "resolution_totals": receipt["totals"],
                },
                "receipt": receipt,
                "pull": pull,
                "error_envelopes": [
                    SkillPullError(code, "fixture").envelope()
                    for code in error_codes
                ],
            }

        golden = json.loads(
            (GOLDEN_DIR / "closed_records.json").read_text(encoding="utf-8")
        )
        self.assertEqual(_normalize_closed_record(actual), golden)

    def test_success_and_drift_failure_do_not_mutate_protected_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            before = _snapshot_tree(fixture.root)
            pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)
            after_success = _snapshot_tree(fixture.root)
            self.assertEqual(before, after_success)

            original = fixture.reference.read_bytes()

            def plant_drift(_receipt: dict[str, object]) -> None:
                fixture.reference.write_text("drift\n", encoding="utf-8")

            with self.assertRaises(SkillPullError):
                pull_host_skill(
                    fixture.model,
                    "sbp",
                    cwd=fixture.repo,
                    after_resolve=plant_drift,
                )
            fixture.reference.write_bytes(original)
            after_failure = _snapshot_tree(fixture.root)

        self.assertEqual(before, after_failure)

    def test_error_envelope_is_closed_and_message_is_bounded(self) -> None:
        error = SkillPullError("SKILL_SOURCE_MISSING", "é" * 400).envelope()
        self.assertEqual(
            set(error),
            {"ok", "schema_version", "error_code", "message", "retryable"},
        )
        self.assertLessEqual(len(error["message"].encode("utf-8")), 512)
        self.assertEqual(json.loads(json.dumps(error)), error)

    def test_cli_parser_and_handler_emit_one_pull_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = HostSkillFixture(Path(tmpdir))
            args = CLI._build_parser().parse_args(  # noqa: SLF001
                ["skill", "pull", "sbp", "--cwd", str(fixture.repo), "--format", "json"]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = CLI._handle_skill(args, ROOT_DIR, fixture.model, "docker")  # noqa: SLF001

        self.assertEqual(exit_code, CLI.EXIT_OK)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], "skill-pull-result/v1")
        self.assertEqual(payload["name"], "sbp")
        self.assertEqual(output.getvalue().count('"schema_version"'), 1)

    def test_resolve_and_pull_reject_hidden_profile_and_client_choices(self) -> None:
        parser = CLI._build_parser()  # noqa: SLF001
        commands = (["skill", "resolve"], ["skill", "pull", "sbp"])
        for command in commands:
            for flag, value in (("--profile", "memory"), ("--client", "private")):
                with self.subTest(command=command, flag=flag):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as raised:
                            parser.parse_args([*command, flag, value])
                    self.assertNotEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
