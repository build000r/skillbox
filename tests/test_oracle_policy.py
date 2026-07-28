from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from runtime_manager.oracle_policy import (
    ORACLE_POLICY_SCHEMA,
    ORACLE_POLICY_STATE_SCHEMA,
    ORACLE_REQUEST_FACTS_SCHEMA,
    CallerPolicy,
    OraclePolicy,
    OraclePolicyEngine,
    OraclePolicyError,
    OracleRequestFacts,
    provision_oracle_policy_authority,
)


def caller_policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "modes": ["standard", "deep-research"],
        "max_prompt_bytes": 1_000,
        "max_files": 2,
        "max_attachment_bytes": 2_000,
        "max_request_bytes": 2_500,
        "max_concurrent": 1,
        "max_requests_per_window": 3,
        "max_bytes_per_window": 5_000,
        "window_seconds": 60,
        "max_runtime_seconds": 300,
        "lease_grace_seconds": 10,
    }
    value.update(overrides)
    return value


def policy_document(**callers: dict[str, object]) -> dict[str, object]:
    return {
        "schema": ORACLE_POLICY_SCHEMA,
        "callers": callers or {"local": caller_policy()},
    }


def request_facts(**overrides: object) -> OracleRequestFacts:
    value: dict[str, object] = {
        "schema": ORACLE_REQUEST_FACTS_SCHEMA,
        "mode": "standard",
        "prompt_bytes": 100,
        "file_count": 0,
        "attachment_bytes": 0,
        "timeout_seconds": 30,
    }
    value.update(overrides)
    return OracleRequestFacts.from_mapping(value)


def canonical_json(value: object) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


