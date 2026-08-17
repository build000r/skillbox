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

Lane plan (``lanes``, additive)
-------------------------------
The envelope hands over the division a coordinator used to hand-build. Each
lane is ``{id, kind, repos, write_scope, rationale, suggested_concurrency}``
and, for ``withheld``, a ``withheld: [{path, reason}]`` list. Kinds come from
:data:`LANE_KINDS`; :data:`EMITTED_LANE_KINDS` is what a plan can actually
contain (``doc-only`` is declared but never emitted -- see its note).

Three properties are load-bearing:

* **One lane per family.** A repo and its linked worktrees always share a
  lane, keyed on ``worktree_of``. Directory-shaped partitioning is what let
  one lane push another's branch through a shared git dir.
* **write_scope ⊇ repos.** The scope covers every checkout on the family's
  shared store, including clean siblings that are not themselves work: a
  write through a shared git dir touches all of them.
* **Parity or a typed withhold.** No kind's end state is a new side ref;
  work that cannot reach origin parity from here lands in ``withheld`` with
  its reason and ``suggested_concurrency: 0``, never silently dropped.

The key is ABSENT when nothing needs a lane, and the tty spends exactly one
line on it -- the plan is for machines.

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
from typing import Any, Iterable, Mapping, Sequence

from .git_inventory import (
    DEFAULT_DEPTH,
    DEFAULT_TIMEOUT_S,
    GitRepoRecord,
    default_scan_roots,
    effective_ahead_behind,
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
    "build_lane_plan",
    "compute_scan_delta",
    "derive_ownership",
    "fix_commands",
    "lane_kind_for_row",
    "parse_only",
    "parse_remote_owner",
    "report_text_lines",
    "resolve_cwd_repo_root",
    "risk_band",
    "risk_sorted",
    "stash_store_owners",
    "stash_summary",
    "worktree_primaries",
    "worktree_primary",
]

#: JSON envelope version. Bump ONLY on a breaking change to the contract.
SCHEMA = "sbp-git/v1"

# --------------------------------------------------------------------------- #
# Ownership + push policy
#
# A coordinator's first act on the 2026-08-15 live run was re-deriving, by
# hand, who may push where: build000r remotes are the operator's, tetsuo-ai and
# choffmanebpm remotes are somebody else's, remoteless repos are neither. That
# derivation is mechanical, and getting it wrong is expensive in one direction
# only -- advising `git push` at an external upstream. On that run this surface
# would have advised exactly that three times.
#
# So the vocabulary below is small and the default is timid: anything this
# module cannot positively establish is `unknown` / `ask`, never `push`.
# --------------------------------------------------------------------------- #

OWNERSHIP_OPERATOR = "operator-owned"
OWNERSHIP_EXTERNAL = "external-upstream"
OWNERSHIP_LOCAL = "owned-local"
OWNERSHIP_UNKNOWN = "unknown"

OWNERSHIP_VALUES = (
    OWNERSHIP_OPERATOR,
    OWNERSHIP_EXTERNAL,
    OWNERSHIP_LOCAL,
    OWNERSHIP_UNKNOWN,
)

PUSH_POLICY_PUSH = "push"
PUSH_POLICY_NO_PUSH = "no-push"
PUSH_POLICY_SCRUB_GATE = "scrub-gate"
PUSH_POLICY_ASK = "ask"

PUSH_POLICY_VALUES = (
    PUSH_POLICY_PUSH,
    PUSH_POLICY_NO_PUSH,
    PUSH_POLICY_SCRUB_GATE,
    PUSH_POLICY_ASK,
)

OWNERSHIP_SOURCE_REGISTRY = "registry"
OWNERSHIP_SOURCE_HEURISTIC = "remote-heuristic"
OWNERSHIP_SOURCE_NONE = "none"

