from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.skill_pull import (  # noqa: E402
    SkillPullError,
    canonical_json_bytes,
    pull_host_skill,
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tree_metadata(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.as_posix()):
        metadata = path.lstat()
        kind = (
            "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
            if stat.S_ISREG(metadata.st_mode)
            else "other"
        )
        rows.append(
            {
                "path": "." if path == root else path.relative_to(root).as_posix(),
                "type": kind,
                "mode": stat.S_IMODE(metadata.st_mode),
                "link_target": os.readlink(path) if kind == "symlink" else None,
                "file_sha256": sha256_bytes(path.read_bytes()) if kind == "file" else None,
            }
        )
    return rows


class AcceptanceFixture:
    def __init__(
        self,
        root: Path,
        *,
        skill_count: int = 1,
        canonical_sbp: Path | None = None,
        minimal_extra_skills: bool = False,
    ) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "proof@skillbox.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Skillbox Proof"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True)

        self.config = root / "config"
        (self.config / "clients").mkdir(parents=True)
        self.global_policy = self.config / "skill-scope.yaml"
        self.repo_override = self.repo / ".skillbox" / "skill-overrides.yaml"
        self.repo_override.parent.mkdir()
        self.repo_override.write_text(
            "version: 1\npin_on: []\npin_off: []\nopt_out_global: []\n",
            encoding="utf-8",
        )

        self.skill_roots = [root / "sources-a", root / "sources-b"]
        for source_root in self.skill_roots:
            source_root.mkdir()
        declared_roots = [*self.skill_roots]
        if canonical_sbp is not None:
            declared_roots.append(canonical_sbp.parent)
        self.global_policy.write_text(
            "policy_epoch: 7\nskill_source_roots:\n"
            + "".join(f"  - {source_root}\n" for source_root in declared_roots)
            + "rules: []\n",
            encoding="utf-8",
        )

        self.claude_root = root / "agent-homes" / "claude" / "skills"
        self.codex_root = root / "agent-homes" / "codex" / "skills"
        self.claude_root.mkdir(parents=True)
        self.codex_root.mkdir(parents=True)
        (self.claude_root / "fixture-link").symlink_to(self.skill_roots[0])
        (self.codex_root / "marker.txt").write_text("codex root\n", encoding="utf-8")

        self.state_root = root / "state"
        self.cache_paths = [self.state_root / "cache", self.state_root / "resolver"]
        for path in self.cache_paths:
            path.mkdir(parents=True)
            (path / "sentinel").write_text("unchanged\n", encoding="utf-8")

        occurrences: list[dict[str, Any]] = []
        if canonical_sbp is None:
            self.sbp_source = self._add_skill("sbp", self.skill_roots[0], occurrences)
        else:
            self.sbp_source = canonical_sbp.resolve(strict=True)
            self._add_occurrence("sbp", self.sbp_source, occurrences)
        self.reference = self.sbp_source / "references" / "model.md"
        for index in range(1, skill_count):
            source_root = self.skill_roots[index % len(self.skill_roots)]
            self._add_skill(
                f"fixture-skill-{index:04d}",
                source_root,
                occurrences,
                minimal=minimal_extra_skills,
            )

        self.model = {
            "repos": [{"id": "fixture-host", "host_path": str(self.repo)}],
            "env": {
                "SKILLBOX_CLIENTS_HOST_ROOT": str(self.config / "clients"),
                "SKILLBOX_STATE_ROOT": str(self.state_root),
            },
            "active_clients": [],
            "active_profiles": [],
            "clients": [],
            "skills": [],
            "_skill_visibility_simulation": {
                "repo_path": str(self.repo),
                "installed_occurrences": occurrences,
            },
        }
        self.constructed_skill_names = {
            str(row["name"]) for row in occurrences
        }

    @staticmethod
    def _add_skill(
        name: str,
        source_root: Path,
        occurrences: list[dict[str, Any]],
        *,
        minimal: bool = False,
    ) -> Path:
        skill = source_root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n\nFixture instructions.\n", encoding="utf-8")
        if not minimal or name == "sbp":
            (skill / "references").mkdir()
            (skill / "references" / "model.md").write_text("model v1\n", encoding="utf-8")
        AcceptanceFixture._add_occurrence(name, skill, occurrences)
        return skill

    @staticmethod
    def _add_occurrence(
        name: str,
        skill: Path,
        occurrences: list[dict[str, Any]],
    ) -> None:
        occurrences.append(
            {
                "name": name,
                "availability": "installed",
                "layer": "what-if:planned-project",
                "layer_label": "fixture project",
                "layer_rank": 100,
                "scope": "installed",
                "source_kind": "directory",
                "source": str(skill),
                "source_bucket": "skills-private" if name == "sbp" else "host-fixture",
                "path": str(skill),
                "state": "ok",
            }
        )

    def add_performance_debris(self) -> dict[str, int]:
        occurrences = self.model["_skill_visibility_simulation"]["installed_occurrences"]
        self._add_skill("fixture-skill-0001", self.skill_roots[0], occurrences, minimal=True)
        broken = self.skill_roots[0] / "broken-link-skill"
        broken.symlink_to(self.root / "missing-skill")
        occurrences.append(
            {
                "name": "broken-link-skill",
                "availability": "installed",
                "layer": "what-if:planned-project",
                "layer_label": "fixture project",
                "layer_rank": 100,
                "scope": "installed",
                "source_kind": "symlink",
                "source": str(broken),
                "source_bucket": "host-fixture",
                "path": str(broken),
                "state": "broken",
            }
        )
        retired = self.skill_roots[1] / "retired-debris"
        retired.mkdir()
        (retired / "SKILL.md").write_text(
            "---\nlifecycle: retired\n---\n# retired debris\n",
            encoding="utf-8",
        )
        self._add_occurrence("retired-debris", retired, occurrences)
        return {
            "duplicate_name_occurrences": 2,
            "broken_link_entries": 1,
            "lifecycle_debris_entries": 1,
        }

    def add_local_fixture_skill(self, name: str) -> Path:
        occurrences = self.model["_skill_visibility_simulation"]["installed_occurrences"]
        source = self._add_skill(
            name,
            self.skill_roots[0],
            occurrences,
            minimal=True,
        )
        self.constructed_skill_names.add(name)
        return source

    def opt_out_unrelated_os_home_skills(self) -> list[str]:
        os_home_names: set[str] = set()
        for surface in ("claude", "codex"):
            skill_root = Path.home() / f".{surface}" / "skills"
            if not skill_root.is_dir():
                continue
            try:
                os_home_names.update(
                    entry.name
                    for entry in skill_root.iterdir()
                    if not entry.name.startswith(".")
                )
            except OSError:
                continue
        excluded = {"sbp", "smart", *self.constructed_skill_names}
        opt_out = sorted(os_home_names - excluded)
        lines = [
            "version: 1",
            "pin_on: []",
            "pin_off: []",
            "opt_out_global:",
            *[f"  - {name}" for name in opt_out],
            "",
        ]
        self.repo_override.write_text("\n".join(lines), encoding="utf-8")
        return opt_out

    def snapshot(self) -> bytes:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        payload = {
            "git_status_porcelain_v1": status,
            "policy_bytes": {
                "global": self.global_policy.read_bytes().hex(),
                "repo": self.repo_override.read_bytes().hex(),
            },
            "agent_skill_roots": {
                "claude": tree_metadata(self.claude_root),
                "codex": tree_metadata(self.codex_root),
            },
            "selected_canonical_source": {
                "logical_source_id": "skills-private",
                "metadata": tree_metadata(self.sbp_source),
            },
            "process_independent_paths": {
                path.name: tree_metadata(path) for path in self.cache_paths
            },
        }
        return canonical_json_bytes(payload)


