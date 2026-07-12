#!/usr/bin/env python3
"""Closeout report helper for the clipboard bootstrap gate.

Maintains the closeout JSON artifact and owns the PASS/FAIL/SKIP verdict
policy:

- ``smoke`` mode (CI / source checkout): live terminal paths may be recorded
  as SKIP with a reason; the overall verdict ignores SKIPped gates. The
  report says explicitly that skips were allowed.
- ``live`` mode (operator / rollout proof): every gate marked ``core`` must
  PASS. A core gate recorded as SKIP is treated as a blocking failure, so a
  live run can never report overall PASS while a core path (for example the
  d3 path or the current-host migration proof) was skipped.

Any FAIL fails the run in both modes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VALID_STATUSES = ("PASS", "FAIL", "SKIP")
VALID_MODES = ("smoke", "live")


def init_report(mode: str, stamp: str, host: str, artifact_dir: str) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode!r} (expected one of {VALID_MODES})")
    return {
        "generated_at": stamp,
        "mode": mode,
        "skips_allowed": mode == "smoke",
        "runner_host": host,
        "artifact_dir": artifact_dir,
        "gates": [],
        "overall": "INCOMPLETE",
    }


def record_gate(
    payload: dict[str, Any],
    *,
    name: str,
    status: str,
    core: bool,
    kind: str = "live",
    exit_code: int | None = None,
    target_host: str = "",
    transport: str = "",
    reason: str = "",
    log: str = "",
    excerpt: str = "",
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r} (expected one of {VALID_STATUSES})")
    gate: dict[str, Any] = {
        "name": name,
        "status": status,
        "core": core,
        "kind": kind,
    }
    if exit_code is not None:
        gate["exit_code"] = exit_code
    if target_host:
        gate["target_host"] = target_host
    if transport:
        gate["transport"] = transport
    if reason:
        gate["reason"] = reason
    if log:
        gate["log"] = log
    if excerpt:
        gate["excerpt"] = excerpt
    payload["gates"].append(gate)
    return payload


def finalize_report(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Compute the overall verdict. Returns (payload, exit_code)."""
    mode = payload.get("mode", "smoke")
    gates = payload.get("gates", [])
    failed = [g["name"] for g in gates if g.get("status") == "FAIL"]
    skipped = [
        {"name": g["name"], "reason": g.get("reason", "")}
        for g in gates
        if g.get("status") == "SKIP"
    ]
    blocking = list(failed)
    if mode == "live":
        # Fail closed: a skipped core path can never yield an overall PASS
        # in live/rollout mode.
        blocking += [
            g["name"]
            for g in gates
            if g.get("status") == "SKIP" and g.get("core", False)
        ]
    payload["failed"] = failed
    payload["skipped"] = skipped
    payload["blocking"] = blocking
    payload["overall"] = "PASS" if not blocking else "FAIL"
    if mode == "smoke" and skipped:
        payload["skip_note"] = (
            "SKIPped gates are live terminal paths not runnable in smoke mode; "
            "they are allowed only because this is an explicitly non-live run. "
            "Run scripts/clipboard-closeout.sh --live for rollout proof."
        )
    return payload, 0 if payload["overall"] == "PASS" else 1


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create a fresh closeout report")
    p_init.add_argument("--out", required=True, type=Path)
    p_init.add_argument("--mode", required=True, choices=VALID_MODES)
    p_init.add_argument("--stamp", required=True)
    p_init.add_argument("--host", default="")
    p_init.add_argument("--artifact-dir", default="")

    p_rec = sub.add_parser("record", help="append a gate result")
    p_rec.add_argument("--out", required=True, type=Path)
    p_rec.add_argument("--name", required=True)
    p_rec.add_argument("--status", required=True, choices=VALID_STATUSES)
    p_rec.add_argument("--core", action="store_true")
    p_rec.add_argument("--kind", default="live")
    p_rec.add_argument("--exit-code", type=int, default=None)
    p_rec.add_argument("--target-host", default="")
    p_rec.add_argument("--transport", default="")
    p_rec.add_argument("--reason", default="")
    p_rec.add_argument("--log", default="")
    p_rec.add_argument("--excerpt-file", default="", help="file whose tail is stored as the gate excerpt")

    p_fin = sub.add_parser("finalize", help="compute the overall verdict; exits 1 on FAIL")
    p_fin.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "init":
        _save(args.out, init_report(args.mode, args.stamp, args.host, args.artifact_dir))
        return 0

    if args.cmd == "record":
        excerpt = ""
        if args.excerpt_file:
            try:
                text = Path(args.excerpt_file).read_text(encoding="utf-8", errors="replace")
                excerpt = text[-2000:]
            except OSError:
                excerpt = ""
        payload = _load(args.out)
        record_gate(
            payload,
            name=args.name,
            status=args.status,
            core=args.core,
            kind=args.kind,
            exit_code=args.exit_code,
            target_host=args.target_host,
            transport=args.transport,
            reason=args.reason,
            log=args.log,
            excerpt=excerpt,
        )
        _save(args.out, payload)
        return 0

    if args.cmd == "finalize":
        payload, rc = finalize_report(_load(args.out))
        _save(args.out, payload)
        width = max((len(g["name"]) for g in payload["gates"]), default=4)
        for gate in payload["gates"]:
            line = f"{gate['name']:<{width}}  {gate['status']}"
            if gate.get("target_host"):
                line += f"  [{gate['target_host']}]"
            if gate.get("status") != "PASS" and gate.get("reason"):
                line += f"  ({gate['reason']})"
            print(line)
        print(f"overall: {payload['overall']} (mode={payload['mode']})")
        if payload["blocking"]:
            print("blocking: " + ", ".join(payload["blocking"]))
        return rc

    return 2


if __name__ == "__main__":
    sys.exit(main())