#: Registry ``ownership:`` spellings -> this module's vocabulary. The live
#: registry only says ``owned`` / ``owned-local`` today; ``external-upstream``
#: and its synonyms are accepted now so bead v6ac.6.4 can start writing them
#: without a second change here. An unrecognized spelling deliberately does NOT
#: fall through to "probably fine" -- it lands on ``unknown`` and asks.
_REGISTRY_OWNERSHIP = {
    "owned": OWNERSHIP_OPERATOR,
    "operator-owned": OWNERSHIP_OPERATOR,
    "owned-local": OWNERSHIP_LOCAL,
    "local": OWNERSHIP_LOCAL,
    "external": OWNERSHIP_EXTERNAL,
    "external-upstream": OWNERSHIP_EXTERNAL,
    "upstream": OWNERSHIP_EXTERNAL,
    "fork": OWNERSHIP_EXTERNAL,
    "vendor": OWNERSHIP_EXTERNAL,
}

#: Fallback operator account when the registry is unreadable. The registry's
#: own ``metadata.owner`` is preferred whenever it parses -- the estate model
#: should not be duplicated in Python.
DEFAULT_OPERATOR_REMOTE_OWNER = "build000r"

#: Hosts whose URLs carry an owner segment we can compare against the operator
#: account. A host outside this set is not "external", it is unrecognized, and
#: an unrecognized remote is an ``ask``.
_KNOWN_FORGE_HOSTS = frozenset({"github.com", "www.github.com", "gitlab.com", "bitbucket.org"})

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
    # A linked worktree whose HEAD branch has no upstream but whose shared
    # store does have a remote. Filterable in its own right so a coordinator
    # can ask for exactly the rows this bead reclassified out of no-remote.
    "unpublished-branch",
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

# ---------------------------------------------------------------------------
# EXTERNAL-STATE JOINS
#
# Everything below reads state that lives OUTSIDE the repo being scanned, and
# every one of them defaults to a real path on the operator's machine. That is
# what makes them dangerous in tests: a fixture that forgets to redirect one
# scans the host instead, and the suite passes or fails depending on whose
# laptop it runs on. On 2026-08-15 a real receipts store leaked into fixture
# envelopes the same day the receipts join shipped.
#
# ADDING A JOIN? Register its env var in ``HERMETIC_JOIN_ENVS`` in
# ``tests/helpers.py`` — or the regression test will fail you.
# ``tests/test_git_estate_hermetic.py`` derives the true set from the constants
# in THIS file, so an unregistered join is a test failure, not a surprise three
# weeks later.
# ---------------------------------------------------------------------------

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
    # Band on the numbers that describe REALITY. When a branch's configured
    # upstream is not the ref holding its commits, its own ahead/behind
    # describe a config artifact -- that is how cfo-qbo-control-plane sat at
    # the top of the risk table as "diverged 3/58" for months while its 3
    # commits were already on origin/<branch> at identical SHA.
    ahead, behind = effective_ahead_behind(record)
    if ahead > 0 and behind > 0:
        return RISK_BAND_NAMES.index("diverged")
    if behind > 0 and not dirty:
        return RISK_BAND_NAMES.index("behind-clean")
    if behind > 0 and dirty:
        return RISK_BAND_NAMES.index("dirty-behind")
    if dirty:
        return RISK_BAND_NAMES.index("dirty")
    if ahead > 0:
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


def parse_remote_owner(url: str) -> tuple[str | None, str | None]:
    """``(host, owner)`` for a remote URL, or ``(None, None)`` when unreadable.

    Handles the three spellings git actually hands back: ``scp``-style
    (``git@github.com:owner/repo.git``), ``ssh://``/``https://`` URLs, and
    plain filesystem paths. A local path has no host and no owner -- it is the
    "ownership-unknown" case the live run hit, not a forge to compare against.
    """
    text = str(url or "").strip()
    if not text:
        return None, None
    # Local paths first: file:// and anything that is just a path.
    if text.startswith("file://"):
        return None, None
    if text.startswith((".", "/", "~")):
        return None, None

    host = ""
    remainder = ""
    if "://" in text:
        _scheme, _, rest = text.partition("://")
        authority, _, remainder = rest.partition("/")
        # Strip credentials and any port.
        host = authority.rpartition("@")[2].partition(":")[0]
    elif "@" in text and ":" in text.rpartition("@")[2]:
        authority, _, remainder = text.rpartition("@")[2].partition(":")
        host = authority.partition(":")[0]
    else:
        # Bare ``host:path`` with no user, or something we do not recognize.
        authority, sep, remainder = text.partition(":")
        if not sep or "/" in authority:
            return None, None
        host = authority

    host = host.strip().lower()
    owner = remainder.strip("/").split("/")[0] if remainder.strip("/") else ""
    return (host or None), (owner or None)


