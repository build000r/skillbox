"""Skillbox clipboard bootstrap: install, routing, and verification."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import clipboard_route as cr

BUNDLE_FILES = ("clipcopy", "clippaste", "tmux.conf", "xterm-ghostty.tic")
TERMINFO_BUNDLE_NAME = "xterm-ghostty.tic"
SEAMLESS_PASTE_VERSION = "2.0.0"
BUNDLE_EXECUTABLES = (
    "clipcopy",
    "clippaste",
    "pbcopy",
    "clipimg-put",
    "clipboard-route",
    "clipboard-snapshot",
    "clipboard-artifact-receive",
    "clipboard-session",
    "clipboard-smart-paste",
    "clipboard-route-exec",
)
LOCAL_PYTHON_MODULES = (
    "__init__.py",
    "clipboard_route.py",
    "clipboard_snapshot.py",
    "clipboard_transfer.py",
    "clipboard_session.py",
    "clipboard_smart_paste.py",
    "clipboard_route_exec.py",
    "clipboard_adapter.py",
    "clipboard_fallback.py",
    "clipboard_status.py",
    "clipboard_metrics.py",
    "clipboard_bootstrap.py",
)
LOCAL_SCRIPT_EXECUTABLES = (
    "clipboard-route",
    "clipboard-snapshot",
    "clipboard-artifact-receive",
    "clipboard-session",
    "clipboard-smart-paste",
    "clipboard-route-exec",
    "clipboard-paste",
    "clipboard-metrics",
)
LOCAL_LAUNCHERS = ("d2", "d3")
REMOTE_PYTHON_MODULES = ("__init__.py", "clipboard_transfer.py")
TMUX_MARKER = "clipboard.tmux.conf"
CONFIG_SUBDIR = ".config/skillbox"
TMUX_FRAGMENT_NAME = "clipboard.tmux.conf"
GHOSTTY_FRAGMENT_NAME = "clipboard.ghostty.conf"
SOURCE_LINE = (
    "if-shell '[ -r \"$HOME/.config/skillbox/clipboard.tmux.conf\" ]' "
    "'source-file \"$HOME/.config/skillbox/clipboard.tmux.conf\"'"
)
TMUX_COMMENT = "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux."
GHOSTTY_COMMENT = (
    "# Skillbox seamless paste: scoped Ghostty Cmd+V probe plus native paste."
)
STATE_SUBDIR = ".local/state/skillbox/clipboard-bootstrap"
SUPPORTED_OPERATOR_PLATFORMS = {"Darwin", "Linux"}


class UnsupportedOperatorPlatform(RuntimeError):
    """The local installer has no focus-safe contract for this platform."""


def operator_platform_supported(system: str | None = None) -> bool:
    return (system or platform.system()) in SUPPORTED_OPERATOR_PLATFORMS


def unsupported_operator_message(system: str | None = None) -> str:
    name = system or platform.system()
    return (
        f"operator platform {name!r} is substrate-only or unsupported; "
        "no local or remote changes were made"
    )


def repo_root(start: Path | None = None) -> Path:
    candidate = start or Path(__file__).resolve().parent.parent.parent
    if (candidate / ".env-manager" / "manage.py").is_file():
        return candidate
    raise FileNotFoundError(f"Skillbox root not found from {candidate}")


def bundle_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "scripts" / "clipboard"


def hosts_path(root: Path | None = None) -> Path:
    return bundle_dir(root) / "hosts.json"


def load_hosts(root: Path | None = None) -> dict[str, Any]:
    return cr.load_host_config(hosts_path(root))


def normalize_tilde(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return f"{home.rstrip('/')}/{path[2:]}"
    return path


def resolve_profile(
    profile: str,
    *,
    target: str | None = None,
    hosts: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    data = hosts or load_hosts(root)
    return cr.resolve_profile(profile, data=data, target=target)


def resolve_clipimg_alias(
    alias: str, hosts: dict[str, Any] | None = None, root: Path | None = None
) -> str:
    data = hosts or load_hosts(root)
    return cr.resolve_alias(alias, data)


@dataclass
class ConferenceRoute:
    transport: str
    ssh_target: str
    clipboard_capable: bool
    reason: str
    used_fallback: bool = False


def default_shell_probe(command: str) -> bool:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def static_conference_route(
    hosts: dict[str, Any] | None = None,
    root: Path | None = None,
) -> ConferenceRoute:
    """Deterministic conference target for plan/help/dry-run surfaces."""
    data = hosts or load_hosts(root)
    direct = data["conference_routing"]["direct_target"]
    return ConferenceRoute(
        transport="ssh",
        ssh_target=direct,
        clipboard_capable=True,
        reason="static_direct_wsl_preferred",
    )


def select_conference_route(
    probe_reachable: Callable[[str], bool] | None = None,
    probe_mosh: Callable[[str], bool] | None = None,
    hosts: dict[str, Any] | None = None,
    root: Path | None = None,
    *,
    live_probe: bool = True,
) -> ConferenceRoute:
    data = hosts or load_hosts(root)
    routing = data["conference_routing"]
    direct = routing["direct_target"]
    fallback = routing["fallback_target"]
    if not live_probe:
        return static_conference_route(hosts=data, root=root)
    reachable = probe_reachable or default_shell_probe
    mosh_ok = probe_mosh or default_shell_probe

    if reachable(routing["probe_reachability"]):
        transport = "mosh" if mosh_ok(routing["probe_mosh"]) else "ssh"
        return ConferenceRoute(
            transport=transport,
            ssh_target=direct,
            clipboard_capable=True,
            reason="direct_wsl_reachable",
        )
    return ConferenceRoute(
        transport="wsl",
        ssh_target=fallback,
        clipboard_capable=False,
        reason="direct_wsl_unreachable_use_fallback",
        used_fallback=True,
    )


def tmux_fragment_path(home: Path) -> Path:
    return home / CONFIG_SUBDIR / TMUX_FRAGMENT_NAME


def tmux_conf_path(home: Path) -> Path:
    return home / ".tmux.conf"


def installed_hosts_path(home: Path) -> Path:
    return home / CONFIG_SUBDIR / "clipboard-hosts.json"


def installed_python_dir(home: Path) -> Path:
    return home / ".local" / "share" / "skillbox" / "python" / "lib"


def ghostty_fragment_path(home: Path) -> Path:
    return home / CONFIG_SUBDIR / GHOSTTY_FRAGMENT_NAME


def ghostty_conf_path(home: Path) -> Path:
    # Ghostty 1.3 loads the XDG path. Older macOS Application Support config
    # remains in the lifecycle baseline for exact legacy restoration.
    return home / ".config" / "ghostty" / "config.ghostty"


def legacy_ghostty_conf_path(home: Path) -> Path:
    return home / "Library" / "Application Support" / "com.mitchellh.ghostty" / "config"


def ghostty_source_line(home: Path) -> str:
    return f"config-file = ?{ghostty_fragment_path(home)}"


def ensure_ghostty_source_line(config: Path, home: Path) -> None:
    line = ghostty_source_line(home)
    content = config.read_text(encoding="utf-8") if config.exists() else ""
    if line in content:
        return
    if content and not content.endswith("\n"):
        content += "\n"
    content += f"\n{GHOSTTY_COMMENT}\n{line}\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(content, encoding="utf-8")


def read_tmux_fragment(root: Path | None = None) -> str:
    return (bundle_dir(root) / "tmux.conf").read_text(encoding="utf-8")


def expected_tmux_fragment_markers() -> tuple[str, ...]:
    return (
        "set -g set-clipboard on",
        "set -ag terminal-features",
        "xterm-ghostty:clipboard:RGB",
        'set -g copy-command "$HOME/.local/bin/clipcopy"',
        "copy-pipe-and-cancel",
    )


def _is_malformed_skillbox_tmux_line(line: str) -> bool:
    if line.startswith("# Skillbox clipboard integration: OSC52"):
        return True
    if line in {"if-shell [", "-r", "]", "'", "] source-file"}:
        return True
    if "'source-file" in line:
        return True
    return "clipboard.tmux.conf" in line and "source-file" not in line


def repair_malformed_tmux_block(content: str) -> str:
    """Remove a broken Skillbox clipboard block while preserving other settings."""
    lines = content.splitlines()
    out: list[str] = []
    repair_skip = False
    for line in lines:
        if line.startswith("# Skillbox clipboard integration: OSC52"):
            repair_skip = True
            continue
        if repair_skip:
            if _is_malformed_skillbox_tmux_line(line):
                continue
            repair_skip = False
            out.append(line)
            continue
        out.append(line)
    repaired = "\n".join(out)
    if content.endswith("\n"):
        repaired += "\n"
    return repaired


def ensure_tmux_source_line(tmux_conf: Path) -> None:
    content = tmux_conf.read_text(encoding="utf-8") if tmux_conf.exists() else ""
    if SOURCE_LINE in content:
        return
    if TMUX_MARKER in content:
        content = repair_malformed_tmux_block(content)
    if SOURCE_LINE not in content:
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{TMUX_COMMENT}\n{SOURCE_LINE}\n"
    tmux_conf.write_text(content, encoding="utf-8")


def clipcopy_client_tty_markers() -> tuple[str, ...]:
    return (
        "tmux list-clients -F '#{client_name}'",
        'printf \'\\033]52;c;%s\\a\' "$b64" >"$client"',
        "tmux load-buffer -w",
    )


def _install_file(src: Path, dest: Path, mode: int, *, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(mode)


def lifecycle_state_dir(home: Path) -> Path:
    return home / STATE_SUBDIR


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_managed_specs(root: Path, home: Path) -> list[tuple[Path, Path, int]]:
    src = bundle_dir(root)
    scripts = root / "scripts"
    bin_dir = home / ".local" / "bin"
    specs: list[tuple[Path, Path, int]] = [
        (src / "clipcopy", bin_dir / "clipcopy", 0o755),
        (src / "clippaste", bin_dir / "clippaste", 0o755),
        (src / "tmux.conf", tmux_fragment_path(home), 0o644),
        (src / "ghostty.conf", ghostty_fragment_path(home), 0o644),
        (hosts_path(root), installed_hosts_path(home), 0o600),
    ]
    shim = "clipimg-put" if platform.system() == "Darwin" else "pbcopy"
    specs.append((src / shim, bin_dir / shim, 0o755))
    specs.extend(
        (scripts / name, bin_dir / name, 0o755) for name in LOCAL_SCRIPT_EXECUTABLES
    )
    specs.extend(
        (scripts / "launchers" / name, bin_dir / name, 0o755)
        for name in LOCAL_LAUNCHERS
    )
    specs.extend(
        (scripts / "lib" / name, installed_python_dir(home) / name, 0o644)
        for name in LOCAL_PYTHON_MODULES
    )
    return specs


def bundle_revision(root: Path, home: Path) -> str:
    digest = hashlib.sha256()
    for source, destination, mode in sorted(
        local_managed_specs(root, home), key=lambda item: str(item[1])
    ):
        digest.update(str(source.relative_to(root)).encode())
        digest.update(str(mode).encode())
        digest.update(source.read_bytes())
    return f"{SEAMLESS_PASTE_VERSION}+{digest.hexdigest()[:12]}"


def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _snapshot_paths(paths: list[Path], destination: Path) -> list[dict[str, Any]]:
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)
    records: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        record: dict[str, Any] = {"path": str(path), "existed": path.is_file()}
        if path.is_file():
            info = path.stat()
            backup = destination / f"{index:04d}.bin"
            shutil.copy2(path, backup)
            backup.chmod(0o600)
            record.update(
                {
                    "backup": str(backup),
                    "mode": info.st_mode & 0o777,
                    "sha256": _sha256(path),
                }
            )
        records.append(record)
    return records


def _managed_paths(root: Path, home: Path) -> list[Path]:
    paths = [
        destination for _source, destination, _mode in local_managed_specs(root, home)
    ]
    paths.extend(
        (tmux_conf_path(home), ghostty_conf_path(home), legacy_ghostty_conf_path(home))
    )
    return paths


def _ensure_baseline(root: Path, home: Path) -> dict[str, Any]:
    state = lifecycle_state_dir(home)
    manifest_path = state / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        known = {record["path"] for record in manifest["baseline"]}
        missing = [
            path for path in _managed_paths(root, home) if str(path) not in known
        ]
        if missing:
            migration = state / "baseline" / "migrations" / str(time.time_ns())
            manifest["baseline"].extend(_snapshot_paths(missing, migration))
            _write_json_private(manifest_path, manifest)
        return manifest
    records = _snapshot_paths(_managed_paths(root, home), state / "baseline")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "owner": "skillbox-seamless-paste",
        "installed_version": None,
        "installed_at": None,
        "baseline": records,
        "installed_hashes": {},
    }
    _write_json_private(manifest_path, manifest)
    return manifest


def _installation_differs(root: Path, home: Path) -> bool:
    for source, destination, _mode in local_managed_specs(root, home):
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            return True
    if SOURCE_LINE not in (
        tmux_conf_path(home).read_text(encoding="utf-8")
        if tmux_conf_path(home).is_file()
        else ""
    ):
        return True
    ghostty_line = ghostty_source_line(home)
    return ghostty_line not in (
        ghostty_conf_path(home).read_text(encoding="utf-8")
        if ghostty_conf_path(home).is_file()
        else ""
    )


def _capture_rollback(root: Path, home: Path) -> None:
    state = lifecycle_state_dir(home)
    rollback = state / "rollback"
    if rollback.exists():
        shutil.rmtree(rollback)
    records = _snapshot_paths(_managed_paths(root, home), rollback / "files")
    manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    _write_json_private(
        rollback / "snapshot.json",
        {
            "schema_version": 1,
            "version": manifest.get("installed_version"),
            "captured_at": time.time(),
            "records": records,
        },
    )


def _record_installed_state(root: Path, home: Path) -> None:
    state = lifecycle_state_dir(home)
    manifest_path = state / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["installed_version"] = bundle_revision(root, home)
    manifest["installed_at"] = time.time()
    manifest["installed_hashes"] = {
        str(path): _sha256(path)
        for path in _managed_paths(root, home)
        if path.is_file()
    }
    _write_json_private(manifest_path, manifest)


def _restore_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        path = Path(record["path"])
        if record["existed"]:
            backup = Path(record["backup"])
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
            path.chmod(int(record["mode"]))
        else:
            path.unlink(missing_ok=True)


def _remove_owned_block(content: str, *, comment: str, line: str) -> str:
    lines = content.splitlines(keepends=True)
    for index in range(len(lines) - 1):
        if lines[index].rstrip("\r\n") != comment:
            continue
        if lines[index + 1].rstrip("\r\n") != line:
            continue
        start = index
        if start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start : index + 2]
        break
    return "".join(lines)


def _restore_config_record(
    record: dict[str, Any],
    *,
    comment: str,
    line: str,
    installed_hash: str | None = None,
) -> None:
    path = Path(record["path"])
    if path.is_file() and installed_hash and _sha256(path) == installed_hash:
        if record["existed"]:
            shutil.copy2(Path(record["backup"]), path)
            path.chmod(int(record["mode"]))
        else:
            path.unlink()
        return
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    stripped = _remove_owned_block(current, comment=comment, line=line)
    baseline = (
        Path(record["backup"]).read_text(encoding="utf-8") if record["existed"] else ""
    )
    if stripped == baseline and record["existed"]:
        shutil.copy2(Path(record["backup"]), path)
        path.chmod(int(record["mode"]))
    elif stripped:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stripped, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _remove_legacy_ghostty_block(home: Path) -> None:
    legacy = legacy_ghostty_conf_path(home)
    if not legacy.is_file() or legacy == ghostty_conf_path(home):
        return
    content = legacy.read_text(encoding="utf-8")
    migrated = _remove_owned_block(
        content, comment=GHOSTTY_COMMENT, line=ghostty_source_line(home)
    )
    if migrated != content:
        if migrated:
            legacy.write_text(migrated, encoding="utf-8")
        else:
            legacy.unlink()


def _remove_owned_python_cache(home: Path) -> None:
    """Remove only bytecode generated from this bundle's managed modules."""
    python_dir = installed_python_dir(home)
    cache = python_dir / "__pycache__"
    if not cache.is_dir() or cache.is_symlink():
        return
    stems = {Path(name).stem for name in LOCAL_PYTHON_MODULES}
    for path in cache.iterdir():
        if not path.is_file() or path.is_symlink() or path.suffix != ".pyc":
            continue
        if path.name.split(".", 1)[0] in stems:
            path.unlink()
    with contextlib.suppress(OSError):
        cache.rmdir()


