"""Per-machine root-mapping profiles + current-machine detection.

Standalone loader for ``skillbox-config/machines.yaml``. It lets policy readers
stop branching on ``socket.gethostname()`` and stop hard-coding root paths like
``/srv/skillbox/repos`` vs ``/Users/operator/repos`` all over the place.

This module is intentionally **standalone**: it is NOT wired into policy
evaluation or any existing code path yet. Consumers land in sibling beads.

Public API
----------
Detection / config:
    ``MACHINE_ENV_VAR``            env var that overrides hostname detection.
    ``find_machines_yaml(...)``    locate ``machines.yaml`` (skillbox-config).
    ``load_machines_config(...)``  parse it into a :class:`MachinesConfig`.
    ``detect_machine_id(...)``     resolve the current machine id.
    ``current_profile(...)``       resolve the current :class:`MachineProfile`.

Path operations (all on :class:`MachinesConfig` / module helpers):
    ``canonicalize_alias(path)``   rewrite an alias path to its real tree.
    ``translate_path(path, src, dst)``  map a path from one machine's root to
                                   the equivalent path under another machine's
                                   root (both directions).
    ``is_foreign_path(path, machine)``  True when a path belongs to a *different*
                                   machine's roots than ``machine``.
    ``classify_path(path)``        which machine(s)/roots a path lives under.

Data classes:
    :class:`MachineProfile`, :class:`MachinesConfig`, :class:`MachineAlias`.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

try:  # PyYAML is optional in this repo; mirror the guard used elsewhere.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only without PyYAML
    yaml = None


# Env var that overrides hostname-based machine detection. Mirrors the
# "overridable via env" convention used by other runtime config knobs.
MACHINE_ENV_VAR = "SKILLBOX_MACHINE"

# Env var that lets a caller (or test) point directly at a machines.yaml file,
# bypassing the candidate search. Resolution prefers this when set.
MACHINES_FILE_ENV_VAR = "SKILLBOX_MACHINES_FILE"

# The config file name, and the private-config repo dir name it lives beside
# (next to skill-scope.yaml). The repo is checked out under different parents on
# different machines, so we search a small set of candidate locations rather
# than hard-coding one absolute path (which would break on the mac profile).
MACHINES_FILE_NAME = "machines.yaml"
PRIVATE_CONFIG_DIR_NAME = "skillbox-config"

SUPPORTED_CONFIG_VERSION = 1


class MachinesConfigError(RuntimeError):
    """Raised when machines.yaml is missing, unparseable, or malformed."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MachineAlias:
    """A symlink/bind alias whose ``alias`` path is the same tree as ``canonical``."""

    alias: str
    canonical: str


@dataclass(frozen=True)
class MachineProfile:
    """One machine's identity and root-mapping declaration.

    ``repo_roots`` and ``projects_roots`` are ordered; the first entry is the
    *canonical* root for that category on this machine and is what root
    translation maps between machines.
    """

    machine_id: str
    hostnames: tuple[str, ...] = ()
    home: str | None = None
    managed_home: str | None = None
    repo_roots: tuple[str, ...] = ()
    projects_roots: tuple[str, ...] = ()

    @property
    def canonical_repo_root(self) -> str | None:
        return self.repo_roots[0] if self.repo_roots else None

    @property
    def canonical_projects_root(self) -> str | None:
        return self.projects_roots[0] if self.projects_roots else None

    def all_roots(self) -> tuple[str, ...]:
        """Every declared root (repos + projects), in declaration order."""
        return tuple(self.repo_roots) + tuple(self.projects_roots)


