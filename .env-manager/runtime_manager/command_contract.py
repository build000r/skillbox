"""Cross-surface command inventory and safety-parity linter.

Skillbox exposes the same operations through four surfaces that drifted apart
because each was maintained by hand: the runtime CLI (``manage.py``), the box
CLI (``scripts/box.py``), the agent-facing ``command_registry`` specs, the
operator MCP tools, and the Make wrappers over the two CLIs. A safety option
could be added to one and silently missing from another — which is exactly how
``make box-down BOX=id`` ended up unable to forward a confirmation to a CLI that
had started requiring one.

This module reads those surfaces and reports where they disagree. It is
deliberately **pure**: it introspects parsers and data structures, and it never
executes a command, spawns a process, or parses arbitrary shell. Where a fact
can only be learned by running something (what argv an MCP handler builds), this
module takes the argv as an *input* and classifies it — the caller does the
observing, under its own stubs. See ``tests/test_command_contract.py``.

It also does not pretend argparse has one universal schema. What it extracts is
narrow and decidable: the set of leaf command paths, and which of a fixed
vocabulary of safety options each one accepts.

Front door::

    from runtime_manager import command_contract as cc
    report = cc.build_report()
    print(cc.render_report(report))   # byte-stable
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPORT_SCHEMA = "skillbox.command-contract.v1"
BASELINE_SCHEMA = "skillbox.command-contract-gaps.v1"

#: The only options this linter reasons about. A closed vocabulary keeps the
#: extraction decidable — anything outside it is simply not a safety option as
#: far as this contract is concerned, rather than a guess.
#:
#: The list must stay honest about bespoke spellings. ``state-backup restore``
#: guards itself with ``--i-understand-data-loss`` and ``dcg relinquish`` with
#: ``--approved-by``; omitting them would make this linter report two correctly
#: guarded commands as unguarded, which is a worse failure than missing one.
SAFETY_OPTIONS = (
    "--apply",
    "--approved-by",
    "--confirm",
    "--dry-run",
    "--force",
    "--i-understand-data-loss",
    "--yes",
)

#: Options that constitute an affirmative confirmation of a destructive act.
#: ``--dry-run`` is deliberately absent: previewing is the opposite of
#: confirming, and ``--force`` overrides a *check*, which is not the same as
#: affirming the act itself.
CONFIRMATION_OPTIONS = (
    "--approved-by",
    "--confirm",
    "--i-understand-data-loss",
    "--yes",
)

#: The strongest form: a confirmation that names its subject, so a confirmation
#: minted for one target can never authorize acting on another. Only
#: ``--confirm <id>`` carries identity; see :func:`argv_confirms_identity`.
IDENTITY_BOUND_OPTIONS = ("--confirm",)

#: Registry values that mean "this can destroy something".
DESTRUCTIVE_VALUES = ("destructive",)

SURFACE_RUNTIME = "runtime"
SURFACE_BOX = "box"
SURFACE_MCP = "mcp"
SURFACE_MAKE = "make"
SURFACES = (SURFACE_RUNTIME, SURFACE_BOX, SURFACE_MCP, SURFACE_MAKE)

# Gap kinds. Stable strings: the checked-in baseline keys on them.
GAP_LIVE_COMMAND_UNREGISTERED = "live_command_unregistered"
GAP_REGISTRY_WITHOUT_LIVE_COMMAND = "registry_without_live_command"
GAP_REGISTRY_JOIN_AMBIGUOUS = "registry_join_ambiguous"
GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION = "destructive_without_confirmation"
GAP_MCP_DESTRUCTIVE_WITHOUT_CONTRACT = "mcp_destructive_without_contract"
GAP_MAKE_CANNOT_FORWARD_CONFIRMATION = "make_cannot_forward_confirmation"
GAP_REGISTRY_MCP_SURFACE_WITHOUT_TOOL = "registry_mcp_surface_without_tool"
GAP_REGISTRY_SURFACE_NOT_MODELLED = "registry_surface_not_modelled"
GAP_REGISTRY_TARGETS_COMMAND_GROUP = "registry_targets_command_group"

GAP_KINDS = (
    GAP_LIVE_COMMAND_UNREGISTERED,
    GAP_REGISTRY_WITHOUT_LIVE_COMMAND,
    GAP_REGISTRY_JOIN_AMBIGUOUS,
    GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION,
    GAP_MCP_DESTRUCTIVE_WITHOUT_CONTRACT,
    GAP_MAKE_CANNOT_FORWARD_CONFIRMATION,
    GAP_REGISTRY_MCP_SURFACE_WITHOUT_TOOL,
    GAP_REGISTRY_SURFACE_NOT_MODELLED,
    GAP_REGISTRY_TARGETS_COMMAND_GROUP,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceCommand:
    """One command as some surface actually presents it."""

    id: str
    surface: str
    name: str
    provenance: str
    observed_safety: tuple[str, ...] = ()
    declared: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "surface": self.surface,
            "name": self.name,
            "provenance": self.provenance,
            "observed_safety": list(self.observed_safety),
        }
        if self.declared:
            payload["declared"] = dict(sorted(self.declared.items()))
        return payload


@dataclass(frozen=True)
class Gap:
    """One compact mismatch record.

    ``detail`` is a short, stable string. It is part of the identity of the gap
    for baseline purposes only through ``id`` and ``kind`` — detail may be
    reworded without invalidating a baseline entry, so a message improvement
    does not read as new drift.
    """

    id: str
    kind: str
    detail: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.id, self.kind)

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "detail": self.detail}


# ---------------------------------------------------------------------------
# Extraction — argparse
# ---------------------------------------------------------------------------


def _walk_parser(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Every leaf command path under ``parser``.

    A "leaf" is a parser with no subparsers of its own. Intermediate groups
    (``skill``, ``fleet``) are not commands you can run, so they are not
    reported as commands.
    """
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        return [(prefix, parser)]
    leaves: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []
    for action in subactions:
        for name, subparser in action.choices.items():
            leaves.extend(_walk_parser(subparser, prefix + (name,)))
    return leaves