def rollback_local(home: Path | None = None) -> dict[str, Any]:
    home_dir = home or Path.home()
    snapshot = lifecycle_state_dir(home_dir) / "rollback" / "snapshot.json"
    if not snapshot.is_file():
        raise FileNotFoundError(
            "no prior seamless-paste version is available for rollback"
        )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    _restore_records(payload["records"])
    return {"ok": True, "restored_version": payload.get("version")}


def uninstall_local(home: Path | None = None) -> dict[str, Any]:
    home_dir = home or Path.home()
    state = lifecycle_state_dir(home_dir)
    manifest_path = state / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": True, "changed": False, "message": "not installed"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config_records: dict[str, dict[str, Any]] = {
        record["path"]: record for record in manifest["baseline"]
    }
    config_paths = {
        str(tmux_conf_path(home_dir)),
        str(ghostty_conf_path(home_dir)),
        str(legacy_ghostty_conf_path(home_dir)),
    }
    ordinary = [
        record for record in manifest["baseline"] if record["path"] not in config_paths
    ]
    _restore_records(ordinary)
    _restore_config_record(
        config_records[str(tmux_conf_path(home_dir))],
        comment=TMUX_COMMENT,
        line=SOURCE_LINE,
        installed_hash=manifest["installed_hashes"].get(str(tmux_conf_path(home_dir))),
    )
    _restore_config_record(
        config_records[str(ghostty_conf_path(home_dir))],
        comment=GHOSTTY_COMMENT,
        line=ghostty_source_line(home_dir),
        installed_hash=manifest["installed_hashes"].get(
            str(ghostty_conf_path(home_dir))
        ),
    )
    _restore_config_record(
        config_records[str(legacy_ghostty_conf_path(home_dir))],
        comment=GHOSTTY_COMMENT,
        line=ghostty_source_line(home_dir),
        installed_hash=manifest["installed_hashes"].get(
            str(legacy_ghostty_conf_path(home_dir))
        ),
    )
    _remove_owned_python_cache(home_dir)
    shutil.rmtree(state)
    return {"ok": True, "changed": True, "restored": len(manifest["baseline"])}


