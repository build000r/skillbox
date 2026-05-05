#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CODEX_TIMEOUT_SECONDS = 240.0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _worker_result(
    *,
    run_id: str,
    state: str,
    summary: str,
    findings: list[str] | None = None,
    actions_taken: list[str] | None = None,
    next_action: str = "",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "state": state,
        "summary": summary,
        "findings": findings or [],
        "actions_taken": actions_taken or [],
        "next_action": next_action,
    }


def _env_path(name: str) -> Path | None:
    raw_value = str(os.environ.get(name) or "").strip()
    return Path(raw_value).expanduser() if raw_value else None


def _codex_bin() -> str | None:
    configured = str(os.environ.get("SKILLBOX_HERMES_CODEX_BIN") or "").strip()
    if configured:
        return configured
    return shutil.which("codex")


def _timeout_seconds() -> float:
    raw_value = str(os.environ.get("SKILLBOX_HERMES_CODEX_TIMEOUT_SECONDS") or "").strip()
    if not raw_value:
        return DEFAULT_CODEX_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        return DEFAULT_CODEX_TIMEOUT_SECONDS


def _task_prompt(task_payload: dict[str, Any], run_id: str) -> str:
    task_spec = task_payload.get("task_spec") if isinstance(task_payload.get("task_spec"), dict) else {}
    resolved_context = (
        task_payload.get("resolved_context") if isinstance(task_payload.get("resolved_context"), dict) else {}
    )
    instruction = str(task_spec.get("instruction") or "").strip()
    task_class = str(task_spec.get("task_class") or "analysis").strip()
    client_id = str(resolved_context.get("client_id") or "").strip()
    repo_id = str(resolved_context.get("repo_id") or "").strip()

    context_lines = [
        f"Run id: {run_id}",
        f"Task class: {task_class}",
        f"Client id: {client_id or '-'}",
        f"Repo id: {repo_id or '-'}",
        "",
        "Instruction:",
        instruction,
        "",
        "Return a concise worker completion report. Stay read-only. Do not edit files.",
    ]
    return "\n".join(context_lines)


def _effective_cwd(root_dir: Path, task_payload: dict[str, Any]) -> Path:
    resolved_context = (
        task_payload.get("resolved_context") if isinstance(task_payload.get("resolved_context"), dict) else {}
    )
    raw_cwd = str(resolved_context.get("effective_cwd") or "").strip()
    if raw_cwd:
        cwd = Path(raw_cwd).expanduser()
        if cwd.is_dir():
            return cwd
    return root_dir


def _codex_command(codex_bin: str, cwd: Path, output_path: Path, prompt: str) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--cd",
        str(cwd),
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
    ]
    model = str(os.environ.get("SKILLBOX_HERMES_CODEX_MODEL") or "").strip()
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    return command


def _run_codex(command: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _read_summary(output_path: Path, fallback: str) -> str:
    try:
        summary = output_path.read_text(encoding="utf-8").strip()
    except OSError:
        summary = ""
    return summary or fallback


def _execute_worker(task_path: Path, result_path: Path, root_dir: Path, run_id: str) -> int:
    task_payload = _load_json(task_path)
    codex_bin = _codex_bin()
    if not codex_bin:
        _write_json(
            result_path,
            _worker_result(
                run_id=run_id,
                state="failed",
                summary="Codex CLI is not installed or configured for the Hermes adapter.",
                next_action="Install codex or set SKILLBOX_HERMES_CODEX_BIN.",
            ),
        )
        return 0

    cwd = _effective_cwd(root_dir, task_payload)
    prompt = _task_prompt(task_payload, run_id)
    with tempfile.TemporaryDirectory(prefix=f"{run_id}-codex-") as tmpdir:
        output_path = Path(tmpdir) / "last-message.md"
        command = _codex_command(codex_bin, cwd, output_path, prompt)
        try:
            process = _run_codex(command, _timeout_seconds())
        except (OSError, subprocess.SubprocessError) as exc:
            _write_json(
                result_path,
                _worker_result(
                    run_id=run_id,
                    state="failed",
                    summary=f"Codex-backed Hermes adapter failed to start: {exc}",
                    next_action="Inspect SKILLBOX_HERMES_CODEX_BIN and local Codex authentication.",
                ),
            )
            return 0

        if process.returncode != 0:
            stderr = (process.stderr or "").strip()
            detail = f" stderr: {stderr[-1000:]}" if stderr else ""
            _write_json(
                result_path,
                _worker_result(
                    run_id=run_id,
                    state="failed",
                    summary=f"Codex-backed Hermes adapter exited with code {process.returncode}.{detail}",
                    next_action="Inspect the worker run result and Codex CLI output.",
                ),
            )
            return 0

        summary = _read_summary(output_path, "Codex-backed Hermes adapter completed.")
        _write_json(
            result_path,
            _worker_result(
                run_id=run_id,
                state="succeeded",
                summary=summary,
                actions_taken=["Ran codex exec through the Skillbox Hermes adapter."],
            ),
        )
        return 0


def main() -> int:
    task_path = _env_path("SKILLBOX_WORKER_TASK_PATH")
    result_path = _env_path("SKILLBOX_WORKER_RESULT_PATH")
    root_dir = _env_path("SKILLBOX_ROOT_DIR") or Path.cwd()
    run_id = str(os.environ.get("SKILLBOX_WORKER_RUN_ID") or "").strip() or "unknown"
    if task_path is None or result_path is None:
        print("SKILLBOX_WORKER_TASK_PATH and SKILLBOX_WORKER_RESULT_PATH are required", file=sys.stderr)
        return 2
    return _execute_worker(task_path, result_path, root_dir, run_id)


if __name__ == "__main__":
    raise SystemExit(main())
