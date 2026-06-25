from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import validation as VALIDATION
from .errors import (
    OVERRIDE_REFUSED_FLOOR,
    OVERRIDE_REFUSED_GLOBAL_ESCALATION,
    OVERRIDE_SKILL_UNKNOWN,
    PRUNE_SKIPPED_PINNED,
    ValidationError,
)
from .shared import *
from .validation import *
from .publish import *
from .runtime_ops import *
from .skill_visibility import *
from .mmdx_open import *
from .operator_booking import *
from .context_rendering import *
from .text_renderers import *
from .workflows import *
from .mcp_visibility import *
from .mcp_render import (
    print_mcp_render_text,
    render_mcp_sync,
)
from .parity_report import *
from .pressure_report import *
from .rch_report import *
from .rch_adapter import *
from .sbh_report import *
from .evidence import *
from .forge import *
from .swimmers_launch import launch_swimmers_batch, swimmers_launch_text_lines
from .structure_doctor import run_structure_doctor, structure_doctor_text_lines
from .command_registry import registry_payload
from .port_registry import port_registry_payload, port_registry_text_lines
from .agent_adapters import collect_agent_adapter_evidence
from .agent_graph import build_agent_graph, build_agent_graph_payload
from .agent_graph_engine import GRAPH_ALGORITHMS, GRAPH_OUTPUT_FORMATS, graph_command_payload, render_graph_payload
from .agent_decisions import BRAIN_COMMAND_TARGET_ALIASES, explain_payload, next_action_payload
from .agent_errors import brain_error_payload
from .agent_search import search_payload
from .agent_timing import attach_elapsed, timer_start
from .agent_snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    create_snapshot_payload,
    diff_snapshots,
    load_snapshot,
    replay_snapshot,
    save_snapshot,
)
from .fleet_converge import build_fleet_converge_plan, fleet_converge_text_lines
from .fleet_relink import (
    apply_relink_plan,
    build_relink_plan,
    relink_text_lines,
)


class DistributionPreviewError(RuntimeError):
    pass


class DistributionRollbackError(RuntimeError):
    pass


MANAGE_COMMAND_NAMES = {
    "acceptance",
    "bootstrap",
    "capabilities",
    "client-diff",
    "client-init",
    "client-open",
    "client-project",
    "client-publish",
    "context",
    "distribution-preview",
    "distribution-publish",
    "distribution-rollback",
    "doctor",
    "down",
    "explain",
    "first-box",
    "focus",
    "forge",
    "graph",
    "logs",
    "mcp",
    "mcp-audit",
    "mmdx",
    "next",
    "onboard",
    "operator-booking",
    "overlay",
    "parity-report",
    "ports",
    "private-init",
    "pressure-report",
    "rch-stage",
    "rch-report",
    "sbh-report",
    "render",
    "restart",
    "robot-docs",
    "robot-triage",
    "search",
    "session-end",
    "session-event",
    "session-resume",
    "session-start",
    "session-status",
    "skill",
    "skill-audit",
    "skills",
    "snap",
    "status",
    "stewardship-report",
    "structure-doctor",
    "swimmers-launch",
    "sync",
    "up",
    "worker-artifacts",
    "worker-promote-learning",
    "worker-status",
    "worker-submit",
}
JSON_FLAG_ALIASES = {
    "--json": "--format json",
    "--jason": "--format json",
    "--jsno": "--format json",
    "--jsson": "--format json",
}


class SkillboxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        command_hint = _command_suggestion_from_error(message)
        hint_lines = [
            "",
            "Agent hint: run `manage.py capabilities --json` for the machine-readable command contract.",
        ]
        if command_hint:
            hint_lines.append(f"Did you mean: `manage.py {command_hint}`?")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n" + "\n".join(hint_lines) + "\n")


def _command_suggestion_from_error(message: str) -> str:
    marker = "invalid choice: "
    if marker not in message:
        return ""
    remainder = message.split(marker, 1)[1]
    raw = remainder.split("(", 1)[0].strip().strip("'\"")
    matches = difflib.get_close_matches(raw, sorted(MANAGE_COMMAND_NAMES), n=1, cutoff=0.68)
    return matches[0] if matches else ""


def _normalize_agent_argv(argv: list[str] | None) -> tuple[list[str], list[str]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    normalized: list[str] = []
    diagnostics: list[str] = []
    command_seen = False
    pending_json = False

    for token in raw:
        if token == "--robot-help":
            normalized.extend(["robot-docs", "guide"])
            command_seen = True
            # robot-docs accepts --format; flush a JSON alias that arrived
            # before it so we don't later inject a spurious `status` command.
            if pending_json:
                normalized.extend(["--format", "json"])
                pending_json = False
            continue
        if token in JSON_FLAG_ALIASES:
            if token != "--json":
                diagnostics.append(
                    f"Interpreting {token} as --format json. Exact command: manage.py <command> --format json"
                )
            if command_seen:
                normalized.extend(["--format", "json"])
            else:
                pending_json = True
            continue
        normalized.append(token)
        if not command_seen and token in MANAGE_COMMAND_NAMES:
            command_seen = True
            if pending_json:
                normalized.extend(["--format", "json"])
                pending_json = False

    if pending_json:
        normalized.extend(["status", "--format", "json"])
    return normalized, diagnostics


def publish_skill_release(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.publish import publish_skill_release as publish
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-publish requires the optional 'cryptography' package"
            ) from exc
        raise

    return publish(**kwargs)


def preview_manifest(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.preview import preview_manifest as preview
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-preview requires the optional 'cryptography' package"
            ) from exc
        raise

    return preview(**kwargs)


def cached_versions(**kwargs: Any) -> list[int]:
    state_root = Path(kwargs["state_root"])
    skill_name = str(kwargs["skill_name"])
    cache_dir = state_root / "bundle-cache" / skill_name
    if not cache_dir.is_dir():
        return []
    prefix = f"{skill_name}-v"
    suffix = ".skillbundle.tar.gz"
    versions: list[int] = []
    for path in cache_dir.iterdir():
        name = path.name
        if not path.is_file() or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        try:
            versions.append(int(name[len(prefix):-len(suffix)]))
        except ValueError:
            continue
    return sorted(set(versions))


def rollback_distributor_skill(**kwargs: Any) -> dict[str, Any]:
    try:
        from .distribution.rollback import rollback_distributor_skill as rollback
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("cryptography"):
            raise RuntimeError(
                "distribution-rollback requires the optional 'cryptography' package"
            ) from exc
        raise

    return rollback(**kwargs)


def _add_profile_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="Activate a runtime profile. Can be repeated. Selecting any profile also includes `core`.",
    )


def _add_client_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--client",
        action="append",
        default=[],
        help="Activate a runtime client overlay. Can be repeated.",
    )


def _add_cwd_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to infer the active client overlay when --client is omitted.",
    )


def _add_service_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="Limit the command to one or more declared service ids. Can be repeated.",
    )


def _add_task_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Limit the command to one or more declared task ids. Can be repeated.",
    )


def _add_context_dir_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--context-dir",
        default=None,
        help=(
            "Write CLAUDE.md and AGENTS.md into this directory instead of the mounted "
            "home/.claude and home/.codex roots. Path is resolved relative to the repo root."
        ),
    )


def _add_skill_lifecycle_common(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("--format", choices=("text", "json"), default="text")
    command_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to infer client overlays, repo roots, and project categories.",
    )
    command_parser.add_argument(
        "--to",
        choices=("auto", "global", "project", "category"),
        default="auto",
        help="Where to install/link the skill. auto uses skill-scope policy.",
    )
    command_parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Project category to target. Can be repeated.",
    )
    command_parser.add_argument(
        "--source",
        default=None,
        help="Explicit skill directory or parent source directory.",
    )
    command_parser.add_argument("--dry-run", action="store_true")
    command_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing non-symlink files and override global policy blocks.",
    )
    command_parser.add_argument(
        "--allow-directories",
        action="store_true",
        help="Allow remove/prune/move to delete real skill directories, not just symlinks/files.",
    )
    command_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm remove/prune/move actions that unlink existing installs.",
    )
    _add_profile_arg(command_parser)
    _add_client_arg(command_parser)


