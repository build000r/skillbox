"""Operator console renderer for `sbp help --human`.

Renders the full sbp command surface as a grouped, colorized dashboard with an
ambient "NOW" panel (runtime / git estate / skill drift) so the operator can
see everything going on from one screen.

Design contract:
- stdout is the dashboard; diagnostics go to stderr.
- Colors only when stdout is a TTY and NO_COLOR is unset (FORCE_COLOR wins).
- Live reads default ON for a TTY, OFF when piped; --live / --no-live override.
- Every live read is best-effort with a hard timeout — the atlas always renders.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field

LIVE_TIMEOUT_SECONDS = 6.0


# ── color ────────────────────────────────────────────────────────────────────

class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def bold(self, t: str) -> str: return self._wrap("1", t)
    def dim(self, t: str) -> str: return self._wrap("2", t)
    def cyan(self, t: str) -> str: return self._wrap("36", t)
    def green(self, t: str) -> str: return self._wrap("32", t)
    def yellow(self, t: str) -> str: return self._wrap("33", t)
    def red(self, t: str) -> str: return self._wrap("31", t)
    def magenta(self, t: str) -> str: return self._wrap("35", t)
    def blue(self, t: str) -> str: return self._wrap("34", t)
    def header(self, t: str) -> str: return self._wrap("1;36", t)


def color_enabled() -> bool:
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# ── command atlas ────────────────────────────────────────────────────────────

@dataclass
class Cmd:
    invocation: str
    desc: str
    pills: tuple[str, ...] = ()


@dataclass
class Group:
    title: str
    blurb: str
    cmds: list[Cmd] = field(default_factory=list)


PILL_STYLES = {
    "json": ("json", "cyan"),
    "dry": ("--dry-run", "green"),
    "mut": ("mutates", "yellow"),
    "yes": ("--yes", "red"),
    "confirm": ("confirm", "red"),
    "remote": ("remote", "blue"),
    "deprecated": ("deprecated", "magenta"),
    "alias": ("alias", "dim"),
}


def atlas(w: str) -> list[Group]:
    return [
        Group("START HERE", "triage + machine contract", [
            Cmd(f"{w}", "compact home view for this cwd (client, profile, git, skill drift)"),
            Cmd(f"{w} help [--human] [FILTER]", "this reference; --human renders the operator console, FILTER narrows it"),
            Cmd(f"{w} doctor", "one front door for all STRUCTURAL gates (INCO/FAIL/PASS); nonzero on FAIL only", ("json",)),
            Cmd(f"{w} capabilities --json", "wrapper machine contract (commands, safety, skill verbs)", ("json",)),
            Cmd(f"{w} robot-docs guide", "agent-facing command guidance", ("json",)),
            Cmd(f"{w} robot-triage", "compact first actions for a cold agent", ("json",)),
        ]),
        Group("RUNTIME", "services via .env-manager/manage.py", [
            Cmd(f"{w} status [profile]", "runtime status: services, artifacts, warnings, next actions", ("json",)),
            Cmd(f"{w} up [profile] [svc]", "start services (default profile: local-all)", ("mut", "dry", "json")),
            Cmd(f"{w} down [profile] [svc]", "stop services", ("mut", "dry", "confirm", "json")),
            Cmd(f"{w} restart [profile] [svc]", "restart services", ("mut", "dry", "json")),
            Cmd(f"{w} logs PROFILE SVC", "show service logs", ("json",)),
            Cmd("profiles", "all|local-all · core · minimal|mini · backend · frontend|front · openclaw|claw"),
        ]),
        Group("SKILLS", "visibility, policy, lifecycle", [
            Cmd(f"{w} skills [--issues-only|--full]", "effective skills for this cwd", ("json",)),
            Cmd(f"{w} skills audit", "scan downstream repos for skill policy drift", ("json",)),
            Cmd(f"{w} recalibrate [--auto-fix]", "cwd/fleet skill drift; --auto-fix previews heal, --yes applies", ("json",)),
            Cmd(f"{w} candidates", "explore linkable skill sources for this cwd", ("json",)),
            Cmd(f"{w} skill why NAME", "explain visibility layers and exact fixes", ("json",)),
            Cmd(f"{w} skill plan NAME", "preview where NAME would be linked"),
            Cmd(f"{w} skill on|off NAME", "durably pin NAME on/off for this repo", ("mut",)),
            Cmd(f"{w} skill heal NAME", "resolve source, pin on, link, print activation packet", ("mut", "json")),
            Cmd(f"{w} skill pull NAME", "read one admitted skill now — no links, no activation", ("json",)),
            Cmd(f"{w} skill activate NAME", "link NAME and return a session packet", ("mut",)),
            Cmd(f"{w} skill add|move|remove NAME", "non-override link management across scopes", ("mut",)),
            Cmd(f"{w} skill resolve", "resolve admitted host skills without mutation"),
            Cmd(f"{w} skill togglable", "list cwd-flippable skills and exact flip commands", ("json",)),
            Cmd(f"{w} skill what-if --repo R", "simulate overlay/pin visibility with zero writes", ("json",)),
            Cmd(f"{w} skill default on NAME", "repo/cross-repo/global default policy edits", ("mut", "dry", "json")),
            Cmd(f"{w} skill sync --dry-run", "link cwd-required missing skills", ("mut", "dry")),
            Cmd(f"{w} skill prune --dry-run", "preview policy-violation unlinks", ("mut", "dry")),
            Cmd(f"{w} skill lint", "lint .skillbox/skill-overrides.yaml"),
            Cmd(f"{w} sync /path/to/skill", "legacy shorthand for global skill add", ("mut",)),
        ]),
        Group("OVERLAYS", "repo-level skill overlays", [
            Cmd(f"{w} overlay", "list active overlays"),
            Cmd(f"{w} overlay on|off|toggle NAME", "flip overlay state for this repo (--keep preserves links)", ("mut",)),
            Cmd(f"{w} m", "toggle the marketing overlay for this repo", ("mut",)),
        ]),
        Group("ESTATE & GIT", "read-only views over ~/repos", [
            Cmd(f"{w} git [--only CLASS] [--cached|--delta|--live]", "risk-sorted estate git status; never fetches or pushes (alias: gs)", ("json",)),
            Cmd(f"{w} registry doctor", "check registry/repos.yaml against local Git repos", ("json",)),
            Cmd(f"{w} repo status .", "repository dossiers through private Repo Atlas", ("json",)),
            Cmd(f"{w} beads status|init|sync", "check or initialize repo-local beads state", ("mut",)),
            Cmd(f"{w} evidence --repo P", "skill invocations per repo from Cass, joined to current policy", ("json",)),
        ]),
        Group("AGENTS & AUTOMATION", "spawn, schedule, gate", [
            Cmd(f"{w} launch DIR... --request '...'", "launch one Swimmers agent per dir (alias: bulk)", ("mut", "dry", "json")),
            Cmd(f"{w} oracle \"question\"", "ask GPT-5 Pro; answer on stdout (--model instant while iterating)", ("remote",)),
            Cmd(f"{w} cass status|search|rebuild|doctor", "remote Cass front door; rebuild is coordinated maintenance", ("remote", "json")),
            Cmd(f"{w} wiki status|search|page|log|list|raw", "read-only central wiki front door; never writes", ("remote", "json")),
            Cmd(f"{w} send-later list|doctor|new|panes", "schedule/inspect delayed or recurring tmux/NTM sends", ("json",)),
            Cmd(f"{w} safe [SECONDS]", "swarm load GO/NO-GO tick; exit 0=GO 1=NO-GO", ("json",)),
            Cmd(f"{w} cron status|apply", "declared crontab + script links from skillbox-config", ("mut",)),
        ]),
        Group("NETWORK & FLEET", "tailnet + human operator", [
            Cmd(f"{w} family health", "read-only Amp family health contract (--family ID, --criteria)", ("json",)),
            Cmd(f"{w} family dashboard", "print the family dashboard URL on port 6969"),
            Cmd(f"{w} conference1 urls|status|helper", "tailnet Serve endpoints + heavy-build lane (conf1, tailnet); expose/remove need --yes", ("remote", "yes")),
            Cmd(f"{w} hire times", "show configured human-operator availability", ("remote",)),
            Cmd(f"{w} hire book [...]", "send magic-link/signup email and create x402 hold", ("remote", "mut")),
        ]),
        Group("MCP CONFIG PARITY", "agent front door via MCP is deprecated — CLI + skills is canonical", [
            Cmd(f"{w} mcp", "read-only Claude JSON / Codex TOML MCP config audit", ("json",)),
            Cmd(f"{w} mcp sync", "render .mcp.json + .codex/config.toml from declaration", ("mut", "dry")),
        ]),
        Group("FILES & NOTES", "repo-local artifacts", [
            Cmd(f"{w} mmdx [QUERY]", "fuzzy-find and open .mmdx/.mmd from this cwd; bare lists recent"),
        ]),
    ]


# ── live "NOW" panel ─────────────────────────────────────────────────────────

def _run_json(cmd: list[str], root: str) -> dict | None:
    try:
        proc = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True,
            timeout=LIVE_TIMEOUT_SECONDS, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def gather_live(root: str, cwd: str, profile: str, client: str) -> dict[str, str | None]:
    results: dict[str, str | None] = {"runtime": None, "git": None, "skills": None}

    def runtime() -> None:
        payload = _run_json(
            ["python3", ".env-manager/manage.py", "status", "--profile", profile, "--format", "json"],
            root,
        )
        if payload is None:
            return
        services = payload.get("services") or []
        counts: dict[str, int] = {}
        down_managed: list[str] = []
        for svc in services:
            state = str(svc.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
            if svc.get("managed") and state == "down":
                down_managed.append(str(svc.get("id")))
        bits = [f"{count} {state}" for state, count in sorted(counts.items())]
        line = " · ".join(bits) if bits else "no services declared"
        if down_managed:
            line += f"  (down: {', '.join(sorted(down_managed))})"
        warnings = payload.get("warnings") or []
        if warnings:
            line += f"  · {len(warnings)} warning{'s' if len(warnings) != 1 else ''}"
        results["runtime"] = line

    def git() -> None:
        try:
            sys.path.insert(0, os.path.join(root, ".env-manager"))
            from runtime_manager.git_scan_cache import home_view_line  # noqa: PLC0415
            results["git"] = home_view_line()
        except Exception:  # noqa: BLE001 - ambient line is strictly best-effort
            results["git"] = None

    def skills() -> None:
        cmd = ["python3", ".env-manager/manage.py", "skills", "--cwd", cwd,
               "--profile", profile, "--issues-only", "--format", "json"]
        if client:
            cmd += ["--client", client]
        payload = _run_json(cmd, root)
        if payload is None:
            return
        effective = payload.get("effective") or []
        issues = [e for e in effective if str(e.get("state") or "ok") != "ok"]
        if issues:
            names = ", ".join(sorted(str(e.get("name")) for e in issues)[:4])
            more = f" +{len(issues) - 4}" if len(issues) > 4 else ""
            results["skills"] = f"{len(issues)} issue{'s' if len(issues) != 1 else ''}: {names}{more}"
        else:
            results["skills"] = f"no drift · {len(effective)} effective"

    threads = [threading.Thread(target=fn, daemon=True) for fn in (runtime, git, skills)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=LIVE_TIMEOUT_SECONDS + 1)
    return results


# ── rendering ────────────────────────────────────────────────────────────────

def term_width() -> int:
    return max(72, min(shutil.get_terminal_size((100, 24)).columns, 140))


def shorten_home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def render_pills(pills: tuple[str, ...], pal: Palette) -> str:
    if not pills:
        return ""
    rendered = []
    for pill in pills:
        label, style = PILL_STYLES.get(pill, (pill, "dim"))
        rendered.append(getattr(pal, style)(f"[{label}]"))
    return " " + " ".join(rendered)


def wrap_desc(desc: str, indent: int, width: int) -> list[str]:
    words = desc.split()
    lines: list[str] = []
    line = ""
    budget = max(20, width - indent)
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > budget and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def matches(term: str, group: Group, cmd: Cmd) -> bool:
    haystack = " ".join([group.title, group.blurb, cmd.invocation, cmd.desc, " ".join(cmd.pills)]).lower()
    return term.lower() in haystack


def render(wrapper: str, root: str, cwd: str, client: str, profile: str, mode: str,
           live: dict[str, str | None] | None, filters: list[str]) -> str:
    pal = Palette(color_enabled())
    width = term_width()
    out: list[str] = []

    # Header
    title = f" {wrapper} · skillbox operator console "
    bar = "─" * max(0, width - 2)
    out.append(pal.dim(f"╭{bar}╮"))
    ctx_right = f"client {client or 'auto'} · profile {profile} · mode {mode}"
    pad = max(1, width - 2 - len(title) - len(ctx_right) - 1)
    out.append(pal.dim("│") + pal.header(title) + " " * pad + pal.dim(ctx_right) + " " + pal.dim("│"))
    cwd_line = f" cwd {shorten_home(cwd)}"
    root_line = f"root {shorten_home(root)}"
    pad2 = max(1, width - 2 - len(cwd_line) - len(root_line) - 1)
    out.append(pal.dim("│") + pal.dim(cwd_line) + " " * pad2 + pal.dim(root_line) + " " + pal.dim("│"))
    out.append(pal.dim(f"╰{bar}╯"))

    # NOW panel
    if live is not None:
        out.append("")
        out.append(pal.header(" NOW ") + pal.dim("· live reads (skipped when piped; --no-live to silence)"))
        rows = [
            ("runtime", live.get("runtime"), f"{wrapper} status --json"),
            ("git", live.get("git"), f"{wrapper} git"),
            ("skills", live.get("skills"), f"{wrapper} skills --issues-only"),
        ]
        for label, value, deeper in rows:
            if value is None:
                shown = pal.dim(f"unavailable — {deeper}")
            else:
                shown = value
                if any(token in value for token in ("down:", "issue", "warning", "dirty")):
                    shown = pal.yellow(value)
                elif "no drift" in value or "up" in value:
                    shown = value
                shown += "  " + pal.dim(deeper)
            out.append(f"  {pal.bold(label.ljust(8))} {shown}")

    filter_terms = [t for t in filters if t.strip()]
    if filter_terms:
        out.append("")
        out.append(pal.dim(f"  filter: {' '.join(filter_terms)}"))

    # Atlas
    invocation_col = 38
    shown_any = False
    for group in atlas(wrapper):
        cmds = group.cmds
        if filter_terms:
            cmds = [c for c in cmds if all(matches(t, group, c) for t in filter_terms)]
        if not cmds:
            continue
        shown_any = True
        out.append("")
        rule = "─" * max(0, width - len(group.title) - 5)
        out.append(f" {pal.header('▌ ' + group.title)} {pal.dim(group.blurb)}")
        out.append(pal.dim(f" {rule[: max(0, width - 2)]}"))
        for cmd in cmds:
            pills = render_pills(cmd.pills, pal)
            invocation = cmd.invocation
            desc_lines = wrap_desc(cmd.desc, invocation_col + 3, width) or [""]
            desc_lines[-1] += pills
            if len(invocation) <= invocation_col:
                out.append(f"  {pal.cyan(invocation.ljust(invocation_col))} {desc_lines[0]}")
            else:
                out.append(f"  {pal.cyan(invocation)}")
                out.append(f"  {' ' * invocation_col} {desc_lines[0]}")
            for extra in desc_lines[1:]:
                out.append(f"  {' ' * invocation_col} {extra}")

    if filter_terms and not shown_any:
        out.append("")
        out.append(f"  no commands match {' '.join(filter_terms)!r} — try `{wrapper} help --human`")

    # Footer
    out.append("")
    out.append(pal.dim(" agents: ") + pal.cyan(f"{wrapper} capabilities --json") + pal.dim(" · ")
               + pal.cyan(f"{wrapper} robot-docs guide") + pal.dim(" · ")
               + pal.cyan(f"{wrapper} robot-triage"))
    out.append(pal.dim(" safety: mutating commands honor --dry-run first; `down`/`expose` want explicit intent"))
    out.append("")
    return "\n".join(out)


PLAIN_TAIL = """
Profiles:
  all | local-all, core | local-core, minimal | mini | local-minimal,
  backend | local-backend, frontend | front | local-frontend,
  openclaw | claw | local-openclaw

