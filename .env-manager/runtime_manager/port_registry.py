"""Machine-readable PORT REGISTRY built from the resolved runtime model.

Phase 0 of the port-guard stack. The registry is the single source of truth
mapping every declared port to its owning service/profile/client/source. Today
ports are scattered across ``workspace/runtime.yaml`` healthchecks, ingress
listeners, ~15 ``SKILLBOX_*_PORT`` env keys, client overlays, and docs prose,
with nothing enforcing uniqueness. Later phases (collision/wildcard/reserved
doctor checks, CLI/MCP surfaces, context table) consume this view.

Extraction is CONSERVATIVE: a health target with no unambiguous integer port
(e.g. ``localhost:8050`` with no scheme is parseable, but ``path_exists``
checks or a bare ``localhost`` are not) NEVER guesses a port. Such cases emit a
``warning`` entry that names the source so an operator can fix the declaration.

This module is standard-library only and is independent of ``scripts/box.py``.
It imports the small, pure port-extraction helpers from
``lib.runtime_model`` and otherwise consumes the already-resolved runtime model
dict produced by :func:`lib.runtime_model.build_runtime_model`.

Registry entry schema (each dict)::

    {
        "port": int | None,        # None only for warning entries
        "owner_id": str,           # service id, ingress listener, or env key
        "owner_kind": str,         # service | ingress | env_surface
        "client": str,             # "" for core/global scope
        "profiles": list[str],     # declared profiles (sorted)
        "bind_scope": str,         # loopback | tailnet | wildcard | unknown
        "source": {"file": str, "key": str},
        "protocol": str,           # http | https | tcp | "" (unknown)
        "warning": str | None,     # set when the port could not be parsed
    }
"""
from __future__ import annotations

from typing import Any

from lib.runtime_model import (
    classify_bind_scope,
    extract_command_port,
    extract_host_port,
    runtime_manifest_path,
)

OWNER_KIND_SERVICE = "service"
OWNER_KIND_INGRESS = "ingress"
OWNER_KIND_ENV = "env_surface"

# Env keys that declare a port. Listing them explicitly keeps the env-surface
# pass deterministic and avoids sweeping in non-port SKILLBOX_* keys.
PORT_ENV_KEYS: tuple[str, ...] = (
    "SKILLBOX_API_PORT",
    "SKILLBOX_WEB_PORT",
    "SKILLBOX_SWIMMERS_PORT",
    "SKILLBOX_CM_MCP_PORT",
    "SKILLBOX_DCG_MCP_PORT",
    "SKILLBOX_FWC_MCP_PORT",
    "SKILLBOX_INGRESS_PUBLIC_PORT",
    "SKILLBOX_INGRESS_PRIVATE_PORT",
)

# Env keys that carry the bind host paired with a listener port. Used to refine
# bind_scope for env-surface entries when a sibling host is declared.
_ENV_HOST_FOR_PORT: dict[str, str] = {
    "SKILLBOX_INGRESS_PUBLIC_PORT": "SKILLBOX_INGRESS_PUBLIC_HOST",
    "SKILLBOX_INGRESS_PRIVATE_PORT": "SKILLBOX_INGRESS_PRIVATE_HOST",
    "SKILLBOX_SWIMMERS_PORT": "SKILLBOX_SWIMMERS_PUBLISH_HOST",
}


def _manifest_rel(model: dict[str, Any]) -> str:
    """Best-effort repo-relative manifest path for the ``source.file`` field."""
    from pathlib import Path

    raw = str(model.get("manifest_file") or "")
    root_dir = str(model.get("root_dir") or "")
    if not raw and root_dir:
        raw = str(runtime_manifest_path(Path(root_dir)))
    if raw and root_dir:
        try:
            return str(Path(raw).relative_to(Path(root_dir)))
        except ValueError:
            return raw
    return raw or "workspace/runtime.yaml"


