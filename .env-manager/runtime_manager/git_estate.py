"""Presentation + machine surface for ``sbp git`` over :mod:`git_inventory`.

The scan engine (:mod:`runtime_manager.git_inventory`) owns discovery and
classification; this module owns everything an agent or operator actually
sees: registry ignore rules, risk-ordered sorting, ``--only`` filters, the
``sbp-git/v1`` JSON envelope, per-row fix handoff commands, and the tty
rendering (cwd detail block first, clean repos folded to one count line).

Read-only, like the engine underneath: no function here ever runs a mutating
git command, and every ``fix`` entry is a *string to hand to the operator*,
never executed.

Risk order (bead-specified, highest first)
------------------------------------------
``blocked`` > ``mid-op`` > ``diverged`` > ``behind-clean`` > ``dirty+behind``
> ``dirty`` > ``ahead`` > ``no-remote`` > ``stash-only`` > ``clean``.
``blocked`` (probe failure -- unassessed risk) and ``no-remote`` are not in
the bead's explicit chain; they are slotted to match the engine's
``PRIMARY_CLASSES`` posture (blocked first) and un-pushed-anywhere risk
(no-remote beside ahead). ``behind-clean`` sits directly under the diverged
band per the bead. Within a band, rows sort by path.

Ignore rules
------------
Never reimplemented: ``skillbox-config/scripts/registry_doctor.py`` is loaded
dynamically (``$SKILLBOX_CONFIG_ROOT`` then ``~/repos/skillbox-config``) and
its ``load_registry``/``normalize_registry``/``matching_ignore`` are reused
against ``<config_root>/registry/repos.yaml``. A missing or broken registry
degrades loudly (one ``registry unavailable: ...`` note) and shows the
unfiltered estate -- it never crashes and never silently skips filtering.
"""

from __future__ import annotations

import importlib.util
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .git_inventory import (
    DEFAULT_DEPTH,
    DEFAULT_TIMEOUT_S,
    GitRepoRecord,
    default_scan_roots,
    primary_class_counts,
    probe_repo,
    scan,
)

__all__ = [
    "FILTER_CLASSES",
    "REGISTRATION_NOTE",
    "RESERVED_FILTER_CLASSES",
    "RISK_BAND_NAMES",
    "SCHEMA",
    "build_report",
    "fix_commands",
    "parse_only",
    "report_text_lines",
    "resolve_cwd_repo_root",
    "risk_band",
    "risk_sorted",
]

#: JSON envelope version. Bump ONLY on a breaking change to the contract.
SCHEMA = "sbp-git/v1"

#: ``--only`` vocabulary backed by scan classes today (repo_inventory.sh
#: semantics: ``behind`` matches behind-clean + diverged-clean, ``ahead``
#: matches ahead-clean + diverged-clean, ``stash`` means stash_count >= 1).
FILTER_CLASSES = (
    "dirty",
    "stash",
    "ahead",
    "behind",
    "diverged-clean",
    "mid-op",
    "no-remote",
    "clean-current",
    "blocked",
)

#: Accepted now so agent muscle memory survives the registry-merge bead, but
#: they yield zero rows plus :data:`REGISTRATION_NOTE` until that bead lands.
RESERVED_FILTER_CLASSES = ("stale-registered", "unregistered")

REGISTRATION_NOTE = "registration states join lands with the registry-merge bead"

#: Risk band names, descending risk; index = sort rank.
RISK_BAND_NAMES = (
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
)

_CLEAN_BAND = RISK_BAND_NAMES.index("clean")

# ANSI SGR per band (tty only). Plain when piped.
_BAND_COLORS = {
    "blocked": "\033[31m",       # red
    "mid-op": "\033[31m",
    "diverged": "\033[31m",
    "behind-clean": "\033[33m",  # yellow
    "dirty-behind": "\033[33m",
    "dirty": "\033[33m",
    "ahead": "\033[36m",         # cyan
    "no-remote": "\033[35m",     # magenta
    "stash-only": "\033[36m",
    "clean": "\033[32m",         # green
}
_ANSI_RESET = "\033[0m"

#: Fix rows shown in the tty ``next_actions:`` footer (top risk rows).
_NEXT_ACTION_ROW_CAP = 5


# --------------------------------------------------------------------------- #
# Risk sort
# --------------------------------------------------------------------------- #


