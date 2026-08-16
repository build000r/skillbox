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

Enrichment surfacing (no new bands, no new columns)
---------------------------------------------------
Stash ages render as relative ages computed against ``generated_at``: the cwd
detail says ``stash: 2 (newest 3d, oldest 40d)`` and stash-only band rows get
an inline ``[stash newest .., oldest ..]`` marker. Unpushed non-HEAD branches
render as an inline ``[+N unpushed branches]`` row marker plus a
``git -C .. branch -vv`` fix line naming each branch; a clean row carrying
them stays visible instead of folding (the silent-loss class hides behind a
clean HEAD) and joins the next_actions footer without joining the issue band
counts. JSON carries the raw fields (``stash_newest``/``stash_oldest``,
``unpushed_branches``, ``branch_scan_note``) untransformed.

Shared ref stores (stash attribution by git-common-dir)
-------------------------------------------------------
Stashes live in the *shared* ref store, not in a checkout, so linked
worktrees and symlink aliases of one repo each report the SAME entries and
naive row math multiplies them (six ``jame--*`` worktrees once showed five
stash rows for two real entries). Rows are therefore grouped by the engine's
``common_dir`` and each physical store's count is attributed to exactly ONE
row: the main worktree (``git_dir == common_dir``) when it was scanned, else
the first-sorted member. Every row keeps its own ``stash_count`` (the truth
that checkout observes) and gains ``stash_attributed`` (0 on rows whose store
another row owns) plus ``stash_store_primary`` (present only on rows sharing
a store); ``stash_summary`` carries the estate's true distinct total, counted
over the ignore-filtered scan like ``registration_summary``. In the tty the
owning row prints its count as before and sharers print ``-`` with a
``[shared store: <primary>]`` marker, so the STASH column sums to the truth
while every checkout stays visible. Repos with a store of their own -- the
overwhelming majority -- are attributed to themselves and render unchanged.

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

Live origin comparison (``--live``, opt-in)
-------------------------------------------
The default scan NEVER talks to the network; ahead/behind counts are vs the
last-fetched upstream. ``--live`` keeps that contract locally and instead
*delegates* the origin probe to the reconcile skill's canonical
``fleet_convergence.py`` (run as a subprocess with ``--json``); its per-repo
verdicts are joined back onto scanned rows by path as ADDITIVE fields
(``origin_state`` / ``origin_head`` on matched rows, a top-level ``live``
object). The envelope stays ``sbp-git/v1``: without ``--live`` none of these
fields exist and output is byte-identical to before. Any delegation failure
(script absent, overall timeout, unexpected exit, unparseable output)
degrades loudly -- one ``live comparison unavailable: <reason>`` note in
``notes`` (tty + JSON) and local-only rows, never a hard failure.

Delta mode (``--delta``, opt-in) and the reconcile receipt join
----------------------------------------------------------------
``compute_scan_delta`` diffs a fresh envelope against the previous cache
generation (see ``git_scan_cache.load_previous_scan``): newly-<band> rows,
resolved rows, appeared/disappeared repos, plus honesty notes when the
baseline used different roots/filters. The result is the additive ``delta``
object -- absent without ``--delta``, so default output stays byte-identical.

The reconcile receipt join reads the receipts store ONCE per scan (env
``SKILLBOX_RECONCILE_RECEIPTS_DIR``, else ``~/.local/state/reconcile/
receipts``; per-receipt shape per the reconcile skill's
``reconcile_receipts.py``) and stamps each row with ``last_reconcile`` (ISO
timestamp of the newest PASSED receipt, or null). The store being absent
adds NOTHING (goldens pin the store-less envelope); an unreadable store is
ONE note; a non-clean row with a receipt older than 30 days gets the
``[last safe sync Nd ago]`` glance marker. Never invokes git or the skill.

Amp joins (capsule default-on, campaign behind ``--amp``)
---------------------------------------------------------
Amp Orb state is delegated, never reimplemented, to the reconcile skill's
guard scripts. ``amp_capsule_guard.sh`` is purely local (file reads plus
read-only git; repos without a capsule are near-free), so its verdicts join
EVERY scan: rows whose sealed Orb capsule drifted gain the additive
``amp_capsule`` field (``capsule-broken-published`` etc.), an inline table
marker, and a reseal fix line. ``amp_campaign_guard.sh`` needs the d3
amp-registry authority read (an SSH round-trip off d3), so its lease
verdicts (``amp-leased`` / ``linked-worktree`` / ``amp-sync-mirror``) join
only on ``--amp`` as the additive ``amp_verdict``/``amp_reasons`` fields.
Both follow the receipts-join degrade contract: guard script absent -> the
capsule join adds NOTHING (goldens pin the guard-less envelope; ``--amp``
is opt-in so an absent campaign guard IS a loud note); a present guard
that fails, times out, or emits garbage -> ONE ``amp ... unavailable``
note. An authority error from the campaign guard is a note, never a
row-spam of ``indeterminate``.

Stale-registered ``located:`` annotation
----------------------------------------
A registry entry may carry ``located:`` (estate environment ids ``mac`` /
``d3`` / ``d3c`` / ``aiops``, or ``amp-orb`` for checkouts living inside an
Amp Orb) plus a free-text ``note:``. A stale-registered entry WITH
``located`` is not junk -- the repo intentionally lives on another box --
so its fix becomes "verify there before touching; do not remove or repoint
from this machine" instead of the remove-or-repoint advice, and both fields
pass through to the envelope's ``stale_registered`` rows.

Paired with the reconcile skill
-------------------------------
``sbp git`` is the read-only estate front door; the reconcile skill is the
mutation dispatcher that acts on what this surface shows (its Phase 0
inventory leads with this command). When the issue backlog is large the
footer says so and routes to reconcile + the divide-and-conquer skill
instead of inviting serial hand-work.

Reconciled means CONVERGED (the reconcile skill's Convergence Contract,
operator-directed 2026-08-15): the acted-on end state for this surface is
origin parity everywhere, not catalogued divergence. Every ``fix`` string
this module emits must therefore be executable by someone tonight — advice
that structurally cannot succeed on this box (e.g. a pull blocked by
amp-owned debris) misroutes the run and is a defect to fix here, not a
state to report politely (see the era-program beads).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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
    "DEFAULT_AMP_GUARD_TIMEOUT_S",
    "DEFAULT_LIVE_TIMEOUT_S",
    "FILTER_CLASSES",
    "JUNK_CANDIDATE_MIN",
    "LIVE_DRIFT_STATES",
    "RECEIPTS_DIR_ENV",
    "RECEIPT_STALE_SECONDS",
    "REGISTRATION_FILTER_CLASSES",
    "RISK_BAND_NAMES",
    "SCHEMA",
    "apply_amp_campaign",
    "apply_live_comparison",
    "build_report",
    "compute_scan_delta",
    "fix_commands",
    "parse_only",
    "report_text_lines",
    "resolve_cwd_repo_root",
    "risk_band",
    "risk_sorted",
    "stash_store_owners",
    "stash_summary",
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

