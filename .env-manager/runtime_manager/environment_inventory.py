"""Versioned, sanitized environment-inventory contract for external consumers.

Skillbox owns machine / client / repo *intent* and root translation:

* ``skillbox-config/machines.yaml``          -- machine identity + repo roots
  (loaded by :mod:`runtime_manager.machines`, which also owns translation),
* ``skillbox-config/registry/repos.yaml``    -- the canonical repo taxonomy,
* ``skillbox-config/clients/*/overlay.yaml`` -- per-client intent.

Today consumers re-derive that by parsing those Skillbox-private files
themselves. The concrete case this contract is written against is **Swimmers**
(``swimmers/src/session/overlay.rs``), which discovers ``skillbox-config`` via
``$SWIMMERS_SKILLBOX_CONFIG`` (else ``~/repos/skillbox-config``), globs
``clients/*/overlay.yaml``, and serde-parses ``client.id``, ``client.label``,
``client.repos[].{id,kind,repo_path}``, ``context.cwd_match``,
``context.plans.{plan_root,plan_draft}`` and
``context.repo_landscape.{scan_roots,repos[].path}``. Every one of those is
covered by :data:`SUPERSEDED_CONSUMER_FIELDS` below and emitted in the payload,
so the consumer can migrate field-by-field and then delete its private parser
rather than run one in parallel forever.

Design rules (each one is exercised by ``tests/test_environment_inventory.py``)
------------------------------------------------------------------------------
**Versioned.** Every payload carries ``schema_version``
(:data:`ENVIRONMENT_INVENTORY_SCHEMA_VERSION`) and a ``contract`` name, using
the ``"<date>+<surface>"`` convention already used by ``agent_snapshots`` /
``agent_search``. A cache written by a different version is ignored, not
half-parsed.

**Declared vs observed.** ``declared`` sub-objects are pure config projection --
byte-identical on every machine that shares the config repo. ``observed``
sub-objects are the ONLY place this box's filesystem is consulted, and they are
``null`` unless the caller opts in with ``observe=True``.

**Stable repo ids.** :func:`stable_repo_id` hashes a repo's path *relative to a
declared root*, where the roots considered are every machine profile's declared
roots (both literal and home-anchored) plus ``${VAR}`` root tokens. So
``~/repos/sweet-potato`` (registry spelling),
``${SKILLBOX_MONOSERVER_ROOT}/sweet-potato`` (overlay spelling) and
``/srv/repos/sweet-potato`` (devbox alias spelling) all collapse to ONE id, and
that id is the same on the laptop and on the devbox. A machine-specific absolute
path is never the hash seed when a declared root matches -- this repo has a
known trap where goldens bake in machine-specific absolute paths, and the id
scheme is designed so nothing downstream can reintroduce it.

**No secrets.** Two independent mechanisms, both tested: (1) the payload is
built by projecting explicit field allowlists, so config keys such as
``client.human_operator.access_token_env`` or ``dev_sanity...auth_token_env``
are structurally unreachable; (2) the finished payload is passed through the
shared ``lib.redaction.redact_value`` table. Git remotes are reduced to
``remote_host``/``remote_kind`` so a ``https://user:token@host`` remote cannot
round-trip. If the shared redaction table cannot be imported, building RAISES
rather than emitting unredacted output.

**Off picker hot paths.** ``build_environment_inventory(observe=False)`` (the
default) never touches the filesystem for repo state; ``observe=True`` costs
exactly one injectable ``probe`` call per declared repo -- one ``os.lstat``, no
directory walks, no globbing, no process execution, no network. The only
entrypoints a
latency-sensitive caller should use are :data:`HOT_PATH_SAFE_ENTRYPOINTS`
(:func:`read_inventory_cache` + :func:`is_stale`): one file read of a payload
built off the hot path.

Public API
----------
``ENVIRONMENT_INVENTORY_SCHEMA_VERSION`` / ``ENVIRONMENT_INVENTORY_CONTRACT``
``build_environment_inventory(...)``  build the payload.
``stable_repo_id(...)``               machine-independent repo identity.
``canonical_json(payload)``           deterministic serialization.
``inventory_cache_path`` / ``write_inventory_cache`` / ``read_inventory_cache``
``is_stale(payload, now=...)``        freshness check for cache readers.
``SUPERSEDED_CONSUMER_FIELDS``        the private parsing this replaces.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

try:  # PyYAML is optional in this repo; mirror the guard used by machines.py.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only without PyYAML
    yaml = None


# --------------------------------------------------------------------------- #
# Contract identity
# --------------------------------------------------------------------------- #

ENVIRONMENT_INVENTORY_CONTRACT = "skillbox.environment_inventory"
ENVIRONMENT_INVENTORY_SCHEMA_VERSION = "2026-07-25+environment_inventory.v1"

#: Default freshness budget for a cached payload, in seconds. A picker that
#: finds a cache older than this should render it as "stale" rather than block
#: on a rebuild.
DEFAULT_TTL_S = 300.0

#: Where a pre-built payload is cached, relative to the repo root. Mirrors the
#: ``.skillbox-state/snapshots/agent_ops`` convention in ``agent_snapshots``.
INVENTORY_CACHE_REL = PurePosixPath(".skillbox-state") / "inventory" / "environment_inventory.json"

#: The ONLY entrypoints a latency-sensitive caller (picker, list view, keystroke
#: handler) should use. Everything else in this module reads config files.
HOT_PATH_SAFE_ENTRYPOINTS = ("read_inventory_cache", "is_stale")

#: Env override pointing directly at a ``registry/repos.yaml``. Deliberately the
#: same variable ``policy_eval.REGISTRY_FILE_ENV_VAR`` already honours, so a test
#: seam set for one surface applies to both.
REGISTRY_FILE_ENV_VAR = "SKILLBOX_REGISTRY_FILE"
REGISTRY_FILE_REL = ("registry", "repos.yaml")
PRIVATE_CONFIG_DIR_NAME = "skillbox-config"
CLIENTS_DIR_NAME = "clients"
CLIENT_OVERLAY_FILE_NAME = "overlay.yaml"

#: Registry document versions this module knows how to project.
SUPPORTED_REGISTRY_SCHEMA_VERSIONS = (1,)
#: Client overlay document versions this module knows how to project.
SUPPORTED_OVERLAY_VERSIONS = (1,)

#: Field allowlists. A config key not named here CANNOT reach the payload.
REPO_DECLARED_FIELDS = (
    "registry_id",
    "path_declared",
    "path_relative",
    "root_category",
    "unresolved_vars",
    "bucket",
    "ownership",
    "runtime_class",
    "sbp_owner",
    "remote_host",
    "remote_kind",
    "kind",
    "clients",
    "declared_by",
)
MACHINE_DECLARED_FIELDS = (
    "machine_id",
    "hostnames",
    "home",
    "managed_home",
    "repo_roots",
    "projects_roots",
    "aliases",
    "declared_machine_ids",
)
CLIENT_DECLARED_FIELDS = (
    "client_id",
    "label",
    "cwd_match",
    "plan_root",
    "plan_draft",
    "scan_roots",
    "repo_ids",
)

#: The consumer-side private parsing this contract retires, mapped to the
#: contract path that now owns each fact. Emitted in the payload under
#: ``supersedes`` so a consumer can assert at runtime that the contract still
#: covers everything it used to parse itself, and delete its parser when the
#: assertion passes. Keys are the literal field paths Swimmers parses today in
#: ``swimmers/src/session/overlay.rs``.
SUPERSEDED_CONSUMER_FIELDS: dict[str, tuple[str, ...]] = {
    "client.id": ("clients[].declared.client_id",),
    "client.label": ("clients[].declared.label",),
    "client.repos[].id": ("repos[].declared.registry_id", "repos[].repo_id"),
    "client.repos[].kind": ("repos[].declared.kind",),
    "client.repos[].repo_path": (
        "repos[].declared.path_declared",
        "repos[].declared.path_relative",
        "repos[].observed.path",
    ),
    "context.cwd_match": ("clients[].declared.cwd_match",),
    "context.plans.plan_root": ("clients[].declared.plan_root",),
    "context.plans.plan_draft": ("clients[].declared.plan_draft",),
    "context.repo_landscape.scan_roots": ("clients[].declared.scan_roots",),
    "context.repo_landscape.repos[].path": (
        "repos[].declared.path_declared",
        "repos[].observed.path",
    ),
    "<discovery> clients/*/overlay.yaml glob": ("clients[]", "sources[]"),
    "<derived> repo exists on this box": ("repos[].observed.present", "repos[].observed.kind"),
    "<derived> which machine am I": ("machine.declared.machine_id", "machine.detection_source"),
    "<derived> repo root translation": (
        "machine.declared.repo_roots",
        "repos[].observed.path",
    ),
}


class EnvironmentInventoryError(RuntimeError):
    """Raised when the contract cannot be built safely (e.g. no redaction table)."""


# --------------------------------------------------------------------------- #
# Redaction (hard dependency -- never degrade to "emit unredacted")
# --------------------------------------------------------------------------- #


def _repo_root_dir() -> Path:
    """``runtime_manager/`` -> ``.env-manager/`` -> repo root (no heavy import)."""
    return Path(__file__).resolve().parent.parent.parent


def _redaction_module() -> Any:
    """Import the single shared redaction table (``scripts/lib/redaction.py``).

    Resolution mirrors ``shared.py``: put ``<repo_root>/scripts`` on ``sys.path``
    and import ``lib.redaction``. ``redaction`` is a stdlib-only leaf, so this
    stays cheap -- it does NOT pull in the heavy ``shared`` facade.
    """
    override = globals().get("_redaction_module_override")
    if override is not None:
        return override()
    scripts_dir = str(_repo_root_dir() / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from lib import redaction  # noqa: PLC0415

        return redaction
    except Exception as exc:  # pragma: no cover - defensive
        raise EnvironmentInventoryError(
            "Cannot import the shared redaction table (scripts/lib/redaction.py). "
            "Refusing to emit an environment inventory that has not been redacted."
        ) from exc


def _machines_module() -> Any:
    """Import the sibling ``machines`` loader, package or standalone."""
    override = globals().get("_machines_module_override")
    if override is not None:
        return override()
    if __package__:
        from . import machines as machines_mod  # noqa: PLC0415

        return machines_mod
    package_dir = str(Path(__file__).resolve().parent)
    if package_dir not in sys.path:  # pragma: no cover - standalone loader path
        sys.path.insert(0, package_dir)
    import machines as machines_mod  # type: ignore[no-redef]  # noqa: PLC0415

    return machines_mod


# --------------------------------------------------------------------------- #
# Declared-path handling (NO machine-specific expansion)
# --------------------------------------------------------------------------- #

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _normalize_declared(value: Any) -> str:
    """Normalize a *declared* path without expanding it against the local $HOME.

    ``~/repos/foo`` stays ``~/repos/foo``. Calling ``expanduser`` here would bake
    this box's home directory into the identity -- the machine-specific-path trap
    this contract exists to avoid.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _substitute_vars(text: str, path_vars: Mapping[str, str]) -> tuple[str, list[str]]:
    """Expand ``${VAR}``/``$VAR`` from ``path_vars``; report what stayed unresolved."""
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = path_vars.get(name)
        if value is None or value == "":
            if name not in unresolved:
                unresolved.append(name)
            return match.group(0)
        return str(value)

    return (_VAR_RE.sub(replace, text), unresolved)


