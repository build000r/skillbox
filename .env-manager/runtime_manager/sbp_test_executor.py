"""Wave-concurrent local unit executor for a sealed ``test-plan/v1``.

:mod:`runtime_manager.sbp_test_plan` computes *what* may run and in which wave.
This module runs it. The difference is material rather than cosmetic:
``runtime_ops.run_tasks`` is sequential and raises on the first failure, which
is the right shape for a bootstrap chain and the wrong one for a test run —
there, one broken unit must not hide the verdict of every independent unit
behind it.

So the process semantics are copied from ``run_tasks`` and the control flow is
not:

* copied — cwd/env translation, ``start_new_session=True`` so the unit and all
  its descendants share a process group, ``os.killpg`` on timeout (killing only
  the immediate child leaves the real work orphaned), one log file per unit;
* new — waves run concurrently under a slot cap, results are collected instead
  of raised, and a failure skips only what actually depends on it.

Three v1 rules shape the scheduler:

**A global slot cap.** Defaults to the core count. A test run that fans out to
every core plus one is slower than one that does not, and on a shared box it is
antisocial.

**Resource groups and exclusivity.** Two units declaring the same
``resource_group`` never co-run — one database, one owner. An ``exclusive`` unit
runs alone in its batch. A unit may declare it consumes several slots (an xdist
unit is really N workers), and a unit that can never fit under the cap is a
scheduling refusal rather than a job that waits forever.

**Placement is consulted per unit, from day one.** ``placement.decide`` runs for
every unit and the explanatory ``machine-placement/v1`` block is persisted
*before* the process starts, so a run can always answer "why did this unit run
here". Candidates are filtered to the current machine until the remote leg
lands — that keeps the decision honest instead of routing a unit somewhere this
executor cannot reach.

Skips are explained, never silent: when a unit fails, everything downstream is
marked skipped with the unit that blocked it, using the same
``agent_graph_algorithms.blast_radius`` the plan compiler uses for cycles.

Not here: run receipts belong to a sibling bead. This module returns results;
it does not define or write a receipt schema.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import agent_graph_algorithms as GA
from . import placement as placement_module

EXECUTOR_PROTOCOL_VERSION = "1"

DEPENDS_ON = "depends_on"

STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_TIMED_OUT = "timed_out"
STATE_SKIPPED = "skipped"
STATE_CANCELLED = "cancelled"
STATE_NOT_RUN = "not_run"
UNIT_STATES = (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_TIMED_OUT,
    STATE_SKIPPED,
    STATE_CANCELLED,
    STATE_NOT_RUN,
)

#: States that stop dependents from running.
BLOCKING_STATES = frozenset({STATE_FAILED, STATE_TIMED_OUT, STATE_CANCELLED})

EXCLUSIVITY_SHARED = "shared"
EXCLUSIVITY_EXCLUSIVE = "exclusive"

#: A unit declares extra slot consumption through a cap token, so no manifest
#: change is needed: `slots:4` (or the `xdist:4` alias) means "this unit is
#: really four workers, bill it four slots".
SLOT_CAP_PREFIXES = ("slots:", "xdist:")
MAX_UNIT_SLOTS = 64
MAX_PARALLEL_CEILING = 256

#: How long to wait for a process group to die after SIGKILL before giving up.
KILL_GRACE_S = 5.0

REFUSAL_CODES = frozenset(
    {
        "plan_invalid",
        "slot_starvation",
        "slots_invalid",
        "executor_misconfigured",
    }
)


class ExecutorRefusal(Exception):
    """A typed, fail-closed refusal to schedule or execute."""

    def __init__(self, code: str, message: str, *, units: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.units = sorted(units)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
        }
        if self.units:
            payload["units"] = list(self.units)
        return payload


def _refuse(code: str, message: str, *, units: Iterable[str] = ()) -> Any:
    raise ExecutorRefusal(code, message, units=units)


def default_max_parallel() -> int:
    """The core count, floored at 1. A cap, not a target."""

    return max(1, int(os.cpu_count() or 1))


# --------------------------------------------------------------------------- #
# Unit view over a sealed plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnitSpec:
    """One plan unit, reduced to what the scheduler and runner need."""

    id: str
    argv: tuple[str, ...]
    cwd: str | None
    timeout_s: int | None
    wave: int | None
    runnable: bool
    slots: int
    resource_group: str | None
    exclusivity: str
    needs: Mapping[str, Any] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()

    @property
    def exclusive(self) -> bool:
        return self.exclusivity == EXCLUSIVITY_EXCLUSIVE


def slots_for(needs: Mapping[str, Any]) -> int:
    """How many slots a unit consumes. Declared via a `slots:N` cap token."""

    caps = needs.get("caps") or []
    if not isinstance(caps, (list, tuple)):
        _refuse("slots_invalid", "unit caps must be a list")
    declared = 1
    for cap in caps:
        if not isinstance(cap, str):
            continue
        for prefix in SLOT_CAP_PREFIXES:
            if cap.startswith(prefix):
                raw = cap[len(prefix) :]
                if not raw.isdigit():
                    _refuse("slots_invalid", f"cap {cap!r} does not declare an integer")
                value = int(raw)
                if not 1 <= value <= MAX_UNIT_SLOTS:
                    _refuse(
                        "slots_invalid",
                        f"cap {cap!r} is outside 1..{MAX_UNIT_SLOTS}",
                    )
                declared = max(declared, value)
    return declared


def plan_units(plan_content: Mapping[str, Any]) -> tuple[UnitSpec, ...]:
    """Project a sealed plan's units into scheduler specs. Never reads the repo."""

    if not isinstance(plan_content, Mapping):
        _refuse("plan_invalid", "plan content must be a mapping")
    raw_units = plan_content.get("units")
    if not isinstance(raw_units, list):
        _refuse("plan_invalid", "plan carries no units list")
    specs: list[UnitSpec] = []
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            _refuse("plan_invalid", "plan unit must be a mapping")
        needs = raw.get("needs") or {}
        if not isinstance(needs, Mapping):
            _refuse("plan_invalid", f"unit {raw.get('id')!r} needs must be a mapping")
        argv = raw.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            _refuse("plan_invalid", f"unit {raw.get('id')!r} argv must be a string list")
        specs.append(
            UnitSpec(
                id=str(raw.get("id") or ""),
                argv=tuple(argv),
                cwd=raw.get("cwd"),
                timeout_s=raw.get("timeout_s"),
                wave=raw.get("wave"),
                runnable=bool(raw.get("runnable")),
                slots=slots_for(needs),
                resource_group=needs.get("resource_group"),
                exclusivity=str(needs.get("exclusivity") or EXCLUSIVITY_SHARED),
                needs=dict(needs),
                env_allowlist=tuple(raw.get("env_allowlist") or []),
                blocked_by=tuple(raw.get("blocked_by") or []),
            )
        )
    if not specs:
        _refuse("plan_invalid", "plan has no units to execute")
    return tuple(specs)


