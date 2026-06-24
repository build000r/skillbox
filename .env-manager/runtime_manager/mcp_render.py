"""Single-source MCP config renderer.

The MCP servers a repo should expose are declared once — as the ``kind:mcp``
services in ``workspace/runtime.yaml`` plus the built-in ``skillbox`` server —
and surfaced through :func:`runtime_manager.workflows.requested_mcp_servers`
(names) and the per-server bodies in this repo's ``.mcp.json``
(``selected_mcp_server_configs``). That same declaration is what
``runtime_manager.mcp_visibility.collect_mcp_audit`` AUDITS against, so audit and
render agree by construction: both read ``selected_mcp_server_configs`` /
``requested_mcp_servers``.

This module RENDERS that one declaration into the two agent-runtime surfaces:

  * ``.mcp.json``            — Claude Code project config (JSON, ``mcpServers``).
  * ``.codex/config.toml``   — Codex project config (TOML, ``mcp_servers``).

Design guarantees (mirroring the audit's stance):

  * **Single source.** Both surfaces are rendered from the same canonical
    ``{name: body}`` map, so they can never disagree after a sync.
  * **Machine-profile path resolution.** The Codex ``cwd`` (and any
    machine-rooted path) resolves through :mod:`runtime_manager.machines` so a
    devbox TOML never contains a foreign ``/Users/operator`` path: a target root that
    belongs to *another* machine's declared roots is translated onto the current
    machine's canonical root before it is written.
  * **Operator-managed entries are preserved.** Entries already present in a
    surface that are NOT part of the declared set are never removed (mirror the
    audit's review-before-remove stance). Declared services flagged
    ``operator_managed: true`` are left exactly as the operator wrote them.
  * **Dry-run is symmetric with apply.** Both paths compute the *identical*
    rendered file text; ``--dry-run`` prints exactly what ``--apply`` would
    write (byte-for-byte, including the diff).

The user-global ``~/.codex/config.toml`` is intentionally OUT OF SCOPE: it is
operator-managed and this renderer only ever touches a repo/config root's
``.codex/config.toml`` (and ``.mcp.json``), never the home-global file. This is
ENFORCED (not merely documented): :func:`collect_mcp_render` resolves the target
and, if the Codex surface path equals ``~/.codex/config.toml`` (which happens for
``mcp sync --cwd ~`` when home has no ``.git`` ancestor), the surface is REFUSED
and never written — see :func:`_is_user_global_codex`.
"""

from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

from . import machines as machines_mod
from .machines import MachineProfile, MachinesConfig
from .mcp_visibility import (
    CLAUDE_MCP_REL,
    CODEX_MCP_REL,
    _read_claude_mcp,
    _read_codex_mcp,
    _target_config_root,
)
from .shared import repo_rel
from .workflows import requested_mcp_servers, selected_mcp_server_configs


# The home-global Codex config is explicitly operator-managed; this renderer
# refuses to write it. Documented here and enforced in :func:`collect_mcp_render`
# / :func:`render_mcp_sync` via :func:`_is_user_global_codex`.
USER_GLOBAL_CODEX_REL = Path(".codex") / "config.toml"
# The home-global Claude config is likewise operator-managed and out of scope.
USER_GLOBAL_CLAUDE_REL = Path(".claude.json")


def _is_user_global_codex(path: Path, *, home: Path | None = None) -> bool:
    """True iff ``path`` resolves to the operator's global ``~/.codex/config.toml``.

    A ``mcp sync --cwd ~`` (a cwd with no ``.git`` ancestor above home) resolves
    the Codex target to exactly this file, which is the operator's global Codex
    source of truth. The renderer must NEVER overwrite it lossily, so we refuse.
    """
    base = (home if home is not None else Path.home())
    try:
        return path.expanduser().resolve() == (base / USER_GLOBAL_CODEX_REL).expanduser().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - resolve edge cases
        return False


def _is_user_global_claude(path: Path, *, home: Path | None = None) -> bool:
    """True iff ``path`` resolves to the operator's global ``~/.claude.json``."""
    base = (home if home is not None else Path.home())
    try:
        return path.expanduser().resolve() == (base / USER_GLOBAL_CLAUDE_REL).expanduser().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - resolve edge cases
        return False


