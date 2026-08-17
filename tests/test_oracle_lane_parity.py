"""One lane contract for every native Oracle surface.

Covers the three things that make the lane contract worth having:

* identity can never come from the environment — the names that used to carry
  a caller id are refused outright rather than read;
* the local lane is proven by a service-owned file, not by an assertion, and
  gets exactly the same admission gates as the fleet lane;
* CLI, MCP dispatch, and in-process callers resolve the SAME lane from the same
  state, on a caller with no browser anywhere on PATH.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from runtime_manager.oracle_broker import (  # noqa: E402
    AUTH_METHOD_LOCAL_SERVICE,
    AUTH_METHOD_PEERCRED,
    AUTH_METHOD_WHOIS,
    IDENTITY_ENV_OVERRIDE_NAMES,
    LANE_FLEET,
    LANE_LOCAL,
    LANES,
    ORACLE_LOCAL_IDENTITY_SCHEMA,
    BindEndpoint,
    LaneResolution,
    OracleBrokerError,
    PeerIdentity,
    ReplayGuard,
    assert_no_identity_env_override,
    local_identity_path,
    local_service_identity,
    oracle_lane_admission,
    peer_identity_from_whois,
    provision_local_identity,
    resolve_lane,
    validate_bind_endpoint,
)
from runtime_manager.oracle_policy import (  # noqa: E402
    OraclePolicy,
    OraclePolicyEngine,
    provision_oracle_policy_authority,
)

MANAGE_PY = ENV_MANAGER_DIR / "manage.py"
CALLER = "devbox-1"
TAG_ALLOWLIST = frozenset({"tag:oracle-client"})


class MutableClock:
    def __init__(self, now: float = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def whois_document(name: str = f"{CALLER}.tailnet-abc.ts.net.") -> dict[str, object]:
    return {"Node": {"Name": name, "Tags": ["tag:oracle-client"]}}


def request_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": "skillbox.oracle-request.v1",
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


def encode(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


class LaneTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> OracleBrokerError:
        with self.assertRaises(OracleBrokerError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def state_root(self, caller_id: str = CALLER) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve() / "state"
        provision_local_identity(root, caller_id)
        return root


class IdentityEnvOverrideTests(LaneTestCase):
    """The named env vars are refused, never read."""

    def test_every_identity_env_name_is_refused(self) -> None:
        for name in IDENTITY_ENV_OVERRIDE_NAMES:
            self.assert_refused(
                "identity_env_override_forbidden",
                lambda name=name: assert_no_identity_env_override({name: "attacker"}),
            )

    def test_empty_value_is_still_a_refusal(self) -> None:
        # Presence is the signal: an exported-but-empty name still means the
        # host believes the environment selects the identity.
        self.assert_refused(
            "identity_env_override_forbidden",
            lambda: assert_no_identity_env_override({"SKILLBOX_ORACLE_CALLER_ID": ""}),
        )

    def test_unrelated_environment_passes(self) -> None:
        assert_no_identity_env_override({"PATH": "/usr/bin", "HOME": "/home/x"})

    def test_env_override_is_refused_before_any_filesystem_read(self) -> None:
        # The state root does not exist, so an implementation that read the
        # identity file first would report identity_file_missing instead.
        self.assert_refused(
            "identity_env_override_forbidden",
            lambda: resolve_lane(
                state_root="/nonexistent/state",
                environ={"SKILLBOX_ORACLE_CALLER_ID": "attacker"},
            ),
        )

    def test_env_cannot_choose_the_caller_on_a_provisioned_host(self) -> None:
        root = self.state_root("devbox-1")
        # Clean environment: the service-owned file decides.
        self.assertEqual(
            "devbox-1", resolve_lane(state_root=root, environ={}).identity.caller_id
        )
        # Asserting a different caller does not switch identity, it fails.
        self.assert_refused(
            "identity_env_override_forbidden",
            lambda: resolve_lane(
                state_root=root, environ={"SKILLBOX_ORACLE_CALLER_ID": "devbox-2"}
            ),
        )


class LocalServiceIdentityTests(LaneTestCase):
    """The local caller id is a file only its uid may write."""

    def test_provision_then_read_round_trips(self) -> None:
        root = self.state_root("devbox-1")
        identity = local_service_identity(root)
        self.assertEqual("devbox-1", identity.caller_id)
        self.assertEqual(AUTH_METHOD_LOCAL_SERVICE, identity.auth_method)
        self.assertEqual("", identity.node)

    def test_provisioned_modes_are_private(self) -> None:
        root = self.state_root()
        path = local_identity_path(root)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path.parent).st_mode))

    def test_missing_identity_is_refused(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assert_refused(
            "identity_file_missing",
            lambda: local_service_identity(Path(temporary.name) / "state"),
        )

    def test_group_or_other_readable_identity_is_refused(self) -> None:
        root = self.state_root()
        path = local_identity_path(root)
        os.chmod(path, 0o644)
        self.assert_refused(
            "identity_file_permissions", lambda: local_service_identity(root)
        )

    def test_group_writable_directory_is_refused(self) -> None:
        root = self.state_root()
        os.chmod(local_identity_path(root).parent, 0o770)
        self.assert_refused(
            "identity_dir_permissions", lambda: local_service_identity(root)
        )

    def test_foreign_uid_is_refused(self) -> None:
        root = self.state_root()
        self.assert_refused(
            "identity_dir_permissions",
            lambda: local_service_identity(root, uid=os.getuid() + 1),
        )

    def test_symlinked_identity_is_refused(self) -> None:
        root = self.state_root()
        path = local_identity_path(root)
        target = path.parent / "elsewhere.json"
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(target, 0o600)
        path.unlink()
        path.symlink_to(target)
        self.assert_refused(
            "identity_file_missing", lambda: local_service_identity(root)
        )

    def test_malformed_identity_documents_are_refused(self) -> None:
        cases = (
            b"{not json",
            b"[]",
            json.dumps({"schema": "other.v1", "caller_id": CALLER}).encode(),
            json.dumps({"schema": ORACLE_LOCAL_IDENTITY_SCHEMA}).encode(),
            json.dumps(
                {
                    "schema": ORACLE_LOCAL_IDENTITY_SCHEMA,
                    "caller_id": CALLER,
                    "extra": 1,
                }
            ).encode(),
            json.dumps(
                {"schema": ORACLE_LOCAL_IDENTITY_SCHEMA, "caller_id": "NOT VALID"}
            ).encode(),
            b'{"schema": "a", "schema": "b"}',
        )
        root = self.state_root()
        path = local_identity_path(root)
        for raw in cases:
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            with self.assertRaises(OracleBrokerError) as caught:
                local_service_identity(root)
            self.assertIn(
                caught.exception.code, {"identity_file_invalid", "duplicate_field"}
            )

    def test_oversize_identity_is_refused(self) -> None:
        root = self.state_root()
        path = local_identity_path(root)
        path.write_bytes(b"x" * (8 * 1024))
        os.chmod(path, 0o600)
        self.assert_refused(
            "identity_file_invalid", lambda: local_service_identity(root)
        )


class LaneResolutionTests(LaneTestCase):
    """The lane follows the proof, and ambiguity fails closed."""

    def fleet_inputs(self) -> tuple[PeerIdentity, BindEndpoint]:
        return (
            peer_identity_from_whois(whois_document(), tag_allowlist=TAG_ALLOWLIST),
            validate_bind_endpoint("100.64.0.1", 8443),
        )

    def test_transport_proof_resolves_the_fleet_lane(self) -> None:
        identity, endpoint = self.fleet_inputs()
        resolution = resolve_lane(
            transport_identity=identity, endpoint=endpoint, environ={}
        )
        self.assertEqual(LANE_FLEET, resolution.lane)
        self.assertEqual(AUTH_METHOD_WHOIS, resolution.identity.auth_method)
        self.assertEqual("authenticated_transport", resolution.reason)

    def test_loopback_listener_is_a_valid_fleet_lane(self) -> None:
        # `tailscale serve` fronts a loopback listener; gate 1 has already
        # proven it private, so the lane must not refuse it.
        identity, _ = self.fleet_inputs()
        resolution = resolve_lane(
            transport_identity=identity,
            endpoint=validate_bind_endpoint("127.0.0.1", 8443),
            environ={},
        )
        self.assertEqual(LANE_FLEET, resolution.lane)
        self.assertEqual("loopback", resolution.endpoint.scope)

    def test_service_identity_resolves_the_local_lane(self) -> None:
        resolution = resolve_lane(state_root=self.state_root(), environ={})
        self.assertEqual(LANE_LOCAL, resolution.lane)
        self.assertEqual(AUTH_METHOD_LOCAL_SERVICE, resolution.identity.auth_method)
        self.assertEqual("service_owned_identity", resolution.reason)
        self.assertIsNone(resolution.endpoint)

    def test_peer_credential_resolves_the_local_lane(self) -> None:
        resolution = resolve_lane(
            local_identity=PeerIdentity(
                caller_id=CALLER, auth_method=AUTH_METHOD_PEERCRED
            ),
            environ={},
        )
        self.assertEqual(LANE_LOCAL, resolution.lane)
        self.assertEqual("peer_credential", resolution.reason)

    def test_offering_both_lanes_fails_closed(self) -> None:
        identity, endpoint = self.fleet_inputs()
        root = self.state_root()
        self.assert_refused(
            "lane_ambiguous",
            lambda: resolve_lane(
                transport_identity=identity,
                endpoint=endpoint,
                state_root=root,
                environ={},
            ),
        )
        self.assert_refused(
            "lane_ambiguous",
            lambda: resolve_lane(
                local_identity=PeerIdentity(
                    caller_id=CALLER, auth_method=AUTH_METHOD_PEERCRED
                ),
                state_root=root,
                environ={},
            ),
        )

    def test_offering_no_lane_fails_closed(self) -> None:
        self.assert_refused("lane_unavailable", lambda: resolve_lane(environ={}))

    def test_transport_identity_without_a_listener_is_refused(self) -> None:
        identity, _ = self.fleet_inputs()
        self.assert_refused(
            "listener_unverified",
            lambda: resolve_lane(transport_identity=identity, environ={}),
        )

    def test_listener_without_a_transport_identity_is_refused(self) -> None:
        _, endpoint = self.fleet_inputs()
        self.assert_refused(
            "peer_identity_unavailable",
            lambda: resolve_lane(endpoint=endpoint, environ={}),
        )

    def test_a_local_identity_cannot_stand_in_for_a_network_peer(self) -> None:
        _, endpoint = self.fleet_inputs()
        local = PeerIdentity(caller_id=CALLER, auth_method=AUTH_METHOD_LOCAL_SERVICE)
        self.assert_refused(
            "lane_ambiguous",
            lambda: LaneResolution(
                lane=LANE_FLEET, identity=local, endpoint=endpoint, reason="forged"
            ),
        )

    def test_whois_cannot_describe_a_local_caller(self) -> None:
        identity, _ = self.fleet_inputs()
        self.assert_refused(
            "lane_ambiguous",
            lambda: LaneResolution(
                lane=LANE_LOCAL, identity=identity, endpoint=None, reason="forged"
            ),
        )

    def test_local_lane_cannot_claim_a_listener(self) -> None:
        _, endpoint = self.fleet_inputs()
        local = PeerIdentity(caller_id=CALLER, auth_method=AUTH_METHOD_PEERCRED)
        self.assert_refused(
            "lane_ambiguous",
            lambda: LaneResolution(
                lane=LANE_LOCAL, identity=local, endpoint=endpoint, reason="forged"
            ),
        )

    def test_unknown_lane_is_refused(self) -> None:
        local = PeerIdentity(caller_id=CALLER, auth_method=AUTH_METHOD_PEERCRED)
        self.assert_refused(
            "lane_unsupported",
            lambda: LaneResolution(
                lane="sideband", identity=local, endpoint=None, reason="forged"
            ),
        )

    def test_lanes_are_exactly_two(self) -> None:
        self.assertEqual({LANE_FLEET, LANE_LOCAL}, set(LANES))


class UnifiedAdmissionTests(LaneTestCase):
    """The local lane gets the fleet lane's gates, not a shortcut."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.clock = MutableClock()
        policy = OraclePolicy.from_mapping(
            {
                "schema": "skillbox.oracle-policy.v1",
                "callers": {
                    CALLER: {
                        "modes": ["standard", "deep-research"],
                        "max_prompt_bytes": 4_000,
                        "max_files": 4,
                        "max_attachment_bytes": 8_000,
                        "max_request_bytes": 12_000,
                        "max_concurrent": 2,
                        "max_requests_per_window": 2,
                        "max_bytes_per_window": 60_000,
                        "window_seconds": 60,
                        "max_runtime_seconds": 600,
                        "lease_grace_seconds": 10,
                    }
                },
            }
        )
        provision_oracle_policy_authority(
            policy, root / "policy", authority_directory=root / "authority"
        )
        self.engine = OraclePolicyEngine(
            policy,
            root / "policy",
            authority_directory=root / "authority",
            clock=self.clock,
        )
        self.guard = ReplayGuard(clock=self.clock)
        self.contacted: list[str] = []
        self.local = resolve_lane(state_root=self.state_root(), environ={})
        self.fleet = resolve_lane(
            transport_identity=peer_identity_from_whois(
                whois_document(), tag_allowlist=TAG_ALLOWLIST
            ),
            endpoint=validate_bind_endpoint("100.64.0.1", 8443),
            environ={},
        )

    def run_oracle(self, payload: object, resolution: object):
        with oracle_lane_admission(
            payload,
            resolution=resolution,
            policy_engine=self.engine,
            replay_guard=self.guard,
            clock=self.clock,
        ) as admission:
            self.contacted.append(admission.receipt.lane)
            return admission

    def test_local_request_is_admitted_and_receipted(self) -> None:
        admission = self.run_oracle(encode(request_document()), self.local)
        payload = admission.receipt.to_payload()
        self.assertEqual(LANE_LOCAL, payload["lane"])
        self.assertEqual(AUTH_METHOD_LOCAL_SERVICE, payload["auth_method"])
        self.assertEqual("", payload["endpoint"])
        self.assertEqual("", payload["scope"])
        self.assertEqual(CALLER, payload["caller_id"])

    def test_fleet_request_uses_the_same_receipt_shape(self) -> None:
        local = self.run_oracle(encode(request_document()), self.local)
        fleet = self.run_oracle(
            encode(request_document(nonce="1" * 32)), self.fleet
        )
        self.assertEqual(
            set(local.receipt.to_payload()), set(fleet.receipt.to_payload())
        )
        self.assertEqual(LANE_FLEET, fleet.receipt.to_payload()["lane"])
        self.assertEqual("100.64.0.1:8443", fleet.receipt.to_payload()["endpoint"])

    def test_local_lane_is_replay_defended(self) -> None:
        payload = encode(request_document())
        self.run_oracle(payload, self.local)
        self.assert_refused(
            "replay_detected", lambda: self.run_oracle(payload, self.local)
        )

    def test_local_lane_spends_the_same_caller_quota(self) -> None:
        # max_requests_per_window is 2 for this caller, and both lanes resolve
        # to the SAME caller id, so a local run must consume fleet quota.
        self.run_oracle(encode(request_document(nonce="1" * 32)), self.local)
        self.run_oracle(encode(request_document(nonce="2" * 32)), self.fleet)
        self.assert_refused(
            "request_quota_exceeded",
            lambda: self.run_oracle(encode(request_document(nonce="3" * 32)), self.local),
        )

    def test_local_lane_enforces_the_same_forbidden_fields(self) -> None:
        for resolution in (self.local, self.fleet):
            self.assert_refused(
                "browser_config_forbidden",
                lambda resolution=resolution: self.run_oracle(
                    encode(request_document(browserConfig={})), resolution
                ),
            )
        self.assertEqual([], self.contacted)

    def test_admission_without_a_resolved_lane_is_refused(self) -> None:
        for resolution in (None, "local", self.local.identity):
            self.assert_refused(
                "lane_unavailable",
                lambda resolution=resolution: self.run_oracle(
                    encode(request_document()), resolution
                ),
            )
        self.assertEqual([], self.contacted)


