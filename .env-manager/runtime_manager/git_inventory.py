"""Read-only multi-repo git estate scan (sbp's own estate-git-scan engine).

Port of the battle-tested ``repo_inventory.sh`` classification logic from the
reconcile skill into a pure-stdlib module, so ``sbp`` owns its own scan engine
with no dependency on the skills-private checkout.

Guarantees (each exercised by ``tests/test_git_inventory.py``)
--------------------------------------------------------------
**Read-only.** No probe ever runs ``git fetch`` or writes anything. Every git
subprocess runs with ``GIT_OPTIONAL_LOCKS=0`` (no index-refresh lock writes)
and the same non-interactive flags the sbp wrapper uses
(``GIT_TERMINAL_PROMPT=0``, ``GCM_INTERACTIVE=never``,
``SSH_ASKPASS_REQUIRE=never``). Ahead/behind counts are therefore relative to
the last-fetched upstream ref and may be stale -- that is the contract, not a
bug.

**Never crashes the scan.** Unreadable directories are skipped during
discovery; any probe failure (not a repo, wedged git subprocess hitting the
per-call timeout or the per-repo overall deadline, missing git binary,
unexpected exception) collapses to a ``blocked`` record carrying an error
string.

**Fast.** :func:`scan` / :func:`scan_estate` probe repositories concurrently
with a thread pool (git probes are subprocess/IO-bound, so threads suffice);
output ordering stays deterministic (sorted by path) regardless of completion
order. Each worker's probe builds its own environment dict and holds no shared
mutable state, so probes are thread-safe by construction.

**Enrichment signals (schema-additive).** Stash ages (``stash_newest`` /
``stash_oldest``, ISO8601 UTC) ride the existing ``stash list`` call via
``--format=%ct`` -- zero extra subprocesses. Unpushed non-HEAD branches
(``unpushed_branches``: work parked on a branch you forgot) cost one
``for-each-ref`` call per non-bare repo; ``%(upstream:track)`` supplies ahead
counts for branches with a live upstream, and upstream-less (or gone)
branches share ONE batched ``rev-list --parents <tips> --not --remotes``
call whose subgraph is walked in Python for exact per-branch counts. Past
:data:`BRANCH_SCAN_LIMIT` local branches the branch scan is skipped and
``branch_scan_note`` says so. None of these fields changes ``classes`` or
``primary_class``.

**Worktree-aware.** Mid-operation markers (``rebase-merge``, ``MERGE_HEAD``,
``CHERRY_PICK_HEAD``, ...) are looked up under ``git rev-parse
--absolute-git-dir`` so linked worktrees are classified against their real
per-worktree git dir -- the same trick as the shell script. Both that
per-worktree dir (``git_dir``) and the SHARED store behind it
(``common_dir``, from ``--git-common-dir``, absolute and symlink-resolved)
ride the same identity ``rev-parse`` batch -- no extra subprocess. They are
raw facts, not policy: a linked worktree has ``git_dir != common_dir``, a
main worktree has them equal, and two symlink aliases of one checkout share
both. :mod:`runtime_manager.git_estate` uses them to attribute ref-store
counts (stashes live in the shared store, so per-checkout rows would
otherwise multiply-count the same entries).

Classification
--------------
Each repo gets a *set* of applicable classes (richer than the shell's
primary-only view) plus a single risk-ranked ``primary_class`` that matches
``repo_inventory.sh``:

===============  ==============================================================
class (set)      applies when
===============  ==============================================================
``mid-op``       a rebase/merge/cherry-pick/revert/sequencer/bisect is in flight
``dirty``        any staged, unstaged, or untracked entries
``stash``        ``stash_count >= 1``
``ahead``        ahead of upstream by > 0 commits
``behind``       behind upstream by > 0 commits
``diverged-clean``  both ahead and behind while the tree is clean
``no-remote``    HEAD has no configured upstream
``clean-current``  nothing else applies
``blocked``      the probe itself failed (error string set)
===============  ==============================================================

``primary_class`` ranking (identical to the shell script): ``mid-op`` >
``dirty`` > ``stash-heavy`` (stash_count >= 5) > ``no-remote`` >
``diverged-clean`` > ``behind-clean`` > ``ahead-clean`` > ``clean-current``,
with ``blocked`` reserved for probe failure. The ``stash-heavy`` >= 5 notion
survives ONLY as this primary-class threshold; the class set uses ``stash``
from the first stash entry.

Discovery
---------
:func:`discover_repos` mirrors the shell script's ``find`` semantics: scan
each root to ``depth`` (default 3, counted in path components under the root,
where the matched entry is the ``.git`` dir *or* gitfile), prune
``node_modules``/``.venv``/``venv``/``target``/``dist``/``build``, and include
the root itself when it is a repo. One deliberate tightening over the shell:
discovery never descends into a discovered (non-root) repo's interior, so
submodule checkouts and vendored repos nested inside another repo's work tree
are not reported as estate repos of their own.

Public API
----------
``GitRepoRecord``                 frozen dataclass, ``to_dict()`` is JSON-safe.
``ScanResult``                    records + elapsed_seconds/repo_count/roots/depth.
``probe_repo(path, ...)``         one read-only probe -> one record.
``discover_repos(roots, ...)``    root/depth-injectable repo discovery.
``scan(roots, ...)``              discover + probe every repo -> list of records.
``scan_estate(roots, ...)``       same scan, returns a :class:`ScanResult`.
``primary_class_counts(records)`` summary counts by primary class.
"""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "ALL_CLASSES",
    "BRANCH_DETACHED",
    "BRANCH_SCAN_LIMIT",
    "DEFAULT_DEPTH",
    "DEFAULT_REPO_DEADLINE_S",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_WORKERS",
    "GitRepoRecord",
    "PRIMARY_CLASSES",
    "PRUNE_DIR_NAMES",
    "READ_ONLY_GIT_ENV",
    "STASH_HEAVY_THRESHOLD",
    "ScanResult",
    "UpstreamMismatch",
    "effective_ahead_behind",
    "default_scan_roots",
    "discover_repos",
    "primary_class_counts",
    "probe_repo",
    "scan",
    "scan_estate",
]

