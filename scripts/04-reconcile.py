#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from lib.runtime_model import build_runtime_model


ROOT_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = ROOT_DIR / "workspace"

# ---------------------------------------------------------------------------
# Expected files: hand-curated CORE + manifest/Makefile-DERIVED (provenance)
# ---------------------------------------------------------------------------
#
# K2 root fix (skillbox-safety-trust-boundary-epic-lzz1.5): the must-exist file
# list is the credibility surface `make doctor` sells in the README. A static
# list silently ROTS whenever someone renames/removes a listed file (or forgets
# to add a newly-required one). So we DERIVE the bulk of the list from the real
# sources that already reference each file — every derived entry carries
# PROVENANCE ("expected because <source>:<reference>") — and keep only a small
# CORE set of genuinely hand-curated files that NO source references. Killing
# the failure CLASS, not patching instances.
#
# CORE is the residual: files that no Makefile target / compose composition /
# workspace manifest / installer / Dockerfile references, so they cannot be
# derived. Each MUST carry an inline justification. Keep this list <= 10.
CORE_EXPECTED_FILES: list[tuple[str, str]] = [
    # The credibility/handshake docs the README + AGENTS.md sell to operators and
    # agents; pure prose, referenced by no build/compose/manifest source.
    ("README.md", "operator-facing project README; not referenced by any build source"),
    ("AGENTS.md", "coding-agent guide; not referenced by any build source"),
    (".env-manager/README.md", "runtime-manager subtree README; documentation only, no source references it"),
    # The curl|bash installer is the entry point a fresh host runs BEFORE any
    # Makefile/compose exists; nothing upstream references it, so it cannot be
    # derived — yet it must ship.
    ("install.sh", "curl|bash bootstrap installer; the root entry point, referenced by nothing upstream"),
    # `.env.example` is the manifest-validated env template. It is consumed by
    # load_env_defaults()/check_env_defaults (a value check, not a path
    # reference) and `make bootstrap-env`; treated as core so its existence is
    # asserted even when those value checks are skipped.
    (".env.example", "manifest-validated env template seeded by bootstrap-env; existence is load-bearing for env-defaults check"),
    # The Dockerfile is the image contract. The Makefile builds it via
    # `$(COMPOSEF) build` (compose resolves `build.dockerfile`), not by naming
    # the path, so it is not statically derivable from the Makefile text.
    ("Dockerfile", "workspace image contract; built via compose `build`, never named as a path in any source"),
]
CORE_EXPECTED_FILE_NAMES: list[str] = [path for path, _ in CORE_EXPECTED_FILES]

# Compose files that participate in the Makefile `$(COMPOSEF)` BASE composition
# (docker-compose.yml -f $(_MONOSERVER_LAYER)). The per-client override
# (_CLIENT_OVERRIDE) and the swimmers overlay are intentionally NOT here: the
# override is optional/generated and the swimmers overlay is derived from
# scripts/05-swimmers.sh instead (see _derive_compose_overlay_files).
MAKEFILE_BASE_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.monoserver.yml")

# Workspace manifests loaded by build_model() — the declared outer model inputs.
WORKSPACE_MANIFEST_FILES = (
    "workspace/sandbox.yaml",
    "workspace/dependencies.yaml",
    "workspace/persistence.yaml",
    "workspace/runtime.yaml",
    "workspace/skill-repos.yaml",
)

# Regexes used to harvest file references out of Makefile / installer text.
_SCRIPT_REF_PATTERN = re.compile(
    r"(?:scripts/[A-Za-z0-9_./-]+\.(?:py|sh)|\.env-manager/[A-Za-z0-9_./-]+\.py)"
)
_COMPOSE_REF_PATTERN = re.compile(r"docker-compose[A-Za-z0-9_.-]*\.yml")

EXPECTED_DIRECTORIES = [
    ".env-manager",
    "docker",
    "repos",
    "scripts",
    "skills",
    "workspace",
    "workspace/client-blueprints",
]
RECONCILE_COMMAND_NAMES = {"capabilities", "doctor", "render", "robot-docs", "robot-triage"}
JSON_FLAG_ALIASES = {"--json", "--jason", "--jsno", "--jsson"}


@dataclass
class CheckResult:
    status: str
    code: str
    message: str
    details: dict[str, Any] | None = None
    fix_command: str | None = None


DRIFT_FIX_COMMAND = "make render"
BEADS_INIT_FIX_COMMAND = "sbp beads init --cwd ."
BEADS_SYNC_FIX_COMMAND = "br sync --flush-only"
BEADS_SYNC_STATUS_COMMAND = "br sync --status --json"
SKILL_SYNC_FIX_COMMAND = "make runtime-sync"
SKILL_SYNC_DRY_RUN_COMMAND = "python3 .env-manager/manage.py sync --dry-run --format json"
RUNTIME_DOCTOR_COMMAND = "python3 .env-manager/manage.py doctor --format json"

# Operator secret files that must never sit directly under a bind-mounted host dir
# (e.g. the `.:/workspace` mount), where in-container agents could read them. The
# canonical home is ${SKILLBOX_STATE_ROOT}/operator/. Kept in sync with the helpers
# in scripts/box.py and scripts/operator_mcp_server.py.
OPERATOR_SECRET_FILENAMES = (".env", ".env.box")


# ---------------------------------------------------------------------------
# Expected-files derivation (K2 root fix)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedFile:
    """A must-exist repo file with the SOURCE that makes it expected.

    ``path`` is repo-relative. ``provenance`` is the human-readable reason
    (surfaced in render + in any missing-file finding). ``source`` is the
    repo-relative file whose text/declaration produced this entry — the doctor
    self-check verifies that source still PARSES (or, for CORE, is the file
    itself) so a derivation that silently stops emitting an entry is caught.
    ``optional`` derivations never produce entries; required referenced files
    become entries whether or not they currently exist.
    """

    path: str
    provenance: str
    source: str


