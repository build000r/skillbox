from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORGE_NO_SIGNAL = "FORGE_NO_SIGNAL"
FORGE_INSUFFICIENT_SIGNAL = "FORGE_INSUFFICIENT_SIGNAL"
FORGE_REPO_DIRTY = "FORGE_REPO_DIRTY"
FORGE_BRANCH_EXISTS = "FORGE_BRANCH_EXISTS"
FORGE_NO_PROPOSAL = "FORGE_NO_PROPOSAL"
FORGE_REASON_REQUIRED = "FORGE_REASON_REQUIRED"
FORGE_SKILL_NOT_FOUND = "FORGE_SKILL_NOT_FOUND"
FORGE_INIT_SETTINGS_LOCKED = "FORGE_INIT_SETTINGS_LOCKED"
FORGE_INIT_SETTINGS_INVALID = "FORGE_INIT_SETTINGS_INVALID"
FORGE_INIT_CODEX_TMUX_MISSING = "FORGE_INIT_CODEX_TMUX_MISSING"
FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED = "FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED"
FORGE_INIT_CRON_UNAVAILABLE = "FORGE_INIT_CRON_UNAVAILABLE"
CODEX_TMUX_MARKER_START = "# skillbox-forge-scoring-start"
CODEX_TMUX_MARKER_END = "# skillbox-forge-scoring-end"
CRON_MARKER = "# skillbox-forge-score-session"
FORGE_METRICS = (
    "ack_rate",
    "validation_rate",
    "checkpoint_rate",
    "risk_gating_rate",
    "correction_rate",
    "completion_rate",
)
FORGE_LOW_IS_GOOD_METRICS = {"checkpoint_rate", "risk_gating_rate", "correction_rate"}
FORGE_STATUS_THRESHOLDS = (
    ("ack_rate", "<", 0.5),
    ("validation_rate", "<", 0.4),
    ("correction_rate", ">", 0.3),
    ("completion_rate", "<", 0.5),
)
FORGE_TREND_EPSILON = 0.05


class ForgeInitError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ForgeProposeError(RuntimeError):
    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class ForgeDecisionError(RuntimeError):
    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def default_scoring_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "score-session.sh"


def _home_dir(home: Path | str | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def default_review_history_path(home: Path | str | None = None) -> Path:
    return _home_dir(home) / ".claude" / "skill-review-history.jsonl"


def default_proposals_path(home: Path | str | None = None) -> Path:
    return _home_dir(home) / ".claude" / "forge-proposals.jsonl"


def default_decisions_path(home: Path | str | None = None) -> Path:
    return _home_dir(home) / ".claude" / "forge-decisions.jsonl"


def _scoring_command(scoring_script: Path, source: str, extra: list[str] | None = None) -> str:
    parts = ["bash", str(scoring_script), "--source", source]
    if extra:
        parts.extend(extra)
    return " ".join(shlex.quote(part) for part in parts)


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ForgeInitError(FORGE_INIT_SETTINGS_INVALID, f"settings file is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ForgeInitError(FORGE_INIT_SETTINGS_INVALID, f"settings file must contain a JSON object: {path}")
    return value


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.W_OK):
        raise ForgeInitError(FORGE_INIT_SETTINGS_LOCKED, f"settings file is not writable: {path}")
    if not path.exists() and path.parent.exists() and not os.access(path.parent, os.W_OK):
        raise ForgeInitError(FORGE_INIT_SETTINGS_LOCKED, f"settings directory is not writable: {path.parent}")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hook_command_matches(hook: Any, scoring_script: Path) -> bool:
    if not isinstance(hook, dict):
        return False
    command = str(hook.get("command") or "")
    return str(scoring_script) in command and "score-session.sh" in command


def _session_end_hook_present(entries: Any, scoring_script: Path) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if isinstance(hooks, list) and any(_hook_command_matches(hook, scoring_script) for hook in hooks):
            return True
        if _hook_command_matches(entry, scoring_script):
            return True
    return False


def ensure_session_end_hook(settings_path: Path, scoring_script: Path) -> dict[str, Any]:
    settings = _load_json_object(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    session_end = hooks.setdefault("SessionEnd", [])
    if not isinstance(session_end, list):
        session_end = []
        hooks["SessionEnd"] = session_end

    if _session_end_hook_present(session_end, scoring_script):
        action = "already_present"
    else:
        session_end.append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": _scoring_command(scoring_script, "claude"),
                    }
                ],
            }
        )
        action = "added"
        _write_json_object(settings_path, settings)

    if action == "already_present" and not settings_path.exists():
        _write_json_object(settings_path, settings)

    return {"path": str(settings_path), "action": action}


