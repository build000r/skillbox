"""Machine-migration relink: old-root -> new-root bulk link rewrite.

A machine move (e.g. a Mac laptop at ``/Users/b/repos`` -> a devbox at
``/srv/skillbox/repos``) is the single biggest drift generator in the skill
estate: every installed skill symlink that pointed at the source tree now points
at a path that does not exist on the new box (the broken-link taxonomy's
``other-machine`` class). ``fleet relink`` first-classes the heal: for each
other-machine link, translate its target onto the destination root; if the
translated target EXISTS and is a valid skill dir, repoint the link in place;
otherwise reclassify (moved/dangling) and leave it for ``fleet converge``.

These tests prove, on purpose-built tmp trees and the shared fixture fleet:

* **rewrite**            — translated target exists + is a skill dir -> ln -sfn.
* **reclassify (missing)**— translated target missing/not a skill dir -> left.
* **partial-overlap roots** — explicit --from-root/--to-root prefix swap.
* **alias roots**        — an alias source path canonicalizes before translating.
* **healthy-link-untouched** — a healthy link is never a relink candidate.
* **dry-run == apply plan** — the plan an apply runs is the plan a dry-run prints.
* **roots default from machines.yaml** — current machine = to-root; others = from.

Cross-machine cases inject a canonical-schema :class:`MachinesConfig` through
``skill_visibility._machines_classifier_override`` (so the taxonomy classifies
the link as other-machine host-independently) AND pass the same config straight
into ``build_relink_plan`` — mirroring ``tests/test_broken_link_taxonomy.py``.

HARD INVARIANT under test: relink NEVER prunes, NEVER touches a healthy link,
and applies ONLY the rewrite actions its plan enumerates.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from runtime_manager import fleet_relink as fr  # noqa: E402
from runtime_manager import machines as m  # noqa: E402
from runtime_manager import skill_visibility as sv  # noqa: E402

from tests.fixture_fleet import build_fixture_fleet  # noqa: E402


# --- helpers ----------------------------------------------------------------


def _config(machines: dict[str, m.MachineProfile], aliases: tuple = ()) -> m.MachinesConfig:
    return m.MachinesConfig(machines=machines, aliases=aliases)


@contextmanager
def _inject_machines(config: m.MachinesConfig | None, machine_id: str | None) -> Iterator[None]:
    """Patch the taxonomy's machines resolution (so links classify as foreign)."""
    sv._machines_classifier_override = lambda: (config, machine_id)  # type: ignore[attr-defined]
    try:
        yield
    finally:
        sv._machines_classifier_override = None  # type: ignore[attr-defined]


def _write_skill(skill_dir: Path, name: str) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: t\n---\n# {name}\n", encoding="utf-8"
    )
    return skill_dir


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    # `.git` *file* so the audit scan-root walk discovers it (mirrors fixture).
    (repo / ".git").write_text("gitdir: ./.realgit\n", encoding="utf-8")
    (repo / ".claude" / "skills").mkdir(parents=True)
    return repo


def _link(link: Path, target: Path | str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(str(target), str(link), target_is_directory=True)


def _model(clients_root: Path, source_roots: list[Path], scan_root: Path) -> dict:
    """A minimal model + on-disk skill-scope.yaml anchoring policy resolution.

    The visibility surface needs ``env.SKILLBOX_CLIENTS_HOST_ROOT`` (the policy
    file resolves next to its parent) and a ``skill-scope.yaml`` declaring the
    source roots (for the moved/relink lookup) and the scan roots (for the fleet
    walk). We keep it tiny: no rules, no categories.
    """
    config_root = clients_root.parent
    scope = config_root / "skill-scope.yaml"
    lines = ["version: 1", "skill_source_roots:"]
    lines += [f"  - {p}" for p in source_roots]
    lines += ["skill_install_scan_roots:", f"  - {scan_root}"]
    scope.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)},
        "active_clients": [],
        "active_profiles": ["core"],
        "clients": [],
        "skills": [],
    }


