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

**Worktree-aware.** Mid-operation markers (``rebase-merge``, ``MERGE_HEAD``,
``CHERRY_PICK_HEAD``, ...) are looked up under ``git rev-parse
--absolute-git-dir`` so linked worktrees are classified against their real
per-worktree git dir -- the same trick as the shell script.

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
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "ALL_CLASSES",
    "BRANCH_DETACHED",
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
class GitRepoRecord:
    """One read-only classification of a single repository."""

    path: str
    classes: frozenset[str] = field(default_factory=frozenset)
    primary_class: str = "clean-current"
    branch: str = "-"
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    stash_count: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    mid_op: str | None = None
    bare: bool = False
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
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
            "mid_op": self.mid_op,
            "bare": self.bare,
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


def _probe_stash_count(repo: str, clock: _ProbeClock) -> int:
    proc = _run_git(repo, ["stash", "list"], clock.call_timeout())
    if proc.returncode != 0:
        return 0
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


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


def _classify(
    *,
    mid_op: str | None,
    dirty: bool,
    stash_count: int,
    upstream: str | None,
    ahead: int,
    behind: int,
) -> tuple[frozenset[str], str]:
    """(class set, primary class) -- primary ranking matches repo_inventory.sh."""
    classes: set[str] = set()
    if mid_op:
        classes.add("mid-op")
    if dirty:
        classes.add("dirty")
    if stash_count >= 1:
        classes.add("stash")
    if upstream is None:
        classes.add("no-remote")
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
    elif upstream is None:
        primary = "no-remote"
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
    stash_count = _probe_stash_count(repo, clock)

    dirty = (staged + unstaged + untracked) > 0
    classes, primary = _classify(
        mid_op=mid_op,
        dirty=dirty,
        stash_count=stash_count,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
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
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        mid_op=mid_op,
        bare=bare,
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