def parser_group_names(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> set[str]:
    """Names of intermediate command groups (``skill``, ``snap``, ``fleet``).

    A group is not runnable, so it is not a command — but a registry spec that
    points at one is a different problem from a spec that points at nothing, and
    the operator fixing it needs to know which.
    """
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        return set()
    groups: set[str] = set()
    if prefix:
        groups.add(" ".join(prefix))
    for action in subactions:
        for name, subparser in action.choices.items():
            groups |= parser_group_names(subparser, prefix + (name,))
    return groups


def observed_safety_options(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """Which of ``SAFETY_OPTIONS`` this parser accepts, sorted."""
    present = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option in SAFETY_OPTIONS
    }
    return tuple(sorted(present))


def extract_parser_commands(
    parser: argparse.ArgumentParser,
    *,
    surface: str,
    provenance: str,
) -> tuple[SurfaceCommand, ...]:
    commands = [
        SurfaceCommand(
            id=f"{surface}:{' '.join(path)}",
            surface=surface,
            name=" ".join(path),
            provenance=provenance,
            observed_safety=observed_safety_options(subparser),
        )
        for path, subparser in _walk_parser(parser)
        if path
    ]
    return tuple(sorted(commands, key=lambda command: command.id))


# ---------------------------------------------------------------------------
# Extraction — MCP tools
# ---------------------------------------------------------------------------


def extract_mcp_commands(
    tools: Iterable[Mapping[str, Any]],
    *,
    provenance: str = "scripts/operator_mcp_server.py:TOOLS",
) -> tuple[SurfaceCommand, ...]:
    commands = []
    for tool in tools:
        name = str(tool.get("name") or "")
        if not name:
            continue
        annotations = tool.get("annotations") or {}
        contract = tool.get("x_skillbox_contract") or {}
        commands.append(
            SurfaceCommand(
                id=f"{SURFACE_MCP}:{name}",
                surface=SURFACE_MCP,
                name=name,
                provenance=provenance,
                declared={
                    "destructive": bool(annotations.get("destructiveHint")),
                    "read_only": bool(annotations.get("readOnlyHint")),
                    "dry_run_required": bool(contract.get("dry_run_required")),
                    "requires_user_confirmation": bool(contract.get("requires_user_confirmation")),
                },
            )
        )
    return tuple(sorted(commands, key=lambda command: command.id))


# ---------------------------------------------------------------------------
# Extraction — Make wrappers
# ---------------------------------------------------------------------------

#: A recipe line this linter is willing to interpret: one python3 invocation of
#: a known CLI entrypoint, with a verb. Anything else is left alone rather than
#: guessed at — a broad Make parser is an explicit non-goal.
_MAKE_WRAPPER_RE = re.compile(
    r"^\s*@?python3\s+(?P<script>scripts/box\.py|\.env-manager/manage\.py)\s+(?P<rest>.+)$"
)
_MAKE_TARGET_RE = re.compile(r"^(?P<target>[A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?!=)")
_MAKE_ASSIGN_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+)\s*:?\+?=\s*(?P<value>.*)$")
_MAKE_VAR_REF_RE = re.compile(r"\$\((?P<name>[A-Za-z0-9_]+)\)")
_FLAG_LITERAL_RE = re.compile(r"--[a-z][a-z0-9-]*")

