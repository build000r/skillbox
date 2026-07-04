from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import (
    make_fake_binary,
    make_runtime_model,
    make_temp_workspace,
    normalize_golden,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.runtime_model import validate_runtime_model_ids  # noqa: E402


class TestHelpersTests(unittest.TestCase):
    def test_make_runtime_model_passes_runtime_id_validator_and_deep_merges(self) -> None:
        model = make_runtime_model(
            env={"EXTRA_ENV": "1"},
            selection={"extra": "kept"},
            clients=[{"id": "robot", "label": "Robot"}],
        )

        validate_runtime_model_ids(model)
        self.assertEqual(model["env"]["SKILLBOX_WORKSPACE_ROOT"], "/workspace")
        self.assertEqual(model["env"]["EXTRA_ENV"], "1")
        self.assertEqual(model["selection"]["default_client"], "personal")
        self.assertEqual(model["selection"]["extra"], "kept")
        self.assertEqual(model["clients"], [{"id": "robot", "label": "Robot"}])

    def test_make_temp_workspace_materializes_and_cleans_file_tree(self) -> None:
        with make_temp_workspace(
            {
                "README.md": "hello\n",
                "bin": {"tool": b"binary\n"},
                "empty": None,
            }
        ) as root:
            captured = root
            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual((root / "bin" / "tool").read_bytes(), b"binary\n")
            self.assertTrue((root / "empty").is_dir())

        self.assertFalse(captured.exists())

    def test_normalize_golden_replaces_temp_root_and_pressure_body(self) -> None:
        with make_temp_workspace({}) as root:
            raw = (
                f"path: {root}/clients/personal\n"
                "## Pressure And Offload Policy\n"
                "host-specific pressure detail\n"
                "\n"
                "## Next Section\n"
                "stable\n"
            )

            normalized = normalize_golden(raw, root)

        self.assertIn("path: <ROOT>/clients/personal", normalized)
        self.assertNotIn("host-specific pressure detail", normalized)
        self.assertIn("<PRESSURE-ADVISORY-NORMALIZED>", normalized)
        self.assertIn("## Next Section\nstable", normalized)

    def test_make_fake_binary_creates_executable_path_shim(self) -> None:
        with make_temp_workspace({}) as root:
            binary = make_fake_binary(root, "fake-tool", "printf 'ok\\n'\n")

            self.assertTrue(os.access(binary, os.X_OK))
            result = subprocess.run(
                [str(binary)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
