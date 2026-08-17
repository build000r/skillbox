"""Contract tests for the restricted trusted-host fleet Oracle RPC broker.

The three gates under test are listener privacy, the allowlisted request
document, and admission (freshness + replay + quota). The suite treats "the
browser was never contacted" as a first-class assertion: a sentinel callable
stands in for every browser-facing action, and each refusal path asserts it was
never invoked.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.oracle_broker import (
    ATTACHMENT_KEYS,
    AUTHORITY_KIND_FIXTURE,
    AUTHORITY_KIND_PRODUCTION,
    AUTHORITY_KINDS,
    AUTH_METHOD_PEERCRED,
    AUTH_METHOD_WHOIS,
    DEFAULT_REPLAY_LEDGER_ENTRIES,
    FORBIDDEN_FIELDS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_CLOCK_SKEW_SECONDS,
    MAX_PROMPT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_TIMEOUT_SECONDS,
    MAX_TTL_SECONDS,
    ORACLE_AUTHORITY_HEALTH_SCHEMA,
    ORACLE_BROKER_PROTOCOL,
    ORACLE_RECEIPT_SCHEMA,
    ORACLE_REPLAY_LEDGER_SCHEMA,
    ORACLE_REQUEST_SCHEMA,
    REFUSAL_CODES,
    REQUEST_KEYS,
    AttachmentDescriptor,
    BindEndpoint,
    DurableReplayLedger,
    OracleBrokerError,
    PeerIdentity,
    PolicyAuthority,
    ReplayDefense,
    ReplayGuard,
    broker_admission,
    replay_ledger_path,
    check_freshness,
    decode_request_document,
    new_nonce,
    parse_request,
    peer_identity_from_peercred,
    peer_identity_from_whois,
    production_policy_authority,
    require_policy_authority,
    sealed_fixture_authority,
    validate_bind_endpoint,
    verify_attachment_bytes,
)
from runtime_manager.oracle_policy import (
    OraclePolicy,
    OraclePolicyEngine,
    OraclePolicyError,
    provision_oracle_policy_authority,
)

BROKER_SOURCE = (
    Path(__file__).resolve().parent.parent
    / ".env-manager"
    / "runtime_manager"
    / "oracle_broker.py"
)

CALLER = "devbox-1"
TAG_ALLOWLIST = frozenset({"tag:oracle-client"})


class MutableClock:
    def __init__(self, now: float = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def caller_policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "modes": ["standard", "deep-research"],
        "max_prompt_bytes": 4_000,
        "max_files": 4,
        "max_attachment_bytes": 8_000,
        "max_request_bytes": 12_000,
        "max_concurrent": 2,
        "max_requests_per_window": 8,
        "max_bytes_per_window": 60_000,
        "window_seconds": 60,
        "max_runtime_seconds": 600,
        "lease_grace_seconds": 10,
    }
    value.update(overrides)
    return value


def policy_document(**overrides: object) -> dict[str, object]:
    return {
        "schema": "skillbox.oracle-policy.v1",
        "callers": {CALLER: caller_policy(**overrides)},
    }


def request_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": ORACLE_REQUEST_SCHEMA,
        "nonce": "0" * 32,
        "issued_at": 1_000_000,
        "expires_at": 1_000_060,
        "mode": "standard",
        "prompt": "Summarize the tailnet posture contract.",
        "timeout_seconds": 300,
        "attachments": [],
    }
    document.update(overrides)
    return document


def attachment(data: bytes = b"hello", name: str = "notes.txt") -> dict[str, object]:
    return {
        "name": name,
        "mime_type": "text/plain",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def encode(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


class BrokerTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> OracleBrokerError:
        with self.assertRaises(OracleBrokerError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        # A refusal must never carry request-derived detail.
        self.assertEqual("oracle broker: refused", str(caught.exception))
        self.assertEqual({}, caught.exception.context)
        return caught.exception


class BindEndpointTests(BrokerTestCase):
    """Gate 1: only a literal loopback or tailnet listener is describable."""

    def test_loopback_and_tailnet_are_accepted(self) -> None:
        cases = {
            "127.0.0.1": "loopback",
            "::1": "loopback",
            "100.64.0.1": "tailnet",
            "100.100.1.3": "tailnet",
            "fd7a:115c:a1e0::1": "tailnet",
        }
        for host, scope in cases.items():
            endpoint = validate_bind_endpoint(host, 8443)
            self.assertEqual(scope, endpoint.scope, host)
            self.assertEqual(8443, endpoint.port)

    def test_ipv6_loopback_is_not_refused_as_reserved(self) -> None:
        # ipaddress reports ::1 as reserved; the gate must still admit it.
        self.assertEqual("loopback", validate_bind_endpoint("::1", 8443).scope)

    def test_wildcard_binds_are_refused(self) -> None:
        for host in ("0.0.0.0", "::", "0000:0000:0000:0000:0000:0000:0000:0000"):
            self.assert_refused(
                "wildcard_listener_forbidden",
                lambda host=host: validate_bind_endpoint(host, 8443),
            )

    def test_ipv4_mapped_wildcard_is_refused(self) -> None:
        # ::ffff:0.0.0.0 binds every interface but does not report itself
        # unspecified until the mapped form is unwrapped.
        self.assert_refused(
            "wildcard_listener_forbidden",
            lambda: validate_bind_endpoint("::ffff:0.0.0.0", 8443),
        )

    def test_ipv4_mapped_loopback_is_accepted_as_loopback(self) -> None:
        endpoint = validate_bind_endpoint("::ffff:127.0.0.1", 8443)
        self.assertEqual("loopback", endpoint.scope)
        self.assertEqual("127.0.0.1", endpoint.host)

    def test_hostnames_are_refused(self) -> None:
        for host in ("localhost", "devbox-1.tailnet.ts.net", "oracle.internal"):
            self.assert_refused(
                "bind_hostname_forbidden",
                lambda host=host: validate_bind_endpoint(host, 8443),
            )

    def test_public_and_lan_addresses_are_refused(self) -> None:
        for host in ("8.8.8.8", "192.168.1.5", "10.0.0.7", "169.254.1.1", "2001:db8::1"):
            self.assert_refused(
                "public_listener_forbidden",
                lambda host=host: validate_bind_endpoint(host, 8443),
            )

    def test_malformed_hosts_are_refused(self) -> None:
        for host in ("", " 127.0.0.1", "[::1]", "fe80::1%eth0"):
            self.assert_refused(
                "bind_host_invalid",
                lambda host=host: validate_bind_endpoint(host, 8443),
            )

    def test_privileged_and_invalid_ports_are_refused(self) -> None:
        for port in (0, 80, 443, 1023, 65536, -1, "8443", True, 8443.0):
            self.assert_refused(
                "bind_port_forbidden",
                lambda port=port: validate_bind_endpoint("127.0.0.1", port),
            )

    def test_endpoint_renders_ipv6_with_brackets(self) -> None:
        self.assertEqual("[::1]:8443", validate_bind_endpoint("::1", 8443).render())
        self.assertEqual(
            "127.0.0.1:8443", validate_bind_endpoint("127.0.0.1", 8443).render()
        )


class PeerIdentityTests(BrokerTestCase):
    """Identity is proven by the transport; it is never caller-asserted."""

    def whois(self, **overrides: object) -> dict[str, object]:
        node: dict[str, object] = {
            "Name": "devbox-1.tailnet-abc.ts.net.",
            "Tags": ["tag:oracle-client"],
        }
        node.update(overrides)
        return {"Node": node, "UserProfile": {"LoginName": "tagged-devices"}}

    def test_whois_derives_caller_id_from_the_first_label(self) -> None:
        identity = peer_identity_from_whois(self.whois(), tag_allowlist=TAG_ALLOWLIST)
        self.assertEqual(CALLER, identity.caller_id)
        self.assertEqual(AUTH_METHOD_WHOIS, identity.auth_method)
        self.assertEqual("devbox-1.tailnet-abc.ts.net", identity.node)

    def test_untagged_peer_is_refused(self) -> None:
        for tags in ([], None, "tag:oracle-client", ["oracle-client"]):
            self.assert_refused(
                "peer_not_allowlisted",
                lambda tags=tags: peer_identity_from_whois(
                    self.whois(Tags=tags), tag_allowlist=TAG_ALLOWLIST
                ),
            )

    def test_unallowlisted_tag_is_refused(self) -> None:
        self.assert_refused(
            "peer_not_allowlisted",
            lambda: peer_identity_from_whois(
                self.whois(Tags=["tag:laptop"]), tag_allowlist=TAG_ALLOWLIST
            ),
        )

    def test_empty_allowlist_refuses_every_peer(self) -> None:
        self.assert_refused(
            "peer_not_allowlisted",
            lambda: peer_identity_from_whois(self.whois(), tag_allowlist=frozenset()),
        )

    def test_missing_or_malformed_whois_is_refused(self) -> None:
        self.assert_refused(
            "peer_identity_unavailable",
            lambda: peer_identity_from_whois(None, tag_allowlist=TAG_ALLOWLIST),
        )
        self.assert_refused(
            "peer_identity_unavailable",
            lambda: peer_identity_from_whois({}, tag_allowlist=TAG_ALLOWLIST),
        )
        self.assert_refused(
            "peer_identity_invalid",
            lambda: peer_identity_from_whois(
                self.whois(Name="../../etc/passwd"), tag_allowlist=TAG_ALLOWLIST
            ),
        )

    def test_peercred_requires_an_allowlisted_uid(self) -> None:
        identity = peer_identity_from_peercred(
            501, CALLER, allowed_uids=frozenset({501})
        )
        self.assertEqual(AUTH_METHOD_PEERCRED, identity.auth_method)
        self.assertEqual("", identity.node)
        self.assert_refused(
            "peer_not_allowlisted",
            lambda: peer_identity_from_peercred(
                502, CALLER, allowed_uids=frozenset({501})
            ),
        )
        self.assert_refused(
            "peer_not_allowlisted",
            lambda: peer_identity_from_peercred(501, CALLER, allowed_uids=frozenset()),
        )

    def test_identity_fields_are_validated(self) -> None:
        for caller_id in ("Devbox-1", "", "a" * 65, "-bad"):
            self.assert_refused(
                "peer_identity_invalid",
                lambda caller_id=caller_id: PeerIdentity(
                    caller_id=caller_id, auth_method=AUTH_METHOD_WHOIS
                ),
            )
        self.assert_refused(
            "peer_identity_invalid",
            lambda: PeerIdentity(caller_id=CALLER, auth_method="trust-me"),
        )


class RequestAllowlistTests(BrokerTestCase):
    """Gate 2: the wire format has no room for anything dangerous."""

    def test_minimal_request_parses(self) -> None:
        request = parse_request(encode(request_document()))
        self.assertEqual("standard", request.mode)
        self.assertEqual(0, request.file_count)
        self.assertEqual(0, request.attachment_bytes)
        self.assertEqual(39, request.prompt_bytes)
        self.assertRegex(request.request_digest, r"^[0-9a-f]{64}$")

    def test_request_with_attachments_parses_and_derives_facts(self) -> None:
        payload = b"hello"
        request = parse_request(
            encode(request_document(attachments=[attachment(payload)]))
        )
        self.assertEqual(1, request.file_count)
        self.assertEqual(len(payload), request.attachment_bytes)
        facts = request.facts
        self.assertEqual(1, facts.file_count)
        self.assertEqual(request.prompt_bytes, facts.prompt_bytes)

    def test_unknown_and_missing_fields_are_refused(self) -> None:
        self.assert_refused(
            "field_not_allowed",
            lambda: parse_request(encode(request_document(extra="x"))),
        )
        trimmed = request_document()
        del trimmed["mode"]
        self.assert_refused("field_missing", lambda: parse_request(encode(trimmed)))

    def test_every_forbidden_family_has_its_own_code(self) -> None:
        cases = {
            "hooks": "hooks_forbidden",
            "env": "env_forbidden",
            "cdp_url": "cdp_target_forbidden",
            "browserConfig": "browser_config_forbidden",
            "cookies": "credential_forbidden",
            "executable_path": "executable_path_forbidden",
            "caller_id": "caller_identity_forbidden",
        }
        for field, code in cases.items():
            self.assert_refused(
                code,
                lambda field=field: parse_request(
                    encode(request_document(**{field: "x"}))
                ),
            )

    def test_forbidden_fields_are_refused_at_every_depth(self) -> None:
        # A nested key would otherwise report only as an unknown attachment
        # field, hiding which control it tripped.
        nested = attachment()
        nested["browser_ws_endpoint"] = "ws://127.0.0.1:9222/devtools/browser/x"
        self.assert_refused(
            "cdp_target_forbidden",
            lambda: parse_request(encode(request_document(attachments=[nested]))),
        )

    def test_forbidden_field_matching_ignores_case_and_separators(self) -> None:
        for spelling in ("browserConfig", "browser_config", "BROWSER-CONFIG", "Browser Config"):
            self.assert_refused(
                "browser_config_forbidden",
                lambda spelling=spelling: parse_request(
                    encode(request_document(**{spelling: {}}))
                ),
            )

    def test_identity_cannot_be_asserted_by_the_caller(self) -> None:
        for field in ("caller_id", "node", "user", "tags"):
            self.assert_refused(
                "caller_identity_forbidden",
                lambda field=field: parse_request(
                    encode(request_document(**{field: CALLER}))
                ),
            )

    def test_duplicate_keys_are_refused(self) -> None:
        raw = b'{"schema": "a", "schema": "b"}'
        self.assert_refused("duplicate_field", lambda: decode_request_document(raw))

    def test_json_constants_and_malformed_payloads_are_refused(self) -> None:
        self.assert_refused(
            "request_not_json",
            lambda: decode_request_document(b'{"prompt": NaN}'),
        )
        self.assert_refused(
            "request_not_json", lambda: decode_request_document(b"{oops")
        )
        self.assert_refused(
            "request_shape_invalid", lambda: decode_request_document(b"[1, 2]")
        )
        self.assert_refused("request_empty", lambda: decode_request_document(b""))
        self.assert_refused(
            "request_encoding_invalid", lambda: decode_request_document(b"\xff\xfe")
        )
        self.assert_refused(
            "request_encoding_invalid", lambda: decode_request_document(42)
        )

    def test_oversize_documents_are_refused_before_parsing(self) -> None:
        self.assert_refused(
            "request_too_large",
            lambda: decode_request_document(b"x" * (MAX_REQUEST_BYTES + 1)),
        )

    def test_deep_and_wide_documents_are_refused(self) -> None:
        deep: object = "leaf"
        for _ in range(12):
            deep = {"nest": deep}
        self.assert_refused(
            "request_too_deep", lambda: decode_request_document(encode(deep))
        )
        wide = {f"field{index}": index for index in range(64)}
        self.assert_refused(
            "request_too_wide", lambda: decode_request_document(encode(wide))
        )

    def test_schema_nonce_and_mode_are_pinned(self) -> None:
        self.assert_refused(
            "schema_unsupported",
            lambda: parse_request(encode(request_document(schema="other.v1"))),
        )
        for nonce in ("", "z" * 32, "0" * 31, 0, "0" * 64):
            self.assert_refused(
                "nonce_invalid",
                lambda nonce=nonce: parse_request(
                    encode(request_document(nonce=nonce))
                ),
            )
        self.assert_refused(
            "request_shape_invalid",
            lambda: parse_request(encode(request_document(mode="raw-cdp"))),
        )

    def test_booleans_are_not_accepted_as_integers(self) -> None:
        self.assert_refused(
            "request_shape_invalid",
            lambda: parse_request(encode(request_document(timeout_seconds=True))),
        )

    def test_timeout_bounds_are_enforced(self) -> None:
        for timeout in (0, -1, MAX_TIMEOUT_SECONDS + 1):
            self.assert_refused(
                "request_shape_invalid",
                lambda timeout=timeout: parse_request(
                    encode(request_document(timeout_seconds=timeout))
                ),
            )

    def test_prompt_must_be_bounded_printable_text(self) -> None:
        for prompt in ("", "before\x00after", "bell\x07", 12):
            self.assert_refused(
                "prompt_invalid",
                lambda prompt=prompt: parse_request(
                    encode(request_document(prompt=prompt))
                ),
            )
        self.assert_refused(
            "prompt_too_large",
            lambda: parse_request(
                encode(request_document(prompt="x" * (MAX_PROMPT_BYTES + 1)))
            ),
        )

    def test_prompt_keeps_ordinary_whitespace(self) -> None:
        request = parse_request(encode(request_document(prompt="a\n\tb\r")))
        self.assertEqual("a\n\tb\r", request.prompt)

    def test_attachment_names_can_never_be_paths(self) -> None:
        for name in (
            "../escape.txt",
            "/etc/passwd",
            "dir/notes.txt",
            "..",
            ".hidden.txt",
            "notes\x00.txt",
            "",
            "a" * 200,
        ):
            self.assert_refused(
                "attachment_name_invalid",
                lambda name=name: parse_request(
                    encode(
                        request_document(attachments=[attachment(name=name)])
                    )
                ),
            )

    def test_attachment_mime_must_be_allowlisted(self) -> None:
        entry = attachment()
        entry["mime_type"] = "application/x-msdownload"
        self.assert_refused(
            "attachment_mime_not_allowed",
            lambda: parse_request(encode(request_document(attachments=[entry]))),
        )

    def test_attachment_digest_and_size_are_validated(self) -> None:
        bad_digest = attachment()
        bad_digest["sha256"] = "nope"
        self.assert_refused(
            "attachment_invalid",
            lambda: parse_request(encode(request_document(attachments=[bad_digest]))),
        )
        empty = attachment()
        empty["bytes"] = 0
        self.assert_refused(
            "attachment_invalid",
            lambda: parse_request(encode(request_document(attachments=[empty]))),
        )

    def test_duplicate_and_excess_attachments_are_refused(self) -> None:
        duplicated = [attachment(b"a"), attachment(b"b")]
        self.assert_refused(
            "attachments_invalid",
            lambda: parse_request(encode(request_document(attachments=duplicated))),
        )
        many = [
            attachment(bytes([index]), name=f"file{index}.txt")
            for index in range(MAX_ATTACHMENTS + 1)
        ]
        self.assert_refused(
            "attachments_invalid",
            lambda: parse_request(encode(request_document(attachments=many))),
        )
        self.assert_refused(
            "attachments_invalid",
            lambda: parse_request(encode(request_document(attachments={}))),
        )

    def test_attachment_bytes_are_bound_to_the_declared_digest(self) -> None:
        request = parse_request(
            encode(request_document(attachments=[attachment(b"hello")]))
        )
        descriptor = request.attachments[0]
        verify_attachment_bytes(descriptor, b"hello")
        self.assert_refused(
            "attachment_size_mismatch",
            lambda: verify_attachment_bytes(descriptor, b"hell"),
        )
        swapped = AttachmentDescriptor(
            name=descriptor.name,
            mime_type=descriptor.mime_type,
            bytes=descriptor.bytes,
            sha256="0" * 64,
        )
        self.assert_refused(
            "attachment_digest_mismatch",
            lambda: verify_attachment_bytes(swapped, b"hello"),
        )


class FreshnessAndReplayTests(BrokerTestCase):
    """Gate 3a: a request is single-use inside a bounded, provable window."""

    def request(self, **overrides: object):
        return parse_request(encode(request_document(**overrides)))

    def test_fresh_request_passes(self) -> None:
        check_freshness(self.request(), 1_000_000)

    def test_stale_and_future_requests_are_refused(self) -> None:
        skew = MAX_CLOCK_SKEW_SECONDS + 1
        self.assert_refused(
            "request_not_fresh",
            lambda: check_freshness(self.request(), 1_000_000 + skew),
        )
        self.assert_refused(
            "request_not_fresh",
            lambda: check_freshness(self.request(), 1_000_000 - skew),
        )

    def test_expired_request_is_refused(self) -> None:
        self.assert_refused(
            "request_expired",
            lambda: check_freshness(self.request(), 1_000_060),
        )

    def test_unbounded_or_inverted_windows_are_refused(self) -> None:
        self.assert_refused(
            "request_window_invalid",
            lambda: check_freshness(
                self.request(expires_at=1_000_000 + MAX_TTL_SECONDS + 1), 1_000_000
            ),
        )
        self.assert_refused(
            "request_window_invalid",
            lambda: check_freshness(self.request(expires_at=999_999), 1_000_000),
        )

    def test_nonce_is_single_use(self) -> None:
        clock = MutableClock()
        guard = ReplayGuard(clock=clock)
        request = self.request()
        guard.observe(CALLER, request.nonce, request.expires_at, request.request_digest)
        self.assert_refused(
            "replay_detected",
            lambda: guard.observe(
                CALLER, request.nonce, request.expires_at, request.request_digest
            ),
        )

    def test_nonce_spliced_onto_a_different_body_is_refused(self) -> None:
        clock = MutableClock()
        guard = ReplayGuard(clock=clock)
        first = self.request()
        second = self.request(prompt="a different question entirely")
        guard.observe(CALLER, first.nonce, first.expires_at, first.request_digest)
        self.assertNotEqual(first.request_digest, second.request_digest)
        self.assert_refused(
            "nonce_reuse_mismatch",
            lambda: guard.observe(
                CALLER, second.nonce, second.expires_at, second.request_digest
            ),
        )

    def test_nonce_is_scoped_to_the_authenticated_caller(self) -> None:
        clock = MutableClock()
        guard = ReplayGuard(clock=clock)
        request = self.request()
        guard.observe(CALLER, request.nonce, request.expires_at, request.request_digest)
        guard.observe(
            "devbox-2", request.nonce, request.expires_at, request.request_digest
        )
        self.assertEqual(2, len(guard))

    def test_pruned_nonces_are_provably_expired(self) -> None:
        # Bounded replay memory is only safe if a forgotten nonce can no longer
        # be presented. Advance past expiry: the guard forgets it, and the
        # freshness gate refuses the replay instead.
        clock = MutableClock()
        guard = ReplayGuard(clock=clock)
        # A short TTL so expiry lands inside the clock-skew window; that is the
        # case where only the replay memory could have caught the repeat.
        request = self.request(expires_at=1_000_030)
        guard.observe(CALLER, request.nonce, request.expires_at, request.request_digest)
        clock.now = float(request.expires_at + 1)
        guard.observe("devbox-2", "1" * 32, request.expires_at + 300, "a" * 64)
        self.assertEqual(1, len(guard))
        self.assert_refused(
            "request_expired", lambda: check_freshness(request, int(clock.now))
        )

    def test_full_window_refuses_rather_than_evicting_a_live_nonce(self) -> None:
        clock = MutableClock()
        guard = ReplayGuard(clock=clock, max_entries=2)
        guard.observe(CALLER, "1" * 32, 1_000_060, "a" * 64)
        guard.observe(CALLER, "2" * 32, 1_000_060, "b" * 64)
        self.assert_refused(
            "replay_capacity_exceeded",
            lambda: guard.observe(CALLER, "3" * 32, 1_000_060, "c" * 64),
        )

    def test_per_caller_capacity_is_bounded(self) -> None:
        clock = MutableClock()
        guard = ReplayGuard(clock=clock, max_entries=32, max_entries_per_caller=1)
        guard.observe(CALLER, "1" * 32, 1_000_060, "a" * 64)
        self.assert_refused(
            "replay_capacity_exceeded",
            lambda: guard.observe(CALLER, "2" * 32, 1_000_060, "b" * 64),
        )
        # A different caller is unaffected by one caller filling its share.
        guard.observe("devbox-2", "3" * 32, 1_000_060, "c" * 64)

    def test_guard_rejects_malformed_inputs(self) -> None:
        guard = ReplayGuard(clock=MutableClock())
        self.assert_refused(
            "nonce_invalid", lambda: guard.observe(CALLER, "short", 1_000_060, "a" * 64)
        )
        self.assert_refused(
            "peer_identity_invalid",
            lambda: guard.observe("BAD", "1" * 32, 1_000_060, "a" * 64),
        )

    def test_new_nonce_is_wire_shaped_and_unique(self) -> None:
        nonces = {new_nonce() for _ in range(64)}
        self.assertEqual(64, len(nonces))
        for nonce in nonces:
            self.assertRegex(nonce, r"^[0-9a-f]{32}$")


class AdmissionTests(BrokerTestCase):
    """Gate 3b: nothing browser-facing runs until every gate has passed."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.state = root / "oracle-policy"
        self.authority = root / "oracle-authority"
        self.clock = MutableClock()
        self.policy = OraclePolicy.from_mapping(policy_document())
        provision_oracle_policy_authority(
            self.policy, self.state, authority_directory=self.authority
        )
        self.engine = OraclePolicyEngine(
            self.policy,
            self.state,
            authority_directory=self.authority,
            clock=self.clock,
        )
        self.endpoint = validate_bind_endpoint("100.64.0.1", 8443)
        self.identity = peer_identity_from_whois(
            {
                "Node": {
                    "Name": f"{CALLER}.tailnet-abc.ts.net.",
                    "Tags": ["tag:oracle-client"],
                }
            },
            tag_allowlist=TAG_ALLOWLIST,
        )
        self.guard = ReplayGuard(clock=self.clock)
        self.contacted: list[str] = []

    def admit(self, payload: object, **overrides: object):
        options: dict[str, object] = {
            "endpoint": self.endpoint,
            "policy_engine": self.engine,
            "replay_guard": self.guard,
            "clock": self.clock,
        }
        options.update(overrides)
        identity = options.pop("identity", self.identity)
        return broker_admission(payload, identity, **options)  # type: ignore[arg-type]

    def run_oracle(self, payload: object, **overrides: object):
        with self.admit(payload, **overrides) as admission:
            self.contacted.append(admission.request.request_digest)
            return admission

    def test_valid_request_is_admitted_and_reserves_quota(self) -> None:
        admission = self.run_oracle(encode(request_document()))
        self.assertEqual(1, len(self.contacted))
        self.assertEqual(CALLER, admission.grant.caller_id)
        self.assertEqual("standard", admission.grant.mode)
        self.assertRegex(admission.grant.reservation_id, r"^[0-9a-f]{32}$")

    def test_reservation_is_released_after_the_block(self) -> None:
        for index in range(3):
            document = request_document(nonce=f"{index:032x}")
            self.run_oracle(encode(document))
        # max_concurrent is 2; three sequential admissions prove release.
        self.assertEqual(3, len(self.contacted))

    def test_reservation_is_released_when_the_body_raises(self) -> None:
        class Boom(RuntimeError):
            pass

        with self.assertRaises(Boom):
            with self.admit(encode(request_document())):
                raise Boom()
        # The lease is gone, so a fresh request still fits under max_concurrent.
        self.run_oracle(encode(request_document(nonce="1" * 32)))
        self.assertEqual(1, len(self.contacted))

    def test_refused_requests_never_reach_the_browser(self) -> None:
        cases = (
            ("field_not_allowed", encode(request_document(extra="x"))),
            ("cookie_case", encode(request_document(cookies={"a": "b"}))),
            ("schema_unsupported", encode(request_document(schema="other"))),
            ("request_not_json", b"{oops"),
            ("prompt_invalid", encode(request_document(prompt=""))),
        )
        for _label, payload in cases:
            with self.assertRaises(OracleBrokerError):
                self.run_oracle(payload)
        self.assertEqual([], self.contacted)

    def test_unverified_listener_is_refused(self) -> None:
        for endpoint in (("100.64.0.1", 8443), "100.64.0.1:8443", None):
            self.assert_refused(
                "listener_unverified",
                lambda endpoint=endpoint: self.run_oracle(
                    encode(request_document()), endpoint=endpoint
                ),
            )
        self.assertEqual([], self.contacted)

    def test_unauthenticated_identity_is_refused(self) -> None:
        for identity in (None, CALLER, {"caller_id": CALLER}):
            self.assert_refused(
                "peer_identity_unavailable",
                lambda identity=identity: self.run_oracle(
                    encode(request_document()), identity=identity
                ),
            )
        self.assertEqual([], self.contacted)

    def test_missing_replay_guard_or_policy_engine_is_refused(self) -> None:
        self.assert_refused(
            "replay_guard_unavailable",
            lambda: self.run_oracle(encode(request_document()), replay_guard=None),
        )
        self.assert_refused(
            "policy_engine_unavailable",
            lambda: self.run_oracle(encode(request_document()), policy_engine=None),
        )
        self.assertEqual([], self.contacted)

    def test_replayed_request_is_refused_after_a_successful_run(self) -> None:
        payload = encode(request_document())
        self.run_oracle(payload)
        self.assert_refused("replay_detected", lambda: self.run_oracle(payload))
        self.assertEqual(1, len(self.contacted))

    def test_expired_request_is_refused_before_quota_is_spent(self) -> None:
        # Expiry inside the skew window, so this is the expiry gate and not the
        # freshness gate that refuses.
        self.clock.now = 1_000_031
        self.assert_refused(
            "request_expired",
            lambda: self.run_oracle(encode(request_document(expires_at=1_000_030))),
        )
        self.assertEqual(0, len(self.guard))
        self.assertEqual([], self.contacted)

    def test_stale_request_is_refused_before_quota_is_spent(self) -> None:
        self.clock.now = 1_000_000 + MAX_CLOCK_SKEW_SECONDS + 1
        self.assert_refused(
            "request_not_fresh", lambda: self.run_oracle(encode(request_document()))
        )
        self.assertEqual(0, len(self.guard))
        self.assertEqual([], self.contacted)

    def test_policy_denials_surface_with_their_own_code(self) -> None:
        unknown_caller = PeerIdentity(
            caller_id="stranger", auth_method=AUTH_METHOD_WHOIS
        )
        self.assert_refused(
            "caller_denied",
            lambda: self.run_oracle(
                encode(request_document()), identity=unknown_caller
            ),
        )
        self.assertEqual([], self.contacted)

    def test_caller_quota_is_enforced_by_the_policy_engine(self) -> None:
        # Under the broker's own ceiling of 32 attachments, over this caller's
        # policy limit of 4: proof the broker defers to per-caller policy.
        many = [
            attachment(bytes([index]), name=f"file{index}.txt") for index in range(5)
        ]
        self.assert_refused(
            "file_count_exceeded",
            lambda: self.run_oracle(encode(request_document(attachments=many))),
        )
        self.assertEqual([], self.contacted)

    def test_receipt_carries_whois_identity_and_no_request_content(self) -> None:
        secret_prompt = "do not put me in a receipt"
        admission = self.run_oracle(encode(request_document(prompt=secret_prompt)))
        payload = admission.receipt.to_payload()
        self.assertEqual(ORACLE_RECEIPT_SCHEMA, payload["schema"])
        self.assertEqual(ORACLE_BROKER_PROTOCOL, payload["protocol"])
        self.assertEqual(CALLER, payload["caller_id"])
        self.assertEqual(AUTH_METHOD_WHOIS, payload["auth_method"])
        self.assertEqual(f"{CALLER}.tailnet-abc.ts.net", payload["node"])
        self.assertEqual("100.64.0.1:8443", payload["endpoint"])
        self.assertEqual("tailnet", payload["scope"])
        self.assertEqual(admission.grant.reservation_id, payload["reservation_id"])
        rendered = json.dumps(payload)
        self.assertNotIn(secret_prompt, rendered)
        self.assertNotIn("do not put me", rendered)

    def test_receipt_records_sizes_not_content(self) -> None:
        admission = self.run_oracle(
            encode(request_document(attachments=[attachment(b"hello")]))
        )
        payload = admission.receipt.to_payload()
        self.assertEqual(1, payload["file_count"])
        self.assertEqual(5, payload["attachment_bytes"])
        self.assertNotIn("prompt", payload)
        self.assertNotIn("attachments", payload)

    # -- injected-authority admission ------------------------------------
    #
    # These live in AdmissionTests on purpose: DurableAdmissionTests
    # subclasses it, so every case below is re-run against the durable
    # replay ledger as well as the in-process guard.

    def test_a_soft_authorizer_never_reaches_the_browser(self) -> None:
        soft = SoftAuthorizer()
        self.assert_refused(
            "policy_authority_unsealed",
            lambda: self.run_oracle(encode(request_document()), policy_engine=soft),
        )
        self.assertEqual([], self.contacted)
        self.assertEqual(0, soft.calls)

    def test_the_real_engine_is_auto_sealed_so_production_wiring_is_unchanged(
        self,
    ) -> None:
        admission = self.run_oracle(encode(request_document()))
        self.assertEqual(admission.authority.kind, AUTHORITY_KIND_PRODUCTION)
        self.assertTrue(admission.authority.healthy)
        self.assertEqual(1, len(self.contacted))

    def test_an_explicitly_sealed_engine_is_accepted_unchanged(self) -> None:
        authority = production_policy_authority(self.engine)
        admission = self.run_oracle(
            encode(request_document()), policy_engine=authority
        )
        self.assertIs(admission.authority, authority)

    def test_a_fixture_authority_is_refused_by_default(self) -> None:
        """Default-closed: production wiring cannot be softened by an import."""
        fixture = sealed_fixture_authority(SoftAuthorizer().admission, label="unit")
        self.assert_refused(
            "policy_authority_unhealthy",
            lambda: self.run_oracle(
                encode(request_document()), policy_engine=fixture
            ),
        )
        self.assertEqual([], self.contacted)

    def test_a_fixture_authority_works_when_the_caller_opts_out_in_writing(
        self,
    ) -> None:
        soft = SoftAuthorizer()
        fixture = sealed_fixture_authority(soft.admission, label="unit")
        admission = self.run_oracle(
            encode(request_document()),
            policy_engine=fixture,
            require_healthy_authority=False,
        )
        self.assertEqual(admission.authority.kind, AUTHORITY_KIND_FIXTURE)
        self.assertFalse(admission.authority.healthy)
        self.assertEqual(1, soft.calls)
        self.assertEqual(1, len(self.contacted))

    def test_opting_out_still_cannot_admit_an_unsealed_authorizer(self) -> None:
        """The opt-out relaxes health, never the seal."""
        self.assert_refused(
            "policy_authority_unsealed",
            lambda: self.run_oracle(
                encode(request_document()),
                policy_engine=SoftAuthorizer(),
                require_healthy_authority=False,
            ),
        )
        self.assertEqual([], self.contacted)

    def test_the_earlier_gates_still_run_before_the_authority_check(self) -> None:
        """A pretender must not mask a listener or replay-store failure."""
        self.assert_refused(
            "listener_unverified",
            lambda: self.run_oracle(
                encode(request_document()),
                endpoint=("100.64.0.1", 8443),
                policy_engine=SoftAuthorizer(),
            ),
        )
        self.assert_refused(
            "replay_guard_unavailable",
            lambda: self.run_oracle(
                encode(request_document()),
                replay_guard=None,
                policy_engine=SoftAuthorizer(),
            ),
        )
        self.assertEqual([], self.contacted)

    def test_a_refused_authority_spends_no_nonce(self) -> None:
        """The replay window must not be burned by a rejected authorizer."""
        payload = encode(request_document())
        self.assert_refused(
            "policy_authority_unsealed",
            lambda: self.run_oracle(payload, policy_engine=SoftAuthorizer()),
        )
        admission = self.run_oracle(payload)
        self.assertEqual(1, len(self.contacted))
        self.assertRegex(admission.grant.reservation_id, r"^[0-9a-f]{32}$")


