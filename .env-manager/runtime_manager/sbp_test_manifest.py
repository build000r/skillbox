"""Strict `.skillbox/test.yaml` schema v1 + loader (skillbox-sbp-test-manifest-v1-23t3).

The manifest is a *contract*, not a convenience: `sbp test` normalizes the
infrastructure, not the tests, so the repo declares its test units once and the
compiler consumes them. That only works if the loader is strict, so this module
refuses rather than guesses.

Design rules that are load-bearing:

* **argv, never a shell string.** ``command`` is a list. A shell string would
  make quoting, word-splitting and injection the compiler's problem on every
  worker; refusing it up front keeps execution shell-free.
* **env names only, never values.** ``env`` is an allowlist of variable NAMES to
  forward. Accepting values here would turn a version-controlled, review-visible
  file into a secret store.
* **no auto-discovery.** Units and the ``default`` group are declared or they do
  not exist. A manifest that silently means "everything I happened to find" is
  not a contract.
* **unknown keys are errors.** Silently ignoring a misspelled key is how a unit
  quietly stops having a timeout.

Two distinct failure kinds, deliberately not conflated:

* **Schema findings** -- the manifest is malformed or self-inconsistent. The
  contract is broken.
* **Drift findings** -- the manifest is well-formed but disagrees with reality
  (a declared command is not installed, a declared cwd does not exist). This is
  what ``EXIT_DRIFT`` is reserved for. A *failing test* is never drift.

Standard library only; PyYAML is optional exactly as elsewhere in the runtime
manager.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

MANIFEST_RELPATH = ".skillbox/test.yaml"

DEFAULT_GROUP = "default"
FULL_GROUP = "full"

UNIT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")

KNOWN_OS = frozenset({"linux", "darwin", "windows"})
EXCLUSIVITY_VALUES = frozenset({"shared", "exclusive"})
# `cache` is a flag with exactly one legal value. It exists so a unit can opt
# OUT of any future caching; there is deliberately no way to opt *in* yet.
CACHE_VALUES = frozenset({"never"})

TOP_LEVEL_KEYS = frozenset({"schema_version", "units", "groups"})
UNIT_KEYS = frozenset(
    {
        "command",
        "cwd",
        "requires",
        "services",
        "depends_on",
        "timeout_s",
        "artifacts",
        "resource_group",
        "exclusivity",
        "cache",
        "env",
    }
)
REQUIRES_KEYS = frozenset({"os", "python", "caps"})


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One typed lint result. ``code`` is the stable machine contract."""

    code: str
    message: str
    unit: str | None = None
    location: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.unit is not None:
            payload["unit"] = self.unit
        if self.location is not None:
            payload["location"] = self.location
        return payload


def findings_payload(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [finding.to_payload() for finding in findings]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Unit:
    id: str
    command: tuple[str, ...]
    cwd: str | None = None
    requires: dict[str, Any] = field(default_factory=dict)
    services: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    timeout_s: int | None = None
    artifacts: tuple[str, ...] = ()
    resource_group: str | None = None
    exclusivity: str = "shared"
    cache: str | None = None
    env: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": list(self.command),
            "cwd": self.cwd,
            "requires": dict(self.requires),
            "services": list(self.services),
            "depends_on": list(self.depends_on),
            "timeout_s": self.timeout_s,
            "artifacts": list(self.artifacts),
            "resource_group": self.resource_group,
            "exclusivity": self.exclusivity,
            "cache": self.cache,
            "env": list(self.env),
        }


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    units: dict[str, Unit]
    groups: dict[str, tuple[str, ...]]


# --------------------------------------------------------------------------- #
# YAML parsing with duplicate-key detection
# --------------------------------------------------------------------------- #


class DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(str(key))
        self.key = str(key)


def _parse_yaml(text: str) -> Any:
    """Parse YAML, treating a duplicate mapping key as an error.

    PyYAML silently keeps the LAST duplicate. For a manifest keyed by unit id
    that would mean a second `unit-a:` block quietly replaces the first, so
    "duplicate ids are refused" has to be enforced at the parser, not after.
    """
    import yaml  # noqa: PLC0415

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise DuplicateKeyError(key)
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - strict SafeLoader subclass


