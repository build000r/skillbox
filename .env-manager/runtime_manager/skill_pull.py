"""Read-only host skill resolution and verified current-session pull."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ._shared.digest import tree_hash
from .skill_visibility import (
    DISPATCHER_CORE,
    SKILL_OVERRIDES_REL,
    SKILL_SCOPE_POLICY_FILES,
    _collect_installed_visibility_layers,
    _declared_skill_occurrences,
    _effective_occurrences,
    _matched_scope_rules_for_cwd,
    _operator_scope_policies,
    _parse_skill_frontmatter,
    _repo_override_visibility,
    _repo_override_policy,
    _simulated_installed_visibility,
    _skill_source_roots,
)


REQUEST_SCHEMA = "skill-resolution-request/v1"
RECEIPT_SCHEMA = "skill-resolution-receipt/v1"
PULL_SCHEMA = "skill-pull-result/v1"
ERROR_SCHEMA = "skill-error/v1"
MAX_ENTRY_BYTES = 1_000_000
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_ERROR_RETRYABLE = {
    "SBP_ENVIRONMENT_UNSUPPORTED": False,
    "SKILL_REQUEST_INVALID": False,
    "SKILL_NOT_ADMITTED": False,
    "SKILL_SOURCE_MISSING": False,
    "SKILL_ENTRY_INVALID_UTF8": False,
    "SKILL_TREE_DRIFT": True,
    "SKILL_LIFECYCLE_RETIRED": False,
    "SKILL_RUNTIME_REQUIREMENT_MISSING": False,
    "SKILL_COMPOSITION_CONFLICT": False,
    "SKILL_CONTEXT_BUDGET_EXCEEDED": False,
    "PERFORMANCE_FIXTURE_INVALID": False,
}

_CATALOG_OMISSION_REASONS = {
    "SKILL_SOURCE_MISSING": "SOURCE_MISSING",
    "SKILL_LIFECYCLE_RETIRED": "RETIRED",
    "SKILL_RUNTIME_REQUIREMENT_MISSING": "RUNTIME_MISSING",
    "SKILL_COMPOSITION_CONFLICT": "CONFLICT",
    "SKILL_CONTEXT_BUDGET_EXCEEDED": "BUDGET",
}


class SkillPullError(RuntimeError):
    """Typed, bounded failure for resolve/pull command output."""

    def __init__(self, error_code: str, message: str) -> None:
        if error_code not in _ERROR_RETRYABLE:
            raise ValueError(f"unknown skill pull error code: {error_code}")
        super().__init__(message)
        self.error_code = error_code

    def envelope(self) -> dict[str, Any]:
        return skill_error_envelope(self.error_code, str(self))


def _bounded_message(message: str) -> str:
    raw = str(message).encode("utf-8")
    if len(raw) <= 512:
        return str(message)
    return raw[:509].decode("utf-8", errors="ignore") + "..."


def skill_error_envelope(error_code: str, message: str) -> dict[str, Any]:
    """Return the closed ErrorEnvelope/v1 record."""
    return {
        "ok": False,
        "schema_version": ERROR_SCHEMA,
        "error_code": error_code,
        "message": _bounded_message(message),
        "retryable": _ERROR_RETRYABLE[error_code],
    }


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical JSON used by policy and receipt digests."""
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Request contains non-canonical JSON values.") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return path == root


def _repo_root(cwd: Path) -> Path:
    current = cwd.resolve() if cwd.is_dir() else cwd.resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise SkillPullError("SKILL_REQUEST_INVALID", "Current directory is not inside a registered Git repository.")


def _git_dir(repo_root: Path) -> Path:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository Git metadata is unreadable.") from exc
    if not line.startswith("gitdir:"):
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository Git metadata is invalid.")
    raw = line.split(":", 1)[1].strip()
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    try:
        return git_dir.resolve(strict=True)
    except OSError as exc:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository Git metadata target is missing.") from exc


def _git_common_dir(git_dir: Path) -> Path:
    marker = git_dir / "commondir"
    if not marker.is_file():
        return git_dir
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return git_dir
    common = Path(raw)
    if not common.is_absolute():
        common = git_dir / common
    return common.resolve(strict=False)


