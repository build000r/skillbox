"""Lazy facade for the runtime_manager package (PEP 562).

Historically this module eagerly star-imported every submodule (~1.9MB of
source), so ``import runtime_manager`` paid the full package load even when a
caller only needed one name. Long-lived processes such as the MCP servers now
import the package once and dispatch read-only commands in-process, so the
facade defers submodule loading until the first attribute access while
preserving the exact star-import surface and shadowing order of:

    from .errors import *
    from .shared import *
    from .validation import *
    from .publish import *
    from .runtime_ops import *
    from .skill_visibility import *
    from .mmdx_open import *
    from .operator_booking import *
    from .context_rendering import *
    from .state_backup import *
    from .text_renderers import *
    from .workflows import *
    from .cli import *

``command_registry`` stays the command_registry MODULE (not cli.py's
command_registry() helper), exactly as the eager facade guaranteed, and stays
importable without loading the rest of the package (it is stdlib-only).
"""
from __future__ import annotations

from importlib import import_module as _import_module

# Star-import order is the shadowing contract: later modules win.
_STAR_SUBMODULES = (
    ".errors",
    ".shared",
    ".validation",
    ".publish",
    ".runtime_ops",
    ".skill_visibility",
    ".mmdx_open",
    ".operator_booking",
    ".context_rendering",
    ".state_backup",
    ".text_renderers",
    ".workflows",
    ".cli",
)

_NAMESPACE_LOADED = False


def _star_names(module: object) -> list[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return list(declared)
    return [name for name in vars(module) if not name.startswith("_")]


def _load_namespace() -> None:
    """Materialize the eager facade surface exactly once."""
    global _NAMESPACE_LOADED
    if _NAMESPACE_LOADED:
        return
    _NAMESPACE_LOADED = True
    namespace: dict[str, object] = {}
    for relative_name in _STAR_SUBMODULES:
        module = _import_module(relative_name, __name__)
        for name in _star_names(module):
            namespace[name] = getattr(module, name)
    # Preserve `from runtime_manager import command_registry` as the
    # command_registry module even though cli.py also has a command_registry()
    # helper.
    namespace["command_registry"] = _import_module(".command_registry", __name__)
    globals().update(namespace)


def __getattr__(name: str):
    if name == "command_registry":
        module = _import_module(".command_registry", __name__)
        globals()["command_registry"] = module
        return module
    _load_namespace()
    try:
        return globals()[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    _load_namespace()
    return sorted(globals())
