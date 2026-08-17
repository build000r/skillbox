"""DCG operator-docs contract (skillbox-dcg-operator-docs-rollout-ncpa).

Operator docs for a security control fail in two directions, and both are
tested here:

* **Unsafe advice.** A doc that tells an operator to curl-pipe a shell, install
  an unpinned build, bypass hook trust, or hand-edit a managed hook file is
  worse than no doc, because it is followed.
* **Overclaiming.** DCG is a PreToolUse hook. It does not see a direct shell,
  and it does not see inside a Codex ``unified_exec`` session. A doc that says
  "every command is guarded" would be false, and would be believed. The proof
  in ``scripts/dcg-protocol-e2e.py`` records ``unified_exec`` with
  ``guarded=false``; the docs must not claim more than that evidence.

Commands cited in the docs are checked against the real Makefile targets and the
real ``manage.py`` parser, so the docs cannot drift into describing a CLI that
does not exist.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

README = ROOT_DIR / "README.md"
OPERATIONS = ROOT_DIR / "docs" / "operations.md"
TROUBLESHOOTING = ROOT_DIR / "docs" / "troubleshooting.md"
DOCS = (README, OPERATIONS, TROUBLESHOOTING)

BYPASS_FLAG = "--dangerously-bypass-hook-trust"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(path: Path) -> str:
    """Doc text with wrapping and markdown emphasis removed.

    Prose is hard-wrapped and uses **bold**, so a naive substring check would
    miss `direct\\nshell` and a naive "do not" check would miss `do **not**`.
    Normalizing here keeps the assertions about MEANING rather than layout.
    """
    text = _text(path).replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).lower()


def _dcg_lines(path: Path) -> list[str]:
    """Lines from the DCG-relevant parts of a doc.

    Scoped so an unrelated section elsewhere in a large shared file cannot fail
    a DCG docs test (or, worse, silently satisfy one).
    """
    lines = []
    for line in _text(path).splitlines():
        lowered = line.lower()
        if "dcg" in lowered or "hook" in lowered or "unified_exec" in lowered:
            lines.append(line)
    return lines


class RequiredCoverageTests(unittest.TestCase):
    """Every topic the bead names must actually be documented."""

    def test_required_tokens_are_present(self) -> None:
        blob = "\n".join(_text(path) for path in DOCS)
        for token in (
            "CODEX_HOOK_TRUST_REQUIRED",
            "dcg-reconcile --remove",
            "direct shell",
            "unified_exec",
            "rollback",
        ):
            with self.subTest(token=token):
                self.assertIn(token, blob, f"docs must mention {token}")

    def test_operations_documents_every_lifecycle_topic(self) -> None:
        text = _text(OPERATIONS)
        for topic in (
            "make dcg-reconcile",     # canonical setup
            "make dcg-verify",        # verify/status
            "make dcg-relinquish",    # uninstall / opt-out
            "--purge",                # full opt-out
            "--dry-run",              # preview before change
            "Upgrade",
            "Rollback",
        ):
            with self.subTest(topic=topic):
                self.assertIn(topic, text)

    def test_verify_states_are_documented_with_exit_codes(self) -> None:
        text = _text(OPERATIONS)
        for state in ("healthy", "changed", "needs-operator-action", "unsupported", "failed"):
            with self.subTest(state=state):
                self.assertIn(state, text)

    def test_troubleshooting_documents_recovery(self) -> None:
        text = _text(TROUBLESHOOTING)
        self.assertIn("DCG recovery", text)
        self.assertIn("codex_trust", text)
        self.assertIn("rollback", text)

    def test_readme_points_at_the_operations_anchor(self) -> None:
        self.assertIn("docs/operations.md#dcg-destructive-command-guard", _text(README))

    def test_operations_anchor_target_exists(self) -> None:
        """A README link to a heading that does not exist is a broken promise."""
        self.assertIn("## DCG (Destructive Command Guard)", _text(OPERATIONS))


class UnsafeAdviceTests(unittest.TestCase):
    """Failure probes: the doc must never TELL an operator to do these."""

    def test_no_curl_pipe_shell(self) -> None:
        pattern = re.compile(r"curl[^\n|]*\|\s*(sudo\s+)?(ba)?sh", re.IGNORECASE)
        for path in DOCS:
            with self.subTest(doc=path.name):
                for line in _dcg_lines(path):
                    self.assertIsNone(
                        pattern.search(line),
                        f"{path.name} advises curl-pipe-shell: {line.strip()!r}",
                    )

    def test_no_unpinned_latest_install(self) -> None:
        pattern = re.compile(r"(install|download|fetch|upgrade)[^\n.]*\blatest\b", re.IGNORECASE)
        for path in DOCS:
            with self.subTest(doc=path.name):
                for line in _dcg_lines(path):
                    if "never" in line.lower() or "not " in line.lower():
                        continue  # a prohibition is the correct way to mention it
                    self.assertIsNone(
                        pattern.search(line),
                        f"{path.name} advises an unpinned install: {line.strip()!r}",
                    )

    def test_bypass_hook_trust_only_ever_appears_as_a_prohibition(self) -> None:
        for path in DOCS:
            text = _text(path)
            if BYPASS_FLAG not in text:
                continue
            for line in text.splitlines():
                if BYPASS_FLAG not in line:
                    continue
                normalized = re.sub(r"\s+", " ", line.replace("*", "").replace("`", "")).lower()
                with self.subTest(doc=path.name, line=line.strip()[:60]):
                    self.assertRegex(
                        normalized,
                        r"never|do not|don't|forbidden|refuse",
                        f"{path.name} mentions {BYPASS_FLAG} without forbidding it",
                    )

    def test_no_hidden_manual_edits_of_managed_files(self) -> None:
        """Hand-editing a managed hook drifts on next converge and is invisible
        to the ledger, so rollback cannot undo it."""
        managed = ("settings.json", "hooks.json", "config.toml")
        advise = re.compile(r"\b(edit|modify|add|append|paste|write)\b", re.IGNORECASE)
        for path in DOCS:
            with self.subTest(doc=path.name):
                for line in _dcg_lines(path):
                    if not any(name in line for name in managed):
                        continue
                    if not advise.search(line):
                        continue
                    self.assertRegex(
                        line.lower(),
                        r"never|do not|don't|instead|rather than|prepares|persists|detect",
                        f"{path.name} appears to advise hand-editing a managed file: {line.strip()!r}",
                    )


class NoOverclaimTests(unittest.TestCase):
    """The docs must not claim more coverage than ln4z's proof supports."""

    def test_coverage_gaps_are_stated_in_every_operator_doc(self) -> None:
        for path in (README, OPERATIONS, TROUBLESHOOTING):
            with self.subTest(doc=path.name):
                text = _flat(path)
                self.assertIn("direct shell", text)
                self.assertIn("unified_exec", text)

    def test_no_total_coverage_claims(self) -> None:
        forbidden = re.compile(
            r"(guards|protects|blocks|catches|intercepts)\s+(all|every)\s+(command|shell)",
            re.IGNORECASE,
        )
        for path in DOCS:
            with self.subTest(doc=path.name):
                for line in _text(path).splitlines():
                    self.assertIsNone(
                        forbidden.search(line),
                        f"{path.name} overclaims coverage: {line.strip()!r}",
                    )

    def test_healthy_is_explicitly_not_equal_to_fully_guarded(self) -> None:
        """The single most likely misreading, so it is stated outright."""
        for path in (README, TROUBLESHOOTING):
            with self.subTest(doc=path.name):
                self.assertIn("not that nothing runs unguarded", _flat(path))

    def test_codex_trust_is_described_as_a_human_step(self) -> None:
        self.assertIn("codex refuses to run a hook it has not trusted", _flat(OPERATIONS))
        self.assertIn("never writes a trust hash", _flat(OPERATIONS))


