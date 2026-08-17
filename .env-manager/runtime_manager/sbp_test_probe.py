"""``probe-receipt/v1``: bounded, opt-in probes inside a disposable capsule.

The static scorer (:mod:`runtime_manager.sbp_test_scorer`) reads bytes and never
executes. That ceiling is real: a static read can say a lane *looks* like it
emits no per-unit evidence, or that two runs *look* like they would collide on a
fixed endpoint, but it cannot say so with proof. The registry has a name for
that gap -- ``likely`` -- and this module is the only thing allowed to close it.

What a probe is
---------------
A probe runs the suite under a deliberately hostile arrangement and records what
happened: the same units repeated serially, two-way and N-way concurrently, in a
seeded-random order, and alongside a **harness-owned synthetic failing canary**
whose only job is to prove that a sibling failure cancels its siblings and that
the workspace is cleaned up afterwards.

Fail-closed authority
---------------------
Executing a repository's own commands is categorically different from reading
its files, so ``--probe`` is not a convenience flag with a sensible default. All
five of these must be **explicit and valid** or the whole mode refuses before
anything runs:

1. an admitted, disposable **capsule workspace** that is not the consumer tree;
2. a **wall-clock budget**;
3. a **maximum parallelism**;
4. **service permission** (may a probe touch an external service at all);
5. **network permission**.

There is no interactive prompt and no inferred default. A missing authority is a
typed refusal naming exactly which one is missing -- an agent must be able to
fix its own invocation from the error, and must never be able to reach execution
by omission.

Evidence versus refusal
-----------------------
The distinction the CLI exists to preserve: **a probe that ran and failed is
evidence** (the suite really is unsafe that way, and that is a finding). **A
probe that could not run is a refusal** (we learned nothing, and nothing may be
upgraded). Collapsing the two is how a test system trains people to re-run
instead of read, so ``ran`` / ``refused`` / ``failed`` are separate states and
the receipt reports all three counts.

Upgrades are exact, never generous
----------------------------------
:data:`PROOF_REQUIREMENTS` maps a finding code to the one probe outcome that
would prove it. A ``likely`` finding becomes ``proven`` only when that exact
requirement holds; a probe that ran but did not establish the requirement
upgrades nothing and records why. Codes with no entry are never upgradeable --
no probe can prove the absence of a partition vocabulary, so
``CROSS_MACHINE_PARTITION_MISSING`` is deliberately absent from the table.

Consumer trees are never touched
--------------------------------
The canary is constructed here, carries a reserved id prefix, and runs entirely
inside the capsule scratch directory. It never edits a consumer source file and
never rewrites an assertion -- a "probe" that made the suite fail by modifying
the suite would be proving something about itself.

Determinism
-----------
The receipt is byte-stable: no timestamps, no absolute paths (the workspace
enters as a digest), seeded shuffles, and a total order on every list. Wall-clock
elapsed is deliberately *outside* the receipt, because a deterministic document
cannot contain a number that changes on every host. That is what makes
:func:`receipt_digest` a meaningful identity and before/after comparison honest.

Standard library only. Execution reaches the outside world through one injected
runner seam, so every test in this slice is bounded and fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import sbp_test_findings as R

PROBE_SCHEMA = "probe-receipt/v1"
PROBE_SCHEMA_VERSION = 1

#: The admission marker a disposable capsule workspace must carry. A directory
#: is not self-evidently disposable, and guessing wrong deletes someone's work.
WORKSPACE_SCHEMA = "probe-workspace/v1"
WORKSPACE_MARKER = ".skillbox-probe-workspace.json"

#: Scratch lives inside the workspace so cleanup and leak checks have exactly
#: one place to look.
PROBE_SCRATCH_DIRNAME = ".sbp-probe"

#: Bounds. A budget or fan-out an agent can set to infinity is not a bound.
MAX_WALL_CLOCK_BUDGET_S = 3600.0
MAX_PROBE_PARALLELISM = 64
MAX_REPEATS = 16

#: Every synthetic unit this module injects carries this prefix. It is reserved:
#: a consumer unit that used it would be indistinguishable from the canary.
CANARY_UNIT_PREFIX = "__sbp_probe_canary__"

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

PROBE_SERIAL_REPEAT = "serial_repeat"
PROBE_CONCURRENCY_TWO = "concurrency_two"
PROBE_CONCURRENCY_N = "concurrency_n"
PROBE_RANDOMIZED_ORDER = "randomized_order"
PROBE_SYNTHETIC_CANARY = "synthetic_canary"
PROBE_CLEANUP_LEAK = "cleanup_leak"

#: Execution order is fixed: cheap agreement probes first, the destructive
#: canary next-to-last, and the leak check last so it observes everything.
PROBE_KINDS: tuple[str, ...] = (
    PROBE_SERIAL_REPEAT,
    PROBE_CONCURRENCY_TWO,
    PROBE_CONCURRENCY_N,
    PROBE_RANDOMIZED_ORDER,
    PROBE_SYNTHETIC_CANARY,
    PROBE_CLEANUP_LEAK,
)

#: ``ran`` -- executed and observed. ``failed`` -- executed and the suite did not
#: hold up (evidence). ``refused`` -- never executed (not evidence).
STATE_RAN = "ran"
STATE_FAILED = "failed"
STATE_REFUSED = "refused"
PROBE_STATES: tuple[str, ...] = (STATE_RAN, STATE_FAILED, STATE_REFUSED)

REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "probe_workspace_missing",
        "probe_workspace_not_admitted",
        "probe_workspace_not_disposable",
        "probe_workspace_inside_consumer_tree",
        "probe_capsule_archive_unverified",
        "probe_capsule_mismatch",
        "probe_budget_missing",
        "probe_budget_invalid",
        "probe_budget_exhausted",
        "probe_parallelism_missing",
        "probe_parallelism_invalid",
        "probe_service_permission_missing",
        "probe_network_permission_missing",
        "probe_repeats_invalid",
        "probe_runner_missing",
        "probe_canary_unsafe",
        "probe_units_missing",
        "probe_services_denied_but_required",
    }
)

#: Refusals that mean "the caller did not give us enough authority", as opposed
#: to "we broke". The CLI maps these onto the needs-input rung.
NEEDS_INPUT_CODES: frozenset[str] = frozenset(
    {
        "probe_workspace_missing",
        "probe_workspace_not_admitted",
        "probe_workspace_not_disposable",
        "probe_workspace_inside_consumer_tree",
        "probe_capsule_archive_unverified",
        "probe_capsule_mismatch",
        "probe_budget_missing",
        "probe_budget_invalid",
        "probe_parallelism_missing",
        "probe_parallelism_invalid",
        "probe_service_permission_missing",
        "probe_network_permission_missing",
        "probe_repeats_invalid",
        "probe_runner_missing",
        "probe_units_missing",
        "probe_services_denied_but_required",
    }
)


class ProbeRefusal(Exception):
    """A typed, fail-closed refusal to probe. Never a traceback, never a prompt."""

    def __init__(
        self, code: str, message: str, *, next_actions: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_actions = tuple(next_actions)

    @property
    def needs_input(self) -> bool:
        return self.code in NEEDS_INPUT_CODES

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "error_code": self.code,
            "probe_state": STATE_REFUSED,
            "next_actions": list(self.next_actions),
        }


def _refuse(code: str, message: str, *, next_actions: Sequence[str] = ()) -> Any:
    raise ProbeRefusal(code, message, next_actions=next_actions)


# --------------------------------------------------------------------------- #
# Capsule workspace admission
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdmittedWorkspace:
    """A capsule workspace that passed every admission check.

    Holding one of these is the proof of admission: nothing in this module
    executes without one, and it cannot be constructed except through
    :func:`admit_workspace`.
    """

    root: Path
    archive_sha256: str
    marker_digest: str

    @property
    def scratch(self) -> Path:
        return self.root / PROBE_SCRATCH_DIRNAME

    def to_payload(self) -> dict[str, Any]:
        # The absolute path is deliberately absent: a receipt that embedded it
        # would differ between hosts for reasons that have nothing to do with
        # what was proven.
        return {
            "archive_sha256": self.archive_sha256,
            "workspace_digest": self.marker_digest,
            "disposable": True,
        }


def write_workspace_marker(
    workspace: Path, archive_sha256: str, *, disposable: bool = True
) -> Path:
    """Stamp a directory as an admitted disposable capsule workspace.

    Separate from admission on purpose: whoever materializes a capsule says so
    once, in a file, and every later probe re-reads that claim rather than
    inferring disposability from a path shape.
    """

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / WORKSPACE_MARKER
    marker.write_text(
        json.dumps(
            {
                "schema": WORKSPACE_SCHEMA,
                "archive_sha256": str(archive_sha256),
                "disposable": bool(disposable),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return marker


def _is_within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
    except ValueError:
        return False
    return True


def admit_workspace(
    workspace: Path | None,
    *,
    consumer_root: Path,
    archive_sha256: str | None = None,
    verify_archive: Callable[[str], bool] | None = None,
) -> AdmittedWorkspace:
    """Admit a disposable capsule workspace, or refuse with a named reason.

    The consumer-tree check is the one that matters most. A probe writes scratch
    and runs a failing canary; pointing that at the repository being scored would
    mutate the very thing the report claims to describe. So a workspace that *is*
    the consumer root, or lives inside it, is refused outright -- there is no
    flag to override it.
    """

    consumer_root = Path(consumer_root).resolve()
    if workspace is None:
        _refuse(
            "probe_workspace_missing",
            "probe mode requires an explicit disposable capsule workspace",
            next_actions=["sbp test score --probe --probe-workspace <dir> ..."],
        )
    root = Path(workspace)
    if not root.is_dir():
        _refuse(
            "probe_workspace_missing",
            f"probe workspace {root.name!r} is not a directory",
            next_actions=["materialize the capsule workspace before probing"],
        )
    root = root.resolve()

    if root == consumer_root or _is_within(root, consumer_root):
        _refuse(
            "probe_workspace_inside_consumer_tree",
            "refusing to probe inside the tree being scored; a probe writes "
            "scratch and runs a failing canary, so it must never share a tree "
            "with the source it is reporting on",
            next_actions=["point --probe-workspace at a disposable capsule outside this repo"],
        )

    marker = root / WORKSPACE_MARKER
    if not marker.is_file():
        _refuse(
            "probe_workspace_not_admitted",
            f"probe workspace carries no {WORKSPACE_MARKER}; a directory is not "
            "self-evidently a disposable capsule",
            next_actions=["admit the capsule workspace, then retry"],
        )
    raw = marker.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _refuse(
            "probe_workspace_not_admitted",
            f"{WORKSPACE_MARKER} is unreadable",
            next_actions=["re-admit the capsule workspace"],
        )
    if not isinstance(document, Mapping) or document.get("schema") != WORKSPACE_SCHEMA:
        _refuse(
            "probe_workspace_not_admitted",
            f"{WORKSPACE_MARKER} is not a {WORKSPACE_SCHEMA} document",
            next_actions=["re-admit the capsule workspace"],
        )
    if document.get("disposable") is not True:
        _refuse(
            "probe_workspace_not_disposable",
            "probe workspace is not marked disposable; probes are destructive by "
            "design and only run where destruction is expected",
            next_actions=["use a disposable capsule workspace"],
        )

    admitted_digest = str(document.get("archive_sha256") or "")
    if archive_sha256 is not None and admitted_digest != str(archive_sha256):
        _refuse(
            "probe_capsule_mismatch",
            "the admitted workspace was built from a different capsule than the "
            "one requested; a receipt bound to the wrong bytes proves nothing",
            next_actions=["re-materialize the workspace from the requested capsule"],
        )
    if verify_archive is not None and not verify_archive(admitted_digest):
        _refuse(
            "probe_capsule_archive_unverified",
            "the capsule archive backing this workspace did not re-verify; "
            "refusing to probe against unverifiable bytes",
            next_actions=["rebuild the capsule with sbp test capsule"],
        )

    return AdmittedWorkspace(
        root=root,
        archive_sha256=admitted_digest,
        marker_digest=hashlib.sha256(raw).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# Authority
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeAuthority:
    """The five explicit authorities, plus the two bounded knobs.

    Every gating field defaults to ``None`` rather than to a permissive value.
    That is the whole design: omission can only ever produce a refusal, never an
    inferred permission, so no invocation reaches execution by accident.
    """

    workspace: Path | None = None
    wall_clock_budget_s: float | None = None
    max_parallel: int | None = None
    allow_services: bool | None = None
    allow_network: bool | None = None
    archive_sha256: str | None = None
    repeats: int = 3
    seed: int = 0

    def validated(self) -> ProbeAuthority:
        """Check the scalar authorities. Raises :class:`ProbeRefusal`."""

        if self.wall_clock_budget_s is None:
            _refuse(
                "probe_budget_missing",
                "probe mode requires an explicit wall-clock budget",
                next_actions=["pass --probe-budget-s <seconds>"],
            )
        try:
            budget = float(self.wall_clock_budget_s)
        except (TypeError, ValueError):
            budget = float("nan")
        if not budget > 0 or budget != budget or budget > MAX_WALL_CLOCK_BUDGET_S:
            _refuse(
                "probe_budget_invalid",
                f"wall-clock budget must be within 0 < b <= {MAX_WALL_CLOCK_BUDGET_S}s",
                next_actions=["pass a --probe-budget-s inside the permitted range"],
            )

        if self.max_parallel is None:
            _refuse(
                "probe_parallelism_missing",
                "probe mode requires an explicit maximum parallelism",
                next_actions=["pass --probe-max-parallel <n>"],
            )
        if type(self.max_parallel) is not int or not 1 <= self.max_parallel <= MAX_PROBE_PARALLELISM:
            _refuse(
                "probe_parallelism_invalid",
                f"max parallelism must be an integer within 1..{MAX_PROBE_PARALLELISM}",
                next_actions=["pass a --probe-max-parallel inside the permitted range"],
            )

        # Service and network permission are separate questions and neither
        # implies the other: a probe may legitimately need a local database and
        # no egress, or egress and no service.
        if self.allow_services is None:
            _refuse(
                "probe_service_permission_missing",
                "probe mode requires an explicit service permission decision",
                next_actions=["pass --probe-allow-services or --probe-deny-services"],
            )
        if self.allow_network is None:
            _refuse(
                "probe_network_permission_missing",
                "probe mode requires an explicit network permission decision",
                next_actions=["pass --probe-allow-network or --probe-deny-network"],
            )

        if type(self.repeats) is not int or not 2 <= self.repeats <= MAX_REPEATS:
            _refuse(
                "probe_repeats_invalid",
                f"repeat count must be an integer within 2..{MAX_REPEATS}; a single "
                "run cannot establish agreement",
                next_actions=["pass a --probe-repeats inside the permitted range"],
            )
        return self

    def to_payload(self) -> dict[str, Any]:
        return {
            "wall_clock_budget_s": float(self.wall_clock_budget_s or 0.0),
            "max_parallel": int(self.max_parallel or 0),
            "repeats": int(self.repeats),
            "seed": int(self.seed),
            "services_permitted": bool(self.allow_services),
            "network_permitted": bool(self.allow_network),
        }


# --------------------------------------------------------------------------- #
# The runner seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeUnit:
    """One unit a probe will ask the runner to execute.

    ``services`` and ``artifacts`` are carried from the declared manifest rather
    than guessed: the first is what the service-permission gate is decided
    against, and the second is what "did this lane emit per-unit evidence" is
    measured against. A probe that inferred either would be grading its own
    homework.
    """

    id: str
    argv: tuple[str, ...] = ()
    synthetic: bool = False
    services: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeRun:
    """One bounded execution request handed to the runner."""

    probe_kind: str
    attempt: int
    units: tuple[ProbeUnit, ...]
    max_parallel: int
    workspace: Path
    scratch: Path
    allow_services: bool
    allow_network: bool
    deadline_s: float


@dataclass(frozen=True)
class ProbeObservation:
    """What the runner saw. Every field is something a probe reasons about."""

    unit_states: Mapping[str, str] = field(default_factory=dict)
    peak_concurrency: int = 1
    per_unit_records: tuple[str, ...] = ()
    cancelled_units: tuple[str, ...] = ()
    residual_paths: tuple[str, ...] = ()
    elapsed_s: float = 0.0

    def states_signature(self) -> str:
        """A stable identity for "did these runs agree"."""

        return json.dumps(
            {str(k): str(v) for k, v in sorted(self.unit_states.items())},
            sort_keys=True,
        )


#: The one seam through which this module reaches the outside world.
ProbeRunner = Callable[[ProbeRun], ProbeObservation]


# --------------------------------------------------------------------------- #
# The local-executor adapter -- the seam, actually connected
# --------------------------------------------------------------------------- #

#: Environment handed to a probed unit. Deliberately tiny: a probe that inherited
#: the operator's whole shell would be testing that shell as much as the suite,
#: and a stray token in the parent environment would ride along into a run whose
#: whole premise is that it is disposable.
_PROBE_ENV_KEEP: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")

#: Marker variables the probed suite can read to see the permissions it is under.
#: They are a *declaration*, not a sandbox -- see ``network_enforcement`` below.
ENV_SERVICES_ALLOWED = "SKILLBOX_PROBE_ALLOW_SERVICES"
ENV_NETWORK_ALLOWED = "SKILLBOX_PROBE_ALLOW_NETWORK"
ENV_WORKSPACE = "SKILLBOX_PROBE_WORKSPACE"

#: How honestly each permission is enforced. Recorded in the receipt because
#: overclaiming here would be exactly the lie this module exists to prevent:
#: refusing a unit that declares a service is a real gate; there is no stdlib way
#: to revoke a process's network, so denial there is a declared contract that the
#: units are expected to honour, and the receipt says so in those words.
PERMISSION_ENFORCEMENT: Mapping[str, str] = {
    "services": "refused_before_launch",
    "network": "declared_only",
}


def _probe_base_env(run: ProbeRun) -> dict[str, str]:
    env = {name: os.environ[name] for name in _PROBE_ENV_KEEP if name in os.environ}
    env[ENV_SERVICES_ALLOWED] = "1" if run.allow_services else "0"
    env[ENV_NETWORK_ALLOWED] = "1" if run.allow_network else "0"
    env[ENV_WORKSPACE] = str(run.workspace)
    return env


def _plan_for(run: ProbeRun) -> dict[str, Any]:
    """Project one :class:`ProbeRun` into a sealed-plan shape the executor accepts.

    Three arrangements, each chosen so the executor's own scheduler produces the
    concurrency the probe is asking about rather than a coincidence:

    * **canary present** -- the canary gets wave 0 alone and every real unit
      declares ``depends_on`` it in wave 1. When the canary fails, the executor's
      own ``blast_radius`` calls the siblings off. That is real cancellation
      through the real code path, and it is deterministic, which a race against a
      sleeping sibling would not be.
    * **width 1** -- one unit per wave, in the order the probe asked for. This is
      what makes ``randomized_order`` mean anything: ``schedule_batches`` packs a
      wave in sorted id order, so a shuffle expressed inside a single wave would
      be silently re-sorted away.
    * **width > 1** -- every unit in one wave, and the executor packs it up to the
      cap.
    """

    timeout = max(1, int(run.deadline_s)) if run.deadline_s > 0 else 1
    canaries = [unit.id for unit in run.units if unit.synthetic]
    real = [unit.id for unit in run.units if not unit.synthetic]

    units: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for unit in run.units:
        units.append(
            {
                "id": unit.id,
                "argv": list(unit.argv),
                # cwd stays None: the executor resolves it against `repo`, which
                # this adapter points at the capsule workspace, never the tree
                # being scored.
                "cwd": None,
                "timeout_s": timeout,
                "runnable": True,
                "needs": {},
                "env_allowlist": [],
                "blocked_by": [],
            }
        )
        if canaries and not unit.synthetic:
            edges.extend(
                {"source": unit.id, "target": canary, "kind": "depends_on"}
                for canary in canaries
            )

    if canaries:
        waves = [canaries, real] if real else [canaries]
    elif run.max_parallel <= 1:
        waves = [[unit.id] for unit in run.units]
    else:
        waves = [[unit.id for unit in run.units]]
    return {"units": units, "waves": waves, "edges": edges}


def _workspace_snapshot(workspace: Path, scratch: Path) -> set[str]:
    """Every path in the capsule except the probe's own scratch.

    The exclusion matters: scratch is where logs and placement blocks are
    *supposed* to land, so counting them as leaks would make every run look dirty
    and train a reader to ignore the field.
    """

    snapshot: set[str] = set()
    for path in workspace.rglob("*"):
        if path == scratch or scratch in path.parents:
            continue
        snapshot.add(str(path.relative_to(workspace)))
    return snapshot


def local_executor_runner(
    *, execute: Callable[..., Any] | None = None
) -> ProbeRunner:
    """Bind :func:`sbp_test_executor.execute_plan` into the :data:`ProbeRunner` seam.

    This is the whole point of the slice: probe mode reuses the wave-concurrent
    local executor that already exists rather than growing a second, weaker
    scheduler that would drift from it. Nothing here relaxes an authority --
    :func:`run_probes` has already refused unless all five are explicit, and the
    workspace has already been admitted as disposable and outside the consumer
    tree, so by the time this runs the only remaining question is what the suite
    does.

    ``execute`` is injectable purely so a test can assert the adapter's plan
    projection without launching a process.
    """

    from . import sbp_test_executor as executor  # noqa: PLC0415

    runner = executor.execute_plan if execute is None else execute

    def _run(run: ProbeRun) -> ProbeObservation:
        scratch = Path(run.scratch) / f"attempt-{run.attempt:03d}"
        scratch.mkdir(parents=True, exist_ok=True)
        workspace = Path(run.workspace)
        before = _workspace_snapshot(workspace, Path(run.scratch))
        started = time.monotonic()

        outcome = runner(
            _plan_for(run),
            repo=workspace,
            log_root=scratch,
            max_parallel=run.max_parallel,
            base_env=_probe_base_env(run),
        )

        payload = outcome.to_payload()
        results = {row["unit_id"]: row for row in payload.get("results") or []}
        states = {uid: str(row.get("state") or "") for uid, row in results.items()}

        # Work that was called off rather than run: the executor marks a
        # dependency-blocked unit `skipped` with the blocker named, and a killed
        # one `cancelled`. Both are "this did not run because something else
        # failed", which is what the canary probe is asking about.
        cancelled = tuple(
            sorted(
                uid
                for uid, state in states.items()
                if state in ("skipped", "cancelled")
            )
        )

        # Concurrency actually reached, not merely scheduled: a batch whose units
        # were all skipped never ran concurrently, and counting it would let a
        # blocked wave masquerade as a fan-out.
        launched = {uid for uid, state in states.items() if state not in ("skipped", "not_run")}
        peak = 1
        for batch in (payload.get("schedule") or {}).get("batches") or []:
            peak = max(peak, sum(1 for uid in batch.get("units") or [] if uid in launched))

        # Per-unit evidence: a declared artifact that actually exists. Declaring
        # one and not emitting it is exactly the RECEIPT_NOT_COMPOSABLE shape, so
        # the file has to be on disk to count.
        records: list[str] = []
        for unit in run.units:
            for artifact in unit.artifacts:
                candidate = workspace / artifact
                if candidate.exists():
                    records.append(f"{unit.id}:{artifact}")

        residual = tuple(sorted(_workspace_snapshot(workspace, Path(run.scratch)) - before))
        return ProbeObservation(
            unit_states=states,
            peak_concurrency=peak,
            per_unit_records=tuple(sorted(records)),
            cancelled_units=cancelled,
            residual_paths=residual,
            elapsed_s=time.monotonic() - started,
        )

    return _run


def assert_canary_safe(unit: ProbeUnit, workspace: AdmittedWorkspace) -> None:
    """The canary must be harness-owned and confined to the capsule.

    Checked at injection time rather than trusted, because the canary is the one
    unit this module invents. A synthetic unit that could reach a consumer path
    would make every probe result meaningless.
    """

    if not unit.synthetic or not unit.id.startswith(CANARY_UNIT_PREFIX):
        _refuse(
            "probe_canary_unsafe",
            "a synthetic canary must be harness-owned and carry the reserved prefix",
        )
    for argument in unit.argv:
        candidate = Path(argument)
        if not candidate.is_absolute():
            continue
        if not _is_within(candidate, workspace.root):
            _refuse(
                "probe_canary_unsafe",
                "the canary referenced a path outside the capsule workspace; a "
                "canary may never touch consumer source or assertions",
            )


def build_canary(workspace: AdmittedWorkspace) -> ProbeUnit:
    """A unit that always fails, owned entirely by this harness.

    It writes nothing and reads nothing: a bare non-zero exit is sufficient to
    test sibling cancellation, and anything more would be a side effect a probe
    has no business having.
    """

    unit = ProbeUnit(
        id=f"{CANARY_UNIT_PREFIX}fail",
        argv=("python3", "-c", "raise SystemExit(1)"),
        synthetic=True,
    )
    assert_canary_safe(unit, workspace)
    return unit


# --------------------------------------------------------------------------- #
# Probe results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """One probe's outcome. ``refused`` is never rendered as ``failed``."""

    kind: str
    state: str
    attempts: int = 0
    observations: Mapping[str, Any] = field(default_factory=dict)
    established: tuple[str, ...] = ()
    refusal_code: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PROBE_KINDS:
            raise ValueError(f"unknown probe kind {self.kind!r}")
        if self.state not in PROBE_STATES:
            raise ValueError(f"unknown probe state {self.state!r}")
        object.__setattr__(self, "established", tuple(sorted(set(self.established))))

    @property
    def executed(self) -> bool:
        """A probe that ran, whether or not the suite held up."""

        return self.state in (STATE_RAN, STATE_FAILED)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "attempts": self.attempts,
            "observations": _canonical(self.observations),
            "established": list(self.established),
            "refusal_code": self.refusal_code,
            "detail": self.detail,
        }