class _MigrationTree:
    """A two-root migration estate: a foreign 'mac' tree and a local 'devbox' tree.

    Materializes under ``tmp`` a scan root holding one repo whose installed link
    points at ``mac_root/<repo>/.claude/skills/<skill>`` — a foreign target. The
    devbox-side copy of that skill is created (or not) by the test to drive the
    rewrite-vs-reclassify decision.
    """

    def __init__(self, tmp: Path) -> None:
        self.root = tmp
        self.config_root = tmp / "skillbox-config"
        self.clients_root = self.config_root / "clients"
        self.clients_root.mkdir(parents=True)
        # mac (foreign) and devbox (current) repo roots.
        self.mac_root = tmp / "Users" / "b" / "repos"
        self.devbox_root = tmp / "srv" / "skillbox" / "repos"
        self.mac_root.mkdir(parents=True)
        self.devbox_root.mkdir(parents=True)
        # The current-box skill source root (where a moved source would live).
        self.skills_root = tmp / "skills"
        self.skills_root.mkdir(parents=True)
        self.config = _config(
            {
                "mac": m.MachineProfile(
                    machine_id="mac", hostnames=("mac",), repo_roots=(str(self.mac_root),)
                ),
                "devbox": m.MachineProfile(
                    machine_id="devbox",
                    hostnames=("devbox",),
                    repo_roots=(str(self.devbox_root),),
                ),
            }
        )
        self.model = _model(self.clients_root, [self.skills_root], self.devbox_root)

    def add_repo(self, name: str, skill: str) -> tuple[Path, str]:
        """A devbox repo with one installed link pointing at the FOREIGN mac tree.

        Returns ``(repo_path, foreign_target)``. The foreign target is the same
        repo/skill path but rooted at the mac root, so translation maps it back
        onto ``devbox_root/<name>/.claude/skills/<skill>``... but we point the
        link at the mac *source* tree so the translated target is a deterministic
        sibling the test can create or omit.
        """
        repo = _make_repo(self.devbox_root, name)
        foreign_target = str(self.mac_root / "live-skills" / skill)
        _link(repo / ".claude" / "skills" / skill, foreign_target)
        return repo, foreign_target

    def devbox_target_for(self, skill: str) -> Path:
        return self.devbox_root / "live-skills" / skill


# --- root resolution --------------------------------------------------------


class ResolveRootsTests(unittest.TestCase):
    def test_default_roots_from_machines_yaml(self) -> None:
        # to-root = current machine's canonical repo root; from-roots = others'.
        config = _config(
            {
                "mac": m.MachineProfile(
                    machine_id="mac", repo_roots=("/Users/b/repos", "/Users/b/alt")
                ),
                "devbox": m.MachineProfile(
                    machine_id="devbox", repo_roots=("/srv/skillbox/repos",)
                ),
            }
        )
        roots = fr.resolve_relink_roots(config, "devbox")
        self.assertEqual(roots["mode"], "default")
        self.assertEqual(roots["to_root"], "/srv/skillbox/repos")
        self.assertEqual(roots["from_roots"], ["/Users/b/repos", "/Users/b/alt"])
        self.assertIsNone(roots["error"])

    def test_explicit_roots_short_circuit_machine_table(self) -> None:
        roots = fr.resolve_relink_roots(
            None, None, from_root="/old/root", to_root="/new/root"
        )
        self.assertEqual(roots["mode"], "explicit")
        self.assertEqual(roots["from_roots"], ["/old/root"])
        self.assertEqual(roots["to_root"], "/new/root")
        self.assertIsNone(roots["error"])

    def test_one_sided_explicit_root_is_an_error(self) -> None:
        roots = fr.resolve_relink_roots(None, None, from_root="/old/root")
        self.assertIsNotNone(roots["error"])

    def test_default_without_machine_config_is_an_error(self) -> None:
        roots = fr.resolve_relink_roots(None, None)
        self.assertIsNotNone(roots["error"])


# --- per-link decision (fleet plan) -----------------------------------------


class RelinkDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = _MigrationTree(Path(self._tmp.name))

    def _plan(self, **kw):
        with _inject_machines(self.tree.config, "devbox"):
            return fr.build_relink_plan(
                self.tree.model,
                config=self.tree.config,
                machine_id="devbox",
                scan_roots=[str(self.tree.devbox_root)],
                **kw,
            )

    def test_translated_target_exists_is_rewritten(self) -> None:
        # The translated devbox-side target exists + is a valid skill dir.
        repo, _foreign = self.tree.add_repo("alpha", "tiny-ui")
        _write_skill(self.tree.devbox_target_for("tiny-ui"), "tiny-ui")

        plan = self._plan()
        self.assertEqual(plan["summary"]["rewrite"], 1)
        self.assertEqual(plan["summary"]["reclassify"], 0)
        row = {r["path"]: r for r in plan["repos"]}[str(repo)]
        action = row["actions"][0]
        self.assertEqual(action["decision"], "rewrite")
        self.assertEqual(
            action["translated_target"], str(self.tree.devbox_target_for("tiny-ui"))
        )
        self.assertTrue(action["command"].startswith("ln -sfn "))
        self.assertIn(str(self.tree.devbox_target_for("tiny-ui")), action["command"])

    def test_translated_target_missing_is_reclassified(self) -> None:
        # Same foreign link, but the devbox-side target does NOT exist -> the
        # repo moved but the skill is gone: reclassify (dangling), never rewrite.
        repo, _foreign = self.tree.add_repo("beta", "ghost")
        # (intentionally do NOT create the devbox-side target)

        plan = self._plan()
        self.assertEqual(plan["summary"]["rewrite"], 0)
        self.assertEqual(plan["summary"]["reclassify"], 1)
        row = {r["path"]: r for r in plan["repos"]}[str(repo)]
        action = row["actions"][0]
        self.assertEqual(action["decision"], "reclassify")
        self.assertEqual(action["reclassify_as"], "dangling")
        self.assertEqual(action["command"], "")

    def test_translated_target_is_a_plain_dir_not_a_skill_is_reclassified(self) -> None:
        # The translated path exists but lacks SKILL.md -> not a valid skill dir
        # -> reclassify, never rewrite. (Proves the skill-dir validity gate.)
        repo, _foreign = self.tree.add_repo("gamma", "tiny-ui")
        self.tree.devbox_target_for("tiny-ui").mkdir(parents=True)  # no SKILL.md

        plan = self._plan()
        self.assertEqual(plan["summary"]["rewrite"], 0)
        self.assertEqual(plan["summary"]["reclassify"], 1)
        action = plan["repos"][0]["actions"][0]
        self.assertEqual(action["decision"], "reclassify")

    def test_mixed_fleet_counts(self) -> None:
        # One rewriteable + one reclassify across two repos rolls up correctly.
        repo_ok, _ = self.tree.add_repo("alpha", "tiny-ui")
        _write_skill(self.tree.devbox_target_for("tiny-ui"), "tiny-ui")
        repo_bad, _ = self.tree.add_repo("beta", "ghost")  # no target

        plan = self._plan()
        self.assertEqual(plan["summary"]["rewrite"], 1)
        self.assertEqual(plan["summary"]["reclassify"], 1)
        self.assertEqual(plan["summary"]["actions_total"], 2)
        self.assertEqual(plan["summary"]["repos_with_plan"], 2)


# --- explicit roots: partial-overlap + alias --------------------------------