def _declared_root_candidates(config: Any) -> list[tuple[str, str]]:
    """Every declared root spelling across ALL machine profiles.

    Returns ``(root_spelling, category)`` pairs, longest first. Both the literal
    spelling (``~/repos``) and the home-anchored form (``/Users/b/repos``) are
    included, so a path written in either notation splits identically -- which is
    what makes :func:`stable_repo_id` machine-independent.
    """
    if config is None:
        return []
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for profile in (getattr(config, "machines", {}) or {}).values():
        home = getattr(profile, "home", None)
        managed_home = getattr(profile, "managed_home", None)
        for category, roots in (
            ("repos", getattr(profile, "repo_roots", ())),
            ("projects", getattr(profile, "projects_roots", ())),
        ):
            for root in roots:
                spellings = [_normalize_declared(root)]
                if spellings[0].startswith("~"):
                    for base in (home, managed_home):
                        if base:
                            remainder = spellings[0][1:].lstrip("/")
                            spellings.append(
                                _normalize_declared(f"{_normalize_declared(base)}/{remainder}")
                            )
                for spelling in spellings:
                    if not spelling:
                        continue
                    key = (spelling, category)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(key)
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    return candidates


def _declared_root_split(
    declared_path: str,
    *,
    config: Any = None,
    path_vars: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None, list[str], str]:
    """Split a declared path into ``(category, relative, unresolved_vars, resolved)``.

    ``relative`` is the portion beneath a declared root and is identical on every
    machine. Returns ``(None, None, ...)`` when no declared root matches, in
    which case the caller falls back to the normalized spelling.
    """
    path_vars = {} if path_vars is None else path_vars
    normalized = _normalize_declared(declared_path)
    if not normalized:
        return (None, None, [], "")
    resolved, unresolved = _substitute_vars(normalized, path_vars)
    resolved = _normalize_declared(resolved)

    # Canonicalize known symlink aliases first (e.g. /srv/repos -> /srv/skillbox/repos)
    # so an alias spelling never reads as a different repo.
    if config is not None and not resolved.startswith(("~", "$")):
        try:
            resolved = _normalize_declared(config.canonicalize_alias(resolved))
        except Exception:  # pragma: no cover - defensive
            pass

    for root, category in _declared_root_candidates(config):
        if resolved == root:
            return (category, "", unresolved, resolved)
        if resolved.startswith(root.rstrip("/") + "/"):
            return (category, resolved[len(root.rstrip("/")) + 1 :], unresolved, resolved)

    # A leading unresolved ``${VAR}`` acts as a symbolic root token, so an overlay
    # spelling still merges with the registry spelling of the same repo.
    if unresolved and resolved.startswith("$"):
        head, _, remainder = resolved.partition("/")
        if remainder:
            return ("repos", remainder, unresolved, resolved)
        del head
    return (None, None, unresolved, resolved)