# --------------------------------------------------------------------------- #
# Deterministic scheduling (pure — this is the golden surface)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Batch:
    """A set of units that may run at the same instant."""

    wave: int
    index: int
    unit_ids: tuple[str, ...]
    slots_used: int


def schedule_batches(
    units: Iterable[UnitSpec],
    waves: Sequence[Sequence[str]],
    *,
    max_parallel: int | None = None,
) -> tuple[Batch, ...]:
    """Split each wave into batches that respect every concurrency rule.

    Pure and deterministic: same plan plus same cap yields the same batches,
    which is what makes wave assignment golden-able without running anything.
    Units are considered in id order and packed greedily, so the result never
    depends on dict ordering or on which process finished first.
    """

    cap = default_max_parallel() if max_parallel is None else int(max_parallel)
    if cap < 1 or cap > MAX_PARALLEL_CEILING:
        _refuse(
            "executor_misconfigured",
            f"max_parallel must be within 1..{MAX_PARALLEL_CEILING}",
        )
    by_id = {unit.id: unit for unit in units}

    starved = sorted(
        uid
        for uid in by_id
        if by_id[uid].runnable and by_id[uid].slots > cap
    )
    if starved:
        # A unit that cannot fit under the cap would wait forever. Refusing at
        # schedule time turns a hang into an answer.
        _refuse(
            "slot_starvation",
            f"unit(s) need more slots than the cap of {cap}",
            units=starved,
        )

    batches: list[Batch] = []
    for wave_index, wave in enumerate(waves):
        pending = sorted(uid for uid in wave if uid in by_id and by_id[uid].runnable)
        batch_index = 0
        while pending:
            chosen: list[str] = []
            used = 0
            held_groups: set[str] = set()
            remaining: list[str] = []
            for uid in pending:
                unit = by_id[uid]
                if unit.exclusive:
                    if chosen:
                        remaining.append(uid)
                        continue
                    chosen.append(uid)
                    used = unit.slots
                    # An exclusive unit closes the batch: nothing else joins.
                    remaining.extend(u for u in pending if u != uid and u not in chosen)
                    break
                if chosen and by_id[chosen[0]].exclusive:
                    remaining.append(uid)
                    continue
                group = unit.resource_group
                if group is not None and group in held_groups:
                    remaining.append(uid)
                    continue
                if used + unit.slots > cap:
                    remaining.append(uid)
                    continue
                chosen.append(uid)
                used += unit.slots
                if group is not None:
                    held_groups.add(group)
            if not chosen:  # pragma: no cover - starvation is refused above
                _refuse(
                    "slot_starvation",
                    "no unit could be scheduled under the current cap",
                    units=pending,
                )
            batches.append(
                Batch(
                    wave=wave_index,
                    index=batch_index,
                    unit_ids=tuple(chosen),
                    slots_used=used,
                )
            )
            batch_index += 1
            pending = sorted(dict.fromkeys(remaining))
    return tuple(batches)