def _read_ref(git_dir: Path, ref: str) -> str | None:
    if not ref.startswith("refs/") or ".." in PurePosixPath(ref).parts:
        return None
    for base in (git_dir, _git_common_dir(git_dir)):
        path = base / PurePosixPath(ref)
        try:
            value = path.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError):
            continue
        if GIT_SHA1_RE.fullmatch(value):
            return value
    packed = _git_common_dir(git_dir) / "packed-refs"
    try:
        lines = packed.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1] == ref and GIT_SHA1_RE.fullmatch(parts[0].lower()):
            return parts[0].lower()
    return None


def _git_head(repo_root: Path) -> str:
    git_dir = _git_dir(repo_root)
    try:
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository HEAD is unreadable.") from exc
    if GIT_SHA1_RE.fullmatch(head.lower()):
        return head.lower()
    if head.startswith("ref:"):
        resolved = _read_ref(git_dir, head.split(":", 1)[1].strip())
        if resolved:
            return resolved
    raise SkillPullError("SKILL_REQUEST_INVALID", "Repository HEAD does not resolve to a commit.")


def _repository_identity(model: dict[str, Any], cwd: Path) -> tuple[Path, dict[str, Any]]:
    repo_root = _repo_root(cwd)
    matches: list[tuple[int, str]] = []
    for row in model.get("repos") or []:
        repo_id = str(row.get("id") or "").strip()
        raw_path = str(row.get("host_path") or "").strip()
        if not repo_id or not raw_path:
            continue
        path = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()
        if _is_under(cwd.resolve(), path) and path == repo_root:
            matches.append((len(path.parts), repo_id))
    if not matches:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository is not registered in the current runtime model.")
    repository_id = max(matches)[1]
    relative = cwd.resolve().relative_to(repo_root).as_posix() or "."
    return repo_root, {
        "repository_id": repository_id,
        "base_sha": _git_head(repo_root),
        "cwd_relative": relative,
    }


def _raw_source_digest(path_value: Any) -> tuple[bool, str | None]:
    raw_path = str(path_value or "").strip()
    if not raw_path or raw_path.startswith("client:"):
        return False, None
    path = Path(raw_path)
    try:
        return True, _sha256(path.read_bytes())
    except OSError:
        return False, None