@dataclass
class InstallPlan:
    profile: str
    scope: str
    steps: list[str] = field(default_factory=list)
    ssh_target: str | None = None
    dry_run: bool = False


def plan_local_install(
    home: Path | None = None, *, dry_run: bool = False, root: Path | None = None
) -> InstallPlan:
    home_dir = home or Path.home()
    plan = InstallPlan(profile="local", scope="local", dry_run=dry_run)
    if not operator_platform_supported():
        plan.steps.append(unsupported_operator_message())
        return plan
    bin_dir = home_dir / ".local" / "bin"
    fragment = tmux_fragment_path(home_dir)
    tmux_conf = tmux_conf_path(home_dir)

    plan.steps.extend(
        [
            f"install helpers to {bin_dir}",
            f"install tmux fragment to {fragment}",
            f"install Ghostty Cmd+V fragment to {ghostty_fragment_path(home_dir)}",
            f"install typed route registry to {installed_hosts_path(home_dir)}",
            f"install smart-paste, secure-transfer, session, route, and launcher helpers to {bin_dir}",
            f"append source line to {tmux_conf} if missing",
            f"append scoped source line to {ghostty_conf_path(home_dir)} if missing",
            f"record reversible ownership under {lifecycle_state_dir(home_dir)}",
            (
                "leave running tmux servers untouched (SKILLBOX_SKIP_TMUX_RELOAD=1)"
                if os.environ.get("SKILLBOX_SKIP_TMUX_RELOAD") == "1"
                else "reload tmux config when tmux is available"
            ),
        ]
    )
    if platform.system() != "Darwin":
        plan.steps.append("install Linux pbcopy shim")
    else:
        plan.steps.append("install clipimg-put (Darwin)")
    return plan


