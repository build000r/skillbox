"""Tests for runtime_manager.git_estate -- the ``sbp git`` presentation layer.

Hermetic throughout: fixture repos are real ``git init`` repos inside a
TemporaryDirectory with pinned git config; the registry ignore fixture is a
temp config root (pointed at via ``SKILLBOX_CONFIG_ROOT``) carrying a
JSON-bodied ``registry/repos.yaml`` (JSON is valid YAML) and a stand-in
``scripts/registry_doctor.py`` exposing the same three functions git_estate
loads from the real skillbox-config helper. Wrapper-level alias tests
subprocess the real ``scripts/sbp`` with ``--root`` pointed at the temp
estate so the suite never scans the operator's ~/repos.
"""

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

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate  # noqa: E402
from runtime_manager.git_inventory import GitRepoRecord  # noqa: E402

SBP = ROOT / "scripts" / "sbp"

# Faithful stand-in for skillbox-config/scripts/registry_doctor.py: same three
# entry points, same path/pattern rule semantics, JSON body instead of PyYAML.
_REGISTRY_DOCTOR_STANDIN = textwrap.dedent(
    '''
    import fnmatch
    import json
    import os
    from pathlib import Path


    def normalize_path(value):
        expanded = os.path.expandvars(os.path.expanduser(str(value)))
        return os.path.abspath(os.path.normpath(expanded))


    def load_registry(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))


    def normalize_registry(payload, root_overrides):
        ignore = []
        for item in payload.get("ignore") or []:
            item = dict(item)
            if "path" in item:
                item["path"] = normalize_path(item["path"])
            if "pattern" in item:
                item["pattern"] = normalize_path(item["pattern"])
            ignore.append(item)
        return {
            "roots": [],
            "max_depth": None,
            "prune_dir_names": set(),
            "repos": [],
            "ignore": ignore,
        }


    def _is_same_or_child(path, parent):
        try:
            Path(path).relative_to(parent)
            return True
        except ValueError:
            return False


    def matching_ignore(path, ignore_rules):
        for rule in ignore_rules:
            if rule.get("path") and _is_same_or_child(path, rule["path"]):
                return rule
            if rule.get("pattern") and fnmatch.fnmatch(path, rule["pattern"]):
                return rule
        return None
    '''
)


def _record(path: str = "/repo", **overrides) -> GitRepoRecord:
    defaults = dict(
        path=path,
        classes=frozenset({"clean-current"}),
        primary_class="clean-current",
        branch="main",
        upstream="origin/main",
    )
    defaults.update(overrides)
    return GitRepoRecord(**defaults)


