from __future__ import annotations

from collections import Counter
from typing import Any

from .shared import EXIT_DRIFT, EXIT_OK, emit_json
from lib.parity_schema import parse_ledger_row


DEV_PROD_PARITY_REPORT_VERSION = 1
PARITY_STATUSES = ("ready", "missing", "drift", "deferred", "not_assessed")
PRODUCTION_CONTRACT_DOMAINS = (
    "reverse_proxy",
    "env",
    "healthcheck",
    "deploy_mode",
    "network",
    "runtime_parity_ledger",
)
BLOCKING_PARITY_STATUSES = {"missing", "drift"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list):
        return []
    return [text for value in values if (text := _clean(value))]


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _id(value: dict[str, Any], *fallback_fields: str) -> str:
    for field in ("id", *fallback_fields):
        text = _clean(value.get(field))
        if text:
            return text
    return ""


def _client_for_report(model: dict[str, Any], client_id: str) -> dict[str, Any]:
    requested = _clean(client_id)
    clients = [client for client in model.get("clients") or [] if isinstance(client, dict)]
    if requested:
        for client in clients:
            if _clean(client.get("id")) == requested:
                return client
        raise RuntimeError(f"Client '{requested}' is not present in the filtered runtime model.")
    active_clients = [_clean(value) for value in model.get("active_clients") or [] if _clean(value)]
    if len(active_clients) == 1:
        return _client_for_report(model, active_clients[0])
    if len(clients) == 1:
        return clients[0]
    raise RuntimeError("parity-report requires exactly one client. Pass a client positional or --client.")


def _client_id_for_report(client: dict[str, Any]) -> str:
    return _clean(client.get("id")) or "unknown"


def _production_stack_contract(client: dict[str, Any]) -> dict[str, Any]:
    for field in ("production_stack", "prod_stack", "dev_prod_parity"):
        value = client.get(field)
        if isinstance(value, dict):
            return value
    return {}


def _contract_source(client: dict[str, Any], suffix: str) -> str:
    overlay = _clean(client.get("_overlay_path")) or f"client:{_client_id_for_report(client)}"
    return f"{overlay}:production_stack.{suffix}"


def _runtime_source(section: str, item: dict[str, Any] | None, fallback: str = "") -> str:
    if not item:
        return section
    item_id = _id(item, "service_id", "service", "env_file")
    return f"{section}.{item_id or fallback or '?'}"


def _finding(
    *,
    domain: str,
    finding_id: str,
    status: str,
    message: str,
    source_declaration: str,
    expected_contract: Any = None,
    actual_runtime: Any = None,
    next_action: str = "",
) -> dict[str, Any]:
    payload = {
        "id": finding_id,
        "domain": domain,
        "status": status,
        "message": message,
        "source_declaration": source_declaration,
        "expected_contract": expected_contract,
        "actual_runtime": actual_runtime,
        "next_action": next_action,
    }
    return {key: value for key, value in payload.items() if value not in ("", None, [], {})}