def _policy_source_dirty(path_value: Any) -> bool:
    """Observe dirt for one exact file-backed policy input.

    This is the only Git call in resolve/pull. It is path-scoped and disables
    optional locks so the diagnostic cannot refresh the index. A policy source
    outside a Git checkout has no checkout state and is therefore clean.
    """
    raw_path = str(path_value or "").strip()
    if not raw_path or raw_path.startswith("client:"):
        return False
    path = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve(strict=False)
    search_from = path if path.is_dir() else path.parent
    canonical_policy_paths = {
        *(PurePosixPath(name).as_posix() for name in SKILL_SCOPE_POLICY_FILES),
        PurePosixPath(SKILL_OVERRIDES_REL).as_posix(),
    }
    repo_root: Path | None = None
    for candidate in (search_from, *search_from.parents):
        if not (candidate / ".git").exists():
            continue
        try:
            candidate_relative = path.relative_to(candidate).as_posix()
        except ValueError:
            continue
        try:
            git_dir = _git_dir(candidate)
        except SkillPullError as exc:
            if candidate_relative in canonical_policy_paths:
                raise SkillPullError(
                    "SBP_ENVIRONMENT_UNSUPPORTED",
                    "Policy checkout identity could not be observed.",
                ) from exc
            continue
        if (git_dir / "HEAD").is_file():
            repo_root = candidate
            break
        if candidate_relative in canonical_policy_paths:
            raise SkillPullError(
                "SBP_ENVIRONMENT_UNSUPPORTED",
                "Policy checkout identity could not be observed.",
            )
    if repo_root is None:
        return False
    try:
        relative = path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise SkillPullError(
            "SBP_ENVIRONMENT_UNSUPPORTED",
            "Policy checkout identity could not be observed.",
        ) from exc
    command = [
        "git",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        "--",
        relative,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillPullError(
            "SBP_ENVIRONMENT_UNSUPPORTED",
            "Policy checkout identity could not be observed.",
        ) from exc
    if result.returncode != 0:
        raise SkillPullError(
            "SBP_ENVIRONMENT_UNSUPPORTED",
            "Policy checkout identity could not be observed.",
        )
    return bool(result.stdout.strip())


def _policy_identity(
    model: dict[str, Any],
    cwd: Path,
    source_roots: tuple[Path, ...],
) -> dict[str, Any]:
    policies = _operator_scope_policies(model)
    if not policies and not source_roots:
        raise SkillPullError(
            "SBP_ENVIRONMENT_UNSUPPORTED",
            "No usable host skill policy or declared source root is available.",
        )

    global_present = False
    global_sha: str | None = None
    if policies:
        global_present, global_sha = _raw_source_digest(policies[0].get("_policy_path"))
        if not global_present:
            global_present = True
            global_sha = _sha256(canonical_json_bytes(policies[0]))

    override = _repo_override_policy(cwd)
    override_present, override_sha = _raw_source_digest(override.get("_policy_path"))
    floor_sha = _sha256(canonical_json_bytes({"dispatcher_floor": sorted(DISPATCHER_CORE)}))
    sources = [
        {"logical_source_id": "dispatcher-floor", "present": True, "sha256": floor_sha},
        {"logical_source_id": "global-scope", "present": global_present, "sha256": global_sha},
        {"logical_source_id": "repo-override", "present": override_present, "sha256": override_sha},
    ]
    policy_epoch = max(
        [int(policy.get("policy_epoch") or 0) for policy in policies if isinstance(policy, dict)] or [0]
    )
    policy_digest_input = {
        "dispatcher_floor": sorted(DISPATCHER_CORE),
        "policy_epoch": policy_epoch,
        "sources": sources,
    }
    dirty_observations: list[bool] = []
    if policies and global_present:
        dirty_observations.append(_policy_source_dirty(policies[0].get("_policy_path")))
    if override_present:
        dirty_observations.append(_policy_source_dirty(override.get("_policy_path")))
    return {
        "policy_sha256": _sha256(canonical_json_bytes(policy_digest_input)),
        "repo_override_sha256": override_sha if override_present else None,
        "policy_epoch": policy_epoch,
        "dispatcher_floor": sorted(DISPATCHER_CORE),
        "sources": sources,
        "dirty": any(dirty_observations),
    }


def _collect_resolution_visibility(
    model: dict[str, Any],
    cwd: Path,
) -> dict[str, Any]:
    """Build only the canonical winner inputs needed by resolve/pull.

    ``collect_skill_visibility`` additionally classifies broken links, computes
    parity, Beads advice, issue groups, and recommendations. Those audit views
    do not affect winner selection, but their source-repair searches dominate a
    500-skill pull. Keep resolution on the same canonical inventory/precedence
    primitives without constructing unrelated diagnostics.
    """
    declared, _declared_layers = _declared_skill_occurrences(model)
    installed, _installed_layers = _collect_installed_visibility_layers(
        cwd,
        include_global=True,
        include_project=True,
    )
    simulated, _simulated_layers = _simulated_installed_visibility(model)
    base_occurrences = [*declared, *installed, *simulated]
    overrides, _override_layers = _repo_override_visibility(
        model,
        cwd,
        base_occurrences,
    )
    occurrences = [*base_occurrences, *overrides]
    decisions, _shadowed = _effective_occurrences(occurrences)
    return {
        "occurrences": occurrences,
        "visibility_decisions": decisions,
        "matched_scope_rules": _matched_scope_rules_for_cwd(model, cwd),
    }


def build_resolution_request(
    model: dict[str, Any],
    *,
    cwd: str | os.PathLike[str] | Path,
    explicit_skills: list[str] | tuple[str, ...] = (),
    request_id: str | None = None,
) -> dict[str, Any]:
    """Construct and validate the closed host CLI V1 request."""
    cwd_path = Path(cwd).expanduser().resolve()
    names = list(explicit_skills)
    if (
        any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
        or any(not SKILL_NAME_RE.fullmatch(name) for name in names)
        or (request_id is not None and (not isinstance(request_id, str) or not request_id))
    ):
        raise SkillPullError("SKILL_REQUEST_INVALID", "Explicit skill names are invalid or duplicated.")
    _root, repository = _repository_identity(model, cwd_path)
    return {
        "schema_version": REQUEST_SCHEMA,
        "request_id": request_id or str(uuid.uuid4()),
        "mode": "host",
        "surface": "host-cli",
        "repository": repository,
        "explicit_skills": names,
        "max_entry_bytes": MAX_ENTRY_BYTES,
    }


def validate_resolution_request(request: Mapping[str, Any]) -> None:
    """Fail closed when a caller supplies anything outside the V1 record."""
    expected = {
        "schema_version",
        "request_id",
        "mode",
        "surface",
        "repository",
        "explicit_skills",
        "max_entry_bytes",
    }
    repository_expected = {"repository_id", "base_sha", "cwd_relative"}
    if set(request) != expected:
        raise SkillPullError("SKILL_REQUEST_INVALID", "Resolution request fields do not match V1.")
    repository = request.get("repository")
    names = request.get("explicit_skills")
    if (
        request.get("schema_version") != REQUEST_SCHEMA
        or not isinstance(request.get("request_id"), str)
        or not request.get("request_id")
        or request.get("mode") != "host"
        or request.get("surface") != "host-cli"
        or not isinstance(request.get("max_entry_bytes"), int)
        or isinstance(request.get("max_entry_bytes"), bool)
        or request.get("max_entry_bytes") != MAX_ENTRY_BYTES
        or not isinstance(repository, Mapping)
        or set(repository) != repository_expected
        or not isinstance(repository.get("repository_id"), str)
        or not repository.get("repository_id")
        or not isinstance(repository.get("base_sha"), str)
        or not GIT_SHA1_RE.fullmatch(repository.get("base_sha"))
        or not isinstance(repository.get("cwd_relative"), str)
        or not isinstance(names, list)
        or any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
        or any(not SKILL_NAME_RE.fullmatch(name) for name in names)
    ):
        raise SkillPullError("SKILL_REQUEST_INVALID", "Resolution request values do not match V1.")
    relative = repository["cwd_relative"]
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or any(part in {"", ".."} for part in pure.parts)
        or (relative != "." and "." in pure.parts)
        or pure.as_posix() != relative
    ):
        raise SkillPullError("SKILL_REQUEST_INVALID", "Repository-relative cwd is invalid.")