def _entry(
    *,
    port: int | None,
    owner_id: str,
    owner_kind: str,
    client: str,
    profiles: list[str] | None,
    bind_scope: str,
    source_file: str,
    source_key: str,
    protocol: str,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "port": port,
        "owner_id": owner_id,
        "owner_kind": owner_kind,
        "client": str(client or ""),
        "profiles": sorted({str(p).strip() for p in (profiles or []) if str(p).strip()}),
        "bind_scope": bind_scope,
        "source": {"file": source_file, "key": source_key},
        "protocol": protocol,
        "warning": warning,
    }


def _service_health_target(service: dict[str, Any]) -> tuple[str, str]:
    """Return ``(target, source_key)`` for a service's health declaration."""
    healthcheck = service.get("healthcheck")
    if isinstance(healthcheck, dict):
        url = str(healthcheck.get("url") or "").strip()
        if url:
            return url, "healthcheck.url"
        port = healthcheck.get("port")
        if port is not None and str(port).strip():
            return str(port).strip(), "healthcheck.port"
        path = str(healthcheck.get("path") or "").strip()
        if path:
            return path, "healthcheck.path"
    raw_target = str(service.get("health_target") or "").strip()
    if raw_target:
        return raw_target, "health_target"
    return "", ""


def _service_bind_scope(service: dict[str, Any], health_host: str) -> str:
    """Prefer the command --host bind, fall back to the health target host."""
    command = str(service.get("command") or "")
    if command:
        import shlex

        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        for index, token in enumerate(tokens):
            for name in ("--host", "--hostname"):
                if token == name and index + 1 < len(tokens):
                    return classify_bind_scope(tokens[index + 1])
                prefix = f"{name}="
                if token.startswith(prefix):
                    return classify_bind_scope(token[len(prefix):])
    if health_host:
        return classify_bind_scope(health_host)
    return "loopback"


def _service_entry(service: dict[str, Any], source_file: str) -> dict[str, Any]:
    owner_id = str(service.get("id") or "").strip() or "(unnamed-service)"
    client = str(service.get("client") or "")
    profiles = list(service.get("profiles") or [])
    target, source_key = _service_health_target(service)

    host, port, scheme = extract_host_port(target)
    if port is None:
        # A bare integer health_target (e.g. healthcheck.port: 8050) is a valid
        # port even though it is not a host:port authority.
        if source_key in {"healthcheck.port"} and str(target).isdigit():
            try:
                port = int(target)
            except ValueError:
                port = None
        if port is None:
            # Fall back to a --port flag on the command before giving up.
            command_port = extract_command_port(str(service.get("command") or ""))
            if command_port is not None:
                return _entry(
                    port=command_port,
                    owner_id=owner_id,
                    owner_kind=OWNER_KIND_SERVICE,
                    client=client,
                    profiles=profiles,
                    bind_scope=_service_bind_scope(service, host),
                    source_file=source_file,
                    source_key="command --port",
                    protocol=scheme or "tcp",
                )
            warning = _service_warning(service, target, source_key)
            return _entry(
                port=None,
                owner_id=owner_id,
                owner_kind=OWNER_KIND_SERVICE,
                client=client,
                profiles=profiles,
                bind_scope="unknown",
                source_file=source_file,
                source_key=source_key or "healthcheck",
                protocol="",
                warning=warning,
            )

    protocol = scheme or "tcp"
    bind_scope = _service_bind_scope(service, host)
    return _entry(
        port=port,
        owner_id=owner_id,
        owner_kind=OWNER_KIND_SERVICE,
        client=client,
        profiles=profiles,
        bind_scope=bind_scope,
        source_file=source_file,
        source_key=source_key or "healthcheck",
        protocol=protocol,
    )