def derive_ownership(
    record: GitRepoRecord,
    *,
    registry_entry: Mapping[str, Any] | None = None,
    operator_owner: str | None = None,
) -> dict[str, Any]:
    """Ownership + push policy for one row. Pure; touches nothing.

    Precedence is registry, then the remote-URL heuristic, then nothing --
    reported verbatim as ``ownership_source`` so a coordinator can see whether
    a verdict was declared or guessed. ``push_policy_reason`` is the one-line
    why, because a coordinator brief should contain zero prose about who may
    push.

    The registry wins on ownership, but it does not get to override an
    observed external remote into a push: an entry saying ``owned`` on a
    checkout whose origin points at somebody else's account is a *conflict*,
    and a conflict is an ``ask``, not a push.
    """
    account = (operator_owner or DEFAULT_OPERATOR_REMOTE_OWNER).strip().lower()
    remotes = dict(record.remotes)
    primary_url = remotes.get("origin") or (
        record.remotes[0][1] if record.remotes else ""
    )
    host, owner = parse_remote_owner(primary_url)

    declared = ""
    if registry_entry:
        declared = str(registry_entry.get("ownership") or "").strip().lower()

    remote_verdict: str | None = None
    if not record.remotes:
        remote_verdict = None
    elif host and owner and host in _KNOWN_FORGE_HOSTS:
        remote_verdict = (
            OWNERSHIP_OPERATOR if owner.lower() == account else OWNERSHIP_EXTERNAL
        )

    if declared:
        ownership = _REGISTRY_OWNERSHIP.get(declared, OWNERSHIP_UNKNOWN)
        source = OWNERSHIP_SOURCE_REGISTRY
    elif remote_verdict is not None:
        ownership = remote_verdict
        source = OWNERSHIP_SOURCE_HEURISTIC
    elif registry_entry is not None and not record.remotes:
        # Registered and genuinely remoteless: the estate model already says
        # this checkout is deliberate, it just has nowhere to push.
        ownership = OWNERSHIP_LOCAL
        source = OWNERSHIP_SOURCE_REGISTRY
    else:
        ownership = OWNERSHIP_UNKNOWN
        source = (
            OWNERSHIP_SOURCE_HEURISTIC if record.remotes else OWNERSHIP_SOURCE_NONE
        )

    conflict = (
        ownership == OWNERSHIP_OPERATOR
        and remote_verdict == OWNERSHIP_EXTERNAL
    )

    policy, reason = _push_policy_for(
        ownership,
        conflict=conflict,
        has_remote=bool(record.remotes),
        owner=owner,
        host=host,
        source=source,
        registry_entry=registry_entry,
    )
    result: dict[str, Any] = {
        "ownership": ownership,
        "ownership_source": source,
        "push_policy": policy,
        "push_policy_reason": reason,
    }
    if primary_url:
        result["remote_url"] = primary_url
    if owner:
        result["remote_owner"] = owner
    return result


def _push_policy_for(
    ownership: str,
    *,
    conflict: bool,
    has_remote: bool,
    owner: str | None,
    host: str | None,
    source: str,
    registry_entry: Mapping[str, Any] | None,
) -> tuple[str, str]:
    if conflict:
        return (
            PUSH_POLICY_ASK,
            f"registry says operator-owned but origin is {owner}/@{host}; confirm before pushing",
        )
    # Forward compatibility with bead v6ac.6.4, which will write explicit
    # push-policy flags into the registry. Honour them the moment they exist.
    declared_policy = ""
    if registry_entry:
        declared_policy = str(registry_entry.get("push_policy") or "").strip().lower()
        if not declared_policy and registry_entry.get("scrub_gate"):
            declared_policy = PUSH_POLICY_SCRUB_GATE
    if declared_policy in PUSH_POLICY_VALUES:
        return declared_policy, f"registry declares push_policy: {declared_policy}"

    if ownership == OWNERSHIP_EXTERNAL:
        who = f" ({owner})" if owner else ""
        return PUSH_POLICY_NO_PUSH, f"external upstream{who}; publish via PR, never a direct push"
    if ownership == OWNERSHIP_LOCAL:
        return PUSH_POLICY_NO_PUSH, "no remote configured; nothing to push to"
    if ownership == OWNERSHIP_OPERATOR:
        if not has_remote:
            return PUSH_POLICY_NO_PUSH, "operator-owned but no remote configured"
        return PUSH_POLICY_PUSH, f"operator-owned remote ({source})"
    if not has_remote:
        return PUSH_POLICY_ASK, "no remote and no registry entry; confirm intent"
    return PUSH_POLICY_ASK, "ownership could not be established from registry or remote"