def install_local(
    home: Path | None = None, *, dry_run: bool = False, root: Path | None = None
) -> InstallPlan:
    resolved_root = root or repo_root()
    home_dir = home or Path.home()
    plan = plan_local_install(home_dir, dry_run=dry_run, root=resolved_root)
    if not operator_platform_supported():
        if dry_run:
            return plan
        raise UnsupportedOperatorPlatform(unsupported_operator_message())
    if not dry_run:
        already_managed = (lifecycle_state_dir(home_dir) / "manifest.json").is_file()
        _ensure_baseline(resolved_root, home_dir)
        if already_managed and _installation_differs(resolved_root, home_dir):
            _capture_rollback(resolved_root, home_dir)

    for source, destination, mode in local_managed_specs(resolved_root, home_dir):
        _install_file(source, destination, mode, dry_run=dry_run)

    tmux_conf = tmux_conf_path(home_dir)
    if not dry_run:
        tmux_conf.parent.mkdir(parents=True, exist_ok=True)
        if not tmux_conf.exists():
            tmux_conf.write_text("", encoding="utf-8")
        ensure_tmux_source_line(tmux_conf)
        ensure_ghostty_source_line(ghostty_conf_path(home_dir), home_dir)
        _remove_legacy_ghostty_block(home_dir)
        _record_installed_state(resolved_root, home_dir)
        if shutil.which("tmux") and os.environ.get("SKILLBOX_SKIP_TMUX_RELOAD") != "1":
            subprocess.run(
                ["tmux", "source-file", str(tmux_conf)],
                capture_output=True,
                check=False,
            )
    return plan


