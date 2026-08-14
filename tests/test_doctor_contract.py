"""The doctor family's ONE envelope, ONE vocabulary, and ONE --fix contract.

Beads skillbox-hws5 (envelope + vocabulary) and skillbox-bylk (--fix / backup /
undo / run artifact). These tests pin the contract itself, not any single
doctor's findings: a doctor that stops speaking the envelope, an exit-code
mirror that drifts from ``_shared/errors.py``, or a ``--fix`` that mutates
without a lease all fail here rather than in production.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(ROOT_DIR / ".env-manager"))

from lib import doctor_contract as DC  # noqa: E402
from lib import doctor_fix as DF  # noqa: E402
from runtime_manager import state_mutation as SM  # noqa: E402


class ExitLadderMirrorTests(unittest.TestCase):
    """``scripts/lib`` cannot import ``runtime_manager``, so the ladder is
    mirrored. A mirror that drifts is worse than no mirror."""

    def test_mirror_matches_the_shared_errors_module(self) -> None:
        from runtime_manager._shared import errors as ERRORS

        for name in ("EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "EXIT_NEEDS_INPUT", "EXIT_DRIFT"):
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(DC, name),
                    getattr(ERRORS, name),
                    f"{name} drifted from runtime_manager._shared.errors",
                )

    def test_ladder_values_are_the_documented_ones(self) -> None:
        self.assertEqual(
            (DC.EXIT_OK, DC.EXIT_ERROR, DC.EXIT_USAGE, DC.EXIT_NEEDS_INPUT, DC.EXIT_DRIFT),
            (0, 1, 2, 3, 4),
        )

    def test_usage_slot_is_never_a_doctor_verdict(self) -> None:
        """2 belongs to argparse. A doctor that RAN and found problems is 4."""
        findings = [DC.Finding(code="x", status=DC.STATUS_FAIL, message="m")]
        self.assertEqual(DC.exit_code_for(findings), DC.EXIT_DRIFT)
        self.assertNotEqual(DC.exit_code_for(findings), DC.EXIT_USAGE)

    def test_inconclusive_is_not_a_failure(self) -> None:
        findings = [DC.Finding(code="x", status=DC.STATUS_INCO, message="m")]
        self.assertEqual(DC.exit_code_for(findings), DC.EXIT_OK)

    def test_warn_is_not_a_failure(self) -> None:
        findings = [DC.Finding(code="x", status=DC.STATUS_WARN, message="m")]
        self.assertEqual(DC.exit_code_for(findings), DC.EXIT_OK)


class VocabularyTests(unittest.TestCase):
    def test_json_vocabulary_is_lowercase_and_closed(self) -> None:
        self.assertEqual(DC.STATUSES, ("pass", "warn", "inco", "fail"))

    def test_legacy_spellings_normalize(self) -> None:
        for raw, expected in (
            ("PASS", "pass"), ("ok", "pass"), (True, "pass"),
            ("FAIL", "fail"), ("error", "fail"), (False, "fail"),
            ("INCO", "inco"), ("inconclusive", "inco"),
            ("WARN", "warn"), ("warning", "warn"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(DC.normalize_status(raw), expected)

    def test_unknown_status_is_inconclusive_not_a_crash(self) -> None:
        self.assertEqual(DC.normalize_status("banana"), DC.STATUS_INCO)

    def test_text_renders_uppercase(self) -> None:
        self.assertEqual(DC.display_status("fail"), "FAIL")


class EnvelopeShapeTests(unittest.TestCase):
    def _envelope(self, *findings: DC.Finding) -> dict:
        return DC.doctor_envelope(tool="sbp doctor", findings=list(findings))

    def test_required_top_level_keys(self) -> None:
        payload = self._envelope(DC.Finding(code="a", status="pass", message="m"))
        for key in ("ok", "exit_code", "schema_version", "tool", "checks", "summary", "next_actions"):
            self.assertIn(key, payload)

    def test_ok_tracks_exit_code(self) -> None:
        good = self._envelope(DC.Finding(code="a", status="pass", message="m"))
        bad = self._envelope(DC.Finding(code="a", status="fail", message="m"))
        self.assertTrue(good["ok"])
        self.assertEqual(good["exit_code"], DC.EXIT_OK)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["exit_code"], DC.EXIT_DRIFT)

    def test_every_finding_carries_the_uniform_field_set(self) -> None:
        payload = self._envelope(DC.Finding(code="a", status="fail", message="m"))
        finding = payload["checks"][0]
        for key in ("code", "status", "message", "details", "fix_command"):
            self.assertIn(key, finding)

    def test_summary_counts_every_status(self) -> None:
        payload = self._envelope(
            DC.Finding(code="a", status="pass", message=""),
            DC.Finding(code="b", status="warn", message=""),
            DC.Finding(code="c", status="inco", message=""),
            DC.Finding(code="d", status="fail", message=""),
        )
        summary = payload["summary"]
        for status in DC.STATUSES:
            with self.subTest(status=status):
                self.assertEqual(summary[status], 1)
        self.assertEqual(summary["total"], 4)

    def test_family_routing_names_one_front_door(self) -> None:
        self.assertEqual(DC.FRONT_DOOR, "sbp doctor")
        self.assertIn(DC.FRONT_DOOR, {entry["doctor"] for entry in DC.FAMILY})

    def test_every_family_entry_names_a_symptom(self) -> None:
        for entry in DC.FAMILY:
            with self.subTest(doctor=entry["doctor"]):
                self.assertTrue(entry["symptom"], "routing without a symptom routes nobody")


class FixContractTests(unittest.TestCase):
    def test_unsupported_fix_must_say_why(self) -> None:
        block = DC.fix_contract(supported=False, reason="nothing here is mechanically fixable")
        self.assertFalse(block["supported"])
        self.assertTrue(block["reason"], "an unsupported --fix must record a reason")

    def test_supported_fix_publishes_preview_apply_undo_and_artifact_dir(self) -> None:
        block = DC.fix_contract(supported=True, artifact_dir="/tmp/x", fixable_codes=("a",))
        self.assertTrue(block["supported"])
        self.assertTrue(block["dry_run_by_default"])
        self.assertTrue(block["confirmation_required"])
        self.assertIn("--fix", block["preview"])
        self.assertIn(str(DC.EXIT_NEEDS_INPUT), block["preview"])
        self.assertIn("--yes", block["apply"])
        self.assertIn("--undo", block["undo"])
        self.assertEqual(block["run_artifact_dir"], "/tmp/x")
        self.assertEqual(tuple(block["fixable_codes"]), ("a",))

    def test_warn_findings_are_never_auto_fixed(self) -> None:
        spec = DF.FixSpec(code="w", command=("true",), description="d")
        [finding] = DF.annotate_fixable(
            [DC.Finding(code="w", status="warn", message="")], DF.build_registry([spec])
        )
        self.assertFalse(finding.fixable)
        self.assertEqual(finding.fix_reason, DF.REASON_ADVISORY)

    def test_fail_without_a_spec_is_not_fixable_and_says_so(self) -> None:
        [finding] = DF.annotate_fixable([DC.Finding(code="f", status="fail", message="")], {})
        self.assertFalse(finding.fixable)
        self.assertEqual(finding.fix_reason, DF.REASON_NO_SPEC)

    def test_passing_findings_carry_no_fix_noise(self) -> None:
        [finding] = DF.annotate_fixable([DC.Finding(code="p", status="pass", message="")], {})
        self.assertFalse(finding.fixable)
        self.assertEqual(finding.fix_reason, "", "a passing check must not carry fix chatter")


class MutationGateTests(unittest.TestCase):
    """--fix cannot ship without a state-mutation inventory row."""

    def test_every_doctor_fix_boundary_is_classified_as_a_mutation(self) -> None:
        for boundary_id in ("manage.doctor", "manage.structure-doctor", "reconcile.doctor"):
            with self.subTest(boundary_id=boundary_id):
                self.assertTrue(SM.boundary(boundary_id).is_mutation)

    def test_an_unclassified_boundary_id_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                with DF.mutation_gate(ROOT_DIR, "manage.status"):  # a READ boundary
                    self.fail("the lease must refuse a non-mutation boundary")
            del tmp

    def test_make_doctor_stays_read_because_the_recipe_passes_no_fix(self) -> None:
        entry = SM.boundary("make.doctor")
        self.assertEqual(entry.classification, SM.READ)
        self.assertEqual(entry.delegates_to, ("reconcile.doctor",))


class RunArtifactTests(unittest.TestCase):
    """Preview writes an artifact and refuses to mutate; apply backs up first."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.state_root = Path(self._tmp.name) / "state"
        self.addCleanup(self._tmp.cleanup)
        self._prev = os.environ.get("SKILLBOX_STATE_ROOT")
        os.environ["SKILLBOX_STATE_ROOT"] = str(self.state_root)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("SKILLBOX_STATE_ROOT", None)
        else:
            os.environ["SKILLBOX_STATE_ROOT"] = self._prev

    def _run(self, *, confirmed: bool, root: Path, spec: DF.FixSpec) -> DF.FixRun:
        return DF.run_fix(
            tool="sbp doctor",
            root_dir=root,
            findings=[DC.Finding(code=spec.code, status="fail", message="broken")],
            registry=DF.build_registry([spec]),
            confirmed=confirmed,
            boundary_id="manage.structure-doctor",
            undo_command_template="undo {artifact}",
            argv=["structure-doctor", "--fix"],
        )

    def test_preview_writes_an_artifact_changes_nothing_and_asks_for_input(self) -> None:
        root = Path(self._tmp.name) / "repo"
        (root).mkdir()
        target = root / "victim.txt"
        target.write_text("original", encoding="utf-8")
        spec = DF.FixSpec(
            code="c", command=(sys.executable, "-c", "open('victim.txt','w').write('CHANGED')"),
            description="d", backup_paths=("victim.txt",),
        )
        run = self._run(confirmed=False, root=root, spec=spec)
        self.assertEqual(run.exit_code, DC.EXIT_NEEDS_INPUT)
        self.assertEqual(run.artifact["mode"], "preview")
        self.assertEqual(run.artifact["outcome"], "confirmation-required")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertTrue(run.artifact_path.exists())
        self.assertEqual(
            json.loads(run.artifact_path.read_text(encoding="utf-8"))["schema_version"],
            DF.RUN_ARTIFACT_SCHEMA_VERSION,
        )

    def test_apply_backs_up_mutates_and_undo_restores(self) -> None:
        root = Path(self._tmp.name) / "repo2"
        root.mkdir()
        target = root / "victim.txt"
        target.write_text("original", encoding="utf-8")
        spec = DF.FixSpec(
            code="c", command=(sys.executable, "-c", "open('victim.txt','w').write('CHANGED')"),
            description="d", backup_paths=("victim.txt",),
        )
        run = self._run(confirmed=True, root=root, spec=spec)
        self.assertEqual(run.exit_code, DC.EXIT_OK)
        self.assertEqual(run.artifact["outcome"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "CHANGED")
        self.assertTrue(run.artifact["undo"]["supported"])
        self.assertEqual(run.artifact["backups"][0]["path"], "victim.txt")

        preview = DF.undo_run(run.artifact_path, root_dir=root)
        self.assertEqual(preview.exit_code, DC.EXIT_NEEDS_INPUT)
        self.assertEqual(
            target.read_text(encoding="utf-8"), "CHANGED", "an undo PREVIEW must change nothing"
        )

        undo = DF.undo_run(run.artifact_path, root_dir=root, confirmed=True)
        self.assertEqual(undo.exit_code, DC.EXIT_OK)
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_undo_deletes_what_the_fix_created_from_nothing(self) -> None:
        root = Path(self._tmp.name) / "repo3"
        root.mkdir()
        created = root / "made-up.txt"
        spec = DF.FixSpec(
            code="c", command=(sys.executable, "-c", "open('made-up.txt','w').write('new')"),
            description="d", backup_paths=("made-up.txt",),
        )
        run = self._run(confirmed=True, root=root, spec=spec)
        self.assertTrue(created.exists())
        self.assertFalse(run.artifact["backups"][0]["existed"])
        DF.undo_run(run.artifact_path, root_dir=root)
        self.assertTrue(created.exists(), "a preview must not delete anything")
        DF.undo_run(run.artifact_path, root_dir=root, confirmed=True)
        self.assertFalse(created.exists(), "undo must remove a path the fix created")

    def test_a_failing_fixer_is_recorded_not_swallowed(self) -> None:
        root = Path(self._tmp.name) / "repo4"
        root.mkdir()
        spec = DF.FixSpec(code="c", command=(sys.executable, "-c", "raise SystemExit(7)"), description="d")
        run = self._run(confirmed=True, root=root, spec=spec)
        self.assertEqual(run.artifact["outcome"], "partially-applied")
        self.assertEqual(run.artifact["applied"][0]["exit_code"], 7)
        self.assertNotEqual(run.exit_code, DC.EXIT_OK)

    def test_artifact_is_owner_only(self) -> None:
        root = Path(self._tmp.name) / "repo5"
        root.mkdir()
        spec = DF.FixSpec(code="c", command=(sys.executable, "-c", "pass"), description="d")
        run = self._run(confirmed=False, root=root, spec=spec)
        self.assertEqual(run.artifact_path.stat().st_mode & 0o777, 0o600)

    def test_run_directories_are_short_stable_slugs(self) -> None:
        for tool, slug in DF.RUN_SLUGS.items():
            with self.subTest(tool=tool):
                self.assertEqual(DF.runs_dir(ROOT_DIR, tool).name, slug)
                self.assertIn(tool, {entry["doctor"] for entry in DC.FAMILY})


class UndoContainmentTests(unittest.TestCase):
    """`--undo` treats its own artifact as hostile input.

    The artifact is JSON on a container-writable bind mount, so the reviewer's
    P0 was exactly right: an undo that believes what it reads is an arbitrary
    `rmtree` with a friendly name. Each test below plants the artifact an
    attacker (or a stale honest run) would produce and asserts the bytes on
    disk survive.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.state_root = base / "state"
        self.root = base / "repo"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self._prev = os.environ.get("SKILLBOX_STATE_ROOT")
        os.environ["SKILLBOX_STATE_ROOT"] = str(self.state_root)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("SKILLBOX_STATE_ROOT", None)
        else:
            os.environ["SKILLBOX_STATE_ROOT"] = self._prev

    def _apply(self, spec: DF.FixSpec) -> DF.FixRun:
        return DF.run_fix(
            tool="sbp doctor",
            root_dir=self.root,
            findings=[DC.Finding(code=spec.code, status="fail", message="broken")],
            registry=DF.build_registry([spec]),
            confirmed=True,
            boundary_id="manage.structure-doctor",
            undo_command_template="undo {artifact}",
        )

    def _seed_run(self) -> DF.FixRun:
        """One honest applied run, so a real signed artifact exists to tamper with."""
        return self._apply(
            DF.FixSpec(
                code="c",
                command=(sys.executable, "-c", "open('made.txt','w').write('new')"),
                description="d",
                backup_paths=("made.txt",),
            )
        )

    def _resign(self, path: Path, mutate) -> None:
        """Rewrite an artifact AND re-sign it with the real key.

        Re-signing is the point: it models the strongest attacker the file-based
        key can be assumed to stop (none — they can read it), which forces the
        containment checks to carry the security on their own.
        """
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        key = DF.integrity_key(self.state_root, create=False)
        path.write_text(
            json.dumps(DF.sign_artifact(payload, key), indent=2, sort_keys=True), encoding="utf-8"
        )

    def test_artifact_outside_the_run_directory_is_refused(self) -> None:
        run = self._seed_run()
        planted = Path(self._tmp.name) / "planted.json"
        planted.write_text(run.artifact_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(DF.DoctorFixError) as caught:
            DF.undo_run(planted, root_dir=self.root, confirmed=True)
        self.assertIn("run directory", str(caught.exception))

    def test_unsigned_or_hand_edited_artifact_is_refused(self) -> None:
        run = self._seed_run()
        payload = json.loads(run.artifact_path.read_text(encoding="utf-8"))
        payload["backups"][0]["resolved"] = str(self.outside / "victim.txt")
        run.artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(DF.DoctorFixError) as caught:
            DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertIn("signature", str(caught.exception))

    def test_absolute_path_outside_the_write_scope_is_refused(self) -> None:
        run = self._seed_run()
        victim = self.outside / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        self._resign(
            run.artifact_path,
            lambda payload: payload["backups"].append(
                {
                    "path": "victim.txt",
                    "backup": None,
                    "existed": False,
                    "kind": "absent",
                    "resolved": str(victim),
                    "scope": str(self.outside),
                    "created": {"kind": "file", "sha256": "0" * 64},
                }
            ),
        )
        undo = DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertTrue(victim.exists(), "undo deleted a path outside every recorded write scope")
        refusals = [row for row in undo.artifact["restored"] if row["action"] == "refused"]
        self.assertTrue(any("write scope" in row["reason"] for row in refusals))
        self.assertNotEqual(undo.exit_code, DC.EXIT_OK)

    def test_dotdot_escape_is_refused(self) -> None:
        run = self._seed_run()
        victim = self.outside / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        escaped = str(self.root / ".." / "outside" / "victim.txt")
        self._resign(
            run.artifact_path,
            lambda payload: payload["backups"].append(
                {
                    "path": "../outside/victim.txt",
                    "backup": None,
                    "existed": False,
                    "kind": "absent",
                    "resolved": escaped,
                    "scope": str(self.root),
                    "created": {"kind": "file", "sha256": "0" * 64},
                }
            ),
        )
        undo = DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertTrue(victim.exists(), "a `..` component walked out of the repo")
        self.assertNotEqual(undo.exit_code, DC.EXIT_OK)

    def test_symlinked_ancestor_swapped_after_the_fix_is_refused(self) -> None:
        run = self._seed_run()
        victim_dir = self.outside / "real"
        victim_dir.mkdir()
        (victim_dir / "victim.txt").write_text("precious", encoding="utf-8")
        # The fix recorded <repo>/sub/victim.txt; `sub` is a symlink to the
        # attacker's directory by the time undo runs.
        (self.root / "sub").symlink_to(victim_dir, target_is_directory=True)
        self._resign(
            run.artifact_path,
            lambda payload: payload["backups"].append(
                {
                    "path": "sub/victim.txt",
                    "backup": None,
                    "existed": False,
                    "kind": "absent",
                    "resolved": str(self.root / "sub" / "victim.txt"),
                    "scope": str(self.root),
                    "created": {"kind": "file", "sha256": "0" * 64},
                }
            ),
        )
        undo = DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertTrue(
            (victim_dir / "victim.txt").exists(), "undo followed a symlink swapped in after the fix"
        )
        self.assertNotEqual(undo.exit_code, DC.EXIT_OK)

    def test_created_directory_with_foreign_content_is_refused_not_deleted(self) -> None:
        """The honest-artifact half of the P0: `sync` populated what `mkdir` made."""
        run = self._apply(
            DF.FixSpec(
                code="expected-directories",
                command=("mkdir", "-p", "repos"),
                description="create the workspace directories",
                backup_paths=("repos",),
            )
        )
        self.assertEqual(run.artifact["outcome"], "applied")
        populated = self.root / "repos" / "someones-clone"
        populated.mkdir(parents=True)
        (populated / "work.txt").write_text("hours of it", encoding="utf-8")

        undo = DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertTrue(
            (populated / "work.txt").exists(),
            "undo recursively deleted a directory populated after the fix",
        )
        refusals = [row for row in undo.artifact["restored"] if row["action"] == "refused"]
        self.assertTrue(refusals, "a refusal must be recorded, loudly")
        self.assertIn("did not create", refusals[0]["reason"])
        self.assertTrue(any("REFUSED" in line for line in undo.lines))
        self.assertNotEqual(undo.exit_code, DC.EXIT_OK)

    def test_untouched_created_directory_still_undoes_cleanly(self) -> None:
        """Containment must not cost the honest round trip."""
        run = self._apply(
            DF.FixSpec(
                code="expected-directories",
                command=("mkdir", "-p", "repos"),
                description="create the workspace directories",
                backup_paths=("repos",),
            )
        )
        self.assertTrue((self.root / "repos").is_dir())
        preview = DF.undo_run(run.artifact_path, root_dir=self.root)
        self.assertEqual(preview.exit_code, DC.EXIT_NEEDS_INPUT)
        self.assertTrue((self.root / "repos").is_dir(), "a preview must delete nothing")
        undo = DF.undo_run(run.artifact_path, root_dir=self.root, confirmed=True)
        self.assertEqual(undo.exit_code, DC.EXIT_OK)
        self.assertFalse((self.root / "repos").exists())

    def test_artifact_from_another_checkout_is_refused(self) -> None:
        run = self._seed_run()
        other = Path(self._tmp.name) / "other-repo"
        other.mkdir()
        with self.assertRaises(DF.DoctorFixError) as caught:
            DF.undo_run(run.artifact_path, root_dir=other, confirmed=True)
        self.assertIn("repo root", str(caught.exception))

    def test_module_never_re_expands_paths_from_the_artifact(self) -> None:
        source = (ROOT_DIR / "scripts" / "lib" / "doctor_fix.py").read_text(encoding="utf-8")
        # Call sites only — the prose explaining WHY it is banned is welcome.
        self.assertNotIn(
            "expandvars(", source,
            "expandvars makes a recorded path environment-dependent; it must not be called",
        )
        # `expanduser` is legal only where the input is code we wrote (a FixSpec
        # path) or operator env ($SKILLBOX_STATE_ROOT) — never artifact content.
        allowed = {"_declared_abs", "_backup_pair", "resolve_state_root"}
        current = ""
        for line_no, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                current = stripped.split("def ", 1)[1].split("(", 1)[0]
            if "expanduser(" in line and not stripped.startswith("#"):
                self.assertIn(
                    current, allowed,
                    f"line {line_no} ({current}) expands a path; only {sorted(allowed)} may, "
                    "because only they receive repo-authored or operator input",
                )


class LiveDoctorEnvelopeTests(unittest.TestCase):
    """The three core doctors really do emit the envelope, end to end."""

    maxDiff = None

    def _json(self, argv: list[str]) -> dict:
        env = dict(os.environ)
        env["SKILLBOX_STATE_ROOT"] = env.get("SKILLBOX_STATE_ROOT") or str(ROOT_DIR / ".skillbox-state")
        proc = subprocess.run(
            argv, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=600, env=env
        )
        self.assertIn(
            proc.returncode,
            (DC.EXIT_OK, DC.EXIT_DRIFT),
            f"{argv} exited {proc.returncode}; a doctor that RAN returns 0 or 4\n{proc.stderr[-2000:]}",
        )
        return json.loads(proc.stdout)

    def _assert_envelope(self, payload: dict, tool: str) -> None:
        self.assertEqual(payload["schema_version"], DC.DOCTOR_SCHEMA_VERSION)
        self.assertEqual(payload["tool"], tool)
        self.assertIn(payload["exit_code"], (DC.EXIT_OK, DC.EXIT_DRIFT))
        self.assertEqual(payload["ok"], payload["exit_code"] == DC.EXIT_OK)
        self.assertTrue(payload["checks"], "a doctor with no checks is a broken doctor")
        for check in payload["checks"]:
            self.assertIn(check["status"], DC.STATUSES, f"{check['code']} speaks a foreign status")
            for key in ("code", "message", "details", "fix_command"):
                self.assertIn(key, check)
        self.assertIn("coverage", payload)
        self.assertIn("fix", payload)

    def test_reconcile_doctor_speaks_the_envelope(self) -> None:
        payload = self._json([sys.executable, "scripts/04-reconcile.py", "doctor", "--format", "json"])
        self._assert_envelope(payload, "make doctor")

    def test_runtime_doctor_speaks_the_envelope(self) -> None:
        payload = self._json([sys.executable, ".env-manager/manage.py", "doctor", "--format", "json"])
        self._assert_envelope(payload, "python3 .env-manager/manage.py doctor")

    def test_fix_without_yes_never_mutates_and_exits_needs_input_or_drift(self) -> None:
        env = dict(os.environ)
        env["SKILLBOX_STATE_ROOT"] = str(ROOT_DIR / ".skillbox-state")
        proc = subprocess.run(
            [sys.executable, "scripts/04-reconcile.py", "doctor", "--fix", "--format", "json"],
            cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=600, env=env,
        )
        self.assertIn(proc.returncode, (DC.EXIT_OK, DC.EXIT_NEEDS_INPUT, DC.EXIT_DRIFT))
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "preview")
        self.assertFalse(payload["confirmed"])
        self.assertEqual(payload["applied"], [])
        self.assertEqual(payload["backups"], [])
        self.assertEqual(payload["boundary_id"], "reconcile.doctor")


if __name__ == "__main__":
    unittest.main()