class MutableClock:
    def __init__(self, now: float = 1_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class OraclePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self.temporary.name).resolve()
        self.root = self.temporary_root / "oracle-policy"
        self.authority = self.temporary_root / "oracle-authority"
        self.clock = MutableClock()
        self.policy = OraclePolicy.from_mapping(policy_document())
        provision_oracle_policy_authority(
            self.policy,
            self.root,
            authority_directory=self.authority,
        )
        self.engine = OraclePolicyEngine(
            self.policy,
            self.root,
            authority_directory=self.authority,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_denied(self, code: str, action: object) -> None:
        with self.assertRaises(OraclePolicyError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), "oracle policy: denied")

    def provision_engine(
        self,
        policy: OraclePolicy,
        root: Path,
        *,
        authority: Path | None = None,
    ) -> OraclePolicyEngine:
        authority_path = authority or root.with_name(f"{root.name}-authority")
        provision_oracle_policy_authority(
            policy,
            root,
            authority_directory=authority_path,
        )
        return OraclePolicyEngine(
            policy,
            root,
            authority_directory=authority_path,
            clock=self.clock,
        )

    def test_config_and_request_schemas_are_exact_and_immutable(self) -> None:
        self.assert_denied(
            "policy_config_invalid",
            lambda: OraclePolicy.from_mapping(
                {**policy_document(), "default_allow": True}
            ),
        )
        self.assert_denied(
            "policy_config_invalid",
            lambda: OraclePolicy.from_mapping(
                policy_document(local=caller_policy(modes=["standard", "other"]))
            ),
        )
        self.assert_denied(
            "policy_config_invalid",
            lambda: OraclePolicy.from_mapping(
                policy_document(local=caller_policy(max_request_bytes=100))
            ),
        )
        self.assert_denied(
            "request_shape_invalid",
            lambda: OracleRequestFacts.from_mapping(
                {
                    "schema": ORACLE_REQUEST_FACTS_SCHEMA,
                    "mode": "standard",
                    "prompt_bytes": 1,
                    "file_count": 0,
                    "attachment_bytes": 0,
                    "timeout_seconds": 1,
                    "hook": "forbidden",
                }
            ),
        )
        self.assert_denied(
            "request_shape_invalid",
            lambda: request_facts(file_count=1, attachment_bytes=0),
        )
        self.assert_denied(
            "request_shape_invalid",
            lambda: request_facts(mode=[]),
        )
        self.assert_denied(
            "request_shape_invalid",
            lambda: OracleRequestFacts(
                mode="standard",
                prompt_bytes=-1_999,
                file_count=1,
                attachment_bytes=2_000,
                timeout_seconds=30,
            ),
        )
        invalid_direct_policy = OraclePolicy(
            callers={
                "local": CallerPolicy(
                    modes=frozenset({"standard"}),
                    **{
                        **{
                            key: value
                            for key, value in caller_policy().items()
                            if key != "modes"
                        },
                        "max_prompt_bytes": -1,
                    },
                )
            }
        )
        self.assert_denied(
            "policy_engine_invalid",
            lambda: OraclePolicyEngine(
                invalid_direct_policy,
                self.root,
                authority_directory=self.authority,
            ),
        )
        self.assert_denied(
            "caller_denied",
            lambda: self.engine.reserve([], request_facts()),  # type: ignore[arg-type]
        )
        with self.assertRaises(TypeError):
            self.policy.callers["rogue"] = self.policy.callers["local"]  # type: ignore[index]

    def test_request_subclasses_and_mutated_primitives_fail_closed(self) -> None:
        class ForgedRequest(OracleRequestFacts):
            @property
            def request_bytes(self) -> int:
                return 1

        forged = ForgedRequest(
            mode="standard",
            prompt_bytes=1_000,
            file_count=1,
            attachment_bytes=2_000,
            timeout_seconds=30,
        )
        self.assert_denied(
            "request_shape_invalid",
            lambda: self.engine.reserve("local", forged),
        )
        mutated = request_facts()
        object.__setattr__(mutated, "prompt_bytes", "100")
        self.assert_denied(
            "request_shape_invalid",
            lambda: self.engine.reserve("local", mutated),
        )
        missing = request_facts()
        object.__delattr__(missing, "timeout_seconds")
        self.assert_denied(
            "request_shape_invalid",
            lambda: self.engine.reserve("local", missing),
        )

    def test_request_aggregate_is_recomputed_without_trusting_property(self) -> None:
        oversized = request_facts(
            prompt_bytes=1_000,
            file_count=1,
            attachment_bytes=2_000,
        )
        with mock.patch.object(
            OracleRequestFacts,
            "request_bytes",
            new_callable=mock.PropertyMock,
            return_value=1,
        ):
            self.assert_denied(
                "request_too_large",
                lambda: self.engine.reserve("local", oversized),
            )

    def test_runtime_entropy_failures_are_stable_before_and_after_pending(
        self,
    ) -> None:
        browser_calls: list[str] = []
        initial_history = self.engine.authority_history_path.read_bytes()
        initial_head = self.engine.authority_head_path.read_bytes()
        initial_namespace = self.engine.namespace_path.read_bytes()
        with mock.patch(
            "runtime_manager.oracle_policy.secrets.token_hex",
            side_effect=RuntimeError("raw reservation entropy failure"),
        ):
            self.assert_denied(
                "reservation_id_failed",
                lambda: self._browser_action(browser_calls),
            )
        self.assertEqual(
            self.engine.authority_history_path.read_bytes(), initial_history
        )
        self.assertEqual(self.engine.authority_head_path.read_bytes(), initial_head)
        self.assertEqual(self.engine.namespace_path.read_bytes(), initial_namespace)
        self.assertFalse(self.engine.state_path.exists())
        self.assertEqual(browser_calls, [])

        with mock.patch(
            "runtime_manager.oracle_policy.secrets.token_hex",
            return_value="malformed-token",
        ):
            self.assert_denied(
                "reservation_id_failed",
                lambda: self._browser_action(browser_calls),
            )
        self.assertEqual(
            self.engine.authority_history_path.read_bytes(), initial_history
        )
        self.assertEqual(self.engine.authority_head_path.read_bytes(), initial_head)
        self.assertEqual(self.engine.namespace_path.read_bytes(), initial_namespace)
        self.assertFalse(self.engine.state_path.exists())
        self.assertEqual(browser_calls, [])

        with mock.patch(
            "runtime_manager.oracle_policy.secrets.token_hex",
            side_effect=[
                "1" * 32,
                RuntimeError("raw temporary-name entropy failure"),
            ],
        ):
            self.assert_denied(
                "state_write_failed",
                lambda: self._browser_action(browser_calls),
            )
        authority_tail = json.loads(
            self.engine.authority_history_path.read_bytes().splitlines()[-1]
        )
        namespace = json.loads(self.engine.namespace_path.read_bytes())
        self.assertEqual(authority_tail["phase"], "pending")
        self.assertIsNotNone(namespace["pending_head"])
        self.assertFalse(self.engine.state_path.exists())
        self.assert_denied(
            "state_transition_incomplete",
            lambda: self._browser_action(browser_calls),
        )
        self.assert_denied(
            "state_transition_incomplete",
            lambda: OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

    def test_reservation_entropy_retry_failure_is_stable_before_pending(self) -> None:
        retry_policy = OraclePolicy.from_mapping(
            policy_document(
                local=caller_policy(
                    max_concurrent=2,
                )
            )
        )
        root = self.temporary_root / "reservation-retry-state"
        authority = self.temporary_root / "reservation-retry-authority"
        engine = self.provision_engine(
            retry_policy,
            root,
            authority=authority,
        )
        first = engine.reserve("local", request_facts())
        history = engine.authority_history_path.read_bytes()
        head = engine.authority_head_path.read_bytes()
        namespace = engine.namespace_path.read_bytes()
        state = engine.state_path.read_bytes()
        browser_calls: list[str] = []

        def retry_browser() -> None:
            with engine.admission("local", request_facts()):
                browser_calls.append("reservation-retry-granted")

        with mock.patch(
            "runtime_manager.oracle_policy.secrets.token_hex",
            side_effect=[
                first.reservation_id,
                RuntimeError("raw retry entropy failure"),
            ],
        ):
            self.assert_denied("reservation_id_failed", retry_browser)
        self.assertEqual(engine.authority_history_path.read_bytes(), history)
        self.assertEqual(engine.authority_head_path.read_bytes(), head)
        self.assertEqual(engine.namespace_path.read_bytes(), namespace)
        self.assertEqual(engine.state_path.read_bytes(), state)
        self.assertEqual(browser_calls, [])

    def test_unknown_caller_and_every_static_limit_deny(self) -> None:
        cases = [
            ("caller_denied", "rogue", request_facts()),
            ("mode_denied", "local", request_facts(mode="deep-research")),
            ("prompt_too_large", "local", request_facts(prompt_bytes=1_001)),
            (
                "file_count_exceeded",
                "local",
                request_facts(file_count=3, attachment_bytes=3),
            ),
            (
                "attachment_bytes_exceeded",
                "local",
                request_facts(file_count=1, attachment_bytes=2_001),
            ),
            (
                "request_too_large",
                "local",
                request_facts(
                    prompt_bytes=1_000,
                    file_count=1,
                    attachment_bytes=2_000,
                ),
            ),
            ("runtime_exceeded", "local", request_facts(timeout_seconds=301)),
        ]
        standard_only = OraclePolicy.from_mapping(
            policy_document(local=caller_policy(modes=["standard"]))
        )
        static_root = self.temporary_root / "static-policy"
        self.engine = self.provision_engine(
            standard_only,
            static_root,
        )
        for code, caller, facts in cases:
            with self.subTest(code=code):
                self.assert_denied(
                    code,
                    lambda caller=caller, facts=facts: self.engine.reserve(
                        caller, facts
                    ),
                )

    def test_denial_happens_before_browser_callback_and_release_is_finally_safe(
        self,
    ) -> None:
        browser_calls: list[str] = []
        first = self.engine.reserve("local", request_facts())
        self.assert_denied(
            "concurrency_exceeded",
            lambda: self._browser_action(browser_calls),
        )
        self.assertEqual(browser_calls, [])
        self.assertTrue(self.engine.release("local", first.reservation_id))
        with self.assertRaisesRegex(RuntimeError, "synthetic browser failure"):
            with self.engine.admission("local", request_facts()):
                browser_calls.append("entered")
                raise RuntimeError("synthetic browser failure")
        self.assertEqual(browser_calls, ["entered"])
        with self.engine.admission("local", request_facts()) as grant:
            self.assertEqual(grant.caller_id, "local")

    def _browser_action(self, browser_calls: list[str]) -> None:
        with self.engine.admission("local", request_facts()):
            browser_calls.append("browser-contact")

    def test_rolling_request_and_byte_quotas_survive_release(self) -> None:
        byte_policy = OraclePolicy.from_mapping(
            policy_document(
                local=caller_policy(
                    max_prompt_bytes=300,
                    max_files=0,
                    max_attachment_bytes=0,
                    max_request_bytes=300,
                    max_concurrent=2,
                    max_requests_per_window=2,
                    max_bytes_per_window=300,
                )
            )
        )
        engine = self.provision_engine(
            byte_policy,
            self.temporary_root / "byte-policy",
        )
        one = engine.reserve("local", request_facts(prompt_bytes=150))
        engine.release("local", one.reservation_id)
        two = engine.reserve("local", request_facts(prompt_bytes=150))
        engine.release("local", two.reservation_id)
        self.assert_denied(
            "request_quota_exceeded",
            lambda: engine.reserve("local", request_facts(prompt_bytes=1)),
        )
        self.clock.now += 61
        grant = engine.reserve("local", request_facts(prompt_bytes=300))
        engine.release("local", grant.reservation_id)
        self.assert_denied(
            "byte_quota_exceeded",
            lambda: engine.reserve("local", request_facts(prompt_bytes=1)),
        )

    def test_concurrent_engines_share_one_threaded_reservation(self) -> None:
        engines = [
            OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=self.authority,
                clock=self.clock,
            )
            for _ in range(8)
        ]
        barrier = threading.Barrier(len(engines))
        grants: list[object] = []
        errors: list[str] = []
        result_lock = threading.Lock()

        def contender(engine: OraclePolicyEngine) -> None:
            barrier.wait()
            try:
                result = engine.reserve("local", request_facts())
                with result_lock:
                    grants.append(result)
            except OraclePolicyError as error:
                with result_lock:
                    errors.append(error.code)

        threads = [
            threading.Thread(target=contender, args=(engine,)) for engine in engines
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(grants), 1)
        self.assertEqual(errors, ["concurrency_exceeded"] * 7)

    def test_concurrent_processes_share_one_reservation(self) -> None:
        process_root = self.temporary_root / "process-policy"
        process_authority = self.temporary_root / "process-authority"
        provision_oracle_policy_authority(
            self.policy,
            process_root,
            authority_directory=process_authority,
        )
        module_root = str(Path(__file__).resolve().parents[1] / ".env-manager")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (module_root, environment.get("PYTHONPATH")))
        )
        contender = """
import json
import sys
from runtime_manager.oracle_policy import (
    ORACLE_REQUEST_FACTS_SCHEMA,
    OraclePolicy,
    OraclePolicyEngine,
    OraclePolicyError,
    OracleRequestFacts,
)

policy = OraclePolicy.from_mapping(json.loads(sys.argv[3]))
engine = OraclePolicyEngine(
    policy,
    sys.argv[1],
    authority_directory=sys.argv[2],
    lock_timeout_seconds=5,
)
request = OracleRequestFacts.from_mapping({
    "schema": ORACLE_REQUEST_FACTS_SCHEMA,
    "mode": "standard",
    "prompt_bytes": 100,
    "file_count": 0,
    "attachment_bytes": 0,
    "timeout_seconds": 30,
})
try:
    engine.reserve("local", request)
except OraclePolicyError as error:
    print(error.code)
else:
    print("granted")
"""
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    contender,
                    str(process_root),
                    str(process_authority),
                    json.dumps(policy_document()),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        results: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stderr, "")
            results.append(stdout.strip())
        self.assertEqual(results.count("granted"), 1)
        self.assertEqual(results.count("concurrency_exceeded"), 7)

    def test_expired_reservation_recovers_but_clock_rollback_fails_closed(
        self,
    ) -> None:
        self.engine.reserve("local", request_facts(timeout_seconds=1))
        self.clock.now += 12
        replacement = self.engine.reserve("local", request_facts())
        self.assertIsNotNone(replacement)
        self.clock.now -= 1
        self.assert_denied(
            "clock_rollback",
            lambda: self.engine.release("local", replacement.reservation_id),
        )

    def test_state_is_private_minimal_and_corruption_fails_closed(self) -> None:
        grant = self.engine.reserve("local", request_facts())
        state_path = self.root / "policy-state.json"
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.engine.namespace_path.parent.stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(self.engine.namespace_path.stat().st_mode),
            0o600,
        )
        self.assertEqual(self.engine.namespace_path.stat().st_nlink, 1)
        serialized = state_path.read_text(encoding="utf-8")
        serialized_namespace = self.engine.namespace_path.read_text(encoding="utf-8")
        self.assertNotRegex(
            serialized + serialized_namespace,
            r"prompt|attachment_path|cookie|token|secret|hook|environment|cdp",
        )
        state = json.loads(serialized)
        self.assertEqual(
            set(state),
            {
                "schema",
                "policy_fingerprint",
                "namespace_generation",
                "revision",
                "last_seen_at",
                "callers",
            },
        )
        namespace = json.loads(serialized_namespace)
        self.assertEqual(
            set(namespace),
            {
                "schema",
                "policy_fingerprint",
                "authority_generation",
                "authority_sequence",
                "generation",
                "state_head",
                "pending_head",
                "state_directory",
                "parent_device",
                "parent_inode",
                "anchor_device",
                "anchor_inode",
                "state_device",
                "state_inode",
            },
        )
        authority_head = json.loads(self.engine.authority_head_path.read_bytes())
        authority_history = self.engine.authority_history_path.read_bytes().splitlines()
        self.assertEqual(authority_head["sequence"], namespace["authority_sequence"])
        self.assertEqual(len(authority_history), authority_head["sequence"] + 1)
        self.assertEqual(
            authority_head["entry_hash"],
            hashlib.sha256(authority_history[-1] + b"\n").hexdigest(),
        )
        self.assertEqual(state["namespace_generation"], namespace["generation"])
        self.assertIsNone(namespace["pending_head"])
        self.assertEqual(namespace["state_head"]["revision"], state["revision"])
        self.assertEqual(
            namespace["state_head"]["sha256"],
            hashlib.sha256(serialized.encode("ascii")).hexdigest(),
        )
        self.assertEqual(namespace["state_directory"], str(self.root))
        state_path.write_text(
            (
                '{"schema":"tampered",'
                f'"schema":"{state["schema"]}",'
                f'"policy_fingerprint":"{state["policy_fingerprint"]}",'
                f'"last_seen_at":{state["last_seen_at"]},"callers":{{}}}}\n'
            ),
            encoding="utf-8",
        )
        os.chmod(state_path, 0o600)
        self.assert_denied(
            "state_corrupt",
            lambda: self.engine.release("local", grant.reservation_id),
        )
        state_path.write_text("{broken", encoding="utf-8")
        os.chmod(state_path, 0o600)
        self.assert_denied(
            "state_corrupt",
            lambda: self.engine.release("local", grant.reservation_id),
        )

    def test_enrollment_precedes_first_state_and_missing_published_state_denies(
        self,
    ) -> None:
        browser_calls: list[str] = []
        self.assertFalse(self.engine.state_path.exists())
        self.assertTrue(self.engine.namespace_path.exists())
        self.assertTrue(self.engine.authority_manifest_path.exists())
        self.assertTrue(self.engine.authority_history_path.exists())
        grant = self.engine.reserve("local", request_facts())
        self.assertTrue(self.engine.state_path.exists())
        self.assertTrue(self.engine.namespace_path.exists())
        self.engine.state_path.unlink()
        self.assert_denied(
            "state_file_unsafe",
            lambda: self._browser_action(browser_calls),
        )
        self.assertEqual(browser_calls, [])
        self.assert_denied(
            "state_file_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assertIsNotNone(grant.reservation_id)

    def test_explicit_enrollment_is_exactly_once_and_restart_uses_authority(
        self,
    ) -> None:
        install_parent = self.temporary_root / "fresh-install"
        authority_parent = self.temporary_root / "fresh-authority"
        root = install_parent / "oracle-policy"
        authority = authority_parent / "oracle-authority"
        self.assert_denied(
            "authority_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                root,
                authority_directory=authority,
                clock=self.clock,
            ),
        )
        self.assertFalse(install_parent.exists())
        self.assertFalse(authority_parent.exists())

        provision_oracle_policy_authority(
            self.policy,
            root,
            authority_directory=authority,
        )
        self.assert_denied(
            "authority_already_enrolled",
            lambda: provision_oracle_policy_authority(
                self.policy,
                root,
                authority_directory=authority,
            ),
        )
        first_engine = OraclePolicyEngine(
            self.policy,
            root,
            authority_directory=authority,
            clock=self.clock,
        )
        grant = first_engine.reserve("local", request_facts())
        self.assertTrue(first_engine.release("local", grant.reservation_id))
        restarted = OraclePolicyEngine(
            self.policy,
            root,
            authority_directory=authority,
            clock=self.clock,
        )
        self.assertIsInstance(restarted, OraclePolicyEngine)
        self.assertEqual(
            restarted._authority_floor_sequence,  # noqa: SLF001
            json.loads(restarted.authority_head_path.read_bytes())["sequence"],
        )
        restarted.namespace_path.unlink()
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                root,
                authority_directory=authority,
                clock=self.clock,
            ),
        )
        self.assertFalse(restarted.namespace_path.exists())
        self.assert_denied(
            "authority_already_enrolled",
            lambda: provision_oracle_policy_authority(
                self.policy,
                root,
                authority_directory=authority,
            ),
        )
        self.assertFalse(restarted.namespace_path.exists())

    def test_enrollment_entropy_failures_are_stable_partial_bootstraps(
        self,
    ) -> None:
        cases = [
            (
                "authority-generation",
                RuntimeError("raw authority entropy failure"),
                "authority_generation_failed",
            ),
            (
                "namespace-generation",
                [
                    "a" * 64,
                    RuntimeError("raw namespace entropy failure"),
                ],
                "namespace_generation_failed",
            ),
        ]
        for label, side_effect, code in cases:
            with self.subTest(label=label):
                root = self.temporary_root / f"{label}-state"
                authority = self.temporary_root / f"{label}-authority"
                with mock.patch(
                    "runtime_manager.oracle_policy.secrets.token_hex",
                    side_effect=side_effect,
                ):
                    self.assert_denied(
                        code,
                        lambda: provision_oracle_policy_authority(
                            self.policy,
                            root,
                            authority_directory=authority,
                        ),
                    )
                self.assertTrue(root.is_dir())
                self.assertTrue(authority.is_dir())
                self.assertTrue((authority / "authority.json").exists())
                self.assertEqual((authority / "authority.json").stat().st_size, 0)
                browser_calls: list[str] = []

                def partial_bootstrap_browser() -> None:
                    engine = OraclePolicyEngine(
                        self.policy,
                        root,
                        authority_directory=authority,
                        clock=self.clock,
                    )
                    with engine.admission("local", request_facts()):
                        browser_calls.append("partial-bootstrap-granted")

                self.assert_denied(
                    "authority_corrupt",
                    partial_bootstrap_browser,
                )
                self.assert_denied(
                    "authority_already_enrolled",
                    lambda: provision_oracle_policy_authority(
                        self.policy,
                        root,
                        authority_directory=authority,
                    ),
                )
                self.assertEqual(browser_calls, [])

    def test_monotonic_state_head_rejects_canonical_reset_and_content_tamper(
        self,
    ) -> None:
        browser_calls: list[str] = []
        grant = self.engine.reserve("local", request_facts())
        revision_one = self.engine.state_path.read_bytes()
        self.assertTrue(self.engine.release("local", grant.reservation_id))
        revision_two = self.engine.state_path.read_bytes()
        state_two = json.loads(revision_two)
        namespace_two = json.loads(self.engine.namespace_path.read_bytes())
        self.assertEqual(state_two["revision"], 2)
        self.assertEqual(namespace_two["state_head"]["revision"], 2)
        self.assertEqual(
            namespace_two["state_head"]["sha256"],
            hashlib.sha256(revision_two).hexdigest(),
        )

        self.engine.state_path.write_bytes(revision_one)
        os.chmod(self.engine.state_path, 0o600)
        self.assert_denied(
            "state_corrupt",
            lambda: self._browser_action(browser_calls),
        )
        self.assertEqual(browser_calls, [])

        tampered = json.loads(revision_two)
        tampered["last_seen_at"] += 1
        self.engine.state_path.write_text(canonical_json(tampered), encoding="ascii")
        os.chmod(self.engine.state_path, 0o600)
        self.assert_denied(
            "state_corrupt",
            lambda: self._browser_action(browser_calls),
        )
        self.assertEqual(browser_calls, [])

    def test_authority_rejects_coherent_state_witness_rewrite_and_pair_rollback(
        self,
    ) -> None:
        quota_policy = OraclePolicy.from_mapping(
            policy_document(
                local=caller_policy(
                    max_requests_per_window=1,
                )
            )
        )
        rewrite_root = self.temporary_root / "coherent-rewrite"
        rewrite_authority = self.temporary_root / "coherent-rewrite-authority"
        rewrite_engine = self.provision_engine(
            quota_policy,
            rewrite_root,
            authority=rewrite_authority,
        )
        first = rewrite_engine.reserve("local", request_facts())
        self.assertTrue(rewrite_engine.release("local", first.reservation_id))
        self.assert_denied(
            "request_quota_exceeded",
            lambda: rewrite_engine.reserve("local", request_facts()),
        )

        rewritten_state = json.loads(rewrite_engine.state_path.read_bytes())
        rewritten_state["callers"] = {}
        rewritten_payload = canonical_json(rewritten_state).encode("ascii")
        rewritten_namespace = json.loads(rewrite_engine.namespace_path.read_bytes())
        rewritten_namespace["state_head"]["sha256"] = hashlib.sha256(
            rewritten_payload
        ).hexdigest()
        rewrite_engine.state_path.write_bytes(rewritten_payload)
        os.chmod(rewrite_engine.state_path, 0o600)
        rewrite_engine.namespace_path.write_text(
            canonical_json(rewritten_namespace),
            encoding="ascii",
        )
        os.chmod(rewrite_engine.namespace_path, 0o600)
        browser_calls: list[str] = []

        def rewritten_browser() -> None:
            with rewrite_engine.admission("local", request_facts()):
                browser_calls.append("rewrite-granted")

        self.assert_denied("authority_corrupt", rewritten_browser)
        self.assert_denied(
            "authority_corrupt",
            lambda: OraclePolicyEngine(
                quota_policy,
                rewrite_root,
                authority_directory=rewrite_authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

        rollback_root = self.temporary_root / "coherent-rollback"
        rollback_authority = self.temporary_root / "coherent-rollback-authority"
        rollback_engine = self.provision_engine(
            quota_policy,
            rollback_root,
            authority=rollback_authority,
        )
        original_namespace = rollback_engine.namespace_path.read_bytes()
        grant = rollback_engine.reserve("local", request_facts())
        self.assertTrue(rollback_engine.release("local", grant.reservation_id))
        rollback_engine.state_path.unlink()
        rollback_engine.namespace_path.write_bytes(original_namespace)
        os.chmod(rollback_engine.namespace_path, 0o600)

        def rolled_back_browser() -> None:
            with rollback_engine.admission("local", request_facts()):
                browser_calls.append("rollback-granted")

        self.assert_denied("authority_corrupt", rolled_back_browser)
        self.assert_denied(
            "authority_corrupt",
            lambda: OraclePolicyEngine(
                quota_policy,
                rollback_root,
                authority_directory=rollback_authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

    def test_pretty_printed_state_is_rejected_before_browser(self) -> None:
        browser_calls: list[str] = []
        self.engine.reserve("local", request_facts())
        state = json.loads(self.engine.state_path.read_bytes())
        self.engine.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="ascii",
        )
        os.chmod(self.engine.state_path, 0o600)
        self.assert_denied(
            "state_corrupt",
            lambda: self._browser_action(browser_calls),
        )
        self.assertEqual(browser_calls, [])

    def test_state_publication_orders_pending_state_then_committed_head(self) -> None:
        first = self.engine.reserve("local", request_facts())
        self.assertTrue(self.engine.release("local", first.reservation_id))
        order: list[str] = []
        append_authority = (
            self.engine._append_authority_transition  # noqa: SLF001
        )
        write_namespace = self.engine._write_namespace  # noqa: SLF001
        write_state = self.engine._write_state  # noqa: SLF001

        def record_authority(
            locked: object,
            *,
            phase: str,
            binding: object,
            namespace_payload: bytes,
        ) -> None:
            order.append(f"authority-{phase}")
            append_authority(
                locked,  # type: ignore[arg-type]
                phase=phase,
                binding=binding,  # type: ignore[arg-type]
                namespace_payload=namespace_payload,
            )

        def record_namespace(
            locked: object,
            binding: object,
            authority_sequence: int,
        ) -> bytes:
            pending_head = getattr(binding, "pending_head")
            phase = "pending" if pending_head is not None else "committed"
            order.append(f"namespace-{phase}")
            return write_namespace(
                locked,  # type: ignore[arg-type]
                binding,  # type: ignore[arg-type]
                authority_sequence,
            )

        def record_state(
            payload: bytes,
            descriptor: int,
            *,
            replace: bool,
        ) -> None:
            order.append("state")
            write_state(payload, descriptor, replace=replace)

        with (
            mock.patch.object(
                self.engine,
                "_append_authority_transition",
                side_effect=record_authority,
            ),
            mock.patch.object(
                self.engine,
                "_write_namespace",
                side_effect=record_namespace,
            ),
            mock.patch.object(
                self.engine,
                "_write_state",
                side_effect=record_state,
            ),
        ):
            self.engine.reserve("local", request_facts())
        self.assertEqual(
            order,
            [
                "authority-pending",
                "namespace-pending",
                "state",
                "namespace-committed",
                "authority-committed",
            ],
        )

    def test_interrupted_state_publications_remain_fail_closed(self) -> None:
        browser_calls: list[str] = []
        first = self.engine.reserve("local", request_facts())
        self.assertTrue(self.engine.release("local", first.reservation_id))
        prior_state = self.engine.state_path.read_bytes()
        prior_namespace = self.engine.namespace_path.read_bytes()

        def crash_before_state(
            _payload: bytes,
            _descriptor: int,
            *,
            replace: bool,
        ) -> None:
            self.assertTrue(replace)
            raise OraclePolicyError("state_write_failed")

        with mock.patch.object(
            self.engine,
            "_write_state",
            side_effect=crash_before_state,
        ):
            self.assert_denied(
                "state_write_failed",
                lambda: self.engine.reserve("local", request_facts()),
            )
        pending = json.loads(self.engine.namespace_path.read_bytes())
        self.assertIsNotNone(pending["pending_head"])
        self.assertTrue(self.engine.state_path.exists())
        self.engine.state_path.write_bytes(prior_state)
        os.chmod(self.engine.state_path, 0o600)
        self.engine.namespace_path.write_bytes(prior_namespace)
        os.chmod(self.engine.namespace_path, 0o600)
        self.assert_denied(
            "state_transition_incomplete",
            lambda: self._browser_action(browser_calls),
        )
        self.assert_denied(
            "state_transition_incomplete",
            lambda: OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

        commit_root = self.temporary_root / "commit-crash"
        commit_engine = self.provision_engine(
            self.policy,
            commit_root,
        )
        commit_authority = commit_root.with_name(f"{commit_root.name}-authority")
        committed = commit_engine.reserve("local", request_facts())
        self.assertTrue(commit_engine.release("local", committed.reservation_id))
        prior_commit_state = commit_engine.state_path.read_bytes()
        prior_commit_namespace = commit_engine.namespace_path.read_bytes()
        original_append_authority = (
            commit_engine._append_authority_transition  # noqa: SLF001
        )

        def crash_before_commit(
            locked: object,
            *,
            phase: str,
            binding: object,
            namespace_payload: bytes,
        ) -> None:
            if phase == "committed":
                raise OraclePolicyError("state_write_failed")
            original_append_authority(
                locked,  # type: ignore[arg-type]
                phase=phase,
                binding=binding,  # type: ignore[arg-type]
                namespace_payload=namespace_payload,
            )

        with mock.patch.object(
            commit_engine,
            "_append_authority_transition",
            side_effect=crash_before_commit,
        ):
            self.assert_denied(
                "state_write_failed",
                lambda: commit_engine.reserve("local", request_facts()),
            )
        pending_after_state = json.loads(commit_engine.namespace_path.read_bytes())
        self.assertIsNone(pending_after_state["pending_head"])
        self.assertTrue(commit_engine.state_path.exists())
        commit_engine.state_path.write_bytes(prior_commit_state)
        os.chmod(commit_engine.state_path, 0o600)
        commit_engine.namespace_path.write_bytes(prior_commit_namespace)
        os.chmod(commit_engine.namespace_path, 0o600)

        def crashed_commit_browser() -> None:
            with commit_engine.admission("local", request_facts()):
                browser_calls.append("browser-contact")

        self.assert_denied(
            "state_transition_incomplete",
            crashed_commit_browser,
        )
        self.assert_denied(
            "state_transition_incomplete",
            lambda: OraclePolicyEngine(
                self.policy,
                commit_root,
                authority_directory=commit_authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

    def test_intermediate_directory_symlink_and_noncanonical_path_are_rejected(
        self,
    ) -> None:
        real_parent = self.temporary_root / "real-parent"
        real_parent.mkdir(mode=0o700)
        alias = self.temporary_root / "parent-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        self.assert_denied(
            "authority_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                alias / "oracle-policy",
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assertFalse((real_parent / "oracle-policy").exists())
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                real_parent / ".." / "other" / "oracle-policy",
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(  # type: ignore[arg-type]
                self.policy,
                None,
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                self.temporary_root / ".oracle-policy-namespaces",
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                f"{self.temporary_root}/nul\x00path",
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "state_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                f"{self.temporary_root}/surrogate\ud800path",
                authority_directory=self.authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "authority_directory_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=f"{self.temporary_root}/surrogate\ud800authority",
                clock=self.clock,
            ),
        )
        with mock.patch(
            "runtime_manager.oracle_policy.os.open",
            side_effect=ValueError("synthetic OS boundary failure"),
        ):
            self.assert_denied(
                "authority_directory_unsafe",
                lambda: OraclePolicyEngine(
                    self.policy,
                    self.temporary_root / "os-error",
                    authority_directory=self.authority,
                    clock=self.clock,
                ),
            )

    def test_raw_lexical_state_and_authority_path_aliases_are_rejected(
        self,
    ) -> None:
        state_parent = os.fspath(self.root.parent)
        state_name = self.root.name
        authority_parent = os.fspath(self.authority.parent)
        authority_name = self.authority.name
        original_head = self.engine.authority_head_path.read_bytes()
        state_aliases: list[object] = [
            f"{state_parent}/./{state_name}",
            f"{state_parent}//{state_name}",
            f"{self.root}/",
            f"{self.root}/../{state_name}",
            os.fsencode(self.root),
            "relative/oracle-policy",
            os.sep,
        ]
        for alias in state_aliases:
            with self.subTest(domain="state", alias_type=type(alias).__name__):
                self.assert_denied(
                    "state_directory_unsafe",
                    lambda alias=alias: OraclePolicyEngine(  # type: ignore[arg-type]
                        self.policy,
                        alias,
                        authority_directory=self.authority,
                        clock=self.clock,
                    ),
                )

        authority_aliases: list[object] = [
            f"{authority_parent}/./{authority_name}",
            f"{authority_parent}//{authority_name}",
            f"{self.authority}/",
            f"{self.authority}/../{authority_name}",
            os.fsencode(self.authority),
            "relative/oracle-authority",
            os.sep,
        ]
        for alias in authority_aliases:
            with self.subTest(domain="authority", alias_type=type(alias).__name__):
                self.assert_denied(
                    "authority_directory_unsafe",
                    lambda alias=alias: OraclePolicyEngine(  # type: ignore[arg-type]
                        self.policy,
                        self.root,
                        authority_directory=alias,
                        clock=self.clock,
                    ),
                )
        self.assertEqual(
            self.engine.authority_head_path.read_bytes(),
            original_head,
        )

    def test_close_and_monotonic_failures_are_stable_before_browser(self) -> None:
        browser_calls: list[str] = []
        real_close = os.close
        close_failed = False

        def close_then_fail(descriptor: int) -> None:
            nonlocal close_failed
            real_close(descriptor)
            if not close_failed:
                close_failed = True
                raise OSError("synthetic close failure")

        with mock.patch(
            "runtime_manager.oracle_policy.os.close",
            side_effect=close_then_fail,
        ):
            self.assert_denied(
                "authority_directory_unsafe",
                lambda: self._browser_action(browser_calls),
            )
        self.assertTrue(close_failed)
        self.assertEqual(browser_calls, [])

        with mock.patch(
            "runtime_manager.oracle_policy.time.monotonic",
            side_effect=RuntimeError("synthetic monotonic failure"),
        ):
            self.assert_denied(
                "state_lock_failed",
                lambda: self._browser_action(browser_calls),
            )
        self.assertEqual(browser_calls, [])

    def test_namespace_witness_corruption_links_and_modes_are_rejected(self) -> None:
        grant = self.engine.reserve("local", request_facts())
        original_witness = self.engine.namespace_path.read_text(encoding="utf-8")
        self.engine.namespace_path.write_text(
            '{"schema":"first","schema":"second"}\n',
            encoding="utf-8",
        )
        self.assert_denied(
            "state_directory_unsafe",
            lambda: self.engine.release("local", grant.reservation_id),
        )
        self.engine.namespace_path.write_text(original_witness, encoding="utf-8")
        witness_alias = self.temporary_root / "witness-hardlink.json"
        os.link(self.engine.namespace_path, witness_alias)
        self.assert_denied(
            "state_directory_unsafe",
            lambda: self.engine.release("local", grant.reservation_id),
        )
        witness_alias.unlink()
        os.chmod(self.engine.namespace_path, 0o644)
        self.assert_denied(
            "state_directory_unsafe",
            lambda: self.engine.release("local", grant.reservation_id),
        )
        os.chmod(self.engine.namespace_path, 0o600)
        os.chmod(self.engine.namespace_path.parent, 0o755)
        self.assert_denied(
            "state_directory_unsafe",
            lambda: self.engine.release("local", grant.reservation_id),
        )
        os.chmod(self.engine.namespace_path.parent, 0o700)
        witness_copy = self.temporary_root / "witness-copy.json"
        witness_copy.write_bytes(self.engine.namespace_path.read_bytes())
        os.chmod(witness_copy, 0o600)
        self.engine.namespace_path.unlink()
        self.engine.namespace_path.symlink_to(witness_copy)
        self.assert_denied(
            "state_directory_unsafe",
            lambda: self.engine.release("local", grant.reservation_id),
        )

    def test_authority_record_loss_and_corruption_deny_before_browser(self) -> None:
        browser_calls: list[str] = []
        loss_root = self.temporary_root / "authority-loss-state"
        loss_authority = self.temporary_root / "authority-loss"
        loss_engine = self.provision_engine(
            self.policy,
            loss_root,
            authority=loss_authority,
        )
        loss_engine.authority_manifest_path.unlink()

        def lost_authority_browser() -> None:
            with loss_engine.admission("local", request_facts()):
                browser_calls.append("loss-granted")

        self.assert_denied("authority_file_unsafe", lost_authority_browser)
        self.assert_denied(
            "authority_file_unsafe",
            lambda: OraclePolicyEngine(
                self.policy,
                loss_root,
                authority_directory=loss_authority,
                clock=self.clock,
            ),
        )

        corrupt_root = self.temporary_root / "authority-corrupt-state"
        corrupt_authority = self.temporary_root / "authority-corrupt"
        corrupt_engine = self.provision_engine(
            self.policy,
            corrupt_root,
            authority=corrupt_authority,
        )
        corrupt_engine.authority_head_path.write_bytes(b"{broken\n")
        os.chmod(corrupt_engine.authority_head_path, 0o600)

        def corrupt_authority_browser() -> None:
            with corrupt_engine.admission("local", request_facts()):
                browser_calls.append("corrupt-granted")

        self.assert_denied("authority_corrupt", corrupt_authority_browser)
        self.assert_denied(
            "authority_corrupt",
            lambda: OraclePolicyEngine(
                self.policy,
                corrupt_root,
                authority_directory=corrupt_authority,
                clock=self.clock,
            ),
        )
        self.assertEqual(browser_calls, [])

    def test_authority_valid_prefix_rollback_denies_current_and_fresh_engine(
        self,
    ) -> None:
        root = self.temporary_root / "authority-prefix-state"
        authority = self.temporary_root / "authority-prefix"
        engine = self.provision_engine(
            self.policy,
            root,
            authority=authority,
        )
        first = engine.reserve("local", request_facts())
        self.assertTrue(engine.release("local", first.reservation_id))
        earlier_history = engine.authority_history_path.read_bytes()
        earlier_head = engine.authority_head_path.read_bytes()
        second = engine.reserve("local", request_facts())
        self.assertTrue(engine.release("local", second.reservation_id))
        engine.authority_history_path.write_bytes(earlier_history)
        os.chmod(engine.authority_history_path, 0o600)
        engine.authority_head_path.write_bytes(earlier_head)
        os.chmod(engine.authority_head_path, 0o600)
        browser_calls: list[str] = []

        def current_engine_browser() -> None:
            with engine.admission("local", request_facts()):
                browser_calls.append("current-prefix-granted")

        self.assert_denied("authority_rollback", current_engine_browser)

        def fresh_engine_browser() -> None:
            fresh = OraclePolicyEngine(
                self.policy,
                root,
                authority_directory=authority,
                clock=self.clock,
            )
            with fresh.admission("local", request_facts()):
                browser_calls.append("fresh-prefix-granted")

        self.assert_denied("authority_corrupt", fresh_engine_browser)
        self.assertEqual(browser_calls, [])

    def test_authority_identity_replacement_denies_fresh_engine(self) -> None:
        root = self.temporary_root / "authority-replacement-state"
        authority = self.temporary_root / "authority-replacement"
        engine = self.provision_engine(
            self.policy,
            root,
            authority=authority,
        )
        first = engine.reserve("local", request_facts())
        self.assertTrue(engine.release("local", first.reservation_id))
        moved_authority = authority.with_name(f"{authority.name}.original")
        authority.rename(moved_authority)
        shutil.copytree(moved_authority, authority)
        browser_calls: list[str] = []

        def replacement_browser() -> None:
            fresh = OraclePolicyEngine(
                self.policy,
                root,
                authority_directory=authority,
                clock=self.clock,
            )
            with fresh.admission("local", request_facts()):
                browser_calls.append("authority-replacement-granted")

        self.assert_denied("authority_directory_unsafe", replacement_browser)
        self.assertEqual(browser_calls, [])

    def test_replacement_at_unlock_cannot_create_a_second_generation(self) -> None:
        moved_directory = self.root.with_name(f"{self.root.name}.unlocked")
        original_flock = fcntl.flock
        replaced = False

        def replace_at_unlock(descriptor: int, operation: int) -> None:
            nonlocal replaced
            if operation == fcntl.LOCK_UN and not replaced:
                self.root.rename(moved_directory)
                self.root.mkdir(mode=0o700)
                replaced = True
            original_flock(descriptor, operation)

        with mock.patch(
            "runtime_manager.oracle_policy.fcntl.flock",
            side_effect=replace_at_unlock,
        ):
            first = self.engine.reserve("local", request_facts())
        self.assertTrue(replaced)
        second_browser_calls: list[str] = []

        def replacement_browser_action() -> None:
            replacement = OraclePolicyEngine(
                self.policy,
                self.root,
                authority_directory=self.authority,
                clock=self.clock,
            )
            with replacement.admission("local", request_facts()):
                second_browser_calls.append("browser-contact")

        self.assert_denied(
            "state_directory_unsafe",
            replacement_browser_action,
        )
        self.assertEqual(second_browser_calls, [])
        self.assertIsNotNone(first.reservation_id)

    def test_replacing_state_and_namespace_directories_cannot_reenroll(self) -> None:
        grant = self.engine.reserve("local", request_facts())
        self.assertTrue(self.engine.release("local", grant.reservation_id))
        state_payload = self.engine.state_path.read_bytes()
        namespace_payload = self.engine.namespace_path.read_bytes()
        anchor = self.engine.namespace_path.parent
        moved_state = self.root.with_name(f"{self.root.name}.original")
        moved_anchor = anchor.with_name(f"{anchor.name}.original")
        replacement_state = self.root.with_name(f"{self.root.name}.replacement")
        replacement_anchor = anchor.with_name(f"{anchor.name}.replacement")
        browser_calls: list[str] = []

        self.root.rename(moved_state)
        anchor.rename(moved_anchor)
        self.root.mkdir(mode=0o700)
        anchor.mkdir(mode=0o700)
        self.engine.state_path.write_bytes(state_payload)
        os.chmod(self.engine.state_path, 0o600)
        self.engine.namespace_path.write_bytes(namespace_payload)
        os.chmod(self.engine.namespace_path, 0o600)
        try:

            def replacement_browser() -> None:
                replacement = OraclePolicyEngine(
                    self.policy,
                    self.root,
                    authority_directory=self.authority,
                    clock=self.clock,
                )
                with replacement.admission("local", request_facts()):
                    browser_calls.append("replacement-granted")

            self.assert_denied(
                "state_directory_unsafe",
                replacement_browser,
            )
            self.assertEqual(browser_calls, [])
        finally:
            self.root.rename(replacement_state)
            anchor.rename(replacement_anchor)
            moved_state.rename(self.root)
            moved_anchor.rename(anchor)

    def test_symlink_hardlink_and_unsafe_mode_state_are_rejected(self) -> None:
        outside = self.temporary_root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        state_path = self.root / "policy-state.json"
        state_path.symlink_to(outside)
        self.assert_denied(
            "state_file_unsafe",
            lambda: self.engine.reserve("local", request_facts()),
        )
        state_path.unlink()
        os.link(outside, state_path)
        self.assert_denied(
            "state_file_unsafe",
            lambda: self.engine.reserve("local", request_facts()),
        )
        state_path.unlink()
        state_path.write_text(
            json.dumps(
                {
                    "schema": ORACLE_POLICY_STATE_SCHEMA,
                    "policy_fingerprint": self.engine.policy_fingerprint,
                    "namespace_generation": "0" * 64,
                    "revision": 1,
                    "last_seen_at": 0,
                    "callers": {},
                }
            ),
            encoding="utf-8",
        )
        os.chmod(state_path, 0o644)
        self.assert_denied(
            "state_file_unsafe",
            lambda: self.engine.reserve("local", request_facts()),
        )

    def test_directory_replace_and_restore_aba_fails_closed(self) -> None:
        moved_directory = self.root.with_name(f"{self.root.name}.moved")

        def replace_directory(
            state: dict[str, object],
        ) -> tuple[dict[str, object], None]:
            self.root.rename(moved_directory)
            self.root.mkdir(mode=0o700)
            self.root.rmdir()
            moved_directory.rename(self.root)
            return state, None

        try:
            self.assert_denied(
                "state_directory_unsafe",
                lambda: self.engine._update(replace_directory),  # noqa: SLF001
            )
        finally:
            if moved_directory.exists() and not self.root.exists():
                moved_directory.rename(self.root)
        self.assertFalse(self.engine.state_path.exists())

    def test_directory_mode_change_during_decision_fails_closed(self) -> None:
        def loosen_directory(
            state: dict[str, object],
        ) -> tuple[dict[str, object], None]:
            os.chmod(self.root, 0o777)
            return state, None

        try:
            self.assert_denied(
                "state_directory_unsafe",
                lambda: self.engine._update(loosen_directory),  # noqa: SLF001
            )
        finally:
            os.chmod(self.root, 0o700)
        self.assertFalse(self.engine.state_path.exists())

    def test_state_is_bound_to_exact_policy_without_pruning_history(self) -> None:
        long_policy = OraclePolicy.from_mapping(
            policy_document(local=caller_policy(max_requests_per_window=1))
        )
        short_policy = OraclePolicy.from_mapping(
            policy_document(
                local=caller_policy(
                    max_requests_per_window=1,
                    window_seconds=1,
                )
            )
        )
        policy_root = self.temporary_root / "bound-policy"
        policy_authority = self.temporary_root / "bound-policy-authority"
        long_engine = self.provision_engine(
            long_policy,
            policy_root,
            authority=policy_authority,
        )
        first = long_engine.reserve("local", request_facts())
        self.assertTrue(long_engine.release("local", first.reservation_id))
        self.assert_denied(
            "policy_state_mismatch",
            lambda: OraclePolicyEngine(
                short_policy,
                policy_root,
                authority_directory=policy_authority,
                clock=self.clock,
            ),
        )
        self.assert_denied(
            "request_quota_exceeded",
            lambda: long_engine.reserve("local", request_facts()),
        )


if __name__ == "__main__":
    unittest.main()
