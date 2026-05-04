from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .shared import EXIT_ERROR, EXIT_OK, load_yaml

DEFAULT_OPERATOR_ENV_FILE = "~/repos/buildooor/.env.local"
DEFAULT_API_URL = "https://api.sweetpotato.dev"
DEFAULT_BOOKING_URL = "https://buildooor.com/bookme"
DEFAULT_API_KEY_ENV = "NEXT_PUBLIC_SPAPS_PUBLISHABLE_KEY"
DEFAULT_ACCESS_TOKEN_ENV = "SPAPS_AUTH_ACCESS_TOKEN"


class OperatorBookingError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        recoverable: bool = True,
        recovery_hint: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.recoverable = recoverable
        self.recovery_hint = recovery_hint
        self.data = data or {}


def operator_booking_error_payload(exc: RuntimeError) -> dict[str, Any]:
    if isinstance(exc, OperatorBookingError):
        error: dict[str, Any] = {
            "type": exc.error_type,
            "message": str(exc),
            "recoverable": exc.recoverable,
        }
        if exc.recovery_hint:
            error["recovery_hint"] = exc.recovery_hint
        error.update(exc.data)
        return {"error": error}
    return {
        "error": {
            "type": "operator_booking_error",
            "message": str(exc),
            "recoverable": True,
        }
    }


def operator_booking_payload(
    model: dict[str, Any],
    *,
    action: str,
    client_id: str | None = None,
    date: str | None = None,
    slot: str | None = None,
    email: str | None = None,
    name: str | None = None,
    redirect_url: str | None = None,
    origin: str | None = None,
    send_magic_link: bool = False,
    dry_run: bool = False,
    limit: int = 8,
    access_token_env: str | None = None,
) -> tuple[dict[str, Any], int]:
    config = resolve_operator_booking_config(
        model,
        client_id=client_id,
        origin=origin,
        access_token_env=access_token_env,
    )
    if action in {"availability", "times", "list"}:
        return _availability_payload(config, limit=limit), EXIT_OK
    if action == "config":
        return _config_payload(config), EXIT_OK
    if action == "book":
        return _book_payload(
            config,
            date=date,
            slot=slot,
            email=email,
            name=name,
            redirect_url=redirect_url,
            send_magic_link=send_magic_link,
            dry_run=dry_run,
        ), EXIT_OK
    raise OperatorBookingError(
        "operator_booking_unknown_action",
        f"Unknown operator booking action: {action}",
        recovery_hint="Use one of: availability, times, config, book.",
        data={"action": action},
    )


def resolve_operator_booking_config(
    model: dict[str, Any],
    *,
    client_id: str | None = None,
    origin: str | None = None,
    access_token_env: str | None = None,
) -> dict[str, Any]:
    client = _select_client(model, client_id)
    overlay_config = _read_overlay_config(client)
    env_file = _expand_path(str(overlay_config.get("env_file") or DEFAULT_OPERATOR_ENV_FILE))
    env_values = _read_env_file(env_file)

    api_base = str(
        overlay_config.get("spaps_api_url")
        or env_values.get("NEXT_PUBLIC_SPAPS_API_URL")
        or os.environ.get("NEXT_PUBLIC_SPAPS_API_URL")
        or DEFAULT_API_URL
    ).rstrip("/")
    availability_url = str(
        overlay_config.get("availability_url")
        or f"{api_base}/api/dayrate/availability"
    )
    api_base = _api_base_from_url(availability_url) or api_base
    config = {
        "client_id": client.get("id"),
        "booking_url": str(overlay_config.get("booking_url") or DEFAULT_BOOKING_URL),
        "availability_url": availability_url,
        "availability_method": str(overlay_config.get("availability_method") or "GET").upper(),
        "booking_hold_url": str(
            overlay_config.get("booking_hold_url")
            or f"{api_base}/api/dayrate/book-x402"
        ),
        "booking_hold_method": str(overlay_config.get("booking_hold_method") or "POST").upper(),
        "magic_link_url": str(
            overlay_config.get("magic_link_url")
            or overlay_config.get("auth_magic_link_url")
            or f"{api_base}/api/auth/magic-link"
        ),
        "magic_link_method": str(overlay_config.get("magic_link_method") or "POST").upper(),
        "magic_link_redirect_url": overlay_config.get("magic_link_redirect_url"),
        "timezone": str(overlay_config.get("timezone") or "America/Toronto"),
        "preferred_session": str(overlay_config.get("preferred_session") or "AI Build Diagnosis"),
        "payment_required_before_handoff": bool(overlay_config.get("payment_required_before_handoff", True)),
        "api_key_env": str(
            overlay_config.get("api_key_env")
            or overlay_config.get("availability_api_key_env")
            or DEFAULT_API_KEY_ENV
        ),
        "access_token_env": str(
            access_token_env
            or overlay_config.get("access_token_env")
            or DEFAULT_ACCESS_TOKEN_ENV
        ),
        "origin": origin or overlay_config.get("availability_origin") or overlay_config.get("origin"),
        "env_file": str(env_file),
        "_env_values": env_values,
        "_overlay_path": client.get("_overlay_path"),
    }
    config["api_key"] = _resolve_api_key(config, overlay_config)
    config["access_token"] = _resolve_access_token(config, overlay_config)
    return config


