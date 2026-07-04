from __future__ import annotations

import argparse
import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import cli as CLI  # noqa: E402


def _parser_command_names() -> set[str]:
    parser = CLI._build_parser()  # noqa: SLF001
    command_action = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction) and action.dest == "command"
    )
    return set(command_action.choices)


class CliDispatchRegistryTests(unittest.TestCase):
    def test_command_registry_covers_every_parser_command(self) -> None:
        parser_commands = _parser_command_names()
        registry = CLI.command_registry()

        self.assertEqual(set(registry), parser_commands)
        self.assertEqual(set(CLI.MANAGE_COMMAND_NAMES), parser_commands)
        for name, spec in registry.items():
            with self.subTest(command=name):
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.help.strip())
                self.assertTrue(callable(spec.handler))

    def test_registry_dispatch_uses_registered_early_handler(self) -> None:
        original = CLI._EARLY_DISPATCH["capabilities"]  # noqa: SLF001
        calls: list[tuple[argparse.Namespace, Path]] = []

        def fake_handler(args: argparse.Namespace, root_dir: Path) -> int:
            calls.append((args, root_dir))
            return 37

        CLI._EARLY_DISPATCH["capabilities"] = fake_handler  # noqa: SLF001
        try:
            args = argparse.Namespace(command="capabilities", format="json")
            root_dir = Path("/repo")
            exit_code = CLI._dispatch_registered_command(args, root_dir, "reuse")  # noqa: SLF001
        finally:
            CLI._EARLY_DISPATCH["capabilities"] = original  # noqa: SLF001

        self.assertEqual(exit_code, 37)
        self.assertEqual(calls, [(args, root_dir)])

    def test_registry_dispatch_routes_model_commands_through_model_loader(self) -> None:
        args = argparse.Namespace(command="status", format="json")
        root_dir = Path("/repo")
        spec = CLI.COMMAND_REGISTRY["status"]

        with mock.patch.object(CLI, "_dispatch_model_command", return_value=43) as dispatch_model:
            exit_code = CLI._dispatch_registered_command(args, root_dir, "reuse")  # noqa: SLF001

        self.assertEqual(exit_code, 43)
        dispatch_model.assert_called_once_with(args, root_dir, "reuse", spec.handler)

    def test_unknown_registered_dispatch_names_suggestion_and_capabilities(self) -> None:
        stderr = StringIO()
        args = argparse.Namespace(command="statu", format="text")

        with redirect_stderr(stderr):
            exit_code = CLI._dispatch_registered_command(args, Path("/repo"), "reuse")  # noqa: SLF001

        self.assertEqual(exit_code, CLI.EXIT_ERROR)
        output = stderr.getvalue()
        self.assertIn("Unknown command: statu", output)
        self.assertIn("Did you mean: `manage.py status`?", output)
        self.assertIn("manage.py capabilities --json", output)


if __name__ == "__main__":
    unittest.main()