def _safe_file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill contains an unreadable file.") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill contains a non-regular entry.")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(fd)


def _safe_read_file(path: Path, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill entry is unreadable.") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill entry is not regular.")
        if metadata.st_size > maximum_bytes:
            raise SkillPullError(
                "SKILL_CONTEXT_BUDGET_EXCEEDED",
                "Skill entry exceeds the V1 context budget.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise SkillPullError(
                    "SKILL_CONTEXT_BUDGET_EXCEEDED",
                    "Skill entry exceeds the V1 context budget.",
                )
    finally:
        os.close(fd)


def _safe_tree_identity(root: Path) -> tuple[str, str, int]:
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill source is missing.") from exc
    if not root.is_dir():
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill source is not a directory.")

    entries: list[tuple[str, str]] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current, dirnames, filenames in walker:
            current_path = Path(current)
            for name in sorted([*dirnames, *filenames]):
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                pure = PurePosixPath(relative)
                if (
                    pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or any(char in relative for char in ("\x00", "\n", "\r"))
                ):
                    raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill contains a path-unsafe entry.")
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    try:
                        target = path.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise SkillPullError(
                            "SKILL_SOURCE_MISSING",
                            "Selected skill contains an invalid symlink entry.",
                        ) from exc
                    if not _is_under(target, root):
                        raise SkillPullError(
                            "SKILL_SOURCE_MISSING",
                            "Selected skill contains an escaping symlink entry.",
                        )
                    target_mode = target.lstat().st_mode
                    if stat.S_ISREG(target_mode):
                        entries.append((relative, _safe_file_sha256(target)))
                    elif not stat.S_ISDIR(target_mode):
                        raise SkillPullError(
                            "SKILL_SOURCE_MISSING",
                            "Selected skill symlink targets a non-regular entry.",
                        )
                    # os.walk(..., followlinks=False) does not descend into
                    # directory links. File links are intentionally hashed at
                    # the link's relative path, matching directory_tree_sha256.
                    continue
                if name in dirnames:
                    if not stat.S_ISDIR(mode):
                        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill contains a non-directory entry.")
                elif stat.S_ISREG(mode):
                    entries.append((relative, _safe_file_sha256(path)))
                else:
                    raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill contains a non-regular entry.")
    except SkillPullError:
        raise
    except OSError as exc:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill tree is unreadable.") from exc

    entry_path = root / "SKILL.md"
    entry_digest = next((digest for path, digest in entries if path == "SKILL.md"), None)
    if entry_digest is None:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Selected skill has no regular SKILL.md entry.")
    entry_bytes = entry_path.stat().st_size
    return tree_hash(entries), entry_digest, entry_bytes


