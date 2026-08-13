"""Contract tests for atomic, merge-safe DCG hook convergence.

Every test runs against a DISPOSABLE HOME materialized into a temp directory
from ``tests/fixtures/dcg_reconcile``. Nothing here reads or writes the real
``$HOME``: :func:`runtime_manager.dcg_reconcile.layout` never consults the
environment and the CLI requires an explicit ``--home``, so a fixture home is
the only home these tests can reach.

The five properties under test, in the order the bead names them:

1. merge safety   -- Claude's ``rch`` hook and every unrelated JSON/TOML byte
                     survive convergence, relinquish, and rollback
2. atomicity      -- a malformed config refuses the WHOLE run; no file is
                     half-written, not even the ones that parse
3. idempotence    -- a second apply is byte-identical across the whole home
4. Codex trust    -- absent/stale trust is needs-operator-action, never healthy,
                     and the bypass flag is refused and never emitted
5. reversibility  -- rollback restores pre-apply bytes; relinquish removes only
                     DCG-owned entries and is idempotent
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import dcg_distribution as DD  # noqa: E402
from runtime_manager import dcg_policy as DP  # noqa: E402
from runtime_manager import dcg_reconcile as DR  # noqa: E402
from runtime_manager.errors import SkillboxError, ValidationError  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_reconcile"
BIN_TOKEN = "@DCG_BIN@"

COMPOSE_FILE = ROOT_DIR / "docker-compose.yml"

# The four home subtrees that must survive a container being replaced.
PERSISTED_SUBTREES = (".claude", ".codex", ".grok", ".local", ".config/dcg")


def _tree(root: Path) -> dict[str, bytes | str]:
    """Every file under ``root`` as {relative path: bytes} (symlinks as targets)."""
    snapshot: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[rel] = f"symlink -> {os.readlink(path)}"
        elif path.is_file():
            snapshot[rel] = path.read_bytes()
        else:
            snapshot[rel] = "dir"
    return snapshot


class _HomeCase(unittest.TestCase):
    """Materializes a fixture home into a temp dir and points a binary at it."""

    def materialize(self, case: str, *, home_name: str = "home") -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="dcg-reconcile-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / home_name
        source = FIXTURES / case / "home"
        if source.is_dir():
            shutil.copytree(source, home, symlinks=True)
        else:
            home.mkdir(parents=True)
        binary = home / DR.DEFAULT_BINARY_RELPATH
        for path in sorted(home.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if BIN_TOKEN in text:
                path.write_text(text.replace(BIN_TOKEN, str(binary)), encoding="utf-8")
        if binary.is_file():
            binary.chmod(0o755)
        return home, binary

    def expected(self, case: str, relpath: str, binary: Path) -> str:
        text = (FIXTURES / case / "expected" / relpath).read_text(encoding="utf-8")
        return text.replace(BIN_TOKEN, str(binary))


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


class HookOwnershipTests(unittest.TestCase):
    def test_every_dcg_spelling_is_owned(self) -> None:
        for command in (
            "dcg",
            "/home/sandbox/.local/bin/dcg",
            "/usr/local/bin/dcg hook",
            "command -v dcg >/dev/null 2>&1 && dcg || true",
            "dcg --robot",
        ):
            with self.subTest(command=command):
                self.assertTrue(DR.is_dcg_command(command))

    def test_unrelated_hooks_are_never_owned(self) -> None:
        for command in (
            "/home/sandbox/.local/bin/rch",
            "rch hook",
            "bash scripts/guard-destructive-op.sh",
            "/opt/dcg-tools/run.sh",
            "dcgfoo",
            "python3 /opt/forge/score.py",
            "",
            None,
        ):
            with self.subTest(command=command):
                self.assertFalse(DR.is_dcg_command(command))

    def test_canonical_shapes_match_upstream_install(self) -> None:
        group = DR.claude_matcher_group("/x/dcg")
        self.assertEqual(group, {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/dcg"}]})
        grok = DR.grok_hook_document("/x/dcg")
        entry = grok["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(entry["timeout"], DR.GROK_HOOK_TIMEOUT)
        self.assertIn("Destructive Command Guard", grok["description"])


# ---------------------------------------------------------------------------
# Absent -> converged, and byte-stable on the second run
# ---------------------------------------------------------------------------


class AbsentHomeTests(_HomeCase):
    def test_apply_creates_all_four_artifacts(self) -> None:
        home, binary = self.materialize("empty")
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_CHANGED)
        self.assertTrue((home / DR.CLAUDE_SETTINGS_RELPATH).is_file())
        self.assertTrue((home / DR.CODEX_HOOKS_RELPATH).is_file())
        self.assertTrue((home / DR.GROK_HOOK_RELPATH).is_file())
        self.assertTrue((home / DR.POLICY_RELPATH).is_file())

        settings = json.loads((home / DR.CLAUDE_SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(settings["hooks"]["PreToolUse"], [DR.claude_matcher_group(binary)])

        policy = (home / DR.POLICY_RELPATH).read_text(encoding="utf-8")
        self.assertEqual(policy, DP.render())
        self.assertIs(DP.validate_rendered(policy).fail_closed, True)

    def test_second_apply_is_byte_identical_across_the_whole_home(self) -> None:
        home, binary = self.materialize("empty")
        first = DR.apply(home, binary=binary)
        snapshot = _tree(home)
        second = DR.apply(home, binary=binary)
        self.assertEqual(first["result"], DR.RESULT_CHANGED)
        self.assertEqual(second["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(second["changed"], [])
        self.assertEqual(_tree(home), snapshot)
        self.assertEqual(first["state_digest"], second["state_digest"])

    def test_third_apply_still_records_no_new_backup(self) -> None:
        home, binary = self.materialize("empty")
        DR.apply(home, binary=binary)
        DR.apply(home, binary=binary)
        DR.apply(home, binary=binary)
        backups = sorted(p.name for p in (home / ".config/dcg/backups").iterdir())
        self.assertEqual(backups, ["0001"])

    def test_dry_run_writes_nothing(self) -> None:
        home, binary = self.materialize("empty")
        before = _tree(home)
        payload = DR.apply(home, binary=binary, dry_run=True)
        self.assertEqual(payload["result"], DR.RESULT_CHANGED)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(_tree(home), before)

    def test_verify_reports_drift_without_writing(self) -> None:
        home, binary = self.materialize("empty")
        before = _tree(home)
        payload = DR.verify(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(payload["status"], DR.STATE_NEEDS_OPERATOR)
        self.assertTrue(payload["pending_changes"])
        self.assertEqual(_tree(home), before)
        DR.apply(home, binary=binary)
        after = DR.verify(home, binary=binary)
        self.assertEqual(after["pending_changes"], [])


# ---------------------------------------------------------------------------
# Merge safety
# ---------------------------------------------------------------------------


class UnrelatedContentTests(_HomeCase):
    def test_claude_rch_hook_and_unrelated_keys_survive_byte_identical(self) -> None:
        home, binary = self.materialize("host_unrelated")
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        before_text = settings.read_text(encoding="utf-8")
        codex_config_before = (home / DR.CODEX_CONFIG_RELPATH).read_bytes()
        grok_other_before = (home / ".grok/hooks/other.json").read_bytes()

        payload = DR.apply(home, binary=binary)
        self.assertTrue(payload["unrelated_preserved"])

        after_text = settings.read_text(encoding="utf-8")
        self.assertEqual(after_text, self.expected("host_unrelated", ".claude/settings.json", binary))

        # Line-level proof: convergence is PURE ADDITION. Every line that was in
        # the file before is still there, byte for byte, in the same order.
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        kept = [line for line in after_lines if line in before_lines]
        self.assertEqual(
            [line for line in after_lines if line not in before_lines],
            [f'            "command": "{binary}"'],
        )
        self.assertEqual(len(kept), len(after_lines) - 1)

        # The rch hook survives as a whole entry, not just as matching text.
        document = json.loads(after_text)
        groups = document["hooks"]["PreToolUse"]
        self.assertIn(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/sandbox/.local/bin/rch"}]},
            groups,
        )
        self.assertEqual(document["model"], "claude-opus-5[1m]")
        self.assertEqual(document["permissions"]["allow"], ["Bash(br show *)"])
        self.assertEqual(len(document["hooks"]["SessionEnd"]), 1)

        # Codex TOML is read-only to this module, and an unrelated Grok hook file
        # is not ours to rewrite.
        self.assertEqual((home / DR.CODEX_CONFIG_RELPATH).read_bytes(), codex_config_before)
        self.assertEqual((home / ".grok/hooks/other.json").read_bytes(), grok_other_before)

    def test_existing_indentation_is_preserved(self) -> None:
        home, binary = self.materialize("empty")
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"model": "x", "hooks": {}}, indent=4) + "\n", encoding="utf-8")
        DR.apply(home, binary=binary)
        text = settings.read_text(encoding="utf-8")
        self.assertIn('\n    "model": "x"', text)
        self.assertTrue(text.endswith("\n"))

    def test_missing_trailing_newline_convention_is_preserved(self) -> None:
        home, binary = self.materialize("empty")
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"model": "x"}, indent=2), encoding="utf-8")
        DR.apply(home, binary=binary)
        self.assertFalse(settings.read_text(encoding="utf-8").endswith("\n"))


class DuplicateAndStaleTests(_HomeCase):
    def test_duplicate_dcg_hooks_collapse_to_one(self) -> None:
        home, binary = self.materialize("host_duplicate")
        payload = DR.apply(home, binary=binary)
        claude = next(item for item in payload["agents"] if item["agent"] == "claude")
        self.assertEqual(claude["duplicates_removed"], 2)
        text = (home / DR.CLAUDE_SETTINGS_RELPATH).read_text(encoding="utf-8")
        self.assertEqual(text, self.expected("host_duplicate", ".claude/settings.json", binary))
        self.assertEqual(text.count(str(binary)), 1)
        # The unrelated telemetry hook that shared a group with a dcg entry stays.
        self.assertIn("/opt/telemetry/record", text)
        self.assertIn("/home/sandbox/.local/bin/rch", text)

    def test_stale_commands_converge_to_the_pinned_binary(self) -> None:
        home, binary = self.materialize("host_stale")
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_CHANGED)
        for relpath in (DR.CLAUDE_SETTINGS_RELPATH, DR.CODEX_HOOKS_RELPATH, DR.GROK_HOOK_RELPATH):
            document = json.loads((home / relpath).read_text(encoding="utf-8"))
            entry = document["hooks"]["PreToolUse"][0]["hooks"][0]
            with self.subTest(relpath=relpath):
                self.assertEqual(entry["command"], str(binary))
        grok = json.loads((home / DR.GROK_HOOK_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(grok["description"], DR.GROK_DESCRIPTION)
        self.assertEqual(grok["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], DR.GROK_HOOK_TIMEOUT)
        self.assertEqual(DR.apply(home, binary=binary)["result"], DR.RESULT_UNCHANGED)

    def test_binary_not_named_dcg_still_converges_exactly_once(self) -> None:
        """Regression: a versioned binary name must not append a hook per run."""
        home, _binary = self.materialize("empty")
        versioned = home / ".local/bin" / f"dcg-{DD.DCG_VERSION}"
        versioned.parent.mkdir(parents=True, exist_ok=True)
        versioned.write_text("#!/bin/sh\necho 0.6.7\n", encoding="utf-8")
        versioned.chmod(0o755)

        first = DR.apply(home, binary=versioned)
        second = DR.apply(home, binary=versioned)
        self.assertEqual(first["result"], DR.RESULT_CHANGED)
        self.assertEqual(second["result"], DR.RESULT_UNCHANGED)
        for relpath in (DR.CLAUDE_SETTINGS_RELPATH, DR.CODEX_HOOKS_RELPATH, DR.GROK_HOOK_RELPATH):
            document = json.loads((home / relpath).read_text(encoding="utf-8"))
            with self.subTest(relpath=relpath):
                self.assertEqual(len(document["hooks"]["PreToolUse"]), 1)

    def test_unknown_keys_on_an_adopted_entry_are_preserved(self) -> None:
        home, binary = self.materialize("empty")
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "note": "operator annotation",
                                "hooks": [{"type": "command", "command": "dcg", "timeout": 42}],
                            }
                        ]
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        DR.apply(home, binary=binary)
        group = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]
        self.assertEqual(group["note"], "operator annotation")
        self.assertEqual(group["hooks"][0]["timeout"], 42)
        self.assertEqual(group["hooks"][0]["command"], str(binary))


class SpacesAndSymlinkTests(_HomeCase):
    def test_home_with_spaces_converges(self) -> None:
        home, binary = self.materialize("host_unrelated", home_name="managed home (box 1)")
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_CHANGED)
        self.assertIn(" ", str(home))
        document = json.loads((home / DR.CLAUDE_SETTINGS_RELPATH).read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["PreToolUse"][-1], DR.claude_matcher_group(binary))
        self.assertEqual(DR.apply(home, binary=binary)["result"], DR.RESULT_UNCHANGED)

    def test_symlinked_settings_file_keeps_its_link(self) -> None:
        home, binary = self.materialize("host_unrelated")
        real = home.parent / "config-farm" / "claude-settings.json"
        real.parent.mkdir(parents=True, exist_ok=True)
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        shutil.move(str(settings), str(real))
        settings.symlink_to(real)

        DR.apply(home, binary=binary)

        self.assertTrue(settings.is_symlink(), "the config farm symlink must survive convergence")
        self.assertEqual(Path(os.readlink(settings)), real)
        document = json.loads(real.read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["PreToolUse"][-1], DR.claude_matcher_group(binary))


# ---------------------------------------------------------------------------
# Malformed and unsupported
# ---------------------------------------------------------------------------


class MalformedConfigTests(_HomeCase):
    def test_malformed_json_refuses_the_whole_run(self) -> None:
        home, binary = self.materialize("host_malformed_json")
        before = _tree(home)
        with self.assertRaises(ValidationError) as caught:
            DR.apply(home, binary=binary)
        self.assertEqual(caught.exception.code, DR.DCG_RECONCILE_MALFORMED_CONFIG)
        # Not one byte moved -- including the codex/grok/policy files this run
        # would otherwise have created.
        self.assertEqual(_tree(home), before)
        self.assertFalse((home / DR.CODEX_HOOKS_RELPATH).exists())
        self.assertFalse((home / DR.GROK_HOOK_RELPATH).exists())
        self.assertFalse((home / DR.POLICY_RELPATH).exists())

    def test_malformed_codex_toml_refuses_the_whole_run(self) -> None:
        home, binary = self.materialize("host_malformed_toml")
        before = _tree(home)
        with self.assertRaises(ValidationError) as caught:
            DR.apply(home, binary=binary)
        self.assertEqual(caught.exception.code, DR.DCG_RECONCILE_MALFORMED_CONFIG)
        self.assertIn("config.toml", caught.exception.context["path"])
        self.assertEqual(_tree(home), before)

    def test_malformed_policy_render_is_never_overwritten(self) -> None:
        home, binary = self.materialize("empty")
        policy = home / DR.POLICY_RELPATH
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(DP.GENERATED_MARKER + "\n[packs\nenabled = [\n", encoding="utf-8")
        before = _tree(home)
        with self.assertRaises(ValidationError):
            DR.apply(home, binary=binary)
        self.assertEqual(_tree(home), before)

    def test_hand_owned_policy_is_left_to_the_operator(self) -> None:
        home, binary = self.materialize("empty")
        policy = home / DR.POLICY_RELPATH
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text('# my own config\n[packs]\nenabled = ["core.git"]\n', encoding="utf-8")
        before = policy.read_bytes()
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["policy"]["state"], DR.STATE_NEEDS_OPERATOR)
        self.assertEqual(policy.read_bytes(), before)
        self.assertTrue(any("hand-owned" in item or "Review" in item for item in payload["operator_actions"]))

    def test_malformed_ledger_refuses_rather_than_resetting_state(self) -> None:
        home, binary = self.materialize("empty")
        DR.apply(home, binary=binary)
        ledger = home / DR.LEDGER_RELPATH
        ledger.write_text("{not json", encoding="utf-8")
        before = _tree(home)
        with self.assertRaises(ValidationError):
            DR.apply(home, binary=binary)
        self.assertEqual(_tree(home), before)


class SitePolicyReconcileTests(_HomeCase):
    TRUST_HASH = "a" * 64

    def _site_payload(self, *, reason: str = "Use the reviewed safe path.") -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": "generic-estate",
            "packs": ["strict_git"],
            "allowlist": ["cargo test --package safe-fixture"],
            "blocklist": [
                {"pattern": r"\bsite-danger\b", "reason": reason},
            ],
            "agents": {
                "default": {"trust_level": "medium"},
                "unknown": {
                    "trust_level": "low",
                    "disabled_allowlist": False,
                    "extra_packs": ["strict_git"],
                },
            },
        }

    def _write_site(self, path: Path, payload: dict[str, object] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload or self._site_payload(), indent=2) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _desired(self, site: Path) -> str:
        loaded = DP.load_site_policy_file(site)
        return DP.render(site_policies=[loaded])

    def _hand_owned(self, desired: str) -> str:
        return "\n".join(
            line
            for line in desired.splitlines()
            if not line.startswith("#")
        ).lstrip("\n") + "\n"

    def _write_trust(self, home: Path, value: str) -> None:
        config = home / DR.CODEX_CONFIG_RELPATH
        config.parent.mkdir(parents=True, exist_ok=True)
        base = config.read_text(encoding="utf-8") if config.is_file() else 'model = "fixture"\n'
        base = base.split('[hooks.state.')[0].rstrip("\n")
        config.write_text(
            base
            + f'\n\n[hooks.state."user:PreToolUse:0"]\nenabled = true\ntrusted_hash = "{value}"\n',
            encoding="utf-8",
        )

    def test_lossless_adoption_is_idempotent_and_rollback_restores_hand_owned_bytes(self) -> None:
        home, binary = self.materialize("empty")
        site = self._write_site(home.parent / "private" / "site.json")
        policy_path = home / DR.POLICY_RELPATH
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        hand_owned = self._hand_owned(self._desired(site))
        policy_path.write_text(hand_owned, encoding="utf-8")

        first = DR.apply(
            home,
            binary=binary,
            site_policy_paths=[site],
            adopt_policy=True,
        )
        self.assertEqual(first["result"], DR.RESULT_CHANGED)
        self.assertTrue(policy_path.read_text(encoding="utf-8").startswith(DP.GENERATED_MARKER))
        self.assertEqual(
            DR.apply(home, binary=binary, site_policy_paths=[site])["result"],
            DR.RESULT_UNCHANGED,
        )

        ledger = json.loads((home / DR.LEDGER_RELPATH).read_text(encoding="utf-8"))
        serialized = json.dumps(ledger["policy"]["site_policy"], sort_keys=True)
        self.assertIn("generic-estate", serialized)
        self.assertNotIn(str(site), serialized)
        self.assertNotIn("site-danger", serialized)

        DR.rollback(home, binary=binary)
        self.assertEqual(policy_path.read_text(encoding="utf-8"), hand_owned)

    def test_adoption_rejects_every_lossy_section_without_writing(self) -> None:
        mutations = {
            "general": lambda text: text.replace("fail_closed = true", "fail_closed = false"),
            "packs": lambda text: text.replace(
                'enabled = ["core.git", "core.filesystem", "strict_git"]',
                'enabled = ["core.git", "core.filesystem", "strict_git", "remote.ssh"]',
            ),
            "allowlist": lambda text: text.replace("safe-fixture", "different-fixture"),
            "blocklist": lambda text: text.replace("Use the reviewed safe path.", "Different reason."),
            "agents": lambda text: text.replace('trust_level = "medium"', 'trust_level = "high"', 1),
        }
        for section, mutate in mutations.items():
            with self.subTest(section=section):
                home, binary = self.materialize("empty", home_name=f"home-{section}")
                site = self._write_site(home.parent / f"private-{section}" / "site.json")
                policy_path = home / DR.POLICY_RELPATH
                policy_path.parent.mkdir(parents=True, exist_ok=True)
                policy_path.write_text(
                    mutate(self._hand_owned(self._desired(site))), encoding="utf-8"
                )
                before = _tree(home)
                with self.assertRaises(ValidationError) as caught:
                    DR.apply(
                        home,
                        binary=binary,
                        site_policy_paths=[site],
                        adopt_policy=True,
                    )
                self.assertEqual(
                    caught.exception.code, DR.DCG_RECONCILE_POLICY_ADOPTION_MISMATCH
                )
                self.assertIn(section, caught.exception.context["mismatched_sections"])
                self.assertEqual(_tree(home), before)

    def test_adopted_site_input_is_required_and_malformed_input_writes_nothing(self) -> None:
        home, binary = self.materialize("empty")
        site = self._write_site(home.parent / "private" / "site.json")
        DR.apply(home, binary=binary, site_policy_paths=[site])
        before = _tree(home)
        with self.assertRaises(ValidationError) as caught:
            DR.apply(home, binary=binary)
        self.assertEqual(caught.exception.code, DR.DCG_RECONCILE_SITE_POLICY_REQUIRED)
        self.assertEqual(_tree(home), before)

        site.write_text("{not json", encoding="utf-8")
        site.chmod(0o600)
        with self.assertRaises(ValidationError):
            DR.apply(home, binary=binary, site_policy_paths=[site])
        self.assertEqual(_tree(home), before)

    def test_policy_only_site_change_preserves_hooks_and_codex_trust(self) -> None:
        home, binary = self.materialize("container_home")
        site = self._write_site(home.parent / "private" / "site.json")
        DR.apply(home, binary=binary, site_policy_paths=[site])
        self._write_trust(home, self.TRUST_HASH)
        trusted = DR.apply(home, binary=binary, site_policy_paths=[site])
        self.assertEqual(trusted["codex_trust"], DR.CODEX_TRUST_TRUSTED)
        hook_bytes = {
            relpath: (home / relpath).read_bytes()
            for relpath in (
                DR.CLAUDE_SETTINGS_RELPATH,
                DR.CODEX_HOOKS_RELPATH,
                DR.GROK_HOOK_RELPATH,
                DR.CODEX_CONFIG_RELPATH,
            )
        }

        self._write_site(site, self._site_payload(reason="Use the revised safe path."))
        changed = DR.apply(home, binary=binary, site_policy_paths=[site])
        self.assertEqual(changed["result"], DR.RESULT_CHANGED)
        self.assertEqual(changed["codex_trust"], DR.CODEX_TRUST_TRUSTED)
        for relpath, before in hook_bytes.items():
            self.assertEqual((home / relpath).read_bytes(), before)

    def test_same_site_renders_identical_policy_across_homes_and_survives_relinquish(self) -> None:
        site_root = Path(tempfile.mkdtemp(prefix="dcg-private-site-"))
        self.addCleanup(shutil.rmtree, site_root, ignore_errors=True)
        site = self._write_site(site_root / "site.json")
        rendered: list[bytes] = []
        for name in ("first-home", "second-home"):
            home, binary = self.materialize("empty", home_name=name)
            DR.apply(home, binary=binary, site_policy_paths=[site])
            rendered.append((home / DR.POLICY_RELPATH).read_bytes())
        self.assertEqual(rendered[0], rendered[1])

        home, binary = self.materialize("empty", home_name="source-in-home")
        in_home_site = self._write_site(home / ".config/dcg/skillbox-site-policy.json")
        source_before = in_home_site.read_bytes()
        DR.apply(home, binary=binary, site_policy_paths=[in_home_site])
        DR.relinquish(home, binary=binary, purge=True)
        self.assertEqual(in_home_site.read_bytes(), source_before)


class UnsupportedShapeTests(_HomeCase):
    def test_unsupported_claude_shape_is_reported_and_left_alone(self) -> None:
        home, binary = self.materialize("host_unsupported")
        settings = home / DR.CLAUDE_SETTINGS_RELPATH
        before = settings.read_bytes()
        payload = DR.apply(home, binary=binary)
        claude = next(item for item in payload["agents"] if item["agent"] == "claude")
        self.assertEqual(claude["state"], DR.STATE_UNSUPPORTED)
        self.assertEqual(payload["status"], DR.STATE_UNSUPPORTED)
        self.assertEqual(settings.read_bytes(), before)
        # The agents we DO understand still converge.
        self.assertTrue((home / DR.CODEX_HOOKS_RELPATH).is_file())
        self.assertTrue((home / DR.GROK_HOOK_RELPATH).is_file())

    def test_unsupported_platform_short_circuits_without_writing(self) -> None:
        home, binary = self.materialize("empty")
        before = _tree(home)
        payload = DR.apply(home, binary=binary, platform="Windows/AMD64")
        self.assertEqual(payload["status"], DR.STATE_UNSUPPORTED)
        self.assertEqual(payload["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(_tree(home), before)


# ---------------------------------------------------------------------------
# Codex trust
# ---------------------------------------------------------------------------


class CodexTrustTests(_HomeCase):
    TRUST_HASH = "9f1c0c2a5b7d4e6f8a0b1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f607"

    def _write_trust(self, home: Path, value: str) -> None:
        """Stand in for Codex persisting its OWN hook-trust hash."""
        config = home / DR.CODEX_CONFIG_RELPATH
        config.parent.mkdir(parents=True, exist_ok=True)
        base = config.read_text(encoding="utf-8") if config.is_file() else 'model = "gpt-5.6-sol"\n'
        base = base.split('[hooks.state.')[0].rstrip("\n")
        config.write_text(
            base + f'\n\n[hooks.state."user:PreToolUse:0"]\nenabled = true\ntrusted_hash = "{value}"\n',
            encoding="utf-8",
        )

    def test_absent_trust_is_needs_operator_action_not_healthy(self) -> None:
        home, binary = self.materialize("container_home")
        payload = DR.apply(home, binary=binary)
        codex = next(item for item in payload["agents"] if item["agent"] == "codex")
        self.assertEqual(payload["codex_trust"], DR.CODEX_TRUST_ABSENT)
        self.assertEqual(codex["health"], DR.STATE_NEEDS_OPERATOR)
        self.assertNotEqual(payload["status"], DR.STATE_HEALTHY)
        self.assertEqual(payload["status"], DR.STATE_NEEDS_OPERATOR)
        self.assertIn(DR.CODEX_TRUST_ACTION, payload["operator_actions"])

    def test_trust_persisted_by_codex_makes_the_home_healthy(self) -> None:
        home, binary = self.materialize("container_home")
        DR.apply(home, binary=binary)  # writes hooks.json; trust is absent
        self._write_trust(home, self.TRUST_HASH)  # Codex persists it, not us
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["codex_trust"], DR.CODEX_TRUST_TRUSTED)
        self.assertEqual(payload["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(payload["status"], DR.STATE_HEALTHY)

    def test_trust_is_invalidated_when_we_rewrite_the_hook(self) -> None:
        home, binary = self.materialize("container_home")
        DR.apply(home, binary=binary)
        self._write_trust(home, self.TRUST_HASH)
        self.assertEqual(DR.apply(home, binary=binary)["codex_trust"], DR.CODEX_TRUST_TRUSTED)

        # The hook now points somewhere else: the persisted hash is stale.
        moved = home / ".local/bin/dcg-next"
        shutil.copy2(binary, moved)
        payload = DR.apply(home, binary=moved)
        self.assertEqual(payload["result"], DR.RESULT_CHANGED)
        self.assertEqual(payload["codex_trust"], DR.CODEX_TRUST_STALE)
        self.assertEqual(payload["status"], DR.STATE_NEEDS_OPERATOR)

        # It STAYS stale until Codex writes a different hash of its own accord.
        self.assertEqual(DR.apply(home, binary=moved)["codex_trust"], DR.CODEX_TRUST_STALE)
        self._write_trust(home, "0" * 64)
        self.assertEqual(DR.apply(home, binary=moved)["codex_trust"], DR.CODEX_TRUST_TRUSTED)

    def test_module_never_writes_the_codex_config(self) -> None:
        home, binary = self.materialize("container_home")
        config = home / DR.CODEX_CONFIG_RELPATH
        before = config.read_bytes()
        DR.apply(home, binary=binary)
        DR.relinquish(home, binary=binary)
        self.assertEqual(config.read_bytes(), before)
        self.assertNotIn("trusted_hash", config.read_text(encoding="utf-8"))

    def test_bypass_flag_is_refused_and_never_emitted(self) -> None:
        home, binary = self.materialize("container_home")
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = DR.main(["apply", "--home", str(home), DR.BYPASS_FLAG])
        self.assertIn(DR.DCG_RECONCILE_BYPASS_FORBIDDEN, captured.getvalue())
        self.assertEqual(code, DR.EXIT_FAILED)
        self.assertFalse((home / DR.CODEX_HOOKS_RELPATH).exists())

        DR.apply(home, binary=binary)
        for path in sorted(home.rglob("*")):
            if path.is_file() and not path.is_symlink():
                with self.subTest(path=path):
                    self.assertNotIn(DR.BYPASS_FLAG, path.read_bytes().decode("utf-8", "ignore"))

    def test_disabled_trust_entries_do_not_count(self) -> None:
        home, binary = self.materialize("container_home")
        DR.apply(home, binary=binary)
        config = home / DR.CODEX_CONFIG_RELPATH
        config.write_text(
            config.read_text(encoding="utf-8")
            + '\n[hooks.state."user:PreToolUse:0"]\nenabled = false\ntrusted_hash = "deadbeef"\n',
            encoding="utf-8",
        )
        self.assertEqual(DR.apply(home, binary=binary)["codex_trust"], DR.CODEX_TRUST_ABSENT)


# ---------------------------------------------------------------------------
# Binary contract
# ---------------------------------------------------------------------------


class BinaryContractTests(_HomeCase):
    def test_missing_binary_is_needs_operator_action_and_never_downloaded(self) -> None:
        home, binary = self.materialize("empty")
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["binary_state"]["state"], DR.STATE_NEEDS_OPERATOR)
        self.assertEqual(payload["binary_state"]["expected_version"], DD.DCG_VERSION)
        self.assertFalse(binary.exists())

    def test_pinned_binary_reports_healthy(self) -> None:
        home, binary = self.materialize("container_home")
        payload = DR.apply(home, binary=binary)
        self.assertEqual(payload["binary_state"]["state"], DR.STATE_HEALTHY)
        self.assertEqual(payload["binary_state"]["installed_version"], DD.DCG_VERSION)
        self.assertEqual(payload["binary_state"]["sha256"], DD.asset_pin().sha256)

    def test_binary_outside_the_default_path_is_linked_and_unlinked(self) -> None:
        home, binary = self.materialize("container_home")
        elsewhere = home / ".local/share/skillbox/dcg/dcg"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(binary), str(elsewhere))
        payload = DR.apply(home, binary=elsewhere)
        self.assertEqual(payload["binary_link"]["state"], DR.STATE_CHANGED)
        self.assertTrue(binary.is_symlink())
        self.assertEqual(Path(os.readlink(binary)), elsewhere)
        self.assertEqual(DR.apply(home, binary=elsewhere)["binary_link"]["state"], DR.STATE_HEALTHY)

        removal = DR.relinquish(home, binary=elsewhere)
        self.assertTrue(removal["binary_link_removed"])
        self.assertFalse(binary.exists())
        self.assertTrue(elsewhere.is_file(), "relinquish removes the link, never the binary")


# ---------------------------------------------------------------------------
# Relinquish and rollback
# ---------------------------------------------------------------------------


class RelinquishTests(_HomeCase):
    def test_relinquish_restores_unrelated_config_byte_for_byte(self) -> None:
        home, binary = self.materialize("host_unrelated")
        before = _tree(home)
        DR.apply(home, binary=binary)
        payload = DR.relinquish(home, binary=binary)

        self.assertEqual(payload["result"], DR.RESULT_REMOVED)
        self.assertTrue(payload["unrelated_preserved"])
        self.assertEqual(
            (home / DR.CLAUDE_SETTINGS_RELPATH).read_bytes(),
            before[DR.CLAUDE_SETTINGS_RELPATH],
        )
        self.assertEqual((home / DR.CODEX_CONFIG_RELPATH).read_bytes(), before[DR.CODEX_CONFIG_RELPATH])
        self.assertEqual((home / ".grok/hooks/other.json").read_bytes(), before[".grok/hooks/other.json"])
        # Files this module created are gone again.
        self.assertFalse((home / DR.CODEX_HOOKS_RELPATH).exists())
        self.assertFalse((home / DR.GROK_HOOK_RELPATH).exists())
        self.assertFalse((home / DR.POLICY_RELPATH).exists())

    def test_relinquish_is_idempotent(self) -> None:
        home, binary = self.materialize("host_unrelated")
        DR.apply(home, binary=binary)
        DR.relinquish(home, binary=binary)
        snapshot = _tree(home)
        second = DR.relinquish(home, binary=binary)
        self.assertEqual(second["result"], DR.RESULT_UNCHANGED)
        self.assertTrue(second["unrelated_preserved"])
        self.assertEqual(_tree(home), snapshot)

    def test_relinquish_keeps_a_pre_existing_dcg_free_file(self) -> None:
        home, binary = self.materialize("host_unrelated")
        before = (home / DR.CLAUDE_SETTINGS_RELPATH).read_bytes()
        payload = DR.relinquish(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_UNCHANGED)
        self.assertEqual((home / DR.CLAUDE_SETTINGS_RELPATH).read_bytes(), before)

    def test_purge_leaves_no_dcg_state_in_the_home(self) -> None:
        home, binary = self.materialize("host_unrelated")
        DR.apply(home, binary=binary)
        DR.relinquish(home, binary=binary, purge=True)
        self.assertFalse((home / ".config/dcg").exists())
        self.assertFalse((home / DR.LEDGER_RELPATH).exists())

    def test_relinquish_dry_run_writes_nothing(self) -> None:
        home, binary = self.materialize("host_unrelated")
        DR.apply(home, binary=binary)
        snapshot = _tree(home)
        payload = DR.relinquish(home, binary=binary, dry_run=True)
        self.assertEqual(payload["result"], DR.RESULT_REMOVED)
        self.assertEqual(_tree(home), snapshot)


class RollbackTests(_HomeCase):
    def test_rollback_restores_the_pre_apply_home(self) -> None:
        home, binary = self.materialize("host_unrelated")
        before = _tree(home)
        DR.apply(home, binary=binary)
        self.assertNotEqual(_tree(home), before)
        payload = DR.rollback(home, binary=binary)
        self.assertEqual(payload["result"], DR.RESULT_ROLLED_BACK)
        self.assertEqual(_tree(home), before)

    def test_rollback_after_relinquish_restores_the_converged_home(self) -> None:
        home, binary = self.materialize("host_unrelated")
        DR.apply(home, binary=binary)
        converged = _tree(home)
        DR.relinquish(home, binary=binary)
        DR.rollback(home, binary=binary)
        self.assertEqual(
            (home / DR.CLAUDE_SETTINGS_RELPATH).read_bytes(),
            converged[DR.CLAUDE_SETTINGS_RELPATH],
        )
        self.assertTrue((home / DR.CODEX_HOOKS_RELPATH).is_file())
        self.assertTrue((home / DR.GROK_HOOK_RELPATH).is_file())

    def test_rollback_without_a_backup_fails_loudly(self) -> None:
        home, binary = self.materialize("host_unrelated")
        with self.assertRaises(SkillboxError) as caught:
            DR.rollback(home, binary=binary)
        self.assertEqual(caught.exception.code, DR.DCG_RECONCILE_NO_BACKUP)

    def test_corrupt_backup_blob_refuses_to_restore(self) -> None:
        home, binary = self.materialize("host_unrelated")
        DR.apply(home, binary=binary)
        blob = next((home / ".config/dcg/backups/0001/files").iterdir())
        blob.write_bytes(b"tampered")
        with self.assertRaises(SkillboxError) as caught:
            DR.rollback(home, binary=binary)
        self.assertEqual(caught.exception.code, DR.DCG_RECONCILE_WRITE_FAILED)


# ---------------------------------------------------------------------------
# Container home: persistence across a replaced container
# ---------------------------------------------------------------------------


class ContainerHomeTests(_HomeCase):
    def test_persisted_subtrees_survive_a_container_replacement(self) -> None:
        home, binary = self.materialize("container_home")
        first = DR.apply(home, binary=binary)

        # A replaced container: every ephemeral layer is thrown away and the
        # home is rebuilt from ONLY the compose-mounted subtrees, at the same
        # in-box path (/home/sandbox) the previous container used.
        stash = home.parent / "mounts"
        for subtree in PERSISTED_SUBTREES:
            source = home / subtree
            if source.exists():
                target = stash / subtree
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, symlinks=True)
        shutil.rmtree(home)
        home.mkdir()
        for subtree in PERSISTED_SUBTREES:
            source = stash / subtree
            if source.exists():
                target = home / subtree
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, symlinks=True)

        after = DR.verify(home, binary=binary)
        self.assertEqual(after["pending_changes"], [], "a replaced container must need no reconvergence")
        self.assertEqual(after["state_digest"], first["state_digest"])
        self.assertEqual(after["binary_state"]["state"], DR.STATE_HEALTHY)

    def test_compose_mounts_every_subtree_the_reconciler_persists(self) -> None:
        compose = COMPOSE_FILE.read_text(encoding="utf-8")
        for subtree in PERSISTED_SUBTREES:
            with self.subTest(subtree=subtree):
                self.assertIn(f"/home/sandbox/{subtree}", compose)
        self.assertEqual(compose.count("/home/sandbox/.config/dcg"), 3)


# ---------------------------------------------------------------------------
# CLI: the bead's own validation contract
# ---------------------------------------------------------------------------


class CliContractTests(_HomeCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "runtime_manager.dcg_reconcile", *args],
            cwd=str(ROOT_DIR),
            env={"PYTHONPATH": str(ENV_MANAGER_DIR), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validation_contract_apply_apply_relinquish(self) -> None:
        home, binary = self.materialize("empty")
        first = self._run("apply", "--home", str(home), "--binary", str(binary), "--format", "json")
        second = self._run("apply", "--home", str(home), "--binary", str(binary), "--format", "json")
        removal = self._run("relinquish", "--home", str(home), "--format", "json")

        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        removal_payload = json.loads(removal.stdout)

        self.assertEqual(first_payload["result"], DR.RESULT_CHANGED)
        self.assertEqual(second_payload["result"], DR.RESULT_UNCHANGED)
        self.assertEqual(removal_payload["result"], DR.RESULT_REMOVED)
        self.assertIs(removal_payload["unrelated_preserved"], True)
        self.assertEqual(removal.returncode, DR.EXIT_OK)

    def test_exit_codes_separate_health_from_failure(self) -> None:
        home, binary = self.materialize("empty")
        needs_operator = self._run("apply", "--home", str(home), "--binary", str(binary), "--format", "json")
        self.assertEqual(needs_operator.returncode, DR.EXIT_NEEDS_OPERATOR)

        malformed_home, malformed_binary = self.materialize("host_malformed_json")
        failed = self._run(
            "apply", "--home", str(malformed_home), "--binary", str(malformed_binary), "--format", "json"
        )
        self.assertEqual(failed.returncode, DR.EXIT_FAILED)
        payload = json.loads(failed.stdout)
        self.assertEqual(payload["error"]["code"], DR.DCG_RECONCILE_MALFORMED_CONFIG)
        self.assertEqual(payload["status"], DR.STATE_FAILED)

        unsupported = self._run(
            "apply", "--home", str(home), "--platform", "Windows/AMD64", "--format", "json"
        )
        self.assertEqual(unsupported.returncode, DR.EXIT_UNSUPPORTED)

    def test_home_is_never_inferred_from_the_environment(self) -> None:
        result = self._run("apply", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--home", result.stderr)

    def test_healthy_home_exits_zero(self) -> None:
        home, binary = self.materialize("container_home")
        DR.apply(home, binary=binary)
        config = home / DR.CODEX_CONFIG_RELPATH
        config.write_text(
            (FIXTURES / "container_trusted" / "codex-config.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = self._run("verify", "--home", str(home), "--binary", str(binary), "--format", "json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], DR.STATE_HEALTHY)
        self.assertEqual(result.returncode, DR.EXIT_OK)

    def test_text_output_names_every_surface(self) -> None:
        home, binary = self.materialize("container_home")
        result = self._run("apply", "--home", str(home), "--binary", str(binary))
        for token in ("claude", "codex", "grok", "policy", "codex trust"):
            with self.subTest(token=token):
                self.assertIn(token, result.stdout)


# ---------------------------------------------------------------------------
# Cross-module contracts
# ---------------------------------------------------------------------------


class SubstrateContractTests(_HomeCase):
    def test_policy_render_is_the_only_source_of_user_config(self) -> None:
        home, binary = self.materialize("empty")
        DR.apply(home, binary=binary)
        text = (home / DR.POLICY_RELPATH).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(DP.GENERATED_MARKER))
        document = tomllib.loads(text)
        self.assertIs(document["general"]["fail_closed"], True)

    def test_version_pin_is_consumed_not_redeclared(self) -> None:
        source = (ENV_MANAGER_DIR / "runtime_manager" / "dcg_reconcile.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn(DD.DCG_VERSION, code, "the pin lives in dcg_distribution, never here")
        self.assertIn("_dist.DCG_VERSION", code)
        self.assertIn("_dist.asset_pin", code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
