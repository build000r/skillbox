from __future__ import annotations

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
    if service_id in APP_SERVICE_IDS:
        return "app"
    return "api"


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
        url = _local_url(hc_url) or meta.get("local_url") or ""
        if not url:
            continue
        probe_url = hc_url or url
        alias = _HARDCODED_ALIASES.get(svc_id) or meta.get("alias")
        rows.append(
            {
                "id": svc_id,
                "url": url,
                "category": _categorize(svc_id, hc_url, meta.get("category")),
                "alias": alias,
                "probe_url": probe_url,
                "status": None,
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
