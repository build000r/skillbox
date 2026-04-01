#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from lib.skill_bundle_filter import iter_included_skill_files


ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "type",
    "license",
    "allowed-tools",
    "metadata",
}

PRIVACY_PATTERNS = [
    (r"/Users/\w+/", "Hardcoded user path"),
    (r"/root/dev/", "Hardcoded server path"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "IP address"),
    (r'(?:api[_-]?key|token|password|secret)\s*[=:]\s*["\'][^"\']{8,}', "Possible secret/credential"),
    (r"\?fpr=|\?ref=|\?affiliate=", "Referral/affiliate link"),
]


def validate_skill(skill_path: str | Path, strict: bool = False) -> tuple[bool, str]:
    """Validate a skill directory before packaging."""
    skill_path = Path(skill_path).resolve()
    warnings: list[str] = []

    valid, payload = _load_skill_document(skill_path)
    if not valid:
        return False, payload
    content, body = payload

    valid, payload = _parse_frontmatter(content)
    if not valid:
        return False, payload
    frontmatter = payload

    valid, message = _validate_frontmatter(frontmatter, warnings)
    if not valid:
        return False, message

    valid, message = _validate_body(body)
    if not valid:
        return False, message

    _collect_privacy_warnings(skill_path, warnings)
    _collect_size_warnings(content, warnings)
    _collect_empty_directory_warnings(skill_path, warnings)

    if strict and warnings:
        return False, f"Strict mode failed: {warnings[0]}"
    if warnings:
        return True, "Skill is valid with warnings:\n  - " + "\n  - ".join(warnings)
    return True, "Skill is valid!"


def _load_skill_document(skill_path: Path) -> tuple[bool, str | tuple[str, str]]:
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"
    return True, (content, _skill_body(content))


def _skill_body(content: str) -> str:
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return ""
    return content[match.end() :]


def _parse_frontmatter(content: str) -> tuple[bool, str | dict]:
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        return False, f"Invalid YAML in frontmatter: {exc}"

    if not isinstance(frontmatter, dict):
        return False, "Frontmatter must be a YAML dictionary"
    return True, frontmatter


def _validate_frontmatter(frontmatter: dict, warnings: list[str]) -> tuple[bool, str]:
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS
    if unexpected_keys:
        return False, (
            "Unexpected key(s) in SKILL.md frontmatter: "
            f"{', '.join(sorted(unexpected_keys))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}"
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    valid, message = _validate_name(frontmatter.get("name"))
    if not valid:
        return False, message

    valid, message = _validate_description(frontmatter.get("description"), warnings)
    if not valid:
        return False, message
    return True, ""


def _validate_body(body: str) -> tuple[bool, str]:
    todo_matches = re.findall(r"\[TODO[:\]].{0,50}", body, re.IGNORECASE)
    if todo_matches:
        return False, f"Incomplete skill: found TODO marker(s): {todo_matches[0]}..."
    return True, ""


def _collect_privacy_warnings(skill_path: Path, warnings: list[str]) -> None:
    for file_path in iter_included_skill_files(skill_path):
        rel = str(file_path.relative_to(skill_path))
        if rel == "scripts/quick_validate.py":
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern, label in PRIVACY_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                sample = matches[0] if isinstance(matches[0], str) else str(matches[0])
                warnings.append(f"Privacy: {label} in {rel} ({sample[:40]}...)")


def _collect_size_warnings(content: str, warnings: list[str]) -> None:
    line_count = len(content.splitlines())
    if line_count > 500:
        warnings.append(
            f"SKILL.md has {line_count} lines (recommended max: 500). Consider splitting into references/."
        )


def _collect_empty_directory_warnings(skill_path: Path, warnings: list[str]) -> None:
    for resource_dir in ("scripts", "references", "assets"):
        dir_path = skill_path / resource_dir
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        files = [item for item in dir_path.iterdir() if not item.name.startswith(".") and item.name != "__pycache__"]
        if not files:
            warnings.append(f"Empty directory: {resource_dir}/ (delete if unused)")


def _validate_name(name: object) -> tuple[bool, str]:
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"

    name = name.strip()
    if not name:
        return True, ""
    if not re.match(r"^[a-z0-9-]+$", name):
        return False, f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
    if len(name) > 64:
        return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters."
    return True, ""


def _validate_description(description: object, warnings: list[str]) -> tuple[bool, str]:
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"

    description = description.strip()
    if not description:
        return True, ""
    if "<" in description or ">" in description:
        return False, "Description cannot contain angle brackets (< or >)"
    if len(description) > 1024:
        return False, f"Description is too long ({len(description)} characters). Maximum is 1024 characters."
    if len(description) < 50:
        warnings.append(f"Description is short ({len(description)} chars). Consider adding trigger phrases.")
    return True, ""


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python quick_validate.py <skill_directory> [--strict]")
        print()
        print("Options:")
        print("  --strict    Treat warnings as errors")
        return 1

    skill_path = sys.argv[1]
    strict = "--strict" in sys.argv

    valid, message = validate_skill(skill_path, strict=strict)
    print(message)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