def _canonical(value: Any) -> Any:
    """Sort every mapping so a receipt renders the same bytes everywhere."""

    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Proof requirements -- the exact rule for every upgrade
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProofRequirement:
    """What one probe must establish before a ``likely`` may become ``proven``.

    ``token`` is the string a probe records in :attr:`ProbeResult.established`
    when the requirement held. Matching is exact string equality against a probe
    of exactly the named kind -- no scoring, no "close enough", because a
    generous upgrade rule is indistinguishable from no rule at all.
    """

    finding_code: str
    probe_kind: str
    token: str
    statement: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "finding_code": self.finding_code,
            "probe_kind": self.probe_kind,
            "token": self.token,
            "statement": self.statement,
        }


#: Deliberately small. A code is here only when a probe can establish its
#: invariant by observation; everything else stays ``likely`` forever, which is
#: the honest answer.
PROOF_REQUIREMENTS: Mapping[str, ProofRequirement] = {
    requirement.finding_code: requirement
    for requirement in (
        ProofRequirement(
            "RECEIPT_NOT_COMPOSABLE",
            PROBE_SERIAL_REPEAT,
            "no_per_unit_records",
            "repeated serial runs completed and emitted no per-unit evidence at all",
        ),
        ProofRequirement(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            PROBE_CONCURRENCY_N,
            "serialized_under_fanout",
            "an N-way fan-out never exceeded one concurrent unit, so the suite "
            "serializes itself and an external scheduler cannot own ordering",
        ),
        ProofRequirement(
            "SERVICE_ENDPOINT_STATIC",
            PROBE_CONCURRENCY_TWO,
            "concurrent_pair_collided",
            "two concurrent runs of the same units collided, which a per-run "
            "endpoint allocation would have prevented",
        ),
    )
}

