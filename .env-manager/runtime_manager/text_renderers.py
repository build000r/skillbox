from __future__ import annotations

from .shared import *
from .runtime_ops import service_bootstrap_task_ids, service_dependency_ids, task_dependency_ids

def print_render_text(model: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in model.get("clients") or []) or "(none)"
    default_client = (model.get("selection") or {}).get("default_client") or "(none)"
    active_clients = model.get("active_clients") or []
    print(f"clients: {available_clients}")
    print(f"default client: {default_client}")
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = model.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print(f"runtime manifest: {model['manifest_file']}")
    print(f"repos: {len(model['repos'])}")
    for repo in model["repos"]:
        print(f"  - {repo['id']}: {repo.get('kind', 'repo')} @ {repo['path']}")
    print(f"artifacts: {len(model['artifacts'])}")
    for artifact in model["artifacts"]:
        print(f"  - {artifact['id']}: {artifact.get('kind', 'artifact')} @ {artifact['path']}")
    print(f"env files: {len(model['env_files'])}")
    for env_file in model["env_files"]:
        print(f"  - {env_file['id']}: {env_file.get('kind', 'env-file')} @ {env_file['path']}")
    print(f"skills: {len(model['skills'])}")
    for skillset in model["skills"]:
        kind = skillset.get('kind', 'skill-repo-set')
        location = skillset.get('skill_repos_config') or skillset.get('bundle_dir') or '(unknown)'
        print(f"  - {skillset['id']}: {kind} @ {location}")
    print(f"tasks: {len(model['tasks'])}")
    for task in model["tasks"]:
        dependency_summary = ""
        dependency_ids = task_dependency_ids(task)
        if dependency_ids:
            dependency_summary = f" depends on {', '.join(dependency_ids)}"
        print(f"  - {task['id']}: {task.get('kind', 'task')}{dependency_summary}")
    print(f"services: {len(model['services'])}")
    for service in model["services"]:
        profiles = ", ".join(service.get("profiles") or []) or "core"
        dependency_summary = ""
        dependency_ids = service_dependency_ids(service)
        if dependency_ids:
            dependency_summary = f" depends on {', '.join(dependency_ids)}"
        bootstrap_summary = ""
        bootstrap_task_ids = service_bootstrap_task_ids(service)
        if bootstrap_task_ids:
            bootstrap_summary = f" bootstrap {', '.join(bootstrap_task_ids)}"
        print(f"  - {service['id']}: {service.get('kind', 'service')} [{profiles}]{dependency_summary}{bootstrap_summary}")
    print(f"logs: {len(model['logs'])}")
    for log_item in model["logs"]:
        print(f"  - {log_item['id']}: {log_item['path']}")
    print(f"checks: {len(model['checks'])}")
    for check in model["checks"]:
        print(f"  - {check['id']}: {check['type']}")
    bridges = model.get("bridges") or []
    if bridges:
        print(f"bridges: {len(bridges)}")
        for bridge in bridges:
            targets = ", ".join(str(t) for t in bridge.get("legacy_targets") or [])
            print(f"  - {bridge['id']}: {bridge.get('env_tier', 'local')} [{targets}]")
    ingress_routes = model.get("ingress_routes") or []
    if ingress_routes:
        print(f"ingress routes: {len(ingress_routes)}")
        for route in ingress_routes:
            listener = str(route.get("listener") or "public")
            match = str(route.get("match") or "exact")
            print(
                f"  - {route['id']}: {listener} {route.get('path', '')} "
                f"-> {route.get('service_id', '')} ({match})"
            )


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


