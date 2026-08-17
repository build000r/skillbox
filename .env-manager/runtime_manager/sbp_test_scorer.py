"""Read-only ``suite-readiness/v1`` scorer adapters (skillbox-sbp-test-scorer-adapters-jyg2).

This module turns a repository into a :mod:`runtime_manager.sbp_test_findings`
report. It owns *evidence gathering only*: the finding codes, evidence states,
gate authority and rollup all live in the registry, and this module may not
invent any of them.

**Narrow typed adapters, never universal parsing.** The duel's consensus was
that "stdlib parse of Makefile targets" understates includes, pattern rules,
evaluated variables and recursive make. So each adapter records not only what it
read but what it *could not* read (:attr:`AdapterRead.gaps`), and the verdict
rule is uniform:

* literal positive evidence            -> ``proven``
* no evidence, and the parse was total -> ``Cleared`` (with the absence as evidence)
* no evidence, but the parse had gaps  -> ``unknown`` + a manual-manifest next action

That third line is the whole point. A missing hit in a partially-parsed Makefile
is not a clean bill of health, and the registry refuses to let it become one.

**Nothing here executes the repository.** Every default-path adapter reads
bytes. ``make -qp`` is implemented (:func:`probe_make_database`) because make's
own database is the only honest way to resolve a real target list, but it is
**off by default and never enabled by the CLI**: `-qp` still evaluates
``$(shell ...)`` at parse time, which is arbitrary repo-script execution by
another name. It is additionally hazard-gated -- a Makefile carrying any
construct that could execute or expand (``$(shell``, ``!=``, ``include``,
``$(eval``, ``$(wildcard``) is refused before make is ever invoked -- bounded by
a timeout and an output cap, and run with a sanitized environment and closed
stdin. Callers that want it must opt in explicitly, and the report always says
which of the two paths produced it.

**Analysis is not a manifest.** Raw-repo output is an inspection result, never a
generated `.skillbox/test.yaml`. When a manifest is present it is read as
declared truth and improves the evidence; when it is absent the report says so
and carries a manual-manifest next action.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import sbp_test_findings as R
from . import sbp_test_manifest as manifest_schema

SCORER_SCHEMA_VERSION = "2026-08-16+sbp_test_scorer"

MAKEFILE_NAMES: tuple[str, ...] = ("Makefile", "makefile", "GNUmakefile")
COMPOSE_NAMES: tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)
PYPROJECT_NAME = "pyproject.toml"
PACKAGE_JSON_NAME = "package.json"

#: Read caps. A scorer that can be made to read a gigabyte is a denial of
#: service against the agent that called it.
MAX_READ_BYTES = 512 * 1024
MAX_CHILD_PACKAGES = 64
MAX_CONFTESTS = 64
MAX_SCAN_DEPTH = 4

#: `make -qp` bounds, used only on the opt-in path.
MAKE_DB_TIMEOUT_S = 5
MAKE_DB_MAX_BYTES = 4 * 1024 * 1024

#: Directories never descended into. Vendored trees are not this repo's suite.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        "vendor",
        ".tox",
    }
)

TEST_SCRIPT_NAMES = ("test", "tests", "test:unit", "test:ci", "check", "vitest", "jest")

#: Constructs that defeat a static Makefile read, and would also make `make -qp`
#: unsafe. Each maps to the gap reason reported to the caller.
MAKE_HAZARD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\$\(shell\b", "shell_expansion"),
    (r"\$\(eval\b", "eval_directive"),
    (r"\$\(wildcard\b", "wildcard_expansion"),
    (r"^\s*[-s]?include\b", "include_directive"),
    (r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*!=", "shell_assignment"),
    (r"^[^\t#][^:=]*%[^:=]*:", "pattern_rule"),
)

_TARGET_RE = re.compile(r"^(?P<name>[A-Za-z0-9_][A-Za-z0-9_./+-]*)\s*::?(?!=)\s*(?P<prereqs>.*)$")
_VAR_REF_RE = re.compile(r"\$[({][A-Za-z_][A-Za-z0-9_]*[)}]")
_ABS_PATH_RE = re.compile(r"(?<![\w$])/(?:Users|home|opt|srv|var|usr|tmp|private)/[\w./-]+")
_ESCAPING_REL_RE = re.compile(r"\.\./\.\.")
_LOCK_RE = re.compile(r"\bflock\b|\.lock\b|lockfile", re.IGNORECASE)
#: A partition vocabulary is a *parameter* the caller supplies, not a filter the
#: repo happens to hard-code. `-k`/`-m` are deliberately absent: a fixed
#: `-m "not db"` selects the same tests on every machine, and `python3 -m
#: unittest` is not a selector at all -- treating either as a partition is how a
#: scorer clears a code it should have proven.
_SHARD_VAR_RE = re.compile(
    r"\$[({]\s*(SHARD|PARTITION|GROUP|SPLIT|CHUNK|BUCKET|SLICE)[A-Z_]*\s*[)}]"
)
_XDIST_RE = re.compile(r"(?:^|\s)-n\s+(?:auto|\d+)|\bxdist\b")
_JUNIT_RE = re.compile(r"--junit-?xml|junit\.xml|--report-file|--reporter=junit")
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ScorerRefusal(Exception):
    """A typed refusal. Never a traceback on stdout."""

    def __init__(self, code: str, message: str, next_actions: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_actions = tuple(next_actions)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "error_code": self.code,
            "next_actions": list(self.next_actions),
        }


# --------------------------------------------------------------------------- #
# Adapter reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdapterRead:
    """What one adapter established, and what it could not.

    ``gaps`` is the honest half: every construct the adapter refused to guess at.
    A gap is not an error -- it is the reason a downstream verdict is ``unknown``
    instead of clean.
    """

    name: str
    present: bool
    gaps: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    def gap_reason(self) -> str:
        return ", ".join(self.gaps)


@dataclass(frozen=True)
class Surface:
    """Everything the adapters read, plus the manifest when one exists."""

    root: Path
    make: AdapterRead
    package: AdapterRead
    pytest: AdapterRead
    compose: AdapterRead
    manifest_present: bool
    manifest_units: tuple[str, ...] = ()
    manifest_artifacts: tuple[str, ...] = ()
    manifest_issues: tuple[str, ...] = ()

    @property
    def adapters(self) -> tuple[AdapterRead, ...]:
        return (self.make, self.package, self.pytest, self.compose)

    @property
    def has_test_surface(self) -> bool:
        return self.manifest_present or any(a.present for a in self.adapters)


def _read_text(path: Path) -> str:
    """Bounded read. Anything bigger is not a config file we should be parsing."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_READ_BYTES + 1)
    if len(raw) > MAX_READ_BYTES:
        raise ScorerRefusal(
            "file_too_large",
            f"{path.name} exceeds the {MAX_READ_BYTES} byte read cap",
            ["declare the test contract in .skillbox/test.yaml instead"],
        )
    return raw.decode("utf-8", errors="replace")


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace(os.sep, "/")


