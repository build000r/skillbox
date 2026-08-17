"""OSS hygiene checks for public docs and defaults.

This does not claim the entire repository is sanitized. It guards the reusable
public surfaces that previously leaked operator-specific paths or repos.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
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
    "docs/orb-tailnet-bootstrap.md",
    "scripts/orb/join-tailnet.sh",
)

# Files that are handed to a remote Amp Orb (the orb kit ships join-tailnet.sh
# verbatim) or that document the tailnet bootstrap. A real box address reaching
# these republishes fleet identity outside the tailnet, so they get a dedicated
# assertion rather than relying on the tree-wide sweep alone.
ORB_LANE_SURFACES = (
    "docs/orb-tailnet-bootstrap.md",
    "scripts/orb/join-tailnet.sh",
    "scripts/orb/deploy_preflight.py",
    "scripts/orb/orb_readiness.py",
    "scripts/sbpd.py",
    "tests/test_sbpd.py",
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
# The CGNAT range's own boundary literals. A test that proves "is this address
# inside the tailnet range" naturally reaches for the network address or its
# first host; neither is a stand-in for a real machine, which is what the
# 100.100.0.0/16 placeholder block is for. Kept as two exact strings rather than
# a subnet rule so this exemption cannot widen without an explicit edit.
_STRUCTURAL_RANGE_LITERALS = ("100.64.0.0", "100.64.0.1")
_TAILNET_ID_RE = re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b")
_TS_CONTROL_ID_RE = re.compile(r"\b[A-Za-z0-9]{10,}CNTRL\b")
_REDACTED_MARKER = "REDACTED"
_FLEET_HOSTNAMES = (
    "skillbox" + "-portfolio-devbox",
    "sweet" + "-potato-prod",
    "skillbox" + "-jeremy",
    "tail" + "4c481e",
)

# One legacy beads issue ID baked a real MagicDNS name into the identifier
# itself, so it recurs in every dependency edge that points at it. Renaming an
# issue ID is not a br CLI operation — it means hand-rewriting the primary key
# across the FK-linked tables of a database the whole swarm is writing to, which
# is a worse risk than the leak. Tracked for a proper rename in skillbox-csbw.
# Exempted as one exact string: any *other* occurrence of the name still fails.
_LEGACY_ID_EXEMPTION = "skillbox" + "-portfolio-devbox" + "-posture-pilot-gmb"


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


def _wip_files() -> list[str]:
    """Untracked, non-ignored files — what the next `git add -A` would publish.

    The tracked-tree sweep only fires once a leak is already committed, which is
    exactly one commit too late. This is the pre-commit half: `--exclude-standard`
    honours .gitignore, so local scratch and generated state stay out of scope.

    Under the canonical gate the checkout is a `git archive` extract with no git
    repo, so nothing is untracked there and this degrades to a no-op.
    """
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def _fleet_identity_hits(rel_paths: list[str]) -> list[str]:
    """Every real-fleet-identity occurrence in ``rel_paths``, as file:line notes.

    Placeholders are the only accepted form: fake IPs in 100.100.0.0/16, fake
    tailnet ids in non-hex text, control ids carrying the REDACTED marker.
    """
    hits: list[str] = []
    for rel_path in rel_paths:
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
                if match.group(0) in _STRUCTURAL_RANGE_LITERALS:
                    continue
                hits.append(f"{rel_path}:{line_no}: fleet ip {match.group(0)}")
            for match in _TS_CONTROL_ID_RE.finditer(line):
                if _REDACTED_MARKER not in match.group(0):
                    hits.append(f"{rel_path}:{line_no}: control id {match.group(0)}")
            if _TAILNET_ID_RE.search(line):
                hits.append(f"{rel_path}:{line_no}: tailnet id")
            # Blank the one exempt identifier before the hostname sweep, so the
            # name is still caught everywhere else on the same line.
            hostname_line = line.replace(_LEGACY_ID_EXEMPTION, "")
            for name in _FLEET_HOSTNAMES:
                if name in hostname_line:
                    hits.append(f"{rel_path}:{line_no}: fleet hostname {name}")
    return hits


_REMEDIATION = (
    "use 100.100.0.0/16 placeholders, *.example targets, or the private "
    "registry (SKILLBOX_CLIPBOARD_HOSTS / SKILLBOX_BOX_HEALTH_URL) instead"
)


class OssHygieneTests(unittest.TestCase):
    maxDiff = None

    def test_tracked_tree_has_no_real_fleet_identity(self) -> None:
        self.assertEqual(
            [],
            _fleet_identity_hits(_tracked_files()),
            msg=f"real fleet identity in tracked tree; {_REMEDIATION}",
        )

    def test_the_scanner_exemptions_stay_narrow(self) -> None:
        """Two exemptions exist; both must stay incapable of hiding a real leak.

        An allowlist nobody tests is how a hygiene gate quietly stops working,
        so each exemption is pinned against a near-miss that must still fail.
        """
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary) / "probe.txt"
            rel = str(probe)

            def hits_for(body: str) -> list[str]:
                probe.write_text(body, encoding="utf-8")
                # _fleet_identity_hits resolves against ROOT_DIR, so hand it an
                # absolute path; ROOT_DIR / <abs> is that same absolute path.
                return _fleet_identity_hits([rel])

            # Assembled, never written literally: this file is itself scanned,
            # so a banned form spelled out here would fail the tree-wide sweep.
            exempt_ip = _STRUCTURAL_RANGE_LITERALS[1]
            neighbour_ip = exempt_ip[: exempt_ip.rindex(".") + 1] + "2"
            self.assertEqual([], hits_for(f"bind to {exempt_ip} to prove range membership\n"))
            self.assertEqual(1, len(hits_for(f"host at {neighbour_ip}\n")))

            self.assertEqual([], hits_for(f"depends on {_LEGACY_ID_EXEMPTION}\n"))
            # The same name outside the exempt identifier is still a leak, and
            # is still caught when it shares a line with the exempt identifier.
            bare_name = _LEGACY_ID_EXEMPTION.rsplit("-posture", 1)[0]
            self.assertEqual(1, len(hits_for(f"ssh {bare_name}\n")))
            self.assertEqual(
                1,
                len(hits_for(f"{_LEGACY_ID_EXEMPTION} covers ssh {bare_name}\n")),
            )

    def test_uncommitted_work_in_progress_has_no_real_fleet_identity(self) -> None:
        """Catch a leak in WIP, before the commit that would publish it.

        skillbox-97fz: the 2026-07-30 filter-repo scrub cleaned published history,
        but uncommitted orb-lane WIP still carried a real box address. Scrubbing
        history a second time is far more expensive than failing here.
        """
        self.assertEqual(
            [],
            _fleet_identity_hits(_wip_files()),
            msg=f"real fleet identity in untracked work in progress; {_REMEDIATION}",
        )

    def test_orb_lane_surfaces_carry_no_real_fleet_identity(self) -> None:
        """The orb kit is handed to a remote Orb, so its sources get their own gate.

        Kept separate from the tree-wide sweep so that unrelated drift elsewhere
        can never mask a regression on the surfaces that actually leave the box.
        """
        for rel_path in ORB_LANE_SURFACES:
            with self.subTest(surface=rel_path):
                self.assertTrue((ROOT_DIR / rel_path).is_file(), rel_path)
                self.assertEqual(
                    [],
                    _fleet_identity_hits([rel_path]),
                    msg=f"real fleet identity on an orb-lane surface; {_REMEDIATION}",
                )

    def test_join_tailnet_default_box_endpoint_is_not_hardcoded_fleet_identity(self) -> None:
        """The shipped default must stay a placeholder overridable from the registry.

        A real address here would travel to every Orb inside the kit tarball; a
        placeholder with no env override would instead make ``--resume`` probe an
        address that cannot answer, so both halves are asserted together.
        """
        text = (ROOT_DIR / "scripts" / "orb" / "join-tailnet.sh").read_text(encoding="utf-8")
        self.assertIn("SKILLBOX_BOX_HEALTH_URL", text)
        self.assertEqual([], _fleet_identity_hits(["scripts/orb/join-tailnet.sh"]))

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