def _add_skill_from_scope_arg(command_parser: argparse.ArgumentParser, *, help_text: str) -> None:
    command_parser.add_argument(
        "--from",
        dest="from_scope",
        choices=("global", "project", "all"),
        default="all",
        help=help_text,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = SkillboxArgumentParser(description="Manage the internal skillbox runtime graph.")
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Override the repo root for testing or embedding.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include the traceback for unexpected (INTERNAL) errors. Off by default so tracebacks never leak.",
    )
    parser.add_argument(
        "--robot-triage",
        action="store_true",
        help="Emit a compact JSON triage packet for agents and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")

    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Print the machine-readable Skillbox CLI contract.",
    )
    capabilities_parser.add_argument("--format", choices=("json",), default="json")
    capabilities_parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact registry entries while preserving the top-level capabilities contract.",
    )
    capabilities_parser.add_argument(
        "--no-adapters",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    robot_docs_parser = subparsers.add_parser(
        "robot-docs",
        help="Print agent-oriented in-tool documentation.",
    )
    robot_docs_parser.add_argument("topic", nargs="?", default="guide", choices=("guide",))
    robot_docs_parser.add_argument("--format", choices=("text", "json"), default="text")

    robot_triage_parser = subparsers.add_parser(
        "robot-triage",
        help="Emit a compact JSON triage packet for agents.",
    )
    robot_triage_parser.add_argument("--format", choices=("json",), default="json")

    render_parser = subparsers.add_parser("render", help="Print the resolved runtime graph.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(render_parser)
    _add_client_arg(render_parser)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Create managed runtime directories, repos, artifacts, and installed skill state.",
    )
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(sync_parser)
    _add_client_arg(sync_parser)
    _add_context_dir_arg(sync_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime graph, filesystem readiness, and installed skill integrity.",
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(doctor_parser)
    _add_client_arg(doctor_parser)

    structure_doctor_parser = subparsers.add_parser(
        "structure-doctor",
        help=(
            "One front door for STRUCTURAL gates (INCO/FAIL/PASS). Complements "
            "(does not replace) the runtime `make doctor`: runs the structure "
            "invariant suite, policy + global-skill-contract lints, lock parity, "
            "MCP parity, skill drift, and — when reachable — the runtime `make "
            "doctor` as a RUNTIME gate. Exits nonzero on FAIL only; INCO/PASS "
            "exit 0. Structure gates complete in <60s (per-gate caps; a gate over "
            "its cap is INCO, not FAIL). Surfaced as `sbp doctor`."
        ),
    )
    structure_doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    structure_doctor_parser.add_argument(
        "--cwd",
        default=None,
        help="Directory to evaluate cwd-scoped gates (skill/MCP drift) against. Defaults to $PWD.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Summarize repo, artifact, skill, service, log, and check state.",
    )
    status_parser.add_argument("--format", choices=("text", "json"), default="text")
    status_parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for agent inspection instead of the full raw status payload.",
    )
    _add_profile_arg(status_parser)
    _add_client_arg(status_parser)
    _add_cwd_arg(status_parser)

    ports_parser = subparsers.add_parser(
        "ports",
        help=(
            "List the machine-readable port registry for the active scope: every "
            "declared port mapped to its owning service/ingress/env_surface, with "
            "source (file+key), profiles, client, and bind scope. Health targets "
            "with no parseable port emit a warning entry and are never guessed. "
            "Read-only. Use --resolve <service-id> to resolve one owner's port(s)."
        ),
    )
    ports_parser.add_argument("--format", choices=("text", "json"), default="json")
    ports_parser.add_argument(
        "--resolve",
        default=None,
        help="Resolve the declared port(s) for a single service/owner id.",
    )
    _add_profile_arg(ports_parser)
    _add_client_arg(ports_parser)
    _add_cwd_arg(ports_parser)

    pressure_report_parser = subparsers.add_parser(
        "pressure-report",
        help="Report local disk pressure, protected buckets, and build-offload/storage guard posture.",
    )
    pressure_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    pressure_report_parser.add_argument(
        "--home",
        default=None,
        help="Home directory to inspect. Defaults to the current user's home.",
    )
    pressure_report_parser.add_argument(
        "--target-box",
        default=DEFAULT_TARGET_BOX,
        help="Non-production box inventory id to report against.",
    )
    pressure_report_parser.add_argument(
        "--scan-candidate-sizes",
        action="store_true",
        help="Run read-only du probes for review-only cleanup candidates.",
    )

    rch_report_parser = subparsers.add_parser(
        "rch-report",
        help="Report RCH build-offload readiness without installing hooks or mutating config.",
    )
    rch_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    rch_report_parser.add_argument(
        "--binary",
        default=None,
        help="Explicit rch binary path. Defaults to SKILLBOX_RCH_BIN then PATH lookup.",
    )
    rch_report_parser.add_argument(
        "--target-box",
        default=DEFAULT_TARGET_BOX,
        help="Approved non-production worker target to name in policy output.",
    )
    rch_report_parser.add_argument("--timeout", type=float, default=5.0)
    rch_report_parser.add_argument(
        "--no-probes",
        action="store_true",
        help="Do not run rch; only report configured policy and binary presence.",
    )

    rch_stage_parser = subparsers.add_parser(
        "rch-stage",
        help="Prepare or run a no-sudo RCH staging lane with ssh/rsync path translation.",
    )
    rch_stage_parser.add_argument("--format", choices=("text", "json"), default="text")
    rch_stage_parser.add_argument(
        "--source",
        default=None,
        help="Repo/project directory to stage. Defaults to the current working directory.",
    )
    rch_stage_parser.add_argument(
        "--stage-root",
        default=None,
        help="Local adapter state root. Defaults to .skillbox-state/rch-adapter.",
    )
    rch_stage_parser.add_argument(
        "--stage-id",
        default=None,
        help="Stable stage id for repeatable tests or operator-managed staging.",
    )
    rch_stage_parser.add_argument(
        "--target-box",
        default=DEFAULT_TARGET_BOX,
        help="Approved non-production worker target named in manifests.",
    )
    rch_stage_parser.add_argument(
        "--remote-root",
        default=DEFAULT_ADAPTER_REMOTE_ROOT,
        help="Writable remote adapter root. Defaults to /srv/skillbox/rch-adapter.",
    )
    rch_stage_parser.add_argument(
        "--rch-binary",
        default=None,
        help="RCH binary to execute when --run is set. Defaults to PATH lookup.",
    )
    rch_stage_parser.add_argument(
        "--real-ssh",
        default=None,
        help="Underlying ssh command for the adapter wrapper. Defaults to Skillbox managed ssh if present.",
    )
    rch_stage_parser.add_argument(
        "--real-rsync",
        default=None,
        help="Underlying rsync command for the adapter wrapper. Defaults to /usr/bin/rsync or PATH.",
    )
    rch_stage_parser.add_argument(
        "--rch-home",
        default=None,
        help="HOME to use for RCH config when --run is set.",
    )
    rch_stage_parser.add_argument(
        "--xdg-state-home",
        default=None,
        help="Short XDG_STATE_HOME for RCH/OpenSSH control sockets when --run is set.",
    )
    rch_stage_parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create the local staging tree, wrappers, source mirror, and manifest without running RCH.",
    )
    rch_stage_parser.add_argument(
        "--run",
        action="store_true",
        help="Prepare, then run `rch exec -- <command>` from the staged project.",
    )
    rch_stage_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only. This is the default when --prepare/--run are omitted.",
    )
    rch_stage_parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Do not mirror source files while preparing. Wrappers and manifest may still be written.",
    )
    rch_stage_parser.add_argument("--timeout", type=float, default=1800.0)
    rch_stage_parser.add_argument("stage_command", nargs=argparse.REMAINDER)

    sbh_report_parser = subparsers.add_parser(
        "sbh-report",
        help="Report SBH storage guard readiness without installing services, cleaning, or mutating ballast.",
    )
    sbh_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    sbh_report_parser.add_argument(
        "--home",
        default=None,
        help="Home directory to inspect. Defaults to the current user's home.",
    )
    sbh_report_parser.add_argument(
        "--binary",
        default=None,
        help="Explicit sbh binary path. Defaults to SKILLBOX_SBH_BIN then PATH lookup.",
    )
    sbh_report_parser.add_argument(
        "--decision-id",
        default=None,
        help="Optional SBH decision id to explain with a read-only `sbh explain` probe.",
    )
    sbh_report_parser.add_argument("--timeout", type=float, default=5.0)
    sbh_report_parser.add_argument(
        "--no-probes",
        action="store_true",
        help="Do not run sbh; only report configured policy and binary presence.",
    )

    skills_parser = subparsers.add_parser(
        "skills",
        help="Show effective skill availability across global, client, and project layers.",
    )
    skills_parser.add_argument("--format", choices=("text", "json"), default="text")
    skills_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for pwd-match and project-local skill discovery. Defaults to the current cwd.",
    )
    skills_parser.add_argument(
        "--full",
        action="store_true",
        help="Show every effective skill in text output and full raw JSON instead of compact JSON.",
    )
    skills_parser.add_argument(
        "--show-shadowed",
        action="store_true",
        help="Show lower-precedence skill declarations hidden by the effective layer.",
    )
    skills_parser.add_argument(
        "--show-sources",
        action="store_true",
        help="Scan configured source roots for skills that are not currently synced. This can be noisy.",
    )
    skills_parser.add_argument(
        "--issues-only",
        action="store_true",
        help="Show compact policy issues and recommendations instead of the effective skill list.",
    )
    skills_parser.add_argument(
        "--limit",
        type=int,
        default=80,
        help="Maximum effective skills to show in text output unless --full is set.",
    )
    skills_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Do not inspect ~/.claude/skills or ~/.codex/skills.",
    )
    skills_parser.add_argument(
        "--no-project",
        action="store_true",
        help="Do not inspect project-local .claude/.codex skill directories near --cwd.",
    )
    _add_profile_arg(skills_parser)
    _add_client_arg(skills_parser)

    skill_audit_parser = subparsers.add_parser(
        "skill-audit",
        help="Audit skill scope policy across configured downstream repos.",
    )
    skill_audit_parser.add_argument("--format", choices=("text", "json"), default="text")
    skill_audit_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for the one-time global drift summary. Defaults to the current cwd.",
    )
    skill_audit_parser.add_argument(
        "--scan-root",
        action="append",
        default=None,
        help="Root to scan for git repos. Can be repeated. Defaults to skill_install_scan_roots from skill-scope.yaml.",
    )
    skill_audit_parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum directory depth under each scan root when finding git repos.",
    )
    skill_audit_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum repo rows and names to show in text output.",
    )
    skill_audit_parser.add_argument(
        "--all",
        action="store_true",
        help="Report clean repos as well as repos with skill policy issues.",
    )
    skill_audit_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Skip the one-time global skill drift summary.",
    )
    _add_profile_arg(skill_audit_parser)
    _add_client_arg(skill_audit_parser)

    mcp_audit_parser = subparsers.add_parser(
        "mcp-audit",
        help=(
            "Audit Claude JSON and Codex TOML MCP config parity for a repo. Servers "
            "declared as kind:mcp services in workspace/runtime.yaml (any profile) are "
            "treated as intentional even when single-surface or profile-gated; only "
            "undeclared servers count as unexplained_drift."
        ),
    )
    mcp_audit_parser.add_argument("--format", choices=("text", "json"), default="text")
    mcp_audit_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to find the target repo root. Defaults to the current cwd.",
    )
    mcp_audit_parser.add_argument(
        "--config-root",
        default=None,
        help="Explicit repo/config root containing .mcp.json and .codex/config.toml.",
    )
    _add_profile_arg(mcp_audit_parser)
    _add_client_arg(mcp_audit_parser)

    mcp_parser = subparsers.add_parser(
        "mcp",
        help=(
            "Single-source MCP config. `mcp sync` renders BOTH .mcp.json (Claude) "
            "and .codex/config.toml (Codex) from the same declaration `mcp-audit` "
            "checks against. (`sbp mcp` with no subcommand runs the read-only audit.)"
        ),
    )
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_action", required=True)

    mcp_sync_parser = mcp_subparsers.add_parser(
        "sync",
        help=(
            "Render .mcp.json + .codex/config.toml from the single MCP declaration. "
            "Operator-managed and unmanaged entries are preserved; --dry-run prints "
            "exactly what --apply would write."
        ),
        description=(
            "Render Claude (.mcp.json) and Codex (.codex/config.toml) MCP config "
            "from the one declaration that `mcp-audit` audits against, so audit and "
            "render agree. Output paths and the Codex `cwd` resolve through machine "
            "profiles (skillbox-config/machines.yaml) so a devbox TOML never gets a "
            "foreign /Users/operator path. Entries marked operator_managed in the "
            "declaration, and any entry present on a surface but not declared, are "
            "PRESERVED (review-before-remove). The user-global ~/.codex/config.toml "
            "is operator-managed and is NEVER rewritten by this command. --dry-run "
            "(default) is symmetric with --apply: it prints the exact rendered text "
            "and diff that --apply writes."
        ),
    )
    mcp_sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    mcp_sync_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to find the target repo root. Defaults to the current cwd.",
    )
    mcp_sync_parser.add_argument(
        "--config-root",
        default=None,
        help="Explicit repo/config root containing .mcp.json and .codex/config.toml.",
    )
    mcp_sync_apply_group = mcp_sync_parser.add_mutually_exclusive_group()
    mcp_sync_apply_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Preview the exact rendered files and diff without writing (default).",
    )
    mcp_sync_apply_group.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        default=False,
        help="Write the rendered .mcp.json and .codex/config.toml to disk.",
    )
    _add_profile_arg(mcp_sync_parser)
    _add_client_arg(mcp_sync_parser)

    fleet_parser = subparsers.add_parser(
        "fleet",
        help=(
            "Fleet-wide skill/MCP convergence. `fleet converge --dry-run` emits a "
            "per-repo heal PLAN (relink/prune/sync/policy/mcp), every action carrying "
            "its exact single-repo command. PLAN ONLY — never writes."
        ),
    )
    fleet_subparsers = fleet_parser.add_subparsers(dest="fleet_action", required=True)

    fleet_converge_parser = fleet_subparsers.add_parser(
        "converge",
        help=(
            "Per-repo heal plan over the deduped canonical fleet, grouped by triage "
            "class (relink/prune/sync/policy/mcp). PLAN ONLY: --dry-run is the only "
            "mode; nothing is written."
        ),
        description=(
            "Walk the deduped canonical repo list (the same candidate set the skill "
            "audit scans, deduped by realpath) and emit ONE diffable document: every "
            "repo's skill/MCP drift grouped into five triage classes — relink "
            "(repoint/migrate broken links), prune (remove dead/unreadable links), "
            "sync (link cwd-expected missing skills), policy (scope violations, with "
            "the rule id), and mcp (Claude/Codex parity). Every action carries its "
            "EXACT single-repo command so an agent can apply per-repo or in bulk. "
            "Output is stable/diffable (deterministically sorted) as a human table or "
            "--format json. PLAN ONLY: --dry-run is the default and only mode; this "
            "command NEVER writes, links, prunes, or migrates anything."
        ),
    )
    fleet_converge_parser.add_argument("--format", choices=("text", "json"), default="text")
    fleet_converge_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for fleet anchoring/global summary. Defaults to the current cwd.",
    )
    fleet_converge_parser.add_argument(
        "--scan-root",
        action="append",
        default=None,
        help="Root to scan for git repos. Can be repeated. Defaults to skill_install_scan_roots from skill-scope.yaml.",
    )
    fleet_converge_parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum directory depth under each scan root when finding git repos.",
    )
    fleet_converge_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum repo sections to show in text output (0 = unlimited).",
    )
    fleet_converge_parser.add_argument(
        "--all",
        action="store_true",
        help="Include clean (converged) repos in the plan as well.",
    )
    fleet_converge_parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip the per-repo Claude/Codex MCP parity pass.",
    )
    fleet_converge_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Plan only (default and only mode). This command never writes.",
    )
    _add_profile_arg(fleet_converge_parser)
    _add_client_arg(fleet_converge_parser)

    fleet_relink_parser = fleet_subparsers.add_parser(
        "relink",
        help=(
            "Machine-migration bulk rewrite: repoint other-machine skill links onto "
            "this box's tree (old-root -> new-root). --dry-run is the DEFAULT; --yes "
            "applies. Only rewrites a link when the translated target EXISTS and is a "
            "valid skill dir; otherwise leaves it for converge. NEVER touches healthy links."
        ),
        description=(
            "First-class the single biggest drift generator: a machine move. For every "
            "other-machine installed link in the fleet, translate its target from the "
            "source root onto the destination root via machines.yaml. If the translated "
            "target EXISTS and is a valid skill dir, repoint the link in place "
            "(ln -sfn). Otherwise reclassify it (moved/dangling) and LEAVE it for "
            "`fleet converge` — relink never prunes and never guesses. Roots default "
            "from machines.yaml: --to-root is this machine's canonical repo root and "
            "--from-root is every other machine's repo roots, so `fleet relink` with no "
            "roots means 'relink everything foreign to this box back onto it'. --dry-run "
            "is the DEFAULT and is symmetric with apply (same plan); --yes is required "
            "to write. --cwd scopes to one repo. Healthy links are never candidates."
        ),
    )
    fleet_relink_parser.add_argument("--format", choices=("text", "json"), default="text")
    fleet_relink_parser.add_argument(
        "--from-root",
        dest="from_root",
        default=None,
        help="Source root to rewrite FROM (e.g. /Users/operator/repos). Defaults to every other machine's repo roots from machines.yaml.",
    )
    fleet_relink_parser.add_argument(
        "--to-root",
        dest="to_root",
        default=None,
        help="Destination root to rewrite TO (e.g. /srv/skillbox/repos). Defaults to this machine's canonical repo root from machines.yaml.",
    )
    fleet_relink_parser.add_argument(
        "--cwd",
        default=None,
        help="Scope the relink to a single repo dir. Defaults to the whole deduped canonical fleet.",
    )
    fleet_relink_parser.add_argument(
        "--scan-root",
        action="append",
        default=None,
        help="Root to scan for git repos. Can be repeated. Defaults to skill_install_scan_roots from skill-scope.yaml.",
    )
    fleet_relink_parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Maximum directory depth under each scan root when finding git repos.",
    )
    fleet_relink_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum repo sections to show in text output (0 = unlimited).",
    )
    fleet_relink_parser.add_argument(
        "--all",
        action="store_true",
        help="Include repos with no relink actions in the plan as well.",
    )
    fleet_relink_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Plan only (the DEFAULT). Symmetric with apply: prints exactly what --yes would write. No-op affordance; relink is dry-run unless --yes is passed.",
    )
    fleet_relink_parser.add_argument(
        "--yes",
        dest="apply",
        action="store_true",
        default=False,
        help="Apply the rewrite actions (repoint the links). Without --yes this is a dry-run.",
    )
    _add_profile_arg(fleet_relink_parser)
    _add_client_arg(fleet_relink_parser)

    evidence_parser = subparsers.add_parser(
        "evidence",
        help=(
            "Read-only runtime evidence packet: doctor, status, pressure, pulse, "
            "skills, MCP parity, git dirty, and a Beads pointer in one machine-readable "
            "payload with explicit blocked/gray conditions. Never mutates runtime state."
        ),
    )
    evidence_parser.add_argument("--format", choices=("text", "json", "md"), default="json")
    evidence_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used to scope skills/MCP surfaces. Defaults to the current cwd.",
    )
    evidence_parser.add_argument(
        "--write",
        action="store_true",
        help="Also write the packet to tests/artifacts/perf/<run-id>/runtime-evidence/.",
    )
    evidence_parser.add_argument(
        "--run-id",
        default=None,
        help="Override the run-id directory name used with --write.",
    )
    _add_profile_arg(evidence_parser)
    _add_client_arg(evidence_parser)

    cass_evidence_parser = subparsers.add_parser(
        "cass-evidence",
        help=(
            "Measure skill INVOCATIONS per repo from the Cass index using "
            "contamination-safe structural detection (Skill-tool records, "
            "first-progress-markers, /slash invocations - never raw doc-echo "
            "mentions), joined against the current skill-visibility policy. "
            "Surfaced as `sbp evidence`. Degrades INCO-style when Cass is "
            "unreachable and never blocks recalibrate."
        ),
    )
    cass_evidence_group = cass_evidence_parser.add_mutually_exclusive_group()
    cass_evidence_group.add_argument(
        "--repo",
        default=None,
        help="Restrict to one repo path (its transcript cwd is mapped to a repo slug).",
    )
    cass_evidence_group.add_argument(
        "--skill",
        default=None,
        help="Restrict to one skill name.",
    )
    cass_evidence_parser.add_argument("--format", choices=("text", "json"), default="json")
    cass_evidence_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max Cass hits per locator query to scan for invocations.",
    )
    cass_evidence_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Per-query Cass front-door timeout in seconds.",
    )
    cass_evidence_parser.add_argument(
        "--proposals",
        action="store_true",
        help=(
            "PROPOSALS mode: emit demotion (linked >90d AND zero structural "
            "invocations) and promotion (used-but-invisible, or repeatedly "
            "activated on-demand in one repo) candidates, each carrying the exact "
            "skill-scope.yaml policy edit it implies. PROPOSALS ONLY - never "
            "auto-applied (sbp read-only-first)."
        ),
    )

    next_parser = subparsers.add_parser(
        "next",
        help="Rank explainable next actions from runtime evidence, graph facts, Beads/BV, SBP, and NTM state.",
    )
    next_parser.add_argument("--format", choices=("text", "json"), default="json")
    next_parser.add_argument("--limit", type=int, default=5)
    next_parser.add_argument("--cwd", default=None, help="Working directory used for evidence scoping.")
    next_parser.add_argument("--ntm-session", default=None, help="Optional NTM session id for load-state evidence.")
    next_parser.add_argument("--no-adapters", action="store_true", help="Skip optional br/bv/sbp/ntm adapters.")
    _add_profile_arg(next_parser)
    _add_client_arg(next_parser)

    graph_parser = subparsers.add_parser(
        "graph",
        help="Inspect the agent operations graph and optional graph algorithms.",
    )
    graph_parser.add_argument("--format", choices=tuple(sorted(GRAPH_OUTPUT_FORMATS)), default="json")
    graph_parser.add_argument("--algorithm", default=None)
    graph_parser.add_argument("--node", default=None, help="Node id for node-scoped graph algorithms.")
    graph_parser.add_argument("--source", default=None, help="Source node id for shortest-path.")
    graph_parser.add_argument("--target", default=None, help="Target node id for shortest-path.")
    graph_parser.add_argument(
        "--blocked-node",
        action="append",
        default=[],
        help="Blocked node id for min-unblock analysis. Can be repeated.",
    )
    graph_parser.add_argument("--cwd", default=None, help="Working directory used for evidence scoping.")
    graph_parser.add_argument("--ntm-session", default=None, help="Optional NTM session id for load-state evidence.")
    graph_parser.add_argument("--no-adapters", action="store_true", help="Skip optional br/bv/sbp/ntm adapters.")
    _add_profile_arg(graph_parser)
    _add_client_arg(graph_parser)

    explain_parser = subparsers.add_parser(
        "explain",
        help=(
            "Explain skill visibility provenance for a skill, OR a graph node / "
            "registered command. A bare slug (e.g. `explain wiki`) is treated as a "
            "skill: is it visible at --cwd, via which layer and scope rule, which "
            "occurrence lost and why, and — when invisible — the ranked, exact "
            "commands to make it visible. A brain node/command id (e.g. "
            "`explain brain.next`) or `--node` routes to the graph/registry explainer."
        ),
    )
    explain_parser.add_argument(
        "target",
        help="Skill name (default), or a graph node / registry command id.",
    )
    explain_parser.add_argument("--format", choices=("text", "json"), default="json")
    explain_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for skill provenance and brain evidence scoping.",
    )
    explain_parser.add_argument(
        "--skill",
        dest="explain_skill",
        action="store_true",
        help="Force skill-visibility provenance even if target also names a graph node.",
    )
    explain_parser.add_argument(
        "--node",
        dest="explain_node",
        action="store_true",
        help="Force the graph/registry explainer (the legacy brain explain) for target.",
    )
    explain_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Skill mode: do not inspect ~/.claude/skills or ~/.codex/skills.",
    )
    explain_parser.add_argument(
        "--no-project",
        action="store_true",
        help="Skill mode: do not inspect project-local .claude/.codex skill dirs near --cwd.",
    )
    explain_parser.add_argument("--ntm-session", default=None, help="Optional NTM session id for load-state evidence.")
    explain_parser.add_argument("--no-adapters", action="store_true", help="Skip optional br/bv/sbp/ntm adapters.")
    _add_profile_arg(explain_parser)
    _add_client_arg(explain_parser)

    search_parser = subparsers.add_parser(
        "search",
        help="Search registry commands, graph nodes, docs, Beads, and evidence with grouped JSON hits.",
    )
    search_parser.add_argument("query", nargs="*", help="Search terms.")
    search_parser.add_argument("--format", choices=("text", "json"), default="json")
    search_parser.add_argument("--source", dest="source_filter", action="append", default=[])
    search_parser.add_argument("--kind", dest="kind_filter", action="append", default=[])
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--cwd", default=None, help="Working directory used for evidence scoping.")
    search_parser.add_argument("--ntm-session", default=None, help="Optional NTM session id for load-state evidence.")
    search_parser.add_argument("--no-adapters", action="store_true", help="Skip optional br/bv/sbp/ntm adapters.")
    _add_profile_arg(search_parser)
    _add_client_arg(search_parser)

    snap_parser = subparsers.add_parser(
        "snap",
        help="Create, diff, or replay redacted agent operations snapshots.",
    )
    snap_parser.add_argument("--format", choices=("text", "json"), default="json")
    snap_subparsers = snap_parser.add_subparsers(dest="snap_action")
    snap_create_parser = snap_subparsers.add_parser("create", help="Create a redacted runtime/evidence/graph snapshot.")
    snap_create_parser.add_argument("--format", choices=("text", "json"), default="json")
    snap_create_parser.add_argument("--name", "--label", dest="name", default=None)
    snap_create_parser.add_argument("--created-at", default=None, help="Override timestamp for deterministic fixtures.")
    snap_create_parser.add_argument("--write", action="store_true", help="Write the snapshot under .skillbox-state.")
    snap_create_parser.add_argument("--cwd", default=None, help="Working directory used for evidence scoping.")
    snap_create_parser.add_argument("--ntm-session", default=None, help="Optional NTM session id for load-state evidence.")
    snap_create_parser.add_argument("--no-adapters", action="store_true", help="Skip optional br/bv/sbp/ntm adapters.")
    _add_profile_arg(snap_create_parser)
    _add_client_arg(snap_create_parser)
    snap_diff_parser = snap_subparsers.add_parser("diff", help="Diff two saved snapshot JSON files.")
    snap_diff_parser.add_argument("paths", nargs="*", help="Optional positional before/after snapshot paths.")
    snap_diff_parser.add_argument("--from", dest="from_path", default=None)
    snap_diff_parser.add_argument("--to", dest="to_path", default=None)
    snap_diff_parser.add_argument("--format", choices=("text", "json"), default="json")
    _add_profile_arg(snap_diff_parser)
    _add_client_arg(snap_diff_parser)
    snap_replay_parser = snap_subparsers.add_parser("replay", help="Replay one snapshot fixture without live services.")
    snap_replay_parser.add_argument("path", help="Snapshot JSON path.")
    snap_replay_parser.add_argument("--format", choices=("text", "json"), default="json")
    _add_profile_arg(snap_replay_parser)
    _add_client_arg(snap_replay_parser)

    mmdx_parser = subparsers.add_parser(
        "mmdx",
        help="Fuzzy-find and open Mermaid/MMDX files from the current repo.",
    )
    mmdx_parser.add_argument(
        "query",
        nargs="*",
        help="File path, stem, or fuzzy path fragment. Omit to list recent diagrams.",
    )
    mmdx_parser.add_argument("--format", choices=("text", "json"), default="text")
    mmdx_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory used for relative paths and default repo-root discovery. Defaults to $PWD.",
    )
    mmdx_parser.add_argument(
        "--search-root",
        action="append",
        default=[],
        help="Directory to scan for .mmdx/.mmd files. Can be repeated.",
    )
    mmdx_parser.add_argument("--limit", type=int, default=MMDX_DEFAULT_LIMIT)
    open_group = mmdx_parser.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open", action="store_true", default=True)
    open_group.add_argument("--no-open", dest="open", action="store_false")
    mmdx_parser.add_argument(
        "--tmux",
        action="store_true",
        help="Open with the MMDX skill's local tmux handoff bridge.",
    )
    mmdx_parser.add_argument(
        "--tmux-submit",
        action="store_true",
        help="With --tmux, press Enter after the viewer sends a handoff packet.",
    )
    mmdx_parser.add_argument(
        "--allow-parser-install",
        action="store_true",
        help="Allow the MMDX script to install its Mermaid parser dependency if missing.",
    )
    _add_profile_arg(mmdx_parser)
    _add_client_arg(mmdx_parser)

    swimmers_launch_parser = subparsers.add_parser(
        "swimmers-launch",
        help="Launch one Swimmers agent session per directory through /v1/sessions/batch.",
    )
    swimmers_launch_parser.add_argument("dirs", nargs="*", help="Directories to launch agents in.")
    swimmers_launch_parser.add_argument(
        "--dir",
        dest="dir_flags",
        action="append",
        default=[],
        help="Directory to launch. Can be repeated.",
    )
    swimmers_launch_parser.add_argument(
        "--cwd",
        dest="cwd_flags",
        action="append",
        default=[],
        help="Directory to launch. Alias for --dir for agent command consistency.",
    )
    swimmers_launch_parser.add_argument(
        "--dirs-file",
        default=None,
        help="File containing one launch directory per line.",
    )
    swimmers_launch_parser.add_argument(
        "--group",
        default=None,
        help="Resolve launch directories from a Swimmers /v1/dirs group.",
    )
    swimmers_launch_parser.add_argument(
        "--path",
        dest="group_path",
        default=None,
        help="Optional /v1/dirs path used with --group.",
    )
    swimmers_launch_parser.add_argument(
        "--managed-only",
        action="store_true",
        help="With --group, ask /v1/dirs for managed entries only.",
    )
    swimmers_launch_parser.add_argument(
        "--request",
        "--prompt",
        dest="request",
        default=None,
        help="Initial request sent to every launched agent.",
    )
    swimmers_launch_parser.add_argument(
        "--request-file",
        "--prompt-file",
        dest="request_file",
        default=None,
        help="Read the initial request from a file.",
    )
    swimmers_launch_parser.add_argument("--tool", choices=("codex", "claude"), default="codex")
    swimmers_launch_parser.add_argument(
        "--target",
        default=None,
        help="Optional Swimmers launch target id. Defaults to local.",
    )
    swimmers_launch_parser.add_argument(
        "--base-url",
        default=None,
        help="Swimmers API base URL. Defaults to SWIMMERS_URL, SWIMMERS_TUI_URL, or http://127.0.0.1:3210.",
    )
    swimmers_launch_parser.add_argument(
        "--auth-token-env",
        default=None,
        help="Environment variable containing the bearer token for token-auth Swimmers APIs.",
    )
    swimmers_launch_parser.add_argument("--timeout", type=float, default=30.0)
    swimmers_launch_parser.add_argument("--dry-run", action="store_true")
    swimmers_launch_parser.add_argument(
        "--invoke-cwd",
        default=None,
        help=argparse.SUPPRESS,
    )
    swimmers_launch_parser.add_argument("--format", choices=("text", "json"), default="text")

    operator_booking_parser = subparsers.add_parser(
        "operator-booking",
        help="Fetch human-operator availability or create an x402 booking hold from client config.",
    )
    operator_booking_parser.add_argument(
        "action",
        nargs="?",
        default="availability",
        choices=("availability", "times", "list", "config", "book"),
    )
    operator_booking_parser.add_argument("--format", choices=("text", "json"), default="text")
    operator_booking_parser.add_argument("--date", default=None, help="Booking date in YYYY-MM-DD format.")
    operator_booking_parser.add_argument("--slot", default=None, help="Slot type to book, e.g. AM or PM.")
    operator_booking_parser.add_argument("--email", default=None, help="Client email for booking/account email.")
    operator_booking_parser.add_argument("--name", default=None, help="Client display name for the booking.")
    operator_booking_parser.add_argument(
        "--redirect-url",
        default=None,
        help="Optional magic-link redirect URL for account sign-in.",
    )
    operator_booking_parser.add_argument(
        "--origin",
        default=None,
        help="Optional Origin header override for browser-style publishable-key requests.",
    )
    operator_booking_parser.add_argument(
        "--access-token-env",
        default=None,
        help="Optional env var containing a verified user JWT; binds bookings to that account.",
    )
    operator_booking_parser.add_argument(
        "--send-magic-link",
        action="store_true",
        help="Request a passwordless account email before creating the x402 hold.",
    )
    operator_booking_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For book, print the planned SPAPS requests without sending them.",
    )
    operator_booking_parser.add_argument("--limit", type=int, default=8)
    _add_profile_arg(operator_booking_parser)
    _add_client_arg(operator_booking_parser)
    _add_cwd_arg(operator_booking_parser)

    skill_parser = subparsers.add_parser(
        "skill",
        help="Plan and apply skill installs, moves, removals, and policy syncs.",
    )
    skill_subparsers = skill_parser.add_subparsers(dest="skill_action", required=True)

    for action_name, help_text in (
        ("plan", "Preview where a skill would be installed."),
        ("add", "Install/link a skill into the selected global, project, or category scope."),
        ("activate", "Install/link a skill and print an activation packet for the current session."),
        ("move", "Install/link a skill into a new scope and remove old installs for that skill."),
    ):
        action_parser = skill_subparsers.add_parser(action_name, help=help_text)
        action_parser.add_argument("skill_name")
        _add_skill_lifecycle_common(action_parser)

    skill_on_parser = skill_subparsers.add_parser(
        "on",
        help="Durably pin a skill on for this repo and return its activation packet.",
    )
    skill_on_parser.add_argument("skill_name")
    _add_skill_lifecycle_common(skill_on_parser)
    skill_on_parser.add_argument(
        "--verify",
        action="store_true",
        help="Re-resolve visibility after applying and report the linked packet hash.",
    )
    skill_on_parser.add_argument(
        "--global",
        dest="to",
        action="store_const",
        const="global",
        help="Request a global pin; refused when the skill is not globally allowed.",
    )
    skill_on_parser.set_defaults(to="project")

    skill_off_parser = skill_subparsers.add_parser(
        "off",
        help="Durably pin a skill off for this repo and unlink current installs.",
    )
    skill_off_parser.add_argument("skill_name")
    _add_skill_lifecycle_common(skill_off_parser)
    _add_skill_from_scope_arg(
        skill_off_parser,
        help_text="Installed scope to unlink after writing pin_off.",
    )
    skill_off_parser.set_defaults(to="project", from_scope="project")

    skill_heal_parser = skill_subparsers.add_parser(
        "heal",
        help="Resolve a skill source, durably pin it on for this repo, link it, and return its activation packet.",
    )
    skill_heal_parser.add_argument("skill_name")
    _add_skill_lifecycle_common(skill_heal_parser)
    skill_heal_parser.add_argument(
        "--verify",
        action="store_true",
        help="Re-resolve visibility after applying and report the linked packet hash.",
    )
    skill_heal_parser.set_defaults(to="project")

    skill_default_parser = skill_subparsers.add_parser(
        "default",
        help="Set repo or global skill defaults with a dry-run/apply diff.",
    )
    skill_default_parser.add_argument("default_action", choices=("on", "off"))
    skill_default_parser.add_argument("skill_name")
    default_scope = skill_default_parser.add_mutually_exclusive_group(required=True)
    default_scope.add_argument(
        "--repo",
        dest="default_scope",
        action="store_const",
        const="repo",
        help="Write the current repo's .skillbox/skill-overrides.yaml.",
    )
    default_scope.add_argument(
        "--global",
        dest="default_scope",
        action="store_const",
        const="global",
        help="Write the operator skill-scope.yaml allow_global defaults.",
    )
    skill_default_parser.add_argument("--format", choices=("text", "json"), default="text")
    skill_default_parser.add_argument("--cwd", default=None)
    skill_default_parser.add_argument("--dry-run", action="store_true")
    skill_default_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm --global apply; --global dry-runs never require this.",
    )
    skill_default_parser.add_argument(
        "--policy-path",
        default=None,
        help="Global skill-scope.yaml path. Defaults to SKILLBOX_SKILL_SCOPE_FILE or the operator config path.",
    )
    _add_profile_arg(skill_default_parser)
    _add_client_arg(skill_default_parser)

    remove_parser = skill_subparsers.add_parser("remove", help="Remove installed links/files for a skill.")
    remove_parser.add_argument("skill_name")
    _add_skill_lifecycle_common(remove_parser)
    _add_skill_from_scope_arg(remove_parser, help_text="Installed scope to remove from.")

    prune_parser = skill_subparsers.add_parser("prune", help="Remove installed skills that violate skill-scope policy.")
    _add_skill_lifecycle_common(prune_parser)
    _add_skill_from_scope_arg(
        prune_parser,
        help_text="Installed scope to prune. Use project for repo-local cleanup without touching global skills.",
    )

    sync_skills_parser = skill_subparsers.add_parser(
        "sync",
        help="Install/link a named skill or all literal skills missing for the current cwd policy.",
    )
    sync_skills_parser.add_argument("skill_name", nargs="?")
    _add_skill_lifecycle_common(sync_skills_parser)
    sync_skills_parser.add_argument(
        "--prune",
        action="store_true",
        help="Also unlink existing policy violations after installing missing skills.",
    )
    _add_skill_from_scope_arg(
        sync_skills_parser,
        help_text="Installed scope to prune when --prune is set.",
    )

    skill_lint_parser = skill_subparsers.add_parser(
        "lint",
        help="Lint the repo-local .skillbox/skill-overrides.yaml file.",
    )
    skill_lint_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(skill_lint_parser)
    _add_client_arg(skill_lint_parser)
    _add_cwd_arg(skill_lint_parser)

    skill_why_parser = skill_subparsers.add_parser(
        "why",
        help="Explain one skill's visibility provenance for this cwd, including absence and fixes.",
    )
    skill_why_parser.add_argument("skill_name")
    skill_why_parser.add_argument("--format", choices=("text", "json"), default="text")
    skill_why_parser.add_argument(
        "--no-global",
        action="store_true",
        help="Do not inspect ~/.claude/skills or ~/.codex/skills.",
    )
    skill_why_parser.add_argument(
        "--no-project",
        action="store_true",
        help="Do not inspect project-local .claude/.codex skill dirs near --cwd.",
    )
    _add_profile_arg(skill_why_parser)
    _add_client_arg(skill_why_parser)
    _add_cwd_arg(skill_why_parser)

    skill_togglable_parser = skill_subparsers.add_parser(
        "togglable",
        aliases=["toggleable"],
        help="List every skill flippable at this cwd and the command to flip it.",
    )
    skill_togglable_parser.add_argument("--format", choices=("text", "json"), default="text")
    skill_togglable_parser.add_argument(
        "--json",
        dest="format",
        action="store_const",
        const="json",
        help="Alias for --format json.",
    )
    _add_profile_arg(skill_togglable_parser)
    _add_client_arg(skill_togglable_parser)
    _add_cwd_arg(skill_togglable_parser)

    overlay_parser = subparsers.add_parser(
        "overlay",
        help="List, enable, disable, toggle, or activate skill scope overlays (e.g. marketing).",
        description=(
            "Manage skill-scope overlays. `activate` is policy-evaluated and ephemeral: "
            "it runs the SAME policy evaluation as `skill sync` with the named overlay "
            "treated as active for THIS invocation only (equivalent to "
            "`SKILLBOX_CLI_OVERLAYS=<name> skill sync` scoped to --cwd), persists NO overlay "
            "state, and links only the policy-correct set for --cwd (often zero in a "
            "non-matching dir) — never every literal overlay-tagged skill. `--dry-run` "
            "previews exactly the plan `activate` would apply."
        ),
    )
    overlay_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "on", "off", "toggle", "activate"),
        help=(
            "list (default), on, off, toggle, or activate. activate is "
            "policy-evaluated and cwd-scoped (same evaluation as `skill sync` with "
            "the overlay forced active for this call only); it persists no state."
        ),
    )
    overlay_parser.add_argument("name", nargs="?", help="Overlay name, e.g. marketing.")
    overlay_parser.add_argument("--cwd", default=None, help="Target cwd for scoped unlinks. Defaults to $PWD.")
    overlay_parser.add_argument(
        "--keep",
        action="store_true",
        help="When turning an overlay off, keep existing symlinks. Default is to unlink overlay-scoped symlinks for --scope.",
    )
    overlay_parser.add_argument(
        "--to",
        choices=("project", "global", "category", "auto"),
        default="project",
        help="Activation destination. Defaults to project so hot overlays stay scoped to --cwd.",
    )
    overlay_parser.add_argument(
        "--scope",
        choices=("project", "global", "all"),
        default="project",
        help="Symlink removal scope for off/toggle. Defaults to project.",
    )
    overlay_parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Project category to target when --to category. Can be repeated.",
    )
    overlay_parser.add_argument(
        "--source",
        default=None,
        help="Explicit skill directory or parent source directory for activation.",
    )
    overlay_parser.add_argument("--dry-run", action="store_true")
    overlay_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing non-symlink files and override global policy blocks.",
    )
    overlay_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(overlay_parser)
    _add_client_arg(overlay_parser)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Sync runtime state and run one-shot bootstrap tasks for the active scope.",
    )
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(bootstrap_parser)
    _add_client_arg(bootstrap_parser)
    _add_cwd_arg(bootstrap_parser)
    _add_task_arg(bootstrap_parser)

    up_parser = subparsers.add_parser(
        "up",
        help="Sync runtime state and start manageable services for the active scope.",
    )
    up_parser.add_argument("--dry-run", action="store_true")
    up_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    up_parser.add_argument("--format", choices=("text", "json"), default="text")
    # --mode is the first-class startup-behavior selector introduced by
    # local_runtime_core_cutover (shared.md:428-469). It is orthogonal to
    # --profile (which answers "which graph?") per Business Rule 2. We do
    # NOT constrain choices at argparse level here because an unknown value
    # must surface as a structured LOCAL_RUNTIME_MODE_UNSUPPORTED envelope
    # rather than an argparse usage error. Validation happens immediately
    # after parse_args() and BEFORE any mutation.
    up_parser.add_argument(
        "--mode",
        default=None,
        help="Local runtime startup behavior. One of: reuse (default), prod, fresh.",
    )
    _add_profile_arg(up_parser)
    _add_client_arg(up_parser)
    _add_cwd_arg(up_parser)
    _add_service_arg(up_parser)

    down_parser = subparsers.add_parser(
        "down",
        help="Stop manageable services for the active scope.",
    )
    down_parser.add_argument("--dry-run", action="store_true")
    down_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_STOP_WAIT_SECONDS)
    down_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(down_parser)
    _add_client_arg(down_parser)
    _add_cwd_arg(down_parser)
    _add_service_arg(down_parser)

    restart_parser = subparsers.add_parser(
        "restart",
        help="Restart manageable services for the active scope.",
    )
    restart_parser.add_argument("--dry-run", action="store_true")
    restart_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    restart_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(restart_parser)
    _add_client_arg(restart_parser)
    _add_cwd_arg(restart_parser)
    _add_service_arg(restart_parser)

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show recent logs for declared services in the active scope.",
    )
    logs_parser.add_argument("--lines", type=int, default=DEFAULT_LOG_TAIL_LINES)
    logs_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(logs_parser)
    _add_client_arg(logs_parser)
    _add_cwd_arg(logs_parser)
    _add_service_arg(logs_parser)

    context_parser = subparsers.add_parser(
        "context",
        help="Generate CLAUDE.md and AGENTS.md from the resolved runtime graph.",
    )
    context_parser.add_argument("--dry-run", action="store_true")
    context_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(context_parser)
    _add_client_arg(context_parser)
    _add_context_dir_arg(context_parser)

    client_init_parser = subparsers.add_parser(
        "client-init",
        help="Scaffold a new workspace client overlay and companion skill directories.",
    )
    client_init_parser.add_argument(
        "client_id",
        nargs="?",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    client_init_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    client_init_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    client_init_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    client_init_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    client_init_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    client_init_parser.add_argument(
        "--list-blueprints",
        action="store_true",
        help="List discoverable client blueprints and their variables.",
    )
    client_init_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    client_init_parser.add_argument("--dry-run", action="store_true")
    client_init_parser.add_argument("--format", choices=("text", "json"), default="text")

    private_init_parser = subparsers.add_parser(
        "private-init",
        help="Attach or initialize a private client-config repo for this checkout.",
    )
    private_init_parser.add_argument(
        "--path",
        default=None,
        help="Private repo path. Defaults to ../skillbox-config.",
    )
    private_init_parser.add_argument("--format", choices=("text", "json"), default="text")

    client_project_parser = subparsers.add_parser(
        "client-project",
        help="Compile a single-client runtime projection bundle with sanitized metadata.",
    )
    client_project_parser.add_argument(
        "client_id",
        help="Existing client slug to project (for example `personal`).",
    )
    client_project_parser.add_argument(
        "--output-dir",
        default=None,
        help="Projection output directory. Defaults to builds/clients/<client-id>.",
    )
    client_project_parser.add_argument("--force", action="store_true", help="Replace an existing projection bundle.")
    client_project_parser.add_argument("--dry-run", action="store_true")
    client_project_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_project_parser)

    client_open_parser = subparsers.add_parser(
        "client-open",
        help="Project one client into a safe surface with scoped context and MCP config.",
    )
    client_open_parser.add_argument(
        "client_id",
        help="Existing client slug to open (for example `personal`).",
    )
    client_open_parser.add_argument(
        "--output-dir",
        default=None,
        help="Open-surface output directory. Defaults to sand/<client-id>.",
    )
    client_open_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to open instead of building a fresh one.",
    )
    client_open_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_open_parser)

    client_publish_parser = subparsers.add_parser(
        "client-publish",
        help="Promote a client projection bundle into a git-backed control-plane repo.",
    )
    client_publish_parser.add_argument(
        "client_id",
        help="Existing client slug to publish (for example `personal`).",
    )
    client_publish_parser.add_argument(
        "--target-dir",
        help="Git repo that receives clients/<client>/current/ and publish.json.",
    )
    client_publish_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to publish instead of building a fresh one.",
    )
    client_publish_parser.add_argument(
        "--acceptance",
        action="store_true",
        help="Run acceptance first and persist compact acceptance evidence with the published client payload.",
    )
    client_publish_parser.add_argument(
        "--deploy-artifact",
        action="store_true",
        help="Build a pinned source archive plus deploy.json for offline box installs.",
    )
    client_publish_parser.add_argument(
        "--commit",
        action="store_true",
        help="Create one local git commit in the target repo when the publish changes files.",
    )
    client_publish_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_publish_parser)

    client_diff_parser = subparsers.add_parser(
        "client-diff",
        help="Compare a client projection bundle against the current published payload in a target repo.",
    )
    client_diff_parser.add_argument(
        "client_id",
        help="Existing client slug to diff (for example `personal`).",
    )
    client_diff_parser.add_argument(
        "--target-dir",
        help="Git repo that holds clients/<client>/current/ and publish.json.",
    )
    client_diff_parser.add_argument(
        "--from-bundle",
        default=None,
        help="Existing client-project bundle to diff instead of building a fresh one.",
    )
    client_diff_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(client_diff_parser)

    distribution_publish_parser = subparsers.add_parser(
        "distribution-publish",
        help="Publish one local skill version into a signed schema_version=2 manifest.",
    )
    distribution_publish_parser.add_argument("skill_path")
    distribution_publish_parser.add_argument("--version", type=int, required=True)
    distribution_publish_parser.add_argument("--manifest-path", required=True)
    distribution_publish_parser.add_argument("--artifact-root", required=True)
    distribution_publish_parser.add_argument("--signing-key", required=True)
    distribution_publish_parser.add_argument("--skill-name", default=None)
    distribution_publish_parser.add_argument("--distributor-id", default="local")
    distribution_publish_parser.add_argument("--client-id", default="local")
    distribution_publish_parser.add_argument("--target", action="append", default=[])
    distribution_publish_parser.add_argument("--capability", action="append", default=[])
    distribution_publish_parser.add_argument("--changelog", default=None)
    distribution_publish_parser.add_argument("--min-version", type=int, default=None)
    distribution_publish_parser.add_argument("--min-version-reason", default=None)
    distribution_publish_parser.add_argument("--download-prefix", default="/skills")
    distribution_publish_parser.add_argument("--format", choices=("text", "json"), default="text")

    distribution_preview_parser = subparsers.add_parser(
        "distribution-preview",
        help="Preview selected artifacts from a signed manifest without mutating local state.",
    )
    distribution_preview_parser.add_argument("--manifest-path", required=True)
    distribution_preview_parser.add_argument("--public-key", required=True)
    distribution_preview_parser.add_argument("--distributor-id", default="local")
    distribution_preview_parser.add_argument("--state-root", default=".skillbox-state")
    distribution_preview_parser.add_argument("--pick", action="append", default=[])
    distribution_preview_parser.add_argument("--pin", action="append", default=[])
    distribution_preview_parser.add_argument("--target-env", default=None)
    distribution_preview_parser.add_argument("--lockfile", default=None)
    distribution_preview_parser.add_argument("--format", choices=("text", "json"), default="text")

    distribution_rollback_parser = subparsers.add_parser(
        "distribution-rollback",
        help="Rollback one skill to a verified cached bundle version.",
    )
    distribution_rollback_parser.add_argument("--manifest-path", default=None)
    distribution_rollback_parser.add_argument("--public-key", default=None)
    distribution_rollback_parser.add_argument("--distributor-id", default="local")
    distribution_rollback_parser.add_argument("--skill", required=True)
    distribution_rollback_parser.add_argument("--version", type=int, default=None)
    distribution_rollback_parser.add_argument("--state-root", default=".skillbox-state")
    distribution_rollback_parser.add_argument("--install-target", action="append", default=[])
    distribution_rollback_parser.add_argument("--lockfile", default=None)
    distribution_rollback_parser.add_argument("--reason", default=None)
    distribution_rollback_parser.add_argument("--emergency-override", action="store_true")
    distribution_rollback_parser.add_argument("--list", action="store_true")
    distribution_rollback_parser.add_argument("--format", choices=("text", "json"), default="text")

    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Macro: scaffold a client, sync, bootstrap, start services, generate context, and verify.",
    )
    onboard_parser.add_argument(
        "client_id",
        help="Lowercase client slug, for example `acme-studio`.",
    )
    onboard_parser.add_argument("--label", default=None, help="Human-friendly label for the client.")
    onboard_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    onboard_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client. Defaults to the client root path.",
    )
    onboard_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint from workspace/client-blueprints/ or an explicit YAML path.",
    )
    onboard_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    onboard_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files.")
    onboard_parser.add_argument("--dry-run", action="store_true")
    onboard_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    onboard_parser.add_argument("--format", choices=("text", "json"), default="text")

    first_box_parser = subparsers.add_parser(
        "first-box",
        help="Canonical first-run path: attach the private repo, reuse or scaffold the client, prove readiness, and open a client surface.",
    )
    first_box_parser.add_argument(
        "client_id",
        nargs="?",
        default="personal",
        help="Client slug to prepare. Defaults to `personal`.",
    )
    first_box_parser.add_argument(
        "--private-path",
        default=None,
        help="Private config repo path. Defaults to ../skillbox-config.",
    )
    first_box_parser.add_argument(
        "--output-dir",
        default=None,
        help="Open-surface output directory. Defaults to sand/<client-id>.",
    )
    first_box_parser.add_argument("--label", default=None, help="Human-friendly label for the client when scaffolding.")
    first_box_parser.add_argument(
        "--root-path",
        default=None,
        help="Runtime path for the client root when scaffolding. Defaults to ${SKILLBOX_MONOSERVER_ROOT}.",
    )
    first_box_parser.add_argument(
        "--default-cwd",
        default=None,
        help="Runtime default cwd for the client when scaffolding. Defaults to the client root path.",
    )
    first_box_parser.add_argument(
        "--blueprint",
        default=None,
        help="Apply a reusable client blueprint when scaffolding a missing client.",
    )
    first_box_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Set a blueprint variable using KEY=VALUE. Can be repeated.",
    )
    first_box_parser.add_argument("--force", action="store_true", help="Overwrite existing scaffold files when onboarding.")
    first_box_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    first_box_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(first_box_parser)

    forge_parser = subparsers.add_parser(
        "forge",
        help="Bootstrap and inspect the skill-forge feedback loop.",
    )
    forge_subparsers = forge_parser.add_subparsers(dest="forge_action", required=True)
    forge_init_parser = forge_subparsers.add_parser(
        "init",
        help="Install idempotent score-session hooks for Claude, codex-tmux, and optional cron.",
    )
    forge_init_parser.add_argument("--with-cron", action="store_true")
    forge_init_parser.add_argument("--scoring-script", default=None)
    forge_init_parser.add_argument("--format", choices=("text", "json"), default="json")
    forge_status_parser = forge_subparsers.add_parser(
        "status",
        help="Show per-skill forge health from the skill review signal store.",
    )
    forge_status_parser.add_argument("--format", choices=("table", "json"), default="table")
    forge_status_parser.add_argument("--skill", default=None, help="Limit status to one skill name.")
    forge_propose_parser = forge_subparsers.add_parser(
        "propose",
        help="Create a deterministic forge/<skill> proposal branch from scored signal.",
    )
    forge_propose_parser.add_argument("skill", help="Skill name to propose an update for.")
    forge_propose_parser.add_argument("--dry-run", action="store_true")
    forge_propose_parser.add_argument("--min-sessions", type=int, default=5)
    forge_propose_parser.add_argument("--format", choices=("text", "json"), default="json")
    forge_accept_parser = forge_subparsers.add_parser(
        "accept",
        help="Fast-forward merge a reviewed forge/<skill> proposal branch and log the decision.",
    )
    forge_accept_parser.add_argument("skill", help="Skill name to accept.")
    forge_accept_parser.add_argument("--format", choices=("text", "json"), default="json")
    forge_reject_parser = forge_subparsers.add_parser(
        "reject",
        help="Delete a forge/<skill> proposal branch and log a rejection reason.",
    )
    forge_reject_parser.add_argument("skill", help="Skill name to reject.")
    forge_reject_parser.add_argument("--reason", default=None, help="Required non-empty rejection reason.")
    forge_reject_parser.add_argument("--format", choices=("text", "json"), default="json")

    focus_parser = subparsers.add_parser(
        "focus",
        help="Activate a client workspace with live state and enriched agent context.",
    )
    focus_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to focus on (e.g. 'personal').",
    )
    focus_parser.add_argument(
        "--resume",
        action="store_true",
        help="Re-activate the last focus session from .focus.json.",
    )
    focus_parser.add_argument(
        "--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS,
    )
    focus_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(focus_parser)
    # --client accepted for CLI consistency with up/status/logs/doctor
    # (local_runtime_core_cutover WG-003). The positional client_id remains
    # the primary input; --client is honored when no positional is given.
    _add_client_arg(focus_parser)
    _add_service_arg(focus_parser)
    _add_context_dir_arg(focus_parser)

    stewardship_parser = subparsers.add_parser(
        "stewardship-report",
        help="Summarize current operator evidence, risks, and unassessed hardening gaps for a client.",
    )
    stewardship_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to report on (e.g. 'personal').",
    )
    stewardship_parser.add_argument("--format", choices=("text", "json", "md"), default="text")
    stewardship_parser.add_argument(
        "--write",
        action="store_true",
        help="Write the report artifact into the client report directory.",
    )
    stewardship_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for report artifacts. Defaults to the client reports/stewardship directory when --write is used.",
    )
    _add_profile_arg(stewardship_parser)
    _add_client_arg(stewardship_parser)

    parity_report_parser = subparsers.add_parser(
        "parity-report",
        help="Compare a client runtime graph against its production-stack parity contract.",
    )
    parity_report_parser.add_argument(
        "client_id",
        nargs="?",
        default="",
        help="Existing client slug to report on (e.g. 'personal').",
    )
    parity_report_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(parity_report_parser)
    _add_client_arg(parity_report_parser)
    _add_cwd_arg(parity_report_parser)

    session_start_parser = subparsers.add_parser(
        "session-start",
        help="Create a durable client-scoped session ledger with metadata and append-only events.",
    )
    session_start_parser.add_argument("client_id", help="Existing client slug to attach the session to.")
    session_start_parser.add_argument("--label", default="", help="Human-friendly session label.")
    session_start_parser.add_argument("--cwd", default="", help="Working directory for the session.")
    session_start_parser.add_argument("--goal", default="", help="Short statement of intent for the session.")
    session_start_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_start_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_event_parser = subparsers.add_parser(
        "session-event",
        help="Append a structured event to an active durable session.",
    )
    session_event_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_event_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_event_parser.add_argument("--event-type", required=True, help="Session event type, with or without session. prefix.")
    session_event_parser.add_argument("--message", default="", help="Optional event message.")
    session_event_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_event_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_end_parser = subparsers.add_parser(
        "session-end",
        help="Close an active durable session and persist a handoff summary.",
    )
    session_end_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_end_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_end_parser.add_argument(
        "--status",
        default="completed",
        choices=sorted(SESSION_TERMINAL_STATUSES),
        help="Terminal lifecycle state to persist.",
    )
    session_end_parser.add_argument("--summary", default="", help="Optional closeout summary.")
    session_end_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_resume_parser = subparsers.add_parser(
        "session-resume",
        help="Resume a previously ended durable session.",
    )
    session_resume_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_resume_parser.add_argument("--session-id", required=True, help="Durable session id.")
    session_resume_parser.add_argument("--actor", default="", help="Optional actor or operator name.")
    session_resume_parser.add_argument("--message", default="", help="Optional resume note.")
    session_resume_parser.add_argument("--format", choices=("text", "json"), default="text")

    session_status_parser = subparsers.add_parser(
        "session-status",
        help="Read one durable session or list recent sessions for a client.",
    )
    session_status_parser.add_argument("client_id", help="Existing client slug that owns the session.")
    session_status_parser.add_argument("--session-id", default=None, help="Specific durable session id to inspect.")
    session_status_parser.add_argument("--limit", type=int, default=10, help="Maximum sessions to return when listing.")
    session_status_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_submit_parser = subparsers.add_parser(
        "worker-submit",
        help="Create a broker-managed worker run without launching a runtime before context resolution.",
    )
    worker_submit_parser.add_argument("task_class", choices=WORKER_TASK_CLASSES)
    worker_submit_parser.add_argument("instruction")
    worker_submit_parser.add_argument("--client", default="", help="Overlay id for context resolution.")
    worker_submit_parser.add_argument("--cwd", default="", help="Working directory hint for context resolution.")
    worker_submit_parser.add_argument("--repo-hint", default="", help="Optional repo id or path hint.")
    worker_submit_parser.add_argument("--runtime", default="", help="Requested runtime id. Defaults to hermes.")
    worker_submit_parser.add_argument("--write-scope", choices=WORKER_WRITE_SCOPES, default=WORKER_DEFAULT_WRITE_SCOPE)
    worker_submit_parser.add_argument("--memory-scope", choices=WORKER_MEMORY_SCOPES, default=WORKER_DEFAULT_MEMORY_SCOPE)
    worker_submit_parser.add_argument("--artifact-policy", default=WORKER_DEFAULT_ARTIFACT_POLICY)
    worker_submit_parser.add_argument("--harness-session-ref", default="", help="Opaque caller correlation id.")
    worker_submit_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_status_parser = subparsers.add_parser(
        "worker-status",
        help="Read a broker-managed worker run by run id.",
    )
    worker_status_parser.add_argument("run_id")
    worker_status_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_artifacts_parser = subparsers.add_parser(
        "worker-artifacts",
        help="Read result artifacts for a terminal broker-managed worker run.",
    )
    worker_artifacts_parser.add_argument("run_id")
    worker_artifacts_parser.add_argument("--format", choices=("text", "json"), default="text")

    worker_promote_parser = subparsers.add_parser(
        "worker-promote-learning",
        help="Promote a reviewed worker learning proposal into its target.",
    )
    worker_promote_parser.add_argument("proposal_id")
    worker_promote_parser.add_argument("--approved-by", default="", help="Operator or reviewer approving promotion.")
    worker_promote_parser.add_argument("--target-kind", required=True)
    worker_promote_parser.add_argument("--target-location", required=True)
    worker_promote_parser.add_argument("--promotion-mode", default="promote")
    worker_promote_parser.add_argument("--format", choices=("text", "json"), default="text")

    acceptance_parser = subparsers.add_parser(
        "acceptance",
        help="Run the first-box readiness gate: doctor-pre, sync, focus, mcp-smoke, doctor-post.",
    )
    acceptance_parser.add_argument(
        "client_id",
        help="Existing client slug to validate for first-box acceptance (for example `personal`).",
    )
    acceptance_parser.add_argument("--wait-seconds", type=float, default=DEFAULT_SERVICE_START_WAIT_SECONDS)
    acceptance_parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_profile_arg(acceptance_parser)

    return parser