#: Registry codes that no probe can ever establish. Recorded so the absence is a
#: decision rather than an oversight.
NEVER_PROVABLE_BY_PROBE: frozenset[str] = frozenset(set(R.CODES) - set(PROOF_REQUIREMENTS))


def _validate_requirements() -> None:
    """Import-time guard: a requirement for an unregistered code is a broken map."""

    for code, requirement in PROOF_REQUIREMENTS.items():
        if code not in R.CODES:
            raise ValueError(f"proof requirement names unregistered code {code!r}")
        if requirement.probe_kind not in PROBE_KINDS:
            raise ValueError(f"{code}: unknown probe kind {requirement.probe_kind!r}")


_validate_requirements()


# --------------------------------------------------------------------------- #
# The probes
# --------------------------------------------------------------------------- #


class _Budget:
    """Wall-clock accounting. Exhaustion refuses the rest; it never truncates silently."""

    def __init__(self, budget_s: float, clock: Callable[[], float]) -> None:
        self._budget = float(budget_s)
        self._clock = clock
        self._start = clock()
        self.exhausted = False

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    @property
    def remaining(self) -> float:
        return self._budget - self.elapsed

    def check(self) -> bool:
        """True while there is budget left. Latches ``exhausted`` once spent."""

        if self.remaining <= 0:
            self.exhausted = True
            return False
        return True


