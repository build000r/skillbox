"""source-capsule/v1 adversarial tests (skillbox-sbp-test-source-capsule-e1jj).

A capsule exists so a receipt can say *which bytes ran*. Every test here is
about a way that claim could be a lie: a secret smuggled along, a symlink
pointing out of the tree, an archive that arrived corrupt, two admissions
racing, a filename chosen to break the parser.

The three identifiers are checked for being genuinely *different* answers, not
aliases: `source_tree_oid` (Git identity), `capsule_manifest_sha256`
(materialized bytes/modes/links, recomputable after extraction) and
`archive_sha256` (transport).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import sbp_test_capsule as C  # noqa: E402

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


class CapsuleRepoMixin(unittest.TestCase):
    """A throwaway git repo with a committed baseline."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.store = self.root / "store"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=GIT_ENV)
        for args in (
            ["config", "user.email", "capsule@example.invalid"],
            ["config", "user.name", "Capsule Test"],
            ["config", "commit.gpgsign", "false"],
        ):
            self._git(*args)
        (self.repo / "a.txt").write_text("hello\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "baseline")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True, env=GIT_ENV,
        )
        return result.stdout

    def _build(self, **kwargs):
        kwargs.setdefault("store", self.store)
        return C.build_capsule(self.repo, **kwargs)


class IdentifierTests(CapsuleRepoMixin):
    def test_all_three_identifiers_are_present_and_distinct(self) -> None:
        capsule = self._build()
        ids = capsule.identifiers()
        self.assertEqual(
            {"source_tree_oid", "capsule_manifest_sha256", "archive_sha256"}, set(ids)
        )
        for value in ids.values():
            self.assertTrue(value)
        self.assertEqual(
            3, len(set(ids.values())), "identifiers must not be interchangeable aliases"
        )

    def test_receipts_stamp_all_three_from_day_one(self) -> None:
        payload = self._build().to_payload()
        for key in ("source_tree_oid", "capsule_manifest_sha256", "archive_sha256"):
            self.assertIn(key, payload)
        self.assertEqual(C.CAPSULE_SCHEMA, payload["schema"])

    def test_source_tree_oid_is_a_real_git_tree(self) -> None:
        capsule = self._build()
        kind = self._git("cat-file", "-t", capsule.source_tree_oid).strip()
        self.assertEqual("tree", kind)

    def test_building_does_not_touch_the_real_index(self) -> None:
        """A read-only question must not stage the operator's working tree."""
        (self.repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        before = self._git("status", "--porcelain=v1")
        self._build()
        self.assertEqual(before, self._git("status", "--porcelain=v1"))

    def test_manifest_digest_is_recomputable_after_extraction(self) -> None:
        """This is what the manifest digest is FOR."""
        capsule = self._build()
        recomputed = C.compute_manifest_sha256(
            C.manifest_from_archive(capsule.archive_path)
        )
        self.assertEqual(capsule.capsule_manifest_sha256, recomputed)

    def test_identical_content_produces_an_identical_archive_digest(self) -> None:
        first = self._build()
        second = C.build_capsule(self.repo, store=self.root / "store2")
        self.assertEqual(first.archive_sha256, second.archive_sha256)
        self.assertEqual(first.capsule_manifest_sha256, second.capsule_manifest_sha256)

    def test_a_content_change_moves_manifest_and_archive_digests(self) -> None:
        first = self._build()
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        second = C.build_capsule(self.repo, store=self.root / "store3")
        self.assertNotEqual(first.capsule_manifest_sha256, second.capsule_manifest_sha256)
        self.assertNotEqual(first.archive_sha256, second.archive_sha256)


class InventoryTests(CapsuleRepoMixin):
    def test_modified_deleted_and_untracked_are_counted_separately(self) -> None:
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "src" / "main.py").unlink()
        (self.repo / "new.txt").write_text("untracked\n", encoding="utf-8")

        inventory = C.collect_inventory(self.repo).to_payload()
        self.assertEqual(1, inventory["modified"])
        self.assertEqual(1, inventory["deleted"])
        self.assertEqual(1, inventory["untracked"])

    def test_uncommitted_work_is_actually_captured(self) -> None:
        """The whole point: the capsule is the working tree, not HEAD."""
        (self.repo / "new.txt").write_text("uncommitted\n", encoding="utf-8")
        capsule = self._build()
        names = {entry.path for entry in capsule.entries}
        self.assertIn("new.txt", names)

    def test_a_deleted_file_is_absent_from_the_capsule(self) -> None:
        (self.repo / "src" / "main.py").unlink()
        capsule = self._build()
        names = {entry.path for entry in capsule.entries}
        self.assertNotIn("src/main.py", names)

    def test_ignored_paths_are_not_captured(self) -> None:
        (self.repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "artifact.bin").write_text("junk\n", encoding="utf-8")
        capsule = self._build()
        names = {entry.path for entry in capsule.entries}
        self.assertNotIn("build/artifact.bin", names)

    def test_inventory_is_plan_visible_in_the_payload(self) -> None:
        payload = self._build().to_payload()
        self.assertEqual(
            {"modified", "deleted", "untracked", "exclusions"}, set(payload["inventory"])
        )