# --------------------------------------------------------------------------- #
# Stable repo identity
# --------------------------------------------------------------------------- #

_DERIVED_ID_PREFIX = "sha256:"
_DERIVED_ID_NAMESPACE = "skillbox.environment_inventory.repo:"
_DERIVED_ID_LEN = 16


def stable_repo_id(
    declared_path: str,
    *,
    config: Any = None,
    path_vars: Mapping[str, str] | None = None,
) -> str:
    """A deterministic, machine-independent identity for a declared repo path.

    The seed is ``"<category>/<path-relative-to-a-declared-root>"`` whenever a
    declared root matches -- never a machine-specific absolute path. A path under
    no declared root falls back to hashing its normalized declared spelling,
    which is still deterministic, just not root-portable.
    """
    category, relative, _unresolved, resolved = _declared_root_split(
        declared_path, config=config, path_vars=path_vars
    )
    seed = f"{category}/{relative}" if (category is not None and relative is not None) else resolved
    digest = hashlib.sha256((_DERIVED_ID_NAMESPACE + seed).encode("utf-8")).hexdigest()
    return _DERIVED_ID_PREFIX + digest[:_DERIVED_ID_LEN]


# --------------------------------------------------------------------------- #
# Remote sanitization
# --------------------------------------------------------------------------- #


def _remote_facts(remote: Any) -> tuple[str | None, str | None]:
    """Reduce a git remote to ``(host, kind)``; never return credentials.

    A remote may legitimately be ``https://user:token@github.com/o/r.git``. The
    contract therefore never carries the URL itself -- only host and transport
    kind, which is all a consumer needs to group repos.
    """
    text = str(remote or "").strip()
    if not text:
        return (None, None)
    if text.startswith(("http://", "https://", "ssh://", "git://")):
        scheme, _, rest = text.partition("://")
        netloc = rest.split("/", 1)[0]
        host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]  # drop any userinfo
        kind = {"https": "https", "http": "http", "ssh": "ssh", "git": "git"}[scheme]
        return (host or None, kind)
    if "@" in text and ":" in text.split("@", 1)[1]:
        # scp-style: git@github.com:owner/repo.git
        host = text.split("@", 1)[1].split(":", 1)[0]
        return (host or None, "ssh")
    if text.startswith(("/", "~", ".", "$")):
        return (None, "local")
    return (None, "other")


