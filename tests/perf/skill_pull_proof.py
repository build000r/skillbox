#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
for path in (ROOT_DIR, ENV_MANAGER_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime_manager.skill_pull import (  # noqa: E402
    SkillPullError,
    pull_host_skill,
    resolve_host_skills,
)
from tests.test_skill_pull_acceptance import AcceptanceFixture  # noqa: E402


MEASURED_RUNS = 20
FIXTURE_SKILLS = 500
P95_INDEX = 18
THRESHOLDS_NS = {
    "SLO-HOST-001": 250_000_000,
    "SLO-HOST-002": 2_000_000_000,
    "SLO-HOST-003": 5_000_000_000,
    "SLO-HOST-004": 1_000_000_000,
}


def fixture_error(message: str) -> SkillPullError:
    return SkillPullError("PERFORMANCE_FIXTURE_INVALID", message)


def guard_fixture(
    *,
    fixture_skill_count: int,
    measured_run_count: int,
    matched_result_count: int,
    results: list[dict[str, Any]],
    expected_shape: Callable[[dict[str, Any]], bool],
    exact_sbp_present: bool,
) -> None:
    if fixture_skill_count < FIXTURE_SKILLS:
        raise fixture_error("Performance fixture requires at least 500 valid skills.")
    if measured_run_count != MEASURED_RUNS:
        raise fixture_error("Performance proof requires exactly 20 measured runs.")
    if matched_result_count <= 0:
        raise fixture_error("Performance selector matched zero results.")
    if len(results) != measured_run_count or not all(expected_shape(row) for row in results):
        raise fixture_error("Performance result shape is invalid.")
    if not exact_sbp_present:
        raise fixture_error("Canonical requested sbp source is absent.")


def measure(action: Callable[[], dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    start = time.perf_counter_ns()
    result = action()
    return time.perf_counter_ns() - start, result


def percentile_95(durations_ns: list[int]) -> int:
    if len(durations_ns) != MEASURED_RUNS:
        raise fixture_error("p95 requires exactly 20 completed durations.")
    return sorted(durations_ns)[P95_INDEX]


def run_capabilities(host_env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [str(ROOT_DIR / "scripts" / "sbp"), "capabilities", "--json"],
        cwd=ROOT_DIR,
        env=host_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise fixture_error("Capabilities subprocess exited nonzero.")
    payload = json.loads(completed.stdout)
    payload["_exit_code"] = completed.returncode
    return payload


def cold_pull(host_env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(ROOT_DIR / "scripts" / "sbp"),
            "skill",
            "pull",
            "sbp",
            "--cwd",
            host_env["SKILLBOX_WORKSPACE_ROOT"],
            "--format",
            "json",
        ],
        cwd=ROOT_DIR,
        env=host_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise fixture_error("Cold pull subprocess exited nonzero.")
    payload = json.loads(completed.stdout)
    payload["_exit_code"] = completed.returncode
    return payload


def prove_fixture_model_admission(
    fixture: AcceptanceFixture,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = resolve_host_skills(fixture.model, cwd=fixture.repo)
    owned_names = fixture.constructed_skill_names
    admitted = [row for row in receipt["skills"] if row["admission"] == "admitted"]
    admitted_names = {str(row["name"]) for row in admitted}
    source_ownership_valid = all(
        row.get("logical_source_id") in {"host-fixture", "skills-private"}
        and (
            row.get("name") == "sbp"
            if row.get("logical_source_id") == "skills-private"
            else row.get("name") != "sbp"
        )
        for row in admitted
    )
    if (
        len(owned_names) != FIXTURE_SKILLS
        or receipt["totals"]["admitted_count"] != FIXTURE_SKILLS
        or admitted_names != owned_names
        or not source_ownership_valid
    ):
        raise fixture_error("Fixture model does not admit exactly 500 owned skill trees.")
    return receipt, {
        "candidate_count": receipt["totals"]["candidate_count"],
        "admitted_count": receipt["totals"]["admitted_count"],
        "omitted_count": receipt["totals"]["omitted_count"],
        "source_ownership_valid": source_ownership_valid,
        "only_sbp_is_skills_private": all(
            row["name"] == "sbp"
            for row in admitted
            if row["logical_source_id"] == "skills-private"
        ),
    }


def prove_cold_fixture_visibility(
    host_env: dict[str, str],
    expected_receipt: dict[str, Any],
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(ROOT_DIR / "scripts" / "sbp"),
            "skill",
            "resolve",
            "--cwd",
            host_env["SKILLBOX_WORKSPACE_ROOT"],
            "--format",
            "json",
        ],
        cwd=ROOT_DIR,
        env=host_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise fixture_error("Mounted cold fixture resolve exited nonzero.")
    payload = json.loads(completed.stdout)
    owned_names = set(host_env["WG003_FIXTURE_NAMES"].split(","))
    admitted_names = {
        str(row.get("name"))
        for row in payload.get("skills", [])
        if row.get("admission") == "admitted"
    }
    decisions = {str(row.get("name")): row for row in payload.get("skills", [])}
    totals = payload.get("totals", {})
    sbp_rows = [row for row in payload.get("skills", []) if row.get("name") == "sbp"]
    expected_rows = {
        str(row["name"]): row
        for row in expected_receipt["skills"]
        if row["admission"] == "admitted"
    }
    command_rows = {
        str(row["name"]): row
        for row in payload.get("skills", [])
        if row.get("admission") == "admitted"
    }
    digest_identity_valid = all(
        command_rows.get(name, {}).get("tree_sha256") == row.get("tree_sha256")
        and command_rows.get(name, {}).get("entry_sha256") == row.get("entry_sha256")
        for name, row in expected_rows.items()
    )
    if (
        len(owned_names) != FIXTURE_SKILLS
        or admitted_names != owned_names
        or totals.get("admitted_count") != FIXTURE_SKILLS
        or not digest_identity_valid
        or len(sbp_rows) != 1
        or sbp_rows[0].get("admission") != "admitted"
        or not sbp_rows[0].get("tree_sha256")
        or decisions.get("broken-link-skill", {}).get("reason_code") != "SOURCE_MISSING"
        or decisions.get("retired-debris", {}).get("reason_code") != "RETIRED"
    ):
        detail = {
            "owned": len(owned_names),
            "admitted": totals.get("admitted_count"),
            "candidate": totals.get("candidate_count"),
            "missing_owned": sorted(owned_names - admitted_names)[:8],
            "unexpected_admitted": sorted(admitted_names - owned_names)[:8],
            "digest_identity_valid": digest_identity_valid,
            "broken": decisions.get("broken-link-skill", {}).get("reason_code"),
            "retired": decisions.get("retired-debris", {}).get("reason_code"),
        }
        raise fixture_error(
            "500-skill fixture visibility mismatch: "
            + json.dumps(detail, sort_keys=True, separators=(",", ":"))
        )
    return {
        "command": "scripts/sbp skill resolve --cwd <registered-checkout> --format json",
        "visible_fixture_skill_count": len(owned_names),
        "exact_sbp_admitted": True,
        "broken_link_reason_code": decisions["broken-link-skill"]["reason_code"],
        "retired_debris_reason_code": decisions["retired-debris"]["reason_code"],
        "candidate_count": totals.get("candidate_count"),
        "admitted_count": totals.get("admitted_count"),
        "omitted_count": totals.get("omitted_count"),
        "operator_global_omitted": totals.get("omitted_count", 0) > 2,
        "digest_identity_valid": digest_identity_valid,
    }


def exact_sbp_source(receipt: dict[str, Any]) -> bool:
    rows = [row for row in receipt["skills"] if row["name"] == "sbp"]
    return (
        len(rows) == 1
        and rows[0]["admission"] == "admitted"
        and rows[0]["logical_source_id"] == "skills-private"
        and all(rows[0].get(key) for key in ("tree_sha256", "entry_sha256"))
    )


def target_record(
    slo: str,
    durations_ns: list[int],
    *,
    fixture_skill_count: int,
    matched_result_count: int,
) -> dict[str, Any]:
    p95_ns = percentile_95(durations_ns)
    threshold_ns = THRESHOLDS_NS[slo]
    if p95_ns > threshold_ns:
        raise AssertionError(f"{slo} p95 {p95_ns}ns exceeds {threshold_ns}ns")
    return {
        "slo": slo,
        "fixture_skill_count": fixture_skill_count,
        "measured_run_count": len(durations_ns),
        "matched_result_count": matched_result_count,
        "durations_ns_ascending": sorted(durations_ns),
        "p95_index": P95_INDEX,
        "p95_ns": p95_ns,
        "threshold_ns": threshold_ns,
        "passed": True,
    }


def prove_negative_guards() -> dict[str, Any]:
    failures: dict[str, Any] = {}
    cases = {
        "499_skill_fixture": dict(
            fixture_skill_count=499,
            measured_run_count=20,
            matched_result_count=1,
        ),
        "zero_match_selector": dict(
            fixture_skill_count=500,
            measured_run_count=20,
            matched_result_count=0,
        ),
    }
    for name, values in cases.items():
        try:
            guard_fixture(
                **values,
                results=[{"ok": True}] * 20,
                expected_shape=lambda row: row.get("ok") is True,
                exact_sbp_present=True,
            )
        except SkillPullError as exc:
            envelope = exc.envelope()
            if envelope["error_code"] != "PERFORMANCE_FIXTURE_INVALID":
                raise
            failures[name] = envelope
        else:
            raise AssertionError(f"negative guard unexpectedly passed: {name}")
    return failures


def run_proof() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        canonical_sbp = Path("/srv/skillbox/repos/skills-private/sbp")
        fixture = AcceptanceFixture(
            Path(tmpdir),
            skill_count=FIXTURE_SKILLS - 1,
            canonical_sbp=canonical_sbp,
            minimal_extra_skills=True,
        )
        local_smart = fixture.add_local_fixture_skill("smart")
        debris_counts = fixture.add_performance_debris()
        operator_global_opt_out = fixture.opt_out_unrelated_os_home_skills()
        model_path = Path(tmpdir) / "isolated-output" / "model.json"
        model_path.parent.mkdir()
        model_path.write_text(json.dumps(fixture.model), encoding="utf-8")
        managed_home = Path(tmpdir) / "managed-home"
        claude_skills = managed_home / ".claude" / "skills"
        codex_skills = managed_home / ".codex" / "skills"
        claude_skills.mkdir(parents=True)
        codex_skills.mkdir(parents=True)
        for index, skill in enumerate(
            sorted(
                [
                    path
                    for root in fixture.skill_roots
                    for path in root.iterdir()
                    if path.is_dir()
                ],
                key=lambda path: path.name,
            )
        ):
            target_root = claude_skills if index % 2 == 0 else codex_skills
            link = target_root / skill.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(skill)
        (codex_skills / "sbp").symlink_to(canonical_sbp)
        smart_link = claude_skills / "smart"
        if not smart_link.exists() and not smart_link.is_symlink():
            smart_link.symlink_to(local_smart)
        broken_link = claude_skills / "broken-link-skill"
        if not broken_link.exists() and not broken_link.is_symlink():
            broken_link.symlink_to(Path(tmpdir) / "missing-host-skill")
        host_env = {
            **os.environ,
            "SKILLBOX_HOME_ROOT": str(managed_home),
            "SKILLBOX_ROOT": str(ROOT_DIR),
            "SKILLBOX_CONFIG_ROOT": str(fixture.config),
            "SKILLBOX_CLIENTS_HOST_ROOT": str(fixture.config / "clients"),
            "SKILLBOX_STATE_ROOT": str(fixture.state_root),
            "SKILLBOX_WORKSPACE_ROOT": str(fixture.repo),
            "SKILLBOX_INVOKE_CWD": str(fixture.repo),
            "WG003_FIXTURE_NAMES": ",".join(sorted(fixture.constructed_skill_names)),
        }
        model_receipt, model_admission = prove_fixture_model_admission(fixture)
        cold_fixture_visibility = prove_cold_fixture_visibility(host_env, model_receipt)
        canonical_sbp_decision = next(
            row
            for row in model_receipt["skills"]
            if row["name"] == "sbp" and row["admission"] == "admitted"
        )
        expected_sbp_tree_sha256 = canonical_sbp_decision["tree_sha256"]
        expected_sbp_entry_sha256 = canonical_sbp_decision["entry_sha256"]

        targets: list[dict[str, Any]] = []

        cap_durations: list[int] = []
        cap_results: list[dict[str, Any]] = []
        for _ in range(MEASURED_RUNS):
            duration, result = measure(lambda: run_capabilities(host_env))
            cap_durations.append(duration)
            cap_results.append(result)
        def cap_shape(row: dict[str, Any]) -> bool:
            return (
                row.get("_exit_code") == 0
                and row.get("mode") == "host"
                and row.get("skill_verbs", {}).get("pull", {}).get("mutates") == "none"
                and row.get("skill_verbs", {}).get("pull", {}).get("returns_packet") is True
                and sum(
                    command.get("name") == "skill-pull"
                    for command in row.get("commands", [])
                )
                == 1
            )
        guard_fixture(
            fixture_skill_count=FIXTURE_SKILLS,
            measured_run_count=len(cap_durations),
            matched_result_count=sum(cap_shape(row) for row in cap_results),
            results=cap_results,
            expected_shape=cap_shape,
            exact_sbp_present=True,
        )
        targets.append(
            target_record(
                "SLO-HOST-001",
                cap_durations,
                fixture_skill_count=FIXTURE_SKILLS,
                matched_result_count=sum(cap_shape(row) for row in cap_results),
            )
        )

        resolve_host_skills(fixture.model, cwd=fixture.repo)
        resolve_durations: list[int] = []
        resolve_results: list[dict[str, Any]] = []
        for _ in range(MEASURED_RUNS):
            duration, result = measure(lambda: resolve_host_skills(fixture.model, cwd=fixture.repo))
            resolve_durations.append(duration)
            resolve_results.append(result)
        def resolve_shape(row: dict[str, Any]) -> bool:
            return (
                row.get("schema_version") == "skill-resolution-receipt/v1"
                and exact_sbp_source(row)
            )
        guard_fixture(
            fixture_skill_count=FIXTURE_SKILLS,
            measured_run_count=len(resolve_durations),
            matched_result_count=sum(resolve_shape(row) for row in resolve_results),
            results=resolve_results,
            expected_shape=resolve_shape,
            exact_sbp_present=all(exact_sbp_source(row) for row in resolve_results),
        )
        targets.append(
            target_record(
                "SLO-HOST-002",
                resolve_durations,
                fixture_skill_count=FIXTURE_SKILLS,
                matched_result_count=sum(resolve_shape(row) for row in resolve_results),
            )
        )

        cold_durations: list[int] = []
        cold_results: list[dict[str, Any]] = []
        for _ in range(MEASURED_RUNS):
            duration, result = measure(lambda: cold_pull(host_env))
            cold_durations.append(duration)
            cold_results.append(result)
        def pull_shape(row: dict[str, Any]) -> bool:
            return (
                row.get("_exit_code", 0) == 0
                and row.get("schema_version") == "skill-pull-result/v1"
                and row.get("name") == "sbp"
                and all(
                    row.get(key)
                    for key in ("tree_sha256", "entry_sha256", "receipt_sha256")
                )
                and row.get("tree_sha256") == expected_sbp_tree_sha256
                and row.get("entry_sha256") == expected_sbp_entry_sha256
            )
        cold_exact_sbp_matches = sum(pull_shape(row) for row in cold_results)
        guard_fixture(
            fixture_skill_count=FIXTURE_SKILLS,
            measured_run_count=len(cold_durations),
            matched_result_count=cold_exact_sbp_matches,
            results=cold_results,
            expected_shape=pull_shape,
            exact_sbp_present=cold_exact_sbp_matches == MEASURED_RUNS,
        )
        targets.append(
            target_record(
                "SLO-HOST-003",
                cold_durations,
                fixture_skill_count=FIXTURE_SKILLS,
                matched_result_count=cold_exact_sbp_matches,
            )
        )

        expected_entry = (fixture.sbp_source / "SKILL.md").read_text(encoding="utf-8")
        pull_host_skill(fixture.model, "sbp", cwd=fixture.repo)
        warm_durations: list[int] = []
        warm_results: list[dict[str, Any]] = []
        for _ in range(MEASURED_RUNS):
            duration, result = measure(lambda: pull_host_skill(fixture.model, "sbp", cwd=fixture.repo))
            warm_durations.append(duration)
            warm_results.append(result)
        def warm_shape(row: dict[str, Any]) -> bool:
            return pull_shape(row) and row.get("entry_text") == expected_entry
        guard_fixture(
            fixture_skill_count=FIXTURE_SKILLS,
            measured_run_count=len(warm_durations),
            matched_result_count=sum(warm_shape(row) for row in warm_results),
            results=warm_results,
            expected_shape=warm_shape,
            exact_sbp_present=True,
        )
        targets.append(
            target_record(
                "SLO-HOST-004",
                warm_durations,
                fixture_skill_count=FIXTURE_SKILLS,
                matched_result_count=sum(warm_shape(row) for row in warm_results),
            )
        )

        receipt = {
            "ok": True,
            "schema_version": "skill-pull-performance-proof/v1",
            "fixture_skill_count": FIXTURE_SKILLS,
            "declared_root_count": len(fixture.skill_roots),
            "debris_counts": debris_counts,
            "operator_global_opt_out_count": len(operator_global_opt_out),
            "cold_fixture_visibility": cold_fixture_visibility,
            "model_admission": model_admission,
            "measured_run_count_per_slo": MEASURED_RUNS,
            "p95_index": P95_INDEX,
            "targets": targets,
            "negative_guards": prove_negative_guards(),
        }
        (model_path.parent / "proof.json").write_text(
            json.dumps(receipt, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return receipt


def run_fixture_preflight() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        canonical_sbp = Path("/srv/skillbox/repos/skills-private/sbp")
        fixture = AcceptanceFixture(
            Path(tmpdir),
            skill_count=FIXTURE_SKILLS - 1,
            canonical_sbp=canonical_sbp,
            minimal_extra_skills=True,
        )
        local_smart = fixture.add_local_fixture_skill("smart")
        fixture.add_performance_debris()
        operator_global_opt_out = fixture.opt_out_unrelated_os_home_skills()
        managed_home = Path(tmpdir) / "managed-home"
        claude_skills = managed_home / ".claude" / "skills"
        codex_skills = managed_home / ".codex" / "skills"
        claude_skills.mkdir(parents=True)
        codex_skills.mkdir(parents=True)
        for index, skill in enumerate(
            sorted(
                [
                    path
                    for root in fixture.skill_roots
                    for path in root.iterdir()
                    if path.is_dir()
                ],
                key=lambda path: path.name,
            )
        ):
            target_root = claude_skills if index % 2 == 0 else codex_skills
            link = target_root / skill.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(skill)
        (codex_skills / "sbp").symlink_to(canonical_sbp)
        smart_link = claude_skills / "smart"
        if not smart_link.exists() and not smart_link.is_symlink():
            smart_link.symlink_to(local_smart)
        broken_link = claude_skills / "broken-link-skill"
        if not broken_link.exists() and not broken_link.is_symlink():
            broken_link.symlink_to(Path(tmpdir) / "missing-host-skill")
        host_env = {
            **os.environ,
            "SKILLBOX_HOME_ROOT": str(managed_home),
            "SKILLBOX_ROOT": str(ROOT_DIR),
            "SKILLBOX_CONFIG_ROOT": str(fixture.config),
            "SKILLBOX_CLIENTS_HOST_ROOT": str(fixture.config / "clients"),
            "SKILLBOX_STATE_ROOT": str(fixture.state_root),
            "SKILLBOX_WORKSPACE_ROOT": str(fixture.repo),
            "SKILLBOX_INVOKE_CWD": str(fixture.repo),
            "WG003_FIXTURE_NAMES": ",".join(sorted(fixture.constructed_skill_names)),
        }
        model_receipt, model_admission = prove_fixture_model_admission(fixture)
        return {
            "ok": True,
            "schema_version": "skill-pull-performance-preflight/v1",
            "operator_global_opt_out_count": len(operator_global_opt_out),
            "model_admission": model_admission,
            "visibility": prove_cold_fixture_visibility(host_env, model_receipt),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    try:
        payload = run_fixture_preflight() if args.preflight_only else run_proof()
    except SkillPullError as exc:
        payload = exc.envelope()
        print(json.dumps(payload, sort_keys=True))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