# --------------------------------------------------------------------------- #
# Contract constants
# --------------------------------------------------------------------------- #

#: Directory depth under each scan root to search for ``.git`` (find -maxdepth).
DEFAULT_DEPTH = 3

#: Per-git-subprocess timeout so a wedged repo (dead NFS mount, fsmonitor hang)
#: cannot stall the whole scan. A timeout classifies the repo as ``blocked``.
DEFAULT_TIMEOUT_S = 5.0

#: Per-repo *overall* wall-clock deadline across every git call in one probe.
#: The per-call timeout alone still allows every call in a probe to burn its
#: full 5s against a single wedged repo (network filesystem, stuck lock); this
#: caps the whole probe so such a repo becomes ``blocked`` with a reason
#: instead of dragging the scan.
DEFAULT_REPO_DEADLINE_S = 15.0

#: Default probe concurrency. Probes are subprocess-bound: each worker spends
#: nearly all its time blocked in ``subprocess.run`` waiting on a git child
#: (GIL released), so threads oversubscribe CPUs 4x to keep cores fed. The
#: floor of 8 keeps small-CPU machines parallel enough to matter; the cap of
#: 32 bounds concurrent git child processes (one per worker at a time) and
#: sits past the point of diminishing returns for fork/exec-dominated work.
DEFAULT_WORKERS = min(32, max(8, (os.cpu_count() or 4) * 4))

#: ``stash_count`` at which the *primary* class becomes ``stash-heavy``
#: (repo_inventory.sh's threshold). The class set flags ``stash`` from 1.
STASH_HEAVY_THRESHOLD = 5

#: Branch reported when HEAD is detached (matches repo_inventory.sh).
BRANCH_DETACHED = "DETACHED"

#: Local-branch count above which the unpushed-branch scan is skipped for a
#: repo (the record carries ``branch_scan_note`` instead of results). The
#: ``for-each-ref`` listing itself is one cheap call; the bound exists because
#: each branch WITHOUT an upstream costs one extra ``rev-list --count``
#: subprocess, and a 50+-branch repo would blow the estate perf budget.
BRANCH_SCAN_LIMIT = 50

#: Heavy non-repo trees pruned during discovery (matches repo_inventory.sh).
PRUNE_DIR_NAMES = frozenset(
    {"node_modules", ".venv", "venv", "target", "dist", "build"}
)

#: Every class that can appear in ``GitRepoRecord.classes``.
ALL_CLASSES = frozenset(
    {
        "dirty",
        "stash",
        "ahead",
        "behind",
        "diverged-clean",
        "mid-op",
        "no-remote",
        # A branch with no upstream inside a store that HAS a remote. Distinct
        # from no-remote on purpose: the work has somewhere to go.
        "unpublished-branch",
        "clean-current",
        "blocked",
    }
)

#: Every value ``primary_class`` can take, in descending risk order.
PRIMARY_CLASSES = (
    "blocked",
    "mid-op",
    "dirty",
    "stash-heavy",
    "no-remote",
    "unpublished-branch",
    "diverged-clean",
    "behind-clean",
    "ahead-clean",
    "clean-current",
)

#: Environment forced onto every git subprocess: read-only, non-interactive.
READ_ONLY_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS_REQUIRE": "never",
}

#: Ambient git overrides stripped so a caller's exported GIT_DIR cannot make
#: every probe silently inspect the wrong repository.
_AMBIENT_GIT_VARS = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"}
)

# Mid-operation markers inside the (worktree-resolved) git dir, in the same
# precedence order as repo_inventory.sh. ``True`` = directory, ``False`` = file.
_MID_OP_MARKERS: tuple[tuple[str, str, bool], ...] = (
    ("rebase", "rebase-merge", True),
    ("rebase", "rebase-apply", True),
    ("merge", "MERGE_HEAD", False),
    ("cherry-pick", "CHERRY_PICK_HEAD", False),
    ("revert", "REVERT_HEAD", False),
    ("sequencer", "sequencer", True),
    ("bisect", "BISECT_LOG", False),
)


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UpstreamMismatch:
    """A branch whose configured upstream is not the ref that holds its commits.

    ``ahead_vs_same_name == 0`` is the whole claim: every local commit is
    already present on ``same_name``, so the ahead/behind measured against
    ``configured`` describes a config artifact, not unpublished work.
    """

    configured: str
    same_name: str
    ahead_vs_same_name: int
    behind_vs_same_name: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "same_name": self.same_name,
            "ahead_vs_same_name": self.ahead_vs_same_name,
            "behind_vs_same_name": self.behind_vs_same_name,
        }