def _ahead_fix(path: str, record: GitRepoRecord, push_policy: str | None) -> str:
    """Advice for an ahead-of-upstream row, gated on who owns the remote.

    ``git push`` is emitted for exactly one policy. Every other policy gets a
    read-only command that shows the same commits without publishing them:
    telling a coordinator to push at somebody else's upstream is the one error
    in this surface that cannot be undone by reading more output.

    ``push_policy=None`` means the caller did not derive one; that is treated
    as unknown and asks, not as permission.
    """
    ahead = record.ahead
    plural = "" if ahead == 1 else "s"
    if push_policy == PUSH_POLICY_PUSH:
        return f"git -C {path} push"
    review = f"git -C {path} log --oneline @{{u}}..HEAD  # {ahead} unpublished commit{plural}"
    if push_policy == PUSH_POLICY_NO_PUSH:
        return f"{review}; external upstream — open a PR, do not publish directly"
    if push_policy == PUSH_POLICY_SCRUB_GATE:
        return f"{review}; scrub gate — run the scrub before publishing"
    return f"{review}; ownership unconfirmed — establish intent before publishing"


def fix_commands(
    record: GitRepoRecord,
    registration: str | None = None,
    registry_path: str | None = None,
    push_policy: str | None = None,
) -> list[str]:
    """Copy-pasteable remediation per row. Diverged rows get the reconcile
    handoff INSTEAD of push/pull so nobody hand-merges a divergence.

    Unregistered rows additionally get the estate-model handoff (register or
    ignore, with the exact registry file path) AFTER the work-securing fixes.
    Blocked rows stay inspect-only: an unprobeable path gets triaged before
    it gets registered.

    ``push_policy`` (from :func:`derive_ownership`) gates the ahead-of-upstream
    advice: ``git push`` is emitted for ``push`` and for nothing else. Passing
    ``None`` is treated as unconfirmed, not as permission.

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
    mismatch = record.upstream_mismatch
    if mismatch is not None:
        # Repair the config, do not reconcile a divergence that does not
        # exist. The commits are already on `same_name` at identical SHA; the
        # only wrong thing here is which ref the branch is pointed at.
        fixes.append(
            f"git -C {path} branch --set-upstream-to {mismatch.same_name}"
            f"  # upstream points at {mismatch.configured}"
        )
    ahead, behind = effective_ahead_behind(record)
    diverged = ahead > 0 and behind > 0
    if diverged:
        fixes.append("sbp doctor / reconcile skill — do not hand-merge")
    elif behind > 0:
        fixes.append(f"git -C {path} pull --ff-only  # or /reconcile")
    elif ahead > 0:
        fixes.append(_ahead_fix(path, record, push_policy))
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


def _load_registry_rules() -> tuple[
    Any, list[dict[str, Any]], list[dict[str, Any]], str | None, str | None
]:
    """(registry_doctor module, ignore rules, entries, unavailable-reason, operator owner).

    ``operator owner`` is the registry's own ``metadata.owner``. Ownership
    derivation compares remote URLs against it rather than hard-coding an
    account here: the estate model is the registry's to declare, and a fork of
    this repo should not have to patch Python to say who it is.

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
        return None, [], [], f"no registry_doctor.py at {script}", None
    if not registry_path.is_file():
        return None, [], [], f"no registry at {registry_path}", None
    try:
        spec = importlib.util.spec_from_file_location("_sbp_registry_doctor", script)
        if spec is None or spec.loader is None:
            return None, [], [], f"cannot load {script}", None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = module.load_registry(registry_path)
        normalized = module.normalize_registry(payload, None)
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
        owner = None
        if isinstance(metadata, Mapping):
            raw_owner = str(metadata.get("owner") or "").strip()
            owner = raw_owner or None
        return module, list(normalized["ignore"]), list(normalized["repos"]), None, owner
    except BaseException as exc:  # SystemExit included: degrade, never crash
        return None, [], [], f"registry_doctor failed: {exc}", None


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