# --------------------------------------------------------------------------- #
# Observed presence (the ONLY filesystem contact on the observe path)
# --------------------------------------------------------------------------- #

PresenceProbe = Callable[[str], "dict[str, Any]"]


def default_presence_probe(path: str) -> dict[str, Any]:
    """Exactly one ``os.lstat`` for one exact path. No walking, no globbing.

    Returns ``{"present": bool, "kind": "dir"|"file"|"symlink"|"missing"|"denied"}``.
    ``kind`` reports ``symlink`` without following the link, because "declared
    here but actually a link elsewhere" is a real fleet condition a consumer must
    be able to see.
    """
    import stat as stat_mod  # noqa: PLC0415 - stdlib, only on the observe path

    try:
        stat_result = os.lstat(path)
    except FileNotFoundError:
        return {"present": False, "kind": "missing"}
    except OSError:
        return {"present": False, "kind": "denied"}
    mode = stat_result.st_mode
    if stat_mod.S_ISLNK(mode):
        kind = "symlink"
    elif stat_mod.S_ISDIR(mode):
        kind = "dir"
    else:
        kind = "file"
    return {"present": True, "kind": kind}


def _observed_path(
    *,
    category: str | None,
    relative: str | None,
    resolved: str,
    config: Any,
    machine_id: str | None,
) -> str | None:
    """Translate a declared path onto THIS machine using declared roots.

    This is the root-translation Skillbox owns: ``~/repos/foo`` observed on the
    devbox resolves under ``/srv/skillbox/repos``, not under ``$HOME/repos``.
    """
    if config is not None and machine_id and category and relative is not None:
        profile = config.get(machine_id)
        if profile is not None:
            roots = profile.repo_roots if category == "repos" else profile.projects_roots
            if roots:
                base = _normalize_declared(roots[0])
                if base.startswith("~") and getattr(profile, "home", None):
                    base = _normalize_declared(
                        f"{_normalize_declared(profile.home)}/{base[1:].lstrip('/')}"
                    )
                return os.path.normpath(f"{base}/{relative}" if relative else base)
    if not resolved or resolved.startswith("$"):
        return None
    if resolved.startswith("~"):
        home = None
        if config is not None and machine_id:
            profile = config.get(machine_id)
            home = getattr(profile, "home", None) if profile is not None else None
        if not home:
            return None
        return os.path.normpath(f"{_normalize_declared(home)}/{resolved[1:].lstrip('/')}")
    return os.path.normpath(resolved)


# --------------------------------------------------------------------------- #
# Config discovery
# --------------------------------------------------------------------------- #


def _expand(value: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(value))))