def schedule_payload(batches: Iterable[Batch]) -> dict[str, Any]:
    """JSON view of a schedule — the shape a wave-assignment golden pins."""

    rows = [
        {
            "wave": batch.wave,
            "index": batch.index,
            "units": list(batch.unit_ids),
            "slots_used": batch.slots_used,
        }
        for batch in batches
    ]
    return {"protocol_version": EXECUTOR_PROTOCOL_VERSION, "batches": rows}


# --------------------------------------------------------------------------- #
# Placement — consulted per unit, persisted before launch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlacementContext:
    """Everything ``placement.decide`` needs, plus this machine's identity."""

    config: Any
    current_id: str | None
    boxes: tuple[Any, ...] = ()
    observations: Mapping[str, Any] | None = None
    profiles: Any = None


def local_only_config(context: PlacementContext) -> Any:
    """Restrict candidates to this machine until the remote leg lands.

    Filtering *before* the decision keeps it honest: the alternative is to let
    ``decide`` select a machine this executor cannot reach and then refuse the
    result, which reports a placement that never happens.
    """

    config = context.config
    machines = getattr(config, "machines", None)
    if not isinstance(machines, Mapping) or context.current_id is None:
        return config
    if context.current_id not in machines:
        return config
    local = {context.current_id: machines[context.current_id]}
    try:
        return type(config)(machines=local, source_path=getattr(config, "source_path", ""))
    except Exception:  # noqa: BLE001 - a config we cannot narrow is used as-is
        return config


def decide_placement(unit: UnitSpec, context: PlacementContext | None) -> dict[str, Any]:
    """The ``machine-placement/v1`` block for one unit."""

    if context is None:
        # No inventory to consult: say so explicitly rather than implying a
        # decision was made.
        return {
            "kind": placement_module.KIND,
            "decision": "no_match",
            "machine_id": None,
            "reasons": ["no machines configuration was supplied to the executor"],
            "local_only": True,
        }
    decision = placement_module.decide(
        dict(unit.needs),
        local_only_config(context),
        context.boxes,
        context.observations,
        context.profiles,
        context.current_id,
    )
    payload = dict(decision)
    payload["local_only"] = True
    return payload


def persist_placement(log_root: Path, unit_id: str, decision: Mapping[str, Any]) -> Path:
    """Write the placement block BEFORE the unit launches."""

    directory = Path(log_root) / "placement"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / f"{unit_id}.json"
    target.write_text(
        json.dumps(decision, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(target, 0o600)
    return target


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass
class UnitResult:
    """What happened to one unit. Every non-run state names its cause."""

    unit_id: str
    state: str
    exit_code: int | None = None
    duration_s: float = 0.0
    log_file: str = ""
    placement_file: str = ""
    blocked_by: tuple[str, ...] = ()
    cause: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 6),
            "log_file": self.log_file,
            "placement_file": self.placement_file,
            "blocked_by": list(self.blocked_by),
            "cause": self.cause,
        }


