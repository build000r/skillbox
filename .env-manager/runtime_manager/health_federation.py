"""Bounded-concurrency collection behind ``doctor --all``.

:mod:`runtime_manager.health_protocol` defines the typed vocabulary and the
deterministic prioritizer, and is deliberately inert — no provider imports, no
collection, no I/O. This module is the other half: the read-only providers that
project Skillbox's three authoritative health surfaces into that vocabulary, and
the executor that runs them concurrently under a wall-clock cap.

The three providers, each aggregating rather than reinterpreting:

* ``structure-doctor`` — ``structure_doctor.run_structure_doctor()`` gates
* ``runtime-evidence`` — ``evidence.collect_runtime_evidence()`` sections
* ``outer-reconcile`` — ``scripts/04-reconcile.py``'s ``doctor_results()``

Each provider's native verdict is carried through, never recomputed. A gate that
says FAIL becomes ``fail``; a section that says warn becomes ``warn``. The
federation adds identity, provenance, and ordering — it does not add opinions,
because a second opinion about a provider's own domain is how a federation
starts lying about it.

**Failure is a result, not an exception.** A provider that raises produces a
``unavailable`` check with the reason; a provider that overruns its cap produces
``timed_out`` with the cap that was applied. Both carry provenance like any
other result, so "we could not tell" is reported as loudly as "this is broken"
instead of vanishing from the output.

**Latency tracks the slowest provider, not their sum.** Providers run on a
bounded thread pool, so a run costs roughly the slowest provider plus overhead.
Ordering never depends on which finished first: results are sorted by the
protocol's deterministic priority key, so the same set of findings renders
identically every time.

**Read-only, and no fix lane.** Every descriptor declares ``read_only=True``
(the protocol refuses any other value), nothing here executes a
``fix_command``, and ``--all`` refuses to combine with ``--fix``. Remediation is
a separately justified surface; a diagnosis front door that can also act is how
one becomes the other by accident.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .health_protocol import (
    ACTION_INSPECT,
    SCOPE_REPO,
    SCOPE_RUNTIME,
    SCOPE_STRUCTURE,
    SEVERITY_ADVISORY,
    SEVERITY_CRITICAL,
    SEVERITY_NONE,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    STATUS_WARN,
    CheckScope,
    HealthCheckResult,
    NextAction,
    NO_ACTION,
    Provenance,
    ProviderDescriptor,
    federation_payload,
)

FEDERATION_KIND = "health-federation"
FEDERATION_SCHEMA = "skillbox.health-federation.v1"

PROVIDER_STRUCTURE = "structure-doctor"
PROVIDER_RUNTIME = "runtime-evidence"
PROVIDER_OUTER = "outer-reconcile"

#: The structure suite budgets 60s of gate caps, so its provider cap sits above
#: that; a provider that overruns is reported, never silently dropped.
DEFAULT_PROVIDER_TIMEOUT_S = 90.0

#: Bounded on purpose. Three providers today, and an unbounded pool on a loaded
#: box turns a diagnosis into a load source.
DEFAULT_MAX_WORKERS = 4

_STRUCTURE_STATUS = {
    "pass": STATUS_PASS,
    "fail": STATUS_FAIL,
    "warn": STATUS_WARN,
}
_SECTION_STATUS = {
    "pass": STATUS_PASS,
    "ok": STATUS_PASS,
    "warn": STATUS_WARN,
    "fail": STATUS_FAIL,
    "unavailable": STATUS_UNAVAILABLE,
    "inco": STATUS_UNAVAILABLE,
}
_SEVERITY_BY_STATUS = {
    STATUS_PASS: SEVERITY_NONE,
    STATUS_WARN: SEVERITY_WARNING,
    STATUS_FAIL: SEVERITY_CRITICAL,
    STATUS_UNAVAILABLE: SEVERITY_UNKNOWN,
    STATUS_TIMED_OUT: SEVERITY_UNKNOWN,
}


def _severity(status: str, *, advisory: bool = False) -> str:
    if status == STATUS_PASS and advisory:
        return SEVERITY_ADVISORY
    return _SEVERITY_BY_STATUS.get(status, SEVERITY_UNKNOWN)


def _inspect_action(action_id: str, summary: str, fix_command: str = "") -> NextAction:
    """A display-only next action. ``fix_command`` is text, never a handle."""

    return NextAction(
        action_id=action_id,
        kind=ACTION_INSPECT,
        summary=summary,
        fix_command=fix_command,
    )


def _cause_text(error: BaseException) -> str:
    """Reason without a traceback: type plus the exception's own short text."""

    detail = str(error).strip().splitlines()[0] if str(error).strip() else ""
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class StructureDoctorProvider:
    """Projects ``structure_doctor`` gates. Aggregates; never re-judges a gate."""

    provider_id = PROVIDER_STRUCTURE

    def __init__(self, root_dir: Path, cwd: Path | None = None) -> None:
        self._root_dir = Path(root_dir)
        self._cwd = Path(cwd) if cwd is not None else None

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            title="Structural gates (sbp doctor)",
            scope_kinds=(SCOPE_STRUCTURE, SCOPE_RUNTIME),
            default_max_age_s=None,
        )

    def collect(self) -> Sequence[HealthCheckResult]:
        from . import structure_doctor as sd

        payload = sd.run_structure_doctor(cwd=str(self._cwd) if self._cwd else None)
        observed_at = time.time()
        provenance = Provenance(
            provider_id=self.provider_id,
            source="runtime_manager.structure_doctor.run_structure_doctor",
            collector="python3 .env-manager/manage.py structure-doctor --format json",
        )
        results: list[HealthCheckResult] = []
        for gate in payload.get("gates") or []:
            results.append(self._gate_result(gate, observed_at, provenance))
        return tuple(results)

    def _gate_result(
        self,
        gate: Mapping[str, Any],
        observed_at: float,
        provenance: Provenance,
    ) -> HealthCheckResult:
        name = str(gate.get("name") or "unnamed")
        native = str(gate.get("status") or "").lower()
        detail = str(gate.get("detail") or "")
        fix_command = str(gate.get("fix_command") or "")
        duration = gate.get("duration_s")

        timeout_s: float | None = None
        cause = ""
        if native in _STRUCTURE_STATUS:
            status = _STRUCTURE_STATUS[native]
        else:
            # INCO. The protocol distinguishes "ran out of time" from "could not
            # reach a dependency", and the gate's own detail is the only thing
            # that knows which — so read it rather than guessing one.
            if "cap" in detail.lower() or "timeout" in detail.lower():
                status = STATUS_TIMED_OUT
                timeout_s = _as_float(gate.get("cap_s"))
                cause = detail or "gate exceeded its cap"
            else:
                status = STATUS_UNAVAILABLE
                cause = detail or "gate could not produce a verdict"

        action = NO_ACTION
        if status in (STATUS_FAIL, STATUS_WARN, STATUS_UNAVAILABLE, STATUS_TIMED_OUT):
            action = _inspect_action(
                action_id=f"{self.provider_id}:{name}",
                summary=detail or f"structure gate {name} needs review",
                fix_command=fix_command,
            )
        return HealthCheckResult(
            check_id=name,
            provider_id=self.provider_id,
            scope=CheckScope(
                kind=SCOPE_RUNTIME if gate.get("kind") == "runtime" else SCOPE_STRUCTURE,
                target=str(self._root_dir),
            ),
            status=status,
            severity=_severity(status),
            observed_at=observed_at,
            provenance=provenance,
            summary=detail[:200],
            detail=detail,
            duration_s=_as_float(duration),
            timeout_s=timeout_s,
            cause=cause,
            next_action=action,
        )


