from __future__ import annotations

from .shared import *
from .runtime_ops import service_bootstrap_task_ids, service_dependency_ids, task_dependency_ids


def _render_header_lines(model: dict[str, Any]) -> list[str]:
    available_clients = ", ".join(client["id"] for client in model.get("clients") or []) or "(none)"
    default_client = (model.get("selection") or {}).get("default_client") or "(none)"
    lines = [
        f"clients: {available_clients}",
        f"default client: {default_client}",
    ]
    active_clients = model.get("active_clients") or []
    if active_clients:
        lines.append(f"active clients: {', '.join(active_clients)}")
    active_profiles = model.get("active_profiles") or []
    if active_profiles:
        lines.append(f"active profiles: {', '.join(active_profiles)}")
    lines.append(f"runtime manifest: {model['manifest_file']}")
    return lines


def _render_repo_lines(model: dict[str, Any]) -> list[str]:
    return [f"repos: {len(model['repos'])}"] + [
        f"  - {repo['id']}: {repo.get('kind', 'repo')} @ {repo['path']}"
        for repo in model["repos"]
    ]


def _render_artifact_lines(model: dict[str, Any]) -> list[str]:
    return [f"artifacts: {len(model['artifacts'])}"] + [
        f"  - {artifact['id']}: {artifact.get('kind', 'artifact')} @ {artifact['path']}"
        for artifact in model["artifacts"]
    ]


def _render_env_file_lines(model: dict[str, Any]) -> list[str]:
    return [f"env files: {len(model['env_files'])}"] + [
        f"  - {env_file['id']}: {env_file.get('kind', 'env-file')} @ {env_file['path']}"
        for env_file in model["env_files"]
    ]


def _render_skill_lines(model: dict[str, Any]) -> list[str]:
    lines = [f"skills: {len(model['skills'])}"]
    for skillset in model["skills"]:
        kind = skillset.get("kind", "skill-repo-set")
        location = skillset.get("skill_repos_config") or skillset.get("bundle_dir") or "(unknown)"
        lines.append(f"  - {skillset['id']}: {kind} @ {location}")
    return lines


def _task_dependency_summary(task: dict[str, Any]) -> str:
    dependency_ids = task_dependency_ids(task)
    return f" depends on {', '.join(dependency_ids)}" if dependency_ids else ""


def _render_task_lines(model: dict[str, Any]) -> list[str]:
    return [f"tasks: {len(model['tasks'])}"] + [
        f"  - {task['id']}: {task.get('kind', 'task')}{_task_dependency_summary(task)}"
        for task in model["tasks"]
    ]


def _service_dependency_summary(service: dict[str, Any]) -> str:
    dependency_ids = service_dependency_ids(service)
    return f" depends on {', '.join(dependency_ids)}" if dependency_ids else ""


def _service_bootstrap_summary(service: dict[str, Any]) -> str:
    bootstrap_task_ids = service_bootstrap_task_ids(service)
    return f" bootstrap {', '.join(bootstrap_task_ids)}" if bootstrap_task_ids else ""


def _render_service_lines(model: dict[str, Any]) -> list[str]:
    lines = [f"services: {len(model['services'])}"]
    for service in model["services"]:
        profiles = ", ".join(service.get("profiles") or []) or "core"
        lines.append(
            f"  - {service['id']}: {service.get('kind', 'service')} [{profiles}]"
            f"{_service_dependency_summary(service)}{_service_bootstrap_summary(service)}"
        )
    return lines


def _render_log_lines(model: dict[str, Any]) -> list[str]:
    return [f"logs: {len(model['logs'])}"] + [
        f"  - {log_item['id']}: {log_item['path']}"
        for log_item in model["logs"]
    ]


def _render_check_lines(model: dict[str, Any]) -> list[str]:
    return [f"checks: {len(model['checks'])}"] + [
        f"  - {check['id']}: {check['type']}"
        for check in model["checks"]
    ]


def _render_bridge_lines(model: dict[str, Any]) -> list[str]:
    bridges = model.get("bridges") or []
    lines: list[str] = []
    if not bridges:
        return lines
    lines.append(f"bridges: {len(bridges)}")
    for bridge in bridges:
        targets = ", ".join(str(t) for t in bridge.get("legacy_targets") or [])
        lines.append(f"  - {bridge['id']}: {bridge.get('env_tier', 'local')} [{targets}]")
    return lines


