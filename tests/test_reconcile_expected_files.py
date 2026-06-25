from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

RECONCILE = SourceFileLoader(
    "skillbox_reconcile_expected_files",
    str((ROOT_DIR / "scripts" / "04-reconcile.py").resolve()),
).load_module()


@contextmanager
def patched_reconcile_root(root: Path):
    old_root = RECONCILE.ROOT_DIR
    old_workspace = RECONCILE.WORKSPACE_DIR
    try:
        RECONCILE.ROOT_DIR = root
        RECONCILE.WORKSPACE_DIR = root / "workspace"
        yield
    finally:
        RECONCILE.ROOT_DIR = old_root
        RECONCILE.WORKSPACE_DIR = old_workspace


def write_minimal_expected_tree(root: Path) -> None:
    for path, _reason in RECONCILE.CORE_EXPECTED_FILES:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    for path in RECONCILE.WORKSPACE_MANIFEST_FILES:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")


class ExpectedFilesDerivationTests(unittest.TestCase):
    def test_missing_makefile_script_reports_target_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_minimal_expected_tree(root)
            (root / "Makefile").write_text(
                "render:\n"
                "\tpython3 scripts/missing-render.py render\n",
                encoding="utf-8",
            )

            with patched_reconcile_root(root):
                result = RECONCILE.check_required_files()

        self.assertEqual(result.status, "fail")
        self.assertIn("scripts/missing-render.py", result.details["missing"])
        self.assertIn(
            "scripts/missing-render.py (referenced by Makefile target render)",
            result.details["missing_with_provenance"],
        )

    def test_removed_makefile_reference_no_longer_requires_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_minimal_expected_tree(root)
            (root / "Makefile").write_text(
                "render:\n"
                "\tpython3 -c 'print(1)'\n",
                encoding="utf-8",
            )

            with patched_reconcile_root(root):
                result = RECONCILE.check_required_files()

        self.assertEqual(result.status, "pass")

    def test_dockerfile_copy_reference_is_required_even_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_minimal_expected_tree(root)
            (root / "Dockerfile").write_text(
                "FROM scratch\n"
                "COPY docker/sandbox-entrypoint.sh /usr/local/bin/sandbox-entrypoint\n",
                encoding="utf-8",
            )

            with patched_reconcile_root(root):
                result = RECONCILE.check_required_files()

        self.assertEqual(result.status, "fail")
        self.assertIn("docker/sandbox-entrypoint.sh", result.details["missing"])
        self.assertIn(
            "docker/sandbox-entrypoint.sh (COPYed into the image by Dockerfile)",
            result.details["missing_with_provenance"],
        )

    def test_optional_client_compose_override_absent_is_not_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_minimal_expected_tree(root)
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "docker-compose.monoserver.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "Makefile").write_text(
                "_CLIENT_OVERRIDE := workspace/.compose-overrides/docker-compose.client-$(_FOCUS_CLIENT).yml\n"
                "_MONOSERVER_LAYER := $(if $(wildcard $(_CLIENT_OVERRIDE)),$(_CLIENT_OVERRIDE),docker-compose.monoserver.yml)\n"
                "COMPOSEF := $(COMPOSE) -f docker-compose.yml -f $(_MONOSERVER_LAYER)\n",
                encoding="utf-8",
            )

            with patched_reconcile_root(root):
                result = RECONCILE.check_required_files()
                expected_paths = [entry.path for entry in RECONCILE.resolved_expected_files(root)]

        self.assertEqual(result.status, "pass")
        self.assertNotIn(
            "workspace/.compose-overrides/docker-compose.client-$(_FOCUS_CLIENT).yml",
            expected_paths,
        )

    def test_core_expected_files_remain_small_and_justified(self) -> None:
        self.assertLessEqual(len(RECONCILE.CORE_EXPECTED_FILES), 10)
        for path, reason in RECONCILE.CORE_EXPECTED_FILES:
            self.assertTrue(path.strip())
            self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