class RuntimeEvidenceProvider:
    """Projects the runtime-evidence sections. One check per section."""

    provider_id = PROVIDER_RUNTIME

    def __init__(
        self,
        root_dir: Path,
        model: Mapping[str, Any],
        cwd: str | None = None,
        declared_servers: Any = None,
    ) -> None:
        self._root_dir = Path(root_dir)
        self._model = model
        self._cwd = cwd
        self._declared_servers = declared_servers

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            title="Runtime evidence packet",
            scope_kinds=(SCOPE_RUNTIME,),
            default_max_age_s=None,
        )

    def collect(self) -> Sequence[HealthCheckResult]:
        from .evidence import collect_runtime_evidence

        payload = collect_runtime_evidence(
            self._root_dir,
            dict(self._model),
            cwd=self._cwd,
            declared_servers=self._declared_servers,
        )
        observed_at = time.time()
        provenance = Provenance(
            provider_id=self.provider_id,
            source="runtime_manager.evidence.collect_runtime_evidence",
            collector="python3 .env-manager/manage.py evidence --format json",
        )
        blocked = tuple(str(item) for item in (payload.get("blocked_conditions") or []))
        sections = payload.get("sections") or {}
        results: list[HealthCheckResult] = []

        # Only some sections carry a verdict of their own; the rest are factual
        # (counts, paths, flags) and the packet's verdict lives at packet level.
        # Inventing a per-section status for a factual section would manufacture
        # unknowns the provider never reported — the precise way a federation
        # starts lying about the surface it aggregates.
        for name, section in sorted(sections.items()):
            if not isinstance(section, Mapping):
                continue
            if not str(section.get("status") or ""):
                continue
            results.append(
                self._section_result(str(name), section, observed_at, provenance, blocked)
            )
        results.append(
            self._packet_result(payload, sections, observed_at, provenance, blocked)
        )
        return tuple(results)

    def _packet_result(
        self,
        payload: Mapping[str, Any],
        sections: Mapping[str, Any],
        observed_at: float,
        provenance: Provenance,
        blocked: tuple[str, ...],
    ) -> HealthCheckResult:
        """The packet's own traffic light, with the factual sections as evidence."""

        overall = str(payload.get("overall") or "").lower()
        status = {
            "green": STATUS_PASS,
            "yellow": STATUS_WARN,
            "red": STATUS_FAIL,
        }.get(overall, STATUS_UNAVAILABLE)
        actions = [str(item) for item in (payload.get("next_actions") or []) if str(item)]
        action = NO_ACTION
        if status != STATUS_PASS and actions:
            action = _inspect_action(
                action_id=f"{self.provider_id}:packet",
                summary=actions[0],
                fix_command=actions[0],
            )
        return HealthCheckResult(
            check_id="evidence-packet",
            provider_id=self.provider_id,
            scope=CheckScope(kind=SCOPE_RUNTIME, target=str(self._root_dir)),
            status=status,
            severity=_severity(status),
            observed_at=observed_at,
            provenance=provenance,
            summary=f"runtime evidence packet is {overall or 'unknown'}",
            detail="; ".join(blocked),
            cause="" if status != STATUS_UNAVAILABLE else (
                f"packet reported unrecognized overall {overall!r}"
            ),
            next_action=action,
            blocked_conditions=blocked,
            # Factual sections ride as evidence rather than as fabricated checks.
            details={
                "factual_sections": sorted(
                    name
                    for name, section in sections.items()
                    if isinstance(section, Mapping) and not str(section.get("status") or "")
                )
            },
        )

    def _section_result(
        self,
        name: str,
        section: Mapping[str, Any],
        observed_at: float,
        provenance: Provenance,
        blocked: tuple[str, ...],
    ) -> HealthCheckResult:
        native = str(section.get("status") or "").lower()
        status = _SECTION_STATUS.get(native, STATUS_UNAVAILABLE)
        cause = "" if status != STATUS_UNAVAILABLE or native in _SECTION_STATUS else (
            f"section reported unrecognized status {native!r}"
        )
        if status == STATUS_UNAVAILABLE and not cause:
            cause = str(section.get("detail") or "section reported no verdict")

        actions = [str(item) for item in (section.get("next_actions") or []) if str(item)]
        action = NO_ACTION
        related: list[NextAction] = []
        if actions and status != STATUS_PASS:
            action = _inspect_action(
                action_id=f"{self.provider_id}:{name}",
                summary=actions[0],
                fix_command=actions[0],
            )
            related = [
                _inspect_action(
                    action_id=f"{self.provider_id}:{name}:{index}",
                    summary=entry,
                    fix_command=entry,
                )
                for index, entry in enumerate(actions[1:], start=1)
            ]
        return HealthCheckResult(
            check_id=name,
            provider_id=self.provider_id,
            scope=CheckScope(kind=SCOPE_RUNTIME, target=str(self._root_dir)),
            status=status,
            severity=_severity(status),
            observed_at=observed_at,
            provenance=provenance,
            summary=str(section.get("summary") or "")[:200],
            detail=str(section.get("detail") or ""),
            cause=cause,
            next_action=action,
            related_actions=tuple(related),
            blocked_conditions=blocked,
        )


