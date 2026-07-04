from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests.helpers import make_fake_binary


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
GUARD_SCRIPT = SCRIPTS_DIR / "guard-destructive-op.sh"
SETTINGS_PATH = ROOT_DIR / ".claude" / "settings.json"
SESSION_ID = "safety-matrix-session"
BOX_ID = "box01"
BOX_EXEC_COMMAND = "touch /tmp/safety-matrix"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

OPERATOR = SourceFileLoader(
    "skillbox_operator_mcp_server_safety_matrix",
    str((SCRIPTS_DIR / "operator_mcp_server.py").resolve()),
).load_module()

TOOLS = ("teardown", "compose_down", "provision", "box_exec")
MARKERS = ("absent", "fresh", "expired")
REPOS = ("clean+pushed", "dirty", "unpushed", "scan-error-injected")
TRANSPORTS = ("ok", "ssh-timeout")
HOOK_TOOLS = {
    "teardown": "mcp__skillbox-operator__operator_teardown",
    "compose_down": "mcp__skillbox-operator__operator_compose_down",
    "box_exec": "mcp__skillbox-operator__operator_box_exec",
}
SERVER_TOOLS = {
    "teardown": "operator_teardown",
    "compose_down": "operator_compose_down",
    "provision": "operator_provision",
    "box_exec": "operator_box_exec",
}


@dataclass(frozen=True)
class MatrixCell:
    tool: str
    marker: str
    repo: str
    transport: str

    @property
    def id(self) -> str:
        return f"{self.tool}|marker={self.marker}|repo={self.repo}|transport={self.transport}"


@dataclass(frozen=True)
class Expected:
    allow: bool
    reason: str = "allow"

    @property
    def label(self) -> str:
        return "allow" if self.allow else f"block({self.reason})"


@dataclass(frozen=True)
class GateOutcome:
    allow: bool
    reason: str
    detail: str


MATRIX = [
    MatrixCell(tool=tool, marker=marker, repo=repo, transport=transport)
    for tool in TOOLS
    for marker in MARKERS
    for repo in REPOS
    for transport in TRANSPORTS
]


def expected_for(cell: MatrixCell) -> Expected:
    if cell.tool in {"teardown", "compose_down"}:
        if cell.repo == "scan-error-injected":
            return Expected(False, "repo-scan-error")
        if cell.tool == "teardown" and cell.transport == "ssh-timeout":
            return Expected(False, "ssh-timeout")
        if cell.repo == "dirty":
            return Expected(False, "repo-dirty")
        if cell.repo == "unpushed":
            return Expected(False, "repo-unpushed")
        if cell.marker != "fresh":
            return Expected(False, _marker_reason(cell.marker))
        return Expected(True)

    if cell.tool == "box_exec":
        if cell.marker != "fresh":
            return Expected(False, _marker_reason(cell.marker))
        if cell.transport == "ssh-timeout":
            return Expected(False, "ssh-timeout")
        return Expected(True)

    if cell.tool == "provision":
        if cell.marker != "fresh":
            return Expected(False, _marker_reason(cell.marker))
        return Expected(True)

    raise AssertionError(f"unhandled tool: {cell.tool}")


def _marker_reason(marker: str) -> str:
    return {
        "absent": "marker-absent",
        "expired": "marker-expired",
        "wrong-session": "marker-session-mismatch",
        "wrong-command-hash": "marker-wrong-command-hash",
    }[marker]


def _render_matrix_contract() -> str:
    lines = ["safety gate matrix contract:"]
    for tool in TOOLS:
        lines.append(f"  {tool}:")
        for marker in MARKERS:
            counts: dict[str, int] = {}
            for cell in MATRIX:
                if cell.tool != tool or cell.marker != marker:
                    continue
                label = expected_for(cell).label
                counts[label] = counts.get(label, 0) + 1
            summary = ", ".join(f"{label}={count}" for label, count in sorted(counts.items()))
            lines.append(f"    marker={marker}: {summary}")
    return "\n".join(lines)