def _select_client(model: dict[str, Any], client_id: str | None) -> dict[str, Any]:
    clients = list(model.get("clients") or [])
    requested = client_id or (model.get("active_clients") or [None])[0]
    if requested:
        for client in clients:
            if client.get("id") == requested:
                return client
        raise OperatorBookingError(
            "operator_booking_client_missing",
            f"Client {requested!r} is not in the resolved runtime model.",
            recovery_hint="Run `manage.py render --format json` and confirm the client overlay is active.",
            data={"client_id": requested},
        )
    if clients:
        return clients[0]
    raise OperatorBookingError(
        "operator_booking_client_missing",
        "No client is available in the resolved runtime model.",
        recovery_hint="Pass --client personal or focus a client before using operator booking.",
    )


def _read_overlay_config(client: dict[str, Any]) -> dict[str, Any]:
    overlay_path = client.get("_overlay_path")
    if not overlay_path:
        return {}
    path = Path(str(overlay_path)).expanduser()
    if not path.is_file():
        return {}
    doc = load_yaml(path)
    raw_client = doc.get("client") if isinstance(doc, dict) else None
    if not isinstance(raw_client, dict):
        return {}
    context = raw_client.get("context") if isinstance(raw_client.get("context"), dict) else {}
    for candidate in (
        raw_client.get("human_operator"),
        raw_client.get("operator_booking"),
        context.get("human_operator"),
        context.get("operator_booking"),
    ):
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _expand_path(raw_path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve(strict=False)


def _api_base_from_url(url: str) -> str | None:
    suffixes = ("/api/dayrate/availability", "/api/dayrate/book-x402", "/api/auth/magic-link")
    stripped = url.rstrip("/")
    for suffix in suffixes:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].rstrip("/")
    return None


def _resolve_api_key(config: dict[str, Any], overlay_config: dict[str, Any]) -> str:
    direct = overlay_config.get("api_key") or overlay_config.get("availability_api_key")
    if direct:
        return str(direct)
    key_env = str(config.get("api_key_env") or DEFAULT_API_KEY_ENV)
    value = config["_env_values"].get(key_env) or os.environ.get(key_env)
    if value:
        return value
    raise OperatorBookingError(
        "operator_booking_api_key_missing",
        f"Publishable key env var {key_env} is not configured.",
        recovery_hint=(
            f"Set {key_env} in the environment or in {config['env_file']}, "
            "or add human_operator.api_key_env to the client overlay."
        ),
        data={"api_key_env": key_env, "env_file": config["env_file"]},
    )


def _resolve_access_token(config: dict[str, Any], overlay_config: dict[str, Any]) -> str | None:
    direct = overlay_config.get("access_token")
    if direct:
        return str(direct)
    token_env = str(config.get("access_token_env") or DEFAULT_ACCESS_TOKEN_ENV)
    return config["_env_values"].get(token_env) or os.environ.get(token_env)