@dataclass(frozen=True)
class RunOutcome:
    """Collected results. Independent units are reported even after a failure."""

    results: tuple[UnitResult, ...]
    batches: tuple[Batch, ...]
    cancelled: bool

    @property
    def ok(self) -> bool:
        return all(result.state == STATE_COMPLETED for result in self.results)

    def by_state(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {state: [] for state in UNIT_STATES}
        for result in self.results:
            grouped[result.state].append(result.unit_id)
        return {state: sorted(ids) for state, ids in grouped.items() if ids}

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": EXECUTOR_PROTOCOL_VERSION,
            "ok": self.ok,
            "cancelled": self.cancelled,
            "schedule": schedule_payload(self.batches),
            "results": [result.to_payload() for result in sorted(
                self.results, key=lambda item: item.unit_id
            )],
            "by_state": self.by_state(),
        }


def _graph_from_plan(plan_content: Mapping[str, Any]) -> dict[str, Any]:
    """The plan's own graph, left un-normalized: blast_radius normalizes itself."""

    nodes = [{"id": unit["id"]} for unit in plan_content.get("units") or []]
    edges = [dict(edge) for edge in plan_content.get("edges") or []]
    return {"nodes": nodes, "edges": edges}


def _downstream_of(graph: Mapping[str, Any], unit_id: str) -> tuple[str, ...]:
    radius = GA.blast_radius(graph, unit_id, edge_kinds=(DEPENDS_ON,))
    return tuple(
        sorted(
            str(item.get("node_id"))
            for item in (radius.get("affected") or [])
            if str(item.get("node_id")) != unit_id
        )
    )


def _unit_env(unit: UnitSpec, base_env: Mapping[str, str] | None) -> dict[str, str]:
    """Translate the environment for one unit.

    A unit declares an allowlist; anything outside it is dropped rather than
    inherited, so a run does not silently depend on whatever the operator's
    shell happened to export.
    """

    source = dict(os.environ if base_env is None else base_env)
    if not unit.env_allowlist:
        return source
    return {name: source[name] for name in unit.env_allowlist if name in source}


def _kill_group(process: subprocess.Popen[Any]) -> None:
    """SIGKILL the whole process group. Killing the child orphans its work."""

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - kernel refused SIGKILL
        pass


def _run_unit(
    unit: UnitSpec,
    *,
    repo: Path,
    log_root: Path,
    base_env: Mapping[str, str] | None,
    placement_file: str,
    cancel: Callable[[], bool] | None,
    registry: dict[str, subprocess.Popen[Any]],
    lock: threading.Lock,
) -> UnitResult:
    log_file = Path(log_root) / f"{unit.id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    result = UnitResult(
        unit_id=unit.id,
        state=STATE_FAILED,
        log_file=str(log_file),
        placement_file=placement_file,
    )
    if cancel is not None and cancel():
        result.state = STATE_CANCELLED
        result.cause = "cancelled before launch"
        return result

    cwd = Path(repo) / unit.cwd if unit.cwd else Path(repo)
    started = time.monotonic()
    with log_file.open("a", encoding="utf-8") as handle:
        try:
            process = subprocess.Popen(
                list(unit.argv),
                cwd=str(cwd),
                env=_unit_env(unit, base_env),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                # New session so the unit and every descendant share a process
                # group we can kill as a whole.
                start_new_session=True,
            )
        except OSError as error:
            result.duration_s = time.monotonic() - started
            result.cause = f"could not start unit: {type(error).__name__}"
            return result
        with lock:
            registry[unit.id] = process
        try:
            timeout = float(unit.timeout_s) if unit.timeout_s else None
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                result.state = STATE_TIMED_OUT
                result.duration_s = time.monotonic() - started
                result.cause = f"exceeded its {unit.timeout_s}s ceiling"
                return result
        finally:
            with lock:
                registry.pop(unit.id, None)

    result.duration_s = time.monotonic() - started
    result.exit_code = exit_code
    if cancel is not None and cancel() and exit_code != 0:
        result.state = STATE_CANCELLED
        result.cause = "cancelled during execution"
        return result
    result.state = STATE_COMPLETED if exit_code == 0 else STATE_FAILED
    if exit_code != 0:
        result.cause = f"exited {exit_code}"
    return result


