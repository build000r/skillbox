from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"

BOX_MODULE = SourceFileLoader(
    "skillbox_box_secrets",
    str((SCRIPTS_DIR / "box.py").resolve()),
).load_module()
OPERATOR_MODULE = SourceFileLoader(
    "skillbox_operator_secrets",
    str((SCRIPTS_DIR / "operator_mcp_server.py").resolve()),
).load_module()

# Both modules deliberately duplicate the same resolution helpers; exercise both.
MODULES = (BOX_MODULE, OPERATOR_MODULE)


class OperatorSecretDirTests(unittest.TestCase):
    def test_default_relative_state_root_resolves_under_repo_root(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__):
                fake_root = Path("/fake/repo")
                with mock.patch.object(module, "REPO_ROOT", fake_root), \
                    mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("SKILLBOX_STATE_ROOT", None)
                    self.assertEqual(
                        module.operator_secret_dir(),
                        Path("/fake/repo/.skillbox-state/operator"),
                    )

    def test_relative_state_root_resolves_against_repo_root(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__):
                fake_root = Path("/fake/repo")
                with mock.patch.object(module, "REPO_ROOT", fake_root), \
                    mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": "./custom-state"}):
                    self.assertEqual(
                        module.operator_secret_dir(),
                        Path("/fake/repo/custom-state/operator"),
                    )

    def test_absolute_state_root_is_honored(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__):
                with mock.patch.object(module, "REPO_ROOT", Path("/fake/repo")), \
                    mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": "/var/lib/skillbox"}):
                    self.assertEqual(
                        module.operator_secret_dir(),
                        Path("/var/lib/skillbox/operator"),
                    )


class LoadOperatorSecretTests(unittest.TestCase):
    def _run(self, module, repo_root: Path, state_root: Path, name: str):
        """Invoke load_operator_secret with mocked roots; capture stderr + load calls."""
        loaded: list[Path] = []
        stderr = io.StringIO()
        with mock.patch.object(module, "REPO_ROOT", repo_root), \
            mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(state_root)}), \
            mock.patch.object(module, "load_dotenv", side_effect=loaded.append), \
            redirect_stderr(stderr):
            module.load_operator_secret(name)
        return loaded, stderr.getvalue()

    def test_prefers_relocated_location_without_warning(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp) / "repo"
                state_root = repo_root / ".skillbox-state"
                operator_dir = state_root / "operator"
                operator_dir.mkdir(parents=True)
                new_path = operator_dir / ".env"
                new_path.write_text("X=1\n", encoding="utf-8")
                # Plant a legacy copy too — the relocated one must win.
                repo_root.mkdir(parents=True, exist_ok=True)
                (repo_root / ".env").write_text("X=legacy\n", encoding="utf-8")

                loaded, err = self._run(module, repo_root, state_root, ".env")
                self.assertEqual(loaded, [new_path])
                self.assertEqual(err, "")

    def test_falls_back_to_legacy_with_deprecation_warning(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp) / "repo"
                repo_root.mkdir(parents=True)
                state_root = repo_root / ".skillbox-state"
                legacy_path = repo_root / ".env.box"
                legacy_path.write_text("Y=2\n", encoding="utf-8")
                new_path = state_root / "operator" / ".env.box"

                loaded, err = self._run(module, repo_root, state_root, ".env.box")
                self.assertEqual(loaded, [legacy_path])
                self.assertIn("DEPRECATED secret location", err)
                self.assertIn(str(legacy_path), err)
                # Exact migration `mv` text must be present.
                self.assertIn(f"mv {legacy_path} {new_path}", err)
                self.assertIn(f"mkdir -p {new_path.parent}", err)

    def test_noop_when_neither_present(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp) / "repo"
                repo_root.mkdir(parents=True)
                state_root = repo_root / ".skillbox-state"

                loaded, err = self._run(module, repo_root, state_root, ".env")
                self.assertEqual(loaded, [])
                self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
