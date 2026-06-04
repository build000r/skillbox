from __future__ import annotations

import shlex
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

# Services whose UI is what an operator typically opens in a browser.
# Everything else with kind=http is treated as a backend API.
APP_SERVICE_IDS = {
    "htma",
    "buildooor",
    "buildooor-web",
    "cca-website",
    "mhb",
    "unclawg",
    "videos",
}

# Service ids whose user-facing name differs from the id. Fall back to
# overlay-derived aliases when the overlay can be read.
_HARDCODED_ALIASES = {
    "ingredient_server": "cyclechef",
}


def _local_url(healthcheck_url: str) -> str:
    """Return the origin (scheme://host:port) so the path doesn't surface."""
    if not healthcheck_url:
        return ""
    parsed = urlparse(healthcheck_url)
    if not parsed.scheme or not parsed.netloc:
        return healthcheck_url
    return f"{parsed.scheme}://{parsed.netloc}"


# Domain-block keys in overlay context.domains that don't match service ids.
_DOMAIN_KEY_TO_SERVICE = {
    "cca": "cca-website",
    "ingredient": "ingredient_server",
}


def _alias_from_production(production: str, svc_id: str) -> str | None:
    if not production:
        return None
    host = urlparse(production).netloc or production
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) > 2 and parts[0] in {"api", "ingredients", "app"}:
        alias = parts[1]
    else:
        alias = parts[0] if parts else ""
    if alias and alias != svc_id:
        return alias
    return None