class ExplicitRootRewriteTests(unittest.TestCase):
    """Explicit --from-root/--to-root prefix swap (machine table not required)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_root = self.tmp / "skillbox-config"
        self.clients_root = self.config_root / "clients"
        self.clients_root.mkdir(parents=True)
        self.skills_root = self.tmp / "skills"
        self.skills_root.mkdir(parents=True)

    def test_partial_overlap_roots_rewrite_by_prefix_swap(self) -> None:
        # from-root and to-root share NO common prefix (a true partial overlap):
        # /old/mac/repos -> <tmp>/new/devbox/repos. The link points under the old
        # root; the translated sibling exists -> rewrite.
        old_root = "/old/mac/repos"
        new_root = self.tmp / "new" / "devbox" / "repos"
        new_root.mkdir(parents=True)
        scan_root = new_root
        repo = _make_repo(new_root, "alpha")
        foreign_target = f"{old_root}/live-skills/tiny-ui"
        _link(repo / ".claude" / "skills" / "tiny-ui", foreign_target)
        # The translated target: <new_root>/live-skills/tiny-ui — make it valid.
        _write_skill(new_root / "live-skills" / "tiny-ui", "tiny-ui")

        model = _model(self.clients_root, [self.skills_root], scan_root)
        # No machines config needed; explicit roots drive a literal prefix swap.
        # The taxonomy still needs to see the target as foreign, so inject a
        # config whose 'other' machine owns /old/mac/repos.
        config = _config(
            {
                "other": m.MachineProfile(machine_id="other", repo_roots=(old_root,)),
                "here": m.MachineProfile(machine_id="here", repo_roots=(str(new_root),)),
            }
        )
        with _inject_machines(config, "here"):
            plan = fr.build_relink_plan(
                model,
                from_root=old_root,
                to_root=str(new_root),
                config=config,
                machine_id="here",
                scan_roots=[str(scan_root)],
            )
        self.assertEqual(plan["roots"]["mode"], "explicit")
        self.assertEqual(plan["summary"]["rewrite"], 1)
        action = plan["repos"][0]["actions"][0]
        self.assertEqual(action["decision"], "rewrite")
        self.assertEqual(
            action["translated_target"], str(new_root / "live-skills" / "tiny-ui")
        )

    def test_alias_source_root_canonicalizes_before_translating(self) -> None:
        # The link points under an ALIAS of the old root (/srv/repos), which the
        # machines config canonicalizes to /srv/skillbox/repos before the prefix
        # swap. Explicit from-root is the canonical old root; the alias still
        # translates because canonicalize_alias folds it first.
        canonical_old = "/srv/skillbox/repos"
        alias_old = "/srv/repos"
        new_root = self.tmp / "devbox" / "repos"
        new_root.mkdir(parents=True)
        repo = _make_repo(new_root, "alpha")
        # Link target uses the ALIAS form.
        foreign_target = f"{alias_old}/live-skills/tiny-ui"
        _link(repo / ".claude" / "skills" / "tiny-ui", foreign_target)
        _write_skill(new_root / "live-skills" / "tiny-ui", "tiny-ui")

        model = _model(self.clients_root, [self.skills_root], new_root)
        config = _config(
            {
                "other": m.MachineProfile(machine_id="other", repo_roots=(canonical_old,)),
                "here": m.MachineProfile(machine_id="here", repo_roots=(str(new_root),)),
            },
            aliases=(m.MachineAlias(alias=alias_old, canonical=canonical_old),),
        )
        with _inject_machines(config, "here"):
            plan = fr.build_relink_plan(
                model,
                from_root=canonical_old,
                to_root=str(new_root),
                config=config,
                machine_id="here",
                scan_roots=[str(new_root)],
            )
        self.assertEqual(plan["summary"]["rewrite"], 1)
        action = plan["repos"][0]["actions"][0]
        self.assertEqual(action["decision"], "rewrite")
        self.assertEqual(
            action["translated_target"], str(new_root / "live-skills" / "tiny-ui")
        )


# --- healthy-link-untouched + dry-run == apply (fixture fleet) ---------------


class HealthyAndApplyTests(unittest.TestCase):
    """End-to-end on the shared fixture fleet."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = build_fixture_fleet(self._tmp.name)
        self.mac_root = self.fleet.root / "fake-mac-root"
        self.config = _config(
            {
                "mac-like": m.MachineProfile(
                    machine_id="mac-like", hostnames=("mac-like",), repo_roots=(str(self.mac_root),)
                ),
                "devbox-like": m.MachineProfile(
                    machine_id="devbox-like",
                    hostnames=("devbox-like",),
                    repo_roots=(str(self.fleet.repos_real),),
                ),
            }
        )

    def _plan(self, cwd=None, apply=False):
        # When a cwd is given we exercise the `--cwd scopes to one repo` path, so
        # we must NOT pass scan_roots (explicit scan_roots overrides cwd scoping).
        # The fixture repos carry a `.git` file, so cwd scoping engages.
        scan_roots = None if cwd is not None else [str(self.fleet.aliased_root)]
        with self.fleet._home_patched(), _inject_machines(self.config, "devbox-like"):
            return fr.build_relink_plan(
                self.fleet.model(),
                config=self.config,
                machine_id="devbox-like",
                scan_roots=scan_roots,
                cwd=cwd,
                apply=apply,
            )

    def test_healthy_link_is_never_a_relink_candidate(self) -> None:
        # The 'healthy' repo's link resolves on-box -> it is NOT in broken_project
        # at all, so relink emits zero actions for it. Scope to it via --cwd.
        plan = self._plan(cwd=str(self.fleet.repo("healthy")))
        self.assertEqual(plan["summary"]["candidate_repos"], 1)
        self.assertEqual(plan["summary"]["actions_total"], 0)
        self.assertEqual(plan["repos"], [])  # nothing to report

    def test_dangling_link_is_not_relinked(self) -> None:
        # The 'dangling' repo's link is origin=dangling, NOT other-machine -> not
        # a relink candidate (relink owns only the migration class).
        plan = self._plan(cwd=str(self.fleet.repo("dangling")))
        self.assertEqual(plan["summary"]["actions_total"], 0)

    def test_other_machine_link_with_missing_devbox_target_reclassifies(self) -> None:
        # The fixture's other-machine link -> fake-mac-root/skills/tiny-ui, which
        # translates to repos_real/skills/tiny-ui (absent) -> reclassify, no write.
        plan = self._plan(cwd=str(self.fleet.repo("other-machine")))
        self.assertEqual(plan["summary"]["rewrite"], 0)
        self.assertEqual(plan["summary"]["reclassify"], 1)
        action = plan["repos"][0]["actions"][0]
        self.assertEqual(action["decision"], "reclassify")

    def test_dry_run_plan_equals_apply_plan(self) -> None:
        # The plan an apply runs is byte-for-byte the plan a dry-run prints: the
        # only difference is the dry_run flag. Strip it and compare.
        repo = str(self.fleet.repo("other-machine"))
        dry = self._plan(cwd=repo, apply=False)
        applied = self._plan(cwd=repo, apply=True)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(applied["dry_run"])
        dry_body = {k: v for k, v in dry.items() if k != "dry_run"}
        apply_body = {k: v for k, v in applied.items() if k != "dry_run"}
        self.assertEqual(dry_body, apply_body)