def _service_warning(service: dict[str, Any], target: str, source_key: str) -> str:
    health = service.get("healthcheck") if isinstance(service.get("healthcheck"), dict) else {}
    htype = str((health or {}).get("type") or service.get("health_type") or "").strip()
    if htype in {"path_exists", "process_running"} or not target:
        return (
            f"service {service.get('id')!r} health check ({htype or 'unknown type'}) "
            "declares no port; not registered"
        )
    return (
        f"service {service.get('id')!r} health target {target!r} has no parseable "
        "port; not registered"
    )


def _ingress_listener_env(model: dict[str, Any], listener: str) -> tuple[int | None, str, str]:
    """Resolve ``(port, host, port_env_key)`` for an ingress listener."""
    env = model.get("env") or {}
    normalized = "private" if str(listener or "").strip().lower() == "private" else "public"
    port_key = f"SKILLBOX_INGRESS_{normalized.upper()}_PORT"
    host_key = f"SKILLBOX_INGRESS_{normalized.upper()}_HOST"
    raw_port = str(env.get(port_key) or "").strip()
    host = str(env.get(host_key) or "").strip() or "127.0.0.1"
    try:
        port = int(raw_port) if raw_port else None
    except ValueError:
        port = None
    return port, host, port_key


def _ingress_entries(model: dict[str, Any], source_file: str) -> list[dict[str, Any]]:
    """One entry per ingress listener that an ingress service/route declares.

    Listener ports come from env, but they are only registered when an ingress
    surface (a ``kind: ingress`` service or an ingress_route) actually uses the
    listener — the env-surface pass already covers the raw env keys.
    """
    listeners: set[str] = set()
    for service in model.get("services") or []:
        if str(service.get("kind") or "").strip().lower() == "ingress":
            # Ingress router fronts both listeners.
            listeners.update({"public", "private"})
    for route in model.get("ingress_routes") or []:
        listener = str(route.get("listener") or "public").strip().lower() or "public"
        listeners.add("private" if listener == "private" else "public")

    entries: list[dict[str, Any]] = []
    for listener in sorted(listeners):
        port, host, port_key = _ingress_listener_env(model, listener)
        if port is None:
            entries.append(
                _entry(
                    port=None,
                    owner_id=f"ingress:{listener}",
                    owner_kind=OWNER_KIND_INGRESS,
                    client="",
                    profiles=[],
                    bind_scope="unknown",
                    source_file=".env",
                    source_key=port_key,
                    protocol="",
                    warning=f"ingress listener {listener!r} has no parseable {port_key}",
                )
            )
            continue
        entries.append(
            _entry(
                port=port,
                owner_id=f"ingress:{listener}",
                owner_kind=OWNER_KIND_INGRESS,
                client="",
                profiles=[],
                bind_scope=classify_bind_scope(host),
                source_file=".env",
                source_key=port_key,
                protocol="http",
            )
        )
    return entries


def _env_surface_entries(model: dict[str, Any]) -> list[dict[str, Any]]:
    env = model.get("env") or {}
    entries: list[dict[str, Any]] = []
    for key in PORT_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            entries.append(
                _entry(
                    port=None,
                    owner_id=key,
                    owner_kind=OWNER_KIND_ENV,
                    client="",
                    profiles=[],
                    bind_scope="unknown",
                    source_file=".env",
                    source_key=key,
                    protocol="",
                    warning=f"env key {key} value {raw!r} is not an integer port",
                )
            )
            continue
        host_key = _ENV_HOST_FOR_PORT.get(key)
        bind_scope = "loopback"
        if host_key:
            host = str(env.get(host_key) or "").strip()
            if host:
                bind_scope = classify_bind_scope(host)
        entries.append(
            _entry(
                port=port,
                owner_id=key,
                owner_kind=OWNER_KIND_ENV,
                client="",
                profiles=[],
                bind_scope=bind_scope,
                source_file=".env",
                source_key=key,
                protocol="tcp",
            )
        )
    return entries


def _entry_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("port") is None,
        entry.get("port") or 0,
        entry.get("owner_kind") or "",
        entry.get("owner_id") or "",
        entry.get("client") or "",
    )