class SecretRefusalTests(CapsuleRepoMixin):
    """`.gitignore` is not a firewall: secret-shaped paths REFUSE, never warn."""

    def test_tracked_secret_refuses(self) -> None:
        (self.repo / "prod.token").write_text("shhh\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "add secret")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertEqual("secret_shaped_path", ctx.exception.code)
        self.assertIn("prod.token", ctx.exception.paths)

    def test_untracked_non_ignored_secret_refuses(self) -> None:
        """The file you least want shipped is the one nobody added a rule for."""
        (self.repo / "api_key.pem").write_text("x\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertEqual("secret_shaped_path", ctx.exception.code)

    def test_secret_in_a_parent_directory_name_refuses(self) -> None:
        (self.repo / "secrets").mkdir()
        (self.repo / "secrets" / "value.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertIn("secrets/value.txt", ctx.exception.paths)

    def test_refusal_is_fail_closed_no_archive_is_admitted(self) -> None:
        (self.repo / "prod.token").write_text("shhh\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal):
            self._build()
        admitted = list(self.store.glob("*.tar")) if self.store.is_dir() else []
        self.assertEqual([], admitted, "a refused capsule must leave nothing behind")

    def test_ordinary_paths_are_not_false_positives(self) -> None:
        (self.repo / "tokenizer.py").write_text("# not a secret\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal):
            # `tokenizer` contains TOKEN: v1 is deliberately fail-closed here,
            # and this test pins that as a KNOWN, accepted cost rather than a
            # surprise. An allowlist is future work, not a v1 silent pass.
            self._build()

    def test_refusal_payload_is_typed_and_names_the_paths(self) -> None:
        (self.repo / "prod.token").write_text("x\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        payload = ctx.exception.to_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual("secret_shaped_path", payload["error_code"])
        self.assertTrue(payload["paths"])


class PathHostilityTests(CapsuleRepoMixin):
    def test_unicode_filename_round_trips(self) -> None:
        name = "ünïcode-café-你好.txt"
        (self.repo / name).write_text("hi\n", encoding="utf-8")
        capsule = self._build()
        self.assertIn(name, {entry.path for entry in capsule.entries})
        recomputed = C.compute_manifest_sha256(C.manifest_from_archive(capsule.archive_path))
        self.assertEqual(capsule.capsule_manifest_sha256, recomputed)

    def test_newline_in_filename_does_not_break_the_inventory(self) -> None:
        """Naive porcelain parsing splits on newlines; -z is why this passes."""
        name = "weird\nname.txt"
        try:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        except OSError:
            self.skipTest("filesystem rejects newlines in filenames")
        inventory = C.collect_inventory(self.repo).to_payload()
        self.assertEqual(1, inventory["untracked"], "newline filename must count as ONE entry")

    def test_newline_filename_survives_capsule_and_manifest(self) -> None:
        name = "weird\nname.txt"
        try:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        except OSError:
            self.skipTest("filesystem rejects newlines in filenames")
        capsule = self._build()
        self.assertIn(name, {entry.path for entry in capsule.entries})
        # The manifest is line-oriented; a raw newline would corrupt it.
        recomputed = C.compute_manifest_sha256(C.manifest_from_archive(capsule.archive_path))
        self.assertEqual(capsule.capsule_manifest_sha256, recomputed)

    def test_quote_and_backslash_filenames_are_unambiguous(self) -> None:
        for name in ('quo"te.txt', "back\\slash.txt"):
            with self.subTest(name=name):
                path = self.repo / name
                try:
                    path.write_text("x\n", encoding="utf-8")
                except OSError:
                    self.skipTest("filesystem rejects this filename")
                capsule = C.build_capsule(self.repo, store=self.root / f"s{abs(hash(name))}")
                self.assertIn(name, {e.path for e in capsule.entries})
                path.unlink()

    def test_internal_symlink_is_captured_as_a_link(self) -> None:
        (self.repo / "link.txt").symlink_to("a.txt")
        capsule = self._build()
        entry = next(e for e in capsule.entries if e.path == "link.txt")
        self.assertEqual(C.KIND_SYMLINK, entry.kind)

    def test_symlink_escaping_the_repo_refuses(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret-ish\n", encoding="utf-8")
        (self.repo / "escape.txt").symlink_to(outside)
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertEqual("symlink_escape", ctx.exception.code)
        self.assertIn("escape.txt", ctx.exception.paths)

    def test_relative_symlink_escape_refuses(self) -> None:
        (self.repo / "sneaky.txt").symlink_to("../outside-relative.txt")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertEqual("symlink_escape", ctx.exception.code)


class SubmoduleRefusalTests(CapsuleRepoMixin):
    def _add_submodule(self) -> Path:
        inner = self.root / "inner"
        inner.mkdir()
        subprocess.run(["git", "init", "-q", str(inner)], check=True, env=GIT_ENV)
        for args in (
            ["config", "user.email", "i@example.invalid"],
            ["config", "user.name", "Inner"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", "-C", str(inner), *args], check=True, env=GIT_ENV)
        (inner / "f.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(inner), "add", "-A"], check=True, env=GIT_ENV)
        subprocess.run(
            ["git", "-C", str(inner), "commit", "-q", "-m", "inner"], check=True, env=GIT_ENV
        )
        add = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
             "submodule", "add", "-q", str(inner), "sub"],
            capture_output=True, text=True, env=GIT_ENV,
        )
        if add.returncode != 0:
            self.skipTest(f"submodule add unavailable: {add.stderr.strip()}")
        self._git("commit", "-q", "-m", "add submodule")
        return self.repo / "sub"

    def test_dirty_submodule_refuses(self) -> None:
        sub = self._add_submodule()
        (sub / "f.txt").write_text("dirtied\n", encoding="utf-8")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build()
        self.assertEqual("dirty_submodule", ctx.exception.code)

    def test_clean_submodule_does_not_refuse(self) -> None:
        self._add_submodule()
        self.assertEqual([], C.dirty_submodules(self.repo))


class IgnoredAllowlistRefusalTests(CapsuleRepoMixin):
    def test_ignored_path_allowlist_is_refused_in_v1(self) -> None:
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build(allow_ignored=["build/artifact.bin"])
        self.assertEqual("ignored_path_allowlist_refused", ctx.exception.code)


class StoreAdmissionTests(CapsuleRepoMixin):
    def test_archive_is_admitted_and_verifiable(self) -> None:
        capsule = self._build()
        self.assertTrue(capsule.archive_path.is_file())
        self.assertTrue(C.verify_stored(self.store, capsule.archive_sha256))

    def test_store_is_mode_0700_and_files_0600(self) -> None:
        capsule = self._build()
        self.assertEqual(0o700, os.stat(self.store).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(capsule.archive_path).st_mode & 0o777)

    def test_store_is_content_addressed_by_archive_digest(self) -> None:
        capsule = self._build()
        self.assertEqual(f"{capsule.archive_sha256}.tar", capsule.archive_path.name)

    def test_corrupt_staged_archive_is_refused_not_admitted(self) -> None:
        root = C.ensure_store(self.store)
        staged = root / "tmp" / "corrupt.tar.part"
        staged.write_bytes(b"not a real archive")
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            C.admit(staged, root, "0" * 64)
        self.assertEqual("archive_digest_mismatch", ctx.exception.code)
        self.assertEqual([], list(root.glob("*.tar")))
        self.assertFalse(staged.exists(), "a rejected staging file must be cleaned up")

    def test_at_rest_corruption_is_detected_by_verify(self) -> None:
        capsule = self._build()
        capsule.archive_path.write_bytes(b"tampered")
        self.assertFalse(C.verify_stored(self.store, capsule.archive_sha256))

    def test_interrupted_upload_leaves_nothing_admitted(self) -> None:
        """A half-written staging file is never mistaken for a capsule."""
        root = C.ensure_store(self.store)
        partial = root / "tmp" / "interrupted.tar.part"
        partial.write_bytes(b"half a tar")
        self.assertEqual([], list(root.glob("*.tar")))
        removed = C.prune_store_temp(root)
        self.assertEqual(1, removed)
        self.assertFalse(partial.exists())

    def test_duplicate_admission_is_idempotent(self) -> None:
        first = self._build()
        second = C.build_capsule(self.repo, store=self.store)
        self.assertEqual(first.archive_path, second.archive_path)
        self.assertEqual(1, len(list(self.store.glob("*.tar"))))

    def test_concurrent_admission_of_identical_content_is_safe(self) -> None:
        results: list[object] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(C.build_capsule(self.repo, store=self.store))
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors, "concurrent admission must not raise")
        self.assertEqual(4, len(results))
        self.assertEqual(1, len(list(self.store.glob("*.tar"))), "one object, not four")
        digests = {r.archive_sha256 for r in results}
        self.assertEqual(1, len(digests))

    def test_quota_breach_refuses_and_admits_nothing(self) -> None:
        with self.assertRaises(C.CapsuleRefusal) as ctx:
            self._build(quota_bytes=1)
        self.assertEqual("capsule_store_quota_exceeded", ctx.exception.code)
        self.assertEqual([], list(self.store.glob("*.tar")))

    def test_quota_breach_leaves_no_staging_residue(self) -> None:
        with self.assertRaises(C.CapsuleRefusal):
            self._build(quota_bytes=1)
        self.assertEqual([], list((self.store / "tmp").iterdir()))

    def test_no_admit_mode_builds_identifiers_without_storing(self) -> None:
        capsule = self._build(admit_to_store=False)
        self.assertIsNone(capsule.archive_path)
        self.assertTrue(capsule.archive_sha256)
        self.assertEqual([], list(self.store.glob("*.tar")))

    def test_store_root_honours_the_env_override(self) -> None:
        override = self.root / "elsewhere"
        try:
            os.environ["SKILLBOX_TEST_CAPSULE_STORE"] = str(override)
            self.assertEqual(override, C.store_root(self.repo))
        finally:
            os.environ.pop("SKILLBOX_TEST_CAPSULE_STORE", None)

    def test_default_store_lives_under_skillbox_state(self) -> None:
        os.environ.pop("SKILLBOX_TEST_CAPSULE_STORE", None)
        self.assertEqual(
            self.repo / C.CAPSULE_STORE_RELPATH, C.store_root(self.repo)
        )


class ArchiveDeterminismTests(CapsuleRepoMixin):
    def test_archive_carries_no_host_identity(self) -> None:
        """uid/gid/uname/mtime would make the digest host-specific."""
        capsule = self._build()
        with tarfile.open(capsule.archive_path, "r") as tar:
            for info in tar.getmembers():
                self.assertEqual(0, info.mtime)
                self.assertEqual(0, info.uid)
                self.assertEqual(0, info.gid)
                self.assertEqual("", info.uname)
                self.assertEqual("", info.gname)

    def test_entries_are_sorted_for_stable_digests(self) -> None:
        (self.repo / "zzz.txt").write_text("z\n", encoding="utf-8")
        (self.repo / "aaa.txt").write_text("a\n", encoding="utf-8")
        capsule = self._build()
        names = [e.path for e in capsule.entries]
        self.assertEqual(sorted(names), names)


class FrontDoorIntegrationTests(CapsuleRepoMixin):
    """`sbp test capsule` stamps all three identifiers into its receipt."""

    def setUp(self) -> None:
        super().setUp()
        from runtime_manager import sbp_test as ST

        self.ST = ST
        os.environ["SKILLBOX_TEST_CAPSULE_STORE"] = str(self.store)
        self.addCleanup(os.environ.pop, "SKILLBOX_TEST_CAPSULE_STORE", None)

    def test_capsule_verb_receipt_carries_all_three_identifiers(self) -> None:
        payload = self.ST.capsule_payload(self.repo)
        self.assertTrue(payload["ok"], payload.get("error"))
        for key in ("source_tree_oid", "capsule_manifest_sha256", "archive_sha256"):
            self.assertIn(key, payload, "receipts stamp all three from day one")
            self.assertTrue(payload[key])
        self.assertEqual(C.CAPSULE_SCHEMA, payload["capsule"]["schema"])

    def test_capsule_verb_surfaces_a_refusal_as_a_typed_payload(self) -> None:
        (self.repo / "prod.token").write_text("x\n", encoding="utf-8")
        payload = self.ST.capsule_payload(self.repo)
        self.assertFalse(payload["ok"])
        self.assertEqual("secret_shaped_path", payload["error_code"])
        self.assertTrue(payload["next_actions"])

    def test_capsule_is_declared_a_write_verb_not_a_gated_one(self) -> None:
        self.assertIn("capsule", self.ST.WRITE_VERBS)
        self.assertIn("capsule", self.ST.VERBS)
        self.assertNotIn("capsule", self.ST.GATED_VERBS)
        self.assertNotIn("capsule", self.ST.READ_ONLY_VERBS)

    def test_plan_and_lint_remain_read_only_after_the_capsule_verb_landed(self) -> None:
        """Regression guard: adding a writing verb must not leak into plan/lint."""
        (self.repo / ".skillbox").mkdir()
        (self.repo / ".skillbox" / "test.yaml").write_text(
            "schema_version: 1\nunits:\n  a:\n    command: [python3, --version]\n"
            "groups:\n  default: [a]\n",
            encoding="utf-8",
        )
        before = {
            (str(p.relative_to(self.repo)), p.stat().st_size)
            for p in self.repo.rglob("*")
            if p.is_file()
        }
        self.ST.plan_payload(self.repo)
        self.ST.lint_payload(self.repo)
        after = {
            (str(p.relative_to(self.repo)), p.stat().st_size)
            for p in self.repo.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