def _agreement(observations: Sequence[ProbeObservation]) -> tuple[bool, int]:
    signatures = {observation.states_signature() for observation in observations}
    return len(signatures) <= 1, len(signatures)


def _run_repeated(
    runner: ProbeRunner,
    *,
    kind: str,
    units: Sequence[ProbeUnit],
    orders: Sequence[Sequence[ProbeUnit]],
    max_parallel: int,
    workspace: AdmittedWorkspace,
    authority: ProbeAuthority,
    budget: _Budget,
) -> tuple[list[ProbeObservation], bool]:
    """Run one probe's attempts under the budget. Returns (observations, complete)."""

    del units  # orders carries the per-attempt unit sequence
    seen: list[ProbeObservation] = []
    scratch = workspace.scratch / kind
    scratch.mkdir(parents=True, exist_ok=True)
    for attempt, ordering in enumerate(orders, start=1):
        if not budget.check():
            return seen, False
        seen.append(
            runner(
                ProbeRun(
                    probe_kind=kind,
                    attempt=attempt,
                    units=tuple(ordering),
                    max_parallel=max_parallel,
                    workspace=workspace.root,
                    scratch=scratch,
                    allow_services=bool(authority.allow_services),
                    allow_network=bool(authority.allow_network),
                    deadline_s=max(0.0, budget.remaining),
                )
            )
        )
    return seen, True


