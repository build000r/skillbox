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
    "DEFAULT_LIVE_TIMEOUT_S",
    "FILTER_CLASSES",
    "LIVE_DRIFT_STATES",
    "REGISTRATION_FILTER_CLASSES",
    "RISK_BAND_NAMES",
    "SCHEMA",
    "apply_live_comparison",
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
    live: bool = False,
    live_timeout_s: float | None = None,
) -> dict[str, Any]:
    """One read-only scan -> the full ``sbp-git/v1`` payload.

    ``only`` carries raw ``--only`` tokens; an unknown token raises
    ``ValueError`` before any git subprocess runs.

    ``live`` runs :func:`apply_live_comparison` AFTER the normal local scan
    (additive fields only; every failure degrades to a note). ``live=False``
    (the default) spawns nothing extra and the envelope is unchanged.
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
            _row(record, registration.get(record.path, "unknown"), registry_path)
            for record in filtered
        ],
        "summary": primary_class_counts(filtered),
        "registration_summary": registration_summary,
        "stale_registered": stale_rows,
        "repo_count": len(filtered),
        "elapsed_seconds": round(elapsed, 3),
    }
    if live:
        apply_live_comparison(report, timeout_s=live_timeout_s)
    return report


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
    if cwd_repo.get("origin_state") in LIVE_DRIFT_STATES:
        lines.append(f"  origin: {cwd_repo['origin_state']} (live)")
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
        # --live origin drift is a marker too (absent without --live).
        if row.get("origin_state") in LIVE_DRIFT_STATES:
            marker += f"  [{row['origin_state']}]"
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
    live = report.get("live")
    if isinstance(live, dict) and live.get("applied"):
        lines.append(
            "  live: origin comparison applied via fleet_convergence "
            f"({live.get('matched_rows', 0)} rows matched)"
        )
    if report.get("registry_applied"):
        lines.append(f"  {report.get('ignored_count', 0)} ignored by registry rules")
        reg = report.get("registration_summary") or {}
        lines.append(
            f"  registration: {reg.get('registered', 0)} registered, "
            f"{reg.get('unregistered', 0)} unregistered, "
            f"{reg.get('stale_registered', 0)} stale-registered"
        )

    # Clean rows collapse to one count line unless clean-current was asked
    # for -- except a locally-clean row whose live origin has newer commits:
    # under --live that IS the news, so it stays visible.
    show_clean = "clean-current" in filters
    visible = [
        r
        for r in rows
        if show_clean
        or r["risk_band"] != "clean"
        or r.get("origin_state") in LIVE_DRIFT_STATES
    ]
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
