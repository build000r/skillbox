"""Oracle enrollment: ephemeral secret handoff and verifier-only state.

The bead names five surfaces the VNC secret must never reach — stdout, stderr,
journald, argv, and durable state — so the suite is organised around those five
and proves each directly rather than by inspection. The stdout/stderr/journald
case is proven in a real subprocess running the whole lifecycle, because that is
what journald actually captures.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.oracle_broker import OracleBrokerError  # noqa: E402
from runtime_manager.oracle_enrollment import (  # noqa: E402
    ORACLE_ENROLLMENT_STATE_SCHEMA,
    REFUSAL_CODES,
    SECRET_PATTERN,
    STATE_KEYS,
    VERIFIER_ALGORITHM,
    EnrollmentSecret,
    EnrollmentState,
    OracleEnrollmentError,
    SecretVerifier,
    assert_argv_secret_free,
    assert_payload_secret_free,
    clear_enrollment_state,
    enrollment_state_path,
    handoff_stdin_payload,
    mint_enrollment_secret,
    read_enrollment_state,
    requires_reenrollment,
    ssh_handoff_argv,
    write_enrollment_state,
)

ENROLLMENT_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "oracle_enrollment.py"

#: The whole lifecycle in one process, printing everything a chatty
#: implementation might print. journald captures exactly this.
LIFECYCLE_WORKER = """
import sys
sys.path.insert(0, {env_manager!r})
from runtime_manager.oracle_enrollment import (
    EnrollmentState, OracleEnrollmentError, clear_enrollment_state,
    handoff_stdin_payload, mint_enrollment_secret, read_enrollment_state,
    ssh_handoff_argv, write_enrollment_state,
)

root = sys.argv[1]
secret = mint_enrollment_secret()
print("minted", secret)
print("repr", repr(secret), file=sys.stderr)
print("format", f"{{secret}}", f"{{secret!r}}", f"{{secret!s}}")

state = EnrollmentState.for_session(
    host="d3", local_port=6080, web_port=6081,
    remote_script="/srv/oracle-enroll-forward.sh", secret=secret,
)
print("state", state, repr(state))
print("payload", state.to_payload())
argv = ssh_handoff_argv("/usr/bin/ssh", "d3", "/srv/oracle-enroll-forward.sh",
                        display=":99", web_port=6081, vnc_port=5900,
                        auth_mode="login")
print("argv", argv)
write_enrollment_state(root, state)
print("read-back", read_enrollment_state(root))

# The one legitimate disclosure, and then every failure path.
handed = handoff_stdin_payload(secret)
sys.stderr.write("handed %d bytes\\n" % len(handed))
for _ in range(2):
    try:
        handoff_stdin_payload(secret)
    except OracleEnrollmentError as error:
        print("refused", error, error.code, error.to_payload(), file=sys.stderr)
try:
    import pickle
    pickle.dumps(secret)
except Exception as error:
    print("pickle refused", error, file=sys.stderr)
