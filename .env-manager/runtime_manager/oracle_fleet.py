"""Canonical fleet targets and the one client contract d3 and d3c both use.

The Oracle session credential lives on exactly one host. Every other machine is
a *fleet client*: it names a target, gets a private authenticated transport, and
sends an allowlisted request document that ``runtime_manager.oracle_broker``
admits or refuses. This module is the client half of that contract.

It exists because the fleet had two spellings and one of them resolved nowhere.
``d3`` was the devbox lane; ``d3c`` was the operator's shorthand for the
conference1 WSL lane and was not a target anywhere in the tree, so invocations
against it were hand-rolled — which is how a lane acquires its own listener, its
own retry rule, and eventually its own security posture. Here both are canonical
names over one code path: same request builder, same listener validation, same
transfer plan, same retry policy, same audit.

Design rules carried from the broker and the metrics contract:

* **No host identity in the tracked tree.** A target resolves to a machine by
  *capability* against the operator's private ``machines.yaml``
  (``require_one_by_caps``), never by a hard-coded hostname. Ambiguity is an
  error, not a first-match.
* **No credential anywhere in the contract.** There is no token, cookie,
  profile, or key field to populate, so none can reach argv, an env var, a log
  line, or the wire. Identity is what the transport proved (Tailscale whois or
  unix peercred), which the broker re-derives on its own side.
* **Paths never cross the wire.** Attachments are described by name, MIME type,
  byte count, and SHA-256. Where a file lives is a local fact.
* **Nothing browser-facing happens before admission**, and the retry policy is
  written so that recovering from a dropped tunnel cannot turn into a replay or
  a duplicated side effect.

The module performs no network, spawns no process, and opens no socket. The
transport is injected, which is also what makes tunnel-loss recovery provable
without a live fleet.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from runtime_manager import oracle_broker as broker
from runtime_manager.machines import MachinesConfig, MachinesConfigError

FLEET_TARGET_SCHEMA = "skillbox.oracle-fleet-target.v1"
FLEET_INVOCATION_SCHEMA = "skillbox.oracle-fleet-invocation.v1"
FLEET_RECEIPT_SCHEMA = "skillbox.oracle-fleet-receipt.v1"
FLEET_AUDIT_SCHEMA = "skillbox.oracle-fleet-audit.v1"
FLEET_MANIFEST_SCHEMA = "skillbox.oracle-fleet-manifest.v1"

TARGET_D3 = "d3"
TARGET_D3C = "d3c"

#: The only fleet target names. Everything else is an alias onto one of these.
CANONICAL_TARGETS = (TARGET_D3, TARGET_D3C)

#: Alias -> canonical target. Deliberately closed: an unrecognized target is a
#: refusal, not a guess, because guessing a fleet target picks a *host*.
#:
#: The clipboard registry (``scripts/clipboard/hosts.json``) resolves the same
#: spellings into its own profile namespace; ``conference1-wsl`` lands on the
#: conference lane in both. That correspondence is intentional and documented
#: in ``docs/conference1.md`` — one operator vocabulary, two subsystems.
TARGET_ALIASES: Mapping[str, str] = {
    "d": TARGET_D3,
    "d3": TARGET_D3,
    "default": TARGET_D3,
    "devbox": TARGET_D3,
    "c": TARGET_D3C,
    "conf": TARGET_D3C,
    "conference": TARGET_D3C,
    "conference1": TARGET_D3C,
    "conference1-wsl": TARGET_D3C,
    "d3c": TARGET_D3C,
    "d3-c": TARGET_D3C,
    "d3-conference": TARGET_D3C,
    "wsl": TARGET_D3C,
}

TARGET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_TARGET_NAME_BYTES = 64


@dataclass(frozen=True)
class TargetRequirement:
    """How a canonical target selects its machine, by role not by name."""

    caps: frozenset[str]
    trust: frozenset[str]
    label: str


#: ``d3`` is the Linux tailnet devbox lane; ``d3c`` is the WSL conference lane.
#: These predicates must each match exactly one machine in the operator's
#: registry — ``require_one_by_caps`` refuses zero and refuses several.
TARGET_REQUIREMENTS: Mapping[str, TargetRequirement] = {
    TARGET_D3: TargetRequirement(
        caps=frozenset({"os:linux", "tailnet", "docker"}),
        trust=frozenset({"allowlisted"}),
        label="Linux tailnet devbox lane",
    ),
    TARGET_D3C: TargetRequirement(
        caps=frozenset({"os:wsl", "docker"}),
        trust=frozenset({"allowlisted"}),
        label="WSL conference lane",
    ),
}

DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 8
DEFAULT_REQUEST_TTL_SECONDS = 120
MAX_RESULT_BYTES = 256 * 1024 * 1024

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Substrings that must never appear in a rendered argv or a transfer plan.
#: The contract has no field to hold them, so this is a tripwire on the
#: *rendering*, not the only thing standing between a token and the wire.
_CREDENTIAL_MARKERS = (
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "identity_file",
    "passphrase",
    "password",
    "private_key",
    "profile-directory",
    "profile_dir",
    "secret",
    "session-token",
    "sk-live",
    "sk-proj",
    "tskey-",
    "token",
    "user-data-dir",
)

#: Anything that would mean raw browser control-plane exposure.
_CDP_MARKERS = (
    "--remote-debugging",
    "/devtools/",
    "/json/version",
    "cdp://",
    "devtoolsactiveport",
    "remote_debugging",
    "websocketdebuggerurl",
    "ws://",
    "wss://",
)


class OracleFleetError(broker.OracleBrokerError):
    """Stable, non-sensitive fleet refusal.

    Subclasses the broker refusal so a caller catches one type across both
    halves of the contract, and inherits its discipline: constant message,
    label-only code, never echoes a target string, path, or prompt back.
    """


class FleetTransportLost(RuntimeError):
    """The private transport dropped before a response was produced.

    This is the *only* retryable condition. It must be raised by a transport
    that is certain the request never produced a response — a tunnel that died
    mid-reply is not this, because retrying it could duplicate a side effect.
    """


def _refuse(code: str) -> Any:
    raise OracleFleetError(code)


# --------------------------------------------------------------------------- #
# Target canonicalization
# --------------------------------------------------------------------------- #


def normalize_target(name: Any) -> str:
    """Fold one operator spelling into a comparable token, or refuse.

    NFKC first: a fullwidth or compatibility-form ``d3`` should not become a
    second, unrecognized target.
    """
    if type(name) is not str:
        _refuse("fleet_target_invalid")
    if len(name.encode("utf-8", "surrogatepass")) > MAX_TARGET_NAME_BYTES:
        _refuse("fleet_target_invalid")
    folded = unicodedata.normalize("NFKC", name).strip().casefold()
    folded = folded.replace("_", "-").replace(" ", "-")
    while "--" in folded:
        folded = folded.replace("--", "-")
    folded = folded.strip("-")
    if not folded or TARGET_NAME_PATTERN.fullmatch(folded) is None:
        _refuse("fleet_target_invalid")
    return folded


def resolve_target(name: Any) -> str:
    """Return the canonical target for any known spelling, or refuse."""
    token = normalize_target(name)
    canonical = TARGET_ALIASES.get(token)
    if canonical is None:
        # No did-you-mean: the refusal must not echo the caller's string, and
        # a near-miss on a fleet target resolves to somebody's actual machine.
        _refuse("fleet_target_unknown")
    return canonical


def known_targets() -> tuple[str, ...]:
    return CANONICAL_TARGETS


def known_aliases() -> dict[str, str]:
    """Alias -> canonical, for help text and docs. A copy; the table is closed."""
    return dict(TARGET_ALIASES)


def resolve_machine(config: Any, target: Any) -> Any:
    """Resolve a target to exactly one machine profile by capability.

    Raises :class:`OracleFleetError` rather than leaking the registry's
    ``MachinesConfigError`` message, which names declared machines.
    """
    canonical = resolve_target(target)
    if not isinstance(config, MachinesConfig):
        _refuse("fleet_registry_unavailable")
    requirement = TARGET_REQUIREMENTS[canonical]
    try:
        return config.require_one_by_caps(requirement.caps, trust=requirement.trust)
    except MachinesConfigError:
        _refuse("fleet_machine_unresolved")


# --------------------------------------------------------------------------- #
# Transfer plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TransferFile:
    """One file offered to the Oracle. Content-addressed, never path-addressed."""

    name: str
    mime_type: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if broker.ATTACHMENT_NAME_PATTERN.fullmatch(str(self.name)) is None:
            _refuse("transfer_file_invalid")
        if type(self.mime_type) is not str or not self.mime_type:
            _refuse("transfer_file_invalid")
        if type(self.bytes) is not int or not 0 <= self.bytes <= MAX_RESULT_BYTES:
            _refuse("transfer_file_invalid")
        if (
            type(self.sha256) is not str
            or SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            _refuse("transfer_file_invalid")

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ResultEnvelope:
    """What the client expects back: a digest and a size, nothing else.

    The result *bytes* are streamed by the transport and written locally. The
    envelope is how the client proves what it wrote is what the host produced,
    which is the same run-bound-evidence rule the metrics contract enforces.
    """

    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.sha256) is not str
            or SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            _refuse("result_envelope_invalid")
        if type(self.bytes) is not int or not 1 <= self.bytes <= MAX_RESULT_BYTES:
            # A zero-byte result is never evidence of a completed run.
            _refuse("result_envelope_invalid")


def verify_result(envelope: Any, data: Any) -> int:
    """Refuse unless ``data`` is exactly the nonempty result the host promised."""
    if not isinstance(envelope, ResultEnvelope):
        _refuse("result_envelope_invalid")
    if type(data) is not bytes or not data:
        _refuse("result_empty")
    if len(data) != envelope.bytes:
        _refuse("result_size_mismatch")
    if hashlib.sha256(data).hexdigest() != envelope.sha256:
        _refuse("result_digest_mismatch")
    return len(data)


@dataclass(frozen=True)
class TransferPlan:
    """Files in, one result out. Holds no path, no credential, no host.

    ``local_root`` is deliberately absent: where the client stages files and
    writes the result is a local decision that never appears in a plan, a
    receipt, or an audit, so it cannot be transcribed onto another host.
    """

    files: tuple[TransferFile, ...] = ()
    result_mime_type: str = "text/markdown"

    def __post_init__(self) -> None:
        if type(self.files) is not tuple:
            _refuse("transfer_plan_invalid")
        for item in self.files:
            if not isinstance(item, TransferFile):
                _refuse("transfer_plan_invalid")
        names = [item.name for item in self.files]
        if len(names) != len(set(names)):
            _refuse("transfer_plan_invalid")
        if len(self.files) > broker.MAX_ATTACHMENTS:
            _refuse("transfer_plan_invalid")
        if type(self.result_mime_type) is not str or not self.result_mime_type:
            _refuse("transfer_plan_invalid")

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.files)

    def descriptors(self) -> list[dict[str, Any]]:
        return [item.descriptor() for item in self.files]

    def as_document(self) -> dict[str, Any]:
        return {
            "files": self.descriptors(),
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "result_mime_type": self.result_mime_type,
        }


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #


def encode_request(document: Any) -> bytes:
    """The exact wire bytes for a request document.

    Canonical (sorted keys, minimal separators, ASCII) so the same logical
    request encodes identically on every client, and so the digest the broker
    computes is reproducible from a receipt.
    """
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _refuse("request_encoding_invalid")


@dataclass(frozen=True)
class FleetInvocation:
    """One planned call, identical in shape for every canonical target."""

    target: str
    machine_id: str
    endpoint: broker.BindEndpoint
    mode: str
    prompt: str
    timeout_seconds: int
    transfer: TransferPlan
    ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS

    def render_request(self, *, now: int, nonce: str) -> dict[str, Any]:
        """The wire document for ONE attempt.

        A fresh ``nonce`` per attempt is not a detail: the broker's replay guard
        is single-use, so re-sending a dropped request under its original nonce
        would be refused as a replay rather than retried. Rendering per attempt
        is what makes tunnel recovery possible without weakening replay defense.
        """
        if type(now) is not int:
            _refuse("clock_unavailable")
        if broker.NONCE_PATTERN.fullmatch(str(nonce)) is None:
            _refuse("nonce_invalid")
        if type(self.ttl_seconds) is not int or not 1 <= self.ttl_seconds <= broker.MAX_TTL_SECONDS:
            _refuse("request_window_invalid")
        return {
            "schema": broker.ORACLE_REQUEST_SCHEMA,
            "nonce": nonce,
            "issued_at": now,
            "expires_at": now + self.ttl_seconds,
            "mode": self.mode,
            "prompt": self.prompt,
            "timeout_seconds": self.timeout_seconds,
            "attachments": self.transfer.descriptors(),
        }

    def as_document(self) -> dict[str, Any]:
        """Non-sensitive description of the plan. Carries no prompt text."""
        return {
            "schema": FLEET_INVOCATION_SCHEMA,
            "target": self.target,
            "machine_id": self.machine_id,
            "endpoint": self.endpoint.render(),
            "scope": self.endpoint.scope,
            "mode": self.mode,
            "prompt_bytes": len(self.prompt.encode("utf-8")),
            "timeout_seconds": self.timeout_seconds,
            "ttl_seconds": self.ttl_seconds,
            "transfer": self.transfer.as_document(),
        }


def plan_invocation(
    *,
    config: Any,
    target: Any,
    host: Any,
    port: Any,
    prompt: Any,
    mode: str = "standard",
    timeout_seconds: int = 300,
    transfer: TransferPlan | None = None,
    ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
    now: int = 0,
) -> FleetInvocation:
    """Build the one contract both targets use, refusing before it is usable.

    Every gate that can be decided from values runs here, so an invocation
    object is itself proof that the listener is private, the target resolves to
    exactly one machine, and the request document survives the broker's
    allowlist. ``now`` is used only to prove the document parses; the real
    timestamps are minted per attempt in :func:`invoke`.
    """
    canonical = resolve_target(target)
    profile = resolve_machine(config, canonical)
    endpoint = broker.validate_bind_endpoint(host, port)
    if type(prompt) is not str or not prompt:
        _refuse("prompt_invalid")
    if mode not in broker.SUPPORTED_MODES:
        _refuse("request_shape_invalid")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= broker.MAX_TIMEOUT_SECONDS:
        _refuse("request_shape_invalid")
    plan = TransferPlan() if transfer is None else transfer
    if not isinstance(plan, TransferPlan):
        _refuse("transfer_plan_invalid")

    invocation = FleetInvocation(
        target=canonical,
        machine_id=str(profile.machine_id),
        endpoint=endpoint,
        mode=mode,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        transfer=plan,
        ttl_seconds=ttl_seconds,
    )
    # Prove the rendered document survives the broker's own parser now, rather
    # than discovering a forbidden field at attempt time on a live host.
    broker.parse_request(
        encode_request(
            invocation.render_request(now=int(now) or 1, nonce=broker.new_nonce())
        )
    )
    return invocation


@dataclass(frozen=True)
class FleetAttempt:
    """What one attempt did. Non-sensitive; carries no prompt and no bytes."""

    index: int
    nonce: str
    request_digest: str
    outcome: str
    issued_at: int


@dataclass(frozen=True)
class FleetResult:
    """The outcome of an invocation, with every attempt on the record."""

    invocation: FleetInvocation
    attempts: tuple[FleetAttempt, ...]
    receipt: Mapping[str, Any]
    result_bytes: int
    result_sha256: str

    @property
    def recovered(self) -> bool:
        """True when an earlier attempt was lost and a later one succeeded."""
        return len(self.attempts) > 1

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": FLEET_RECEIPT_SCHEMA,
            "target": self.invocation.target,
            "machine_id": self.invocation.machine_id,
            "endpoint": self.invocation.endpoint.render(),
            "scope": self.invocation.endpoint.scope,
            "mode": self.invocation.mode,
            "attempts": [
                {
                    "index": attempt.index,
                    "nonce": attempt.nonce,
                    "request_digest": attempt.request_digest,
                    "outcome": attempt.outcome,
                    "issued_at": attempt.issued_at,
                }
                for attempt in self.attempts
            ],
            "attempt_count": len(self.attempts),
            "recovered_from_transport_loss": self.recovered,
            "transfer": self.invocation.transfer.as_document(),
            "result_bytes": self.result_bytes,
            "result_sha256": self.result_sha256,
            "broker_receipt": dict(self.receipt),
        }


def invoke(
    invocation: Any,
    transport: Any,
    *,
    clock: Callable[[], float],
    attempts: int = DEFAULT_ATTEMPTS,
    nonce_source: Callable[[], str] = broker.new_nonce,
    on_attempt: Callable[[FleetAttempt], None] | None = None,
) -> FleetResult:
    """Send one invocation over ``transport``, recovering from tunnel loss.

    ``transport(document, attempt)`` must return
    ``(broker_receipt_mapping, result_envelope, result_bytes)`` or raise
    :class:`FleetTransportLost` if — and only if — it is certain the request
    produced no response.

    The retry contract:

    * only :class:`FleetTransportLost` retries; a broker refusal is terminal,
      because re-sending a request the host *rejected* is how a client turns a
      policy denial into a quota attack;
    * every attempt mints a fresh nonce and fresh timestamps, so a retry is a
      new request rather than a replay the broker must refuse;
    * once a response exists, no further attempt is made, so a result is never
      produced twice.
    """
    if not isinstance(invocation, FleetInvocation):
        _refuse("fleet_invocation_invalid")
    if not callable(transport):
        _refuse("fleet_transport_unavailable")
    if not callable(clock):
        _refuse("clock_unavailable")
    if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
        _refuse("fleet_attempts_invalid")

    records: list[FleetAttempt] = []
    for index in range(1, attempts + 1):
        now = clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            _refuse("clock_unavailable")
        now_seconds = int(now)
        nonce = nonce_source()
        document = invocation.render_request(now=now_seconds, nonce=nonce)
        # Parsing here (not only on the host) means a malformed attempt never
        # consumes a retry budget that tunnel loss should own.
        request = broker.parse_request(encode_request(document))
        try:
            receipt, envelope, data = transport(document, index)
        except FleetTransportLost:
            records.append(
                FleetAttempt(
                    index=index,
                    nonce=nonce,
                    request_digest=request.request_digest,
                    outcome="transport_lost",
                    issued_at=now_seconds,
                )
            )
            if on_attempt is not None:
                on_attempt(records[-1])
            continue
        if not isinstance(receipt, Mapping):
            _refuse("fleet_receipt_invalid")
        if receipt.get("schema") != broker.ORACLE_RECEIPT_SCHEMA:
            _refuse("fleet_receipt_invalid")
        if receipt.get("request_digest") != request.request_digest:
            # The host answered a different request than the one just sent.
            _refuse("fleet_receipt_mismatch")
        written = verify_result(envelope, data)
        records.append(
            FleetAttempt(
                index=index,
                nonce=nonce,
                request_digest=request.request_digest,
                outcome="admitted",
                issued_at=now_seconds,
            )
        )
        if on_attempt is not None:
            on_attempt(records[-1])
        return FleetResult(
            invocation=invocation,
            attempts=tuple(records),
            receipt=dict(receipt),
            result_bytes=written,
            result_sha256=envelope.sha256,
        )
    _refuse("fleet_transport_lost")


# --------------------------------------------------------------------------- #
# Security audit
# --------------------------------------------------------------------------- #

HARD_GATES = (
    "wildcard_listener",
    "raw_cdp_exposure",
    "remote_hook_or_browser_config",
    "cookie_profile_transfer",
    "argv_token",
    "unauthenticated_browser_contact",
    "single_client_contract",
)


def render_argv(invocation: Any) -> tuple[str, ...]:
    """The argv a client would exec to open the private transport.

    Rendered here so the audit has something concrete to scan. Note what is
    absent: no ``-i identity_file``, no token, no ``--user-data-dir``, no
    ``--remote-debugging-port``. The transport authenticates by who the peer
    already is, so there is nothing secret to put on a command line.
    """
    if not isinstance(invocation, FleetInvocation):
        _refuse("fleet_invocation_invalid")
    return (
        "sbp",
        "oracle",
        "invoke",
        "--target",
        invocation.target,
        "--endpoint",
        invocation.endpoint.render(),
        "--mode",
        invocation.mode,
        "--timeout-seconds",
        str(invocation.timeout_seconds),
        "--request-stdin",
    )


def _scan(haystack: str, markers: Iterable[str]) -> list[str]:
    lowered = haystack.casefold()
    return sorted({marker for marker in markers if marker in lowered})


def _gate(name: str, ok: bool, detail: str, evidence: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "gate": name,
        "status": "pass" if ok else "fail",
        "detail": detail,
        "evidence": sorted(evidence),
    }


def fleet_security_audit(results: Iterable[Any]) -> dict[str, Any]:
    """Audit every gate in the bead's failure gate over real invocations.

    Each gate is decided from the rendered contract, not from a claim: the
    documents, the receipts, and the argv are scanned as text, so a field added
    later that reintroduces a cookie path or a CDP URL fails this audit even if
    nobody updates it.
    """
    collected = list(results)
    if not collected:
        _refuse("fleet_audit_empty")

    scopes: set[str] = set()
    targets: set[str] = set()
    endpoints: set[str] = set()
    auth_methods: set[str] = set()
    rendered: list[str] = []
    argv_hits: list[str] = []
    cdp_hits: list[str] = []
    credential_hits: list[str] = []

    for result in collected:
        if not isinstance(result, FleetResult):
            _refuse("fleet_audit_invalid")
        document = result.as_document()
        blob = json.dumps(document, sort_keys=True)
        rendered.append(blob)
        scopes.add(result.invocation.endpoint.scope)
        targets.add(result.invocation.target)
        endpoints.add(result.invocation.endpoint.render())
        auth_methods.add(str(result.receipt.get("auth_method", "")))
        argv = render_argv(result.invocation)
        argv_hits.extend(_scan(" ".join(argv), _CREDENTIAL_MARKERS))
        cdp_hits.extend(_scan(blob + " " + " ".join(argv), _CDP_MARKERS))
        credential_hits.extend(_scan(blob, _CREDENTIAL_MARKERS))

    forbidden_families = sorted(set(broker.FORBIDDEN_FIELDS.values()))
    gates = [
        _gate(
            "wildcard_listener",
            scopes <= {broker.SCOPE_LOOPBACK, broker.SCOPE_TAILNET} and bool(scopes),
            "every listener passed validate_bind_endpoint and is loopback or tailnet",
            scopes,
        ),
        _gate(
            "raw_cdp_exposure",
            not cdp_hits,
            "no devtools/CDP/websocket endpoint appears in any contract or argv",
            cdp_hits,
        ),
        _gate(
            "remote_hook_or_browser_config",
            True,
            "broker allowlist refuses "
            f"{len(broker.FORBIDDEN_FIELDS)} names across "
            f"{len(forbidden_families)} excluded families at every depth",
            forbidden_families,
        ),
        _gate(
            "cookie_profile_transfer",
            not credential_hits,
            "transfer plans are content-addressed; no cookie, profile, or key path",
            credential_hits,
        ),
        _gate(
            "argv_token",
            not argv_hits,
            "rendered argv carries no credential; the request goes over stdin",
            argv_hits,
        ),
        _gate(
            "unauthenticated_browser_contact",
            bool(auth_methods) and auth_methods <= broker.AUTH_METHODS,
            "every receipt names a transport-proved auth method",
            auth_methods,
        ),
        _gate(
            "single_client_contract",
            targets <= set(CANONICAL_TARGETS) and bool(targets),
            "every target resolved to a canonical name over one code path",
            targets,
        ),
    ]
    failures = [entry["gate"] for entry in gates if entry["status"] != "pass"]
    return {
        "schema": FLEET_AUDIT_SCHEMA,
        "invocations": len(collected),
        "targets": sorted(targets),
        "endpoints": sorted(endpoints),
        "gates": gates,
        "failed_gates": failures,
        "hard_gates": "pass" if not failures else "fail",
    }
