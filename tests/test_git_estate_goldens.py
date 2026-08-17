"""Golden-output tests for ``sbp git`` -- byte-stable tty + JSON envelopes.

A FIXED fixture estate (one repo per interesting class: clean clone, dirty
staged+unstaged+untracked, ahead, aged stashes (pinned committer dates),
mid-op merge, no-remote, a clean repo with an unpushed non-HEAD branch, an
unregistered repo, a stale registry entry, and an ignore-rule hit) is built
from real ``git init`` repos inside a TemporaryDirectory, scanned once via
``git_estate.build_report``, and the result is pinned against goldens in
``tests/goldens/``:

* ``git_estate_report.json``       the normalized ``sbp-git/v1`` envelope
* ``git_estate_report.txt``        the tty rendering (plain, as when piped)
* ``git_estate_report_color.txt``  the tty rendering with ANSI band colors

Normalization rules (what makes the goldens commit-able)
---------------------------------------------------------
* every occurrence of the TemporaryDirectory prefix in any string is replaced
  with the stable placeholder ``/GOLDEN_TMP`` (covers repo paths, roots, fix
  commands, and the registry path inside fix strings);
* ``generated_at`` is replaced with the fixed ``1970-01-01T00:00:00+00:00``;
* ``stash_newest`` / ``stash_oldest`` (wall-clock committer timestamps) are
  replaced with placeholders pinned exactly 3 and 40 days BEFORE the
  ``generated_at`` placeholder, so the renderer's relative ages come out as
  a stable ``(newest 3d, oldest 40d)`` on every machine;
* ``elapsed_seconds`` is replaced with ``0.0``;
* git-version-dependent strings are pinned at the fixture level:
  ``init.defaultBranch=main`` via a hermetic ``GIT_CONFIG_GLOBAL`` (plus
  ``GIT_CONFIG_NOSYSTEM=1``), so branch/upstream names never depend on the
  machine's git configuration.

The text goldens are rendered FROM the normalized report, so table column
widths are computed over placeholder paths and stay identical everywhere.

Regenerating the goldens intentionally
--------------------------------------
If you *meant* to change the envelope or the renderer:

    UPDATE_GOLDENS=1 python3 -m unittest tests.test_git_estate_goldens

then review the resulting diff before committing. The same run re-asserts
against the freshly written files.
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
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate  # noqa: E402

from tests import helpers  # noqa: E402

GOLDENS_DIR = ROOT / "tests" / "goldens"
UPDATE_ENV = "UPDATE_GOLDENS"

#: Stable stand-in for the TemporaryDirectory prefix in every golden string.
PATH_PLACEHOLDER = "/GOLDEN_TMP"
#: Stable stand-in for the wall-clock ``generated_at`` timestamp.
GENERATED_AT_PLACEHOLDER = "1970-01-01T00:00:00+00:00"
#: Stash-age placeholders: exactly 3d / 40d before GENERATED_AT_PLACEHOLDER,
#: so the tty rendering (relative ages vs generated_at) is byte-stable.
STASH_NEWEST_PLACEHOLDER = "1969-12-29T00:00:00+00:00"
STASH_OLDEST_PLACEHOLDER = "1969-11-22T00:00:00+00:00"

# Same faithful registry_doctor.py stand-in as tests/test_git_estate.py: the
# three entry points git_estate loads, same rule semantics, JSON body instead
# of PyYAML so the fixture stays hermetic. Copied (not imported) so this file
# owns everything it depends on.
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
        repos = []
        for item in payload.get("repos") or []:
            item = dict(item)
            item["path"] = normalize_path(item["path"])
            repos.append(item)
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
            "repos": repos,
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


def _normalize_report(report: dict[str, Any], tmp_root: str) -> dict[str, Any]:
    """Deep-copy ``report`` with every machine-dependent value replaced.

    Path prefix -> :data:`PATH_PLACEHOLDER`, ``generated_at`` ->
    :data:`GENERATED_AT_PLACEHOLDER`, non-null ``stash_newest`` /
    ``stash_oldest`` -> the fixed 3d/40d-before-generated_at placeholders,
    ``elapsed_seconds`` -> ``0.0``.
    """

    def swap(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(tmp_root, PATH_PLACEHOLDER)
        if isinstance(value, list):
            return [swap(item) for item in value]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if key == "stash_newest" and isinstance(item, str):
                    out[key] = STASH_NEWEST_PLACEHOLDER
                elif key == "stash_oldest" and isinstance(item, str):
                    out[key] = STASH_OLDEST_PLACEHOLDER
                else:
                    out[key] = swap(item)
            return out
        return value

    normalized = swap(report)
    normalized["generated_at"] = GENERATED_AT_PLACEHOLDER
    normalized["elapsed_seconds"] = 0.0
    return normalized


class GitEstateGoldenTests(unittest.TestCase):
    """Pin the sbp-git/v1 envelope and the tty rendering byte-for-byte."""

    maxDiff = None
    _regen = bool(os.environ.get(UPDATE_ENV))

    # ------------------------------------------------------------------ #
    # Fixture estate (built once; the scan is read-only, so tests share it)
    # ------------------------------------------------------------------ #

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="git-estate-golden-")
        cls.addClassCleanup(cls._tmpdir.cleanup)
        cls.tmp = Path(cls._tmpdir.name).resolve()
        cls.estate = cls.tmp / "estate"
        cls.estate.mkdir()
        cls.origins = cls.tmp / "origins"
        cls.origins.mkdir()
        cls.config_root = cls.tmp / "config"

        gitconfig = cls.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            "\temail = fixture@example.invalid\n"
            "\tname = Git Estate Golden Fixture\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[commit]\n"
            "\tgpgsign = false\n",
            encoding="utf-8",
        )
        env_patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "SKILLBOX_CONFIG_ROOT": str(cls.config_root),
                # Hermetic joins: the goldens pin the store-less/guard-less
                # envelope, so a real receipts store or reconcile-skill
                # checkout on the host must never leak into the scan.
                **helpers.hermetic_join_env(cls.tmp),
            },
        )
        env_patcher.start()
        cls.addClassCleanup(env_patcher.stop)

        cls._build_estate()
        cls.report = cls._scan()
        cls.normalized = _normalize_report(cls.report, str(cls.tmp))

    # -- git plumbing ---------------------------------------------------- #

    @classmethod
    def _git(cls, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    @classmethod
    def _make_repo(cls, name: str, *, parent: Path | None = None) -> Path:
        repo = (parent or cls.estate) / name
        repo.mkdir(parents=True)
        cls._git(repo, "init", "-q", "-b", "main")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        cls._git(repo, "add", "tracked.txt")
        cls._git(repo, "commit", "-q", "-m", "base")
        return repo

    @classmethod
    def _make_clone(cls, name: str) -> Path:
        origin = cls._make_repo(f"{name}-origin", parent=cls.origins)
        clone = cls.estate / name
        cls._git(cls.tmp, "clone", "-q", f"file://{origin}", str(clone))
        return clone

    # -- the fixed estate -------------------------------------------------- #

    @classmethod
    def _build_estate(cls) -> None:
        # a-clean: pristine clone with an upstream -> clean-current (folded).
        a_clean = cls._make_clone("a-clean")

        # b-dirty: clone with one staged, one unstaged, one untracked entry.
        b_dirty = cls._make_clone("b-dirty")
        (b_dirty / "staged.txt").write_text("staged\n", encoding="utf-8")
        cls._git(b_dirty, "add", "staged.txt")
        (b_dirty / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (b_dirty / "loose.txt").write_text("loose\n", encoding="utf-8")
        cls.b_dirty = b_dirty

        # c-ahead: clone one local commit ahead of its upstream.
        c_ahead = cls._make_clone("c-ahead")
        (c_ahead / "local.txt").write_text("local\n", encoding="utf-8")
        cls._git(c_ahead, "add", "local.txt")
        cls._git(c_ahead, "commit", "-q", "-m", "local work")

        # d-stash: clone with two AGED stashes and a clean tree -> stash-only
        # band. Committer dates are pinned via GIT_COMMITTER_DATE (stash
        # timestamps come from the stash commit); the exact values are
        # irrelevant because the normalizer replaces them with the fixed
        # 3d/40d-before-generated_at placeholders.
        d_stash = cls._make_clone("d-stash")
        for date, content in (
            ("2026-01-01T00:00:00+00:00", "old stash"),
            ("2026-02-01T00:00:00+00:00", "new stash"),
        ):
            (d_stash / "tracked.txt").write_text(f"{content}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": date}):
                cls._git(d_stash, "stash", "push", "-q", "-m", content)

        # e-midop: merge conflict in flight (mid-op + dirty + no-remote).
        e_midop = cls._make_repo("e-midop")
        cls._git(e_midop, "checkout", "-q", "-b", "feature")
        (e_midop / "tracked.txt").write_text("feature\n", encoding="utf-8")
        cls._git(e_midop, "commit", "-q", "-am", "feature change")
        cls._git(e_midop, "checkout", "-q", "main")
        (e_midop / "tracked.txt").write_text("mainline\n", encoding="utf-8")
        cls._git(e_midop, "commit", "-q", "-am", "main change")
        merge = cls._git(e_midop, "merge", "feature", check=False)
        if merge.returncode == 0:
            raise AssertionError("fixture merge should conflict")

        # f-noremote: local-only repo, no upstream configured.
        f_noremote = cls._make_repo("f-noremote")

        # g-unregistered: scanned repo absent from the registry.
        cls._make_repo("g-unregistered")

        # i-unpushed: clean-current clone whose work is parked on a non-HEAD
        # branch with no upstream -- the silent-loss class. The row stays
        # visible (never folded with the clean rows) and carries the
        # [+1 unpushed branch] marker + branch -vv fix line.
        i_unpushed = cls._make_clone("i-unpushed")
        cls._git(i_unpushed, "checkout", "-q", "-b", "parked-work")
        (i_unpushed / "parked.txt").write_text("parked\n", encoding="utf-8")
        cls._git(i_unpushed, "add", "parked.txt")
        cls._git(i_unpushed, "commit", "-q", "-m", "parked work")
        cls._git(i_unpushed, "checkout", "-q", "main")

        # z-ignored: repo matched by a registry ignore rule (never a row).
        z_ignored = cls._make_repo("z-ignored")
        (z_ignored / "junk.txt").write_text("junk\n", encoding="utf-8")

        # Registry: every repo but g-unregistered, plus one stale entry whose
        # checkout never exists on disk, plus the ignore rule for z-ignored.
        gone = cls.estate / "gone-checkout"
        scripts = cls.config_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "registry_doctor.py").write_text(
            _REGISTRY_DOCTOR_STANDIN, encoding="utf-8"
        )
        registry = cls.config_root / "registry"
        registry.mkdir(parents=True)
        # JSON is valid YAML: keeps the fixture hermetic (no PyYAML needed).
        (registry / "repos.yaml").write_text(
            json.dumps(
                {
                    "repos": [
                        {"id": "a-clean", "path": str(a_clean)},
                        {"id": "b-dirty", "path": str(b_dirty)},
                        {"id": "c-ahead", "path": str(c_ahead)},
                        {"id": "d-stash", "path": str(d_stash)},
                        {"id": "e-midop", "path": str(e_midop)},
                        {"id": "f-noremote", "path": str(f_noremote)},
                        {"id": "i-unpushed", "path": str(i_unpushed)},
                        {"id": "gone", "path": str(gone)},
                    ],
                    "ignore": [{"path": str(z_ignored), "reason": "fixture"}],
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def _scan(cls) -> dict[str, Any]:
        return git_estate.build_report(
            roots=[str(cls.estate)], depth=2, cwd=str(cls.b_dirty)
        )

    # ------------------------------------------------------------------ #
    # Golden plumbing
    # ------------------------------------------------------------------ #

    def _check(self, name: str, actual: str) -> None:
        path = GOLDENS_DIR / name
        if self._regen:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
        self.assertTrue(
            path.is_file(),
            f"Missing golden {path}. Regenerate with "
            f"{UPDATE_ENV}=1 python3 -m unittest tests.test_git_estate_goldens",
        )
        expected = path.read_text(encoding="utf-8")
        self.assertEqual(
            expected,
            actual,
            f"sbp git output drift detected for golden '{name}'. If "
            f"intentional, regenerate with {UPDATE_ENV}=1 and review the diff.",
        )

    # ------------------------------------------------------------------ #
    # The goldens
    # ------------------------------------------------------------------ #

    def test_json_envelope_matches_golden(self) -> None:
        actual = json.dumps(self.normalized, indent=2) + "\n"
        self._check("git_estate_report.json", actual)

    def test_tty_rendering_matches_golden(self) -> None:
        lines = git_estate.report_text_lines(self.normalized, color=False)
        self._check("git_estate_report.txt", "\n".join(lines) + "\n")

    def test_tty_rendering_color_matches_golden(self) -> None:
        lines = git_estate.report_text_lines(self.normalized, color=True)
        self._check("git_estate_report_color.txt", "\n".join(lines) + "\n")

    # ------------------------------------------------------------------ #
    # Guard rails around the goldens themselves
    # ------------------------------------------------------------------ #

    def test_normalized_report_is_stable_across_scans(self) -> None:
        """Two scans of the same estate normalize to identical payloads."""
        again = _normalize_report(self._scan(), str(self.tmp))
        self.assertEqual(self.normalized, again)

    def test_fixture_covers_the_required_estate_shapes(self) -> None:
        """The golden stays meaningful: every bead-required shape is present."""
        rows = self.normalized["repos"]
        classes = {cls_ for row in rows for cls_ in row["classes"]}
        self.assertLessEqual(
            {"clean-current", "dirty", "ahead", "stash", "mid-op", "no-remote"},
            classes,
        )
        self.assertIn("merge", {row["mid_op"] for row in rows if row["mid_op"]})
        # Dirty fixture carries all three tree-state kinds at once.
        dirty = next(row for row in rows if row["path"].endswith("b-dirty"))
        self.assertEqual(
            (dirty["staged"], dirty["unstaged"], dirty["untracked"]), (1, 1, 1)
        )
        # Stash-aged fixture: two stashes whose timestamps normalized to the
        # fixed 3d/40d-before-generated_at placeholders.
        stash = next(row for row in rows if row["path"].endswith("d-stash"))
        self.assertEqual(stash["stash_count"], 2)
        self.assertEqual(stash["stash_newest"], STASH_NEWEST_PLACEHOLDER)
        self.assertEqual(stash["stash_oldest"], STASH_OLDEST_PLACEHOLDER)
        # Unpushed-branch fixture: clean HEAD, work parked on a non-HEAD
        # branch with no upstream (the silent-loss class).
        unpushed = next(row for row in rows if row["path"].endswith("i-unpushed"))
        self.assertEqual(unpushed["risk_band"], "clean")
        self.assertEqual(
            unpushed["unpushed_branches"], [{"name": "parked-work", "ahead": 1}]
        )
        self.assertEqual(self.normalized["ignored_count"], 1)
        self.assertEqual(
            [entry["id"] for entry in self.normalized["stale_registered"]],
            ["gone"],
        )
        self.assertEqual(
            self.normalized["registration_summary"],
            {"registered": 7, "unregistered": 1, "unknown": 0, "stale_registered": 1},
        )
        # Placeholder discipline: no tmpdir path and no raw fixture stash
        # timestamp survives normalization.
        blob = json.dumps(self.normalized)
        self.assertNotIn(str(self.tmp), blob)
        self.assertNotIn("2026-01-01", blob)
        self.assertNotIn("2026-02-01", blob)
        self.assertIn(PATH_PLACEHOLDER, blob)


if __name__ == "__main__":
    unittest.main()
