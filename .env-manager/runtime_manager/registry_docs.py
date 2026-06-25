"""Markdown/API reference rendering for the command registry."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from .command_registry import REGISTRY_ABI_VERSION, CommandSpec, default_registry

API_REFERENCE_RELATIVE_PATH = Path("docs") / "API_REFERENCE.md"
_DOMAIN_ORDER = ("brain", "runtime", "outer", "box", "make")


def render_api_reference(specs: Iterable[CommandSpec] | None = None) -> str:
    """Render a deterministic Markdown reference for every registry entry."""
    if specs is None:
        specs = default_registry()
    specs = sorted(
        specs,
        key=lambda spec: (_tier_key(spec), _domain_key(spec), spec.id),
    )
    lines: list[str] = [
        "<!-- GENERATED FILE: do not hand-edit. -->",
        "<!-- Regenerate: python3 .env-manager/manage.py registry-docs --write. -->",
        f"<!-- Generated from command registry ABI: {REGISTRY_ABI_VERSION}. -->",
        "",
        "# Skillbox API Reference",
        "",
        f"Generated from command registry ABI `{REGISTRY_ABI_VERSION}`.",
        "Do not edit by hand; run `python3 .env-manager/manage.py registry-docs --write`.",
        "",
        f"Registry entries: {len(specs)}.",
        "",
    ]

    current_tier: int | None = None
    current_domain: str | None = None
    for spec in specs:
        if spec.tier != current_tier:
            current_tier = spec.tier
            current_domain = None
            lines.extend([f"## Tier {spec.tier}", ""])
        domain = _domain_for(spec)
        if domain != current_domain:
            current_domain = domain
            lines.extend([f"### {domain}", ""])
        lines.extend(_render_spec(spec))
    return "\n".join(lines).rstrip() + "\n"


def registry_docs_payload(
    root_dir: Path,
    *,
    write: bool = False,
    include_content: bool = False,
) -> dict[str, Any]:
    """Return the command payload for ``manage.py registry-docs``."""
    content = render_api_reference()
    path = root_dir / API_REFERENCE_RELATIVE_PATH
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    specs = default_registry()
    payload: dict[str, Any] = {
        "ok": True,
        "abi_version": REGISTRY_ABI_VERSION,
        "count": len(specs),
        "path": str(API_REFERENCE_RELATIVE_PATH),
        "written": bool(write),
        "bytes": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    if include_content:
        payload["content"] = content
    return payload


def _tier_key(spec: CommandSpec) -> int:
    return spec.tier


def _domain_key(spec: CommandSpec) -> tuple[int, str]:
    domain = _domain_for(spec)
    try:
        return (_DOMAIN_ORDER.index(domain), domain)
    except ValueError:
        return (len(_DOMAIN_ORDER), domain)


def _domain_for(spec: CommandSpec) -> str:
    return spec.id.split(".", 1)[0]


def _render_spec(spec: CommandSpec) -> list[str]:
    lines = [
        f"#### {spec.id}",
        "",
        spec.summary,
        "",
        f"- Surfaces: {_join_code(spec.surface)}",
        f"- Scopes: {_join_code(spec.scopes) if spec.scopes else 'None'}",
        f"- Side effect: `{spec.side_effect}`",
        f"- Risk: `{spec.risk}`",
        f"- Entrypoint: `{spec.entrypoint}`",
        f"- Owner binary: `{spec.owner_binary}`" if spec.owner_binary else "- Owner binary: None",
        f"- MCP mirror: `{spec.mcp_tool}`" if spec.mcp_tool else "- MCP mirror: None",
        "",
        "**Inputs**",
        "",
    ]
    lines.extend(_schema_table(spec.inputs))
    lines.extend(["", "**Outputs**", ""])
    lines.extend(_schema_table(spec.outputs))
    lines.extend(["", "**Examples**", ""])
    lines.extend(_command_blocks(spec.examples))
    lines.extend(["", "**Validation**", ""])
    lines.extend(_command_blocks(spec.validations) if spec.validations else ["None"])
    if spec.graph_nodes:
        lines.extend(["", f"**Graph Nodes**: {_join_code(spec.graph_nodes)}"])
    lines.append("")
    return lines


def _schema_table(schema: Mapping[str, Any]) -> list[str]:
    rows = _flatten_schema(schema)
    if not rows:
        return ["None"]
    lines = ["| Name | Type | Required |", "|---|---|---|"]
    for name, type_name, required in rows:
        lines.append(f"| `{_escape_table(name)}` | `{_escape_table(type_name)}` | {required} |")
    return lines


def _flatten_schema(schema: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for key in sorted(schema):
        raw = schema[key]
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(raw, Mapping):
            rows.append((name, "object", "yes"))
            rows.extend(_flatten_schema(raw, name))
            continue
        type_name = str(raw)
        required = "no" if type_name.endswith("?") else "yes"
        rows.append((name, type_name, required))
    return rows


def _command_blocks(commands: Iterable[str]) -> list[str]:
    lines: list[str] = []
    for command in commands:
        if lines:
            lines.append("")
        lines.extend(["```bash", command, "```"])
    return lines or ["None"]


def _join_code(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _escape_table(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


__all__ = [
    "API_REFERENCE_RELATIVE_PATH",
    "render_api_reference",
    "registry_docs_payload",
]