if any(arg == "-v" or (arg.startswith("-") and "v" in arg) for arg in sys.argv):
    sys.__stderr__.write(_render_matrix_contract() + "\n")


def _copy_guard(root: Path) -> Path:
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True)
    target = scripts_dir / "guard-destructive-op.sh"
    target.write_text(GUARD_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def _marker_subject(tool: str) -> tuple[str, str]:
    if tool == "compose_down":
        return "operator_compose_down", "local"
    if tool == "teardown":
        return "operator_teardown", BOX_ID
    if tool == "provision":
        return "operator_provision", BOX_ID
    if tool == "box_exec":
        return "operator_box_exec", OPERATOR._box_exec_marker_key(BOX_ID, BOX_EXEC_COMMAND)  # noqa: SLF001
    raise AssertionError(f"unhandled tool: {tool}")


def _write_marker(root: Path, cell: MatrixCell) -> None:
    if cell.marker == "absent":
        return
    tool_name, key = _marker_subject(cell.tool)
    marker_dir = root / ".skillbox-state" / "dryrun-markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f".skillbox-dryrun-{tool_name}-{key}"
    payload = {
        "tool": tool_name,
        "key": key,
        "session": SESSION_ID,
        "created_at": OPERATOR._utc_timestamp(),  # noqa: SLF001
    }
    if cell.marker == "expired":
        payload["created_at"] = "2000-01-01T00:00:00Z"
    elif cell.marker == "wrong-session":
        payload["session"] = "other-session"
    elif cell.marker == "wrong-command-hash":
        if cell.tool == "box_exec":
            wrong_key = OPERATOR._box_exec_marker_key(BOX_ID, "touch /tmp/other-command")  # noqa: SLF001
            marker = marker_dir / f".skillbox-dryrun-{tool_name}-{wrong_key}"
            payload["key"] = wrong_key
        else:
            payload["key"] = "wrong-key"
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    if cell.marker == "expired":
        old = 946684800
        os.utime(marker, (old, old))


def _write_inventory(root: Path) -> None:
    inventory = root / "workspace" / "boxes.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps(
            {
                "boxes": [
                    {
                        "id": BOX_ID,
                        "state": "ready",
                        "tailscale_ip": "100.64.0.9",
                        "ssh_user": "skillbox",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _make_fake_git(bin_dir: Path, repo_state: str) -> None:
    make_fake_binary(
        bin_dir,
        "git",
        f"""
cmd="$*"
case "$cmd" in
  *"status --porcelain"*)
    if [ "{repo_state}" = "dirty" ]; then
      printf ' M dirty.txt\\n'
    fi
    exit 0
    ;;
  *"rev-parse --abbrev-ref @{{upstream}}"*)
    printf 'origin/main\\n'
    exit 0
    ;;
  *"log @{{upstream}}..HEAD --oneline"*)
    if [ "{repo_state}" = "unpushed" ]; then
      printf 'abc123 ahead\\n'
    fi
    exit 0
    ;;
  *"log --oneline -1"*)
    printf 'abc123 init\\n'
    exit 0
    ;;
esac
exit 0
""",
    )


def _make_python_scan_shim(bin_dir: Path, repo_state: str) -> None:
    if repo_state != "scan-error-injected":
        return
    real_python = shutil.which("python3") or sys.executable
    make_fake_binary(
        bin_dir,
        "python3",
        f"""
for arg in "$@"; do
  case "$arg" in
    *discover_git_roots*)
      echo 'simulated repo scan failure' >&2
      exit 99
      ;;
  esac
done
exec "{real_python}" "$@"
""",
    )


def _make_fake_ssh(bin_dir: Path, transport: str) -> Path:
    if transport == "ssh-timeout":
        return make_fake_binary(bin_dir, "ssh", "exit 124\n")
    return make_fake_binary(bin_dir, "ssh", "exit 0\n")


def _hook_tool_input(cell: MatrixCell) -> dict[str, object]:
    if cell.tool == "compose_down":
        return {"dry_run": False}
    if cell.tool == "teardown":
        return {"dry_run": False, "box_id": BOX_ID}
    if cell.tool == "box_exec":
        return {"dry_run": False, "box_id": BOX_ID, "command": BOX_EXEC_COMMAND}
    raise AssertionError(f"tool has no hook: {cell.tool}")


def _run_hook(root: Path, cell: MatrixCell) -> GateOutcome:
    script = _copy_guard(root)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _make_fake_git(bin_dir, cell.repo)
    _make_python_scan_shim(bin_dir, cell.repo)
    fake_ssh = _make_fake_ssh(bin_dir, cell.transport)
    (root / ".git").mkdir()
    (root / "empty-clients").mkdir()
    (root / "empty-mono").mkdir()
    _write_inventory(root)
    payload = {"tool_name": HOOK_TOOLS[cell.tool], "tool_input": _hook_tool_input(cell)}
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_SESSION_ID": SESSION_ID,
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "SKILLBOX_CLIENTS_HOST_ROOT": str(root / "empty-clients"),
            "SKILLBOX_MONOSERVER_HOST_ROOT": str(root / "empty-mono"),
            "SKILLBOX_GUARD_SSH_BIN": str(fake_ssh),
            "SKILLBOX_GUARD_SSH_TIMEOUT_SECONDS": "1",
            "SKILLBOX_DRYRUN_MARKER_TTL_SECONDS": "600",
        }
    )
    result = subprocess.run(
        ["bash", str(script)],
        input=json.dumps(payload),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        return GateOutcome(True, "allow", result.stderr)
    return GateOutcome(False, _reason_from_text(result.stderr, cell), result.stderr)


def _server_params(cell: MatrixCell) -> dict[str, object]:
    if cell.tool == "compose_down":
        return {"dry_run": False}
    if cell.tool in {"teardown", "provision"}:
        return {"box_id": BOX_ID, "dry_run": False}
    if cell.tool == "box_exec":
        return {"box_id": BOX_ID, "command": BOX_EXEC_COMMAND, "dry_run": False, "timeout": 3}
    raise AssertionError(f"unhandled tool: {cell.tool}")


def _content_payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def _server_success_boundary(cell: MatrixCell) -> tuple[bool, int, dict[str, Any]]:
    return True, 0, {"ok": True, "tool": cell.tool}


def _run_server(root: Path, cell: MatrixCell, monkeypatch: pytest.MonkeyPatch) -> GateOutcome:
    monkeypatch.setenv("CLAUDE_SESSION_ID", SESSION_ID)
    monkeypatch.setenv("SKILLBOX_DRYRUN_MARKER_TTL_SECONDS", "600")
    monkeypatch.setattr(OPERATOR, "REPO_ROOT", root)
    monkeypatch.setattr(OPERATOR, "BOX_PY", root / "scripts" / "box.py")
    monkeypatch.setattr(OPERATOR, "RECONCILE_PY", root / "scripts" / "04-reconcile.py")
    OPERATOR._DRYRUN_MARKER_STATUS_CACHE.clear()  # noqa: SLF001
    _write_inventory(root)
    handler = OPERATOR._DISPATCH[SERVER_TOOLS[cell.tool]]  # noqa: SLF001

    with (
        mock.patch.object(OPERATOR, "emit_event"),
        mock.patch.object(OPERATOR, "emit_box_exec_audit"),
        mock.patch.object(OPERATOR, "run_script", return_value=_server_success_boundary(cell)),
        mock.patch.object(OPERATOR, "run_compose", return_value=_server_success_boundary(cell)),
    ):
        if cell.tool == "box_exec":
            if cell.transport == "ssh-timeout":
                ssh_result = (
                    False,
                    -1,
                    {
                        "error": {
                            "type": "timeout",
                            "message": "SSH command timed out after 3s.",
                            "recoverable": True,
                        }
                    },
                )
            else:
                ssh_result = _server_success_boundary(cell)
            with mock.patch.object(OPERATOR, "run_ssh", return_value=ssh_result):
                result = handler(_server_params(cell))
        else:
            result = handler(_server_params(cell))

    payload = _content_payload(result)
    if not result.get("isError"):
        return GateOutcome(True, "allow", json.dumps(payload, sort_keys=True))
    return GateOutcome(False, _reason_from_payload(payload, cell), json.dumps(payload, sort_keys=True))


def _run_cell(cell: MatrixCell, monkeypatch: pytest.MonkeyPatch) -> GateOutcome:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_marker(root, cell)
        if cell.tool in HOOK_TOOLS:
            hook_outcome = _run_hook(root, cell)
            if not hook_outcome.allow:
                return hook_outcome
        return _run_server(root, cell, monkeypatch)


def _reason_from_payload(payload: dict[str, Any], cell: MatrixCell) -> str:
    error = payload.get("error") or {}
    error_type = str(error.get("type") or "")
    marker = error.get("marker") if isinstance(error, dict) else None
    marker_reason = str((marker or {}).get("reason") or "")
    if error_type == "dry_run_required":
        return _normalize_marker_reason(marker_reason, cell)
    if error_type == "timeout":
        return "ssh-timeout"
    return error_type or "server-error"


def _reason_from_text(text: str, cell: MatrixCell) -> str:
    if "timed out" in text and "remote repos" in text:
        return "ssh-timeout"
    if "repo cleanliness" in text and "could not evaluate" in text:
        return "repo-scan-error"
    if "Uncommitted changes in:" in text:
        return "repo-dirty"
    if "Unpushed commits in:" in text:
        return "repo-unpushed"
    marker = "Rejection reason: "
    if marker in text:
        raw = text.split(marker, 1)[1].split(" ", 1)[0].strip()
        return _normalize_marker_reason(raw, cell)
    return "hook-block"


def _normalize_marker_reason(raw: str, cell: MatrixCell) -> str:
    if cell.marker == "wrong-command-hash" and raw in {"payload-mismatch", "absent"}:
        return "marker-wrong-command-hash"
    return {
        "absent": "marker-absent",
        "expired": "marker-expired",
        "session-mismatch": "marker-session-mismatch",
        "payload-mismatch": "marker-wrong-command-hash",
    }.get(raw, f"marker-{raw or 'unknown'}")


@pytest.mark.parametrize("cell", MATRIX, ids=[cell.id for cell in MATRIX])
def test_safety_gate_matrix(cell: MatrixCell, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = expected_for(cell)
    outcome = _run_cell(cell, monkeypatch)
    assert outcome.allow is expected.allow, outcome.detail
    assert outcome.reason == expected.reason, outcome.detail


def test_matrix_covers_declared_dimensions() -> None:
    assert len(MATRIX) == len(TOOLS) * len(MARKERS) * len(REPOS) * len(TRANSPORTS)
    assert {cell.tool for cell in MATRIX} == set(TOOLS)
    assert {cell.marker for cell in MATRIX} == set(MARKERS)
    assert {cell.repo for cell in MATRIX} == set(REPOS)
    assert {cell.transport for cell in MATRIX} == set(TRANSPORTS)


def test_hook_settings_cover_gated_operator_tools() -> None:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    hooks = settings["hooks"]["PreToolUse"]
    matchers = {entry.get("matcher") for entry in hooks}
    assert "mcp__skillbox-operator__operator_teardown" in matchers
    assert "mcp__skillbox-operator__operator_compose_down" in matchers
    assert "mcp__skillbox-operator__operator_box_exec" in matchers
    assert "mcp__skillbox-operator__operator_provision" not in matchers