@dataclass(frozen=True)
class GitRepoRecord:
    """One read-only classification of a single repository.

    Enrichment fields (all additive; none affects ``classes`` or
    ``primary_class``):

    * ``stash_newest`` / ``stash_oldest``: committer timestamps (ISO8601 UTC,
      same style as the envelope's ``generated_at``) of the newest and oldest
      stash entries; ``None`` when the repo has no stash.
    * ``unpushed_branches``: non-HEAD local branches whose commits are absent
      from every remote, as ``(name, ahead)`` pairs (``to_dict`` projects
      ``[{"name", "ahead"}]``); the silent-loss class of work parked on a
      branch you forgot.
    * ``branch_scan_note``: non-``None`` when the unpushed-branch scan was
      skipped (e.g. ``"branch scan skipped: 73 local branches"`` past
      :data:`BRANCH_SCAN_LIMIT`); doubles as the skipped flag.
    * ``upstream_mismatch``: set when the branch's configured upstream is not
      the ref holding its commits (see :func:`_probe_upstream_mismatch`). The
      row's own ``ahead``/``behind`` stay as measured against the CONFIGURED
      upstream; banding reads :func:`effective_ahead_behind` instead.
    * ``remotes``: configured remotes as ``(name, url)`` pairs, read from
      local config only (``to_dict`` projects ``[{"name", "url"}]``). This is
      a *configuration* read, never a network one: no fetch, no ls-remote, so
      it stays inside the read-only glance boundary. Ownership and push policy
      are derived from these URLs one layer up, in ``git_estate``.
    * ``git_dir`` / ``common_dir``: this checkout's own git dir and the
      physical store it shares (both absolute and symlink-resolved, so
      aliases collapse to one key); ``None`` on a blocked probe or a git too
      old for ``--git-common-dir``. ``git_dir == common_dir`` identifies a
      main worktree; a linked worktree points at ``<common>/worktrees/<name>``
      while naming the same ``common_dir``.
    """

    path: str
    classes: frozenset[str] = field(default_factory=frozenset)
    primary_class: str = "clean-current"
    branch: str = "-"
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    stash_count: int = 0
    stash_newest: str | None = None
    stash_oldest: str | None = None
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    mid_op: str | None = None
    unpushed_branches: tuple[tuple[str, int], ...] = ()
    branch_scan_note: str | None = None
    remotes: tuple[tuple[str, str], ...] = ()
    upstream_mismatch: "UpstreamMismatch | None" = None
    bare: bool = False
    git_dir: str | None = None
    common_dir: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection with deterministic key order and sorted classes."""
        return {
            "path": self.path,
            "classes": sorted(self.classes),
            "primary_class": self.primary_class,
            "branch": self.branch,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "stash_count": self.stash_count,
            "stash_newest": self.stash_newest,
            "stash_oldest": self.stash_oldest,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
            "mid_op": self.mid_op,
            "unpushed_branches": [
                {"name": name, "ahead": ahead}
                for name, ahead in self.unpushed_branches
            ],
            "branch_scan_note": self.branch_scan_note,
            "remotes": [{"name": name, "url": url} for name, url in self.remotes],
            "upstream_mismatch": (
                self.upstream_mismatch.to_dict() if self.upstream_mismatch else None
            ),
            "bare": self.bare,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "error": self.error,
        }


@dataclass(frozen=True)
class ScanResult:
    """One full estate scan: the records plus scan-level metadata.

    ``records`` is the exact list :func:`scan` returns (sorted by path);
    ``elapsed_seconds`` covers discovery + all probes, wall clock.
    """

    records: list[GitRepoRecord]
    elapsed_seconds: float
    repo_count: int
    roots: list[str]
    depth: int
    workers: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (records via ``GitRepoRecord.to_dict``)."""
        return {
            "elapsed_seconds": self.elapsed_seconds,
            "repo_count": self.repo_count,
            "roots": list(self.roots),
            "depth": self.depth,
            "workers": self.workers,
            "records": [record.to_dict() for record in self.records],
        }


# --------------------------------------------------------------------------- #
# Git subprocess plumbing
# --------------------------------------------------------------------------- #


def _git_env() -> dict[str, str]:
    """Process environment for git probes: inherited, minus ambient repo
    overrides, plus the read-only/non-interactive flags."""
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_GIT_VARS}
    env.update(READ_ONLY_GIT_ENV)
    return env