def _overlay_metadata(model: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Read `client.context.domains.{frontends,apis}` from the active overlay.
    Returns {service_id: {category, alias, local_url}} where category is
    'app' (frontend) or 'api'."""
    out: dict[str, dict[str, str]] = {}
    if yaml is None:
        return out
    for client in model.get("clients") or []:
        overlay_path = client.get("_overlay_path")
        if not overlay_path:
            continue
        path = Path(overlay_path)
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        domains = (((data.get("client") or {}).get("context") or {}).get("domains") or {})
        for block_key, category in (("frontends", "app"), ("apis", "api")):
            block = domains.get(block_key) or {}
            if not isinstance(block, dict):
                continue
            for domain_key, info in block.items():
                if not isinstance(info, dict):
                    continue
                svc_id = _DOMAIN_KEY_TO_SERVICE.get(domain_key, domain_key)
                entry: dict[str, str] = {"category": category}
                production = info.get("production")
                if isinstance(production, str):
                    alias = _alias_from_production(production, svc_id)
                    if alias:
                        entry["alias"] = alias
                local = info.get("local")
                if isinstance(local, str) and local:
                    entry["local_url"] = local
                out[svc_id] = entry
    return out


def _categorize(service_id: str, healthcheck_url: str, overlay_category: str | None) -> str:
    if overlay_category:
        return overlay_category
    if service_id in APP_SERVICE_IDS or service_id.endswith(("-web", "-frontend")):
        return "app"
    return "api"


_LOOPBACK_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0:0:0:0:0:0:0:1",
}
_WILDCARD_HOSTS = {"0.0.0.0", "::"}


def _url_host_port(url: str) -> tuple[str, int | None, str]:
    if not url:
        return "", None, ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "", None, ""
    if not parsed.scheme or not parsed.netloc:
        return "", None, ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.hostname or "", port, parsed.scheme


def _replace_url_host(url: str, host: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return parsed._replace(netloc=f"{host}{port}").geturl()


def _box_tailnet_hosts(box_access: dict[str, Any]) -> list[str]:
    state = str(box_access.get("tailscale_state") or "").strip().lower()
    if state and state != "running":
        return []
    hosts: list[str] = []
    for key in ("tailscale_ip", "tailscale_hostname"):
        value = str(box_access.get(key) or "").strip().rstrip(".")
        if value:
            hosts.append(value)
    return hosts


def _preferred_tailnet_host(box_access: dict[str, Any]) -> str:
    hosts = _box_tailnet_hosts(box_access)
    return hosts[0] if hosts else ""


def _normalized_host_values(values: list[str]) -> set[str]:
    return {value.lower().rstrip(".") for value in values if value}


def _ingress_listener_settings(model: dict[str, Any], listener: str) -> dict[str, Any]:
    env = model.get("env") or {}
    normalized_listener = "private" if str(listener or "").strip().lower() == "private" else "public"
    host_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_HOST"
    port_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_PORT"
    base_url_key = f"SKILLBOX_INGRESS_{normalized_listener.upper()}_BASE_URL"
    host = str(env.get(host_key) or "").strip() or "127.0.0.1"
    port_raw = str(env.get(port_key) or "").strip() or ("9080" if normalized_listener == "private" else "8080")
    try:
        port = int(port_raw)
    except ValueError:
        port = 9080 if normalized_listener == "private" else 8080
    display_host = "127.0.0.1" if host in _WILDCARD_HOSTS else host
    raw_base_url = str(env.get(base_url_key) or "").strip().rstrip("/")
    base_url = raw_base_url or f"http://{display_host}:{port}"
    return {"listener": normalized_listener, "host": host, "port": port, "base_url": base_url}


def _service_ingress_routes(model: dict[str, Any], service_id: str) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for route in model.get("ingress_routes") or []:
        if str(route.get("service_id") or "").strip() != service_id:
            continue
        listener = str(route.get("listener") or "public").strip().lower() or "public"
        path = str(route.get("path") or route.get("path_prefix") or "").strip()
        settings = _ingress_listener_settings(model, listener)
        routes.append(
            {
                "id": str(route.get("id") or "").strip(),
                "listener": listener,
                "path": path,
                "path_prefix": str(route.get("path_prefix") or "").strip(),
                "match": str(route.get("match") or "exact").strip().lower() or "exact",
                "strip_prefix": route.get("strip_prefix") is True,
                "route_host": str(route.get("host") or "").strip(),
                "request_url": f"{settings['base_url']}{path}" if path else settings["base_url"],
                "host": settings["host"],
                "port": settings["port"],
            }
        )
    return sorted(routes, key=lambda item: (item["listener"], item["path"], item["id"]))


def _service_command_strings(service: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    command = service.get("command")
    if isinstance(command, str) and command.strip():
        commands.append(command)
    mode_commands = service.get("commands") or {}
    if isinstance(mode_commands, dict):
        for value in mode_commands.values():
            if isinstance(value, str) and value.strip():
                commands.append(value)
    return commands


def _command_token_value(tokens: list[str], names: set[str]) -> str:
    for index, token in enumerate(tokens):
        if token in names and index + 1 < len(tokens):
            return tokens[index + 1]
        for name in names:
            prefix = f"{name}="
            if token.startswith(prefix):
                return token[len(prefix):]
    return ""


def _service_command_bind_url(service: dict[str, Any], fallback_url: str) -> str:
    fallback_host, fallback_port, fallback_scheme = _url_host_port(fallback_url)
    scheme = fallback_scheme or "http"
    for command in _service_command_strings(service):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        host = _command_token_value(tokens, {"--host", "--hostname"})
        port = _command_token_value(tokens, {"--port"})
        if not host:
            continue
        try:
            port_number = int(port) if port else fallback_port
        except ValueError:
            port_number = fallback_port
        if port_number is None:
            continue
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{scheme}://{host}:{port_number}"
    return ""


def _service_local_url(service: dict[str, Any], meta: dict[str, str]) -> str:
    healthcheck = service.get("healthcheck") or {}
    healthcheck_url = str(healthcheck.get("url") or "")
    command_url = _service_command_bind_url(service, _local_url(healthcheck_url))
    if command_url:
        return command_url
    return (
        _local_url(str(service.get("origin_url") or ""))
        or meta.get("local_url")
        or _local_url(healthcheck_url)
        or ""
    )


def _tailnet_direct_url(local_url: str, box_access: dict[str, Any]) -> str:
    host, _port, _scheme = _url_host_port(local_url)
    if not host:
        return ""
    normalized_host = host.lower()
    if normalized_host in _LOOPBACK_HOSTS:
        return ""
    if normalized_host in _WILDCARD_HOSTS:
        tailnet_host = _preferred_tailnet_host(box_access)
        return _replace_url_host(local_url, tailnet_host) if tailnet_host else ""
    if normalized_host in _normalized_host_values(_box_tailnet_hosts(box_access)):
        return local_url
    return ""


def _ingress_route_tailnet_url(route: dict[str, Any], box_access: dict[str, Any]) -> str:
    request_url = str(route.get("request_url") or "").strip()
    if not request_url:
        return ""
    url_host, _port, _scheme = _url_host_port(request_url)
    route_host = str(route.get("host") or url_host or "").strip().lower()
    normalized_url_host = url_host.lower()
    if route_host in _WILDCARD_HOSTS or normalized_url_host in _WILDCARD_HOSTS:
        tailnet_host = _preferred_tailnet_host(box_access)
        return _replace_url_host(request_url, tailnet_host) if tailnet_host else ""
    if route_host in _LOOPBACK_HOSTS or normalized_url_host in _LOOPBACK_HOSTS:
        return ""
    if normalized_url_host in _normalized_host_values(_box_tailnet_hosts(box_access)):
        return request_url
    return ""


def service_endpoint_exposure(
    model: dict[str, Any],
    service: dict[str, Any],
    *,
    box_access: dict[str, Any] | None = None,
    overlay_meta: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    if service.get("kind") != "http":
        return None
    service_id = str(service.get("id") or "").strip()
    if not service_id:
        return None
    meta = (overlay_meta or _overlay_metadata(model)).get(service_id, {})
    local_url = _service_local_url(service, meta)
    if not local_url:
        return None
    routes = _service_ingress_routes(model, service_id)
    category = _categorize(service_id, local_url, meta.get("category"))
    box_access = box_access or {}
    direct_url = _tailnet_direct_url(local_url, box_access)
    annotated_routes: list[dict[str, Any]] = []
    for route in routes:
        annotated = dict(route)
        route_tailnet_url = _ingress_route_tailnet_url(route, box_access)
        annotated["tailnet_url"] = route_tailnet_url
        annotated["viewable_from_tailnet"] = bool(route_tailnet_url)
        annotated_routes.append(annotated)
    viewable_routes = [route for route in annotated_routes if route.get("viewable_from_tailnet")]
    if direct_url:
        exposure = "tailnet-direct"
        access_url = direct_url
        tailnet_url = direct_url
    elif viewable_routes:
        exposure = "ingress-routed"
        access_url = str(viewable_routes[0].get("tailnet_url") or viewable_routes[0].get("request_url") or "")
        tailnet_url = access_url
    elif annotated_routes:
        exposure = "loopback-only"
        access_url = str(annotated_routes[0].get("request_url") or local_url)
        tailnet_url = ""
    else:
        exposure = "loopback-only"
        access_url = local_url
        tailnet_url = ""
    endpoint = {
        "url": local_url,
        "local_url": local_url,
        "access_url": access_url,
        "tailnet_url": tailnet_url,
        "category": category,
        "exposure": exposure,
        "viewable_from_tailnet": bool(tailnet_url),
        "ingress_routes": annotated_routes,
    }
    host, port, scheme = _url_host_port(local_url)
    if host:
        endpoint["host"] = host
    if port is not None:
        endpoint["port"] = port
    if scheme:
        endpoint["scheme"] = scheme
    if exposure == "loopback-only" and category == "app":
        if annotated_routes:
            endpoint["warning"] = (
                f"{service_id} has only loopback-only ingress/local access at {access_url}; "
                "make the ingress listener Tailnet-reachable before treating it as phone-viewable."
            )
        else:
            endpoint["warning"] = (
                f"{service_id} is loopback-only at {local_url}; add an ingress route "
                "or bind it to a Tailnet-reachable listener before treating it as phone-viewable."
            )
    return endpoint


def annotate_service_rows(
    model: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    box_access: dict[str, Any] | None = None,
) -> list[str]:
    services = {
        str(service.get("id") or "").strip(): service
        for service in model.get("services") or []
        if str(service.get("id") or "").strip()
    }
    overlay_meta = _overlay_metadata(model)
    warnings: list[str] = []
    for row in rows:
        service_id = str(row.get("id") or "").strip()
        endpoint = service_endpoint_exposure(
            model,
            services.get(service_id) or {},
            box_access=box_access,
            overlay_meta=overlay_meta,
        )
        if endpoint is None:
            continue
        row["endpoint"] = endpoint
        row["exposure"] = endpoint["exposure"]
        row["endpoint_url"] = endpoint["access_url"]
        row["viewable_from_tailnet"] = endpoint["viewable_from_tailnet"]
        if endpoint.get("warning"):
            warnings.append(str(endpoint["warning"]))
    return warnings


def _probe(url: str, timeout: float) -> str:
    """Return one of: ok, starting, down."""
    if not url:
        return "down"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            code = resp.getcode()
            if code is None or code < 500:
                return "ok"
            return "starting"
    except urllib.error.HTTPError as exc:
        # Any HTTP response means something is listening; 4xx is "ok-ish",
        # 5xx is "starting/broken".
        if exc.code < 500:
            return "ok"
        return "starting"
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError, OSError):
        return "down"


def build_endpoint_summary(
    model: dict[str, Any],
    started_service_ids: set[str] | None = None,
    *,
    probe: bool = True,
    timeout: float = 0.5,
    box_access: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build a {"apps": [...], "apis": [...]} listing of HTTP services with
    their local URLs and (optionally) live probe results.

    `started_service_ids`, when provided, narrows the list to services that
    were actually attempted in the current `up` invocation.
    """
    overlay_meta = _overlay_metadata(model)
    rows: list[dict[str, Any]] = []
    for svc in model.get("services") or []:
        if svc.get("kind") != "http":
            continue
        svc_id = svc.get("id") or ""
        if started_service_ids is not None and svc_id not in started_service_ids:
            continue
        hc = svc.get("healthcheck") or {}
        hc_url = hc.get("url") or ""
        meta = overlay_meta.get(svc_id, {})
        alias = _HARDCODED_ALIASES.get(svc_id) or meta.get("alias")
        endpoint = service_endpoint_exposure(
            model,
            svc,
            box_access=box_access,
            overlay_meta=overlay_meta,
        )
        url = (endpoint or {}).get("local_url") or _local_url(hc_url) or meta.get("local_url") or ""
        if not url:
            continue
        access_url = (endpoint or {}).get("access_url") or url
        probe_url = hc_url or url
        rows.append(
            {
                "id": svc_id,
                "url": url,
                "access_url": access_url,
                "category": (endpoint or {}).get("category") or _categorize(svc_id, url, meta.get("category")),
                "alias": alias,
                "probe_url": probe_url,
                "status": None,
                "endpoint": endpoint or {},
                "exposure": (endpoint or {}).get("exposure"),
            }
        )

    if probe and rows:
        with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            results = list(pool.map(lambda r: _probe(r["probe_url"], timeout), rows))
        for row, status in zip(rows, results):
            row["status"] = status

    apps = [r for r in rows if r["category"] == "app"]
    apis = [r for r in rows if r["category"] == "api"]
    return {"apps": apps, "apis": apis}
