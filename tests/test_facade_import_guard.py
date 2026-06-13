"""Guard against the facade import-stripping bug class.

``runtime_manager/skill_visibility.py`` is a facade that executes each submodule
body (``_skill_common``, ``policy_eval``, ``inventory``, ``audit_report``,
``lifecycle``) into the facade namespace AFTER stripping the submodule's own
top-level imports, relying on a single fixed header (``_SKILL_VISIBILITY_HEADER``)
for shared imports. Any top-level ``import`` a submodule adds that the header does
not also provide silently vanishes -> the name NameErrors at runtime whenever a
function that uses it is called (this is exactly how ``import shlex`` shipped into
``audit_report`` and broke broken-link classification only when invoked).

This test fails loudly the moment a submodule's top-level import is not resolvable
in the facade namespace, so the fix is forced to be deliberate: add the import to
the header, or make it function-local.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ENV_MANAGER_DIR = Path(__file__).resolve().parents[1] / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

import runtime_manager.skill_visibility as facade  # noqa: E402

_SUBMODULES = ["_skill_common", "policy_eval", "inventory", "audit_report", "lifecycle"]
_RT_DIR = Path(facade.__file__).resolve().parent


def _top_level_import_bindings(src: str) -> set[str]:
    bindings: set[str] = set()
    for node in ast.parse(src).body:  # top-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":  # `from .lower import *` re-exports
                    continue
                bindings.add(alias.asname or alias.name)
    return bindings


class FacadeImportGuardTests(unittest.TestCase):
    def test_every_submodule_top_level_import_resolves_in_the_facade(self) -> None:
        missing: dict[str, list[str]] = {}
        for mod in _SUBMODULES:
            src = (_RT_DIR / f"{mod}.py").read_text(encoding="utf-8")
            absent = sorted(
                name for name in _top_level_import_bindings(src)
                if not hasattr(facade, name)
            )
            if absent:
                missing[mod] = absent
        self.assertEqual(
            missing,
            {},
            "submodule top-level imports are stripped by the facade and not "
            "provided by _SKILL_VISIBILITY_HEADER -> they will NameError at "
            "runtime. Add them to the header or make them function-local: "
            f"{missing}",
        )


if __name__ == "__main__":
    unittest.main()