def _selected_source(
    decision: Mapping[str, Any],
    source_roots: tuple[Path, ...],
) -> Path:
    raw = str(decision.get("source") or decision.get("path") or "").strip()
    if not raw:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Admitted skill has no selected source.")
    try:
        source = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SkillPullError("SKILL_SOURCE_MISSING", "Admitted skill source is missing.") from exc
    if not source_roots or not any(_is_under(source, root) for root in source_roots):
        raise SkillPullError("SKILL_SOURCE_MISSING", "Admitted skill source is outside declared roots.")
    if not source.is_dir():
        raise SkillPullError("SKILL_SOURCE_MISSING", "Admitted skill source is not a directory.")
    return source


def _source_repo_sha(source: Path) -> str | None:
    try:
        return _git_head(_repo_root(source))
    except SkillPullError:
        return None


def _lifecycle(source: Path) -> str:
    value = str(_parse_skill_frontmatter(source).get("lifecycle") or "active").strip().lower()
    return value if value in {"active", "deprecated", "superseded", "retired"} else "active"


def _admitted_reason(
    name: str,
    winner: Mapping[str, Any],
    matched_rules: list[dict[str, Any]],
    *,
    explicit: bool,
) -> str:
    if name in DISPATCHER_CORE:
        return "DISPATCHER_FLOOR"
    if winner.get("override_action") == "pin_on":
        return "REPO_PIN_ON"
    if any(name in {str(pattern) for pattern in rule.get("patterns") or []} for rule in matched_rules):
        return "CWD_RULE"
    if str(winner.get("layer") or "").startswith("global:"):
        return "GLOBAL_DEFAULT_ON"
    return "EXPLICIT_REQUEST" if explicit else "GLOBAL_DEFAULT_ON"


def _omitted_reason(winner: Mapping[str, Any] | None) -> str:
    if winner and winner.get("override_action") in {"pin_off", "opt_out_global"}:
        return "PIN_OFF"
    if winner and winner.get("state") == "broken":
        return "SOURCE_MISSING"
    return "DEFAULT_OFF"


def _omitted_decision(
    name: str,
    reason_code: str,
    *,
    lifecycle: str = "active",
    winner: Mapping[str, Any] | None = None,
    source: Path | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "lifecycle": lifecycle,
        "admission": "omitted",
        "reason_code": reason_code,
        "logical_source_id": (
            str(winner.get("source_bucket") or "host-canonical")
            if winner is not None and source is not None
            else None
        ),
        "source_repo_sha": _source_repo_sha(source) if source is not None else None,
        "tree_sha256": None,
        "entry_sha256": None,
        "entry_bytes": None,
        "estimated_entry_tokens": None,
    }