# --------------------------------------------------------------------------- #
# Field validation helpers
# --------------------------------------------------------------------------- #


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_cwd(raw: Any, unit_id: str, out: list[Finding]) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        out.append(Finding("invalid_cwd", "`cwd` must be a non-empty string", unit_id, "cwd"))
        return None
    candidate = raw.strip()
    if PurePosixPath(candidate).is_absolute() or (len(candidate) > 1 and candidate[1] == ":"):
        out.append(
            Finding(
                "unsafe_cwd",
                f"`cwd` must be repo-relative, got absolute path {candidate!r}",
                unit_id,
                "cwd",
            )
        )
        return None
    if candidate.startswith("~"):
        out.append(
            Finding("unsafe_cwd", "`cwd` must not start with '~'", unit_id, "cwd")
        )
        return None
    # Reject any path that climbs out of the repo, including sneaky ones like
    # a/../../b that only escape after normalization.
    parts = PurePosixPath(candidate).parts
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
        elif part not in (".", ""):
            depth += 1
        if depth < 0:
            out.append(
                Finding(
                    "unsafe_cwd",
                    f"`cwd` escapes the repository root: {candidate!r}",
                    unit_id,
                    "cwd",
                )
            )
            return None
    return candidate


def _check_command(raw: Any, unit_id: str, out: list[Finding]) -> tuple[str, ...]:
    if isinstance(raw, str):
        out.append(
            Finding(
                "command_not_argv",
                "`command` must be an argv list, not a shell string; "
                f"write ['{raw.split(' ')[0]}', ...] so execution needs no shell",
                unit_id,
                "command",
            )
        )
        return ()
    if raw is None:
        out.append(Finding("missing_command", "unit has no `command`", unit_id, "command"))
        return ()
    if not _is_str_list(raw):
        out.append(
            Finding("command_not_argv", "`command` must be a list of strings", unit_id, "command")
        )
        return ()
    if not raw:
        out.append(Finding("empty_command", "`command` must not be empty", unit_id, "command"))
        return ()
    return tuple(raw)


