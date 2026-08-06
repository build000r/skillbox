"""Machine-readable status and doctor checks for seamless paste."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import clipboard_adapter
from . import clipboard_bootstrap as bootstrap
from . import clipboard_fallback
from . import clipboard_route
from . import clipboard_session
from . import clipboard_transfer


SCHEMA_VERSION = 1
LEGACY_CLIPBOARD_PORTS = {6000, 18339}
MAX_MANIFEST_BYTES = 256 * 1024


class ListenerProbeError(RuntimeError):
    """Clipboard listener containment could not be observed safely."""


def _check(
    check_id: str, status: str, message: str, repair: str | None = None
) -> dict[str, Any]:
    return {"id": check_id, "status": status, "message": message, "repair": repair}


def diagnose_facts(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    listeners = facts.get("listeners", [])
    exposed = [
        item
        for item in listeners
        if item.get("address") not in {"127.0.0.1", "::1", "unix"}
    ]
    listener_probe_error = bool(facts.get("listener_probe_error"))
    if listener_probe_error:
        checks.append(
            _check(
                "network.containment",
                "fail",
                "clipboard listener containment could not be observed",
                "install or repair lsof, then rerun clipboard-paste doctor",
            )
        )
    else:
        checks.append(
            _check(
                "network.containment",
                "fail" if exposed else "pass",
                "clipboard-related listener is exposed beyond loopback"
                if exposed
                else "no non-loopback clipboard listener",
                "stop the exposed process; seamless paste requires no listener"
                if exposed
                else None,
            )
        )
    unsafe_modes = [
        item
        for item in facts.get("private_paths", facts.get("private_files", []))
        if (
            item.get("kind", "file") not in {"file", "directory"}
            or int(item.get("mode", 0)) & 0o077
        )
    ]
    checks.append(
        _check(
            "files.private_modes",
            "fail" if unsafe_modes else "pass",
            "private state has an unsafe mode, type, or symlink"
            if unsafe_modes
            else "private state modes are restricted",
            "remove state symlinks; chmod directories 700 and files 600; rerun clipboard-paste doctor"
            if unsafe_modes
            else None,
        )
    )
    stale_route = bool(facts.get("route_stale"))
    checks.append(
        _check(
            "route.freshness",
            "fail" if stale_route else "pass",
            "focused route is stale"
            if stale_route
            else "route freshness is acceptable",
            "relaunch with d2 or d3" if stale_route else None,
        )
    )
    duplicates = int(facts.get("duplicate_tmux_features", 0))
    checks.append(
        _check(
            "tmux.duplicate_features",
            "fail" if duplicates else "pass",
            f"{duplicates} duplicate tmux feature lines"
            if duplicates
            else "tmux feature lines are unique",
            "clipboard-bootstrap install to migrate the managed fragment"
            if duplicates
            else None,
        )
    )
    manifest_error = bool(facts.get("manifest_error"))
    checks.append(
        _check(
            "lifecycle.manifest",
            "fail" if manifest_error else "pass",
            "reversible install manifest is invalid or unsafe"
            if manifest_error
            else "reversible install manifest is readable",
            "restore the private lifecycle manifest from a trusted backup before install, rollback, or uninstall"
            if manifest_error
            else None,
        )
    )
    bridge = facts.get("legacy_bridge", {})
    for key, message, repair in (
        (
            "token_stale",
            "legacy bridge token is stale",
            "uninstall the legacy bridge or rotate its token",
        ),
        (
            "display_missing",
            "running Codex lacks required legacy DISPLAY",
            "restart Codex after repairing the bridge",
        ),
        (
            "sidecar_dead",
            "legacy mosh clipboard sidecar is dead",
            "restart or uninstall the legacy sidecar",
        ),
        (
            "dependency_outdated",
            "legacy clipboard dependency is outdated",
            "review and update the pinned dependency",
        ),
    ):
        if bridge.get(key):
            checks.append(_check(f"legacy_bridge.{key}", "fail", message, repair))
        else:
            checks.append(
                _check(
                    f"legacy_bridge.{key}",
                    "not_applicable",
                    "no adopted legacy bridge is required",
                )
            )
    return checks


def classify_state(
    *,
    unsupported: bool = False,
    offline: bool = False,
    ambiguous: bool = False,
    stale: bool = False,
    installed: bool = False,
    install_issues: bool = False,
    failed_checks: bool = False,
) -> str:
    if unsupported:
        return "unsupported"
    if install_issues:
        return "configured" if installed else "degraded"
    if ambiguous:
        return "ambiguous"
    if stale:
        return "stale"
    if offline:
        return "offline"
    if failed_checks:
        return "degraded"
    return "ready"


def _read_installed_version(path: Path) -> tuple[str | None, str | None]:
    """Read the private lifecycle manifest without following or racing its path."""
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise ValueError("unsafe manifest inode")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise ValueError("manifest changed during open")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, min(64 * 1024, MAX_MANIFEST_BYTES + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_MANIFEST_BYTES:
                    raise ValueError("manifest is oversized")
        finally:
            os.close(fd)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest root is not an object")
        version = payload.get("installed_version")
        if version is not None and not isinstance(version, str):
            raise ValueError("manifest version has the wrong type")
        return version, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "reversible install manifest is invalid or unsafe"


def _redact_home(value: str | None, home: Path) -> str | None:
    if value is None:
        return None
    return value.replace(str(home), "~")


def collect_clipboard_listeners(
    *,
    runner: Any = subprocess.run,
) -> list[dict[str, Any]]:
    """Return redacted listener facts for known clipboard bridge surfaces."""
    try:
        result = runner(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerProbeError("clipboard listener probe unavailable") from exc
    if result.returncode not in {0, 1}:
        raise ListenerProbeError("clipboard listener probe failed")
    listeners: list[dict[str, Any]] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        process = fields[0]
        endpoint = next(
            (field for field in reversed(fields) if ":" in field and field != "(LISTEN)"),
            "",
        )
        if not endpoint:
            continue
        address, separator, port_text = endpoint.rpartition(":")
        if not separator:
            continue
        try:
            port = int(port_text)
        except ValueError:
            continue
        process_key = process.lower()
        if port not in LEGACY_CLIPBOARD_PORTS and not any(
            token in process_key for token in ("clipboard", "cc-clip", "xvfb")
        ):
            continue
        address = address.removeprefix("TCP").strip()
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]
        if address in {"*", "0.0.0.0", "::"}:
            normalized = "0.0.0.0" if address != "::" else "::"
        else:
            normalized = address
        listeners.append(
            {"process": process, "address": normalized, "port": port}
        )
    return listeners


def probe_ssh_target(
    target: str | None,
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Run one explicit, non-interactive reachability probe with redacted output."""
    if not target:
        return {
            "attempted": True,
            "reachable": None,
            "error": "profile_has_no_ssh_target",
        }
    try:
        target = clipboard_transfer.validate_target(target)
    except clipboard_transfer.TransferError:
        return {"attempted": True, "reachable": False, "error": "invalid_target"}
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        target,
        "true",
    ]
    try:
        result = runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=4,
        )
    except subprocess.TimeoutExpired:
        return {"attempted": True, "reachable": False, "error": "timeout"}
    except OSError:
        return {
            "attempted": True,
            "reachable": False,
            "error": "ssh_unavailable",
        }
    if result.returncode == 0:
        return {"attempted": True, "reachable": True, "error": None}
    stderr = result.stderr.lower()
    error = (
        "authentication_failed"
        if "permission denied" in stderr or "authentication" in stderr
        else "offline_or_unreachable"
    )
    return {"attempted": True, "reachable": False, "error": error}