def _resolve_internal(
    model: dict[str, Any],
    *,
    cwd: str | os.PathLike[str] | Path,
    explicit_skills: list[str] | tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    cwd_path = Path(cwd).expanduser().resolve()
    request = build_resolution_request(model, cwd=cwd_path, explicit_skills=explicit_skills)
    validate_resolution_request(request)
    visibility = _collect_resolution_visibility(model, cwd_path)
    source_roots = tuple(
        root.resolve()
        for root in _skill_source_roots(model, visibility.get("occurrences") or [])
        if root.is_dir()
    )
    policy = _policy_identity(model, cwd_path, source_roots)
    winners = {
        str(row.get("name") or ""): row
        for row in visibility.get("visibility_decisions") or []
        if str(row.get("name") or "")
    }
    requested = list(request["explicit_skills"])
    names = requested or sorted(winners)
    matched_rules = list(visibility.get("matched_scope_rules") or [])
    selected_sources: dict[str, Path] = {}
    decisions: list[dict[str, Any]] = []

    for name in names:
        winner = winners.get(name)
        admitted = bool(winner) and winner.get("state") not in {"broken", "disabled"}
        if not admitted:
            decisions.append(_omitted_decision(name, _omitted_reason(winner)))
            continue

        source: Path | None = None
        lifecycle = "active"
        try:
            source = _selected_source(winner, source_roots)
            lifecycle = _lifecycle(source)
            if lifecycle == "retired":
                raise SkillPullError("SKILL_LIFECYCLE_RETIRED", "Requested skill is retired.")
            tree_sha, entry_sha, entry_bytes = _safe_tree_identity(source)
            frontmatter = _parse_skill_frontmatter(source)
            requirements = frontmatter.get("runtime_requirements") or []
            if isinstance(requirements, str):
                requirements = [requirements]
            if any(not _runtime_available(str(requirement)) for requirement in requirements):
                raise SkillPullError(
                    "SKILL_RUNTIME_REQUIREMENT_MISSING",
                    "A declared host runtime requirement is unavailable.",
                )
            raw_conflicts = frontmatter.get("conflicts") or []
            if isinstance(raw_conflicts, str):
                raw_conflicts = [raw_conflicts]
            conflicts = {str(item) for item in raw_conflicts}
            admitted_names = {
                candidate
                for candidate, row in winners.items()
                if row.get("state") not in {"broken", "disabled"}
            }
            if conflicts & admitted_names:
                raise SkillPullError(
                    "SKILL_COMPOSITION_CONFLICT",
                    "Admitted skill composition has a conflict.",
                )
            if entry_bytes > MAX_ENTRY_BYTES:
                raise SkillPullError(
                    "SKILL_CONTEXT_BUDGET_EXCEEDED",
                    "Skill entry exceeds the V1 context budget.",
                )
        except SkillPullError as exc:
            omission_reason = _CATALOG_OMISSION_REASONS.get(exc.error_code)
            if requested or omission_reason is None:
                raise
            decisions.append(
                _omitted_decision(
                    name,
                    omission_reason,
                    lifecycle=lifecycle,
                    winner=winner,
                    source=source,
                )
            )
            continue

        assert source is not None
        selected_sources[name] = source
        decisions.append(
            {
                "name": name,
                "lifecycle": lifecycle,
                "admission": "admitted",
                "reason_code": _admitted_reason(
                    name,
                    winner,
                    matched_rules,
                    explicit=name in requested,
                ),
                "logical_source_id": str(winner.get("source_bucket") or "host-canonical"),
                "source_repo_sha": _source_repo_sha(source),
                "tree_sha256": tree_sha,
                "entry_sha256": entry_sha,
                "entry_bytes": entry_bytes,
                "estimated_entry_tokens": (entry_bytes + 3) // 4,
            }
        )

    decisions.sort(key=lambda row: (row["admission"] != "admitted", row["name"]))
    if requested and any(row["admission"] != "admitted" for row in decisions):
        raise SkillPullError("SKILL_NOT_ADMITTED", "Requested skill is not admitted by current host policy.")
    selected_names = sorted(row["name"] for row in decisions if row["admission"] == "admitted")
    admitted_rows = [row for row in decisions if row["admission"] == "admitted"]
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "resolution_id": str(uuid.uuid4()),
        "request_id": request["request_id"],
        "created_at": _utc_now(),
        "mode": "host",
        "surface": "host-cli",
        "repository": request["repository"],
        "policy": policy,
        "skills": decisions,
        "selected_names": selected_names,
        "totals": {
            "candidate_count": len(decisions),
            "admitted_count": len(admitted_rows),
            "omitted_count": len(decisions) - len(admitted_rows),
            "admitted_entry_bytes": sum(int(row["entry_bytes"]) for row in admitted_rows),
            "estimated_entry_tokens": sum(int(row["estimated_entry_tokens"]) for row in admitted_rows),
        },
    }
    receipt["receipt_sha256"] = _sha256(canonical_json_bytes(receipt))
    return request, receipt, selected_sources


