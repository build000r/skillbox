from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_SWIMMERS_URL = "http://127.0.0.1:3210"
TOKEN_ENV_CANDIDATES = ("AUTH_TOKEN", "SWIMMERS_AUTH_TOKEN", "SWIMMERS_OPERATOR_TOKEN")


class SwimmersLaunchError(RuntimeError):
    pass


def _base_url(raw_base_url: str | None) -> str:
    raw = (
        raw_base_url
        or os.environ.get("SWIMMERS_URL")
        or os.environ.get("SWIMMERS_TUI_URL")
        or os.environ.get("SKILLBOX_SWIMMERS_URL")
        or DEFAULT_SWIMMERS_URL
    )
    raw = raw.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SwimmersLaunchError(f"invalid swimmers URL: {raw!r}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _resolve_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return os.path.normpath(str(path))


def _read_lines_file(path_arg: str, invoke_cwd: Path) -> list[str]:
    path = Path(_resolve_path(path_arg, invoke_cwd))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SwimmersLaunchError(f"failed to read {path}: {exc}") from exc
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _resolve_prompt(
    request: str | None,
    request_file: str | None,
    invoke_cwd: Path,
) -> str | None:
    if request and request_file:
        raise SwimmersLaunchError("pass either --request or --request-file, not both")
    if request_file:
        path = Path(_resolve_path(request_file, invoke_cwd))
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SwimmersLaunchError(f"failed to read request file {path}: {exc}") from exc
    if request is None:
        return None
    request = request.strip()
    return request or None


def _auth_token(auth_token_env: str | None) -> tuple[str | None, str | None]:
    if auth_token_env:
        token = os.environ.get(auth_token_env)
        if not token:
            raise SwimmersLaunchError(f"{auth_token_env} is not set")
        return auth_token_env, token
    for env_name in TOKEN_ENV_CANDIDATES:
        token = os.environ.get(env_name)
        if token:
            return env_name, token
    return None, None


def _json_request(
    *,
    method: str,
    base_url: str,
    path: str,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            raw = response.read()
            status = int(response.getcode())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = _decode_json(raw)
        message = _error_message(payload) or exc.reason or "request failed"
        raise SwimmersLaunchError(f"{method} {path} failed with HTTP {exc.code}: {message}") from exc
    except (OSError, TimeoutError) as exc:
        raise SwimmersLaunchError(f"failed to reach swimmers at {base_url}: {exc}") from exc
    return status, _decode_json(raw)


def _decode_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwimmersLaunchError(f"swimmers returned non-JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise SwimmersLaunchError("swimmers returned a non-object JSON response")
    return payload


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
    message = payload.get("message")
    return str(message) if message else ""


def _dirs_from_group(
    *,
    base_url: str,
    group: str,
    path: str | None,
    managed_only: bool,
    invoke_cwd: Path,
    token: str | None,
    timeout: float,
) -> list[str]:
    query = {"group": group}
    if path:
        query["path"] = _resolve_path(path, invoke_cwd)
    if managed_only:
        query["managed_only"] = "true"
    _status, payload = _json_request(
        method="GET",
        base_url=base_url,
        path="/v1/dirs",
        query=query,
        token=token,
        timeout=timeout,
    )
    response_path = Path(str(payload.get("path") or "/"))
    dirs: list[str] = []
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("group"):
            continue
        full_path = entry.get("full_path")
        if full_path:
            dirs.append(str(full_path))
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            dirs.append(str(response_path / name))
    if not dirs:
        raise SwimmersLaunchError(f"group {group!r} did not resolve to any launchable directories")
    return dirs


def _resolve_dirs(
    *,
    positional_dirs: list[str],
    dir_flags: list[str],
    cwd_flags: list[str],
    dirs_file: str | None,
    invoke_cwd: Path,
) -> list[str]:
    raw_dirs = [*positional_dirs, *dir_flags, *cwd_flags]
    if dirs_file:
        raw_dirs.extend(_read_lines_file(dirs_file, invoke_cwd))
    return _dedupe([_resolve_path(value, invoke_cwd) for value in raw_dirs if value.strip()])


def build_swimmers_launch_payload(
    *,
    positional_dirs: list[str],
    dir_flags: list[str] | None = None,
    cwd_flags: list[str] | None = None,
    dirs_file: str | None = None,
    group: str | None = None,
    group_path: str | None = None,
    managed_only: bool = False,
    request: str | None = None,
    request_file: str | None = None,
    tool: str = "codex",
    launch_target: str | None = None,
    base_url: str | None = None,
    auth_token_env: str | None = None,
    invoke_cwd: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    invoke_base = Path(invoke_cwd or os.environ.get("PWD") or os.getcwd()).expanduser()
    resolved_base_url = _base_url(base_url)
    token_env, token = _auth_token(auth_token_env)
    dirs = _resolve_dirs(
        positional_dirs=positional_dirs,
        dir_flags=dir_flags or [],
        cwd_flags=cwd_flags or [],
        dirs_file=dirs_file,
        invoke_cwd=invoke_base,
    )
    if group:
        dirs.extend(
            _dirs_from_group(
                base_url=resolved_base_url,
                group=group,
                path=group_path,
                managed_only=managed_only,
                invoke_cwd=invoke_base,
                token=token,
                timeout=timeout,
            )
        )
        dirs = _dedupe(dirs)
    if not dirs:
        raise SwimmersLaunchError("swimmers-launch requires at least one directory or --group")

    prompt = _resolve_prompt(request, request_file, invoke_base)
    body: dict[str, Any] = {
        "dirs": dirs,
        "spawn_tool": tool,
    }
    if launch_target:
        body["launch_target"] = launch_target
    if prompt:
        body["initial_request"] = prompt
    return {
        "base_url": resolved_base_url,
        "token_env": token_env,
        "request_body": body,
        "tool": tool,
        "launch_target": launch_target or "local",
        "dirs": dirs,
        "prompt": prompt,
    }


def launch_swimmers_batch(
    *,
    positional_dirs: list[str],
    dir_flags: list[str] | None = None,
    cwd_flags: list[str] | None = None,
    dirs_file: str | None = None,
    group: str | None = None,
    group_path: str | None = None,
    managed_only: bool = False,
    request: str | None = None,
    request_file: str | None = None,
    tool: str = "codex",
    launch_target: str | None = None,
    base_url: str | None = None,
    auth_token_env: str | None = None,
    invoke_cwd: str | None = None,
    timeout: float = 30.0,
    dry_run: bool = False,
) -> tuple[int, dict[str, Any]]:
    payload = build_swimmers_launch_payload(
        positional_dirs=positional_dirs,
        dir_flags=dir_flags,
        cwd_flags=cwd_flags,
        dirs_file=dirs_file,
        group=group,
        group_path=group_path,
        managed_only=managed_only,
        request=request,
        request_file=request_file,
        tool=tool,
        launch_target=launch_target,
        base_url=base_url,
        auth_token_env=auth_token_env,
        invoke_cwd=invoke_cwd,
        timeout=timeout,
    )
    if dry_run:
        payload.update(
            {
                "ok": True,
                "dry_run": True,
                "requested_count": len(payload["dirs"]),
                "success_count": 0,
                "failure_count": 0,
                "next_actions": ["remove --dry-run to launch the batch"],
            }
        )
        return 0, payload

    token = os.environ.get(payload["token_env"] or "") if payload.get("token_env") else None
    status, response = _json_request(
        method="POST",
        base_url=payload["base_url"],
        path="/v1/sessions/batch",
        body=payload["request_body"],
        token=token,
        timeout=timeout,
    )
    results = response.get("results") or []
    result_dicts = [result for result in results if isinstance(result, dict)]
    success_count = sum(1 for result in result_dicts if result.get("ok"))
    explicit_failures = sum(1 for result in result_dicts if not result.get("ok"))
    malformed_failures = len(results) - len(result_dicts)
    missing_results = max(0, len(payload["dirs"]) - len(results))
    failure_count = explicit_failures + malformed_failures + missing_results
    payload.update(
        {
            "ok": failure_count == 0,
            "dry_run": False,
            "http_status": status,
            "requested_count": len(payload["dirs"]),
            "success_count": success_count,
            "failure_count": failure_count,
            "response": response,
            "next_actions": ["swimmers-tui", f"open {payload['base_url']}"] if failure_count == 0 else [],
        }
    )
    return (0 if failure_count == 0 else 1), payload


def swimmers_launch_text_lines(payload: dict[str, Any]) -> list[str]:
    base_url = payload.get("base_url") or DEFAULT_SWIMMERS_URL
    requested = int(payload.get("requested_count") or len(payload.get("dirs") or []))
    if payload.get("dry_run"):
        lines = [
            f"swimmers launch dry-run: {requested} dirs -> {base_url}",
            f"tool: {payload.get('tool')} target: {payload.get('launch_target')}",
        ]
        lines.extend(f"  - {path}" for path in payload.get("dirs") or [])
        return lines

    lines = [
        f"swimmers launch: {payload.get('success_count', 0)}/{requested} created via {base_url}",
    ]
    response = payload.get("response") or {}
    for result in response.get("results") or []:
        if not isinstance(result, dict):
            continue
        cwd = result.get("cwd") or "-"
        index = result.get("index")
        if result.get("ok"):
            session = result.get("session") or {}
            session_id = session.get("session_id") or "-"
            tmux_name = session.get("tmux_name") or "-"
            lines.append(f"  ok [{index}] {cwd} -> {session_id} {tmux_name}")
        else:
            error = result.get("error") or {}
            code = error.get("code") or "ERROR"
            message = error.get("message") or "launch failed"
            lines.append(f"  fail [{index}] {cwd} -> {code}: {message}")
    return lines