# --------------------------------------------------------------------------- #
# Lane plan
#
# The 2026-08-15 coordinator brief hand-partitioned 55 issue rows into 5 lanes
# with prose write scopes. Every rule it applied was mechanical -- band, size,
# ownership, worktree grouping -- so the envelope hands the division over
# instead of making the next coordinator re-derive it.
#
# Everything here is a PURE function over rows: no I/O, no git, no clock, so a
# lane plan is unit-testable and identical across runs on the same envelope.
# Lanes carry fix strings and never execute: the glance still only plans.
#
# Per the epic's VISION CORRECTION, the partition's goal is ORIGIN PARITY per
# lane. No lane's end state may be a new side ref -- there is deliberately no
# "snapshot to a safety branch" kind, because safety branches are the debris a
# reconcile eliminates, not an outcome. Work that genuinely cannot converge
# from here is a TYPED EXCEPTION (``withheld``) carrying its reason, never a
# silently dropped row.
# --------------------------------------------------------------------------- #

LANE_WITHHELD = "withheld"
LANE_DIVERGED = "diverged"
LANE_DIRTY_BEHIND = "dirty-behind"
LANE_CONVERGE = "converge"
LANE_PUSH_AHEAD = "push-ahead"
LANE_UNREGISTERED_DIRTY = "unregistered-dirty"
LANE_SMALL_DIRTY = "small-dirty"
LANE_MECHANICAL = "mechanical-cluster"
LANE_DOC_ONLY = "doc-only"

#: Emission order IS the assignment ladder: first match wins, so every issue
#: row lands in exactly one lane and the result is deterministic.
#:
#: ``doc-only`` is in the vocabulary because the bead names it, but it is NOT
#: emitted: deciding a change is docs-only needs file-level data
#: (``git diff --name-only``) that the envelope does not carry and that the
#: read-only glance does not probe. Declaring it here without producing it
#: keeps the contract honest and leaves the slot for a bead that adds the data.
LANE_KINDS: tuple[str, ...] = (
    LANE_WITHHELD,
    LANE_DIVERGED,
    LANE_DIRTY_BEHIND,
    LANE_CONVERGE,
    LANE_PUSH_AHEAD,
    LANE_UNREGISTERED_DIRTY,
    LANE_SMALL_DIRTY,
    LANE_MECHANICAL,
    LANE_DOC_ONLY,
)

#: Kinds a lane plan can actually contain today.
EMITTED_LANE_KINDS: tuple[str, ...] = tuple(
    kind for kind in LANE_KINDS if kind != LANE_DOC_ONLY
)

#: Staged+unstaged+untracked at or under this is a "small" dirty tree: one
#: commit's worth of review, not a session's.
SMALL_DIRTY_MAX = 5

#: Upper bound on a lane's suggested parallelism. Past this the coordinator is
#: managing agents rather than work.
MAX_LANE_CONCURRENCY = 4

_LANE_RATIONALE = {
    LANE_WITHHELD: (
        "cannot reach origin parity from here — each row carries its reason; "
        "these are the judgment blocks, not work to dispatch"
    ),
    LANE_DIVERGED: (
        "ahead and behind: pull, merge, push to parity — never hand-merge, "
        "never park on a side branch"
    ),
    LANE_DIRTY_BEHIND: "commit the working tree first, then converge to parity",
    LANE_CONVERGE: "clean and behind: fast-forward to parity",
    LANE_PUSH_AHEAD: "local commits on an operator-owned remote: push to parity",
    LANE_UNREGISTERED_DIRTY: (
        "uncommitted work in a repo the estate model does not know: secure the "
        "work, then register or ignore it"
    ),
    LANE_SMALL_DIRTY: "small working trees: one commit each",
    LANE_MECHANICAL: (
        "bounded per-repo handoffs, no cross-repo coordination: forgotten "
        "branches to publish, upstream re-pointing, stash review"
    ),
}