Examples:
  {w}
  {w} status core --json
  {w} down core api --dry-run --json
  {w} skills --issues-only
  {w} candidates --json
  {w} recalibrate --auto-fix
  {w} mcp
  {w} beads status
  {w} skill plan mcp-server-design
  {w} skill why wiki --json
  {w} skill togglable --json
  {w} skill what-if --repo . --overlay marketing --json
  {w} skill on wiki --verify
  {w} skill default on wiki --repo --dry-run
  {w} skill lint
  {w} mmdx review
  {w} launch ../api ../web --request 'Audit auth drift' --dry-run --json
  {w} hire book --date 2026-05-06 --slot AM --email person@example.com --name "Person" --send-magic-link
  {w} up backend spaps
"""


def render_plain(wrapper: str) -> str:
    """Flat, ANSI-free rendering of the SAME atlas `--human` uses.

    This is the agent-facing `sbp help`: one inventory (atlas()), two
    renderers. No colors, no groups, no pills, no live reads — just
    `invocation  description` rows plus the static Profiles/Examples tail.
    """
    width = 100
    invocation_col = 38
    out = [
        f"{wrapper} - personal skillbox runtime and skill helper",
        "",
        f"Operator console: {wrapper} help --human   (grouped, colorized, live panels, FILTER)",
        "",
        "Usage:",
    ]
    for group in atlas(wrapper):
        for cmd in group.cmds:
            desc_lines = wrap_desc(cmd.desc, invocation_col + 3, width) or [""]
            if len(cmd.invocation) <= invocation_col:
                out.append(f"  {cmd.invocation.ljust(invocation_col)} {desc_lines[0]}")
            else:
                out.append(f"  {cmd.invocation}")
                out.append(f"  {' ' * invocation_col} {desc_lines[0]}")
            for extra in desc_lines[1:]:
                out.append(f"  {' ' * invocation_col} {extra}")
    out.append(PLAIN_TAIL.replace("{w}", wrapper).rstrip())
    out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sbp-help-human", add_help=False)
    parser.add_argument("--wrapper", default="sbp")
    parser.add_argument("--root", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--client", default="")
    parser.add_argument("--profile", default="local-all")
    parser.add_argument("--mode", default="reuse")
    parser.add_argument("--live", dest="live", action="store_true", default=None)
    parser.add_argument("--no-live", dest="live", action="store_false")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("filters", nargs="*", default=[])
    args = parser.parse_args(argv)

    if args.plain:
        print(render_plain(args.wrapper))
        return 0

    live_default = sys.stdout.isatty() and os.environ.get("SBP_HELP_LIVE", "") != "0"
    want_live = live_default if args.live is None else args.live
    live = gather_live(args.root, args.cwd, args.profile, args.client) if want_live else None

    print(render(args.wrapper, args.root, args.cwd, args.client, args.profile, args.mode, live, args.filters))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
