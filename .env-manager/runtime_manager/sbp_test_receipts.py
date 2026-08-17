"""``test-attempt/v1`` + ``test-receipt/v1``: three-axis verdicts that never lie.

A single "pass/fail" field cannot carry a test run honestly, because three
independent things can go wrong and only one of them is the test. So a verdict
is a triple:

* **test outcome** — ``passed`` / ``failed`` / ``skipped`` / ``not_run``: what
  the test itself said.
* **execution outcome** — ``completed`` / ``launch_failed`` / ``timeout`` /
  ``canceled`` / ``result_unavailable`` / ``artifact_incomplete`` /
  ``executor_lost`` / ``admission_unknown``: what happened to the attempt.
* **proof** — ``complete`` / ``partial`` / ``indeterminate``: how much of the
  evidence we actually hold.

The repair that makes the vocabulary coherent: **a nonzero test exit is
``test_outcome=failed`` + ``execution_outcome=completed`` + ``proof=complete``**.
The run worked perfectly; the test failed. Calling that an infrastructure
failure is the most common way a test system lies, and it trains people to
re-run instead of read.

The inverse lie is the one this module exists to prevent: a timeout, a
cancellation, or a missing artifact must never render as a test failure, and
must never render as a pass. They are ``not_run`` with a named execution
outcome, and they exit nonzero — because "we do not know" is not "fine".

**Invalid is unrepresentable.** Of the 4 x 8 x 3 = 96 nominal triples, 15 are
meaningful; :data:`VALID_VERDICTS` is the matrix and :class:`Verdict` refuses
anything outside it at construction, so no code path can assemble an incoherent
verdict and no consumer needs to re-check.

**Attempts are append-only.** A retry adds ``attempt 2``; it never overwrites
``attempt 1``. A run that ends green after a flake still shows the flake, which
is the only way flakiness is ever noticed.

**Only harvested exit-0 writes green.** The aggregate is a pass only when every
required unit is terminal AND its required proof is complete — the box-down
teardown ethic: you do not get to claim success for evidence you did not
collect.

Storage is ``.skillbox-state/test-runs/<run_id>/``, 0600 files under a 0700
directory, every payload passed through the shared redaction table, and every
write atomic so a crash leaves the previous state intact rather than a torn file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ._shared.errors import EXIT_DRIFT, EXIT_ERROR, EXIT_NEEDS_INPUT, EXIT_OK
from .sbp_test_capsule import is_secret_key

ATTEMPT_SCHEMA = "test-attempt/v1"
RECEIPT_SCHEMA = "test-receipt/v1"
RECEIPT_STORE_RELPATH = ".skillbox-state/test-runs"

STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600

# --------------------------------------------------------------------------- #
# The three axes
# --------------------------------------------------------------------------- #

TEST_PASSED = "passed"
TEST_FAILED = "failed"
TEST_SKIPPED = "skipped"
TEST_NOT_RUN = "not_run"
TEST_OUTCOMES = (TEST_PASSED, TEST_FAILED, TEST_SKIPPED, TEST_NOT_RUN)

#: NORMATIVE (review repair 2026-08-14): ``completed`` — never ``healthy``.
#: A nonzero test exit is a completed execution of a failed test, so a
#: "healthy" value would contradict the axis it lives on.
EXEC_COMPLETED = "completed"
EXEC_LAUNCH_FAILED = "launch_failed"
EXEC_TIMEOUT = "timeout"
EXEC_CANCELED = "canceled"
EXEC_RESULT_UNAVAILABLE = "result_unavailable"
EXEC_ARTIFACT_INCOMPLETE = "artifact_incomplete"
EXEC_EXECUTOR_LOST = "executor_lost"
EXEC_ADMISSION_UNKNOWN = "admission_unknown"
EXECUTION_OUTCOMES = (
    EXEC_COMPLETED,
    EXEC_LAUNCH_FAILED,
    EXEC_TIMEOUT,
    EXEC_CANCELED,
    EXEC_RESULT_UNAVAILABLE,
    EXEC_ARTIFACT_INCOMPLETE,
    EXEC_EXECUTOR_LOST,
    EXEC_ADMISSION_UNKNOWN,
)

PROOF_COMPLETE = "complete"
PROOF_PARTIAL = "partial"
PROOF_INDETERMINATE = "indeterminate"
PROOF_LEVELS = (PROOF_COMPLETE, PROOF_PARTIAL, PROOF_INDETERMINATE)

#: The validity matrix: 15 coherent triples out of 96 nominal combinations.
#:
#: Read it as "what could we have observed together":
#:
#: * ``completed`` means the process ran and we read its exit code, so the test
#:   said something definite and the proof is complete. It can never pair with
#:   ``skipped``/``not_run`` — something that never ran cannot have completed.
#: * ``timeout`` / ``canceled`` / ``result_unavailable`` produced no verdict, so
#:   the test outcome is ``not_run``. ``partial`` is allowed because a truncated
#:   log is real evidence; ``complete`` never is, because a verdict is exactly
#:   what is missing.
#: * ``artifact_incomplete`` is the one case where the test spoke but the
#:   evidence is short. It is never ``complete``, which is what stops a run with
#:   missing artifacts from aggregating to green.
#: * ``launch_failed`` / ``executor_lost`` / ``admission_unknown`` are pure
#:   unknowns: ``not_run`` + ``indeterminate``.
#: * ``skipped`` pairs with ``canceled``: a dependency-blocked unit had its
#:   attempt called off. See ``SKIP_EXECUTION_OUTCOME`` for why.
VALID_VERDICTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        (TEST_PASSED, EXEC_COMPLETED, PROOF_COMPLETE),
        (TEST_FAILED, EXEC_COMPLETED, PROOF_COMPLETE),
        (TEST_PASSED, EXEC_ARTIFACT_INCOMPLETE, PROOF_PARTIAL),
        (TEST_FAILED, EXEC_ARTIFACT_INCOMPLETE, PROOF_PARTIAL),
        (TEST_NOT_RUN, EXEC_ARTIFACT_INCOMPLETE, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_TIMEOUT, PROOF_PARTIAL),
        (TEST_NOT_RUN, EXEC_TIMEOUT, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_CANCELED, PROOF_PARTIAL),
        (TEST_NOT_RUN, EXEC_CANCELED, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_RESULT_UNAVAILABLE, PROOF_PARTIAL),
        (TEST_NOT_RUN, EXEC_RESULT_UNAVAILABLE, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_LAUNCH_FAILED, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_EXECUTOR_LOST, PROOF_INDETERMINATE),
        (TEST_NOT_RUN, EXEC_ADMISSION_UNKNOWN, PROOF_INDETERMINATE),
        (TEST_SKIPPED, EXEC_CANCELED, PROOF_INDETERMINATE),
    }
)

#: A dependency-blocked unit is recorded as ``canceled``.
#:
#: Judgement call, recorded because the vocabulary is normative and I may not
#: extend it: a skip is a *known* decision not to attempt, so
#: ``admission_unknown`` would be a lie (we know exactly why). Among the eight
#: enumerated values ``canceled`` is the only one that means "the attempt was
#: called off", so a skip maps there and the reason names the blocker. A
#: dedicated ``not_attempted`` value would be cleaner; adding one is a
#: vocabulary change, not an implementation detail.
SKIP_EXECUTION_OUTCOME = EXEC_CANCELED

#: Verdicts that end a unit's story. Anything else invites a resume.
TERMINAL_TEST_OUTCOMES = frozenset({TEST_PASSED, TEST_FAILED, TEST_SKIPPED})

RUN_ID_PATTERN = re.compile(r"^[0-9a-z][0-9a-z._-]{0,63}$")
UNIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_ATTEMPTS = 64
MAX_DOCUMENT_BYTES = 1024 * 1024

REFUSAL_CODES = frozenset(
    {
        "attempt_exists",
        "attempt_invalid",
        "attempt_overflow",
        "receipt_invalid",
        "store_invalid",
        "verdict_invalid",
    }
)


class ReceiptRefusal(Exception):
    """A typed, fail-closed refusal. Carries a code, never a secret."""

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
    raise ReceiptRefusal(code, message, units=units)


# --------------------------------------------------------------------------- #
# Verdict — invalid is unrepresentable
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verdict:
    """One unit's three-axis verdict. Refuses any incoherent triple."""

    test_outcome: str
    execution_outcome: str
    proof: str

    def __post_init__(self) -> None:
        triple = (self.test_outcome, self.execution_outcome, self.proof)
        if self.test_outcome not in TEST_OUTCOMES:
            _refuse("verdict_invalid", f"unknown test outcome {self.test_outcome!r}")
        if self.execution_outcome not in EXECUTION_OUTCOMES:
            _refuse(
                "verdict_invalid",
                f"unknown execution outcome {self.execution_outcome!r}",
            )
        if self.proof not in PROOF_LEVELS:
            _refuse("verdict_invalid", f"unknown proof level {self.proof!r}")
        if triple not in VALID_VERDICTS:
            _refuse(
                "verdict_invalid",
                "incoherent verdict "
                f"{self.test_outcome}/{self.execution_outcome}/{self.proof}: "
                "that combination of observations cannot have happened together",
            )

    @property
    def green(self) -> bool:
        """Only a completed, fully-proven pass is green. Nothing else, ever."""

        return (
            self.test_outcome == TEST_PASSED
            and self.execution_outcome == EXEC_COMPLETED
            and self.proof == PROOF_COMPLETE
        )

    @property
    def is_test_failure(self) -> bool:
        """True only when the TEST failed. A timeout is not a test failure."""

        return self.test_outcome == TEST_FAILED

    @property
    def is_unproven(self) -> bool:
        """We do not hold enough evidence to say. Nonzero, but not a failure."""

        return self.proof != PROOF_COMPLETE

    @property
    def is_terminal(self) -> bool:
        return self.test_outcome in TERMINAL_TEST_OUTCOMES and not self.is_unproven

    def to_payload(self) -> dict[str, Any]:
        return {
            "test_outcome": self.test_outcome,
            "execution_outcome": self.execution_outcome,
            "proof": self.proof,
            "green": self.green,
            "terminal": self.is_terminal,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> Verdict:
        if not isinstance(value, Mapping):
            _refuse("verdict_invalid", "verdict must be a mapping")
        return cls(
            test_outcome=str(value.get("test_outcome") or ""),
            execution_outcome=str(value.get("execution_outcome") or ""),
            proof=str(value.get("proof") or ""),
        )


def invalid_verdicts() -> tuple[tuple[str, str, str], ...]:
    """Every nominal triple that the matrix rejects — the generator's input."""

    return tuple(
        sorted(
            (test, execution, proof)
            for test in TEST_OUTCOMES
            for execution in EXECUTION_OUTCOMES
            for proof in PROOF_LEVELS
            if (test, execution, proof) not in VALID_VERDICTS
        )
    )


# --------------------------------------------------------------------------- #
# Observation -> verdict (the reducer's per-attempt half)
# --------------------------------------------------------------------------- #

#: Executor unit states, translated. The executor spells it "cancelled"; this
#: axis is normatively "canceled". Both spellings are accepted on input so the
#: two modules can disagree about English without disagreeing about meaning.
_EXECUTOR_CANCELLED = ("cancelled", "canceled")


def verdict_from_unit_result(
    result: Mapping[str, Any],
    *,
    artifacts_complete: bool = True,
    log_present: bool | None = None,
) -> Verdict:
    """Translate one ``sbp_test_executor`` result into a three-axis verdict.

    The whole vocabulary repair lives in the first branch: a completed process
    with a nonzero exit is a **failed test**, not a broken executor.
    """

    if not isinstance(result, Mapping):
        _refuse("attempt_invalid", "unit result must be a mapping")
    state = str(result.get("state") or "")
    exit_code = result.get("exit_code")
    cause = str(result.get("cause") or "")
    has_log = bool(result.get("log_file")) if log_present is None else bool(log_present)

    if state == "completed":
        if not artifacts_complete:
            # The test spoke, but we did not collect what it promised. Never
            # complete proof, so this can never aggregate to green.
            return Verdict(
                TEST_PASSED if exit_code == 0 else TEST_FAILED,
                EXEC_ARTIFACT_INCOMPLETE,
                PROOF_PARTIAL,
            )
        if exit_code == 0:
            return Verdict(TEST_PASSED, EXEC_COMPLETED, PROOF_COMPLETE)
        return Verdict(TEST_FAILED, EXEC_COMPLETED, PROOF_COMPLETE)

    if state == "failed":
        # The executor could not start it at all: no test evidence exists.
        if "could not start" in cause:
            return Verdict(TEST_NOT_RUN, EXEC_LAUNCH_FAILED, PROOF_INDETERMINATE)
        if exit_code is None:
            return Verdict(TEST_NOT_RUN, EXEC_RESULT_UNAVAILABLE, PROOF_INDETERMINATE)
        if not artifacts_complete:
            return Verdict(TEST_FAILED, EXEC_ARTIFACT_INCOMPLETE, PROOF_PARTIAL)
        return Verdict(TEST_FAILED, EXEC_COMPLETED, PROOF_COMPLETE)

    if state == "timed_out":
        return Verdict(
            TEST_NOT_RUN,
            EXEC_TIMEOUT,
            PROOF_PARTIAL if has_log else PROOF_INDETERMINATE,
        )

    if state in _EXECUTOR_CANCELLED:
        return Verdict(
            TEST_NOT_RUN,
            EXEC_CANCELED,
            PROOF_PARTIAL if has_log else PROOF_INDETERMINATE,
        )

    if state == "skipped":
        return Verdict(TEST_SKIPPED, SKIP_EXECUTION_OUTCOME, PROOF_INDETERMINATE)

    if state == "not_run":
        return Verdict(TEST_NOT_RUN, EXEC_ADMISSION_UNKNOWN, PROOF_INDETERMINATE)

    # An executor state this module does not recognise is an unknown, never a
    # pass: the run may have happened, but nothing here can vouch for it.
    return Verdict(TEST_NOT_RUN, EXEC_EXECUTOR_LOST, PROOF_INDETERMINATE)


# --------------------------------------------------------------------------- #
# test-attempt/v1
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Attempt:
    """One immutable attempt at one unit. Retries append; they never overwrite."""

    unit_id: str
    attempt: int
    verdict: Verdict
    exit_code: int | None = None
    duration_s: float = 0.0
    log_digest: str | None = None
    artifacts: tuple[str, ...] = ()
    cause: str = ""
    cache_key: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if UNIT_ID_PATTERN.fullmatch(self.unit_id) is None:
            _refuse("attempt_invalid", "attempt requires a well-formed unit id")
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_ATTEMPTS:
            _refuse("attempt_invalid", f"attempt number must be 1..{MAX_ATTEMPTS}")
        if not isinstance(self.verdict, Verdict):
            _refuse("attempt_invalid", "attempt requires a Verdict")
        if self.log_digest is not None and DIGEST_PATTERN.fullmatch(self.log_digest) is None:
            _refuse("attempt_invalid", "log_digest must be a sha256 hex digest")

    def to_payload(self) -> dict[str, Any]:
        return redact_payload(
            {
                "schema": ATTEMPT_SCHEMA,
                "unit_id": self.unit_id,
                "attempt": self.attempt,
                "verdict": self.verdict.to_payload(),
                "exit_code": self.exit_code,
                "duration_s": round(float(self.duration_s), 6),
                "log_digest": self.log_digest,
                "artifacts": list(self.artifacts),
                "cause": self.cause,
                "cache_key": dict(self.cache_key),
            }
        )

    @classmethod
    def from_mapping(cls, value: Any) -> Attempt:
        if not isinstance(value, Mapping):
            _refuse("attempt_invalid", "attempt must be a mapping")
        if value.get("schema") != ATTEMPT_SCHEMA:
            _refuse("attempt_invalid", f"attempt schema must be {ATTEMPT_SCHEMA}")
        return cls(
            unit_id=str(value.get("unit_id") or ""),
            attempt=value.get("attempt"),  # type: ignore[arg-type]
            verdict=Verdict.from_mapping(value.get("verdict")),
            exit_code=value.get("exit_code"),
            duration_s=float(value.get("duration_s") or 0.0),
            log_digest=value.get("log_digest"),
            artifacts=tuple(value.get("artifacts") or []),
            cause=str(value.get("cause") or ""),
            cache_key=dict(value.get("cache_key") or {}),
        )


def cache_key_material(
    *,
    plan_digest: str,
    manifest_digest: str,
    unit_argv: Sequence[str],
    source: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Cache-key material, recorded from day one; nothing here consults a cache.

    Execution in this leaf is always fresh — cache *authority* is the P5 leaf's.
    Recording the material now means those runs can be matched retroactively
    instead of starting from an empty history.

    The env contribution is a **digest of names and values**, never the values:
    a cache key must be sensitive to a changed token without ever storing one.
    """

    argv_digest = hashlib.sha256(
        "\0".join(str(part) for part in unit_argv).encode("utf-8")
    ).hexdigest()
    env_items = sorted((str(k), str(v)) for k, v in (env or {}).items())
    env_digest = hashlib.sha256(
        "\0".join(f"{k}={v}" for k, v in env_items).encode("utf-8")
    ).hexdigest()
    return {
        "plan_digest": plan_digest,
        "manifest_digest": manifest_digest,
        "argv_digest": argv_digest,
        "env_digest": env_digest,
        "env_names": [name for name, _ in env_items],
        "source": dict(sorted((source or {}).items())),
    }


# --------------------------------------------------------------------------- #
# Redaction + atomic storage
# --------------------------------------------------------------------------- #


def redact_payload(document: Any, _depth: int = 0) -> Any:
    """Drop secret-shaped keys before anything is written or rendered.

    Uses the shared ``scripts/lib/redaction`` table via
    :mod:`runtime_manager.sbp_test_capsule`, so a receipt screens exactly what a
    capsule screens.
    """

    if _depth > 12:
        _refuse("receipt_invalid", "receipt document nests too deeply")
    if isinstance(document, Mapping):
        clean: dict[str, Any] = {}
        for key, value in document.items():
            name = str(key)
            if is_secret_key(name):
                clean[name] = "[REDACTED]"
                continue
            clean[name] = redact_payload(value, _depth + 1)
        return clean
    if isinstance(document, (list, tuple)):
        return [redact_payload(item, _depth + 1) for item in document]
    return document


def run_store(state_root: Any, run_id: str) -> Path:
    """``<state_root>/test-runs/<run_id>`` — created 0700 on first use."""

    if RUN_ID_PATTERN.fullmatch(str(run_id) or "") is None:
        _refuse("store_invalid", "run id is malformed")
    if isinstance(state_root, Path):
        root = state_root
    elif isinstance(state_root, str) and state_root:
        root = Path(state_root)
    else:
        _refuse("store_invalid", "state root is required")
    return root / "test-runs" / str(run_id)


def _write_json(
    path: Path,
    document: Any,
    *,
    on_write: Callable[[str, Path], None] | None = None,
) -> Path:
    """Atomic private write. ``on_write`` is the crash-injection seam.

    temp file in the same directory -> 0600 -> fsync -> ``os.replace``. A crash
    at any point leaves the previous file intact; a reader never sees a torn
    one. ``on_write`` is called with ``"before"`` and ``"after"`` so a test can
    raise at either edge and assert exactly that.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, STORE_DIR_MODE)
    encoded = json.dumps(redact_payload(document), sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _refuse("receipt_invalid", "receipt document exceeds the size budget")
    if on_write is not None:
        on_write("before", path)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, STORE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    if on_write is not None:
        on_write("after", path)
    return path


def append_attempt(
    state_root: Any,
    run_id: str,
    attempt: Attempt,
    *,
    on_write: Callable[[str, Path], None] | None = None,
) -> Path:
    """Write one attempt. Refuses to overwrite an existing attempt number.

    Append-only is the whole point: a run that goes green on retry must still
    show the attempt that did not, because that is the only way flakiness is
    ever seen.
    """

    if not isinstance(attempt, Attempt):
        _refuse("attempt_invalid", "append_attempt requires an Attempt")
    store = run_store(state_root, run_id)
    target = store / "attempts" / attempt.unit_id / f"{attempt.attempt:03d}.json"
    if target.exists():
        _refuse(
            "attempt_exists",
            f"attempt {attempt.attempt} for {attempt.unit_id!r} already exists; "
            "attempts are append-only and a retry must use the next number",
            units=[attempt.unit_id],
        )
    return _write_json(target, attempt.to_payload(), on_write=on_write)


def read_attempts(state_root: Any, run_id: str, unit_id: str) -> tuple[Attempt, ...]:
    """Every recorded attempt for one unit, in attempt order."""

    directory = run_store(state_root, run_id) / "attempts" / unit_id
    if not directory.is_dir():
        return ()
    attempts: list[Attempt] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _refuse("attempt_invalid", f"attempt file is unreadable: {path.name}")
        attempts.append(Attempt.from_mapping(document))
    return tuple(sorted(attempts, key=lambda item: item.attempt))


def next_attempt_number(state_root: Any, run_id: str, unit_id: str) -> int:
    existing = read_attempts(state_root, run_id, unit_id)
    number = (existing[-1].attempt + 1) if existing else 1
    if number > MAX_ATTEMPTS:
        _refuse("attempt_overflow", f"unit {unit_id!r} exceeded {MAX_ATTEMPTS} attempts")
    return number


# --------------------------------------------------------------------------- #
# Reducer + test-receipt/v1
# --------------------------------------------------------------------------- #


def reduce_unit(attempts: Sequence[Attempt]) -> Verdict:
    """Fold a unit's attempts into its current verdict.

    Monotonic in the only direction that matters: a later attempt supersedes an
    earlier one, so a retry that passes yields a pass — but the earlier attempt
    is still on disk, so the flake is not erased. With no attempts at all the
    answer is an honest unknown, never a pass.
    """

    if not attempts:
        return Verdict(TEST_NOT_RUN, EXEC_ADMISSION_UNKNOWN, PROOF_INDETERMINATE)
    return sorted(attempts, key=lambda item: item.attempt)[-1].verdict


@dataclass(frozen=True)
class RunReceipt:
    """``test-receipt/v1``: the aggregate, and the exit code it justifies."""

    run_id: str
    plan_digest: str
    units: Mapping[str, Verdict]
    required: tuple[str, ...]
    attempts_by_unit: Mapping[str, int] = field(default_factory=dict)
    manifest_mismatch: tuple[str, ...] = ()

    @property
    def required_verdicts(self) -> tuple[Verdict, ...]:
        return tuple(self.units[uid] for uid in self.required if uid in self.units)

    @property
    def missing_required(self) -> tuple[str, ...]:
        return tuple(sorted(uid for uid in self.required if uid not in self.units))

    @property
    def green(self) -> bool:
        """Every required unit passed, completed, and fully proven. Nothing less.

        The box-down teardown ethic: a run does not get to claim success for
        evidence it never collected. A *skipped* required unit is deliberately
        not exempt — a skip means nobody ran it, so the run cannot claim it
        passed; it reports unproven and a resume can settle it.

        A run with no required units is not green either, because there is
        nothing it could have proven.
        """

        if self.manifest_mismatch or self.missing_required or not self.required:
            return False
        return all(verdict.green for verdict in self.required_verdicts)

    @property
    def failed_units(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                uid
                for uid in self.required
                if uid in self.units and self.units[uid].is_test_failure
            )
        )

    @property
    def unproven_units(self) -> tuple[str, ...]:
        """Required units we cannot vouch for — missing, skipped, or unproven."""

        return tuple(
            sorted(
                uid
                for uid in self.required
                if uid not in self.units or self.units[uid].is_unproven
            )
        )

    def exit_code(self) -> int:
        """Verdict -> the family exit ladder.

        Precedence, and the reason for it:

        1. ``EXIT_DRIFT`` (4) — the manifest does not describe reality, so every
           other number would be computed from the wrong question.
        2. ``EXIT_ERROR`` (1) — a required test failed. A definite negative
           outranks an unknown; you have something to fix.
        3. ``EXIT_NEEDS_INPUT`` (3) — unproven. Nonzero, because "we do not
           know" is not "fine", and deliberately NOT rendered as a test failure.
        4. ``EXIT_OK`` (0) — only a fully harvested green.
        """

        if self.manifest_mismatch:
            return EXIT_DRIFT
        if self.failed_units:
            return EXIT_ERROR
        if self.unproven_units:
            return EXIT_NEEDS_INPUT
        return EXIT_OK if self.green else EXIT_NEEDS_INPUT

    def next_actions(self) -> list[str]:
        code = self.exit_code()
        if code == EXIT_NEEDS_INPUT:
            return ["sbp test resume"]
        if code == EXIT_DRIFT:
            return ["sbp test plan --format json  # reconcile the manifest with reality"]
        return []

    def to_payload(self) -> dict[str, Any]:
        code = self.exit_code()
        return redact_payload(
            {
                "schema": RECEIPT_SCHEMA,
                "run_id": self.run_id,
                "plan_digest": self.plan_digest,
                "green": self.green,
                "exit_code": code,
                "verdict_class": _verdict_class(code),
                "required": list(self.required),
                "failed_units": list(self.failed_units),
                "unproven_units": list(self.unproven_units),
                "missing_required": list(self.missing_required),
                "manifest_mismatch": list(self.manifest_mismatch),
                "units": {
                    uid: verdict.to_payload() for uid, verdict in sorted(self.units.items())
                },
                "attempts_by_unit": dict(sorted(self.attempts_by_unit.items())),
                "next_actions": self.next_actions(),
            }
        )


def _verdict_class(code: int) -> str:
    if code == EXIT_OK:
        return "passed"
    if code == EXIT_ERROR:
        return "failed"
    if code == EXIT_DRIFT:
        return "drifted"
    return "unproven"


def finalize_indeterminate(
    receipt: RunReceipt, units: Iterable[str], *, reason: str = ""
) -> RunReceipt:
    """Authoritatively mark units indeterminate — the missing-artifact case.

    A run may end knowing it never harvested a unit's artifacts. Saying so is
    authoritative and final; leaving the unit silently absent would let the
    aggregate read as if nothing were missing.
    """

    updated = dict(receipt.units)
    for unit_id in units:
        current = updated.get(unit_id)
        if current is not None and current.execution_outcome == EXEC_COMPLETED:
            # It ran and we read its exit code; the shortfall is the evidence.
            updated[unit_id] = Verdict(
                current.test_outcome, EXEC_ARTIFACT_INCOMPLETE, PROOF_PARTIAL
            )
        else:
            updated[unit_id] = Verdict(
                TEST_NOT_RUN, EXEC_ARTIFACT_INCOMPLETE, PROOF_INDETERMINATE
            )
    del reason  # recorded by the caller's attempt, not re-stated here
    return RunReceipt(
        run_id=receipt.run_id,
        plan_digest=receipt.plan_digest,
        units=updated,
        required=receipt.required,
        attempts_by_unit=receipt.attempts_by_unit,
        manifest_mismatch=receipt.manifest_mismatch,
    )


def build_receipt(
    state_root: Any,
    run_id: str,
    *,
    plan_digest: str,
    required: Sequence[str],
    manifest_mismatch: Sequence[str] = (),
) -> RunReceipt:
    """Reduce every recorded attempt in a run into one receipt."""

    store = run_store(state_root, run_id)
    attempts_root = store / "attempts"
    units: dict[str, Verdict] = {}
    counts: dict[str, int] = {}
    if attempts_root.is_dir():
        for directory in sorted(p for p in attempts_root.iterdir() if p.is_dir()):
            attempts = read_attempts(state_root, run_id, directory.name)
            units[directory.name] = reduce_unit(attempts)
            counts[directory.name] = len(attempts)
    return RunReceipt(
        run_id=str(run_id),
        plan_digest=str(plan_digest),
        units=units,
        required=tuple(required),
        attempts_by_unit=counts,
        manifest_mismatch=tuple(manifest_mismatch),
    )


def write_receipt(
    state_root: Any,
    receipt: RunReceipt,
    *,
    on_write: Callable[[str, Path], None] | None = None,
) -> Path:
    """Persist ``test-receipt/v1``. Overwrites only the reduction, never attempts."""

    if not isinstance(receipt, RunReceipt):
        _refuse("receipt_invalid", "write_receipt requires a RunReceipt")
    target = run_store(state_root, receipt.run_id) / "receipt.json"
    return _write_json(target, receipt.to_payload(), on_write=on_write)


def read_receipt(state_root: Any, run_id: str) -> dict[str, Any]:
    target = run_store(state_root, run_id) / "receipt.json"
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _refuse("receipt_invalid", "receipt is unreadable")


__all__ = [
    "ATTEMPT_SCHEMA",
    "EXECUTION_OUTCOMES",
    "EXEC_ADMISSION_UNKNOWN",
    "EXEC_ARTIFACT_INCOMPLETE",
    "EXEC_CANCELED",
    "EXEC_COMPLETED",
    "EXEC_EXECUTOR_LOST",
    "EXEC_LAUNCH_FAILED",
    "EXEC_RESULT_UNAVAILABLE",
    "EXEC_TIMEOUT",
    "MAX_ATTEMPTS",
    "PROOF_COMPLETE",
    "PROOF_INDETERMINATE",
    "PROOF_LEVELS",
    "PROOF_PARTIAL",
    "RECEIPT_SCHEMA",
    "RECEIPT_STORE_RELPATH",
    "REFUSAL_CODES",
    "SKIP_EXECUTION_OUTCOME",
    "TEST_FAILED",
    "TEST_NOT_RUN",
    "TEST_OUTCOMES",
    "TEST_PASSED",
    "TEST_SKIPPED",
    "VALID_VERDICTS",
    "Attempt",
    "ReceiptRefusal",
    "RunReceipt",
    "Verdict",
    "append_attempt",
    "build_receipt",
    "cache_key_material",
    "finalize_indeterminate",
    "invalid_verdicts",
    "next_attempt_number",
    "read_attempts",
    "read_receipt",
    "redact_payload",
    "reduce_unit",
    "run_store",
    "verdict_from_unit_result",
    "write_receipt",
]