def run_pull_document(
    fixture: AcceptanceFixture,
    *,
    plant_drift: bool = False,
) -> tuple[int, str]:
    try:
        result = pull_host_skill(
            fixture.model,
            "sbp",
            cwd=fixture.repo,
            after_resolve=(
                lambda _receipt: fixture.reference.write_text("model v2\n", encoding="utf-8")
            )
            if plant_drift
            else None,
        )
        return 0, json.dumps(result, sort_keys=True, separators=(",", ":"))
    except SkillPullError as exc:
        return 2, json.dumps(exc.envelope(), sort_keys=True, separators=(",", ":"))


class SkillPullAcceptanceTests(unittest.TestCase):
    def test_success_and_non_entry_drift_preserve_protected_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = AcceptanceFixture(Path(tmpdir))
            capture_dir = Path(tmpdir) / "captured-output"
            capture_dir.mkdir()

            success_before = fixture.snapshot()
            success_code, success_output = run_pull_document(fixture)
            (capture_dir / "success.json").write_text(success_output, encoding="utf-8")
            success_after = fixture.snapshot()
            self.assertEqual(success_code, 0)
            success_payload = json.loads(success_output)
            self.assertEqual(success_payload["name"], "sbp")
            self.assertIn("entry_text", success_payload)
            self.assertEqual(success_before, success_after)

            failure_before_holder: list[bytes] = []
            original_reference = fixture.reference.read_bytes()

            def plant_and_snapshot(_receipt: dict[str, Any]) -> None:
                fixture.reference.write_text("model v2\n", encoding="utf-8")
                failure_before_holder.append(fixture.snapshot())

            try:
                result = pull_host_skill(
                    fixture.model,
                    "sbp",
                    cwd=fixture.repo,
                    after_resolve=plant_and_snapshot,
                )
                failure_code, failure_output = 0, json.dumps(result)
            except SkillPullError as exc:
                failure_code = 2
                failure_output = json.dumps(exc.envelope(), sort_keys=True, separators=(",", ":"))
            failure_before = failure_before_holder[0]
            failure_after = fixture.snapshot()
            (capture_dir / "failure.json").write_text(failure_output, encoding="utf-8")

            self.assertNotEqual(failure_code, 0)
            self.assertEqual(len([line for line in failure_output.splitlines() if line]), 1)
            failure_payload = json.loads(failure_output)
            self.assertEqual(failure_payload["error_code"], "SKILL_TREE_DRIFT")
            self.assertTrue(failure_payload["retryable"])
            self.assertNotIn("entry_text", failure_payload)
            self.assertEqual(failure_before, failure_after)
            fixture.reference.write_bytes(original_reference)

            hashes = {
                "success_before": sha256_bytes(success_before),
                "success_after": sha256_bytes(success_after),
                "drift_before": sha256_bytes(failure_before),
                "drift_after": sha256_bytes(failure_after),
            }
            print("WG003_MUTATION_PROOF_JSON " + json.dumps(hashes, sort_keys=True))

    def test_snapshot_has_every_released_protected_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = json.loads(AcceptanceFixture(Path(tmpdir)).snapshot())

        self.assertEqual(
            set(payload),
            {
                "git_status_porcelain_v1",
                "policy_bytes",
                "agent_skill_roots",
                "selected_canonical_source",
                "process_independent_paths",
            },
        )
        self.assertEqual(set(payload["agent_skill_roots"]), {"claude", "codex"})
        metadata = payload["selected_canonical_source"]["metadata"]
        self.assertTrue(all(set(row) == {"path", "type", "mode", "link_target", "file_sha256"} for row in metadata))
        self.assertTrue(any(row["path"] == "SKILL.md" and row["file_sha256"] for row in metadata))

    def test_fixture_override_opts_out_only_unrelated_os_home_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = AcceptanceFixture(Path(tmpdir), skill_count=3)
            opted_out = fixture.opt_out_unrelated_os_home_skills()
            override_text = fixture.repo_override.read_text(encoding="utf-8")

        self.assertNotIn("sbp", opted_out)
        self.assertNotIn("smart", opted_out)
        self.assertTrue(fixture.constructed_skill_names.isdisjoint(opted_out))
        self.assertIn("opt_out_global:", override_text)
        for name in opted_out:
            self.assertIn(f"  - {name}\n", override_text)


if __name__ == "__main__":
    unittest.main()