def _line_of(text: str, needle: str, *, start: int = 1) -> int:
    """1-indexed line of the first occurrence, or ``start`` when absent.

    Evidence must point somewhere real; falling back to the file's first line is
    honest (the file is the evidence) and keeps locators deterministic.
    """
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return start


def _iter_files(root: Path, name: str, *, limit: int) -> list[Path]:
    """Bounded, deterministic walk for a given filename."""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(found) < limit:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if depth < MAX_SCAN_DEPTH and entry.name not in SKIP_DIRS:
                    stack.append((entry, depth + 1))
            elif entry.name == name and entry != root / name:
                found.append(entry)
                if len(found) >= limit:
                    break
    return sorted(found)


# --------------------------------------------------------------------------- #
# Make adapter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MakeTarget:
    name: str
    line: int
    prereqs: tuple[str, ...]
    recipe: tuple[tuple[int, str], ...]

    @property
    def recipe_text(self) -> str:
        return "\n".join(text for _, text in self.recipe)


def read_make(root: Path) -> AdapterRead:
    """Static, bounded Makefile read. Executes nothing.

    Hazards are recorded rather than worked around: a Makefile that computes its
    own targets is not statically knowable, and pretending otherwise is exactly
    the failure mode the duel called out.
    """
    path = next((root / name for name in MAKEFILE_NAMES if (root / name).is_file()), None)
    if path is None:
        return AdapterRead("make", present=False)

    text = _read_text(path)
    relpath = _rel(root, path)
    gaps: list[str] = []
    for pattern, reason in MAKE_HAZARD_PATTERNS:
        if re.search(pattern, text, re.MULTILINE):
            gaps.append(reason)
    if "$(MAKE)" in text or "${MAKE}" in text:
        gaps.append("recursive_make")

    targets: dict[str, MakeTarget] = {}
    phony: set[str] = set()
    current: str | None = None
    recipe: list[tuple[int, str]] = []
    pending: dict[str, Any] | None = None

    def _flush() -> None:
        nonlocal pending, recipe, current
        if pending is not None and current is not None:
            targets[current] = MakeTarget(
                current, pending["line"], tuple(pending["prereqs"]), tuple(recipe)
            )
        pending, recipe, current = None, [], None

    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("\t"):
            if current is not None:
                recipe.append((number, line.strip()))
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(".PHONY"):
            _flush()
            phony.update(stripped.split(":", 1)[-1].split())
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:+?!]?=", stripped):
            _flush()
            continue
        match = _TARGET_RE.match(stripped)
        if match:
            _flush()
            current = match.group("name")
            pending = {
                "line": number,
                "prereqs": tuple(match.group("prereqs").split()),
            }
    _flush()

    return AdapterRead(
        "make",
        present=True,
        gaps=tuple(sorted(set(gaps))),
        facts={
            "path": relpath,
            "text": text,
            "targets": targets,
            "phony": sorted(phony),
        },
    )