def risk_band(record: GitRepoRecord) -> int:
    """Index into :data:`RISK_BAND_NAMES`; lower = riskier, surfaces first."""
    classes = record.classes
    if "blocked" in classes:
        return RISK_BAND_NAMES.index("blocked")
    if "mid-op" in classes:
        return RISK_BAND_NAMES.index("mid-op")
    dirty = "dirty" in classes
    if record.ahead > 0 and record.behind > 0:
        return RISK_BAND_NAMES.index("diverged")
    if record.behind > 0 and not dirty:
        return RISK_BAND_NAMES.index("behind-clean")
    if record.behind > 0 and dirty:
        return RISK_BAND_NAMES.index("dirty-behind")
    if dirty:
        return RISK_BAND_NAMES.index("dirty")
    if record.ahead > 0:
        return RISK_BAND_NAMES.index("ahead")
    if "no-remote" in classes:
        return RISK_BAND_NAMES.index("no-remote")
    if record.stash_count >= 1:
        return RISK_BAND_NAMES.index("stash-only")
    return _CLEAN_BAND


def risk_sorted(records: Iterable[GitRepoRecord]) -> list[GitRepoRecord]:
    """Deterministic order: risk band, then path within the band."""
    return sorted(records, key=lambda record: (risk_band(record), record.path))


# --------------------------------------------------------------------------- #
# Fix handoff -- exact commands, NEVER executed here
# --------------------------------------------------------------------------- #


def fix_commands(record: GitRepoRecord) -> list[str]:
    """Copy-pasteable remediation per row. Diverged rows get the reconcile
    handoff INSTEAD of push/pull so nobody hand-merges a divergence."""
    path = record.path
    fixes: list[str] = []
    if "blocked" in record.classes:
        return [f"inspect: {record.error or 'probe failed'}"]
    if record.mid_op:
        fixes.append(f"git -C {path} status  # finish or abort the {record.mid_op}")
    diverged = record.ahead > 0 and record.behind > 0
    if diverged:
        fixes.append("sbp doctor / reconcile skill — do not hand-merge")
    elif record.behind > 0:
        fixes.append(f"git -C {path} pull --ff-only  # or /reconcile")
    elif record.ahead > 0:
        fixes.append(f"git -C {path} push")
    if "dirty" in record.classes:
        fixes.append(f"git -C {path} add -p && git -C {path} commit")
    if record.stash_count >= 1:
        fixes.append(f"git -C {path} stash list  # git-stash-janitor pass")
    if "no-remote" in record.classes:
        fixes.append("add a remote or register intent")
    return fixes


# --------------------------------------------------------------------------- #
# --only filter parsing + matching
# --------------------------------------------------------------------------- #


