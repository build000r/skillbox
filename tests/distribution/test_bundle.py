"""Tests for bundle format primitives (WG-001)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from runtime_manager.distribution.bundle import (
    BUNDLE_SUFFIX,
    MANIFEST_FILENAME,
    SKILL_META_DIR,
    BundleContentMismatchError,
    BundleFileEntry,
    BundleManifest,
    BundleStructureError,
    compute_tree_sha256,
    pack_skill_bundle,
    unpack_skill_bundle,
    verify_bundle_contents,
)


def _make_skill_dir(root: Path, *, name: str = "deploy") -> Path:
    """Create a minimal skill directory with SKILL.md and a references/ file."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Deploy\n\nDeploy things.\n", encoding="utf-8")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("## Guide\n\nStep by step.\n", encoding="utf-8")
    return skill_dir


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class TestPackUnpackRoundTrip(unittest.TestCase):

    def test_round_trip_preserves_content_and_tree_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            original_tree = compute_tree_sha256(skill_dir)

            bundle_path = pack_skill_bundle(skill_dir, 3)

            self.assertTrue(bundle_path.name.endswith(BUNDLE_SUFFIX))
            self.assertIn("deploy-v3", bundle_path.name)

            extract_dir = tmp / "extracted"
            extract_dir.mkdir()
            manifest = unpack_skill_bundle(bundle_path, extract_dir)

            self.assertEqual(manifest.name, "deploy")
            self.assertEqual(manifest.version, 3)
            self.assertEqual(manifest.tree_sha256, original_tree)
            self.assertEqual(len(manifest.files), 2)

            file_paths = sorted(f.path for f in manifest.files)
            self.assertEqual(file_paths, ["SKILL.md", "references/guide.md"])

            verify_bundle_contents(manifest, extract_dir)

    def test_tree_sha_is_deterministic_across_repack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)

            bundle1 = pack_skill_bundle(skill_dir, 1, output_dir=tmp / "out1")
            ext1 = tmp / "ext1"
            ext1.mkdir()
            m1 = unpack_skill_bundle(bundle1, ext1)

            bundle2 = pack_skill_bundle(skill_dir, 1, output_dir=tmp / "out2")
            ext2 = tmp / "ext2"
            ext2.mkdir()
            m2 = unpack_skill_bundle(bundle2, ext2)

            self.assertEqual(m1.tree_sha256, m2.tree_sha256)

    def test_bundle_bytes_are_identical_across_repacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)

            out1 = tmp / "out1"
            out1.mkdir()
            b1 = pack_skill_bundle(skill_dir, 5, output_dir=out1)

            out2 = tmp / "out2"
            out2.mkdir()
            b2 = pack_skill_bundle(skill_dir, 5, output_dir=out2)

            self.assertEqual(b1.read_bytes(), b2.read_bytes())

    def test_custom_name_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)

            bundle_path = pack_skill_bundle(
                skill_dir, 2, name="my-deploy", tags=["infra", "core"],
            )
            self.assertIn("my-deploy-v2", bundle_path.name)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)
            self.assertEqual(manifest.name, "my-deploy")
            self.assertEqual(manifest.tags, ["infra", "core"])

    def test_output_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            out = tmp / "bundles"
            out.mkdir()

            bundle_path = pack_skill_bundle(skill_dir, 1, output_dir=out)
            self.assertEqual(bundle_path.parent, out)
            self.assertTrue(bundle_path.exists())

    def test_preserves_existing_skill_meta_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            meta = skill_dir / SKILL_META_DIR
            meta.mkdir()
            (meta / "changelog.md").write_text("## v1\nInitial.\n", encoding="utf-8")
            (meta / "compatibility.json").write_text('{"min_skillbox_version":"1.0.0"}', encoding="utf-8")

            bundle_path = pack_skill_bundle(skill_dir, 1)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)

            self.assertTrue((ext / SKILL_META_DIR / "changelog.md").exists())
            self.assertTrue((ext / SKILL_META_DIR / "compatibility.json").exists())
            self.assertTrue((ext / SKILL_META_DIR / MANIFEST_FILENAME).exists())

            self.assertEqual(len(manifest.files), 2)
            file_paths = sorted(f.path for f in manifest.files)
            self.assertNotIn(f"{SKILL_META_DIR}/changelog.md", file_paths)