# ---------------------------------------------------------------------------
# Canonical declaration -> ordered server map
# ---------------------------------------------------------------------------


def _operator_managed_servers(model: dict[str, Any]) -> set[str]:
    """Names of declared MCP servers the operator owns (never rewritten).

    A ``kind:mcp`` service may opt out of rendering by setting
    ``operator_managed: true`` in ``workspace/runtime.yaml``. Its existing
    surface entry is preserved verbatim instead of being regenerated.
    """
    from .workflows import mcp_server_name_for_service

    managed: set[str] = set()
    for service in model.get("services") or []:
        if str(service.get("kind") or "").strip() != "mcp":
            continue
        if not _truthy(service.get("operator_managed")):
            continue
        name = mcp_server_name_for_service(service)
        if name:
            managed.add(name)
    return managed


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def canonical_server_map(
    root_dir: Path,
    model: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The single source of truth: ``{name: body}`` for every declared server.

    Bodies come from this repo's ``.mcp.json`` (translated for the runtime env),
    selected by the SAME ``requested_mcp_servers`` set the audit checks. Order
    follows ``requested_mcp_servers`` (skillbox first, then declared services).
    Returns ``(server_map, ordered_names)``.
    """
    selected, names = selected_mcp_server_configs(root_dir, model)
    ordered = [str(item["name"]) for item in requested_mcp_servers(model) if item.get("name")]
    # selected only contains servers that resolved to a config body; keep the
    # declared order but drop names with no body (optional, profile-gated).
    ordered_present = [name for name in ordered if name in selected]
    for name in names:
        if name not in ordered_present and name in selected:
            ordered_present.append(name)
    return {name: selected[name] for name in ordered_present}, ordered_present


# ---------------------------------------------------------------------------
# Machine-profile path resolution
# ---------------------------------------------------------------------------


def resolve_codex_cwd(
    target_root: Path,
    *,
    machines: MachinesConfig | None = None,
    profile: MachineProfile | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Absolute repo root for the Codex ``cwd``, resolved for THIS machine.

    The Codex ``cwd`` must be a path that exists on the box that will run Codex.
    If ``target_root`` is rooted under some *other* machine's declared roots
    (e.g. a Mac ``/Users/operator/...`` path handed to a devbox), translate it onto the
    current machine's canonical root via :mod:`runtime_manager.machines`. When no
    machines.yaml is available, or the path is already local, the resolved
    absolute path is returned unchanged.
    """
    local = str(target_root)
    try:
        config = machines if machines is not None else machines_mod.load_machines_config(env=env)
    except machines_mod.MachinesConfigError:
        return local
    current = profile if profile is not None else config.current_profile(env=env)
    if current is None:
        return local

    classified = config.classify_path(local)
    machine_ids = classified.get("machines") or []
    if not machine_ids:
        # Path is under no declared root; nothing to translate.
        return local
    if current.machine_id in machine_ids:
        # Already a local path for this machine (alias-canonicalized).
        return config.canonicalize_alias(local)

    # Foreign path: translate from the owning machine onto this machine's root.
    for src_machine in machine_ids:
        translated = config.translate_path(local, src_machine, current.machine_id)
        if translated:
            return translated
    return local


# ---------------------------------------------------------------------------
# Surface readers (existing on disk) — reuse audit parsers for parity
# ---------------------------------------------------------------------------


def _existing_claude_servers(path: Path) -> dict[str, Any]:
    servers, _error = _read_claude_mcp(path)
    return servers if isinstance(servers, dict) else {}


def _existing_codex_doc(path: Path) -> dict[str, Any]:
    """Full parsed Codex TOML doc (so we preserve ``[features]``/``[apps.*]``)."""
    if not path.is_file() or tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):  # type: ignore[union-attr]
        return {}


# ---------------------------------------------------------------------------
# Merge: declared (managed) + preserved (operator/unmanaged) entries
# ---------------------------------------------------------------------------