# --- apply writes (tmp tree only) -------------------------------------------


class ApplyWritesTests(unittest.TestCase):
    """``apply_relink_plan`` actually repoints links — tmp tree only, never live."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = _MigrationTree(Path(self._tmp.name))

    def _plan(self, **kw):
        with _inject_machines(self.tree.config, "devbox"):
            return fr.build_relink_plan(
                self.tree.model,
                config=self.tree.config,
                machine_id="devbox",
                scan_roots=[str(self.tree.devbox_root)],
                **kw,
            )

    def test_apply_dry_run_writes_nothing(self) -> None:
        repo, foreign = self.tree.add_repo("alpha", "tiny-ui")
        _write_skill(self.tree.devbox_target_for("tiny-ui"), "tiny-ui")
        link = repo / ".claude" / "skills" / "tiny-ui"

        plan = self._plan(apply=True)
        result = fr.apply_relink_plan(plan, dry_run=True)
        self.assertEqual(result["summary"]["rewritten"], 0)
        # The link still points at the foreign target — nothing was written.
        self.assertEqual(os.readlink(link), foreign)

    def test_apply_rewrites_only_rewrite_actions(self) -> None:
        repo_ok, _ = self.tree.add_repo("alpha", "tiny-ui")
        target = self.tree.devbox_target_for("tiny-ui")
        _write_skill(target, "tiny-ui")
        repo_bad, foreign_bad = self.tree.add_repo("beta", "ghost")  # no target
        link_ok = repo_ok / ".claude" / "skills" / "tiny-ui"
        link_bad = repo_bad / ".claude" / "skills" / "ghost"

        plan = self._plan(apply=True)
        result = fr.apply_relink_plan(plan, dry_run=False)

        self.assertEqual(result["summary"]["rewritten"], 1)
        self.assertEqual(result["summary"]["failed"], 0)
        # The reclassify link is counted as skipped, never executed.
        self.assertEqual(result["summary"]["skipped_reclassify"], 1)
        # The rewriteable link now points at the translated devbox target...
        self.assertEqual(os.readlink(link_ok), str(target))
        self.assertTrue(link_ok.resolve().is_dir())
        # ...and the reclassify link is left exactly as it was (foreign, broken).
        self.assertEqual(os.readlink(link_bad), foreign_bad)

    def test_apply_is_idempotent(self) -> None:
        repo_ok, _ = self.tree.add_repo("alpha", "tiny-ui")
        target = self.tree.devbox_target_for("tiny-ui")
        _write_skill(target, "tiny-ui")
        link_ok = repo_ok / ".claude" / "skills" / "tiny-ui"

        # Apply once.
        fr.apply_relink_plan(self._plan(apply=True), dry_run=False)
        self.assertEqual(os.readlink(link_ok), str(target))
        # The link is now healthy -> a re-plan finds no other-machine links.
        replan = self._plan(apply=True)
        self.assertEqual(replan["summary"]["actions_total"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