def _check_env(raw: Any, unit_id: str, out: list[Finding]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not _is_str_list(raw):
        out.append(
            Finding("invalid_env", "`env` must be a list of variable NAMES", unit_id, "env")
        )
        return ()
    names: list[str] = []
    for item in raw:
        if "=" in item:
            out.append(
                Finding(
                    "env_value_supplied",
                    f"`env` carries names only, never values; got {item!r}. "
                    "A version-controlled manifest must not become a secret store.",
                    unit_id,
                    "env",
                )
            )
            continue
        if not ENV_NAME_PATTERN.match(item):
            out.append(
                Finding(
                    "invalid_env_name",
                    f"{item!r} is not a valid environment variable name",
                    unit_id,
                    "env",
                )
            )
            continue
        names.append(item)
    return tuple(names)


def _check_requires(raw: Any, unit_id: str, out: list[Finding]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        out.append(
            Finding("invalid_requires", "`requires` must be a mapping", unit_id, "requires")
        )
        return {}
    unknown = sorted(set(raw) - REQUIRES_KEYS)
    for key in unknown:
        out.append(
            Finding(
                "unknown_key",
                f"unknown `requires` key {key!r}; allowed: {sorted(REQUIRES_KEYS)}",
                unit_id,
                f"requires.{key}",
            )
        )
    resolved: dict[str, Any] = {}
    if "os" in raw:
        if not _is_str_list(raw["os"]):
            out.append(
                Finding("invalid_requires", "`requires.os` must be a list", unit_id, "requires.os")
            )
        else:
            bad = sorted(set(raw["os"]) - KNOWN_OS)
            for item in bad:
                out.append(
                    Finding(
                        "unknown_os",
                        f"unknown os {item!r}; known: {sorted(KNOWN_OS)}",
                        unit_id,
                        "requires.os",
                    )
                )
            resolved["os"] = list(raw["os"])
    if "python" in raw:
        if not isinstance(raw["python"], str):
            out.append(
                Finding(
                    "invalid_requires",
                    "`requires.python` must be a version spec string",
                    unit_id,
                    "requires.python",
                )
            )
        else:
            resolved["python"] = raw["python"]
    if "caps" in raw:
        if not _is_str_list(raw["caps"]):
            out.append(
                Finding(
                    "invalid_requires", "`requires.caps` must be a list", unit_id, "requires.caps"
                )
            )
        else:
            resolved["caps"] = list(raw["caps"])
    return resolved


def _check_unit(unit_id: str, raw: Any, out: list[Finding]) -> Unit | None:
    if not UNIT_ID_PATTERN.match(unit_id):
        out.append(
            Finding(
                "invalid_unit_id",
                f"unit id {unit_id!r} must match {UNIT_ID_PATTERN.pattern}",
                unit_id,
            )
        )
        return None
    if not isinstance(raw, dict):
        out.append(Finding("invalid_unit", "unit must be a mapping", unit_id))
        return None

    for key in sorted(set(raw) - UNIT_KEYS):
        out.append(
            Finding(
                "unknown_key",
                f"unknown unit key {key!r}; allowed: {sorted(UNIT_KEYS)}",
                unit_id,
                key,
            )
        )

    command = _check_command(raw.get("command"), unit_id, out)
    cwd = _check_cwd(raw.get("cwd"), unit_id, out)
    requires = _check_requires(raw.get("requires"), unit_id, out)
    env = _check_env(raw.get("env"), unit_id, out)

    services: tuple[str, ...] = ()
    if raw.get("services") is not None:
        if _is_str_list(raw["services"]):
            services = tuple(raw["services"])
        else:
            out.append(
                Finding("invalid_services", "`services` must be a list", unit_id, "services")
            )

    depends_on: tuple[str, ...] = ()
    if raw.get("depends_on") is not None:
        if _is_str_list(raw["depends_on"]):
            depends_on = tuple(raw["depends_on"])
        else:
            out.append(
                Finding("invalid_depends_on", "`depends_on` must be a list", unit_id, "depends_on")
            )

    artifacts: tuple[str, ...] = ()
    if raw.get("artifacts") is not None:
        if _is_str_list(raw["artifacts"]):
            artifacts = tuple(raw["artifacts"])
        else:
            out.append(
                Finding("invalid_artifacts", "`artifacts` must be a list", unit_id, "artifacts")
            )

    timeout_s: int | None = None
    if raw.get("timeout_s") is not None:
        value = raw["timeout_s"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            out.append(
                Finding(
                    "invalid_timeout",
                    "`timeout_s` must be a positive integer number of seconds",
                    unit_id,
                    "timeout_s",
                )
            )
        else:
            timeout_s = value

    resource_group = None
    if raw.get("resource_group") is not None:
        if isinstance(raw["resource_group"], str) and raw["resource_group"].strip():
            resource_group = raw["resource_group"].strip()
        else:
            out.append(
                Finding(
                    "invalid_resource_group",
                    "`resource_group` must be a non-empty string",
                    unit_id,
                    "resource_group",
                )
            )

    exclusivity = "shared"
    if raw.get("exclusivity") is not None:
        if raw["exclusivity"] in EXCLUSIVITY_VALUES:
            exclusivity = raw["exclusivity"]
        else:
            out.append(
                Finding(
                    "invalid_exclusivity",
                    f"`exclusivity` must be one of {sorted(EXCLUSIVITY_VALUES)}",
                    unit_id,
                    "exclusivity",
                )
            )

    cache = None
    if raw.get("cache") is not None:
        if raw["cache"] in CACHE_VALUES:
            cache = raw["cache"]
        else:
            out.append(
                Finding(
                    "invalid_cache",
                    f"`cache` must be one of {sorted(CACHE_VALUES)}",
                    unit_id,
                    "cache",
                )
            )

    if not command:
        return None
    return Unit(
        id=unit_id,
        command=command,
        cwd=cwd,
        requires=requires,
        services=services,
        depends_on=depends_on,
        timeout_s=timeout_s,
        artifacts=artifacts,
        resource_group=resource_group,
        exclusivity=exclusivity,
        cache=cache,
        env=env,
    )


def _check_groups(
    raw: Any,
    units: dict[str, Unit],
    out: list[Finding],
    declared_ids: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, ...]]:
    if raw is None:
        out.append(
            Finding(
                "missing_groups",
                f"`groups` is required and must declare {DEFAULT_GROUP!r}; "
                "there is no auto-discovery",
                location="groups",
            )
        )
        return {}
    if not isinstance(raw, dict):
        out.append(Finding("invalid_groups", "`groups` must be a mapping", location="groups"))
        return {}

    groups: dict[str, tuple[str, ...]] = {}
    for name, members in raw.items():
        location = f"groups.{name}"
        if not isinstance(name, str) or not name.strip():
            out.append(Finding("invalid_group_name", "group names must be strings", location=location))
            continue
        if not _is_str_list(members):
            out.append(
                Finding("invalid_group", f"group {name!r} must be a list of unit ids", location=location)
            )
            continue
        seen: set[str] = set()
        resolved: list[str] = []
        for member in members:
            if member in seen:
                out.append(
                    Finding(
                        "ambiguous_group_membership",
                        f"group {name!r} lists unit {member!r} more than once; "
                        "membership must be unambiguous",
                        member,
                        location,
                    )
                )
                continue
            if member not in units:
                # A unit that WAS declared but failed validation is already
                # reported against the unit itself. Repeating it here as
                # "unknown" would bury the real cause under a cascade.
                if member not in declared_ids:
                    out.append(
                        Finding(
                            "unknown_group_member",
                            f"group {name!r} references unknown unit {member!r}",
                            member,
                            location,
                        )
                    )
                continue
            seen.add(member)
            resolved.append(member)
        groups[name] = tuple(resolved)

    if DEFAULT_GROUP not in groups:
        out.append(
            Finding(
                "missing_default_group",
                f"`groups.{DEFAULT_GROUP}` is required; a manifest never means "
                "'everything I happened to find'",
                location="groups",
            )
        )
    return groups


def _check_dependencies(
    units: dict[str, Unit],
    out: list[Finding],
    declared_ids: frozenset[str] = frozenset(),
) -> None:
    for unit in units.values():
        for dep in unit.depends_on:
            if dep == unit.id:
                out.append(
                    Finding(
                        "dependency_cycle",
                        f"unit {unit.id!r} depends on itself",
                        unit.id,
                        "depends_on",
                    )
                )
            elif dep not in units and dep not in declared_ids:
                out.append(
                    Finding(
                        "undeclared_dependency",
                        f"unit {unit.id!r} depends on undeclared unit {dep!r}",
                        unit.id,
                        "depends_on",
                    )
                )
    cycle = find_cycle(units)
    if cycle:
        out.append(
            Finding(
                "dependency_cycle",
                "dependency cycle: " + " -> ".join(cycle),
                cycle[0],
                "depends_on",
            )
        )


def find_cycle(units: dict[str, Unit]) -> list[str]:
    """Return one cycle as a node list, or [] when the graph is a DAG."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {uid: WHITE for uid in units}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        color[node] = GREY
        stack.append(node)
        for dep in units[node].depends_on:
            if dep not in units or dep == node:
                continue
            if color[dep] == GREY:
                start = stack.index(dep)
                return [*stack[start:], dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        color[node] = BLACK
        stack.pop()
        return []

    for uid in sorted(units):
        if color[uid] == WHITE:
            found = visit(uid)
            if found:
                return found
    return []


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_manifest(text: str) -> tuple[Manifest | None, list[Finding]]:
    """Parse and validate manifest text. Returns (manifest, schema findings)."""
    out: list[Finding] = []
    try:
        raw = _parse_yaml(text)
    except DuplicateKeyError as exc:
        return None, [
            Finding(
                "duplicate_unit_id",
                f"duplicate key {exc.key!r}; PyYAML would silently keep the last one",
            )
        ]
    except ImportError:
        return None, [Finding("pyyaml_missing", "PyYAML is required to read the manifest")]
    except Exception as exc:  # noqa: BLE001 - any parse error is a typed finding
        return None, [Finding("invalid_yaml", f"manifest is not valid YAML: {exc}")]

    if raw is None:
        return None, [Finding("empty_manifest", "manifest is empty")]
    if not isinstance(raw, dict):
        return None, [Finding("invalid_manifest", "manifest must be a mapping at the top level")]

    for key in sorted(set(raw) - TOP_LEVEL_KEYS):
        out.append(
            Finding("unknown_key", f"unknown top-level key {key!r}; allowed: {sorted(TOP_LEVEL_KEYS)}", location=key)
        )

    version = raw.get("schema_version")
    if version is None:
        out.append(
            Finding(
                "missing_schema_version",
                f"`schema_version` is required; this loader supports {list(SUPPORTED_SCHEMA_VERSIONS)}",
                location="schema_version",
            )
        )
        return None, out
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_SCHEMA_VERSIONS:
        out.append(
            Finding(
                "unknown_schema_version",
                f"unsupported schema_version {version!r}; supported: {list(SUPPORTED_SCHEMA_VERSIONS)}",
                location="schema_version",
            )
        )
        return None, out

    raw_units = raw.get("units")
    units: dict[str, Unit] = {}
    declared_ids: set[str] = set()
    if raw_units is None:
        out.append(Finding("missing_units", "`units` is required", location="units"))
    elif not isinstance(raw_units, dict):
        out.append(Finding("invalid_units", "`units` must be a mapping of id -> unit", location="units"))
    else:
        for unit_id, raw_unit in raw_units.items():
            declared_ids.add(str(unit_id))
            unit = _check_unit(str(unit_id), raw_unit, out)
            if unit is not None:
                units[unit.id] = unit

    _check_dependencies(units, out, frozenset(declared_ids))
    groups = _check_groups(raw.get("groups"), units, out, frozenset(declared_ids))

    manifest = Manifest(schema_version=version, units=units, groups=groups)
    return manifest, out


def load_manifest(repo_root: Path) -> tuple[Manifest | None, list[Finding]]:
    path = Path(repo_root) / MANIFEST_RELPATH
    if not path.is_file():
        return None, [
            Finding("manifest_missing", f"no test manifest at {MANIFEST_RELPATH}", location=MANIFEST_RELPATH)
        ]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [Finding("manifest_unreadable", f"cannot read {MANIFEST_RELPATH}: {exc}")]
    return parse_manifest(text)


def compile_plan(manifest: Manifest, group: str = DEFAULT_GROUP) -> tuple[list[Unit], list[Finding]]:
    """Resolve a group into dependency-ordered units.

    Dependencies are pulled in transitively even when the group does not list
    them: a group names what you want run, not the full closure required to run
    it. Ordering is deterministic (dependencies first, then declaration order).
    """
    out: list[Finding] = []
    if group not in manifest.groups:
        out.append(
            Finding(
                "unknown_group",
                f"unknown group {group!r}; declared: {sorted(manifest.groups)}",
                location=f"groups.{group}",
            )
        )
        return [], out

    cycle = find_cycle(manifest.units)
    if cycle:
        out.append(
            Finding("dependency_cycle", "dependency cycle: " + " -> ".join(cycle), cycle[0])
        )
        return [], out

    ordered: list[Unit] = []
    emitted: set[str] = set()

    def emit(unit_id: str) -> None:
        if unit_id in emitted or unit_id not in manifest.units:
            return
        unit = manifest.units[unit_id]
        for dep in unit.depends_on:
            emit(dep)
        if unit_id not in emitted:
            emitted.add(unit_id)
            ordered.append(unit)

    for member in manifest.groups[group]:
        emit(member)
    return ordered, out


def detect_drift(manifest: Manifest, repo_root: Path) -> list[Finding]:
    """Manifest/reality mismatches. NEVER test outcomes.

    A red test is a test result. Drift is "this manifest describes a world that
    does not exist here" -- a command that is not installed, a cwd that is not
    there. Conflating them would make EXIT_DRIFT meaningless.
    """
    root = Path(repo_root)
    out: list[Finding] = []
    for unit_id in sorted(manifest.units):
        unit = manifest.units[unit_id]
        if unit.cwd is not None and not (root / unit.cwd).is_dir():
            out.append(
                Finding(
                    "cwd_not_found",
                    f"declared cwd {unit.cwd!r} does not exist",
                    unit_id,
                    "cwd",
                )
            )
        if not unit.command:
            continue
        executable = unit.command[0]
        if os.sep in executable or executable.startswith("."):
            if not (root / executable).exists():
                out.append(
                    Finding(
                        "command_not_found",
                        f"declared command {executable!r} does not exist in this repo",
                        unit_id,
                        "command",
                    )
                )
        elif shutil.which(executable) is None:
            out.append(
                Finding(
                    "command_not_found",
                    f"declared command {executable!r} is not on PATH here",
                    unit_id,
                    "command",
                )
            )
    return out