def _runtime_available(requirement: str) -> bool:
    """Check a simple declared executable requirement without shell/network."""
    name = requirement.strip()
    if not name or "/" in name or name in {".", ".."}:
        return False
    return any(
        (Path(part) / name).is_file() and os.access(Path(part) / name, os.X_OK)
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part
    )


def resolve_host_skills(
    model: dict[str, Any],
    *,
    cwd: str | os.PathLike[str] | Path,
    explicit_skills: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return one authoritative SkillResolutionReceipt/v1."""
    _request, receipt, _sources = _resolve_internal(
        model,
        cwd=cwd,
        explicit_skills=explicit_skills,
    )
    return receipt


def pull_host_skill(
    model: dict[str, Any],
    name: str,
    *,
    cwd: str | os.PathLike[str] | Path,
    after_resolve: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Resolve, recheck, and return exact current-session instructions."""
    _request, receipt, sources = _resolve_internal(model, cwd=cwd, explicit_skills=[name])
    if after_resolve is not None:
        after_resolve(receipt)
    decision = next(row for row in receipt["skills"] if row["name"] == name)
    source = sources[name]
    try:
        observed_tree, observed_entry, entry_bytes = _safe_tree_identity(source)
    except SkillPullError as exc:
        if exc.error_code == "SKILL_SOURCE_MISSING":
            raise
        raise SkillPullError("SKILL_TREE_DRIFT", "Skill source changed after resolution.") from exc
    if observed_tree != decision["tree_sha256"] or observed_entry != decision["entry_sha256"]:
        raise SkillPullError("SKILL_TREE_DRIFT", "Skill tree changed after resolution.")
    if entry_bytes > MAX_ENTRY_BYTES:
        raise SkillPullError("SKILL_CONTEXT_BUDGET_EXCEEDED", "Skill entry exceeds the V1 context budget.")
    try:
        entry_payload = _safe_read_file(source / "SKILL.md", maximum_bytes=MAX_ENTRY_BYTES)
        entry_text = entry_payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SkillPullError("SKILL_ENTRY_INVALID_UTF8", "Skill entry is not valid UTF-8.") from exc
    if _sha256(entry_payload) != observed_entry:
        raise SkillPullError("SKILL_TREE_DRIFT", "Skill entry changed before output.")
    return {
        "ok": True,
        "schema_version": PULL_SCHEMA,
        "name": name,
        "lifecycle": decision["lifecycle"],
        "entry_text": entry_text,
        "tree_sha256": observed_tree,
        "entry_sha256": observed_entry,
        "receipt_sha256": receipt["receipt_sha256"],
        "source_classification": "host-canonical",
        "instructions": "use this content immediately in the current session",
    }


__all__ = [
    "ERROR_SCHEMA",
    "MAX_ENTRY_BYTES",
    "PULL_SCHEMA",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "SkillPullError",
    "build_resolution_request",
    "canonical_json_bytes",
    "pull_host_skill",
    "resolve_host_skills",
    "skill_error_envelope",
    "validate_resolution_request",
]
