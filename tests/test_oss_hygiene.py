"""OSS hygiene checks for public docs and defaults.

This does not claim the entire repository is sanitized. It guards the reusable
public surfaces that previously leaked operator-specific paths or repos.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent

PUBLIC_SURFACES = (
    "README.md",
    "AGENTS.md",
    ".env.example",
    ".gitignore",
    "docker-compose.yml",
    "docs/SBP_OUTPUT_SCHEMAS.md",
    "docs/tailnet-ingress.md",
    "scripts/04-reconcile.py",
    "scripts/gen_output_schemas.py",
    "scripts/lib/runtime_model.py",
    "scripts/sbp",
    "workspace/runtime.yaml",
    ".env-manager/runtime_manager/audit_report.py",
    ".env-manager/runtime_manager/cli.py",
    ".env-manager/runtime_manager/operator_booking.py",
    ".env-manager/runtime_manager/command_registry.py",
    ".env-manager/runtime_manager/endpoints.py",
    ".env-manager/runtime_manager/fleet_relink.py",
    ".env-manager/runtime_manager/machines.py",
    ".env-manager/runtime_manager/mcp_render.py",
    ".env-manager/runtime_manager/pressure_report.py",
    ".env-manager/runtime_manager/rch_adapter.py",
    ".env-manager/runtime_manager/rch_report.py",
)

_PRIVATE_OWNER = "Dickles" + "worthstone"
_PRIVATE_REPOS = (
    "sweet" + "-potato",
    "ht" + "ma" + "_server",
    "ingredient" + "_server",
    "un" + "clawg",
    "build" + "ooor",
    "cca" + "-website",
    "voice" + "-to-text",
    "portfolio" + "-devbox",
)

PRIVATE_PATTERNS = (
    re.compile(r"/Users/" + "b" + r"(?=/)"),
    re.compile(r"github\.com/" + re.escape(_PRIVATE_OWNER) + r"/"),
    re.compile(r"\b(" + "|".join(re.escape(name) for name in _PRIVATE_REPOS) + r")\b"),
    re.compile(r"/srv/skillbox/repos/skills-" + "private" + r"\b"),
    re.compile(r"/home/skillbox/repos/(marketing" + "skills" + r"|skills-" + "private" + r")\b"),
)


class OssHygieneTests(unittest.TestCase):
    maxDiff = None

    def test_public_surfaces_do_not_reference_private_operator_repos(self) -> None:
        hits: list[str] = []
        for rel_path in PUBLIC_SURFACES:
            path = ROOT_DIR / rel_path
            self.assertTrue(path.is_file(), rel_path)
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                for pattern in PRIVATE_PATTERNS:
                    if pattern.search(line):
                        hits.append(f"{rel_path}:{line_no}: {line.strip()}")
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
