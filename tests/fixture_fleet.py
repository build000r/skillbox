"""Fixture-fleet factory: a miniature skill estate for provable visibility tests.

Why this exists
===============

Every Skillbox "visibility" surface -- ``sbp skills`` / ``sbp recalibrate``
(``collect_skill_visibility``), ``sbp recalibrate --fleet`` / ``sbp skills
audit`` (``collect_skill_audit``), and ``sbp skill add/sync/prune``
(``skill_lifecycle_plan`` + ``apply_skill_lifecycle_plan``) -- ultimately reads
a *model* dict plus the real filesystem (skill source roots, installed
``.claude/skills`` / ``.codex/skills`` symlinks, and a layered ``skill-scope.yaml``
policy resolved relative to ``env.SKILLBOX_CLIENTS_HOST_ROOT``).

Asserting against the live operator estate is non-deterministic and slow. This
module miniaturizes that estate into a self-contained temp tree so any future
change to a visibility surface can be *proved* in a single ``pytest`` run.

What the factory builds
=======================

``build_fixture_fleet(tmp_root)`` materializes, under ``tmp_root``:

* **Two machine profiles** (``mac-like`` and ``devbox-like``) plus a
  ``machines.yaml`` describing them. ``machines.yaml`` is an estate-shape
  artifact (it mirrors the operator concept of "which box am I on"); the
  runtime surfaces do not parse it, but it lets fleet tests reason about
  cross-machine link targets (e.g. a ``/fake-mac-root/...`` link seen from the
  devbox).
* **Two homes** -- an OS-style home and a managed home -- exercising *both*
  install-link variants the scanner must tolerate:
    - ``os_home``: a per-entry-symlink home (``~/.claude/skills/<name>`` is a
      symlink to a source skill dir), and
    - ``managed_home``: a dir-symlink home (``~/.claude/skills`` itself is a
      symlink to a managed skills directory).
* **Skill source roots**: a mini ``skills/`` and ``private-skills/`` carrying
  4 tiny skills, one of which (``needs-beads``) declares
  ``requires_beads: true`` frontmatter.
* **The ``_shared`` symlink chain**: ``private-skills/_shared`` -> the shared
  payload under ``skills/_shared_payload`` (mirrors the real cross-root
  ``_shared`` link skills depend on).
* **A ``registry/repos.yaml`` and a ``skill-scope.yaml``** policy fixture
  (``skill_source_roots``, ``skill_install_scan_roots``, ``project_categories``,
  ``rules``, ``global_allowlist``, one ``overlay``-gated rule).
* **Four fake repos** under an aliased root (``/srv/repos`` style: ``aliased``
  is a symlink dir to ``repos_real``) exhibiting:
    1. ``healthy``     -- a healthy per-entry skill link (resolves on-box),
    2. ``other-machine`` -- a link whose target is ``/fake-mac-root/...``
       (valid on the mac, dangling here -> simulates a cross-machine link),
    3. ``dangling``    -- a link to a deleted source (broken link),
    4. ``overlay-repo`` -- a frontend repo with an overlay-gated skill that is
       only "expected" when the marketing overlay is active.

Helpers
=======

The three helpers return *parsed* runtime payloads (already dicts, since the
runtime functions return dicts -- "parsed JSON" in the bead's words):

* ``fleet.run_resolution(cwd)`` -> ``collect_skill_visibility`` payload for a cwd.
* ``fleet.run_audit()``         -> ``collect_skill_audit`` payload (fleet scan).
* ``fleet.apply_plan(...)``     -> applied ``skill_lifecycle_plan`` result.

Every helper runs with ``Path.home()`` patched to the fixture's OS home so the
global ``~/.claude/skills`` / ``~/.codex/skills`` roots resolve *inside* the
temp tree, never against the operator's real home.

How a future worker invokes it
==============================

    from tests.fixture_fleet import build_fixture_fleet

    def test_something(tmp_path):
        fleet = build_fixture_fleet(tmp_path)
        payload = fleet.run_resolution(fleet.repo("overlay-repo"))
        assert payload["matched_scope_rules"]  # ... lock current behavior

Or, via the pytest fixture exported from ``conftest.py``:

    def test_something(fixture_fleet):
        payload = fixture_fleet.run_resolution(fixture_fleet.repo("healthy"))

This is a baseline-capture harness: it calls *unchanged* runtime code. Do not
bake assumptions about "correct" behavior into the factory -- assert against it
in the golden test instead, so the golden is what locks behavior.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = REPO_ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_visibility  # noqa: E402


# --- tiny skill payloads -----------------------------------------------------

def _skill_md(name: str, *, requires_beads: bool = False, overlay: str | None = None) -> str:
    front = ["---", f"name: {name}", f"description: Tiny fixture skill {name}."]
    if requires_beads:
        front.append("requires_beads: true")
    if overlay:
        front.append(f"overlay: {overlay}")
    front.append("---")
    body = f"# {name}\n\nFixture skill body for {name}.\n"
    return "\n".join(front) + "\n\n" + body


def _write_skill(skill_dir: Path, name: str, **frontmatter: Any) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_md(name, **frontmatter), encoding="utf-8")
    return skill_dir


def _relink(link: Path, target: Path | str, *, target_is_directory: bool = True) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(str(target), str(link), target_is_directory=target_is_directory)


@dataclass
class FixtureFleet:
    """A materialized miniature skill estate plus runtime helpers.

    Attributes mirror the directories the factory created so tests can point
    the helpers at concrete paths without recomputing layout.
    """

    root: Path
    config_root: Path
    clients_root: Path
    skill_scope_path: Path
    repos_real: Path
    aliased_root: Path
    os_home: Path
    managed_home: Path
    skills_root: Path
    skills_private_root: Path
    machines_path: Path
    registry_path: Path
    repos: dict[str, Path] = field(default_factory=dict)
    skills: dict[str, Path] = field(default_factory=dict)

    # -- model -------------------------------------------------------------
    def model(self, *, active_clients: list[str] | None = None) -> dict[str, Any]:
        """Return the runtime ``model`` dict that points at this fixture estate.

        The only field the visibility surfaces strictly need is
        ``env.SKILLBOX_CLIENTS_HOST_ROOT`` (the policy file is resolved next to
        its parent). ``clients``/``skills`` are empty so behavior is driven by
        the on-disk ``skill-scope.yaml`` and installed links, not by declared
        skill-repo-sets.
        """
        return {
            "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(self.clients_root)},
            "active_clients": active_clients or [],
            "active_profiles": ["core"],
            "clients": [],
            "skills": [],
        }

    def repo(self, name: str) -> Path:
        return self.repos[name]

    def skill(self, name: str) -> Path:
        return self.skills[name]

    @contextmanager
    def _home_patched(self) -> Iterator[None]:
        """Resolve ``Path.home()`` to the fixture OS home for the duration.

        Both ``collect_skill_visibility`` (global ``~/.claude/skills`` roots)
        and ``collect_skill_audit`` (global drift row) read ``Path.home()``;
        patch it so they never touch the operator's real home.
        """
        with mock.patch.object(
            skill_visibility.Path, "home", return_value=self.os_home
        ):
            yield

    # -- helpers (return parsed payloads) ----------------------------------
    def run_resolution(
        self,
        cwd: str | os.PathLike[str],
        *,
        include_global: bool = True,
        include_project: bool = True,
        include_sources: bool = False,
        active_clients: list[str] | None = None,
    ) -> dict[str, Any]:
        """``sbp recalibrate`` / ``sbp skills`` resolution surface for ``cwd``."""
        with self._home_patched():
            return skill_visibility.collect_skill_visibility(
                self.model(active_clients=active_clients),
                cwd=str(cwd),
                include_global=include_global,
                include_project=include_project,
                include_sources=include_sources,
            )

    def run_audit(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        scan_roots: list[str] | None = None,
        max_depth: int = 3,
        include_clean: bool = False,
        active_clients: list[str] | None = None,
    ) -> dict[str, Any]:
        """``sbp recalibrate --fleet`` cross-repo audit surface."""
        if scan_roots is None:
            scan_roots = [str(self.aliased_root)]
        with self._home_patched():
            return skill_visibility.collect_skill_audit(
                self.model(active_clients=active_clients),
                cwd=str(cwd) if cwd is not None else None,
                scan_roots=scan_roots,
                max_depth=max_depth,
                include_clean=include_clean,
            )

    def apply_plan(
        self,
        action: str,
        *,
        skill_name: str,
        cwd: str | os.PathLike[str],
        to: str = "project",
        categories: list[str] | None = None,
        source: str | os.PathLike[str] | None = None,
        dry_run: bool = True,
        active_clients: list[str] | None = None,
    ) -> dict[str, Any]:
        """``sbp skill add/activate/prune`` lifecycle plan, then apply it.

        Defaults to ``dry_run=True`` so callers opt in to real symlink writes.
        Returns the applied-plan result dict.
        """
        with self._home_patched():
            plan = skill_visibility.skill_lifecycle_plan(
                self.model(active_clients=active_clients),
                action,
                skill_name=skill_name,
                cwd=str(cwd),
                to=to,
                categories=categories,
                source=str(source) if source is not None else None,
            )
            return skill_visibility.apply_skill_lifecycle_plan(plan, dry_run=dry_run)


def _write_machines_yaml(path: Path, *, mac_root: Path, devbox_root: Path) -> None:
    path.write_text(
        "version: 1\n"
        "machines:\n"
        "  - id: mac-like\n"
        "    role: laptop\n"
        f"    repos_root: {mac_root}\n"
        "    home_style: per-entry-symlink\n"
        "  - id: devbox-like\n"
        "    role: devbox\n"
        f"    repos_root: {devbox_root}\n"
        "    home_style: dir-symlink\n"
        "    public_ip: true\n",
        encoding="utf-8",
    )


def _write_registry_repos_yaml(path: Path, *, scan_root: Path, repos: dict[str, Path]) -> None:
    lines = [
        "schema_version: 1",
        "metadata:",
        "  owner: fixture-fleet",
        "scan:",
        "  roots:",
        f"    - {scan_root}",
        "  max_depth: 3",
        "repos:",
    ]
    classes = {
        "healthy": "backend",
        "other-machine": "backend",
        "dangling": "backend",
        "overlay-repo": "frontend",
    }
    for name, repo_path in repos.items():
        lines.append(f"  - path: {repo_path}")
        lines.append(f"    name: {name}")
        lines.append(f"    class: {classes.get(name, 'backend')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_skill_scope_yaml(
    path: Path,
    *,
    skills_root: Path,
    skills_private_root: Path,
    scan_root: Path,
    frontend_repo: Path,
    cli_repo: Path,
) -> None:
    path.write_text(
        "version: 1\n"
        "global_allowlist: [always-global]\n"
        "skill_source_roots:\n"
        f"  - {skills_root}\n"
        f"  - {skills_private_root}\n"
        "skill_install_scan_roots:\n"
        f"  - {scan_root}\n"
        "project_categories:\n"
        "  frontend:\n"
        f"    paths: [{frontend_repo}]\n"
        "  cli:\n"
        f"    paths: [{cli_repo}]\n"
        "rules:\n"
        "  - id: frontend-local\n"
        "    skills: [tiny-ui]\n"
        "    categories: [frontend]\n"
        "  - id: cli-local\n"
        "    skills: [tiny-cli]\n"
        "    categories: [cli]\n"
        "  - id: marketing-overlay-local\n"
        "    skills: [tiny-marketing]\n"
        "    categories: [frontend]\n"
        "    overlay: marketing\n"
        "    default: off\n"
        "    activation: on-demand\n",
        encoding="utf-8",
    )


def build_fixture_fleet(tmp_root: str | os.PathLike[str]) -> FixtureFleet:
    """Materialize the miniature skill estate under ``tmp_root``.

    ``tmp_root`` is typically a pytest ``tmp_path``. Returns a
    :class:`FixtureFleet` whose ``run_resolution`` / ``run_audit`` /
    ``apply_plan`` helpers exercise the *unchanged* runtime surfaces.
    """
    root = Path(tmp_root).resolve()

    # --- config root + clients root (anchors policy resolution) -----------
    config_root = root / "skillbox-config"
    clients_root = config_root / "clients"
    clients_root.mkdir(parents=True)
    registry_dir = config_root / "registry"
    registry_dir.mkdir(parents=True)

    # --- skill source roots: mini skills/ + private-skills/ ---------------
    skills_root = root / "skills"
    skills_private_root = root / "private-skills"
    skills_root.mkdir(parents=True)
    skills_private_root.mkdir(parents=True)

    skills: dict[str, Path] = {}
    skills["tiny-ui"] = _write_skill(skills_root / "tiny-ui", "tiny-ui")
    skills["tiny-cli"] = _write_skill(skills_root / "tiny-cli", "tiny-cli")
    # One skill carrying requires_beads frontmatter (private root).
    skills["needs-beads"] = _write_skill(
        skills_private_root / "needs-beads", "needs-beads", requires_beads=True
    )
    # Overlay-gated marketing skill.
    skills["tiny-marketing"] = _write_skill(
        skills_private_root / "tiny-marketing", "tiny-marketing", overlay="marketing"
    )

    # --- _shared symlink chain --------------------------------------------
    # Real estate: skills depend on a cross-root `_shared` payload reached via
    # a symlink. Model it as private-skills/_shared -> skills/_shared_payload.
    shared_payload = skills_root / "_shared_payload"
    shared_payload.mkdir(parents=True)
    (shared_payload / "README.md").write_text("# shared payload\n", encoding="utf-8")
    shared_link = skills_private_root / "_shared"
    _relink(shared_link, shared_payload, target_is_directory=True)

    # --- machine profiles + machines.yaml ---------------------------------
    mac_root = root / "fake-mac-root"
    devbox_root = root / "srv" / "repos"  # devbox-like layout
    mac_root.mkdir(parents=True)
    devbox_root.mkdir(parents=True)
    machines_path = config_root / "machines.yaml"
    _write_machines_yaml(machines_path, mac_root=mac_root, devbox_root=devbox_root)

    # --- two homes: OS (per-entry) + managed (dir-symlink) ----------------
    os_home = root / "home"
    os_claude_skills = os_home / ".claude" / "skills"
    os_codex_skills = os_home / ".codex" / "skills"
    os_claude_skills.mkdir(parents=True)
    os_codex_skills.mkdir(parents=True)
    # Per-entry-symlink variant: link a single allowlisted-style skill globally.
    _relink(os_claude_skills / "tiny-ui", skills["tiny-ui"])

    # Managed home: the skills dir *itself* is a symlink (dir-symlink variant).
    managed_home = root / "managed-home"
    managed_skills_payload = root / "managed-skills" / "claude"
    managed_skills_payload.mkdir(parents=True)
    _relink(managed_skills_payload / "tiny-cli", skills["tiny-cli"])
    (managed_home / ".claude").mkdir(parents=True)
    _relink(managed_home / ".claude" / "skills", managed_skills_payload)

    # --- aliased repos root: /srv/repos style symlink dir -----------------
    repos_real = root / "repos_real"
    repos_real.mkdir(parents=True)
    aliased_root = root / "aliased"  # e.g. /srv/repos -> /srv/skillbox/repos
    _relink(aliased_root, repos_real)

    repos: dict[str, Path] = {}

    def _make_repo(name: str) -> Path:
        repo_path = repos_real / name
        repo_path.mkdir(parents=True)
        # Use a ``.git`` *file* (worktree-style) so the fleet audit's scan-root
        # walk (``_git_repo_paths_under``) actually discovers the repo. A ``.git``
        # *directory* is pruned by the scanner's skip-dirs before the membership
        # check, so a dir-form repo only ever enters the audit via a category
        # pin -- capturing both shapes here keeps the fixture honest.
        (repo_path / ".git").write_text("gitdir: ./.realgit\n", encoding="utf-8")
        for surface in ("claude", "codex"):
            (repo_path / f".{surface}" / "skills").mkdir(parents=True)
        repos[name] = repo_path
        return repo_path

    # 1) healthy: per-entry link that resolves on-box.
    healthy = _make_repo("healthy")
    _relink(healthy / ".claude" / "skills" / "tiny-cli", skills["tiny-cli"])
    override_dir = healthy / ".skillbox"
    override_dir.mkdir()
    (override_dir / "skill-overrides.yaml").write_text(
        "version: 1\n"
        "pin_on: [needs-beads]\n"
        "pin_off: [tiny-marketing]\n"
        "opt_out_global: [project-status-mmdx]\n"
        "overlays:\n"
        "  enable: [marketing]\n"
        "  disable: [swarm]\n"
        "defaults: [tiny-ui]\n"
        "reason: fixture override\n",
        encoding="utf-8",
    )

    # 2) other-machine: link target lives under /fake-mac-root (valid on the
    #    mac, dangling here -> simulates a cross-machine link).
    other_machine = _make_repo("other-machine")
    other_machine_target = mac_root / "skills" / "tiny-ui"
    _relink(
        other_machine / ".claude" / "skills" / "tiny-ui",
        other_machine_target,
    )  # target does not exist here -> broken/cross-machine

    # 3) dangling: link to a now-deleted source.
    dangling = _make_repo("dangling")
    deleted_source = root / "deleted-source"
    deleted_source.mkdir()
    (deleted_source / "SKILL.md").write_text(_skill_md("ghost"), encoding="utf-8")
    _relink(dangling / ".claude" / "skills" / "ghost", deleted_source)
    # Remove the source so the link dangles.
    (deleted_source / "SKILL.md").unlink()
    deleted_source.rmdir()

    # 4) overlay-repo: frontend repo with an overlay-gated skill linked in.
    overlay_repo = _make_repo("overlay-repo")
    _relink(
        overlay_repo / ".claude" / "skills" / "tiny-marketing",
        skills["tiny-marketing"],
    )
    overlay_override_dir = overlay_repo / ".skillbox"
    overlay_override_dir.mkdir()
    (overlay_override_dir / "skill-overrides.yaml").write_text(
        "version: 1\n"
        "pin_off: [tiny-cli]\n"
        "reason: fixture overlay override\n",
        encoding="utf-8",
    )

    # --- skill-scope.yaml policy fixture ----------------------------------
    skill_scope_path = config_root / "skill-scope.yaml"
    _write_skill_scope_yaml(
        skill_scope_path,
        skills_root=skills_root,
        skills_private_root=skills_private_root,
        scan_root=aliased_root,
        frontend_repo=overlay_repo,
        cli_repo=healthy,
    )

    # --- registry/repos.yaml fixture --------------------------------------
    registry_path = registry_dir / "repos.yaml"
    _write_registry_repos_yaml(registry_path, scan_root=aliased_root, repos=repos)

    return FixtureFleet(
        root=root,
        config_root=config_root,
        clients_root=clients_root,
        skill_scope_path=skill_scope_path,
        repos_real=repos_real,
        aliased_root=aliased_root,
        os_home=os_home,
        managed_home=managed_home,
        skills_root=skills_root,
        skills_private_root=skills_private_root,
        machines_path=machines_path,
        registry_path=registry_path,
        repos=repos,
        skills=skills,
    )