clear_enrollment_state(root)
# Emitted last, on a line of its own, so the test can extract it.
sys.stderr.write("SECRET_SENTINEL:" + handed.decode().strip() + "\\n")
"""


class EnrollmentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_root = Path(temporary.name).resolve() / "state"

    def assert_refused(self, code: str, action: object) -> OracleEnrollmentError:
        with self.assertRaises(OracleEnrollmentError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def session(self, secret: EnrollmentSecret, **overrides: object) -> EnrollmentState:
        values: dict[str, object] = {
            "host": "d3",
            "local_port": 6080,
            "web_port": 6081,
            "remote_script": "/srv/oracle-enroll-forward.sh",
            "secret": secret,
            "now_ms": 1_700_000_000_000,
        }
        values.update(overrides)
        return EnrollmentState.for_session(**values)  # type: ignore[arg-type]


class DurableStateTests(EnrollmentTestCase):
    """Surface 1: the secret must never reach durable state."""

    def test_the_written_state_does_not_contain_the_secret(self) -> None:
        secret = mint_enrollment_secret()
        state = self.session(secret)
        path = write_enrollment_state(self.state_root, state)
        raw = path.read_bytes()
        value = handoff_stdin_payload(secret).decode().strip()
        self.assertNotIn(value.encode(), raw)
        self.assertNotIn(b"VNC_PASSWORD", raw)
        self.assertNotIn(b"password", raw.lower())

    def test_the_state_document_has_an_exact_key_allowlist(self) -> None:
        secret = mint_enrollment_secret()
        payload = self.session(secret).to_payload()
        self.assertEqual(set(STATE_KEYS), set(payload))
        self.assertEqual(ORACLE_ENROLLMENT_STATE_SCHEMA, payload["schema"])
        self.assertEqual(VERIFIER_ALGORITHM, payload["verifier"]["algorithm"])

    def test_a_payload_carrying_a_secret_shaped_value_is_blocked(self) -> None:
        # Belt and braces: even if a future field held the secret, the writer
        # refuses rather than producing a file.
        secret = mint_enrollment_secret()
        value = handoff_stdin_payload(secret).decode().strip()
        self.assert_refused(
            "secret_leak_blocked",
            lambda: assert_payload_secret_free({"ok": True, "leaked": value}),
        )
        self.assert_refused(
            "secret_leak_blocked",
            lambda: assert_payload_secret_free({"nested": [{"deep": value}]}),
        )

    def test_the_verifier_hex_is_not_mistaken_for_a_secret(self) -> None:
        secret = mint_enrollment_secret()
        assert_payload_secret_free(self.session(secret).to_payload())

    def test_the_state_file_is_private_and_atomic(self) -> None:
        secret = mint_enrollment_secret()
        path = write_enrollment_state(self.state_root, self.session(secret))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path.parent).st_mode))
        # No `$file.new.$$`-style leftovers, and no predictable temp name.
        self.assertEqual([path.name], [entry.name for entry in path.parent.iterdir()])

    def test_repeated_writes_leave_no_temporary_files(self) -> None:
        secret = mint_enrollment_secret()
        state = self.session(secret)
        for _ in range(5):
            write_enrollment_state(self.state_root, state)
        names = sorted(entry.name for entry in enrollment_state_path(self.state_root).parent.iterdir())
        self.assertEqual(["enrollment.json"], names)

    def test_state_round_trips_through_disk(self) -> None:
        secret = mint_enrollment_secret()
        write_enrollment_state(self.state_root, self.session(secret))
        restored = read_enrollment_state(self.state_root)
        self.assertEqual("d3", restored.host)
        self.assertEqual(6080, restored.local_port)
        self.assertTrue(restored.verifier.matches(handoff_stdin_payload(secret).decode().strip()))

    def test_a_group_readable_state_file_refuses(self) -> None:
        secret = mint_enrollment_secret()
        path = write_enrollment_state(self.state_root, self.session(secret))
        os.chmod(path, 0o644)
        self.assert_refused(
            "enrollment_state_permissions",
            lambda: read_enrollment_state(self.state_root),
        )

    def test_malformed_state_refuses(self) -> None:
        secret = mint_enrollment_secret()
        path = write_enrollment_state(self.state_root, self.session(secret))
        for raw in (
            b"{not json",
            b"[]",
            json.dumps({"schema": "other.v1"}).encode(),
            json.dumps({**self.session(secret).to_payload(), "extra": 1}).encode(),
        ):
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            with self.assertRaises(OracleEnrollmentError):
                read_enrollment_state(self.state_root)


class ArgvTests(EnrollmentTestCase):
    """Surface 2: argv is world-readable through ps."""

    def test_the_handoff_command_carries_no_secret(self) -> None:
        secret = mint_enrollment_secret()
        value = handoff_stdin_payload(secret).decode().strip()
        argv = ssh_handoff_argv(
            "/usr/bin/ssh",
            "d3",
            "/srv/oracle-enroll-forward.sh",
            display=":99",
            web_port=6081,
            vnc_port=5900,
            auth_mode="login",
        )
        self.assertNotIn(value, argv)
        self.assertIn("--password-stdin", argv)
        for entry in argv:
            self.assertNotIn(value, entry)

    def test_a_secret_shaped_argument_is_refused(self) -> None:
        secret = mint_enrollment_secret()
        value = handoff_stdin_payload(secret).decode().strip()
        self.assert_refused(
            "secret_in_argv_forbidden",
            lambda: assert_argv_secret_free(["ssh", "d3", value]),
        )

    def test_the_builder_has_no_parameter_that_could_carry_a_secret(self) -> None:
        import inspect

        from runtime_manager import oracle_enrollment

        parameters = set(inspect.signature(oracle_enrollment.ssh_handoff_argv).parameters)
        for banned in ("secret", "password", "vnc_secret", "token"):
            self.assertNotIn(banned, parameters)

    def test_handoff_arguments_are_validated(self) -> None:
        for kwargs in (
            {"display": "99"},
            {"display": ":notanumber"},
            {"auth_mode": "LOGIN"},
            {"web_port": 80},
            {"vnc_port": 70000},
        ):
            options: dict[str, object] = {
                "display": ":99",
                "web_port": 6081,
                "vnc_port": 5900,
                "auth_mode": "login",
            }
            options.update(kwargs)
            self.assert_refused(
                "enrollment_state_invalid",
                lambda options=options: ssh_handoff_argv(
                    "/usr/bin/ssh", "d3", "/srv/x.sh", **options
                ),
            )


class OneTimeHandoffTests(EnrollmentTestCase):
    """The secret is handed over once and is then gone."""

    def test_the_secret_is_disclosed_exactly_once(self) -> None:
        secret = mint_enrollment_secret()
        self.assertTrue(secret.available)
        value = secret.disclose()
        self.assertRegex(value, SECRET_PATTERN)
        self.assertFalse(secret.available)
        self.assertTrue(secret.disclosed)
        self.assert_refused("secret_already_disclosed", secret.disclose)

    def test_a_second_take_is_distinguishable_from_never_having_one(self) -> None:
        taken = mint_enrollment_secret()
        taken.disclose()
        self.assert_refused("secret_already_disclosed", taken.disclose)
        wiped = mint_enrollment_secret()
        wiped.wipe()
        self.assert_refused("secret_already_disclosed", wiped.disclose)

    def test_the_context_manager_wipes_on_exit(self) -> None:
        with mint_enrollment_secret() as secret:
            self.assertTrue(secret.available)
        self.assertFalse(secret.available)
        self.assert_refused("secret_already_disclosed", secret.disclose)

    def test_wipe_is_idempotent(self) -> None:
        secret = mint_enrollment_secret()
        secret.wipe()
        secret.wipe()
        self.assertFalse(secret.available)

    def test_the_verifier_survives_disclosure(self) -> None:
        # Verification must keep working after the secret is gone; that is the
        # whole point of persisting a verifier instead of the secret.
        secret = mint_enrollment_secret()
        value = secret.disclose()
        self.assertTrue(secret.matches(value))
        self.assertFalse(secret.matches("x" * 40))

    def test_minted_secrets_are_unique_and_well_shaped(self) -> None:
        values = {mint_enrollment_secret().disclose() for _ in range(32)}
        self.assertEqual(32, len(values))
        for value in values:
            self.assertRegex(value, SECRET_PATTERN)

    def test_entropy_bounds_are_enforced(self) -> None:
        for entropy in (0, 17, 97, True, "32"):
            self.assert_refused(
                "secret_invalid",
                lambda entropy=entropy: mint_enrollment_secret(entropy_bytes=entropy),
            )

    def test_a_malformed_secret_is_refused_without_echoing_it(self) -> None:
        error = self.assert_refused("secret_invalid", lambda: EnrollmentSecret("short"))
        self.assertNotIn("short", str(error))
        self.assertNotIn("short", repr(error.to_payload()))


class VerifierTests(EnrollmentTestCase):
    """The persisted representation answers one question and reveals nothing."""

    def test_the_verifier_matches_only_the_minted_secret(self) -> None:
        secret = mint_enrollment_secret()
        value = secret.disclose()
        verifier = secret.verifier
        self.assertTrue(verifier.matches(value))
        self.assertFalse(verifier.matches(value[:-1] + ("A" if value[-1] != "A" else "B")))

    def test_a_malformed_candidate_is_false_not_an_error(self) -> None:
        secret = mint_enrollment_secret()
        for candidate in (None, "", "short", 42, b"bytes", "x" * 200):
            self.assertFalse(secret.verifier.matches(candidate))

    def test_two_verifiers_for_one_secret_differ_by_salt(self) -> None:
        secret = mint_enrollment_secret().disclose()
        first = SecretVerifier.for_secret(secret)
        second = SecretVerifier.for_secret(secret)
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.digest, second.digest)
        self.assertTrue(first.matches(secret))
        self.assertTrue(second.matches(secret))

    def test_the_verifier_payload_never_contains_the_secret(self) -> None:
        secret = mint_enrollment_secret()
        value = secret.disclose()
        rendered = json.dumps(secret.verifier.to_payload())
        self.assertNotIn(value, rendered)

    def test_verifier_fields_are_validated(self) -> None:
        good = SecretVerifier.for_secret(mint_enrollment_secret().disclose()).to_payload()
        for override in (
            {"algorithm": "md5"},
            {"salt": "zz"},
            {"digest": "abc"},
            {"iterations": 1},
            {"iterations": True},
        ):
            self.assert_refused(
                "verifier_invalid",
                lambda override=override: SecretVerifier.from_mapping(
                    {**good, **override}
                ),
            )
        self.assert_refused(
            "verifier_invalid",
            lambda: SecretVerifier.from_mapping({**good, "extra": 1}),
        )


class RestartAndCleanupTests(EnrollmentTestCase):
    """Fail closed by construction: a restart cannot recover the secret."""

    def test_state_read_from_disk_always_requires_reenrollment(self) -> None:
        secret = mint_enrollment_secret()
        write_enrollment_state(self.state_root, self.session(secret))
        restored = read_enrollment_state(self.state_root)
        self.assertTrue(requires_reenrollment(restored))

    def test_no_public_api_turns_persisted_state_back_into_a_secret(self) -> None:
        # The guarantee is structural, so assert on the surface itself: nothing
        # reachable from a restored state exposes the value.
        secret = mint_enrollment_secret()
        value = secret.disclose()
        write_enrollment_state(self.state_root, self.session(secret))
        restored = read_enrollment_state(self.state_root)
        rendered = json.dumps(restored.to_payload()) + repr(restored) + str(restored)
        self.assertNotIn(value, rendered)
        for name in dir(restored):
            if name.startswith("_"):
                continue
            attribute = getattr(restored, name)
            self.assertNotIn(value, repr(attribute), name)

    def test_a_live_secret_reports_no_reenrollment_until_it_is_spent(self) -> None:
        secret = mint_enrollment_secret()
        self.assertFalse(requires_reenrollment(secret))
        secret.disclose()
        self.assertTrue(requires_reenrollment(secret))

    def test_teardown_removes_the_state_and_is_idempotent(self) -> None:
        secret = mint_enrollment_secret()
        write_enrollment_state(self.state_root, self.session(secret))
        self.assertTrue(clear_enrollment_state(self.state_root))
        self.assertFalse(enrollment_state_path(self.state_root).exists())
        self.assertFalse(clear_enrollment_state(self.state_root))

    def test_teardown_sweeps_orphaned_temporary_files(self) -> None:
        secret = mint_enrollment_secret()
        write_enrollment_state(self.state_root, self.session(secret))
        directory = enrollment_state_path(self.state_root).parent
        orphan = directory / ".enrollment-crashed.tmp"
        orphan.write_text("{}", encoding="utf-8")
        clear_enrollment_state(self.state_root)
        self.assertEqual([], list(directory.iterdir()))

    def test_requires_reenrollment_refuses_an_unknown_object(self) -> None:
        self.assert_refused(
            "enrollment_state_invalid", lambda: requires_reenrollment("state")
        )


class OutputSurfaceTests(EnrollmentTestCase):
    """Surfaces 3-5: stdout, stderr, and therefore journald."""

    def test_repr_and_str_are_redacted(self) -> None:
        secret = mint_enrollment_secret()
        value = secret.disclose()
        for rendering in (repr(secret), str(secret), f"{secret}", f"{secret!r}", f"{secret!s}"):
            self.assertNotIn(value, rendering)
            self.assertIn("REDACTED", rendering)

    def test_a_format_spec_cannot_bypass_the_redaction(self) -> None:
        secret = mint_enrollment_secret()
        value = secret.disclose()
        self.assertNotIn(value, f"{secret:>10}")

    def test_pickling_a_secret_is_refused(self) -> None:
        import pickle

        secret = mint_enrollment_secret()
        with self.assertRaises(OracleEnrollmentError):
            pickle.dumps(secret)

    def test_the_whole_lifecycle_prints_no_secret_in_a_real_process(self) -> None:
        # journald captures a unit's stdout and stderr, so proving both are
        # clean in a real subprocess proves the journald case too.
        script = LIFECYCLE_WORKER.format(env_manager=str(ENV_MANAGER_DIR))
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.state_root)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        sentinel = ""
        cleaned: list[str] = []
        for line in result.stderr.splitlines():
            if line.startswith("SECRET_SENTINEL:"):
                sentinel = line.split(":", 1)[1]
                continue
            cleaned.append(line)
        self.assertRegex(sentinel, SECRET_PATTERN)

        stderr = "\n".join(cleaned)
        self.assertNotIn(sentinel, result.stdout)
        self.assertNotIn(sentinel, stderr)
        # The run really did exercise the paths that could have leaked.
        self.assertIn("REDACTED", result.stdout)
        self.assertIn("refused", stderr)
        self.assertIn("pickle refused", stderr)

    def test_the_output_scan_catches_a_deliberate_leak(self) -> None:
        # Negative control: a "we saw no secret" assertion is worthless if the
        # scan could not have seen one. A caller that prints the disclosed
        # value must be caught by exactly the check used above.
        leaky = (
            "import sys\n"
            f"sys.path.insert(0, {str(ENV_MANAGER_DIR)!r})\n"
            "from runtime_manager.oracle_enrollment import mint_enrollment_secret\n"
            "secret = mint_enrollment_secret()\n"
            "value = secret.disclose()\n"
            "print('leaked', value)\n"
            "sys.stderr.write('SECRET_SENTINEL:' + value + chr(10))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", leaky], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(0, result.returncode, result.stderr)
        sentinel = ""
        for line in result.stderr.splitlines():
            if line.startswith("SECRET_SENTINEL:"):
                sentinel = line.split(":", 1)[1]
        self.assertRegex(sentinel, SECRET_PATTERN)
        self.assertIn(sentinel, result.stdout)

    def test_the_module_writes_to_no_log_stream_of_its_own(self) -> None:
        source = ENROLLMENT_SOURCE.read_text(encoding="utf-8")
        for banned in ("import logging", "print(", "sys.stdout", "sys.stderr", "syslog"):
            self.assertNotIn(banned, source, banned)


class ContractTests(EnrollmentTestCase):
    """Invariants that keep the contract honest as it changes."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = ENROLLMENT_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_the_state_schema_has_no_secret_bearing_key(self) -> None:
        for key in STATE_KEYS:
            self.assertNotIn("password", key)
            self.assertNotIn("secret", key)
            self.assertNotIn("token", key)

    def test_the_secret_shape_matches_the_shell_contract(self) -> None:
        # The shell client validates `^[A-Za-z0-9_-]{24,128}$`; a port that
        # disagreed would reject secrets the other side considers valid.
        self.assertEqual("^[A-Za-z0-9_-]{24,128}$", SECRET_PATTERN.pattern)

    def test_refusals_share_the_oracle_error_surface(self) -> None:
        error = self.assert_refused(
            "secret_invalid", lambda: EnrollmentSecret("nope")
        )
        self.assertIsInstance(error, OracleBrokerError)
        self.assertEqual("secret_invalid", error.to_payload()["error_code"])

    def test_only_one_function_touches_the_secret_value(self) -> None:
        source = ENROLLMENT_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(1, source.count("secret.disclose()"), source.count("secret.disclose()"))


if __name__ == "__main__":
    unittest.main()
