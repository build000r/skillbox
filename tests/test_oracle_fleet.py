"""Contract tests for ``runtime_manager.oracle_fleet``.

Run:

    PYTHONPATH=.env-manager python3 -m unittest tests.test_oracle_fleet

The claim under test is that ``d3`` and ``d3c`` are two names over *one* client
contract, and that the contract's security posture is a property of its shape
rather than of caller discipline. So the suite is organized as:

* canonicalization — every operator spelling lands on a canonical target, and
  an unknown one refuses instead of guessing at somebody's machine;
* machine binding — a target resolves by capability against a fixture registry,
  never by hard-coded hostname, and ambiguity is an error;
* one contract — d3 and d3c produce structurally identical plans;
* tunnel loss — recovery is proven against a fault-injecting transport, with
  the replay and duplicate-side-effect rules asserted, not assumed;
* the failure gate — wildcard listeners, CDP exposure, hooks/browserConfig,
  cookie/profile transfer, argv tokens, and unauthenticated contact.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

from runtime_manager import machines as m
from runtime_manager import oracle_broker as broker
from runtime_manager import oracle_fleet as fleet


# Machine ids here are deliberately arbitrary and share no substring with the
# target names or their aliases. If binding were by name rather than by
# capability, nothing in this fixture would resolve.
FIXTURE_YAML = """
version: 1

machines:
  box-alpha:
    hostnames: [alpha.example]
    home: /Users/operator
    caps: [os:darwin, arch:arm64, xcode, durable]
    trust: local

  box-bravo:
    hostnames: [bravo.example]
    home: /home/skillbox
    caps: [os:linux, arch:amd64, docker, tailnet, durable]
    trust: allowlisted

  box-charlie:
    hostnames: [charlie.example]
    home: /home/aiops
    caps: [os:linux, durable]
    trust: allowlisted

  box-delta:
    hostnames: [delta.example]
    home: /home/worker
    caps: [os:wsl, arch:amd64, docker, durable]
    trust: allowlisted
"""

FIXTURE_MACHINE_IDS = ("box-alpha", "box-bravo", "box-charlie", "box-delta")

RESULT_BYTES = b"# Oracle result\n\nfindings\n"
RESULT_SHA = hashlib.sha256(RESULT_BYTES).hexdigest()

TAILNET_ENDPOINT = ("100.64.0.9", 8443)
LOOPBACK_ENDPOINT = ("127.0.0.1", 8443)


class FixedClock:
    def __init__(self, now: float = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def sequence_nonces() -> "object":
    """Deterministic nonce source so attempt digests are comparable."""
    counter = {"n": 0}

    def mint() -> str:
        counter["n"] += 1
        return f"{counter['n']:032x}"

    return mint


def attachment(name: str, payload: bytes) -> fleet.TransferFile:
    return fleet.TransferFile(
        name=name,
        mime_type="text/markdown",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def broker_receipt(document: dict, *, auth_method: str = broker.AUTH_METHOD_WHOIS) -> dict:
    """A receipt shaped like the broker's, bound to this exact request."""
    request = broker.parse_request(fleet.encode_request(document))
    return {
        "schema": broker.ORACLE_RECEIPT_SCHEMA,
        "protocol": broker.ORACLE_BROKER_PROTOCOL,
        "caller_id": "devbox-1",
        "auth_method": auth_method,
        "node": "client.tailnet-example.ts.net.",
        "endpoint": "100.64.0.9:8443",
        "scope": broker.SCOPE_TAILNET,
        "mode": request.mode,
        "reservation_id": "0" * 32,
        "request_digest": request.request_digest,
        "prompt_bytes": request.prompt_bytes,
        "file_count": request.file_count,
        "attachment_bytes": request.attachment_bytes,
        "timeout_seconds": request.timeout_seconds,
        "admitted_at": 1_700_000_000,
        "expires_at": 1_700_000_120,
    }