def execute_plan(
    plan_content: Mapping[str, Any],
    *,
    repo: Path,
    log_root: Path,
    max_parallel: int | None = None,
    base_env: Mapping[str, str] | None = None,
    placement_context: PlacementContext | None = None,
    cancel: Callable[[], bool] | None = None,
) -> RunOutcome:
    """Run a sealed plan wave-concurrently, collecting every unit's verdict.

    A failure does not stop the run: only units that actually depend on the
    failed one are skipped, and the reason names the unit that blocked them.
    Cancellation kills running process groups and marks the rest cancelled
    rather than leaving them unaccounted for.
    """

    units = plan_units(plan_content)
    by_id = {unit.id: unit for unit in units}
    waves = plan_content.get("waves") or []
    if not isinstance(waves, list):
        _refuse("plan_invalid", "plan waves must be a list")
    batches = schedule_batches(units, waves, max_parallel=max_parallel)
    graph = _graph_from_plan(plan_content)

    root = Path(log_root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)

    results: dict[str, UnitResult] = {}
    # Units the plan already declared unrunnable keep the plan's own reason.
    for unit in units:
        if not unit.runnable:
            results[unit.id] = UnitResult(
                unit_id=unit.id,
                state=STATE_SKIPPED,
                blocked_by=unit.blocked_by,
                cause="not runnable in the sealed plan",
            )

    blocked: dict[str, str] = {}
    registry: dict[str, subprocess.Popen[Any]] = {}
    lock = threading.Lock()
    cancelled = False

    for batch in batches:
        if cancel is not None and cancel():
            cancelled = True
        runnable_now: list[UnitSpec] = []
        for uid in batch.unit_ids:
            if uid in results:
                continue
            if uid in blocked:
                results[uid] = UnitResult(
                    unit_id=uid,
                    state=STATE_SKIPPED,
                    blocked_by=(blocked[uid],),
                    cause=f"blocked by {blocked[uid]}",
                )
                continue
            if cancelled:
                results[uid] = UnitResult(
                    unit_id=uid, state=STATE_CANCELLED, cause="run was cancelled"
                )
                continue
            runnable_now.append(by_id[uid])

        if not runnable_now:
            continue

        # Placement is decided and PERSISTED before anything launches, so a run
        # can always answer "why here" even if it dies mid-batch.
        placement_files: dict[str, str] = {}
        for unit in runnable_now:
            decision = decide_placement(unit, placement_context)
            placement_files[unit.id] = str(persist_placement(root, unit.id, decision))

        threads: list[threading.Thread] = []
        batch_results: dict[str, UnitResult] = {}

        def _worker(unit: UnitSpec) -> None:
            batch_results[unit.id] = _run_unit(
                unit,
                repo=Path(repo),
                log_root=root,
                base_env=base_env,
                placement_file=placement_files[unit.id],
                cancel=cancel,
                registry=registry,
                lock=lock,
            )

        for unit in runnable_now:
            thread = threading.Thread(target=_worker, args=(unit,), daemon=True)
            thread.start()
            threads.append(thread)

        if cancel is not None:
            while any(thread.is_alive() for thread in threads):
                if cancel():
                    cancelled = True
                    with lock:
                        running = list(registry.values())
                    for process in running:
                        _kill_group(process)
                    break
                time.sleep(0.02)
        for thread in threads:
            thread.join()

        for unit_id, result in batch_results.items():
            results[unit_id] = result
            if result.state in BLOCKING_STATES:
                for downstream in _downstream_of(graph, unit_id):
                    blocked.setdefault(downstream, unit_id)

    for unit in units:
        results.setdefault(
            unit.id,
            UnitResult(
                unit_id=unit.id,
                state=STATE_CANCELLED if cancelled else STATE_NOT_RUN,
                blocked_by=(blocked[unit.id],) if unit.id in blocked else (),
                cause="run was cancelled" if cancelled else "never scheduled",
            ),
        )

    return RunOutcome(
        results=tuple(results[unit.id] for unit in units),
        batches=batches,
        cancelled=cancelled,
    )


__all__ = [
    "BLOCKING_STATES",
    "EXCLUSIVITY_EXCLUSIVE",
    "EXCLUSIVITY_SHARED",
    "EXECUTOR_PROTOCOL_VERSION",
    "MAX_PARALLEL_CEILING",
    "MAX_UNIT_SLOTS",
    "REFUSAL_CODES",
    "SLOT_CAP_PREFIXES",
    "STATE_CANCELLED",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_NOT_RUN",
    "STATE_SKIPPED",
    "STATE_TIMED_OUT",
    "UNIT_STATES",
    "Batch",
    "ExecutorRefusal",
    "PlacementContext",
    "RunOutcome",
    "UnitResult",
    "UnitSpec",
    "decide_placement",
    "default_max_parallel",
    "execute_plan",
    "local_only_config",
    "persist_placement",
    "plan_units",
    "schedule_batches",
    "schedule_payload",
    "slots_for",
]