#: Firm overall wall-clock budget for the --live delegation, in seconds
#: (subprocess-level; a glance tool must not hang on network probes).
DEFAULT_LIVE_TIMEOUT_S = 60.0
#: Env override for the --live budget (float seconds); tests use tiny values.
_LIVE_TIMEOUT_ENV = "SKILLBOX_FLEET_CONVERGENCE_TIMEOUT_S"
#: Env override for the fleet_convergence.py path (tests point at a fake).
_LIVE_SCRIPT_ENV = "SKILLBOX_FLEET_CONVERGENCE"
#: fleet_convergence exits that still carry a parseable verdict payload:
#: 0 converged, 1 blocked/diverged, 3 partial/unreachable. 2 = config error.
_LIVE_OK_EXITS = frozenset({0, 1, 3})
#: ``origin_state`` values that mean the live origin disagrees with the local
#: checkout (rendered as inline markers; keep clean rows visible in the tty).
LIVE_DRIFT_STATES = frozenset(
    {"behind-origin", "diverged-from-origin", "origin-newer", "origin-differs"}
)

#: Env override for the reconcile receipts store (tests point at a fixture);
#: falls back to the reconcile skill's state dir
#: (``~/.local/state/reconcile/receipts`` -- the skill's loader,
#: ``reconcile_receipts.py``, is path-agnostic, so the store lives under the
#: skill state next to ``residuals.json``).
RECEIPTS_DIR_ENV = "SKILLBOX_RECONCILE_RECEIPTS_DIR"
#: A non-clean row whose last passed reconcile receipt is older than this
#: gets the at-a-glance ``[last safe sync Nd ago]`` marker.
RECEIPT_STALE_SECONDS = 30 * 86400
#: Per-receipt file size cap: the join must stay a cheap glance read.
_RECEIPT_MAX_BYTES = 1 << 20

#: Firm overall wall-clock budget for the amp guard delegations, in seconds
#: (shared across scan roots; the default capsule join must stay a glance).
DEFAULT_AMP_GUARD_TIMEOUT_S = 20.0
#: Env override for the amp guard budget (float seconds); tests use tiny values.
_AMP_TIMEOUT_ENV = "SKILLBOX_AMP_GUARD_TIMEOUT_S"
#: Env overrides for the guard script paths (tests point at fakes).
_AMP_CAPSULE_ENV = "SKILLBOX_AMP_CAPSULE_GUARD"
_AMP_CAMPAIGN_ENV = "SKILLBOX_AMP_CAMPAIGN_GUARD"
#: Guard exits that still carry a verdict payload: 0 all-clear, 1 non-clear
#: rows present. 2 = usage error / zero repos ("empty is not clear").
_AMP_OK_EXITS = frozenset({0, 1})
#: Capsule verdicts that are non-news (no field, no marker, no fix line).
_AMP_CAPSULE_QUIET = frozenset({"capsule-clear", "capsule-absent"})

#: Issue-row count at or above which the footer routes to the reconcile
#: skill + divide-and-conquer instead of inviting serial hand-work.
_BACKLOG_THRESHOLD = 10

#: Untracked-entry floor at which a row earns the ``git-repo-janitor`` handoff.
#: Matches that skill's own bar -- below five candidates its recovery-bundle
#: overhead does not pay off, so naming it sooner would be noise. Counts are
#: directory-collapsed (``--untracked-files=normal``), same as the scan.
JUNK_CANDIDATE_MIN = 5


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

    Rows carrying at least :data:`JUNK_CANDIDATE_MIN` untracked entries get the
    ``git-repo-janitor`` handoff, mirroring the ``git-stash-janitor`` one: it
    lands after the commit fix (secure the work first, then clean the junk).
    Both name a skill rather than a destructive command -- this function still
    only ever hands back something to read.
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
    if record.untracked >= JUNK_CANDIDATE_MIN:
        fixes.append(
            f"git -C {path} status --short  "
            f"# git-repo-janitor pass ({record.untracked} untracked)"
        )
    if record.stash_count >= 1:
        fixes.append(f"git -C {path} stash list  # git-stash-janitor pass")
    if record.unpushed_branches:
        count = len(record.unpushed_branches)
        noun = "branch" if count == 1 else "branches"
        listing = ", ".join(
            f"{name}(+{ahead})" for name, ahead in record.unpushed_branches
        )
        fixes.append(
            f"git -C {path} branch -vv  # {count} unpushed {noun}: {listing}"
        )
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
# --live origin comparison (delegated to the reconcile skill, never local)
# --------------------------------------------------------------------------- #