def _budget_refusal(kind: str) -> ProbeResult:
    return ProbeResult(
        kind=kind,
        state=STATE_REFUSED,
        refusal_code="probe_budget_exhausted",
        detail="the wall-clock budget was spent before this probe could run",
    )


def _probe_serial_repeat(context: _Context) -> ProbeResult:
    """The same units, repeated serially. Disagreement is non-determinism."""

    orders = [list(context.units)] * context.authority.repeats
    seen, complete = _run_repeated(
        context.runner,
        kind=PROBE_SERIAL_REPEAT,
        units=context.units,
        orders=orders,
        max_parallel=1,
        workspace=context.workspace,
        authority=context.authority,
        budget=context.budget,
    )
    if not complete:
        return _budget_refusal(PROBE_SERIAL_REPEAT)
    agreed, distinct = _agreement(seen)
    records = sorted({record for item in seen for record in item.per_unit_records})
    established: list[str] = []
    # The exact requirement: the runs must have COMPLETED and produced nothing
    # per-unit. A run that never finished proves nothing about its evidence.
    completed = all(
        state == "completed" for item in seen for state in item.unit_states.values()
    )
    if completed and not records:
        established.append("no_per_unit_records")
    return ProbeResult(
        kind=PROBE_SERIAL_REPEAT,
        state=STATE_RAN if agreed else STATE_FAILED,
        attempts=len(seen),
        observations={
            "agreed": agreed,
            "distinct_state_signatures": distinct,
            "per_unit_records": records,
            "all_units_completed": completed,
        },
        established=established,
        detail="" if agreed else "repeated serial runs disagreed; the suite is non-deterministic",
    )


