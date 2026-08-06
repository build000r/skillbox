"""OSS hygiene checks for public docs and defaults.

This does not claim the entire repository is sanitized. It guards the reusable
public surfaces that previously leaked operator-specific paths or repos.
"""
from __future__ import annotations

import re
import subprocess
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


# Real fleet identity must never appear in the tracked tree. History was
# filter-repo scrubbed on 2026-07-30; these generic patterns keep it that way
# without this file itself carrying any banned literal.
#
# Placeholder convention: fake tailnet IPs live in 100.100.0.0/16, fake
# tailnet ids use non-hex text (e.g. "tailexample"), and redacted Tailscale
# control ids carry the REDACTED marker.
_FLEET_IP_RE = re.compile(
    r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b"
)
_PLACEHOLDER_IP_PREFIX = "100.100."
_TAILNET_ID_RE = re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b")
_TS_CONTROL_ID_RE = re.compile(r"\b[A-Za-z0-9]{10,}CNTRL\b")
_REDACTED_MARKER = "REDACTED"
_FLEET_HOSTNAMES = (
    "skillbox" + "-portfolio-devbox",
    "sweet" + "-potato-prod",
    "skillbox" + "-jeremy",
    "tail" + "4c481e",
)


def _tracked_files() -> list[str]:
    """Files that ship with the repo.

    The canonical gate (scripts/self-test.sh) runs against a `git archive`
    extract, which is NOT a git repo — so `git ls-files` raised CalledProcessError
    and this test could never pass under the very gate that is supposed to
    enforce it. It went unnoticed because it passes fine in a working checkout.

    In an archive extract every present file is by definition a tracked file, so
    walking the tree there checks exactly the same invariant.
    """
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.splitlines()

    walked: list[str] = []
    for path in ROOT_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT_DIR)
        # Never present in an archive; skip defensively so a stray local run
        # does not scan build output or vendored trees.
        if any(part in {".git", "node_modules", "__pycache__", ".skillbox-state"} for part in rel.parts):
            continue
        walked.append(str(rel))
    return walked


class OssHygieneTests(unittest.TestCase):
    maxDiff = None

    def test_tracked_tree_has_no_real_fleet_identity(self) -> None:
        hits: list[str] = []
        for rel_path in _tracked_files():
            path = ROOT_DIR / rel_path
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                for match in _FLEET_IP_RE.finditer(line):
                    if match.group(0).startswith(_PLACEHOLDER_IP_PREFIX):
                        continue
                    if line[match.end() : match.end() + 3] == "/10":
                        continue  # the CGNAT range literal itself, e.g. ufw/ipaddress rules
                    hits.append(f"{rel_path}:{line_no}: fleet ip {match.group(0)}")
                for match in _TS_CONTROL_ID_RE.finditer(line):
                    if _REDACTED_MARKER not in match.group(0):
                        hits.append(f"{rel_path}:{line_no}: control id {match.group(0)}")
                if _TAILNET_ID_RE.search(line):
                    hits.append(f"{rel_path}:{line_no}: tailnet id")
                for name in _FLEET_HOSTNAMES:
                    if name in line:
                        hits.append(f"{rel_path}:{line_no}: fleet hostname {name}")
        self.assertEqual(
            [],
            hits,
            msg=(
                "real fleet identity in tracked tree; use 100.100.0.0/16 "
                "placeholders, *.example targets, or the private registry "
                "(SKILLBOX_CLIPBOARD_HOSTS) instead"
            ),
        )

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
