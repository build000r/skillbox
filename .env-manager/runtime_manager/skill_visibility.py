"""Skill-visibility facade.

Historically this module was a single ~5400-line file that accreted machine /
registry resolution, scope-rule evaluation, the overlay registry, the
broken-link taxonomy, parity, evidence annotation, and audit-row serialization
across many epics. The implementation has been decomposed into cohesive, layered
source modules for maintainability:

    ._skill_common  -- dependency-free leaf helpers + layer-rank constants
    .policy_eval    -- scope rules, layer ranking, machine/registry/alias
                       resolution, overlay matching (depends on _skill_common)
    .inventory      -- skill sources, candidates, occurrences, global-home
                       resolution, parity (depends on policy_eval)
    .audit_report   -- visibility snapshot assembly, issue groups, fleet/audit
                       rows, broken-link taxonomy, evidence annotation, explain
                       view, serialization/compaction, text renderers
                       (depends on inventory)
    .lifecycle      -- link/unlink/sync/activate plans + apply, overlay activate
                       (top layer; depends on audit_report)

The layering is a strict DAG (common < policy_eval < inventory < audit_report <
lifecycle), so there are no circular imports: each module imports only from
lower layers (see each module's ``from .<lower> import *`` header). Every module
declares an explicit ``__all__`` covering its public *and* underscore-prefixed
symbols, so the layered star-imports surface every name.

This module remains the single public namespace. To preserve the original
single-file runtime semantics *exactly*, the decomposed module *bodies* (the
symbol definitions, excluding their module docstring, imports, and ``__all__``)
are executed -- in dependency order, under one shared import header -- directly
into THIS module's namespace. That guarantees:

  * every existing ``from .skill_visibility import X`` and
    ``from runtime_manager.skill_visibility import X`` keeps working unchanged;
  * ``from .skill_visibility import *`` (used by cli.py and
    runtime_manager/__init__.py) still surfaces every name;
  * every moved function's ``globals()`` is THIS module's namespace, so the
    ``globals().get('_machines_classifier_override')`` /
    ``globals().get('_registry_doctor_module_override')`` injection hooks resolve
    here, and ``mock.patch('runtime_manager.skill_visibility.<name>')`` /
    ``mock.patch.object(sv, '<name>')`` patch the exact binding the moved code
    executes against -- behavior is byte-identical to the pre-split module.

The standalone modules remain independently importable (each is valid Python
with its own header and star-imports); only the facade re-executes their bodies
into one namespace so the package keeps behaving as a single module.
"""

from __future__ import annotations

import ast as _ast
import os as _os

# The shared import header every moved symbol was originally defined under. It is
# executed once into this namespace before any module body, so the bodies resolve
# their stdlib / .shared dependencies here (exactly as the original file did).
_SKILL_VISIBILITY_HEADER = """\
from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from .shared import (
    GLOBAL_HOME_ROOT_ENV,
    GLOBAL_HOME_SURFACES,
    atomic_write_text,
    directory_tree_sha256,
    load_json_file,
    load_yaml,
    load_skill_repos_config,
)
from .errors import PRUNE_SKIPPED_PINNED
"""

# Decomposed modules in strict dependency (layer) order.
_SKILL_VISIBILITY_SUBMODULES = (
    "_skill_common",
    "policy_eval",
    "inventory",
    "audit_report",
    "lifecycle",
)

_skill_visibility_dir = _os.path.dirname(_os.path.abspath(__file__))


def _skill_visibility_module_body(path: str) -> str:
    """Return a module's symbol definitions, dropping its header.

    Strips the leading module docstring, every top-level ``import`` /
    ``from ... import``, the ``yaml`` import guard, and the ``__all__``
    assignment -- i.e. everything the shared header (or the facade itself)
    already provides -- leaving only the function / class / constant
    definitions to execute into the shared namespace.
    """
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    source_lines = source.splitlines(keepends=True)
    tree = _ast.parse(source)

    def _is_header_node(node: _ast.stmt) -> bool:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            return True
        if isinstance(node, _ast.Try):
            # The ``try: import yaml`` guard.
            return True
        if isinstance(node, _ast.Assign) and any(
            isinstance(t, _ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return True
        return False

    # Index of the first non-header top-level statement (skip the docstring too).
    body = list(tree.body)
    start_line = None
    for index, node in enumerate(body):
        if index == 0 and isinstance(node, _ast.Expr) and isinstance(
            getattr(node, "value", None), _ast.Constant
        ) and isinstance(node.value.value, str):
            continue  # module docstring
        if _is_header_node(node):
            continue
        start_line = node.lineno
        break
    if start_line is None:
        return ""
    return "".join(source_lines[start_line - 1 :])


def _load_skill_visibility_modules() -> list[str]:
    """Execute each submodule body into this namespace; return moved names.

    Imports each submodule normally first (so it stays an independently
    importable module and its ``__all__`` is authoritative), then re-executes
    its body here so every moved symbol shares this single namespace.
    """
    namespace = globals()
    exec(compile(_SKILL_VISIBILITY_HEADER, "<skill_visibility-header>", "exec"), namespace)  # noqa: S102
    import importlib

    exported: list[str] = []
    seen: set[str] = set()
    package = __name__.rsplit(".", 1)[0]
    for mod_name in _SKILL_VISIBILITY_SUBMODULES:
        module = importlib.import_module(f"{package}.{mod_name}")
        path = _os.path.join(_skill_visibility_dir, f"{mod_name}.py")
        body = _skill_visibility_module_body(path)
        code = compile(body, path, "exec")
        exec(code, namespace)  # noqa: S102 - intentional shared-namespace load
        for name in getattr(module, "__all__", ()):  # authoritative moved names
            if name not in seen:
                seen.add(name)
                exported.append(name)
    return exported


__all__ = _load_skill_visibility_modules()