def _headers(config: dict[str, Any], *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-API-Key": str(config["api_key"]),
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    if config.get("origin"):
        headers["Origin"] = str(config["origin"])
    if config.get("access_token"):
        headers["Authorization"] = f"Bearer {config['access_token']}"
    return headers


def _http_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return _parse_json_payload(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        parsed = _parse_json_payload(payload)
        message = _spaps_error_message(parsed) or f"SPAPS request failed with HTTP {exc.code}"
        raise OperatorBookingError(
            "operator_booking_http_error",
            message,
            recovery_hint="Check the configured SPAPS URL, publishable key scope, and rate-limit state.",
            data={"status": exc.code, "url": url, "response": parsed},
        ) from exc
    except OSError as exc:
        raise OperatorBookingError(
            "operator_booking_network_error",
            str(exc),
            recovery_hint="Confirm the SPAPS dev endpoint is reachable from this machine.",
            data={"url": url},
        ) from exc


def _parse_json_payload(payload: str) -> dict[str, Any]:
    if not payload.strip():
        return {}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OperatorBookingError(
            "operator_booking_invalid_json",
            "SPAPS returned a non-JSON response.",
            data={"body_preview": payload[:500]},
        ) from exc
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _unwrap_spaps_data(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("success") is False:
        raise OperatorBookingError(
            "operator_booking_spaps_error",
            _spaps_error_message(payload) or "SPAPS request failed.",
            data={"response": payload},
        )
    data = payload.get("data") if "success" in payload else payload
    return data if isinstance(data, dict) else {"data": data}


def _spaps_error_message(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if payload.get("message"):
        return str(payload["message"])
    return None


def _availability_payload(config: dict[str, Any], *, limit: int) -> dict[str, Any]:
    response = _http_json(
        str(config["availability_url"]),
        method=str(config["availability_method"]),
        headers=_headers(config),
    )
    data = _unwrap_spaps_data(response)
    slots = [slot for slot in data.get("slots") or [] if isinstance(slot, dict)]
    available_slots = [slot for slot in slots if slot.get("available")]
    limit = max(1, int(limit or 8))
    return {
        "action": "availability",
        "client_id": config["client_id"],
        "booking_url": config["booking_url"],
        "timezone": config["timezone"],
        "preferred_session": config["preferred_session"],
        "totalSlots": data.get("totalSlots"),
        "bookedSlots": data.get("bookedSlots"),
        "baseRate": data.get("baseRate"),
        "available": len(available_slots),
        "slots": available_slots[:limit],
        "next_actions": [
            "operator-booking book --date <YYYY-MM-DD> --slot <SLOT> --email <EMAIL> --name <NAME>",
        ],
    }


def _config_payload(config: dict[str, Any]) -> dict[str, Any]:
    public_config = {
        key: value
        for key, value in config.items()
        if key not in {"api_key", "access_token", "_env_values"} and not str(key).startswith("_")
    }
    public_config["api_key_env"] = config.get("api_key_env")
    public_config["api_key_configured"] = bool(config.get("api_key"))
    public_config["access_token_env"] = config.get("access_token_env")
    public_config["access_token_configured"] = bool(config.get("access_token"))
    return {
        "action": "config",
        "operator_booking": public_config,
        "next_actions": ["operator-booking availability --format json"],
    }


def _book_payload(
    config: dict[str, Any],
    *,
    date: str | None,
    slot: str | None,
    email: str | None,
    name: str | None,
    redirect_url: str | None,
    send_magic_link: bool,
    dry_run: bool,
) -> dict[str, Any]:
    missing = [
        label
        for label, value in (("date", date), ("slot", slot), ("email", email), ("name", name))
        if not value
    ]
    if missing:
        raise OperatorBookingError(
            "operator_booking_missing_booking_fields",
            "Missing required booking fields: " + ", ".join(missing),
            recovery_hint="Pass --date, --slot, --email, and --name.",
            data={"missing": missing},
        )

    booking_body = {
        "date": date,
        "slot": slot,
        "clientEmail": email,
        "clientName": name,
    }
    magic_link_body = {
        "email": email,
        "generate_state": True,
    }
    effective_redirect = redirect_url or config.get("magic_link_redirect_url")
    if effective_redirect:
        magic_link_body["redirect_url"] = effective_redirect

    if dry_run:
        return {
            "action": "book",
            "dry_run": True,
            "booking_url": config["booking_hold_url"],
            "booking_body": booking_body,
            "magic_link_url": config["magic_link_url"] if send_magic_link else None,
            "magic_link_body": magic_link_body if send_magic_link else None,
        }

    magic_link = None
    if send_magic_link:
        magic_link = _unwrap_spaps_data(
            _http_json(
                str(config["magic_link_url"]),
                method=str(config["magic_link_method"]),
                headers=_headers(config, json_body=True),
                body=magic_link_body,
            )
        )

    booking = _unwrap_spaps_data(
        _http_json(
            str(config["booking_hold_url"]),
            method=str(config["booking_hold_method"]),
            headers=_headers(config, json_body=True),
            body=booking_body,
        )
    )
    next_actions = []
    if booking.get("resourceKey") and booking.get("actionKey"):
        next_actions.append(
            "Pay the x402 resource, then POST the payment signature to "
            f"/api/x402/resources/{booking['resourceKey']}/actions/{booking['actionKey']}."
        )
    return {
        "action": "book",
        "client_id": config["client_id"],
        "magic_link": magic_link,
        "booking": booking,
        "payment_required_before_handoff": config["payment_required_before_handoff"],
        "next_actions": next_actions,
    }
