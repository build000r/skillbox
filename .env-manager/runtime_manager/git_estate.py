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

Within a band, unregistered rows outrank registered ones (dirty+unregistered
is work that exists nowhere in the estate model -- the highest-loss-risk
object in the estate), then rows sort by path.

Registry join (ignore rules + registration states)
--------------------------------------------------
Never reimplemented: ``skillbox-config/scripts/registry_doctor.py`` is loaded
dynamically (``$SKILLBOX_CONFIG_ROOT`` then ``~/repos/skillbox-config``) and
its ``load_registry``/``normalize_registry``/``matching_ignore`` are reused
against ``<config_root>/registry/repos.yaml``. ONE parse feeds everything:
the same ``normalize_registry`` payload supplies the ignore rules, the
registered set (its ``repos`` paths), and the stale-registered entries
(registry paths with no ``.git`` on disk -- ``registry_doctor.build_report``'s
stale semantics, without its disk re-scan). Every scanned row carries a
``registration`` state (``registered`` / ``unregistered`` / ``unknown``);
stale entries are not scanned rows, so they surface as a dedicated
``stale_registered`` summary list in the envelope plus a tty section rather
than fake table rows. A missing or broken registry degrades loudly (one
``registry unavailable: ...`` note), shows the unfiltered estate with
``registration: unknown`` everywhere -- it never crashes and never silently
skips filtering.
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
    "REGISTRATION_FILTER_CLASSES",
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

#: ``--only`` vocabulary backed by the registry join: rows are filtered by
#: their ``registration`` state. ``stale-registered`` names registry entries
#: with no repo on disk -- those are never scanned rows, so as a lone filter
#: it yields zero rows while still surfacing the ``stale_registered`` section.
REGISTRATION_FILTER_CLASSES = ("stale-registered", "unregistered")

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


def risk_sorted(
    records: Iterable[GitRepoRecord],
    registration: dict[str, str] | None = None,
) -> list[GitRepoRecord]:
    """Deterministic order: risk band, registration tiebreak, then path.

    ``registration`` maps record path -> state. Within a band, unregistered
    rows sort first (dirty+unregistered is the highest-loss-risk object in
    the estate); ``registered`` and ``unknown`` tie, keeping the degraded
    (registry-unavailable) order identical to the plain path order.
    """
    states = registration or {}

    def key(record: GitRepoRecord) -> tuple[int, int, str]:
        unregistered = states.get(record.path) == "unregistered"
        return (risk_band(record), 0 if unregistered else 1, record.path)

    return sorted(records, key=key)


# --------------------------------------------------------------------------- #
# Fix handoff -- exact commands, NEVER executed here
# --------------------------------------------------------------------------- #


def fix_commands(
    record: GitRepoRecord,
    registration: str | None = None,
    registry_path: str | None = None,
) -> list[str]:
    """Copy-pasteable remediation per row. Diverged rows get the reconcile
    handoff INSTEAD of push/pull so nobody hand-merges a divergence.

    Unregistered rows additionally get the estate-model handoff (register or
    ignore, with the exact registry file path) AFTER the work-securing fixes.
    Blocked rows stay inspect-only: an unprobeable path gets triaged before
    it gets registered.
    """
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
    if registration == "unregistered":
        target = registry_path or str(_config_root() / "registry" / "repos.yaml")
        fixes.append(f"register in {target} or add an ignore rule there")
    return fixes


# --------------------------------------------------------------------------- #
# --only filter parsing + matching
# --------------------------------------------------------------------------- #


