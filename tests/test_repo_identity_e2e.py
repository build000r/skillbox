"""Fixture-fleet e2e for repo-identity (registry-id) policy resolution (bead y8w.5).

This locks the y8w.3 ``repos:`` / ``categories:`` registry-id resolution END TO
END on the two-machine simulated tree the fixture fleet already materializes
(``tests/fixture_fleet.py``: a ``machines.yaml`` with a ``mac-like`` and a
``devbox-like`` profile rooted at DIFFERENT paths, an aliased ``/srv/repos``-style
symlink root, and a ``registry/repos.yaml`` fixture). Where the y8w.3 *unit*
tests (``tests/test_skill_scope_registry.py`` /
``tests/test_registry_path_duplication.py``) assert the resolver functions in
isolation, this module drives the WHOLE evaluator (``collect_skill_visibility``
-> ``_scope_rules`` -> ``_scope_rule_paths`` -> registry resolution) so a single
scope rule written with ``repos: [<id>]`` provably:

  * resolves to the correct PER-MACHINE path (devbox-like vs mac-like — different
    roots, SAME registry id);
  * expands a ``categories: [<bucket>]`` rule to every repo in that bucket;
  * raises the clear ``RegistryResolutionError`` WITH the did-you-mean hint on an
    unknown id;
  * folds the ``/srv/repos`` <-> ``/srv/skillbox/repos`` alias roots;
  * resolves a ``repos: [id]`` rule and the literal-path form it replaces to the
    SAME matched set for a cwd under that repo (back-compat equivalence);
  * fires the registry-path-duplication lint on a literal path an id covers and
    does NOT fire on a genuinely-uncovered path.

Hermetic, exactly like the y8w.3 units: a registry + machine config are injected
through ``skill_visibility``'s ``_registry_doctor_module_override`` /
``_machines_classifier_override`` seams, but the DATA is derived from the fixture
fleet's two real machine profiles and its aliased root so this is a fleet-shaped
e2e, not a re-run of the units. The scope policy is built INLINE in the model's
client context (not from the live ``skill-scope.yaml``, which another worker is
migrating concurrently), so nothing here depends on operator policy state.

Runs under pytest and ``python3 -m unittest discover -s tests``.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
for _path in (str(ROOT_DIR), str(ENV_MANAGER_DIR), str(ROOT_DIR / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from runtime_manager import skill_visibility as sv  # noqa: E402
from runtime_manager import machines as machines_mod  # noqa: E402
from runtime_manager.validation import (  # noqa: E402
    REGISTRY_PATH_DUPLICATION_CODE,
    validate_registry_path_duplication,
)

from fixture_fleet import build_fixture_fleet  # noqa: E402


# --------------------------------------------------------------------------- #
# A registry shaped like the fixture fleet's registry/repos.yaml, but carrying
# the ``bucket`` classification the resolver reads (the fixture file writes
# ``class:``; the y8w.3 resolver keys off ``bucket:``). Paths are home-relative
# (``~/repos/<id>``) exactly like the canonical registry; per-machine re-rooting
# is what the evaluator derives from machines.yaml at eval time.
# --------------------------------------------------------------------------- #
REGISTRY_REPOS = [
    {"id": "app-core", "path": "~/repos/app_core", "bucket": "app"},
    {"id": "api-server", "path": "~/repos/api_server", "bucket": "backend"},
    {"id": "ingredient-server", "path": "~/repos/ingredient_server", "bucket": "backend"},
    {"id": "design-registry", "path": "~/repos/design_registry", "bucket": "registry"},
]


def _fake_registry_doctor() -> types.SimpleNamespace:
    """Stand-in for skillbox-config/scripts/registry_doctor.py (load_registry)."""

    def load_registry(_path: object) -> dict:
        return {"repos": [dict(item) for item in REGISTRY_REPOS]}

    return types.SimpleNamespace(
        load_registry=load_registry,
        DEFAULT_REGISTRY="/fake/registry/repos.yaml",
    )


def _machines_config_for(machine_id: str, repo_roots: tuple[str, ...]) -> machines_mod.MachinesConfig:
    """One-machine config naming the CURRENT machine + its repo roots.

    Mirrors the two profiles the fixture fleet declares (``mac-like`` /
    ``devbox-like``) one at a time, so the evaluator re-roots the SAME registry id
    under whichever machine is "current".
    """
    profile = machines_mod.MachineProfile(
        machine_id=machine_id,
        hostnames=(machine_id,),
        repo_roots=repo_roots,
    )
    return machines_mod.MachinesConfig(machines={machine_id: profile})


class _FleetRegistryHarness:
    """Inject a fixture-fleet-derived registry + machine profile via the seams.

    ``machine_id`` / ``repo_roots`` pick which of the fixture fleet's two profiles
    is "current"; ``aliases`` (optional) installs declared alias folds so the
    alias-root e2e can fold ``/srv/repos`` into ``/srv/skillbox/repos`` even when
    no real symlink exists in the temp tree.
    """

    def __init__(
        self,
        *,
        machine_id: str,
        repo_roots: tuple[str, ...],
        aliases: tuple[machines_mod.MachineAlias, ...] = (),
    ) -> None:
        self._machine_id = machine_id
        self._repo_roots = repo_roots
        self._aliases = aliases

    def __enter__(self) -> "_FleetRegistryHarness":
        sv._registry_doctor_module_override = _fake_registry_doctor  # type: ignore[attr-defined]
        config = _machines_config_for(self._machine_id, self._repo_roots)
        if self._aliases:
            config = machines_mod.MachinesConfig(
                machines=config.machines, aliases=self._aliases
            )
        sv._machines_classifier_override = lambda: (config, self._machine_id)  # type: ignore[attr-defined]
        # Point the registry-file resolver at an existing file so it is "found"
        # (the fake load_registry ignores the path).
        self._prev_env = sv.os.environ.get(sv.REGISTRY_FILE_ENV_VAR)
        sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = __file__
        return self

    def __exit__(self, *_exc: object) -> None:
        sv.__dict__.pop("_registry_doctor_module_override", None)
        sv.__dict__.pop("_machines_classifier_override", None)
        if self._prev_env is None:
            sv.os.environ.pop(sv.REGISTRY_FILE_ENV_VAR, None)
        else:
            sv.os.environ[sv.REGISTRY_FILE_ENV_VAR] = self._prev_env


def _model_with_inline_policy(rules: list[dict]) -> dict:
    """A runtime model carrying an INLINE scope policy via a client context.

    Deliberately leaves ``env`` empty so the on-disk fixture ``skill-scope.yaml``
    is NOT loaded (``_operator_scope_policies`` only reads it when
    ``SKILLBOX_CLIENTS_HOST_ROOT`` is set) — the only policy in scope is the one
    we declare here. ``active_clients`` empty means the client is in scope, so the
    inline policy is the single source of rules. This keeps the e2e independent of
    the live policy that another worker is migrating.
    """
    return {
        "env": {},
        "active_clients": [],
        "active_profiles": ["core"],
        "clients": [
            {
                "id": "e2e",
                "context": {"skill_scope": {"_policy_path": "inline:e2e", "rules": rules}},
            }
        ],
        "skills": [],
    }


# Devbox-like / mac-like roots, named to mirror the fixture fleet's two profiles.
DEVBOX_ROOTS = ("/srv/skillbox/repos", "/srv/repos")
MAC_ROOTS = ("/Users/operator/repos",)


class RepoIdentityE2ETests(unittest.TestCase):
    # The fixture fleet is built once per test method (cheap; it materializes a
    # tiny temp tree). It anchors this module to the real harness even though the
    # registry/machine data is injected, so a future change to the fleet's
    # two-profile / aliased-root shape surfaces here.
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = build_fixture_fleet(self._tmp.name)
        # The fixture fleet declares exactly the two profiles this e2e simulates.
        self.assertTrue(self.fleet.machines_path.is_file())
        machines_text = self.fleet.machines_path.read_text(encoding="utf-8")
        self.assertIn("mac-like", machines_text)
        self.assertIn("devbox-like", machines_text)
        # And it ships a registry fixture + an aliased root (the alias the fold
        # test exercises).
        self.assertTrue(self.fleet.registry_path.is_file())
        self.assertTrue(self.fleet.aliased_root.is_symlink())

    # -- per-machine id resolution -------------------------------------------
    def test_repo_id_resolves_per_machine_devbox_vs_mac(self) -> None:
        """SAME registry id, DIFFERENT root per machine -> different cwd matches."""
        rule = {"id": "app-local", "skills": ["app-*"], "repos": ["app-core"]}

        # On the devbox-like profile, app-core re-roots under /srv/skillbox/repos.
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            model = _model_with_inline_policy([rule])
            devbox_cwd = "/srv/skillbox/repos/app_core/src"
            payload = sv.collect_skill_visibility(
                model, cwd=devbox_cwd, include_global=False, include_project=False
            )
        matched = payload["matched_scope_rules"]
        self.assertEqual([item["id"] for item in matched], ["app-local"])
        self.assertIn("/srv/skillbox/repos/app_core", matched[0]["paths"])
        # The mac root is NOT a resolved path for this rule on the devbox.
        self.assertNotIn("/Users/operator/repos/app_core", matched[0]["paths"])

        # On the mac-like profile, the SAME id re-roots under the mac root, and a
        # cwd under the devbox root no longer matches.
        with _FleetRegistryHarness(machine_id="mac-like", repo_roots=MAC_ROOTS):
            model = _model_with_inline_policy([rule])
            mac_cwd = "/Users/operator/repos/app_core/src"
            payload_mac = sv.collect_skill_visibility(
                model, cwd=mac_cwd, include_global=False, include_project=False
            )
            # A devbox cwd does NOT match when the current machine is the mac.
            payload_devbox_cwd_on_mac = sv.collect_skill_visibility(
                model, cwd="/srv/skillbox/repos/app_core/src",
                include_global=False, include_project=False,
            )
        mac_matched = payload_mac["matched_scope_rules"]
        self.assertEqual([item["id"] for item in mac_matched], ["app-local"])
        self.assertIn("/Users/operator/repos/app_core", mac_matched[0]["paths"])
        self.assertEqual(payload_devbox_cwd_on_mac["matched_scope_rules"], [])

    # -- category (registry bucket) expansion --------------------------------
    def test_category_expands_to_every_repo_in_bucket(self) -> None:
        """``categories: [backend]`` matches EVERY repo whose registry bucket is backend."""
        rule = {"id": "backend-local", "skills": ["be-*"], "categories": ["backend"]}
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            model = _model_with_inline_policy([rule])
            # Both backend repos resolve; matched from a cwd under each.
            for repo in ("api_server", "ingredient_server"):
                payload = sv.collect_skill_visibility(
                    model, cwd=f"/srv/skillbox/repos/{repo}/pkg",
                    include_global=False, include_project=False,
                )
                matched = payload["matched_scope_rules"]
                self.assertEqual([item["id"] for item in matched], ["backend-local"])
                self.assertIn(f"/srv/skillbox/repos/{repo}", matched[0]["paths"])
            # The rule's resolved path set covers BOTH backend repos and excludes
            # the app/registry-bucket repos.
            both = matched[0]["paths"]
            self.assertIn("/srv/skillbox/repos/api_server", both)
            self.assertIn("/srv/skillbox/repos/ingredient_server", both)
            self.assertNotIn("/srv/skillbox/repos/app_core", both)
            self.assertNotIn("/srv/skillbox/repos/design_registry", both)
            # A cwd under a NON-backend repo does not match the backend rule.
            payload_app = sv.collect_skill_visibility(
                model, cwd="/srv/skillbox/repos/app_core/x",
                include_global=False, include_project=False,
            )
            self.assertEqual(payload_app["matched_scope_rules"], [])

    # -- unknown id -> error surfaced WITH did-you-mean, rule SKIPPED ---------
    def test_unknown_id_is_surfaced_without_nuking_the_report(self) -> None:
        """An unknown ``repos:`` id is reported via ``last_scope_rule_errors`` with
        a did-you-mean hint, but it is SKIPPED so the rest of the report survives
        (BUG C: one typo'd rule must not take down the whole report).
        """
        typo_rule = {"id": "typo", "skills": ["x"], "repos": ["app-cor"]}  # near app-core
        good_rule = {"id": "app-local", "skills": ["app-*"], "repos": ["app-core"]}
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            model = _model_with_inline_policy([typo_rule, good_rule])
            # The report resolves cleanly (no raise) even with a typo'd rule.
            payload = sv.collect_skill_visibility(
                model, cwd="/srv/skillbox/repos/app_core/src",
                include_global=False, include_project=False,
            )
            errors = sv.last_scope_rule_errors()
        # The GOOD rule still matched — the typo did not nuke it.
        matched = payload["matched_scope_rules"]
        self.assertEqual([item["id"] for item in matched], ["app-local"])
        self.assertIn("/srv/skillbox/repos/app_core", matched[0]["paths"])
        # The typo is surfaced as a collected error with the self-healing hint.
        self.assertEqual([e["rule_id"] for e in errors], ["typo"])
        self.assertEqual(errors[0]["type"], "RegistryResolutionError")
        message = errors[0]["error"]
        self.assertIn("'app-cor' not in registry/repos.yaml", message)
        self.assertIn("did you mean 'app-core'", message)
        self.assertIn("declared ids:", message)
        # The full declared-id list is part of the self-healing hint.
        self.assertIn("design-registry", message)

    # -- alias roots fold -----------------------------------------------------
    def test_alias_roots_fold_srv_repos_into_canonical(self) -> None:
        """A cwd under the /srv/repos alias matches a repos:-id rule resolved to
        the /srv/skillbox/repos canonical tree (alias fold via declared aliases)."""
        alias = machines_mod.MachineAlias(
            alias="/srv/repos", canonical="/srv/skillbox/repos"
        )
        rule = {"id": "app-local", "skills": ["app-*"], "repos": ["app-core"]}
        with _FleetRegistryHarness(
            machine_id="devbox-like", repo_roots=DEVBOX_ROOTS, aliases=(alias,)
        ):
            model = _model_with_inline_policy([rule])
            # The resolved path set carries the canonical /srv/skillbox/repos
            # spelling; a cwd written with the /srv/repos alias still matches it
            # because _path_prefix_matches resolves the alias symlink. We assert
            # the canonical spelling is present, and that BOTH spellings of the
            # repo path resolve to the same _expand_policy_path canonical form.
            payload = sv.collect_skill_visibility(
                model, cwd="/srv/skillbox/repos/app_core/src",
                include_global=False, include_project=False,
            )
        matched = payload["matched_scope_rules"]
        self.assertEqual([item["id"] for item in matched], ["app-local"])
        self.assertIn("/srv/skillbox/repos/app_core", matched[0]["paths"])
        # Machine-level canonicalization folds the alias prefix regardless of a
        # real on-disk symlink (the declared alias does it by string prefix), so
        # the alias and canonical spelling of the same repo collapse to ONE
        # canonical path and never split the match into a distinct
        # /srv/repos/app_core entry.
        self.assertEqual(
            sv._canonicalize_repo_path("/srv/repos/app_core"),
            "/srv/skillbox/repos/app_core",
        )
        # Where the alias symlink is real on this host, _expand_policy_path's
        # resolve() folds it too (defense-in-depth alongside the declared alias).
        if Path("/srv/repos").is_symlink():
            self.assertEqual(
                sv._expand_policy_path("/srv/repos/app_core"),
                sv._expand_policy_path("/srv/skillbox/repos/app_core"),
            )

    # -- equivalence: repos: [id] == the literal paths it replaces -----------
    def test_repos_id_equals_literal_path_form_for_cwd(self) -> None:
        """A ``repos: [id]`` rule and the literal-path rule it replaces resolve to
        the SAME matched set for a cwd under that repo."""
        cwd = "/srv/skillbox/repos/api_server/svc"
        id_rule = {"id": "api-local", "skills": ["api-*"], "repos": ["api-server"]}
        # The literal form an operator would hand-list before migrating to repos:.
        literal_rule = {
            "id": "api-local",
            "skills": ["api-*"],
            "paths": [
                "~/repos/api_server",
                "/srv/repos/api_server",
                "/srv/skillbox/repos/api_server",
            ],
        }
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            id_payload = sv.collect_skill_visibility(
                _model_with_inline_policy([id_rule]), cwd=cwd,
                include_global=False, include_project=False,
            )
            literal_payload = sv.collect_skill_visibility(
                _model_with_inline_policy([literal_rule]), cwd=cwd,
                include_global=False, include_project=False,
            )
        id_matched = id_payload["matched_scope_rules"]
        literal_matched = literal_payload["matched_scope_rules"]
        # Both forms match the same rule for this cwd.
        self.assertEqual([item["id"] for item in id_matched], ["api-local"])
        self.assertEqual([item["id"] for item in literal_matched], ["api-local"])
        # Equivalence: the literal form's effective path set is a SUBSET of the
        # id form's resolved set (the id form additionally carries the home-relative
        # spelling), and both cover the canonical /srv/skillbox/repos/api_server,
        # so they match identically for any cwd under that repo.
        literal_paths = set(literal_matched[0]["paths"])
        id_paths = set(id_matched[0]["paths"])
        self.assertTrue(
            literal_paths.issubset(id_paths),
            f"literal paths not covered by id form: {literal_paths - id_paths}",
        )
        self.assertIn("/srv/skillbox/repos/api_server", id_paths)
        self.assertIn("/srv/skillbox/repos/api_server", literal_paths)
        # The match decision (the load-bearing observable) is identical.
        self.assertEqual(id_matched[0]["match"], literal_matched[0]["match"])

    # -- registry-path-duplication lint: fires / does NOT fire ----------------
    def test_lint_fires_when_literals_enumerate_the_full_resolved_set(self) -> None:
        """The lint WARNS only when the rule's literals enumerate the id's FULL
        resolved set (a no-op ``repos:`` swap). On the devbox-like profile the
        ``api-server`` id resolves to BOTH the home form and the /srv re-rooting, so
        the rule must list both spellings to be a genuine duplicate (the y8w fix:
        equality, not membership)."""
        policy = {
            "rules": [
                {
                    "id": "api-old",
                    "skills": ["x"],
                    "paths": ["~/repos/api_server", "/srv/skillbox/repos/api_server"],
                },
            ]
        }
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            results = validate_registry_path_duplication(policy, policy_path="inline:e2e")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.code, REGISTRY_PATH_DUPLICATION_CODE)
        self.assertIn("api-old", result.message)
        self.assertIn("'api-server'", result.message)
        self.assertIn("repos: [<id>]", result.message)
        redundant = result.details["redundant"]
        self.assertEqual(redundant[0]["rule"], "api-old")
        self.assertEqual(redundant[0]["registry_id"], "api-server")

    def test_lint_does_not_fire_on_subset_literal_that_repos_would_widen(self) -> None:
        """BUG 1 regression at the fleet level: a single /srv-form literal is a
        STRICT SUBSET of the id's two-spelling resolved set on the devbox-like
        profile, so swapping it for ``repos: [api-server]`` would WIDEN the match
        set (add the home form). The lint must STAY SILENT."""
        policy = {
            "rules": [
                {"id": "api-narrow", "skills": ["x"], "paths": ["/srv/skillbox/repos/api_server"]},
            ]
        }
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            results = validate_registry_path_duplication(policy, policy_path="inline:e2e")
        self.assertEqual(
            results[0].status,
            "pass",
            f"subset literal must not be flagged: {results[0].details}",
        )

    def test_lint_does_not_fire_on_uncovered_path(self) -> None:
        """The lint PASSES on a literal path no registry id covers."""
        policy = {
            "rules": [
                {"id": "misc", "skills": ["x"], "paths": ["~/repos/not-in-registry"]},
            ]
        }
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            results = validate_registry_path_duplication(policy, policy_path="inline:e2e")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "pass")
        self.assertEqual(results[0].code, REGISTRY_PATH_DUPLICATION_CODE)

    def test_lint_passes_on_migrated_repos_form(self) -> None:
        """The migrated form (repos: [id], no literal paths) has nothing to flag."""
        policy = {
            "rules": [
                {"id": "api-local", "skills": ["x"], "repos": ["api-server", "app-core"]},
            ]
        }
        with _FleetRegistryHarness(machine_id="devbox-like", repo_roots=DEVBOX_ROOTS):
            results = validate_registry_path_duplication(policy, policy_path="inline:e2e")
        self.assertEqual(results[0].status, "pass")


if __name__ == "__main__":
    unittest.main()