class RiskSortTests(unittest.TestCase):
    def _band_fixture(self) -> list[GitRepoRecord]:
        return [
            _record("/r/blocked", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="boom"),
            _record("/r/midop", classes=frozenset({"mid-op", "dirty"}), primary_class="mid-op", mid_op="merge", staged=1),
            _record("/r/diverged", classes=frozenset({"ahead", "behind", "diverged-clean"}), primary_class="diverged-clean", ahead=1, behind=1),
            _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=2),
            _record("/r/dirty-behind", classes=frozenset({"dirty", "behind"}), primary_class="dirty", behind=1, unstaged=1),
            _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1),
            _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=3),
            _record("/r/noremote", classes=frozenset({"no-remote"}), primary_class="no-remote", upstream=None),
            _record("/r/stash", classes=frozenset({"stash"}), primary_class="clean-current", stash_count=2),
            _record("/r/clean", classes=frozenset({"clean-current"})),
        ]

    def test_risk_sort_band_order(self) -> None:
        records = self._band_fixture()
        shuffled = list(reversed(records))
        ordered = git_estate.risk_sorted(shuffled)
        bands = [git_estate.RISK_BAND_NAMES[git_estate.risk_band(r)] for r in ordered]
        self.assertEqual(
            bands,
            [
                "blocked",
                "mid-op",
                "diverged",
                "behind-clean",
                "dirty-behind",
                "dirty",
                "ahead",
                "no-remote",
                "stash-only",
                "clean",
            ],
        )
        self.assertEqual(
            [r.path for r in ordered],
            [
                "/r/blocked",
                "/r/midop",
                "/r/diverged",
                "/r/behind",
                "/r/dirty-behind",
                "/r/dirty",
                "/r/ahead",
                "/r/noremote",
                "/r/stash",
                "/r/clean",
            ],
        )

    def test_within_band_sorts_by_path(self) -> None:
        b = _record("/r/b", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        a = _record("/r/a", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        self.assertEqual([r.path for r in git_estate.risk_sorted([b, a])], ["/r/a", "/r/b"])


class FixCommandTests(unittest.TestCase):
    def test_ahead_gets_push(self) -> None:
        record = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=2)
        self.assertEqual(git_estate.fix_commands(record), ["git -C /r/ahead push"])

    def test_behind_gets_ff_only_pull(self) -> None:
        record = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=2)
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/behind pull --ff-only  # or /reconcile"],
        )

    def test_diverged_gets_reconcile_handoff_never_push_pull(self) -> None:
        record = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        fixes = git_estate.fix_commands(record)
        self.assertEqual(fixes, ["sbp doctor / reconcile skill — do not hand-merge"])
        self.assertFalse(any("push" in fix or "pull" in fix for fix in fixes))

    def test_mid_op_names_the_kind(self) -> None:
        record = _record(
            "/r/mid", classes=frozenset({"mid-op"}), primary_class="mid-op", mid_op="rebase"
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            ["git -C /r/mid status  # finish or abort the rebase"],
        )

    def test_dirty_stash_and_no_remote(self) -> None:
        record = _record(
            "/r/mix",
            classes=frozenset({"dirty", "stash", "no-remote"}),
            primary_class="dirty",
            upstream=None,
            unstaged=1,
            stash_count=1,
        )
        self.assertEqual(
            git_estate.fix_commands(record),
            [
                "git -C /r/mix add -p && git -C /r/mix commit",
                "git -C /r/mix stash list  # git-stash-janitor pass",
                "add a remote or register intent",
            ],
        )

    def test_blocked_carries_error_and_nothing_else(self) -> None:
        record = _record(
            "/r/blk", classes=frozenset({"blocked"}), primary_class="blocked", upstream=None, error="probe died"
        )
        self.assertEqual(git_estate.fix_commands(record), ["inspect: probe died"])

    def test_dirty_and_ahead_carry_both_fixes(self) -> None:
        record = _record(
            "/r/da", classes=frozenset({"dirty", "ahead"}), primary_class="dirty", ahead=1, staged=1
        )
        fixes = git_estate.fix_commands(record)
        self.assertIn("git -C /r/da push", fixes)
        self.assertIn("git -C /r/da add -p && git -C /r/da commit", fixes)

    def test_clean_has_no_fix(self) -> None:
        self.assertEqual(git_estate.fix_commands(_record("/r/clean")), [])


class OnlyFilterTests(unittest.TestCase):
    def test_unknown_token_raises_with_vocabulary(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            git_estate.parse_only(["bogus"])
        message = str(ctx.exception)
        for token in git_estate.FILTER_CLASSES + git_estate.RESERVED_FILTER_CLASSES:
            self.assertIn(token, message)

    def test_reserved_tokens_parse_as_reserved(self) -> None:
        active, reserved = git_estate.parse_only(["unregistered,stale-registered"])
        self.assertEqual(active, ())
        self.assertEqual(reserved, ("unregistered", "stale-registered"))

    def test_comma_and_repeat_forms_merge(self) -> None:
        active, reserved = git_estate.parse_only(["dirty,stash", "dirty", "mid-op"])
        self.assertEqual(active, ("dirty", "stash", "mid-op"))
        self.assertEqual(reserved, ())

    def test_behind_matches_behind_clean_and_diverged_clean(self) -> None:
        behind = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=1)
        diverged = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        ahead = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=1)
        kept = git_estate._apply_only([behind, diverged, ahead], ("behind",))
        self.assertEqual([r.path for r in kept], ["/r/behind", "/r/div"])

    def test_ahead_matches_ahead_clean_and_diverged_clean(self) -> None:
        behind = _record("/r/behind", classes=frozenset({"behind"}), primary_class="behind-clean", behind=1)
        diverged = _record(
            "/r/div",
            classes=frozenset({"ahead", "behind", "diverged-clean"}),
            primary_class="diverged-clean",
            ahead=1,
            behind=1,
        )
        ahead = _record("/r/ahead", classes=frozenset({"ahead"}), primary_class="ahead-clean", ahead=1)
        kept = git_estate._apply_only([behind, diverged, ahead], ("ahead",))
        self.assertEqual([r.path for r in kept], ["/r/div", "/r/ahead"])

    def test_stash_means_count_at_least_one(self) -> None:
        stashed = _record("/r/stash", classes=frozenset({"stash"}), primary_class="clean-current", stash_count=1)
        clean = _record("/r/clean")
        kept = git_estate._apply_only([stashed, clean], ("stash",))
        self.assertEqual([r.path for r in kept], ["/r/stash"])

    def test_multi_class_filter_is_a_union(self) -> None:
        dirty = _record("/r/dirty", classes=frozenset({"dirty"}), primary_class="dirty", untracked=1)
        midop = _record("/r/mid", classes=frozenset({"mid-op"}), primary_class="mid-op", mid_op="merge")
        clean = _record("/r/clean")
        kept = git_estate._apply_only([dirty, midop, clean], ("dirty", "mid-op"))
        self.assertEqual({r.path for r in kept}, {"/r/dirty", "/r/mid"})