def plan_remote_bootstrap(
    profile: str,
    *,
    target: str | None = None,
    dry_run: bool = False,
    root: Path | None = None,
    ssh_target_override: str | None = None,
    live_probe: bool | None = None,
) -> InstallPlan:
    resolved = resolve_profile(profile, target=target, root=root)
    probe = live_probe if live_probe is not None else not dry_run
    if profile == "conference1" and not target:
        route = select_conference_route(root=root, live_probe=probe)
        if route.used_fallback:
            resolved = resolve_profile("conference1-fallback", root=root)
        ssh_target = route.ssh_target
    else:
        ssh_target = ssh_target_override or resolved.get("ssh_target") or target
    plan = InstallPlan(
        profile=resolved["profile"],
        scope=resolved.get("scope", "remote"),
        ssh_target=ssh_target,
        dry_run=dry_run,
    )
    if not ssh_target:
        raise ValueError(f"profile {profile!r} has no ssh_target")

    plan.steps.extend(
        [
            f"ssh {ssh_target}: install helpers to ~/.local/bin",
            f"ssh {ssh_target}: install tmux fragment to ~/.config/skillbox/clipboard.tmux.conf",
            f"ssh {ssh_target}: append idempotent source line to ~/.tmux.conf",
            f"ssh {ssh_target}: install xterm-ghostty terminfo from bundled {TERMINFO_BUNDLE_NAME}",
            f"ssh {ssh_target}: verify infocmp -x xterm-ghostty (warn if unavailable)",
            f"ssh {ssh_target}: verify clipcopy executable and tmux fragment present",
        ]
    )
    if not cr.capability(resolved, "osc52_copy"):
        plan.steps.append(
            "warning: profile is OSC52-hostile; clipboard not expected to work"
        )
    return plan


