#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = "local-all"
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_ASSET_LIMIT = 8
CORE_SERVICE_IDS = {"internal-env-manager", "pulse", "ingress-router"}


@dataclass(frozen=True)
class FetchResult:
    status: int
    content_type: str
    body: bytes


class AssetParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        raw_url = ""
        if tag == "script":
            raw_url = values.get("src", "")
        elif tag == "link":
            rel_values = {part.lower() for part in values.get("rel", "").split()}
            if rel_values & {"stylesheet", "modulepreload", "preload"}:
                raw_url = values.get("href", "")
        if raw_url:
            self._append_same_origin(raw_url)

    def _append_same_origin(self, raw_url: str) -> None:
        absolute = urllib.parse.urljoin(self.base_url, raw_url)
        base = urllib.parse.urlparse(self.base_url)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            return
        if parsed.netloc != base.netloc:
            return
        if absolute not in self.urls:
            self.urls.append(absolute)


def split_clients(values: list[str]) -> list[str]:
    clients: list[str] = []
    for value in values:
        for part in value.split(","):
            client = part.strip()
            if client and client not in clients:
                clients.append(client)
    return clients


def clients_from_pulse_state(root_dir: Path) -> list[str]:
    path = root_dir / ".skillbox-state" / "logs" / "runtime" / "pulse.state.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return split_clients([",".join(str(value) for value in payload.get("active_clients") or [])])


def default_clients(root_dir: Path) -> list[str]:
    env_clients = split_clients([os.environ.get("SKILLBOX_PULSE_CLIENTS", "")])
    if env_clients:
        return env_clients
    return clients_from_pulse_state(root_dir)


def fetch_url(url: str, timeout: float) -> FetchResult:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "skillbox-tailnet-app-smoke/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied local URL.
        status = int(getattr(response, "status", response.getcode()))
        content_type = str(response.headers.get("Content-Type", ""))
        body = response.read(2_000_000)
    return FetchResult(status=status, content_type=content_type, body=body)


def status_for_client(root_dir: Path, client: str, profile: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(root_dir / ".env-manager" / "manage.py"),
            "status",
            "--client",
            client,
            "--profile",
            profile,
            "--format",
            "json",
            "--compact",
        ],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"status exited {result.returncode}"
        raise RuntimeError(detail)
    return json.loads(result.stdout)


def app_services(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for service in status_payload.get("services") or []:
        service_id = str(service.get("id") or "").strip()
        if not service_id or service_id in CORE_SERVICE_IDS:
            continue
        if service.get("endpoint_url") or service.get("exposure"):
            services.append(service)
    return services


def asset_urls(base_url: str, body: bytes, content_type: str) -> list[str]:
    if "html" not in content_type.lower():
        return []
    parser = AssetParser(base_url)
    parser.feed(body.decode("utf-8", errors="ignore"))
    return parser.urls


def _status_ok(status: int) -> bool:
    return 200 <= status < 400


def probe_service(
    client: str,
    service: dict[str, Any],
    *,
    timeout: float,
    asset_limit: int,
    fetcher: Callable[[str, float], FetchResult] = fetch_url,
) -> dict[str, Any]:
    service_id = str(service.get("id") or "")
    url = str(service.get("endpoint_url") or "")
    result: dict[str, Any] = {
        "client": client,
        "service": service_id,
        "url": url,
        "state": service.get("state"),
        "exposure": service.get("exposure"),
        "viewable_from_tailnet": bool(service.get("viewable_from_tailnet")),
        "ok": False,
        "assets_checked": 0,
    }
    if service.get("state") != "running":
        result["error"] = "service is not running"
        return result
    if service.get("exposure") != "tailnet-direct":
        result["error"] = "service is not tailnet-direct"
        return result
    if not service.get("viewable_from_tailnet"):
        result["error"] = "service is not marked viewable_from_tailnet"
        return result
    if not url:
        result["error"] = "service has no endpoint_url"
        return result
    try:
        root_response = fetcher(url, timeout)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        result["error"] = f"root fetch failed: {exc}"
        return result
    result["status"] = root_response.status
    if not _status_ok(root_response.status):
        result["error"] = f"root returned HTTP {root_response.status}"
        return result
    assets = asset_urls(url, root_response.body, root_response.content_type)[:asset_limit]
    for asset_url in assets:
        try:
            asset_response = fetcher(asset_url, timeout)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            result["error"] = f"asset fetch failed: {asset_url}: {exc}"
            return result
        if not _status_ok(asset_response.status):
            result["error"] = f"asset returned HTTP {asset_response.status}: {asset_url}"
            return result
        result["assets_checked"] += 1
    result["ok"] = True
    return result


def run_smoke(
    *,
    root_dir: Path,
    clients: list[str],
    profile: str,
    timeout: float,
    asset_limit: int,
    status_loader: Callable[[Path, str, str], dict[str, Any]] = status_for_client,
    fetcher: Callable[[str, float], FetchResult] = fetch_url,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for client in clients:
        try:
            status_payload = status_loader(root_dir, client, profile)
        except (RuntimeError, json.JSONDecodeError) as exc:
            rows.append({"client": client, "ok": False, "error": f"status failed: {exc}"})
            continue
        services = app_services(status_payload)
        if not services:
            rows.append({"client": client, "ok": False, "error": "no app services with endpoints"})
            continue
        for service in services:
            rows.append(
                probe_service(
                    client,
                    service,
                    timeout=timeout,
                    asset_limit=asset_limit,
                    fetcher=fetcher,
                )
            )
    ok = bool(rows) and all(row.get("ok") for row in rows)
    return {
        "ok": ok,
        "profile": profile,
        "clients": clients,
        "service_count": sum(1 for row in rows if row.get("service")),
        "asset_count": sum(int(row.get("assets_checked") or 0) for row in rows),
        "results": rows,
    }


def print_text(payload: dict[str, Any]) -> None:
    status = "ok" if payload["ok"] else "fail"
    print(
        f"tailnet-app-smoke: {status} "
        f"clients={len(payload['clients'])} "
        f"services={payload['service_count']} "
        f"assets={payload['asset_count']}"
    )
    for row in payload["results"]:
        marker = "+" if row.get("ok") else "!"
        service = row.get("service") or "-"
        url = row.get("url") or "-"
        suffix = f" assets={row.get('assets_checked', 0)}" if row.get("ok") else f" error={row.get('error')}"
        print(f"{marker} {row['client']} {service} {url}{suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test Tailnet-direct local app URLs.")
    parser.add_argument("--root-dir", type=Path, default=ROOT, help="Skillbox repo root.")
    parser.add_argument("--client", action="append", default=[], help="Client id or comma-list. Defaults to pulse clients.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"Runtime profile to inspect. Default: {DEFAULT_PROFILE}.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout in seconds.")
    parser.add_argument("--asset-limit", type=int, default=DEFAULT_ASSET_LIMIT, help="Same-origin CSS/JS assets to probe per app.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root_dir = args.root_dir.resolve()
    clients = split_clients(args.client) or default_clients(root_dir)
    if not clients:
        payload = {
            "ok": False,
            "profile": args.profile,
            "clients": [],
            "service_count": 0,
            "asset_count": 0,
            "results": [{"client": "-", "ok": False, "error": "no clients supplied and pulse state has none"}],
        }
    else:
        payload = run_smoke(
            root_dir=root_dir,
            clients=clients,
            profile=args.profile,
            timeout=args.timeout,
            asset_limit=max(args.asset_limit, 0),
        )
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