def _apply_default_command(args: argparse.Namespace) -> None:
    """When no subcommand is given, fall back to a compact `status` invocation."""
    if args.command is None:
        args.command = "status"
        args.format = "text"
        args.compact = False
        args.profile = []
        args.client = []


def _resolve_local_runtime_mode(args: argparse.Namespace) -> tuple[str, dict[str, Any] | None]:
    """Validate `--mode` and return the resolved mode plus an optional structured error.

    WG-003 requires unknown values to surface as a structured
    LOCAL_RUNTIME_MODE_UNSUPPORTED envelope (shared.md:457-469) rather than
    an argparse usage error. Only `up` accepts `--mode` today; other
    surfaces ignore it. Mode is orthogonal to --profile and --service
    (shared.md:518-520, Business Rule 2).
    """
    requested_mode_raw = getattr(args, "mode", None)
    default_mode = "reuse"
    if requested_mode_raw is None or str(requested_mode_raw).strip() == "":
        return default_mode, None

    resolved_mode = str(requested_mode_raw).strip()
    if resolved_mode in LOCAL_RUNTIME_START_MODES:
        return resolved_mode, None

    supported = ", ".join(LOCAL_RUNTIME_START_MODES)
    err = local_runtime_error(
        LOCAL_RUNTIME_MODE_UNSUPPORTED,
        f"Unsupported --mode value {resolved_mode!r}. Supported modes: {supported}.",
        recoverable=True,
        next_action=f"Re-run with --mode <{'|'.join(LOCAL_RUNTIME_START_MODES)}>.",
    )
    err["error"]["requested_mode"] = resolved_mode
    return resolved_mode, err