def parse_only(values: Sequence[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split/validate ``--only`` tokens -> (git classes, registration tokens).

    Accepts repeated flags and comma-joined lists. Both halves compose as ONE
    union filter (a row matching any requested token stays); they are split
    only because registration tokens match the registry-join state, not the
    scan classes. An unknown token raises ``ValueError`` carrying the full
    valid vocabulary (the CLI maps that to exit 2).
    """
    active: list[str] = []
    registration: list[str] = []
    for raw in values or ():
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            if token in FILTER_CLASSES:
                if token not in active:
                    active.append(token)
            elif token in REGISTRATION_FILTER_CLASSES:
                if token not in registration:
                    registration.append(token)
            else:
                vocabulary = ", ".join(
                    list(FILTER_CLASSES) + list(REGISTRATION_FILTER_CLASSES)
                )
                raise ValueError(
                    f"unknown --only class {token!r}; valid classes: {vocabulary}"
                )
    return tuple(active), tuple(registration)


def _matches_only(record: GitRepoRecord, token: str, registration: str = "unknown") -> bool:
    # Class-set semantics reproduce the shell expansions: `behind`/`ahead`
    # class membership already includes the diverged-clean case, and `stash`
    # is count-based (>= 1), not the stash-heavy primary threshold.
    # Registration tokens match the joined state; `stale-registered` is never
    # a scanned row's state (stale entries have no repo to scan), so it keeps
    # rows out while the stale_registered section carries the entries.
    if token in REGISTRATION_FILTER_CLASSES:
        return registration == token
    if token == "stash":
        return record.stash_count >= 1
    return token in record.classes


def _apply_only(
    records: Sequence[GitRepoRecord],
    active: Sequence[str],
    registration: dict[str, str] | None = None,
) -> list[GitRepoRecord]:
    if not active:
        return list(records)
    states = registration or {}
    return [
        record
        for record in records
        if any(
            _matches_only(record, token, states.get(record.path, "unknown"))
            for token in active
        )
    ]


# --------------------------------------------------------------------------- #
# Registry ignore rules (loaded from skillbox-config, never reimplemented)
# --------------------------------------------------------------------------- #


def _config_root() -> Path:
    override = str(os.environ.get("SKILLBOX_CONFIG_ROOT") or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    return Path.home() / "repos" / "skillbox-config"


def _load_registry_rules() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """(registry_doctor module, ignore rules, registered entries, unavailable-reason).

    ONE registry parse: ``load_registry`` + ``normalize_registry`` run once
    and that single normalized payload supplies both the ignore rules and the
    registered ``repos`` entries (paths already normalized). Any failure --
    missing config root, missing registry file, unimportable helper
    (registry_doctor raises SystemExit without PyYAML) -- returns a reason
    string instead of raising, so the scan degrades loudly.
    """
    config_root = _config_root()
    script = config_root / "scripts" / "registry_doctor.py"
    registry_path = config_root / "registry" / "repos.yaml"
    if not script.is_file():
        return None, [], [], f"no registry_doctor.py at {script}"
    if not registry_path.is_file():
        return None, [], [], f"no registry at {registry_path}"
    try:
        spec = importlib.util.spec_from_file_location("_sbp_registry_doctor", script)
        if spec is None or spec.loader is None:
            return None, [], [], f"cannot load {script}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = module.load_registry(registry_path)
        normalized = module.normalize_registry(payload, None)
        return module, list(normalized["ignore"]), list(normalized["repos"]), None
    except BaseException as exc:  # SystemExit included: degrade, never crash
        return None, [], [], f"registry_doctor failed: {exc}"


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


def _registration_states(
    records: Sequence[GitRepoRecord],
    module: Any,
    repo_entries: Sequence[dict[str, Any]],
) -> dict[str, str]:
    """record path -> ``registered`` | ``unregistered`` | ``unknown``.

    Runs on ignore-filtered rows only, so ``unregistered`` means scanned, not
    in the registry, AND not ignore-matched (ignore hits are dropped and
    counted separately upstream). ``unknown`` everywhere when the registry is
    unavailable.
    """
    if module is None:
        return {record.path: "unknown" for record in records}
    registered = {entry["path"] for entry in repo_entries}
    return {
        record.path: (
            "registered"
            if module.normalize_path(record.path) in registered
            else "unregistered"
        )
        for record in records
    }


def _stale_registered_entries(
    repo_entries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Registry entries whose path has no ``.git`` on disk.

    Mirrors ``registry_doctor.build_report``'s stale test exactly
    (``os.path.exists(<path>/.git)``) WITHOUT calling build_report, which
    would re-scan the disk the estate scan already walked.
    """
    return sorted(
        (
            entry
            for entry in repo_entries
            if not os.path.exists(os.path.join(entry["path"], ".git"))
        ),
        key=lambda entry: entry["path"],
    )


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


def _row(
    record: GitRepoRecord,
    registration: str = "unknown",
    registry_path: str | None = None,
) -> dict[str, Any]:
    row = record.to_dict()
    row["risk_band"] = RISK_BAND_NAMES[risk_band(record)]
    row["registration"] = registration
    row["fix"] = fix_commands(record, registration, registry_path)
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
    active, registration_tokens = parse_only(only)
    resolved_roots = [
        os.path.expanduser(str(root)) for root in (roots or default_scan_roots())
    ]

    started = time.monotonic()
    records = scan(resolved_roots, depth=depth, timeout_s=timeout_s)

    module, rules, repo_entries, registry_reason = _load_registry_rules()
    kept, ignored_count = _split_ignored(records, module, rules)
    registration = _registration_states(kept, module, repo_entries)
    stale_entries = _stale_registered_entries(repo_entries)
    registry_path = str(_config_root() / "registry" / "repos.yaml")

    notes: list[str] = []
    if registry_reason:
        notes.append(f"registry unavailable: {registry_reason}; showing unfiltered")
        if registration_tokens:
            notes.append(
                f"--only {','.join(registration_tokens)}: registration unknown "
                "while the registry is unavailable; no rows can match"
            )

    tokens = tuple(active) + tuple(registration_tokens)
    filtered = risk_sorted(_apply_only(kept, tokens, registration), registration)

    cwd_root = resolve_cwd_repo_root(cwd)
    cwd_repo = None
    if cwd_root:
        cwd_record = probe_repo(cwd_root, timeout_s=timeout_s)
        cwd_states = _registration_states([cwd_record], module, repo_entries)
        cwd_repo = _row(cwd_record, cwd_states[cwd_record.path], registry_path)

    # Estate-level like ignored_count: counted over the ignore-filtered scan,
    # NOT the --only view, so a filtered report still tells the whole truth.
    registration_summary = {
        "registered": 0,
        "unregistered": 0,
        "unknown": 0,
        "stale_registered": len(stale_entries),
    }
    for state in registration.values():
        registration_summary[state] += 1

    stale_rows = [
        {
            "path": entry["path"],
            "id": entry.get("id"),
            "registration": "stale-registered",
            "fix": [f"remove or repoint the registry entry in {registry_path}"],
        }
        for entry in stale_entries
    ]

    elapsed = time.monotonic() - started
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": resolved_roots,
        "cwd_repo": cwd_repo,
        "filters": list(active) + list(registration_tokens),
        "notes": notes,
        "ignored_count": ignored_count,
        "registry_applied": registry_reason is None,
        "repos": [
            _row(record, registration.get(record.path, "unknown"), registry_path)
            for record in filtered
        ],
        "summary": primary_class_counts(filtered),
        "registration_summary": registration_summary,
        "stale_registered": stale_rows,
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
    if cwd_repo.get("registration") == "unregistered":
        lines.append("  registration: unregistered (not in registry, not ignore-matched)")
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
        # Unregistered rows carry an inline marker instead of a column: they
        # already cluster at the top of each band via the sort tiebreak.
        marker = "  [unregistered]" if row.get("registration") == "unregistered" else ""
        lines.append(
            f"  {_paint(f'{band:{band_w}s}', band, color)}  "
            f"{ab:>5s}  {counts:>7s}  "
            f"{row['stash_count']:>5d}  {str(row['branch']):{branch_w}s}  "
            f"{row['path']}{marker}"
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
        reg = report.get("registration_summary") or {}
        lines.append(
            f"  registration: {reg.get('registered', 0)} registered, "
            f"{reg.get('unregistered', 0)} unregistered, "
            f"{reg.get('stale_registered', 0)} stale-registered"
        )

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

    # Stale entries are not scanned rows (no repo on disk), so they render as
    # their own section: on the default view and whenever explicitly asked
    # for, but not when --only narrows to unrelated classes.
    stale_rows = list(report.get("stale_registered") or [])
    if stale_rows and (not filters or "stale-registered" in filters):
        lines.append("")
        lines.append(
            f"stale-registered: {len(stale_rows)} registry entries with no repo on disk"
        )
        for stale in stale_rows:
            lines.append(f"  - {stale['path']}  -> {stale['fix'][0]}")

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