def _merge_servers(
    declared: dict[str, dict[str, Any]],
    existing: dict[str, Any],
    *,
    operator_managed: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Combine declared bodies with operator/unmanaged entries to preserve.

    Returns ``(merged, provenance)`` where provenance maps name ->
    "managed" | "operator-managed" | "preserved". Managed entries are
    (re)generated from the declaration; operator-managed declared entries keep
    their on-disk body; entries present on disk but absent from the declaration
    are preserved untouched (never blown away).
    """
    merged: dict[str, dict[str, Any]] = {}
    provenance: dict[str, str] = {}

    for name, body in declared.items():
        if name in operator_managed and name in existing:
            merged[name] = copy.deepcopy(existing[name])
            provenance[name] = "operator-managed"
        elif name in operator_managed and name not in existing:
            # Operator owns it but it is not present yet; do not synthesize a
            # body the operator did not author. Skip — surfaced as a hint.
            provenance[name] = "operator-managed-absent"
        else:
            merged[name] = copy.deepcopy(body)
            provenance[name] = "managed"

    for name, body in existing.items():
        if name in merged or name in provenance:
            continue
        merged[name] = copy.deepcopy(body)
        provenance[name] = "preserved"

    return merged, provenance


# ---------------------------------------------------------------------------
# Surface renderers (declaration -> file text)
# ---------------------------------------------------------------------------


def render_claude_json(servers: dict[str, dict[str, Any]]) -> str:
    """Render ``.mcp.json`` text from the merged server map (stable order)."""
    ordered = {name: servers[name] for name in sorted(servers)}
    return json.dumps({"mcpServers": ordered}, indent=2, sort_keys=False) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def _toml_table_key(name: str) -> str:
    """Quote a TOML key only when it is not a bare key (matches Codex output)."""
    if name and all(ch.isalnum() or ch in "-_" for ch in name):
        return name
    return json.dumps(name)


def _emit_server_body(name: str, body: dict[str, Any], *, cwd: str) -> list[str]:
    """Faithfully emit one ``[mcp_servers.<name>]`` table from its body.

    The body is re-emitted LOSSLESSLY so no operator/declared key is dropped:

      * ``command`` then ``args`` first (Codex's conventional ordering), then
        every OTHER scalar/list key in a stable (sorted) order — e.g.
        ``startup_timeout_ms``, and for http-type servers ``type``/``url``/
        ``headers``/``bearer_token_env_var``;
      * each nested-dict key as its own ``[mcp_servers.<name>.<subtable>]``
        (recursively, so ``env`` is just one such subtable and a deeper
        ``tools.<x>`` table survives too).

    The machine-resolved ``cwd`` is injected ONLY when the body has no ``cwd``
    of its own — an operator's explicit ``cwd`` always wins. ``cwd`` is never
    forced onto a body that carries no ``command`` (e.g. an http-type server),
    so we don't brick a remote server with a bogus working directory.
    """
    table = f"[mcp_servers.{_toml_table_key(name)}]"
    lines: list[str] = [table]

    scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in body.items() if isinstance(v, dict)}

    # command / args first (conventional Codex ordering), then the remaining
    # scalar/list keys in a stable order so renders are byte-identical.
    if "command" in scalars:
        lines.append(f"command = {_toml_value(scalars['command'])}")
    if "args" in scalars and scalars["args"]:
        lines.append(f"args = {_toml_value(list(scalars['args']))}")

    has_own_cwd = "cwd" in scalars
    # Inject the machine-resolved cwd only when the body lacks its own and it
    # is a launched (command-based) server. Operator cwd wins; remote/http
    # servers (no command) are never given a working directory.
    if not has_own_cwd and "command" in scalars:
        lines.append(f"cwd = {_toml_value(cwd)}")

    for key in sorted(k for k in scalars if k not in {"command", "args"}):
        lines.append(f"{_toml_table_key(key)} = {_toml_value(scalars[key])}")

    for key in sorted(nested):
        prefix = f"mcp_servers.{_toml_table_key(name)}"
        lines.extend(_emit_toml_section(key, nested[key], prefix=prefix))

    return lines


def render_codex_toml(
    servers: dict[str, dict[str, Any]],
    *,
    cwd: str,
    preamble: str = "",
) -> str:
    """Render ``.codex/config.toml`` text from the merged server map.

    ``preamble`` carries any preserved non-MCP TOML (top-level scalars like
    ``model``/``approval_policy`` plus tables like ``[features]``/``[apps.*]``)
    verbatim so operator-managed Codex sections survive a sync. Each
    ``[mcp_servers.<name>]`` table is re-emitted FAITHFULLY from its body (see
    :func:`_emit_server_body`); the machine-resolved ``cwd`` is injected only
    when the body carries no ``cwd`` of its own.
    """
    lines: list[str] = []
    if preamble.strip():
        lines.append(preamble.rstrip("\n"))
        lines.append("")
    for name in sorted(servers):
        lines.extend(_emit_server_body(name, servers[name], cwd=cwd))
        lines.append("")
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


def _codex_preamble(doc: dict[str, Any], raw_text: str) -> str:
    """Extract preserved non-MCP TOML sections (schema header, features, apps).

    We re-emit them from the parsed doc so the renderer output is canonical and
    diffable, while keeping operator-managed Codex behavior intact. The leading
    ``#:schema`` comment (not part of the parsed doc) is carried from raw text.
    """
    lines: list[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#:schema") or (stripped.startswith("#") and "schema" in stripped):
            lines.append(line.rstrip())
            break
    sections = _render_preserved_sections(doc)
    if sections:
        if lines:
            lines.append("")
        lines.append(sections)
    return "\n".join(lines).rstrip("\n")


def _render_preserved_sections(doc: dict[str, Any]) -> str:
    """Emit every top-level TOML entry EXCEPT ``mcp_servers`` verbatim-ish.

    Top-level SCALAR keys (``model = "gpt-5.5"``, ``approval_policy``,
    ``sandbox_mode``, ``cli_auth_credentials_store`` …) are emitted FIRST, as
    bare ``key = value`` lines, because in TOML any key after a ``[table]``
    header belongs to that table. Then every nested table is emitted. Without
    the leading scalars, those settings were silently dropped on every sync.
    """
    lines: list[str] = []
    top_scalars = {k: v for k, v in doc.items() if k != "mcp_servers" and not isinstance(v, dict)}
    top_tables = {k: v for k, v in doc.items() if k != "mcp_servers" and isinstance(v, dict)}
    for key in top_scalars:
        lines.append(f"{_toml_table_key(key)} = {_toml_value(top_scalars[key])}")
    if top_scalars and top_tables:
        lines.append("")
    for key in top_tables:
        lines.extend(_emit_toml_section(key, top_tables[key]))
    return "\n".join(lines).rstrip("\n")


def _emit_toml_section(key: str, value: Any, prefix: str = "") -> list[str]:
    """Emit a TOML table (possibly nested) for a preserved non-MCP section."""
    full_key = f"{prefix}.{_toml_table_key(key)}" if prefix else _toml_table_key(key)
    lines: list[str] = []
    if isinstance(value, dict):
        scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in value.items() if isinstance(v, dict)}
        lines.append(f"[{full_key}]")
        for k in scalars:
            lines.append(f"{_toml_table_key(k)} = {_toml_value(scalars[k])}")
        lines.append("")
        for k in tables:
            lines.extend(_emit_toml_section(k, tables[k], prefix=full_key))
    return lines


# ---------------------------------------------------------------------------
# Top-level: collect_mcp_render + render_mcp_sync (dry-run/apply symmetric)
# ---------------------------------------------------------------------------


def _diff(before: str, after: str, label: str) -> list[str]:
    return list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (current)",
            tofile=f"{label} (rendered)",
        )
    )


def collect_mcp_render(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    config_root: str | None = None,
    machines: MachinesConfig | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the full render plan for both surfaces. PURE — no writes.

    This is what both ``--dry-run`` and ``--apply`` consume: it produces the
    exact text each surface should contain, the diff vs. the current file, and
    the provenance of every entry. ``--dry-run`` prints it; ``--apply`` writes
    ``surfaces[*]['rendered']`` to ``surfaces[*]['path']``.
    """
    target_root = _target_config_root(root_dir, cwd=cwd, config_root=config_root)
    claude_path = target_root / CLAUDE_MCP_REL
    codex_path = target_root / CODEX_MCP_REL

    # SAFETY GUARD: never overwrite the operator's home-global Codex/Claude
    # source-of-truth. A `mcp sync --cwd ~` resolves the Codex target to exactly
    # `~/.codex/config.toml`; writing it would lossily clobber the global config.
    codex_refused = (
        "refusing to write the user-global ~/.codex/config.toml (operator-managed)"
        if _is_user_global_codex(codex_path)
        else None
    )
    claude_refused = (
        "refusing to write the user-global ~/.claude.json (operator-managed)"
        if _is_user_global_claude(claude_path)
        else None
    )

    declared, declared_order = canonical_server_map(root_dir, model)
    operator_managed = _operator_managed_servers(model)
    codex_cwd = resolve_codex_cwd(target_root, machines=machines, env=env)

    # --- Claude (.mcp.json) ---
    existing_claude = _existing_claude_servers(claude_path)
    claude_servers, claude_prov = _merge_servers(
        declared, existing_claude, operator_managed=operator_managed
    )
    claude_rendered = render_claude_json(claude_servers)
    claude_current = (
        claude_path.read_text(encoding="utf-8") if claude_path.is_file() else ""
    )

    # --- Codex (.codex/config.toml) ---
    codex_raw = codex_path.read_text(encoding="utf-8") if codex_path.is_file() else ""
    codex_doc = _existing_codex_doc(codex_path)
    existing_codex = codex_doc.get("mcp_servers")
    existing_codex = existing_codex if isinstance(existing_codex, dict) else {}
    codex_servers, codex_prov = _merge_servers(
        declared, existing_codex, operator_managed=operator_managed
    )
    preamble = _codex_preamble(codex_doc, codex_raw)
    codex_rendered = render_codex_toml(codex_servers, cwd=codex_cwd, preamble=preamble)

    surfaces = {
        "claude": _surface_render_payload(
            name="claude",
            fmt="json",
            path=claude_path,
            current=claude_current,
            rendered=claude_rendered,
            servers=sorted(claude_servers),
            provenance=claude_prov,
            root_dir=root_dir,
            refused=claude_refused,
        ),
        "codex": _surface_render_payload(
            name="codex",
            fmt="toml",
            path=codex_path,
            current=codex_raw,
            rendered=codex_rendered,
            servers=sorted(codex_servers),
            provenance=codex_prov,
            root_dir=root_dir,
            refused=codex_refused,
        ),
    }
    payload: dict[str, Any] = {
        "config_root": str(target_root),
        "codex_cwd": codex_cwd,
        "declared_servers": declared_order,
        "operator_managed": sorted(operator_managed),
        "surfaces": surfaces,
        "summary": {
            "declared": len(declared_order),
            "operator_managed": len(operator_managed),
            "claude_changed": surfaces["claude"]["changed"],
            "codex_changed": surfaces["codex"]["changed"],
            "preserved": sorted(
                set(
                    name
                    for surface in surfaces.values()
                    for name, kind in surface["provenance"].items()
                    if kind in {"preserved", "operator-managed"}
                )
            ),
        },
    }
    return payload


def _surface_render_payload(
    *,
    name: str,
    fmt: str,
    path: Path,
    current: str,
    rendered: str,
    servers: list[str],
    provenance: dict[str, str],
    root_dir: Path,
    refused: str | None = None,
) -> dict[str, Any]:
    # A refused surface (e.g. the home-global ~/.codex/config.toml) is NEVER
    # written: report changed=False so apply skips it, and carry the reason.
    changed = (current != rendered) and not refused
    try:
        rel = repo_rel(root_dir, path)
    except ValueError:
        rel = str(path)
    return {
        "name": name,
        "format": fmt,
        "path": str(path),
        "rel_path": rel,
        "present": path.is_file(),
        "changed": changed,
        "refused": bool(refused),
        "refused_reason": refused,
        "servers": servers,
        "provenance": provenance,
        "rendered": rendered,
        "current": current,
        "diff": _diff(current, rendered, rel) if changed else [],
    }


def render_mcp_sync(
    root_dir: Path,
    model: dict[str, Any],
    *,
    cwd: str | None = None,
    config_root: str | None = None,
    apply: bool = False,
    machines: MachinesConfig | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Plan (and optionally apply) the MCP render for both surfaces.

    ``apply=False`` (dry-run) and ``apply=True`` compute the SAME plan via
    :func:`collect_mcp_render`; the only difference is that apply writes
    ``rendered`` to disk. The returned payload is identical in shape so callers
    can prove symmetry.
    """
    payload = collect_mcp_render(
        root_dir,
        model,
        cwd=cwd,
        config_root=config_root,
        machines=machines,
        env=env,
    )
    payload["applied"] = bool(apply)
    payload["dry_run"] = not bool(apply)
    written: list[str] = []
    refused: list[str] = []
    if apply:
        for surface in payload["surfaces"].values():
            # Defense in depth: never write a refused surface (e.g. the
            # home-global ~/.codex/config.toml), even if upstream said changed.
            if surface.get("refused"):
                refused.append(str(surface["path"]))
                continue
            if not surface["changed"]:
                continue
            dest = Path(surface["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(surface["rendered"], encoding="utf-8")
            written.append(str(dest))
    payload["written"] = written
    payload["refused"] = sorted(
        s["path"]
        for s in payload["surfaces"].values()
        if s.get("refused")
    )
    payload["next_actions"] = _render_next_actions(payload, apply=bool(apply))
    return payload


def _render_next_actions(payload: dict[str, Any], *, apply: bool) -> list[str]:
    actions: list[str] = []
    surfaces = payload.get("surfaces") or {}
    for surface in surfaces.values():
        if surface.get("refused"):
            reason = surface.get("refused_reason") or "refused (out of scope)"
            actions.append(f"skipped {surface['rel_path']}: {reason}")
    changed = [s for s in surfaces.values() if s.get("changed")]
    if not changed:
        if not actions:
            actions.append("mcp config already matches the declaration; nothing to write")
        return actions
    if apply:
        for surface in changed:
            actions.append(f"wrote {surface['rel_path']}")
    else:
        names = ", ".join(s["rel_path"] for s in changed)
        actions.append(f"run `mcp sync --apply` to write: {names}")
    return actions


# ---------------------------------------------------------------------------
# Text rendering (CLI --format text)
# ---------------------------------------------------------------------------


def print_mcp_render_text(payload: dict[str, Any], *, root_dir: Path) -> None:
    summary = payload.get("summary") or {}
    mode = "apply" if payload.get("applied") else "dry-run"
    print(
        f"mcp sync ({mode}): "
        f"declared={summary.get('declared', 0)} "
        f"operator_managed={summary.get('operator_managed', 0)} "
        f"claude_changed={str(summary.get('claude_changed')).lower()} "
        f"codex_changed={str(summary.get('codex_changed')).lower()}"
    )
    print(f"config_root: {payload.get('config_root')}")
    print(f"codex_cwd: {payload.get('codex_cwd')}")
    preserved = summary.get("preserved") or []
    if preserved:
        print(f"preserved (operator/unmanaged): {', '.join(preserved)}")
    for key in ("claude", "codex"):
        surface = (payload.get("surfaces") or {}).get(key) or {}
        managed = [n for n, k in (surface.get("provenance") or {}).items() if k == "managed"]
        print(
            f"{key}: {surface.get('rel_path')} "
            f"changed={str(surface.get('changed')).lower()} "
            f"servers={len(surface.get('servers') or [])} "
            f"managed={len(managed)}"
        )
        for line in surface.get("diff") or []:
            print(line.rstrip("\n"))
    for action in payload.get("next_actions") or []:
        print(f"  - {action}")
