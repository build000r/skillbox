from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shared import DEFAULT_ROOT_DIR, EXIT_OK

MMDX_EXTENSIONS = (".mmdx", ".mmd")
MMDX_DEFAULT_LIMIT = 8
MMDX_MAX_SCAN_FILES = 20000
MMDX_MIN_MATCH_SCORE = 0.65
MMDX_SKIP_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".skillbox-state",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "builds",
    "coverage",
    "dist",
    "invocations",
    "logs",
    "node_modules",
    "sand",
    "skill-repos",
    "target",
    "vendor",
}


class MmdxOpenError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        recoverable: bool = True,
        recovery_hint: str = "",
        next_actions: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.recoverable = recoverable
        self.recovery_hint = recovery_hint
        self.next_actions = next_actions or []
        self.data = data or {}


def mmdx_error_payload(exc: RuntimeError) -> dict[str, Any]:
    if isinstance(exc, MmdxOpenError):
        error = {
            "type": exc.error_type,
            "message": str(exc),
            "recoverable": exc.recoverable,
        }
        if exc.recovery_hint:
            error["recovery_hint"] = exc.recovery_hint
        if exc.next_actions:
            error["next_actions"] = exc.next_actions
        error.update(exc.data)
        return {"error": error}

    return {
        "error": {
            "type": "mmdx_error",
            "message": str(exc),
            "recoverable": True,
            "recovery_hint": "Run `mmdx --no-open --format json` to inspect available diagrams.",
            "next_actions": ["mmdx --no-open --format json"],
        }
    }