def build_port_registry(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the flat registry VIEW from a resolved runtime model.

    The model is expected to be ALREADY scope-filtered by the caller (client +
    profiles) so the registry is scope-aware: only ports active in the current
    scope appear. Cross-client analysis is the doctor's job, not the view's.
    """
    source_file = _manifest_rel(model)
    entries: list[dict[str, Any]] = []
    for service in model.get("services") or []:
        entries.append(_service_entry(service, source_file))
    entries.extend(_ingress_entries(model, source_file))
    entries.extend(_env_surface_entries(model))
    entries.sort(key=_entry_sort_key)
    return entries


def _registry_warnings(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"owner_id": e["owner_id"], "source": e["source"], "warning": e["warning"]}
        for e in entries
        if e.get("warning")
    ]


def port_registry_payload(
    model: dict[str, Any],
    *,
    resolve: str | None = None,
) -> dict[str, Any]:
    """Structured/compact payload for ``manage.py ports``.

    When ``resolve`` is set, the payload narrows to the entries owned by that
    service id (``owner_id``) and reports its resolved port(s).
    """
    entries = build_port_registry(model)
    warnings = _registry_warnings(entries)

    if resolve is not None:
        target = str(resolve).strip()
        matched = [
            e
            for e in entries
            if e["owner_id"] == target
            or (e["owner_kind"] == OWNER_KIND_SERVICE and e["owner_id"] == target)
        ]
        resolved_ports = sorted({e["port"] for e in matched if e["port"] is not None})
        return {
            "ok": True,
            "command": "ports",
            "resolve": target,
            "found": bool(matched),
            "ports": resolved_ports,
            "entries": matched,
            "warnings": [w for w in warnings if w["owner_id"] == target],
        }

    declared = [e for e in entries if e["port"] is not None]
    return {
        "ok": True,
        "command": "ports",
        "scope": {
            "clients": sorted(model.get("active_clients") or []),
            "profiles": sorted(model.get("active_profiles") or []),
        },
        "count": len(declared),
        "entries": entries,
        "warnings": warnings,
    }


def port_registry_text_lines(payload: dict[str, Any]) -> list[str]:
    """Compact human-readable view mirroring the JSON payload."""
    if payload.get("resolve") is not None:
        ports = payload.get("ports") or []
        header = (
            f"ports for {payload['resolve']}: "
            + (", ".join(str(p) for p in ports) if ports else "(none)")
        )
        lines = [header]
        for entry in payload.get("entries") or []:
            lines.append(_format_entry_line(entry))
        return lines

    entries = payload.get("entries") or []
    lines = [
        f"port registry ({payload.get('count', 0)} declared port(s); "
        f"{len(payload.get('warnings') or [])} warning(s))"
    ]
    for entry in entries:
        lines.append(_format_entry_line(entry))
    return lines


def _format_entry_line(entry: dict[str, Any]) -> str:
    port = entry.get("port")
    port_str = str(port) if port is not None else "????"
    client = entry.get("client") or "-"
    profiles = ",".join(entry.get("profiles") or []) or "-"
    source = entry.get("source") or {}
    src = f"{source.get('file', '')}:{source.get('key', '')}".strip(":")
    warn = f"  !! {entry['warning']}" if entry.get("warning") else ""
    return (
        f"  {port_str:>5}  {entry.get('owner_kind', ''):<11} "
        f"{entry.get('owner_id', ''):<28} client={client} profiles={profiles} "
        f"bind={entry.get('bind_scope', '')} via {src}{warn}"
    )


__all__ = [
    "OWNER_KIND_SERVICE",
    "OWNER_KIND_INGRESS",
    "OWNER_KIND_ENV",
    "PORT_ENV_KEYS",
    "build_port_registry",
    "port_registry_payload",
    "port_registry_text_lines",
]