def _private_file_facts(home: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for root in (
        home / ".local" / "state" / "skillbox" / "paste-routes",
        bootstrap.lifecycle_state_dir(home),
        home / ".cache" / "skillbox" / "smart-paste" / "receipts",
    ):
        if not root.exists() and not root.is_symlink():
            continue
        pending = [root]
        while pending:
            path = pending.pop()
            try:
                info = path.lstat()
            except OSError:
                facts.append({"path": str(path), "mode": 0o777, "kind": "unreadable"})
                continue
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            facts.append(
                {"path": str(path), "mode": stat.S_IMODE(info.st_mode), "kind": kind}
            )
            if kind == "directory":
                try:
                    pending.extend(path.iterdir())
                except OSError:
                    facts.append(
                        {"path": str(path), "mode": 0o777, "kind": "unreadable"}
                    )
    return facts


def _tmux_duplicate_count(home: Path) -> int:
    fragment = bootstrap.tmux_fragment_path(home)
    if not fragment.is_file():
        return 0
    lines = [
        line.strip()
        for line in fragment.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return len(lines) - len(set(lines))


def _codex_version() -> str | None:
    try:
        result = subprocess.run(
            ["codex", "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _current_tmux_client(
    pane: str,
    *,
    runner: Any = subprocess.run,
) -> str:
    result = runner(
        ["tmux", "display-message", "-p", "-t", pane, "#{client_name}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    client = result.stdout.strip() if result.returncode == 0 else ""
    if not client:
        raise clipboard_session.SessionError(
            result.stderr.strip() or "focused tmux client is unavailable"
        )
    return client


def _missing_route_error(message: str) -> bool:
    lowered = message.lower()
    return "no registered paste route" in lowered or "invalid option" in lowered


def inspect_status(
    *,
    home: Path | None = None,
    root: Path | None = None,
    profile: str = "d3",
    route_path: Path | None = None,
    now: float | None = None,
    listener_runner: Any = subprocess.run,
    tmux_runner: Any = subprocess.run,
    environment: Mapping[str, str] | None = None,
    probe_target_live: bool = False,
    target_runner: Any = subprocess.run,
    codex_version_fn: Any = _codex_version,
) -> dict[str, Any]:
    home = home or Path.home()
    resolved_root = root
    if resolved_root is None:
        try:
            resolved_root = bootstrap.repo_root()
        except FileNotFoundError:
            resolved_root = None
    now = time.time() if now is None else now
    environment = os.environ if environment is None else environment
    install_issues = bootstrap.verify_local_install(home)
    installed_manifest = bootstrap.lifecycle_state_dir(home) / "manifest.json"
    installed_version, manifest_error = _read_installed_version(installed_manifest)
    if installed_manifest.exists() or installed_manifest.is_symlink():
        try:
            bootstrap.validate_local_lifecycle(
                home,
                root=resolved_root or Path("/"),
                action="install",
            )
        except bootstrap.LifecycleError:
            manifest_error = "reversible install manifest is invalid or unsafe"
    if manifest_error:
        install_issues.append(manifest_error)
    route_record: dict[str, Any] | None = None
    route_error: str | None = None
    route_stale = False
    if route_path is not None:
        try:
            route_record = clipboard_session.load_record(route_path, now=now)
        except (OSError, clipboard_session.SessionError) as exc:
            route_error = str(exc)
            route_stale = True
    elif environment.get("TMUX") and environment.get("TMUX_PANE"):
        pane = str(environment["TMUX_PANE"])
        try:
            client = _current_tmux_client(pane, runner=tmux_runner)
            route_record, route_path = clipboard_session.resolve_tmux(
                pane=pane,
                client=client,
                now=now,
                runner=tmux_runner,
            )
        except (OSError, clipboard_session.SessionError) as exc:
            route_error = str(exc)
            route_stale = not _missing_route_error(route_error)
    host_data = (
        bootstrap.load_hosts(resolved_root)
        if resolved_root is not None
        else clipboard_route.load_host_config(bootstrap.installed_hosts_path(home))
    )
    profile_record = clipboard_route.resolve_profile(profile, data=host_data)
    target_probe = (
        probe_ssh_target(profile_record.get("ssh_target"), runner=target_runner)
        if probe_target_live
        else {"attempted": False, "reachable": None, "error": None}
    )
    fallback = clipboard_fallback.explain_fallback(
        {
            "registered": route_record is not None,
            "generation_matches": route_record is not None,
            "pane_matches": route_record is not None,
            "client_matches": route_record is not None,
            "stale": route_stale,
        }
    )
    listener_probe_error: str | None = None
    try:
        listeners = collect_clipboard_listeners(runner=listener_runner)
    except ListenerProbeError as exc:
        listeners = []
        listener_probe_error = str(exc)
    facts = {
        "listeners": listeners,
        "listener_probe_error": listener_probe_error,
        "private_paths": _private_file_facts(home),
        "route_stale": route_stale,
        "duplicate_tmux_features": _tmux_duplicate_count(home),
        "manifest_error": manifest_error,
        "legacy_bridge": {},
    }
    checks = diagnose_facts(facts)
    codex_version = codex_version_fn()
    adapter = clipboard_adapter.choose_adapter(
        agent="codex",
        agent_version=codex_version,
        input_kind="remote_image_path",
        route_ready=route_record is not None,
    )
    checks.append(
        _check(
            "agent.codex_attachment",
            "pass" if adapter.state == "ready" else "fail",
            adapter.reason,
            None
            if adapter.state == "ready"
            else "update Codex to the proven version, then rerun clipboard-paste doctor",
        )
    )
    failing = [check for check in checks if check["status"] == "fail"]
    unsupported = not profile_record["capabilities"]["inbound"]["smart_path_paste"]
    state = classify_state(
        unsupported=unsupported,
        ambiguous=route_record is None and not route_stale,
        stale=route_stale,
        offline=target_probe["attempted"] and target_probe["reachable"] is False,
        installed=(bootstrap.lifecycle_state_dir(home) / "manifest.json").is_file(),
        install_issues=bool(install_issues),
        failed_checks=bool(failing),
    )
    receipts = home / ".cache" / "skillbox" / "smart-paste" / "receipts"
    latest = (
        max(
            receipts.glob("*.json"), key=lambda path: path.stat().st_mtime, default=None
        )
        if receipts.is_dir()
        else None
    )
    version = (
        bootstrap.bundle_revision(resolved_root, home)
        if resolved_root is not None
        else installed_version
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "version": version,
        "profile": profile_record["profile"],
        "target": "configured" if profile_record.get("ssh_target") else None,
        "target_probe": target_probe,
        "install": {
            "ready": not install_issues,
            "issues": [_redact_home(issue, home) for issue in install_issues],
        },
        "route": {
            "ready": route_record is not None,
            "route_id": route_record.get("route_id") if route_record else None,
            "generation": route_record.get("generation") if route_record else None,
            "remote_session": "registered"
            if route_record and route_record.get("remote_session")
            else None,
            "error": _redact_home(route_error, home),
        },
        "capabilities": profile_record["capabilities"],
        "fallback": fallback,
        "agent": {
            "codex_version": codex_version,
            "adapter": adapter.public_dict(),
        },
        "checks": checks,
        "last_receipt": latest.name if latest else None,
        "generated_at": now,
        "redaction": "clipboard bytes, text, tokens, credentials, absolute home paths, targets, and session labels omitted",
    }


def render_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"seamless paste: {report['state']} ({report['profile']} -> {report['target']})"
    ]
    for issue in report["install"]["issues"]:
        lines.append(f"  install: {issue}")
    for check in report["checks"]:
        if check["status"] == "fail":
            lines.append(f"  FAIL {check['id']}: {check['message']}")
            if check["repair"]:
                lines.append(f"    repair: {check['repair']}")
    if report["route"]["error"]:
        lines.append(f"  route: {report['route']['error']}")
    if not report["route"]["ready"]:
        lines.append("  route: launch d2 or d3 to register the exact focused pane")
    return "\n".join(lines)


def dump(report: Mapping[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"))