def _lane_withhold_reason(row: Mapping[str, Any]) -> str | None:
    """Why this row cannot be dispatched toward parity, or ``None``.

    A withhold is a TYPED EXCEPTION, not a dropped row: the coordinator still
    sees it, with the reason it needs a human.
    """
    if "blocked" in (row.get("classes") or []):
        return f"probe blocked: {row.get('error') or 'unknown'}"
    if row.get("mid_op"):
        return f"{row['mid_op']} in flight — finish or abort before any lane runs"
    classes = row.get("classes") or []
    ahead, behind = _lane_effective(row)
    if (
        "no-remote" in classes
        and "dirty" not in classes
        and not ahead
        and not behind
        and not row.get("unpushed_branches")
    ):
        # Nothing to converge TO. Several of these are registry-declared
        # "Deliberately remoteless. Verified 2026-08-15" -- dispatching an
        # agent to add a remote to one of those would be actively wrong, so
        # the declaration is repeated back rather than overridden.
        if row.get("ownership") == OWNERSHIP_LOCAL:
            return "remoteless by declaration (registry: owned-local) — no origin to converge to"
        return "no remote configured — declare intent before any lane can converge it"
    policy = row.get("push_policy")
    if policy == PUSH_POLICY_NO_PUSH and _lane_needs_publish(row):
        return f"{row.get('push_policy_reason') or 'publishing withheld'}"
    if policy == PUSH_POLICY_ASK and _lane_needs_publish(row):
        return f"ownership unconfirmed — {row.get('push_policy_reason') or 'confirm intent'}"
    if policy == PUSH_POLICY_SCRUB_GATE and _lane_needs_publish(row):
        return "scrub gate — run the scrub before publishing"
    return None


def _lane_needs_publish(row: Mapping[str, Any]) -> bool:
    """True when reaching parity would require pushing something."""
    mismatch = row.get("upstream_mismatch") or {}
    ahead = (
        int(mismatch.get("ahead_vs_same_name") or 0)
        if mismatch
        else int(row.get("ahead") or 0)
    )
    return ahead > 0 or bool(row.get("unpushed_branches"))


def _lane_effective(row: Mapping[str, Any]) -> tuple[int, int]:
    """Row-level twin of ``effective_ahead_behind`` (rows, not records)."""
    mismatch = row.get("upstream_mismatch") or {}
    if mismatch:
        return (
            int(mismatch.get("ahead_vs_same_name") or 0),
            int(mismatch.get("behind_vs_same_name") or 0),
        )
    return int(row.get("ahead") or 0), int(row.get("behind") or 0)


def lane_kind_for_row(row: Mapping[str, Any]) -> str | None:
    """The one lane this row belongs to, or ``None`` when it needs no lane.

    First match down :data:`LANE_KINDS` wins, so the assignment is total and
    order-independent of how the rows arrived.
    """
    if not _is_issue_row(dict(row)):
        return None
    if _lane_withhold_reason(row):
        return LANE_WITHHELD

    dirty = "dirty" in (row.get("classes") or [])
    ahead, behind = _lane_effective(row)
    unregistered = row.get("registration") == "unregistered"

    if ahead > 0 and behind > 0:
        return LANE_DIVERGED
    if dirty and behind > 0:
        return LANE_DIRTY_BEHIND
    if behind > 0:
        return LANE_CONVERGE
    if ahead > 0:
        return LANE_PUSH_AHEAD
    if dirty and unregistered:
        return LANE_UNREGISTERED_DIRTY
    if dirty:
        return LANE_SMALL_DIRTY
    return LANE_MECHANICAL


def _lane_family_key(row: Mapping[str, Any]) -> str:
    """The shared-store family a row belongs to.

    A linked worktree keys on its PRIMARY, so a repo and its worktrees are one
    unit. This is the hard rule the live run paid for: lanes partitioned by
    directory let L2 push L4's branch through the shared git dir.
    """
    return str(row.get("worktree_of") or row.get("path") or "")


