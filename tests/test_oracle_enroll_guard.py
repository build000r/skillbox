"""Fail-closed preflight for the Oracle enrollment script.

The 2026-08-06 incident is the spec: a stale `oracle-enroll-forward.sh` with
`DEFAULT_DISPLAY=:99` and `-nopw` would have attached a passwordless VNC server
to an unrelated Xvfb running with access control off — and nothing would have
failed. Every test here is aimed at that outcome being impossible to reach
quietly, and the headline case reconstructs the stale script byte for byte.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import oracle_enroll_guard as GUARD  # noqa: E402

GUARD_SCRIPT = SCRIPTS_DIR / "lib" / "oracle_enroll_guard.py"

#: The live checkout this repo's pin describes. Absent on a box without the
#: skills repo, so the tests that use it skip rather than fail there.
LIVE_SCRIPT_CANDIDATES = (
    ROOT_DIR.parent
    / "skills"
    / "deep-research-prompt"
    / "assets"
    / "scripts"
    / "oracle-enroll-forward.sh",
    ROOT_DIR
    / "workspace"
    / "skill-repos"
    / "build000r-skills"
    / "deep-research-prompt"
    / "assets"
    / "scripts"
    / "oracle-enroll-forward.sh",
)

HARDENED = """#!/usr/bin/env bash
set -euo pipefail
readonly DEFAULT_DISPLAY=":97"
DISPLAY_VALUE="${ORACLE_XVFB_DISPLAY:-$DEFAULT_DISPLAY}"
x11vnc -display "$DISPLAY_VALUE" -rfbauth "$HOST_VNC_PASSWORD_FILE" -auth "$HOST_XAUTHORITY"
"""

#: The stale Mac copy from the operator report, reconstructed.
STALE = """#!/usr/bin/env bash
set -euo pipefail
readonly DEFAULT_DISPLAY=":99"
DISPLAY_VALUE="${ORACLE_XVFB_DISPLAY:-$DEFAULT_DISPLAY}"
x11vnc -display "$DISPLAY_VALUE" -nopw -forever -shared
"""


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def script(self, source: str, name: str = "oracle-enroll-forward.sh") -> Path:
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def assert_refused(self, code: str, action: object) -> GUARD.EnrollGuardError:
        with self.assertRaises(GUARD.EnrollGuardError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def live_script(self) -> Path:
        for candidate in LIVE_SCRIPT_CANDIDATES:
            if candidate.is_file():
                return candidate
        self.skipTest("skills checkout with oracle-enroll-forward.sh not present")


class StaleScriptTests(GuardTestCase):
    """The incident itself: a stale script must fail closed, loudly."""

    def test_the_stale_mac_copy_is_unsafe(self) -> None:
        verdict = GUARD.verify_enroll_script(self.script(STALE))
        self.assertEqual(GUARD.STATE_UNSAFE, verdict.state)
        self.assertFalse(verdict.safe)
        self.assertFalse(verdict.trusted)
        self.assertEqual(":99", verdict.declared_display)

    def test_both_hazards_are_named_separately(self) -> None:
        # An operator must learn it is BOTH the wrong display and passwordless,
        # not just the first thing the guard happened to notice.
        verdict = GUARD.verify_enroll_script(self.script(STALE))
        reasons = " | ".join(verdict.reasons)
        self.assertIn(":99", reasons)
        self.assertIn(":97", reasons)
        self.assertIn("-nopw", reasons)
        self.assertIn("-rfbauth", reasons)

    def test_an_unsafe_script_can_never_be_overridden(self) -> None:
        # The core of fail-closed: naming the digest accepts an *unreviewed*
        # script, never a *dangerous* one.
        path = self.script(STALE)
        digest = GUARD.read_script(path)[1]
        self.assert_refused(
            "script_unsafe",
            lambda: GUARD.authorize_enrollment(path, accept_digest=digest),
        )
        self.assert_refused(
            "script_unsafe",
            lambda: GUARD.authorize_enrollment(
                path, accept_digest=digest, pinned_digests={digest: "forced"}
            ),
        )

    def test_each_hazard_alone_is_enough_to_refuse(self) -> None:
        wrong_display = HARDENED.replace('":97"', '":99"')
        no_password = HARDENED.replace('-rfbauth "$HOST_VNC_PASSWORD_FILE"', "-nopw")
        missing_marker = HARDENED.replace('-auth "$HOST_XAUTHORITY"', "")
        for source in (wrong_display, no_password, missing_marker):
            verdict = GUARD.verify_enroll_script(self.script(source))
            self.assertEqual(GUARD.STATE_UNSAFE, verdict.state, source)

    def test_a_script_declaring_no_display_is_unsafe(self) -> None:
        verdict = GUARD.verify_enroll_script(
            self.script(HARDENED.replace('readonly DEFAULT_DISPLAY=":97"', ""))
        )
        self.assertEqual(GUARD.STATE_UNSAFE, verdict.state)
        self.assertEqual("", verdict.declared_display)
        self.assertIn("<none>", " ".join(verdict.reasons))


class PinningTests(GuardTestCase):
    """Identity: reviewed revisions pass; unreviewed ones need a named digest."""

    def test_a_pinned_hardened_script_is_trusted(self) -> None:
        path = self.script(HARDENED)
        digest = GUARD.read_script(path)[1]
        verdict = GUARD.verify_enroll_script(
            path, pinned_digests={digest: "test fixture"}
        )
        self.assertEqual(GUARD.STATE_TRUSTED, verdict.state)
        self.assertTrue(verdict.trusted)
        self.assertEqual("test fixture", verdict.pinned_as)
        self.assertEqual((), verdict.reasons)

    def test_an_unpinned_but_safe_script_is_not_silently_accepted(self) -> None:
        path = self.script(HARDENED)
        verdict = GUARD.verify_enroll_script(path, pinned_digests={})
        self.assertEqual(GUARD.STATE_UNPINNED, verdict.state)
        self.assertTrue(verdict.safe)
        self.assertFalse(verdict.trusted)
        self.assert_refused(
            "script_unpinned",
            lambda: GUARD.authorize_enrollment(path, pinned_digests={}),
        )

    def test_accepting_an_unpinned_script_requires_its_exact_digest(self) -> None:
        path = self.script(HARDENED)
        digest = GUARD.read_script(path)[1]
        verdict = GUARD.authorize_enrollment(
            path, accept_digest=digest, pinned_digests={}
        )
        self.assertEqual(GUARD.STATE_UNPINNED, verdict.state)
        self.assert_refused(
            "accept_digest_mismatch",
            lambda: GUARD.authorize_enrollment(
                path, accept_digest="0" * 64, pinned_digests={}
            ),
        )

    def test_the_unpinned_note_carries_the_digest_to_paste(self) -> None:
        path = self.script(HARDENED)
        verdict = GUARD.verify_enroll_script(path, pinned_digests={})
        self.assertIn(verdict.digest, " ".join(verdict.notes))

    def test_the_digest_changes_with_the_content(self) -> None:
        first = GUARD.read_script(self.script(HARDENED, "a.sh"))[1]
        second = GUARD.read_script(self.script(HARDENED + "\n# edit\n", "b.sh"))[1]
        self.assertNotEqual(first, second)


class CommentHandlingTests(GuardTestCase):
    """A script that documents the old flag must not be refused for saying so."""

    def test_a_comment_mentioning_nopw_does_not_trip_the_guard(self) -> None:
        documented = HARDENED.replace(
            "set -euo pipefail",
            "set -euo pipefail\n# B4 hardening: we no longer pass -nopw here.",
        )
        verdict = GUARD.verify_enroll_script(self.script(documented), pinned_digests={})
        self.assertTrue(verdict.safe, verdict.reasons)

    def test_an_indented_comment_is_also_ignored(self) -> None:
        documented = HARDENED.replace(
            "set -euo pipefail", "set -euo pipefail\n    # historical: -nopw"
        )
        self.assertTrue(
            GUARD.verify_enroll_script(self.script(documented), pinned_digests={}).safe
        )

    def test_a_trailing_comment_after_code_still_counts(self) -> None:
        # A flag hidden after real code on the same line is exactly where a
        # disabled-but-present option would sit, so it is NOT ignored.
        sneaky = HARDENED.replace(
            '-auth "$HOST_XAUTHORITY"', '-auth "$HOST_XAUTHORITY" # -nopw'
        )
        verdict = GUARD.verify_enroll_script(self.script(sneaky), pinned_digests={})
        self.assertEqual(GUARD.STATE_UNSAFE, verdict.state)


class DisplayTests(GuardTestCase):
    """The script defaulting to :97 does not help if the caller asks for :99."""

    def test_the_shared_display_is_refused_by_name(self) -> None:
        self.assert_refused(
            "display_unsafe", lambda: GUARD.verify_requested_display(":99")
        )
        self.assertIn(":99", GUARD.UNSAFE_DISPLAYS)
        self.assertIn("-ac", GUARD.UNSAFE_DISPLAYS[":99"])

    def test_the_oracle_display_is_accepted(self) -> None:
        self.assertEqual(":97", GUARD.verify_requested_display(":97"))

    def test_malformed_displays_are_refused(self) -> None:
        for display in ("97", ":", "", ":9999", "; rm -rf /", None, 97):
            self.assert_refused(
                "display_invalid",
                lambda display=display: GUARD.verify_requested_display(display),
            )

    def test_authorization_checks_the_display_before_the_script(self) -> None:
        # A missing script must not mask an unsafe display, and vice versa: the
        # first refusal an operator sees should be the one they asked for.
        self.assert_refused(
            "display_unsafe",
            lambda: GUARD.authorize_enrollment(self.root / "absent.sh", display=":99"),
        )


class ScriptReadingTests(GuardTestCase):
    """Unreadable input refuses; it never becomes a verdict."""

    def test_a_missing_script_refuses(self) -> None:
        self.assert_refused(
            "script_missing", lambda: GUARD.verify_enroll_script(self.root / "absent.sh")
        )

    def test_a_directory_is_not_a_script(self) -> None:
        self.assert_refused(
            "script_unreadable", lambda: GUARD.verify_enroll_script(self.root)
        )

    def test_an_oversize_file_refuses(self) -> None:
        path = self.script("#!/bin/sh\n" + "x" * (GUARD.MAX_SCRIPT_BYTES + 1))
        self.assert_refused("script_unreadable", lambda: GUARD.verify_enroll_script(path))

    def test_a_non_utf8_file_refuses(self) -> None:
        path = self.root / "binary.sh"
        path.write_bytes(b"\xff\xfe\x00")
        self.assert_refused("script_unreadable", lambda: GUARD.verify_enroll_script(path))

    def test_an_invalid_path_refuses(self) -> None:
        for value in (None, "", 42):
            self.assert_refused(
                "script_path_invalid",
                lambda value=value: GUARD.verify_enroll_script(value),
            )


class LiveCheckoutTests(GuardTestCase):
    """The pin in this repo must actually describe the checkout on this box."""

    def test_the_live_script_is_trusted(self) -> None:
        path = self.live_script()
        verdict = GUARD.verify_enroll_script(path)
        self.assertEqual(GUARD.STATE_TRUSTED, verdict.state, verdict.reasons)
        self.assertEqual(GUARD.ORACLE_DISPLAY, verdict.declared_display)
        self.assertIn("3955fe3", verdict.pinned_as)

    def test_the_live_script_carries_no_passwordless_flag(self) -> None:
        source, _digest = GUARD.read_script(self.live_script())
        self.assertNotIn("-nopw", source)
        self.assertIn("-rfbauth", source)

    def test_the_pin_matches_the_live_digest(self) -> None:
        # If the skills checkout moves, this is the test that says "re-review
        # and re-pin" rather than the guard silently going unpinned in the field.
        _source, digest = GUARD.read_script(self.live_script())
        self.assertIn(digest, GUARD.PINNED_DIGESTS)


class CommandLineTests(GuardTestCase):
    """The shell preflight surface: JSON out, exit code in."""

    def run_cli(self, *argv: str) -> tuple[int, dict, str]:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT), *argv],
            capture_output=True,
            text=True,
            timeout=120,
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.returncode, payload, result.stderr

    def test_a_hardened_script_exits_zero_with_a_verdict(self) -> None:
        path = self.script(HARDENED)
        digest = GUARD.read_script(path)[1]
        code, payload, _stderr = self.run_cli(
            "--script", str(path), "--accept-digest", digest, "--format", "json"
        )
        self.assertEqual(GUARD.EXIT_OK, code)
        self.assertTrue(payload["ok"])
        self.assertEqual(GUARD.STATE_UNPINNED, payload["state"])

    def test_a_stale_script_exits_nonzero(self) -> None:
        code, payload, _stderr = self.run_cli(
            "--script", str(self.script(STALE)), "--format", "json"
        )
        self.assertEqual(GUARD.EXIT_REFUSED, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("script_unsafe", payload["error"]["code"])

    def test_an_unsafe_display_exits_nonzero(self) -> None:
        code, payload, _stderr = self.run_cli(
            "--script", str(self.script(HARDENED)), "--display", ":99", "--format", "json"
        )
        self.assertEqual(GUARD.EXIT_REFUSED, code)
        self.assertEqual("display_unsafe", payload["error"]["code"])

    def test_text_mode_says_nothing_on_stdout_when_refusing(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT), "--script", str(self.script(STALE))],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(GUARD.EXIT_REFUSED, result.returncode)
        self.assertEqual("", result.stdout.strip())
        self.assertIn("refused", result.stderr)


class ContractTests(GuardTestCase):
    """Invariants that keep the guard meaningful as it changes."""

    def test_every_refusal_code_in_the_source_is_reachable(self) -> None:
        import re

        source = GUARD_SCRIPT.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        # Each code is a distinct, snake_case token; none is a stray typo of
        # another, which would make a refusal untestable.
        self.assertEqual(len(used), len({code.strip() for code in used}))

    def test_the_pin_table_is_not_empty(self) -> None:
        # An empty pin table would make every script "unpinned", which degrades
        # the guard to a property check and loses the reviewed-revision signal.
        self.assertTrue(GUARD.PINNED_DIGESTS)
        for digest in GUARD.PINNED_DIGESTS:
            self.assertEqual(64, len(digest))

    def test_the_oracle_display_is_not_in_the_unsafe_set(self) -> None:
        self.assertNotIn(GUARD.ORACLE_DISPLAY, GUARD.UNSAFE_DISPLAYS)

    def test_the_guard_starts_nothing(self) -> None:
        source = GUARD_SCRIPT.read_text(encoding="utf-8")
        for banned in ("subprocess", "socket", "urllib", "os.system", "popen"):
            self.assertNotIn(banned, source, banned)


if __name__ == "__main__":
    unittest.main()