def parse_only(values: Sequence[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split/validate ``--only`` tokens -> (active classes, reserved tokens).

    Accepts repeated flags and comma-joined lists. An unknown token raises
    ``ValueError`` carrying the full valid vocabulary (the CLI maps that to
    exit 2).
    """
    active: list[str] = []
    reserved: list[str] = []
    for raw in values or ():
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            if token in FILTER_CLASSES:
                if token not in active:
                    active.append(token)
            elif token in RESERVED_FILTER_CLASSES:
                if token not in reserved:
                    reserved.append(token)
            else:
                vocabulary = ", ".join(list(FILTER_CLASSES) + list(RESERVED_FILTER_CLASSES))
                raise ValueError(
                    f"unknown --only class {token!r}; valid classes: {vocabulary}"
                )
    return tuple(active), tuple(reserved)


def _matches_only(record: GitRepoRecord, token: str) -> bool:
    # Class-set semantics reproduce the shell expansions: `behind`/`ahead`
    # class membership already includes the diverged-clean case, and `stash`
    # is count-based (>= 1), not the stash-heavy primary threshold.
    if token == "stash":
        return record.stash_count >= 1
    return token in record.classes


def _apply_only(
    records: Sequence[GitRepoRecord], active: Sequence[str]
) -> list[GitRepoRecord]:
    if not active:
        return list(records)
    return [
        record
        for record in records
        if any(_matches_only(record, token) for token in active)
    ]


# --------------------------------------------------------------------------- #
# Registry ignore rules (loaded from skillbox-config, never reimplemented)
# --------------------------------------------------------------------------- #


def _config_root() -> Path:
    override = str(os.environ.get("SKILLBOX_CONFIG_ROOT") or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    return Path.home() / "repos" / "skillbox-config"


def _load_registry_rules() -> tuple[Any, list[dict[str, Any]], str | None]:
    """(registry_doctor module, ignore rules, unavailable-reason).

    Any failure -- missing config root, missing registry file, unimportable
    helper (registry_doctor raises SystemExit without PyYAML) -- returns a
    reason string instead of raising, so the scan degrades loudly.
    """
    config_root = _config_root()
    script = config_root / "scripts" / "registry_doctor.py"
    registry_path = config_root / "registry" / "repos.yaml"
    if not script.is_file():
        return None, [], f"no registry_doctor.py at {script}"
    if not registry_path.is_file():
        return None, [], f"no registry at {registry_path}"
    try:
        spec = importlib.util.spec_from_file_location("_sbp_registry_doctor", script)
        if spec is None or spec.loader is None:
            return None, [], f"cannot load {script}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = module.load_registry(registry_path)
        rules = module.normalize_registry(payload, None)["ignore"]
        return module, list(rules), None
    except BaseException as exc:  # SystemExit included: degrade, never crash
        return None, [], f"registry_doctor failed: {exc}"


def _split_ignored(
    records: Sequence[GitRepoRecord],
    module: Any,
    rules: list[dict[str, Any]],
) -> tuple[list[GitRepoRecord], int]:
    if module is None or not rules:
        return list(records), 0
    kept: list[GitRepoRecord] = []
    ignored = 0
    for record in records:
        if module.matching_ignore(module.normalize_path(record.path), rules):
            ignored += 1
        else:
            kept.append(record)
    return kept, ignored


# --------------------------------------------------------------------------- #
# cwd detail probe
# --------------------------------------------------------------------------- #


def resolve_cwd_repo_root(cwd: str | os.PathLike[str] | None) -> str | None:
    """Nearest ancestor of ``cwd`` (inclusive) with a ``.git`` dir or gitfile
    (worktree-safe). None when cwd is not inside any repo."""
    if cwd is None:
        return None
    try:
        path = Path(os.path.expanduser(str(cwd))).resolve()
    except OSError:
        return None
    for candidate in (path, *path.parents):
        git_entry = candidate / ".git"
        try:
            if git_entry.is_dir() or git_entry.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Report (the sbp-git/v1 envelope; text rendering reads it, JSON emits it)
# --------------------------------------------------------------------------- #


def _row(record: GitRepoRecord) -> dict[str, Any]:
    row = record.to_dict()
    row["risk_band"] = RISK_BAND_NAMES[risk_band(record)]
    row["fix"] = fix_commands(record)
    return row


def build_report(
    *,
    roots: Sequence[str] | None = None,
    depth: int = DEFAULT_DEPTH,
    cwd: str | None = None,
    only: Sequence[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """One read-only scan -> the full ``sbp-git/v1`` payload.

    ``only`` carries raw ``--only`` tokens; an unknown token raises
    ``ValueError`` before any git subprocess runs.
    """
    active, reserved = parse_only(only)
    resolved_roots = [
        os.path.expanduser(str(root)) for root in (roots or default_scan_roots())
    ]

    started = time.monotonic()
    records = scan(resolved_roots, depth=depth, timeout_s=timeout_s)

    module, rules, registry_reason = _load_registry_rules()
    kept, ignored_count = _split_ignored(records, module, rules)

    notes: list[str] = []
    if registry_reason:
        notes.append(f"registry unavailable: {registry_reason}; showing unfiltered")
    if reserved:
        notes.append(f"--only {','.join(reserved)}: {REGISTRATION_NOTE}")

    filtered = risk_sorted(_apply_only(kept, active))
    # Reserved registration tokens are a filter with (for now) zero members:
    # when they are the ONLY filter requested, no row can match yet.
    if reserved and not active:
        filtered = []

    cwd_root = resolve_cwd_repo_root(cwd)
    cwd_repo = _row(probe_repo(cwd_root, timeout_s=timeout_s)) if cwd_root else None

    elapsed = time.monotonic() - started
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": resolved_roots,
        "cwd_repo": cwd_repo,
        "filters": list(active) + list(reserved),
        "notes": notes,
        "ignored_count": ignored_count,
        "registry_applied": registry_reason is None,
        "repos": [_row(record) for record in filtered],
        "summary": primary_class_counts(filtered),
        "repo_count": len(filtered),
        "elapsed_seconds": round(elapsed, 3),
    }


# --------------------------------------------------------------------------- #
# tty rendering
# --------------------------------------------------------------------------- #


def _paint(text: str, band: str, color: bool) -> str:
    if not color:
        return text
    return f"{_BAND_COLORS.get(band, '')}{text}{_ANSI_RESET}"


def _cwd_detail_lines(cwd_repo: dict[str, Any], color: bool) -> list[str]:
    band = str(cwd_repo.get("risk_band", "clean"))
    lines = [f"cwd repo: {cwd_repo.get('path')} [{_paint(band, band, color)}]"]
    if cwd_repo.get("error"):
        lines.append(f"  error: {cwd_repo['error']}")
        return lines
    upstream = cwd_repo.get("upstream") or "(none)"
    lines.append(
        f"  branch: {cwd_repo.get('branch')}  upstream: {upstream} "
        f"(ahead {cwd_repo.get('ahead', 0)}, behind {cwd_repo.get('behind', 0)})"
    )
    lines.append(
        f"  tree: {cwd_repo.get('staged', 0)} staged, "
        f"{cwd_repo.get('unstaged', 0)} unstaged, "
        f"{cwd_repo.get('untracked', 0)} untracked"
    )
    lines.append(f"  stash: {cwd_repo.get('stash_count', 0)}")
    if cwd_repo.get("mid_op"):
        lines.append(f"  mid-op: {cwd_repo['mid_op']} in flight")
    return lines


def _table_lines(rows: list[dict[str, Any]], color: bool) -> list[str]:
    if not rows:
        return []
    band_w = max(len("BAND"), *(len(str(r["risk_band"])) for r in rows))
    branch_w = max(len("BRANCH"), *(len(str(r["branch"])) for r in rows))
    header = (
        f"  {'BAND':{band_w}s}  {'A/B':>5s}  {'S/U/?':>7s}  {'STASH':>5s}  "
        f"{'BRANCH':{branch_w}s}  PATH"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]
    for row in rows:
        counts = f"{row['staged']}/{row['unstaged']}/{row['untracked']}"
        ab = f"{row['ahead']}/{row['behind']}"
        band = str(row["risk_band"])
        lines.append(
            f"  {_paint(f'{band:{band_w}s}', band, color)}  "
            f"{ab:>5s}  {counts:>7s}  "
            f"{row['stash_count']:>5d}  {str(row['branch']):{branch_w}s}  {row['path']}"
        )
    return lines


def report_text_lines(report: dict[str, Any], *, color: bool = False) -> list[str]:
    """Human view: cwd detail first, risk-sorted rollup with clean repos
    folded to one count line, then the issues:/next_actions: footer."""
    lines = ["sbp git — read-only estate git status (counts vs last-fetched upstream)", ""]

    cwd_repo = report.get("cwd_repo")
    if cwd_repo:
        lines.extend(_cwd_detail_lines(cwd_repo, color))
        lines.append("")

    rows = list(report.get("repos") or [])
    filters = list(report.get("filters") or [])
    lines.append(
        f"estate: {report.get('repo_count', 0)} repos under "
        f"{', '.join(report.get('roots') or [])}"
        + (f" (--only {','.join(filters)})" if filters else "")
    )
    for note in report.get("notes") or []:
        lines.append(f"  note: {note}")
    if report.get("registry_applied"):
        lines.append(f"  {report.get('ignored_count', 0)} ignored by registry rules")

    # Clean rows collapse to one count line unless clean-current was asked for.
    show_clean = "clean-current" in filters
    visible = [r for r in rows if show_clean or r["risk_band"] != "clean"]
    clean_hidden = len(rows) - len(visible)
    if visible:
        lines.append("")
        lines.extend(_table_lines(visible, color))
    if clean_hidden:
        lines.append(
            f"  {clean_hidden} clean-current repos (rows folded; "
            "--only clean-current to list)"
        )

    issue_rows = [r for r in rows if r["risk_band"] != "clean"]
    if issue_rows:
        counts: dict[str, int] = {}
        for row in issue_rows:
            counts[row["risk_band"]] = counts.get(row["risk_band"], 0) + 1
        lines.append("")
        lines.append("issues:")
        for band in RISK_BAND_NAMES:
            if band in counts:
                lines.append(f"  - {band}: {counts[band]}")
        lines.append("next_actions:")
        for row in issue_rows[:_NEXT_ACTION_ROW_CAP]:
            for fix in row["fix"]:
                lines.append(f"  - {fix}")
    return lines
