#!/usr/bin/env python3
"""Validate a DCG protocol e2e receipt (skillbox-dcg-agent-protocol-e2e-ln4z).

A receipt is only evidence if something refuses to accept a bad one. This
validator is that something, and it fails CLOSED: anything it cannot positively
verify is a failure, never a pass.

Identity binding (skillbox-jkl3). A receipt lives at
``.../evidence/dcg/<implementation_sha>/<name>.json``. Three things must agree
before it is identity-bound, and disagreement is a rejection:

  1. the SHA in the receipt's parent DIRECTORY name
  2. ``implementation_sha`` inside the receipt
  3. ``--implementation-sha`` (the tree under test)

and the receipt must declare ``worktree_clean: true`` -- evidence produced from a
dirty tree describes code that no SHA names. Before this, only (2) vs (3) was
checked, so a receipt could be filed under one commit while attesting another.

Two documented flags may downgrade a binding to a warning. They are never
silent: each prints ``DCG_RECEIPT_WARN`` and the success line then says
``NOT identity-bound``.

  * ``--allow-unbound-path``  receipt is outside the canonical layout
  * ``--allow-dirty-tree``    receipt cannot attest a clean tree

``--require-clean-tree`` additionally verifies the live tree, and
``--allow-dirty-tree`` deliberately cannot silence it.

It rejects:
  * a receipt whose directory, contents, and claimed tree SHA disagree
  * a receipt that does not declare, or cannot prove, a clean worktree
  * a missing or empty required identity field
  * an implementation SHA that does not match the tree being claimed (stale
    source -- a receipt from other code proves nothing about this code)
  * any agent whose harmless command was not allowed
  * any agent whose destructive command was not denied
  * a malformed payload that was ALLOWED (it must fail closed)
  * a timed-out probe treated as a pass
  * an absent binary/policy/hook/trust digest
  * executed != false anywhere, or a present execution sentinel

Standard library only. Read-only: it validates, it never repairs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

OK_MARKER = "DCG_RECEIPT_OK"
FAIL_PREFIX = "DCG_RECEIPT_FAIL"
#: Emitted when an identity check was DOWNGRADED by an explicit operator flag.
#: A downgrade is never silent: the receipt still validates, but the run says
#: out loud which binding it stopped enforcing.
WARN_PREFIX = "DCG_RECEIPT_WARN"

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

REQUIRED_TOP_FIELDS = (
    "schema",
    "implementation_sha",
    "binary_version",
    "binary_sha256",
    "policy_sha256",
    "hook_state_sha256",
    "timestamp",
    "agents",
)
REQUIRED_AGENT_FIELDS = ("name", "agent", "safe", "destructive", "executed", "timestamp")
REQUIRED_PROBE_FIELDS = ("decision", "payload_sha256", "executed")

# Digests that must be real. "absent" is the harness's honest marker for a
# missing artifact, and a receipt built on a missing binary or policy is not
# proof of anything.
MUST_NOT_BE_ABSENT = ("binary_sha256", "policy_sha256", "hook_state_sha256")

REQUIRED_AGENTS = ("claude", "codex", "grok")


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def validate(receipt: dict[str, Any], *, implementation_sha: str | None) -> list[str]:
    problems: list[str] = []

    for field in REQUIRED_TOP_FIELDS:
        if field not in receipt:
            _fail(problems, f"missing required field: {field}")
        elif isinstance(receipt[field], str) and not receipt[field].strip():
            _fail(problems, f"required field is empty: {field}")

    for field in MUST_NOT_BE_ABSENT:
        if str(receipt.get(field, "")).strip() == "absent":
            _fail(problems, f"{field} is 'absent'; the run had no real artifact to measure")

    if implementation_sha is not None:
        claimed = str(receipt.get("implementation_sha", "")).strip()
        if not claimed:
            _fail(problems, "implementation_sha is missing; identity is required")
        elif claimed != implementation_sha.strip():
            _fail(
                problems,
                f"stale source SHA: receipt claims {claimed[:12]} but tree is "
                f"{implementation_sha.strip()[:12]}",
            )

    agents = receipt.get("agents")
    if not isinstance(agents, list) or not agents:
        _fail(problems, "receipt carries no agents")
        return problems

    seen = set()
    for entry in agents:
        if not isinstance(entry, dict):
            _fail(problems, "agent entry is not an object")
            continue
        name = str(entry.get("name", "?"))
        seen.add(name)
        for field in REQUIRED_AGENT_FIELDS:
            if field not in entry:
                _fail(problems, f"{name}: missing agent field {field}")
        if entry.get("executed") is not False:
            _fail(problems, f"{name}: executed must be false, got {entry.get('executed')!r}")

        for probe_name, expected in (("safe", "allow"), ("destructive", "deny")):
            probe = entry.get(probe_name)
            if not isinstance(probe, dict):
                _fail(problems, f"{name}: {probe_name} probe missing")
                continue
            for field in REQUIRED_PROBE_FIELDS:
                if field not in probe:
                    _fail(problems, f"{name}.{probe_name}: missing {field}")
            if probe.get("timed_out"):
                _fail(problems, f"{name}.{probe_name}: timed out; a hang is not a verdict")
            if probe.get("decision") != expected:
                _fail(
                    problems,
                    f"{name}.{probe_name}: decision {probe.get('decision')!r}, expected {expected!r}",
                )
            if probe.get("executed") is not False:
                _fail(problems, f"{name}.{probe_name}: executed must be false")

    for required in REQUIRED_AGENTS:
        if required not in seen:
            _fail(problems, f"receipt does not cover required agent: {required}")

    malformed = receipt.get("malformed")
    if not isinstance(malformed, dict):
        _fail(problems, "missing malformed-input probe")
    elif malformed.get("decision") == "allow":
        _fail(problems, "malformed payload was ALLOWED; it must fail closed")

    sentinel = receipt.get("sentinel") or {}
    if sentinel.get("present"):
        _fail(problems, "execution sentinel is present; a guarded command RAN")

    if receipt.get("executed") is not False:
        _fail(problems, "receipt-level executed must be false")

    return problems


def check_path_identity(
    path: Path, receipt: dict[str, Any], implementation_sha: str | None
) -> list[str]:
    """The receipt's LOCATION must agree with the identity it claims.

    A receipt lives at ``.../evidence/dcg/<implementation_sha>/<name>.json``. If
    the directory naming a SHA and the SHA written inside can disagree, the
    receipt can be filed under one commit while attesting another -- which is
    how a stale proof gets read as a current one. All three must agree.
    """
    problems: list[str] = []
    directory = path.parent.name
    if not SHA_PATTERN.match(directory):
        problems.append(
            f"receipt directory {directory!r} is not a 40-hex implementation SHA; "
            "identity cannot be derived from the path"
        )
        return problems

    claimed = str(receipt.get("implementation_sha", "")).strip()
    if directory != claimed:
        problems.append(
            f"path/receipt SHA mismatch: directory says {directory[:12]}, "
            f"receipt says {claimed[:12] or '<empty>'}"
        )
    if implementation_sha and directory != implementation_sha.strip():
        problems.append(
            f"path/tree SHA mismatch: directory says {directory[:12]}, "
            f"tree is {implementation_sha.strip()[:12]}"
        )
    return problems


def check_tree_identity(receipt: dict[str, Any], *, require_clean_tree: bool) -> list[str]:
    """The receipt must attest a CLEAN tree, and be able to prove it.

    Evidence produced from a dirty worktree describes code that no SHA names, so
    ``worktree_clean`` is required to be present AND true. Absent is a rejection,
    not a pass: unverifiable is never the same as verified.
    """
    problems: list[str] = []
    declared = receipt.get("worktree_clean")
    if declared is None:
        problems.append(
            "receipt does not declare worktree_clean; a receipt that cannot say "
            "whether its tree was clean is not identity-bound"
        )
    elif declared is not True:
        problems.append(
            f"receipt declares worktree_clean={declared!r}; evidence from a dirty "
            "tree describes code that no SHA names"
        )

    if require_clean_tree:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            problems.append("--require-clean-tree: `git status` failed; cannot verify the tree")
        elif result.stdout.strip():
            dirty = len(result.stdout.strip().splitlines())
            problems.append(f"--require-clean-tree: worktree has {dirty} uncommitted entr(ies)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a DCG protocol e2e receipt")
    parser.add_argument("receipt")
    parser.add_argument("--implementation-sha", default=None)
    parser.add_argument(
        "--allow-unbound-path",
        action="store_true",
        help=(
            "Downgrade the path-identity check to a warning. Use only for a receipt "
            "outside the canonical evidence/dcg/<sha>/ layout; identity is then no "
            "stronger than --implementation-sha."
        ),
    )
    parser.add_argument(
        "--allow-dirty-tree",
        action="store_true",
        help=(
            "Downgrade the worktree_clean requirement to a warning. The receipt is "
            "then explicitly NOT identity-bound."
        ),
    )
    parser.add_argument(
        "--require-clean-tree",
        action="store_true",
        help="Additionally verify live via `git status --porcelain` that the tree is clean.",
    )
    args = parser.parse_args(argv)

    path = Path(args.receipt)
    if not path.is_file():
        print(f"{FAIL_PREFIX} receipt not found: {path}", file=sys.stderr)
        return 2
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{FAIL_PREFIX} receipt is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(receipt, dict):
        print(f"{FAIL_PREFIX} receipt must be a JSON object", file=sys.stderr)
        return 2

    problems = validate(receipt, implementation_sha=args.implementation_sha)
    warnings: list[str] = []

    path_problems = check_path_identity(path, receipt, args.implementation_sha)
    if args.allow_unbound_path:
        warnings.extend(path_problems)
    else:
        problems.extend(path_problems)

    tree_problems = check_tree_identity(receipt, require_clean_tree=args.require_clean_tree)
    if args.allow_dirty_tree:
        # --require-clean-tree is an explicit demand; --allow-dirty-tree must not
        # be able to silence it, or the two flags together would mean nothing.
        live = [item for item in tree_problems if item.startswith("--require-clean-tree")]
        warnings.extend(item for item in tree_problems if item not in live)
        problems.extend(live)
    else:
        problems.extend(tree_problems)

    for warning in warnings:
        print(f"{WARN_PREFIX} downgraded by flag: {warning}", file=sys.stderr)

    if problems:
        for problem in problems:
            print(f"{FAIL_PREFIX} {problem}", file=sys.stderr)
        return 1
    if warnings:
        print(f"{OK_MARKER} {path} (NOT identity-bound: {len(warnings)} check(s) downgraded)")
        return 0
    print(f"{OK_MARKER} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