class TestUnpackErrors(unittest.TestCase):

    def test_missing_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bundle_path = tmp / "bad.skillbundle.tar.gz"
            with open(bundle_path, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    with tarfile.open(fileobj=gz, mode="w") as tar:
                        data = b"# Hello\n"
                        info = tarfile.TarInfo(name="SKILL.md")
                        info.size = len(data)
                        tar.addfile(info, __import__("io").BytesIO(data))

            ext = tmp / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError) as ctx:
                unpack_skill_bundle(bundle_path, ext)
            self.assertIn(MANIFEST_FILENAME, str(ctx.exception))

    def test_non_gzip_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bad = tmp / "notgzip.skillbundle.tar.gz"
            bad.write_text("this is not gzip", encoding="utf-8")

            ext = tmp / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError) as ctx:
                unpack_skill_bundle(bad, ext)
            self.assertIn("failed to extract", str(ctx.exception))

    def test_plain_tar_not_gzipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bundle_path = tmp / "plain.tar"
            with tarfile.open(bundle_path, "w") as tar:
                data = b"# Hello\n"
                info = tarfile.TarInfo(name="SKILL.md")
                info.size = len(data)
                tar.addfile(info, __import__("io").BytesIO(data))

            ext = tmp / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError):
                unpack_skill_bundle(bundle_path, ext)

    def test_nonexistent_bundle_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir) / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError):
                unpack_skill_bundle(Path(tmpdir) / "nope.tar.gz", ext)

    def test_malformed_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bundle_path = tmp / "bad-json.skillbundle.tar.gz"

            with open(bundle_path, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    with tarfile.open(fileobj=gz, mode="w") as tar:
                        manifest_data = b"not valid json{{"
                        info = tarfile.TarInfo(name=f"{SKILL_META_DIR}/{MANIFEST_FILENAME}")
                        info.size = len(manifest_data)
                        tar.addfile(info, __import__("io").BytesIO(manifest_data))

            ext = tmp / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError) as ctx:
                unpack_skill_bundle(bundle_path, ext)
            self.assertIn("invalid manifest JSON", str(ctx.exception))

    def test_manifest_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bundle_path = tmp / "incomplete.skillbundle.tar.gz"

            manifest = json.dumps({"name": "x"}).encode("utf-8")
            with open(bundle_path, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    with tarfile.open(fileobj=gz, mode="w") as tar:
                        info = tarfile.TarInfo(name=f"{SKILL_META_DIR}/{MANIFEST_FILENAME}")
                        info.size = len(manifest)
                        tar.addfile(info, __import__("io").BytesIO(manifest))

            ext = tmp / "ext"
            ext.mkdir()
            with self.assertRaises(BundleStructureError) as ctx:
                unpack_skill_bundle(bundle_path, ext)
            self.assertIn("missing required fields", str(ctx.exception))


class TestVerifyBundleContents(unittest.TestCase):

    def test_verify_passes_on_valid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            bundle_path = pack_skill_bundle(skill_dir, 1)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)
            verify_bundle_contents(manifest, ext)

    def test_file_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            bundle_path = pack_skill_bundle(skill_dir, 1)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)

            tampered = ext / "SKILL.md"
            tampered.write_text("TAMPERED CONTENT", encoding="utf-8")

            with self.assertRaises(BundleContentMismatchError) as ctx:
                verify_bundle_contents(manifest, ext)
            self.assertIn("SHA256 mismatch", str(ctx.exception))
            self.assertIn("SKILL.md", str(ctx.exception))

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            bundle_path = pack_skill_bundle(skill_dir, 1)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)

            (ext / "SKILL.md").unlink()

            with self.assertRaises(BundleContentMismatchError) as ctx:
                verify_bundle_contents(manifest, ext)
            self.assertIn("missing", str(ctx.exception))

    def test_tree_sha_mismatch_from_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            bundle_path = pack_skill_bundle(skill_dir, 1)

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle_path, ext)

            (ext / "extra.txt").write_text("surprise", encoding="utf-8")

            with self.assertRaises(BundleContentMismatchError) as ctx:
                verify_bundle_contents(manifest, ext)
            self.assertIn("tree SHA256 mismatch", str(ctx.exception))


class TestBundleManifestSerialization(unittest.TestCase):

    def test_to_dict_from_dict_round_trip(self) -> None:
        manifest = BundleManifest(
            name="test-skill",
            version=5,
            tree_sha256="a" * 64,
            min_skillbox_version="1.2.0",
            tags=["core"],
            files=[BundleFileEntry(path="SKILL.md", sha256="b" * 64)],
        )
        d = manifest.to_dict()
        restored = BundleManifest.from_dict(d)
        self.assertEqual(restored.name, manifest.name)
        self.assertEqual(restored.version, manifest.version)
        self.assertEqual(restored.tree_sha256, manifest.tree_sha256)
        self.assertEqual(restored.min_skillbox_version, manifest.min_skillbox_version)
        self.assertEqual(restored.tags, manifest.tags)
        self.assertEqual(len(restored.files), 1)
        self.assertEqual(restored.files[0].path, "SKILL.md")

    def test_from_dict_missing_fields(self) -> None:
        with self.assertRaises(BundleStructureError):
            BundleManifest.from_dict({"name": "x", "version": 1})

    def test_from_dict_malformed_files(self) -> None:
        with self.assertRaises(BundleStructureError):
            BundleManifest.from_dict({
                "name": "x",
                "version": 1,
                "tree_sha256": "a" * 64,
                "min_skillbox_version": "0.0.0",
                "tags": [],
                "files": [{"bad_key": "value"}],
            })


class TestComputeTreeSha(unittest.TestCase):

    def test_excludes_skill_meta_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)

            sha_before = compute_tree_sha256(skill_dir)

            meta = skill_dir / SKILL_META_DIR
            meta.mkdir()
            (meta / "manifest.json").write_text("{}", encoding="utf-8")

            sha_after = compute_tree_sha256(skill_dir)
            self.assertEqual(sha_before, sha_after)

    def test_deterministic_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            skill_dir = _make_skill_dir(tmp)
            self.assertEqual(
                compute_tree_sha256(skill_dir),
                compute_tree_sha256(skill_dir),
            )

    def test_different_content_different_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            d1 = _make_skill_dir(tmp, name="s1")
            d2 = _make_skill_dir(tmp, name="s2")
            (d2 / "SKILL.md").write_text("different content", encoding="utf-8")
            self.assertNotEqual(
                compute_tree_sha256(d1),
                compute_tree_sha256(d2),
            )


class TestPackEdgeCases(unittest.TestCase):

    def test_nonexistent_source_raises(self) -> None:
        with self.assertRaises(Exception):
            pack_skill_bundle(Path("/nonexistent/dir"), 1)

    def test_empty_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            empty = tmp / "empty-skill"
            empty.mkdir()

            bundle = pack_skill_bundle(empty, 1)
            self.assertTrue(bundle.exists())

            ext = tmp / "ext"
            ext.mkdir()
            manifest = unpack_skill_bundle(bundle, ext)
            self.assertEqual(manifest.files, [])
            verify_bundle_contents(manifest, ext)


if __name__ == "__main__":
    unittest.main()