def _handle_client_init(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        if args.list_blueprints:
            blueprints = list_client_blueprints(root_dir)
            if args.format == "json":
                emit_json({"blueprints": blueprints})
            else:
                print_client_blueprints_text(blueprints)
            return EXIT_OK

        if not args.client_id:
            raise RuntimeError("client-init requires <client_id> unless --list-blueprints is used.")

        assignments = parse_key_value_assignments(args.set, "--set")
        actions, blueprint_metadata = scaffold_client_overlay(
            root_dir=root_dir,
            client_id=args.client_id,
            label=args.label,
            default_cwd=args.default_cwd,
            root_path=args.root_path,
            blueprint_name=args.blueprint,
            blueprint_assignments=assignments,
            dry_run=args.dry_run,
            force=args.force,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-init"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    cid = validate_client_id(args.client_id)
    payload: dict[str, Any] = {
        "client_id": cid,
        "dry_run": args.dry_run,
        "force": args.force,
        "actions": actions,
        "next_actions": next_actions_for_client_init(cid),
    }
    if blueprint_metadata is not None:
        payload["blueprint"] = blueprint_metadata
    if args.format == "json":
        emit_json(payload)
    else:
        if blueprint_metadata is not None:
            print(f"blueprint: {blueprint_metadata['id']}")
        print("\n".join(actions))
    return EXIT_OK


def _handle_onboard(args: argparse.Namespace, root_dir: Path) -> int:
    return run_onboard(
        root_dir=root_dir,
        client_id=args.client_id,
        label=args.label,
        default_cwd=args.default_cwd,
        root_path=args.root_path,
        blueprint_name=args.blueprint,
        set_args=args.set,
        dry_run=args.dry_run,
        force=args.force,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_first_box(args: argparse.Namespace, root_dir: Path) -> int:
    return run_first_box(
        root_dir=root_dir,
        client_id=args.client_id,
        private_path_arg=args.private_path,
        profiles=args.profile,
        output_dir_arg=args.output_dir,
        label=args.label,
        default_cwd=args.default_cwd,
        root_path=args.root_path,
        blueprint_name=args.blueprint,
        set_args=args.set,
        force=args.force,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_forge(args: argparse.Namespace, root_dir: Path) -> int:
    if args.forge_action == "status":
        payload = forge_status(skill=args.skill, root_dir=root_dir)
        if args.format == "json":
            emit_json(payload)
        else:
            print("\n".join(format_forge_status_table(payload)))
        return EXIT_OK

    if args.forge_action == "propose":
        try:
            payload = forge_propose(
                args.skill,
                dry_run=bool(args.dry_run),
                min_sessions=int(args.min_sessions),
                root_dir=root_dir,
            )
        except ForgeProposeError as exc:
            payload = {
                "ok": False,
                "code": exc.code,
                "error": {
                    "type": exc.code,
                    "message": str(exc),
                },
            }
            payload.update(exc.payload)
            if args.format == "json":
                emit_json(payload)
            else:
                print(f"{exc.code}: {exc}", file=sys.stderr)
            return EXIT_ERROR

        if args.format == "json":
            emit_json(payload)
        else:
            prefix = "would create" if payload.get("dry_run") else "created"
            print(f"{prefix}: {payload['branch']}")
            print(f"repo: {payload['repo']}")
            print(f"watch_metric: {payload['watch_metric']}")
        return EXIT_OK

    if args.forge_action in {"accept", "reject"}:
        try:
            if args.forge_action == "accept":
                payload = forge_accept(args.skill, root_dir=root_dir)
            else:
                payload = forge_reject(args.skill, reason=getattr(args, "reason", None), root_dir=root_dir)
        except ForgeDecisionError as exc:
            payload = {
                "ok": False,
                "code": exc.code,
                "error": {
                    "type": exc.code,
                    "message": str(exc),
                },
            }
            payload.update(exc.payload)
            if args.format == "json":
                emit_json(payload)
            else:
                print(f"{exc.code}: {exc}", file=sys.stderr)
            return EXIT_ERROR

        if args.format == "json":
            emit_json(payload)
        else:
            print(f"{payload['action']}: {payload['skill']}")
            if payload.get("commit"):
                print(f"commit: {payload['commit']}")
            if payload.get("reason"):
                print(f"reason: {payload['reason']}")
            if payload.get("sync_next_action"):
                print(payload["sync_next_action"])
        return EXIT_OK

    if args.forge_action != "init":
        message = f"Unsupported forge action: {args.forge_action}"
        if args.format == "json":
            emit_json(classify_error(RuntimeError(message), "forge"))
        else:
            print(message, file=sys.stderr)
        return EXIT_ERROR
    try:
        payload = forge_init(
            with_cron=bool(args.with_cron),
            scoring_script=getattr(args, "scoring_script", None),
        )
    except ForgeInitError as exc:
        payload = {
            "ok": False,
            "error": {
                "type": exc.code,
                "message": str(exc),
            },
        }
        if args.format == "json":
            emit_json(payload)
        else:
            print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"settings: {payload['settings']['action']}")
        print(f"codex-tmux: {payload['codex_tmux']['action']}")
        print(f"cron: {payload['cron']['action']}")
        for warning in payload.get("warnings") or []:
            print(f"warning: {warning}")
    return EXIT_OK


def _handle_private_init(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = init_private_repo(
            root_dir=root_dir,
            target_dir_arg=args.path,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "private-init"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"target_dir: {payload['target_dir']}")
        print(f"clients_host_root: {payload['clients_host_root']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_acceptance(args: argparse.Namespace, root_dir: Path) -> int:
    return run_acceptance(
        root_dir=root_dir,
        client_id=args.client_id,
        profiles=args.profile,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
    )


def _handle_client_project(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = project_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            profiles=args.profile,
            output_dir_arg=args.output_dir,
            dry_run=args.dry_run,
            force=args.force,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-project"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"output_dir: {payload['output_dir']}")
        print(f"files: {payload['file_count']}")
        print(f"payload_tree_sha256: {payload['payload_tree_sha256']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_client_open(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload, exit_code = open_client_surface(
            root_dir=root_dir,
            client_id=args.client_id,
            profiles=args.profile,
            output_dir_arg=args.output_dir,
            from_bundle_arg=args.from_bundle,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-open"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"output_dir: {payload['output_dir']}")
        print(f"profiles: {', '.join(payload['active_profiles'])}")
        print(f"mcp_servers: {', '.join(payload['mcp_servers'])}")
        print(f"focus: {payload['focus']['status']}")
        print()
        print("\n".join(payload["actions"]))
    return exit_code


def _handle_client_publish(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = publish_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            target_dir_arg=args.target_dir,
            from_bundle_arg=args.from_bundle,
            profiles=args.profile,
            require_acceptance=args.acceptance,
            write_deploy_artifact=args.deploy_artifact,
            commit=args.commit,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-publish"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"client: {payload['client_id']}")
        print(f"target_dir: {payload['target_dir']}")
        print(f"changed: {payload['changed']}")
        print(f"payload_tree_sha256: {payload['payload_tree_sha256']}")
        if payload["acceptance"]["present"]:
            print(f"accepted_at: {payload['acceptance']['accepted_at']}")
            print(f"acceptance_profiles: {', '.join(payload['acceptance']['active_profiles'])}")
        if payload["deploy"]["present"]:
            print(f"deploy_manifest: {payload['deploy']['manifest']}")
            print(f"deploy_archive: {payload['deploy']['archive']}")
        if payload["commit_hash"]:
            print(f"commit: {payload['commit_hash']}")
        print()
        print("\n".join(payload["actions"]))
    return EXIT_OK


def _handle_client_diff(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = diff_client_bundle(
            root_dir=root_dir,
            client_id=args.client_id,
            target_dir_arg=args.target_dir,
            from_bundle_arg=args.from_bundle,
            profiles=args.profile,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "client-diff"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print_client_diff_text(payload)
    return EXIT_OK


def _handle_distribution_publish(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = publish_skill_release(
            skill_path=host_path_to_absolute_path(root_dir, args.skill_path),
            version=args.version,
            manifest_path=host_path_to_absolute_path(root_dir, args.manifest_path),
            artifact_root=host_path_to_absolute_path(root_dir, args.artifact_root),
            signing_key_ref=args.signing_key,
            distributor_id=args.distributor_id,
            client_id=args.client_id,
            skill_name=args.skill_name,
            targets=args.target,
            capabilities=args.capability,
            changelog=args.changelog,
            min_version=args.min_version,
            min_version_reason=args.min_version_reason,
            download_prefix=args.download_prefix,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-publish"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"skill: {payload['skill']}")
        print(f"version: {payload['version']}")
        print(f"result: {payload['result']}")
        print(f"artifact: {payload['artifact_path']}")
        print(f"manifest: {payload['manifest_path']}")
        print(f"sha256: {payload['artifact_sha256']}")
    return EXIT_OK


def _handle_distribution_preview(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = preview_manifest(
            manifest_bytes=host_path_to_absolute_path(root_dir, args.manifest_path).read_bytes(),
            public_key_config=args.public_key,
            distributor_id=args.distributor_id,
            state_root=host_path_to_absolute_path(root_dir, args.state_root),
            pick=args.pick,
            pin=_parse_distribution_pin_args(args.pin),
            target_env=args.target_env,
            lockfile_path=(
                host_path_to_absolute_path(root_dir, args.lockfile)
                if args.lockfile else None
            ),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-preview"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"distributor: {payload['distributor_id']}")
        print(f"manifest_version: {payload['manifest_version']}")
        print(f"ready: {payload['ready']}")
        for item in payload["items"]:
            print(
                f"{item['action']}: {item['skill']} v{item['selected_version']} "
                f"{item['pinned_by']} cache={item['cache_state']}"
            )
    return EXIT_OK if payload.get("ready") else EXIT_ERROR


def _handle_distribution_rollback(args: argparse.Namespace, root_dir: Path) -> int:
    state_root = host_path_to_absolute_path(root_dir, args.state_root)
    if args.list:
        payload = {
            "ok": True,
            "skill": args.skill,
            "cached_versions": cached_versions(
                state_root=state_root,
                skill_name=args.skill,
            ),
        }
        if args.format == "json":
            emit_json(payload)
        else:
            print(f"skill: {payload['skill']}")
            print("cached_versions: " + ", ".join(str(v) for v in payload["cached_versions"]))
        return EXIT_OK

    try:
        missing = [
            option
            for option, value in (
                ("--manifest-path", args.manifest_path),
                ("--public-key", args.public_key),
                ("--version", args.version),
                ("--lockfile", args.lockfile),
            )
            if value is None
        ]
        if missing:
            raise DistributionRollbackError(
                "distribution-rollback requires " + ", ".join(missing)
            )
        install_targets = [
            {"id": f"target-{index + 1}", "host_path": str(host_path_to_absolute_path(root_dir, target))}
            for index, target in enumerate(args.install_target)
        ]
        if not install_targets:
            raise DistributionRollbackError("distribution-rollback requires --install-target")
        payload = rollback_distributor_skill(
            manifest_path=host_path_to_absolute_path(root_dir, args.manifest_path),
            public_key_config=args.public_key,
            distributor_id=args.distributor_id,
            skill_name=args.skill,
            target_version=args.version,
            state_root=state_root,
            install_targets=install_targets,
            lockfile_path=host_path_to_absolute_path(root_dir, args.lockfile),
            reason=args.reason,
            emergency_override=args.emergency_override,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(RuntimeError(str(exc)), "distribution-rollback"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print(f"skill: {payload['skill']}")
        print(f"to_version: {payload['to_version']}")
        print(f"source: {payload['source']}")
        print(f"lockfile_updated: {payload['lockfile_updated']}")
    return EXIT_OK


def _parse_distribution_pin_args(raw_items: list[str]) -> dict[str, int]:
    pins: dict[str, int] = {}
    for raw_item in raw_items or []:
        if "=" not in raw_item:
            raise DistributionPreviewError("--pin must use skill=version")
        skill, raw_version = raw_item.split("=", 1)
        skill = skill.strip()
        if not skill:
            raise DistributionPreviewError("--pin skill name must be non-empty")
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise DistributionPreviewError("--pin version must be an integer") from exc
        pins[skill] = version
    return pins


def _handle_focus(args: argparse.Namespace, root_dir: Path) -> int:
    cid = args.client_id or ""
    # CLI-consistency (WG-003): fall back to --client for parity with
    # up/status/logs/doctor when no positional client_id was provided.
    if not cid:
        client_flags = getattr(args, "client", []) or []
        if client_flags:
            cid = str(client_flags[0]).strip()
    if not cid and not args.resume:
        message = "focus requires a client_id or --resume."
        if args.format == "json":
            emit_json(classify_error(RuntimeError(message), "focus"))
        else:
            print(message, file=sys.stderr)
        return EXIT_ERROR
    return run_focus(
        root_dir=root_dir,
        client_id=cid,
        profiles=args.profile,
        service_filter=getattr(args, "service", []),
        resume=args.resume,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        fmt=args.format,
        context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
    )


def _handle_stewardship_report(args: argparse.Namespace, root_dir: Path) -> int:
    cid = args.client_id or ""
    if not cid:
        client_flags = getattr(args, "client", []) or []
        if client_flags:
            cid = str(client_flags[0]).strip()
    if not cid:
        print("stewardship-report requires a client_id or --client.", file=sys.stderr)
        return EXIT_ERROR
    return run_stewardship_report(
        root_dir=root_dir,
        client_id=cid,
        profiles=args.profile,
        fmt=args.format,
        write=bool(args.write),
        output_dir_arg=args.output_dir,
    )


def _handle_session_start(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = start_client_session(
            root_dir,
            args.client_id,
            label=args.label,
            cwd=args.cwd,
            goal=args.goal,
            actor=args.actor,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-start"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
        if session.get("label"):
            print(f"label: {session['label']}")
    return EXIT_OK


def _handle_session_event(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        detail = {"actor": args.actor} if args.actor else None
        payload = append_client_session_event(
            root_dir,
            args.client_id,
            args.session_id,
            event_type=args.event_type,
            message=args.message,
            detail=detail,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-event"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"last_event: {session.get('last_event_type', '-')}")
        if session.get("last_message"):
            print(f"message: {session['last_message']}")
    return EXIT_OK


def _handle_session_end(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = end_client_session(
            root_dir,
            args.client_id,
            args.session_id,
            final_status=args.status,
            summary=args.summary,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-end"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
    return EXIT_OK


def _handle_session_resume(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = resume_client_session(
            root_dir,
            args.client_id,
            args.session_id,
            actor=args.actor,
            message=args.message,
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-resume"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        session = payload["session"]
        print(f"client: {payload['client_id']}")
        print(f"session: {session['session_id']}")
        print(f"status: {session['status']}")
    return EXIT_OK


def _handle_session_status(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload = session_status_payload(
            root_dir,
            args.client_id,
            session_id=args.session_id,
            limit=max(0, int(args.limit)),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "session-status"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        if payload.get("session"):
            session = payload["session"]
            print(f"client: {payload['client_id']}")
            print(f"session: {session['session_id']}")
            print(f"status: {session['status']}")
            print(f"events: {len(session.get('recent_events') or [])}")
        else:
            print(f"client: {payload['client_id']}")
            print(f"sessions: {payload['count']}")
            for session in payload.get("sessions") or []:
                label = session.get("label") or "-"
                print(f"  - {session['session_id']}: {session.get('status', 'unknown')} {label}")
    return EXIT_OK


def _handle_worker_submit(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: create_worker_run(
            root_dir,
            task_class=args.task_class,
            instruction=args.instruction,
            client_id=args.client,
            cwd=args.cwd,
            repo_hint=args.repo_hint,
            runtime=args.runtime,
            artifact_policy=args.artifact_policy,
            write_scope=args.write_scope,
            memory_scope=args.memory_scope,
            harness_session_ref=args.harness_session_ref,
        ),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_submit_text)


def _emit_worker_payload_or_error(
    args: argparse.Namespace,
    operation: Callable[[], dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    try:
        return EXIT_OK, operation()
    except WorkerRuntimeError as exc:
        return EXIT_ERROR, worker_runtime_error_payload(exc)
    except RuntimeError as exc:
        return EXIT_ERROR, classify_error(exc, str(getattr(args, "command", "worker")))


def _emit_worker_cli_payload(
    args: argparse.Namespace,
    exit_code: int,
    payload: dict[str, Any],
    text_lines: Callable[[dict[str, Any]], list[str]],
) -> int:
    if args.format == "json":
        emit_json(payload)
    elif exit_code != EXIT_OK:
        print(payload["error"]["message"], file=sys.stderr)
    else:
        print("\n".join(text_lines(payload)))
    return exit_code


def _worker_submit_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"worker run: {payload['run_id']}",
        f"state: {payload['state']}",
        f"runtime: {payload['runtime']}",
        f"context: {payload['context_state']}",
    ]


def _worker_status_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"worker run: {payload['run_id']}",
        f"state: {payload['state']}",
        f"runtime: {payload['runtime']}",
    ]


def _worker_artifacts_text(payload: dict[str, Any]) -> list[str]:
    result = payload.get("result") or {}
    lines = [
        f"worker run: {payload['run_id']}",
        f"artifacts: {len(payload.get('artifacts') or [])}",
    ]
    if result:
        lines.append(f"summary: {result.get('summary') or '-'}")
    return lines


def _worker_promote_text(payload: dict[str, Any]) -> list[str]:
    return [
        f"proposal: {payload['proposal_id']}",
        f"status: {payload['status']}",
        f"target: {payload['target_kind']} {payload['target_location']}",
    ]


def _handle_worker_status(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: worker_status_payload(root_dir, args.run_id),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_status_text)


def _handle_worker_artifacts(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: worker_artifacts_payload(root_dir, args.run_id),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_artifacts_text)


def _handle_worker_promote_learning(args: argparse.Namespace, root_dir: Path) -> int:
    exit_code, payload = _emit_worker_payload_or_error(
        args,
        lambda: promote_worker_learning(
            root_dir,
            proposal_id=args.proposal_id,
            approved_by=args.approved_by,
            target_kind=args.target_kind,
            target_location=args.target_location,
            promotion_mode=args.promotion_mode,
        ),
    )
    return _emit_worker_cli_payload(args, exit_code, payload, _worker_promote_text)


def _handle_swimmers_launch(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        exit_code, payload = launch_swimmers_batch(
            positional_dirs=list(getattr(args, "dirs", []) or []),
            dir_flags=list(getattr(args, "dir_flags", []) or []),
            cwd_flags=list(getattr(args, "cwd_flags", []) or []),
            dirs_file=getattr(args, "dirs_file", None),
            group=getattr(args, "group", None),
            group_path=getattr(args, "group_path", None),
            managed_only=bool(getattr(args, "managed_only", False)),
            request=getattr(args, "request", None),
            request_file=getattr(args, "request_file", None),
            tool=str(getattr(args, "tool", "codex") or "codex"),
            launch_target=getattr(args, "target", None),
            base_url=getattr(args, "base_url", None),
            auth_token_env=getattr(args, "auth_token_env", None),
            invoke_cwd=getattr(args, "invoke_cwd", None),
            timeout=float(getattr(args, "timeout", 30.0) or 30.0),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except RuntimeError as exc:
        if args.format == "json":
            emit_json(classify_error(exc, "swimmers-launch"))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(swimmers_launch_text_lines(payload)))
    return exit_code


def _handle_mmdx(args: argparse.Namespace, root_dir: Path) -> int:
    try:
        payload, exit_code = mmdx_open_payload(
            root_dir=root_dir,
            cwd=Path(args.cwd) if args.cwd else None,
            query_parts=getattr(args, "query", []) or [],
            search_roots=getattr(args, "search_root", []) or [],
            open_file=bool(getattr(args, "open", True)),
            limit=max(1, int(getattr(args, "limit", MMDX_DEFAULT_LIMIT))),
            tmux=bool(getattr(args, "tmux", False)),
            tmux_submit=bool(getattr(args, "tmux_submit", False)),
            allow_parser_install=bool(getattr(args, "allow_parser_install", False)),
        )
    except RuntimeError as exc:
        payload = mmdx_error_payload(exc)
        if args.format == "json":
            emit_json(payload)
        else:
            print_mmdx_payload_text(payload)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        print_mmdx_payload_text(payload)
    return exit_code


def _compact_registry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact_entries: list[dict[str, Any]] = []
    for entry in payload.get("capabilities") or []:
        compact_entry = {
            key: entry[key]
            for key in (
                "id",
                "tier",
                "surface",
                "summary",
                "side_effect",
                "risk",
                "entrypoint",
                "mcp_tool",
            )
            if key in entry
        }
        compact_entries.append(compact_entry)
    return {
        "abi_version": payload.get("abi_version"),
        "counts": payload.get("counts") or {},
        "capabilities": compact_entries,
    }


def _capabilities_payload(root_dir: Path, *, compact: bool = False) -> dict[str, Any]:
    start = timer_start()
    commands = [
        {
            "name": name,
            "json": True,
            "safe_first_try": _safe_first_try_command(name),
        }
        for name in sorted(MANAGE_COMMAND_NAMES)
    ]
    registry = registry_payload()
    if compact:
        registry = _compact_registry_payload(registry)
    payload = {
        "ok": True,
        "tool": "skillbox-manage",
        "contract_version": "2026-05-09",
        "root_dir": str(root_dir),
        "entrypoints": [
            "python3 .env-manager/manage.py",
            "python3 scripts/04-reconcile.py",
            "python3 scripts/box.py",
            "scripts/sbp",
            "scripts/sbo",
        ],
        "commands": commands,
        "registry": registry,
        "agent_surfaces": {
            "capabilities": "python3 .env-manager/manage.py capabilities --json",
            "robot_docs": "python3 .env-manager/manage.py robot-docs guide",
            "robot_triage": "python3 .env-manager/manage.py --robot-triage",
            "json_aliases": sorted(JSON_FLAG_ALIASES),
            "outer_reconcile": "python3 scripts/04-reconcile.py capabilities --json",
            "box_lifecycle": "python3 scripts/box.py capabilities --json",
            "wrappers": ["scripts/sbp capabilities --json", "scripts/sbo capabilities --json"],
        },
        "safe_previews": [
            "python3 .env-manager/manage.py client-init --list-blueprints --format json",
            "python3 .env-manager/manage.py client-project <client> --dry-run --format json",
            "python3 .env-manager/manage.py client-diff <client> --target-dir <control-plane-repo> --format json",
            (
                "python3 .env-manager/manage.py distribution-preview --manifest-path <manifest.json> "
                "--public-key <public-key.pem> --format json"
            ),
            "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
            "python3 .env-manager/manage.py pressure-report --format json",
            "python3 .env-manager/manage.py rch-report --format json",
            "python3 .env-manager/manage.py rch-stage --dry-run --format json -- cargo check",
            "python3 .env-manager/manage.py sbh-report --format json",
            "python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json",
        ],
        "exit_codes": {
            "0": "success",
            "1": "user input, runtime, or environment error",
            "2": "drift detected or argparse usage error, depending on command surface",
            "3": "operator input required",
        },
        "env": {
            "SKILLBOX_STATE_ROOT": "Persistent runtime state root.",
            "SKILLBOX_MONOSERVER_ROOT": "Host or container repo universe root.",
            "SKILLBOX_CLIENTS_ROOT": "Runtime client overlay root.",
            "SKILLBOX_RCH_BIN": "Optional RCH CLI path for build-offload checks.",
            "SKILLBOX_SBH_BIN": "Optional SBH CLI path for storage-guard checks.",
            "NO_COLOR": "Suppress ANSI styling in supported surfaces.",
            "CI": "Prefer non-interactive behavior in supported surfaces.",
        },
        "next_actions": [
            "python3 .env-manager/manage.py next --format json",
            "python3 .env-manager/manage.py graph --format json --no-adapters",
            "python3 .env-manager/manage.py status --format json",
            "python3 .env-manager/manage.py doctor --format json",
            "python3 .env-manager/manage.py robot-docs guide",
        ],
    }
    return attach_elapsed(payload, start)


def _safe_first_try_command(name: str) -> str:
    if name in {"capabilities", "robot-triage"}:
        return f"manage.py {name} --json"
    if name == "robot-docs":
        return "manage.py robot-docs guide"
    if name in {"next", "graph"}:
        return f"manage.py {name} --format json --no-adapters"
    if name == "search":
        return "manage.py search graph --format json --no-adapters"
    if name == "explain":
        return "manage.py explain brain.next --format json --no-adapters"
    if name == "snap":
        return "manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json"
    if name in {"client-init"}:
        return "manage.py client-init --list-blueprints --format json"
    if name in {"client-project"}:
        return "manage.py client-project <client> --dry-run --format json"
    if name in {"client-diff"}:
        return "manage.py client-diff <client> --target-dir <control-plane-repo> --format json"
    if name in {"client-publish"}:
        return "manage.py client-diff <client> --target-dir <control-plane-repo> --format json"
    if name in {"client-open"}:
        return "manage.py client-project <client> --dry-run --format json"
    if name in {"distribution-preview"}:
        return "manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json"
    if name in {"distribution-publish"}:
        return "manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json"
    if name in {"distribution-rollback"}:
        return "manage.py distribution-rollback --list --skill <skill> --format json"
    if name in {"status", "render", "doctor", "skills", "skill-audit", "mcp-audit"}:
        return f"manage.py {name} --format json"
    if name == "pressure-report":
        return "manage.py pressure-report --format json"
    if name == "rch-report":
        return "manage.py rch-report --format json"
    if name == "rch-stage":
        return "manage.py rch-stage --dry-run --format json -- cargo check"
    if name == "sbh-report":
        return "manage.py sbh-report --format json"
    if name == "parity-report":
        return "manage.py parity-report <client> --format json"
    if name == "swimmers-launch":
        return "manage.py swimmers-launch <dir> <dir> --request '<prompt>' --dry-run --format json"
    if name in {"up", "down", "restart", "sync", "bootstrap", "context"}:
        return f"manage.py {name} --dry-run --format json"
    return f"manage.py {name} --help"


def _robot_docs_guide() -> str:
    return """Skillbox agent guide

Primary entrypoint:
  python3 .env-manager/manage.py <command> [options]

Start here:
  python3 .env-manager/manage.py capabilities --json
  python3 .env-manager/manage.py next --format json
  python3 .env-manager/manage.py status --format json
  python3 .env-manager/manage.py doctor --format json
  python3 .env-manager/manage.py --robot-triage

Structured output:
  Most read-side and runtime commands accept --format json.
  Agent-friendly aliases are accepted: --json, --jason, --jsno, --jsson.
  Diagnostics and typo-alias notices are printed to stderr, not stdout.

Safe mutation pattern:
  Preview first when available: sync --dry-run, bootstrap --dry-run,
  up --dry-run, down --dry-run, restart --dry-run, context --dry-run.
  For skill lifecycle removals or pruning, use --dry-run before --yes.

Useful command families:
  next --format json            Rank explainable next actions from evidence.
  graph --format json           Inspect the agent operations graph.
  explain <node> --format json  Explain graph nodes, Beads, tools, and commands.
  search <query> --format json  Search commands, graph nodes, docs, Beads, evidence.
  snap replay <file>            Replay redacted snapshot fixtures without live services.
  status --format json          Compact runtime state.
  doctor --format json          Runtime validation checks.
  pressure-report --format json Disk pressure and RCH/SBH posture.
  rch-report --format json      RCH worker/hook readiness without mutation.
  rch-stage --dry-run -- ...    Plan a no-sudo RCH staging lane.
  sbh-report --format json      SBH observe-first storage guard posture.
  skills --issues-only          Skill visibility issues for the current cwd.
  cass-evidence --format json   Skill invocations per repo from Cass (sbp evidence).
  mcp-audit --format json       Claude/Codex MCP parity audit.
  parity-report <client>        Dev/prod parity report for a client.
  swimmers-launch <dirs...>     Launch one Swimmers agent session per dir.
  focus <client> --format json  Sync, bootstrap, start, collect live state, context.

Pressure/offload rule:
  The approved non-production worker target is worker-devbox.
  Excluded targets are prod, production, and primary-prod.
  Protected paths like ~/.codex, ~/.claude, and ~/.ssh are hard no-touch.
  Use pressure-report, rch-report, and sbh-report before expensive builds or cleanup.
  Use rch-stage --dry-run before any staged remote build; it strips remote delete flags by default.
  Do not install RCH hooks, run SBH cleanup, mutate ballast, or touch production boxes without explicit approval.
"""


def _handle_capabilities(args: argparse.Namespace, root_dir: Path) -> int:
    emit_json(_capabilities_payload(root_dir, compact=bool(getattr(args, "compact", False))))
    return EXIT_OK


def _handle_robot_docs(args: argparse.Namespace, root_dir: Path) -> int:
    guide = _robot_docs_guide()
    if args.format == "json":
        emit_json({"ok": True, "topic": args.topic, "guide": guide})
    else:
        print(guide.rstrip())
    return EXIT_OK


def _handle_robot_triage(args: argparse.Namespace, root_dir: Path) -> int:
    payload: dict[str, Any] = {
        "ok": True,
        "tool": "skillbox-manage",
        "quick_ref": _capabilities_payload(root_dir)["next_actions"],
        "recommendations": [
            {
                "id": "start-next",
                "command": "python3 .env-manager/manage.py next --format json",
                "why": "Evidence-driven ranked next actions with reasons, commands, validations, and blockers.",
            },
            {
                "id": "start-status",
                "command": "python3 .env-manager/manage.py status --format json",
                "why": "Fastest non-mutating runtime inspection.",
            },
            {
                "id": "validate-runtime",
                "command": "python3 .env-manager/manage.py doctor --format json",
                "why": "Find graph, filesystem, and skill-integrity drift.",
            },
            {
                "id": "inspect-pressure",
                "command": "python3 .env-manager/manage.py pressure-report --format json",
                "why": "Read-only disk pressure, protected buckets, and RCH/SBH posture.",
            },
            {
                "id": "inspect-rch",
                "command": "python3 .env-manager/manage.py rch-report --format json",
                "why": "Read-only RCH worker, check, status, and hook posture.",
            },
            {
                "id": "inspect-sbh",
                "command": "python3 .env-manager/manage.py sbh-report --format json",
                "why": "Read-only SBH doctor, status, stats, blame, and mutation-gate posture.",
            },
            {
                "id": "preview-before-mutation",
                "command": "python3 .env-manager/manage.py up --dry-run --format json",
                "why": "Preview service graph actions before starting services.",
            },
        ],
        "commands": {
            "capabilities": "python3 .env-manager/manage.py capabilities --json",
            "guide": "python3 .env-manager/manage.py robot-docs guide",
            "status": "python3 .env-manager/manage.py status --format json",
            "doctor": "python3 .env-manager/manage.py doctor --format json",
            "pressure-report": "python3 .env-manager/manage.py pressure-report --format json",
            "rch-report": "python3 .env-manager/manage.py rch-report --format json",
            "rch-stage": "python3 .env-manager/manage.py rch-stage --dry-run --format json -- cargo check",
            "sbh-report": "python3 .env-manager/manage.py sbh-report --format json",
        },
    }
    try:
        model = build_runtime_model(root_dir)
        payload["health"] = {
            "model_loaded": True,
            "repos": len(model.get("repos") or []),
            "services": len(model.get("services") or []),
            "checks": len(model.get("checks") or []),
        }
    except Exception as exc:
        payload["ok"] = False
        payload["health"] = {
            "model_loaded": False,
            "error": str(exc),
            "next_action": "python3 .env-manager/manage.py doctor --format json",
        }
    emit_json(payload)
    return EXIT_OK if payload["ok"] else EXIT_ERROR


def _handle_pressure_report(args: argparse.Namespace, root_dir: Path) -> int:
    payload = collect_pressure_report(
        root_dir,
        home=Path(args.home) if getattr(args, "home", None) else None,
        target_box=str(getattr(args, "target_box", DEFAULT_TARGET_BOX) or DEFAULT_TARGET_BOX),
        scan_candidate_sizes=bool(getattr(args, "scan_candidate_sizes", False)),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(pressure_report_text_lines(payload)))
    return EXIT_OK


def _handle_rch_report(args: argparse.Namespace, root_dir: Path) -> int:
    payload = collect_rch_report(
        root_dir,
        binary=getattr(args, "binary", None),
        run_probes=not bool(getattr(args, "no_probes", False)),
        timeout_seconds=max(0.1, float(getattr(args, "timeout", 5.0) or 5.0)),
        target_box=str(getattr(args, "target_box", DEFAULT_TARGET_BOX) or DEFAULT_TARGET_BOX),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(rch_report_text_lines(payload)))
    return EXIT_OK


def _handle_rch_stage(args: argparse.Namespace, root_dir: Path) -> int:
    command_parts = list(getattr(args, "stage_command", []) or [])
    plan = build_rch_stage_plan(
        root_dir,
        source=Path(args.source) if getattr(args, "source", None) else None,
        stage_root=Path(args.stage_root) if getattr(args, "stage_root", None) else None,
        stage_id=getattr(args, "stage_id", None),
        command_parts=command_parts,
        target_box=str(getattr(args, "target_box", DEFAULT_TARGET_BOX) or DEFAULT_TARGET_BOX),
        remote_root=str(getattr(args, "remote_root", DEFAULT_ADAPTER_REMOTE_ROOT) or DEFAULT_ADAPTER_REMOTE_ROOT),
        rch_binary=getattr(args, "rch_binary", None),
        real_ssh=getattr(args, "real_ssh", None),
        real_rsync=getattr(args, "real_rsync", None),
        rch_home=Path(args.rch_home) if getattr(args, "rch_home", None) else None,
        xdg_state_home=Path(args.xdg_state_home) if getattr(args, "xdg_state_home", None) else None,
    )
    run_requested = bool(getattr(args, "run", False))
    prepare_requested = bool(getattr(args, "prepare", False))
    if run_requested and not plan["command"]["argv"]:
        plan.update(
            {
                "ok": False,
                "mode": "error",
                "error": {
                    "type": "missing_command",
                    "message": "rch-stage --run requires a command after --, for example: -- cargo check",
                },
            }
        )
        if args.format == "json":
            emit_json(plan)
        else:
            print(plan["error"]["message"], file=sys.stderr)
        return EXIT_ERROR

    if run_requested or prepare_requested:
        plan["mode"] = "run" if run_requested else "prepare"
        plan["mutates"] = True
        plan["remote_writes"] = run_requested
        try:
            prepare_result = prepare_rch_stage(
                plan,
                copy_source=not bool(getattr(args, "no_copy", False)),
                write_manifest=True,
            )
            plan["prepare_result"] = prepare_result
            if run_requested:
                result = execute_rch_stage(plan, timeout_seconds=max(1.0, float(getattr(args, "timeout", 1800.0))))
                plan["result"] = result
                write_stage_manifest(plan, result=result)
                plan["ok"] = bool(result.get("ok"))
            else:
                plan["result"] = prepare_result
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            plan["ok"] = False
            plan["error"] = {"type": exc.__class__.__name__, "message": str(exc)}
    else:
        plan["mode"] = "dry_run"

    if args.format == "json":
        emit_json(plan)
    else:
        print("\n".join(rch_stage_text_lines(plan)))
    return EXIT_OK if plan.get("ok") else EXIT_ERROR


def _handle_sbh_report(args: argparse.Namespace, root_dir: Path) -> int:
    payload = collect_sbh_report(
        root_dir,
        home=Path(args.home) if getattr(args, "home", None) else None,
        binary=getattr(args, "binary", None),
        run_probes=not bool(getattr(args, "no_probes", False)),
        timeout_seconds=max(0.1, float(getattr(args, "timeout", 5.0) or 5.0)),
        decision_id=getattr(args, "decision_id", None),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(sbh_report_text_lines(payload)))
    return EXIT_OK


def _operator_booking_config_lines(payload: dict[str, Any]) -> list[str]:
    config = payload.get("operator_booking") or {}
    return [
        f"operator booking: {config.get('client_id') or '-'}",
        f"availability: {config.get('availability_url') or '-'}",
        f"book hold: {config.get('booking_hold_url') or '-'}",
        f"magic link: {config.get('magic_link_url') or '-'}",
        f"publishable key: {config.get('api_key_env') or '-'} configured={config.get('api_key_configured')}",
        f"access token: {config.get('access_token_env') or '-'} configured={config.get('access_token_configured')}",
    ]


def _operator_booking_availability_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"operator booking: {payload.get('client_id') or '-'}",
        f"booking url: {payload.get('booking_url') or '-'}",
        f"timezone: {payload.get('timezone') or '-'}",
        f"available slots: {payload.get('available', 0)}",
    ]
    for slot in payload.get("slots") or []:
        price = slot.get("price")
        price_text = f"${price}" if price is not None else "-"
        lines.append(f"  - {slot.get('date')} {slot.get('slot')} {price_text}")
    return lines


def _operator_booking_book_lines(payload: dict[str, Any]) -> list[str]:
    if payload.get("dry_run"):
        lines = ["operator booking dry run", f"book hold: {payload.get('booking_url')}"]
        if payload.get("magic_link_url"):
            lines.append(f"magic link: {payload.get('magic_link_url')}")
        return lines

    booking = payload.get("booking") or {}
    magic_link = payload.get("magic_link")
    lines = [
        f"booking id: {booking.get('bookingId') or '-'}",
        f"resource: {booking.get('resourceKey') or '-'}",
        f"action: {booking.get('actionKey') or '-'}",
        f"price: {booking.get('priceDisplay') or booking.get('price') or '-'}",
    ]
    if magic_link:
        lines.append(f"magic link sent: {magic_link.get('email') or '-'}")
    lines.extend(f"next: {next_action}" for next_action in payload.get("next_actions") or [])
    return lines


def _operator_booking_text_lines(payload: dict[str, Any]) -> list[str]:
    action = payload.get("action")
    if action == "config":
        return _operator_booking_config_lines(payload)
    if action == "availability":
        return _operator_booking_availability_lines(payload)
    if action == "book":
        return _operator_booking_book_lines(payload)
    return [str(payload)]


def _print_operator_booking_text(payload: dict[str, Any]) -> None:
    print("\n".join(_operator_booking_text_lines(payload)))


def _handle_operator_booking(
    args: argparse.Namespace,
    root_dir: Path,
    model: dict[str, Any],
    resolved_mode: str,
) -> int:
    try:
        payload, exit_code = operator_booking_payload(
            model,
            action=str(getattr(args, "action", "availability") or "availability"),
            client_id=_primary_client_id(args, model, default=""),
            date=getattr(args, "date", None),
            slot=getattr(args, "slot", None),
            email=getattr(args, "email", None),
            name=getattr(args, "name", None),
            redirect_url=getattr(args, "redirect_url", None),
            origin=getattr(args, "origin", None),
            send_magic_link=bool(getattr(args, "send_magic_link", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            limit=int(getattr(args, "limit", 8) or 8),
            access_token_env=getattr(args, "access_token_env", None),
        )
    except RuntimeError as exc:
        payload = operator_booking_error_payload(exc)
        if args.format == "json":
            emit_json(payload)
        else:
            print(payload["error"]["message"], file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        emit_json(payload)
    else:
        _print_operator_booking_text(payload)
    return exit_code


def _handle_structure_doctor(args: argparse.Namespace, root_dir: Path) -> int:
    """`sbp doctor` — the structural verification front door.

    Read-only: every gate is a lint/audit/subprocess that does not mutate state,
    so this runs as an early-dispatch handler (no runtime-model prefilter). Exits
    nonzero ONLY when a gate is FAIL; INCO and PASS both exit 0.
    """
    cwd = Path(getattr(args, "cwd", None) or os.getcwd())
    payload = run_structure_doctor(runtime_root=root_dir, cwd=cwd)
    if args.format == "json":
        emit_json(payload)
    else:
        for line in structure_doctor_text_lines(payload):
            print(line)
    return int(payload.get("exit_code", 0))


def _load_sbp_evidence_module():
    """Best-effort import of the skillbox-config evidence backend.

    Returns the loaded module or None. NEVER raises: a missing/unimportable
    backend simply means candidates carry no evidence (graceful degradation).
    """
    config_root = Path(
        os.environ.get("SKILLBOX_CONFIG_ROOT")
        or (Path(os.path.expanduser("~")) / "repos" / "skillbox-config")
    )
    helper = config_root / "scripts" / "sbp_evidence.py"
    if not helper.is_file():
        return None
    try:
        import importlib.util

        scripts_dir = str(helper.parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        spec = importlib.util.spec_from_file_location("sbp_evidence", helper)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _skill_evidence_provider(cwd: str | None) -> Callable[[], dict[str, Any] | None] | None:
    """Build the OPTIONAL per-candidate evidence provider for ``cwd``.

    Returns a zero-arg callable that yields a per-skill evidence index for the
    repo at ``cwd`` (from the Cass-backed evidence backend), or None when the
    backend is unavailable. The callable itself NEVER raises and returns None on
    any Cass/front-door failure, so candidate rows simply carry no evidence and
    no recalibrate is ever blocked.
    """
    module = _load_sbp_evidence_module()
    if module is None or not hasattr(module, "skill_evidence_index"):
        return None
    repo_path = str(Path(cwd or os.getcwd()).resolve())

    def _provider() -> dict[str, Any] | None:
        try:
            result = module.skill_evidence_index(repo_path=repo_path)
        except Exception:
            return None
        if not isinstance(result, dict) or not result.get("cass_available"):
            return None
        index = result.get("index")
        return index if isinstance(index, dict) else None

    return _provider


def _handle_cass_evidence(args: argparse.Namespace, root_dir: Path) -> int:
    """Delegate to the skillbox-config Cass evidence helper.

    Measures skill INVOCATIONS per repo from the Cass index (contamination-safe
    structural detection) joined against current skill-visibility policy. The
    helper lives beside the Cass front door (``sbp_cass.py``) in skillbox-config
    and degrades INCO-style when Cass is unreachable, so this never blocks.
    """
    config_root = Path(
        os.environ.get("SKILLBOX_CONFIG_ROOT")
        or (Path(os.path.expanduser("~")) / "repos" / "skillbox-config")
    )
    helper = config_root / "scripts" / "sbp_evidence.py"
    if not helper.is_file():
        emit_json(
            {
                "command": "evidence",
                "ok": False,
                "status": "evidence_helper_missing",
                "expected_path": str(helper),
                "note": "Set SKILLBOX_CONFIG_ROOT or install skillbox-config to enable sbp evidence.",
            }
        )
        return EXIT_ERROR
    cmd = [sys.executable, str(helper), "--format", str(getattr(args, "format", "json") or "json")]
    if getattr(args, "proposals", False):
        cmd += ["--proposals"]
    if getattr(args, "repo", None):
        cmd += ["--repo", str(args.repo)]
    if getattr(args, "skill", None):
        cmd += ["--skill", str(args.skill)]
    if getattr(args, "limit", None) is not None:
        cmd += ["--limit", str(args.limit)]
    if getattr(args, "timeout_seconds", None) is not None:
        cmd += ["--timeout-seconds", str(args.timeout_seconds)]
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


_EARLY_DISPATCH: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "cass-evidence": _handle_cass_evidence,
    "capabilities": _handle_capabilities,
    "robot-docs": _handle_robot_docs,
    "robot-triage": _handle_robot_triage,
    "pressure-report": _handle_pressure_report,
    "rch-report": _handle_rch_report,
    "rch-stage": _handle_rch_stage,
    "sbh-report": _handle_sbh_report,
    "client-init": _handle_client_init,
    "onboard": _handle_onboard,
    "first-box": _handle_first_box,
    "forge": _handle_forge,
    "private-init": _handle_private_init,
    "acceptance": _handle_acceptance,
    "client-project": _handle_client_project,
    "client-open": _handle_client_open,
    "client-publish": _handle_client_publish,
    "client-diff": _handle_client_diff,
    "distribution-publish": _handle_distribution_publish,
    "distribution-preview": _handle_distribution_preview,
    "distribution-rollback": _handle_distribution_rollback,
    "focus": _handle_focus,
    "stewardship-report": _handle_stewardship_report,
    "session-start": _handle_session_start,
    "session-event": _handle_session_event,
    "session-end": _handle_session_end,
    "session-resume": _handle_session_resume,
    "session-status": _handle_session_status,
    "worker-submit": _handle_worker_submit,
    "worker-status": _handle_worker_status,
    "worker-artifacts": _handle_worker_artifacts,
    "worker-promote-learning": _handle_worker_promote_learning,
    "swimmers-launch": _handle_swimmers_launch,
    "mmdx": _handle_mmdx,
    "structure-doctor": _handle_structure_doctor,
}


def _handle_render(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    if args.format == "json":
        emit_json(model)
    else:
        print_render_text(model)
    return EXIT_OK


def _handle_sync(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    actions = sync_runtime(model, dry_run=args.dry_run)
    actions.extend(
        sync_context(
            model,
            root_dir,
            dry_run=args.dry_run,
            context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
        )
    )
    if args.format == "json":
        emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_sync()})
    else:
        print("\n".join(actions))
    return EXIT_OK


def _handle_context(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    actions = sync_context(
        model,
        root_dir,
        dry_run=args.dry_run,
        context_dir=resolve_context_dir(root_dir, getattr(args, "context_dir", None)),
    )
    if args.format == "json":
        emit_json({"actions": actions, "dry_run": args.dry_run, "next_actions": next_actions_for_context()})
    else:
        print("\n".join(actions))
    return EXIT_OK


def _handle_ports(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    model.setdefault("active_profiles", normalize_active_profiles(getattr(args, "profile", [])))
    model.setdefault("active_clients", _active_clients_for_args(args, model))
    payload = port_registry_payload(model, resolve=getattr(args, "resolve", None))
    if args.format == "json":
        emit_json(payload)
    else:
        print("\n".join(port_registry_text_lines(payload)))
    return EXIT_OK


def _handle_doctor(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    results = doctor_results(model, root_dir)
    has_fail = any(result.status == "fail" for result in results)
    if args.format == "json":
        emit_json({
            "checks": [asdict(result) for result in results],
            "next_actions": next_actions_for_doctor(results),
        })
    else:
        print_doctor_text(results)
    if has_fail:
        return EXIT_DRIFT
    return EXIT_OK


def _handle_status(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    status_payload = runtime_status(model)
    if args.format == "json":
        status_payload["next_actions"] = next_actions_for_status(status_payload)
        emit_json(compact_runtime_status(status_payload) if args.compact else status_payload)
    else:
        print_status_text(status_payload)
    return EXIT_OK


def _handle_skills(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    # Attach OPTIONAL per-candidate evidence only in candidates mode
    # (`--show-sources`, i.e. `sbp candidates`), so a plain `sbp skills` never
    # pays the Cass round-trip. The provider degrades to None when Cass is down,
    # leaving candidate rows with no evidence rather than blocking.
    evidence_provider = (
        _skill_evidence_provider(args.cwd) if getattr(args, "show_sources", False) else None
    )
    payload = collect_skill_visibility(
        model,
        cwd=args.cwd,
        include_global=not args.no_global,
        include_project=not args.no_project,
        include_sources=args.show_sources,
        evidence_provider=evidence_provider,
    )
    if args.format == "json":
        emit_json(payload if args.full else compact_skill_visibility_payload(payload))
    else:
        print_skill_visibility_text(
            payload,
            full=args.full,
            show_shadowed=args.show_shadowed,
            issues_only=args.issues_only,
            limit=max(0, int(args.limit)),
        )
    return EXIT_OK


def _handle_skill_audit(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    payload = collect_skill_audit(
        model,
        cwd=args.cwd,
        scan_roots=getattr(args, "scan_root", None),
        max_depth=max(0, int(args.max_depth)),
        include_global=not bool(args.no_global),
        include_clean=bool(getattr(args, "all", False)),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_skill_audit_text(payload, limit=max(0, int(args.limit)))
    return EXIT_OK


def _full_declared_mcp_servers(root_dir: Path) -> list[str] | None:
    """Names of every kind:mcp service declared in the unfiltered model.

    The dispatched model is profile-filtered, which would make profile-gated MCP
    services (memory/connectors) look like drift. Returns None (expected-only
    baseline) when the full model cannot be built.
    """
    try:
        full_model = build_runtime_model(root_dir)
    except RuntimeError:
        return None
    return [str(item["name"]) for item in requested_mcp_servers(full_model) if item.get("name")]


def _handle_mcp_audit(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    payload = collect_mcp_audit(
        root_dir,
        model,
        cwd=args.cwd,
        config_root=getattr(args, "config_root", None),
        declared_servers=_full_declared_mcp_servers(root_dir),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_mcp_audit_text(payload, root_dir=root_dir)
    return EXIT_OK


def _handle_mcp(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    action = str(getattr(args, "mcp_action", "") or "")
    if action == "sync":
        return _handle_mcp_sync(args, root_dir, model, resolved_mode)
    raise RuntimeError(f"unknown mcp action: {action!r}")


def _handle_fleet(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    action = str(getattr(args, "fleet_action", "") or "")
    if action == "converge":
        return _handle_fleet_converge(args, root_dir, model, resolved_mode)
    if action == "relink":
        return _handle_fleet_relink(args, root_dir, model, resolved_mode)
    raise RuntimeError(f"unknown fleet action: {action!r}")


def _handle_fleet_converge(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    # PLAN ONLY: --dry-run is the default and only mode. The MCP parity pass
    # uses the FULL (unfiltered) declared-server baseline so profile-gated MCP
    # services (memory/connectors) are not mistaken for drift, mirroring the
    # mcp-audit handler.
    plan = build_fleet_converge_plan(
        model,
        cwd=args.cwd,
        scan_roots=getattr(args, "scan_root", None),
        max_depth=max(0, int(args.max_depth)),
        include_clean=bool(getattr(args, "all", False)),
        include_mcp=not bool(getattr(args, "no_mcp", False)),
        root_dir=root_dir,
        declared_servers=_full_declared_mcp_servers(root_dir),
    )
    if args.format == "json":
        emit_json(plan)
    else:
        for line in fleet_converge_text_lines(plan, limit=max(0, int(args.limit))):
            print(line)
    return EXIT_OK


def _handle_fleet_relink(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del root_dir, resolved_mode
    # --dry-run is the DEFAULT and is symmetric with apply: the plan an apply
    # executes is byte-for-byte the plan a dry-run prints. --yes (args.apply) is
    # the only thing that writes, and it executes ONLY the rewrite actions the
    # plan already enumerates (reclassify links are left for converge).
    apply = bool(getattr(args, "apply", False))
    plan = build_relink_plan(
        model,
        from_root=getattr(args, "from_root", None),
        to_root=getattr(args, "to_root", None),
        cwd=getattr(args, "cwd", None),
        scan_roots=getattr(args, "scan_root", None),
        max_depth=max(0, int(args.max_depth)),
        include_clean=bool(getattr(args, "all", False)),
        apply=apply,
    )
    applied: dict[str, Any] | None = None
    if apply:
        applied = apply_relink_plan(plan, dry_run=False)

    if args.format == "json":
        payload = dict(plan)
        if applied is not None:
            payload["applied"] = applied
        emit_json(payload)
    else:
        for line in relink_text_lines(plan, limit=max(0, int(args.limit))):
            print(line)
        if applied is not None:
            summary = applied.get("summary") or {}
            print("")
            print(
                f"applied: rewritten={summary.get('rewritten', 0)} "
                f"failed={summary.get('failed', 0)} "
                f"skipped_reclassify={summary.get('skipped_reclassify', 0)}"
            )
    # Surface a root-resolution error as drift so callers/CI can gate on it.
    if (plan.get("roots") or {}).get("error"):
        return EXIT_DRIFT
    return EXIT_OK


def _handle_mcp_sync(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    # --dry-run is the default; --apply is the only thing that writes. The two
    # paths compute the identical plan, so dry-run prints exactly what apply
    # would write.
    apply = bool(getattr(args, "apply", False))
    # Render from the FULL (unfiltered) model so profile-gated MCP services are
    # rendered, matching the audit's full-declared baseline rather than the
    # profile-filtered dispatch model.
    try:
        full_model = build_runtime_model(root_dir)
    except RuntimeError:
        full_model = model
    payload = render_mcp_sync(
        root_dir,
        full_model,
        cwd=getattr(args, "cwd", None),
        config_root=getattr(args, "config_root", None),
        apply=apply,
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_mcp_render_text(payload, root_dir=root_dir)
    changed = bool(payload.get("summary", {}).get("claude_changed")) or bool(
        payload.get("summary", {}).get("codex_changed")
    )
    # In dry-run, surface drift via EXIT_DRIFT so audits/CI can gate on it; an
    # apply that wrote the files exits OK.
    if changed and not apply:
        return EXIT_DRIFT
    return EXIT_OK


def _handle_evidence(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    payload = collect_runtime_evidence(
        root_dir,
        model,
        cwd=args.cwd,
        declared_servers=_full_declared_mcp_servers(root_dir),
    )
    if getattr(args, "write", False):
        payload["artifact"] = _write_runtime_evidence_artifact(root_dir, payload, getattr(args, "run_id", None))
    if args.format == "json":
        emit_json(payload)
    elif args.format == "md":
        print(runtime_evidence_markdown(payload))
    else:
        print_runtime_evidence_text(payload)
    return EXIT_OK


def _write_runtime_evidence_artifact(root_dir: Path, payload: dict[str, Any], run_id: str | None) -> dict[str, str]:
    from datetime import datetime, timezone

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = root_dir / "tests" / "artifacts" / "perf" / run_id / "runtime-evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "evidence.json"
    md_path = out_dir / "evidence.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(runtime_evidence_markdown(payload), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def _brain_adapters_for_args(root_dir: Path, model: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "no_adapters", False)):
        return {}
    payload = collect_agent_adapter_evidence(
        root_dir,
        model=model,
        cwd=getattr(args, "cwd", None),
        ntm_session=getattr(args, "ntm_session", None),
    )
    return payload.get("adapters") or {}


def _brain_graph_payload(model: dict[str, Any], adapters: dict[str, Any]) -> dict[str, Any]:
    return build_agent_graph_payload(model, adapters=adapters)


def _print_next_text(payload: dict[str, Any]) -> None:
    print(
        f"next: {payload['summary']['returned']}/{payload['summary']['recommendation_count']} "
        f"recommendations"
    )
    for item in payload.get("recommendations") or []:
        print(f"- {item['id']} score={item['score']} risk={item['risk']} side_effect={item['side_effect']}")
        for reason in item.get("reasons") or []:
            print(f"  reason: {reason}")
        for command in item.get("commands") or []:
            print(f"  command: {command}")
    if payload.get("disagreements"):
        print("disagreements:")
        for item in payload["disagreements"]:
            print(f"- {item['code']}: {item['message']}")


def _print_explain_text(payload: dict[str, Any]) -> None:
    print_explain_brain_text(payload)


def _print_search_text(payload: dict[str, Any]) -> None:
    print_search_brain_text(payload)


def _print_snap_text(payload: dict[str, Any]) -> None:
    if "error" in payload:
        print(payload["error"]["message"], file=sys.stderr)
        return
    if "snapshot_id" in payload and "inputs" in payload:
        print(f"snapshot: {payload['snapshot_id']} label={payload.get('label')}")
        artifact = payload.get("artifact")
        if artifact:
            print(f"artifact: {artifact}")
        return
    if "changes" in payload:
        print(f"snapshot diff: {payload['change_count']} changes")
        for item in payload.get("changes") or []:
            print(f"- {item['severity']} {item['change']} {item['entity']}")
        return
    if "summary" in payload:
        summary = payload["summary"]
        print(
            f"snapshot replay: {payload.get('snapshot_id')} "
            f"services={summary['services']} graph_nodes={summary['graph_nodes']}"
        )


def _handle_next(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    adapters = _brain_adapters_for_args(root_dir, model, args)
    graph_payload = _brain_graph_payload(model, adapters)
    payload = next_action_payload(
        graph_payload,
        adapters=adapters,
        limit=max(0, int(getattr(args, "limit", 5))),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        _print_next_text(payload)
    return EXIT_OK


def _handle_graph(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    adapters = _brain_adapters_for_args(root_dir, model, args)
    graph_payload = _brain_graph_payload(model, adapters)
    payload = graph_command_payload(
        graph_payload,
        algorithm=getattr(args, "algorithm", None),
        node_id=getattr(args, "node", None),
        source=getattr(args, "source", None),
        target=getattr(args, "target", None),
        blocked_nodes=getattr(args, "blocked_node", None),
    )
    if args.format == "json":
        emit_json(payload)
    elif "error" in payload:
        print_graph_error_text(payload)
        return EXIT_ERROR
    else:
        print(render_graph_payload(payload, args.format))
    return EXIT_ERROR if "error" in payload else EXIT_OK


def _explain_target_is_brain(args: argparse.Namespace) -> bool:
    """Decide whether ``explain <target>`` routes to the brain/graph explainer.

    Skill names are bare slugs (``wiki``, ``tiny-cli``); brain node/command ids
    carry a ``.`` or ``:`` (``brain.next``, ``runtime.skills``, ``service:foo``)
    or name a registered command. ``--node`` forces brain; ``--skill`` forces
    skill (and wins over ``--node`` only if both are passed, which argparse does
    not prevent — skill is the documented primary, so it takes precedence).
    """
    if getattr(args, "explain_skill", False):
        return False
    if getattr(args, "explain_node", False):
        return True
    target = str(getattr(args, "target", "") or "").strip()
    if "." in target or ":" in target:
        return True
    if target in BRAIN_COMMAND_TARGET_ALIASES:
        return True
    try:
        from .command_registry import load_default_registry

        if target in load_default_registry():
            return True
    except Exception:
        pass
    return False


def _handle_explain(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    if not _explain_target_is_brain(args):
        payload = explain_skill_visibility(
            model,
            args.target,
            cwd=getattr(args, "cwd", None),
            include_global=not getattr(args, "no_global", False),
            include_project=not getattr(args, "no_project", False),
        )
        unresolved = (
            not payload.get("visible")
            and not payload.get("occurrences")
            and not payload.get("source_options")
        )
        # A bare target that resolves to NEITHER a skill NOR a brain node is a
        # genuine "unknown target". Unless `--skill` forces skill mode, fall
        # back to the graph/registry explainer so the unknown-target error path
        # stays a single, consistent message. `--skill` keeps the structured
        # "how would I make it visible" answer (source_restore) even when the
        # skill is unknown today.
        if unresolved and not getattr(args, "explain_skill", False):
            return _handle_explain_brain(args, root_dir, model)
        if args.format == "json":
            emit_json(payload)
        else:
            _print_explain_skill_text(payload)
        return EXIT_ERROR if unresolved else EXIT_OK
    return _handle_explain_brain(args, root_dir, model)


def _handle_explain_brain(args: argparse.Namespace, root_dir: Path, model: dict[str, Any]) -> int:
    adapters = _brain_adapters_for_args(root_dir, model, args)
    graph_payload = _brain_graph_payload(model, adapters)
    payload = explain_payload(graph_payload, args.target, adapters=adapters)
    if args.format == "json":
        emit_json(payload)
    else:
        _print_explain_text(payload)
    return EXIT_ERROR if "error" in payload else EXIT_OK


def _print_explain_skill_text(payload: dict[str, Any]) -> None:
    skill = payload.get("skill")
    visible = "VISIBLE" if payload.get("visible") else "NOT VISIBLE"
    print(f"explain {skill}: {visible}")
    print(f"cwd: {payload.get('cwd')}")
    print(f"reason: {payload.get('reason')}")
    if payload.get("visible"):
        print(
            f"layer: {payload.get('layer')} "
            f"[{payload.get('layer_family')}] rank={payload.get('layer_rank')}"
        )
        winner = payload.get("winner") or {}
        if winner.get("source"):
            print(f"source: {winner.get('source')}")
        if winner.get("path"):
            print(f"path: {winner.get('path')}")
    scope_rules = payload.get("scope_rules") or []
    if scope_rules:
        print("scope rules:")
        for rule in scope_rules:
            cwd_flag = "matches-cwd" if rule.get("matches_cwd") else "no-cwd-match"
            overlay = f" overlay={rule.get('overlay')}" if rule.get("overlay") else ""
            print(
                f"  - {rule.get('id')} (pattern={rule.get('matched_pattern')}) "
                f"{cwd_flag}{overlay} [{rule.get('policy_path')}]"
            )
    lost = payload.get("lost") or []
    if lost:
        print("lost occurrences:")
        for item in lost:
            print(f"  - {item.get('layer')}: {item.get('lost_reason')}")
    overlays = payload.get("active_overlays") or []
    if overlays:
        print(f"active overlays: {', '.join(overlays)}")
    remediation = payload.get("remediation") or []
    if remediation:
        print("paths to visibility (ranked):")
        for step in remediation:
            print(f"  {step.get('rank')}. [{step.get('kind')}] {step.get('command')}")
            if step.get("why"):
                print(f"     why: {step.get('why')}")
    next_actions = payload.get("next_actions") or []
    if next_actions:
        print("next_actions:")
        for action in next_actions:
            print(f"  - {action}")


def _handle_search(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    adapters = _brain_adapters_for_args(root_dir, model, args)
    graph_payload = _brain_graph_payload(model, adapters)
    evidence_payload = None
    evidence_adapter = adapters.get("evidence") if isinstance(adapters, dict) else None
    if isinstance(evidence_adapter, dict) and isinstance(evidence_adapter.get("payload"), dict):
        evidence_payload = evidence_adapter["payload"]
    payload = search_payload(
        " ".join(getattr(args, "query", []) or []),
        graph=graph_payload,
        adapters=adapters,
        evidence=evidence_payload,
        root_dir=root_dir,
        source_filter=getattr(args, "source_filter", []) or [],
        kind_filter=getattr(args, "kind_filter", []) or [],
        limit=max(0, int(getattr(args, "limit", 10))),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        _print_search_text(payload)
    return EXIT_ERROR if "error" in payload else EXIT_OK


def _snap_diff_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    paths = [Path(item) for item in getattr(args, "paths", []) or []]
    from_path = Path(args.from_path) if getattr(args, "from_path", None) else (paths[0] if paths else None)
    to_path = Path(args.to_path) if getattr(args, "to_path", None) else (paths[1] if len(paths) > 1 else None)
    if from_path is None or to_path is None:
        raise RuntimeError("snap diff requires --from/--to or two positional snapshot paths")
    return from_path, to_path


def _snap_subcommands() -> list[dict[str, str]]:
    return [
        {
            "name": "create",
            "side_effect": "none unless --write is passed",
            "writes_only_with": "--write",
            "safe_first_try": "python3 .env-manager/manage.py snap create --format json --no-adapters",
        },
        {
            "name": "diff",
            "side_effect": "none",
            "safe_first_try": "python3 .env-manager/manage.py snap diff --from before.json --to after.json --format json",
        },
        {
            "name": "replay",
            "side_effect": "none",
            "safe_first_try": "python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json",
        },
    ]


def _snap_usage_payload(*, unknown_action: str | None = None) -> dict[str, Any]:
    subcommands = _snap_subcommands()
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "read_only_default": True,
        "summary": "snap subcommands: create (dry-run unless --write), diff, replay",
        "subcommands": subcommands,
        "actions": subcommands,
        "next_actions": [
            "python3 .env-manager/manage.py snap --format json replay tests/goldens/agent_ops_snapshot.json",
            "python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json",
            "python3 .env-manager/manage.py capabilities --format json",
        ],
    }
    if unknown_action is not None:
        payload.update(
            brain_error_payload(
                SNAPSHOT_SCHEMA_VERSION,
                "SNAP_UNKNOWN_ACTION",
                f"unknown snap action: {unknown_action}",
                context={"action": unknown_action},
            )
        )
    return payload


def _snap_action_required_text_payload() -> dict[str, Any]:
    return {
        "error": {
            "code": "SNAP_ACTION_REQUIRED",
            "type": "invalid_argument",
            "message": "snap requires an action: create, diff, or replay",
            "recoverable": True,
        },
        "next_actions": [
            "python3 .env-manager/manage.py snap create --format json --no-adapters",
            "python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json",
        ],
    }


def _snap_error_payload(
    code: str,
    message: str,
    *,
    context: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    return brain_error_payload(
        SNAPSHOT_SCHEMA_VERSION,
        code,
        message,
        context=context,
        next_actions=next_actions or [
            "python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json",
            "python3 .env-manager/manage.py snap --format json",
        ],
    )


def _load_snapshot_for_brain(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path_text = str(path)
    try:
        payload = load_snapshot(path)
    except FileNotFoundError:
        return None, _snap_error_payload(
            "SNAPSHOT_NOT_FOUND",
            f"snapshot not found: {path_text}",
            context={"path": path_text},
        )
    except json.JSONDecodeError as exc:
        return None, _snap_error_payload(
            "SNAPSHOT_SCHEMA_MISMATCH",
            f"snapshot is not valid JSON: {path_text}",
            context={"path": path_text, "reason": str(exc)},
        )
    except OSError as exc:
        return None, _snap_error_payload(
            "SNAPSHOT_NOT_FOUND",
            f"snapshot is unreadable: {path_text}",
            context={"path": path_text, "reason": str(exc)},
        )
    if not isinstance(payload, dict):
        return None, _snap_error_payload(
            "SNAPSHOT_SCHEMA_MISMATCH",
            f"snapshot root must be an object: {path_text}",
            context={"path": path_text, "actual_type": type(payload).__name__},
        )
    inputs = payload.get("inputs")
    if (
        payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or not payload.get("snapshot_id")
        or not isinstance(inputs, dict)
    ):
        return None, _snap_error_payload(
            "SNAPSHOT_SCHEMA_MISMATCH",
            f"snapshot does not match {SNAPSHOT_SCHEMA_VERSION}: {path_text}",
            context={
                "path": path_text,
                "schema_version": payload.get("schema_version"),
                "has_snapshot_id": bool(payload.get("snapshot_id")),
                "has_inputs": isinstance(inputs, dict),
            },
        )
    return payload, None


def _handle_snap(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del resolved_mode
    snap_action = getattr(args, "snap_action", None)
    if not snap_action:
        payload = _snap_usage_payload() if args.format == "json" else _snap_action_required_text_payload()
    elif snap_action == "create":
        adapters = _brain_adapters_for_args(root_dir, model, args)
        payload = create_snapshot_payload(
            status=runtime_status(model),
            doctor={"checks": [asdict(result) for result in doctor_results(model, root_dir)]},
            evidence=collect_runtime_evidence(
                root_dir,
                model,
                cwd=getattr(args, "cwd", None),
                declared_servers=_full_declared_mcp_servers(root_dir),
            ),
            graph=build_agent_graph(model, adapters=adapters).to_payload(),
            label=getattr(args, "name", None),
            created_at=getattr(args, "created_at", None),
        )
        if getattr(args, "write", False):
            payload["artifact"] = str(save_snapshot(root_dir, payload))
    elif snap_action == "diff":
        try:
            from_path, to_path = _snap_diff_paths(args)
        except RuntimeError as exc:
            payload = _snap_error_payload(
                "INVALID_ARGUMENT",
                str(exc),
                context={"action": "diff"},
                next_actions=["python3 .env-manager/manage.py snap diff --from before.json --to after.json --format json"],
            )
        else:
            before, error = _load_snapshot_for_brain(from_path)
            if error is not None:
                payload = error
            else:
                after, error = _load_snapshot_for_brain(to_path)
                payload = error if error is not None else diff_snapshots(before, after)
    elif snap_action == "replay":
        snapshot, error = _load_snapshot_for_brain(Path(args.path))
        payload = error if error is not None else replay_snapshot(snapshot)
    else:
        payload = (
            _snap_usage_payload(unknown_action=snap_action)
            if args.format == "json"
            else {"error": {"message": f"unknown snap action: {snap_action}"}}
        )

    if args.format == "json":
        emit_json(payload)
    else:
        _print_snap_text(payload)
    if args.format == "json" and payload.get("ok") is True and "error" not in payload:
        return EXIT_OK
    return EXIT_ERROR if "error" in payload else EXIT_OK


def _handle_parity_report(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    del root_dir, resolved_mode
    client_id = _primary_client_id(args, model, default=getattr(args, "client_id", "") or "")
    payload = collect_dev_prod_parity_report(model, client_id=client_id)
    return emit_dev_prod_parity_report(payload, fmt=args.format)


def _print_skill_override_lint_text(payload: dict[str, Any]) -> None:
    print("skill override lint")
    print(f"path: {payload.get('policy_path')}")
    if not payload.get("exists"):
        print("findings: none (no override file)")
        return
    findings = payload.get("findings") or []
    if not findings:
        print("findings: none")
        return
    print("findings:")
    for finding in findings:
        severity = str(finding.get("severity") or "warn").upper()
        rule = finding.get("rule")
        skill = finding.get("skill") or "(file)"
        location = ""
        if finding.get("line") is not None:
            location = f":{finding.get('line')}"
        elif finding.get("lines"):
            rendered = ", ".join(
                f"{key}:{value}" for key, value in (finding.get("lines") or {}).items()
            )
            location = f" ({rendered})"
        print(f"  - {severity} {rule} {skill}{location}")
        print(f"    {finding.get('explanation')}")
        print(f"    fix: {finding.get('suggested_fix')}")


def _override_list(policy: dict[str, Any], key: str) -> list[str]:
    return [
        str(item)
        for item in policy.get(key) or []
        if str(item).strip()
    ]


def _mutate_skill_pin(policy: dict[str, Any], skill_name: str, skill_action: str) -> dict[str, Any]:
    updated = dict(policy)
    pin_on = [item for item in _override_list(updated, "pin_on") if item != skill_name]
    pin_off = [item for item in _override_list(updated, "pin_off") if item != skill_name]
    if skill_action == "on":
        pin_on.append(skill_name)
    else:
        pin_off.append(skill_name)
    updated["pin_on"] = pin_on
    updated["pin_off"] = pin_off
    return updated


def _pin_state(policy: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(_override_list(policy, "pin_on")), tuple(_override_list(policy, "pin_off"))


def _skill_pin_would_change(policy: dict[str, Any], skill_name: str, skill_action: str) -> bool:
    return _pin_state(policy) != _pin_state(_mutate_skill_pin(policy, skill_name, skill_action))


def _apply_skill_pin(
    cwd: str | None,
    skill_name: str,
    skill_action: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    current = _repo_override_policy(cwd or os.getcwd())
    policy_path = str(current.get("_policy_path") or "")
    would_change = _skill_pin_would_change(current, skill_name, skill_action)
    if dry_run:
        return {
            "changed": False,
            "would_change": would_change,
            "policy_path": policy_path,
            "pin": "pin_on" if skill_action == "on" else "pin_off",
        }

    result = update_repo_override_policy(
        cwd or os.getcwd(),
        lambda policy: _mutate_skill_pin(policy, skill_name, skill_action),
    )
    return {
        "changed": bool(result.get("changed")),
        "would_change": would_change,
        "policy_path": str(result.get("_policy_path") or policy_path),
        "pin": "pin_on" if skill_action == "on" else "pin_off",
    }


def _heal_pin_reason(skill_name: str) -> str:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"heal:{skill_name} {timestamp}"


def _apply_skill_heal_pin(
    cwd: str | None,
    skill_name: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    current = _repo_override_policy(cwd or os.getcwd())
    policy_path = str(current.get("_policy_path") or "")
    would_change = _skill_pin_would_change(current, skill_name, "on")
    reason = _heal_pin_reason(skill_name)
    if dry_run:
        return {
            "changed": False,
            "would_change": would_change,
            "policy_path": policy_path,
            "pin": "pin_on",
            "reason": reason if would_change else str(current.get("reason") or ""),
        }
    if not would_change:
        return {
            "changed": False,
            "would_change": False,
            "policy_path": policy_path,
            "pin": "pin_on",
            "reason": str(current.get("reason") or ""),
        }

    def mutator(policy: dict[str, Any]) -> dict[str, Any]:
        updated = _mutate_skill_pin(policy, skill_name, "on")
        updated["reason"] = reason
        return updated

    result = update_repo_override_policy(cwd or os.getcwd(), mutator)
    return {
        "changed": bool(result.get("changed")),
        "would_change": would_change,
        "policy_path": str(result.get("_policy_path") or policy_path),
        "pin": "pin_on",
        "reason": reason,
    }


def _mutate_skill_default(policy: dict[str, Any], skill_name: str, default_action: str) -> dict[str, Any]:
    updated = dict(policy)
    defaults = [item for item in _override_list(updated, "defaults") if item != skill_name]
    pin_on = [item for item in _override_list(updated, "pin_on") if item != skill_name]
    pin_off = [item for item in _override_list(updated, "pin_off") if item != skill_name]
    if default_action == "on":
        defaults.append(skill_name)
        pin_on.append(skill_name)
    else:
        pin_off.append(skill_name)
    updated["defaults"] = defaults
    updated["pin_on"] = pin_on
    updated["pin_off"] = pin_off
    return updated


def _unified_policy_diff(path: str, before: str, after: str) -> str:
    if before == after:
        return ""
    before_label = path if before else "/dev/null"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=before_label,
            tofile=path,
        )
    )


def _check_result_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "code": result.code,
        "message": result.message,
        "details": result.details or {},
    }


def _handle_repo_skill_default(
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    skill_name = str(args.skill_name)
    default_action = str(args.default_action)
    cwd_path = Path(args.cwd or os.getcwd()).resolve()
    if default_action == "off" and skill_name in set(DISPATCHER_CORE):
        raise ValidationError(
            OVERRIDE_REFUSED_FLOOR,
            f"Refusing to default off dispatcher floor skill {skill_name!r}.",
            context={
                "skill": skill_name,
                "action": "default off",
                "floor": list(DISPATCHER_CORE),
                "policy": "repo defaults cannot disable dispatcher floor skills",
            },
            next_actions=[
                f"sbp skill default on {skill_name} --repo --cwd {cwd_path}",
                f"sbp skill lint --cwd {cwd_path}",
            ],
        )

    current = _repo_override_policy(str(cwd_path))
    policy_path = str(current.get("_policy_path") or "")
    path = Path(policy_path)
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    after = _repo_policy_text_for_default(before, skill_name, default_action)
    diff_text = _unified_policy_diff(
        policy_path,
        before,
        after,
    )
    would_change = before != after
    result: dict[str, Any] = {
        "action": "default",
        "default_action": default_action,
        "scope": "repo",
        "skill": skill_name,
        "cwd": str(cwd_path),
        "dry_run": dry_run,
        "changed": False,
        "would_change": would_change,
        "noop": not would_change,
        "policy_path": policy_path,
        "diff": diff_text,
        "override": {
            "pin": "pin_on" if default_action == "on" else "pin_off",
            "defaults": default_action == "on",
        },
    }
    if dry_run:
        return result

    if would_change:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, after)
    result["changed"] = would_change
    result["noop"] = not would_change
    return result


def _load_policy_from_text(text: str) -> dict[str, Any]:
    yaml_mod = require_yaml("read skill-scope policy")
    parsed = yaml_mod.safe_load(text) if text.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def _repo_policy_text_for_default(text: str, skill_name: str, default_action: str) -> str:
    base_text = text if text.strip() else "version: 1\n"
    policy = _load_policy_from_text(base_text)
    updated = _mutate_skill_default(policy, skill_name, default_action)
    rendered = _upsert_top_level_list(
        base_text,
        "pin_on",
        _override_list(updated, "pin_on"),
        after_keys=("version",),
    )
    rendered = _upsert_top_level_list(
        rendered,
        "pin_off",
        _override_list(updated, "pin_off"),
        after_keys=("pin_on", "version"),
    )
    rendered = _upsert_top_level_list(
        rendered,
        "defaults",
        _override_list(updated, "defaults"),
        after_keys=("overlays", "opt_out_global", "pin_off", "pin_on", "version"),
    )
    parsed = _load_policy_from_text(rendered)
    if not isinstance(parsed, dict):
        raise RuntimeError("repo skill override policy would not parse as a mapping")
    return rendered


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []


def _allow_global_patterns(rule: dict[str, Any]) -> list[str]:
    raw = rule.get("skills") or rule.get("patterns") or rule.get("names") or []
    return [name for item in _string_list(raw) if (name := item.strip())]


def _global_allow_rule_matches_skill(rule: dict[str, Any], skill_name: str) -> bool:
    if not bool(rule.get("allow_global", False)):
        return False
    return any(fnmatch.fnmatchcase(skill_name, pattern) for pattern in _allow_global_patterns(rule))


def _allow_global_union(policy: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("allow_global", False)):
            continue
        names.update(_allow_global_patterns(rule))
    return names


def _global_policy_grants_skill(policy: dict[str, Any], skill_name: str) -> bool:
    for rule in policy.get("rules") or []:
        if isinstance(rule, dict) and _global_allow_rule_matches_skill(rule, skill_name):
            return True
    return False


def _hand_authored_global_grants_skill(policy: dict[str, Any], skill_name: str) -> bool:
    generated_id = _generated_global_rule_id(skill_name)
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict) or str(rule.get("id") or "") == generated_id:
            continue
        if _global_allow_rule_matches_skill(rule, skill_name):
            return True
    return False


def _top_level_block_end(lines: list[str], start: int) -> int:
    index = start + 1
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped and not line.startswith((" ", "\t")) and not stripped.startswith("#"):
            break
        index += 1
    return index


def _replace_top_level_list(text: str, key: str, values: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    replacement = [f"{key}:\n"]
    replacement.extend(f"  - {value}\n" for value in values)
    if not values:
        replacement = [f"{key}: []\n"]
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            end = _top_level_block_end(lines, index)
            return "".join([*lines[:index], *replacement, *lines[end:]])
    return text


def _upsert_top_level_list(
    text: str,
    key: str,
    values: list[str],
    *,
    after_keys: tuple[str, ...] = (),
) -> str:
    updated = _replace_top_level_list(text, key, values)
    if updated != text:
        return updated
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    replacement = [f"{key}:\n"]
    replacement.extend(f"  - {value}\n" for value in values)
    if not values:
        replacement = [f"{key}: []\n"]
    insert_at = len(lines)
    for after_key in after_keys:
        found_anchor = False
        for index, line in enumerate(lines):
            if line.startswith(f"{after_key}:"):
                insert_at = _top_level_block_end(lines, index)
                found_anchor = True
                break
        if found_anchor:
            break
    return "".join([*lines[:insert_at], *replacement, *lines[insert_at:]])


def _generated_global_rule_id(skill_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", skill_name.lower()).strip("-") or "skill"
    return f"skillbox-default-global-{slug}"


def _generated_global_rule_lines(skill_name: str) -> list[str]:
    return [
        f"  - id: {_generated_global_rule_id(skill_name)}\n",
        "    skills:\n",
        f"      - {skill_name}\n",
        "    allow_global: true\n",
        "    default: on\n",
    ]


def _has_generated_global_rule(text: str, skill_name: str) -> bool:
    needle = f"  - id: {_generated_global_rule_id(skill_name)}"
    return any(line.strip() == needle.strip() for line in text.splitlines())


def _append_generated_global_rule(text: str, skill_name: str) -> str:
    if _has_generated_global_rule(text, skill_name):
        return text
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    rule_lines = _generated_global_rule_lines(skill_name)
    for index, line in enumerate(lines):
        if not line.startswith("rules:"):
            continue
        if line.strip() != "rules:":
            if line.strip() not in {"rules: []", "rules: null"}:
                raise RuntimeError(
                    "cannot preserve inline non-empty rules while adding a generated default; "
                    "convert rules to block form first."
                )
            end = _top_level_block_end(lines, index)
            return "".join([*lines[:index], "rules:\n", *rule_lines, *lines[end:]])
        end = _top_level_block_end(lines, index)
        return "".join([*lines[:end], *rule_lines, *lines[end:]])
    prefix = "".join(lines)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "rules:\n" + "".join(rule_lines)


def _remove_generated_global_rule(text: str, skill_name: str) -> str:
    rule_id = _generated_global_rule_id(skill_name)
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() != f"- id: {rule_id}":
            continue
        end = index + 1
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith("- id: ") or (stripped and not lines[end].startswith((" ", "\t"))):
                break
            end += 1
        return "".join([*lines[:index], *lines[end:]])
    return text


def _sync_global_allowlist_snapshot(text: str) -> str:
    policy = _load_policy_from_text(text)
    if "global_allowlist" not in policy:
        return text
    return _replace_top_level_list(
        text,
        "global_allowlist",
        sorted(_allow_global_union(policy)),
    )


def _validate_global_policy_text_or_raise(text: str, policy_path: Path) -> list[CheckResult]:
    policy = _load_policy_from_text(text)
    results = validate_global_skill_contract(policy, policy_path=str(policy_path))
    failures = [result for result in results if result.status == "fail"]
    if failures:
        messages = "; ".join(result.message for result in failures)
        raise RuntimeError(f"skill-scope policy would fail global contract lint: {messages}")
    return results


def _global_policy_text_for_default(text: str, skill_name: str, default_action: str) -> str:
    policy = _load_policy_from_text(text)
    allowed_before = _global_policy_grants_skill(policy, skill_name)
    if default_action == "on":
        updated = text if allowed_before else _append_generated_global_rule(text, skill_name)
    else:
        if _hand_authored_global_grants_skill(policy, skill_name) or (
            allowed_before and not _has_generated_global_rule(text, skill_name)
        ):
            raise RuntimeError(
                "skill default off --global only removes allow_global rules created by "
                "`skill default on --global`; edit the existing hand-authored rule deliberately."
            )
        updated = _remove_generated_global_rule(text, skill_name)
    return _sync_global_allowlist_snapshot(updated)


def _skill_scope_policy_path_arg(args: argparse.Namespace) -> Path:
    raw = str(getattr(args, "policy_path", None) or "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return VALIDATION._skill_scope_policy_path().resolve()


def _handle_global_skill_default(
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not dry_run and not bool(getattr(args, "yes", False)):
        raise RuntimeError(
            "skill default --global writes outside this repo; run with --dry-run first, "
            "then pass --yes to apply."
        )
    skill_name = str(args.skill_name)
    default_action = str(args.default_action)
    policy_path = _skill_scope_policy_path_arg(args)
    if not policy_path.is_file():
        raise RuntimeError(f"skill-scope policy not found: {policy_path}")
    before = policy_path.read_text(encoding="utf-8")
    after = _global_policy_text_for_default(before, skill_name, default_action)
    validation_results = _validate_global_policy_text_or_raise(after, policy_path)
    diff_text = _unified_policy_diff(str(policy_path), before, after)
    would_change = before != after
    result: dict[str, Any] = {
        "action": "default",
        "default_action": default_action,
        "scope": "global",
        "skill": skill_name,
        "cwd": str(Path(args.cwd or os.getcwd()).resolve()),
        "dry_run": dry_run,
        "changed": False,
        "would_change": would_change,
        "noop": not would_change,
        "policy_path": str(policy_path),
        "diff": diff_text,
        "validation": [_check_result_payload(item) for item in validation_results],
    }
    if dry_run:
        return result
    if would_change:
        atomic_write_text(policy_path, after)
    result["changed"] = would_change
    return result


def _handle_skill_default(
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    scope = str(args.default_scope)
    if scope == "repo":
        return _handle_repo_skill_default(args, dry_run=dry_run)
    if scope == "global":
        return _handle_global_skill_default(args, dry_run=dry_run)
    raise RuntimeError("skill default requires exactly one scope: --repo or --global")


def _print_skill_default_text(payload: dict[str, Any]) -> None:
    mode = "dry-run" if payload.get("dry_run") else "apply"
    print(f"skill default {payload.get('default_action')}: {payload.get('skill')} ({mode})")
    print(f"scope: {payload.get('scope')}")
    print(f"path: {payload.get('policy_path')}")
    print(f"changed: {str(bool(payload.get('changed'))).lower()}")
    print(f"would_change: {str(bool(payload.get('would_change'))).lower()}")
    if payload.get("validation"):
        statuses = ", ".join(
            f"{item.get('code')}={item.get('status')}" for item in payload.get("validation") or []
        )
        print(f"validation: {statuses}")
    diff_text = str(payload.get("diff") or "")
    if diff_text:
        print("diff:")
        print(diff_text, end="" if diff_text.endswith("\n") else "\n")
    else:
        print("diff: none")


def _build_skill_togglable_payload(
    model: dict[str, Any],
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    cwd_path = Path(cwd or os.getcwd()).resolve()
    visibility = collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=False,
        include_project=True,
        include_sources=True,
    )
    override = _repo_override_policy(str(cwd_path))
    pin_on = {str(name) for name in _override_list(override, "pin_on")}
    pin_off = {str(name) for name in _override_list(override, "pin_off")}
    effective = {
        str(item.get("name")): item
        for item in visibility.get("effective") or []
        if item.get("name")
    }
    missing = {
        str(item.get("name")): item
        for item in (visibility.get("issues") or {}).get("missing_for_cwd") or []
        if item.get("name")
    }
    names: set[str] = set()
    for rule in visibility.get("matched_scope_rules") or []:
        for skill_name in rule.get("skills") or []:
            text = str(skill_name).strip()
            if text:
                names.add(text)
    names.update(pin_on)
    names.update(pin_off)
    names.update(effective)
    names.update(missing)

    items: list[dict[str, Any]] = []
    for skill_name in sorted(names):
        source: str | None = None
        if skill_name in effective:
            winner = effective[skill_name]
            source = str(winner.get("path") or winner.get("source") or "") or None

        if skill_name in pin_on:
            state = "pinned_on"
            pinned_by = "override"
        elif skill_name in pin_off:
            state = "pinned_off"
            pinned_by = "override"
        elif skill_name in missing:
            state = "missing_for_cwd"
            pinned_by = "policy"
        elif skill_name in effective:
            state = "on"
            pinned_by = "policy"
        else:
            state = "off"
            pinned_by = "policy"

        next_action = "on" if state in {"missing_for_cwd", "off", "pinned_off"} else "off"
        items.append({
            "skill": skill_name,
            "state": state,
            "source": source,
            "pinned_by": pinned_by,
            "command_to_flip": f"sbp skill {next_action} {skill_name} --cwd {cwd_path}",
        })

    return {"cwd": str(cwd_path), "items": items}


def _print_skill_togglable_text(payload: dict[str, Any]) -> None:
    print(f"skill togglable: {payload.get('cwd')}")
    for item in payload.get("items") or []:
        print(
            f"  - {item.get('skill')}: {item.get('state')} "
            f"({item.get('pinned_by')}) -> {item.get('command_to_flip')}"
        )
    if not payload.get("items"):
        print("  (none)")


def _drop_same_link_actions(plan: dict[str, Any]) -> dict[str, Any]:
    filtered = [
        action for action in plan.get("actions") or []
        if not (
            action.get("op") == "link"
            and (action.get("existing") or {}).get("state") == "same_link"
        )
    ]
    if len(filtered) == len(plan.get("actions") or []):
        return plan
    updated = dict(plan)
    updated["actions"] = filtered
    updated["summary"] = _lifecycle_plan_summary(filtered, skipped=updated.get("skipped") or [])
    return updated


def _simulated_skill_off_plan(
    model: dict[str, Any],
    args: argparse.Namespace,
    cwd_path: Path,
) -> dict[str, Any]:
    visibility = collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=True,
        include_project=True,
        include_sources=False,
    )
    skill_name = str(args.skill_name)
    decisions = [
        item for item in visibility.get("visibility_decisions") or []
        if str(item.get("name") or "") != skill_name
    ]
    decisions.append({
        "name": skill_name,
        "availability": "override",
        "state": "disabled",
        "override_action": "pin_off",
        "layer": "repo-override-file",
        "winning_layer": "repo-override-file",
    })
    simulated_visibility = dict(visibility)
    simulated_visibility["visibility_decisions"] = decisions
    skipped: list[dict[str, Any]] = []
    actions = _plan_skill_prune_actions(
        simulated_visibility,
        skill_name,
        from_scope=getattr(args, "from_scope", "project"),
        skipped=skipped,
    )
    deduped = _dedupe_actions(actions)
    return {
        "action": "off",
        "skill": skill_name,
        "cwd": str(cwd_path),
        "requested_to": getattr(args, "to", "project"),
        "resolved_to": "project",
        "categories": getattr(args, "category", []) or [],
        "from_scope": getattr(args, "from_scope", "project"),
        "source_options": [],
        "selected_source": None,
        "activation_packet": None,
        "warnings": [],
        "actions": deduped,
        "skipped": skipped,
        "summary": _lifecycle_plan_summary(deduped, skipped=skipped),
    }


def _skill_packet_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256((path / "SKILL.md").read_bytes()).hexdigest()
    except OSError:
        return None


def _skill_toggle_verification(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    activation_packet: dict[str, Any] | None,
    *,
    expected_targets: list[str] | None = None,
) -> dict[str, Any]:
    visibility = collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=True,
        include_project=True,
        include_sources=False,
    )
    effective_now = [
        item for item in visibility.get("effective") or []
        if str(item.get("name") or "") == skill_name
    ]
    packet_sha = str((activation_packet or {}).get("skill_md_sha256") or "")
    linked: list[dict[str, Any]] = []
    for item in visibility.get("occurrences") or []:
        if item.get("availability") != "installed":
            continue
        if str(item.get("name") or "") != skill_name:
            continue
        path = Path(str(item.get("path") or ""))
        resolved = path.resolve() if path.exists() else None
        linked.append({
            "path": str(path),
            "is_symlink": path.is_symlink(),
            "resolved": str(resolved) if resolved else None,
            "skill_md_sha256": _skill_packet_sha256(resolved) if resolved else None,
        })
    sha_matches = bool(packet_sha) and any(
        row.get("skill_md_sha256") == packet_sha for row in linked
    )
    expected_link_rows: list[dict[str, Any]] = []
    packet_source = str((activation_packet or {}).get("source") or "")
    packet_source_real = os.path.realpath(packet_source) if packet_source else ""
    for target in expected_targets or []:
        path = Path(target)
        exists = os.path.lexists(path)
        resolved = os.path.realpath(path) if exists else None
        target_sha = _skill_packet_sha256(Path(resolved)) if resolved else None
        ok = (
            bool(exists)
            and path.is_symlink()
            and bool(packet_source_real)
            and resolved == packet_source_real
            and target_sha == packet_sha
        )
        expected_link_rows.append({
            "path": str(path),
            "exists": exists,
            "is_symlink": path.is_symlink(),
            "resolved": resolved,
            "skill_md_sha256": target_sha,
            "ok": ok,
        })
    expected_links_ok = all(row.get("ok") for row in expected_link_rows) if expected_targets is not None else True
    return {
        "verified": bool(effective_now) and sha_matches and expected_links_ok,
        "effective_now": effective_now,
        "symlink_resolved": linked,
        "expected_targets": expected_link_rows,
        "skill_md_sha256": packet_sha or None,
    }


def _activation_packet_targets(activation_packet: dict[str, Any] | None) -> list[str]:
    targets: list[str] = []
    for values in ((activation_packet or {}).get("surface_targets") or {}).values():
        targets.extend(str(value) for value in values or [] if value)
    return sorted(dict.fromkeys(targets))


def _skill_name_suggestions(model: dict[str, Any], skill_name: str) -> list[str]:
    names: set[str] = set()
    try:
        declared_occurrences, _layers = _declared_skill_occurrences(model)
        for root in _skill_source_roots(model, declared_occurrences):
            for candidate in _skill_source_candidates(root):
                name = str(candidate.get("name") or "")
                if name:
                    names.add(name)
    except Exception:
        names = set()
    return difflib.get_close_matches(skill_name, sorted(names), n=5, cutoff=0.45)


def _selected_source_from_visible_skill(
    visibility: dict[str, Any],
    skill_name: str,
) -> dict[str, Any] | None:
    rows = [
        *(visibility.get("effective") or []),
        *(visibility.get("occurrences") or []),
    ]
    for row in rows:
        if str(row.get("name") or "") != skill_name:
            continue
        if str(row.get("state") or "") in {"disabled", "broken"}:
            continue
        raw_source = str(row.get("source") or "")
        raw_path = str(row.get("path") or "")
        candidates = [raw_source, raw_path]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path.absolute()
            if (resolved / "SKILL.md").is_file():
                return {
                    "name": skill_name,
                    "source": str(resolved),
                    "source_bucket": row.get("source_bucket") or _source_bucket(str(resolved)),
                    "root": str(resolved.parent),
                    "explicit": False,
                }
    return None


def _raise_heal_unknown(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    *,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
    extra_suggestions: list[str] | None = None,
) -> None:
    suggestions: list[str] = []
    for suggestion in extra_suggestions or []:
        if suggestion and suggestion != skill_name and suggestion not in suggestions:
            suggestions.append(suggestion)
    for suggestion in _skill_name_suggestions(model, skill_name):
        if suggestion and suggestion not in suggestions:
            suggestions.append(suggestion)
    suffix = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValidationError(
        OVERRIDE_SKILL_UNKNOWN,
        (message or f"Cannot heal skill {skill_name!r}: no source directory found.") + suffix,
        context={
            "skill": skill_name,
            "cwd": str(cwd_path),
            "suggestions": suggestions,
            "source_options": (payload or {}).get("source_options") or [],
        },
        next_actions=[
            f"sbp candidates --cwd {cwd_path} --json",
            f"sbp skill why {skill_name} --cwd {cwd_path} --json",
        ],
    )


def _validate_heal_source_identity_or_raise(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    selected_source: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if not bool(selected_source.get("explicit")):
        return
    source_path = Path(str(selected_source.get("source") or "")).resolve()
    source_name = source_path.name
    if source_name == skill_name:
        return
    _raise_heal_unknown(
        model,
        skill_name,
        cwd_path,
        payload=payload,
        message=(
            f"Cannot heal skill {skill_name!r}: explicit source {source_path} "
            f"appears to be skill {source_name!r}."
        ),
        extra_suggestions=[source_name],
    )


def _resolve_heal_source_or_raise(
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    selected_source = payload.get("selected_source")
    if selected_source:
        _validate_heal_source_identity_or_raise(model, skill_name, cwd_path, selected_source, payload)
        return selected_source

    visibility = collect_skill_visibility(
        model,
        cwd=str(cwd_path),
        include_global=True,
        include_project=True,
        include_sources=False,
    )
    selected_source = _selected_source_from_visible_skill(visibility, skill_name)
    if selected_source:
        return selected_source

    _raise_heal_unknown(model, skill_name, cwd_path, payload=payload)
    raise AssertionError("unreachable")


def _heal_lifecycle_plan_or_raise(
    args: argparse.Namespace,
    model: dict[str, Any],
    skill_name: str,
    cwd_path: Path,
    requested_to: str,
) -> dict[str, Any]:
    try:
        return skill_lifecycle_plan(
            model,
            "activate",
            skill_name=skill_name,
            cwd=str(cwd_path),
            to=requested_to,
            categories=getattr(args, "category", []) or [],
            source=getattr(args, "source", None),
            force=True,
        )
    except RuntimeError as exc:
        if getattr(args, "source", None):
            _raise_heal_unknown(
                model,
                skill_name,
                cwd_path,
                message=f"Cannot heal skill {skill_name!r}: explicit source could not be resolved: {exc}",
            )
        raise


def _validate_skill_toggle_security(
    model: dict[str, Any],
    *,
    skill_name: str,
    skill_action: str,
    requested_to: str,
    cwd_path: Path,
) -> None:
    if skill_action == "off" and skill_name in set(DISPATCHER_CORE):
        raise ValidationError(
            OVERRIDE_REFUSED_FLOOR,
            f"Refusing to pin off dispatcher floor skill {skill_name!r}.",
            context={
                "skill": skill_name,
                "action": skill_action,
                "floor": list(DISPATCHER_CORE),
                "policy": "repo overrides cannot disable dispatcher floor skills",
            },
            next_actions=[
                f"sbp skill on {skill_name} --cwd {cwd_path}",
                f"sbp skill lint --cwd {cwd_path}",
            ],
        )
    global_refusal = (
        _global_override_refusal_context(model, skill_name)
        if skill_action == "on" and requested_to == "global"
        else None
    )
    if global_refusal is not None:
        context = dict(global_refusal)
        context.update({
            "action": skill_action,
            "requested_to": requested_to,
            "policy": "repo overrides may widen visibility only inside the current repo",
        })
        raise ValidationError(
            OVERRIDE_REFUSED_GLOBAL_ESCALATION,
            f"Refusing global pin for skill {skill_name!r}: allow_global is false.",
            context=context,
            next_actions=[
                f"sbp skill on {skill_name} --cwd {cwd_path}",
                "Edit the operator skill-scope allow_global rule if this truly belongs in the global layer.",
            ],
        )


def _handle_skill_toggle(
    args: argparse.Namespace,
    model: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    skill_action = str(args.skill_action)
    skill_name = str(args.skill_name)
    cwd_path = Path(args.cwd or os.getcwd()).resolve()
    requested_to = str(getattr(args, "to", "project") or "project")
    from_scope = str(getattr(args, "from_scope", "project") or "project")
    _validate_skill_toggle_security(
        model,
        skill_name=skill_name,
        skill_action=skill_action,
        requested_to=requested_to,
        cwd_path=cwd_path,
    )
    if requested_to != "project":
        raise RuntimeError("skill on/off currently supports repo-local project scope only; use --to project.")
    if skill_action == "off" and from_scope != "project":
        raise RuntimeError("skill off currently unlinks project installs only; use --from project.")

    override: dict[str, Any] | None = None
    if skill_action == "on":
        payload = skill_lifecycle_plan(
            model,
            "activate",
            skill_name=skill_name,
            cwd=str(cwd_path),
            to=requested_to,
            categories=getattr(args, "category", []) or [],
            source=getattr(args, "source", None),
            force=True,
        )
        payload = _drop_same_link_actions(payload)
    else:
        if dry_run:
            payload = _simulated_skill_off_plan(model, args, cwd_path)
        else:
            override = _apply_skill_pin(args.cwd, skill_name, skill_action, dry_run=False)
            payload = skill_lifecycle_plan(
                model,
                "prune",
                skill_name=skill_name,
                cwd=str(cwd_path),
                to=requested_to,
                from_scope=from_scope,
            )
            payload["action"] = "off"

    if override is None:
        override = _apply_skill_pin(args.cwd, skill_name, skill_action, dry_run=dry_run)
    payload["action"] = skill_action
    payload["override"] = override
    payload["changed"] = bool(override.get("changed"))
    payload["noop"] = not bool(override.get("would_change")) and not bool(payload.get("actions"))
    payload = apply_skill_lifecycle_plan(
        payload,
        dry_run=dry_run,
        allow_directories=bool(getattr(args, "allow_directories", False)),
        force=bool(getattr(args, "force", False)),
    )
    if skill_action == "on" and bool(getattr(args, "verify", False)) and not dry_run:
        payload["verification"] = _skill_toggle_verification(
            model,
            skill_name,
            cwd_path,
            payload.get("activation_packet"),
        )
    else:
        payload["verification"] = None
    return payload


def _handle_skill_heal(
    args: argparse.Namespace,
    model: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    skill_name = str(args.skill_name)
    cwd_path = Path(args.cwd or os.getcwd()).resolve()
    requested_to = str(getattr(args, "to", "project") or "project")
    if requested_to != "project":
        raise RuntimeError("skill heal currently supports repo-local project scope only; use --to project.")
    _validate_skill_toggle_security(
        model,
        skill_name=skill_name,
        skill_action="on",
        requested_to=requested_to,
        cwd_path=cwd_path,
    )
    payload = _heal_lifecycle_plan_or_raise(args, model, skill_name, cwd_path, requested_to)
    selected_source = _resolve_heal_source_or_raise(model, skill_name, cwd_path, payload)
    payload["selected_source"] = selected_source
    if not payload.get("activation_packet"):
        activation_packet, packet_warning = _activation_packet(
            skill_name,
            selected_source,
            payload.get("actions") or [],
        )
        payload["activation_packet"] = activation_packet
        if packet_warning:
            payload.setdefault("warnings", []).append(packet_warning)
    payload = _drop_same_link_actions(payload)
    override = _apply_skill_heal_pin(args.cwd, skill_name, dry_run=dry_run)
    payload["action"] = "heal"
    payload["override"] = override
    payload["changed"] = bool(override.get("changed"))
    payload["noop"] = not bool(override.get("would_change")) and not bool(payload.get("actions"))
    payload = apply_skill_lifecycle_plan(
        payload,
        dry_run=dry_run,
        allow_directories=bool(getattr(args, "allow_directories", False)),
        force=bool(getattr(args, "force", False)),
    )
    if bool(getattr(args, "verify", False)) and not dry_run:
        payload["verification"] = _skill_toggle_verification(
            model,
            skill_name,
            cwd_path,
            payload.get("activation_packet"),
            expected_targets=_activation_packet_targets(payload.get("activation_packet")),
        )
    else:
        payload["verification"] = None
    return payload


def _handle_skill(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    skill_action = str(args.skill_action)
    if skill_action == "lint":
        payload = repo_skill_override_lint_payload(model, cwd=args.cwd)
        if args.format == "json":
            emit_json(payload)
        else:
            _print_skill_override_lint_text(payload)
        return EXIT_ERROR if any(
            item.get("severity") == "error"
            for item in payload.get("findings") or []
        ) else EXIT_OK

    if skill_action == "why":
        payload = explain_skill_visibility(
            model,
            args.skill_name,
            cwd=args.cwd,
            include_global=not getattr(args, "no_global", False),
            include_project=not getattr(args, "no_project", False),
        )
        if args.format == "json":
            emit_json(payload)
        else:
            _print_explain_skill_text(payload)
        # Absence is a successful diagnosis for this read-only command.
        return EXIT_OK

    if skill_action in {"togglable", "toggleable"}:
        payload = _build_skill_togglable_payload(model, cwd=args.cwd)
        if args.format == "json":
            emit_json(payload)
        else:
            _print_skill_togglable_text(payload)
        return EXIT_OK

    dry_run = bool(args.dry_run or skill_action == "plan")
    if skill_action in {"on", "off"}:
        payload = _handle_skill_toggle(args, model, dry_run=dry_run)
        if args.format == "json":
            emit_json(payload)
        else:
            print_skill_lifecycle_text(payload)
        return EXIT_OK
    if skill_action == "default":
        payload = _handle_skill_default(args, dry_run=dry_run)
        if args.format == "json":
            emit_json(payload)
        else:
            _print_skill_default_text(payload)
        return EXIT_OK
    if skill_action == "heal":
        payload = _handle_skill_heal(args, model, dry_run=dry_run)
        if args.format == "json":
            emit_json(payload)
        else:
            print_skill_lifecycle_text(payload)
        return EXIT_OK

    if (
        not dry_run
        and not bool(getattr(args, "yes", False))
        and (
            skill_action in {"move", "remove", "prune"}
            or (skill_action == "sync" and bool(getattr(args, "prune", False)))
        )
    ):
        raise RuntimeError(
            f"`skill {skill_action}` may unlink existing installs. "
            "Re-run with --dry-run to preview or --yes to apply."
        )
    payload = skill_lifecycle_plan(
        model,
        skill_action,
        skill_name=getattr(args, "skill_name", None),
        cwd=args.cwd,
        to=args.to,
        categories=getattr(args, "category", []) or [],
        source=args.source,
        from_scope=getattr(args, "from_scope", "all"),
        prune=bool(getattr(args, "prune", False)),
        force=bool(args.force),
    )
    payload = apply_skill_lifecycle_plan(
        payload,
        dry_run=dry_run,
        allow_directories=bool(args.allow_directories),
        force=bool(args.force),
    )
    if args.format == "json":
        emit_json(payload)
    else:
        print_skill_lifecycle_text(payload)
    if not dry_run:
        problematic = [
            item for item in payload.get("actions") or []
            if str(item.get("status") or "").startswith(("blocked", "conflict", "skipped"))
            and str(item.get("code") or "") != PRUNE_SKIPPED_PINNED
        ]
        if problematic:
            return EXIT_DRIFT
    return EXIT_OK


def _overlay_action_and_name(args: argparse.Namespace) -> tuple[str, str]:
    action = str(getattr(args, "action", "list"))
    name = str(getattr(args, "name", "") or "").strip()
    if action != "list" and not name:
        raise RuntimeError(
            f"overlay {action}: pass an overlay name, e.g. `overlay {action} marketing`."
        )
    return action, name


def _overlay_declared_records(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared-overlay records (name/description/default) for this model.

    Best-effort: a malformed/absent policy yields no declarations rather than
    breaking the overlay command (so `overlay` stays usable on a box with no
    skill-scope policy, and the on/off paths simply skip the undeclared guard).
    """
    try:
        return list(declared_overlay_records(model))
    except Exception:
        return []


def _guard_overlay_is_declared(
    action: str, name: str, declared_records: list[dict[str, Any]]
) -> None:
    """Fail an on/off/toggle/activate of an UNDECLARED overlay, printing the registry.

    No declared registry (legacy/empty policy) means there is nothing to validate
    against, so the guard is a no-op (mirrors the lint's empty-policy pass and
    keeps the existing `overlay on` tests that pass an empty model green).
    """
    if action == "list" or not name or not declared_records:
        return
    declared_names = [str(record.get("name") or "") for record in declared_records]
    if name in declared_names:
        return
    raise RuntimeError(
        f"overlay {action}: '{name}' is not a declared overlay. "
        f"Declared overlays: {', '.join(declared_names) or '(none)'}. "
        "Declare it in skill-scope.yaml `overlays:` (then re-run), "
        "or use one of the declared names above."
    )


def _apply_persistent_overlay_action(action: str, name: str) -> tuple[bool, list[str], bool]:
    was_on = name in active_overlays()
    if action == "on":
        set_overlay(name, True)
    elif action == "off":
        set_overlay(name, False)
    elif action == "toggle":
        toggle_overlay(name)
    current = sorted(active_overlays())
    return was_on, current, name in current


def _preview_overlay_action(action: str, name: str) -> tuple[bool, list[str], bool]:
    current_set = set(active_overlays())
    was_on = name in current_set
    if action == "on":
        current_set.add(name)
    elif action == "off":
        current_set.discard(name)
    elif action == "toggle":
        if name in current_set:
            current_set.remove(name)
        else:
            current_set.add(name)
    current = sorted(current_set)
    return was_on, current, name in current


def _overlay_cwd(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "cwd", None) or os.environ.get("PWD") or os.getcwd())


def _overlay_activations(
    args: argparse.Namespace,
    model: dict[str, Any],
    action: str,
    name: str,
    overlay_cwd: Path,
) -> list[dict[str, Any]]:
    if not name or action != "activate":
        return []
    return activate_overlay_scoped_skills(
        model,
        name,
        overlay_cwd,
        to=str(getattr(args, "to", "project")),
        categories=getattr(args, "category", []) or [],
        source=getattr(args, "source", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
    )


def _overlay_removed_links(
    args: argparse.Namespace,
    model: dict[str, Any],
    action: str,
    name: str,
    was_on: bool,
    now_on: bool,
    overlay_cwd: Path,
) -> list[str]:
    should_unlink = (
        name
        and (action == "off" or (action == "toggle" and was_on and not now_on))
        and not now_on
        and not bool(getattr(args, "keep", False))
        and not bool(getattr(args, "dry_run", False))
    )
    if not should_unlink:
        return []
    return unlink_overlay_scoped_skills(
        model,
        name,
        overlay_cwd,
        scope=str(getattr(args, "scope", "project")),
    )


def _overlay_declared_state(
    declared_records: list[dict[str, Any]], current: list[str]
) -> list[dict[str, Any]]:
    """Declared overlays annotated with their live on/off state for the list view."""
    active = set(current)
    rows: list[dict[str, Any]] = []
    for record in declared_records:
        name = str(record.get("name") or "")
        rows.append(
            {
                "name": name,
                "description": str(record.get("description") or ""),
                "default": "off" if record.get("default_off", True) else "on",
                "state": "on" if name in active else "off",
            }
        )
    return rows


def _overlay_payload(
    args: argparse.Namespace,
    *,
    action: str,
    name: str,
    current: list[str],
    overlay_cwd: Path,
    removed: list[str],
    activations: list[dict[str, Any]],
    declared_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    declared_records = declared_records or []
    undeclared_active = sorted(
        item for item in current
        if declared_records and item not in {str(r.get("name") or "") for r in declared_records}
    )
    return {
        "overlays": current,
        "action": action,
        "name": name,
        "cwd": str(overlay_cwd),
        "to": getattr(args, "to", "project"),
        "scope": getattr(args, "scope", "project"),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "persistent": action in {"on", "off", "toggle"} and not bool(getattr(args, "dry_run", False)),
        "would_persist": action in {"on", "off", "toggle"} and bool(getattr(args, "dry_run", False)),
        "unlinked": removed,
        "activations": activations,
        "declared": _overlay_declared_state(declared_records, current),
        "undeclared_active": undeclared_active,
    }


def _print_activation_packet_text(activation: dict[str, Any]) -> None:
    packet = activation.get("activation_packet")
    if not packet:
        print(f"activation packet: {activation.get('skill')} unavailable")
        return
    print(f"activation packet: {packet.get('name')}")
    print(f"source: {packet.get('source')}")
    print(f"skill_md_sha256: {packet.get('skill_md_sha256')}")
    print("skill_md:")
    print(str(packet.get("skill_md") or "").rstrip())


def _print_overlay_text(payload: dict[str, Any]) -> None:
    action = str(payload.get("action") or "list")
    current = list(payload.get("overlays") or [])
    declared = list(payload.get("declared") or [])
    undeclared_active = list(payload.get("undeclared_active") or [])
    if action == "list":
        if declared:
            print("declared overlays:")
            for row in declared:
                name = str(row.get("name") or "")
                state = str(row.get("state") or "off")
                desc = str(row.get("description") or "").strip()
                line = f"  {name}: {state}  (default {row.get('default', 'off')})"
                if desc:
                    line += f" — {desc}"
                print(line)
            if undeclared_active:
                print(
                    "WARNING: active overlay(s) not declared (filter nothing): "
                    + ", ".join(undeclared_active)
                )
            print(f"on: {', '.join(current)}" if current else "on: (none)")
        else:
            # No declaration registry on this box: fall back to the on-only view.
            print(f"overlays on: {', '.join(current)}" if current else "overlays: (none)")
        return

    name = str(payload.get("name") or "")
    dry_run = bool(payload.get("dry_run"))
    now_on = name in current
    if action == "activate":
        state = "would activate" if dry_run else "activated"
    elif dry_run:
        state = "would turn on" if now_on else "would turn off"
    else:
        state = "on" if now_on else "off"
    print(f"overlay {name}: {state}")
    if current:
        print("all on:", ", ".join(current))
    removed = payload.get("unlinked") or []
    activations = payload.get("activations") or []
    if removed:
        print(f"unlinked: {len(removed)} symlinks")
    if activations:
        activation_label = "would activate" if dry_run and action == "activate" else "activated"
        print(f"{activation_label}: {len(activations)} skills")
        for activation in activations:
            _print_activation_packet_text(activation)


def _handle_overlay(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    action, name = _overlay_action_and_name(args)
    declared_records = _overlay_declared_records(model)
    # An on/off/toggle/activate of an UNDECLARED overlay fails here (before any
    # state write), printing the declared registry. list is unaffected.
    _guard_overlay_is_declared(action, name, declared_records)
    if bool(getattr(args, "dry_run", False)):
        was_on, current, now_on = _preview_overlay_action(action, name)
    else:
        was_on, current, now_on = _apply_persistent_overlay_action(action, name)
    overlay_cwd = _overlay_cwd(args)
    activations = _overlay_activations(args, model, action, name, overlay_cwd)
    removed = _overlay_removed_links(args, model, action, name, was_on, now_on, overlay_cwd)
    payload = _overlay_payload(
        args,
        action=action,
        name=name,
        current=current,
        overlay_cwd=overlay_cwd,
        removed=removed,
        activations=activations,
        declared_records=declared_records,
    )
    if args.format == "json":
        emit_json(payload)
    else:
        _print_overlay_text(payload)
    return EXIT_OK


def _handle_bootstrap(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    requested_tasks = select_tasks(model, getattr(args, "task", []))
    tasks = resolve_tasks_for_run(model, requested_tasks)
    if not args.dry_run:
        ensure_required_env_files_ready(select_env_files_for_tasks(model, tasks))
    task_results = run_tasks(
        model,
        tasks,
        dry_run=args.dry_run,
    )
    payload: dict[str, Any] = {
        "dry_run": args.dry_run,
        "sync_actions": sync_actions,
        "tasks": task_results,
        "next_actions": next_actions_for_bootstrap(task_results),
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print_service_actions_text(payload)
    return EXIT_OK


def _check_logs_deferred_surfaces(args: argparse.Namespace, model: dict[str, Any], raw_service_ids: list[str]) -> int | None:
    """Return EXIT_ERROR after emitting a structured deferred-surface envelope, else None.

    WG-006: for `logs` we must consult the parity ledger before
    select_services so that unknown-but-declared-deferred surfaces emit
    LOCAL_RUNTIME_SERVICE_DEFERRED instead of "Unknown service id"
    (shared.md:174-177, flows.md Flow 5).
    """
    if args.command != "logs" or not raw_service_ids:
        return None
    logs_classification = classify_requested_surfaces(model, raw_service_ids)
    if not logs_classification["deferred"]:
        return None
    cid_for_logs = _primary_client_id(args, model)
    profile_for_logs = (
        args.profile[0]
        if args.profile
        else (local_runtime_active_profile(model) or "local-minimal")
    )
    surface_id, item = logs_classification["deferred"][0]
    err_payload = build_local_runtime_service_deferred_error(
        item,
        client_id=cid_for_logs,
        profile=profile_for_logs,
        surface_id=surface_id,
    )
    if args.format == "json":
        emit_json(err_payload)
    else:
        print_local_runtime_error_text(err_payload)
    return EXIT_ERROR


def _emit_up_payload(
    args: argparse.Namespace,
    payload: dict[str, Any],
    exit_code: int,
    model: dict[str, Any] | None = None,
) -> int:
    if model is not None:
        _attach_runtime_access_payload(model, payload)
    if args.format == "json":
        emit_json(payload)
    elif exit_code != EXIT_OK and "error" in payload:
        print_local_runtime_error_text(payload)
    else:
        print_service_actions_text(payload)
        if model is not None and not getattr(args, "dry_run", False):
            try:
                from .endpoints import build_endpoint_summary

                started = {
                    s["id"]
                    for s in (payload.get("services") or [])
                    if s.get("id")
                    and s.get("result") in {"started", "already-running", "would-restart", "dry-run"}
                }
                summary = build_endpoint_summary(
                    model,
                    started or None,
                    box_access=payload.get("box_access") or {},
                )
                print_endpoint_summary(summary)
            except Exception:
                # Endpoint summary is purely informational; never let it fail
                # the `up` command.
                pass
    return exit_code


def _append_unique_warnings(payload: dict[str, Any], warnings: list[str]) -> None:
    existing = [
        str(value)
        for value in (payload.get("warnings") or [])
        if str(value).strip()
    ]
    seen = set(existing)
    for warning in warnings:
        if warning not in seen:
            existing.append(warning)
            seen.add(warning)
    payload["warnings"] = existing


def _attach_runtime_access_payload(model: dict[str, Any], payload: dict[str, Any]) -> None:
    box_access = payload.get("box_access") or runtime_box_access_from_env(model.get("env") or {})
    payload["box_access"] = box_access
    services = payload.get("services")
    if not isinstance(services, list):
        payload.setdefault("warnings", payload.get("warnings") or [])
        return
    try:
        from .endpoints import annotate_service_rows

        warnings = annotate_service_rows(model, services, box_access=box_access)
    except Exception:
        warnings = []
    _append_unique_warnings(payload, warnings)


def _primary_client_id(args: argparse.Namespace, model: dict[str, Any], default: str = "personal") -> str:
    requested_clients = [
        str(value).strip()
        for value in (getattr(args, "client", []) or [])
        if str(value).strip()
    ]
    if requested_clients:
        return requested_clients[0]
    active_clients = [
        str(value).strip()
        for value in (model.get("active_clients") or [])
        if str(value).strip()
    ]
    if active_clients:
        return active_clients[0]
    return default


def _handle_local_profile_up(
    args: argparse.Namespace,
    model: dict[str, Any],
    resolved_mode: str,
    active_local_profile: str,
) -> int:
    client_id_for_up = _primary_client_id(args, model)
    service_filter = [s for s in (getattr(args, "service", []) or []) if s]
    up_exit, up_payload = run_up(
        model=model,
        client_id=client_id_for_up,
        profile=active_local_profile,
        requested_mode=resolved_mode,
        service_filter=service_filter,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    return _emit_up_payload(args, up_payload, up_exit, model=model)


def _bridge_missing_payload(args: argparse.Namespace, bridge: dict[str, Any]) -> dict[str, Any]:
    profile_hint = f" --profile {' --profile '.join(args.profile)}" if args.profile else " --profile local-minimal"
    return local_runtime_error(
        "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
        f"Bridge {bridge['id']} outputs missing. Run 'manage.py focus' first.",
        recoverable=True,
        next_action=f"manage.py focus {args.client[0] if args.client else 'personal'}{profile_hint}",
    )


def _emit_missing_bridge_if_needed(args: argparse.Namespace, model: dict[str, Any]) -> int | None:
    bridges = model.get("bridges") or []
    if not bridges or args.dry_run:
        return None
    for bridge in bridges:
        if bridge_outputs_state(bridge)["state"] != "missing":
            continue
        return _emit_up_payload(args, _bridge_missing_payload(args, bridge), EXIT_ERROR)
    return None


def _legacy_up_payload(
    args: argparse.Namespace,
    model: dict[str, Any],
    requested_services: list[dict[str, Any]],
    resolved_mode: str,
) -> dict[str, Any]:
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    services = resolve_services_for_start(model, requested_services)
    bootstrap_tasks = resolve_tasks_for_services(model, services)
    if not args.dry_run:
        ensure_required_env_files_ready(
            select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, services)
        )
    task_results = run_tasks(model, bootstrap_tasks, dry_run=args.dry_run, mode=resolved_mode)
    service_results = start_services(
        model,
        services,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
        mode=resolved_mode,
    )
    return {
        "dry_run": args.dry_run,
        "requested_mode": resolved_mode,
        "effective_mode": resolved_mode,
        "sync_actions": sync_actions,
        "bootstrap_tasks": task_results,
        "services": service_results,
        "next_actions": next_actions_for_up(service_results),
    }


def _start_failure_ids(service_results: list[dict[str, Any]]) -> list[str]:
    failed_ids: list[str] = []
    for index, entry in enumerate(service_results, start=1):
        if entry.get("result") not in {"failed", "timeout", "blocked"}:
            continue
        service_id = str(entry.get("id") or entry.get("service") or f"service-{index}")
        failed_ids.append(service_id)
    return failed_ids


def _apply_start_failure_error(payload: dict[str, Any], service_results: list[dict[str, Any]]) -> int:
    failed_ids = _start_failure_ids(service_results)
    if not failed_ids:
        return EXIT_OK
    specific_error = first_service_error_payload(service_results)
    if specific_error is not None:
        payload.update(specific_error)
        return EXIT_ERROR
    payload.update(local_runtime_error(
        LOCAL_RUNTIME_START_BLOCKED,
        f"Some services did not become healthy: {', '.join(failed_ids)}",
        recoverable=True,
        blocked_services=failed_ids,
        next_action="manage.py status --format json",
    ))
    return EXIT_ERROR


def _handle_up(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    # Local-runtime profiles use workflows.run_up; legacy profiles keep the
    # inline path for compatibility with existing lifecycle behavior.
    active_local_profile = local_runtime_active_profile(model)
    if active_local_profile:
        return _handle_local_profile_up(args, model, resolved_mode, active_local_profile)

    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    deferred_exit = _emit_missing_bridge_if_needed(args, model)
    if deferred_exit is not None:
        return deferred_exit
    payload = _legacy_up_payload(args, model, requested_services, resolved_mode)
    exit_code = _apply_start_failure_error(payload, payload.get("services") or [])
    return _emit_up_payload(
        args,
        payload,
        exit_code,
        model=model,
    )


def _handle_down(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    services = resolve_services_for_stop(model, requested_services)
    service_results = stop_services(
        model,
        services,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    payload = {
        "dry_run": args.dry_run,
        "services": service_results,
        "next_actions": next_actions_for_down(),
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print_service_actions_text(payload)
    return EXIT_OK


def _handle_restart(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    stop_targets = resolve_services_for_stop(model, requested_services)
    start_targets = resolve_services_for_start(model, stop_targets)
    bootstrap_tasks = resolve_tasks_for_services(model, start_targets)
    stop_results = stop_services(
        model,
        stop_targets,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    sync_actions = sync_runtime(model, dry_run=args.dry_run)
    if not args.dry_run:
        ensure_required_env_files_ready(
            select_env_files_for_tasks(model, bootstrap_tasks) + select_env_files_for_services(model, start_targets)
        )
    task_results = run_tasks(
        model,
        bootstrap_tasks,
        dry_run=args.dry_run,
    )
    start_results = start_services(
        model,
        start_targets,
        dry_run=args.dry_run,
        wait_seconds=max(0.0, float(args.wait_seconds)),
    )
    payload = {
        "dry_run": args.dry_run,
        "stop_services": stop_results,
        "sync_actions": sync_actions,
        "bootstrap_tasks": task_results,
        "start_services": start_results,
        "next_actions": next_actions_for_up(start_results),
    }
    exit_code = _apply_start_failure_error(payload, start_results)
    if args.format == "json":
        emit_json(payload)
    else:
        print("stop:")
        print_service_actions_text({"services": stop_results})
        print()
        print_service_actions_text({"sync_actions": sync_actions, "tasks": task_results, "services": start_results})
        if exit_code != EXIT_OK and "error" in payload:
            print()
            print_local_runtime_error_text(payload)
    return exit_code


def _handle_logs(args: argparse.Namespace, root_dir: Path, model: dict[str, Any], resolved_mode: str) -> int:
    raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
    requested_services = select_services(model, raw_service_ids)
    logs_payload: dict[str, Any] = {
        "services": collect_service_logs(
            model,
            requested_services,
            line_count=max(0, int(args.lines)),
        ),
        "next_actions": ["status --format json"],
    }
    if args.format == "json":
        emit_json(logs_payload)
    else:
        print_service_logs_text(logs_payload)
    return EXIT_OK


_MODEL_DISPATCH: dict[str, Callable[[argparse.Namespace, Path, dict[str, Any], str], int]] = {
    "render": _handle_render,
    "ports": _handle_ports,
    "sync": _handle_sync,
    "context": _handle_context,
    "doctor": _handle_doctor,
    "status": _handle_status,
    "skills": _handle_skills,
    "skill-audit": _handle_skill_audit,
    "mcp-audit": _handle_mcp_audit,
    "mcp": _handle_mcp,
    "fleet": _handle_fleet,
    "evidence": _handle_evidence,
    "next": _handle_next,
    "graph": _handle_graph,
    "explain": _handle_explain,
    "search": _handle_search,
    "snap": _handle_snap,
    "parity-report": _handle_parity_report,
    "skill": _handle_skill,
    "overlay": _handle_overlay,
    "operator-booking": _handle_operator_booking,
    "bootstrap": _handle_bootstrap,
    "up": _handle_up,
    "down": _handle_down,
    "restart": _handle_restart,
    "logs": _handle_logs,
}


def _emit_mode_error(args: argparse.Namespace, mode_error: dict[str, Any]) -> int:
    if getattr(args, "format", "text") == "json":
        emit_json(mode_error)
    else:
        print_local_runtime_error_text(mode_error)
    return EXIT_ERROR


def _typed_error_payload(exc: SkillboxError, command: str) -> dict[str, Any]:
    """Render a SkillboxError into the back-compat envelope.

    Runs the message-pattern classifier (preserving recovery_hint/next_actions
    for known messages), then makes the typed code authoritative and layers in
    the structured context + the error's own next_actions. Code stays unchanged:
    the typed code is the same value classify_error derives for this message.
    """
    payload = classify_error(exc, command)
    error_obj = payload.setdefault("error", {})
    error_obj["code"] = exc.code
    error_obj["type"] = exc.code
    payload["error_code"] = exc.code
    if exc.context:
        error_obj["context"] = dict(exc.context)
    if exc.next_actions:
        error_obj["next_actions"] = list(exc.next_actions)
        payload["next_actions"] = list(exc.next_actions)
    return payload


def _emit_main_exception(args: argparse.Namespace, exc: Exception) -> int:
    is_json = getattr(args, "format", "text") == "json"
    verbose = bool(getattr(args, "verbose", False))

    # Runtime-id grammar violations are raised by the leaf runtime_model layer
    # as a plain RuntimeIdValidationError (it must not import the typed-error
    # hierarchy — runtime_manager imports runtime_model, not the reverse). Here,
    # at the runtime_manager boundary, we PROMOTE it to a typed ValidationError
    # so the surfaced envelope carries code RUNTIME_ID_INVALID + the structured
    # provenance (id/kind/source_file) + the rename playbook.
    if isinstance(exc, RuntimeIdValidationError):
        exc = ValidationError(
            exc.code,
            str(exc),
            context=exc.context,
            next_actions=exc.next_actions,
            recoverable=True,
        )

    # Typed errors carry their stable code + structured context. We still run
    # the message-pattern table (classify_error) so the recovery_hint and
    # next_actions affordances are preserved, then let the typed code/context be
    # authoritative. The typed code MUST equal what classify_error would derive
    # for the same message (codes are unchanged), so this only enriches.
    if isinstance(exc, SkillboxError):
        if is_json:
            emit_json(_typed_error_payload(exc, args.command))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # Legacy RuntimeError raisers still classify through the message table; the
    # enriched ``structured_error`` carrier gives them the new envelope keys.
    if isinstance(exc, RuntimeError):
        if is_json:
            emit_json(classify_error(exc, args.command))
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # Truly unexpected exception: generic INTERNAL envelope. Never leak the
    # traceback by default — only when --verbose is set.
    if is_json:
        context = {"traceback": traceback.format_exc()} if verbose else None
        emit_json(
            internal_error_payload(
                f"Unexpected error: {exc}",
                context=context,
                next_actions=["doctor --format json"],
            )
        )
    elif verbose:
        traceback.print_exc()
    else:
        print(f"Unexpected error: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _path_is_under_or_equal(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return candidate == root


def _runtime_path_from_repo(repo: dict[str, Any]) -> Path | None:
    raw_path = str(repo.get("host_path") or repo.get("path") or "").strip()
    if not raw_path:
        return None
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()


def _longest_matching_runtime_root(cwd: Path, repos: list[dict[str, Any]]) -> int:
    best_len = 0
    for repo in repos:
        root = _runtime_path_from_repo(repo)
        if root is None or not _path_is_under_or_equal(cwd, root):
            continue
        best_len = max(best_len, len(str(root)))
    return best_len


def _runtime_item_owned_by_client(
    item: dict[str, Any],
    repo_map: dict[str, dict[str, Any]],
    client_id: str,
) -> bool:
    item_client = str(item.get("client") or "").strip()
    if item_client:
        return item_client == client_id
    repo = repo_map.get(runtime_repo_reference_id(item))
    return str((repo or {}).get("client") or "").strip() == client_id


def _runtime_path_under_or_equal(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return candidate == root


def _runtime_safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _runtime_model_match_path(model: dict[str, Any], raw_path: Any) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    root_dir = Path(str(model.get("root_dir") or DEFAULT_ROOT_DIR))
    env = model.get("env") or {}
    try:
        translated = runtime_path_to_host_path(
            root_dir,
            env,
            os.path.expandvars(os.path.expanduser(value)),
            storage=model.get("storage"),
        )
        return _runtime_safe_resolve(Path(str(translated)))
    except Exception:
        return _runtime_safe_resolve(Path(os.path.expandvars(os.path.expanduser(value))))


def _runtime_repo_tail(raw_path: Any) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    parts = path.parts
    repo_index = -1
    for index, part in enumerate(parts):
        if part == "repos":
            repo_index = index
    if repo_index < 0 or repo_index + 1 >= len(parts):
        return None
    tail_parts = parts[repo_index + 1:]
    if not tail_parts:
        return None
    if tail_parts[0] in {".", ".."} or any(part == ".." for part in tail_parts):
        return None
    return Path(*tail_parts)


def _runtime_operator_repo_roots(model: dict[str, Any]) -> list[Path]:
    env = model.get("env") or {}
    raw_roots = [
        env.get("SKILLBOX_MONOSERVER_HOST_ROOT"),
        env.get("SKILLBOX_MONOSERVER_ROOT"),
        env.get("SKILLBOX_OPERATOR_REPOS_ROOT"),
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_root in raw_roots:
        path = _runtime_model_match_path(model, raw_root)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _runtime_model_match_paths(model: dict[str, Any], raw_path: Any) -> list[Path]:
    paths: list[Path] = []
    primary = _runtime_model_match_path(model, raw_path)
    if primary is not None:
        paths.append(primary)
    repo_tail = _runtime_repo_tail(raw_path)
    if repo_tail is not None:
        paths.extend(root / repo_tail for root in _runtime_operator_repo_roots(model))
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _runtime_client_match_paths(model: dict[str, Any], client: dict[str, Any]) -> list[Path]:
    raw_paths: list[Any] = [
        client.get("default_cwd_host_path"),
        client.get("default_cwd"),
    ]
    context = client.get("context") or {}
    if isinstance(context, dict):
        raw_matches = context.get("cwd_match") or []
        if isinstance(raw_matches, str):
            raw_matches = [raw_matches]
        raw_paths.extend(raw_matches)
        deploy = context.get("deploy") or {}
        if isinstance(deploy, dict):
            raw_paths.append(deploy.get("repo_root"))
    paths = [
        path
        for value in raw_paths
        for path in _runtime_model_match_paths(model, value)
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _runtime_client_name_matches_cwd(client_id: str, cwd: Path) -> bool:
    normalized_id = client_id.lower().replace("-", "_")
    if not normalized_id:
        return False
    normalized_name = cwd.name.lower().replace("-", "_")
    return normalized_name == normalized_id


def _runtime_client_matches_for_cwd(model: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    matches = list(matched_skill_clients(model, cwd))
    seen = {str(match.get("id") or "") for match in matches}
    resolved_cwd = _runtime_safe_resolve(cwd)
    extra_matches: list[dict[str, Any]] = []
    for client in model.get("clients") or []:
        client_id = str(client.get("id") or "").strip()
        if not client_id or client_id in seen:
            continue
        best_path = ""
        best_len = -1
        for path in _runtime_client_match_paths(model, client):
            if not _runtime_path_under_or_equal(resolved_cwd, path):
                continue
            path_text = str(path)
            if len(path_text) > best_len:
                best_path = path_text
                best_len = len(path_text)
        if not best_path and not _runtime_client_name_matches_cwd(client_id, resolved_cwd):
            continue
        extra_matches.append(
            {
                "id": client_id,
                "label": str(client.get("label") or client_id),
                "match": best_path or str(resolved_cwd),
            }
        )
        seen.add(client_id)
    # `matched_skill_clients` already orders its results by a richer key
    # (path-name match, match length, default_cwd match, then id). Sort the
    # combined list *stably* on match length alone so that ties preserve that
    # upstream ordering instead of collapsing back to an alphabetical id
    # tiebreak — otherwise a client whose default_cwd is exactly this directory
    # could lose to an alphabetically earlier client that merely shares a
    # cwd_match prefix.
    return sorted(
        matches + extra_matches,
        key=lambda item: -len(str(item.get("match") or "")),
    )


def _local_runtime_services_for_profiles(
    model: dict[str, Any],
    active_profiles: set[str],
) -> list[dict[str, Any]]:
    services_by_id: dict[str, dict[str, Any]] = {}
    for profile in sorted(active_profiles):
        if not profile.startswith("local-"):
            continue
        for service in select_local_runtime_services(model, profile):
            service_id = str(service.get("id") or "").strip()
            if service_id:
                services_by_id.setdefault(service_id, service)
    return list(services_by_id.values())


def _runtime_client_cwd_score(
    model: dict[str, Any],
    active_profiles: set[str],
    client_id: str,
    cwd: Path,
) -> tuple[int, ...]:
    repo_map = runtime_repo_map(model)
    client_services = [
        service
        for service in _local_runtime_services_for_profiles(model, active_profiles)
        if _runtime_item_owned_by_client(service, repo_map, client_id)
    ]
    client_repos = [
        repo
        for repo in model.get("repos") or []
        if str(repo.get("client") or "").strip() == client_id
    ]
    service_repos = [
        repo_map[repo_id]
        for service in client_services
        for repo_id in [runtime_repo_reference_id(service)]
        if repo_id in repo_map
    ]
    matching_service_root_len = _longest_matching_runtime_root(cwd, service_repos)
    matching_client_root_len = _longest_matching_runtime_root(cwd, client_repos)
    manageable_count = sum(
        1 for service in client_services
        if service_supports_lifecycle(service, model)[0]
    )
    return (
        int(matching_service_root_len > 0),
        matching_service_root_len,
        int(bool(client_services)),
        int(matching_client_root_len > 0),
        matching_client_root_len,
        manageable_count,
        len(client_services),
    )


def _runtime_client_for_cwd(
    args: argparse.Namespace,
    model: dict[str, Any],
    cwd: Path,
    matches: list[dict[str, Any]],
) -> str:
    # Prefer the client whose `cwd_match` prefix is the most specific (longest
    # expanded match), regardless of whether the client owns a managed
    # service. The broader `personal` overlay covers ~/repos and would
    # otherwise win for every repo because it owns the canonical service
    # definition for each one. Falls back to the service-ownership signal
    # only as a tiebreak.
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    ranked: list[tuple[tuple[int, int], int, tuple[int, ...], str]] = []
    for index, match in enumerate(matches):
        client_id = str(match.get("id") or "").strip()
        if not client_id:
            continue
        filtered = filter_model(
            model,
            active_profiles,
            normalize_active_clients(model, [client_id]),
        )
        runtime_score = _runtime_client_cwd_score(filtered, active_profiles, client_id, cwd)
        match_len = len(str(match.get("match") or ""))
        ranked.append((
            (match_len, runtime_score[0]),
            -index,
            runtime_score,
            client_id,
        ))
    if not ranked:
        return str(matches[0]["id"])
    return max(ranked)[3]


def _active_clients_for_args(args: argparse.Namespace, model: dict[str, Any]) -> list[str]:
    requested_clients = getattr(args, "client", [])
    positional_client = str(getattr(args, "client_id", "") or "").strip()
    if args.command == "parity-report" and positional_client and not requested_clients:
        requested_clients = [positional_client]
    active_clients = normalize_active_clients(model, requested_clients)
    cwd_inferred_commands = {
        "skills",
        "skill",
        "overlay",
        "mcp-audit",
        "next",
        "graph",
        "explain",
        "search",
        "snap",
        "parity-report",
        "ports",
        "bootstrap",
        "up",
        "down",
        "restart",
        "status",
        "logs",
        "operator-booking",
    }
    if args.command not in cwd_inferred_commands or requested_clients:
        return active_clients
    raw_cwd = getattr(args, "cwd", None)
    skill_cwd = Path(raw_cwd or os.getcwd())
    matches = _runtime_client_matches_for_cwd(model, skill_cwd)
    if not matches:
        return active_clients
    if args.command in {"bootstrap", "up", "down", "restart", "status", "logs"}:
        return normalize_active_clients(model, [_runtime_client_for_cwd(args, model, skill_cwd.resolve(), matches)])
    return normalize_active_clients(model, [matches[0]["id"]])


def _filtered_model_for_args(args: argparse.Namespace, root_dir: Path) -> dict[str, Any]:
    model = build_runtime_model(root_dir)
    active_profiles = normalize_active_profiles(getattr(args, "profile", []))
    return filter_model(model, active_profiles, _active_clients_for_args(args, model))


def _dispatch_model_command(
    args: argparse.Namespace,
    root_dir: Path,
    resolved_mode: str,
    model_handler: Callable[[argparse.Namespace, Path, dict[str, Any], str], int],
) -> int:
    try:
        model = _filtered_model_for_args(args, root_dir)
        raw_service_ids = [s for s in (getattr(args, "service", []) or []) if s]
        deferred_exit = _check_logs_deferred_surfaces(args, model, raw_service_ids)
        if deferred_exit is not None:
            return deferred_exit
        return model_handler(args, root_dir, model, resolved_mode)
    except Exception as exc:
        return _emit_main_exception(args, exc)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    normalized_argv, diagnostics = _normalize_agent_argv(argv)
    args = parser.parse_args(normalized_argv)
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)
    if getattr(args, "robot_triage", False):
        args.command = "robot-triage"
        args.format = "json"
    _apply_default_command(args)
    root_dir = resolve_root_dir(args.root_dir)

    resolved_mode, mode_error = _resolve_local_runtime_mode(args)
    if mode_error is not None:
        return _emit_mode_error(args, mode_error)

    early_handler = _EARLY_DISPATCH.get(args.command)
    if early_handler is not None:
        return early_handler(args, root_dir)

    model_handler = _MODEL_DISPATCH.get(args.command)
    if model_handler is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return EXIT_ERROR
    return _dispatch_model_command(args, root_dir, resolved_mode, model_handler)