def _codex_tmux_block(scoring_script: Path) -> str:
    command = (
        f"bash {shlex.quote(str(scoring_script))} --source codex "
        '--session-id "$SESSION"'
    )
    return "\n".join(
        [
            f"        {CODEX_TMUX_MARKER_START}",
            f"        {command} >/dev/null 2>&1 || true",
            f"        {CODEX_TMUX_MARKER_END}",
        ]
    )


def patch_codex_tmux_wrapper(run_py: Path, scoring_script: Path) -> dict[str, Any]:
    if not run_py.exists():
        return {
            "path": str(run_py),
            "action": "missing",
            "warning": FORGE_INIT_CODEX_TMUX_MISSING,
        }

    text = run_py.read_text(encoding="utf-8")
    if CODEX_TMUX_MARKER_START in text:
        return {"path": str(run_py), "action": "already_present"}

    needle = '        echo "Result written to: $RESULT_FILE"'
    if needle not in text:
        return {
            "path": str(run_py),
            "action": "skipped",
            "warning": FORGE_INIT_CODEX_TMUX_PATCH_SKIPPED,
        }

    updated = text.replace(needle, needle + "\n\n" + _codex_tmux_block(scoring_script), 1)
    run_py.write_text(updated, encoding="utf-8")
    return {"path": str(run_py), "action": "patched"}


def ensure_cron_entry(scoring_script: Path, *, subprocess_run: Any = subprocess.run) -> dict[str, Any]:
    command = _scoring_command(scoring_script, "both", ["--since", "week"])
    entry = f"*/10 * * * * {command} >/dev/null 2>&1 {CRON_MARKER}"
    current = subprocess_run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if current.returncode not in (0, 1):
        return {"action": "skipped", "warning": FORGE_INIT_CRON_UNAVAILABLE}
    existing = current.stdout or ""
    if CRON_MARKER in existing:
        return {"action": "already_present"}
    new_crontab = existing.rstrip()
    if new_crontab:
        new_crontab += "\n"
    new_crontab += entry + "\n"
    write = subprocess_run(["crontab", "-"], input=new_crontab, capture_output=True, text=True, check=False)
    if write.returncode != 0:
        return {
            "action": "skipped",
            "warning": FORGE_INIT_CRON_UNAVAILABLE,
            "stderr": write.stderr.strip(),
        }
    return {"action": "added"}


