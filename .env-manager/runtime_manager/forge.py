from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

FORGE_INIT_SETTINGS_LOCKED = "FORGE_INIT_SETTINGS_LOCKED"
FORGE_INIT_SETTINGS_INVALID = "FORGE_INIT_SETTINGS_INVALID"
FORGE_INIT_CODEX_TMUX_MISSING = "FORGE_INIT_CODEX_TMUX_MISSING"
FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED = "FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED"
FORGE_INIT_CRON_UNAVAILABLE = "FORGE_INIT_CRON_UNAVAILABLE"
CODEX_TMUX_MARKER_START = "# skillbox-forge-scoring-start"
CODEX_TMUX_MARKER_END = "# skillbox-forge-scoring-end"
CRON_MARKER = "# skillbox-forge-score-session"


class ForgeInitError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_scoring_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "score-session.sh"


def _home_dir(home: Path | str | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _scoring_command(scoring_script: Path, source: str, extra: list[str] | None = None) -> str:
    parts = ["bash", str(scoring_script), "--source", source]
    if extra:
        parts.extend(extra)
    return " ".join(shlex.quote(part) for part in parts)


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ForgeInitError(FORGE_INIT_SETTINGS_INVALID, f"settings file is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ForgeInitError(FORGE_INIT_SETTINGS_INVALID, f"settings file must contain a JSON object: {path}")
    return value


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.W_OK):
        raise ForgeInitError(FORGE_INIT_SETTINGS_LOCKED, f"settings file is not writable: {path}")
    if not path.exists() and path.parent.exists() and not os.access(path.parent, os.W_OK):
        raise ForgeInitError(FORGE_INIT_SETTINGS_LOCKED, f"settings directory is not writable: {path.parent}")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hook_command_matches(hook: Any, scoring_script: Path) -> bool:
    if not isinstance(hook, dict):
        return False
    command = str(hook.get("command") or "")
    return str(scoring_script) in command and "score-session.sh" in command


def _session_end_hook_present(entries: Any, scoring_script: Path) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if isinstance(hooks, list) and any(_hook_command_matches(hook, scoring_script) for hook in hooks):
            return True
        if _hook_command_matches(entry, scoring_script):
            return True
    return False


def ensure_session_end_hook(settings_path: Path, scoring_script: Path) -> dict[str, Any]:
    settings = _load_json_object(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    session_end = hooks.setdefault("SessionEnd", [])
    if not isinstance(session_end, list):
        session_end = []
        hooks["SessionEnd"] = session_end

    if _session_end_hook_present(session_end, scoring_script):
        action = "already_present"
    else:
        session_end.append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": _scoring_command(scoring_script, "claude"),
                    }
                ],
            }
        )
        action = "added"
        _write_json_object(settings_path, settings)

    if action == "already_present" and not settings_path.exists():
        _write_json_object(settings_path, settings)

    return {"path": str(settings_path), "action": action}


def _codex_tmux_block(scoring_script: Path) -> str:
    command = (
        f"bash {shlex.quote(str(scoring_script))} --source codex "
        '--session-id "$SESSION"'
    )
    return "\n".join(
        [
            f"        {CODEX_TMUX_MARKER_START}",
            f"        {command} >/dev/null 2>&1 || true",
            f"        {CODEX_TMUX_MARKER_END}",
        ]
    )


def patch_codex_tmux_wrapper(run_py: Path, scoring_script: Path) -> dict[str, Any]:
    if not run_py.exists():
        return {
            "path": str(run_py),
            "action": "missing",
            "warning": FORGE_INIT_CODEX_TMUX_MISSING,
        }

    text = run_py.read_text(encoding="utf-8")
    if CODEX_TMUX_MARKER_START in text:
        return {"path": str(run_py), "action": "already_present"}

    needle = '        echo "Result written to: $RESULT_FILE"'
    if needle not in text:
        return {
            "path": str(run_py),
            "action": "skipped",
            "warning": FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED,
        }

    updated = text.replace(needle, needle + "\n\n" + _codex_tmux_block(scoring_script), 1)
    run_py.write_text(updated, encoding="utf-8")
    return {"path": str(run_py), "action": "patched"}


def ensure_cron_entry(scoring_script: Path, *, subprocess_run: Any = subprocess.run) -> dict[str, Any]:
    command = _scoring_command(scoring_script, "both", ["--since", "week"])
    entry = f"*/10 * * * * {command} >/dev/null 2>&1 {CRON_MARKER}"
    current = subprocess_run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if current.returncode not in (0, 1):
        return {"action": "skipped", "warning": FORGE_INIT_CRON_UNAVAILABLE}
    existing = current.stdout or ""
    if CRON_MARKER in existing:
        return {"action": "already_present"}
    new_crontab = existing.rstrip()
    if new_crontab:
        new_crontab += "\n"
    new_crontab += entry + "\n"
    write = subprocess_run(["crontab", "-"], input=new_crontab, capture_output=True, text=True, check=False)
    if write.returncode != 0:
        return {
            "action": "skipped",
            "warning": FORGE_INIT_CRON_UNAVAILABLE,
            "stderr": write.stderr.strip(),
        }
    return {"action": "added"}


def forge_init(
    with_cron: bool = False,
    scoring_script: Path | str | None = None,
    *,
    home: Path | str | None = None,
    codex_tmux_run_py: Path | str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    home_dir = _home_dir(home)
    score_path = Path(scoring_script).expanduser() if scoring_script else default_scoring_script()
    score_path = score_path.resolve()
    settings_path = home_dir / ".claude" / "settings.json"
    codex_tmux_path = (
        Path(codex_tmux_run_py).expanduser()
        if codex_tmux_run_py is not None
        else home_dir / ".claude" / "skills" / "codex-tmux" / "scripts" / "run.py"
    )

    payload: dict[str, Any] = {
        "ok": True,
        "scoring_script": str(score_path),
        "settings": ensure_session_end_hook(settings_path, score_path),
        "codex_tmux": patch_codex_tmux_wrapper(codex_tmux_path, score_path),
        "cron": {"action": "skipped", "reason": "not_requested"},
        "warnings": [],
    }

    if with_cron:
        payload["cron"] = ensure_cron_entry(score_path, subprocess_run=subprocess_run)

    for section in ("codex_tmux", "cron"):
        warning = payload[section].get("warning")
        if warning:
            payload["warnings"].append(warning)
    return payload