def _probe_concurrency(context: _Context, kind: str, width: int) -> ProbeResult:
    """One run at a fixed width. Collisions and serialization are both findings."""

    seen, complete = _run_repeated(
        context.runner,
        kind=kind,
        units=context.units,
        orders=[list(context.units)],
        max_parallel=width,
        workspace=context.workspace,
        authority=context.authority,
        budget=context.budget,
    )
    if not complete:
        return _budget_refusal(kind)
    observation = seen[0]
    failures = sorted(
        unit for unit, state in observation.unit_states.items() if state != "completed"
    )
    peak = int(observation.peak_concurrency)
    established: list[str] = []
    if kind == PROBE_CONCURRENCY_TWO and failures and width >= 2 and peak >= 2:
        # Genuinely concurrent, and something broke that serial runs did not.
        established.append("concurrent_pair_collided")
    if kind == PROBE_CONCURRENCY_N and width >= 2 and peak <= 1 and not failures:
        # Asked for a fan-out, got a queue: the suite serialized itself.
        established.append("serialized_under_fanout")
    return ProbeResult(
        kind=kind,
        state=STATE_FAILED if failures else STATE_RAN,
        attempts=1,
        observations={
            "requested_parallelism": width,
            "peak_concurrency": peak,
            "failed_units": failures,
        },
        established=established,
        detail=(
            f"{len(failures)} unit(s) did not complete at parallelism {width}"
            if failures
            else ""
        ),
    )


def _probe_randomized_order(context: _Context) -> ProbeResult:
    """Seeded shuffles. Order dependence shows up as disagreement, reproducibly."""

    generator = random.Random(context.authority.seed)
    orders: list[list[ProbeUnit]] = []
    for _ in range(context.authority.repeats):
        shuffled = list(context.units)
        generator.shuffle(shuffled)
        orders.append(shuffled)
    seen, complete = _run_repeated(
        context.runner,
        kind=PROBE_RANDOMIZED_ORDER,
        units=context.units,
        orders=orders,
        max_parallel=1,
        workspace=context.workspace,
        authority=context.authority,
        budget=context.budget,
    )
    if not complete:
        return _budget_refusal(PROBE_RANDOMIZED_ORDER)
    agreed, distinct = _agreement(seen)
    return ProbeResult(
        kind=PROBE_RANDOMIZED_ORDER,
        state=STATE_RAN if agreed else STATE_FAILED,
        attempts=len(seen),
        observations={
            "agreed": agreed,
            "distinct_state_signatures": distinct,
            "seed": int(context.authority.seed),
            # The permutations are part of the receipt because they are seeded and
            # therefore reproducible: a disagreement can be re-run exactly.
            "orders": [[unit.id for unit in ordering] for ordering in orders],
        },
        detail="" if agreed else "unit order changed the outcome; the suite is order dependent",
    )