class RecordingTransport:
    """A private transport that can be told to drop the tunnel N times."""

    def __init__(self, *, lose_first: int = 0, auth_method: str = broker.AUTH_METHOD_WHOIS):
        self.lose_first = lose_first
        self.auth_method = auth_method
        self.seen: list[dict] = []
        self.responses = 0

    def __call__(self, document: dict, attempt: int):
        self.seen.append(document)
        if attempt <= self.lose_first:
            raise fleet.FleetTransportLost("tunnel closed before any reply")
        self.responses += 1
        envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
        return broker_receipt(document, auth_method=self.auth_method), envelope, RESULT_BYTES


class FleetTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "machines.yaml"
        path.write_text(textwrap.dedent(FIXTURE_YAML).strip() + "\n", encoding="utf-8")
        self.config = m.load_machines_config(path)
        self.clock = FixedClock()

    def plan(self, target: str = "d3", **overrides):
        options = {
            "config": self.config,
            "target": target,
            "host": TAILNET_ENDPOINT[0],
            "port": TAILNET_ENDPOINT[1],
            "prompt": "compare the two proposals",
            "mode": "standard",
            "timeout_seconds": 300,
        }
        options.update(overrides)
        return fleet.plan_invocation(**options)  # type: ignore[arg-type]

    def run_invocation(self, target: str = "d3", *, transport=None, **kwargs):
        invocation = self.plan(target)
        return fleet.invoke(
            invocation,
            transport or RecordingTransport(),
            clock=self.clock,
            nonce_source=sequence_nonces(),
            **kwargs,
        )


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class TargetCanonicalizationTests(FleetTestCase):
    def test_only_two_canonical_targets_exist(self) -> None:
        self.assertEqual(fleet.known_targets(), ("d3", "d3c"))

    def test_every_alias_lands_on_a_canonical_target(self) -> None:
        for alias, canonical in fleet.known_aliases().items():
            with self.subTest(alias=alias):
                self.assertIn(canonical, fleet.CANONICAL_TARGETS)
                self.assertEqual(fleet.resolve_target(alias), canonical)

    def test_canonical_targets_resolve_to_themselves(self) -> None:
        for target in fleet.CANONICAL_TARGETS:
            with self.subTest(target=target):
                self.assertEqual(fleet.resolve_target(target), target)

    def test_d3c_and_the_conference_spellings_agree(self) -> None:
        """The drift this bead exists to fix: d3c resolved nowhere."""
        for spelling in ("d3c", "D3C", "d3-c", "d3_c", "conference1-wsl", "conf"):
            with self.subTest(spelling=spelling):
                self.assertEqual(fleet.resolve_target(spelling), "d3c")

    def test_normalization_folds_case_separators_and_padding(self) -> None:
        for spelling in ("  D3  ", "D3", "d3--", "d3\n", "\td3 "):
            with self.subTest(spelling=spelling):
                self.assertEqual(fleet.resolve_target(spelling), "d3")
        self.assertEqual(fleet.resolve_target("D3_C"), "d3c")

    def test_compatibility_forms_do_not_become_a_second_target(self) -> None:
        self.assertEqual(fleet.resolve_target("ｄ３"), "d3")  # fullwidth d3

    def test_unknown_targets_refuse_instead_of_guessing(self) -> None:
        for spelling in ("d4", "prod", "sweet", "d3cc", "conference2"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(fleet.OracleFleetError) as caught:
                    fleet.resolve_target(spelling)
                self.assertEqual(caught.exception.code, "fleet_target_unknown")

    def test_malformed_targets_are_refused_before_lookup(self) -> None:
        for value in ("", "  ", "-", "../d3", "d3;rm -rf /", "d3$(id)", "x" * 200, None, 3):
            with self.subTest(value=value):
                with self.assertRaises(fleet.OracleFleetError) as caught:
                    fleet.resolve_target(value)
                self.assertEqual(caught.exception.code, "fleet_target_invalid")

    def test_a_refusal_never_echoes_the_caller_string(self) -> None:
        secret = "d3-acquisition-memo"
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.resolve_target(secret)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("acquisition", repr(caught.exception.to_payload()))


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class MachineBindingTests(FleetTestCase):
    def test_targets_bind_by_capability_not_by_hostname(self) -> None:
        """The fixture ids share no substring with any target or alias."""
        self.assertEqual(fleet.resolve_machine(self.config, "d3").machine_id, "box-bravo")
        self.assertEqual(fleet.resolve_machine(self.config, "d3c").machine_id, "box-delta")

    def test_no_machine_id_is_hard_coded_in_the_module(self) -> None:
        """Identity stays in the private registry; the module holds predicates."""
        source = (
            ROOT_DIR / ".env-manager" / "runtime_manager" / "oracle_fleet.py"
        ).read_text(encoding="utf-8")
        for requirement in fleet.TARGET_REQUIREMENTS.values():
            for cap in requirement.caps:
                self.assertIn(cap, source)
        for machine_id in FIXTURE_MACHINE_IDS:
            with self.subTest(machine_id=machine_id):
                self.assertNotIn(machine_id, source)

    def test_the_binding_table_holds_only_capabilities_and_trust(self) -> None:
        for target, requirement in fleet.TARGET_REQUIREMENTS.items():
            with self.subTest(target=target):
                self.assertEqual(
                    set(fleet.TargetRequirement.__dataclass_fields__),
                    {"caps", "trust", "label"},
                )
                self.assertTrue(requirement.caps)
                self.assertTrue(requirement.trust)

    def test_an_unresolvable_target_refuses_without_naming_the_registry(self) -> None:
        empty = m.MachinesConfig(machines={}, aliases=(), source_path=None)
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.resolve_machine(empty, "d3")
        self.assertEqual(caught.exception.code, "fleet_machine_unresolved")

    def test_an_ambiguous_registry_refuses_rather_than_picking_one(self) -> None:
        path = Path(self._tmp.name) / "ambiguous.yaml"
        path.write_text(
            textwrap.dedent(
                """
                version: 1
                machines:
                  box-a:
                    caps: [os:wsl, arch:amd64, docker, durable]
                    trust: allowlisted
                  box-b:
                    caps: [os:wsl, arch:amd64, docker, durable]
                    trust: allowlisted
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        config = m.load_machines_config(path)
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.resolve_machine(config, "d3c")
        self.assertEqual(caught.exception.code, "fleet_machine_unresolved")

    def test_a_non_registry_object_is_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.resolve_machine({"machines": {}}, "d3")
        self.assertEqual(caught.exception.code, "fleet_registry_unavailable")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class OneClientContractTests(FleetTestCase):
    """d3 and d3c differ in target and machine, and in nothing else."""

    def test_both_targets_produce_structurally_identical_plans(self) -> None:
        d3 = self.plan("d3").as_document()
        d3c = self.plan("d3c").as_document()
        self.assertEqual(sorted(d3), sorted(d3c))
        for key in d3:
            if key in ("target", "machine_id"):
                continue
            with self.subTest(key=key):
                self.assertEqual(d3[key], d3c[key])
        self.assertEqual((d3["target"], d3c["target"]), ("d3", "d3c"))

    def test_an_alias_produces_the_same_plan_as_its_canonical_name(self) -> None:
        self.assertEqual(
            self.plan("conference1-wsl").as_document(),
            self.plan("d3c").as_document(),
        )

    def test_the_plan_carries_no_prompt_text_only_its_size(self) -> None:
        prompt = "the acquisition memo for Q3"
        document = self.plan("d3", prompt=prompt).as_document()
        self.assertNotIn(prompt, repr(document))
        self.assertEqual(document["prompt_bytes"], len(prompt.encode("utf-8")))

    def test_attachments_are_content_addressed_never_path_addressed(self) -> None:
        payload = b"notes"
        plan = fleet.TransferPlan(files=(attachment("notes.md", payload),))
        document = self.plan("d3", transfer=plan).as_document()
        entry = document["transfer"]["files"][0]
        self.assertEqual(sorted(entry), ["bytes", "mime_type", "name", "sha256"])
        self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())

    def test_a_transfer_plan_has_no_local_path_field_at_all(self) -> None:
        fields = set(fleet.TransferPlan.__dataclass_fields__)
        self.assertEqual(fields, {"files", "result_mime_type"})
        self.assertEqual(
            set(fleet.TransferFile.__dataclass_fields__),
            {"name", "mime_type", "bytes", "sha256"},
        )

    def test_duplicate_attachment_names_are_refused(self) -> None:
        payload = b"x"
        with self.assertRaises(fleet.OracleFleetError):
            fleet.TransferPlan(files=(attachment("a.md", payload), attachment("a.md", payload)))

    def test_traversal_shaped_attachment_names_are_refused(self) -> None:
        for name in ("../escape.md", "/etc/passwd", "a/b.md", ""):
            with self.subTest(name=name):
                with self.assertRaises(fleet.OracleFleetError):
                    attachment(name, b"x")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class ListenerGateTests(FleetTestCase):
    """The plan cannot even be built against a non-private listener."""

    def test_loopback_and_tailnet_listeners_are_accepted(self) -> None:
        self.assertEqual(self.plan("d3").endpoint.scope, broker.SCOPE_TAILNET)
        loopback = self.plan("d3", host=LOOPBACK_ENDPOINT[0], port=LOOPBACK_ENDPOINT[1])
        self.assertEqual(loopback.endpoint.scope, broker.SCOPE_LOOPBACK)

    def test_wildcard_hostname_and_public_listeners_are_refused(self) -> None:
        cases = (
            ("0.0.0.0", "wildcard_listener_forbidden"),
            ("::", "wildcard_listener_forbidden"),
            ("::ffff:0.0.0.0", "wildcard_listener_forbidden"),
            ("localhost", "bind_hostname_forbidden"),
            ("oracle.example.com", "bind_hostname_forbidden"),
            ("203.0.113.10", "public_listener_forbidden"),
            ("192.168.1.10", "public_listener_forbidden"),
        )
        for host, code in cases:
            with self.subTest(host=host):
                with self.assertRaises(broker.OracleBrokerError) as caught:
                    self.plan("d3", host=host)
                self.assertEqual(caught.exception.code, code)

    def test_privileged_ports_are_refused(self) -> None:
        with self.assertRaises(broker.OracleBrokerError) as caught:
            self.plan("d3", port=443)
        self.assertEqual(caught.exception.code, "bind_port_forbidden")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class RequestGateTests(FleetTestCase):
    """The rendered document survives the broker's own allowlist, at plan time."""

    def test_the_planned_document_parses_as_a_broker_request(self) -> None:
        document = self.plan("d3").render_request(now=1_700_000_000, nonce="0" * 32)
        request = broker.parse_request(fleet.encode_request(document))
        self.assertEqual(request.mode, "standard")
        self.assertEqual(sorted(document), sorted(broker.REQUEST_KEYS))

    def test_the_wire_encoding_is_canonical_and_reproducible(self) -> None:
        document = self.plan("d3").render_request(now=1_700_000_000, nonce="0" * 32)
        payload = fleet.encode_request(document)
        self.assertEqual(payload, fleet.encode_request(dict(reversed(list(document.items())))))
        self.assertEqual(payload.decode("ascii")[:1], "{")

    def test_the_wire_document_has_no_identity_or_host_field(self) -> None:
        document = self.plan("d3c").render_request(now=1_700_000_000, nonce="0" * 32)
        for absent in ("target", "machine_id", "endpoint", "caller_id", "node", "scope"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, document)

    def test_an_empty_prompt_and_a_bad_mode_are_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError):
            self.plan("d3", prompt="")
        with self.assertRaises(fleet.OracleFleetError):
            self.plan("d3", mode="turbo")

    def test_a_ttl_outside_the_broker_window_is_refused_at_plan_time(self) -> None:
        """Planning renders once, so a bad window fails before any attempt."""
        for value in (0, -1, broker.MAX_TTL_SECONDS + 1):
            with self.subTest(ttl_seconds=value):
                with self.assertRaises(fleet.OracleFleetError) as caught:
                    self.plan("d3", ttl_seconds=value)
                self.assertEqual(caught.exception.code, "request_window_invalid")

    def test_a_malformed_nonce_is_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError) as caught:
            self.plan("d3").render_request(now=1_700_000_000, nonce="nope")
        self.assertEqual(caught.exception.code, "nonce_invalid")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class TunnelRecoveryTests(FleetTestCase):
    """Recovery from tunnel loss, proven against a fault-injecting transport."""

    def test_a_clean_invocation_takes_one_attempt(self) -> None:
        transport = RecordingTransport()
        result = self.run_invocation("d3", transport=transport)
        self.assertEqual(len(result.attempts), 1)
        self.assertFalse(result.recovered)
        self.assertEqual(result.result_bytes, len(RESULT_BYTES))
        self.assertEqual(result.result_sha256, RESULT_SHA)

    def test_a_dropped_tunnel_is_retried_and_recovers(self) -> None:
        transport = RecordingTransport(lose_first=2)
        result = self.run_invocation("d3c", transport=transport)
        self.assertEqual([a.outcome for a in result.attempts],
                         ["transport_lost", "transport_lost", "admitted"])
        self.assertTrue(result.recovered)
        self.assertEqual(transport.responses, 1)

    def test_each_attempt_mints_a_fresh_nonce_so_a_retry_is_not_a_replay(self) -> None:
        """The broker's replay guard is single-use; a reused nonce is refused."""
        transport = RecordingTransport(lose_first=2)
        result = self.run_invocation("d3", transport=transport)
        nonces = [attempt.nonce for attempt in result.attempts]
        self.assertEqual(len(nonces), len(set(nonces)))
        wire_nonces = [document["nonce"] for document in transport.seen]
        self.assertEqual(wire_nonces, nonces)

    def test_each_attempt_carries_a_distinct_request_digest(self) -> None:
        transport = RecordingTransport(lose_first=1)
        result = self.run_invocation("d3", transport=transport)
        digests = [attempt.request_digest for attempt in result.attempts]
        self.assertEqual(len(digests), len(set(digests)))

    def test_no_further_attempt_is_made_once_a_response_exists(self) -> None:
        """A duplicated result is a worse failure than a lost one."""
        transport = RecordingTransport()
        self.run_invocation("d3", transport=transport, attempts=5)
        self.assertEqual(transport.responses, 1)
        self.assertEqual(len(transport.seen), 1)

    def test_exhausting_the_attempt_budget_fails_closed(self) -> None:
        transport = RecordingTransport(lose_first=9)
        with self.assertRaises(fleet.OracleFleetError) as caught:
            self.run_invocation("d3", transport=transport, attempts=3)
        self.assertEqual(caught.exception.code, "fleet_transport_lost")
        self.assertEqual(transport.responses, 0)

    def test_a_broker_refusal_is_terminal_and_never_retried(self) -> None:
        """Re-sending a rejected request turns a denial into a quota attack."""
        calls = {"n": 0}

        def refusing(document, attempt):
            calls["n"] += 1
            raise broker.OracleBrokerError("replay_detected")

        with self.assertRaises(broker.OracleBrokerError):
            self.run_invocation("d3", transport=refusing, attempts=4)
        self.assertEqual(calls["n"], 1)

    def test_a_receipt_for_a_different_request_is_refused(self) -> None:
        def mismatched(document, attempt):
            receipt = broker_receipt(document)
            receipt["request_digest"] = "f" * 64
            envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
            return receipt, envelope, RESULT_BYTES

        with self.assertRaises(fleet.OracleFleetError) as caught:
            self.run_invocation("d3", transport=mismatched)
        self.assertEqual(caught.exception.code, "fleet_receipt_mismatch")

    def test_a_receipt_of_the_wrong_schema_is_refused(self) -> None:
        def wrong_schema(document, attempt):
            receipt = broker_receipt(document)
            receipt["schema"] = "something.else.v1"
            envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
            return receipt, envelope, RESULT_BYTES

        with self.assertRaises(fleet.OracleFleetError) as caught:
            self.run_invocation("d3", transport=wrong_schema)
        self.assertEqual(caught.exception.code, "fleet_receipt_invalid")

    def test_attempt_budget_bounds_are_enforced(self) -> None:
        for value in (0, -1, fleet.MAX_ATTEMPTS + 1, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(fleet.OracleFleetError) as caught:
                    self.run_invocation("d3", attempts=value)
                self.assertEqual(caught.exception.code, "fleet_attempts_invalid")

    def test_a_non_callable_transport_is_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.invoke(self.plan("d3"), None, clock=self.clock)
        self.assertEqual(caught.exception.code, "fleet_transport_unavailable")

    def test_a_foreign_invocation_object_is_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.invoke({"target": "d3"}, RecordingTransport(), clock=self.clock)
        self.assertEqual(caught.exception.code, "fleet_invocation_invalid")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class ResultTransferTests(FleetTestCase):
    """Results are accepted only when they are what the host promised."""

    def test_a_matching_result_verifies(self) -> None:
        envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
        self.assertEqual(fleet.verify_result(envelope, RESULT_BYTES), len(RESULT_BYTES))

    def test_an_empty_result_is_never_evidence(self) -> None:
        with self.assertRaises(fleet.OracleFleetError):
            fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=0)
        envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.verify_result(envelope, b"")
        self.assertEqual(caught.exception.code, "result_empty")

    def test_a_size_or_digest_mismatch_is_refused(self) -> None:
        envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.verify_result(envelope, RESULT_BYTES + b"!")
        self.assertEqual(caught.exception.code, "result_size_mismatch")
        other = b"x" * len(RESULT_BYTES)
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.verify_result(envelope, other)
        self.assertEqual(caught.exception.code, "result_digest_mismatch")

    def test_a_malformed_envelope_is_refused(self) -> None:
        for digest in ("", "deadbeef", RESULT_SHA.upper(), None):
            with self.subTest(digest=digest):
                with self.assertRaises(fleet.OracleFleetError):
                    fleet.ResultEnvelope(sha256=digest, bytes=10)

    def test_the_invocation_receipt_records_the_verified_result(self) -> None:
        result = self.run_invocation("d3")
        document = result.as_document()
        self.assertEqual(document["result_sha256"], RESULT_SHA)
        self.assertEqual(document["result_bytes"], len(RESULT_BYTES))
        self.assertEqual(document["schema"], fleet.FLEET_RECEIPT_SCHEMA)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class SecurityAuditTests(FleetTestCase):
    """The bead's failure gate, decided from the rendered contract."""

    def both(self):
        return [
            self.run_invocation("d3"),
            self.run_invocation("conference1-wsl", transport=RecordingTransport(lose_first=1)),
        ]

    def test_every_hard_gate_passes_for_a_real_pair_of_invocations(self) -> None:
        audit = fleet.fleet_security_audit(self.both())
        self.assertEqual(audit["failed_gates"], [])
        self.assertEqual(audit["hard_gates"], "pass")
        self.assertEqual(sorted(entry["gate"] for entry in audit["gates"]),
                         sorted(fleet.HARD_GATES))
        self.assertEqual(audit["targets"], ["d3", "d3c"])

    def test_the_audit_covers_every_declared_hard_gate(self) -> None:
        audit = fleet.fleet_security_audit(self.both())
        self.assertEqual(len(audit["gates"]), len(fleet.HARD_GATES))
        for entry in audit["gates"]:
            with self.subTest(gate=entry["gate"]):
                self.assertEqual(entry["status"], "pass")
                self.assertTrue(entry["detail"])

    def test_rendered_argv_carries_no_credential_and_no_cdp_flag(self) -> None:
        argv = fleet.render_argv(self.plan("d3"))
        joined = " ".join(argv).casefold()
        for marker in fleet._CREDENTIAL_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, joined)
        for marker in fleet._CDP_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, joined)

    def test_the_audit_fails_when_an_unauthenticated_receipt_appears(self) -> None:
        transport = RecordingTransport()
        transport.auth_method = "trust-me"
        invocation = self.plan("d3")

        def unauthenticated(document, attempt):
            receipt = broker_receipt(document)
            receipt["auth_method"] = "trust-me"
            envelope = fleet.ResultEnvelope(sha256=RESULT_SHA, bytes=len(RESULT_BYTES))
            return receipt, envelope, RESULT_BYTES

        result = fleet.invoke(
            invocation, unauthenticated, clock=self.clock, nonce_source=sequence_nonces()
        )
        audit = fleet.fleet_security_audit([result])
        self.assertEqual(audit["hard_gates"], "fail")
        self.assertIn("unauthenticated_browser_contact", audit["failed_gates"])

    def test_the_audit_is_not_vacuous_for_cdp_and_credential_markers(self) -> None:
        """A future field reintroducing a CDP URL or cookie path must fail."""
        result = self.run_invocation("d3")
        poisoned = dict(result.receipt)
        poisoned["node"] = "ws://127.0.0.1:9222/devtools/browser/abc"
        cdp = fleet.FleetResult(
            invocation=result.invocation,
            attempts=result.attempts,
            receipt=poisoned,
            result_bytes=result.result_bytes,
            result_sha256=result.result_sha256,
        )
        audit = fleet.fleet_security_audit([cdp])
        self.assertEqual(audit["hard_gates"], "fail")
        self.assertIn("raw_cdp_exposure", audit["failed_gates"])

        leaky = dict(result.receipt)
        leaky["node"] = "/home/worker/.config/chrome/Default/Cookies"
        cookie = fleet.FleetResult(
            invocation=result.invocation,
            attempts=result.attempts,
            receipt=leaky,
            result_bytes=result.result_bytes,
            result_sha256=result.result_sha256,
        )
        audit = fleet.fleet_security_audit([cookie])
        self.assertEqual(audit["hard_gates"], "fail")
        self.assertIn("cookie_profile_transfer", audit["failed_gates"])

    def test_an_empty_or_foreign_audit_input_is_refused(self) -> None:
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.fleet_security_audit([])
        self.assertEqual(caught.exception.code, "fleet_audit_empty")
        with self.assertRaises(fleet.OracleFleetError) as caught:
            fleet.fleet_security_audit([{"target": "d3"}])
        self.assertEqual(caught.exception.code, "fleet_audit_invalid")

    def test_the_contract_declares_no_credential_field_anywhere(self) -> None:
        """Nothing can put a token in argv because nothing holds a token."""
        for cls in (fleet.FleetInvocation, fleet.TransferPlan, fleet.TransferFile,
                    fleet.ResultEnvelope, fleet.FleetAttempt):
            with self.subTest(cls=cls.__name__):
                for name in cls.__dataclass_fields__:
                    lowered = name.casefold()
                    for marker in ("token", "cookie", "secret", "password", "key", "profile"):
                        self.assertNotIn(marker, lowered, f"{cls.__name__}.{name}")


if __name__ == "__main__":
    unittest.main()