def _run_git(
    repo: str, args: Sequence[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command against ``repo``.

    Raises ``subprocess.TimeoutExpired`` / ``OSError`` upward; the probe layer
    converts those into a ``blocked`` record. Non-zero exit codes are returned,
    not raised, so callers can tolerate expected failures (no upstream, ...).
    """
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _first_stderr_line(proc: subprocess.CompletedProcess[str]) -> str:
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


class _RepoDeadlineExceeded(Exception):
    """A probe's overall per-repo deadline is exhausted (wedged repo)."""


class _ProbeClock:
    """Wall-clock budget for one ``probe_repo`` call.

    One instance is created per probe and never shared across workers, so it
    needs no locking. Each git call's timeout is the per-call ``timeout_s``
    clipped to whatever remains of the per-repo deadline; once the deadline is
    spent, :meth:`call_timeout` raises :class:`_RepoDeadlineExceeded` and the
    probe collapses to a ``blocked`` record.
    """

    __slots__ = ("timeout_s", "_deadline")

    def __init__(self, timeout_s: float, deadline_s: float | None) -> None:
        self.timeout_s = timeout_s
        self._deadline = (
            None if deadline_s is None else time.monotonic() + deadline_s
        )

    def expired(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def call_timeout(self) -> float:
        """Timeout for the next git call; raises when the deadline is spent."""
        if self._deadline is None:
            return self.timeout_s
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _RepoDeadlineExceeded()
        return min(self.timeout_s, remaining)


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #


def _blocked(path: str, error: str) -> GitRepoRecord:
    return GitRepoRecord(
        path=path,
        classes=frozenset({"blocked"}),
        primary_class="blocked",
        error=error,
    )


def _resolve_git_path(repo: str, raw: str) -> str | None:
    """Absolute, symlink-resolved form of a ``rev-parse`` path answer.

    ``--git-common-dir`` answers relatively (plain ``.git``) from a main
    worktree, so it is joined onto ``repo`` first. Both answers are then
    ``realpath``-ed: two symlink aliases of one checkout (``opensource/x`` ->
    ``repos/x``) must collapse to ONE store key, otherwise the alias
    double-counts the very entries this resolution exists to dedupe.

    ``git rev-parse`` echoes an option it does not recognise back verbatim and
    still exits 0, so a git too old for ``--git-common-dir`` yields the flag
    itself rather than a path: that (and an empty answer) reads as "unknown",
    never as a store key.
    """
    value = raw.strip()
    if not value or value.startswith("-"):
        return None
    if not os.path.isabs(value):
        value = os.path.join(repo, value)
    try:
        return os.path.realpath(value)
    except OSError:
        return None


def _probe_branch(repo: str, clock: _ProbeClock) -> str:
    head = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], clock.call_timeout())
    branch = head.stdout.strip()
    if head.returncode == 0 and branch and branch != "HEAD":
        return branch
    # ``--abbrev-ref HEAD`` reports the literal "HEAD" both when detached and
    # when HEAD is an unborn branch (fresh init / fresh bare). Only a truly
    # detached HEAD is not a symbolic ref, so symbolic-ref disambiguates.
    sym = _run_git(
        repo, ["symbolic-ref", "--short", "--quiet", "HEAD"], clock.call_timeout()
    )
    unborn = sym.stdout.strip()
    if sym.returncode == 0 and unborn:
        return unborn
    return BRANCH_DETACHED


def _probe_status_counts(repo: str, clock: _ProbeClock) -> tuple[int, int, int]:
    """(staged, unstaged, untracked) entry counts from porcelain v1 status."""
    status = _run_git(
        repo,
        ["status", "--porcelain=v1", "--untracked-files=normal"],
        clock.call_timeout(),
    )
    staged = unstaged = untracked = 0
    if status.returncode != 0:
        return staged, unstaged, untracked
    for line in status.stdout.splitlines():
        if len(line) < 3:
            continue
        x, y = line[0], line[1]
        if x == "?" or y == "?":
            untracked += 1
            continue
        if x != " ":
            staged += 1
        if y != " ":
            unstaged += 1
    return staged, unstaged, untracked


def _probe_mid_op(git_dir: str) -> str | None:
    """Mid-operation kind from marker files under the (worktree-aware) git
    dir, which the identity probe already resolved -- no extra git call."""
    if not git_dir:
        return None
    base = Path(git_dir)
    for kind, marker, is_dir in _MID_OP_MARKERS:
        target = base / marker
        try:
            present = target.is_dir() if is_dir else target.is_file()
        except OSError:
            present = False
        if present:
            return kind
    return None


def _probe_stash(repo: str, clock: _ProbeClock) -> tuple[int, str | None, str | None]:
    """(stash_count, newest, oldest) from ONE ``stash list`` call.

    ``--format=%ct`` swaps the default listing for one committer-date epoch
    per stash entry, so the age enrichment costs zero extra subprocesses.
    Timestamps are ISO8601 UTC (the envelope's ``generated_at`` style);
    ``(0, None, None)`` on any failure, matching the old count-only probe.
    Newest/oldest use max/min rather than list order so backdated stash
    commits (GIT_COMMITTER_DATE) still report truthfully.
    """
    proc = _run_git(repo, ["stash", "list", "--format=%ct"], clock.call_timeout())
    if proc.returncode != 0:
        return 0, None, None
    count = 0
    epochs: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        count += 1
        try:
            epochs.append(int(line))
        except ValueError:
            continue  # unparseable line still counts as a stash entry
    if not epochs:
        return count, None, None

    def _iso(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    return count, _iso(max(epochs)), _iso(min(epochs))


def _parse_track_ahead(track: str) -> int:
    """Ahead count out of a ``%(upstream:track)`` string.

    Formats: `` `` (in sync), ``[ahead 2]``, ``[behind 1]``,
    ``[ahead 2, behind 1]``, ``[gone]``. Anything unparseable reads as 0.
    """
    inner = track.strip().strip("[]")
    for part in inner.split(","):
        words = part.split()
        if len(words) == 2 and words[0] == "ahead":
            try:
                return int(words[1])
            except ValueError:
                return 0
    return 0


def _probe_unpushed_branches(
    repo: str,
    head_branch: str,
    clock: _ProbeClock,
    *,
    include_head: bool = False,
) -> tuple[tuple[tuple[str, int], ...], str | None]:
    """(unpushed branches as (name, ahead) pairs, skip note).

    The HEAD branch is normally excluded: its own ahead/behind ride the record
    directly. ``include_head=True`` puts it back in when it has NO upstream,
    for the one case where nothing else would report it -- a store-backed
    linked worktree, whose band no longer says ``no-remote`` and whose
    upstream-less HEAD therefore has no other surface. Without this, demoting
    that band would trade an overstatement for a silent omission.

    At most TWO subprocesses per repo, whatever the branch count:

    * one ``for-each-ref refs/heads`` call lists every local branch with its
      tip, upstream, and ``%(upstream:track)`` -- branches WITH a live
      upstream get their ahead count straight from the track string;
    * branches without an upstream (or with a gone one) share ONE batched
      ``rev-list --parents <tips...> --not --remotes`` call, and per-branch
      ahead counts come from walking that unpushed subgraph in Python. The
      walk is exact: any path from a candidate tip to an unpushed commit
      stays inside the subgraph (a remote-reachable intermediate commit
      would make all its ancestors remote-reachable too), so the count
      equals what per-branch ``rev-list --count <branch> --not --remotes``
      would report -- commits absent from EVERY remote.

    Past :data:`BRANCH_SCAN_LIMIT` local branches the whole scan is skipped
    and the note names the count. Read-only like every other probe.
    """
    refs = _run_git(
        repo,
        [
            "for-each-ref",
            "refs/heads",
            "--format=%(refname:short)%09%(objectname)%09%(upstream:short)%09%(upstream:track)",
        ],
        clock.call_timeout(),
    )
    if refs.returncode != 0:
        return (), None
    lines = [line for line in refs.stdout.splitlines() if line.strip()]
    if len(lines) > BRANCH_SCAN_LIMIT:
        return (), f"branch scan skipped: {len(lines)} local branches"
    unpushed: list[tuple[str, int]] = []
    candidates: list[tuple[str, str]] = []  # (name, tip) needing the rev-list
    for line in lines:
        parts = line.split("\t")
        name = parts[0].strip()
        tip = parts[1].strip() if len(parts) > 1 else ""
        upstream = parts[2].strip() if len(parts) > 2 else ""
        track = parts[3].strip() if len(parts) > 3 else ""
        if not name:
            continue
        if name == head_branch and not (include_head and not upstream):
            continue
        if upstream and track != "[gone]":
            ahead = _parse_track_ahead(track)
            if ahead > 0:
                unpushed.append((name, ahead))
            continue
        if tip:
            candidates.append((name, tip))
    if candidates:
        # With no remotes at all, ``--remotes`` matches nothing and the whole
        # branch history counts -- honest for a local-only repo's parked work.
        proc = _run_git(
            repo,
            [
                "rev-list",
                "--parents",
                *sorted({tip for _, tip in candidates}),
                "--not",
                "--remotes",
            ],
            clock.call_timeout(),
        )
        if proc.returncode == 0:
            parents: dict[str, list[str]] = {}
            for row in proc.stdout.splitlines():
                shas = row.split()
                if shas:
                    parents[shas[0]] = shas[1:]
            for name, tip in candidates:
                if tip not in parents:
                    continue  # tip is remote-reachable: fully pushed
                seen = {tip}
                stack = [tip]
                while stack:
                    for parent in parents.get(stack.pop(), ()):
                        if parent in parents and parent not in seen:
                            seen.add(parent)
                            stack.append(parent)
                unpushed.append((name, len(seen)))
    return tuple(sorted(unpushed)), None


def _parse_status_v2(stdout: str) -> tuple[str, str | None, int, int, int, int, int]:
    """Parse ``git status --porcelain=v2 --branch`` output.

    Returns ``(branch, upstream, ahead, behind, staged, unstaged, untracked)``
    with exactly the same field semantics as the individual probes it
    replaces (one subprocess instead of up to five):

    * ``branch``: ``# branch.head``; ``(detached)`` maps to
      :data:`BRANCH_DETACHED`, an unborn branch reports its name (matching the
      old ``symbolic-ref`` fallback).
    * ``upstream``: ``# branch.upstream``, but only honoured when
      ``# branch.ab`` is also present -- a configured-but-gone upstream omits
      ``branch.ab``, matching the old ``rev-parse @{upstream}`` failure that
      classified such repos ``no-remote``.
    * counts: ``1``/``2``/``u`` entries split staged/unstaged on the XY
      columns (``.`` = unmodified; unmerged entries count on both sides, as
      the porcelain-v1 parser did), ``?`` entries count as untracked.
    """
    branch = BRANCH_DETACHED
    upstream: str | None = None
    have_ab = False
    ahead = behind = 0
    staged = unstaged = untracked = 0
    for line in stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            if head and head != "(detached)":
                branch = head
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip() or None
        elif line.startswith("# branch.ab "):
            parts = line.split()
            if len(parts) == 4:
                try:
                    ahead, behind = int(parts[2]), abs(int(parts[3]))
                    have_ab = True
                except ValueError:
                    ahead = behind = 0
        elif line.startswith("? "):
            untracked += 1
        elif line.startswith(("1 ", "2 ", "u ")):
            fields = line.split(" ", 2)
            xy = fields[1] if len(fields) > 2 else ""
            if len(xy) == 2:
                if xy[0] != ".":
                    staged += 1
                if xy[1] != ".":
                    unstaged += 1
    if not have_ab:
        upstream = None
        ahead = behind = 0
    return branch, upstream, ahead, behind, staged, unstaged, untracked


def _probe_remotes(repo: str, clock: _ProbeClock) -> tuple[tuple[str, str], ...]:
    """Configured remotes as sorted ``(name, url)`` pairs.

    ``git remote -v`` reads local config and contacts nothing -- the glance
    boundary forbids a fetch, and this keeps it. Only the ``(fetch)`` side is
    kept: a push URL that differs is a publishing detail, and taking both would
    make ownership derivation depend on which line came back first. A repo with
    no remotes, or a git that fails the call, yields ``()`` and the caller
    degrades to ownership-unknown rather than guessing.
    """
    proc = _run_git(repo, ["remote", "-v"], clock.call_timeout())
    if proc.returncode != 0:
        return ()
    seen: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.endswith("(fetch)"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0].strip(), parts[1].strip()
        if name and url and name not in seen:
            seen[name] = url
    return tuple(sorted(seen.items()))


def _probe_upstream(repo: str, clock: _ProbeClock) -> tuple[str | None, int, int]:
    """(upstream, ahead, behind); upstream is None when HEAD has no upstream."""
    proc = _run_git(
        repo, ["rev-parse", "--abbrev-ref", "@{upstream}"], clock.call_timeout()
    )
    upstream = proc.stdout.strip()
    if proc.returncode != 0 or not upstream:
        return None, 0, 0
    ahead = behind = 0
    counts = _run_git(
        repo,
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        clock.call_timeout(),
    )
    parts = counts.stdout.split()
    if counts.returncode == 0 and len(parts) == 2:
        try:
            ahead, behind = int(parts[0]), int(parts[1])
        except ValueError:
            ahead = behind = 0
    return upstream, ahead, behind


def _same_name_ref(branch: str, configured: str) -> str | None:
    """``<remote>/<branch>`` for a branch whose upstream is NOT its own ref.

    The remote is taken from the configured upstream rather than hard-coded to
    ``origin``, so a fork whose branch tracks ``upstream/main`` is compared
    against ``upstream/<branch>`` -- the ref that would actually explain its
    commits. Returns ``None`` when there is nothing to compare: a detached
    HEAD, an unparseable upstream, or an upstream that already IS the
    same-name ref (the overwhelming majority, which therefore costs nothing).
    """
    if not branch or branch == BRANCH_DETACHED or "/" not in configured:
        return None
    remote = configured.split("/", 1)[0].strip()
    if not remote:
        return None
    candidate = f"{remote}/{branch}"
    return None if candidate == configured else candidate


def _probe_upstream_mismatch(
    repo: str,
    branch: str,
    configured: str,
    ahead: int,
    clock: _ProbeClock,
) -> UpstreamMismatch | None:
    """Detect a branch measured against the WRONG ref. One subprocess, no network.

    The 2026-08-15 live run had ``cfo-qbo-control-plane`` sitting at the top of
    the risk table as diverged 3/58 for months. Its 3 commits were already on
    ``origin/codex/qbo-control-plane`` at identical SHA -- the branch's
    configured upstream was ``origin/main``, so ahead/behind measured against a
    ref that was never going to contain them. The scan trusted
    ``branch.<name>.merge`` blindly; one local comparison exposes it.

    Only runs when the row claims local commits (``ahead > 0``): with nothing
    to explain there is no false divergence to kill, and normal repos pay
    nothing. The comparison uses the last-fetched remote-tracking ref exactly
    like every other count here -- no fetch, no ls-remote.

    Returns ``None`` unless the same-name ref exists AND fully explains the
    local commits (``ahead_vs_same_name == 0``). A branch that is genuinely
    ahead of both refs is genuinely ahead, and is left alone.
    """
    if ahead <= 0:
        return None
    same_name = _same_name_ref(branch, configured)
    if not same_name:
        return None
    # A missing ref makes rev-list fail; that IS the existence check, so the
    # probe stays one subprocess instead of a verify + a count.
    counts = _run_git(
        repo,
        ["rev-list", "--left-right", "--count", f"HEAD...{same_name}"],
        clock.call_timeout(),
    )
    parts = counts.stdout.split()
    if counts.returncode != 0 or len(parts) != 2:
        return None
    try:
        ahead_vs, behind_vs = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if ahead_vs != 0:
        return None
    return UpstreamMismatch(
        configured=configured,
        same_name=same_name,
        ahead_vs_same_name=ahead_vs,
        behind_vs_same_name=behind_vs,
    )


def effective_ahead_behind(record: "GitRepoRecord") -> tuple[int, int]:
    """Ahead/behind to BAND on: the same-name ref's numbers when the configured
    upstream is misconfigured, the record's own otherwise.

    ``record.ahead``/``record.behind`` stay honest about what the configured
    upstream says -- that is a real fact about the repo's config, and rewriting
    it would hide the misconfiguration instead of reporting it. Banding reads
    through here so the RISK shown is the risk that exists.
    """
    mismatch = record.upstream_mismatch
    if mismatch is None:
        return record.ahead, record.behind
    return mismatch.ahead_vs_same_name, mismatch.behind_vs_same_name


def _classify(
    *,
    mid_op: str | None,
    dirty: bool,
    stash_count: int,
    upstream: str | None,
    ahead: int,
    behind: int,
    linked_worktree: bool = False,
    has_remote: bool = False,
) -> tuple[frozenset[str], str]:
    """(class set, primary class) -- primary ranking matches repo_inventory.sh.

    ``no-remote`` is the scariest non-blocked band: it reads as "this work
    exists nowhere else". For a LINKED WORKTREE whose shared store has a
    remote, that reading is false -- the commits are one ``git push`` from
    safety, and the 2026-08-15 live run proved it by pushing four such
    "orphaned" branches trivially once they were reclassified. So a linked
    worktree backed by a store with a remote never takes the ``no-remote``
    class; its band falls out of its own checkout state instead.

    A linked worktree whose store genuinely has NO remote still bands
    ``no-remote``, because then the reading is true.
    """
    store_backed = linked_worktree and has_remote
    classes: set[str] = set()
    if mid_op:
        classes.add("mid-op")
    if dirty:
        classes.add("dirty")
    if stash_count >= 1:
        classes.add("stash")
    if upstream is None and not store_backed:
        classes.add("no-remote")
    elif upstream is None:
        # Truth preserved without the scary band: the branch is unpublished,
        # but the store it lives in has somewhere to publish to.
        classes.add("unpublished-branch")
    else:
        if ahead > 0:
            classes.add("ahead")
        if behind > 0:
            classes.add("behind")
        if ahead > 0 and behind > 0 and not dirty:
            classes.add("diverged-clean")
    if not classes:
        classes.add("clean-current")

    if mid_op:
        primary = "mid-op"
    elif dirty:
        primary = "dirty"
    elif stash_count >= STASH_HEAVY_THRESHOLD:
        primary = "stash-heavy"
    elif upstream is None and not store_backed:
        primary = "no-remote"
    elif upstream is None:
        primary = "unpublished-branch"
    elif ahead > 0 and behind > 0:
        primary = "diverged-clean"
    elif behind > 0:
        primary = "behind-clean"
    elif ahead > 0:
        primary = "ahead-clean"
    else:
        primary = "clean-current"
    return frozenset(classes), primary


def probe_repo(
    path: str | os.PathLike[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    deadline_s: float | None = DEFAULT_REPO_DEADLINE_S,
) -> GitRepoRecord:
    """Read-only probe of one repository. Never raises, never fetches, never
    writes; any failure yields a ``blocked`` record with an error string.

    ``timeout_s`` bounds each individual git call; ``deadline_s`` bounds the
    whole probe's wall clock (``None`` disables it), so a wedged repo -- dead
    network filesystem, stuck lock -- becomes ``blocked`` with a reason instead
    of consuming a full per-call timeout for every one of its git calls.
    Thread-safe: each call builds its own clock and env dict and shares no
    mutable state, so probes may run concurrently across pool workers.
    """
    repo = str(path)
    if deadline_s is not None and deadline_s <= 0:
        raise ValueError(f"deadline_s must be positive or None: {deadline_s}")
    clock = _ProbeClock(timeout_s, deadline_s)
    try:
        return _probe(repo, clock)
    except _RepoDeadlineExceeded:
        return _blocked(
            repo, f"repo probe exceeded {deadline_s}s overall deadline (wedged repo?)"
        )
    except subprocess.TimeoutExpired as exc:
        cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(map(str, exc.cmd or []))
        if clock.expired():
            return _blocked(
                repo, f"repo probe exceeded {deadline_s}s overall deadline: {cmd}"
            )
        return _blocked(repo, f"git timed out after {timeout_s}s: {cmd}")
    except OSError as exc:
        return _blocked(repo, f"probe failed: {exc}")
    except Exception as exc:  # pragma: no cover - belt and braces: never crash
        return _blocked(repo, f"unexpected probe failure: {exc!r}")


def _probe(repo: str, clock: _ProbeClock) -> GitRepoRecord:
    # One combined identity call (rev-parse prints one line per query, in
    # argument order) replaces three separate rev-parse spawns.
    ident = _run_git(
        repo,
        [
            "rev-parse",
            "--is-inside-work-tree",
            "--is-bare-repository",
            "--absolute-git-dir",
            "--git-common-dir",
        ],
        clock.call_timeout(),
    )
    lines = ident.stdout.splitlines()
    if ident.returncode != 0 or len(lines) < 3:
        detail = _first_stderr_line(ident)
        error = "not a git work tree" + (f": {detail}" if detail else "")
        return _blocked(repo, error)
    inside_work_tree = lines[0].strip() == "true"
    is_bare = lines[1].strip() == "true"
    git_dir = lines[2].strip()
    # A missing fourth line means an old git dropped the query, not a broken
    # repo: the store key degrades to None and the repo probes as before.
    common_dir = _resolve_git_path(repo, lines[3] if len(lines) > 3 else "")
    if not inside_work_tree and not is_bare:
        return _blocked(repo, "not a git work tree")
    bare = not inside_work_tree

    # One porcelain-v2 status call yields branch + upstream + ahead/behind +
    # staged/unstaged/untracked together (was up to five calls). Bare repos
    # (no work tree, so no status) and any unexpected status failure fall back
    # to the original per-field probes, preserving their exact semantics.
    branch = BRANCH_DETACHED
    upstream: str | None = None
    ahead = behind = 0
    staged = unstaged = untracked = 0
    consolidated = False
    if not bare:
        status = _run_git(
            repo,
            ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            clock.call_timeout(),
        )
        if status.returncode == 0:
            (
                branch,
                upstream,
                ahead,
                behind,
                staged,
                unstaged,
                untracked,
            ) = _parse_status_v2(status.stdout)
            consolidated = True
    if not consolidated:
        branch = _probe_branch(repo, clock)
        if not bare:
            staged, unstaged, untracked = _probe_status_counts(repo, clock)
        upstream, ahead, behind = _probe_upstream(repo, clock)

    mid_op = _probe_mid_op(git_dir)
    remotes = _probe_remotes(repo, clock)
    resolved_git_dir = _resolve_git_path(repo, git_dir)
    # Git's own definition of a linked worktree: its per-worktree git dir is
    # not the shared store. Same rule the amp campaign guard uses, so the guard
    # and the scan can never disagree about what is a worktree. Determined here
    # (not down in _classify) because the branch scan below needs it too.
    linked_worktree = bool(
        resolved_git_dir and common_dir and resolved_git_dir != common_dir
    )
    store_backed = linked_worktree and bool(remotes)
    stash_count, stash_newest, stash_oldest = _probe_stash(repo, clock)
    # Bare repos are skipped: they usually ARE the remote, and "work parked on
    # a forgotten local branch" is a working-checkout risk, not a bare one.
    unpushed_branches: tuple[tuple[str, int], ...] = ()
    branch_scan_note: str | None = None
    if not bare:
        unpushed_branches, branch_scan_note = _probe_unpushed_branches(
            repo, branch, clock, include_head=store_backed and upstream is None
        )

    dirty = (staged + unstaged + untracked) > 0
    # Kill the false-diverged class before classifying: a branch measured
    # against the wrong ref must not reach the top of the risk table.
    upstream_mismatch = None
    if upstream and not bare:
        upstream_mismatch = _probe_upstream_mismatch(
            repo, branch, upstream, ahead, clock
        )
    band_ahead, band_behind = (
        (upstream_mismatch.ahead_vs_same_name, upstream_mismatch.behind_vs_same_name)
        if upstream_mismatch
        else (ahead, behind)
    )
    classes, primary = _classify(
        mid_op=mid_op,
        dirty=dirty,
        stash_count=stash_count,
        upstream=upstream,
        ahead=band_ahead,
        behind=band_behind,
        linked_worktree=linked_worktree,
        has_remote=bool(remotes),
    )
    return GitRepoRecord(
        path=repo,
        classes=classes,
        primary_class=primary,
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        stash_count=stash_count,
        stash_newest=stash_newest,
        stash_oldest=stash_oldest,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        mid_op=mid_op,
        unpushed_branches=unpushed_branches,
        branch_scan_note=branch_scan_note,
        remotes=remotes,
        upstream_mismatch=upstream_mismatch,
        bare=bare,
        git_dir=resolved_git_dir,
        common_dir=common_dir,
        error=None,
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def default_scan_roots() -> list[str]:
    """Default scan roots: ``$HOME/repos`` (repo_inventory.sh's fallback)."""
    return [os.path.join(os.path.expanduser("~"), "repos")]


def _has_git_entry(directory: Path) -> bool:
    git_path = directory / ".git"
    try:
        return git_path.is_dir() or git_path.is_file()
    except OSError:
        return False


def discover_repos(
    roots: Iterable[str | os.PathLike[str]] | None = None,
    *,
    depth: int = DEFAULT_DEPTH,
) -> list[str]:
    """Discover repo work-tree paths under ``roots`` (default ``$HOME/repos``).

    ``depth`` bounds the ``.git`` entry's depth in path components under each
    root (find -maxdepth semantics: a repo at ``root/a/b`` has its ``.git`` at
    depth 3). Prunes :data:`PRUNE_DIR_NAMES`, matches ``.git`` directories and
    gitfiles, includes a root that is itself a repo, skips symlinked dirs and
    unreadable dirs, and does not descend into a discovered (non-root) repo's
    interior -- so submodules and vendored repos inside another repo's work
    tree are not reported. Returns sorted, de-duplicated paths.
    """
    if depth < 1:
        raise ValueError(f"depth must be a positive integer: {depth}")
    if roots is None:
        roots = default_scan_roots()

    found: set[str] = set()
    for root in roots:
        root_path = Path(os.path.expanduser(str(root)))
        try:
            if not root_path.is_dir():
                continue
        except OSError:
            continue
        if _has_git_entry(root_path):
            found.add(str(root_path))
        # The root itself is always descended into (even when it is a repo, as
        # in the shell script) so a monorepo-style root still yields children.
        _walk(root_path, remaining=depth, is_root=True, found=found)
    return sorted(found)


def _walk(directory: Path, *, remaining: int, is_root: bool, found: set[str]) -> None:
    """Recursive scandir walk. ``remaining`` is the depth budget for entries
    directly inside ``directory`` (each level consumes 1)."""
    if remaining < 1:
        return
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return  # unreadable dir: skip, never crash the scan

    is_repo = False
    subdirs: list[Path] = []
    for entry in entries:
        try:
            if entry.name == ".git":
                if entry.is_dir(follow_symlinks=False) or entry.is_file(
                    follow_symlinks=False
                ):
                    is_repo = True
                continue
            if entry.name in PRUNE_DIR_NAMES:
                continue
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(Path(entry.path))
        except OSError:
            continue

    if is_repo and not is_root:
        found.add(str(directory))
        return  # never descend into a discovered repo's interior (submodules)

    for subdir in subdirs:
        _walk(subdir, remaining=remaining - 1, is_root=False, found=found)


# --------------------------------------------------------------------------- #
# Scan + summary
# --------------------------------------------------------------------------- #


def scan_estate(
    roots: Iterable[str | os.PathLike[str]] | None = None,
    *,
    depth: int = DEFAULT_DEPTH,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    deadline_s: float | None = DEFAULT_REPO_DEADLINE_S,
    workers: int | None = None,
) -> ScanResult:
    """Discover and probe every repo under ``roots``; read-only throughout.

    Repos are probed concurrently on a thread pool of ``workers`` threads
    (default :data:`DEFAULT_WORKERS`; discovery stays sequential -- it is a
    scandir walk measured at ~0.1s for a 100-repo estate, so parallelism buys
    nothing there). ``records`` is deterministically sorted by path regardless
    of probe completion order, because discovery output is sorted and
    ``ThreadPoolExecutor.map`` preserves input order. ``elapsed_seconds`` is
    the wall clock for discovery plus all probes.
    """
    if workers is None:
        workers = DEFAULT_WORKERS
    if workers < 1:
        raise ValueError(f"workers must be a positive integer: {workers}")
    resolved_roots = [
        os.path.expanduser(str(root))
        for root in (default_scan_roots() if roots is None else roots)
    ]

    start = time.monotonic()
    repos = discover_repos(resolved_roots, depth=depth)

    def _probe_one(repo: str) -> GitRepoRecord:
        return probe_repo(repo, timeout_s=timeout_s, deadline_s=deadline_s)

    records: list[GitRepoRecord]
    if not repos:
        records = []
    elif workers == 1 or len(repos) == 1:
        records = [_probe_one(repo) for repo in repos]
    else:
        pool_size = min(workers, len(repos))
        with ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="git-inventory"
        ) as pool:
            records = list(pool.map(_probe_one, repos))

    elapsed = time.monotonic() - start
    return ScanResult(
        records=records,
        elapsed_seconds=elapsed,
        repo_count=len(records),
        roots=resolved_roots,
        depth=depth,
        workers=workers,
    )


def scan(
    roots: Iterable[str | os.PathLike[str]] | None = None,
    *,
    depth: int = DEFAULT_DEPTH,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    deadline_s: float | None = DEFAULT_REPO_DEADLINE_S,
    workers: int | None = None,
) -> list[GitRepoRecord]:
    """Discover every repo under ``roots`` and probe each one, read-only.

    Thin wrapper over :func:`scan_estate` keeping the original
    ``list[GitRepoRecord]`` return shape; use :func:`scan_estate` when the
    caller also wants ``elapsed_seconds`` and the other scan metadata.
    """
    return scan_estate(
        roots,
        depth=depth,
        timeout_s=timeout_s,
        deadline_s=deadline_s,
        workers=workers,
    ).records


def primary_class_counts(records: Iterable[GitRepoRecord]) -> dict[str, int]:
    """Count records by ``primary_class``, keys sorted for stable output."""
    counts: dict[str, int] = {}
    for record in records:
        counts[record.primary_class] = counts.get(record.primary_class, 0) + 1
    return dict(sorted(counts.items()))