def print_status_text(status_payload: dict[str, Any]) -> None:
    available_clients = ", ".join(client["id"] for client in status_payload.get("clients") or []) or "(none)"
    print(f"clients: {available_clients}")
    default_client = status_payload.get("default_client") or "(none)"
    print(f"default client: {default_client}")
    active_clients = status_payload.get("active_clients") or []
    if active_clients:
        print(f"active clients: {', '.join(active_clients)}")
    active_profiles = status_payload.get("active_profiles") or []
    if active_profiles:
        print(f"active profiles: {', '.join(active_profiles)}")
    print("repos:")
    for repo in status_payload["repos"]:
        summary = "present" if repo["present"] else "missing"
        if repo.get("git"):
            summary = (
                f"{summary}, git {repo.get('branch', '(detached)')}, "
                f"{repo.get('dirty', 0)} dirty, {repo.get('untracked', 0)} untracked"
            )
        print(f"  - {repo['id']}: {summary}")

    print("artifacts:")
    for artifact in status_payload["artifacts"]:
        print(f"  - {artifact['id']}: {artifact.get('state', 'unknown')} ({artifact.get('source_kind', 'manual')})")

    print("env files:")
    for env_file in status_payload["env_files"]:
        print(f"  - {env_file['id']}: {env_file['state']} ({env_file['source_kind']})")

    print("skills:")
    for skillset in status_payload["skills"]:
        total_targets = 0
        healthy_targets = 0
        for skill_entry in skillset["skills"]:
            for target in skill_entry["targets"]:
                total_targets += 1
                if target["state"] == "ok":
                    healthy_targets += 1

        lock_summary = "invalid" if skillset.get("lock_error") else ("present" if skillset["lock_present"] else "missing")
        print(
            f"  - {skillset['id']}: lock {lock_summary}, "
            f"{len(skillset['skills'])} skills, {healthy_targets}/{total_targets} targets healthy"
        )

    print("tasks:")
    for task in status_payload["tasks"]:
        summary = task.get("state", "pending")
        dependency_summary = ""
        dependency_ids = task.get("depends_on") or []
        if dependency_ids:
            dependency_summary = f", depends on {', '.join(dependency_ids)}"
        print(f"  - {task['id']}: {summary}{dependency_summary}")

    print("services:")
    for service in status_payload["services"]:
        summary = service.get("state", "declared")
        if service.get("pid") is not None:
            summary = f"{summary} (pid {service['pid']})"
        elif service.get("managed") is False and service.get("manager_reason"):
            summary = f"{summary} ({service['manager_reason']})"
        dependency_summary = ""
        dependency_ids = service.get("depends_on") or []
        if dependency_ids:
            dependency_summary = f", depends on {', '.join(dependency_ids)}"
        bootstrap_summary = ""
        bootstrap_task_ids = service.get("bootstrap_tasks") or []
        if bootstrap_task_ids:
            bootstrap_summary = f", bootstrap {', '.join(bootstrap_task_ids)}"
        # WG-006: ownership_state badge so operators can see at a glance
        # whether a service is covered, bridge-only, deferred, or external
        # per the parity ledger (shared.md:148-180, backend.md:77-90).
        ownership_state = str(service.get("ownership_state") or "").strip()
        badge = f" [{ownership_state}]" if ownership_state else ""
        print(
            f"  - {service['id']}{badge}: "
            f"{summary}{dependency_summary}{bootstrap_summary}"
        )

    # WG-006: summarise parity ledger + blocked services at the end of
    # the status block so the observational surface tells the operator
    # what the overlay explicitly chose to defer and which covered
    # services are currently blocked (backend.md Rule 3a).
    parity_block = status_payload.get("parity_ledger") or {}
    deferred = parity_block.get("deferred_surfaces") or []
    if deferred:
        print("deferred surfaces (parity ledger):")
        for surface in deferred:
            print(f"  - {surface}")
    blocked_services = status_payload.get("blocked_services") or []
    if blocked_services:
        print("blocked services:")
        for sid in blocked_services:
            print(f"  - {sid}")

    ingress = status_payload.get("ingress") or {}
    ingress_routes = ingress.get("routes") or []
    if ingress_routes:
        print("ingress:")
        for route in ingress_routes:
            print(
                f"  - {route['id']}: {route['listener']} {route['path']} "
                f"-> {route['service_id']} @ {route['request_url']}"
            )

    print("logs:")
    for log_item in status_payload["logs"]:
        if log_item["present"]:
            print(
                f"  - {log_item['id']}: {log_item['files']} files, "
                f"{human_bytes(int(log_item['bytes']))}"
            )
        else:
            print(f"  - {log_item['id']}: missing")

    print("checks:")
    for check in status_payload["checks"]:
        state = "ok" if check.get("ok") else "missing"
        print(f"  - {check['id']}: {state}")


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


def print_service_actions_text(payload: dict[str, Any]) -> None:
    sync_actions = payload.get("sync_actions") or []
    if sync_actions:
        print("sync:")
        for action in sync_actions:
            print(f"  - {action}")

    task_results = payload.get("tasks") or payload.get("bootstrap_tasks") or []
    if task_results:
        print("tasks:")
        for item in task_results:
            summary = item.get("result", "unknown")
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
        print(f"  - {item['id']}: {summary}")


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


def print_client_diff_text(payload: dict[str, Any]) -> None:
    current = payload.get("current") or {}
    candidate = payload.get("candidate") or {}
    summary = payload.get("summary") or {}
    publish_metadata = payload.get("publish_metadata") or {}
    runtime_changes = payload.get("runtime_changes") or {}
    sections = runtime_changes.get("sections") or {}

    print(f"client: {payload['client_id']}")
    print(f"target_dir: {payload['target_dir']}")
    print(f"current_dir: {payload['current_dir']}")
    print(f"changed: {payload['changed']}")
    print(f"candidate_payload_tree_sha256: {candidate.get('payload_tree_sha256')}")
    print(f"current_payload_tree_sha256: {current.get('payload_tree_sha256') or '(none)'}")
    print(
        "files: "
        f"+{summary.get('added', 0)} "
        f"~{summary.get('changed', 0)} "
        f"-{summary.get('removed', 0)} "
        f"={summary.get('unchanged', 0)}"
    )
    print(
        "publish_metadata: "
        + ("match" if publish_metadata.get("matches_candidate") else "drift")
    )
    if publish_metadata.get("changed_fields"):
        print("publish_metadata_fields: " + ", ".join(publish_metadata["changed_fields"]))

    changed_sections = runtime_changes.get("changed_sections") or []
    if changed_sections:
        print("runtime_changes:")
        for section in changed_sections:
            change = sections.get(section) or {}
            parts: list[str] = []
            if change.get("added"):
                parts.append("added " + ", ".join(change["added"]))
            if change.get("removed"):
                parts.append("removed " + ", ".join(change["removed"]))
            if change.get("changed"):
                parts.append("changed " + ", ".join(change["changed"]))
            print(f"  - {section}: " + "; ".join(parts))

    if payload["files"]["added"]:
        print("added_files:")
        for rel_path in payload["files"]["added"]:
            print(f"  - {rel_path}")
    if payload["files"]["removed"]:
        print("removed_files:")
        for rel_path in payload["files"]["removed"]:
            print(f"  - {rel_path}")
    if payload["files"]["changed"]:
        print("changed_files:")
        for item in payload["files"]["changed"]:
            print(f"  - {item['path']}")