class GitEstateFixtureCase(unittest.TestCase):
    """Temp estate + temp config root, hermetic git configuration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-estate-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        self.origins = self.tmp / "origins"
        self.origins.mkdir()
        self.config_root = self.tmp / "config"

        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            "\temail = fixture@example.invalid\n"
            "\tname = Git Estate Fixture\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[commit]\n"
            "\tgpgsign = false\n",
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "SKILLBOX_CONFIG_ROOT": str(self.config_root),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def make_repo(self, name: str, *, parent: Path | None = None) -> Path:
        repo = (parent or self.estate) / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "tracked.txt")
        self.git(repo, "commit", "-q", "-m", "base")
        return repo

    def make_ahead_clone(self, name: str) -> Path:
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        return clone

    def make_clean_clone(self, name: str) -> Path:
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        return clone

    def write_config_fixture(self, ignore: list[dict] | None = None) -> None:
        scripts = self.config_root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "registry_doctor.py").write_text(_REGISTRY_DOCTOR_STANDIN, encoding="utf-8")
        registry = self.config_root / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        # JSON is valid YAML: keeps the fixture hermetic (no PyYAML needed).
        (registry / "repos.yaml").write_text(
            json.dumps({"repos": [], "ignore": ignore or []}), encoding="utf-8"
        )


class BuildReportTests(GitEstateFixtureCase):
    def test_envelope_shape_order_ignore_rules_and_fixes(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        ahead = self.make_ahead_clone("b-ahead")
        clean = self.make_clean_clone("c-clean")
        ignored = self.make_repo("z-ignored")
        (ignored / "junk.txt").write_text("junk\n", encoding="utf-8")
        self.write_config_fixture(ignore=[{"path": str(ignored), "reason": "fixture"}])

        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=str(dirty)
        )

        self.assertEqual(report["schema"], "sbp-git/v1")
        for key in (
            "generated_at",
            "roots",
            "ignored_count",
            "repos",
            "summary",
            "elapsed_seconds",
            "repo_count",
        ):
            self.assertIn(key, report)
        self.assertIsInstance(report["elapsed_seconds"], float)
        self.assertEqual(report["roots"], [str(self.estate)])
        self.assertEqual(report["ignored_count"], 1)
        self.assertTrue(report["registry_applied"])
        self.assertEqual(report["notes"], [])

        paths = [row["path"] for row in report["repos"]]
        self.assertEqual(paths, [str(dirty), str(ahead), str(clean)])
        self.assertNotIn(str(ignored), paths)
        self.assertEqual(report["repo_count"], 3)
        self.assertEqual(
            report["summary"],
            {"ahead-clean": 1, "clean-current": 1, "dirty": 1},
        )

        rows = {row["path"]: row for row in report["repos"]}
        self.assertEqual(rows[str(dirty)]["risk_band"], "dirty")
        # The local-only fixture repo has no upstream, so the dirty row also
        # carries the no-remote handoff.
        self.assertEqual(
            rows[str(dirty)]["fix"],
            [
                f"git -C {dirty} add -p && git -C {dirty} commit",
                "add a remote or register intent",
            ],
        )
        self.assertEqual(rows[str(ahead)]["fix"], [f"git -C {ahead} push"])
        self.assertEqual(rows[str(clean)]["fix"], [])

        # cwd detail probes the enclosing repo root, even from a subdirectory.
        sub = dirty / "nested"
        sub.mkdir()
        nested = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(sub))
        self.assertEqual(nested["cwd_repo"]["path"], str(dirty))
        self.assertEqual(nested["cwd_repo"]["risk_band"], "dirty")

        # Deterministic: a second scan yields the same row order.
        again = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(dirty))
        self.assertEqual([row["path"] for row in again["repos"]], paths)

    def test_registry_absent_degrades_loudly_and_unfiltered(self) -> None:
        repo = self.make_repo("solo")
        # No config fixture written: SKILLBOX_CONFIG_ROOT points at nothing.
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        self.assertFalse(report["registry_applied"])
        self.assertEqual(report["ignored_count"], 0)
        self.assertEqual([row["path"] for row in report["repos"]], [str(repo)])
        self.assertTrue(
            any(
                note.startswith("registry unavailable:") and note.endswith("showing unfiltered")
                for note in report["notes"]
            ),
            report["notes"],
        )

    def test_only_filter_and_reserved_tokens(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.make_clean_clone("b-clean")
        self.write_config_fixture()

        only_dirty = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["dirty"]
        )
        self.assertEqual([row["path"] for row in only_dirty["repos"]], [str(dirty)])
        self.assertEqual(only_dirty["filters"], ["dirty"])

        reserved = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["unregistered"]
        )
        self.assertEqual(reserved["repos"], [])
        self.assertEqual(reserved["repo_count"], 0)
        self.assertTrue(
            any(git_estate.REGISTRATION_NOTE in note for note in reserved["notes"]),
            reserved["notes"],
        )

        mixed = git_estate.build_report(
            roots=[str(self.estate)], depth=2, only=["dirty", "stale-registered"]
        )
        self.assertEqual([row["path"] for row in mixed["repos"]], [str(dirty)])
        self.assertTrue(any(git_estate.REGISTRATION_NOTE in note for note in mixed["notes"]))

        with self.assertRaises(ValueError):
            git_estate.build_report(roots=[str(self.estate)], depth=2, only=["bogus"])

    def test_cwd_outside_any_repo_yields_null_detail(self) -> None:
        self.write_config_fixture()
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(outside))
        self.assertIsNone(report["cwd_repo"])


class TextRenderingTests(GitEstateFixtureCase):
    def test_clean_rows_fold_detail_first_and_footer(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        clean = self.make_clean_clone("b-clean")
        self.write_config_fixture()

        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=str(dirty))
        lines = git_estate.report_text_lines(report, color=False)
        text = "\n".join(lines)

        # cwd detail block comes before the estate rollup.
        self.assertLess(text.index("cwd repo:"), text.index("estate:"))
        self.assertIn(f"cwd repo: {dirty}", text)
        # Clean repos are one count line, not rows.
        self.assertNotIn(str(clean), text)
        self.assertIn("1 clean-current repos", text)
        self.assertIn("0 ignored by registry rules", text)
        self.assertIn("issues:", text)
        self.assertIn("  - dirty: 1", text)
        self.assertIn("next_actions:", text)
        self.assertIn(f"  - git -C {dirty} add -p && git -C {dirty} commit", text)
        # Plain output when not a tty.
        self.assertNotIn("\033[", text)

    def test_color_only_when_requested(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        colored = "\n".join(git_estate.report_text_lines(report, color=True))
        self.assertIn("\033[", colored)

    def test_registry_unavailable_note_is_rendered(self) -> None:
        self.make_repo("solo")
        report = git_estate.build_report(roots=[str(self.estate)], depth=2)
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("registry unavailable:", text)
        self.assertNotIn("ignored by registry rules", text)


class WrapperAliasTests(GitEstateFixtureCase):
    """`sbp git` / `sbp gs` / `sbp git status` through the real wrapper."""

    def run_sbp(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SBP), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "SKILLBOX_ROOT": str(ROOT),
                "SKILLBOX_INVOKE_CWD": str(self.estate),
                "SKILLBOX_CONFIG_ROOT": str(self.config_root),
            },
        )

    def scan_args(self) -> list[str]:
        return ["--root", str(self.estate), "--depth", "2"]

    def test_git_gs_and_git_status_are_the_same_command(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()

        payloads = []
        for alias in (["git"], ["gs"], ["git", "status"]):
            result = self.run_sbp(*alias, "--json", *self.scan_args())
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], "sbp-git/v1")
            payloads.append(payload)
        self.assertEqual(
            [p["repo_count"] for p in payloads], [payloads[0]["repo_count"]] * 3
        )
        self.assertEqual(
            [row["path"] for row in payloads[0]["repos"]],
            [row["path"] for row in payloads[1]["repos"]],
        )

    def test_git_push_is_refused_never_proxied(self) -> None:
        self.write_config_fixture()
        result = self.run_sbp("git", "push")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to proxy", result.stderr)
        self.assertIn("Usage:", result.stderr)

    def test_unknown_only_class_exits_2_with_vocabulary(self) -> None:
        self.write_config_fixture()
        result = self.run_sbp("git", "--only", "bogus", *self.scan_args())
        self.assertEqual(result.returncode, 2)
        self.assertIn("valid classes:", result.stderr)
        self.assertIn("diverged-clean", result.stderr)

    def test_piped_output_is_plain_text(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        self.write_config_fixture()
        result = self.run_sbp("gs", *self.scan_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\033[", result.stdout)
        self.assertIn("estate:", result.stdout)


if __name__ == "__main__":
    unittest.main()