def _render_ingress_route_lines(model: dict[str, Any]) -> list[str]:
    ingress_routes = model.get("ingress_routes") or []
    lines: list[str] = []
    if not ingress_routes:
        return lines
    lines.append(f"ingress routes: {len(ingress_routes)}")
    for route in ingress_routes:
        listener = str(route.get("listener") or "public")
        match = str(route.get("match") or "exact")
        path = str(route.get("path") or route.get("path_prefix") or "")
        lines.append(
            f"  - {route['id']}: {listener} {path} "
            f"-> {route.get('service_id', '')} ({match})"
        )
    return lines


def render_text_lines(model: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for renderer in (
        _render_header_lines,
        _render_repo_lines,
        _render_artifact_lines,
        _render_env_file_lines,
        _render_skill_lines,
        _render_task_lines,
        _render_service_lines,
        _render_log_lines,
        _render_check_lines,
        _render_bridge_lines,
        _render_ingress_route_lines,
    ):
        lines.extend(renderer(model))
    return lines


def print_render_text(model: dict[str, Any]) -> None:
    for line in render_text_lines(model):
        print(line)


def detail_lines(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {', '.join(str(item) for item in value)}")
        else:
            lines.append(f"{key}: {value}")
    return lines


def print_doctor_text(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status.upper():4} {result.code}: {result.message}")
        if result.details:
            for line in detail_lines(result.details):
                print(f"     {line}")

    counts = {
        "pass": sum(1 for item in results if item.status == "pass"),
        "warn": sum(1 for item in results if item.status == "warn"),
        "fail": sum(1 for item in results if item.status == "fail"),
    }
    print()
    print(
        "summary: "
        f"{counts['pass']} passed, "
        f"{counts['warn']} warnings, "
        f"{counts['fail']} failed"
    )


def _print_status_header(status_payload: dict[str, Any]) -> None:
    box_access = status_payload.get("box_access") or {}
    if box_access.get("phone_url"):
        print(f"Open this on phone: {box_access['phone_url']}")
    if box_access.get("magicdns_url"):
        print(f"MagicDNS: {box_access['magicdns_url']}")
    available_clients = ", ".join(client["id"] for client in status_payload.get("clients") or []) or "(none)"
    print(f"clients: {available_clients}")
    print(f"default client: {status_payload.get('default_client') or '(none)'}")
    active_clients = status_payload.get("active_clients") or []
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = status_payload.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")


def _format_repo_summary(repo: dict[str, Any]) -> str:
    summary = "present" if repo["present"] else "missing"
    if repo.get("git"):
        summary = (
            f"{summary}, git {repo.get('branch', '(detached)')}, "
            f"{repo.get('dirty', 0)} dirty, {repo.get('untracked', 0)} untracked"
        )
    return summary


def _print_status_repos(status_payload: dict[str, Any]) -> None:
    print("repos:")
    for repo in status_payload["repos"]:
        print(f"  - {repo['id']}: {_format_repo_summary(repo)}")


def _print_status_artifacts(status_payload: dict[str, Any]) -> None:
    print("artifacts:")
    for artifact in status_payload["artifacts"]:
        print(f"  - {artifact['id']}: {artifact.get('state', 'unknown')} ({artifact.get('source_kind', 'manual')})")


def _print_status_env_files(status_payload: dict[str, Any]) -> None:
    print("env files:")
    for env_file in status_payload["env_files"]:
        print(f"  - {env_file['id']}: {env_file['state']} ({env_file['source_kind']})")


def _format_skillset_summary(skillset: dict[str, Any]) -> str:
    total_targets = 0
    healthy_targets = 0
    for skill_entry in skillset["skills"]:
        for target in skill_entry["targets"]:
            total_targets += 1
            if target["state"] == "ok":
                healthy_targets += 1
    lock_summary = "invalid" if skillset.get("lock_error") else ("present" if skillset["lock_present"] else "missing")
    return (
        f"lock {lock_summary}, {len(skillset['skills'])} skills, "
        f"{healthy_targets}/{total_targets} targets healthy"
    )


def _print_status_skills(status_payload: dict[str, Any]) -> None:
    print("skills:")
    for skillset in status_payload["skills"]:
        print(f"  - {skillset['id']}: {_format_skillset_summary(skillset)}")


def _format_task_summary(task: dict[str, Any]) -> str:
    summary = task.get("state", "pending")
    dependency_ids = task.get("depends_on") or []
    if dependency_ids:
        summary = f"{summary}, depends on {', '.join(dependency_ids)}"
    return summary


def _print_status_tasks(status_payload: dict[str, Any]) -> None:
    print("tasks:")
    for task in status_payload["tasks"]:
        print(f"  - {task['id']}: {_format_task_summary(task)}")


def _format_service_line(service: dict[str, Any]) -> str:
    summary = service.get("state", "declared")
    if service.get("pid") is not None:
        summary = f"{summary} (pid {service['pid']})"
    elif service.get("managed") is False and service.get("manager_reason"):
        summary = f"{summary} ({service['manager_reason']})"
    dependency_ids = service.get("depends_on") or []
    if dependency_ids:
        summary = f"{summary}, depends on {', '.join(dependency_ids)}"
    bootstrap_task_ids = service.get("bootstrap_tasks") or []
    if bootstrap_task_ids:
        summary = f"{summary}, bootstrap {', '.join(bootstrap_task_ids)}"
    endpoint = service.get("endpoint") or {}
    endpoint_url = str(service.get("endpoint_url") or endpoint.get("access_url") or "").strip()
    exposure = str(endpoint.get("exposure") or service.get("exposure") or "").strip()
    if endpoint_url:
        summary = f"{summary} -> {endpoint_url}"
    if exposure and endpoint_url:
        summary = f"{summary} [{exposure}]"
    elif exposure:
        summary = f"{summary}, exposure {exposure}"
    # WG-006: ownership_state badge so operators can see at a glance
    # whether a service is covered, bridge-only, deferred, or external
    # per the parity ledger (shared.md:148-180, backend.md:77-90).
    ownership_state = str(service.get("ownership_state") or "").strip()
    badge = f" [{ownership_state}]" if ownership_state else ""
    return f"  - {service['id']}{badge}: {summary}"


def _print_status_services(status_payload: dict[str, Any]) -> None:
    print("services:")
    for service in status_payload["services"]:
        print(_format_service_line(service))


def _print_status_parity(status_payload: dict[str, Any]) -> None:
    # WG-006: surface deferred surfaces + blocked services so the
    # observational surface tells the operator what the overlay explicitly
    # chose to defer and which covered services are currently blocked
    # (backend.md Rule 3a).
    deferred = (status_payload.get("parity_ledger") or {}).get("deferred_surfaces") or []
    if deferred:
        print("deferred surfaces (parity ledger):")
        for surface in deferred:
            print(f"  - {surface}")
    blocked_services = status_payload.get("blocked_services") or []
    if blocked_services:
        print("blocked services:")
        for sid in blocked_services:
            print(f"  - {sid}")


def _print_status_ingress(status_payload: dict[str, Any]) -> None:
    ingress_routes = (status_payload.get("ingress") or {}).get("routes") or []
    if not ingress_routes:
        return
    print("ingress:")
    for route in ingress_routes:
        print(
            f"  - {route['id']}: {route['listener']} {route.get('path') or route.get('path_prefix') or ''} "
            f"-> {route['service_id']} @ {route['request_url']}"
        )


def _print_status_pressure(status_payload: dict[str, Any]) -> None:
    advisory = status_payload.get("pressure_advisory") or {}
    if not advisory:
        return
    disk = advisory.get("local_disk") or {}
    target = advisory.get("target_worker") or {}
    rch = advisory.get("rch") or {}
    sbh = advisory.get("sbh") or {}
    print("pressure/offload:")
    print(
        f"  - local: {disk.get('free_gib')}GiB free "
        f"({disk.get('free_percent')}%, {disk.get('pressure_level')})"
    )
    print(f"  - target: {target.get('id')} state={target.get('state') or 'unknown'}")
    print(f"  - rch: {rch.get('state')} worker={rch.get('worker_state')}")
    print(f"  - sbh: {sbh.get('state')} daemon={sbh.get('daemon_state')}")
    for warning in advisory.get("warnings") or []:
        print(f"  ! {warning}")


def _print_status_logs(status_payload: dict[str, Any]) -> None:
    print("logs:")
    for log_item in status_payload["logs"]:
        if log_item["present"]:
            print(
                f"  - {log_item['id']}: {log_item['files']} files, "
                f"{human_bytes(int(log_item['bytes']))}"
            )
        else:
            print(f"  - {log_item['id']}: missing")


def _print_status_checks(status_payload: dict[str, Any]) -> None:
    print("checks:")
    for check in status_payload["checks"]:
        state = "ok" if check.get("ok") else "missing"
        print(f"  - {check['id']}: {state}")


def print_status_text(status_payload: dict[str, Any]) -> None:
    _print_status_header(status_payload)
    _print_status_repos(status_payload)
    _print_status_artifacts(status_payload)
    _print_status_env_files(status_payload)
    _print_status_skills(status_payload)
    _print_status_tasks(status_payload)
    _print_status_services(status_payload)
    _print_status_parity(status_payload)
    _print_status_ingress(status_payload)
    _print_status_pressure(status_payload)
    _print_status_logs(status_payload)
    _print_status_checks(status_payload)


def print_local_runtime_error_text(err: dict[str, Any]) -> None:
    """Render a LOCAL_RUNTIME_* error envelope to stderr.

    Mirrors the structured fields that JSON consumers already see
    (``error.type``, ``error.detail``, ``error.requested_mode``,
    ``error.blocked_services``, ``error.next_action``) so text-mode
    operators are not left with a single opaque ``ERROR: <detail>`` line.
    Optional fields are omitted entirely when empty so the output stays
    quiet for envelopes that do not carry them (e.g. mode pre-validation
    has no blocked services).
    """
    error_block = (err or {}).get("error") or {}
    code = str(error_block.get("type") or "").strip()
    detail = str(error_block.get("detail") or error_block.get("message") or "").strip()
    headline = f"ERROR [{code}]: {detail}" if code else f"ERROR: {detail}"
    print(headline, file=sys.stderr)

    requested_mode = str(error_block.get("requested_mode") or "").strip()
    if requested_mode:
        print(f"requested mode: {requested_mode}", file=sys.stderr)

    blocked = error_block.get("blocked_services") or []
    if blocked:
        print("blocked services:", file=sys.stderr)
        for sid in blocked:
            print(f"  - {sid}", file=sys.stderr)

    next_action = str(error_block.get("next_action") or "").strip()
    if next_action:
        print(f"next action: {next_action}", file=sys.stderr)

    next_actions = error_block.get("next_actions") or []
    if next_actions:
        print("next actions:", file=sys.stderr)
        for action in next_actions:
            print(f"  - {action}", file=sys.stderr)


def print_service_actions_text(payload: dict[str, Any]) -> None:
    warnings = payload.get("warnings") or []
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    sync_actions = payload.get("sync_actions") or []
    if sync_actions:
        print("sync:")
        for action in sync_actions:
            print(f"  - {action}")

    task_results = payload.get("tasks") or payload.get("bootstrap_tasks") or []
    if task_results:
        print("tasks:")
        for item in task_results:
            summary = item.get("result") or item.get("status") or "unknown"
            if item.get("target"):
                summary = f"{summary} ({item['target']})"
            print(f"  - {item['id']}: {summary}")

    print("services:")
    for item in payload.get("services") or []:
        summary = item.get("result", "unknown")
        if item.get("pid") is not None:
            summary = f"{summary} (pid {item['pid']})"
        if item.get("reason"):
            summary = f"{summary} ({item['reason']})"
        endpoint = item.get("endpoint") or {}
        endpoint_url = item.get("endpoint_url") or endpoint.get("access_url")
        exposure = item.get("exposure") or endpoint.get("exposure")
        if endpoint_url:
            summary = f"{summary} -> {endpoint_url}"
        if exposure:
            summary = f"{summary} [{exposure}]"
        print(f"  - {item['id']}: {summary}")


def print_endpoint_summary(summary: dict[str, list[dict[str, Any]]]) -> None:
    apps = summary.get("apps") or []
    apis = summary.get("apis") or []
    if not apps and not apis:
        return

    def _row(item: dict[str, Any], id_width: int, url_width: int) -> str:
        alias = item.get("alias")
        status = item.get("status")
        display_url = item.get("access_url") or (item.get("endpoint") or {}).get("access_url") or item["url"]
        suffix_parts: list[str] = []
        if status:
            suffix_parts.append(status)
        exposure = item.get("exposure") or (item.get("endpoint") or {}).get("exposure")
        if exposure:
            suffix_parts.append(str(exposure))
        if alias:
            suffix_parts.append(f"({alias})")
        suffix = "   " + "   ".join(suffix_parts) if suffix_parts else ""
        return f"  {item['id']:<{id_width}}  {display_url:<{url_width}}{suffix}".rstrip()

    if apps:
        id_w = max(len(a["id"]) for a in apps)
        url_w = max(len(a.get("access_url") or (a.get("endpoint") or {}).get("access_url") or a["url"]) for a in apps)
        print()
        print("apps:")
        for app in apps:
            print(_row(app, id_w, url_w))
    if apis:
        id_w = max(len(a["id"]) for a in apis)
        url_w = max(len(a.get("access_url") or (a.get("endpoint") or {}).get("access_url") or a["url"]) for a in apis)
        print()
        print("APIs:")
        for api in apis:
            print(_row(api, id_w, url_w))


def print_service_logs_text(payload: dict[str, Any]) -> None:
    for item in payload.get("services") or []:
        # WG-006: render a dedicated deferred badge for non-covered surfaces
        # so operators are not left staring at a missing log file.
        if item.get("deferred"):
            ownership = item.get("ownership_state") or "deferred"
            print(f"[{item['id']}] (parity ledger: {ownership})")
            next_action = item.get("next_action") or ""
            if next_action:
                print(f"  next action: {next_action}")
            continue
        print(f"[{item['id']}] {item['log_file']}")
        if not item.get("present"):
            print("(missing)")
        elif item.get("lines"):
            for line in item["lines"]:
                print(line)
        else:
            print("(empty)")


def print_client_blueprints_text(blueprints: list[dict[str, Any]]) -> None:
    if not blueprints:
        print("No client blueprints found.")
        return

    for blueprint in blueprints:
        description = blueprint.get("description") or "No description."
        print(f"{blueprint['id']}: {description}")
        variables = blueprint.get("variables") or []
        if not variables:
            print("  vars: none")
            continue
        rendered_variables: list[str] = []
        for variable in variables:
            summary = variable["name"]
            if variable.get("required"):
                summary += " (required)"
            elif variable.get("default") is not None:
                summary += f" (default: {variable['default']})"
            rendered_variables.append(summary)
        print(f"  vars: {', '.join(rendered_variables)}")


def _client_diff_header_lines(payload: dict[str, Any]) -> list[str]:
    current = payload.get("current") or {}
    candidate = payload.get("candidate") or {}
    summary = payload.get("summary") or {}
    publish_metadata = payload.get("publish_metadata") or {}
    lines = [
        f"client: {payload['client_id']}",
        f"target_dir: {payload['target_dir']}",
        f"current_dir: {payload['current_dir']}",
        f"changed: {payload['changed']}",
        f"candidate_payload_tree_sha256: {candidate.get('payload_tree_sha256')}",
        f"current_payload_tree_sha256: {current.get('payload_tree_sha256') or '(none)'}",
        "files: "
        f"+{summary.get('added', 0)} "
        f"~{summary.get('changed', 0)} "
        f"-{summary.get('removed', 0)} "
        f"={summary.get('unchanged', 0)}",
        "publish_metadata: "
        + ("match" if publish_metadata.get("matches_candidate") else "drift"),
    ]
    if publish_metadata.get("changed_fields"):
        lines.append("publish_metadata_fields: " + ", ".join(publish_metadata["changed_fields"]))
    return lines


def _runtime_change_parts(change: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if change.get("added"):
        parts.append("added " + ", ".join(change["added"]))
    if change.get("removed"):
        parts.append("removed " + ", ".join(change["removed"]))
    if change.get("changed"):
        parts.append("changed " + ", ".join(change["changed"]))
    return parts


def _client_diff_runtime_lines(payload: dict[str, Any]) -> list[str]:
    runtime_changes = payload.get("runtime_changes") or {}
    sections = runtime_changes.get("sections") or {}
    changed_sections = runtime_changes.get("changed_sections") or []
    if not changed_sections:
        return []
    lines = ["runtime_changes:"]
    for section in changed_sections:
        change = sections.get(section) or {}
        lines.append(f"  - {section}: " + "; ".join(_runtime_change_parts(change)))
    return lines


def _client_diff_file_group_lines(
    title: str,
    values: list[Any],
    value_key: str | None = None,
) -> list[str]:
    if not values:
        return []
    lines = [f"{title}:"]
    for item in values:
        value = item.get(value_key) if value_key and isinstance(item, dict) else item
        lines.append(f"  - {value}")
    return lines


def client_diff_text_lines(payload: dict[str, Any]) -> list[str]:
    files = payload["files"]
    lines = _client_diff_header_lines(payload)
    lines.extend(_client_diff_runtime_lines(payload))
    lines.extend(_client_diff_file_group_lines("added_files", files["added"]))
    lines.extend(_client_diff_file_group_lines("removed_files", files["removed"]))
    lines.extend(_client_diff_file_group_lines("changed_files", files["changed"], "path"))
    return lines


def print_client_diff_text(payload: dict[str, Any]) -> None:
    for line in client_diff_text_lines(payload):
        print(line)
