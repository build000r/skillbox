"""Skillbox clipboard bootstrap: install, routing, and verification."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BUNDLE_FILES = ("clipcopy", "clippaste", "tmux.conf", "xterm-ghostty.tic")
TERMINFO_BUNDLE_NAME = "xterm-ghostty.tic"
BUNDLE_EXECUTABLES = ("clipcopy", "clippaste", "pbcopy", "clipimg-put")
TMUX_MARKER = "clipboard.tmux.conf"
CONFIG_SUBDIR = ".config/skillbox"
TMUX_FRAGMENT_NAME = "clipboard.tmux.conf"
SOURCE_LINE = (
    'if-shell \'[ -r "$HOME/.config/skillbox/clipboard.tmux.conf" ]\' '
    '\'source-file "$HOME/.config/skillbox/clipboard.tmux.conf"\''
)
TMUX_COMMENT = "# Skillbox clipboard integration: OSC52 across local tmux, SSH, mosh, and nested tmux."


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
    return json.loads(hosts_path(root).read_text(encoding="utf-8"))


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
    profiles = data["profiles"]
    key = profile.strip().lower()
    if key == "generic" or (target and not key):
        if not target:
            raise ValueError("generic profile requires --target user@host")
        return {
            "profile": "generic",
            "label": profiles["generic"]["label"],
            "ssh_target": target,
            "remote_home": None,
            "transport": "ssh",
            "scope": "remote",
            "clipboard_capable": True,
        }
    if key not in profiles:
        raise ValueError(f"unknown profile {profile!r}; supported: {', '.join(sorted(profiles))}")
    entry = dict(profiles[key])
    entry["profile"] = key
    if target:
        entry["ssh_target"] = target
    return entry


def resolve_clipimg_alias(alias: str, hosts: dict[str, Any] | None = None, root: Path | None = None) -> str:
    data = hosts or load_hosts(root)
    mapping = data.get("clipimg_aliases", {})
    return mapping.get(alias.strip().lower(), alias.strip().lower())


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
        "printf '\\033]52;c;%s\\a' \"$b64\" >\"$client\"",
        "tmux load-buffer -w",
    )


def _install_file(src: Path, dest: Path, mode: int, *, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(mode)


@dataclass
class InstallPlan:
    profile: str
    scope: str
    steps: list[str] = field(default_factory=list)
    ssh_target: str | None = None
    dry_run: bool = False


def plan_local_install(home: Path | None = None, *, dry_run: bool = False, root: Path | None = None) -> InstallPlan:
    home_dir = home or Path.home()
    plan = InstallPlan(profile="local", scope="local", dry_run=dry_run)
    bin_dir = home_dir / ".local" / "bin"
    fragment = tmux_fragment_path(home_dir)
    tmux_conf = tmux_conf_path(home_dir)

    plan.steps.extend(
        [
            f"install helpers to {bin_dir}",
            f"install tmux fragment to {fragment}",
            f"append source line to {tmux_conf} if missing",
            "reload tmux config when tmux is available",
        ]
    )
    if platform.system() != "Darwin":
        plan.steps.append("install Linux pbcopy shim")
    else:
        plan.steps.append("install clipimg-put (Darwin)")
    return plan


def install_local(home: Path | None = None, *, dry_run: bool = False, root: Path | None = None) -> InstallPlan:
    resolved_root = root or repo_root()
    home_dir = home or Path.home()
    plan = plan_local_install(home_dir, dry_run=dry_run, root=resolved_root)
    src = bundle_dir(resolved_root)
    bin_dir = home_dir / ".local" / "bin"

    for name in ("clipcopy", "clippaste"):
        _install_file(src / name, bin_dir / name, 0o755, dry_run=dry_run)

    if platform.system() == "Darwin":
        _install_file(src / "clipimg-put", bin_dir / "clipimg-put", 0o755, dry_run=dry_run)
    else:
        _install_file(src / "pbcopy", bin_dir / "pbcopy", 0o755, dry_run=dry_run)

    fragment_dest = tmux_fragment_path(home_dir)
    _install_file(src / "tmux.conf", fragment_dest, 0o644, dry_run=dry_run)

    tmux_conf = tmux_conf_path(home_dir)
    if not dry_run:
        tmux_conf.parent.mkdir(parents=True, exist_ok=True)
        if not tmux_conf.exists():
            tmux_conf.write_text("", encoding="utf-8")
        ensure_tmux_source_line(tmux_conf)
        if shutil.which("tmux"):
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
    if resolved.get("clipboard_capable") is False:
        plan.steps.append("warning: profile is OSC52-hostile; clipboard not expected to work")
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
mkdir -p "$bin_dir" "$config_dir"
install -m 0755 "$bundle_dir/clipcopy" "$bin_dir/clipcopy"
install -m 0755 "$bundle_dir/clippaste" "$bin_dir/clippaste"
install -m 0755 "$bundle_dir/pbcopy" "$bin_dir/pbcopy"
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
test -f "$config_dir/clipboard.tmux.conf"
if [ "$terminfo_ok" = "0" ]; then
  echo "warning: xterm-ghostty terminfo unavailable after bundled install" >&2
fi
echo "skillbox clipboard bootstrap: ok on $(hostname)"
"""


def make_bundle_tar(root: Path | None = None) -> bytes:
    import io
    import tarfile

    resolved_root = root or repo_root()
    src = bundle_dir(resolved_root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in (*BUNDLE_FILES, "pbcopy"):
            path = src / name
            info = tarfile.TarInfo(name=path.name)
            data = path.read_bytes()
            info.size = len(data)
            if path.name in {"tmux.conf", TERMINFO_BUNDLE_NAME}:
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


def verify_local_install(home: Path) -> list[str]:
    issues: list[str] = []
    for name in ("clipcopy", "clippaste"):
        path = home / ".local" / "bin" / name
        if not path.is_file():
            issues.append(f"missing {path}")
        elif not os.access(path, os.X_OK):
            issues.append(f"not executable: {path}")
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
    return issues


def is_idempotent_reinstall(home: Path, *, root: Path | None = None) -> bool:
    """True when a second install would not change tracked files."""
    resolved_root = root or repo_root()
    src = bundle_dir(resolved_root)
    bin_dir = home / ".local" / "bin"
    shim = "clipimg-put" if platform.system() == "Darwin" else "pbcopy"
    for name in ("clipcopy", "clippaste", shim):
        dest = bin_dir / name
        if not dest.is_file():
            return False
        if dest.read_bytes() != (src / name).read_bytes():
            return False
    fragment = tmux_fragment_path(home)
    if not fragment.is_file():
        return False
    if fragment.read_text(encoding="utf-8") != read_tmux_fragment(resolved_root):
        return False
    tmux_conf = tmux_conf_path(home)
    if not tmux_conf.is_file():
        return False
    return SOURCE_LINE in tmux_conf.read_text(encoding="utf-8")