def _clean_query(raw: str) -> str:
    cleaned = raw.strip().strip("'\"")
    cleaned = re.sub(r"\s*/\s*", "/", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _strip_line_suffix(cleaned)


def _strip_line_suffix(raw: str) -> str:
    # Accept paths copied as file.mmdx:12 or file.mmdx:12:3.
    match = re.match(r"^(.+\.(?:mmdx|mmd))(?::\d+){1,2}$", raw, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return raw


def _query_variants(query_parts: list[str] | tuple[str, ...] | None) -> list[str]:
    parts = [str(part).strip() for part in (query_parts or []) if str(part).strip()]
    if not parts:
        return []

    variants = [
        " ".join(parts),
        "".join(parts),
        str(Path(*parts)),
    ]
    if len(parts) == 1:
        variants.append(parts[0])

    cleaned: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        item = _clean_query(variant)
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def _normalize_for_match(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\.(?:mmdx|mmd)$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _git_root(cwd: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    return Path(raw).resolve() if raw else None


def _default_search_roots(cwd: Path, explicit_roots: list[str] | tuple[str, ...] | None = None) -> list[Path]:
    roots: list[Path] = []
    for raw_root in explicit_roots or []:
        raw = str(raw_root).strip()
        if not raw:
            continue
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not candidate.is_absolute():
            candidate = cwd / candidate
        roots.append(candidate.resolve(strict=False))

    if not roots:
        git_root = _git_root(cwd)
        if git_root is not None and _is_relative_to(cwd, git_root):
            roots.append(git_root)
        roots.append(cwd)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root.resolve(strict=False))
    return deduped


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _candidate_from_exact_query(cwd: Path, variants: list[str]) -> Path | None:
    for variant in variants:
        raw = Path(os.path.expandvars(os.path.expanduser(variant)))
        candidates = [raw] if raw.is_absolute() else [cwd / raw, raw]
        for candidate in candidates:
            path = candidate.resolve(strict=False)
            if path.is_file() and path.suffix.lower() in MMDX_EXTENSIONS:
                return path
    return None


def _directory_from_query(cwd: Path, variants: list[str]) -> Path | None:
    for variant in variants:
        raw = Path(os.path.expandvars(os.path.expanduser(variant)))
        candidates = [raw] if raw.is_absolute() else [cwd / raw, raw]
        for candidate in candidates:
            path = candidate.resolve(strict=False)
            if path.is_dir():
                return path
    return None


def _iter_mmdx_files(roots: list[Path]) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname for dirname in sorted(dirnames)
                if dirname not in MMDX_SKIP_DIRS and not dirname.startswith(".tmp")
            ]
            for filename in sorted(filenames):
                path = Path(current) / filename
                if path.suffix.lower() not in MMDX_EXTENSIONS:
                    continue
                key = str(path.resolve(strict=False))
                if key in seen:
                    continue
                seen.add(key)
                files.append(path.resolve(strict=False))
                if len(files) >= MMDX_MAX_SCAN_FILES:
                    return files, True
    return files, truncated


def _best_display_root(path: Path, cwd: Path, roots: list[Path]) -> Path:
    candidates = [cwd, *roots]
    best = cwd
    best_len = -1
    for root in candidates:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        root_len = len(str(root))
        if root_len > best_len:
            best = root
            best_len = root_len
    return best


def _mtime_payload(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    iso = datetime.fromtimestamp(mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return mtime, iso


def _candidate_payload(path: Path, cwd: Path, roots: list[Path], *, score: float | None = None) -> dict[str, Any]:
    display_root = _best_display_root(path, cwd, roots)
    try:
        rel_path = str(path.relative_to(display_root))
    except ValueError:
        rel_path = str(path)
    mtime, modified_at = _mtime_payload(path)
    payload = {
        "path": str(path),
        "rel_path": rel_path,
        "name": path.name,
        "stem": path.stem,
        "extension": path.suffix.lower(),
        "modified_at": modified_at,
        "_mtime": mtime,
    }
    if score is not None:
        payload["score"] = round(score, 4)
    return payload


def _score_candidate(path: Path, rel_path: str, query: str) -> float:
    q = _normalize_for_match(query)
    rel = _normalize_for_match(rel_path)
    name = _normalize_for_match(path.name)
    stem = _normalize_for_match(path.stem)
    suffix_path = "/".join(Path(rel_path).parts[-3:])
    suffix = _normalize_for_match(suffix_path)

    if not q:
        return 0.0
    if q == rel:
        return 1.25
    if q == suffix:
        return 1.18
    if q == name or q == stem:
        return 1.12
    if rel.endswith(q):
        return 1.08
    if q in rel:
        return 0.98 + min(0.08, len(q) / max(len(rel), 1) * 0.08)
    if all(token in rel for token in q.split()):
        return 0.9
    return max(
        difflib.SequenceMatcher(None, q, rel).ratio(),
        difflib.SequenceMatcher(None, q, name).ratio(),
        difflib.SequenceMatcher(None, q, stem).ratio(),
        difflib.SequenceMatcher(None, q, suffix).ratio(),
    )


def _rank_candidates(
    files: list[Path],
    cwd: Path,
    roots: list[Path],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files:
        display_root = _best_display_root(path, cwd, roots)
        try:
            rel_path = str(path.relative_to(display_root))
        except ValueError:
            rel_path = str(path)
        score = _score_candidate(path, rel_path, query) if query else None
        payload = _candidate_payload(path, cwd, roots, score=score)
        rows.append(payload)

    if query:
        rows.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("_mtime") or 0.0), str(item["rel_path"])), reverse=True)
    else:
        rows.sort(key=lambda item: (float(item.get("_mtime") or 0.0), str(item["rel_path"])), reverse=True)

    for row in rows:
        row.pop("_mtime", None)
    return rows[:max(0, limit)]


def _mmdx_script_candidates(root_dir: Path) -> list[Path]:
    raw_env = [
        os.environ.get("SKILLBOX_MMDX_SCRIPT"),
        os.environ.get("MMDX_SCRIPT"),
    ]
    raw_dirs = [
        os.environ.get("SKILLBOX_MMDX_SKILL_DIR"),
        os.environ.get("MMDX_SKILL_DIR"),
    ]
    candidates: list[Path] = []
    for raw in raw_env:
        if raw:
            candidates.append(Path(os.path.expandvars(os.path.expanduser(raw))))
    for raw in raw_dirs:
        if raw:
            candidates.append(Path(os.path.expandvars(os.path.expanduser(raw))) / "scripts" / "mmd.py")
    candidates.extend(
        [
            root_dir.parent / "skills" / "mmdx" / "scripts" / "mmd.py",
            root_dir / "workspace" / "skill-repos" / "mmdx" / "scripts" / "mmd.py",
            root_dir / "home" / ".claude" / "skills" / "mmdx" / "scripts" / "mmd.py",
            root_dir / "home" / ".codex" / "skills" / "mmdx" / "scripts" / "mmd.py",
            Path.home() / ".claude" / "skills" / "mmdx" / "scripts" / "mmd.py",
            Path.home() / ".codex" / "skills" / "mmdx" / "scripts" / "mmd.py",
            Path.home() / ".agents" / "skills" / "mmdx" / "scripts" / "mmd.py",
        ]
    )
    return candidates


def resolve_mmdx_script(root_dir: Path = DEFAULT_ROOT_DIR) -> Path:
    seen: set[str] = set()
    for candidate in _mmdx_script_candidates(root_dir):
        path = candidate.expanduser().resolve(strict=False)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    raise MmdxOpenError(
        "mmdx_script_not_found",
        "Could not find the MMDX skill script `scripts/mmd.py`.",
        recoverable=True,
        recovery_hint=(
            "Set SKILLBOX_MMDX_SKILL_DIR to the mmdx skill directory or install the mmdx skill into "
            "~/.claude/skills or ~/.codex/skills."
        ),
        next_actions=["skill add mmdx --cwd \"$PWD\"", "mmdx --no-open --format json"],
    )


def _open_selected_mmdx(
    selected: dict[str, Any],
    *,
    root_dir: Path,
    tmux: bool = False,
    tmux_submit: bool = False,
    allow_parser_install: bool = False,
    mmd_script: Path | None = None,
) -> dict[str, Any]:
    script = mmd_script or resolve_mmdx_script(root_dir)
    command = [
        sys.executable,
        str(script),
        str(selected["path"]),
        "--open",
    ]
    if tmux:
        command.append("--tmux")
    if tmux_submit:
        command.append("--tmux-submit")
    if not allow_parser_install:
        command.append("--no-parser-install")

    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        raise MmdxOpenError(
            "mmdx_open_failed",
            "MMDX viewer open failed.",
            recoverable=True,
            recovery_hint=stderr or stdout or "Run the command with --no-open, then invoke the reported mmd.py command manually.",
            next_actions=[f"mmdx {selected['rel_path']} --no-open --format json"],
            data={"exit_code": proc.returncode, "stderr": stderr, "stdout": stdout},
        )

    url = ""
    for line in stdout.splitlines():
        if line.startswith("http://") or line.startswith("https://") or line.startswith("pako:"):
            url = line.strip()
            break

    return {
        "command": command,
        "url": url,
        "stdout": stdout,
        "stderr": stderr,
    }


def mmdx_open_payload(
    *,
    root_dir: Path = DEFAULT_ROOT_DIR,
    cwd: Path | None = None,
    query_parts: list[str] | tuple[str, ...] | None = None,
    search_roots: list[str] | tuple[str, ...] | None = None,
    open_file: bool = True,
    limit: int = MMDX_DEFAULT_LIMIT,
    tmux: bool = False,
    tmux_submit: bool = False,
    allow_parser_install: bool = False,
    mmd_script: Path | None = None,
) -> tuple[dict[str, Any], int]:
    resolved_cwd = (cwd or Path(os.environ.get("PWD") or os.getcwd())).expanduser().resolve(strict=False)
    variants = _query_variants(query_parts)
    query = variants[0] if variants else ""
    effective_limit = max(1, int(limit or MMDX_DEFAULT_LIMIT))

    exact = _candidate_from_exact_query(resolved_cwd, variants)
    directory = None if exact else _directory_from_query(resolved_cwd, variants)
    roots = [directory] if directory else _default_search_roots(resolved_cwd, search_roots)
    roots = [root for root in roots if root.is_dir()]
    if not roots and exact is None:
        raise MmdxOpenError(
            "mmdx_search_root_not_found",
            f"No readable MMDX search roots for cwd {resolved_cwd}.",
            recoverable=True,
            recovery_hint="Pass --cwd or --search-root pointing at a repo or directory that contains .mmdx files.",
            next_actions=["mmdx --cwd \"$PWD\" --no-open --format json"],
            data={"cwd": str(resolved_cwd)},
        )

    selected: dict[str, Any] | None = None
    matches: list[dict[str, Any]]
    scanned = 0
    truncated = False

    if exact is not None:
        exact_roots = _default_search_roots(resolved_cwd, search_roots)
        selected = _candidate_payload(exact, resolved_cwd, exact_roots, score=1.5)
        selected.pop("_mtime", None)
        matches = [selected]
        scanned = 1
    else:
        files, truncated = _iter_mmdx_files(roots)
        scanned = len(files)
        matches = _rank_candidates(files, resolved_cwd, roots, "" if directory else query, limit=effective_limit)
        if query and matches:
            selected = matches[0]
            if directory is None and float(selected.get("score") or 0.0) < MMDX_MIN_MATCH_SCORE:
                selected = None

    if query and selected is None:
        raise MmdxOpenError(
            "mmdx_no_match",
            f"No .mmdx or .mmd files matched {query!r}.",
            recoverable=True,
            recovery_hint="Run without a query to list recent diagrams, or pass --search-root for a wider directory.",
            next_actions=["mmdx --no-open", "mmdx --search-root <dir> <query>"],
            data={
                "query": query,
                "cwd": str(resolved_cwd),
                "search_roots": [str(root) for root in roots],
                "alternatives": matches[: min(3, len(matches))],
            },
        )

    opened: dict[str, Any] | None = None
    action = "listed"
    if selected is not None:
        action = "resolved"
        if open_file:
            opened = _open_selected_mmdx(
                selected,
                root_dir=root_dir,
                tmux=tmux,
                tmux_submit=tmux_submit,
                allow_parser_install=allow_parser_install,
                mmd_script=mmd_script,
            )
            action = "opened"

    next_actions = [
        "mmdx --no-open",
    ]
    if selected is not None:
        next_actions.insert(0, f"mmdx {selected['rel_path']} --no-open --format json")

    payload: dict[str, Any] = {
        "ok": True,
        "action": action,
        "query": query,
        "cwd": str(resolved_cwd),
        "search_roots": [str(root) for root in roots],
        "scanned": scanned,
        "truncated": truncated,
        "returned": len(matches),
        "selected": selected,
        "matches": matches,
        "open": bool(open_file),
        "next_actions": next_actions,
    }
    if opened is not None:
        payload["viewer"] = {
            "url": opened["url"],
            "command": opened["command"],
        }
        if opened["stderr"]:
            payload["viewer"]["stderr"] = opened["stderr"]
    return payload, EXIT_OK


def print_mmdx_payload_text(payload: dict[str, Any]) -> None:
    if "error" in payload:
        error = payload["error"]
        print(f"mmdx: error {error.get('type', 'mmdx_error')}", file=sys.stderr)
        print(str(error.get("message") or ""), file=sys.stderr)
        if error.get("recovery_hint"):
            print(f"hint: {error['recovery_hint']}", file=sys.stderr)
        for action in error.get("next_actions") or []:
            print(f"next: {action}", file=sys.stderr)
        return

    print(f"mmdx: {payload.get('action')}")
    print(f"cwd: {payload.get('cwd')}")
    selected = payload.get("selected")
    if selected:
        print(f"path: {selected.get('rel_path')}")
        print(f"score: {selected.get('score')}")
    viewer = payload.get("viewer") or {}
    if viewer.get("url"):
        print(f"url: {viewer['url']}")
    print(f"matches: {payload.get('returned', 0)} of {payload.get('scanned', 0)}")
    if payload.get("truncated"):
        print(f"truncated: true (scan limit {MMDX_MAX_SCAN_FILES})")

    matches = payload.get("matches") or []
    if matches and not selected:
        for match in matches:
            print(f"  - {match.get('rel_path')} {match.get('modified_at')}")
    elif len(matches) > 1:
        print("alternates:")
        for match in matches[1:4]:
            score = match.get("score")
            score_text = f" score={score}" if score is not None else ""
            print(f"  - {match.get('rel_path')}{score_text}")

    actions = payload.get("next_actions") or []
    if actions:
        print("next:")
        for action in actions[:3]:
            print(f"  {action}")