LEDGER_WORKER = """
import sys
sys.path.insert(0, {env_manager!r})
from runtime_manager.oracle_broker import DurableReplayLedger, OracleBrokerError

ledger = DurableReplayLedger(
    sys.argv[1], clock=lambda: float(sys.argv[5]), lock_timeout_seconds=10
)
try:
    ledger.observe(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[6])
except OracleBrokerError as error:
    print(error.code)
else:
    print("recorded")
"""


class LedgerTestCase(BrokerTestCase):
    """Shared fixture: a private state root plus a controllable clock."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_root = Path(temporary.name).resolve() / "state"
        self.clock = MutableClock()

    def ledger(self, **overrides: object) -> DurableReplayLedger:
        options: dict[str, object] = {"clock": self.clock}
        options.update(overrides)
        return DurableReplayLedger(self.state_root, **options)  # type: ignore[arg-type]

    def worker(self, *argv: str, timeout: int = 60) -> str:
        """Observe from a SEPARATE process — a real worker boundary."""
        script = LEDGER_WORKER.format(env_manager=str(ENV_MANAGER_DIR))
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.state_root), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()


class DurableReplayLedgerTests(LedgerTestCase):
    """A spent request stays spent — across restarts and across workers."""

    def test_first_claim_is_recorded_privately(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        self.assertEqual(1, len(ledger))
        path = replay_ledger_path(self.state_root)
        self.assertEqual(path, ledger.path)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path.parent).st_mode))
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(ORACLE_REPLAY_LEDGER_SCHEMA, document["schema"])

    def test_ledger_carries_no_request_content(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        raw = replay_ledger_path(self.state_root).read_text(encoding="utf-8")
        for secret in ("prompt", "Summarize", "attachment"):
            self.assertNotIn(secret, raw)

    def test_replay_is_refused_by_the_same_instance(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        self.assert_refused(
            "replay_detected",
            lambda: ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64),
        )

    def test_replay_survives_a_broker_restart(self) -> None:
        # The whole point: a captured request must stay rejected after the
        # process that first saw it is gone.
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        restarted = self.ledger()
        self.assert_refused(
            "replay_detected",
            lambda: restarted.observe(CALLER, "0" * 32, 1_000_060, "a" * 64),
        )

    def test_spliced_nonce_is_refused_across_a_restart(self) -> None:
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        restarted = self.ledger()
        self.assert_refused(
            "nonce_reuse_mismatch",
            lambda: restarted.observe(CALLER, "0" * 32, 1_000_060, "b" * 64),
        )

    def test_nonces_are_scoped_to_the_authenticated_caller(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        ledger.observe("devbox-2", "0" * 32, 1_000_060, "a" * 64)
        self.assertEqual(2, len(ledger))

    def test_expired_claims_are_pruned_and_are_provably_expired(self) -> None:
        # Same argument as the in-process guard: bounded storage is safe only
        # because a forgotten claim can no longer pass the freshness gate.
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_030, "a" * 64)
        self.clock.now = 1_000_031.0
        self.assertEqual(0, len(ledger))
        ledger.observe("devbox-2", "1" * 32, 1_000_090, "b" * 64)
        document = json.loads(
            replay_ledger_path(self.state_root).read_text(encoding="utf-8")
        )
        self.assertNotIn(CALLER, document["callers"])
        request = parse_request(encode(request_document(expires_at=1_000_030)))
        self.assert_refused(
            "request_expired", lambda: check_freshness(request, int(self.clock.now))
        )

    def test_a_full_ledger_refuses_rather_than_evicting_a_live_claim(self) -> None:
        ledger = self.ledger(max_entries=2)
        ledger.observe(CALLER, "1" * 32, 1_000_060, "a" * 64)
        ledger.observe(CALLER, "2" * 32, 1_000_060, "b" * 64)
        self.assert_refused(
            "replay_capacity_exceeded",
            lambda: ledger.observe(CALLER, "3" * 32, 1_000_060, "c" * 64),
        )
        # And the earlier claims are still claims, not casualties of the refusal.
        self.assert_refused(
            "replay_detected",
            lambda: self.ledger().observe(CALLER, "1" * 32, 1_000_060, "a" * 64),
        )

    def test_per_caller_capacity_is_bounded_and_durable(self) -> None:
        ledger = self.ledger(max_entries=8, max_entries_per_caller=1)
        ledger.observe(CALLER, "1" * 32, 1_000_060, "a" * 64)
        self.assert_refused(
            "replay_capacity_exceeded",
            lambda: self.ledger(
                max_entries=8, max_entries_per_caller=1
            ).observe(CALLER, "2" * 32, 1_000_060, "b" * 64),
        )
        # One caller filling its share does not spend another caller's.
        ledger.observe("devbox-2", "3" * 32, 1_000_060, "c" * 64)

    def test_construction_arguments_are_validated(self) -> None:
        for kwargs in (
            {"max_entries": 0},
            {"max_entries": "8"},
            {"max_entries": True},
            {"max_entries_per_caller": 0},
            {"max_entries_per_caller": 9, "max_entries": 8},
            {"lock_timeout_seconds": 0},
            {"lock_timeout_seconds": 31},
            {"lock_timeout_seconds": float("nan")},
            {"clock": "now"},
        ):
            self.assert_refused(
                "replay_ledger_unavailable", lambda kwargs=kwargs: self.ledger(**kwargs)
            )
        self.assert_refused(
            "replay_ledger_unavailable", lambda: DurableReplayLedger(None)
        )

    def test_observe_arguments_are_validated(self) -> None:
        ledger = self.ledger()
        self.assert_refused(
            "peer_identity_invalid",
            lambda: ledger.observe("NOT VALID", "0" * 32, 1_000_060, "a" * 64),
        )
        self.assert_refused(
            "nonce_invalid",
            lambda: ledger.observe(CALLER, "short", 1_000_060, "a" * 64),
        )
        self.assert_refused(
            "request_shape_invalid",
            lambda: ledger.observe(CALLER, "0" * 32, 1_000_060, "nope"),
        )

    def test_default_capacity_matches_the_in_process_guard(self) -> None:
        self.assertEqual(8192, DEFAULT_REPLAY_LEDGER_ENTRIES)


class LedgerFailClosedTests(LedgerTestCase):
    """A ledger that cannot be trusted refuses; it never reads as empty."""

    def corrupt(self, raw: bytes) -> None:
        path = replay_ledger_path(self.state_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_bytes(raw)
        os.chmod(path, 0o600)

    def test_corrupt_ledgers_refuse_instead_of_admitting_a_replay(self) -> None:
        cases = (
            b"{not json",
            b"[]",
            b'{"schema": "other.v1", "callers": {}}',
            b'{"callers": {}}',
            b'{"schema": "%s", "callers": []}' % ORACLE_REPLAY_LEDGER_SCHEMA.encode(),
            json.dumps(
                {
                    "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
                    "callers": {"NOT VALID": {}},
                }
            ).encode(),
            json.dumps(
                {
                    "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
                    "callers": {CALLER: {"short": {"expires_at": 1, "digest": "a" * 64}}},
                }
            ).encode(),
            json.dumps(
                {
                    "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
                    "callers": {
                        CALLER: {"0" * 32: {"expires_at": 1, "digest": "nope"}}
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
                    "callers": {
                        CALLER: {"0" * 32: {"expires_at": "soon", "digest": "a" * 64}}
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "schema": ORACLE_REPLAY_LEDGER_SCHEMA,
                    "callers": {CALLER: {"0" * 32: {"expires_at": 1}}},
                }
            ).encode(),
            b'{"schema": "x", "schema": "y"}',
        )
        for raw in cases:
            self.corrupt(raw)
            self.assert_refused(
                "replay_ledger_corrupt",
                lambda: self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64),
            )

    def test_an_empty_file_is_a_fresh_ledger_not_a_corrupt_one(self) -> None:
        self.corrupt(b"")
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)

    def test_group_readable_ledger_is_refused(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        os.chmod(replay_ledger_path(self.state_root), 0o644)
        self.assert_refused(
            "replay_ledger_permissions",
            lambda: self.ledger().observe(CALLER, "1" * 32, 1_000_060, "b" * 64),
        )

    def test_symlinked_ledger_is_refused(self) -> None:
        ledger = self.ledger()
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        path = replay_ledger_path(self.state_root)
        target = path.parent / "elsewhere.json"
        target.write_bytes(path.read_bytes())
        os.chmod(target, 0o600)
        path.unlink()
        path.symlink_to(target)
        self.assert_refused(
            "replay_ledger_permissions",
            lambda: self.ledger().observe(CALLER, "1" * 32, 1_000_060, "b" * 64),
        )

    def test_foreign_uid_is_refused(self) -> None:
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        self.assert_refused(
            "replay_ledger_permissions",
            lambda: self.ledger(uid=os.getuid() + 1).observe(
                CALLER, "1" * 32, 1_000_060, "b" * 64
            ),
        )

    def test_a_held_lock_times_out_instead_of_hanging(self) -> None:
        ledger = self.ledger(lock_timeout_seconds=0.05)
        ledger.observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        lock_path = replay_ledger_path(self.state_root).with_name(
            replay_ledger_path(self.state_root).name + ".lock"
        )
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.assert_refused(
                "replay_ledger_locked",
                lambda: ledger.observe(CALLER, "1" * 32, 1_000_060, "b" * 64),
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class LedgerWorkerBoundaryTests(LedgerTestCase):
    """Separate PROCESSES share one ledger, and race it safely."""

    def test_a_claim_made_by_another_worker_is_refused_here(self) -> None:
        self.assertEqual(
            "recorded",
            self.worker(CALLER, "0" * 32, "1000060", "1000000", "a" * 64),
        )
        self.assert_refused(
            "replay_detected",
            lambda: self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64),
        )

    def test_a_claim_made_here_is_refused_by_another_worker(self) -> None:
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        self.assertEqual(
            "replay_detected",
            self.worker(CALLER, "0" * 32, "1000060", "1000000", "a" * 64),
        )

    def test_a_spliced_body_is_refused_across_the_worker_boundary(self) -> None:
        self.ledger().observe(CALLER, "0" * 32, 1_000_060, "a" * 64)
        self.assertEqual(
            "nonce_reuse_mismatch",
            self.worker(CALLER, "0" * 32, "1000060", "1000000", "b" * 64),
        )

    def test_concurrent_workers_admit_exactly_one_claim(self) -> None:
        # The atomicity proof: eight processes race the SAME nonce through the
        # read-prune-check-write critical section at once.
        script = LEDGER_WORKER.format(env_manager=str(ENV_MANAGER_DIR))
        argv = [CALLER, "0" * 32, "1000060", "1000000", "a" * 64]
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.state_root), *argv],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
            outcomes.append(stdout.strip())
        self.assertEqual(1, outcomes.count("recorded"), outcomes)
        self.assertEqual(7, outcomes.count("replay_detected"), outcomes)

    def test_concurrent_workers_with_distinct_nonces_all_persist(self) -> None:
        # The other half of atomicity: a lost update would silently drop a
        # claim, so every distinct nonce must survive the concurrent writes.
        script = LEDGER_WORKER.format(env_manager=str(ENV_MANAGER_DIR))
        nonces = [f"{index:032x}" for index in range(8)]
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(self.state_root),
                    CALLER,
                    nonce,
                    "1000060",
                    "1000000",
                    "a" * 64,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for nonce in nonces
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
            self.assertEqual("recorded", stdout.strip())
        ledger = self.ledger()
        self.assertEqual(8, len(ledger))
        for nonce in nonces:
            self.assert_refused(
                "replay_detected",
                lambda nonce=nonce: ledger.observe(
                    CALLER, nonce, 1_000_060, "a" * 64
                ),
            )


class DurableAdmissionTests(AdmissionTests):
    """Every AdmissionTests case, re-run against the durable ledger.

    Subclassing is the point: the durable ledger must satisfy the in-process
    guard's entire contract at the admission layer, not just in isolation.
    """

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger_root = Path(temporary.name).resolve() / "state"
        self.guard = DurableReplayLedger(self.ledger_root, clock=self.clock)

    def test_a_captured_request_stays_rejected_across_a_restart(self) -> None:
        payload = encode(request_document())
        self.run_oracle(payload)
        self.assertEqual(1, len(self.contacted))
        # The worker restarts inside the request's own freshness window: the
        # replay guard is new, the ledger is not.
        self.guard = DurableReplayLedger(self.ledger_root, clock=self.clock)
        self.assert_refused("replay_detected", lambda: self.run_oracle(payload))
        self.assertEqual(1, len(self.contacted))

    def test_a_second_worker_cannot_spend_the_same_request(self) -> None:
        payload = encode(request_document())
        self.run_oracle(payload)
        second_worker = DurableReplayLedger(self.ledger_root, clock=self.clock)
        self.guard = second_worker
        self.assert_refused("replay_detected", lambda: self.run_oracle(payload))
        self.assertEqual(1, len(self.contacted))


class ContractTests(BrokerTestCase):
    """Invariants that keep the gates honest as the module changes."""

    def test_both_replay_stores_share_one_contract(self) -> None:
        self.assertTrue(issubclass(ReplayGuard, ReplayDefense))
        self.assertTrue(issubclass(DurableReplayLedger, ReplayDefense))

    def test_an_object_that_is_not_a_replay_store_is_refused(self) -> None:
        class Pretender:
            def observe(self, *_args: object) -> None:
                return None

        manager = broker_admission(
            encode(request_document()),
            PeerIdentity(caller_id=CALLER, auth_method=AUTH_METHOD_WHOIS),
            endpoint=validate_bind_endpoint("100.64.0.1", 8443),
            policy_engine=None,
            replay_guard=Pretender(),
        )
        self.assert_refused("replay_guard_unavailable", manager.__enter__)

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = BROKER_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_allowlists_and_denylist_are_disjoint(self) -> None:
        def normalized(keys: frozenset[str]) -> set[str]:
            return {key.replace("_", "") for key in keys}

        self.assertEqual(set(), normalized(REQUEST_KEYS) & set(FORBIDDEN_FIELDS))
        self.assertEqual(set(), normalized(ATTACHMENT_KEYS) & set(FORBIDDEN_FIELDS))

    def test_broker_ceilings_match_the_policy_engine_ceilings(self) -> None:
        from runtime_manager import oracle_policy

        bounds = oracle_policy._INTEGER_BOUNDS
        self.assertEqual(bounds["max_prompt_bytes"][1], MAX_PROMPT_BYTES)
        self.assertEqual(bounds["max_files"][1], MAX_ATTACHMENTS)
        self.assertEqual(bounds["max_attachment_bytes"][1], MAX_ATTACHMENT_BYTES)
        self.assertEqual(bounds["max_runtime_seconds"][1], MAX_TIMEOUT_SECONDS)

    def test_refusal_renders_the_typed_error_envelope(self) -> None:
        error = self.assert_refused(
            "wildcard_listener_forbidden",
            lambda: validate_bind_endpoint("0.0.0.0", 8443),
        )
        payload = error.to_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual("wildcard_listener_forbidden", payload["error"]["code"])
        self.assertEqual("wildcard_listener_forbidden", payload["error_code"])

    def test_module_imports_no_browser_or_process_surface(self) -> None:
        source = BROKER_SOURCE.read_text(encoding="utf-8")
        for banned in (
            "import subprocess",
            "import socket",
            "import http",
            "import urllib",
            "selenium",
            "playwright",
            "webdriver",
        ):
            self.assertNotIn(banned, source, banned)

    def test_broker_declares_no_mcp_mirror(self) -> None:
        source = BROKER_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("mcp_tool", source)

    def test_bind_endpoint_cannot_be_forged_with_a_bad_scope(self) -> None:
        self.assert_refused(
            "bind_host_invalid",
            lambda: BindEndpoint(host="0.0.0.0", port=8443, scope="wildcard"),
        )

    def test_policy_error_type_is_not_leaked_through_admission(self) -> None:
        # OraclePolicyError stays available for direct engine callers; the
        # broker re-raises it as its own typed refusal.
        self.assertTrue(issubclass(OraclePolicyError, RuntimeError))
        self.assertFalse(issubclass(OracleBrokerError, OraclePolicyError))


def soft_grant() -> object:
    """A grant that is shaped exactly like a real one."""

    return SimpleNamespace(
        reservation_id="0" * 32, admitted_at=1_000_000, expires_at=1_000_600
    )


class SoftAuthorizer:
    """The escape hatch under test: a syntactically valid "yes".

    It consults no policy, reserves no quota, and touches no enrolled
    authority, but every downstream check — including the receipt — reads as
    correct. A soft decision shaped like a real one is worse than an outright
    failure, because nothing after admission can tell them apart.
    """

    def __init__(self) -> None:
        self.calls = 0

    @contextlib.contextmanager
    def admission(self, caller_id: str, facts: object):
        self.calls += 1
        yield soft_grant()


class PolicyAuthoritySealTests(BrokerTestCase):
    """Nothing reaches admission as an authority without proving it is one."""

    def test_a_real_engine_is_the_only_production_authority(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            policy = OraclePolicy.from_mapping(policy_document())
            provision_oracle_policy_authority(
                policy, root / "state", authority_directory=root / "authority"
            )
            engine = OraclePolicyEngine(
                policy, root / "state", authority_directory=root / "authority"
            )
            authority = production_policy_authority(engine)
            self.assertEqual(authority.kind, AUTHORITY_KIND_PRODUCTION)
            self.assertTrue(authority.healthy)
            self.assertRegex(authority.fingerprint, r"^[0-9a-f]{64}$")

    def test_a_duck_typed_soft_authorizer_is_refused(self) -> None:
        """The exact defect this bead names."""
        soft = SoftAuthorizer()
        self.assert_refused(
            "policy_authority_unsealed", lambda: require_policy_authority(soft)
        )
        self.assertEqual(0, soft.calls, "the soft authorizer was never consulted")

    def test_the_refusal_distinguishes_a_pretender_from_a_missing_engine(self) -> None:
        """A pretender is a security event; None is a wiring mistake."""
        self.assert_refused(
            "policy_authority_unsealed",
            lambda: require_policy_authority(SimpleNamespace(admission=lambda *_a: None)),
        )
        for value in (None, "engine", 7, object()):
            with self.subTest(value=value):
                self.assert_refused(
                    "policy_engine_unavailable",
                    lambda value=value: require_policy_authority(value),
                )

    def test_a_policy_engine_subclass_cannot_pose_as_production(self) -> None:
        """One inheritance hop is the same soft-decision problem."""

        class Sneaky(OraclePolicyEngine):
            @contextlib.contextmanager
            def admission(self, caller_id: str, facts: object):
                yield soft_grant()

        sneaky = object.__new__(Sneaky)
        self.assert_refused(
            "policy_authority_unsealed", lambda: require_policy_authority(sneaky)
        )
        self.assert_refused(
            "policy_authority_unsealed", lambda: production_policy_authority(sneaky)
        )

    def test_the_authority_cannot_be_subclassed(self) -> None:
        def define() -> None:
            class Forged(PolicyAuthority):
                pass

        self.assert_refused("policy_authority_unsealed", define)

    def test_direct_construction_without_the_seal_is_refused(self) -> None:
        self.assert_refused(
            "policy_authority_unsealed",
            lambda: PolicyAuthority(
                object(),
                kind=AUTHORITY_KIND_PRODUCTION,
                fingerprint="a" * 64,
                admission=lambda *_a: None,
            ),
        )

    def test_an_instance_that_never_ran_init_is_refused_not_crashed(self) -> None:
        """``object.__new__`` skips the seal; that must be typed, not an
        AttributeError escaping to the caller."""
        ghost = object.__new__(PolicyAuthority)
        self.assert_refused(
            "policy_authority_unsealed", lambda: require_policy_authority(ghost)
        )

    def test_overwriting_the_seal_after_construction_is_refused(self) -> None:
        authority = sealed_fixture_authority(lambda *_a: None, label="unit")
        object.__setattr__(authority, "_seal", object())
        self.assert_refused(
            "policy_authority_unsealed", lambda: require_policy_authority(authority)
        )

    def test_a_fixture_authority_can_never_report_healthy(self) -> None:
        authority = sealed_fixture_authority(lambda *_a: None, label="unit")
        self.assertEqual(authority.kind, AUTHORITY_KIND_FIXTURE)
        self.assertFalse(authority.healthy)
        report = authority.health()
        self.assertEqual(report["schema"], ORACLE_AUTHORITY_HEALTH_SCHEMA)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["reasons"], ["fixture_authority"])

    def test_the_health_report_carries_no_path_or_policy_body(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            policy = OraclePolicy.from_mapping(policy_document())
            provision_oracle_policy_authority(
                policy, root / "state", authority_directory=root / "authority"
            )
            engine = OraclePolicyEngine(
                policy, root / "state", authority_directory=root / "authority"
            )
            report = production_policy_authority(engine).health()
            encoded = json.dumps(report)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn(CALLER, encoded)
            self.assertEqual(
                sorted(report),
                ["authority_fingerprint", "healthy", "kind", "reasons", "schema"],
            )

    def test_fixture_labels_are_bounded_and_the_admission_must_be_callable(self) -> None:
        for label in ("", "Bad Label", "x" * 65, None, 3):
            with self.subTest(label=label):
                self.assert_refused(
                    "policy_authority_unsealed",
                    lambda label=label: sealed_fixture_authority(
                        lambda *_a: None, label=label
                    ),
                )
        self.assert_refused(
            "policy_authority_unsealed", lambda: sealed_fixture_authority(None)
        )

    def test_only_the_declared_kinds_exist(self) -> None:
        self.assertEqual(
            AUTHORITY_KINDS, {AUTHORITY_KIND_PRODUCTION, AUTHORITY_KIND_FIXTURE}
        )


if __name__ == "__main__":
    unittest.main()