class OuterReconcileProvider:
    """Projects ``scripts/04-reconcile.py`` doctor checks.

    Loaded lazily by path: the outer reconciler cannot import
    ``runtime_manager`` (that constraint is why ``scripts/lib/doctor_contract``
    exists), and a module-level import here would put the dependency the wrong
    way round in the CLI's import graph. A load failure becomes an
    ``unavailable`` result rather than an exception, which is the truthful
    answer for "the outer surface could not be consulted".
    """

    provider_id = PROVIDER_OUTER

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id=self.provider_id,
            title="Outer reconcile checks",
            scope_kinds=(SCOPE_REPO,),
            default_max_age_s=None,
        )

    def _load(self) -> Any:
        script = self._root_dir / "scripts" / "04-reconcile.py"
        # The script imports `lib.runtime_model`, so `scripts/` must be on the
        # path — the same bootstrap tests/test_reconcile.py performs. Idempotent,
        # and it adds a directory to the import path, nothing on disk.
        scripts_dir = str(self._root_dir / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        name = "skillbox_outer_reconcile_health"
        cached = sys.modules.get(name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(name, script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {script.name}")
        module = importlib.util.module_from_spec(spec)
        # Registered before exec: a module that inspects sys.modules for itself
        # during import must find it there.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    def collect(self) -> Sequence[HealthCheckResult]:
        module = self._load()
        # Skip the compose and skill-sync probes: both shell out, and a health
        # front door must not start anything to find out how things are.
        checks = module.doctor_results(skip_compose=True, skip_skill_sync=True)
        observed_at = time.time()
        provenance = Provenance(
            provider_id=self.provider_id,
            source="scripts/04-reconcile.py doctor_results",
            collector="python3 scripts/04-reconcile.py doctor --format json",
        )
        results: list[HealthCheckResult] = []
        for check in checks:
            native = str(getattr(check, "status", "") or "").lower()
            status = _SECTION_STATUS.get(native, STATUS_UNAVAILABLE)
            code = str(getattr(check, "code", "") or "outer-check")
            message = str(getattr(check, "message", "") or "")
            fix_command = str(getattr(check, "fix_command", "") or "")
            action = NO_ACTION
            if status != STATUS_PASS:
                action = _inspect_action(
                    action_id=f"{self.provider_id}:{code}",
                    summary=message or f"outer check {code} needs review",
                    fix_command=fix_command,
                )
            results.append(
                HealthCheckResult(
                    check_id=code,
                    provider_id=self.provider_id,
                    scope=CheckScope(kind=SCOPE_REPO, target=str(self._root_dir)),
                    status=status,
                    severity=_severity(status),
                    observed_at=observed_at,
                    provenance=provenance,
                    summary=message[:200],
                    detail=message,
                    cause="" if status != STATUS_UNAVAILABLE else message,
                    next_action=action,
                )
            )
        return tuple(results)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric == numeric and numeric >= 0 else None


# --------------------------------------------------------------------------- #
# Bounded-concurrency collection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CollectionReport:
    """Results plus how they were obtained. Ordering is never arrival order."""

    results: tuple[HealthCheckResult, ...]
    descriptors: tuple[ProviderDescriptor, ...]
    elapsed_s: float
    timeout_s: float
    max_workers: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed_s, 6),
            "timeout_s": self.timeout_s,
            "max_workers": self.max_workers,
            "providers": [descriptor.to_payload() for descriptor in self.descriptors],
        }


def _unknown_result(
    descriptor: ProviderDescriptor,
    status: str,
    cause: str,
    observed_at: float,
    timeout_s: float | None,
    elapsed_s: float | None,
) -> HealthCheckResult:
    """A provider that produced no verdict still owes the operator a result."""

    return HealthCheckResult(
        check_id=f"{descriptor.provider_id}:provider",
        provider_id=descriptor.provider_id,
        scope=CheckScope(
            kind=descriptor.scope_kinds[0] if descriptor.scope_kinds else SCOPE_RUNTIME,
            target=descriptor.provider_id,
        ),
        status=status,
        severity=SEVERITY_UNKNOWN,
        observed_at=observed_at,
        provenance=Provenance(
            provider_id=descriptor.provider_id,
            source=descriptor.title or descriptor.provider_id,
            collector="health federation",
        ),
        summary=f"{descriptor.provider_id} produced no verdict",
        detail=cause,
        duration_s=elapsed_s,
        timeout_s=timeout_s,
        cause=cause,
        next_action=_inspect_action(
            action_id=f"{descriptor.provider_id}:provider",
            summary=f"consult {descriptor.provider_id} directly; the federation could not",
        ),
    )


def collect_health(
    providers: Iterable[Any],
    *,
    timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S,
    max_workers: int = DEFAULT_MAX_WORKERS,
    clock: Callable[[], float] = time.monotonic,
) -> CollectionReport:
    """Run every provider concurrently under one wall-clock cap.

    Total latency tracks the slowest provider plus overhead rather than the sum,
    and a provider that raises or overruns yields an ``unavailable`` /
    ``timed_out`` result instead of removing itself from the report.
    """

    materialized = list(providers)
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    descriptors: list[ProviderDescriptor] = []
    for provider in materialized:
        descriptor = provider.describe()
        if not isinstance(descriptor, ProviderDescriptor):
            raise TypeError(f"{provider!r} did not describe itself")
        descriptors.append(descriptor)

    started = clock()
    results: list[HealthCheckResult] = []
    if materialized:
        workers = min(max_workers, len(materialized))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                (descriptor, pool.submit(provider.collect))
                for provider, descriptor in zip(materialized, descriptors)
            ]
            deadline = clock() + timeout_s
            for descriptor, future in futures:
                remaining = max(0.0, deadline - clock())
                try:
                    collected = future.result(timeout=remaining)
                except FutureTimeoutError:
                    future.cancel()
                    results.append(
                        _unknown_result(
                            descriptor,
                            STATUS_TIMED_OUT,
                            f"provider exceeded the {timeout_s:g}s federation cap",
                            time.time(),
                            timeout_s,
                            timeout_s,
                        )
                    )
                    continue
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    results.append(
                        _unknown_result(
                            descriptor,
                            STATUS_UNAVAILABLE,
                            _cause_text(error),
                            time.time(),
                            None,
                            None,
                        )
                    )
                    continue
                for result in collected:
                    if not isinstance(result, HealthCheckResult):
                        raise TypeError(
                            f"provider {descriptor.provider_id!r} returned a non-result"
                        )
                    results.append(result)
    elapsed = clock() - started
    return CollectionReport(
        results=tuple(results),
        descriptors=tuple(sorted(descriptors, key=lambda d: d.provider_id)),
        elapsed_s=elapsed,
        timeout_s=timeout_s,
        max_workers=max_workers,
    )