def _domain_status(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "not_assessed"
    statuses = {_clean(item.get("status")) for item in findings}
    if "drift" in statuses:
        return "drift"
    if "missing" in statuses:
        return "missing"
    if "deferred" in statuses:
        return "deferred"
    if statuses == {"ready"}:
        return "ready"
    return "not_assessed"


def _domain(domain_id: str, label: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(_clean(item.get("status")) for item in findings)
    return {
        "id": domain_id,
        "label": label,
        "status": _domain_status(findings),
        "counts": {status: counts.get(status, 0) for status in PARITY_STATUSES},
        "findings": findings,
    }


def _single_not_assessed(
    domain: str,
    source_declaration: str,
    message: str,
    next_action: str,
) -> list[dict[str, Any]]:
    return [
        _finding(
            domain=domain,
            finding_id=f"{domain}-contract",
            status="not_assessed",
            message=message,
            source_declaration=source_declaration,
            expected_contract="production contract declaration",
            next_action=next_action,
        )
    ]


def _services_by_id(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_clean(service.get("id")): service for service in model.get("services") or [] if _clean(service.get("id"))}


def _routes(model: dict[str, Any]) -> list[dict[str, Any]]:
    return [route for route in model.get("ingress_routes") or [] if isinstance(route, dict)]


def _route_path(route: dict[str, Any]) -> str:
    return _clean(route.get("path") or route.get("path_prefix"))


def _env_files_by_id(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_clean(env_file.get("id")): env_file for env_file in model.get("env_files") or [] if _clean(env_file.get("id"))}


def _active_scope_contains(values: list[str], active_values: set[str]) -> bool:
    if not values or not active_values:
        return True
    if not set(values).isdisjoint(active_values):
        return True
    if "local-all" in values and any(value.startswith("local-") for value in active_values):
        return True
    if "local-all" in active_values and any(value.startswith("local-") for value in values):
        return True
    return False


def _mode_commands(model: dict[str, Any]) -> list[dict[str, Any]]:
    active_clients = {_clean(value) for value in model.get("active_clients") or [] if _clean(value)}
    active_profiles = {_clean(value) for value in model.get("active_profiles") or [] if _clean(value)}
    scoped: list[dict[str, Any]] = []
    for item in model.get("service_mode_commands") or []:
        if not isinstance(item, dict):
            continue
        item_client = _clean(item.get("client"))
        if active_clients and item_client and item_client not in active_clients:
            continue
        item_profiles = _clean_list(item.get("profiles"))
        if not _active_scope_contains(item_profiles, active_profiles):
            continue
        scoped.append(item)
    return scoped


def _route_service_id(route: dict[str, Any]) -> str:
    return _clean(route.get("service_id") or route.get("service"))


def _expected_service_id(item: dict[str, Any]) -> str:
    return _clean(item.get("service_id") or item.get("service"))


def _route_origin(route: dict[str, Any], services: dict[str, dict[str, Any]]) -> str:
    direct = _clean(route.get("upstream") or route.get("origin_url"))
    if direct:
        return direct
    service = services.get(_route_service_id(route)) or {}
    return _clean(service.get("origin_url") or service.get("url"))


def _find_route(expected: dict[str, Any], routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    expected_id = _id(expected, "route")
    if expected_id:
        for route in routes:
            if _clean(route.get("id")) == expected_id:
                return route
    service_id = _expected_service_id(expected)
    expected_path = _clean(expected.get("path") or expected.get("path_prefix"))
    expected_listener = _clean(expected.get("listener"))
    for route in routes:
        if service_id and _route_service_id(route) != service_id:
            continue
        if expected_path and _route_path(route) != expected_path:
            continue
        if expected_listener and _clean(route.get("listener") or "public") != expected_listener:
            continue
        return route
    return None


def _field_mismatches(actual: dict[str, Any], expected: dict[str, Any], fields: list[str]) -> dict[str, dict[str, str]]:
    mismatches: dict[str, dict[str, str]] = {}
    for field in fields:
        expected_value = _clean(expected.get(field))
        if not expected_value:
            continue
        actual_value = _clean(actual.get(field))
        if actual_value != expected_value:
            mismatches[field] = {"expected": expected_value, "actual": actual_value}
    return mismatches


def _reverse_proxy_domain(model: dict[str, Any], client: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    reverse_proxy = contract.get("reverse_proxy") if isinstance(contract.get("reverse_proxy"), dict) else {}
    expected_routes = _as_list(reverse_proxy.get("routes"))
    source = _contract_source(client, "reverse_proxy.routes")
    if not expected_routes:
        return _domain(
            "reverse_proxy",
            "Reverse Proxy",
            _single_not_assessed(
                "reverse_proxy",
                source,
                "No production reverse-proxy routes are declared for this client.",
                f"add production_stack.reverse_proxy.routes in {source.split(':', 1)[0]}",
            ),
        )
    routes = _routes(model)
    services = _services_by_id(model)
    findings: list[dict[str, Any]] = []
    for expected in expected_routes:
        finding_id = _id(expected, "route", "service") or "route"
        if bool(expected.get("deferred")):
            findings.append(
                _finding(
                    domain="reverse_proxy",
                    finding_id=finding_id,
                    status="deferred",
                    message="Reverse-proxy route is explicitly deferred in the production contract.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action=f"remove deferred: true from {source} when the route is ready",
                )
            )
            continue
        route = _find_route(expected, routes)
        if route is None:
            findings.append(
                _finding(
                    domain="reverse_proxy",
                    finding_id=finding_id,
                    status="missing",
                    message="Expected production reverse-proxy route has no matching runtime ingress route.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action="add a matching ingress_routes entry or correct production_stack.reverse_proxy.routes",
                )
            )
            continue
        actual = {
            "id": _clean(route.get("id")),
            "service_id": _route_service_id(route),
            "listener": _clean(route.get("listener") or "public"),
            "path": _route_path(route),
            "match": _clean(route.get("match") or "exact"),
            "upstream": _route_origin(route, services),
        }
        expected_compare = dict(expected)
        if expected_compare.get("path_prefix") and not expected_compare.get("path"):
            expected_compare["path"] = expected_compare["path_prefix"]
        if expected_compare.get("service") and not expected_compare.get("service_id"):
            expected_compare["service_id"] = expected_compare["service"]
        if expected_compare.get("origin_url") and not expected_compare.get("upstream"):
            expected_compare["upstream"] = expected_compare["origin_url"]
        mismatches = _field_mismatches(actual, expected_compare, ["service_id", "listener", "path", "match", "upstream"])
        status = "drift" if mismatches else "ready"
        findings.append(
            _finding(
                domain="reverse_proxy",
                finding_id=finding_id,
                status=status,
                message="Reverse-proxy route matches the production contract." if status == "ready" else "Reverse-proxy route differs from the production contract.",
                source_declaration=_runtime_source("runtime.ingress_routes", route, finding_id),
                expected_contract=expected,
                actual_runtime=actual | ({"mismatches": mismatches} if mismatches else {}),
                next_action="update ingress_routes or production_stack.reverse_proxy.routes so listener/path/match/upstream agree" if mismatches else "",
            )
        )
    return _domain("reverse_proxy", "Reverse Proxy", findings)


def _env_domain(model: dict[str, Any], client: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    env = contract.get("env") if isinstance(contract.get("env"), dict) else {}
    expected_files = _as_list(env.get("files"))
    source = _contract_source(client, "env.files")
    if not expected_files:
        return _domain(
            "env",
            "Environment",
            _single_not_assessed(
                "env",
                source,
                "No production env-file expectations are declared for this client.",
                f"add production_stack.env.files in {source.split(':', 1)[0]}",
            ),
        )
    env_files = _env_files_by_id(model)
    findings: list[dict[str, Any]] = []
    for expected in expected_files:
        expected_id = _id(expected, "env_file")
        finding_id = expected_id or "env-file"
        if bool(expected.get("deferred")):
            findings.append(
                _finding(
                    domain="env",
                    finding_id=finding_id,
                    status="deferred",
                    message="Env parity check is explicitly deferred in the production contract.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action=f"remove deferred: true from {source} when env parity is ready",
                )
            )
            continue
        env_file = env_files.get(expected_id)
        if env_file is None:
            findings.append(
                _finding(
                    domain="env",
                    finding_id=finding_id,
                    status="missing",
                    message="Expected production env file has no matching runtime env_files entry.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action="add a matching env_files entry or correct production_stack.env.files",
                )
            )
            continue
        expected_keys = set(_clean_list(expected.get("required_keys") or expected.get("keys")))
        actual_keys = set(_clean_list(env_file.get("required_keys") or env_file.get("keys")))
        missing_keys = sorted(expected_keys - actual_keys)
        actual = {
            "id": _clean(env_file.get("id")),
            "path": _clean(env_file.get("path")),
            "source_path": _clean((env_file.get("source") or {}).get("path")) if isinstance(env_file.get("source"), dict) else "",
            "required_keys": sorted(actual_keys),
        }
        mismatches = _field_mismatches(actual, expected, ["path", "source_path"])
        if missing_keys:
            mismatches["required_keys"] = {"expected": ",".join(sorted(expected_keys)), "actual": ",".join(sorted(actual_keys))}
        status = "drift" if mismatches else "ready"
        findings.append(
            _finding(
                domain="env",
                finding_id=finding_id,
                status=status,
                message="Runtime env declaration matches the production contract." if status == "ready" else "Runtime env declaration differs from the production contract.",
                source_declaration=_runtime_source("runtime.env_files", env_file, finding_id),
                expected_contract=expected,
                actual_runtime=actual | ({"mismatches": mismatches} if mismatches else {}),
                next_action="update env_files required_keys/path or production_stack.env.files" if mismatches else "",
            )
        )
    return _domain("env", "Environment", findings)


def _health_target(healthcheck: dict[str, Any]) -> str:
    for field in ("url", "path", "port", "pattern"):
        if _clean(healthcheck.get(field)):
            return _clean(healthcheck.get(field))
    return ""


def _healthcheck_domain(model: dict[str, Any], client: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    health = contract.get("healthchecks")
    if not isinstance(health, dict):
        health = contract.get("healthcheck") if isinstance(contract.get("healthcheck"), dict) else {}
    expected_services = _as_list(health.get("services"))
    source = _contract_source(client, "healthchecks.services")
    if not expected_services:
        return _domain(
            "healthcheck",
            "Healthcheck",
            _single_not_assessed(
                "healthcheck",
                source,
                "No production healthcheck expectations are declared for this client.",
                f"add production_stack.healthchecks.services in {source.split(':', 1)[0]}",
            ),
        )
    services = _services_by_id(model)
    findings: list[dict[str, Any]] = []
    for expected in expected_services:
        service_id = _expected_service_id(expected)
        finding_id = service_id or _id(expected) or "service"
        if bool(expected.get("deferred")):
            findings.append(
                _finding(
                    domain="healthcheck",
                    finding_id=finding_id,
                    status="deferred",
                    message="Healthcheck parity is explicitly deferred in the production contract.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action=f"remove deferred: true from {source} when healthcheck parity is ready",
                )
            )
            continue
        service = services.get(service_id)
        if service is None:
            findings.append(
                _finding(
                    domain="healthcheck",
                    finding_id=finding_id,
                    status="missing",
                    message="Expected service has no runtime service declaration.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action="add a matching services entry or correct production_stack.healthchecks.services",
                )
            )
            continue
        healthcheck = service.get("healthcheck") if isinstance(service.get("healthcheck"), dict) else {}
        actual = {
            "service_id": service_id,
            "type": _clean(healthcheck.get("type") or service.get("health_type")),
            "target": _health_target(healthcheck) or _clean(service.get("health_target")),
            "timeout_seconds": _clean(healthcheck.get("timeout_seconds")),
        }
        expected_compare = dict(expected)
        if not expected_compare.get("target"):
            expected_compare["target"] = _health_target(expected_compare)
        mismatches = _field_mismatches(actual, expected_compare, ["type", "target", "timeout_seconds"])
        status = "drift" if mismatches else "ready"
        findings.append(
            _finding(
                domain="healthcheck",
                finding_id=finding_id,
                status=status,
                message="Runtime healthcheck matches the production contract." if status == "ready" else "Runtime healthcheck differs from the production contract.",
                source_declaration=_runtime_source("runtime.services", service, finding_id),
                expected_contract=expected,
                actual_runtime=actual | ({"mismatches": mismatches} if mismatches else {}),
                next_action="update services.healthcheck or production_stack.healthchecks.services" if mismatches else "",
            )
        )
    return _domain("healthcheck", "Healthcheck", findings)


def _find_mode_command(expected: dict[str, Any], commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    service_id = _expected_service_id(expected)
    mode = _clean(expected.get("mode") or expected.get("deploy_mode") or "skillbox-local")
    for command in commands:
        if _clean(command.get("service_id")) == service_id and _clean(command.get("mode")) == mode:
            return command
    return None


def _deploy_mode_domain(model: dict[str, Any], client: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    deploy = contract.get("deploy") if isinstance(contract.get("deploy"), dict) else {}
    expected_modes = _as_list(deploy.get("modes"))
    source = _contract_source(client, "deploy.modes")
    if not expected_modes:
        return _domain(
            "deploy_mode",
            "Deploy Mode",
            _single_not_assessed(
                "deploy_mode",
                source,
                "No production deploy-mode expectations are declared for this client.",
                f"add production_stack.deploy.modes in {source.split(':', 1)[0]}",
            ),
        )
    commands = _mode_commands(model)
    findings: list[dict[str, Any]] = []
    for expected in expected_modes:
        service_id = _expected_service_id(expected)
        mode = _clean(expected.get("mode") or expected.get("deploy_mode") or "skillbox-local")
        finding_id = f"{service_id}:{mode}" if service_id else mode
        if bool(expected.get("deferred")):
            findings.append(
                _finding(
                    domain="deploy_mode",
                    finding_id=finding_id,
                    status="deferred",
                    message="Deploy-mode parity is explicitly deferred in the production contract.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action=f"remove deferred: true from {source} when deploy-mode parity is ready",
                )
            )
            continue
        command = _find_mode_command(expected, commands)
        if command is None:
            findings.append(
                _finding(
                    domain="deploy_mode",
                    finding_id=finding_id,
                    status="missing",
                    message="Expected deploy mode has no matching runtime service command.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action="add services[].commands.<mode> or correct production_stack.deploy.modes",
                )
            )
            continue
        actual = {
            "service_id": _clean(command.get("service_id")),
            "mode": _clean(command.get("mode")),
            "command": _clean(command.get("command")),
        }
        mismatches = _field_mismatches(actual, expected, ["command"])
        status = "drift" if mismatches else "ready"
        findings.append(
            _finding(
                domain="deploy_mode",
                finding_id=finding_id,
                status=status,
                message="Deploy mode matches the production contract." if status == "ready" else "Deploy mode differs from the production contract.",
                source_declaration=_runtime_source("runtime.service_mode_commands", command, finding_id),
                expected_contract=expected,
                actual_runtime=actual | ({"mismatches": mismatches} if mismatches else {}),
                next_action="update services[].commands or production_stack.deploy.modes" if mismatches else "",
            )
        )
    return _domain("deploy_mode", "Deploy Mode", findings)


def _service_networks(service: dict[str, Any]) -> list[str]:
    values = []
    for field in ("network", "networks"):
        value = service.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    for parent_field in ("docker", "compose"):
        parent = service.get(parent_field)
        if isinstance(parent, dict):
            values.extend(_clean_list(parent.get("networks") or parent.get("network")))
    return sorted(set(_clean_list(values)))


def _network_contract_items(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("networks")
    if isinstance(raw, list):
        return _as_list(raw)
    if isinstance(raw, dict):
        if isinstance(raw.get("services"), list):
            return _as_list(raw.get("services"))
        if _clean(raw.get("service") or raw.get("service_id")):
            return [dict(raw)]
    network = contract.get("network") if isinstance(contract.get("network"), dict) else {}
    return _as_list(network.get("services"))


def _network_domain(model: dict[str, Any], client: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    expected_networks = _network_contract_items(contract)
    source = _contract_source(client, "networks")
    if not expected_networks:
        return _domain(
            "network",
            "Network",
            _single_not_assessed(
                "network",
                source,
                "No production Docker/network assumptions are declared for this client.",
                f"add production_stack.networks in {source.split(':', 1)[0]}",
            ),
        )
    services = _services_by_id(model)
    findings: list[dict[str, Any]] = []
    for expected in expected_networks:
        service_id = _expected_service_id(expected)
        network_name = _clean(expected.get("name") or expected.get("network"))
        finding_id = f"{service_id}:{network_name}" if service_id and network_name else service_id or network_name or "network"
        if bool(expected.get("deferred")):
            findings.append(
                _finding(
                    domain="network",
                    finding_id=finding_id,
                    status="deferred",
                    message="Network parity is explicitly deferred in the production contract.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action=f"remove deferred: true from {source} when network parity is ready",
                )
            )
            continue
        service = services.get(service_id)
        if service is None:
            findings.append(
                _finding(
                    domain="network",
                    finding_id=finding_id,
                    status="missing",
                    message="Expected network service has no runtime service declaration.",
                    source_declaration=source,
                    expected_contract=expected,
                    next_action="add a matching services entry or correct production_stack.networks",
                )
            )
            continue
        actual_networks = _service_networks(service)
        if not network_name:
            status = "not_assessed"
            message = "Network contract row does not name the expected network."
            next_action = "set name or network in production_stack.networks"
        elif network_name in actual_networks:
            status = "ready"
            message = "Runtime service network matches the production contract."
            next_action = ""
        else:
            status = "drift"
            message = "Runtime service network differs from the production contract."
            next_action = "update services[].networks or production_stack.networks"
        findings.append(
            _finding(
                domain="network",
                finding_id=finding_id,
                status=status,
                message=message,
                source_declaration=_runtime_source("runtime.services", service, finding_id),
                expected_contract=expected,
                actual_runtime={"service_id": service_id, "networks": actual_networks},
                next_action=next_action,
            )
        )
    return _domain("network", "Network", findings)


def _runtime_parity_ledger_domain(model: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    items = [item for item in model.get("parity_ledger") or [] if isinstance(item, dict)]
    source = _contract_source(client, "parity_ledger")
    if not items:
        return _domain(
            "runtime_parity_ledger",
            "Runtime Parity Ledger",
            _single_not_assessed(
                "runtime_parity_ledger",
                source,
                "No runtime parity-ledger rows are present for this client/profile.",
                "add parity_ledger rows for production-owned runtime surfaces or document why the ledger is not needed",
            ),
        )
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        row = parse_ledger_row(item, index=index, source=source)
        state = row.ownership_state
        surface_id = row.id or row.legacy_surface or "surface"
        if state in {"covered", "external"}:
            status = "ready"
            message = "Runtime parity-ledger row is covered or intentionally external."
            next_action = ""
        else:
            status = "deferred"
            message = "Runtime parity-ledger row is not covered by a native Skillbox runtime surface."
            next_action = row.request_error or "promote or remove the deferred parity-ledger row"
        findings.append(
            _finding(
                domain="runtime_parity_ledger",
                finding_id=surface_id,
                status=status,
                message=message,
                source_declaration=f"runtime.parity_ledger.{surface_id}",
                expected_contract={"ownership_state": "covered|external"},
                actual_runtime={
                    "ownership_state": state,
                    "surface_type": row.surface_type,
                    "legacy_surface": row.legacy_surface,
                    "profiles": list(row.intended_profiles or row.profiles),
                },
                next_action=next_action,
            )
        )
    return _domain("runtime_parity_ledger", "Runtime Parity Ledger", findings)


def _domain_counts(domains: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_clean(domain.get("status")) for domain in domains)
    return {status: counts.get(status, 0) for status in PARITY_STATUSES}


def _finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_clean(finding.get("status")) for finding in findings)
    return {status: counts.get(status, 0) for status in PARITY_STATUSES}


def _next_actions(findings: list[dict[str, Any]], client_id: str) -> list[str]:
    actions: list[str] = []
    for finding in findings:
        if _clean(finding.get("status")) == "ready":
            continue
        action = _clean(finding.get("next_action"))
        if action and action not in actions:
            actions.append(action)
    report_action = f"parity-report {client_id} --format json"
    if report_action not in actions:
        actions.append(report_action)
    return actions


def collect_dev_prod_parity_report(model: dict[str, Any], *, client_id: str = "") -> dict[str, Any]:
    client = _client_for_report(model, client_id)
    cid = _client_id_for_report(client)
    contract = _production_stack_contract(client)
    domains = [
        _reverse_proxy_domain(model, client, contract),
        _env_domain(model, client, contract),
        _healthcheck_domain(model, client, contract),
        _deploy_mode_domain(model, client, contract),
        _network_domain(model, client, contract),
        _runtime_parity_ledger_domain(model, client),
    ]
    findings = [finding for domain in domains for finding in domain.get("findings") or []]
    blocking = [finding for finding in findings if _clean(finding.get("status")) in BLOCKING_PARITY_STATUSES]
    domain_counts = _domain_counts(domains)
    finding_counts = _finding_counts(findings)
    ready = not blocking and domain_counts["ready"] == len(domains)
    return {
        "ok": ready,
        "ready": ready,
        "version": DEV_PROD_PARITY_REPORT_VERSION,
        "client_id": cid,
        "active_profiles": sorted(model.get("active_profiles") or []),
        "contract_present": bool(contract),
        "status": "ready" if ready else ("drift" if blocking else "not_ready"),
        "summary": {
            "domains": domain_counts,
            "findings": finding_counts,
            "total_domains": len(domains),
            "total_findings": len(findings),
        },
        "domains": domains,
        "findings": findings,
        "blocking_count": len(blocking),
        "next_actions": _next_actions(findings, cid),
    }


def parity_report_evidence_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "ok": bool(payload.get("ok")),
        "contract_present": bool(payload.get("contract_present")),
        "blocking_count": int(payload.get("blocking_count") or 0),
        "summary": payload.get("summary") or {},
        "domains": [
            {
                "id": domain.get("id"),
                "status": domain.get("status"),
                "counts": domain.get("counts") or {},
            }
            for domain in payload.get("domains") or []
        ],
        "next_actions": payload.get("next_actions") or [],
    }


def dev_prod_parity_text_lines(payload: dict[str, Any]) -> list[str]:
    summary = payload.get("summary") or {}
    domain_counts = summary.get("domains") or {}
    lines = [
        f"dev-prod parity: {payload.get('client_id') or '-'}",
        f"status: {payload.get('status') or 'unknown'} blocking={payload.get('blocking_count', 0)}",
        "domains: "
        + " ".join(f"{status}={domain_counts.get(status, 0)}" for status in PARITY_STATUSES),
    ]
    for domain in payload.get("domains") or []:
        findings = domain.get("findings") or []
        lines.append(f"- {domain.get('id')}: {domain.get('status')} ({len(findings)} finding(s))")
        for finding in findings[:3]:
            if finding.get("status") == "ready":
                continue
            lines.append(f"  - {finding.get('id')}: {finding.get('status')} - {finding.get('message')}")
    actions = payload.get("next_actions") or []
    if actions:
        lines.append("next:")
        lines.extend(f"  - {action}" for action in actions[:5])
    return lines


def emit_dev_prod_parity_report(payload: dict[str, Any], *, fmt: str) -> int:
    if fmt == "json":
        emit_json(payload)
    else:
        print("\n".join(dev_prod_parity_text_lines(payload)))
    return EXIT_DRIFT if int(payload.get("blocking_count") or 0) else EXIT_OK