class CitedCommandsExistTests(unittest.TestCase):
    """Docs may only cite commands this repo actually has."""

    def test_cited_make_targets_exist(self) -> None:
        makefile = _text(ROOT_DIR / "Makefile")
        for target in ("dcg-reconcile", "dcg-verify", "dcg-relinquish"):
            with self.subTest(target=target):
                self.assertRegex(makefile, rf"(?m)^{re.escape(target)}:")

    def test_cited_manage_flags_exist(self) -> None:
        result = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "dcg-reconcile", "--help"],
            cwd=ROOT_DIR, capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(ENV_MANAGER_DIR), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        help_text = result.stdout
        for flag in ("--action", "--remove", "--purge", "--dry-run", "--format"):
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)

    def test_documented_states_match_the_implementation(self) -> None:
        from runtime_manager import dcg_reconcile as R

        text = _text(OPERATIONS)
        for state in (
            R.STATE_HEALTHY, R.STATE_CHANGED, R.STATE_NEEDS_OPERATOR,
            R.STATE_UNSUPPORTED, R.STATE_FAILED,
        ):
            with self.subTest(state=state):
                self.assertIn(state, text)

    def test_documented_trust_exit_code_matches_the_implementation(self) -> None:
        from runtime_manager import dcg_reconcile as R

        self.assertEqual(3, R.EXIT_NEEDS_OPERATOR)
        self.assertIn("exit 3", _text(OPERATIONS).lower())

    def test_documented_bypass_flag_matches_the_implementation(self) -> None:
        from runtime_manager import dcg_reconcile as R

        self.assertEqual(BYPASS_FLAG, R.BYPASS_FLAG)
        self.assertIn(R.BYPASS_FLAG, _text(OPERATIONS))

    def test_codex_hook_trust_required_is_labelled_as_a_condition_not_a_constant(self) -> None:
        """It is an operator-facing NAME. Claiming it is a source constant would
        send operators grepping for something that does not exist."""
        from runtime_manager import dcg_reconcile as R

        self.assertFalse(
            hasattr(R, "CODEX_HOOK_TRUST_REQUIRED"),
            "if this ever becomes a real constant, update the docs wording",
        )
        self.assertIn("not a code in the source", _flat(OPERATIONS))


if __name__ == "__main__":
    unittest.main()