def _record_skill_name(record: dict[str, Any]) -> str:
    return str(record.get("skill") or record.get("skill_name") or record.get("name") or "").strip()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _record_metrics(record: dict[str, Any]) -> dict[str, float]:
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        summary = record.get("summary")
        metrics = summary.get("metrics") if isinstance(summary, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {metric: round(_coerce_float(metrics.get(metric)), 3) for metric in FORGE_METRICS}


def _record_scored_at(record: dict[str, Any]) -> str:
    for key in ("timestamp", "scored_at", "generated_at", "date"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def _parse_datetime(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_review_history(
    history_path: Path | str | None = None,
    *,
    home: Path | str | None = None,
    skill: str | None = None,
) -> list[dict[str, Any]]:
    path = Path(history_path).expanduser() if history_path is not None else default_review_history_path(home)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            skill_name = _record_skill_name(record)
            if not skill_name:
                continue
            if skill and skill_name != skill:
                continue
            records.append(record)
    return records


def _average_metrics(records: list[dict[str, Any]]) -> dict[str, float]:
    if not records:
        return {metric: 0.0 for metric in FORGE_METRICS}
    totals = {metric: 0.0 for metric in FORGE_METRICS}
    for record in records:
        metrics = _record_metrics(record)
        for metric in FORGE_METRICS:
            totals[metric] += metrics[metric]
    return {metric: round(totals[metric] / len(records), 3) for metric in FORGE_METRICS}


def _health_score(metrics: dict[str, float]) -> float:
    values: list[float] = []
    for metric in FORGE_METRICS:
        value = max(0.0, min(1.0, metrics.get(metric, 0.0)))
        values.append(1.0 - value if metric in FORGE_LOW_IS_GOOD_METRICS else value)
    return sum(values) / len(values) if values else 0.0


def _trend(records: list[dict[str, Any]]) -> str:
    if len(records) < 6:
        return "stable"
    previous_score = _health_score(_average_metrics(records[-6:-3]))
    recent_score = _health_score(_average_metrics(records[-3:]))
    delta = recent_score - previous_score
    if delta <= -FORGE_TREND_EPSILON:
        return "declining"
    if delta >= FORGE_TREND_EPSILON:
        return "improving"
    return "stable"


def _threshold_label(metric: str, operator: str, threshold: float) -> str:
    return f"{metric} {operator} {threshold:g}"


def thresholds_crossed(metrics: dict[str, float]) -> list[str]:
    crossed: list[str] = []
    for metric, operator, threshold in FORGE_STATUS_THRESHOLDS:
        value = metrics.get(metric, 0.0)
        if operator == "<" and value < threshold:
            crossed.append(_threshold_label(metric, operator, threshold))
        elif operator == ">" and value > threshold:
            crossed.append(_threshold_label(metric, operator, threshold))
    return crossed


def pending_forge_skills(
    root_dir: Path | str,
    *,
    subprocess_run: Any = subprocess.run,
) -> set[str]:
    clone_root = Path(root_dir) / "workspace" / "skill-repos"
    if not clone_root.is_dir():
        return set()

    pending: set[str] = set()
    for repo_dir in clone_root.iterdir():
        if not repo_dir.is_dir():
            continue
        result = subprocess_run(
            [
                "git",
                "-C",
                str(repo_dir),
                "branch",
                "--list",
                "forge/*",
                "--format",
                "%(refname:short)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            branch = line.strip()
            if branch.startswith("forge/") and len(branch) > len("forge/"):
                pending.add(branch[len("forge/"):])
    return pending


def scoring_hook_installed(
    *,
    home: Path | str | None = None,
    scoring_script: Path | str | None = None,
) -> bool:
    settings_path = _home_dir(home) / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    score_path = Path(scoring_script).expanduser() if scoring_script else default_scoring_script()
    try:
        settings = _load_json_object(settings_path)
    except ForgeInitError:
        return False
    hooks = settings.get("hooks")
    session_end = hooks.get("SessionEnd") if isinstance(hooks, dict) else []
    return _session_end_hook_present(session_end, score_path.resolve())


def forge_status(
    *,
    skill: str | None = None,
    home: Path | str | None = None,
    history_path: Path | str | None = None,
    root_dir: Path | str | None = None,
    scoring_script: Path | str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    records = load_review_history(history_path, home=home, skill=skill)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[_record_skill_name(record)].append(record)

    pending = pending_forge_skills(root_dir or Path.cwd(), subprocess_run=subprocess_run)
    skills: list[dict[str, Any]] = []
    for skill_name in sorted(grouped):
        skill_records = sorted(grouped[skill_name], key=lambda item: _parse_datetime(_record_scored_at(item)))
        metrics = _average_metrics(skill_records[-3:])
        skills.append(
            {
                "name": skill_name,
                "sessions_scored": len(skill_records),
                "trend": _trend(skill_records),
                "metrics": metrics,
                "thresholds_crossed": thresholds_crossed(metrics),
                "proposal_pending": skill_name in pending,
                "last_scored": _record_scored_at(skill_records[-1]) if skill_records else None,
            }
        )

    payload: dict[str, Any] = {
        "skills": skills,
        "total_sessions_scored": sum(item["sessions_scored"] for item in skills),
        "scoring_hook_installed": scoring_hook_installed(home=home, scoring_script=scoring_script),
        "unscored_sessions": 0,
    }
    if not skills:
        payload["code"] = FORGE_NO_SIGNAL
        payload["message"] = "no signal yet"
    return payload


def _run_git(
    repo: Path,
    args: list[str],
    *,
    subprocess_run: Any = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return subprocess_run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_stdout(repo: Path, args: list[str], *, subprocess_run: Any = subprocess.run) -> str:
    result = _run_git(repo, args, subprocess_run=subprocess_run)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _git_root_for(path: Path, *, subprocess_run: Any = subprocess.run) -> Path | None:
    result = subprocess_run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return Path(text).resolve() if text else None


def _skill_repo_candidates(skill: str, root_dir: Path, home_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    clone_root = root_dir / "workspace" / "skill-repos"
    if clone_root.is_dir():
        candidates.extend(sorted(clone_root.glob(f"*/{skill}/SKILL.md")))

    client_builder_root = root_dir / "workspace" / "client-skill-builder-skills"
    if client_builder_root.is_dir():
        candidates.append(client_builder_root / skill / "SKILL.md")

    for agent in (".claude", ".codex"):
        candidates.append(home_dir / agent / "skills" / skill / "SKILL.md")
    return candidates


def resolve_skill_source(
    skill: str,
    *,
    root_dir: Path | str | None = None,
    home: Path | str | None = None,
    skill_dir: Path | str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    root_path = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
    home_dir = _home_dir(home)
    candidates = [Path(skill_dir).expanduser() / "SKILL.md"] if skill_dir is not None else _skill_repo_candidates(skill, root_path, home_dir)

    for skill_md in candidates:
        try:
            resolved_skill_md = skill_md.resolve()
        except FileNotFoundError:
            resolved_skill_md = skill_md
        if not resolved_skill_md.exists():
            continue
        repo_root = _git_root_for(resolved_skill_md.parent, subprocess_run=subprocess_run)
        if repo_root is None:
            continue
        return {
            "skill": skill,
            "skill_dir": str(resolved_skill_md.parent),
            "skill_md": str(resolved_skill_md),
            "repo": str(repo_root),
        }

    raise ForgeProposeError(
        FORGE_SKILL_NOT_FOUND,
        f"skill source not found in managed skill repos or agent skill roots: {skill}",
        {"skill": skill},
    )


def _branch_exists(repo: Path, branch: str, *, subprocess_run: Any = subprocess.run) -> bool:
    result = _run_git(repo, ["branch", "--list", branch, "--format", "%(refname:short)"], subprocess_run=subprocess_run)
    if result.returncode != 0:
        return False
    return any(line.strip() == branch for line in (result.stdout or "").splitlines())


def _ensure_clean_repo(repo: Path, *, subprocess_run: Any = subprocess.run) -> None:
    status = _run_git(repo, ["status", "--porcelain"], subprocess_run=subprocess_run)
    if status.returncode != 0:
        raise ForgeProposeError(FORGE_REPO_DIRTY, f"could not inspect git status for {repo}")
    dirty = [line for line in (status.stdout or "").splitlines() if line.strip()]
    if dirty:
        raise ForgeProposeError(
            FORGE_REPO_DIRTY,
            f"skill repo has uncommitted changes: {repo}",
            {"dirty_count": len(dirty), "dirty": dirty[:20]},
        )


def _proposal_id(skill: str, records: list[dict[str, Any]]) -> str:
    latest = _record_scored_at(records[-1]) if records else "none"
    token = "".join(ch if ch.isalnum() else "-" for ch in f"{skill}-{len(records)}-{latest}".lower())
    return "-".join(part for part in token.split("-") if part)[:96]


def _select_watch_metric(metrics: dict[str, float]) -> str:
    crossed = thresholds_crossed(metrics)
    if crossed:
        return crossed[0].split(" ", 1)[0]
    scored: list[tuple[float, str]] = []
    for metric in FORGE_METRICS:
        value = metrics.get(metric, 0.0)
        concern = value if metric in FORGE_LOW_IS_GOOD_METRICS else 1.0 - value
        scored.append((concern, metric))
    return max(scored)[1] if scored else "completion_rate"


def _render_proposal_section(
    *,
    skill: str,
    proposal_id: str,
    watch_metric: str,
    baseline: dict[str, float],
    thresholds: list[str],
    evidence_sessions: list[str],
) -> str:
    lines = [
        "",
        f"<!-- skillbox-forge-proposal-start:{proposal_id} -->",
        "## Skillbox Forge Proposal",
        "",
        f"- Skill: `{skill}`",
        f"- Watch metric: `{watch_metric}`",
        "- Baseline: "
        + ", ".join(f"{metric}={baseline.get(metric, 0.0):.3f}" for metric in FORGE_METRICS),
        "- Thresholds crossed: " + (", ".join(thresholds) if thresholds else "(none)"),
        "- Evidence sessions: " + (", ".join(evidence_sessions) if evidence_sessions else "(none)"),
        "",
        "### Proposed adjustment",
        "",
        "Review recent low-scoring sessions for this skill before the next release. "
        "Tighten the SKILL.md guidance around the watch metric above, then rerun the same scorer window.",
        f"<!-- skillbox-forge-proposal-end:{proposal_id} -->",
        "",
    ]
    return "\n".join(lines)


def _append_proposal_to_skill(skill_md: Path, section: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    separator = "" if text.endswith("\n") else "\n"
    skill_md.write_text(text + separator + section, encoding="utf-8")


def _append_ledger(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_latest_proposal(
    skill: str,
    *,
    home: Path | str | None = None,
    proposals_path: Path | str | None = None,
) -> dict[str, Any] | None:
    path = Path(proposals_path).expanduser() if proposals_path is not None else default_proposals_path(home)
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get("skill") or "") == skill:
                latest = record
    return latest


def _proposal_context(proposal: dict[str, Any] | None) -> dict[str, Any]:
    if not proposal:
        return {}
    evidence_sessions = [str(item) for item in proposal.get("evidence_sessions") or [] if str(item).strip()]
    context: dict[str, Any] = {
        "proposal_id": proposal.get("proposal_id"),
        "watch_metric": proposal.get("watch_metric"),
        "baseline": proposal.get("baseline"),
        "evidence_sessions": evidence_sessions,
    }
    if evidence_sessions:
        context["observation_window"] = {
            "sessions": len(evidence_sessions),
            "from": evidence_sessions[0],
            "to": evidence_sessions[-1],
        }
    else:
        sessions_scored = proposal.get("sessions_scored")
        if sessions_scored is not None:
            context["observation_window"] = {"sessions": sessions_scored}
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _local_branches(repo: Path, *, subprocess_run: Any = subprocess.run) -> set[str]:
    result = _run_git(repo, ["branch", "--format", "%(refname:short)"], subprocess_run=subprocess_run)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _current_branch(repo: Path, *, subprocess_run: Any = subprocess.run) -> str:
    return _git_stdout(repo, ["rev-parse", "--abbrev-ref", "HEAD"], subprocess_run=subprocess_run)


def _default_base_branch(repo: Path, forge_branch: str, *, subprocess_run: Any = subprocess.run) -> str:
    branches = _local_branches(repo, subprocess_run=subprocess_run)
    for candidate in ("main", "master"):
        if candidate in branches and candidate != forge_branch:
            return candidate
    current = _current_branch(repo, subprocess_run=subprocess_run)
    if current and current != forge_branch:
        return current
    for branch in sorted(branches):
        if branch != forge_branch and not branch.startswith("forge/"):
            return branch
    raise ForgeDecisionError(
        FORGE_NO_PROPOSAL,
        f"could not identify a base branch for {forge_branch}",
        {"branch": forge_branch, "branches": sorted(branches)},
    )


def _ensure_proposal_branch(repo: Path, branch: str, skill: str, *, subprocess_run: Any = subprocess.run) -> None:
    if not _branch_exists(repo, branch, subprocess_run=subprocess_run):
        raise ForgeDecisionError(
            FORGE_NO_PROPOSAL,
            f"forge branch does not exist: {branch}",
            {"skill": skill, "repo": str(repo), "branch": branch},
        )


def _checkout_branch(repo: Path, branch: str, *, subprocess_run: Any = subprocess.run) -> None:
    result = _run_git(repo, ["checkout", "-q", branch], subprocess_run=subprocess_run)
    if result.returncode != 0:
        raise ForgeDecisionError(FORGE_NO_PROPOSAL, result.stderr.strip() or f"could not checkout {branch}", {"branch": branch})


def _decision_payload(
    *,
    action: str,
    skill: str,
    repo: Path,
    branch: str,
    home: Path | str | None,
    proposals_path: Path | str | None,
) -> dict[str, Any]:
    proposal = _load_latest_proposal(skill, home=home, proposals_path=proposals_path)
    payload: dict[str, Any] = {
        "ok": True,
        "skill": skill,
        "action": action,
        "repo": str(repo),
        "branch": branch,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(_proposal_context(proposal))
    return payload


def forge_propose(
    skill: str,
    *,
    dry_run: bool = False,
    min_sessions: int = 5,
    home: Path | str | None = None,
    history_path: Path | str | None = None,
    root_dir: Path | str | None = None,
    skill_dir: Path | str | None = None,
    proposals_path: Path | str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    if min_sessions < 1:
        min_sessions = 1

    source = resolve_skill_source(
        skill,
        root_dir=root_dir,
        home=home,
        skill_dir=skill_dir,
        subprocess_run=subprocess_run,
    )
    repo = Path(source["repo"])
    skill_md = Path(source["skill_md"])
    branch = f"forge/{skill}"

    records = sorted(
        load_review_history(history_path, home=home, skill=skill),
        key=lambda item: _parse_datetime(_record_scored_at(item)),
    )
    if len(records) < min_sessions:
        raise ForgeProposeError(
            FORGE_INSUFFICIENT_SIGNAL,
            f"{skill} has {len(records)} scored sessions; {min_sessions} required",
            {"skill": skill, "sessions_scored": len(records), "min_sessions": min_sessions},
        )

    _ensure_clean_repo(repo, subprocess_run=subprocess_run)
    if _branch_exists(repo, branch, subprocess_run=subprocess_run):
        raise ForgeProposeError(
            FORGE_BRANCH_EXISTS,
            f"forge branch already exists: {branch}",
            {"skill": skill, "repo": str(repo), "branch": branch},
        )

    recent = records[-min_sessions:]
    baseline = _average_metrics(recent)
    watch_metric = _select_watch_metric(baseline)
    evidence_sessions = [_record_scored_at(record) for record in recent if _record_scored_at(record)]
    proposal_id = _proposal_id(skill, recent)
    thresholds = thresholds_crossed(baseline)
    diff_summary = "append deterministic Skillbox Forge proposal section to SKILL.md"
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "ok": True,
        "skill": skill,
        "repo": str(repo),
        "skill_md": str(skill_md),
        "branch": branch,
        "packet_family": "deterministic-local-proposal",
        "diff_summary": diff_summary,
        "evidence_sessions": evidence_sessions,
        "watch_metric": watch_metric,
        "baseline": baseline,
        "thresholds_crossed": thresholds,
        "sessions_scored": len(records),
        "min_sessions": min_sessions,
        "proposal_id": proposal_id,
        "created_at": now,
        "dry_run": dry_run,
    }

    if dry_run:
        payload["would_create_branch"] = True
        payload["would_write_ledger"] = True
        return payload

    current_branch = _git_stdout(repo, ["rev-parse", "--abbrev-ref", "HEAD"], subprocess_run=subprocess_run)
    checkout = _run_git(repo, ["checkout", "-b", branch], subprocess_run=subprocess_run)
    if checkout.returncode != 0:
        raise ForgeProposeError(FORGE_BRANCH_EXISTS, checkout.stderr.strip() or f"could not create {branch}", payload)

    section = _render_proposal_section(
        skill=skill,
        proposal_id=proposal_id,
        watch_metric=watch_metric,
        baseline=baseline,
        thresholds=thresholds,
        evidence_sessions=evidence_sessions,
    )
    _append_proposal_to_skill(skill_md, section)
    _run_git(repo, ["add", "--", str(skill_md.relative_to(repo))], subprocess_run=subprocess_run)
    commit = _run_git(
        repo,
        [
            "-c",
            "user.email=forge@example.test",
            "-c",
            "user.name=Skillbox Forge",
            "commit",
            "-m",
            f"forge: propose {skill} update",
        ],
        subprocess_run=subprocess_run,
    )
    if commit.returncode != 0:
        if current_branch:
            _run_git(repo, ["checkout", current_branch], subprocess_run=subprocess_run)
        raise ForgeProposeError(FORGE_REPO_DIRTY, commit.stderr.strip() or "forge proposal commit failed", payload)

    payload["commit"] = _git_stdout(repo, ["rev-parse", "HEAD"], subprocess_run=subprocess_run)
    ledger = Path(proposals_path).expanduser() if proposals_path is not None else default_proposals_path(home)
    _append_ledger(ledger, payload)
    payload["ledger"] = str(ledger)
    return payload


def forge_accept(
    skill: str,
    *,
    home: Path | str | None = None,
    root_dir: Path | str | None = None,
    skill_dir: Path | str | None = None,
    proposals_path: Path | str | None = None,
    decisions_path: Path | str | None = None,
    base_branch: str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    source = resolve_skill_source(
        skill,
        root_dir=root_dir,
        home=home,
        skill_dir=skill_dir,
        subprocess_run=subprocess_run,
    )
    repo = Path(source["repo"])
    branch = f"forge/{skill}"
    _ensure_proposal_branch(repo, branch, skill, subprocess_run=subprocess_run)
    try:
        _ensure_clean_repo(repo, subprocess_run=subprocess_run)
    except ForgeProposeError as exc:
        raise ForgeDecisionError(exc.code, str(exc), exc.payload) from exc

    target_branch = base_branch or _default_base_branch(repo, branch, subprocess_run=subprocess_run)
    _checkout_branch(repo, target_branch, subprocess_run=subprocess_run)
    merge = _run_git(repo, ["merge", "--ff-only", branch], subprocess_run=subprocess_run)
    if merge.returncode != 0:
        raise ForgeDecisionError(
            FORGE_NO_PROPOSAL,
            merge.stderr.strip() or f"could not fast-forward merge {branch}",
            {"skill": skill, "repo": str(repo), "branch": branch, "base_branch": target_branch},
        )
    commit = _git_stdout(repo, ["rev-parse", "HEAD"], subprocess_run=subprocess_run)
    delete = _run_git(repo, ["branch", "-d", branch], subprocess_run=subprocess_run)
    if delete.returncode != 0:
        raise ForgeDecisionError(
            FORGE_NO_PROPOSAL,
            delete.stderr.strip() or f"could not delete {branch}",
            {"skill": skill, "repo": str(repo), "branch": branch, "base_branch": target_branch},
        )

    payload = _decision_payload(
        action="accepted",
        skill=skill,
        repo=repo,
        branch=branch,
        home=home,
        proposals_path=proposals_path,
    )
    payload.update(
        {
            "commit": commit,
            "base_branch": target_branch,
            "sync_next_action": "Run `make runtime-sync` to install reviewed skill changes.",
        }
    )
    ledger = Path(decisions_path).expanduser() if decisions_path is not None else default_decisions_path(home)
    _append_ledger(ledger, payload)
    payload["ledger"] = str(ledger)
    return payload


def forge_reject(
    skill: str,
    *,
    reason: str | None,
    home: Path | str | None = None,
    root_dir: Path | str | None = None,
    skill_dir: Path | str | None = None,
    proposals_path: Path | str | None = None,
    decisions_path: Path | str | None = None,
    base_branch: str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ForgeDecisionError(FORGE_REASON_REQUIRED, "reject requires a non-empty --reason", {"skill": skill})

    source = resolve_skill_source(
        skill,
        root_dir=root_dir,
        home=home,
        skill_dir=skill_dir,
        subprocess_run=subprocess_run,
    )
    repo = Path(source["repo"])
    branch = f"forge/{skill}"
    _ensure_proposal_branch(repo, branch, skill, subprocess_run=subprocess_run)
    current_branch = _current_branch(repo, subprocess_run=subprocess_run)
    if current_branch == branch:
        target_branch = base_branch or _default_base_branch(repo, branch, subprocess_run=subprocess_run)
        _checkout_branch(repo, target_branch, subprocess_run=subprocess_run)
    else:
        target_branch = current_branch

    delete = _run_git(repo, ["branch", "-D", branch], subprocess_run=subprocess_run)
    if delete.returncode != 0:
        raise ForgeDecisionError(
            FORGE_NO_PROPOSAL,
            delete.stderr.strip() or f"could not delete {branch}",
            {"skill": skill, "repo": str(repo), "branch": branch},
        )

    payload = _decision_payload(
        action="rejected",
        skill=skill,
        repo=repo,
        branch=branch,
        home=home,
        proposals_path=proposals_path,
    )
    payload.update({"reason": clean_reason, "base_branch": target_branch})
    ledger = Path(decisions_path).expanduser() if decisions_path is not None else default_decisions_path(home)
    _append_ledger(ledger, payload)
    payload["ledger"] = str(ledger)
    return payload


def format_forge_status_table(payload: dict[str, Any]) -> list[str]:
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    if not skills:
        return [f"{payload.get('code', FORGE_NO_SIGNAL)}: {payload.get('message', 'no signal yet')}"]

    headers = ["Skill", "Sessions", "Trend", "Thresholds Crossed", "Pending", "Last Scored"]
    rows: list[list[str]] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                str(item.get("name") or ""),
                str(item.get("sessions_scored") or 0),
                str(item.get("trend") or "stable"),
                ", ".join(str(value) for value in item.get("thresholds_crossed") or []) or "(none)",
                "yes" if item.get("proposal_pending") else "no",
                str(item.get("last_scored") or ""),
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    return [
        render_row(headers),
        render_row(["-" * width for width in widths]),
        *[render_row(row) for row in rows],
    ]


def forge_init(
    with_cron: bool = False,
    scoring_script: Path | str | None = None,
    *,
    home: Path | str | None = None,
    codex_tmux_run_py: Path | str | None = None,
    subprocess_run: Any = subprocess.run,
) -> dict[str, Any]:
    home_dir = _home_dir(home)
    score_path = Path(scoring_script).expanduser() if scoring_script else default_scoring_script()
    score_path = score_path.resolve()
    settings_path = home_dir / ".claude" / "settings.json"
    codex_tmux_path = (
        Path(codex_tmux_run_py).expanduser()
        if codex_tmux_run_py is not None
        else home_dir / ".claude" / "skills" / "codex-tmux" / "scripts" / "run.py"
    )

    payload: dict[str, Any] = {
        "ok": True,
        "scoring_script": str(score_path),
        "settings": ensure_session_end_hook(settings_path, score_path),
        "codex_tmux": patch_codex_tmux_wrapper(codex_tmux_path, score_path),
        "cron": {"action": "skipped", "reason": "not_requested"},
        "warnings": [],
    }

    if with_cron:
        payload["cron"] = ensure_cron_entry(score_path, subprocess_run=subprocess_run)

    for section in ("codex_tmux", "cron"):
        warning = payload[section].get("warning")
        if warning:
            payload["warnings"].append(warning)
    return payload