def remote_install_script() -> str:
    """Shell script run on remote host via stdin (or SKILLBOX_CLIPBOARD_BUNDLE_B64)."""
    return f"""#!/usr/bin/env bash
set -euo pipefail
bundle_dir="${{TMPDIR:-/tmp}}/skillbox-clipboard.$$"
trap 'rm -rf "$bundle_dir"' EXIT
mkdir -p "$bundle_dir"
if [ -n "${{SKILLBOX_CLIPBOARD_BUNDLE_B64:-}}" ]; then
  printf '%s' "$SKILLBOX_CLIPBOARD_BUNDLE_B64" | base64 -d | tar -xzf - -C "$bundle_dir"
else
  tar -xzf - -C "$bundle_dir"
fi
bin_dir="$HOME/.local/bin"
config_dir="$HOME/.config/skillbox"
python_dir="$HOME/.local/share/skillbox/python/lib"
state_dir="$HOME/.local/state/skillbox/clipboard-bootstrap"
mkdir -p "$bin_dir" "$config_dir" "$python_dir" "$state_dir"
snapshot_set() {{
  snapshot_dir="$1"
  rm -rf "$snapshot_dir"
  mkdir -p "$snapshot_dir/files"
  : >"$snapshot_dir/records.tsv"
  snapshot_one() {{
    snapshot_id="$1"
    snapshot_path="$2"
    if [ -f "$snapshot_path" ]; then
      cp -p "$snapshot_path" "$snapshot_dir/files/$snapshot_id"
      snapshot_mode=$(stat -c '%a' "$snapshot_path" 2>/dev/null || stat -f '%Lp' "$snapshot_path")
      printf '%s\t1\t%s\t%s\n' "$snapshot_id" "$snapshot_mode" "$snapshot_path" >>"$snapshot_dir/records.tsv"
    else
      printf '%s\t0\t-\t%s\n' "$snapshot_id" "$snapshot_path" >>"$snapshot_dir/records.tsv"
    fi
  }}
  snapshot_one clipcopy "$bin_dir/clipcopy"
  snapshot_one clippaste "$bin_dir/clippaste"
  snapshot_one pbcopy "$bin_dir/pbcopy"
  snapshot_one receiver "$bin_dir/clipboard-artifact-receive"
  snapshot_one pyinit "$python_dir/__init__.py"
  snapshot_one transfer "$python_dir/clipboard_transfer.py"
  snapshot_one tmux_fragment "$config_dir/clipboard.tmux.conf"
  snapshot_one tmux_conf "$HOME/.tmux.conf"
}}
incoming_version=$(cat "$bundle_dir/VERSION")
if [ ! -f "$state_dir/baseline/records.tsv" ]; then
  snapshot_set "$state_dir/baseline"
fi
if [ -f "$state_dir/version" ] && [ "$(cat "$state_dir/version")" != "$incoming_version" ]; then
  snapshot_set "$state_dir/rollback"
  cp "$state_dir/version" "$state_dir/rollback-version"
fi
install -m 0755 "$bundle_dir/clipcopy" "$bin_dir/clipcopy"
install -m 0755 "$bundle_dir/clippaste" "$bin_dir/clippaste"
install -m 0755 "$bundle_dir/pbcopy" "$bin_dir/pbcopy"
install -m 0755 "$bundle_dir/clipboard-artifact-receive" "$bin_dir/clipboard-artifact-receive"
install -m 0644 "$bundle_dir/lib/__init__.py" "$python_dir/__init__.py"
install -m 0644 "$bundle_dir/lib/clipboard_transfer.py" "$python_dir/clipboard_transfer.py"
install -m 0644 "$bundle_dir/tmux.conf" "$config_dir/clipboard.tmux.conf"
tmux_conf="$HOME/.tmux.conf"
touch "$tmux_conf"
valid_source='if-shell '"'"'[ -r "$HOME/.config/skillbox/clipboard.tmux.conf" ]'"'"' '"'"'source-file "$HOME/.config/skillbox/clipboard.tmux.conf"'"'"''
if ! grep -Fq "$valid_source" "$tmux_conf"; then
  if grep -Fq '{TMUX_MARKER}' "$tmux_conf"; then
    repair_skip=0
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "# Skillbox clipboard integration: OSC52"*)
          repair_skip=1
          continue
          ;;
      esac
      if [ "$repair_skip" = "1" ]; then
        case "$line" in
          "if-shell ["|"-r"|"]"|"'") continue ;;
          "] source-file") continue ;;
          *"'source-file"*) continue ;;
          *clipboard.tmux.conf*) continue ;;
          *)
            repair_skip=0
            printf '%s\\n' "$line"
            continue
            ;;
        esac
        continue
      fi
      printf '%s\\n' "$line"
    done <"$tmux_conf" >"$tmux_conf.tmp" && mv "$tmux_conf.tmp" "$tmux_conf"
  fi
  cat >>"$tmux_conf" <<'SKILLBOX_CLIPBOARD_TMUX'

# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux.
if-shell '[ -r "$HOME/.config/skillbox/clipboard.tmux.conf" ]' 'source-file "$HOME/.config/skillbox/clipboard.tmux.conf"'
SKILLBOX_CLIPBOARD_TMUX
fi
terminfo_ok=0
if command -v infocmp >/dev/null 2>&1 && infocmp -x xterm-ghostty >/dev/null 2>&1; then
  terminfo_ok=1
fi
if [ "$terminfo_ok" = "0" ] && command -v tic >/dev/null 2>&1 && [ -f "$bundle_dir/{TERMINFO_BUNDLE_NAME}" ]; then
  tic -x "$bundle_dir/{TERMINFO_BUNDLE_NAME}" 2>/dev/null || true
  if command -v infocmp >/dev/null 2>&1 && infocmp -x xterm-ghostty >/dev/null 2>&1; then
    terminfo_ok=1
  fi
fi
if [ "$terminfo_ok" = "0" ] && command -v tic >/dev/null 2>&1 && command -v infocmp >/dev/null 2>&1; then
  if infocmp -x xterm-ghostty >/dev/null 2>&1; then
    infocmp -x xterm-ghostty | tic -x - 2>/dev/null || true
    terminfo_ok=1
  fi
fi
if command -v tmux >/dev/null 2>&1; then
  tmux source-file "$tmux_conf" >/dev/null 2>&1 || true
fi
test -x "$bin_dir/clipcopy"
test -x "$bin_dir/clipboard-artifact-receive"
test -f "$config_dir/clipboard.tmux.conf"
printf '%s\n' "$incoming_version" >"$state_dir/version"
chmod 0600 "$state_dir/version"
if [ "$terminfo_ok" = "0" ]; then
  echo "warning: xterm-ghostty terminfo unavailable after bundled install" >&2
fi
echo "skillbox clipboard bootstrap: ok on $(hostname)"
"""


