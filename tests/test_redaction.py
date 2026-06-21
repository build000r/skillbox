"""Tests for the consolidated secret-redaction module (scripts/lib/redaction.py).

This is the single golden corpus for redaction across the tree. It proves:
  * every provider/shape in the UNION pattern table is redacted;
  * redaction is idempotent and never raises on weird input;
  * near-miss strings (token.txt, the word "password" in prose) are PRESERVED;
  * each of the four prior surfaces now redacts via the shared impl;
  * box.py redacts remote doctl/ssh output at its subprocess boundary.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
for _p in (str(ROOT_DIR), str(SCRIPTS_DIR), str(ENV_MANAGER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.redaction import (  # noqa: E402
    REDACTION_MARKER,
    is_secret_key,
    redact_text,
    redact_value,
)


# --------------------------------------------------------------------------- #
# Golden corpus: (input, [secrets that MUST disappear]) for each shape.
# Reused conceptually by the per-surface smokes below.
# --------------------------------------------------------------------------- #
SECRET_CORPUS: list[tuple[str, list[str]]] = [
    # KEY=VALUE / KEY: VALUE assignments (the original four-copy coverage).
    ("SKILLBOX_DO_TOKEN=do-secret-value", ["do-secret-value"]),
    ("API_KEY=api-secret", ["api-secret"]),
    ("api-key: api-secret-2", ["api-secret-2"]),
    ("password=secret123", ["secret123"]),
    ("PASSWD=p4ssw0rd", ["p4ssw0rd"]),
    ('AUTH_TOKEN="supersecret"', ["supersecret"]),
    ("PRIVATE_KEY=rsa-blob-xyz", ["rsa-blob-xyz"]),
    ("ACCESS_KEY=akia-blah", ["akia-blah"]),
    ("SKILLBOX_TS_AUTHKEY=ts-secret", ["ts-secret"]),
    ("PASSPHRASE=open-sesame", ["open-sesame"]),
    ("DB_CREDENTIAL=conn-string-secret", ["conn-string-secret"]),
    # Bearer tokens (Authorization / Proxy-Authorization).
    ("Authorization: Bearer abc.def.ghi", ["abc.def.ghi"]),
    ("proxy-authorization: bearer token-secret", ["token-secret"]),
    # URL userinfo (scheme://user:pass@host) -- NEW.
    ("clone https://alice:hunter2@github.com/x.git", ["hunter2", "alice:hunter2"]),
    ("redis://default:r3disPass@cache:6379/0", ["r3disPass"]),
    # Provider-prefixed credential tokens (bare, no KEY=) -- NEW.
    ("enrolled with tskey-kRandom1234ABCD now", ["tskey-kRandom1234ABCD"]),
    ("doctl error: token dop_v1_0123456789abcdef rejected", ["dop_v1_0123456789abcdef"]),
    ("oauth doo_v1_aaaa1111 expired", ["doo_v1_aaaa1111"]),
    ("refresh dor_v1_bbbb2222 invalid", ["dor_v1_bbbb2222"]),
]

# Near-miss strings that must be PRESERVED verbatim (no over-redaction).
NEGATIVE_CORPUS: list[str] = [
    "see token.txt for the layout",
    "the password field is required",
    "this is a secret recipe blog post",
    "rotate your api keys quarterly",
    "ssh user@host works fine",            # userinfo with NO password
    "git@github.com:org/repo.git",          # scp-style remote, no password
    "the authkey concept is documented",    # bare word, no assignment/prefix
    "https://example.com/path?ok=1",        # url, no userinfo
    "deadbeef tskeyword not a key",         # 'tskey' not followed by '-'
]


class RedactTextShapeTests(unittest.TestCase):
    def test_each_corpus_entry_is_redacted(self) -> None:
        for raw, secrets in SECRET_CORPUS:
            out = redact_text(raw)
            self.assertIn(REDACTION_MARKER, out, f"no marker in: {raw!r} -> {out!r}")
            for secret in secrets:
                self.assertNotIn(secret, out, f"leaked {secret!r} in: {out!r}")

    def test_negatives_are_preserved_unchanged(self) -> None:
        for raw in NEGATIVE_CORPUS:
            out = redact_text(raw)
            self.assertEqual(raw, out, f"over-redacted near-miss: {raw!r} -> {out!r}")
            self.assertNotIn(REDACTION_MARKER, out)

    def test_substring_key_assignment_is_redacted_parity_with_prior_copies(self) -> None:
        # All four prior surfaces redacted KEY=value when KEY merely *contains*
        # a sensitive token (e.g. TOKENIZER=, MY_AUTH_TOKEN_2=). This is
        # intentional broad coverage we must NOT narrow, so assert the value is
        # still redacted. (Distinct from a bare file path / prose word, which is
        # preserved -- see test_negatives_are_preserved_unchanged.)
        for raw in ("TOKENIZER=fast-bpe", "MY_AUTH_TOKEN_2=zzz", "DB.PASSWORD.MAIN=qqq"):
            out = redact_text(raw)
            self.assertIn(REDACTION_MARKER, out, raw)

    def test_benign_context_around_secret_is_kept(self) -> None:
        out = redact_text("build failed SKILLBOX_DO_TOKEN=do-secret while cloning")
        self.assertIn("build failed", out)
        self.assertIn("while cloning", out)
        self.assertIn("SKILLBOX_DO_TOKEN=", out)
        self.assertNotIn("do-secret", out)

    def test_multiple_secrets_in_one_line(self) -> None:
        raw = (
            "Authorization: Bearer btok password=ptok api_key=ktok "
            "tskey-xyz dop_v1_deadbeef https://u:p@h/x"
        )
        out = redact_text(raw)
        for secret in ("btok", "ptok", "ktok", "tskey-xyz", "dop_v1_deadbeef", "u:p"):
            self.assertNotIn(secret, out, out)


class RedactPropertyTests(unittest.TestCase):
    def test_idempotent_over_whole_corpus(self) -> None:
        for raw, _ in SECRET_CORPUS:
            once = redact_text(raw)
            twice = redact_text(once)
            self.assertEqual(once, twice, f"not idempotent: {raw!r}")

    def test_idempotent_on_negatives(self) -> None:
        for raw in NEGATIVE_CORPUS:
            self.assertEqual(redact_text(raw), redact_text(redact_text(raw)))

    def test_never_raises_on_weird_input(self) -> None:
        for weird in (None, 123, 4.5, True, b"TOKEN=bytes-secret", ["TOKEN=x"], {"k": "v"}):
            # Must not raise; returns a str.
            self.assertIsInstance(redact_text(weird), str)
        self.assertEqual(redact_text(None), "")

    def test_redact_value_recurses_and_redacts_by_key(self) -> None:
        payload = {
            "TOKEN": "should-vanish",
            "note": "password=inline-secret",
            "safe": "keep-me",
            "nested": [{"API_KEY": "k1"}, {"plain": "Authorization: Bearer btok"}],
            "tuple_vals": ("password=tsecret", "ok"),
        }
        out = redact_value(payload)
        self.assertEqual(out["TOKEN"], REDACTION_MARKER)        # whole value by key
        self.assertNotIn("inline-secret", out["note"])          # in-string shape
        self.assertEqual(out["safe"], "keep-me")                # preserved
        self.assertEqual(out["nested"][0]["API_KEY"], REDACTION_MARKER)
        self.assertNotIn("btok", out["nested"][1]["plain"])
        self.assertIsInstance(out["tuple_vals"], list)          # tuples -> lists
        self.assertNotIn("tsecret", out["tuple_vals"][0])
        # Keys and structure are intact.
        self.assertEqual(set(out), set(payload))

    def test_redact_value_never_raises_on_weird_input(self) -> None:
        for weird in (None, 123, 4.5, True, b"x", object()):
            redact_value(weird)  # must not raise
        deep: dict = {}
        cur = deep
        for _ in range(500):
            cur["next"] = {}
            cur = cur["next"]
        cur["TOKEN"] = "deep-secret"
        redact_value(deep)  # depth guard: must not raise

    def test_redact_value_leaves_non_strings_untouched(self) -> None:
        out = redact_value({"count": 3, "ratio": 1.5, "flag": False, "nothing": None})
        self.assertEqual(out, {"count": 3, "ratio": 1.5, "flag": False, "nothing": None})

    def test_is_secret_key_matches_names_not_prose(self) -> None:
        for name in ("TOKEN", "api_key", "SKILLBOX_TS_AUTHKEY", "db_password", "ACCESS_KEY"):
            self.assertTrue(is_secret_key(name))
        for name in ("status", "label", "count", "host", "id"):
            self.assertFalse(is_secret_key(name))
        self.assertFalse(is_secret_key(None))


# --------------------------------------------------------------------------- #
# Per-surface integration smokes: prove each surface redacts via the shared impl.
# --------------------------------------------------------------------------- #

class SurfaceUsesSharedImplTests(unittest.TestCase):
    def test_agent_adapters_alias_is_shared_redact_text(self) -> None:
        from runtime_manager import agent_adapters as ADAPT

        self.assertEqual(ADAPT.redact_diagnostic_text, redact_text)
        self.assertEqual(ADAPT.REDACTION_MARKER, REDACTION_MARKER)
        out = ADAPT.redact_diagnostic_text("Authorization: Bearer btok api_key=ktok tskey-z")
        for secret in ("btok", "ktok", "tskey-z"):
            self.assertNotIn(secret, out)

    def test_agent_snapshots_uses_shared_key_matcher(self) -> None:
        from runtime_manager import agent_snapshots as SNAP

        out = SNAP.redact_snapshot_value(
            {"token": "secret-value", "msg": "password=inline", "z": "keep"}
        )
        self.assertEqual(out["token"], REDACTION_MARKER)
        self.assertNotIn("inline", out["msg"])
        self.assertEqual(out["z"], "keep")

    def test_mcp_server_alias_is_shared_redact_text(self) -> None:
        module = SourceFileLoader(
            "skillbox_mcp_redaction_probe",
            str(ENV_MANAGER_DIR / "mcp_server.py"),
        ).load_module()
        self.assertEqual(module.redact_diagnostic_text, redact_text)
        self.assertNotIn("do-secret", module.redact_diagnostic_text("SKILLBOX_DO_TOKEN=do-secret"))

    def test_operator_mcp_aliases_are_shared(self) -> None:
        module = SourceFileLoader(
            "skillbox_operator_redaction_probe",
            str(SCRIPTS_DIR / "operator_mcp_server.py"),
        ).load_module()
        # redact_diagnostic_text preserved as alias (box_exec audit path uses it).
        self.assertEqual(module.redact_diagnostic_text, redact_text)
        self.assertEqual(module._redact_diagnostic_value, redact_value)
        value = module._redact_diagnostic_value({"SECRET": "x", "ok": "Bearer not-here"})
        self.assertEqual(value["SECRET"], REDACTION_MARKER)


# --------------------------------------------------------------------------- #
# box.py: planted secrets in remote (doctl/ssh) output emerge redacted.
# --------------------------------------------------------------------------- #

class BoxRemoteOutputRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.box = SourceFileLoader(
            "skillbox_box_redaction_probe",
            str(SCRIPTS_DIR / "box.py"),
        ).load_module()

    def _completed(self, stdout: str = "", stderr: str = "", code: int = 0):
        return subprocess.CompletedProcess(["ssh"], code, stdout=stdout, stderr=stderr)

    def test_ssh_cmd_redacts_tailscale_authkey_in_remote_output(self) -> None:
        planted_out = "TAILSCALE_IPV4=100.64.0.1 SKILLBOX_TS_AUTHKEY=tskey-PLANTED123"
        planted_err = "warn: doctl token dop_v1_PLANTEDdeadbeef rejected"
        with mock.patch.object(
            self.box.subprocess,
            "run",
            return_value=self._completed(stdout=planted_out, stderr=planted_err),
        ):
            result = self.box.ssh_cmd("skillbox", "box.example.com", "env")
        self.assertNotIn("tskey-PLANTED123", result.stdout)
        self.assertNotIn("dop_v1_PLANTEDdeadbeef", result.stderr)
        self.assertIn(REDACTION_MARKER, result.stdout)
        self.assertIn(REDACTION_MARKER, result.stderr)
        # Benign context (the tailnet IP) survives.
        self.assertIn("TAILSCALE_IPV4=100.64.0.1", result.stdout)

    def test_doctl_redacts_token_in_output(self) -> None:
        planted = '{"error":"unauthorized","detail":"SKILLBOX_DO_TOKEN=dop_v1_LEAKED9999"}'
        with mock.patch.object(
            self.box.subprocess,
            "run",
            return_value=self._completed(stdout=planted, stderr="auth_token=secret-tail"),
        ):
            result = self.box.doctl("compute", "droplet", "list", "--output", "json")
        self.assertNotIn("dop_v1_LEAKED9999", result.stdout)
        self.assertNotIn("secret-tail", result.stderr)
        self.assertIn(REDACTION_MARKER, result.stdout)

    def test_clean_json_output_remains_parseable_after_redaction(self) -> None:
        import json

        clean = '{"id":123,"name":"skillbox-box","status":"active"}'
        with mock.patch.object(
            self.box.subprocess,
            "run",
            return_value=self._completed(stdout=clean),
        ):
            result = self.box.doctl("compute", "droplet", "list")
        # No secret shapes -> output unchanged and still valid JSON.
        self.assertEqual(result.stdout, clean)
        self.assertEqual(json.loads(result.stdout)["name"], "skillbox-box")

    def test_called_process_error_output_is_redacted(self) -> None:
        err = subprocess.CalledProcessError(
            1, ["doctl"], output="SKILLBOX_DO_TOKEN=dop_v1_FAILTOKEN", stderr="authkey=tskey-FAIL"
        )
        with mock.patch.object(self.box.subprocess, "run", side_effect=err):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                self.box.run(["doctl", "account", "get"])
        self.assertNotIn("dop_v1_FAILTOKEN", ctx.exception.stdout)
        self.assertNotIn("tskey-FAIL", ctx.exception.stderr)


if __name__ == "__main__":
    unittest.main()
