from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

from .shared import repo_rel
from .workflows import requested_mcp_servers


CLAUDE_MCP_REL = Path(".mcp.json")
CODEX_MCP_REL = Path(".codex") / "config.toml"


def _resolve_path(raw_path: str | None) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _nearest_repo_root(path: Path) -> Path:
    cursor = path if path.is_dir() else path.parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return cursor.resolve()


def _target_config_root(
    root_dir: Path,
    *,
    cwd: str | None = None,
    config_root: str | None = None,
) -> Path:
    explicit_root = _resolve_path(config_root)
    if explicit_root is not None:
        return explicit_root
    cwd_path = _resolve_path(cwd)
    if cwd_path is not None:
        return _nearest_repo_root(cwd_path)
    return root_dir.resolve()


def _server_is_disabled(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    if config.get("enabled") is False:
        return True
    if config.get("disabled") is True:
        return True
    return False


def _surface_payload(
    *,
    name: str,
    fmt: str,
    path: Path,
    servers: dict[str, Any],
    expected: list[str],
    declared: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    all_names = sorted(str(key) for key in servers.keys())
    # A config that is a symlink whose target no longer exists reads as
    # "absent" through is_file(); report the dangling link explicitly so the
    # repair action targets the link instead of suggesting writes through it.
    is_symlink = path.is_symlink()
    broken_symlink = is_symlink and not path.exists()
    symlink_target: str | None = None
    if is_symlink:
        try:
            symlink_target = os.readlink(str(path))
        except OSError:
            symlink_target = None
    disabled = sorted(
        str(key)
        for key, value in servers.items()
        if _server_is_disabled(value)
    )
    effective = sorted(name for name in all_names if name not in disabled)
    expected_set = set(expected)
    effective_set = set(effective)
    # A server is "explained" if it is expected for the active profile/scope or
    # declared as a kind:mcp service anywhere in the runtime model. Servers that
    # are present in a surface config but explained by neither are real drift.
    explained_set = expected_set | set(declared or [])
    extra = sorted(effective_set - expected_set)
    unexpected = sorted(effective_set - explained_set)
    return {
        "name": name,
        "format": fmt,
        "path": str(path),
        "present": path.is_file(),
        "broken_symlink": broken_symlink,
        "symlink_target": symlink_target,
        "valid": error is None,
        "servers": all_names,
        "effective_servers": effective,
        "disabled_servers": disabled,
        "missing": sorted(expected_set - effective_set),
        "extra": extra,
        # Declared-but-inactive servers (e.g. profile-gated) present in this
        # surface; intentional, not drift.
        "extra_intentional": sorted(set(extra) - set(unexpected)),
        # Undeclared servers present in this surface; unexplained drift.
        "unexpected": unexpected,
        "error": error,
    }


def _read_claude_mcp(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {}, str(exc)
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return {}, "mcpServers must be an object"
    return servers, None


def _read_codex_mcp(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, None
    if tomllib is None:
        return {}, "tomllib is unavailable; Python 3.11+ is required to parse TOML"
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:  # type: ignore[union-attr]
        return {}, str(exc)
    servers = payload.get("mcp_servers")
    if servers is None:
        return {}, None
    if not isinstance(servers, dict):
        return {}, "mcp_servers must be a table"
    return servers, None


def _expected_mcp_servers(root_dir: Path, target_root: Path, model: dict[str, Any]) -> list[str]:
    expected = [str(item["name"]) for item in requested_mcp_servers(model)]
    if target_root.resolve() == root_dir.resolve() and "skillbox-operator" not in expected:
        expected.append("skillbox-operator")
    return sorted(dict.fromkeys(name for name in expected if name))


def _parity_payload(
    claude: dict[str, Any],
    codex: dict[str, Any],
    declared: list[str] | None = None,
) -> dict[str, Any]:
    claude_set = set(claude.get("effective_servers") or [])
    codex_set = set(codex.get("effective_servers") or [])
    declared_set = set(declared or [])
    claude_only = sorted(claude_set - codex_set)
    codex_only = sorted(codex_set - claude_set)
    return {
        "claude_only": claude_only,
        "codex_only": codex_only,
        "shared": sorted(claude_set & codex_set),
        # Single-surface servers that ARE declared MCP services are a deliberate
        # status (the surface is configured for a profile-gated capability the
        # other surface does not mirror) rather than accidental drift.
        "claude_only_declared": [name for name in claude_only if name in declared_set],
        "claude_only_unexpected": [name for name in claude_only if name not in declared_set],
        "codex_only_declared": [name for name in codex_only if name in declared_set],
        "codex_only_unexpected": [name for name in codex_only if name not in declared_set],
    }


def _mcp_next_actions(payload: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    surfaces = payload.get("surfaces") or {}
    for key in ("claude", "codex"):
        surface = surfaces.get(key) or {}
        if surface.get("error"):
            actions.append(f"fix {surface['path']}: {surface['error']}")
        if surface.get("broken_symlink"):
            target = surface.get("symlink_target") or "?"
            actions.append(
                f"repair broken symlink {surface['path']} -> {target}: "
                "relink to an existing config before adding servers"
            )
        elif surface.get("missing"):
            missing = ", ".join(surface["missing"])
            actions.append(f"add {missing} to {surface['path']}")
        if surface.get("unexpected"):
            unexpected = ", ".join(surface["unexpected"])
            actions.append(
                f"declare {unexpected} as a kind:mcp service in workspace/runtime.yaml "
                f"or remove from {surface['path']}"
            )
        if surface.get("disabled_servers"):
            disabled = ", ".join(surface["disabled_servers"])
            actions.append(f"enable or remove disabled {disabled} in {surface['path']}")

    parity = payload.get("parity") or {}
    if parity.get("claude_only_unexpected"):
        actions.append(
            "mirror Claude-only MCP servers into Codex TOML or remove if obsolete: "
            + ", ".join(parity["claude_only_unexpected"])
        )
    if parity.get("codex_only_unexpected"):
        actions.append(
            "mirror Codex-only MCP servers into Claude JSON or remove if obsolete: "
            + ", ".join(parity["codex_only_unexpected"])
        )
    return actions


def collect_mcp_audit(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    config_root: str | None = None,
    declared_servers: list[str] | None = None,
) -> dict[str, Any]:
    target_root = _target_config_root(root_dir, cwd=cwd, config_root=config_root)
    claude_path = target_root / CLAUDE_MCP_REL
    codex_path = target_root / CODEX_MCP_REL
    expected = _expected_mcp_servers(root_dir, target_root, model)
    # The declared baseline (kind:mcp services across all profiles) only applies
    # when auditing this repo's own config; for a foreign --cwd we cannot vouch
    # for which servers that repo intends. Always treat expected as explained.
    declared = sorted(set(expected) | set(_resolve_declared_servers(
        model, declared_servers, target_root, root_dir,
    )))

    claude_servers, claude_error = _read_claude_mcp(claude_path)
    codex_servers, codex_error = _read_codex_mcp(codex_path)

    claude = _surface_payload(
        name="claude",
        fmt="json",
        path=claude_path,
        servers=claude_servers,
        expected=expected,
        declared=declared,
        error=claude_error,
    )
    codex = _surface_payload(
        name="codex",
        fmt="toml",
        path=codex_path,
        servers=codex_servers,
        expected=expected,
        declared=declared,
        error=codex_error,
    )
    payload: dict[str, Any] = {
        "cwd": str(_resolve_path(cwd) or Path.cwd().resolve()),
        "config_root": str(target_root),
        "expected_servers": expected,
        "declared_servers": declared,
        "surfaces": {
            "claude": claude,
            "codex": codex,
        },
        "parity": _parity_payload(claude, codex, declared),
    }
    payload["summary"] = {
        "expected": len(expected),
        "declared": len(declared),
        "claude_missing": len(claude["missing"]),
        "codex_missing": len(codex["missing"]),
        "claude_extra": len(claude["extra"]),
        "codex_extra": len(codex["extra"]),
        "claude_only": len(payload["parity"]["claude_only"]),
        "codex_only": len(payload["parity"]["codex_only"]),
        "claude_unexpected": len(claude["unexpected"]),
        "codex_unexpected": len(codex["unexpected"]),
        "unexplained_drift": len(claude["unexpected"]) + len(codex["unexpected"]),
        "invalid_configs": sum(
            1
            for surface in (claude, codex)
            if not bool(surface.get("valid"))
        ),
    }
    payload["next_actions"] = _mcp_next_actions(payload)
    return payload


def _resolve_declared_servers(
    model: dict[str, Any],
    declared_servers: list[str] | None,
    target_root: Path,
    root_dir: Path,
) -> list[str]:
    """The set of MCP servers the runtime model declares (any profile).

    Callers that have the full, unfiltered model pass it via ``declared_servers``
    so profile-gated servers (e.g. memory/connectors) are recognized as
    intentional. When auditing a foreign repo we cannot apply this repo's
    declarations, so fall back to whatever the passed model declares.
    """
    if declared_servers is not None and target_root.resolve() == root_dir.resolve():
        return [str(name) for name in declared_servers if str(name).strip()]
    return [str(item["name"]) for item in requested_mcp_servers(model) if item.get("name")]


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "-"


def _display_path(root_dir: Path, raw_path: str) -> str:
    path = Path(raw_path)
    try:
        return repo_rel(root_dir, path)
    except ValueError:
        return str(path)


def _print_surface(root_dir: Path, label: str, surface: dict[str, Any]) -> None:
    print(
        f"{label}: "
        f"{_display_path(root_dir, surface['path'])} "
        f"present={str(surface.get('present')).lower()} "
        f"valid={str(surface.get('valid')).lower()}"
    )
    if surface.get("broken_symlink"):
        print(f"  symlink: broken -> {surface.get('symlink_target') or '?'}")
    print(f"  servers: {_join_or_none(surface.get('effective_servers') or [])}")
    if surface.get("missing"):
        print(f"  missing: {_join_or_none(surface['missing'])}")
    if surface.get("extra_intentional"):
        print(f"  declared (profile-gated): {_join_or_none(surface['extra_intentional'])}")
    if surface.get("unexpected"):
        print(f"  unexpected: {_join_or_none(surface['unexpected'])}")
    if surface.get("disabled_servers"):
        print(f"  disabled: {_join_or_none(surface['disabled_servers'])}")
    if surface.get("error"):
        print(f"  error: {surface['error']}")


def print_mcp_audit_text(
    payload: dict[str, Any],
    *,
    root_dir: Path,
) -> None:
    summary = payload.get("summary") or {}
    print(
        "mcp audit: "
        f"expected={summary.get('expected', 0)} "
        f"claude_missing={summary.get('claude_missing', 0)} "
        f"codex_missing={summary.get('codex_missing', 0)} "
        f"parity={summary.get('claude_only', 0) + summary.get('codex_only', 0)} "
        f"unexplained_drift={summary.get('unexplained_drift', 0)}"
    )
    print(f"cwd: {payload.get('cwd')}")
    print(f"config_root: {payload.get('config_root')}")
    print(f"expected: {_join_or_none(payload.get('expected_servers') or [])}")
    print(f"declared: {_join_or_none(payload.get('declared_servers') or [])}")
    surfaces = payload.get("surfaces") or {}
    _print_surface(root_dir, "claude-json", surfaces.get("claude") or {})
    _print_surface(root_dir, "codex-toml", surfaces.get("codex") or {})
    parity = payload.get("parity") or {}
    if parity.get("claude_only") or parity.get("codex_only"):
        print(
            "parity: "
            f"claude_only={_join_or_none(parity.get('claude_only') or [])} "
            f"codex_only={_join_or_none(parity.get('codex_only') or [])}"
        )
        if parity.get("claude_only_declared") or parity.get("codex_only_declared"):
            print(
                "  intentional (declared, profile-gated): "
                f"claude_only={_join_or_none(parity.get('claude_only_declared') or [])} "
                f"codex_only={_join_or_none(parity.get('codex_only_declared') or [])}"
            )
    next_actions = payload.get("next_actions") or []
    if next_actions:
        print("next_actions:")
        for action in next_actions:
            print(f"  - {action}")
