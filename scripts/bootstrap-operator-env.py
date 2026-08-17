#!/usr/bin/env python3
"""Seed the operator secret directory under the single-writer lease.

``make bootstrap-env`` used to do this inline:

    @mkdir -p $(_STATE_ROOT)/operator
    @test -f $(_STATE_ROOT)/operator/.env || test -f ./.env \\
        || cp .env.example $(_STATE_ROOT)/operator/.env

Those two lines write *state-root* state, and a Makefile recipe cannot take the
lease — so this was the one Make target that mutated the state root with nothing
serializing it (found by ``tests/test_state_mutation_integration.py``). The
recipe now calls this script, which is the final mutation owner and acquires
``make.bootstrap-env`` for the span of both writes.

Semantics are preserved exactly, because the point is the lock, not a behaviour
change:

* the ``mkdir`` is unconditional and idempotent;
* the seed happens only when BOTH ``<state_root>/operator/.env`` and the
  repo-root ``.env`` are absent — an operator who keeps a repo-root ``.env`` is
  not silently given a second one;
* the source is ``.env.example``, copied, never rendered.

Read-only when there is nothing to do, but it takes the lease either way: the
decision and the write have to be one atomic span, or two processes could both
observe "absent" and both copy.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_ID = "make.bootstrap-env"
EXAMPLE_NAME = ".env.example"


def _load_lease():
    """Import the authoritative lease, or refuse.

    Mirrors ``scripts/lib/doctor_fix.py``: the import is lazy, local, and
    fail-closed. An ungated write is never the degrade path — if the lease
    cannot be reached, seeding the operator directory is not urgent enough to
    do it unserialized.
    """
    env_manager = REPO_ROOT / ".env-manager"
    if str(env_manager) not in sys.path:
        sys.path.insert(0, str(env_manager))
    from runtime_manager import state_mutation  # noqa: PLC0415

    return state_mutation


def bootstrap_operator_env(repo_root: Path | None = None) -> dict[str, object]:
    """Create ``<state_root>/operator`` and seed ``.env`` when absent."""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    state_mutation = _load_lease()
    state_root = state_mutation.canonical_runtime_state_root(root)
    operator_dir = state_root / "operator"
    target = operator_dir / ".env"
    repo_env = root / ".env"
    example = root / EXAMPLE_NAME

    state_root.mkdir(parents=True, exist_ok=True)
    with state_mutation.runtime_mutation_lease(
        BOUNDARY_ID, root_dir=root, annotations={"surface": "make"}
    ):
        operator_dir.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            return {"state_root": str(state_root), "seeded": False, "reason": "already present"}
        if repo_env.is_file():
            return {"state_root": str(state_root), "seeded": False, "reason": "repo-root .env present"}
        if not example.is_file():
            return {"state_root": str(state_root), "seeded": False, "reason": f"{EXAMPLE_NAME} missing"}
        shutil.copyfile(example, target)
        # 0600: the operator directory exists precisely because these files must
        # not be readable by in-container agents (see scripts/box.py).
        os.chmod(target, 0o600)
        return {"state_root": str(state_root), "seeded": True, "reason": f"copied {EXAMPLE_NAME}"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if args:
        print(f"bootstrap-operator-env: unexpected argument {args[0]!r}", file=sys.stderr)
        return 2
    try:
        result = bootstrap_operator_env()
    except Exception as exc:  # noqa: BLE001 — refusing loudly is the safe answer
        print(f"bootstrap-operator-env: refused ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    if result["seeded"]:
        print(f"bootstrap-env: seeded {result['state_root']}/operator/.env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