def _config_root_candidates(
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Ordered candidate ``skillbox-config`` directories (no absolute hard-codes)."""
    candidates: list[str] = []
    if config_root is not None:
        candidates.append(_expand(str(config_root)))
    root = str(_repo_root_dir()) if root_dir is None else _expand(str(root_dir))
    candidates.append(_expand(os.path.join(root, "..", PRIVATE_CONFIG_DIR_NAME)))
    candidates.append(_expand(os.path.join(root, "..", "..", PRIVATE_CONFIG_DIR_NAME)))
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def find_registry_yaml(
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """First existing ``registry/repos.yaml``, or ``None``."""
    env = os.environ if env is None else env
    override = str(env.get(REGISTRY_FILE_ENV_VAR) or "").strip()
    if override and os.path.isfile(_expand(override)):
        return _expand(override)
    for root in _config_root_candidates(root_dir=root_dir, config_root=config_root):
        candidate = os.path.join(root, *REGISTRY_FILE_REL)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_clients_dir(
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
) -> str | None:
    """First existing ``skillbox-config/clients`` directory, or ``None``."""
    for root in _config_root_candidates(root_dir=root_dir, config_root=config_root):
        candidate = os.path.join(root, CLIENTS_DIR_NAME)
        if os.path.isdir(candidate):
            return candidate
    return None


def _load_yaml_mapping(path: str, *, what: str) -> tuple[dict[str, Any], str]:
    if yaml is None:
        raise EnvironmentInventoryError(
            f"Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` to read {what}."
        )
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    raw = yaml.safe_load(text)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise EnvironmentInventoryError(f"Expected a YAML mapping in {path}.")
    return (raw, text)


# --------------------------------------------------------------------------- #
# Freshness facts
# --------------------------------------------------------------------------- #


def _source_fact(
    kind: str,
    path: str | None,
    *,
    text: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Freshness fact for one config document.

    Carries a content digest, not just an mtime, so a consumer can tell a real
    config change from a ``touch``. ``path`` is legitimately machine-specific
    runtime data; fixtures never assert on it.
    """
    fact: dict[str, Any] = {
        "kind": kind,
        "client_id": client_id,
        "path": path,
        "present": bool(path),
        "sha256": None,
        "mtime": None,
        "size": None,
    }
    if not path:
        return fact
    try:
        stat_result = os.stat(path)
        fact["mtime"] = float(stat_result.st_mtime)
        fact["size"] = int(stat_result.st_size)
    except OSError:
        fact["present"] = False
        return fact
    if text is None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - raced away between stat and read
            return fact
    fact["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return fact


# --------------------------------------------------------------------------- #
# Repo accumulation
# --------------------------------------------------------------------------- #


class _RepoAccumulator:
    """Merges repo declarations from every source into one record per stable id."""

    def __init__(self, *, config: Any, path_vars: Mapping[str, str]) -> None:
        self._config = config
        self._path_vars = path_vars
        self._records: dict[str, dict[str, Any]] = {}

    def add(
        self,
        declared_path: str,
        *,
        source: str,
        registry_id: str | None = None,
        kind: str | None = None,
        client_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> str | None:
        normalized = _normalize_declared(declared_path)
        if not normalized:
            return None
        category, relative, unresolved, resolved = _declared_root_split(
            normalized, config=self._config, path_vars=self._path_vars
        )
        repo_id = stable_repo_id(normalized, config=self._config, path_vars=self._path_vars)
        record = self._records.get(repo_id)
        if record is None:
            record = {
                "registry_id": None,
                "path_declared": normalized,
                "path_relative": relative,
                "root_category": category,
                "unresolved_vars": list(unresolved),
                "bucket": None,
                "ownership": None,
                "runtime_class": None,
                "sbp_owner": None,
                "remote_host": None,
                "remote_kind": None,
                "kind": None,
                "clients": [],
                "declared_by": [],
                "_resolved": resolved,
            }
            self._records[repo_id] = record
        if registry_id and not record["registry_id"]:
            record["registry_id"] = registry_id
        if kind and not record["kind"]:
            record["kind"] = kind
        if client_id and client_id not in record["clients"]:
            record["clients"].append(client_id)
        if source not in record["declared_by"]:
            record["declared_by"].append(source)
        for key, value in (extra or {}).items():
            if key in record and record[key] is None and value is not None:
                record[key] = value
        return repo_id

    def blocks(
        self,
        *,
        machine_id: str | None,
        observe: bool,
        probe: PresenceProbe,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for repo_id, record in self._records.items():
            resolved = record.pop("_resolved", "")
            record["clients"] = sorted(record["clients"])
            record["declared_by"] = sorted(record["declared_by"])
            declared = {field: record.get(field) for field in REPO_DECLARED_FIELDS}
            block: dict[str, Any] = {
                "repo_id": repo_id,
                "declared": declared,
                "observed": None,
            }
            if observe:
                path = _observed_path(
                    category=declared["root_category"],
                    relative=declared["path_relative"],
                    resolved=resolved,
                    config=self._config,
                    machine_id=machine_id,
                )
                if path is None:
                    block["observed"] = {
                        "path": None,
                        "present": False,
                        "kind": "unresolvable",
                    }
                else:
                    result = probe(path)
                    block["observed"] = {
                        "path": path,
                        "present": bool(result.get("present")),
                        "kind": result.get("kind"),
                    }
            blocks.append(block)
        blocks.sort(key=lambda item: str(item["repo_id"]))
        return blocks


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def build_environment_inventory(
    *,
    machines_config: Any = None,
    machines_path: str | os.PathLike[str] | None = None,
    registry_document: Mapping[str, Any] | None = None,
    registry_path: str | os.PathLike[str] | None = None,
    client_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    clients_dir: str | os.PathLike[str] | None = None,
    include_clients: bool = True,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    path_vars: Mapping[str, str] | None = None,
    hostname: str | None = None,
    machine_id: str | None = None,
    observe: bool = False,
    probe: PresenceProbe | None = None,
    now: float | None = None,
    ttl_s: float = DEFAULT_TTL_S,
) -> dict[str, Any]:
    """Build the versioned environment-inventory payload.

    ``observe=False`` (the default) produces a purely declared document with no
    per-repo filesystem contact. ``observe=True`` adds an ``observed`` block per
    repo at a cost of exactly one ``probe`` call each.

    Every input is injectable (config objects, documents, overlays, clock,
    probe, path variables) so tests -- and machines without the private config
    repo -- never depend on this box's real layout.
    """
    redaction = _redaction_module()  # fail closed BEFORE any data is gathered
    env = dict(os.environ if env is None else env)
    path_vars = dict(env if path_vars is None else path_vars)
    now = float(now) if now is not None else _clock()
    probe = probe or default_presence_probe
    recovery: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []

    # -- machines.yaml (machine intent + root translation) ------------------ #
    machines_mod = _machines_module()
    config = machines_config
    machines_source_path: str | None = None
    if config is None:
        try:
            machines_source_path = (
                os.fspath(machines_path)
                if machines_path is not None
                else machines_mod.find_machines_yaml(
                    root_dir=root_dir, config_root=config_root, env=env
                )
            )
            if machines_source_path:
                config = machines_mod.load_machines_config(machines_source_path)
        except Exception as exc:
            config = None
            recovery.append(
                {
                    "code": "machines_config_unreadable",
                    "message": f"machines.yaml could not be loaded: {exc}",
                    "hint": (
                        "Check out skillbox-config beside this repo, or point "
                        "SKILLBOX_MACHINES_FILE at a machines.yaml."
                    ),
                }
            )
    else:
        machines_source_path = getattr(config, "source_path", None)
    if config is None and not any(item["code"].startswith("machines_") for item in recovery):
        recovery.append(
            {
                "code": "machines_config_missing",
                "message": "No machines.yaml resolved; machine intent is unknown.",
                "hint": "Install skillbox-config or set SKILLBOX_MACHINES_FILE.",
            }
        )
    sources.append(_source_fact("machines", machines_source_path))

    resolved_machine_id, detection_source = _detect_machine(
        config, machine_id=machine_id, hostname=hostname, env=env, recovery=recovery
    )

    accumulator = _RepoAccumulator(config=config, path_vars=path_vars)

    # -- registry/repos.yaml (repo taxonomy) -------------------------------- #
    registry_source_path: str | None = None
    document: Mapping[str, Any] | None = registry_document
    if document is None:
        registry_source_path = (
            os.fspath(registry_path)
            if registry_path is not None
            else find_registry_yaml(root_dir=root_dir, config_root=config_root, env=env)
        )
        if registry_source_path:
            try:
                document, _text = _load_yaml_mapping(
                    registry_source_path, what="registry/repos.yaml"
                )
            except Exception as exc:
                document = None
                recovery.append(
                    {
                        "code": "registry_unreadable",
                        "message": f"registry/repos.yaml could not be loaded: {exc}",
                        "hint": "Run the registry doctor in skillbox-config, or fix the YAML.",
                    }
                )
        else:
            recovery.append(
                {
                    "code": "registry_missing",
                    "message": "No registry/repos.yaml resolved; repo taxonomy is unknown.",
                    "hint": "Install skillbox-config or set SKILLBOX_REGISTRY_FILE.",
                }
            )
    sources.append(_source_fact("registry", registry_source_path))

    if document is not None:
        version = document.get("schema_version")
        if version not in SUPPORTED_REGISTRY_SCHEMA_VERSIONS:
            recovery.append(
                {
                    "code": "registry_schema_unsupported",
                    "message": (
                        f"registry/repos.yaml schema_version {version!r} is not in "
                        f"{list(SUPPORTED_REGISTRY_SCHEMA_VERSIONS)}; entries were projected "
                        "on a best-effort basis."
                    ),
                    "hint": "Upgrade this contract module alongside the registry schema.",
                }
            )
        _absorb_registry(document, accumulator)

    # -- clients/*/overlay.yaml (client intent) ----------------------------- #
    clients: list[dict[str, Any]] = []
    if include_clients:
        overlays, overlay_sources = _resolve_client_overlays(
            client_overlays=client_overlays,
            clients_dir=clients_dir,
            root_dir=root_dir,
            config_root=config_root,
            recovery=recovery,
        )
        sources.extend(overlay_sources)
        for client_id in sorted(overlays):
            clients.append(
                _absorb_client_overlay(
                    client_id, overlays[client_id], accumulator, recovery=recovery
                )
            )

    repos = accumulator.blocks(
        machine_id=resolved_machine_id, observe=observe, probe=probe
    )

    payload: dict[str, Any] = {
        "schema_version": ENVIRONMENT_INVENTORY_SCHEMA_VERSION,
        "contract": ENVIRONMENT_INVENTORY_CONTRACT,
        "generated_at": now,
        "machine": _machine_block(
            config,
            resolved_machine_id,
            detection_source=detection_source,
            observe=observe,
            probe=probe,
        ),
        "clients": clients,
        "repos": repos,
        "sources": sources,
        "freshness": {
            "generated_at": now,
            "ttl_s": float(ttl_s),
            "expires_at": now + float(ttl_s),
            "observed": bool(observe),
            "source_count": len(sources),
        },
        "readiness": _readiness(
            config=config,
            machine_id=resolved_machine_id,
            repos=repos,
            clients=clients,
            observe=observe,
            recovery=recovery,
        ),
        "recovery": recovery,
        "supersedes": {
            "consumer": "swimmers",
            "note": (
                "Read these contract fields instead of parsing skillbox-config "
                "privately. Every key below is a field Swimmers parses today in "
                "src/session/overlay.rs; the values are the contract paths that "
                "now own that fact. Guaranteed for the life of this schema_version."
            ),
            "fields": {
                key: list(value) for key, value in sorted(SUPERSEDED_CONSUMER_FIELDS.items())
            },
        },
        "redaction": {
            "marker": redaction.REDACTION_MARKER,
            "applied": True,
            "policy": "field-allowlist projection, then shared lib.redaction.redact_value",
        },
    }

    # Defense in depth: the allowlist projection above should already make this a
    # no-op, which is exactly what the redaction tests assert.
    return redaction.redact_value(payload)


def _clock() -> float:
    import time  # noqa: PLC0415 - keeps the module import graph stdlib-minimal

    return time.time()


def _absorb_registry(document: Mapping[str, Any], accumulator: _RepoAccumulator) -> None:
    raw = document.get("repos")
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        declared_path = str(entry.get("path") or "").strip()
        if not declared_path:
            continue
        remote_host, remote_kind = _remote_facts(entry.get("remote"))
        accumulator.add(
            declared_path,
            source="registry",
            registry_id=_clean_scalar(entry.get("id")),
            extra={
                "bucket": _clean_scalar(entry.get("bucket")),
                "ownership": _clean_scalar(entry.get("ownership")),
                "runtime_class": _clean_scalar(entry.get("runtime_class")),
                "sbp_owner": _clean_scalar(entry.get("sbp_owner")),
                "remote_host": remote_host,
                "remote_kind": remote_kind,
            },
        )


def _resolve_client_overlays(
    *,
    client_overlays: Mapping[str, Mapping[str, Any]] | None,
    clients_dir: str | os.PathLike[str] | None,
    root_dir: str | os.PathLike[str] | None,
    config_root: str | os.PathLike[str] | None,
    recovery: list[dict[str, str]],
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, Any]]]:
    """Load ``clients/*/overlay.yaml``.

    Bounded cost: ONE ``os.listdir`` of the clients directory plus one read per
    client overlay. No recursion, no globbing below that level. This runs on the
    build path only -- a picker reads the cache instead.
    """
    if client_overlays is not None:
        return (dict(client_overlays), [])

    resolved_dir = (
        os.fspath(clients_dir)
        if clients_dir is not None
        else find_clients_dir(root_dir=root_dir, config_root=config_root)
    )
    if not resolved_dir or not os.path.isdir(resolved_dir):
        recovery.append(
            {
                "code": "clients_dir_missing",
                "message": "No skillbox-config/clients directory resolved; client intent is unknown.",
                "hint": "Install skillbox-config beside this repo, or pass clients_dir=.",
            }
        )
        return ({}, [])

    overlays: dict[str, Mapping[str, Any]] = {}
    facts: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(resolved_dir))
    except OSError as exc:
        recovery.append(
            {
                "code": "clients_dir_unreadable",
                "message": f"clients directory could not be listed: {exc}",
                "hint": "Check permissions on skillbox-config/clients.",
            }
        )
        return ({}, [])

    for name in names:
        overlay_path = os.path.join(resolved_dir, name, CLIENT_OVERLAY_FILE_NAME)
        if not os.path.isfile(overlay_path):
            continue
        try:
            document, text = _load_yaml_mapping(overlay_path, what="clients/*/overlay.yaml")
        except Exception as exc:
            recovery.append(
                {
                    "code": "client_overlay_unreadable",
                    "message": f"{name}/overlay.yaml could not be loaded: {exc}",
                    "hint": "Fix the overlay YAML, or remove the client directory.",
                }
            )
            facts.append(_source_fact("client_overlay", overlay_path, client_id=name))
            continue
        overlays[name] = document
        facts.append(_source_fact("client_overlay", overlay_path, text=text, client_id=name))
    return (overlays, facts)


def _absorb_client_overlay(
    client_dir_name: str,
    document: Mapping[str, Any],
    accumulator: _RepoAccumulator,
    *,
    recovery: list[dict[str, str]],
) -> dict[str, Any]:
    """Project ONE client overlay through the allowlist and feed its repos in."""
    version = document.get("version")
    if version is not None and version not in SUPPORTED_OVERLAY_VERSIONS:
        recovery.append(
            {
                "code": "client_overlay_version_unsupported",
                "message": (
                    f"{client_dir_name}/overlay.yaml version {version!r} is not in "
                    f"{list(SUPPORTED_OVERLAY_VERSIONS)}; projected best-effort."
                ),
                "hint": "Upgrade this contract module alongside the overlay schema.",
            }
        )

    client = document.get("client")
    client = client if isinstance(client, Mapping) else {}
    context = client.get("context")
    context = context if isinstance(context, Mapping) else {}
    plans = context.get("plans")
    plans = plans if isinstance(plans, Mapping) else {}
    landscape = context.get("repo_landscape")
    landscape = landscape if isinstance(landscape, Mapping) else {}

    client_id = _clean_scalar(client.get("id")) or client_dir_name
    repo_ids: list[str] = []

    for entry in _mapping_list(client.get("repos")):
        repo_id = accumulator.add(
            str(entry.get("repo_path") or ""),
            source=f"client:{client_id}.repos",
            registry_id=_clean_scalar(entry.get("id")),
            kind=_clean_scalar(entry.get("kind")),
            client_id=client_id,
        )
        if repo_id and repo_id not in repo_ids:
            repo_ids.append(repo_id)

    for entry in _mapping_list(landscape.get("repos")):
        repo_id = accumulator.add(
            str(entry.get("path") or ""),
            source=f"client:{client_id}.repo_landscape",
            registry_id=_clean_scalar(entry.get("id")),
            client_id=client_id,
        )
        if repo_id and repo_id not in repo_ids:
            repo_ids.append(repo_id)

    declared = {
        "client_id": client_id,
        "label": _clean_scalar(client.get("label")),
        "cwd_match": _string_list(context.get("cwd_match")),
        "plan_root": _clean_scalar(plans.get("plan_root")),
        "plan_draft": _clean_scalar(plans.get("plan_draft")),
        "scan_roots": _string_list(landscape.get("scan_roots")),
        "repo_ids": sorted(repo_ids),
    }
    return {
        "client_id": client_id,
        "declared": {field: declared.get(field) for field in CLIENT_DECLARED_FIELDS},
        "observed": None,
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_scalar(item)
        if text:
            out.append(_normalize_declared(text))
    return out


def _clean_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _detect_machine(
    config: Any,
    *,
    machine_id: str | None,
    hostname: str | None,
    env: Mapping[str, str],
    recovery: list[dict[str, str]],
) -> tuple[str | None, str]:
    """Resolve the machine id and record HOW it was resolved."""
    explicit = str(machine_id or "").strip()
    if explicit:
        return (explicit, "explicit")
    if config is None:
        return (None, "none")
    try:
        detected = config.detect_machine_id(hostname=hostname, env=dict(env))
    except Exception as exc:
        recovery.append(
            {
                "code": "machine_detection_failed",
                "message": f"Machine detection failed: {exc}",
                "hint": "Set SKILLBOX_MACHINE to a machine id declared in machines.yaml.",
            }
        )
        return (None, "none")
    if detected is None:
        recovery.append(
            {
                "code": "machine_undetected",
                "message": "This host matches no declared machine profile.",
                "hint": "Add its short hostname to machines.yaml, or set SKILLBOX_MACHINE.",
            }
        )
        return (None, "none")
    source = "env" if str(env.get("SKILLBOX_MACHINE") or "").strip() else "hostname"
    return (detected, source)


def _machine_block(
    config: Any,
    machine_id: str | None,
    *,
    detection_source: str,
    observe: bool,
    probe: PresenceProbe,
) -> dict[str, Any]:
    declared: dict[str, Any] = {
        "machine_id": machine_id,
        "hostnames": [],
        "home": None,
        "managed_home": None,
        "repo_roots": [],
        "projects_roots": [],
        "aliases": [],
        "declared_machine_ids": sorted(getattr(config, "machines", {}) or {}),
    }
    profile = config.get(machine_id) if (config is not None and machine_id) else None
    if profile is not None:
        declared["hostnames"] = list(profile.hostnames)
        declared["home"] = profile.home
        declared["managed_home"] = profile.managed_home
        declared["repo_roots"] = list(profile.repo_roots)
        declared["projects_roots"] = list(profile.projects_roots)
    if config is not None:
        declared["aliases"] = [
            {"alias": alias.alias, "canonical": alias.canonical}
            for alias in getattr(config, "aliases", ())
        ]
    declared = {field: declared.get(field) for field in MACHINE_DECLARED_FIELDS}

    block: dict[str, Any] = {
        "declared": declared,
        "detection_source": detection_source,
        "observed": None,
    }
    if observe:
        roots: list[dict[str, Any]] = []
        for category, values in (
            ("repos", declared["repo_roots"]),
            ("projects", declared["projects_roots"]),
        ):
            for root in values or []:
                # Probe the declared root ITSELF (not the canonical root it would
                # translate to), so "this alternate root is missing here" is
                # visible rather than collapsed onto the canonical one.
                path = _resolve_root_for_probe(root, declared["home"])
                result = probe(path) if path else {"present": False, "kind": "unresolvable"}
                roots.append(
                    {
                        "category": category,
                        "root_declared": _normalize_declared(root),
                        "path": path,
                        "present": bool(result.get("present")),
                        "kind": result.get("kind"),
                    }
                )
        block["observed"] = {
            "detected": bool(machine_id),
            "roots": roots,
            "roots_present": sum(1 for entry in roots if entry["present"]),
        }
    return block


def _resolve_root_for_probe(root: Any, home: str | None) -> str | None:
    text = _normalize_declared(root)
    if not text:
        return None
    if text.startswith("~"):
        if not home:
            return None
        return os.path.normpath(f"{_normalize_declared(home)}/{text[1:].lstrip('/')}")
    if text.startswith("$"):
        return None
    return os.path.normpath(text)


def _readiness(
    *,
    config: Any,
    machine_id: str | None,
    repos: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    observe: bool,
    recovery: list[dict[str, str]],
) -> dict[str, Any]:
    """Coarse, consumer-facing readiness with the reasons spelled out.

    ``ready``        -- machine identified and repo intent loaded.
    ``degraded``     -- usable, but something is missing (unknown machine, or
                        observation found declared repos absent).
    ``unconfigured`` -- no machine profile AND no repo intent; a consumer must
                        not read an empty repo list as "this box has no repos".
    """
    reasons: list[str] = []
    observed = [repo for repo in repos if isinstance(repo.get("observed"), dict)]
    present = sum(1 for repo in observed if repo["observed"]["present"])
    missing = len(observed) - present

    if config is None:
        reasons.append("machines.yaml not loaded")
    if machine_id is None:
        reasons.append("machine not identified")
    if not repos:
        reasons.append("no declared repos")
    if observe and missing:
        reasons.append(f"{missing} declared repo(s) absent on this machine")

    if config is None and not repos:
        status = "unconfigured"
    elif reasons:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "reasons": reasons,
        "declared_repo_count": len(repos),
        "declared_client_count": len(clients),
        "observed_present_count": present if observe else None,
        "observed_missing_count": missing if observe else None,
        "recovery_count": len(recovery),
    }


# --------------------------------------------------------------------------- #
# Serialization + cache (the picker-facing hot path)
# --------------------------------------------------------------------------- #


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic serialization: sorted keys, compact separators, trailing NL."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def inventory_cache_path(root_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(root_dir) if root_dir is not None else _repo_root_dir()
    return root / str(INVENTORY_CACHE_REL)


def write_inventory_cache(
    payload: Mapping[str, Any],
    root_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist a pre-built payload so hot-path readers never rebuild it."""
    path = inventory_cache_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(canonical_json(payload), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_inventory_cache(
    root_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Hot-path read: ONE file read, no config parsing, no probing, no exec.

    Returns ``None`` when there is no cache, it is unreadable, or it was written
    by a different ``schema_version`` -- a picker should then render "inventory
    not built yet" rather than block on a rebuild.
    """
    path = inventory_cache_path(root_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != ENVIRONMENT_INVENTORY_SCHEMA_VERSION:
        return None
    return payload


def is_stale(payload: Mapping[str, Any] | None, *, now: float | None = None) -> bool:
    """True when a cached payload has outlived its ``freshness.ttl_s`` budget."""
    if not payload:
        return True
    freshness = payload.get("freshness")
    if not isinstance(freshness, Mapping):
        return True
    expires_at = freshness.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return True
    return (_clock() if now is None else float(now)) > float(expires_at)


__all__ = [
    "ENVIRONMENT_INVENTORY_CONTRACT",
    "ENVIRONMENT_INVENTORY_SCHEMA_VERSION",
    "DEFAULT_TTL_S",
    "INVENTORY_CACHE_REL",
    "HOT_PATH_SAFE_ENTRYPOINTS",
    "REGISTRY_FILE_ENV_VAR",
    "SUPPORTED_REGISTRY_SCHEMA_VERSIONS",
    "SUPPORTED_OVERLAY_VERSIONS",
    "REPO_DECLARED_FIELDS",
    "MACHINE_DECLARED_FIELDS",
    "CLIENT_DECLARED_FIELDS",
    "SUPERSEDED_CONSUMER_FIELDS",
    "EnvironmentInventoryError",
    "PresenceProbe",
    "default_presence_probe",
    "find_registry_yaml",
    "find_clients_dir",
    "stable_repo_id",
    "build_environment_inventory",
    "canonical_json",
    "inventory_cache_path",
    "write_inventory_cache",
    "read_inventory_cache",
    "is_stale",
]