def _probe_synthetic_canary(context: _Context) -> ProbeResult:
    """A harness-owned failing unit, to prove siblings are cancelled and cleaned up.

    This is the only probe that deliberately introduces a failure, and it does so
    without touching a single consumer byte: the canary is a bare non-zero exit
    injected next to the real units.
    """

    canary = build_canary(context.workspace)
    units = [*context.units, canary]
    seen, complete = _run_repeated(
        context.runner,
        kind=PROBE_SYNTHETIC_CANARY,
        units=units,
        orders=[units],
        max_parallel=max(2, min(context.authority.max_parallel or 2, len(units))),
        workspace=context.workspace,
        authority=context.authority,
        budget=context.budget,
    )
    if not complete:
        return _budget_refusal(PROBE_SYNTHETIC_CANARY)
    observation = seen[0]
    canary_state = observation.unit_states.get(canary.id, "")
    cancelled = sorted(observation.cancelled_units)
    residual = sorted(observation.residual_paths)
    # Both halves must hold: the canary really did fail, and its siblings really
    # were called off. A canary that passed tested nothing.
    canary_failed = canary_state in ("failed", "timed_out")
    siblings_cancelled = bool(cancelled)
    state = STATE_RAN if (canary_failed and siblings_cancelled and not residual) else STATE_FAILED
    if not canary_failed:
        detail = "the synthetic canary did not fail; sibling cancellation was not exercised"
    elif not siblings_cancelled:
        detail = "the canary failed but no sibling was cancelled"
    elif residual:
        detail = f"{len(residual)} path(s) survived cleanup after the canary failed"
    else:
        detail = ""
    return ProbeResult(
        kind=PROBE_SYNTHETIC_CANARY,
        state=state,
        attempts=1,
        observations={
            "canary_unit_id": canary.id,
            "canary_state": canary_state,
            "canary_failed": canary_failed,
            "cancelled_units": cancelled,
            "residual_paths": residual,
            "consumer_paths_touched": [],
        },
        detail=detail,
    )


def _probe_cleanup_leak(context: _Context) -> ProbeResult:
    """Remove the scratch tree and verify it is actually gone.

    Runs last and needs no execution, so it is never budget-refused: cleanup that
    only happens when there is time left is not cleanup.
    """

    scratch = context.workspace.scratch
    removed = scratch.is_dir()
    if removed:
        shutil.rmtree(scratch, ignore_errors=True)
    leaked = sorted(
        str(path.relative_to(scratch)) for path in scratch.rglob("*")
    ) if scratch.exists() else []
    return ProbeResult(
        kind=PROBE_CLEANUP_LEAK,
        state=STATE_RAN if not leaked else STATE_FAILED,
        attempts=1,
        observations={
            "scratch_existed": removed,
            "scratch_removed": not scratch.exists(),
            "leaked_paths": leaked,
        },
        detail="" if not leaked else f"{len(leaked)} path(s) leaked out of the capsule scratch",
    )