def build_lane_plan(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Partition issue rows into dispatchable lanes. Pure and deterministic.

    Returns ``[]`` when nothing needs a lane, so the envelope key stays absent
    on a healthy estate (additive contract).
    """
    families: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        families.setdefault(_lane_family_key(row), []).append(row)

    # One kind per FAMILY, taken from its most urgent member: a worktree and
    # its parent must never be dispatched to two different lanes.
    family_kind: dict[str, str] = {}
    for key, members in families.items():
        kinds = [
            kind
            for kind in (lane_kind_for_row(member) for member in members)
            if kind is not None
        ]
        if kinds:
            family_kind[key] = min(kinds, key=LANE_KINDS.index)

    lanes: list[dict[str, Any]] = []
    for kind in EMITTED_LANE_KINDS:
        members = sorted(key for key, value in family_kind.items() if value == kind)
        if not members:
            continue
        repos: list[str] = []
        write_scope: list[str] = []
        withholds: list[dict[str, str]] = []
        for key in members:
            family = families.get(key, [])
            # write_scope is the WHOLE shared store, including checkouts that
            # are clean: writing through a shared git dir touches every
            # worktree on it, so a clean sibling still belongs to the scope.
            for member in family:
                path = str(member.get("path") or "")
                if path and path not in write_scope:
                    write_scope.append(path)
                if path and lane_kind_for_row(member) is not None and path not in repos:
                    repos.append(path)
                reason = _lane_withhold_reason(member) if kind == LANE_WITHHELD else None
                if reason:
                    withholds.append({"path": path, "reason": reason})
        lane: dict[str, Any] = {
            "id": f"L{len(lanes) + 1}",
            "kind": kind,
            "repos": sorted(repos),
            "write_scope": sorted(write_scope),
            "rationale": _LANE_RATIONALE[kind],
            # One agent per independent family, capped: families in a lane
            # share no git dir, so they are safe to run in parallel.
            "suggested_concurrency": min(len(members), MAX_LANE_CONCURRENCY),
        }
        if withholds:
            lane["withheld"] = sorted(withholds, key=lambda item: item["path"])
            # A withheld lane is not dispatchable work; it is a read.
            lane["suggested_concurrency"] = 0
        lanes.append(lane)
    return lanes


def _is_issue_row(row: dict[str, Any]) -> bool:
    """A row that earns footer next_actions: non-clean band, unpushed
    branches (silent-loss class), a misconfigured upstream (correctly banded
    clean, but still carrying a repair), or a non-quiet amp verdict on a clean
    HEAD (an Orb problem hides behind a clean tree the same way)."""
    return bool(
        row.get("risk_band") != "clean"
        or row.get("unpushed_branches")
        or row.get("upstream_mismatch")
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


def worktree_primary(
    record: GitRepoRecord, scanned: Mapping[str, str] | None = None
) -> str | None:
    """The main checkout behind a linked worktree, or ``None``.

    A linked worktree is ``git_dir != common_dir`` -- git's own definition, and
    the same one the amp campaign guard uses, so guard and scan cannot disagree.

    The primary path is taken from a scanned sibling whenever one exists
    (authoritative: that row IS the main worktree). Otherwise it is derived
    from the store path, because git puts a main worktree's store at
    ``<primary>/.git`` -- which is how a worktree whose parent lives outside
    the scan roots still names its parent instead of reporting nothing. A store
    that is not a ``.git`` directory (a bare repo serving worktrees) yields
    ``None`` rather than a guess at its parent directory.
    """
    git_dir = record.git_dir
    common = record.common_dir
    if not git_dir or not common or git_dir == common:
        return None
    if scanned and common in scanned:
        return scanned[common]
    parent = Path(common)
    if parent.name != ".git":
        return None
    return str(parent.parent)


def worktree_primaries(records: Sequence[GitRepoRecord]) -> dict[str, str]:
    """``common_dir -> path`` for every scanned MAIN worktree.

    Feeds :func:`worktree_primary` so a linked worktree prefers a real scanned
    sibling over a path derived from the store layout.
    """
    return {
        record.common_dir: record.path
        for record in records
        if record.common_dir and record.git_dir == record.common_dir
    }


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
    registry_entry: Mapping[str, Any] | None = None,
    operator_owner: str | None = None,
    worktree_parents: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row = record.to_dict()
    row["risk_band"] = RISK_BAND_NAMES[risk_band(record)]
    row["registration"] = registration
    # Additive and present only on linked worktrees, so an estate without any
    # renders byte-identically. Lane partitioning (v6ac.1.2) groups on this:
    # directory-based partitioning let one lane push another's branch through
    # the shared git dir, so grouping has to be mechanical, not path-shaped.
    parent = worktree_primary(record, worktree_parents)
    if parent:
        row["worktree_of"] = parent
    ownership = derive_ownership(
        record, registry_entry=registry_entry, operator_owner=operator_owner
    )
    row.update(ownership)
    # The fix list is derived AFTER ownership so the ahead-of-upstream advice
    # can be gated on it. This ordering is the whole point of the bead.
    row["fix"] = fix_commands(
        record, registration, registry_path, ownership["push_policy"]
    )
    _attribute_stash(row, stash_owner)
    return row


def _registry_entry_map(
    module: Any, repo_entries: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """normalized path -> registry entry, from the ONE registry parse.

    ``normalize_registry`` already preserves every field on each entry, so the
    ownership join costs no extra read: it is a lookup over the same payload
    that supplies ignore rules and registration states.
    """
    if module is None:
        return {}
    return {str(entry["path"]): entry for entry in repo_entries if entry.get("path")}


def _entry_for(
    module: Any, entries: Mapping[str, Mapping[str, Any]], path: str
) -> Mapping[str, Any] | None:
    if module is None or not entries:
        return None
    return entries.get(module.normalize_path(path))


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

    module, rules, repo_entries, registry_reason, operator_owner = _load_registry_rules()
    kept, ignored_count = _split_ignored(records, module, rules)
    registration = _registration_states(kept, module, repo_entries)
    stale_entries = _stale_registered_entries(repo_entries)
    entry_map = _registry_entry_map(module, repo_entries)
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
    worktree_parents = worktree_primaries(kept)

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
            _entry_for(module, entry_map, cwd_record.path),
            operator_owner,
            worktree_parents,
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
                _entry_for(module, entry_map, record.path),
                operator_owner,
                worktree_parents,
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
    # Additive: absent entirely on an estate with nothing to dispatch, so a
    # healthy envelope is byte-identical to before.
    lanes = build_lane_plan(report["repos"])
    if lanes:
        report["lanes"] = lanes
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
        # Push policy earns a marker ONLY where it changes the advice: a row
        # that is ahead of its upstream and may not simply push. Marking every
        # external or unknown row would put a badge on repos nobody was about
        # to publish, and the table stays column-stable either way.
        policy = row.get("push_policy")
        # Effective ahead, not the raw column: a misconfigured upstream shows
        # ahead > 0 against the wrong ref while having nothing to publish, and
        # badging that row with a push policy would be advice about work that
        # does not exist.
        mismatch = row.get("upstream_mismatch") or {}
        effective_ahead = (
            int(mismatch.get("ahead_vs_same_name") or 0)
            if mismatch
            else int(row.get("ahead") or 0)
        )
        if effective_ahead > 0 and policy and policy != PUSH_POLICY_PUSH:
            marker += f"  [{policy}]"
        # A branch measured against the wrong ref. The A/B column still shows
        # what the CONFIGURED upstream says (that is a real fact about the
        # config), so the marker is what tells a reader the numbers beside it
        # are a config artifact rather than unpublished work.
        if row.get("upstream_mismatch"):
            marker += "  [upstream-misconfigured]"
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
        # Correctly banded clean, but it still has a config repair to hand
        # over. Folding it would trade a false alarm for silence.
        or r.get("upstream_mismatch")
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
    lanes = report.get("lanes") or []
    if lanes:
        # One line by contract: the plan is for machines, and reprinting it as
        # a table would bury the rows the operator came to read.
        kinds = ", ".join(f"{lane['id']} {lane['kind']}" for lane in lanes)
        lines.append(f"lanes: {len(lanes)} ({kinds}) — use --json for write scopes")
    if report.get("backlog"):
        lines.append(f"backlog: {report['backlog']}")
    return lines