class SurfaceParityTests(LaneTestCase):
    """CLI, MCP dispatch, and in-process callers agree on one answer."""

    PARITY_KEYS = ("lane", "caller_id", "auth_method", "node", "reason", "scope")

    def clean_env(self, **overrides: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in IDENTITY_ENV_OVERRIDE_NAMES
        }
        env.update(overrides)
        return env

    def cli_payload(self, root: Path, env: dict[str, str] | None = None) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(MANAGE_PY),
                "oracle-lane",
                "--state-root",
                str(root),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            env=env or self.clean_env(),
            timeout=120,
        )
        return json.loads(result.stdout), result.returncode

    def test_cli_and_in_process_resolve_the_same_lane(self) -> None:
        root = self.state_root("devbox-1")
        payload, returncode = self.cli_payload(root)
        self.assertEqual(0, returncode)
        self.assertTrue(payload["ok"])
        standalone = resolve_lane(state_root=root, environ={}).to_payload()
        for key in self.PARITY_KEYS:
            self.assertEqual(standalone[key], payload[key], key)

    def test_mcp_script_dispatch_matches_the_cli(self) -> None:
        # The operator MCP server has no Oracle tool (the surface is frozen);
        # its script-dispatch contract is the MCP path a lane check travels.
        import operator_mcp_server as mcp

        root = self.state_root("devbox-1")
        ok, _code, data = mcp.run_script(
            MANAGE_PY,
            ["oracle-lane", "--state-root", str(root), "--format", "json"],
            timeout=120,
        )
        self.assertTrue(ok, data)
        cli, _returncode = self.cli_payload(root)
        for key in self.PARITY_KEYS:
            self.assertEqual(cli[key], data[key], key)

    def test_every_surface_refuses_an_unprovisioned_host_identically(self) -> None:
        import operator_mcp_server as mcp

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "state"

        payload, returncode = self.cli_payload(root)
        self.assertEqual(1, returncode)
        self.assertFalse(payload["ok"])
        self.assertEqual("identity_file_missing", payload["error"]["code"])

        ok, _code, data = mcp.run_script(
            MANAGE_PY,
            ["oracle-lane", "--state-root", str(root), "--format", "json"],
            timeout=120,
        )
        self.assertFalse(ok)
        self.assertEqual("identity_file_missing", data["error"]["code"])

        self.assert_refused(
            "identity_file_missing", lambda: resolve_lane(state_root=root, environ={})
        )

    def test_cli_refuses_an_environment_asserted_identity(self) -> None:
        root = self.state_root("devbox-1")
        env = self.clean_env(SKILLBOX_ORACLE_CALLER_ID="devbox-2")
        payload, returncode = self.cli_payload(root, env=env)
        self.assertEqual(1, returncode)
        self.assertEqual(
            "identity_env_override_forbidden", payload["error"]["code"]
        )
        self.assertIn("unset SKILLBOX_ORACLE_CALLER_ID", payload["next_actions"])

    def test_a_chrome_less_caller_resolves_the_same_lane(self) -> None:
        # Nothing in the lane contract may reach for a browser: with a PATH
        # holding no chrome/chromium at all, the answer must be unchanged.
        root = self.state_root("devbox-1")
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertEqual([], sorted(Path(empty.name).iterdir()))
        env = self.clean_env(PATH=empty.name)
        env["PATH"] = f"{empty.name}{os.pathsep}{os.path.dirname(sys.executable)}"

        payload, returncode = self.cli_payload(root, env=env)
        self.assertEqual(0, returncode)
        baseline, _ = self.cli_payload(root)
        for key in self.PARITY_KEYS:
            self.assertEqual(baseline[key], payload[key], key)

    def test_cli_never_writes_to_the_state_root(self) -> None:
        root = self.state_root("devbox-1")

        def snapshot() -> dict[str, tuple[int, int]]:
            return {
                str(path.relative_to(root)): (
                    path.stat().st_size,
                    stat.S_IMODE(path.stat().st_mode),
                )
                for path in sorted(root.rglob("*"))
            }

        before = snapshot()
        self.cli_payload(root)
        self.assertEqual(before, snapshot())


if __name__ == "__main__":
    unittest.main()