def _fleet_convergence_script() -> Path:
    """Path to the reconcile skill's fleet_convergence.py (may not exist).

    Resolution order: ``$SKILLBOX_FLEET_CONVERGENCE`` (tests / explicit
    installs), ``$SKILLBOX_MONOSERVER_ROOT/skills-private/...`` (container
    mount of the repos root), then ``~/repos/skills-private/...``. The last
    candidate is returned even when absent so the degrade note can name the
    path that was tried.
    """
    override = str(os.environ.get(_LIVE_SCRIPT_ENV) or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    relative = Path("skills-private") / "reconcile" / "scripts" / "fleet_convergence.py"
    candidates: list[Path] = []
    mono = str(os.environ.get("SKILLBOX_MONOSERVER_ROOT") or "").strip()
    if mono:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(mono))) / relative)
    candidates.append(Path.home() / "repos" / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def _live_timeout_s() -> float:
    raw = str(os.environ.get(_LIVE_TIMEOUT_ENV) or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_LIVE_TIMEOUT_S


def _local_environment_id(script: Path) -> str | None:
    """Best-effort id of the estate environment with ``transport: local``.

    fleet_convergence has no per-path probe; ``--env <local>`` is its most
    bounded read (skips SSH probes to remote boxes while keeping the live
    origin comparison for local checkouts). The estate authority lives next
    to the script (``../references/estate.yaml``). ANY failure -- no PyYAML,
    unreadable/absent estate, zero or multiple local environments -- returns
    None and the caller falls back to the whole-estate run.
    """
    try:
        import yaml  # PyYAML is optional here; degrade to whole-estate.
    except ImportError:
        return None
    estate = script.parent.parent / "references" / "estate.yaml"
    try:
        payload = yaml.safe_load(estate.read_text(encoding="utf-8"))
    except Exception:
        return None
    environments = payload.get("environments") if isinstance(payload, dict) else None
    if not isinstance(environments, list):
        return None
    local_ids = [
        env.get("id")
        for env in environments
        if isinstance(env, dict)
        and isinstance(env.get("id"), str)
        and isinstance(env.get("access"), dict)
        and env["access"].get("transport") == "local"
    ]
    return local_ids[0] if len(local_ids) == 1 else None


def _run_fleet_convergence(
    script: Path, timeout_s: float
) -> tuple[dict[str, Any] | None, str | None]:
    """One bounded fleet_convergence run -> (payload, None) or (None, reason).

    Invocation: ``--json --all --timeout N`` plus ``--env <local>`` when the
    local environment id is discoverable. ``--all`` includes converged rows
    so matched-but-current checkouts can be positively annotated; ``--json``
    is its machine surface; exit codes 0/1/3 are verdict data (converged /
    blocked-diverged / partial), NOT failures -- only exit 2 (config error),
    other exits, timeouts, and unparseable stdout degrade.

    fleet_convergence's own ``--timeout`` is a per-host probe deadline it
    *survives*: at the deadline it still emits (partial) JSON. It therefore
    gets a few seconds LESS than our overall kill budget, so a slow estate
    yields partial live verdicts instead of a killed subprocess.
    """
    if not script.is_file():
        return None, f"fleet_convergence not found at {script}"
    probe_deadline = timeout_s - 5.0 if timeout_s > 10.0 else timeout_s
    argv = [
        sys.executable,
        str(script),
        "--json",
        "--all",
        "--timeout",
        f"{max(1.0, min(probe_deadline, 600.0)):g}",
    ]
    env_id = _local_environment_id(script)
    if env_id:
        argv.extend(["--env", env_id])
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout_s:g}s"
    except OSError as exc:
        return None, f"could not run {script.name}: {exc}"
    if proc.returncode not in _LIVE_OK_EXITS:
        detail = (proc.stderr or "").strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return None, f"{script.name} exited {proc.returncode}{suffix}"
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None, f"unparseable output from {script.name}"
    if not isinstance(payload, dict) or not isinstance(payload.get("repos"), list):
        return None, f"unparseable output from {script.name}"
    return payload, None


def _live_origin_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """fleet payload -> {checkout path: {"origin_head", "stale"}}.

    A checkout is *stale* when its host appears in an ``origin-mismatch(...)``
    problem or its recorded head differs from the single live origin head --
    i.e. the live origin default branch does not point at the local HEAD.
    Both the raw path and its realpath are indexed (fleet realpaths local
    roots; the scan may hand out unresolved paths). First entry wins on the
    (unlikely) cross-host path collision -- payload order is deterministic.
    """
    index: dict[str, dict[str, Any]] = {}
    for entry in payload.get("repos") or []:
        if not isinstance(entry, dict):
            continue
        origin_head = entry.get("origin_head")
        if not isinstance(origin_head, str) or not origin_head:
            origin_head = None
        heads = entry.get("heads")
        heads = heads if isinstance(heads, dict) else {}
        mismatch_hosts: set[str] = set()
        for problem in entry.get("problems") or []:
            if isinstance(problem, str) and problem.startswith("origin-mismatch("):
                tail = problem.partition("):")[2]
                mismatch_hosts.update(host for host in tail.split(",") if host)
        for location in entry.get("paths") or []:
            if not isinstance(location, dict):
                continue
            path = location.get("path")
            host = location.get("host")
            if not isinstance(path, str) or not path:
                continue
            head = heads.get(host) if isinstance(host, str) else None
            stale = (isinstance(host, str) and host in mismatch_hosts) or bool(
                origin_head and isinstance(head, str) and head and head != origin_head
            )
            info = {"origin_head": origin_head, "stale": stale}
            index.setdefault(path, info)
            index.setdefault(os.path.realpath(path), info)
    return index


def _origin_state(ahead: int, behind: int, stale: bool) -> str:
    """Join the live head comparison with the local cached ahead/behind.

    A raw head mismatch alone cannot distinguish behind/diverged/unpushed;
    the local counts (vs the last-fetched upstream) disambiguate:

    * clean + mismatch      -> ``origin-newer`` (origin moved since last fetch)
    * behind + mismatch     -> ``behind-origin`` (live-confirmed)
    * ahead+behind mismatch -> ``diverged-from-origin``
    * ahead-only + mismatch -> ``origin-differs`` (unpushed local work; origin
      may ALSO have moved -- unknowable without a fetch, so say only "differs")
    * no mismatch           -> ``origin-current``

    Callers must map a matched row with NO live origin head (no remote, or
    the live origin probe failed) to ``origin-unknown`` instead -- claiming
    "current" without a live head would overstate what was observed.
    """
    if not stale:
        return "origin-current"
    if ahead > 0 and behind > 0:
        return "diverged-from-origin"
    if behind > 0:
        return "behind-origin"
    if ahead > 0:
        return "origin-differs"
    return "origin-newer"


def _annotate_row_with_origin(
    row: dict[str, Any], index: dict[str, dict[str, Any]]
) -> bool:
    """Additive per-row fields on a path match; unmatched rows stay untouched."""
    path = str(row.get("path") or "")
    info = index.get(path) or index.get(os.path.realpath(path))
    if info is None:
        return False
    if info["origin_head"] is None and not info["stale"]:
        state = "origin-unknown"
    else:
        state = _origin_state(
            int(row.get("ahead") or 0), int(row.get("behind") or 0), bool(info["stale"])
        )
    row["origin_state"] = state
    row["origin_head"] = info["origin_head"]
    if state == "origin-newer":
        fixes = row.setdefault("fix", [])
        fixes.append(f"git -C {path} pull --ff-only  # origin has newer (live)")
    return True


def apply_live_comparison(
    report: dict[str, Any], *, timeout_s: float | None = None
) -> None:
    """Mutate ``report`` in place with the --live delegation results.

    Strictly additive: a top-level ``live`` object ({applied, reason?, ...})
    plus ``origin_state``/``origin_head`` on matched rows (including the cwd
    detail row). Every failure mode degrades loudly to local-only output --
    one ``live comparison unavailable: <reason>`` note -- and never raises.
    """
    budget = timeout_s if timeout_s is not None else _live_timeout_s()
    script = _fleet_convergence_script()
    payload, reason = _run_fleet_convergence(script, budget)
    if reason is not None:
        report["live"] = {"applied": False, "reason": reason}
        report.setdefault("notes", []).append(
            f"live comparison unavailable: {reason}"
        )
        return
    index = _live_origin_index(payload)
    matched = sum(
        1 for row in report.get("repos") or [] if _annotate_row_with_origin(row, index)
    )
    cwd_repo = report.get("cwd_repo")
    if cwd_repo:
        _annotate_row_with_origin(cwd_repo, index)
    report["live"] = {
        "applied": True,
        "source": str(script),
        "matched_rows": matched,
    }


# --------------------------------------------------------------------------- #
# --delta: diff a fresh scan against the previous cache generation
# --------------------------------------------------------------------------- #


def _band_by_path(envelope: dict[str, Any]) -> dict[str, str]:
    """{row path: risk_band} over an envelope's ``repos`` rows (defensive:
    malformed rows are skipped, a missing band defaults to ``clean``)."""
    bands: dict[str, str] = {}
    for row in envelope.get("repos") or []:
        if not isinstance(row, dict):
            continue
        path = row.get("path")
        if isinstance(path, str) and path:
            bands[path] = str(row.get("risk_band") or "clean")
    return bands


def compute_scan_delta(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    baseline_written_at: str | None = None,
) -> dict[str, Any]:
    """Diff two ``sbp-git/v1`` envelopes -> the additive ``delta`` object.

    ``baseline`` is the generation that was current BEFORE the scan that
    produced ``current`` (both self-describe their roots/filters).

    * ``newly``: per risk band, paths present in both scans whose band
      CHANGED to that (non-clean) band -- so ``newly.dirty``, ``newly.ahead``,
      ``newly.mid-op`` / ``diverged`` / ``blocked``, etc.
    * ``resolved``: paths that were non-clean in the baseline and are now
      clean or gone (a gone non-clean row also appears in ``disappeared``;
      the overlap is deliberate -- "resolved" answers *was the problem
      cleared*, "disappeared" answers *is the repo still scanned*).
    * ``appeared`` / ``disappeared``: row-set membership changes.
    * ``notes``: honesty flags -- when the baseline was scanned with
      different roots or ``--only`` filters the diff is still shown, but the
      membership changes are expected noise and say so.

    ``baseline_written_at`` defaults to the baseline envelope's own
    ``generated_at`` (scan end == cache write, to within milliseconds).
    """
    current_bands = _band_by_path(current)
    baseline_bands = _band_by_path(baseline)

    appeared = sorted(set(current_bands) - set(baseline_bands))
    disappeared = sorted(set(baseline_bands) - set(current_bands))

    newly: dict[str, list[str]] = {}
    resolved: list[str] = []
    for path, old_band in sorted(baseline_bands.items()):
        new_band = current_bands.get(path)
        if old_band != "clean" and (new_band is None or new_band == "clean"):
            resolved.append(path)
        if new_band is not None and new_band != old_band and new_band != "clean":
            newly.setdefault(new_band, []).append(path)

    notes: list[str] = []
    if sorted(current.get("roots") or []) != sorted(baseline.get("roots") or []):
        notes.append("delta baseline used different roots")
    if list(current.get("filters") or []) != list(baseline.get("filters") or []):
        notes.append("delta baseline used different --only filters")

    return {
        "available": True,
        "baseline_written_at": baseline_written_at
        or str(baseline.get("generated_at") or ""),
        "newly": {band: sorted(paths) for band, paths in newly.items()},
        "resolved": resolved,
        "appeared": appeared,
        "disappeared": disappeared,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Reconcile receipt join (read-only glance data, never invokes the skill)
# --------------------------------------------------------------------------- #


def _receipts_dir() -> Path:
    """The reconcile receipts store: env override, else the skill state dir."""
    override = str(os.environ.get(RECEIPTS_DIR_ENV) or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    return Path.home() / ".local" / "state" / "reconcile" / "receipts"


def _receipt_timestamp(payload: Any) -> tuple[str, datetime] | None:
    """(subject id, created_at) of one PASSED receipt; None for anything else.

    Shape authority: the reconcile skill's ``reconcile_receipts.py`` --
    receipts are JSON objects with ``state`` (passed/failed/skipped),
    ``created_at`` (RFC 3339 with timezone), and ``subject: {kind, id}``.
    Only a *passed* receipt counts as a safe sync. Malformed files are
    skipped silently (per-file note spam would drown the glance).
    """
    if not isinstance(payload, dict) or payload.get("state") != "passed":
        return None
    subject = payload.get("subject")
    if not isinstance(subject, dict):
        return None
    subject_id = subject.get("id")
    if not isinstance(subject_id, str) or not subject_id:
        return None
    raw = payload.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        return None
    return subject_id, created


def _load_reconcile_receipts() -> tuple[dict[str, str], str | None, bool]:
    """One pass over the receipts store -> ``(index, error, store_present)``.

    ``index`` maps a receipt subject id to the newest passed ``created_at``
    (ISO string). The whole store absent (no reconcile skill state on this
    box) is NOT an error: ``({}, None, False)`` and the join stays entirely
    silent. An unreadable store (present but unlistable) degrades to ONE
    note via ``error``. Cheap by contract: every ``*.json`` file is read at
    most once per scan and nothing here ever spawns git or the skill.
    """
    store = _receipts_dir()
    if not store.is_dir():
        return {}, None, False
    newest: dict[str, tuple[datetime, str]] = {}
    try:
        entries = sorted(store.iterdir())
    except OSError as exc:
        return {}, f"cannot read {store}: {exc}", True
    for entry in entries:
        if entry.suffix != ".json" or not entry.is_file():
            continue
        try:
            if entry.stat().st_size > _RECEIPT_MAX_BYTES:
                continue
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # one bad receipt must not poison the glance
        parsed = _receipt_timestamp(payload)
        if parsed is None:
            continue
        subject_id, created = parsed
        known = newest.get(subject_id)
        if known is None or created > known[0]:
            newest[subject_id] = (created, str(payload["created_at"]))
    return {key: value[1] for key, value in newest.items()}, None, True


def _last_reconcile_for(row: dict[str, Any], index: dict[str, str]) -> str | None:
    """Join a scan row to the receipt index by path, realpath, then repo id
    (basename) -- receipt subject ids are identifiers, not always paths."""
    path = str(row.get("path") or "")
    for key in (path, os.path.realpath(path), os.path.basename(path)):
        if key and key in index:
            return index[key]
    return None


def _apply_reconcile_receipts(report: dict[str, Any]) -> None:
    """Mutate ``report`` with the additive per-row ``last_reconcile`` field.

    Store absent -> NOTHING is added (the default envelope stays
    byte-identical, which the goldens pin). Store present -> every row (and
    the cwd detail row) gains ``last_reconcile`` (ISO timestamp or null);
    unreadable store -> one note and null everywhere. Never raises.
    """
    index, error, store_present = _load_reconcile_receipts()
    if not store_present:
        return
    if error:
        report.setdefault("notes", []).append(
            f"reconcile receipts unavailable: {error}"
        )
    for row in report.get("repos") or []:
        if isinstance(row, dict):
            row["last_reconcile"] = _last_reconcile_for(row, index)
    cwd_repo = report.get("cwd_repo")
    if isinstance(cwd_repo, dict):
        cwd_repo["last_reconcile"] = _last_reconcile_for(cwd_repo, index)


# --------------------------------------------------------------------------- #
# Amp joins (delegated to the reconcile skill's guard scripts, never local)
# --------------------------------------------------------------------------- #


def _amp_guard_script(env_var: str, name: str) -> Path:
    """Path to a reconcile-skill guard script (may not exist).

    Same resolution ladder as :func:`_fleet_convergence_script`: env override
    (tests / explicit installs), ``$SKILLBOX_MONOSERVER_ROOT`` mount, then
    ``~/repos/skills-private/...``; the last candidate is returned even when
    absent so degrade notes can name the path that was tried.
    """
    override = str(os.environ.get(env_var) or "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    relative = Path("skills-private") / "reconcile" / "scripts" / name
    candidates: list[Path] = []
    mono = str(os.environ.get("SKILLBOX_MONOSERVER_ROOT") or "").strip()
    if mono:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(mono))) / relative)
    candidates.append(Path.home() / "repos" / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def _amp_timeout_s() -> float:
    raw = str(os.environ.get(_AMP_TIMEOUT_ENV) or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_AMP_GUARD_TIMEOUT_S


def _run_amp_guard(
    script: Path, roots: Sequence[str], budget_s: float
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    """Run one guard per root -> (rows, last payload, None) or ([], None, reason).

    The guards emit ``{rows: [...]}`` JSON on exits 0/1 (0 all-clear, 1
    non-clear rows -- both verdict data). Anything else -- exit 2 (usage /
    zero repos), other exits with unparseable stdout, timeout, unrunnable
    script -- degrades to a single reason string. Stdout that parses to a
    rows payload is trusted over the exit code so a "zero repos under this
    root" exit 2 with an empty rows list stays a non-event. The budget is
    shared across roots (a glance surface must not hang per-root).
    """
    rows: list[dict[str, Any]] = []
    payload: dict[str, Any] | None = None
    deadline = time.monotonic() + budget_s
    for root in roots:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], None, f"timed out after {budget_s:g}s"
        argv = ["bash", str(script), "--json", "--root", str(root)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=remaining, check=False
            )
        except subprocess.TimeoutExpired:
            return [], None, f"timed out after {budget_s:g}s"
        except OSError as exc:
            return [], None, f"could not run {script.name}: {exc}"
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            if proc.returncode in _AMP_OK_EXITS:
                return [], None, f"unparseable output from {script.name}"
            detail = (proc.stderr or "").strip().splitlines()
            suffix = f": {detail[0]}" if detail else ""
            return [], None, f"{script.name} exited {proc.returncode}{suffix}"
        rows.extend(row for row in payload["rows"] if isinstance(row, dict))
    return rows, payload, None


def _amp_row_index(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Guard rows by path AND realpath (the capsule guard realpaths; the scan
    may hand out unresolved paths). First entry wins on collision."""
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row.get("path")
        if not isinstance(path, str) or not path:
            continue
        index.setdefault(path, row)
        index.setdefault(os.path.realpath(path), row)
    return index


def _amp_lookup(
    row: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    path = str(row.get("path") or "")
    return index.get(path) or index.get(os.path.realpath(path))


def _apply_amp_capsules(report: dict[str, Any]) -> None:
    """Default-on capsule join: mutate ``report`` with additive fields only.

    Guard script absent -> NOTHING is added (the default envelope stays
    byte-identical on boxes without skills-private, which the goldens pin).
    Guard present but failing -> ONE ``amp capsule guard unavailable`` note.
    A drifted row gains ``amp_capsule`` (the verdict) and a reseal fix line
    (``amp_capsule_reseal.py`` next to the guard -- repair is a reseal by the
    owning Orb, never a hand-edit). Never raises.
    """
    script = _amp_guard_script(_AMP_CAPSULE_ENV, "amp_capsule_guard.sh")
    if not script.is_file():
        return
    rows, _, reason = _run_amp_guard(
        script, report.get("roots") or [], _amp_timeout_s()
    )
    if reason is not None:
        report.setdefault("amp", {})["capsule"] = {"applied": False, "reason": reason}
        report.setdefault("notes", []).append(
            f"amp capsule guard unavailable: {reason}"
        )
        return
    index = _amp_row_index(rows)
    reseal = script.parent / "amp_capsule_reseal.py"
    flagged = 0
    targets = list(report.get("repos") or [])
    cwd_repo = report.get("cwd_repo")
    if isinstance(cwd_repo, dict):
        targets.append(cwd_repo)
    for row in targets:
        if not isinstance(row, dict):
            continue
        info = _amp_lookup(row, index)
        if info is None:
            continue
        verdict = info.get("verdict")
        if not isinstance(verdict, str) or verdict in _AMP_CAPSULE_QUIET:
            continue
        row["amp_capsule"] = verdict
        row.setdefault("fix", []).append(
            f"python3 {reseal} --repo {row.get('path')}  "
            f"# {verdict}: owning Orb reseals; reconcile skill"
        )
        flagged += 1
    report.setdefault("amp", {})["capsule"] = {
        "applied": True,
        "source": str(script),
        "flagged_rows": flagged,
    }


def apply_amp_campaign(report: dict[str, Any], *, timeout_s: float | None = None) -> None:
    """``--amp`` opt-in: join the campaign guard's lease verdicts.

    Strictly additive (``amp_verdict`` / ``amp_reasons`` per matched
    non-clear row, an ``amp.campaign`` object). Because the flag was asked
    for, an absent guard IS a loud note (unlike the default capsule join).
    An authority error (the d3 registry read failed -- SSH down, snapshot
    stale) degrades to ONE note WITHOUT stamping rows: the guard fails
    closed to ``indeterminate`` everywhere, and repeating that per row would
    bury the one actionable fact. Never raises.
    """
    script = _amp_guard_script(_AMP_CAMPAIGN_ENV, "amp_campaign_guard.sh")
    if not script.is_file():
        reason = f"amp_campaign_guard.sh not found at {script}"
        report.setdefault("amp", {})["campaign"] = {"applied": False, "reason": reason}
        report.setdefault("notes", []).append(f"amp campaign guard unavailable: {reason}")
        return
    budget = timeout_s if timeout_s is not None else _amp_timeout_s()
    rows, payload, reason = _run_amp_guard(script, report.get("roots") or [], budget)
    if reason is not None:
        report.setdefault("amp", {})["campaign"] = {"applied": False, "reason": reason}
        report.setdefault("notes", []).append(f"amp campaign guard unavailable: {reason}")
        return
    authority_error = (payload or {}).get("authority_error")
    if isinstance(authority_error, dict) and authority_error:
        code = authority_error.get("code") or "unknown"
        detail = authority_error.get("detail") or ""
        reason = f"{code}: {detail}".rstrip(": ")
        report.setdefault("amp", {})["campaign"] = {"applied": False, "reason": reason}
        report.setdefault("notes", []).append(f"amp authority unavailable: {reason}")
        return
    index = _amp_row_index(rows)
    flagged = 0
    targets = list(report.get("repos") or [])
    cwd_repo = report.get("cwd_repo")
    if isinstance(cwd_repo, dict):
        targets.append(cwd_repo)
    for row in targets:
        if not isinstance(row, dict):
            continue
        info = _amp_lookup(row, index)
        if info is None:
            continue
        verdict = info.get("verdict")
        if not isinstance(verdict, str) or verdict == "clear":
            continue
        row["amp_verdict"] = verdict
        reasons = info.get("reasons")
        if isinstance(reasons, list):
            row["amp_reasons"] = [str(item) for item in reasons]
        if verdict == "amp-leased":
            row.setdefault("fix", []).append(
                "active amp lease — reconcile skill dws-closeout lane; "
                "do not push over the Orb"
            )
        flagged += 1
    campaign: dict[str, Any] = {
        "applied": True,
        "source": str(script),
        "flagged_rows": flagged,
    }
    if isinstance(payload, dict):
        if isinstance(payload.get("active_leases"), int):
            campaign["active_leases"] = payload["active_leases"]
        authority = payload.get("authority")
        if isinstance(authority, dict):
            campaign["authority"] = {
                "environment": authority.get("authority_environment_id"),
                "captured_at": authority.get("captured_at"),
            }
    report.setdefault("amp", {})["campaign"] = campaign


def _is_issue_row(row: dict[str, Any]) -> bool:
    """A row that earns footer next_actions: non-clean band, unpushed
    branches (silent-loss class), or a non-quiet amp verdict on a clean HEAD
    (an Orb problem hides behind a clean tree the same way)."""
    return bool(
        row.get("risk_band") != "clean"
        or row.get("unpushed_branches")
        or row.get("amp_capsule")
        or row.get("amp_verdict")
    )


# --------------------------------------------------------------------------- #
# Shared ref stores -- stash attribution by git-common-dir
# --------------------------------------------------------------------------- #


def stash_store_owners(records: Sequence[GitRepoRecord]) -> dict[str, str]:
    """Map ``path -> owning path`` for every checkout on a SHARED ref store.

    Rows are grouped by :attr:`GitRepoRecord.common_dir` (already absolute and
    symlink-resolved by the engine, so an alias checkout lands in its target's
    group). A store scanned exactly once is left out of the map entirely --
    the overwhelming majority of repos, which therefore keep their existing
    self-attributed behaviour with no marker and no extra fields.

    Owner choice, in order:

    1. the main worktree (``git_dir == common_dir``); the count then hangs off
       the checkout that actually owns the store on disk;
    2. failing that -- the main worktree lives outside the scan roots, or was
       ignored by a registry rule -- the first-sorted member, so the estate
       still counts the store's entries exactly once and the choice is stable
       across runs.

    A record with no ``common_dir`` (blocked probe, or a git too old for
    ``--git-common-dir``) is never grouped: an unknown key must not be treated
    as "same store as every other unknown". Duplicate paths collapse to one
    member, so scanning a path twice cannot make it share a store with itself.
    """
    groups: dict[str, dict[str, GitRepoRecord]] = {}
    for record in records:
        key = record.common_dir
        if not key:
            continue
        groups.setdefault(key, {})[record.path] = record

    owners: dict[str, str] = {}
    for key, members in groups.items():
        if len(members) < 2:
            continue
        primaries = sorted(
            path for path, member in members.items() if member.git_dir == key
        )
        owner = primaries[0] if primaries else sorted(members)[0]
        for path in members:
            owners[path] = owner
    return owners


def _attribute_stash(row: dict[str, Any], owner: str | None) -> None:
    """Stamp one row's stash attribution in place (additive fields only).

    ``owner`` is the path that owns this row's physical store, or ``None``
    when the store is not shared with any other scanned row. ``stash_count``
    is never rewritten -- it stays the honest per-checkout observation, and
    the risk band, ``--only stash`` matching and fix handoff keep reading it,
    so a worktree parked on a shared stash stays just as visible as before.
    """
    count = int(row.get("stash_count") or 0)
    if owner is None:
        row["stash_attributed"] = count
        return
    row["stash_store_primary"] = owner
    row["stash_attributed"] = 0 if owner != row.get("path") else count


def stash_summary(records: Sequence[GitRepoRecord]) -> dict[str, int]:
    """Estate stash truth: distinct entries, not row math.

    ``total`` sums each physical store exactly once, so it equals the number
    of stash entries that really exist across ``records`` even when a repo is
    checked out several times. Like ``ignored_count`` and
    ``registration_summary`` this is counted over the ignore-filtered scan and
    NOT over the ``--only`` view, so a narrowed report still tells the whole
    truth.
    """
    owners = stash_store_owners(records)
    seen: set[str] = set()
    total = row_total = counted_rows = shared_rows = 0
    for record in records:
        if record.path in seen:
            continue
        seen.add(record.path)
        row_total += record.stash_count
        if owners.get(record.path, record.path) != record.path:
            shared_rows += 1
            continue
        total += record.stash_count
        if record.stash_count >= 1:
            counted_rows += 1
    return {
        "total": total,
        # What naive per-checkout row math yields; equal to ``total`` unless a
        # store is scanned more than once. Not derivable from the emitted rows
        # under ``--only``, which is why it ships.
        "row_total": row_total,
        "counted_rows": counted_rows,
        "shared_rows": shared_rows,
        "shared_stores": len(set(owners.values())),
    }


# --------------------------------------------------------------------------- #
# Report (the sbp-git/v1 envelope; text rendering reads it, JSON emits it)
# --------------------------------------------------------------------------- #


def _row(
    record: GitRepoRecord,
    registration: str = "unknown",
    registry_path: str | None = None,
    stash_owner: str | None = None,
) -> dict[str, Any]:
    row = record.to_dict()
    row["risk_band"] = RISK_BAND_NAMES[risk_band(record)]
    row["registration"] = registration
    row["fix"] = fix_commands(record, registration, registry_path)
    _attribute_stash(row, stash_owner)
    return row


def build_report(
    *,
    roots: Sequence[str] | None = None,
    depth: int = DEFAULT_DEPTH,
    cwd: str | None = None,
    only: Sequence[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    live: bool = False,
    live_timeout_s: float | None = None,
    amp: bool = False,
) -> dict[str, Any]:
    """One read-only scan -> the full ``sbp-git/v1`` payload.

    ``only`` carries raw ``--only`` tokens; an unknown token raises
    ``ValueError`` before any git subprocess runs.

    ``live`` runs :func:`apply_live_comparison` AFTER the normal local scan
    (additive fields only; every failure degrades to a note). ``live=False``
    (the default) spawns nothing extra and the envelope is unchanged.

    ``amp`` additionally runs :func:`apply_amp_campaign` (the lease-authority
    read; may SSH to d3). The local capsule guard join is NOT gated on it --
    that runs on every scan whenever the guard script exists.
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

    # Attribution is decided over the ignore-filtered scan, never over the
    # --only view: which checkout owns a physical store is a fact about the
    # estate, so narrowing the table must not promote a linked worktree to
    # owner and change what the very same row says.
    stash_owners = stash_store_owners(kept)

    cwd_root = resolve_cwd_repo_root(cwd)
    cwd_repo = None
    if cwd_root:
        cwd_record = probe_repo(cwd_root, timeout_s=timeout_s)
        cwd_states = _registration_states([cwd_record], module, repo_entries)
        # Only an owner drawn from the scanned rows is meaningful here. A cwd
        # outside the scan roots has no scanned sibling to defer to, so it
        # keeps ordinary self-attribution rather than pointing at a row the
        # report does not contain.
        cwd_repo = _row(
            cwd_record,
            cwd_states[cwd_record.path],
            registry_path,
            stash_owners.get(cwd_record.path),
        )

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

    stale_rows = []
    for entry in stale_entries:
        located = entry.get("located")
        located = located if isinstance(located, str) and located else None
        note = entry.get("note")
        note = note if isinstance(note, str) and note else None
        stale_row: dict[str, Any] = {
            "path": entry["path"],
            "id": entry.get("id"),
            "registration": "stale-registered",
        }
        if located:
            # Annotated entries are not junk: the checkout intentionally
            # lives on another box / inside an Amp Orb, so never advise
            # removing the registry entry from here.
            stale_row["located"] = located
            stale_row["fix"] = [
                f"lives on {located} — verify there before touching; "
                "do not remove or repoint from this machine"
            ]
        else:
            stale_row["fix"] = [
                f"remove or repoint the registry entry in {registry_path}"
            ]
        if note:
            stale_row["note"] = note
        stale_rows.append(stale_row)

    elapsed = time.monotonic() - started
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": resolved_roots,
        "cwd_repo": cwd_repo,
        "filters": list(active) + list(registration_tokens),
        "notes": notes,
        "ignored_count": ignored_count,
        "registry_applied": registry_reason is None,
        "repos": [
            _row(
                record,
                registration.get(record.path, "unknown"),
                registry_path,
                stash_owners.get(record.path),
            )
            for record in filtered
        ],
        "summary": primary_class_counts(filtered),
        "registration_summary": registration_summary,
        "stash_summary": stash_summary(kept),
        "stale_registered": stale_rows,
        "repo_count": len(filtered),
        "elapsed_seconds": round(elapsed, 3),
    }
    _apply_reconcile_receipts(report)
    _apply_amp_capsules(report)
    if live:
        apply_live_comparison(report, timeout_s=live_timeout_s)
    if amp:
        apply_amp_campaign(report)
    # Backlog routing AFTER every join (an amp verdict can turn a clean row
    # into an issue row). Below the threshold the key is absent, keeping the
    # small-estate default envelope byte-identical.
    issue_count = sum(1 for row in report["repos"] if _is_issue_row(row))
    if issue_count >= _BACKLOG_THRESHOLD:
        report["backlog"] = (
            f"{issue_count} issue rows — run the reconcile skill (dispatcher) "
            "and split lanes with the divide-and-conquer skill; "
            "do not hand-work this table"
        )
    return report


# --------------------------------------------------------------------------- #
# tty rendering
# --------------------------------------------------------------------------- #


def _paint(text: str, band: str, color: bool) -> str:
    if not color:
        return text
    return f"{_BAND_COLORS.get(band, '')}{text}{_ANSI_RESET}"


def _relative_age(timestamp: str | None, now: str | None) -> str | None:
    """Coarse age (``3d`` / ``5h`` / ``<1h``) of ``timestamp`` vs ``now``.

    Both are ISO8601 strings (the envelope's ``generated_at`` style, which
    the stash timestamps share). ``None`` on any parse problem so callers
    degrade to their age-free line instead of crashing the render.
    """
    if not timestamp or not now:
        return None
    try:
        seconds = (
            datetime.fromisoformat(now) - datetime.fromisoformat(timestamp)
        ).total_seconds()
    except (ValueError, TypeError):  # unparseable, or naive/aware mix
        return None
    seconds = max(0.0, seconds)
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days}d"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours}h"
    return "<1h"


def _age_seconds(timestamp: str | None, now: str | None) -> float | None:
    """Seconds between two envelope-style ISO8601 strings; None on any parse
    problem (callers degrade to age-free output, never crash the render)."""
    if not timestamp or not now:
        return None
    try:
        seconds = (
            datetime.fromisoformat(now) - datetime.fromisoformat(timestamp)
        ).total_seconds()
    except (ValueError, TypeError):
        return None
    return max(0.0, seconds)


def _format_span(seconds: float) -> str:
    """``34s`` / ``12m`` / ``5h`` / ``3d`` -- the delta banner's age unit."""
    span = max(0, int(seconds))
    if span < 60:
        return f"{span}s"
    if span < 3600:
        return f"{span // 60}m"
    if span < 86400:
        return f"{span // 3600}h"
    return f"{span // 86400}d"


#: Repo names listed inline in the delta banner before folding to "+N more".
_DELTA_NAME_CAP = 5


def _delta_names(paths: list[str]) -> str:
    """Glance listing: repo basenames, capped -- JSON carries full paths."""
    names = [os.path.basename(path.rstrip("/")) or path for path in paths]
    if len(names) > _DELTA_NAME_CAP:
        shown = ", ".join(names[:_DELTA_NAME_CAP])
        return f"{shown}, +{len(names) - _DELTA_NAME_CAP} more"
    return ", ".join(names)


def _delta_lines(delta: dict[str, Any], now: str | None) -> list[str]:
    """tty rendering of the additive ``delta`` object (absent by default)."""
    if not delta.get("available"):
        return [f"delta unavailable: {delta.get('reason') or 'no previous scan'}"]
    age = _age_seconds(delta.get("baseline_written_at"), now)
    versus = f"scan {_format_span(age)} ago" if age is not None else "previous scan"
    segments: list[str] = []
    newly = delta.get("newly") or {}
    for band in RISK_BAND_NAMES:  # deterministic band order, riskiest first
        paths = newly.get(band) or []
        if paths:
            segments.append(f"{len(paths)} newly {band} ({_delta_names(paths)})")
    for label in ("resolved", "appeared", "disappeared"):
        paths = delta.get(label) or []
        if paths:
            segments.append(f"{len(paths)} {label} ({_delta_names(paths)})")
    body = "; ".join(segments) if segments else "no changes"
    lines = [f"delta vs {versus}: {body}"]
    for note in delta.get("notes") or []:
        lines.append(f"  note: {note}")
    return lines


def _shared_store_marker(row: dict[str, Any]) -> str | None:
    """``[shared store: <primary>]`` for a row another checkout counts for.

    ``None`` for an owning row, an unshared store, and for a shared store
    holding no stash at all: with nothing to double-count, the marker would be
    pure noise on every linked worktree in the estate.
    """
    primary = row.get("stash_store_primary")
    if not isinstance(primary, str) or primary == row.get("path"):
        return None
    if int(row.get("stash_count") or 0) < 1:
        return None
    return f"[shared store: {primary}]"


def _unpushed_listing(row: dict[str, Any]) -> str:
    return ", ".join(
        f"{entry['name']} (+{entry['ahead']})"
        for entry in row.get("unpushed_branches") or []
    )


def _cwd_detail_lines(
    cwd_repo: dict[str, Any], color: bool, now: str | None = None
) -> list[str]:
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
    stash_line = f"  stash: {cwd_repo.get('stash_count', 0)}"
    newest = _relative_age(cwd_repo.get("stash_newest"), now)
    oldest = _relative_age(cwd_repo.get("stash_oldest"), now)
    if newest and oldest:
        stash_line += f" (newest {newest}, oldest {oldest})"
    # The count stays -- from here those entries really are reachable -- but a
    # sharer says where the estate counts them, so the detail block and the
    # table below it never look like they disagree.
    shared = _shared_store_marker(cwd_repo)
    if shared:
        stash_line += f"  {shared}"
    lines.append(stash_line)
    if cwd_repo.get("unpushed_branches"):
        lines.append(f"  unpushed branches: {_unpushed_listing(cwd_repo)}")
    if cwd_repo.get("branch_scan_note"):
        lines.append(f"  note: {cwd_repo['branch_scan_note']}")
    if cwd_repo.get("mid_op"):
        lines.append(f"  mid-op: {cwd_repo['mid_op']} in flight")
    if cwd_repo.get("origin_state") in LIVE_DRIFT_STATES:
        lines.append(f"  origin: {cwd_repo['origin_state']} (live)")
    if cwd_repo.get("amp_capsule"):
        lines.append(f"  amp capsule: {cwd_repo['amp_capsule']} (reseal, never hand-edit)")
    if cwd_repo.get("amp_verdict"):
        reasons = cwd_repo.get("amp_reasons") or []
        suffix = f" — {reasons[0]}" if reasons else ""
        lines.append(f"  amp: {cwd_repo['amp_verdict']}{suffix}")
    # Reconcile receipt join: the line exists only when a receipt exists (a
    # missing receipt -- or the whole store -- stays blank, never an error).
    if cwd_repo.get("last_reconcile"):
        sync_age = _relative_age(cwd_repo["last_reconcile"], now)
        if sync_age:
            lines.append(f"  last safe sync: {sync_age} ago (reconcile receipt)")
    if cwd_repo.get("registration") == "unregistered":
        lines.append("  registration: unregistered (not in registry, not ignore-matched)")
    return lines


def _table_lines(
    rows: list[dict[str, Any]], color: bool, now: str | None = None
) -> list[str]:
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
        # --live origin drift is a marker too (absent without --live).
        if row.get("origin_state") in LIVE_DRIFT_STATES:
            marker += f"  [{row['origin_state']}]"
        # Amp joins: capsule drift (default join) and lease/campaign verdicts
        # (--amp) are markers, not columns -- absent whenever the guards
        # found nothing or did not run.
        if row.get("amp_capsule"):
            marker += f"  [{row['amp_capsule']}]"
        if row.get("amp_verdict"):
            marker += f"  [{row['amp_verdict']}]"
        # Unpushed non-HEAD branches: the silent-loss class. A marker, not a
        # column -- table widths stay unchanged; the fix line names branches.
        unpushed = row.get("unpushed_branches") or []
        if unpushed:
            noun = "branch" if len(unpushed) == 1 else "branches"
            marker += f"  [+{len(unpushed)} unpushed {noun}]"
        # Shared ref store: the count belongs to ONE row, so a sharer prints
        # "-" instead of a copy of its primary's number -- the STASH column
        # stays summable -- and names the row that carries it. The stash ages
        # are that same store's ages, so they render there too, not twice.
        shared = _shared_store_marker(row)
        stash_cell = f"{'-':>5s}" if shared else f"{row['stash_count']:>5d}"
        if shared:
            marker += f"  {shared}"
        # Stash age matters most where the stash IS the story (stash-only
        # band); elsewhere the band's own signal leads and the cwd detail /
        # JSON fields carry the ages.
        elif band == "stash-only":
            newest = _relative_age(row.get("stash_newest"), now)
            oldest = _relative_age(row.get("stash_oldest"), now)
            if newest and oldest:
                marker += f"  [stash newest {newest}, oldest {oldest}]"
        # Reconcile receipt staleness at a glance: only when a receipt EXISTS,
        # the row is non-clean, and the last safe sync is over the 30d
        # threshold -- rows without receipts stay blank (zero noise).
        if band != "clean" and row.get("last_reconcile"):
            sync_seconds = _age_seconds(row["last_reconcile"], now)
            if sync_seconds is not None and sync_seconds > RECEIPT_STALE_SECONDS:
                marker += f"  [last safe sync {int(sync_seconds // 86400)}d ago]"
        lines.append(
            f"  {_paint(f'{band:{band_w}s}', band, color)}  "
            f"{ab:>5s}  {counts:>7s}  "
            f"{stash_cell}  {str(row['branch']):{branch_w}s}  "
            f"{row['path']}{marker}"
        )
    return lines


def report_text_lines(report: dict[str, Any], *, color: bool = False) -> list[str]:
    """Human view: cwd detail first, risk-sorted rollup with clean repos
    folded to one count line, then the issues:/next_actions: footer."""
    lines = ["sbp git — read-only estate git status (counts vs last-fetched upstream)", ""]
    now = report.get("generated_at")

    cwd_repo = report.get("cwd_repo")
    if cwd_repo:
        lines.extend(_cwd_detail_lines(cwd_repo, color, now))
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
    live = report.get("live")
    if isinstance(live, dict) and live.get("applied"):
        lines.append(
            "  live: origin comparison applied via fleet_convergence "
            f"({live.get('matched_rows', 0)} rows matched)"
        )
    amp = report.get("amp") or {}
    capsule = amp.get("capsule") if isinstance(amp, dict) else None
    if isinstance(capsule, dict) and capsule.get("applied"):
        lines.append(
            "  amp: capsule guard joined "
            f"({capsule.get('flagged_rows', 0)} rows flagged)"
        )
    campaign = amp.get("campaign") if isinstance(amp, dict) else None
    if isinstance(campaign, dict) and campaign.get("applied"):
        leases = campaign.get("active_leases")
        suffix = f", {leases} active leases" if isinstance(leases, int) else ""
        lines.append(
            "  amp: campaign guard joined "
            f"({campaign.get('flagged_rows', 0)} rows flagged{suffix})"
        )
    if report.get("registry_applied"):
        lines.append(f"  {report.get('ignored_count', 0)} ignored by registry rules")
        reg = report.get("registration_summary") or {}
        lines.append(
            f"  registration: {reg.get('registered', 0)} registered, "
            f"{reg.get('unregistered', 0)} unregistered, "
            f"{reg.get('stale_registered', 0)} stale-registered"
        )

    # The estate's true distinct stash total, stated ONLY where row math would
    # have lied (a shared store actually holding stashes). Estates without one
    # render exactly as before -- the per-row column already sums to the truth.
    stash = report.get("stash_summary") or {}
    shared_rows = int(stash.get("shared_rows") or 0)
    if shared_rows and int(stash.get("total") or 0):
        single = shared_rows == 1
        row_noun = "row" if single else "rows"
        pronoun = "its" if single else "their"
        lines.append(
            f"  stash: {stash['total']} distinct entries "
            f"({shared_rows} {row_noun} counted at {pronoun} primary store)"
        )

    # --delta section (additive: the key only exists on --delta runs), shown
    # between the estate header and the table so the change summary leads.
    delta = report.get("delta")
    if isinstance(delta, dict):
        lines.append("")
        lines.extend(_delta_lines(delta, now))

    # Clean rows collapse to one count line unless clean-current was asked
    # for -- except a locally-clean row whose live origin has newer commits,
    # that carries unpushed non-HEAD branches, or that an amp guard flagged:
    # that IS the news (the silent-loss class hides behind a clean HEAD), so
    # it stays visible.
    show_clean = "clean-current" in filters
    visible = [
        r
        for r in rows
        if show_clean
        or r["risk_band"] != "clean"
        or r.get("origin_state") in LIVE_DRIFT_STATES
        or r.get("unpushed_branches")
        or r.get("amp_capsule")
        or r.get("amp_verdict")
    ]
    clean_hidden = len(rows) - len(visible)
    if visible:
        lines.append("")
        lines.extend(_table_lines(visible, color, now))
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
        located_count = sum(1 for stale in stale_rows if stale.get("located"))
        breakdown = ""
        if located_count:
            unaccounted = len(stale_rows) - located_count
            breakdown = (
                f" ({located_count} located elsewhere, {unaccounted} unaccounted)"
                if unaccounted
                else f" ({located_count} located elsewhere)"
            )
        lines.append("")
        lines.append(
            f"stale-registered: {len(stale_rows)} registry entries "
            f"with no repo on disk{breakdown}"
        )
        for stale in stale_rows:
            located = f"  [located: {stale['located']}]" if stale.get("located") else ""
            note = f"  ({stale['note']})" if stale.get("note") else ""
            lines.append(f"  - {stale['path']}{located}  -> {stale['fix'][0]}{note}")

    # Clean rows with unpushed branches or amp verdicts carry a real
    # next_action without being an issue band, so they join the footer's
    # action rows but never the band counts (amp gets its own count lines).
    issue_rows = [r for r in rows if _is_issue_row(r)]
    if issue_rows:
        counts: dict[str, int] = {}
        for row in issue_rows:
            if row["risk_band"] != "clean":
                counts[row["risk_band"]] = counts.get(row["risk_band"], 0) + 1
        amp_capsule_count = sum(1 for r in rows if r.get("amp_capsule"))
        amp_campaign_count = sum(1 for r in rows if r.get("amp_verdict"))
        lines.append("")
        if counts or amp_capsule_count or amp_campaign_count:
            lines.append("issues:")
            for band in RISK_BAND_NAMES:
                if band in counts:
                    lines.append(f"  - {band}: {counts[band]}")
            if amp_capsule_count:
                lines.append(f"  - amp-capsule: {amp_capsule_count}")
            if amp_campaign_count:
                lines.append(f"  - amp-campaign: {amp_campaign_count}")
        lines.append("next_actions:")
        for row in issue_rows[:_NEXT_ACTION_ROW_CAP]:
            for fix in row["fix"]:
                lines.append(f"  - {fix}")
        hidden_rows = len(issue_rows) - _NEXT_ACTION_ROW_CAP
        if hidden_rows > 0:
            lines.append(
                f"  (… {hidden_rows} more issue rows — "
                "sbp git --json for the full set)"
            )
    if report.get("backlog"):
        lines.append(f"backlog: {report['backlog']}")
    return lines