_SCRIPT_SURFACE = {
    "scripts/box.py": SURFACE_BOX,
    ".env-manager/manage.py": SURFACE_RUNTIME,
}


def _make_assignments(lines: Sequence[str]) -> dict[str, str]:
    """Simple ``NAME := value`` assignments, verbatim and unevaluated.

    The values are never evaluated. ``BOX_DOWN_ARGS`` is a ``$(if ...)``
    expression whose result depends on the operator's environment, and guessing
    that result would be fiction. What *is* decidable, and what this linter
    needs, is which safety flags the expression is capable of emitting at all.
    """
    assignments: dict[str, str] = {}
    for line in lines:
        if line.startswith("\t"):
            continue
        match = _MAKE_ASSIGN_RE.match(line)
        if match:
            assignments[match.group("name")] = match.group("value")
    return assignments


def _expand_flag_literals(text: str, assignments: Mapping[str, str], depth: int = 4) -> str:
    """Inline ``$(VAR)`` references so flag literals inside them become visible.

    Bounded recursion, and unknown variables are dropped rather than guessed.
    """
    for _ in range(depth):
        replaced = _MAKE_VAR_REF_RE.sub(
            lambda match: assignments.get(match.group("name"), ""), text
        )
        if replaced == text:
            break
        text = replaced
    return text


def extract_make_wrappers(
    makefile_text: str,
    *,
    provenance: str = "Makefile",
) -> tuple[SurfaceCommand, ...]:
    """Make targets that wrap a CLI verb, and the safety flags they can forward.

    "Can forward" is the honest question. A wrapper whose recipe never mentions
    ``--confirm`` cannot pass one no matter what the operator types, which is
    precisely the defect this surface is here to catch.
    """
    lines = makefile_text.splitlines()
    assignments = _make_assignments(lines)
    commands: list[SurfaceCommand] = []
    current_target: str | None = None

    for line in lines:
        if not line.startswith("\t"):
            match = _MAKE_TARGET_RE.match(line)
            current_target = match.group("target") if match else None
            continue
        if current_target is None:
            continue
        wrapper = _MAKE_WRAPPER_RE.match(line)
        if not wrapper:
            continue
        script = wrapper.group("script")
        rest = _expand_flag_literals(wrapper.group("rest"), assignments)
        tokens = rest.split()
        verb = next((token for token in tokens if not token.startswith(("-", "$"))), None)
        if verb is None:
            continue
        # Flags are found by pattern, not by whitespace token: expanding a
        # `$(if cond,--confirm ...)` leaves the literal glued to a comma, and
        # `",--confirm" in SAFETY_OPTIONS` is False.
        forwarded = tuple(
            sorted({flag for flag in _FLAG_LITERAL_RE.findall(rest) if flag in SAFETY_OPTIONS})
        )
        commands.append(
            SurfaceCommand(
                id=f"{SURFACE_MAKE}:{current_target}",
                surface=SURFACE_MAKE,
                name=current_target,
                provenance=f"{provenance}:{current_target}",
                observed_safety=forwarded,
                declared={
                    "wraps_surface": _SCRIPT_SURFACE[script],
                    "wraps_command": verb,
                },
            )
        )
        current_target = None  # one wrapper record per target

    return tuple(sorted(commands, key=lambda command: command.id))


# ---------------------------------------------------------------------------
# Registry join
# ---------------------------------------------------------------------------

_REGISTRY_PREFIX_SURFACE = {
    "box": SURFACE_BOX,
    "runtime": SURFACE_RUNTIME,
    "brain": SURFACE_RUNTIME,
}


def registry_join_candidates(spec_id: str) -> tuple[str, ...]:
    """Candidate live command names for a registry id.

    Registry ids compress a command path into one token
    (``runtime.state_backup_restore`` for ``state-backup restore``), and the
    underscores are ambiguous: some are word separators inside a segment, some
    are the space between segments. Rather than guess a single reading, emit
    every reading and let the caller require exactly one live match.
    """
    _, _, tail = spec_id.partition(".")
    if not tail:
        return ()
    parts = tail.split("_")
    candidates = {tail, tail.replace("_", "-"), tail.replace("_", " ")}
    # Every way of splitting "a_b_c" into "<prefix with dashes> <suffix>".
    for cut in range(1, len(parts)):
        head = "-".join(parts[:cut])
        rest = "-".join(parts[cut:])
        candidates.add(f"{head} {rest}")
    return tuple(sorted(candidates))


