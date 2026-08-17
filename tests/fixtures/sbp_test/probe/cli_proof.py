"""Real-CLI probe proof (skillbox-sbp-test-probe-mode-sz4d).

Runs `sbp test score --probe` end to end against a **disposable copy** of the
consumer fixture, with services and network explicitly denied, and asserts that
every probe kind actually executed rather than refusing.

This exists as a fixture-owned script, not a one-off shell command, because the
claim it makes ("the CLI really probes") is the acceptance criterion for this
bead and has to be re-runnable by anyone reviewing it:

    python3 tests/fixtures/sbp_test/probe/cli_proof.py

It is also driven by ``tests/test_sbp_test_probe.py`` so the proof is part of the
suite rather than a screenshot in a report.

Two separate copies of the fixture are made on purpose:

* ``consumer/`` is the tree being *scored*. It is hashed before and after and
  must not change by a single byte.
* ``workspace/`` is the admitted disposable capsule the probes actually execute
  in -- what a real capsule extraction would have materialized.

Both live in a temporary directory that is discarded with the process. Nothing
here touches the repository, a service, or the network: the fixture's declared
units are two `python3 -c print(...)` calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "consumer"
ROOT_DIR = Path(__file__).resolve().parents[4]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"

#: A capsule digest the workspace marker is stamped with. The proof does not
#: pass --probe-capsule, so this is never compared against a store; it only has
#: to be a well-formed digest.
FIXTURE_ARCHIVE = "a" * 64

EXPECTED_KINDS = (
    "serial_repeat",
    "concurrency_two",
    "concurrency_n",
    "randomized_order",
    "synthetic_canary",
    "cleanup_leak",
)


def tree_digest(root: Path) -> dict[str, str]:
    """Path -> content hash. Catches an in-place rewrite of the same size."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_cli_probe(base: Path, *, budget_s: float = 120.0, max_parallel: int = 4) -> dict:
    """Materialize the fixture, run the real CLI, return the parsed payload."""
    sys.path.insert(0, str(ENV_MANAGER_DIR))
    from runtime_manager import sbp_test_probe as probe_mode

    consumer = base / "consumer"
    workspace = base / "workspace"
    shutil.copytree(FIXTURE, consumer)
    shutil.copytree(FIXTURE, workspace)
    probe_mode.write_workspace_marker(workspace, FIXTURE_ARCHIVE)

    before = tree_digest(consumer)
    completed = subprocess.run(
        [
            sys.executable,
            str(ENV_MANAGER_DIR / "manage.py"),
            "test",
            "score",
            "--cwd",
            str(consumer),
            "--probe",
            "--probe-workspace",
            str(workspace),
            "--probe-budget-s",
            str(budget_s),
            "--probe-max-parallel",
            str(max_parallel),
            # The two permissions the acceptance criterion requires be denied.
            "--probe-deny-services",
            "--probe-deny-network",
            "--probe-repeats",
            "3",
            "--probe-seed",
            "7",
            "--format",
            "json",
        ],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
    )
    payload = json.loads(completed.stdout)
    payload["_exit_code"] = completed.returncode
    payload["_consumer_before"] = before
    payload["_consumer_after"] = tree_digest(consumer)
    payload["_workspace"] = str(workspace)
    return payload


def check(payload: dict) -> list[str]:
    """Every acceptance assertion, as a list of failures (empty means green)."""
    failures: list[str] = []
    receipt = payload.get("probe_receipt") or {}
    probes = {p["kind"]: p for p in receipt.get("probes") or []}

    if payload.get("error_code") == "probe_runner_missing":
        failures.append("REGRESSION: CLI still refuses with probe_runner_missing")
    if not payload.get("probed"):
        failures.append(f"probed is False (error_code={payload.get('error_code')!r})")

    for kind in EXPECTED_KINDS:
        probe = probes.get(kind)
        if probe is None:
            failures.append(f"{kind}: absent from the receipt")
        elif probe["state"] == "refused":
            failures.append(f"{kind}: refused ({probe.get('refusal_code')})")
        elif probe["attempts"] < 1 and kind != "cleanup_leak":
            failures.append(f"{kind}: recorded {probe['attempts']} attempts")

    authority = receipt.get("authority") or {}
    if authority.get("services_permitted") is not False:
        failures.append("services were not denied")
    if authority.get("network_permitted") is not False:
        failures.append("network was not denied")

    if payload["_consumer_before"] != payload["_consumer_after"]:
        changed = sorted(
            key
            for key in payload["_consumer_before"]
            if payload["_consumer_before"][key] != payload["_consumer_after"].get(key)
        )
        failures.append(f"consumer tree mutated: {changed}")

    scratch = Path(payload["_workspace"]) / ".sbp-probe"
    if scratch.exists():
        failures.append("probe scratch survived cleanup")

    return failures


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        payload = run_cli_probe(Path(tmp))
        receipt = payload.get("probe_receipt") or {}
        print(f"exit_code        : {payload['_exit_code']}")
        print(f"probed           : {payload.get('probed')}")
        print(f"error_code       : {payload.get('error_code')}")
        print(f"receipt_digest   : {payload.get('probe_receipt_digest')}")
        print(f"counts           : {receipt.get('counts')}")
        print(f"budget_exhausted : {receipt.get('budget_exhausted')}")
        for probe in receipt.get("probes") or []:
            detail = f" -- {probe['detail']}" if probe.get("detail") else ""
            print(
                f"  {probe['state']:<8} {probe['kind']:<18} "
                f"attempts={probe['attempts']}{detail}"
            )
        upgrades = payload.get("probe_upgrades") or []
        for upgrade in upgrades:
            print(f"  proven   {upgrade['finding_code']} via {upgrade['probe_kind']}")
        if not upgrades:
            print("  proven   none (no probe met an exact proof requirement)")

        failures = check(payload)
        for failure in failures:
            print(f"FAIL: {failure}")
        print("PROOF GREEN" if not failures else "PROOF RED")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
