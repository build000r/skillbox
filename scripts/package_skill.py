#!/usr/bin/env python3
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from lib.skill_bundle_filter import iter_included_skill_files
from quick_validate import validate_skill


def package_skill(skill_path: str | Path, output_dir: str | Path | None = None) -> Path | None:
    """Package a skill folder into a distributable `.skill` archive."""
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists():
        print(f"Error: skill folder not found: {skill_path}")
        return None
    if not skill_path.is_dir():
        print(f"Error: path is not a directory: {skill_path}")
        return None
    if not (skill_path / "SKILL.md").exists():
        print(f"Error: SKILL.md not found in {skill_path}")
        return None

    print("Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"Validation failed: {message}")
        print("Please fix the validation errors before packaging.")
        return None
    print(f"{message}\n")

    if output_dir is None:
        output_path = Path.cwd()
    else:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)

    skill_filename = output_path / f"{skill_path.name}.skill"

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in iter_included_skill_files(skill_path):
                arcname = file_path.relative_to(skill_path.parent)
                archive.write(file_path, arcname)
                print(f"  Added: {arcname}")
    except Exception as exc:
        print(f"Error creating .skill file: {exc}")
        return None

    print()
    print(f"Successfully packaged skill to: {skill_filename}")
    return skill_filename


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/package_skill.py <path/to/skill-folder> [output-directory]")
        print()
        print("Example:")
        print("  python scripts/package_skill.py skills/public/my-skill")
        print("  python scripts/package_skill.py skills/public/my-skill ./dist")
        return 1

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Packaging skill: {skill_path}")
    if output_dir:
        print(f"Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
