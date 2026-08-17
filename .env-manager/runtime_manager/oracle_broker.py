"""Restricted trusted-host fleet RPC for the invisible Oracle subagent.

The Oracle session credential lives on exactly one host. Fleet callers never
receive it: they send an allowlisted request document over an authenticated
private transport, and this module decides -- before any browser-facing code
runs -- whether that request may proceed.

Three fail-closed gates, in order:

1. :func:`validate_bind_endpoint` refuses to describe any listener that is not a
   literal loopback or Tailscale address. Wildcard binds (``0.0.0.0``, ``::``,
   and their IPv4-mapped spellings), hostnames, public addresses, and
   privileged ports are all refused, so an unverified listener cannot be handed
   to :func:`broker_admission` at all.
2. :func:`parse_request` accepts an exact key allowlist and refuses -- with a
   distinct code per family -- hooks, environment, CDP/devtools targets,
   browser configuration, cookie/credential material, executable paths, and any
   caller-asserted identity. Identity comes from the transport, never the body.
3. :func:`broker_admission` binds request freshness to a single-use nonce
   (replay defense) and reserves quota through
   :mod:`runtime_manager.oracle_policy` before it yields. Every refusal raises
   BEFORE the yield, so a caller that touches the browser only inside the
   ``with`` block can never make unauthenticated contact.

This module performs no network I/O, spawns no process, reads no file, and
never imports a browser driver. It is pure validation over values the transport
already holds. Content transfer is deliberately out of scope: a request carries
attachment *descriptors* (name, mime type, size, digest), never paths, and
:func:`verify_attachment_bytes` binds delivered bytes to the declared digest.

Refusals raise :class:`OracleBrokerError`, whose ``code`` is a stable,
non-sensitive string. Refusal messages never echo request content. Denials
raised by :mod:`runtime_manager.oracle_policy` are re-raised with their own
code preserved, so quota codes such as ``byte_quota_exceeded`` reach the caller
unchanged.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .errors import ValidationError
from .oracle_attachments import DEFAULT_ALLOWED_MIME_TYPES
from .oracle_policy import (
    CALLER_ID_PATTERN,
    SUPPORTED_MODES,
    OraclePolicyEngine,
    OraclePolicyError,
    OracleRequestFacts,
)

ORACLE_BROKER_PROTOCOL = "skillbox.oracle-broker.v1"
ORACLE_REQUEST_SCHEMA = "skillbox.oracle-request.v1"
ORACLE_RECEIPT_SCHEMA = "skillbox.oracle-receipt.v1"

# Bounds. The prompt/attachment/timeout ceilings mirror the policy engine's own
# ceilings so a document can never describe a request the engine would have to
# reject on shape alone; test_oracle_broker pins that agreement.
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENTS = 32
MAX_ATTACHMENT_BYTES = 256 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 21_600
MAX_ATTACHMENT_NAME_BYTES = 128
MAX_DOCUMENT_DEPTH = 6
MAX_DOCUMENT_KEYS = 32
MAX_DOCUMENT_ITEMS = 64

# Freshness window. A nonce may only be pruned once it is provably expired, so
# these two bounds are what make bounded replay memory safe (see ReplayGuard).
MAX_CLOCK_SKEW_SECONDS = 60
MAX_TTL_SECONDS = 300

NONCE_BYTES = 16
NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ATTACHMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TAILSCALE_TAG_PATTERN = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,63}$")
NODE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}\.?$")

AUTH_METHOD_WHOIS = "tailscale-whois"
AUTH_METHOD_PEERCRED = "unix-peercred"
AUTH_METHOD_LOCAL_SERVICE = "local-service"
AUTH_METHODS = frozenset(
    {AUTH_METHOD_WHOIS, AUTH_METHOD_PEERCRED, AUTH_METHOD_LOCAL_SERVICE}
)

# One lane contract for every native Oracle surface. `fleet` is a request that
# arrived over the authenticated tailnet transport; `local` originated on the
# host that holds the credential. There is no third lane and no default: a call
# that offers proof for both, or for neither, is refused.
LANE_FLEET = "fleet"
LANE_LOCAL = "local"
LANES = frozenset({LANE_FLEET, LANE_LOCAL})

# Which auth methods may prove which lane. A local-service identity can never
# stand in for a network peer, and whois can never describe a local caller.
_LANE_AUTH_METHODS: Mapping[str, frozenset[str]] = {
    LANE_FLEET: frozenset({AUTH_METHOD_WHOIS}),
    LANE_LOCAL: frozenset({AUTH_METHOD_PEERCRED, AUTH_METHOD_LOCAL_SERVICE}),
}

ORACLE_LOCAL_IDENTITY_SCHEMA = "skillbox.oracle-local-identity.v1"
LOCAL_IDENTITY_REL_PATH = ("oracle", "identity.json")
MAX_LOCAL_IDENTITY_BYTES = 4 * 1024
_LOCAL_IDENTITY_KEYS = frozenset({"schema", "caller_id"})

# Durable replay ledger. An in-process guard forgets every claim when the
# worker restarts, which hands an attacker a fresh five-minute window on every
# bounce; this ledger is the shared, restart-surviving record of which requests
# have already been spent.
ORACLE_REPLAY_LEDGER_SCHEMA = "skillbox.oracle-replay-ledger.v1"
REPLAY_LEDGER_REL_PATH = ("oracle", "replay-ledger.json")
MAX_REPLAY_LEDGER_BYTES = 4 * 1024 * 1024
MAX_REPLAY_LEDGER_RECORDS = 100_000
DEFAULT_REPLAY_LEDGER_ENTRIES = 8192
DEFAULT_REPLAY_LEDGER_ENTRIES_PER_CALLER = 256
_REPLAY_ENTRY_KEYS = frozenset({"expires_at", "digest"})

# Environment names that have historically been read as a caller identity. The
# broker never reads them: a caller id taken from the environment is a quota
# identity any child process can forge. Their PRESENCE is refused instead of
# ignored, so a host still exporting one fails loudly rather than silently
# running under a different identity than the operator believes.
IDENTITY_ENV_OVERRIDE_NAMES = frozenset(
    {
        "SKILLBOX_ORACLE_CALLER_ID",
        "SKILLBOX_ORACLE_CALLER",
        "SKILLBOX_ORACLE_IDENTITY",
        "SKILLBOX_ORACLE_LANE",
        "ORACLE_CALLER_ID",
        "ORACLE_CALLER",
        "ORACLE_IDENTITY",
        "ORACLE_LANE",
    }
)

SCOPE_LOOPBACK = "loopback"
SCOPE_TAILNET = "tailnet"

# Tailscale's assigned ranges. Anything outside these plus loopback is refused
# as a listener, which is what keeps the broker off every public interface.
TAILNET_V4_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6_NETWORK = ipaddress.ip_network("fd7a:115c:a1e0::/48")

MIN_BIND_PORT = 1024
MAX_BIND_PORT = 65535

# Exact key allowlists. Anything not named here is refused; identity, transport,
# and execution details are structurally absent from the wire format.
REQUEST_KEYS = frozenset(
    {
        "schema",
        "nonce",
        "issued_at",
        "expires_at",
        "mode",
        "prompt",
        "timeout_seconds",
        "attachments",
    }
)
ATTACHMENT_KEYS = frozenset({"name", "mime_type", "bytes", "sha256"})

# Denylist families, each with its own refusal code so an operator learns WHY a
# field is impossible rather than only that it was unknown. Names are matched
# after normalization (case folded, separators removed), so ``browserConfig``,
# ``browser_config``, and ``BROWSER-CONFIG`` all resolve to one entry. The scan
# runs at every depth: the top-level allowlist alone would report a nested
# ``cookies`` key as merely unknown.
_FORBIDDEN_FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "hooks_forbidden",
        (
            "hook",
            "hooks",
            "prehook",
            "posthook",
            "onrequest",
            "onresponse",
            "callback",
            "callbacks",
            "webhook",
            "webhooks",
        ),
    ),
    (
        "env_forbidden",
        (
            "env",
            "envs",
            "environ",
            "environment",
            "envvars",
            "environmentvariables",
            "setenv",
            "dotenv",
        ),
    ),
    (
        "cdp_target_forbidden",
        (
            "cdp",
            "cdpurl",
            "cdpendpoint",
            "cdptarget",
            "targetid",
            "sessionid",
            "devtools",
            "devtoolsurl",
            "devtoolsfrontendurl",
            "websocketdebuggerurl",
            "wsendpoint",
            "browserwsendpoint",
            "debuggeraddress",
            "remotedebuggingport",
        ),
    ),
    (
        "browser_config_forbidden",
        (
            "browser",
            "browserconfig",
            "browseroptions",
            "launchoptions",
            "launchargs",
            "chromeflags",
            "chromiumflags",
            "userdatadir",
            "profiledir",
            "profilepath",
            "profile",
            "viewport",
            "useragent",
            "proxy",
            "headless",
        ),
    ),
    (
        "credential_forbidden",
        (
            "cookie",
            "cookies",
            "cookiejar",
            "setcookie",
            "sessioncookie",
            "session",
            "sessiontoken",
            "authorization",
            "auth",
            "token",
            "tokens",
            "accesstoken",
            "refreshtoken",
            "idtoken",
            "bearer",
            "apikey",
            "apikeys",
            "credential",
            "credentials",
            "password",
            "passphrase",
            "secret",
            "secrets",
            "privatekey",
            "signature",
        ),
    ),
    (
        "executable_path_forbidden",
        (
            "exec",
            "execpath",
            "executable",
            "executablepath",
            "binary",
            "binarypath",
            "bin",
            "command",
            "commands",
            "cmd",
            "argv",
            "args",
            "arguments",
            "shell",
            "entrypoint",
            "interpreter",
            "script",
            "scriptpath",
            "path",
            "paths",
            "filepath",
            "filepaths",
            "sourcepath",
            "cwd",
            "workdir",
            "workingdirectory",
            "chdir",
            "rootdir",
        ),
    ),
    (
        "caller_identity_forbidden",
        (
            "callerid",
            "caller",
            "identity",
            "peer",
            "peerid",
            "node",
            "nodeid",
            "user",
            "username",
            "login",
            "loginname",
            "tag",
            "tags",
            "acl",
        ),
    ),
)


def _build_forbidden_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    for code, names in _FORBIDDEN_FIELD_GROUPS:
        for name in names:
            # A name in two families would make the reported code depend on
            # declaration order; that ambiguity is a defect, not a runtime case.
            if name in fields:
                raise AssertionError(f"duplicate forbidden field: {name}")
            fields[name] = code
    return fields


FORBIDDEN_FIELDS: Mapping[str, str] = _build_forbidden_fields()

# Every broker-owned refusal code. Policy denials keep their own codes.
REFUSAL_CODES = frozenset(
    {
        "attachment_digest_mismatch",
        "attachment_invalid",
        "attachment_mime_not_allowed",
        "attachment_name_invalid",
        "attachment_size_mismatch",
        "attachments_invalid",
        "bind_host_invalid",
        "bind_hostname_forbidden",
        "bind_port_forbidden",
        "clock_unavailable",
        "duplicate_field",
        "field_not_allowed",
        "field_missing",
        "identity_dir_permissions",
        "identity_env_override_forbidden",
        "identity_file_invalid",
        "identity_file_missing",
        "identity_file_permissions",
        "lane_ambiguous",
        "lane_unavailable",
        "lane_unsupported",
        "listener_unverified",
        "nonce_invalid",
        "nonce_reuse_mismatch",
        "peer_identity_invalid",
        "peer_identity_unavailable",
        "peer_not_allowlisted",
        "policy_authority_unhealthy",
        "policy_authority_unsealed",
        "policy_engine_unavailable",
        "prompt_invalid",
        "prompt_too_large",
        "public_listener_forbidden",
        "replay_capacity_exceeded",
        "replay_detected",
        "replay_guard_unavailable",
        "replay_ledger_corrupt",
        "replay_ledger_io",
        "replay_ledger_locked",
        "replay_ledger_permissions",
        "replay_ledger_unavailable",
        "request_empty",
        "request_encoding_invalid",
        "request_expired",
        "request_not_fresh",
        "request_not_json",
        "request_shape_invalid",
        "request_too_deep",
        "request_too_large",
        "request_too_wide",
        "request_window_invalid",
        "schema_unsupported",
        "wildcard_listener_forbidden",
    }
    | set(FORBIDDEN_FIELDS.values())
)


class OracleBrokerError(ValidationError):
    """Stable, non-sensitive broker refusal.

    Subclasses the runtime manager's typed :class:`ValidationError` so a CLI
    surface can render ``to_payload()`` directly. The message is a constant and
    the context is empty by construction: a refusal must never echo prompt
    text, a digest, a peer address, or any other request-derived value.
    """

    def __init__(self, code: str) -> None:
        super().__init__(str(code), "oracle broker: refused", recoverable=True)


def _refuse(code: str) -> Any:
    raise OracleBrokerError(code)


def new_nonce() -> str:
    """Mint a client-side single-use nonce for one request."""

    return secrets.token_hex(NONCE_BYTES)


# --------------------------------------------------------------------------- #
# Gate 1: listener validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BindEndpoint:
    """A listener proven to be private. Construct via validate_bind_endpoint."""

    host: str
    port: int
    scope: str

    def __post_init__(self) -> None:
        if self.scope not in (SCOPE_LOOPBACK, SCOPE_TAILNET):
            _refuse("bind_host_invalid")

    def render(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


def validate_bind_endpoint(host: Any, port: Any) -> BindEndpoint:
    """Return a private BindEndpoint, or refuse.

    Only literal loopback and Tailscale addresses pass. Hostnames are refused
    outright: what a name resolves to can change under the listener, so a name
    can never prove privacy. IPv4-mapped IPv6 forms are unwrapped first, since
    ``::ffff:0.0.0.0`` is a wildcard that does not report itself as one.
    """

    if type(host) is not str or not host or host != host.strip():
        _refuse("bind_host_invalid")
    if host.startswith("[") or host.endswith("]"):
        _refuse("bind_host_invalid")
    if "%" in host:
        # A scoped/link-local literal binds per-interface; out of contract.
        _refuse("bind_host_invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        _refuse("bind_hostname_forbidden")
    if address.version == 6:
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    if address.is_unspecified:
        _refuse("wildcard_listener_forbidden")

    # Loopback and tailnet are tested first on purpose: ``::1`` reports
    # ``is_reserved`` true, so a reserved/multicast check ahead of this would
    # refuse IPv6 loopback.
    if address.is_loopback:
        scope = SCOPE_LOOPBACK
    elif address.version == 4 and address in TAILNET_V4_NETWORK:
        scope = SCOPE_TAILNET
    elif address.version == 6 and address in TAILNET_V6_NETWORK:
        scope = SCOPE_TAILNET
    elif address.is_multicast or address.is_reserved:
        _refuse("bind_host_invalid")
    else:
        # Public, LAN, and link-local addresses all land here: a private
        # listener is loopback or tailnet, and nothing else.
        _refuse("public_listener_forbidden")

    if type(port) is not int or not MIN_BIND_PORT <= port <= MAX_BIND_PORT:
        # Privileged ports are refused because the broker must never run with
        # the capability to bind one.
        _refuse("bind_port_forbidden")

    return BindEndpoint(host=str(address), port=port, scope=scope)


# --------------------------------------------------------------------------- #
# Transport identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PeerIdentity:
    """Who the transport proved the peer to be. Never caller-asserted."""

    caller_id: str
    auth_method: str
    node: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.caller_id) is not str
            or CALLER_ID_PATTERN.fullmatch(self.caller_id) is None
        ):
            _refuse("peer_identity_invalid")
        if self.auth_method not in AUTH_METHODS:
            _refuse("peer_identity_invalid")
        if type(self.node) is not str or len(self.node) > 253:
            _refuse("peer_identity_invalid")
        if self.node and NODE_NAME_PATTERN.fullmatch(self.node) is None:
            _refuse("peer_identity_invalid")


def peer_identity_from_whois(
    document: Any,
    *,
    tag_allowlist: frozenset[str],
) -> PeerIdentity:
    """Derive a PeerIdentity from a Tailscale LocalAPI whois response.

    The caller id is the first label of the peer's MagicDNS name, and the peer
    must carry at least one allowlisted ACL tag. An untagged node, an unknown
    tag, or a malformed response is refused: there is no default identity.
    """

    if not isinstance(tag_allowlist, frozenset) or not tag_allowlist:
        _refuse("peer_not_allowlisted")
    for tag in tag_allowlist:
        if type(tag) is not str or TAILSCALE_TAG_PATTERN.fullmatch(tag) is None:
            _refuse("peer_not_allowlisted")
    if not isinstance(document, Mapping):
        _refuse("peer_identity_unavailable")
    node = document.get("Node")
    if not isinstance(node, Mapping):
        _refuse("peer_identity_unavailable")

    raw_name = node.get("Name")
    if type(raw_name) is not str or not raw_name:
        _refuse("peer_identity_invalid")
    name = unicodedata.normalize("NFC", raw_name).strip().rstrip(".").lower()
    if NODE_NAME_PATTERN.fullmatch(name) is None:
        _refuse("peer_identity_invalid")
    caller_id = name.split(".", 1)[0]
    if CALLER_ID_PATTERN.fullmatch(caller_id) is None:
        _refuse("peer_identity_invalid")

    raw_tags = node.get("Tags")
    if not isinstance(raw_tags, list) or not raw_tags or len(raw_tags) > 32:
        _refuse("peer_not_allowlisted")
    tags: set[str] = set()
    for tag in raw_tags:
        if type(tag) is not str or TAILSCALE_TAG_PATTERN.fullmatch(tag) is None:
            _refuse("peer_not_allowlisted")
        tags.add(tag)
    if not tags & tag_allowlist:
        _refuse("peer_not_allowlisted")

    return PeerIdentity(
        caller_id=caller_id,
        auth_method=AUTH_METHOD_WHOIS,
        node=name,
    )


def peer_identity_from_peercred(
    uid: Any,
    caller_id: Any,
    *,
    allowed_uids: frozenset[int],
) -> PeerIdentity:
    """Derive a PeerIdentity from a unix-socket peer credential.

    Used for the same-host loopback path, where there is no tailnet peer to ask
    whois about. The uid must be explicitly allowlisted.
    """

    if not isinstance(allowed_uids, frozenset) or not allowed_uids:
        _refuse("peer_not_allowlisted")
    for allowed in allowed_uids:
        if type(allowed) is not int or allowed < 0:
            _refuse("peer_not_allowlisted")
    if type(uid) is not int or uid < 0:
        _refuse("peer_identity_invalid")
    if uid not in allowed_uids:
        _refuse("peer_not_allowlisted")
    return PeerIdentity(
        caller_id=_validated_caller_id(caller_id),
        auth_method=AUTH_METHOD_PEERCRED,
    )


def _validated_caller_id(value: Any) -> str:
    if type(value) is not str or CALLER_ID_PATTERN.fullmatch(value) is None:
        _refuse("peer_identity_invalid")
    return value


# --------------------------------------------------------------------------- #
# Local lane identity: service-owned, never environment-derived
# --------------------------------------------------------------------------- #


def assert_no_identity_env_override(environ: Mapping[str, str] | None = None) -> None:
    """Refuse if the environment tries to assert an Oracle caller or lane.

    An identity read from the environment is a quota identity any child process
    can forge, so these names are never read as values. Their presence is a
    hard refusal: a host still exporting one fails loudly instead of quietly
    running under an identity the operator did not choose.
    """

    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        _refuse("identity_env_override_forbidden")
    for name in IDENTITY_ENV_OVERRIDE_NAMES:
        if name in source:
            _refuse("identity_env_override_forbidden")


def local_identity_path(state_root: Any) -> Path:
    """Where the service-owned local identity lives under a state root."""

    if isinstance(state_root, Path):
        root = state_root
    elif isinstance(state_root, str) and state_root:
        root = Path(state_root)
    else:
        _refuse("identity_file_missing")
    return root.joinpath(*LOCAL_IDENTITY_REL_PATH)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - POSIX-only contract
        _refuse("identity_file_permissions")
    return int(getuid())


def _verify_private_mode(metadata: os.stat_result, uid: int, code: str) -> None:
    if metadata.st_uid != uid:
        _refuse(code)
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        # Group/other access means another local account could rewrite the
        # identity, which is exactly the forgery this file exists to prevent.
        _refuse(code)


def local_service_identity(state_root: Any, *, uid: int | None = None) -> PeerIdentity:
    """Read the service-owned local caller identity, or refuse.

    The file and its parent directory must both be owned by the calling uid
    with no group or other access, and neither may be a symlink. That is the
    honest boundary: two processes running as the same uid are one caller, and
    nothing on a shared host can separate them — but no process can assert an
    identity without writing a file only that uid may write.
    """

    resolved_uid = _current_uid() if uid is None else uid
    if type(resolved_uid) is not int or resolved_uid < 0:
        _refuse("identity_file_permissions")

    path = local_identity_path(state_root)
    directory = path.parent
    try:
        directory_metadata = os.lstat(directory)
    except OSError:
        _refuse("identity_file_missing")
    if not stat.S_ISDIR(directory_metadata.st_mode):
        _refuse("identity_dir_permissions")
    _verify_private_mode(directory_metadata, resolved_uid, "identity_dir_permissions")

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _refuse("identity_file_missing")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _refuse("identity_file_permissions")
        _verify_private_mode(metadata, resolved_uid, "identity_file_permissions")
        if metadata.st_size > MAX_LOCAL_IDENTITY_BYTES:
            _refuse("identity_file_invalid")
        raw = os.read(descriptor, MAX_LOCAL_IDENTITY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_LOCAL_IDENTITY_BYTES:
        _refuse("identity_file_invalid")

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError):
        _refuse("identity_file_invalid")
    if not isinstance(document, dict) or set(document) != set(_LOCAL_IDENTITY_KEYS):
        _refuse("identity_file_invalid")
    if document["schema"] != ORACLE_LOCAL_IDENTITY_SCHEMA:
        _refuse("identity_file_invalid")
    caller_id = document["caller_id"]
    if type(caller_id) is not str or CALLER_ID_PATTERN.fullmatch(caller_id) is None:
        _refuse("identity_file_invalid")

    return PeerIdentity(caller_id=caller_id, auth_method=AUTH_METHOD_LOCAL_SERVICE)


def provision_local_identity(
    state_root: Any,
    caller_id: Any,
    *,
    uid: int | None = None,
) -> Path:
    """Write the service-owned local identity. Never call from a request path.

    Separate from the read path on purpose, exactly as
    ``provision_oracle_policy_authority`` is: enrolling an identity is an
    operator action, not something a request can trigger.
    """

    validated = _validated_caller_id(caller_id)
    path = local_identity_path(state_root)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    document = json.dumps(
        {"schema": ORACLE_LOCAL_IDENTITY_SCHEMA, "caller_id": validated},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(path, 0o600)
        os.write(descriptor, document)
    finally:
        os.close(descriptor)
    # Prove the file we just wrote satisfies the read contract rather than
    # trusting the umask and the mode bits we asked for.
    local_service_identity(state_root, uid=uid)
    return path


# --------------------------------------------------------------------------- #
# Lane resolution: one contract every native surface routes through
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LaneResolution:
    """Which lane a request runs on, and the identity that proved it."""

    lane: str
    identity: PeerIdentity
    endpoint: BindEndpoint | None
    reason: str

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            _refuse("lane_unsupported")
        if not isinstance(self.identity, PeerIdentity):
            _refuse("peer_identity_unavailable")
        if self.identity.auth_method not in _LANE_AUTH_METHODS[self.lane]:
            _refuse("lane_ambiguous")
        if self.lane == LANE_FLEET:
            # Either scope is legitimate here: the listener may bind a tailnet
            # address directly, or bind loopback behind `tailscale serve`. Gate
            # 1 has already proven both are private, which is the property that
            # matters; the receipt records which one it was.
            if not isinstance(self.endpoint, BindEndpoint):
                _refuse("listener_unverified")
        elif self.endpoint is not None:
            # A local caller has no listener; carrying one would make the
            # receipt claim a network exposure that does not exist.
            _refuse("lane_ambiguous")

    def to_payload(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "caller_id": self.identity.caller_id,
            "auth_method": self.identity.auth_method,
            "node": self.identity.node,
            "endpoint": self.endpoint.render() if self.endpoint else None,
            "scope": self.endpoint.scope if self.endpoint else None,
            "reason": self.reason,
        }


def resolve_lane(
    *,
    transport_identity: Any = None,
    endpoint: Any = None,
    local_identity: Any = None,
    state_root: Any = None,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
) -> LaneResolution:
    """Resolve the one lane a request runs on, or refuse.

    The lane is decided by which PROOF is present, never by a declaration: a
    verified transport peer means `fleet`, a peercred or service-owned identity
    means `local`. Offering proof for both is `lane_ambiguous` and offering
    neither is `lane_unavailable` — there is no default lane, because a default
    is what lets one surface quietly run somewhere the operator did not intend.

    Contacts nothing: no browser, no socket, no subprocess. A caller with no
    Chrome installed resolves exactly the same lane as one with it.
    """

    assert_no_identity_env_override(environ)

    fleet_offered = transport_identity is not None or endpoint is not None
    local_offered = local_identity is not None or state_root is not None
    if fleet_offered and local_offered:
        _refuse("lane_ambiguous")

    if fleet_offered:
        if not isinstance(transport_identity, PeerIdentity):
            _refuse("peer_identity_unavailable")
        if not isinstance(endpoint, BindEndpoint):
            _refuse("listener_unverified")
        return LaneResolution(
            lane=LANE_FLEET,
            identity=transport_identity,
            endpoint=endpoint,
            reason="authenticated_transport",
        )

    if local_offered:
        if local_identity is not None and state_root is not None:
            _refuse("lane_ambiguous")
        if local_identity is not None:
            if not isinstance(local_identity, PeerIdentity):
                _refuse("peer_identity_unavailable")
            return LaneResolution(
                lane=LANE_LOCAL,
                identity=local_identity,
                endpoint=None,
                reason="peer_credential",
            )
        return LaneResolution(
            lane=LANE_LOCAL,
            identity=local_service_identity(state_root, uid=uid),
            endpoint=None,
            reason="service_owned_identity",
        )

    _refuse("lane_unavailable")


# --------------------------------------------------------------------------- #
# Gate 2: request document
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttachmentDescriptor:
    """What a caller may say about a file: never where it lives."""

    name: str
    mime_type: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class BrokerRequest:
    """A validated request. Holds no path, no identity, and no credential."""

    nonce: str
    issued_at: int
    expires_at: int
    mode: str
    prompt: str
    timeout_seconds: int
    attachments: tuple[AttachmentDescriptor, ...]
    prompt_bytes: int
    attachment_bytes: int
    request_digest: str

    @property
    def file_count(self) -> int:
        return len(self.attachments)

    @property
    def facts(self) -> OracleRequestFacts:
        """The size/count-only view the policy engine admits on."""

        return OracleRequestFacts(
            mode=self.mode,
            prompt_bytes=self.prompt_bytes,
            file_count=self.file_count,
            attachment_bytes=self.attachment_bytes,
            timeout_seconds=self.timeout_seconds,
        )


def _normalize_key(key: str) -> str:
    folded = unicodedata.normalize("NFKC", key).casefold()
    return folded.replace("_", "").replace("-", "").replace(" ", "")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            _refuse("duplicate_field")
        document[key] = value
    return document


def _reject_constant(_name: str) -> Any:
    # NaN/Infinity are valid JavaScript literals but not valid JSON, and they
    # defeat every numeric bound below.
    _refuse("request_not_json")


def _scan_forbidden(value: Any, depth: int = 0) -> None:
    if depth > MAX_DOCUMENT_DEPTH:
        _refuse("request_too_deep")
    if isinstance(value, Mapping):
        if len(value) > MAX_DOCUMENT_KEYS:
            _refuse("request_too_wide")
        for key, item in value.items():
            if type(key) is not str:
                _refuse("request_shape_invalid")
            code = FORBIDDEN_FIELDS.get(_normalize_key(key))
            if code is not None:
                _refuse(code)
            _scan_forbidden(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_DOCUMENT_ITEMS:
            _refuse("request_too_wide")
        for item in value:
            _scan_forbidden(item, depth + 1)


def _exact_keys(value: Any, keys: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _refuse(code)
    present = set(value)
    unknown = present - keys
    if unknown:
        _refuse("field_not_allowed")
    if present != set(keys):
        _refuse("field_missing")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    # `type(...) is int` rather than isinstance: bool is an int subclass, and
    # `true` must not silently become a size of 1.
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse(code)
    return value


def _validated_prompt(value: Any) -> tuple[str, int]:
    if type(value) is not str or not value:
        _refuse("prompt_invalid")
    for character in value:
        if character in ("\t", "\n", "\r"):
            continue
        if character < " " or character == "\x7f":
            _refuse("prompt_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates survive json.loads but are not encodable text.
        _refuse("prompt_invalid")
    if not encoded:
        _refuse("prompt_invalid")
    if len(encoded) > MAX_PROMPT_BYTES:
        _refuse("prompt_too_large")
    return value, len(encoded)


def _validated_attachment(value: Any) -> AttachmentDescriptor:
    raw = _exact_keys(value, ATTACHMENT_KEYS, "attachment_invalid")

    name = raw["name"]
    if (
        type(name) is not str
        or ATTACHMENT_NAME_PATTERN.fullmatch(name) is None
        or len(name.encode("utf-8")) > MAX_ATTACHMENT_NAME_BYTES
        or ".." in name
    ):
        # The pattern already excludes separators, NUL, and control bytes, so a
        # display name can never be read as a path by anything downstream.
        _refuse("attachment_name_invalid")

    mime_type = raw["mime_type"]
    if type(mime_type) is not str or mime_type not in DEFAULT_ALLOWED_MIME_TYPES:
        _refuse("attachment_mime_not_allowed")

    size = _bounded_int(raw["bytes"], 1, MAX_ATTACHMENT_BYTES, "attachment_invalid")

    digest = raw["sha256"]
    if type(digest) is not str or SHA256_PATTERN.fullmatch(digest) is None:
        _refuse("attachment_invalid")

    return AttachmentDescriptor(
        name=name,
        mime_type=mime_type,
        bytes=size,
        sha256=digest,
    )


def _validated_attachments(value: Any) -> tuple[AttachmentDescriptor, ...]:
    if not isinstance(value, list):
        _refuse("attachments_invalid")
    if len(value) > MAX_ATTACHMENTS:
        _refuse("attachments_invalid")
    attachments = tuple(_validated_attachment(item) for item in value)
    names = {attachment.name.casefold() for attachment in attachments}
    if len(names) != len(attachments):
        # Duplicate display names make a receipt ambiguous about what ran.
        _refuse("attachments_invalid")
    total = sum(attachment.bytes for attachment in attachments)
    if total > MAX_ATTACHMENT_BYTES:
        _refuse("attachments_invalid")
    return attachments


def _canonical_digest(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def decode_request_document(payload: Any) -> dict[str, Any]:
    """Strictly decode a request document without interpreting it.

    Enforces the wire budget, UTF-8, real JSON (no NaN/Infinity), unique keys,
    bounded depth and width, and the forbidden-field scan. The scan runs here,
    before structural validation, so a nested ``cookies`` or ``browserConfig``
    key reports its own family code instead of a generic unknown-field refusal.
    """

    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="strict")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        _refuse("request_encoding_invalid")
    raw = bytes(payload)
    if not raw:
        _refuse("request_empty")
    if len(raw) > MAX_REQUEST_BYTES:
        _refuse("request_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("request_encoding_invalid")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError:
        _refuse("request_too_deep")
    except ValueError:
        _refuse("request_not_json")
    if not isinstance(document, dict):
        _refuse("request_shape_invalid")
    _scan_forbidden(document)
    return document


def parse_request(payload: Any) -> BrokerRequest:
    """Decode and validate one request document into a BrokerRequest."""

    document = decode_request_document(payload)
    raw = _exact_keys(document, REQUEST_KEYS, "request_shape_invalid")

    if raw["schema"] != ORACLE_REQUEST_SCHEMA:
        _refuse("schema_unsupported")

    nonce = raw["nonce"]
    if type(nonce) is not str or NONCE_PATTERN.fullmatch(nonce) is None:
        _refuse("nonce_invalid")

    issued_at = _bounded_int(raw["issued_at"], 0, 2**63 - 1, "request_window_invalid")
    expires_at = _bounded_int(raw["expires_at"], 0, 2**63 - 1, "request_window_invalid")

    mode = raw["mode"]
    if type(mode) is not str or mode not in SUPPORTED_MODES:
        _refuse("request_shape_invalid")

    prompt, prompt_bytes = _validated_prompt(raw["prompt"])
    timeout_seconds = _bounded_int(
        raw["timeout_seconds"], 1, MAX_TIMEOUT_SECONDS, "request_shape_invalid"
    )
    attachments = _validated_attachments(raw["attachments"])
    attachment_bytes = sum(attachment.bytes for attachment in attachments)

    request = BrokerRequest(
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
        mode=mode,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        attachments=attachments,
        prompt_bytes=prompt_bytes,
        attachment_bytes=attachment_bytes,
        request_digest=_canonical_digest(document),
    )
    # Constructing the facts here means a document the policy engine would
    # reject on shape is refused before it reaches admission.
    try:
        request.facts
    except OraclePolicyError as error:
        raise OracleBrokerError(error.code) from None
    return request


def verify_attachment_bytes(descriptor: Any, data: Any) -> None:
    """Bind delivered content to its declared size and digest, or refuse."""

    if not isinstance(descriptor, AttachmentDescriptor):
        _refuse("attachment_invalid")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _refuse("attachment_invalid")
    raw = bytes(data)
    if len(raw) != descriptor.bytes:
        _refuse("attachment_size_mismatch")
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), descriptor.sha256):
        _refuse("attachment_digest_mismatch")


# --------------------------------------------------------------------------- #
# Gate 3: freshness, replay, admission
# --------------------------------------------------------------------------- #


class ReplayDefense:
    """What admission requires of a replay store: one method, fail closed.

    Two implementations share this contract. :class:`ReplayGuard` is in-process
    and forgets everything when the worker exits; :class:`DurableReplayLedger`
    is a private on-disk ledger shared by every worker on the host. Admission
    accepts either, so a single-worker deployment pays nothing for durability
    and a fleet deployment gets it without a second code path.
    """

    __slots__ = ()

    def observe(
        self,
        caller_id: str,
        nonce: str,
        expires_at: int,
        request_digest: str,
    ) -> None:
        """Record one nonce, refusing a repeat within its freshness window."""

        raise NotImplementedError


class ReplayGuard(ReplayDefense):
    """Single-use nonces over a bounded, self-pruning window.

    Bounded memory is safe here only because freshness is enforced first: a
    request is accepted solely while ``expires_at > now``, and ``expires_at``
    can never be more than ``MAX_TTL_SECONDS`` past an ``issued_at`` that is
    itself within ``MAX_CLOCK_SKEW_SECONDS`` of now. So an entry dropped by
    pruning is provably expired, and re-presenting it fails the freshness gate
    rather than passing an empty replay check.

    When the window is genuinely full the guard refuses rather than evicting a
    live nonce -- forgetting a live nonce would open the replay it exists to
    close.
    """

    __slots__ = ("_clock", "_entries", "_max_entries", "_max_per_caller")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_entries: int = 8192,
        max_entries_per_caller: int = 256,
    ) -> None:
        if type(max_entries) is not int or max_entries < 1:
            _refuse("replay_guard_unavailable")
        if type(max_entries_per_caller) is not int or max_entries_per_caller < 1:
            _refuse("replay_guard_unavailable")
        if not callable(clock):
            _refuse("replay_guard_unavailable")
        self._clock = clock
        self._max_entries = max_entries
        self._max_per_caller = max_entries_per_caller
        self._entries: dict[tuple[str, str], tuple[int, str]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _prune(self, now: int) -> None:
        expired = [
            key for key, (expires_at, _) in self._entries.items() if expires_at <= now
        ]
        for key in expired:
            del self._entries[key]

    def observe(
        self,
        caller_id: str,
        nonce: str,
        expires_at: int,
        request_digest: str,
    ) -> None:
        """Record one nonce, refusing a repeat within its freshness window."""

        caller_id = _validated_caller_id(caller_id)
        if type(nonce) is not str or NONCE_PATTERN.fullmatch(nonce) is None:
            _refuse("nonce_invalid")
        if type(request_digest) is not str or (
            SHA256_PATTERN.fullmatch(request_digest) is None
        ):
            _refuse("request_shape_invalid")
        expires_at = _bounded_int(expires_at, 0, 2**63 - 1, "request_window_invalid")

        now = _clock_seconds(self._clock)
        self._prune(now)
        key = (caller_id, nonce)
        seen = self._entries.get(key)
        if seen is not None:
            if not hmac.compare_digest(seen[1], request_digest):
                # Same nonce, different body: a splice attempt, not a retry.
                _refuse("nonce_reuse_mismatch")
            _refuse("replay_detected")
        if len(self._entries) >= self._max_entries:
            _refuse("replay_capacity_exceeded")
        caller_entries = sum(
            1 for seen_caller, _ in self._entries if seen_caller == caller_id
        )
        if caller_entries >= self._max_per_caller:
            _refuse("replay_capacity_exceeded")
        self._entries[key] = (expires_at, request_digest)


def replay_ledger_path(state_root: Any) -> Path:
    """Where the durable replay ledger lives under a state root."""

    if isinstance(state_root, Path):
        root = state_root
    elif isinstance(state_root, str) and state_root:
        root = Path(state_root)
    else:
        _refuse("replay_ledger_unavailable")
    return root.joinpath(*REPLAY_LEDGER_REL_PATH)


def _no_duplicate_ledger_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            # Our own writer never emits a duplicate key, so one on disk means
            # the file was edited or corrupted.
            _refuse("replay_ledger_corrupt")
        document[key] = value
    return document


def _write_all_bytes(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        try:
            written += os.write(descriptor, payload[written:])
        except OSError:
            _refuse("replay_ledger_io")


class DurableReplayLedger(ReplayDefense):
    """Single-use nonces recorded on disk, shared by every worker on the host.

    The in-process guard resets its whole window when a worker restarts, so a
    captured request became replayable again on every bounce — and two workers
    never saw each other's claims at all. This ledger closes both holes with one
    private file under the state root, serialized by an ``flock`` on a sibling
    lock file.

    Every observation runs expiry, capacity, and insertion inside ONE critical
    section, so concurrent workers cannot both win the same nonce and cannot
    read a half-written ledger: the file is replaced atomically and the
    directory is fsynced, so a claim that was accepted is on disk before the
    caller is told it may proceed.

    Fail-closed choices worth naming:

    * an unparseable or structurally wrong ledger is ``replay_ledger_corrupt``,
      never "treat as empty" — treating corruption as an empty ledger would
      admit every replay it was meant to stop;
    * a full window refuses instead of evicting a live nonce, exactly as the
      in-process guard does;
    * the ledger and its directory must be uid-owned with no group/other
      access, and neither may be a symlink.

    A missing ledger IS a fresh ledger. The honest boundary is the same one the
    local identity file has: deleting the ledger requires the uid that owns it,
    and a process running as that uid could forge claims directly.
    """

    __slots__ = (
        "_clock",
        "_lock_path",
        "_lock_timeout_seconds",
        "_max_entries",
        "_max_per_caller",
        "_path",
        "_uid",
    )

    def __init__(
        self,
        state_root: Any,
        *,
        clock: Callable[[], float] = time.time,
        max_entries: int = DEFAULT_REPLAY_LEDGER_ENTRIES,
        max_entries_per_caller: int | None = None,
        lock_timeout_seconds: float = 2.0,
        uid: int | None = None,
    ) -> None:
        if (
            type(max_entries) is not int
            or not 1 <= max_entries <= MAX_REPLAY_LEDGER_RECORDS
        ):
            _refuse("replay_ledger_unavailable")
        if max_entries_per_caller is None:
            # A default must never make construction fail: a small total
            # capacity simply caps the per-caller share too. An EXPLICIT
            # per-caller value larger than the total is still a contradiction.
            max_entries_per_caller = min(
                DEFAULT_REPLAY_LEDGER_ENTRIES_PER_CALLER, max_entries
            )
        if (
            type(max_entries_per_caller) is not int
            or not 1 <= max_entries_per_caller <= max_entries
        ):
            _refuse("replay_ledger_unavailable")
        if not callable(clock):
            _refuse("replay_ledger_unavailable")
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or lock_timeout_seconds != lock_timeout_seconds
            or not 0 < lock_timeout_seconds <= 30
        ):
            _refuse("replay_ledger_unavailable")
        resolved_uid = _current_uid() if uid is None else uid
        if type(resolved_uid) is not int or resolved_uid < 0:
            _refuse("replay_ledger_unavailable")

        self._clock = clock
        self._max_entries = max_entries
        self._max_per_caller = max_entries_per_caller
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        self._uid = resolved_uid
        self._path = replay_ledger_path(state_root)
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        """Live (unexpired) claims. Reads under the lock; never writes."""

        now = _clock_seconds(self._clock)
        with self._locked():
            state = self._read_state()
        return sum(
            1
            for nonces in state.values()
            for expires_at, _ in nonces.values()
            if expires_at > now
        )

    # -- storage ---------------------------------------------------------- #

    def _ensure_directory(self) -> None:
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            _refuse("replay_ledger_io")
        try:
            metadata = os.lstat(directory)
        except OSError:
            _refuse("replay_ledger_permissions")
        if not stat.S_ISDIR(metadata.st_mode):
            _refuse("replay_ledger_permissions")
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            # A group-writable directory lets another local account swap the
            # ledger for an empty one, which is a replay window.
            try:
                os.chmod(directory, 0o700)
            except OSError:
                _refuse("replay_ledger_permissions")
            metadata = os.lstat(directory)
        _verify_private_mode(metadata, self._uid, "replay_ledger_permissions")

    def _acquire(self, descriptor: int) -> None:
        # Deliberately the real monotonic clock, not the injected one: a test
        # clock that never advances must not turn a contended lock into a hang.
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _refuse("replay_ledger_locked")
                time.sleep(0.01)
            except (OSError, ValueError):
                _refuse("replay_ledger_io")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_directory()
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError:
            _refuse("replay_ledger_permissions")
        try:
            try:
                os.fchmod(descriptor, 0o600)
                metadata = os.fstat(descriptor)
            except OSError:
                _refuse("replay_ledger_permissions")
            if not stat.S_ISREG(metadata.st_mode):
                _refuse("replay_ledger_permissions")
            _verify_private_mode(metadata, self._uid, "replay_ledger_permissions")
            self._acquire(descriptor)
            try:
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (OSError, ValueError):
                    _refuse("replay_ledger_io")
        finally:
            os.close(descriptor)

    def _read_state(self) -> dict[str, dict[str, tuple[int, str]]]:
        try:
            descriptor = os.open(
                self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            # A missing ledger is a fresh ledger, not a corrupt one.
            return {}
        except OSError:
            _refuse("replay_ledger_permissions")
        try:
            try:
                metadata = os.fstat(descriptor)
            except OSError:
                _refuse("replay_ledger_permissions")
            if not stat.S_ISREG(metadata.st_mode):
                _refuse("replay_ledger_permissions")
            _verify_private_mode(metadata, self._uid, "replay_ledger_permissions")
            if metadata.st_size > MAX_REPLAY_LEDGER_BYTES:
                _refuse("replay_ledger_corrupt")
            try:
                raw = os.read(descriptor, MAX_REPLAY_LEDGER_BYTES + 1)
            except OSError:
                _refuse("replay_ledger_io")
        finally:
            os.close(descriptor)
        if not raw:
            return {}
        if len(raw) > MAX_REPLAY_LEDGER_BYTES:
            _refuse("replay_ledger_corrupt")
        return self._decode_state(raw)

    def _decode_state(self, raw: bytes) -> dict[str, dict[str, tuple[int, str]]]:
        try:
            document = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_no_duplicate_ledger_keys
            )
        except (UnicodeDecodeError, ValueError):
            _refuse("replay_ledger_corrupt")
        if not isinstance(document, dict) or set(document) != {"schema", "callers"}:
            _refuse("replay_ledger_corrupt")
        if document["schema"] != ORACLE_REPLAY_LEDGER_SCHEMA:
            _refuse("replay_ledger_corrupt")
        raw_callers = document["callers"]
        if not isinstance(raw_callers, dict):
            _refuse("replay_ledger_corrupt")

        state: dict[str, dict[str, tuple[int, str]]] = {}
        records = 0
        for caller_id, raw_nonces in raw_callers.items():
            if (
                type(caller_id) is not str
                or CALLER_ID_PATTERN.fullmatch(caller_id) is None
            ):
                _refuse("replay_ledger_corrupt")
            if not isinstance(raw_nonces, dict):
                _refuse("replay_ledger_corrupt")
            nonces: dict[str, tuple[int, str]] = {}
            for nonce, entry in raw_nonces.items():
                if type(nonce) is not str or NONCE_PATTERN.fullmatch(nonce) is None:
                    _refuse("replay_ledger_corrupt")
                if not isinstance(entry, dict) or set(entry) != set(
                    _REPLAY_ENTRY_KEYS
                ):
                    _refuse("replay_ledger_corrupt")
                expires_at = entry["expires_at"]
                digest = entry["digest"]
                if type(expires_at) is not int or not 0 <= expires_at <= 2**63 - 1:
                    _refuse("replay_ledger_corrupt")
                if (
                    type(digest) is not str
                    or SHA256_PATTERN.fullmatch(digest) is None
                ):
                    _refuse("replay_ledger_corrupt")
                nonces[nonce] = (expires_at, digest)
                records += 1
                if records > MAX_REPLAY_LEDGER_RECORDS:
                    _refuse("replay_ledger_corrupt")
            state[caller_id] = nonces
        return state

    def _write_state(self, state: Mapping[str, Mapping[str, tuple[int, str]]]) -> None:
        document = {
            "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
            "callers": {
                caller_id: {
                    nonce: {"expires_at": expires_at, "digest": digest}
                    for nonce, (expires_at, digest) in sorted(nonces.items())
                }
                for caller_id, nonces in sorted(state.items())
                if nonces
            },
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if len(encoded) > MAX_REPLAY_LEDGER_BYTES:
            _refuse("replay_capacity_exceeded")

        directory = self._path.parent
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=directory, prefix=".replay-ledger-", suffix=".tmp"
            )
        except OSError:
            _refuse("replay_ledger_io")
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                _refuse("replay_ledger_io")
            _write_all_bytes(descriptor, encoded)
            try:
                os.fsync(descriptor)
            except OSError:
                _refuse("replay_ledger_io")
            os.close(descriptor)
            descriptor = -1
            try:
                os.replace(temporary, self._path)
            except OSError:
                _refuse("replay_ledger_io")
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        # fsync the directory so the rename itself survives a crash: a claim we
        # already told the caller was accepted must not vanish on restart.
        try:
            directory_descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            _refuse("replay_ledger_io")
        try:
            with contextlib.suppress(OSError):
                # Not every filesystem allows fsync on a directory handle;
                # os.replace is already atomic, so this is durability, not
                # correctness.
                os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    # -- contract --------------------------------------------------------- #

    def observe(
        self,
        caller_id: str,
        nonce: str,
        expires_at: int,
        request_digest: str,
    ) -> None:
        """Claim one nonce durably, refusing a repeat any worker already spent."""

        caller_id = _validated_caller_id(caller_id)
        if type(nonce) is not str or NONCE_PATTERN.fullmatch(nonce) is None:
            _refuse("nonce_invalid")
        if type(request_digest) is not str or (
            SHA256_PATTERN.fullmatch(request_digest) is None
        ):
            _refuse("request_shape_invalid")
        expires_at = _bounded_int(expires_at, 0, 2**63 - 1, "request_window_invalid")

        now = _clock_seconds(self._clock)
        with self._locked():
            state = self._read_state()
            # Expiry, capacity, and insertion all happen inside this one lock,
            # so two workers can never both observe the same nonce and never
            # race the pruning that frees the capacity they are checking.
            live: dict[str, dict[str, tuple[int, str]]] = {}
            total = 0
            for seen_caller, nonces in state.items():
                kept = {
                    seen_nonce: entry
                    for seen_nonce, entry in nonces.items()
                    if entry[0] > now
                }
                if kept:
                    live[seen_caller] = kept
                    total += len(kept)

            bucket = live.setdefault(caller_id, {})
            seen = bucket.get(nonce)
            if seen is not None:
                if not hmac.compare_digest(seen[1], request_digest):
                    _refuse("nonce_reuse_mismatch")
                _refuse("replay_detected")
            if total >= self._max_entries:
                _refuse("replay_capacity_exceeded")
            if len(bucket) >= self._max_per_caller:
                _refuse("replay_capacity_exceeded")

            bucket[nonce] = (expires_at, request_digest)
            self._write_state(live)


def _clock_seconds(clock: Callable[[], float]) -> int:
    try:
        value = clock()
    except Exception:
        _refuse("clock_unavailable")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("clock_unavailable")
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        _refuse("clock_unavailable")
    return int(value)


def check_freshness(request: BrokerRequest, now: int) -> None:
    """Refuse a request that is stale, expired, or carries an unbounded window."""

    if not isinstance(request, BrokerRequest):
        _refuse("request_shape_invalid")
    if abs(request.issued_at - now) > MAX_CLOCK_SKEW_SECONDS:
        _refuse("request_not_fresh")
    if request.expires_at <= request.issued_at:
        _refuse("request_window_invalid")
    if request.expires_at - request.issued_at > MAX_TTL_SECONDS:
        _refuse("request_window_invalid")
    if request.expires_at <= now:
        _refuse("request_expired")


@dataclass(frozen=True)
class BrokerReceipt:
    """Non-sensitive proof of one admission. Carries no prompt and no content.

    One receipt shape covers both lanes, so a fleet request and a local request
    are audited identically; ``lane`` and ``auth_method`` carry the provenance,
    and ``endpoint``/``scope`` are empty on the local lane because there is no
    listener to name.
    """

    lane: str
    caller_id: str
    auth_method: str
    node: str
    endpoint: str
    scope: str
    mode: str
    reservation_id: str
    request_digest: str
    prompt_bytes: int
    file_count: int
    attachment_bytes: int
    timeout_seconds: int
    admitted_at: int
    expires_at: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": ORACLE_RECEIPT_SCHEMA,
            "protocol": ORACLE_BROKER_PROTOCOL,
            "lane": self.lane,
            "caller_id": self.caller_id,
            "auth_method": self.auth_method,
            "node": self.node,
            "endpoint": self.endpoint,
            "scope": self.scope,
            "mode": self.mode,
            "reservation_id": self.reservation_id,
            "request_digest": self.request_digest,
            "prompt_bytes": self.prompt_bytes,
            "file_count": self.file_count,
            "attachment_bytes": self.attachment_bytes,
            "timeout_seconds": self.timeout_seconds,
            "admitted_at": self.admitted_at,
            "expires_at": self.expires_at,
        }


def build_receipt(
    request: BrokerRequest,
    resolution: LaneResolution,
    grant: Any,
) -> BrokerReceipt:
    """Render the one receipt shape shared by both lanes."""

    if not isinstance(resolution, LaneResolution):
        _refuse("lane_unavailable")
    identity = resolution.identity
    endpoint = resolution.endpoint
    return BrokerReceipt(
        lane=resolution.lane,
        caller_id=identity.caller_id,
        auth_method=identity.auth_method,
        node=identity.node,
        endpoint=endpoint.render() if endpoint else "",
        scope=endpoint.scope if endpoint else "",
        mode=request.mode,
        reservation_id=str(getattr(grant, "reservation_id", "")),
        request_digest=request.request_digest,
        prompt_bytes=request.prompt_bytes,
        file_count=request.file_count,
        attachment_bytes=request.attachment_bytes,
        timeout_seconds=request.timeout_seconds,
        admitted_at=int(getattr(grant, "admitted_at", 0)),
        expires_at=int(getattr(grant, "expires_at", 0)),
    )


ORACLE_AUTHORITY_HEALTH_SCHEMA = "skillbox.oracle-authority-health.v1"

AUTHORITY_KIND_PRODUCTION = "production"
AUTHORITY_KIND_FIXTURE = "fixture"
AUTHORITY_KINDS = frozenset({AUTHORITY_KIND_PRODUCTION, AUTHORITY_KIND_FIXTURE})

_AUTHORITY_BINDING_DOMAIN = b"skillbox.oracle-policy-authority.binding.v1"
_AUTHORITY_FIXTURE_DOMAIN = b"skillbox.oracle-policy-authority.fixture.v1"

# Module-private construction token. Nothing outside this module can obtain it
# except by calling one of the two sealing factories, so a hand-rolled object
# cannot present itself as an authority. A caller who can read this name can
# already edit the module, which is the same trust boundary
# ``oracle_policy`` documents under "Honest local authority boundary" -- the
# seal defends against a soft authorizer reaching admission by accident or by
# library injection, not against the owner of the source tree.
_AUTHORITY_SEAL = object()

_FIXTURE_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PolicyAuthority:
    """A quota authority that had to prove what it is before it could exist.

    The hole this closes: admission used to accept any object with a callable
    ``admission`` attribute. A library caller could inject a stand-in that
    yields a syntactically valid grant -- a reservation id, an admitted_at, an
    expires_at -- and every downstream check, including the receipt, would look
    correct while no per-caller quota, no enrolled authority journal, and no
    replay-bound reservation had ever been consulted. A soft decision that is
    shaped like a real one is worse than an outright failure, because nothing
    downstream can tell them apart.

    So there is no interface to implement. There are two sealed factories:
    :func:`production_policy_authority`, which will only wrap a genuine
    :class:`~runtime_manager.oracle_policy.OraclePolicyEngine`, and
    :func:`sealed_fixture_authority`, which exists so tests keep working and
    which **can never report healthy**. Subclassing is refused, direct
    construction without the seal is refused, and admission checks the exact
    type rather than an ``isinstance``, so an object built with
    ``object.__new__`` carries no seal and is rejected too.

    ``fingerprint`` is what makes the authority *identity-bound*: for a
    production authority it is derived from the enrollment the engine proved at
    construction (its policy fingerprint plus its canonical state and authority
    directories), so two engines pointed at different enrollments are two
    different authorities and neither can borrow the other's provenance.
    """

    __slots__ = ("_admission", "_fingerprint", "_kind", "_seal")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A subclass could override admission() and inherit the seal, which is
        # exactly the escape hatch this class exists to remove.
        _refuse("policy_authority_unsealed")

    def __init__(
        self,
        seal: Any,
        *,
        kind: str,
        fingerprint: str,
        admission: Any,
    ) -> None:
        if seal is not _AUTHORITY_SEAL:
            _refuse("policy_authority_unsealed")
        if kind not in AUTHORITY_KINDS:
            _refuse("policy_authority_unsealed")
        if type(fingerprint) is not str or SHA256_PATTERN.fullmatch(fingerprint) is None:
            _refuse("policy_authority_unsealed")
        if not callable(admission):
            _refuse("policy_authority_unsealed")
        object.__setattr__(self, "_seal", seal)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_fingerprint", fingerprint)
        object.__setattr__(self, "_admission", admission)

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def healthy(self) -> bool:
        """Only a production authority is ever healthy.

        A fixture is structurally incapable of returning True here, which is
        the whole point: a health surface wired to this cannot be made green by
        injecting a stand-in.
        """

        return self._kind == AUTHORITY_KIND_PRODUCTION

    def health(self) -> dict[str, Any]:
        """Non-sensitive health report. Carries no path and no policy body."""

        return {
            "schema": ORACLE_AUTHORITY_HEALTH_SCHEMA,
            "kind": self._kind,
            "healthy": self.healthy,
            "authority_fingerprint": self._fingerprint,
            "reasons": [] if self.healthy else ["fixture_authority"],
        }

    def admission(self, caller_id: str, facts: Any) -> Any:
        """Delegate to the sealed implementation."""

        return self._admission(caller_id, facts)


def _sealed_authority(kind: str, fingerprint: str, admission: Any) -> PolicyAuthority:
    return PolicyAuthority(
        _AUTHORITY_SEAL, kind=kind, fingerprint=fingerprint, admission=admission
    )


def _canonical_directory(value: Any) -> str:
    path = Path(os.fspath(value))
    if not path.is_absolute():
        _refuse("policy_authority_unsealed")
    return str(path)


def production_policy_authority(engine: Any) -> PolicyAuthority:
    """Seal a genuine policy engine as the production authority.

    The type check is exact, not ``isinstance``. A subclass of
    ``OraclePolicyEngine`` that overrides ``admission`` would otherwise pass an
    isinstance check while granting whatever it likes -- the same soft-decision
    problem one inheritance hop away.

    Nothing here re-verifies the enrollment, because constructing an
    ``OraclePolicyEngine`` already does: a missing, corrupt, or rolled-back
    authority makes construction raise. This binds to what that construction
    proved rather than re-implementing its checks from the outside.
    """

    if type(engine) is not OraclePolicyEngine:
        _refuse("policy_authority_unsealed")
    fingerprint = getattr(engine, "policy_fingerprint", None)
    if (
        type(fingerprint) is not str
        or SHA256_PATTERN.fullmatch(fingerprint) is None
    ):
        _refuse("policy_authority_unsealed")
    state_directory = _canonical_directory(engine.state_directory)
    authority_directory = _canonical_directory(engine.authority_directory)
    if state_directory == authority_directory:
        # oracle_policy enforces this at enrollment; re-asserting it here keeps
        # the binding meaningful rather than trusting an attribute.
        _refuse("policy_authority_unsealed")
    digest = hashlib.sha256()
    for part in (
        _AUTHORITY_BINDING_DOMAIN,
        fingerprint.encode("ascii"),
        state_directory.encode("utf-8"),
        authority_directory.encode("utf-8"),
    ):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return _sealed_authority(
        AUTHORITY_KIND_PRODUCTION, digest.hexdigest(), engine.admission
    )


def sealed_fixture_authority(admission: Any, *, label: str = "default") -> PolicyAuthority:
    """Seal a test double as an explicitly non-production authority.

    Testability is preserved deliberately and visibly: a fixture is a real
    ``PolicyAuthority``, so it flows through admission unchanged, but
    :attr:`PolicyAuthority.healthy` is False for it by construction and
    :func:`oracle_lane_admission` refuses it unless the caller opts out of the
    health requirement in so many words. A test can never become production
    wiring by accident, and production wiring cannot be softened by importing a
    fixture.
    """

    if not callable(admission):
        _refuse("policy_authority_unsealed")
    if type(label) is not str or _FIXTURE_LABEL_PATTERN.fullmatch(label) is None:
        _refuse("policy_authority_unsealed")
    digest = hashlib.sha256()
    digest.update(_AUTHORITY_FIXTURE_DOMAIN)
    digest.update(label.encode("ascii"))
    return _sealed_authority(AUTHORITY_KIND_FIXTURE, digest.hexdigest(), admission)


def require_policy_authority(value: Any) -> PolicyAuthority:
    """Resolve a caller-supplied engine to a sealed authority, or refuse.

    A genuine ``OraclePolicyEngine`` is auto-sealed so every existing
    production call site keeps working unchanged. Everything else -- a
    duck-typed stand-in, a subclass, an unsealed instance -- is refused.
    """

    # getattr with a default, not attribute access: an instance built with
    # ``object.__new__`` never ran __init__ and has no _seal at all, and that
    # has to be a typed refusal rather than an AttributeError escaping to the
    # caller as an untyped crash.
    if (
        type(value) is PolicyAuthority
        and getattr(value, "_seal", None) is _AUTHORITY_SEAL
    ):
        return value
    if type(value) is OraclePolicyEngine:
        return production_policy_authority(value)
    if value is not None and callable(getattr(value, "admission", None)):
        # Names the real problem: this object looks like an authority and is
        # not one. A generic "unavailable" would read as a wiring mistake.
        _refuse("policy_authority_unsealed")
    _refuse("policy_engine_unavailable")


@dataclass(frozen=True)
class Admission:
    """What an admitted request yields to the browser-facing caller."""

    request: BrokerRequest
    grant: Any
    receipt: BrokerReceipt
    authority: Any = None


@contextlib.contextmanager
def oracle_lane_admission(
    payload: Any,
    *,
    resolution: Any,
    policy_engine: Any,
    replay_guard: Any,
    clock: Callable[[], float] = time.time,
    require_healthy_authority: bool = True,
) -> Iterator[Admission]:
    """Admit one request on a resolved lane, or refuse before any browser call.

    This is the single admission path every native Oracle surface routes
    through — CLI, MCP, and in-process alike. Both lanes get the same document
    allowlist, the same freshness and replay defense, the same per-caller quota
    reservation, and the same receipt: the local lane does not skip a gate just
    because it never crossed a network.

    Every gate raises before the ``yield``, so a server that contacts the Oracle
    only inside the ``with`` body cannot make unauthenticated, unadmitted, or
    replayed contact. The quota reservation is released on exit, including when
    the body raises.
    """

    if not isinstance(resolution, LaneResolution):
        # A caller that skipped resolve_lane has proven no lane, so there is
        # nothing to admit against.
        _refuse("lane_unavailable")
    if not isinstance(replay_guard, ReplayDefense):
        _refuse("replay_guard_unavailable")
    authority = require_policy_authority(policy_engine)
    if require_healthy_authority and not authority.healthy:
        # Default-closed: a sealed fixture is usable only when the caller says
        # so explicitly, so production wiring cannot be softened by an import.
        _refuse("policy_authority_unhealthy")

    identity = resolution.identity
    now = _clock_seconds(clock)
    request = parse_request(payload)
    check_freshness(request, now)
    replay_guard.observe(
        identity.caller_id,
        request.nonce,
        request.expires_at,
        request.request_digest,
    )

    with contextlib.ExitStack() as stack:
        try:
            grant = stack.enter_context(
                authority.admission(identity.caller_id, request.facts)
            )
        except OraclePolicyError as error:
            raise OracleBrokerError(error.code) from None
        yield Admission(
            request=request,
            grant=grant,
            receipt=build_receipt(request, resolution, grant),
            authority=authority,
        )


@contextlib.contextmanager
def broker_admission(
    payload: Any,
    identity: Any,
    *,
    endpoint: Any,
    policy_engine: Any,
    replay_guard: Any,
    clock: Callable[[], float] = time.time,
    require_healthy_authority: bool = True,
) -> Iterator[Admission]:
    """Fleet-lane façade over :func:`oracle_lane_admission`.

    Kept because the tailnet transport holds an already-verified peer and
    endpoint rather than a resolution; it resolves the fleet lane and then runs
    the one shared admission path, so fleet and local cannot drift apart.
    """

    if not isinstance(endpoint, BindEndpoint):
        # A raw host/port tuple has not been through validate_bind_endpoint, so
        # it carries no proof the listener is private. Checked here, ahead of
        # resolve_lane, so the refusal names the listener rather than the lane.
        _refuse("listener_unverified")
    if not isinstance(identity, PeerIdentity):
        _refuse("peer_identity_unavailable")
    resolution = resolve_lane(transport_identity=identity, endpoint=endpoint)
    with oracle_lane_admission(
        payload,
        resolution=resolution,
        policy_engine=policy_engine,
        replay_guard=replay_guard,
        clock=clock,
        require_healthy_authority=require_healthy_authority,
    ) as admission:
        yield admission


__all__ = [
    "ATTACHMENT_KEYS",
    "AUTHORITY_KINDS",
    "AUTHORITY_KIND_FIXTURE",
    "AUTHORITY_KIND_PRODUCTION",
    "AUTH_METHODS",
    "AUTH_METHOD_LOCAL_SERVICE",
    "AUTH_METHOD_PEERCRED",
    "AUTH_METHOD_WHOIS",
    "Admission",
    "AttachmentDescriptor",
    "BindEndpoint",
    "BrokerReceipt",
    "BrokerRequest",
    "DEFAULT_REPLAY_LEDGER_ENTRIES",
    "DEFAULT_REPLAY_LEDGER_ENTRIES_PER_CALLER",
    "DurableReplayLedger",
    "FORBIDDEN_FIELDS",
    "IDENTITY_ENV_OVERRIDE_NAMES",
    "LANES",
    "LANE_FLEET",
    "LANE_LOCAL",
    "LaneResolution",
    "ORACLE_AUTHORITY_HEALTH_SCHEMA",
    "ORACLE_LOCAL_IDENTITY_SCHEMA",
    "ORACLE_REPLAY_LEDGER_SCHEMA",
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_PROMPT_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "MAX_TTL_SECONDS",
    "ORACLE_BROKER_PROTOCOL",
    "ORACLE_RECEIPT_SCHEMA",
    "ORACLE_REQUEST_SCHEMA",
    "OracleBrokerError",
    "PeerIdentity",
    "PolicyAuthority",
    "REFUSAL_CODES",
    "REQUEST_KEYS",
    "ReplayDefense",
    "ReplayGuard",
    "SCOPE_LOOPBACK",
    "SCOPE_TAILNET",
    "TAILNET_V4_NETWORK",
    "TAILNET_V6_NETWORK",
    "assert_no_identity_env_override",
    "broker_admission",
    "build_receipt",
    "check_freshness",
    "decode_request_document",
    "local_identity_path",
    "local_service_identity",
    "new_nonce",
    "oracle_lane_admission",
    "parse_request",
    "peer_identity_from_peercred",
    "peer_identity_from_whois",
    "production_policy_authority",
    "provision_local_identity",
    "replay_ledger_path",
    "require_policy_authority",
    "resolve_lane",
    "sealed_fixture_authority",
    "validate_bind_endpoint",
    "verify_attachment_bytes",
]