@dataclass(frozen=True)
class MachinesConfig:
    """Parsed machines.yaml: machine profiles + alias declarations."""

    machines: dict[str, MachineProfile] = field(default_factory=dict)
    aliases: tuple[MachineAlias, ...] = ()
    source_path: str | None = None

    # -- lookup ----------------------------------------------------------

    def get(self, machine_id: str) -> MachineProfile | None:
        return self.machines.get(machine_id)

    def require(self, machine_id: str) -> MachineProfile:
        profile = self.machines.get(machine_id)
        if profile is None:
            available = ", ".join(sorted(self.machines)) or "(none)"
            raise MachinesConfigError(
                f"Unknown machine id {machine_id!r}. Declared machines: {available}."
            )
        return profile

    def detect_machine_id(
        self,
        *,
        hostname: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str | None:
        """Resolve the current machine id.

        Precedence:
          1. ``SKILLBOX_MACHINE`` env override (must name a declared machine),
          2. short-hostname match against each profile's ``hostnames``.

        Mirrors the ``IS_INDEX_HOST`` detection style in
        ``skillbox-config/scripts/sbp_cass.py``: compare the short hostname
        (``socket.gethostname().split(".")[0]``) against a small fixed set,
        case-insensitively.
        """
        env = os.environ if env is None else env
        override = str(env.get(MACHINE_ENV_VAR) or "").strip()
        if override:
            # An explicit override that does not name a declared machine is a
            # configuration error worth surfacing, not a silent fallback.
            self.require(override)
            return override

        short = _short_hostname(hostname)
        if not short:
            return None
        short_lower = short.lower()
        for machine_id, profile in self.machines.items():
            if any(short_lower == host.lower() for host in profile.hostnames):
                return machine_id
        return None

    def current_profile(
        self,
        *,
        hostname: str | None = None,
        env: dict[str, str] | None = None,
    ) -> MachineProfile | None:
        machine_id = self.detect_machine_id(hostname=hostname, env=env)
        if machine_id is None:
            return None
        return self.machines.get(machine_id)

    # -- alias canonicalization -----------------------------------------

    def canonicalize_alias(self, path: str | os.PathLike[str]) -> str:
        """Rewrite a path under a declared alias to its canonical tree.

        e.g. ``/srv/repos/x`` -> ``/srv/skillbox/repos/x``. Aliases are
        machine-agnostic; an alias that does not apply on the current machine
        is simply a no-op (its prefix won't match). The longest matching alias
        wins, and the result is itself re-canonicalized so chained aliases
        collapse fully.
        """
        result = _normalize(path)
        # Re-apply until a fixed point so chained aliases (a->b, b->c) collapse.
        for _ in range(len(self.aliases) + 1):
            changed = False
            best = _longest_prefix_alias(result, self.aliases)
            if best is not None:
                alias, remainder = best
                result = _join_under(alias.canonical, remainder)
                changed = True
            if not changed:
                break
        return result

    # -- root translation -----------------------------------------------

    def translate_path(
        self,
        path: str | os.PathLike[str],
        src_machine: str,
        dst_machine: str,
        *,
        category: str | None = None,
    ) -> str | None:
        """Map a path under ``src_machine``'s root to ``dst_machine``'s root.

        Symmetric: ``translate_path(p, A, B)`` and its inverse on the result
        round-trip. Aliases on the source side are canonicalized first, so an
        alias path translates as if it were the canonical path.

        ``category`` may be ``"repos"`` or ``"projects"`` to constrain which
        root family to match; default tries repos then projects.

        Returns the translated path, or ``None`` if ``path`` is not under any of
        ``src_machine``'s roots (i.e. there is nothing to translate).
        """
        src = self.require(src_machine)
        dst = self.require(dst_machine)
        canon = self.canonicalize_alias(path)

        for src_roots, dst_roots in self._root_pairs(src, dst, category):
            # Home-anchor the SOURCE profile's ~ roots to its declared home, so a
            # foreign machine's ~/repos is not expanded against the local $HOME.
            match = _match_under_roots(canon, src_roots, profile=src)
            if match is None:
                continue
            _matched_root, remainder = match
            dst_root = dst_roots[0] if dst_roots else None
            if dst_root is None:
                return None
            # Home-anchor the DST canonical root to the dst profile's declared
            # home when it is ~-relative, so the translated path lands under the
            # foreign machine's home rather than this box's local $HOME.
            dst_base = _resolve_dst_root(dst_root, dst)
            if dst_base is None:
                return None
            return _join_under(dst_base, remainder)
        return None

    def _root_pairs(
        self,
        src: MachineProfile,
        dst: MachineProfile,
        category: str | None,
    ) -> Iterable[tuple[tuple[str, ...], tuple[str, ...]]]:
        if category in (None, "repos"):
            yield (src.repo_roots, dst.repo_roots)
        if category in (None, "projects"):
            yield (src.projects_roots, dst.projects_roots)

    # -- classification --------------------------------------------------

    def classify_path(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Describe which machine(s)/roots a path lives under.

        Returns a dict::

            {
              "input": <original>,
              "canonical": <alias-canonicalized>,
              "matches": [
                  {"machine": <id>, "category": "repos"|"projects",
                   "root": <root>, "remainder": <rel-or-"">},
                  ...
              ],
              "machines": [<machine-id>, ...],   # de-duped, declaration order
            }
        """
        canon = self.canonicalize_alias(path)
        matches: list[dict[str, Any]] = []
        for machine_id, profile in self.machines.items():
            for category, roots in (
                ("repos", profile.repo_roots),
                ("projects", profile.projects_roots),
            ):
                # Home-anchor this profile's ~ roots to ITS declared home, so a
                # foreign machine's ~/repos never matches a local-home path.
                hit = _match_under_roots(canon, roots, profile=profile)
                if hit is None:
                    continue
                root, remainder = hit
                matches.append(
                    {
                        "machine": machine_id,
                        "category": category,
                        "root": root,
                        "remainder": remainder,
                    }
                )
        machines: list[str] = []
        for entry in matches:
            if entry["machine"] not in machines:
                machines.append(entry["machine"])
        return {
            "input": str(path),
            "canonical": canon,
            "matches": matches,
            "machines": machines,
        }

    def is_foreign_path(self, path: str | os.PathLike[str], machine: str) -> bool:
        """True when ``path`` belongs to a machine's roots OTHER than ``machine``.

        A path under no declared root at all is *not* foreign (we make no claim
        about it). A path under ``machine``'s own roots is not foreign. A path
        that lives only under some *other* machine's roots is foreign.
        """
        self.require(machine)
        classified = self.classify_path(path)
        machines = classified["machines"]
        if not machines:
            return False
        if machine in machines:
            return False
        return True


# ---------------------------------------------------------------------------
# Locating + loading
# ---------------------------------------------------------------------------


def _runtime_root_dir() -> PurePosixPath:
    """Resolve the skillbox runtime root the way shared.py does.

    ``runtime_manager/`` -> ``.env-manager/`` -> repo root. Kept as a plain
    path computation (no import of the heavy package) so this module stays
    standalone and cheap to import in tests.
    """
    here = os.path.abspath(__file__)
    package_dir = os.path.dirname(here)          # runtime_manager/
    env_manager_dir = os.path.dirname(package_dir)  # .env-manager/
    root_dir = os.path.dirname(env_manager_dir)  # repo root
    return PurePosixPath(root_dir)


def _machines_file_candidates(
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Ordered candidate locations for machines.yaml.

    Resolution mirrors how runtime_manager finds skill-scope.yaml: it lives in
    the private config repo (``skillbox-config``), located *relative to* the
    runtime root rather than at one hard-coded absolute. We search, in order:

      1. ``SKILLBOX_MACHINES_FILE`` env override (a file path),
      2. an explicit ``config_root`` (the skillbox-config dir) if given,
      3. ``<runtime_root>/../skillbox-config`` — the DEFAULT_PRIVATE_REPO_REL
         convention used by shared.py (``Path("..") / "skillbox-config"``),
      4. ``<repos_root>/skillbox-config`` — the devbox layout, where the runtime
         repo is nested under ``opensource/`` and skillbox-config is a sibling
         of ``opensource/`` (two levels up).

    Returns absolute, expanded candidate file paths (existence not checked).
    """
    env = os.environ if env is None else env
    candidates: list[str] = []

    override = str(env.get(MACHINES_FILE_ENV_VAR) or "").strip()
    if override:
        candidates.append(_expand(override))

    if config_root is not None:
        candidates.append(_expand(os.path.join(str(config_root), MACHINES_FILE_NAME)))

    if root_dir is None:
        root = str(_runtime_root_dir())
    else:
        root = _expand(str(root_dir))

    # (3) sibling-of-runtime-root: ../skillbox-config
    candidates.append(
        _expand(os.path.join(root, "..", PRIVATE_CONFIG_DIR_NAME, MACHINES_FILE_NAME))
    )
    # (4) sibling-of-opensource: ../../skillbox-config (devbox nesting)
    candidates.append(
        _expand(os.path.join(root, "..", "..", PRIVATE_CONFIG_DIR_NAME, MACHINES_FILE_NAME))
    )

    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def find_machines_yaml(
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Return the first existing machines.yaml path, or None.

    See :func:`_machines_file_candidates` for the resolution order.
    """
    for candidate in _machines_file_candidates(
        root_dir=root_dir, config_root=config_root, env=env
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def load_machines_config(
    path: str | os.PathLike[str] | None = None,
    *,
    root_dir: str | os.PathLike[str] | None = None,
    config_root: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> MachinesConfig:
    """Load and parse machines.yaml into a :class:`MachinesConfig`.

    If ``path`` is given it is used directly. Otherwise the file is located via
    :func:`find_machines_yaml`. Raises :class:`MachinesConfigError` on a missing
    file, missing PyYAML, parse failure, or malformed/unsupported document.
    """
    if path is None:
        path = find_machines_yaml(root_dir=root_dir, config_root=config_root, env=env)
        if path is None:
            searched = _machines_file_candidates(
                root_dir=root_dir, config_root=config_root, env=env
            )
            raise MachinesConfigError(
                "machines.yaml not found. Searched: " + ", ".join(searched)
            )

    path = os.fspath(path)
    if yaml is None:
        raise MachinesConfigError(
            "Missing PyYAML. Install `python3-yaml` or `pip install pyyaml` "
            "to read machines.yaml."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle.read())
    except FileNotFoundError as exc:
        raise MachinesConfigError(f"machines.yaml not found: {path}") from exc
    except Exception as exc:  # pragma: no cover - defensive parse path
        raise MachinesConfigError(f"Failed to parse {path}: {exc}") from exc

    return _build_config(raw, source_path=path)


def _build_config(raw: Any, *, source_path: str | None) -> MachinesConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MachinesConfigError(
            f"Expected a YAML mapping in machines.yaml ({source_path})."
        )

    version = raw.get("version", SUPPORTED_CONFIG_VERSION)
    if version != SUPPORTED_CONFIG_VERSION:
        raise MachinesConfigError(
            f"Unsupported machines.yaml version {version!r} "
            f"(supported: {SUPPORTED_CONFIG_VERSION})."
        )

    raw_machines = raw.get("machines") or {}
    if not isinstance(raw_machines, dict):
        raise MachinesConfigError("machines.yaml `machines` must be a mapping.")

    machines: dict[str, MachineProfile] = {}
    for machine_id, body in raw_machines.items():
        machines[str(machine_id)] = _build_profile(str(machine_id), body)

    aliases = _build_aliases(raw.get("aliases"))

    return MachinesConfig(
        machines=machines,
        aliases=aliases,
        source_path=source_path,
    )


def _build_profile(machine_id: str, body: Any) -> MachineProfile:
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise MachinesConfigError(
            f"machines.yaml machine {machine_id!r} must be a mapping."
        )
    return MachineProfile(
        machine_id=machine_id,
        hostnames=_string_tuple(body.get("hostnames")),
        home=_optional_path(body.get("home")),
        managed_home=_optional_path(body.get("managed_home")),
        repo_roots=_path_tuple(body.get("repo_roots")),
        projects_roots=_path_tuple(body.get("projects_roots")),
    )


def _build_aliases(raw_aliases: Any) -> tuple[MachineAlias, ...]:
    if raw_aliases is None:
        return ()
    if not isinstance(raw_aliases, list):
        raise MachinesConfigError("machines.yaml `aliases` must be a list.")
    aliases: list[MachineAlias] = []
    for entry in raw_aliases:
        if not isinstance(entry, dict):
            raise MachinesConfigError(
                "machines.yaml `aliases` entries must be mappings with "
                "`alias` and `canonical`."
            )
        alias = _optional_path(entry.get("alias"))
        canonical = _optional_path(entry.get("canonical"))
        if not alias or not canonical:
            raise MachinesConfigError(
                "machines.yaml alias entries require both `alias` and `canonical`."
            )
        aliases.append(MachineAlias(alias=alias, canonical=canonical))
    return tuple(aliases)


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def detect_machine_id(
    config: MachinesConfig | None = None,
    *,
    hostname: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve the current machine id, loading config if not supplied."""
    config = config if config is not None else load_machines_config(env=env)
    return config.detect_machine_id(hostname=hostname, env=env)


def current_profile(
    config: MachinesConfig | None = None,
    *,
    hostname: str | None = None,
    env: dict[str, str] | None = None,
) -> MachineProfile | None:
    """Resolve the current machine profile, loading config if not supplied."""
    config = config if config is not None else load_machines_config(env=env)
    return config.current_profile(hostname=hostname, env=env)


def repo_roots_for_machine(config: MachinesConfig, machine_id: str) -> list[str]:
    """Expanded repo roots for a declared machine profile, longest first."""
    profile = config.require(machine_id)
    return sorted(
        _expand_roots_for_match(profile.repo_roots, profile),
        key=len,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Path helpers (pure; POSIX semantics — these are deployment paths, not the
# host's local filesystem, so we must not resolve symlinks on the running box)
# ---------------------------------------------------------------------------


def _expand(path: str) -> str:
    """Expand ~ and env vars against the LOCAL env, then normalize.

    Use ONLY for the current machine's own roots (or non-``~`` paths). For a
    FOREIGN machine's ``~``-relative root, see :func:`_expand_profile_root`:
    expanding the mac's ``~/repos`` against the devbox's local ``$HOME`` wrongly
    resolves it to ``/srv/skillbox/home/repos`` and misclassifies a managed-home
    path as belonging to the other machine (BUG E). No symlink resolution.
    """
    expanded = os.path.expanduser(os.path.expandvars(path))
    return _normalize(expanded)


def _starts_with_home_ref(path: str) -> bool:
    """True when ``path`` is ``~``-relative (``~`` or ``~/...``); NOT ``~user``."""
    text = str(path or "")
    return text == "~" or text.startswith("~/")


def _expand_profile_root(root: str, home: str | None) -> str | None:
    """Expand a profile root for matching, honoring the profile's declared home.

    A ``~``-relative root is expanded against ``home`` (the profile's declared
    ``home``/``managed_home``), NOT the local ``$HOME``, so a FOREIGN machine's
    ``~/repos`` resolves under THAT machine's home rather than this box's. When a
    ``~``-relative root has no declared home to anchor it, return ``None`` so the
    caller SKIPS it (a local-home path must not match an unanchored foreign ``~``
    root). Non-``~`` roots and ``$VAR`` expansion are unchanged.
    """
    raw = str(root or "")
    if _starts_with_home_ref(raw):
        if not home:
            return None
        remainder = raw[1:].lstrip("/")  # drop the leading "~"
        base = _normalize(os.path.expandvars(home))
        return _join_under(base, remainder) if remainder else base
    return _expand(raw)


def _normalize(path: str | os.PathLike[str]) -> str:
    """Collapse ``.``/``..``/duplicate slashes and strip a trailing slash.

    Uses POSIX path semantics regardless of the host OS, because the paths in
    machines.yaml describe target deployments (a Linux devbox and a macOS
    laptop), not necessarily the machine running this code.
    """
    text = os.fspath(path)
    # PurePosixPath normalizes separators but does not fold "..", so do both.
    posix = PurePosixPath(text)
    # Re-fold ".." segments without touching the filesystem.
    parts: list[str] = []
    for part in posix.parts:
        if part == "..":
            if parts and parts[-1] not in ("", "/"):
                parts.pop()
            else:
                parts.append(part)
        elif part == ".":
            continue
        else:
            parts.append(part)
    if not parts:
        return "."
    if parts[0] == "/":
        return "/" + "/".join(parts[1:]) if len(parts) > 1 else "/"
    return "/".join(parts)


def _short_hostname(hostname: str | None) -> str:
    raw = hostname if hostname is not None else socket.gethostname()
    return str(raw or "").split(".")[0].strip()


def _is_under(root: str, candidate: str) -> str | None:
    """If ``candidate`` is at/under ``root``, return the relative remainder.

    The remainder is ``""`` when ``candidate == root``. Returns ``None`` when
    not under ``root``. Comparison is on already-normalized POSIX paths and
    respects path-segment boundaries (so ``/a/bc`` is not under ``/a/b``).
    """
    root_n = _normalize(root)
    cand_n = _normalize(candidate)
    if cand_n == root_n:
        return ""
    prefix = root_n if root_n.endswith("/") else root_n + "/"
    if cand_n.startswith(prefix):
        return cand_n[len(prefix):]
    return None


def _profile_home_bases(profile: "MachineProfile | None") -> list[str]:
    """Candidate home bases for expanding a profile's ``~``-relative roots.

    Both the declared ``home`` and (when present) the agent-managed
    ``managed_home`` anchor ``~`` for that machine, so a ``~/repos`` root matches
    under either home tree. Empty when the profile declares no home — its ``~``
    roots are then skipped rather than matched against the local ``$HOME``.
    """
    if profile is None:
        return []
    bases: list[str] = []
    for candidate in (profile.home, profile.managed_home):
        if candidate and str(candidate).strip():
            bases.append(str(candidate))
    return bases


def _expand_roots_for_match(
    roots: Iterable[str], profile: "MachineProfile | None"
) -> list[str]:
    """Expand a profile's roots for matching, home-anchoring its ``~`` roots.

    Non-``~`` roots expand against the local env (deployment-absolute paths). A
    ``~``-relative root expands against EACH of the profile's declared home bases
    (:func:`_profile_home_bases`); with no declared home it is SKIPPED — never
    matched against the local ``$HOME`` (BUG E). When no profile is supplied the
    legacy local-``~`` behavior is preserved so the current-machine path is
    unaffected.
    """
    expanded: list[str] = []
    homes = _profile_home_bases(profile)
    for root in roots:
        raw = str(root or "")
        if _starts_with_home_ref(raw):
            if profile is None:
                expanded.append(_expand(raw))  # legacy: current-machine local ~
                continue
            for home in homes:
                resolved = _expand_profile_root(raw, home)
                if resolved is not None:
                    expanded.append(resolved)
            # No declared home -> skip this ~ root entirely.
        else:
            expanded.append(_expand(raw))
    return expanded


def _resolve_dst_root(root: str, profile: "MachineProfile | None") -> str | None:
    """Expand a translation DESTINATION root, home-anchoring a ~ root.

    A ~-relative dst canonical root expands against the dst profile's declared
    home (NOT the local $HOME); with no declared home it cannot be anchored, so
    return ``None`` (nothing to translate to). Non-~ roots expand normally.
    """
    raw = str(root or "")
    if _starts_with_home_ref(raw):
        for home in _profile_home_bases(profile):
            resolved = _expand_profile_root(raw, home)
            if resolved is not None:
                return resolved
        return None
    return _expand(raw)


def _match_under_roots(
    path: str,
    roots: Iterable[str],
    *,
    profile: "MachineProfile | None" = None,
) -> tuple[str, str] | None:
    """Return ``(matched_root, remainder)`` for the longest matching root.

    ``profile`` (when given) home-anchors the profile's ``~``-relative roots so a
    FOREIGN machine's ``~/repos`` is NOT expanded against the local ``$HOME``
    (BUG E). The returned ``matched_root`` is the EXPANDED path actually matched
    (so a ``~`` root reports its home-anchored form, not the raw ``~`` spelling).
    """
    best: tuple[str, str] | None = None
    best_len = -1
    for expanded_root in _expand_roots_for_match(roots, profile):
        remainder = _is_under(expanded_root, path)
        if remainder is None:
            continue
        root_len = len(expanded_root)
        if root_len > best_len:
            best_len = root_len
            best = (expanded_root, remainder)
    return best


def _longest_prefix_alias(
    path: str, aliases: Iterable[MachineAlias]
) -> tuple[MachineAlias, str] | None:
    best: tuple[MachineAlias, str] | None = None
    best_len = -1
    for alias in aliases:
        remainder = _is_under(_expand(alias.alias), path)
        if remainder is None:
            continue
        alias_len = len(_expand(alias.alias))
        if alias_len > best_len:
            best_len = alias_len
            best = (alias, remainder)
    return best


def _join_under(root: str, remainder: str) -> str:
    root_n = _expand(root)
    if not remainder:
        return root_n
    return _normalize(root_n.rstrip("/") + "/" + remainder)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _path_tuple(value: Any) -> tuple[str, ...]:
    # Paths are kept in their declared (un-expanded) form so profiles read back
    # exactly as written; expansion happens at comparison time via _expand.
    return _string_tuple(value)


def _optional_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