@dataclass(frozen=True)
class _Context:
    runner: ProbeRunner
    units: tuple[ProbeUnit, ...]
    workspace: AdmittedWorkspace
    authority: ProbeAuthority
    budget: _Budget


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_probes(
    units: Iterable[ProbeUnit],
    *,
    consumer_root: Path,
    authority: ProbeAuthority,
    runner: ProbeRunner | None = None,
    verify_archive: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Admit, probe, and return a deterministic ``probe-receipt/v1``.

    Every refusal path raises :class:`ProbeRefusal` *before* anything executes.
    Once admitted, individual probes may still refuse (budget exhaustion) without
    failing the run -- a refused probe is recorded as refused and upgrades
    nothing, which is the difference between "we did not learn this" and "this is
    broken".
    """

    authority = authority.validated()
    workspace = admit_workspace(
        authority.workspace,
        consumer_root=consumer_root,
        archive_sha256=authority.archive_sha256,
        verify_archive=verify_archive,
    )
    if runner is None:
        _refuse(
            "probe_runner_missing",
            "probe mode has no execution runner in this build; the static score "
            "is unaffected and no probe was attempted",
            next_actions=["sbp test score --format json  # static analysis"],
        )
    unit_list = tuple(units)
    if not unit_list:
        _refuse(
            "probe_units_missing",
            "probe mode needs at least one unit to execute",
            next_actions=["declare units in .skillbox/test.yaml, then retry"],
        )
    for unit in unit_list:
        if unit.id.startswith(CANARY_UNIT_PREFIX) or unit.synthetic:
            _refuse(
                "probe_canary_unsafe",
                f"{CANARY_UNIT_PREFIX!r} is reserved for harness-owned canaries; a "
                "consumer unit may not use it",
            )

    # The service gate is enforced by refusing to launch, not by hoping a unit
    # behaves. Checked once, up front, so a suite that needs a database under
    # --probe-deny-services never starts a single unit.
    if not authority.allow_services:
        needy = sorted(unit.id for unit in unit_list if unit.services)
        if needy:
            _refuse(
                "probe_services_denied_but_required",
                "services are denied but these units declare one, so nothing was "
                f"launched: {', '.join(needy)}",
                next_actions=[
                    "pass --probe-allow-services, or probe a service-free group",
                ],
            )

    budget = _Budget(float(authority.wall_clock_budget_s or 0.0), clock)
    context = _Context(
        runner=runner,
        units=unit_list,
        workspace=workspace,
        authority=authority,
        budget=budget,
    )

    results = [
        _probe_serial_repeat(context),
        _probe_concurrency(context, PROBE_CONCURRENCY_TWO, 2),
        _probe_concurrency(context, PROBE_CONCURRENCY_N, int(authority.max_parallel or 2)),
        _probe_randomized_order(context),
        _probe_synthetic_canary(context),
        # Cleanup always runs, including after a budget exhaustion, because a
        # capsule left dirty is a leak whether or not we ran out of time.
        _probe_cleanup_leak(context),
    ]
    return build_receipt(results, authority=authority, workspace=workspace, budget=budget)


def build_receipt(
    results: Sequence[ProbeResult],
    *,
    authority: ProbeAuthority,
    workspace: AdmittedWorkspace,
    budget: _Budget | None = None,
) -> dict[str, Any]:
    """Assemble the deterministic receipt. No clocks, no paths, total ordering."""

    ordered = sorted(results, key=lambda item: PROBE_KINDS.index(item.kind))
    counts = {state: sum(1 for item in ordered if item.state == state) for state in PROBE_STATES}
    return {
        "schema": PROBE_SCHEMA,
        "schema_version": PROBE_SCHEMA_VERSION,
        "authority": {
            **authority.to_payload(),
            "capsule": workspace.to_payload(),
            # How honestly each denial is enforced. A reader must be able to tell
            # a gate that refuses to launch from a contract the units are merely
            # asked to honour.
            "enforcement": dict(PERMISSION_ENFORCEMENT),
        },
        "probes": [item.to_payload() for item in ordered],
        "counts": counts,
        "budget_exhausted": bool(budget.exhausted) if budget is not None else False,
        "proof_requirements": [
            PROOF_REQUIREMENTS[code].to_payload() for code in sorted(PROOF_REQUIREMENTS)
        ],
        "never_provable_by_probe": sorted(NEVER_PROVABLE_BY_PROBE),
        "consumer_mutation": {
            "attempted": False,
            "guard": (
                "synthetic units are harness-owned, carry a reserved id prefix, and "
                "are confined to the capsule workspace; consumer source and "
                "assertions are never modified"
            ),
        },
    }


def receipt_json(receipt: Mapping[str, Any]) -> str:
    """Byte-stable rendering. Same probes, same bytes, on every host."""

    return json.dumps(_canonical(receipt), indent=2, sort_keys=True) + "\n"


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """The receipt's identity. Two identical probe runs digest identically."""

    return hashlib.sha256(receipt_json(receipt).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# likely -> proven
# --------------------------------------------------------------------------- #


def established_tokens(receipt: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """Probe kind -> the tokens it established, for probes that actually ran."""

    tokens: dict[str, set[str]] = {}
    for probe in receipt.get("probes") or []:
        if str(probe.get("state")) not in (STATE_RAN, STATE_FAILED):
            # A refused probe established nothing, by definition.
            continue
        kind = str(probe.get("kind"))
        tokens.setdefault(kind, set()).update(
            str(token) for token in probe.get("established") or []
        )
    return {kind: frozenset(values) for kind, values in tokens.items()}


def upgrade_report(
    report: Mapping[str, Any], receipt: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Upgrade ``likely`` findings to ``proven`` where the exact requirement held.

    Only ``likely`` is eligible. An ``unknown`` never becomes ``proven`` here --
    a probe that observed something the static read could not even locate has not
    confirmed a finding, it has produced a new one, and inventing findings from a
    probe is out of scope for this slice.

    The report is rebuilt through :func:`sbp_test_findings.build_report` rather
    than patched in place, so every registry invariant (gates, rollup, axis
    states, readiness class) is recomputed from the upgraded findings instead of
    being left describing the old ones.
    """

    tokens = established_tokens(receipt)
    upgrades: list[dict[str, Any]] = []
    findings: list[R.Finding] = []
    cleared: list[R.Cleared] = []

    for payload in report.get("cleared") or []:
        cleared.append(
            R.Cleared(
                payload["finding_code"],
                tuple(_evidence_from(item) for item in payload.get("evidence") or []),
                note=payload.get("note"),
            )
        )

    for payload in report.get("findings") or []:
        code = payload["finding_code"]
        status = payload["status"]
        evidence = [_evidence_from(item) for item in payload.get("evidence") or []]
        requirement = PROOF_REQUIREMENTS.get(code)
        upgraded = (
            status == "likely"
            and requirement is not None
            and requirement.token in tokens.get(requirement.probe_kind, frozenset())
        )
        if upgraded:
            assert requirement is not None  # narrowed by `upgraded`
            evidence.append(
                R.Evidence(
                    "probe",
                    f"{PROBE_SCHEMA}#{requirement.probe_kind}",
                    requirement.statement,
                )
            )
            upgrades.append(
                {
                    "finding_code": code,
                    "from": "likely",
                    "to": "proven",
                    "probe_kind": requirement.probe_kind,
                    "token": requirement.token,
                    "requirement": requirement.statement,
                }
            )
        findings.append(
            R.Finding(
                code,
                "proven" if upgraded else status,
                evidence=tuple(evidence),
                affected_units=tuple(payload.get("affected_units") or []),
                reason=None if upgraded else payload.get("reason"),
                severity=payload.get("severity"),
                proposed_fragment=payload.get("proposed_fragment"),
            )
        )

    subject = report.get("subject") or {}
    rebuilt = R.build_report(
        R.Subject(
            label=str(subject.get("label") or ""),
            capsule_digest=subject.get("capsule_digest"),
        ),
        findings,
        cleared,
    )
    # Provenance and scorer-authored next actions belong to the scorer; carry
    # them across untouched rather than silently dropping them.
    for key in ("provenance", "next_actions"):
        if key in report:
            rebuilt[key] = report[key]
    return rebuilt, sorted(upgrades, key=lambda item: item["finding_code"])


def _evidence_from(payload: Mapping[str, Any]) -> R.Evidence:
    return R.Evidence(
        str(payload.get("kind")),
        str(payload.get("locator")),
        payload.get("detail"),
    )


__all__ = [
    "CANARY_UNIT_PREFIX",
    "MAX_PROBE_PARALLELISM",
    "MAX_REPEATS",
    "MAX_WALL_CLOCK_BUDGET_S",
    "NEEDS_INPUT_CODES",
    "NEVER_PROVABLE_BY_PROBE",
    "PERMISSION_ENFORCEMENT",
    "PROBE_CLEANUP_LEAK",
    "PROBE_CONCURRENCY_N",
    "PROBE_CONCURRENCY_TWO",
    "PROBE_KINDS",
    "PROBE_RANDOMIZED_ORDER",
    "PROBE_SCHEMA",
    "PROBE_SCHEMA_VERSION",
    "PROBE_SERIAL_REPEAT",
    "PROBE_STATES",
    "PROBE_SYNTHETIC_CANARY",
    "PROOF_REQUIREMENTS",
    "REFUSAL_CODES",
    "STATE_FAILED",
    "STATE_RAN",
    "STATE_REFUSED",
    "WORKSPACE_MARKER",
    "WORKSPACE_SCHEMA",
    "AdmittedWorkspace",
    "ProbeAuthority",
    "ProbeObservation",
    "ProbeRefusal",
    "ProbeResult",
    "ProbeRun",
    "ProbeUnit",
    "ProofRequirement",
    "admit_workspace",
    "assert_canary_safe",
    "build_canary",
    "build_receipt",
    "established_tokens",
    "local_executor_runner",
    "receipt_digest",
    "receipt_json",
    "run_probes",
    "upgrade_report",
    "write_workspace_marker",
]