def _read_text_if_present(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except UnicodeDecodeError:  # pragma: no cover - defensive
        return None


def _makefile_target_names(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or line.startswith(("\t", " ")):
        return None
    head, separator, tail = line.partition(":")
    if not separator or tail.lstrip().startswith("="):
        return None
    targets = [item for item in head.split() if item and not item.startswith(".")]
    return targets


def _derive_makefile_script_files(root_dir: Path) -> list[ExpectedFile]:
    """Scripts referenced by Makefile targets (scripts/*.py|sh, .env-manager/*.py).

    A file is required because a make target invokes it; if the script is
    renamed/removed the target breaks, so doctor should fail until the rename
    is reflected everywhere. Optionality is implicit: a script the Makefile
    never names simply never appears here.
    """
    text = _read_text_if_present(root_dir / "Makefile")
    if text is None:
        return []
    entries: dict[str, ExpectedFile] = {}
    current_targets: list[str] = []
    for line in text.splitlines():
        target_names = _makefile_target_names(line)
        if target_names is not None:
            current_targets = target_names
        elif line.strip() and not line.startswith(("\t", "#")):
            current_targets = []

        provenance = "referenced by Makefile"
        if current_targets:
            provenance = f"referenced by Makefile target {', '.join(current_targets)}"
        for match in _SCRIPT_REF_PATTERN.findall(line):
            entries.setdefault(match, ExpectedFile(path=match, provenance=provenance, source="Makefile"))
    return list(entries.values())


def _derive_makefile_compose_files(root_dir: Path) -> list[ExpectedFile]:
    """Compose files in the Makefile `$(COMPOSEF)` BASE composition.

    Only the always-on base files (docker-compose.yml + the fat-default
    monoserver layer) are required. The per-client override is OPTIONAL
    (generated, may be absent) and the swimmers overlay is derived from
    scripts/05-swimmers.sh instead — neither is forced here.
    """
    text = _read_text_if_present(root_dir / "Makefile")
    if text is None:
        return []
    present = set(_COMPOSE_REF_PATTERN.findall(text))
    return [
        ExpectedFile(
            path=name,
            provenance="Makefile $(COMPOSEF) base compose composition",
            source="Makefile",
        )
        for name in MAKEFILE_BASE_COMPOSE_FILES
        if name in present
    ]


def _derive_swimmers_compose_overlay(root_dir: Path) -> list[ExpectedFile]:
    """The swimmers compose overlay, derived from scripts/05-swimmers.sh.

    05-swimmers.sh hard-references docker-compose.swimmers.yml in its
    COMPOSE_FILES array (unconditionally), so the overlay is required whenever
    that script ships. We only emit a compose file the script actually names;
    the per-client override it may also pick up is optional and not emitted.
    """
    text = _read_text_if_present(root_dir / "scripts" / "05-swimmers.sh")
    if text is None:
        return []
    refs = set(_COMPOSE_REF_PATTERN.findall(text))
    name = "docker-compose.swimmers.yml"
    if name not in refs:
        return []
    return [
        ExpectedFile(
            path=name,
            provenance="referenced by scripts/05-swimmers.sh COMPOSE_FILES overlay",
            source="scripts/05-swimmers.sh",
        )
    ]


def _derive_installer_script_files(root_dir: Path) -> list[ExpectedFile]:
    """Scripts referenced by the curl|bash installer (install.sh).

    install.sh runs the DO bootstrap + Tailscale install scripts by path; if
    they are renamed the installer silently breaks. Derived (not CORE) so a
    rename forces a coordinated update. Only files install.sh actually names
    are emitted.
    """
    text = _read_text_if_present(root_dir / "install.sh")
    if text is None:
        return []
    entries: dict[str, ExpectedFile] = {}
    for match in sorted(set(_SCRIPT_REF_PATTERN.findall(text))):
        entries.setdefault(
            match,
            ExpectedFile(path=match, provenance="referenced by install.sh", source="install.sh"),
        )
    return list(entries.values())


def _derive_dockerfile_entrypoint(root_dir: Path) -> list[ExpectedFile]:
    """The container entrypoint script COPYed by the Dockerfile.

    The Dockerfile `COPY docker/sandbox-entrypoint.sh ...` is what backs the
    sandbox.yaml `entrypoints`. Derived from the literal COPY source so a
    rename of the entrypoint script is caught. COPYed shell script sources are
    emitted even when missing so doctor can report the broken image contract.
    """
    text = _read_text_if_present(root_dir / "Dockerfile")
    if text is None:
        return []
    entries: dict[str, ExpectedFile] = {}
    copy_pattern = re.compile(r"^\s*COPY\s+(\S+)\s", re.MULTILINE)
    for raw in copy_pattern.findall(text):
        candidate = raw.strip().removeprefix("./")
        if candidate.endswith(".sh") and not candidate.startswith("--"):
            entries.setdefault(
                candidate,
                ExpectedFile(
                    path=candidate,
                    provenance="COPYed into the image by Dockerfile",
                    source="Dockerfile",
                ),
            )
    return list(entries.values())


def _derive_workspace_manifest_files(root_dir: Path) -> list[ExpectedFile]:
    """Workspace manifests build_model() loads as the outer model inputs."""
    return [
        ExpectedFile(
            path=name,
            provenance="declared workspace manifest loaded by build_model()",
            source=name,
        )
        for name in WORKSPACE_MANIFEST_FILES
    ]


def _derive_reconcile_lib_files(root_dir: Path) -> list[ExpectedFile]:
    """The lib package this script imports (`from lib.runtime_model import ...`).

    scripts/04-reconcile.py imports scripts/lib/runtime_model.py, which makes
    the module + its package __init__ load-bearing for the reconcile tool
    itself. Both are required whenever the import statement is present.
    """
    text = _read_text_if_present(root_dir / "scripts" / "04-reconcile.py")
    if text is None or "from lib.runtime_model import" not in text:
        return []
    return [
        ExpectedFile(
            path="scripts/lib/__init__.py",
            provenance="lib package init imported by scripts/04-reconcile.py",
            source="scripts/04-reconcile.py",
        ),
        ExpectedFile(
            path="scripts/lib/runtime_model.py",
            provenance="imported by scripts/04-reconcile.py (`from lib.runtime_model import ...`)",
            source="scripts/04-reconcile.py",
        ),
    ]


_DERIVATION_SOURCES = (
    _derive_makefile_script_files,
    _derive_makefile_compose_files,
    _derive_swimmers_compose_overlay,
    _derive_installer_script_files,
    _derive_dockerfile_entrypoint,
    _derive_workspace_manifest_files,
    _derive_reconcile_lib_files,
)


def derive_expected_files(root_dir: Path) -> list[ExpectedFile]:
    """Compute the DERIVED expected-file list from the real sources.

    Deduplicates by path (first provenance wins, deterministic source order).
    CORE files are intentionally not included here — they are merged by
    ``resolved_expected_files``.
    """
    by_path: dict[str, ExpectedFile] = {}
    for derive in _DERIVATION_SOURCES:
        for entry in derive(root_dir):
            by_path.setdefault(entry.path, entry)
    return [by_path[path] for path in sorted(by_path)]


def resolved_expected_files(root_dir: Path) -> list[ExpectedFile]:
    """CORE (hand-curated) + DERIVED, deduplicated, sorted by path.

    The single source of truth for both the required-files check and the
    inspectable render surface. CORE wins ties (a hand-curated justification is
    more specific than a derivation).
    """
    by_path: dict[str, ExpectedFile] = {}
    for name, reason in CORE_EXPECTED_FILES:
        by_path[name] = ExpectedFile(path=name, provenance=f"core: {reason}", source=name)
    for entry in derive_expected_files(root_dir):
        by_path.setdefault(entry.path, entry)
    return [by_path[path] for path in sorted(by_path)]


def _operator_state_root() -> Path:
    state_root = os.environ.get("SKILLBOX_STATE_ROOT", "").strip() or "./.skillbox-state"
    base = Path(state_root)
    if not base.is_absolute():
        base = ROOT_DIR / base
    return base


def _operator_env_path() -> Path:
    """Resolve the operator `.env` (non-secret overrides), preferring the relocated
    state-root copy and falling back to the deprecated repo-root location."""
    relocated = (_operator_state_root() / "operator" / ".env").resolve()
    if relocated.is_file():
        return relocated
    return ROOT_DIR / ".env"


def repo_rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to use reconcile commands."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required file missing: {repo_rel(path)}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise RuntimeError(f"Failed to parse {repo_rel(path)}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a YAML object in {repo_rel(path)}")
    return raw


def load_env_defaults(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid env line in {repo_rel(path)}: {raw_line}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_runtime_env_overrides(path: Path, allowed_keys: Any) -> dict[str, str]:
    """Load operator overrides from `.env` for keys participating in the runtime env.

    Skips empty values so `${KEY:-default}` still resolves to the manifest default,
    matching docker compose's substitution semantics. Malformed lines are ignored
    here (the strict `.env.example` check handles syntax validation).
    """
    if not path.is_file():
        return {}
    allowed = set(allowed_keys)
    overrides: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in allowed and value:
            overrides[key] = value
    return overrides


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse {repo_rel(path)}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"Expected a JSON object in {repo_rel(path)}")
    return raw


def declared_skill_names(skill_repos_doc: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in skill_repos_doc.get("skill_repos") or []:
        pick = entry.get("pick") or []
        if pick:
            names.extend(str(item) for item in pick if str(item).strip())
            continue

        repo_name = str(entry.get("repo") or "").strip()
        path_name = str(entry.get("path") or "").strip()
        if repo_name:
            names.append(repo_name.rsplit("/", 1)[-1])
        elif path_name:
            names.append(Path(path_name).name)
    return sorted(dict.fromkeys(names))


def lockfile_skill_names(lock_payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in lock_payload.get("skills") or []:
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return sorted(dict.fromkeys(names))


def build_model() -> dict[str, Any]:
    sandbox_doc = load_yaml(WORKSPACE_DIR / "sandbox.yaml")
    dependencies_doc = load_yaml(WORKSPACE_DIR / "dependencies.yaml")
    persistence_doc = load_yaml(WORKSPACE_DIR / "persistence.yaml")
    skill_repos_doc = load_yaml(WORKSPACE_DIR / "skill-repos.yaml")
    skill_repos_lock = load_json(WORKSPACE_DIR / "skill-repos.lock.json")
    runtime_model = build_runtime_model(ROOT_DIR)
    env_defaults = load_env_defaults(ROOT_DIR / ".env.example")

    sandbox = sandbox_doc.get("sandbox") or {}
    runtime = sandbox.get("runtime") or {}
    paths = sandbox.get("paths") or {}
    ports = sandbox.get("ports") or {}
    runtime_skillsets = runtime_model.get("skills") or []
    default_skillset = next(
        (
            item
            for item in runtime_skillsets
            if item.get("id") == "default-skills" and item.get("client", "") == ""
        ),
        {},
    )

    home_root = str(paths.get("claude_root", "")).rsplit("/.claude", 1)[0]
    monoserver_root = str(paths.get("monoserver_root", ""))
    storage = runtime_model.get("storage") or {}
    targets = persistence_doc.get("targets") or {}
    local_target = targets.get("local") or {}
    local_state_root = str(local_target.get("default_state_root") or "./.skillbox-state")

    def join_state_root(relative_path: str) -> str:
        return f"{local_state_root.rstrip('/')}/{relative_path}"

    expected_env = {
        "SKILLBOX_NAME": str(sandbox.get("name", "")),
        "SKILLBOX_STORAGE_PROVIDER": str(local_target.get("provider") or "local"),
        "SKILLBOX_STATE_ROOT": local_state_root,
        "SKILLBOX_STORAGE_FILESYSTEM": "",
        "SKILLBOX_STORAGE_REQUIRED": "false",
        "SKILLBOX_STORAGE_MIN_FREE_GB": "0",
        "SKILLBOX_WORKSPACE_ROOT": str(paths.get("workspace_root", "")),
        "SKILLBOX_REPOS_ROOT": str(paths.get("repos_root", "")),
        "SKILLBOX_SKILLS_ROOT": str(paths.get("skills_root", "")),
        "SKILLBOX_LOG_ROOT": str(paths.get("log_root", "")),
        "SKILLBOX_HOME_ROOT": home_root,
        "SKILLBOX_MONOSERVER_ROOT": monoserver_root,
        "SKILLBOX_CLIENTS_ROOT": f"{paths.get('workspace_root', '')}/workspace/clients",
        "SKILLBOX_CLIENTS_HOST_ROOT": join_state_root("clients"),
        "SKILLBOX_MONOSERVER_HOST_ROOT": join_state_root("monoserver"),
        "SKILLBOX_API_PORT": str(ports.get("api", "")),
        "SKILLBOX_WEB_PORT": str(ports.get("web", "")),
        "SKILLBOX_SWIMMERS_PORT": str(ports.get("swimmers", "")),
        "SKILLBOX_SWIMMERS_PUBLISH_HOST": "127.0.0.1",
        "SKILLBOX_SWIMMERS_REPO": f"{monoserver_root}/swimmers",
        "SKILLBOX_SWIMMERS_INSTALL_DIR": f"{home_root}/.local/bin",
        "SKILLBOX_SWIMMERS_BIN": f"{home_root}/.local/bin/swimmers",
        "SKILLBOX_SWIMMERS_DOWNLOAD_URL": "",
        "SKILLBOX_SWIMMERS_DOWNLOAD_SHA256": "",
        "SKILLBOX_SWIMMERS_AUTH_MODE": "",
        "SKILLBOX_SWIMMERS_AUTH_TOKEN": "",
        "SKILLBOX_SWIMMERS_OBSERVER_TOKEN": "",
        "SKILLBOX_DCG_BIN": f"{home_root}/.local/bin/dcg",
        "SKILLBOX_DCG_DOWNLOAD_URL": "",
        "SKILLBOX_DCG_DOWNLOAD_SHA256": "",
        "SKILLBOX_DCG_PACKS": "core.git,core.filesystem",
        "SKILLBOX_RCH_BIN": f"{home_root}/.local/bin/rch",
        "SKILLBOX_RCHD_BIN": f"{home_root}/.local/bin/rchd",
        "SKILLBOX_RCH_WORKER_BIN": f"{home_root}/.local/bin/rch-wkr",
        "SKILLBOX_RCH_WORKERS_CONFIG": f"{home_root}/.config/rch/workers.toml",
        "SKILLBOX_RCH_DOWNLOAD_URL": "",
        "SKILLBOX_RCH_DOWNLOAD_SHA256": "",
        "SKILLBOX_SBH_BIN": f"{home_root}/.local/bin/sbh",
        "SKILLBOX_SBH_CONFIG": f"{home_root}/.config/sbh/config.toml",
        "SKILLBOX_SBH_DOWNLOAD_URL": "",
        "SKILLBOX_SBH_DOWNLOAD_SHA256": "",
        "SKILLBOX_CASS_BIN": f"{home_root}/.local/bin/cass",
        "SKILLBOX_CASS_DOWNLOAD_URL": "",
        "SKILLBOX_CASS_DOWNLOAD_SHA256": "",
        "SKILLBOX_CM_BIN": f"{home_root}/.local/bin/cm",
        "SKILLBOX_CM_DOWNLOAD_URL": "",
        "SKILLBOX_CM_DOWNLOAD_SHA256": "",
        "SKILLBOX_CM_MCP_PORT": "3222",
        "SKILLBOX_UBS_BIN": f"{home_root}/.local/bin/ubs",
        "SKILLBOX_UBS_DOWNLOAD_URL": "",
        "SKILLBOX_UBS_DOWNLOAD_SHA256": "",
        "SKILLBOX_APR_BIN": f"{home_root}/.local/bin/apr",
        "SKILLBOX_APR_DOWNLOAD_URL": "",
        "SKILLBOX_APR_DOWNLOAD_SHA256": "",
        "SKILLBOX_DCG_MCP_PORT": "3220",
        "SKILLBOX_FWC_BIN": f"{home_root}/.local/bin/fwc",
        "SKILLBOX_FWC_DOWNLOAD_URL": "",
        "SKILLBOX_FWC_DOWNLOAD_SHA256": "",
        "SKILLBOX_FWC_MCP_PORT": "3221",
        "SKILLBOX_FWC_ZONE": "work",
        "SKILLBOX_FWC_CONNECTORS": "github,slack,linear",
        "SKILLBOX_PULSE_INTERVAL": "30",
        "SKILLBOX_PULSE_CLIENTS": "",
        "SKILLBOX_PULSE_PROFILES": "",
        "SKILLBOX_PULSE_UNHEALTHY_GRACE_SECONDS": "60",
    }
    runtime_env = {
        key: value
        for key, value in expected_env.items()
        if key not in {"SKILLBOX_NAME", "SKILLBOX_MONOSERVER_HOST_ROOT"}
    }
    # Operators may override runtime env keys via `.env`; docker compose substitutes those values
    # into the rendered config, so reflect the same overrides here so doctor's compose checks
    # verify structural alignment rather than rejecting legitimate local overrides. The
    # `env-defaults` check still validates that `.env.example` itself matches the manifest.
    # The operator `.env` now lives under ${SKILLBOX_STATE_ROOT}/operator/ (out of the
    # workspace bind mount); prefer it, falling back to a legacy repo-root copy. This
    # mirrors the --env-file compose passes in the Makefile and the load_operator_secret
    # resolution order in box.py / operator_mcp_server.py.
    runtime_env.update(load_runtime_env_overrides(_operator_env_path(), runtime_env.keys()))
    # CLIENTS_HOST_ROOT has dual semantics: on the host it's the source path used by
    # client-init scaffolding, but inside the container docker-compose.yml maps it to
    # CLIENTS_ROOT so in-container callers see the same path under either name. The
    # compose-* checks compare against the container view, so force this alignment after
    # the operator overlay.
    runtime_env["SKILLBOX_CLIENTS_HOST_ROOT"] = expected_env["SKILLBOX_CLIENTS_ROOT"]
    state_root = str(storage.get("state_root") or "").strip()

    def compose_mount_source(binding: dict[str, Any]) -> str:
        resolved_host_path = str(binding.get("resolved_host_path") or "").strip()
        storage_class = str(binding.get("storage_class") or "").strip().lower()
        relative_path = str(binding.get("relative_path") or "").strip()
        if storage_class != "external" and state_root and relative_path:
            return str((Path(state_root) / Path(relative_path)).resolve())
        return resolved_host_path

    base_mounts = [
        {
            "source": compose_mount_source(binding),
            "target": str(binding.get("runtime_path")),
        }
        for binding in storage.get("bindings") or []
        if compose_mount_source(binding) and binding.get("runtime_path")
    ]

    # Per-client overrides replace the default /monoserver bind with client-scoped repo mounts.
    monoserver_layer = _resolve_monoserver_layer()
    if monoserver_layer != "docker-compose.monoserver.yml":
        override_path = ROOT_DIR / monoserver_layer
        try:
            override_doc = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
            ws_volumes = (override_doc.get("services", {}).get("workspace", {}).get("volumes") or [])
            base_mounts = [mount for mount in base_mounts if mount["target"] != monoserver_root]
            for vol_str in ws_volumes:
                parts = str(vol_str).split(":", 1)
                if len(parts) == 2:
                    base_mounts.append({"source": parts[0], "target": parts[1]})
        except (OSError, Exception):
            pass

    expected_mounts = base_mounts

    return {
        "root_dir": str(ROOT_DIR),
        "sandbox": {
            "name": sandbox.get("name"),
            "purpose": sandbox.get("purpose"),
            "runtime": runtime,
            "paths": paths,
            "ports": ports,
            "entrypoints": sandbox.get("entrypoints") or [],
        },
        "dependencies": dependencies_doc,
        "storage": storage,
        "env_defaults": env_defaults,
        "expected_env": expected_env,
        "runtime_env": runtime_env,
        "expected_mounts": expected_mounts,
        "skill_sync": {
            "config_file": str(WORKSPACE_DIR / "skill-repos.yaml"),
            "lock_file": str(WORKSPACE_DIR / "skill-repos.lock.json"),
            "clone_root": str(WORKSPACE_DIR / "skill-repos"),
            "declared_skills": declared_skill_names(skill_repos_doc),
            "locked_skills": lockfile_skill_names(skill_repos_lock),
            "config_sha": skill_repos_lock.get("config_sha"),
            "runtime_skillset": default_skillset,
        },
        "runtime_manager": {
            "script": str(ROOT_DIR / ".env-manager" / "manage.py"),
            "manifest_file": runtime_model["manifest_file"],
            "persistence_manifest_file": runtime_model.get("persistence_manifest_file"),
            "clients": runtime_model.get("clients") or [],
            "repos": runtime_model["repos"],
            "skills": runtime_model["skills"],
            "services": runtime_model["services"],
            "logs": runtime_model["logs"],
            "checks": runtime_model["checks"],
        },
    }


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_monoserver_layer() -> str:
    """Return the compose file path for the monoserver layer."""
    focus_path = ROOT_DIR / "workspace" / ".focus.json"
    if focus_path.is_file():
        try:
            focus = json.loads(focus_path.read_text(encoding="utf-8"))
            client_id = focus.get("client_id", "")
            override = ROOT_DIR / "workspace" / ".compose-overrides" / f"docker-compose.client-{client_id}.yml"
            if client_id and override.is_file():
                return str(override.relative_to(ROOT_DIR))
        except (json.JSONDecodeError, OSError):
            pass
    return "docker-compose.monoserver.yml"


def compose_config(include_surfaces: bool, include_swimmers: bool = False) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError("`docker` is not installed or not on PATH")

    monoserver_layer = _resolve_monoserver_layer()
    args = ["docker", "compose", "-f", "docker-compose.yml", "-f", monoserver_layer]
    if include_swimmers:
        args.extend(["-f", "docker-compose.swimmers.yml"])
    if include_surfaces:
        args.extend(["--profile", "surfaces"])
    args.extend(["config", "--format", "json"])

    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "docker compose config failed")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse docker compose JSON output: {exc}") from exc


def volume_map(service: dict[str, Any]) -> dict[str, str]:
    volumes = service.get("volumes") or []
    return {item.get("target", ""): item.get("source", "") for item in volumes}


def runtime_env_view(environment: dict[str, Any], expected_runtime_env: dict[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in (environment or {}).items()
        if key in expected_runtime_env
    }


def expected_runtime_paths(model: dict[str, Any]) -> dict[str, str]:
    paths = model["sandbox"]["paths"]
    return {
        "claude-config": str(paths.get("claude_root")),
        "codex-config": str(paths.get("codex_root")),
        "sandbox-root": str(paths.get("repos_root")),
        "monoserver-root": str(paths.get("monoserver_root")),
        "local-skills": str(paths.get("skills_root")),
    }


def check_required_files() -> CheckResult:
    """Assert every resolved expected file (CORE + DERIVED) exists, naming the
    PROVENANCE of any missing one so the operator knows WHY it was expected.

    A missing DERIVED file means a real reference (Makefile/install.sh/compose/
    Dockerfile/manifest) still points at a file that is gone — fix the rename at
    its source, not by patching this list.
    """
    expected = resolved_expected_files(ROOT_DIR)
    missing = [entry for entry in expected if not (ROOT_DIR / entry.path).is_file()]
    if missing:
        return CheckResult(
            status="fail",
            code="required-files",
            message="required files are missing",
            details={
                "missing": [entry.path for entry in missing],
                "missing_with_provenance": [
                    f"{entry.path} ({entry.provenance})" for entry in missing
                ],
            },
        )
    return CheckResult(
        status="pass",
        code="required-files",
        message="required files are present",
        details={"checked": len(expected)},
    )


def check_expected_files_sources() -> CheckResult:
    """Doctor self-check for the expected-files derivation itself.

    Two failure classes:

    1. A static CORE entry is missing on disk — report the exact CORE tuple to
       delete (the stale instance) so the rot is fixable without spelunking.
    2. A DERIVED entry's source no longer PARSES as expected (the source file
       vanished while the derivation still names it) — surfaced as a warning so
       a half-applied rename is visible before it silently drops coverage.
    """
    issues: list[str] = []
    warnings: list[str] = []

    for path, _reason in CORE_EXPECTED_FILES:
        if not (ROOT_DIR / path).is_file():
            issues.append(
                f"{path}: stale CORE entry — file is gone; delete its tuple from "
                "CORE_EXPECTED_FILES in scripts/04-reconcile.py"
            )

    derived = derive_expected_files(ROOT_DIR)
    for entry in derived:
        if not (ROOT_DIR / entry.source).is_file():
            warnings.append(
                f"{entry.path}: derivation source {entry.source} is missing "
                f"({entry.provenance})"
            )

    if issues:
        return CheckResult(
            status="fail",
            code="expected-files-sources",
            message="the expected-files CORE list has stale entries",
            details={"stale_core": issues, "stale_sources": warnings},
            fix_command="python3 scripts/04-reconcile.py render --format json",
        )
    if warnings:
        return CheckResult(
            status="warn",
            code="expected-files-sources",
            message="an expected-files derivation source could not be resolved",
            details={"stale_sources": warnings},
            fix_command="python3 scripts/04-reconcile.py render --format json",
        )
    return CheckResult(
        status="pass",
        code="expected-files-sources",
        message="expected-files CORE list and derivation sources resolve cleanly",
        details={"core": len(CORE_EXPECTED_FILES), "derived": len(derived)},
    )


def check_expected_directories() -> CheckResult:
    missing = [path for path in EXPECTED_DIRECTORIES if not (ROOT_DIR / path).is_dir()]
    if missing:
        return CheckResult(
            status="fail",
            code="expected-directories",
            message="expected workspace directories are missing",
            details={"missing": missing},
            fix_command=f"mkdir -p {' '.join(missing)}",
        )
    return CheckResult(status="pass", code="expected-directories", message="expected workspace directories are present")


def check_manifest_alignment(model: dict[str, Any]) -> CheckResult:
    issues: list[str] = []
    dependencies = model["dependencies"]
    paths = model["sandbox"]["paths"]
    dependency_paths = expected_runtime_paths(model)

    home_mounts = {item.get("id"): item.get("path") for item in dependencies.get("home_mounts") or []}
    repo_workspaces = {item.get("id"): item.get("path") for item in dependencies.get("repo_workspaces") or []}
    skill_roots = {item.get("id"): item.get("path") for item in dependencies.get("skill_roots") or []}

    if home_mounts.get("claude-config") != dependency_paths["claude-config"]:
        issues.append("home_mounts.claude-config does not match sandbox.paths.claude_root")
    if home_mounts.get("codex-config") != dependency_paths["codex-config"]:
        issues.append("home_mounts.codex-config does not match sandbox.paths.codex_root")
    if repo_workspaces.get("sandbox-root") != dependency_paths["sandbox-root"]:
        issues.append("repo_workspaces.sandbox-root does not match sandbox.paths.repos_root")
    if repo_workspaces.get("monoserver-root") != dependency_paths["monoserver-root"]:
        issues.append("repo_workspaces.monoserver-root does not match sandbox.paths.monoserver_root")
    if skill_roots.get("local-skills") != dependency_paths["local-skills"]:
        issues.append("skill_roots.local-skills does not match sandbox.paths.skills_root")

    skillset = model["skill_sync"]["runtime_skillset"] or {}
    expected_config_path = f"{paths.get('workspace_root')}/workspace/skill-repos.yaml"
    expected_lock_path = f"{paths.get('workspace_root')}/workspace/skill-repos.lock.json"
    expected_clone_root = f"{paths.get('workspace_root')}/workspace/skill-repos"
    if not skillset:
        issues.append("runtime.yaml is missing the default-skills skill-repo-set")
    else:
        if skillset.get("kind") != "skill-repo-set":
            issues.append("runtime.skills.default-skills kind is not skill-repo-set")
        if skillset.get("skill_repos_config") != expected_config_path:
            issues.append("runtime.skills.default-skills skill_repos_config does not match workspace_root")
        if skillset.get("lock_path") != expected_lock_path:
            issues.append("runtime.skills.default-skills lock_path does not match workspace_root")
        if skillset.get("clone_root") != expected_clone_root:
            issues.append("runtime.skills.default-skills clone_root does not match workspace_root")
        if (skillset.get("sync") or {}).get("mode") != "clone-and-install":
            issues.append("runtime.skills.default-skills sync.mode is not clone-and-install")

    if issues:
        return CheckResult(
            status="fail",
            code="manifest-alignment",
            message="manifest files disagree on runtime paths",
            details={"issues": issues},
            fix_command=DRIFT_FIX_COMMAND,
        )
    return CheckResult(status="pass", code="manifest-alignment", message="manifest files agree on runtime paths")


def check_env_defaults(model: dict[str, Any]) -> CheckResult:
    mismatches: list[str] = []
    env_defaults = model["env_defaults"]
    for key, expected in model["expected_env"].items():
        actual = env_defaults.get(key)
        if actual != expected:
            mismatches.append(f"{key}: expected {expected!r}, found {actual!r}")

    if mismatches:
        return CheckResult(
            status="fail",
            code="env-defaults",
            message=".env.example is out of sync with the manifest",
            details={"mismatches": mismatches},
            fix_command=DRIFT_FIX_COMMAND,
        )
    return CheckResult(status="pass", code="env-defaults", message=".env.example matches manifest defaults")


def _compose_config_failure(exc: RuntimeError) -> list[CheckResult]:
    return [
        CheckResult(
            status="fail",
            code="compose-config",
            message="docker compose config could not be resolved",
            details={"error": str(exc)},
            fix_command="docker compose config",
        )
    ]


def _compose_config_success() -> CheckResult:
    return CheckResult(
        status="pass",
        code="compose-config",
        message="docker compose config resolved for default, surfaces, and swimmers overlay variants",
    )


def _workspace_compose_issues(base_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if base_config.get("name") != model["expected_env"]["SKILLBOX_NAME"]:
        issues.append("compose project name does not match sandbox.name / SKILLBOX_NAME")
    if (base_config.get("x-runtime-env") or {}) != model["runtime_env"]:
        issues.append("x-runtime-env does not match manifest-derived runtime env")

    workspace = (base_config.get("services") or {}).get("workspace") or {}
    if workspace.get("working_dir") != model["sandbox"]["paths"].get("workspace_root"):
        issues.append("workspace working_dir does not match sandbox.paths.workspace_root")
    if workspace.get("tty") is not True:
        issues.append("workspace tty should be enabled")
    if workspace.get("stdin_open") is not True:
        issues.append("workspace stdin_open should be enabled")
    if runtime_env_view(workspace.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
        issues.append("workspace environment does not match manifest-derived runtime env")

    mounts = volume_map(workspace)
    for expected_mount in model["expected_mounts"]:
        target = expected_mount["target"]
        source = expected_mount["source"]
        if mounts.get(target) != source:
            issues.append(f"workspace bind mount {source} -> {target} is missing or different")
    return issues


def _surface_compose_issues(surfaces_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for service_name, port_key in (("api", "api"), ("web", "web")):
        service = (surfaces_config.get("services") or {}).get(service_name) or {}
        if service.get("profiles") != ["surfaces"]:
            issues.append(f"{service_name} service is not scoped to the surfaces profile")
        if runtime_env_view(service.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
            issues.append(f"{service_name} environment does not match manifest-derived runtime env")

        expected_port = int(model["sandbox"]["ports"].get(port_key))
        ports = service.get("ports") or []
        if len(ports) != 1:
            issues.append(f"{service_name} should publish exactly one port")
            continue

        published = ports[0]
        if published.get("host_ip") != "127.0.0.1":
            issues.append(f"{service_name} should bind only to 127.0.0.1")
        if int(published.get("target", 0)) != expected_port:
            issues.append(f"{service_name} container port does not match sandbox.ports.{port_key}")
        if str(published.get("published")) != str(expected_port):
            issues.append(f"{service_name} published port does not match sandbox.ports.{port_key}")
    return issues


def _swimmers_compose_issues(swimmers_config: dict[str, Any], model: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    swimmers_workspace = (swimmers_config.get("services") or {}).get("workspace") or {}
    if runtime_env_view(swimmers_workspace.get("environment") or {}, model["runtime_env"]) != model["runtime_env"]:
        issues.append("swimmers workspace environment does not match manifest-derived runtime env")

    expected_swimmers_port = int(model["sandbox"]["ports"].get("swimmers"))
    expected_swimmers_host = model["expected_env"]["SKILLBOX_SWIMMERS_PUBLISH_HOST"]
    swimmers_ports = swimmers_workspace.get("ports") or []
    if len(swimmers_ports) != 1:
        issues.append("swimmers workspace overlay should publish exactly one port")
        return issues

    published = swimmers_ports[0]
    if published.get("host_ip") != expected_swimmers_host:
        issues.append("swimmers workspace overlay host_ip does not match SKILLBOX_SWIMMERS_PUBLISH_HOST")
    if int(published.get("target", 0)) != expected_swimmers_port:
        issues.append("swimmers workspace overlay target port does not match sandbox.ports.swimmers")
    if str(published.get("published")) != str(expected_swimmers_port):
        issues.append("swimmers workspace overlay published port does not match sandbox.ports.swimmers")
    return issues


def _compose_check_result(code: str, success_message: str, failure_message: str, issues: list[str]) -> CheckResult:
    if issues:
        return CheckResult(
            status="fail",
            code=code,
            message=failure_message,
            details={"issues": issues},
            fix_command=DRIFT_FIX_COMMAND,
        )
    return CheckResult(status="pass", code=code, message=success_message)


def check_compose_model(model: dict[str, Any]) -> list[CheckResult]:
    try:
        base_config = compose_config(include_surfaces=False)
        surfaces_config = compose_config(include_surfaces=True)
        swimmers_config = compose_config(include_surfaces=False, include_swimmers=True)
    except RuntimeError as exc:
        return _compose_config_failure(exc)

    return [
        _compose_config_success(),
        _compose_check_result(
            "compose-workspace",
            "workspace service matches manifest-derived env and mounts",
            "workspace service drifted from the manifests",
            _workspace_compose_issues(base_config, model),
        ),
        _compose_check_result(
            "compose-surfaces",
            "api/web services match manifest-derived env, profile, and ports",
            "api/web surface services drifted from the manifests",
            _surface_compose_issues(surfaces_config, model),
        ),
        _compose_check_result(
            "compose-swimmers",
            "workspace swimmers overlay matches manifest-derived env and port publishing",
            "workspace swimmers overlay drifted from the manifests",
            _swimmers_compose_issues(swimmers_config, model),
        ),
    ]


def _secret_migration_fix_command(exposed: list[str]) -> str:
    """Build the exact (manual) migration command for the exposed secret files."""
    parts = ["mkdir -p ./.skillbox-state/operator"]
    for name in OPERATOR_SECRET_FILENAMES:
        if name in exposed:
            parts.append(f"mv ./{name} ./.skillbox-state/operator/{name}")
    return " && ".join(parts)


def check_secrets_visible_in_workspace() -> CheckResult:
    """Fail if any operator secret file sits directly under a bind-mounted host dir.

    Parses `docker compose config` and inspects every bind-mount host source path.
    For the `.:/workspace` mount the host source is ROOT_DIR, so this catches
    ROOT_DIR/.env and ROOT_DIR/.env.box. NEVER moves files automatically — on a
    failure it only emits the manual migration command.
    """
    try:
        config = compose_config(include_surfaces=False)
    except RuntimeError as exc:
        return CheckResult(
            status="fail",
            code="secrets-visible-in-workspace",
            message="docker compose config could not be resolved to check secret exposure",
            details={"error": str(exc)},
            fix_command="docker compose config",
        )

    host_sources: set[Path] = set()
    for service in (config.get("services") or {}).values():
        for volume in service.get("volumes") or []:
            if volume.get("type") != "bind":
                continue
            source = volume.get("source")
            if not source:
                continue
            try:
                host_sources.add(Path(source).resolve())
            except (OSError, ValueError):
                continue

    exposed: list[str] = []
    for host_dir in host_sources:
        if not host_dir.is_dir():
            continue
        for name in OPERATOR_SECRET_FILENAMES:
            if (host_dir / name).is_file() and name not in exposed:
                exposed.append(name)

    if exposed:
        return CheckResult(
            status="fail",
            code="secrets-visible-in-workspace",
            message=(
                "operator secret files are readable by in-container agents — they sit "
                "inside a bind-mounted host directory"
            ),
            details={"exposed": exposed},
            fix_command=_secret_migration_fix_command(exposed),
        )

    return CheckResult(
        status="pass",
        code="secrets-visible-in-workspace",
        message="no operator secret files are exposed inside workspace bind mounts",
        details={"exposed": []},
    )


def check_skill_sync_dry_run(model: dict[str, Any]) -> CheckResult:
    result = run_command(["python3", ".env-manager/manage.py", "sync", "--dry-run", "--format", "json"])
    if result.returncode != 0:
        return CheckResult(
            status="fail",
            code="skill-repo-sync-dry-run",
            message="manage.py sync --dry-run failed for the default skill-repo-set",
            details={
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
            fix_command=SKILL_SYNC_FIX_COMMAND,
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            status="fail",
            code="skill-repo-sync-dry-run",
            message="manage.py sync --dry-run emitted invalid JSON",
            details={"error": str(exc)},
            fix_command=SKILL_SYNC_DRY_RUN_COMMAND,
        )

    actions = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(actions, list):
        return CheckResult(
            status="fail",
            code="skill-repo-sync-dry-run",
            message="manage.py sync --dry-run emitted an unexpected JSON shape",
            details={"payload_type": type(payload).__name__},
            fix_command=SKILL_SYNC_DRY_RUN_COMMAND,
        )

    return CheckResult(
        status="pass",
        code="skill-repo-sync-dry-run",
        message="manage.py sync --dry-run can resolve the configured default skill-repo-set",
        details={"preview": actions[:4]},
    )


def check_bundle_state(model: dict[str, Any]) -> CheckResult:
    lock_path = Path(model["skill_sync"]["lock_file"])
    expected = set(model["skill_sync"]["declared_skills"])
    locked = set(model["skill_sync"]["locked_skills"])
    if not lock_path.is_file():
        return CheckResult(
            status="warn",
            code="skill-repo-lock-state",
            message="workspace skill repo lockfile is missing",
            details={"expected_path": repo_rel(lock_path)},
            fix_command=SKILL_SYNC_FIX_COMMAND,
        )

    missing = sorted(expected - locked)
    extra = sorted(locked - expected)
    if missing or extra:
        return CheckResult(
            status="warn",
            code="skill-repo-lock-state",
            message="workspace skill repo lockfile does not exactly match the declared picks",
            details={"missing": missing, "extra": extra},
            fix_command=SKILL_SYNC_FIX_COMMAND,
        )

    return CheckResult(
        status="pass",
        code="skill-repo-lock-state",
        message="workspace skill repo lockfile matches the declared picks",
        details={"skills": sorted(locked)},
    )


def check_beads_state() -> CheckResult:
    beads_dir = ROOT_DIR / ".beads"
    db_path = beads_dir / "beads.db"
    jsonl_path = beads_dir / "issues.jsonl"

    if not beads_dir.is_dir():
        return CheckResult(
            status="warn",
            code="beads-state",
            message="Beads issue state is not initialized",
            details={"expected_path": ".beads"},
            fix_command=BEADS_INIT_FIX_COMMAND,
        )

    if not db_path.is_file():
        return CheckResult(
            status="warn",
            code="beads-state",
            message="Beads database is missing",
            details={"expected_path": repo_rel(db_path)},
            fix_command=BEADS_INIT_FIX_COMMAND,
        )

    if not jsonl_path.is_file():
        return CheckResult(
            status="warn",
            code="beads-state",
            message="Beads JSONL export is missing",
            details={"expected_path": repo_rel(jsonl_path)},
            fix_command=BEADS_SYNC_FIX_COMMAND,
        )

    if shutil.which("br"):
        result = run_command(["br", "sync", "--status", "--json"])
        if result.returncode != 0:
            return CheckResult(
                status="warn",
                code="beads-state",
                message="Beads sync status could not be checked",
                details={
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                },
                fix_command=BEADS_SYNC_STATUS_COMMAND,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return CheckResult(
                status="warn",
                code="beads-state",
                message="Beads sync status emitted invalid JSON",
                details={"error": str(exc)},
                fix_command=BEADS_SYNC_STATUS_COMMAND,
            )

        dirty_count = int(payload.get("dirty_count") or 0) if isinstance(payload, dict) else 0
        db_newer = bool(payload.get("db_newer")) if isinstance(payload, dict) else False
        jsonl_exists = bool(payload.get("jsonl_exists", True)) if isinstance(payload, dict) else True
        if dirty_count or db_newer or not jsonl_exists:
            return CheckResult(
                status="warn",
                code="beads-state",
                message="Beads JSONL export is not synced with the local database",
                details={
                    "dirty_count": dirty_count,
                    "db_newer": db_newer,
                    "jsonl_exists": jsonl_exists,
                },
                fix_command=BEADS_SYNC_FIX_COMMAND,
            )
        return CheckResult(
            status="pass",
            code="beads-state",
            message="Beads issue state is initialized and synced",
            details={
                "jsonl": repo_rel(jsonl_path),
                "dirty_count": dirty_count,
                "last_export_time": payload.get("last_export_time") if isinstance(payload, dict) else None,
            },
        )

    db_mtime = db_path.stat().st_mtime
    jsonl_mtime = jsonl_path.stat().st_mtime
    if db_mtime - jsonl_mtime > 2:
        return CheckResult(
            status="warn",
            code="beads-state",
            message="Beads database appears newer than the JSONL export",
            details={
                "database": repo_rel(db_path),
                "jsonl": repo_rel(jsonl_path),
            },
            fix_command=BEADS_SYNC_FIX_COMMAND,
        )

    return CheckResult(
        status="pass",
        code="beads-state",
        message="Beads issue state is initialized",
        details={"jsonl": repo_rel(jsonl_path)},
    )


def check_reference_drift() -> CheckResult:
    hits: list[str] = []
    ignored_prefixes = {
        ".cache",
        "logs",
    }
    ignored_files = {
        "scripts/04-reconcile.py",
    }

    for path in ROOT_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = repo_rel(path)
        if rel in ignored_files:
            continue
        if any(rel == prefix or rel.startswith(f"{prefix}/") for prefix in ignored_prefixes):
            continue
        if path.suffix == ".skill":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines, start=1):
            if "00-skill-sync.sh" in line:
                hits.append(f"{rel}:{index}")

    if hits:
        return CheckResult(
            status="fail",
            code="reference-drift",
            message="stale references to 00-skill-sync.sh remain in the repo",
            details={"hits": hits},
            fix_command='rg "00-skill-sync.sh" .',
        )
    return CheckResult(
        status="pass",
        code="reference-drift",
        message="no stale 00-skill-sync.sh references were found",
    )


def check_runtime_manager_model(model: dict[str, Any]) -> CheckResult:
    runtime_manager = model["runtime_manager"]
    return CheckResult(
        status="pass",
        code="runtime-manager-model",
        message="internal runtime manager manifest resolved successfully",
        details={
            "manifest": repo_rel(Path(runtime_manager["manifest_file"])),
            "persistence_manifest": repo_rel(Path(runtime_manager["persistence_manifest_file"])),
            "clients": len(runtime_manager.get("clients") or []),
            "repos": len(runtime_manager["repos"]),
            "skills": len(runtime_manager["skills"]),
            "services": len(runtime_manager["services"]),
            "logs": len(runtime_manager["logs"]),
            "checks": len(runtime_manager["checks"]),
            "storage_bindings": len(model.get("storage", {}).get("bindings") or []),
        },
    )


def check_runtime_manager_doctor() -> CheckResult:
    result = run_command(["python3", ".env-manager/manage.py", "doctor", "--format", "json"])
    if result.returncode != 0:
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor failed",
            details={
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
            fix_command=RUNTIME_DOCTOR_COMMAND,
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor emitted invalid JSON",
            details={"error": str(exc)},
            fix_command=RUNTIME_DOCTOR_COMMAND,
        )

    items = payload.get("checks", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return CheckResult(
            status="fail",
            code="runtime-manager-doctor",
            message="internal runtime manager doctor emitted an unexpected JSON shape",
            details={"payload_type": type(payload).__name__},
            fix_command=RUNTIME_DOCTOR_COMMAND,
        )

    warnings = sum(1 for item in items if item.get("status") == "warn")
    details = {"warnings": warnings}
    if warnings:
        details["warning_codes"] = [item.get("code") for item in items if item.get("status") == "warn"]

    return CheckResult(
        status="pass",
        code="runtime-manager-doctor",
        message="internal runtime manager doctor completed without failures",
        details=details,
    )


def compose_summary(model: dict[str, Any]) -> dict[str, Any]:
    base_config = compose_config(include_surfaces=False)
    surfaces_config = compose_config(include_surfaces=True)
    swimmers_config = compose_config(include_surfaces=False, include_swimmers=True)

    def service_summary(service: dict[str, Any]) -> dict[str, Any]:
        return {
            "working_dir": service.get("working_dir"),
            "environment": service.get("environment") or {},
            "volumes": volume_map(service),
            "ports": service.get("ports") or [],
            "profiles": service.get("profiles") or [],
        }

    services = base_config.get("services") or {}
    surfaces_services = surfaces_config.get("services") or {}

    return {
        "project_name": base_config.get("name"),
        "workspace": service_summary(services.get("workspace") or {}),
        "workspace_swimmers": service_summary((swimmers_config.get("services") or {}).get("workspace") or {}),
        "api": service_summary(surfaces_services.get("api") or {}),
        "web": service_summary(surfaces_services.get("web") or {}),
        "expected_runtime_env": model["runtime_env"],
    }


def expected_files_payload() -> list[dict[str, str]]:
    """The resolved expected-files list with provenance, for the render surface.

    Inspectable so an operator can see exactly which files doctor will require
    and WHY each one is expected (core justification or derivation source).
    """
    return [
        {"path": entry.path, "provenance": entry.provenance, "source": entry.source}
        for entry in resolved_expected_files(ROOT_DIR)
    ]


def build_render_payload(with_compose: bool) -> dict[str, Any]:
    model = build_model()
    payload = {
        "sandbox": model["sandbox"],
        "expected_env": model["expected_env"],
        "expected_files": expected_files_payload(),
        "expected_mounts": model["expected_mounts"],
        "dependencies": model["dependencies"],
        "storage": model["storage"],
        "skill_sync": model["skill_sync"],
        "runtime_manager": model["runtime_manager"],
    }
    if with_compose:
        payload["compose"] = compose_summary(model)
    return payload


def print_render_text(payload: dict[str, Any]) -> None:
    sandbox = payload["sandbox"]
    print(f"sandbox: {sandbox.get('name')}")
    print(f"purpose: {sandbox.get('purpose')}")
    runtime = sandbox.get("runtime") or {}
    print(f"runtime: {runtime.get('mode')} as {runtime.get('agent_user')}")
    print(f"entrypoints: {', '.join(sandbox.get('entrypoints') or [])}")
    print()
    print("env defaults:")
    for key, value in payload["expected_env"].items():
        print(f"  {key}={value}")
    if payload.get("expected_files"):
        print()
        print("expected files:")
        for entry in payload["expected_files"]:
            print(f"  {entry['path']}  <- {entry['provenance']}")
    print()
    print("expected mounts:")
    for mount in payload["expected_mounts"]:
        print(f"  {mount['source']} -> {mount['target']}")
    print()
    storage = payload.get("storage") or {}
    print("storage:")
    print(f"  provider: {storage.get('provider') or 'unknown'}")
    print(f"  state_root: {storage.get('state_root') or 'unknown'}")
    print(f"  filesystem: {storage.get('filesystem') or '(unset)'}")
    print(f"  required: {storage.get('required')}")
    print(f"  min_free_gb: {storage.get('min_free_gb')}")
    for binding in storage.get("bindings") or []:
        print(
            "  "
            f"{binding.get('id')}: "
            f"{binding.get('resolved_host_path')} -> {binding.get('runtime_path')} "
            f"({binding.get('storage_class')})"
        )
    print()
    print("skill sync:")
    skill_sync = payload["skill_sync"]
    print(f"  config: {repo_rel(Path(skill_sync['config_file']))}")
    print(f"  lockfile: {repo_rel(Path(skill_sync['lock_file']))}")
    print(f"  clone root: {repo_rel(Path(skill_sync['clone_root']))}")
    print(f"  declared skills: {', '.join(skill_sync['declared_skills']) or '(none)'}")
    print(f"  locked skills: {', '.join(skill_sync['locked_skills']) or '(none)'}")
    print()
    print("runtime manager:")
    runtime_manager = payload["runtime_manager"]
    print(f"  script: {repo_rel(Path(runtime_manager['script']))}")
    print(f"  manifest: {repo_rel(Path(runtime_manager['manifest_file']))}")
    if runtime_manager.get("persistence_manifest_file"):
        print(f"  persistence: {repo_rel(Path(runtime_manager['persistence_manifest_file']))}")
    print(f"  clients: {len(runtime_manager.get('clients') or [])}")
    print(f"  repos: {len(runtime_manager['repos'])}")
    print(f"  skills: {len(runtime_manager['skills'])}")
    print(f"  services: {len(runtime_manager['services'])}")
    print(f"  logs: {len(runtime_manager['logs'])}")
    print(f"  checks: {len(runtime_manager['checks'])}")
    if "compose" in payload:
        compose = payload["compose"]
        print()
        print("compose:")
        print(f"  project: {compose.get('project_name')}")
        print(f"  workspace working_dir: {compose['workspace'].get('working_dir')}")
        print(f"  swimmers workspace ports: {compose['workspace_swimmers'].get('ports')}")
        print(f"  api ports: {compose['api'].get('ports')}")
        print(f"  web ports: {compose['web'].get('ports')}")


def detail_lines(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            joined = ", ".join(str(item) for item in value)
            lines.append(f"{key}: {joined}")
        else:
            lines.append(f"{key}: {value}")
    return lines


def print_doctor_text(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status.upper():4} {result.code}: {result.message}")
        if result.details:
            for line in detail_lines(result.details):
                print(f"     {line}")
        if result.fix_command:
            print(f"     fix: {result.fix_command}")

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


def doctor_results(skip_compose: bool, skip_skill_sync: bool) -> list[CheckResult]:
    model = build_model()
    results = [
        check_required_files(),
        check_expected_files_sources(),
        check_expected_directories(),
        check_manifest_alignment(model),
        check_env_defaults(model),
        check_bundle_state(model),
        check_beads_state(),
        check_reference_drift(),
        check_runtime_manager_model(model),
        check_runtime_manager_doctor(),
    ]

    if skip_compose:
        results.append(
            CheckResult(
                status="warn",
                code="compose-config",
                message="compose validation skipped",
                fix_command="python3 scripts/04-reconcile.py doctor --format json",
            )
        )
        results.append(
            CheckResult(
                status="warn",
                code="secrets-visible-in-workspace",
                message="workspace secret-exposure check skipped (needs docker compose config)",
                fix_command="python3 scripts/04-reconcile.py doctor --format json",
            )
        )
    else:
        results.extend(check_compose_model(model))
        results.append(check_secrets_visible_in_workspace())

    if skip_skill_sync:
        results.append(
            CheckResult(
                status="warn",
                code="skill-repo-sync-dry-run",
                message="skill-repo sync dry run skipped",
                details={
                    "command": (
                        SKILL_SYNC_DRY_RUN_COMMAND
                    )
                },
                fix_command=SKILL_SYNC_DRY_RUN_COMMAND,
            )
        )
    else:
        results.append(check_skill_sync_dry_run(model))

    return results


def _agent_command(name: str) -> dict[str, Any]:
    safe_first_try = {
        "capabilities": "python3 scripts/04-reconcile.py capabilities --json",
        "doctor": "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
        "render": "python3 scripts/04-reconcile.py render --format json",
        "robot-docs": "python3 scripts/04-reconcile.py robot-docs guide",
        "robot-triage": "python3 scripts/04-reconcile.py --robot-triage",
    }[name]
    return {
        "name": name,
        "json": True,
        "safe_first_try": safe_first_try,
    }


def capabilities_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "skillbox-reconcile",
        "contract_version": "2026-05-11",
        "root_dir": str(ROOT_DIR),
        "entrypoint": "python3 scripts/04-reconcile.py",
        "commands": [_agent_command(name) for name in sorted(RECONCILE_COMMAND_NAMES)],
        "agent_surfaces": {
            "capabilities": "python3 scripts/04-reconcile.py capabilities --json",
            "robot_docs": "python3 scripts/04-reconcile.py robot-docs guide",
            "robot_triage": "python3 scripts/04-reconcile.py --robot-triage",
            "json_aliases": sorted(JSON_FLAG_ALIASES),
        },
        "stdout_stderr_contract": {
            "json_stdout": "When JSON is requested, stdout is parseable JSON only.",
            "diagnostics_stderr": "JSON typo alias notices and parser errors go to stderr.",
        },
        "doctor_remediation": {
            "json_field": "fix_command",
            "text_prefix": "fix:",
            "extract_commands": (
                "python3 scripts/04-reconcile.py doctor --format json "
                "| python3 -c 'import json,sys; "
                "print(\"\\n\".join(item[\"fix_command\"] for item in json.load(sys.stdin) "
                "if item.get(\"status\") != \"pass\" and item.get(\"fix_command\")))'"
            ),
        },
        "safe_previews": [
            "python3 scripts/04-reconcile.py render --format json",
            "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
            "python3 .env-manager/manage.py sync --dry-run --format json",
        ],
        "exit_codes": {
            "0": "success",
            "1": "validation failure or runtime/environment error",
            "2": "argparse usage error",
        },
        "next_actions": [
            "python3 scripts/04-reconcile.py render --format json",
            "python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync",
            "python3 scripts/04-reconcile.py robot-docs guide",
        ],
    }


def robot_docs_guide() -> str:
    return """Skillbox reconcile agent guide

Primary entrypoint:
  python3 scripts/04-reconcile.py <command> [options]

Start here:
  python3 scripts/04-reconcile.py capabilities --json
  python3 scripts/04-reconcile.py render --format json
  python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync
  python3 scripts/04-reconcile.py --robot-triage

Structured output:
  render, doctor, capabilities, and robot-triage support JSON output.
  Agent-friendly aliases are accepted: --json, --jason, --jsno, --jsson.
  Diagnostics and typo-alias notices are printed to stderr, not stdout.
  Doctor findings include fix_command when a check has a copy-pasteable next command.

Safe validation pattern:
  Use doctor --format json --skip-compose --skip-skill-sync for a fast read-side
  check, then run full doctor when Docker and manage.py dry-run checks are safe.
  The skill sync check is a dry-run command:
  python3 .env-manager/manage.py sync --dry-run --format json
"""


def robot_triage_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "skillbox-reconcile",
        "quick_ref": capabilities_payload()["next_actions"],
        "recommendations": [
            {
                "id": "render-model",
                "command": "python3 scripts/04-reconcile.py render --format json",
                "why": "Fastest read-only outer model inspection.",
            },
            {
                "id": "fast-doctor",
                "command": (
                    "python3 scripts/04-reconcile.py doctor --format json "
                    "--skip-compose --skip-skill-sync"
                ),
                "why": "Separates manifest drift from Docker and runtime dry-run availability.",
            },
            {
                "id": "full-doctor",
                "command": "python3 scripts/04-reconcile.py doctor --format json",
                "why": "Runs the full outer validation once side effects are acceptable.",
            },
        ],
    }


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _suggest_command(message: str) -> str | None:
    marker = "invalid choice: '"
    if marker not in message:
        return None
    bad = message.split(marker, 1)[1].split("'", 1)[0]
    matches = difflib.get_close_matches(bad, sorted(RECONCILE_COMMAND_NAMES), n=1)
    return matches[0] if matches else None


class ReconcileArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse exits
        suggestion = _suggest_command(message)
        if suggestion:
            message = (
                f"{message}\nDid you mean: `{self.prog} {suggestion}`?\n"
                f"Discover commands: `{self.prog} capabilities --json`."
            )
        super().error(message)


def _normalize_agent_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    diagnostics: list[str] = []
    command_seen = False
    pending_json = False
    for token in argv:
        if token == "--robot-help":
            normalized.extend(["robot-docs", "guide"])
            command_seen = True
            continue
        if token == "--robot-triage":
            normalized.append("robot-triage")
            command_seen = True
            continue
        if token in JSON_FLAG_ALIASES:
            if token != "--json":
                diagnostics.append(
                    f"Interpreting {token} as --format json. "
                    "Exact command: 04-reconcile.py <command> --format json"
                )
            if command_seen:
                normalized.extend(["--format", "json"])
            else:
                pending_json = True
            continue
        if not token.startswith("-") and not command_seen:
            command_seen = True
            normalized.append(token)
            if pending_json:
                normalized.extend(["--format", "json"])
                pending_json = False
            continue
        normalized.append(token)
    if pending_json and not command_seen:
        normalized.extend(["doctor", "--format", "json"])
    return normalized, diagnostics


def build_parser() -> argparse.ArgumentParser:
    parser = ReconcileArgumentParser(
        prog="04-reconcile.py",
        description="Render and validate the skillbox sandbox model.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities_parser = subparsers.add_parser(
        "capabilities",
        help="Print the machine-readable agent contract.",
    )
    capabilities_parser.add_argument("--format", choices=("json",), default="json")

    robot_docs_parser = subparsers.add_parser(
        "robot-docs",
        help="Print agent-facing command guidance.",
    )
    robot_docs_parser.add_argument("topic", nargs="?", default="guide", choices=("guide",))
    robot_docs_parser.add_argument("--format", choices=("text", "json"), default="text")

    robot_triage_parser = subparsers.add_parser(
        "robot-triage",
        help="Print compact machine-readable first actions.",
    )
    robot_triage_parser.add_argument("--format", choices=("json",), default="json")

    render_parser = subparsers.add_parser("render", help="Print the resolved sandbox model.")
    render_parser.add_argument("--format", choices=("text", "json"), default="text")
    render_parser.add_argument(
        "--with-compose",
        action="store_true",
        help="Include resolved docker compose summary information.",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Validate manifest/runtime drift.")
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    doctor_parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="Skip docker compose validation.",
    )
    doctor_parser.add_argument(
        "--skip-skill-sync",
        action="store_true",
        help="Skip the manage.py sync --dry-run check for the default skill-repo-set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    normalized_argv, diagnostics = _normalize_agent_argv(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalized_argv)
    for diagnostic in diagnostics:
        print(diagnostic, file=sys.stderr)

    try:
        if args.command == "capabilities":
            emit_json(capabilities_payload())
            return 0

        if args.command == "robot-docs":
            guide = robot_docs_guide()
            if args.format == "json":
                emit_json({"ok": True, "topic": args.topic, "guide": guide})
            else:
                print(guide.rstrip())
            return 0

        if args.command == "robot-triage":
            emit_json(robot_triage_payload())
            return 0

        if args.command == "render":
            payload = build_render_payload(with_compose=args.with_compose)
            if args.format == "json":
                emit_json(payload)
            else:
                print_render_text(payload)
            return 0

        results = doctor_results(skip_compose=args.skip_compose, skip_skill_sync=args.skip_skill_sync)
        if args.format == "json":
            emit_json([asdict(result) for result in results])
        else:
            print_doctor_text(results)
        return 1 if any(result.status == "fail" for result in results) else 0
    except RuntimeError as exc:
        print(f"04-reconcile.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