def remote_restore_script(*, rollback: bool = False) -> str:
    """Restore the remote baseline or the previous installed version."""
    snapshot = "rollback" if rollback else "baseline"
    final_cleanup = "" if rollback else 'rm -rf "$state_dir"'
    version_restore = (
        'if [ -f "$state_dir/rollback-version" ]; then cp "$state_dir/rollback-version" "$state_dir/version"; fi'
        if rollback
        else ""
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail
state_dir="$HOME/.local/state/skillbox/clipboard-bootstrap"
snapshot_dir="$state_dir/{snapshot}"
records="$snapshot_dir/records.tsv"
if [ ! -f "$records" ]; then
  echo "skillbox clipboard bootstrap: no remote {snapshot} snapshot" >&2
  exit 1
fi
while IFS=$'\t' read -r snapshot_id existed mode path; do
  if [ "$existed" = "1" ]; then
    mkdir -p "$(dirname "$path")"
    cp -p "$snapshot_dir/files/$snapshot_id" "$path"
    chmod "$mode" "$path"
  else
    rm -f "$path"
  fi
done <"$records"
{version_restore}
{final_cleanup}
echo "skillbox clipboard bootstrap: remote {snapshot} restore ok on $(hostname)"
"""


def make_bundle_tar(root: Path | None = None) -> bytes:
    import io
    import tarfile

    resolved_root = root or repo_root()
    src = bundle_dir(resolved_root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        bundle_sources = [(src / name, name) for name in (*BUNDLE_FILES, "pbcopy")]
        bundle_sources.append(
            (
                resolved_root / "scripts" / "clipboard-artifact-receive",
                "clipboard-artifact-receive",
            )
        )
        bundle_sources.extend(
            (resolved_root / "scripts" / "lib" / name, f"lib/{name}")
            for name in REMOTE_PYTHON_MODULES
        )
        version_data = bundle_revision(resolved_root, Path.home()).encode() + b"\n"
        version_info = tarfile.TarInfo(name="VERSION")
        version_info.size = len(version_data)
        version_info.mode = 0o644
        archive.addfile(version_info, io.BytesIO(version_data))
        for path, archive_name in bundle_sources:
            info = tarfile.TarInfo(name=archive_name)
            data = path.read_bytes()
            info.size = len(data)
            if archive_name in {
                "tmux.conf",
                TERMINFO_BUNDLE_NAME,
            } or archive_name.startswith("lib/"):
                info.mode = 0o644
            else:
                info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def run_remote_install(
    home: Path,
    *,
    root: Path | None = None,
    bundle: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute remote_install_script against a local HOME (fixture/e2e path)."""
    import base64

    resolved_root = root or repo_root()
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env["HOME"] = str(home)
    run_env["SKILLBOX_CLIPBOARD_BUNDLE_B64"] = base64.b64encode(
        bundle if bundle is not None else make_bundle_tar(resolved_root)
    ).decode()
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", "-s"],
        input=remote_install_script(),
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def apply_remote_via_ssh(
    ssh_target: str,
    *,
    root: Path | None = None,
    transport: str = "ssh",
    wsl_distro: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run remote install over SSH; runner is injectable for tests."""
    import base64

    resolved_root = root or repo_root()
    bundle_b64 = base64.b64encode(make_bundle_tar(resolved_root)).decode()
    run = runner or subprocess.run
    distro = wsl_distro or os.environ.get("SKILLBOX_WSL_DISTRO", "Ubuntu")
    if transport == "wsl":
        remote_cmd = (
            f"wsl -d {distro} --cd ~ --exec env "
            f"SKILLBOX_CLIPBOARD_BUNDLE_B64={bundle_b64} bash -s"
        )
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            ssh_target,
            remote_cmd,
        ]
    else:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            ssh_target,
            f"SKILLBOX_CLIPBOARD_BUNDLE_B64={bundle_b64}",
            "bash",
            "-s",
        ]
    return run(
        argv,
        input=remote_install_script().encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=120,
    )


def apply_remote_restore_via_ssh(
    ssh_target: str,
    *,
    rollback: bool = False,
    transport: str = "ssh",
    wsl_distro: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    run = runner or subprocess.run
    script = remote_restore_script(rollback=rollback)
    distro = wsl_distro or os.environ.get("SKILLBOX_WSL_DISTRO", "Ubuntu")
    if transport == "wsl":
        argv = ["ssh", ssh_target, f"wsl -d {distro} --cd ~ --exec bash -s"]
    else:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            ssh_target,
            "bash",
            "-s",
        ]
    return run(
        argv,
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=120,
    )


def verify_local_install(home: Path) -> list[str]:
    issues: list[str] = []
    helper_names = (
        "clipcopy",
        "clippaste",
        *LOCAL_SCRIPT_EXECUTABLES,
        *LOCAL_LAUNCHERS,
    )
    for name in helper_names:
        path = home / ".local" / "bin" / name
        if not path.is_file():
            issues.append(f"missing {path}")
        elif not os.access(path, os.X_OK):
            issues.append(f"not executable: {path}")
    route_config = installed_hosts_path(home)
    if not route_config.is_file():
        issues.append(f"missing route registry {route_config}")
    else:
        try:
            cr.load_host_config(route_config)
        except (OSError, cr.HostConfigError) as exc:
            issues.append(f"invalid route registry {route_config}: {exc}")
    for name in LOCAL_PYTHON_MODULES:
        module = installed_python_dir(home) / name
        if not module.is_file():
            issues.append(f"missing clipboard module {module}")
    fragment = tmux_fragment_path(home)
    if not fragment.is_file():
        issues.append(f"missing tmux fragment {fragment}")
    else:
        content = fragment.read_text(encoding="utf-8")
        for marker in expected_tmux_fragment_markers():
            if marker not in content:
                issues.append(f"tmux fragment missing marker: {marker}")
    tmux_conf = tmux_conf_path(home)
    if tmux_conf.is_file() and SOURCE_LINE not in tmux_conf.read_text(encoding="utf-8"):
        issues.append(f"{tmux_conf} missing valid source line for {TMUX_MARKER}")
    ghostty_fragment = ghostty_fragment_path(home)
    if not ghostty_fragment.is_file():
        issues.append(f"missing Ghostty fragment {ghostty_fragment}")
    else:
        ghostty_content = ghostty_fragment.read_text(encoding="utf-8")
        for marker in ("super+v=text:\\x1b[99~", "chain=paste_from_clipboard"):
            if marker not in ghostty_content:
                issues.append(f"Ghostty fragment missing marker: {marker}")
    ghostty_conf = ghostty_conf_path(home)
    if not ghostty_conf.is_file() or ghostty_source_line(
        home
    ) not in ghostty_conf.read_text(encoding="utf-8"):
        issues.append(f"{ghostty_conf} missing scoped seamless-paste source line")
    manifest = lifecycle_state_dir(home) / "manifest.json"
    if not manifest.is_file():
        issues.append(f"missing reversible install manifest {manifest}")
    return issues


def is_idempotent_reinstall(home: Path, *, root: Path | None = None) -> bool:
    """True when a second install would not change tracked files."""
    resolved_root = root or repo_root()
    for source, destination, mode in local_managed_specs(resolved_root, home):
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            return False
        if destination.stat().st_mode & 0o777 != mode:
            return False
    tmux_conf = tmux_conf_path(home)
    if not tmux_conf.is_file():
        return False
    ghostty_conf = ghostty_conf_path(home)
    return (
        SOURCE_LINE in tmux_conf.read_text(encoding="utf-8")
        and ghostty_conf.is_file()
        and ghostty_source_line(home) in ghostty_conf.read_text(encoding="utf-8")
    )