def probe_make_database(root: Path, read: AdapterRead) -> AdapterRead:
    """Opt-in ``make -qp``: make's own database, only where it is safe.

    Refused outright when the static read found any hazard, because `-qp` still
    *evaluates* the makefile -- ``$(shell date > /tmp/x)`` runs during a database
    dump. With the hazard gate clean there is nothing left to evaluate, so this
    is a bounded parse and not repo-script execution. Bounded further by a
    timeout, an output cap, closed stdin and a sanitized environment.

    Never called by the CLI. Enrichment only: it can add targets, never remove a
    gap that the static read recorded.
    """
    if not read.present:
        return read
    if read.gaps:
        facts = dict(read.facts)
        facts["make_database"] = {"probed": False, "refused": "hazards:" + ",".join(read.gaps)}
        return AdapterRead("make", True, read.gaps, facts)
    make_bin = shutil.which("make")
    if make_bin is None:
        facts = dict(read.facts)
        facts["make_database"] = {"probed": False, "refused": "make_not_on_path"}
        return AdapterRead("make", True, (*read.gaps, "make_not_available"), facts)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LC_ALL": "C",
        "MAKEFLAGS": "",
        "MAKELEVEL": "",
    }
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, bounded
            [make_bin, "-qp", "--no-print-directory"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=MAKE_DB_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        facts = dict(read.facts)
        facts["make_database"] = {"probed": False, "refused": type(exc).__name__}
        return AdapterRead("make", True, (*read.gaps, "make_database_unavailable"), facts)

    stdout = completed.stdout[:MAKE_DB_MAX_BYTES]
    database_targets = sorted(
        {
            match.group("name")
            for line in stdout.splitlines()
            if not line.startswith(("\t", "#", " "))
            for match in [_TARGET_RE.match(line.strip())]
            if match and not match.group("name").startswith(".")
        }
    )
    facts = dict(read.facts)
    facts["make_database"] = {
        "probed": True,
        "target_count": len(database_targets),
        "truncated": len(completed.stdout) > MAKE_DB_MAX_BYTES,
    }
    facts["database_targets"] = database_targets
    return AdapterRead("make", True, read.gaps, facts)


# --------------------------------------------------------------------------- #
# package.json adapter
# --------------------------------------------------------------------------- #


def read_package(root: Path) -> AdapterRead:
    path = root / PACKAGE_JSON_NAME
    if not path.is_file():
        return AdapterRead("package", present=False)
    text = _read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScorerRefusal(
            "malformed_package_json",
            f"{PACKAGE_JSON_NAME} is not valid JSON: {exc.msg} (line {exc.lineno})",
            ["fix package.json, or declare the test contract in .skillbox/test.yaml"],
        ) from exc
    if not isinstance(data, dict):
        raise ScorerRefusal(
            "malformed_package_json",
            "package.json must be a JSON object",
            ["fix package.json, or declare the test contract in .skillbox/test.yaml"],
        )

    scripts = data.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    gaps: list[str] = []
    if not isinstance(data.get("scripts", {}), dict):
        gaps.append("scripts_not_an_object")

    children: list[dict[str, Any]] = []
    for child_path in _iter_files(root, PACKAGE_JSON_NAME, limit=MAX_CHILD_PACKAGES):
        try:
            child = json.loads(_read_text(child_path))
        except (json.JSONDecodeError, ScorerRefusal):
            gaps.append("unreadable_child_package")
            continue
        child_scripts = child.get("scripts") if isinstance(child, dict) else None
        if isinstance(child_scripts, dict) and any(
            name in child_scripts for name in TEST_SCRIPT_NAMES
        ):
            children.append(
                {"path": _rel(root, child_path), "scripts": sorted(child_scripts)}
            )

    aggregate = None
    for name, body in sorted(scripts.items()):
        if not isinstance(body, str):
            continue
        if name in TEST_SCRIPT_NAMES and re.search(
            r"--workspaces|-r\b|--recursive|turbo run|lerna run|nx run-many", body
        ):
            aggregate = {"script": name, "body": body}
            break

    return AdapterRead(
        "package",
        present=True,
        gaps=tuple(sorted(set(gaps))),
        facts={
            "path": _rel(root, path),
            "text": text,
            "scripts": {k: v for k, v in scripts.items() if isinstance(v, str)},
            "workspaces": data.get("workspaces") if isinstance(data.get("workspaces"), list) else [],
            "child_packages": children,
            "aggregate": aggregate,
        },
    )


# --------------------------------------------------------------------------- #
# pytest / pyproject adapter
# --------------------------------------------------------------------------- #


def read_pytest(root: Path) -> AdapterRead:
    path = root / PYPROJECT_NAME
    gaps: list[str] = []
    facts: dict[str, Any] = {}
    present = False

    if (root / "pytest.ini").is_file() or (root / "setup.cfg").is_file():
        # Deliberately not parsed: two more config dialects is exactly the
        # "universal parsing" claim this slice refuses to make.
        gaps.append("unparsed_pytest_config_dialect")
        present = True

    if path.is_file():
        present = True
        text = _read_text(path)
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ScorerRefusal(
                "malformed_pyproject",
                f"{PYPROJECT_NAME} is not valid TOML: {exc}",
                ["fix pyproject.toml, or declare the test contract in .skillbox/test.yaml"],
            ) from exc
        options = (
            data.get("tool", {}).get("pytest", {}).get("ini_options", {})
            if isinstance(data.get("tool"), dict)
            else {}
        )
        addopts = options.get("addopts")
        facts.update(
            {
                "path": _rel(root, path),
                "text": text,
                "markers": [m for m in options.get("markers", []) if isinstance(m, str)],
                "addopts": addopts if isinstance(addopts, str) else "",
                "testpaths": options.get("testpaths", []),
            }
        )

    derived: list[str] = []
    for conftest in _iter_files(root, "conftest.py", limit=MAX_CONFTESTS):
        try:
            body = _read_text(conftest)
        except ScorerRefusal:
            gaps.append("unreadable_conftest")
            continue
        present = True
        if "pytest_collection_modifyitems" in body and "add_marker" in body:
            derived.append(_rel(root, conftest))
    if (root / "conftest.py").is_file():
        present = True
        body = _read_text(root / "conftest.py")
        if "pytest_collection_modifyitems" in body and "add_marker" in body:
            derived.append("conftest.py")
    facts["derived_marker_conftests"] = sorted(derived)

    return AdapterRead("pytest", present=present, gaps=tuple(sorted(set(gaps))), facts=facts)


# --------------------------------------------------------------------------- #
# compose adapter (static declarations only; services are never started)
# --------------------------------------------------------------------------- #


def read_compose(root: Path) -> AdapterRead:
    paths = [root / name for name in COMPOSE_NAMES if (root / name).is_file()]
    if not paths:
        return AdapterRead("compose", present=False)
    path = paths[0]
    text = _read_text(path)
    try:
        import yaml  # noqa: PLC0415
    except ModuleNotFoundError:
        return AdapterRead(
            "compose",
            present=True,
            gaps=("pyyaml_unavailable",),
            facts={"path": _rel(root, path), "text": text},
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScorerRefusal(
            "malformed_compose",
            f"{_rel(root, path)} is not valid YAML: {exc.__class__.__name__}",
            ["fix the compose file, or declare the test contract in .skillbox/test.yaml"],
        ) from exc
    if not isinstance(data, dict):
        raise ScorerRefusal(
            "malformed_compose",
            f"{_rel(root, path)} must be a YAML mapping",
            ["fix the compose file, or declare the test contract in .skillbox/test.yaml"],
        )

    services = data.get("services")
    services = services if isinstance(services, dict) else {}
    gaps: list[str] = []
    if len(paths) > 1:
        gaps.append("multiple_compose_files")

    unpinned: list[str] = []
    static_ports: list[str] = []
    for name in sorted(services):
        body = services[name]
        if not isinstance(body, dict):
            gaps.append("unreadable_service")
            continue
        image = body.get("image")
        if isinstance(image, str) and not _DIGEST_RE.search(image):
            unpinned.append(name)
        elif not isinstance(image, str):
            if "build" in body:
                gaps.append("locally_built_service")
            else:
                gaps.append("service_without_image")
        for entry in body.get("ports") or []:
            if isinstance(entry, str) and ":" in entry:
                host = entry.split(":")[0].strip('"')
                if host and host != "0" and not host.startswith("$"):
                    static_ports.append(f"{name}:{entry}")
            elif isinstance(entry, dict):
                published = entry.get("published")
                if published not in (None, 0, "0"):
                    static_ports.append(f"{name}:{published}")

    return AdapterRead(
        "compose",
        present=True,
        gaps=tuple(sorted(set(gaps))),
        facts={
            "path": _rel(root, path),
            "text": text,
            "project_name": data.get("name") if isinstance(data.get("name"), str) else None,
            "services": sorted(services),
            "unpinned_images": sorted(unpinned),
            "static_ports": sorted(static_ports),
        },
    )


# --------------------------------------------------------------------------- #
# Surface assembly
# --------------------------------------------------------------------------- #


def read_surface(root: Path) -> Surface:
    root = Path(root)
    manifest, issues = manifest_schema.load_manifest(root)
    present = (root / manifest_schema.MANIFEST_RELPATH).is_file()
    artifacts: list[str] = []
    units: list[str] = []
    if manifest is not None:
        units = sorted(manifest.units)
        for unit in manifest.units.values():
            artifacts.extend(unit.artifacts)
    return Surface(
        root=root,
        make=read_make(root),
        package=read_package(root),
        pytest=read_pytest(root),
        compose=read_compose(root),
        manifest_present=present,
        manifest_units=tuple(units),
        manifest_artifacts=tuple(sorted(set(artifacts))),
        manifest_issues=tuple(sorted({finding.code for finding in issues})),
    )


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def _verdict(
    code: str,
    *,
    hits: Sequence[R.Evidence],
    gaps: Sequence[str],
    clear_evidence: R.Evidence | None,
    status: str = "proven",
    units: Sequence[str] = (),
    fragment: dict[str, Any] | None = None,
) -> R.Finding | R.Cleared:
    """The uniform rule: evidence wins, then gaps, then a clean read.

    Ordering matters. Checking gaps first would suppress a violation the adapter
    genuinely proved elsewhere in the same file; checking clean first would turn
    an unparsed construct into a pass.
    """
    if hits:
        return R.Finding(
            code,
            status,
            evidence=tuple(hits),
            affected_units=tuple(units),
            proposed_fragment=fragment,
        )
    if gaps:
        return R.Finding(
            code,
            "unknown",
            reason=f"not determinable from a static read: {', '.join(sorted(set(gaps)))}",
        )
    if clear_evidence is None:
        return R.Finding(code, "unknown", reason="no adapter evidence for this invariant")
    return R.Cleared(code, (clear_evidence,))


def _make_targets(surface: Surface) -> dict[str, MakeTarget]:
    return surface.make.facts.get("targets", {}) if surface.make.present else {}


def _test_targets(surface: Surface) -> list[MakeTarget]:
    return [
        target
        for name, target in sorted(_make_targets(surface).items())
        if re.search(r"test|check|ci|verify|gate", name)
    ]


def _evaluate_path_fragile(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.make.present:
        return R.Finding(
            "PATH_FRAGILE", "unknown", reason="no Makefile; no typed adapter read paths here"
        )
    relpath = surface.make.facts["path"]
    hits: list[R.Evidence] = []
    gaps = list(surface.make.gaps)
    for target in _make_targets(surface).values():
        for line, text in target.recipe:
            if _ABS_PATH_RE.search(text):
                hits.append(R.Evidence("file", f"{relpath}:{line}", "absolute path in a recipe"))
            elif _ESCAPING_REL_RE.search(text):
                hits.append(R.Evidence("file", f"{relpath}:{line}", "path escapes the repo root"))
            elif _VAR_REF_RE.search(text):
                gaps.append("unexpanded_variable_in_recipe")
    fragment = None
    if hits:
        fragment = {"units": {"<unit>": {"cwd": "<repo-relative path>"}}}
    return _verdict(
        "PATH_FRAGILE",
        hits=hits[:3],
        gaps=gaps if not hits else [],
        clear_evidence=R.Evidence("parsed_target", f"{relpath}#recipes", "no absolute or escaping paths"),
        units=surface.manifest_units[:3],
        fragment=fragment,
    )


def _evaluate_target_monolithic(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.make.present:
        return R.Finding(
            "TARGET_MONOLITHIC", "unknown", reason="no Makefile to read the declared gate from"
        )
    relpath = surface.make.facts["path"]
    hits: list[R.Evidence] = []
    for target in _test_targets(surface):
        chained = sum(text.count("&&") for _, text in target.recipe)
        if chained >= 2:
            hits.append(
                R.Evidence(
                    "file",
                    f"{relpath}:{target.line}",
                    f"target {target.name!r} chains {chained + 1} phases with &&",
                )
            )
    return _verdict(
        "TARGET_MONOLITHIC",
        hits=hits[:3],
        gaps=surface.make.gaps if not hits else [],
        clear_evidence=R.Evidence(
            "parsed_target", f"{relpath}#targets", "test targets are separately invocable"
        ),
    )


def _evaluate_lock_seam(surface: Surface) -> R.Finding | R.Cleared:
    """A shared lock with no lock-free sibling is the cross-group refusal.

    This is where sweet-potato's dependency harness lands: the lock is real and
    correct *within* a group, and it is exactly why the scorer may not claim two
    independently managed groups can run concurrently.
    """
    if not surface.make.present:
        return R.Finding(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            "unknown",
            reason="no Makefile; serialization ownership is not statically visible",
        )
    relpath = surface.make.facts["path"]
    locking: list[MakeTarget] = []
    for target in _make_targets(surface).values():
        if _LOCK_RE.search(target.recipe_text):
            locking.append(target)
    shared_project = surface.compose.facts.get("project_name") if surface.compose.present else None

    if not locking and not shared_project and surface.make.gaps:
        # A makefile we could not fully read is not a makefile we can certify as
        # lock-free: the lock could live behind the include we refused to follow.
        return R.Finding(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            "unknown",
            reason=f"lanes not fully readable: {surface.make.gap_reason()}",
        )
    if not locking and not shared_project:
        return R.Cleared(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            (
                R.Evidence(
                    "absent",
                    f"{relpath}#recipes",
                    "no target takes a global lock, so an external scheduler owns ordering",
                ),
            ),
        )

    lock_free_siblings = [
        target.name
        for target in _test_targets(surface)
        if not _LOCK_RE.search(target.recipe_text)
    ]
    if locking and lock_free_siblings and not shared_project:
        return R.Cleared(
            "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING",
            (
                R.Evidence(
                    "parsed_target",
                    f"{relpath}#{lock_free_siblings[0]}",
                    "a lock-free lane exists alongside the locking one",
                ),
            ),
        )

    evidence: list[R.Evidence] = []
    if locking:
        evidence.append(
            R.Evidence(
                "file",
                f"{relpath}:{locking[0].line}",
                f"target {locking[0].name!r} takes its own lock",
            )
        )
    if shared_project:
        evidence.append(
            R.Evidence(
                "parsed_target",
                f"{surface.compose.facts['path']}#name",
                f"compose project {shared_project!r} is shared, so groups cannot be launched independently",
            )
        )
    return R.Finding(
        "EXTERNAL_SCHEDULER_LOCK_SEAM_MISSING", "proven", evidence=tuple(evidence)
    )


def _evaluate_cross_machine_partition(surface: Surface) -> R.Finding | R.Cleared:
    """In-process sharding is not a cross-machine partition -- the rename's whole point."""
    sources: list[R.Evidence] = []
    gaps: list[str] = []
    xdist_only = False

    if surface.make.present:
        relpath = surface.make.facts["path"]
        gaps.extend(surface.make.gaps)
        for target in _test_targets(surface):
            body = target.recipe_text
            if _SHARD_VAR_RE.search(body):
                sources.append(
                    R.Evidence(
                        "parsed_target",
                        f"{relpath}#{target.name}",
                        "target accepts a caller-supplied partition selector",
                    )
                )
            elif _XDIST_RE.search(body):
                xdist_only = True
    if _XDIST_RE.search(surface.pytest.facts.get("addopts", "")):
        xdist_only = True
    if len(surface.manifest_units) > 1:
        sources.append(
            R.Evidence(
                "parsed_target",
                f"{manifest_schema.MANIFEST_RELPATH}#units",
                f"{len(surface.manifest_units)} declared units are independently addressable",
            )
        )
    if surface.package.present and surface.package.facts.get("aggregate"):
        # Only *reachable* package lanes partition anything. Unreachable ones are
        # the PACKAGE_LANES_UNENUMERATED finding; using them as partition
        # evidence would clear one code with the proof of another.
        sources.append(
            R.Evidence(
                "parsed_target",
                f"{surface.package.facts['path']}#scripts",
                "per-package lanes are addressable from the root aggregate",
            )
        )

    if sources:
        return R.Cleared("CROSS_MACHINE_PARTITION_MISSING", (sources[0],))
    if not surface.make.present and not surface.pytest.present and not surface.package.present:
        return R.Finding(
            "CROSS_MACHINE_PARTITION_MISSING",
            "unknown",
            reason="no typed surface exposes a selection vocabulary to read",
        )
    detail = (
        "in-process sharding only; it partitions within one machine, not across machines"
        if xdist_only
        else "no CLI-addressable partition vocabulary"
    )
    evidence = R.Evidence(
        "absent",
        f"{surface.make.facts['path']}#targets" if surface.make.present else "repo#selection",
        detail,
    )
    if gaps:
        return R.Finding(
            "CROSS_MACHINE_PARTITION_MISSING",
            "likely",
            evidence=(evidence,),
            reason=f"partial read: {', '.join(sorted(set(gaps)))}",
        )
    return R.Finding("CROSS_MACHINE_PARTITION_MISSING", "proven", evidence=(evidence,))


def _evaluate_service_images(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.compose.present:
        return R.Finding(
            "SERVICE_IMAGES_UNPINNED",
            "not_applicable",
            reason="no compose file declares external service images",
        )
    facts = surface.compose.facts
    if surface.compose.gaps:
        return R.Finding(
            "SERVICE_IMAGES_UNPINNED",
            "unknown",
            reason=f"compose not fully readable: {surface.compose.gap_reason()}",
        )
    if facts["unpinned_images"]:
        return R.Finding(
            "SERVICE_IMAGES_UNPINNED",
            "proven",
            evidence=tuple(
                R.Evidence(
                    "parsed_target",
                    f"{facts['path']}#services.{name}.image",
                    "image is not pinned by digest",
                )
                for name in facts["unpinned_images"][:3]
            ),
        )
    return R.Cleared(
        "SERVICE_IMAGES_UNPINNED",
        (R.Evidence("parsed_target", f"{facts['path']}#services", "every image is digest-pinned"),),
    )


def _evaluate_service_endpoints(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.compose.present:
        return R.Finding(
            "SERVICE_ENDPOINT_STATIC",
            "not_applicable",
            reason="no compose file declares service endpoints",
        )
    facts = surface.compose.facts
    if surface.compose.gaps:
        return R.Finding(
            "SERVICE_ENDPOINT_STATIC",
            "unknown",
            reason=f"compose not fully readable: {surface.compose.gap_reason()}",
        )
    if facts["static_ports"]:
        return R.Finding(
            "SERVICE_ENDPOINT_STATIC",
            "proven",
            evidence=tuple(
                R.Evidence(
                    "parsed_target",
                    f"{facts['path']}#services.{entry.split(':')[0]}.ports",
                    "fixed host port; two concurrent runs collide",
                )
                for entry in facts["static_ports"][:3]
            ),
        )
    return R.Cleared(
        "SERVICE_ENDPOINT_STATIC",
        (
            R.Evidence(
                "parsed_target", f"{facts['path']}#services", "no fixed host ports are published"
            ),
        ),
    )


def _evaluate_service_free_lane(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.compose.present:
        return R.Finding(
            "SERVICE_FREE_LANE_MISSING",
            "not_applicable",
            reason="no external services are declared, so no lane needs to be free of them",
        )
    manifest_free = [
        unit
        for unit in surface.manifest_units
        if unit and not _service_bound_unit(surface, unit)
    ]
    if manifest_free:
        return R.Cleared(
            "SERVICE_FREE_LANE_MISSING",
            (
                R.Evidence(
                    "parsed_target",
                    f"{manifest_schema.MANIFEST_RELPATH}#units.{manifest_free[0]}",
                    "declared unit needs no service",
                ),
            ),
        )
    if surface.make.present:
        for target in _test_targets(surface):
            body = target.recipe_text
            if not re.search(r"compose|docker|postgres|redis|mysql", body, re.IGNORECASE):
                return R.Cleared(
                    "SERVICE_FREE_LANE_MISSING",
                    (
                        R.Evidence(
                            "parsed_target",
                            f"{surface.make.facts['path']}#{target.name}",
                            "test lane starts no service",
                        ),
                    ),
                )
        if surface.make.gaps:
            return R.Finding(
                "SERVICE_FREE_LANE_MISSING",
                "unknown",
                reason=f"lanes not fully readable: {surface.make.gap_reason()}",
            )
        return R.Finding(
            "SERVICE_FREE_LANE_MISSING",
            "proven",
            evidence=(
                R.Evidence(
                    "absent",
                    f"{surface.make.facts['path']}#targets",
                    "every test lane brings up a service",
                ),
            ),
        )
    return R.Finding(
        "SERVICE_FREE_LANE_MISSING",
        "unknown",
        reason="services are declared but no typed adapter can enumerate the lanes",
    )


def _service_bound_unit(surface: Surface, unit_id: str) -> bool:
    manifest, _ = manifest_schema.load_manifest(surface.root)
    if manifest is None:
        return True
    unit = manifest.units.get(unit_id)
    return bool(unit and unit.services)


def _evaluate_service_requirement(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.compose.present:
        return R.Finding(
            "SERVICE_REQUIREMENT_UNDERIVED",
            "not_applicable",
            reason="no services are declared, so nothing declares a service requirement",
        )
    if not surface.pytest.present:
        return R.Finding(
            "SERVICE_REQUIREMENT_UNDERIVED",
            "unknown",
            reason="services exist but no pytest configuration models the requirement",
        )
    facts = surface.pytest.facts
    derived = facts.get("derived_marker_conftests") or []
    if derived:
        return R.Cleared(
            "SERVICE_REQUIREMENT_UNDERIVED",
            (
                R.Evidence(
                    "parsed_target",
                    f"{derived[0]}#pytest_collection_modifyitems",
                    "markers are derived from what tests request",
                ),
            ),
        )
    markers = facts.get("markers") or []
    service_markers = [m for m in markers if re.match(r"^(db|database|postgres|redis|service)", m)]
    if service_markers:
        return R.Finding(
            "SERVICE_REQUIREMENT_UNDERIVED",
            "likely",
            evidence=(
                R.Evidence(
                    "parsed_target",
                    f"{facts.get('path', PYPROJECT_NAME)}#tool.pytest.ini_options.markers",
                    f"service marker {service_markers[0].split(':')[0]!r} declared with no deriving conftest",
                ),
            ),
            reason="hand-maintenance is inferred from the absence of a deriving hook, not proven",
        )
    if surface.pytest.gaps:
        return R.Finding(
            "SERVICE_REQUIREMENT_UNDERIVED",
            "unknown",
            reason=f"pytest config not fully readable: {surface.pytest.gap_reason()}",
        )
    return R.Finding(
        "SERVICE_REQUIREMENT_UNDERIVED",
        "unknown",
        reason="services exist but no marker model was found to classify them",
    )


def _evaluate_receipt_composable(surface: Surface) -> R.Finding | R.Cleared:
    if surface.manifest_artifacts:
        return R.Cleared(
            "RECEIPT_NOT_COMPOSABLE",
            (
                R.Evidence(
                    "parsed_target",
                    f"{manifest_schema.MANIFEST_RELPATH}#units.artifacts",
                    "per-unit artifacts are declared, so proof composes",
                ),
            ),
        )
    if surface.make.present:
        relpath = surface.make.facts["path"]
        for target in _test_targets(surface):
            if _JUNIT_RE.search(target.recipe_text):
                return R.Cleared(
                    "RECEIPT_NOT_COMPOSABLE",
                    (
                        R.Evidence(
                            "parsed_target",
                            f"{relpath}#{target.name}",
                            "lane emits a machine-readable per-lane report",
                        ),
                    ),
                )
        if _JUNIT_RE.search(surface.pytest.facts.get("addopts", "")):
            return R.Cleared(
                "RECEIPT_NOT_COMPOSABLE",
                (
                    R.Evidence(
                        "parsed_target",
                        f"{surface.pytest.facts['path']}#addopts",
                        "pytest emits a machine-readable report",
                    ),
                ),
            )
        if surface.make.gaps:
            return R.Finding(
                "RECEIPT_NOT_COMPOSABLE",
                "unknown",
                reason=f"lanes not fully readable: {surface.make.gap_reason()}",
            )
        return R.Finding(
            "RECEIPT_NOT_COMPOSABLE",
            "likely",
            evidence=(
                R.Evidence(
                    "absent",
                    f"{relpath}#targets",
                    "no lane emits a per-unit report; proof looks whole-tree only",
                ),
            ),
            reason="absence of a report flag is strong but not proof that no receipt exists",
        )
    return R.Finding(
        "RECEIPT_NOT_COMPOSABLE",
        "unknown",
        reason="no typed adapter can see how this suite emits proof",
    )


def _evaluate_package_lanes(surface: Surface) -> R.Finding | R.Cleared:
    if not surface.package.present:
        return R.Finding(
            "PACKAGE_LANES_UNENUMERATED",
            "not_applicable",
            reason="no package manifest declares test-bearing packages",
        )
    facts = surface.package.facts
    children = facts.get("child_packages") or []
    if not children:
        return R.Cleared(
            "PACKAGE_LANES_UNENUMERATED",
            (
                R.Evidence(
                    "parsed_target",
                    f"{facts['path']}#scripts",
                    "no child package declares its own test script",
                ),
            ),
        )
    if facts.get("aggregate"):
        return R.Cleared(
            "PACKAGE_LANES_UNENUMERATED",
            (
                R.Evidence(
                    "file",
                    f"{facts['path']}:{_line_of(facts['text'], facts['aggregate']['script'])}",
                    "a root aggregate script reaches every package lane",
                ),
            ),
        )
    if surface.package.gaps:
        return R.Finding(
            "PACKAGE_LANES_UNENUMERATED",
            "unknown",
            reason=f"packages not fully readable: {surface.package.gap_reason()}",
        )
    return R.Finding(
        "PACKAGE_LANES_UNENUMERATED",
        "proven",
        evidence=tuple(
            R.Evidence(
                "parsed_target",
                f"{child['path']}#scripts",
                "test-bearing package reachable from no root aggregate",
            )
            for child in children[:3]
        ),
        proposed_fragment={
            "units": {
                Path(child["path"]).parent.name or "root": {"command": ["npm", "test"]}
                for child in children[:3]
            }
        },
    )


_EVALUATORS = (
    _evaluate_path_fragile,
    _evaluate_target_monolithic,
    _evaluate_lock_seam,
    _evaluate_cross_machine_partition,
    _evaluate_service_images,
    _evaluate_service_endpoints,
    _evaluate_service_free_lane,
    _evaluate_service_requirement,
    _evaluate_receipt_composable,
    _evaluate_package_lanes,
)


def evaluate(surface: Surface) -> tuple[list[R.Finding], list[R.Cleared]]:
    findings: list[R.Finding] = []
    cleared: list[R.Cleared] = []
    for evaluator in _EVALUATORS:
        result = evaluator(surface)
        (cleared if isinstance(result, R.Cleared) else findings).append(result)
    return findings, cleared


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def provenance(surface: Surface) -> dict[str, Any]:
    """How the report was produced. An agent should never have to guess."""
    return {
        "scorer_schema_version": SCORER_SCHEMA_VERSION,
        "executed_anything": False,
        "manifest_present": surface.manifest_present,
        "adapters": {
            adapter.name: {
                "present": adapter.present,
                "gaps": list(adapter.gaps),
            }
            for adapter in surface.adapters
        },
        "make_database": surface.make.facts.get(
            "make_database", {"probed": False, "refused": "not_probed_by_default"}
        ),
    }


def probed_provenance(
    block: dict[str, Any], receipt: dict[str, Any], digest: str
) -> dict[str, Any]:
    """Restamp provenance once probes have run (skillbox-sbp-test-probe-mode-sz4d).

    ``executed_anything`` is the one field in this module that an opt-in probe
    can falsify, and a report carrying probe-upgraded findings under
    ``executed_anything: False`` would be the exact lie the flag exists to avoid.
    The static path never calls this, so the default stays honest too.
    """
    updated = dict(block)
    updated["executed_anything"] = True
    updated["probe"] = {
        "schema": receipt.get("schema"),
        "receipt_digest": digest,
        "counts": dict(receipt.get("counts") or {}),
        "budget_exhausted": bool(receipt.get("budget_exhausted")),
    }
    return updated


def score_report(cwd: Path, *, label: str | None = None) -> dict[str, Any]:
    """Analyse ``cwd`` and return the ``suite-readiness/v1`` report plus provenance.

    Raises :class:`ScorerRefusal` for bad input; the caller turns that into a
    typed envelope. Nothing here writes, executes, or reaches the network.
    """
    root = Path(cwd)
    if not root.is_dir():
        raise ScorerRefusal(
            "cwd_not_found",
            f"{root} is not a directory",
            ["pass --cwd pointing at a repository root"],
        )
    surface = read_surface(root)
    if not surface.has_test_surface:
        raise ScorerRefusal(
            "no_test_surface",
            "no Makefile, package.json, pytest config, compose file or .skillbox/test.yaml here",
            [
                f"declare the test contract manually in {manifest_schema.MANIFEST_RELPATH}",
                "sbp test lint --format json",
            ],
        )
    findings, cleared = evaluate(surface)
    report = R.build_report(
        R.Subject(label=label or root.name),
        findings,
        cleared,
    )
    report["provenance"] = provenance(surface)
    report["next_actions"] = _next_actions(report, surface)
    return report


def _next_actions(report: dict[str, Any], surface: Surface) -> list[str]:
    """Keep the registry's actions, then add the ones only the scorer knows.

    Capped, because a hundred next actions is the same as none.
    """
    actions = list(report.get("next_actions") or [])
    if not surface.manifest_present:
        actions.insert(
            0,
            f"declare {manifest_schema.MANIFEST_RELPATH} manually; this analysis is not a manifest",
        )
    elif surface.manifest_issues:
        actions.insert(0, "sbp test lint --format json")
    if any(adapter.gaps for adapter in surface.adapters):
        actions.append(
            f"resolve the unknowns by declaring them in {manifest_schema.MANIFEST_RELPATH}"
        )
    if not actions:
        # "Nothing to fix" is still an answer, and an agent that asked what to do
        # next should never be handed an empty list to interpret.
        actions.append("no v1 blockers or unknowns; sbp test plan --format json")
    return actions[:8]


def report_text_lines(report: dict[str, Any]) -> list[str]:
    """Human rendering. Empty state is stated, never blank."""
    rollup = report.get("rollup") or {}
    counts = report.get("counts") or {}
    coverage = report.get("coverage") or {}
    gates = report.get("gates") or {}
    blockers = sorted(
        {code for gate in gates.values() for code in gate.get("blocked_by") or []}
    )
    lines = [
        f"readiness: {report.get(R.V1_READINESS_KEY)} "
        f"(rollup {rollup.get('score')}/{rollup.get('max')}, advisory)",
        "blockers: " + (", ".join(blockers) if blockers else "none (0 proven)"),
        "coverage: "
        f"{len(coverage.get('v1_covered') or [])}/{coverage.get('axes_total')} axes evaluated in v1; "
        f"not covered: {', '.join(coverage.get('not_covered_in_v1') or []) or 'none'}",
        "evidence: "
        + ", ".join(f"{status}={counts.get(status, 0)}" for status in (*R.STATUSES, "cleared")),
    ]
    for intent in R.INTENTS:
        gate = gates.get(intent) or {}
        state = "admitted" if gate.get("admitted") else "blocked"
        blocked_by = ", ".join(gate.get("blocked_by") or [])
        unproven = ", ".join(gate.get("unproven_for_intent") or [])
        if blocked_by:
            suffix = f" blocked_by=[{blocked_by}]"
        elif unproven:
            suffix = f" unproven=[{unproven}]"
        else:
            suffix = ""
        lines.append(f"  {intent}: {state}{suffix}")
    for finding in report.get("findings") or []:
        if finding.get("status") in ("proven", "likely"):
            locator = (finding.get("evidence") or [{}])[0].get("locator", "")
            lines.append(
                f"  {finding['status']} {finding['finding_code']} "
                f"({finding['blocks']}) {locator}"
            )
    return lines


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Before/after over the same bytes only (delegates the rule to the registry)."""
    return {
        "comparable": R.is_comparable(before, after),
        "score_delta": (after.get("rollup") or {}).get("score", 0)
        - (before.get("rollup") or {}).get("score", 0),
    }
