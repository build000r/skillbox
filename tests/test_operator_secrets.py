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
RECONCILE_MODULE = SourceFileLoader(
    "skillbox_reconcile_secrets",
    str((SCRIPTS_DIR / "04-reconcile.py").resolve()),
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

    def test_refuses_in_mount_legacy_secret_file(self) -> None:
        # A secret file that only exists at the repo root (inside the workspace
        # bind mount) must be a hard error, never a soft-warned load.
        for module in MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp) / "repo"
                repo_root.mkdir(parents=True)
                state_root = repo_root / ".skillbox-state"
                legacy_path = repo_root / ".env.box"
                legacy_path.write_text("Y=2\n", encoding="utf-8")
                new_path = state_root / "operator" / ".env.box"

                loaded: list[Path] = []
                with mock.patch.object(module, "REPO_ROOT", repo_root), \
                    mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(state_root)}), \
                    mock.patch.object(module, "load_dotenv", side_effect=loaded.append):
                    with self.assertRaises(SystemExit) as raised:
                        module.load_operator_secret(".env.box")

                # Nothing was loaded from the in-mount file.
                self.assertEqual(loaded, [])
                message = str(raised.exception)
                self.assertIn("REFUSING", message)
                self.assertIn(str(legacy_path), message)
                # The message must point at the sanctioned path and migration command.
                self.assertIn(f"mv {legacy_path} {new_path}", message)
                self.assertIn(f"mkdir -p {new_path.parent}", message)
                self.assertIn(f"chmod 600 {new_path}", message)

    def test_noop_when_neither_present(self) -> None:
        for module in MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp) / "repo"
                repo_root.mkdir(parents=True)
                state_root = repo_root / ".skillbox-state"

                loaded, err = self._run(module, repo_root, state_root, ".env")
                self.assertEqual(loaded, [])
                self.assertEqual(err, "")


class OperatorSecretContainmentDoctorTests(unittest.TestCase):
    """Doctor-side guard for the sanctioned operator-secret layout (04-reconcile)."""

    def _check(self, repo_root: Path):
        with mock.patch.object(RECONCILE_MODULE, "ROOT_DIR", repo_root), \
            mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(repo_root / ".skillbox-state")}):
            return RECONCILE_MODULE.check_operator_secret_containment()

    def _operator_dir(self, repo_root: Path) -> Path:
        operator_dir = repo_root / ".skillbox-state" / "operator"
        operator_dir.mkdir(parents=True, exist_ok=True)
        return operator_dir

    def test_fails_on_planted_repo_root_env_box(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            (repo_root / ".env.box").write_text("SKILLBOX_DO_TOKEN=planted\n", encoding="utf-8")

            result = self._check(repo_root)
            self.assertEqual(result.status, "fail")
            self.assertEqual(result.code, "operator-secret-containment")
            self.assertTrue(any(".env.box" in issue for issue in result.details["issues"]))
            self.assertIn("mv ./.env.box ./.skillbox-state/operator/.env.box", result.fix_command)

    def test_fails_on_group_or_other_readable_operator_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            secret = self._operator_dir(repo_root) / ".env.box"
            secret.write_text("SKILLBOX_DO_TOKEN=contained\n", encoding="utf-8")
            secret.chmod(0o640)

            result = self._check(repo_root)
            self.assertEqual(result.status, "fail")
            self.assertEqual(result.code, "operator-secret-containment")
            self.assertTrue(any("group/other-accessible" in issue for issue in result.details["issues"]))
            self.assertEqual(result.fix_command, f"chmod 600 {secret}")

    def test_passes_on_contained_owner_only_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            for name in (".env", ".env.box"):
                secret = self._operator_dir(repo_root) / name
                secret.write_text("KEY=value\n", encoding="utf-8")
                secret.chmod(0o600)

            result = self._check(repo_root)
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.code, "operator-secret-containment")
            self.assertEqual(result.details, {"issues": []})


if __name__ == "__main__":
    unittest.main()