def default_providers(
    root_dir: Path,
    model: Mapping[str, Any],
    *,
    cwd: str | None = None,
    declared_servers: Any = None,
) -> tuple[Any, ...]:
    """The three authoritative surfaces, in a stable order."""

    return (
        OuterReconcileProvider(root_dir),
        StructureDoctorProvider(root_dir, Path(cwd) if cwd else None),
        RuntimeEvidenceProvider(root_dir, model, cwd=cwd, declared_servers=declared_servers),
    )


def federated_health_payload(
    report: CollectionReport,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """The full read-only payload: federation contract plus collection facts."""

    payload = federation_payload(report.results, now)
    payload["schema"] = FEDERATION_SCHEMA
    payload["collection"] = report.to_payload()
    payload["read_only"] = True
    return payload


def collect_federated_health(
    root_dir: Path,
    model: Mapping[str, Any],
    *,
    cwd: str | None = None,
    declared_servers: Any = None,
    timeout_s: float = DEFAULT_PROVIDER_TIMEOUT_S,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """One read-only call behind ``doctor --all``."""

    report = collect_health(
        default_providers(
            root_dir, model, cwd=cwd, declared_servers=declared_servers
        ),
        timeout_s=timeout_s,
        max_workers=max_workers,
    )
    return federated_health_payload(report)


def federation_text_lines(payload: Mapping[str, Any]) -> list[str]:
    """Human rendering. Prints the one primary action, never a menu of them."""

    lines = ["doctor --all — federated health (read-only; no fixes are run)", ""]
    summary = payload.get("summary") or {}
    collection = payload.get("collection") or {}
    lines.append(
        f"  overall={payload.get('prioritization', {}).get('overall', '?')}  "
        f"checks={summary.get('total', 0)}  unknown={summary.get('unknown', 0)}  "
        f"elapsed={collection.get('elapsed_s', 0)}s"
    )
    lines.append("")
    for check in payload.get("checks") or []:
        lines.append(
            f"  {str(check.get('status', '')):11s}  "
            f"{str(check.get('provider_id', '')):17s}  "
            f"{str(check.get('check_id', ''))}"
        )
    primary = (payload.get("prioritization") or {}).get("primary")
    lines.append("")
    if primary:
        action = primary.get("action") or {}
        lines.append(f"  next: {action.get('summary', '')}")
        if action.get("fix_command"):
            lines.append(f"        {action['fix_command']}   (display only)")
    else:
        lines.append("  next: nothing actionable")
    return lines


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_PROVIDER_TIMEOUT_S",
    "FEDERATION_KIND",
    "FEDERATION_SCHEMA",
    "PROVIDER_OUTER",
    "PROVIDER_RUNTIME",
    "PROVIDER_STRUCTURE",
    "CollectionReport",
    "OuterReconcileProvider",
    "RuntimeEvidenceProvider",
    "StructureDoctorProvider",
    "collect_federated_health",
    "collect_health",
    "default_providers",
    "federated_health_payload",
    "federation_text_lines",
]