def resolve_registry_command(
    spec_id: str,
    live_names_by_surface: Mapping[str, set[str]],
) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(resolved_live_id, matched_candidates)`` for a registry spec."""
    prefix = spec_id.split(".", 1)[0]
    surface = _REGISTRY_PREFIX_SURFACE.get(prefix)
    if surface is None:
        return (None, ())
    live = live_names_by_surface.get(surface, set())
    matches = tuple(name for name in registry_join_candidates(spec_id) if name in live)
    if len(matches) == 1:
        return (f"{surface}:{matches[0]}", matches)
    return (None, matches)


# ---------------------------------------------------------------------------
# Safety forwarding
# ---------------------------------------------------------------------------


def forwarded_safety_options(argv: Sequence[str]) -> tuple[str, ...]:
    """Which safety options an argv actually carries."""
    return tuple(sorted({str(token) for token in argv if str(token) in SAFETY_OPTIONS}))


def argv_confirms_identity(argv: Sequence[str], subject: str) -> bool:
    """Whether ``argv`` confirms a destructive act on ``subject`` by name.

    ``--confirm <subject>`` counts; a bare ``--yes`` does not. The distinction
    is the whole point of an identity-bound confirmation: a blanket flag can
    confirm the destruction of something the caller never named, which is how a
    marker for one box could authorize destroying another.
    """
    tokens = [str(token) for token in argv]
    for index, token in enumerate(tokens[:-1]):
        if token == "--confirm" and tokens[index + 1] == subject:
            return True
    return f"--confirm={subject}" in tokens


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractReport:
    commands: tuple[SurfaceCommand, ...]
    gaps: tuple[Gap, ...]
    counts: Mapping[str, int]

    def gap_keys(self) -> set[tuple[str, str]]:
        return {gap.key for gap in self.gaps}

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "counts": dict(sorted(self.counts.items())),
            "commands": [command.to_payload() for command in self.commands],
            "gaps": [gap.to_payload() for gap in self.gaps],
        }


def _classify(
    commands: Sequence[SurfaceCommand],
    specs: Sequence[Any],
    group_names_by_surface: Mapping[str, set[str]] | None = None,
) -> list[Gap]:
    gaps: list[Gap] = []
    by_id = {command.id: command for command in commands}
    live_names_by_surface: dict[str, set[str]] = {}
    for command in commands:
        if command.surface in (SURFACE_RUNTIME, SURFACE_BOX):
            live_names_by_surface.setdefault(command.surface, set()).add(command.name)
    groups = dict(group_names_by_surface or {})

    resolved_live_ids: set[str] = set()
    destructive_live_ids: set[str] = set()
    for spec in sorted(specs, key=lambda spec: spec.id):
        prefix = spec.id.split(".", 1)[0]
        declared_destructive = (
            spec.side_effect in DESTRUCTIVE_VALUES or spec.risk in DESTRUCTIVE_VALUES
        )
        if prefix not in _REGISTRY_PREFIX_SURFACE:
            # `clipboard.*`, `outer.*`, `make.*` are real specs for surfaces this
            # linter does not model. Saying "no live command" would be a lie.
            gaps.append(
                Gap(
                    id=f"registry:{spec.id}",
                    kind=GAP_REGISTRY_SURFACE_NOT_MODELLED,
                    detail=f"spec prefix {prefix!r} is outside the modelled surfaces",
                )
            )
            continue

        resolved, matches = resolve_registry_command(spec.id, live_names_by_surface)
        if resolved is None:
            surface = _REGISTRY_PREFIX_SURFACE[prefix]
            group_hits = [
                name
                for name in registry_join_candidates(spec.id)
                if name in groups.get(surface, set())
            ]
            if len(matches) > 1:
                kind = GAP_REGISTRY_JOIN_AMBIGUOUS
                detail = f"registry spec matches {len(matches)} live commands"
            elif group_hits:
                kind = GAP_REGISTRY_TARGETS_COMMAND_GROUP
                detail = "spec names a command group, which is not runnable on its own"
            else:
                kind = GAP_REGISTRY_WITHOUT_LIVE_COMMAND
                detail = "registry spec has no live command"
            gaps.append(Gap(id=f"registry:{spec.id}", kind=kind, detail=detail))
        else:
            resolved_live_ids.add(resolved)
            if declared_destructive:
                destructive_live_ids.add(resolved)
            command = by_id[resolved]
            if declared_destructive and not any(
                option in command.observed_safety for option in CONFIRMATION_OPTIONS
            ):
                gaps.append(
                    Gap(
                        id=resolved,
                        kind=GAP_DESTRUCTIVE_WITHOUT_CONFIRMATION,
                        detail="registry declares destructive; CLI exposes no confirmation option",
                    )
                )

        if SURFACE_MCP in tuple(spec.surface) and not spec.mcp_tool:
            gaps.append(
                Gap(
                    id=f"registry:{spec.id}",
                    kind=GAP_REGISTRY_MCP_SURFACE_WITHOUT_TOOL,
                    detail="spec claims the mcp surface but names no tool",
                )
            )

    for command in commands:
        if command.surface in (SURFACE_RUNTIME, SURFACE_BOX) and command.id not in resolved_live_ids:
            gaps.append(
                Gap(
                    id=command.id,
                    kind=GAP_LIVE_COMMAND_UNREGISTERED,
                    detail="live command has no command_registry spec",
                )
            )
        elif command.surface == SURFACE_MCP:
            declared = command.declared
            if declared.get("destructive") and not (
                declared.get("dry_run_required") and declared.get("requires_user_confirmation")
            ):
                gaps.append(
                    Gap(
                        id=command.id,
                        kind=GAP_MCP_DESTRUCTIVE_WITHOUT_CONTRACT,
                        detail="destructive MCP tool without dry-run + confirmation contract",
                    )
                )
        elif command.surface == SURFACE_MAKE:
            wrapped = f"{command.declared.get('wraps_surface')}:{command.declared.get('wraps_command')}"
            target = by_id.get(wrapped)
            if target is None:
                continue
            # The trigger is what the registry DECLARES about the wrapped
            # command, not whether that command happens to own a confirmation
            # flag. `manage.py doctor` carries --yes for its optional --fix;
            # `make dev-sanity` wrapping it destroys nothing.
            needs_confirmation = wrapped in destructive_live_ids
            can_forward = any(
                option in command.observed_safety for option in CONFIRMATION_OPTIONS
            )
            if needs_confirmation and not can_forward:
                gaps.append(
                    Gap(
                        id=command.id,
                        kind=GAP_MAKE_CANNOT_FORWARD_CONFIRMATION,
                        detail=f"wraps {wrapped}, which requires confirmation the recipe cannot pass",
                    )
                )

    gaps.sort(key=lambda gap: (gap.kind, gap.id))
    return gaps


def build_report(
    *,
    runtime_parser: argparse.ArgumentParser | None = None,
    box_parser: argparse.ArgumentParser | None = None,
    mcp_tools: Iterable[Mapping[str, Any]] | None = None,
    makefile_text: str | None = None,
    specs: Sequence[Any] | None = None,
) -> ContractReport:
    """Build the inventory. Every input is injectable so tests can drive drift.

    Defaults load the live surfaces. Loading is import-and-introspect only: no
    command runs, and nothing is written.
    """
    if runtime_parser is None:
        from . import cli as runtime_cli

        runtime_parser = runtime_cli._build_parser()
    if box_parser is None:
        box_parser = _load_box_parser()
    if mcp_tools is None:
        mcp_tools = _load_mcp_tools()
    if makefile_text is None:
        makefile_text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    if specs is None:
        from . import command_registry

        specs = command_registry.default_registry()

    commands = (
        extract_parser_commands(
            runtime_parser,
            surface=SURFACE_RUNTIME,
            provenance=".env-manager/runtime_manager/cli.py:_build_parser",
        )
        + extract_parser_commands(
            box_parser,
            surface=SURFACE_BOX,
            provenance="scripts/box.py:build_parser",
        )
        + extract_mcp_commands(mcp_tools)
        + extract_make_wrappers(makefile_text)
    )
    commands = tuple(sorted(commands, key=lambda command: command.id))
    group_names = {
        SURFACE_RUNTIME: parser_group_names(runtime_parser),
        SURFACE_BOX: parser_group_names(box_parser),
    }
    gaps = tuple(_classify(commands, list(specs), group_names))

    counts = {
        "commands": len(commands),
        "gaps": len(gaps),
        "registry_specs": len(list(specs)),
        **{
            f"commands_{surface}": sum(1 for c in commands if c.surface == surface)
            for surface in SURFACES
        },
        **{
            f"gaps_{kind}": sum(1 for gap in gaps if gap.kind == kind)
            for kind in GAP_KINDS
        },
    }
    return ContractReport(commands=commands, gaps=gaps, counts=counts)


def _load_box_parser() -> argparse.ArgumentParser:
    module = _load_module_by_path(
        "runtime_manager_command_contract_box", REPO_ROOT / "scripts" / "box.py"
    )
    return module.build_parser()


def _load_module_by_path(name: str, path: Path) -> Any:
    """Import a script by path for introspection only.

    Importing runs module-level code, which for both scripts is imports and
    constant definitions — no command dispatch, which happens under
    ``if __name__ == "__main__"``. ``tests.test_command_contract.PurityTests``
    holds that line by building a report with the subprocess and write seams
    booby-trapped.
    """
    import importlib.util
    import sys

    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE executing: both scripts define dataclasses, and
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _load_mcp_tools() -> tuple[Mapping[str, Any], ...]:
    module = _load_module_by_path(
        "runtime_manager_command_contract_mcp",
        REPO_ROOT / "scripts" / "operator_mcp_server.py",
    )
    return tuple(module.TOOLS)


def render_report(report: ContractReport) -> str:
    """Canonical JSON text. Two renders of one report are byte-identical."""
    return json.dumps(report.to_payload(), indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def load_baseline(path: Path) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Read the checked-in known-gap baseline, keyed by ``(id, kind)``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != BASELINE_SCHEMA:
        raise ValueError(f"unexpected baseline schema: {payload.get('schema')!r}")
    entries: dict[tuple[str, str], Mapping[str, Any]] = {}
    for entry in payload.get("gaps") or []:
        entries[(str(entry["id"]), str(entry["kind"]))] = entry
    return entries


def diff_against_baseline(
    report: ContractReport,
    baseline: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[list[Gap], list[tuple[str, str]]]:
    """Return ``(new_gaps, resolved_baseline_keys)``.

    The ratchet is shrink-only: a new gap is a failure, a resolved one is not.
    Resolved entries are reported so the baseline can be trimmed, but they never
    fail the build — otherwise fixing a gap would break the tree until someone
    remembered to also edit a JSON file.
    """
    new_gaps = [gap for gap in report.gaps if gap.key not in baseline]
    live_keys = report.gap_keys()
    resolved = sorted(key for key in baseline if key not in live_keys)
    return (new_gaps, resolved)


# ---------------------------------------------------------------------------
# Destructive dispatch policy
# ---------------------------------------------------------------------------
#
# Everything above this line *describes* the surfaces. This section makes the
# description load-bearing at the dispatch boundary: a command that declares
# itself destructive must actually be reachable only through a preview and a
# confirmation, and a wrapper that fronts it must actually forward that
# confirmation to the CLI that enforces it.
#
# The failure this exists to prevent is safety metadata that is ceremonial --
# `risk: destructive` in a registry while the CLI happily runs unconfirmed, or a
# wrapper whose argv quietly drops the flag on the way to the gate. Both are
# invisible to a reader of either side alone, which is why they are checked as a
# cross-surface invariant rather than a per-file convention.
#
# Scope discipline, taken from the bead: this section REPORTS. It repairs no
# behavior, merges no parsers, and infers nothing about an alias it cannot
# resolve -- an unresolvable mapping is recorded as its own finding so the
# ambiguity is explicit instead of being guessed into a pass or a fail.

#: Risk levels that a destructive surface may legitimately declare. `medium`
#: and `low` are downgrades: a surface whose side effect is destruction cannot
#: also claim it is a small thing.
DESTRUCTIVE_RISKS = ("destructive", "high")

#: The side effect a destructive surface must declare. `local_write` is not
#: enough -- it is the honest label for a cache refresh, and using it for a
#: teardown is exactly the undeclared-side-effect drift this checks for.
DESTRUCTIVE_SIDE_EFFECTS = ("destructive",)

#: What counts as a non-mutating preview at the CLI boundary.
PREVIEW_OPTIONS = ("--dry-run",)

INVARIANT_RISK = "destructive_risk_declared"
INVARIANT_SIDE_EFFECT = "destructive_side_effect_declared"
INVARIANT_PREVIEW = "destructive_preview_available"
INVARIANT_CONFIRMATION = "destructive_confirmation_available"
INVARIANT_ARGV_FORWARDED = "destructive_confirmation_forwarded"
INVARIANT_MAPPING = "destructive_mapping_resolvable"

DESTRUCTIVE_INVARIANTS = (
    INVARIANT_ARGV_FORWARDED,
    INVARIANT_CONFIRMATION,
    INVARIANT_MAPPING,
    INVARIANT_PREVIEW,
    INVARIANT_RISK,
    INVARIANT_SIDE_EFFECT,
)

#: Who fixes a finding on each surface when the spec names no owner binary.
#: A diagnostic that cannot say who owns it is a diagnostic nobody acts on.
_SURFACE_OWNERS = {
    SURFACE_RUNTIME: ".env-manager/runtime_manager/cli.py",
    SURFACE_BOX: "scripts/box.py",
    SURFACE_MCP: "scripts/operator_mcp_server.py",
    SURFACE_MAKE: "Makefile",
}


@dataclass(frozen=True)
class PolicyFinding:
    """One violated destructive invariant, addressed to whoever can fix it.

    Five fields, all required, because a safety diagnostic that omits any of
    them costs the reader a bisect: *which* surface, *where* the claim came
    from, *who* owns it, *what* rule broke, and *what exactly* to do.
    """

    surface: str
    source: str
    owner: str
    invariant: str
    detail: str
    fix: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.surface, self.invariant)

    def to_payload(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "source": self.source,
            "owner": self.owner,
            "invariant": self.invariant,
            "detail": self.detail,
            "fix": self.fix,
        }

    def render(self) -> str:
        return (
            f"{self.surface}: {self.invariant}\n"
            f"  source: {self.source}\n"
            f"  owner:  {self.owner}\n"
            f"  detail: {self.detail}\n"
            f"  fix:    {self.fix}"
        )


#: Findings the tree carries today, each owned and explained. This is the
#: ratchet for THIS policy, deliberately separate from
#: ``command_contract_gaps.json`` (that baseline belongs to the inventory
#: linter and this bead does not expand it).
#:
#: An entry here is an admission, not an exemption: the invariant still fails,
#: it is simply already known. Repairing one means DELETING its row, which is
#: what makes the set shrink-only.
ACCEPTED_DESTRUCTIVE_FINDINGS = (
    {
        "surface": "runtime:state-backup restore",
        "invariant": INVARIANT_PREVIEW,
        "owner": ".env-manager/runtime_manager/cli.py",
        "reason": (
            "state-backup restore guards itself with --i-understand-data-loss but "
            "offers no preview: an operator cannot see which backup would be "
            "restored over live state before committing to it. Adding one is a "
            "behavior change, which this contract deliberately does not make."
        ),
    },
)


def accepted_finding_keys() -> frozenset[tuple[str, str]]:
    """``(surface, invariant)`` pairs already recorded as owned gaps."""
    return frozenset(
        (str(entry["surface"]), str(entry["invariant"]))
        for entry in ACCEPTED_DESTRUCTIVE_FINDINGS
    )


def spec_is_destructive(spec: Any) -> bool:
    """Whether a registry spec claims destructiveness on either axis.

    Either axis triggers the policy on purpose. A spec that says
    ``side_effect: destructive`` while calling its risk ``low`` is precisely the
    downgrade this is meant to catch, and it could only escape by being asked
    to satisfy both conditions before being checked at all.
    """
    return getattr(spec, "side_effect", None) in DESTRUCTIVE_VALUES or (
        getattr(spec, "risk", None) in DESTRUCTIVE_VALUES
    )


def _spec_owner(spec: Any, surface: str | None) -> str:
    owner = getattr(spec, "owner_binary", None)
    if owner:
        return str(owner)
    return _SURFACE_OWNERS.get(str(surface or ""), "unassigned")


def check_destructive_policy(
    *,
    specs: Sequence[Any],
    commands: Sequence[SurfaceCommand],
) -> tuple[PolicyFinding, ...]:
    """Enforce the destructive contract over normalized command records.

    Returns findings in a stable order. An empty result means every surface that
    calls itself destructive also behaves like it at the dispatch boundary.
    """
    by_id = {command.id: command for command in commands}
    live_names_by_surface: dict[str, set[str]] = {}
    for command in commands:
        if command.surface in (SURFACE_RUNTIME, SURFACE_BOX):
            live_names_by_surface.setdefault(command.surface, set()).add(command.name)

    findings: list[PolicyFinding] = []
    for spec in sorted(specs, key=lambda item: str(item.id)):
        if not spec_is_destructive(spec):
            continue
        prefix = str(spec.id).split(".", 1)[0]
        surface = _REGISTRY_PREFIX_SURFACE.get(prefix)
        source = f"command_registry spec {spec.id}"
        owner = _spec_owner(spec, surface)

        if str(getattr(spec, "risk", "")) not in DESTRUCTIVE_RISKS:
            findings.append(
                PolicyFinding(
                    surface=f"registry:{spec.id}",
                    source=source,
                    owner=owner,
                    invariant=INVARIANT_RISK,
                    detail=(
                        f"declares side_effect={spec.side_effect!r} but risk="
                        f"{spec.risk!r}, which is not one of {list(DESTRUCTIVE_RISKS)}"
                    ),
                    fix=(
                        f"set risk to 'destructive' on spec {spec.id} in "
                        ".env-manager/runtime_manager/command_registry.py"
                    ),
                )
            )
        if str(getattr(spec, "side_effect", "")) not in DESTRUCTIVE_SIDE_EFFECTS:
            findings.append(
                PolicyFinding(
                    surface=f"registry:{spec.id}",
                    source=source,
                    owner=owner,
                    invariant=INVARIANT_SIDE_EFFECT,
                    detail=(
                        f"declares risk={spec.risk!r} but side_effect="
                        f"{spec.side_effect!r}, which understates what it does"
                    ),
                    fix=(
                        f"set side_effect to 'destructive' on spec {spec.id} in "
                        ".env-manager/runtime_manager/command_registry.py"
                    ),
                )
            )

        if surface is None:
            # Unmodelled surface: say so rather than assert anything about a
            # CLI this linter never read.
            findings.append(
                PolicyFinding(
                    surface=f"registry:{spec.id}",
                    source=source,
                    owner=owner,
                    invariant=INVARIANT_MAPPING,
                    detail=f"spec prefix {prefix!r} is outside the modelled surfaces",
                    fix=(
                        "model the surface in command_contract._REGISTRY_PREFIX_SURFACE "
                        "or move the spec under a modelled prefix"
                    ),
                )
            )
            continue

        resolved, matches = resolve_registry_command(str(spec.id), live_names_by_surface)
        if resolved is None:
            findings.append(
                PolicyFinding(
                    surface=f"registry:{spec.id}",
                    source=source,
                    owner=owner,
                    invariant=INVARIANT_MAPPING,
                    detail=(
                        f"resolves to {len(matches)} live {surface} commands, so the "
                        "dispatch boundary it guards cannot be identified"
                    ),
                    fix=(
                        f"rename the spec id or the {surface} command so {spec.id} names "
                        "exactly one live command; do not guess an alias"
                    ),
                )
            )
            continue

        command = by_id[resolved]
        cli_source = f"{command.provenance} ({command.name})"
        if not any(option in command.observed_safety for option in PREVIEW_OPTIONS):
            findings.append(
                PolicyFinding(
                    surface=resolved,
                    source=cli_source,
                    owner=_SURFACE_OWNERS.get(command.surface, owner),
                    invariant=INVARIANT_PREVIEW,
                    detail="destructive command exposes no non-mutating preview",
                    fix=f"add --dry-run to the {command.name!r} parser in {command.provenance}",
                )
            )
        if not any(option in command.observed_safety for option in CONFIRMATION_OPTIONS):
            findings.append(
                PolicyFinding(
                    surface=resolved,
                    source=cli_source,
                    owner=_SURFACE_OWNERS.get(command.surface, owner),
                    invariant=INVARIANT_CONFIRMATION,
                    detail="destructive command accepts no confirmation option",
                    fix=(
                        f"add --confirm <subject> (preferred, identity-bound) to "
                        f"{command.name!r} in {command.provenance}"
                    ),
                )
            )

    findings.sort(key=lambda finding: (finding.invariant, finding.surface))
    return tuple(findings)


def check_forwarded_argv(
    argv: Sequence[str],
    *,
    surface: str,
    subject: str,
    source: str,
    owner: str,
    require_identity: bool = True,
) -> tuple[PolicyFinding, ...]:
    """Whether a wrapper's argv carries its confirmation to the CLI gate.

    This is the half no static read can answer: the wrapper builds the argv at
    runtime, so the CALLER observes it (under its own stubs) and hands it here
    to be classified. That split is what keeps this module pure while still
    making the forwarding claim testable rather than asserted.

    ``require_identity`` distinguishes the two honest strengths. An
    identity-bound ``--confirm <subject>`` cannot authorize acting on anything
    but ``subject``; a blanket ``--yes`` can, which is why it is not accepted
    where identity is required.
    """
    tokens = [str(token) for token in argv]
    if require_identity:
        if argv_confirms_identity(tokens, subject):
            return ()
        carried = forwarded_safety_options(tokens)
        detail = (
            f"argv carries {list(carried)} but never names {subject!r}"
            if carried
            else f"argv carries no safety option at all: {tokens}"
        )
        return (
            PolicyFinding(
                surface=surface,
                source=source,
                owner=owner,
                invariant=INVARIANT_ARGV_FORWARDED,
                detail=detail,
                fix=f"extend the forwarded argv with ['--confirm', {subject!r}]",
            ),
        )
    if any(option in tokens for option in CONFIRMATION_OPTIONS):
        return ()
    return (
        PolicyFinding(
            surface=surface,
            source=source,
            owner=owner,
            invariant=INVARIANT_ARGV_FORWARDED,
            detail=f"argv carries no confirmation option: {tokens}",
            fix=f"forward one of {list(CONFIRMATION_OPTIONS)} to the CLI gate",
        ),
    )


def unaccepted_findings(
    findings: Sequence[PolicyFinding],
) -> tuple[PolicyFinding, ...]:
    """Findings that are not already recorded as owned gaps."""
    accepted = accepted_finding_keys()
    return tuple(finding for finding in findings if finding.key not in accepted)


def render_findings(findings: Sequence[PolicyFinding]) -> str:
    """Human-readable block. Byte-stable for a given finding sequence."""
    return "\n".join(finding.render() for finding in findings)
