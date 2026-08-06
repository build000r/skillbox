"""Contract tests for ``runtime_manager.environment_inventory``.

Covers the five properties the contract promises, each as an assertion rather
than a comment:

* it is **versioned** and shaped as documented (schema fixture / golden),
* **declared intent and observed presence are separate** (and observation is
  opt-in),
* **repo ids are stable and machine-independent** -- four different spellings of
  one repo collapse to one id, and that id does not change with the current
  machine profile (the machine-specific-absolute-path trap),
* it **carries no secrets** -- credentials fed through both an ignored field and
  an allowlisted field are proven absent from the serialized output,
* it **stays off picker hot paths** -- probe accounting, a source-level scan ban,
  and a cache read that touches exactly one file.

Every case loads synthetic fixtures from ``tests/fixtures/environment_inventory``
(all paths under ``/synthetic/...``) and injects an explicit clock, machine id,
path variables and presence probe, so nothing reads this box's real config,
hostname, home directory, or filesystem.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
FIXTURES = ROOT_DIR / "tests" / "fixtures" / "environment_inventory"

if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

try:  # PyYAML is required to parse the fixture configs; skip cleanly if absent.
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

from runtime_manager import environment_inventory as ei
from runtime_manager import machines as machines_mod


MACHINES_FIXTURE = FIXTURES / "machines.yaml"
REGISTRY_FIXTURE = FIXTURES / "registry.repos.yaml"
CLIENTS_FIXTURE = FIXTURES / "clients"
GOLDEN_FIXTURE = FIXTURES / "golden_declared.json"

# ``${CLIENT_ROOT}`` in the acme overlay resolves onto beta's canonical repo
# root, so the overlay spelling and the registry spelling name the same repo.
PATH_VARS = {"CLIENT_ROOT": "/synthetic/beta/repos"}

# Literal secrets planted in the fixtures. None of these may survive into the
# serialized contract, whichever field they entered through.
PLANTED_SECRETS = (
    "s3cr3t-token-value",          # credential inside a git remote URL
    "hunter2-must-never-appear",   # ignored overlay field (client.human_operator)
    "acme-deploy-secret-value",    # ignored overlay field (client.skills[])
    "acme-check-secret-value",     # ignored top-level overlay section (checks[])
    "bravo-label-secret-value",    # ALLOWLISTED field (client.label)
)

# The fields Swimmers parses privately today in src/session/overlay.rs. The
# contract claims to supersede each one; ``test_supersedes_*`` proves the claim.
SWIMMERS_PARSED_FIELDS = (
    "client.id",
    "client.label",
    "client.repos[].id",
    "client.repos[].kind",
    "client.repos[].repo_path",
    "context.cwd_match",
    "context.plans.plan_root",
    "context.plans.plan_draft",
    "context.repo_landscape.scan_roots",
    "context.repo_landscape.repos[].path",
)


class CountingProbe:
    """Presence probe that records every path it is asked about."""

    def __init__(self, present: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._present = present or set()

    def __call__(self, path: str) -> dict[str, object]:
        self.calls.append(path)
        if path in self._present:
            return {"present": True, "kind": "dir"}
        return {"present": False, "kind": "missing"}


def build(**overrides):
    """Build a payload from the fixtures with every ambient input pinned."""
    kwargs = dict(
        machines_path=str(MACHINES_FIXTURE),
        registry_path=str(REGISTRY_FIXTURE),
        clients_dir=str(CLIENTS_FIXTURE),
        env={},
        path_vars=PATH_VARS,
        machine_id="beta-devbox",
        now=1_700_000_000.0,
        ttl_s=300.0,
    )
    kwargs.update(overrides)
    return ei.build_environment_inventory(**kwargs)


def normalize_for_golden(payload):
    """Strip legitimately machine-specific/volatile facts before golden compare.

    ``sources[].path`` and ``sources[].mtime`` depend on where the checkout
    lives and when it was written. Keeping them out of the golden is what stops
    this fixture from re-acquiring the machine-specific-absolute-path problem
    that other goldens in this repo already have.
    """
    normalized = json.loads(json.dumps(payload))
    normalized["sources"] = [
        {"kind": item["kind"], "client_id": item["client_id"], "present": item["present"]}
        for item in normalized["sources"]
    ]
    return normalized


def resolve_contract_path(payload, dotted: str):
    """Resolve a ``a.b[].c`` contract path, returning the list of leaf values."""
    current = [payload]
    for part in dotted.split("."):
        expand = part.endswith("[]")
        key = part[:-2] if expand else part
        nxt = []
        for node in current:
            if not isinstance(node, dict) or key not in node:
                return None
            value = node[key]
            if expand:
                if not isinstance(value, list):
                    return None
                nxt.extend(value)
            else:
                nxt.append(value)
        current = nxt
    return current


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class VersionAndShapeTests(unittest.TestCase):
    def test_schema_version_and_contract_name(self):
        payload = build()
        self.assertEqual(
            payload["schema_version"], "2026-07-25+environment_inventory.v1"
        )
        self.assertEqual(
            payload["schema_version"], ei.ENVIRONMENT_INVENTORY_SCHEMA_VERSION
        )
        self.assertEqual(payload["contract"], "skillbox.environment_inventory")

    def test_top_level_shape(self):
        payload = build()
        self.assertEqual(
            sorted(payload),
            [
                "clients",
                "contract",
                "freshness",
                "generated_at",
                "machine",
                "readiness",
                "recovery",
                "redaction",
                "repos",
                "schema_version",
                "sources",
                "supersedes",
            ],
        )

    def test_declared_key_sets_match_the_allowlists(self):
        payload = build()
        self.assertEqual(
            sorted(payload["machine"]["declared"]), sorted(ei.MACHINE_DECLARED_FIELDS)
        )
        for client in payload["clients"]:
            self.assertEqual(
                sorted(client["declared"]), sorted(ei.CLIENT_DECLARED_FIELDS)
            )
        for repo in payload["repos"]:
            self.assertEqual(sorted(repo["declared"]), sorted(ei.REPO_DECLARED_FIELDS))

    def test_golden_fixture(self):
        self.assertTrue(
            GOLDEN_FIXTURE.is_file(), f"missing golden fixture {GOLDEN_FIXTURE}"
        )
        expected = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(normalize_for_golden(build()), expected)

    def test_golden_fixture_has_no_absolute_host_paths(self):
        """The golden must not bake in this checkout's location (repo-known trap)."""
        text = GOLDEN_FIXTURE.read_text(encoding="utf-8")
        self.assertNotIn(str(ROOT_DIR), text)
        self.assertNotIn(str(Path.home()), text)
        for value in json.loads(text)["sources"]:
            self.assertNotIn("path", value)


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class DeclaredVersusObservedTests(unittest.TestCase):
    def test_observed_is_null_without_opt_in(self):
        payload = build()
        self.assertFalse(payload["freshness"]["observed"])
        self.assertIsNone(payload["machine"]["observed"])
        for repo in payload["repos"]:
            self.assertIsNone(repo["observed"])
        for client in payload["clients"]:
            self.assertIsNone(client["observed"])

    def test_observed_block_appears_on_opt_in(self):
        probe = CountingProbe(present={"/synthetic/beta/repos/widget"})
        payload = build(observe=True, probe=probe)
        self.assertTrue(payload["freshness"]["observed"])
        observed = {repo["repo_id"]: repo["observed"] for repo in payload["repos"]}
        self.assertTrue(all(isinstance(item, dict) for item in observed.values()))
        present = [item for item in observed.values() if item["present"]]
        self.assertEqual(len(present), 1)
        self.assertEqual(present[0]["path"], "/synthetic/beta/repos/widget")
        self.assertEqual(present[0]["kind"], "dir")

    def test_declared_block_is_identical_with_and_without_observation(self):
        declared_only = {
            repo["repo_id"]: repo["declared"] for repo in build()["repos"]
        }
        observed_too = {
            repo["repo_id"]: repo["declared"]
            for repo in build(observe=True, probe=CountingProbe())["repos"]
        }
        self.assertEqual(declared_only, observed_too)

    def test_observation_translates_roots_onto_the_current_machine(self):
        """``~/repos/widget`` observes under the CURRENT machine's canonical root."""
        alpha = build(machine_id="alpha-laptop", observe=True, probe=CountingProbe())
        beta = build(machine_id="beta-devbox", observe=True, probe=CountingProbe())

        def path_for(payload, registry_id):
            for repo in payload["repos"]:
                if repo["declared"]["registry_id"] == registry_id:
                    return repo["observed"]["path"]
            raise AssertionError(f"{registry_id} not in payload")

        self.assertEqual(path_for(alpha, "widget"), "/synthetic/alpha-home/repos/widget")
        self.assertEqual(path_for(beta, "widget"), "/synthetic/beta/repos/widget")


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class StableRepoIdTests(unittest.TestCase):
    def setUp(self):
        self.config = machines_mod.load_machines_config(str(MACHINES_FIXTURE))

    def test_four_spellings_of_one_repo_share_one_id(self):
        spellings = (
            "~/repos/widget",                       # registry / machine-agnostic
            "/synthetic/alpha-home/repos/widget",   # alpha absolute
            "/synthetic/beta/repos/widget",         # beta absolute
            "/synthetic/beta/repos-alias/widget",   # beta symlink alias
            "${CLIENT_ROOT}/widget",                # overlay ${VAR} spelling
        )
        ids = {
            ei.stable_repo_id(spelling, config=self.config, path_vars=PATH_VARS)
            for spelling in spellings
        }
        self.assertEqual(len(ids), 1, f"spellings disagreed: {ids}")

    def test_id_does_not_depend_on_the_current_machine(self):
        alpha = {repo["repo_id"] for repo in build(machine_id="alpha-laptop")["repos"]}
        beta = {repo["repo_id"] for repo in build(machine_id="beta-devbox")["repos"]}
        none_detected = {repo["repo_id"] for repo in build(machine_id=None)["repos"]}
        self.assertEqual(alpha, beta)
        self.assertEqual(alpha, none_detected)

    def test_ids_are_derived_from_relative_paths_not_absolute_ones(self):
        """Moving a machine's roots must not renumber its repos."""
        relocated = machines_mod.load_machines_config(str(MACHINES_FIXTURE))
        moved = ei.stable_repo_id(
            "/synthetic/beta/repos/widget", config=relocated, path_vars=PATH_VARS
        )
        anchored = ei.stable_repo_id(
            "~/repos/widget", config=relocated, path_vars=PATH_VARS
        )
        self.assertEqual(moved, anchored)
        self.assertTrue(moved.startswith("sha256:"))
        for repo in build()["repos"]:
            self.assertNotIn("/", repo["repo_id"].split(":", 1)[1])
            self.assertNotIn("synthetic", repo["repo_id"])

    def test_unregistered_repo_still_gets_a_stable_id(self):
        """``sprocket`` exists only in the overlay, written through the alias root."""
        sprocket = [
            repo
            for repo in build()["repos"]
            if repo["declared"]["registry_id"] == "sprocket"
        ]
        self.assertEqual(len(sprocket), 1)
        self.assertEqual(sprocket[0]["declared"]["path_relative"], "sprocket")
        self.assertEqual(sprocket[0]["declared"]["root_category"], "repos")
        self.assertEqual(
            sprocket[0]["repo_id"],
            ei.stable_repo_id(
                "~/repos/sprocket", config=self.config, path_vars=PATH_VARS
            ),
        )


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class MergeAndProvenanceTests(unittest.TestCase):
    def repo_by_registry_id(self, payload, registry_id):
        matches = [
            repo
            for repo in payload["repos"]
            if repo["declared"]["registry_id"] == registry_id
        ]
        self.assertEqual(len(matches), 1, f"expected exactly one {registry_id}")
        return matches[0]

    def test_registry_and_overlay_declarations_merge_into_one_record(self):
        payload = build()
        widget = self.repo_by_registry_id(payload, "widget")
        self.assertEqual(
            widget["declared"]["declared_by"], ["client:acme.repos", "registry"]
        )
        self.assertEqual(widget["declared"]["clients"], ["acme"])
        self.assertEqual(widget["declared"]["bucket"], "app")
        self.assertEqual(widget["declared"]["kind"], "repo")

    def test_repo_landscape_entries_merge_with_the_registry(self):
        gadget = self.repo_by_registry_id(build(), "gadget")
        self.assertEqual(
            gadget["declared"]["declared_by"],
            ["client:acme.repo_landscape", "registry"],
        )

    def test_client_projection(self):
        payload = build()
        acme = [item for item in payload["clients"] if item["client_id"] == "acme"][0]
        self.assertEqual(acme["declared"]["label"], "Acme Co")
        self.assertEqual(acme["declared"]["plan_root"], "plans/released")
        self.assertEqual(acme["declared"]["plan_draft"], "plans/draft")
        self.assertEqual(
            acme["declared"]["cwd_match"], ["~/repos/widget", "/synthetic/beta/repos"]
        )
        self.assertEqual(
            acme["declared"]["scan_roots"], ["~/repos", "/synthetic/beta/repos"]
        )
        self.assertEqual(len(acme["declared"]["repo_ids"]), 3)

    def test_repos_are_sorted_and_unique(self):
        ids = [repo["repo_id"] for repo in build()["repos"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class RedactionTests(unittest.TestCase):
    def test_planted_secrets_never_appear_in_the_payload(self):
        for payload in (build(), build(observe=True, probe=CountingProbe())):
            serialized = ei.canonical_json(payload)
            for secret in PLANTED_SECRETS:
                self.assertNotIn(secret, serialized, f"leaked {secret!r}")

    def test_git_remote_is_reduced_to_host_and_kind(self):
        gadget = [
            repo
            for repo in build()["repos"]
            if repo["declared"]["registry_id"] == "gadget"
        ][0]
        self.assertEqual(gadget["declared"]["remote_host"], "github.test")
        self.assertEqual(gadget["declared"]["remote_kind"], "https")
        serialized = ei.canonical_json(build())
        self.assertNotIn("deploy:", serialized)
        self.assertNotIn("github.test/example/gadget.git", serialized)

    def test_secret_in_an_allowlisted_field_is_redacted_not_dropped(self):
        """The value reaches projection; redaction -- not the allowlist -- scrubs it."""
        bravo = [
            item for item in build()["clients"] if item["client_id"] == "bravo"
        ][0]
        label = bravo["declared"]["label"]
        self.assertIn("Bravo", label)
        self.assertIn("[REDACTED]", label)
        self.assertNotIn("bravo-label-secret-value", label)

    def test_ignored_overlay_sections_are_structurally_unreachable(self):
        serialized = ei.canonical_json(build())
        for token in (
            "human_operator",
            "access_token_env",
            "ACME_ACCESS_TOKEN",
            "lock_path",
            "deploy_secret",
            "booking_url",
            "default_cwd",
            "plan_index",
            "does_not_own",
        ):
            self.assertNotIn(token, serialized, f"allowlist leaked {token!r}")

    def test_redaction_metadata_is_declared(self):
        payload = build()
        self.assertEqual(payload["redaction"]["marker"], "[REDACTED]")
        self.assertTrue(payload["redaction"]["applied"])

    def test_build_fails_closed_without_the_redaction_table(self):
        def boom():
            raise ei.EnvironmentInventoryError("no redaction table")

        ei._redaction_module_override = boom
        try:
            with self.assertRaises(ei.EnvironmentInventoryError):
                build()
        finally:
            del ei._redaction_module_override

    def test_arbitrary_secret_values_are_scrubbed_from_injected_config(self):
        """Feed secret-like values through every projected string field."""
        payload = build(
            registry_document={
                "schema_version": 1,
                "repos": [
                    {
                        "id": "leaky",
                        "path": "~/repos/leaky",
                        "bucket": "SECRET=bucket-secret-9999",
                        "ownership": "API_KEY=own-secret-8888",
                        "runtime_class": "runnable",
                        "sbp_owner": "PASSWORD=owner-secret-7777",
                        "remote": "https://u:remote-secret-6666@git.test/x.git",
                    }
                ],
            },
            client_overlays={
                "leaky": {
                    "version": 1,
                    "client": {
                        "id": "leaky",
                        "label": "TOKEN=label-secret-5555",
                        "context": {
                            "cwd_match": ["PASSPHRASE=cwd-secret-4444"],
                            "plans": {"plan_root": "AUTH_KEY=plan-secret-3333"},
                            "repo_landscape": {
                                "scan_roots": ["CREDENTIAL=scan-secret-2222"]
                            },
                        },
                    },
                }
            },
        )
        serialized = ei.canonical_json(payload)
        for secret in (
            "bucket-secret-9999",
            "own-secret-8888",
            "owner-secret-7777",
            "remote-secret-6666",
            "label-secret-5555",
            "cwd-secret-4444",
            "plan-secret-3333",
            "scan-secret-2222",
        ):
            self.assertNotIn(secret, serialized, f"leaked {secret!r}")
        self.assertIn("[REDACTED]", serialized)


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class HotPathTests(unittest.TestCase):
    #: Anything here would turn a contract build into an unbounded or blocking
    #: operation. The module must not reference them at all.
    BANNED_SOURCE_TOKENS = (
        "os.walk",
        "os.scandir",
        "rglob",
        "glob.glob",
        "subprocess",
        "socket.",
        "urllib",
        "http.client",
        "requests",
        "shutil",
        "sleep",
    )

    def test_module_source_contains_no_scanning_or_blocking_calls(self):
        source = Path(ei.__file__).read_text(encoding="utf-8")
        for token in self.BANNED_SOURCE_TOKENS:
            self.assertNotIn(token, source, f"module references {token!r}")

    def test_declared_build_performs_zero_presence_probes(self):
        probe = CountingProbe()
        build(probe=probe)
        self.assertEqual(probe.calls, [])

    def test_observation_costs_exactly_one_probe_per_repo_plus_roots(self):
        probe = CountingProbe()
        payload = build(observe=True, probe=probe)
        repo_count = len(payload["repos"])
        root_count = len(payload["machine"]["observed"]["roots"])
        self.assertEqual(len(probe.calls), repo_count + root_count)
        self.assertEqual(len(probe.calls), len(set(probe.calls)))

    def test_hot_path_entrypoints_are_declared(self):
        self.assertEqual(
            ei.HOT_PATH_SAFE_ENTRYPOINTS, ("read_inventory_cache", "is_stale")
        )

    def test_cache_read_touches_one_file_and_no_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build()
            path = ei.write_inventory_cache(payload, tmp)
            self.assertEqual(
                path, Path(tmp) / str(ei.INVENTORY_CACHE_REL)
            )

            opened: list[str] = []
            real_open = Path.read_text

            def spy(self, *args, **kwargs):
                opened.append(str(self))
                return real_open(self, *args, **kwargs)

            Path.read_text = spy
            try:
                loaded = ei.read_inventory_cache(tmp)
            finally:
                Path.read_text = real_open

            self.assertEqual(opened, [str(path)])
            self.assertEqual(loaded["repos"], payload["repos"])

    def test_cache_ignores_a_foreign_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build()
            payload["schema_version"] = "1999-01-01+environment_inventory.v0"
            ei.write_inventory_cache(payload, tmp)
            self.assertIsNone(ei.read_inventory_cache(tmp))

    def test_cache_miss_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ei.read_inventory_cache(tmp))

    def test_canonical_json_is_deterministic(self):
        self.assertEqual(ei.canonical_json(build()), ei.canonical_json(build()))


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class FreshnessReadinessRecoveryTests(unittest.TestCase):
    def test_freshness_facts(self):
        payload = build(now=1000.0, ttl_s=60.0)
        self.assertEqual(payload["generated_at"], 1000.0)
        self.assertEqual(payload["freshness"]["generated_at"], 1000.0)
        self.assertEqual(payload["freshness"]["ttl_s"], 60.0)
        self.assertEqual(payload["freshness"]["expires_at"], 1060.0)
        self.assertEqual(payload["freshness"]["source_count"], len(payload["sources"]))

    def test_is_stale(self):
        payload = build(now=1000.0, ttl_s=60.0)
        self.assertFalse(ei.is_stale(payload, now=1059.0))
        self.assertTrue(ei.is_stale(payload, now=1061.0))
        self.assertTrue(ei.is_stale(None))
        self.assertTrue(ei.is_stale({"freshness": {}}))

    def test_sources_carry_content_digests(self):
        payload = build()
        kinds = [item["kind"] for item in payload["sources"]]
        self.assertEqual(kinds.count("machines"), 1)
        self.assertEqual(kinds.count("registry"), 1)
        self.assertEqual(kinds.count("client_overlay"), 2)
        for item in payload["sources"]:
            self.assertTrue(item["present"])
            self.assertEqual(len(item["sha256"]), 64)
            self.assertIsInstance(item["mtime"], float)

    def test_source_digest_changes_when_content_changes(self):
        def digest(path):
            payload = build(registry_path=path)
            return [
                item["sha256"] for item in payload["sources"] if item["kind"] == "registry"
            ][0]

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "registry.repos.yaml"
            copy.write_text(REGISTRY_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(digest(str(copy)), digest(str(REGISTRY_FIXTURE)))
            copy.write_text(
                REGISTRY_FIXTURE.read_text(encoding="utf-8") + "\n# touched\n",
                encoding="utf-8",
            )
            self.assertNotEqual(digest(str(copy)), digest(str(REGISTRY_FIXTURE)))

    def test_readiness_ready(self):
        readiness = build()["readiness"]
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["reasons"], [])
        self.assertEqual(readiness["declared_repo_count"], len(build()["repos"]))
        self.assertEqual(readiness["declared_client_count"], 2)
        self.assertIsNone(readiness["observed_present_count"])

    def test_readiness_degraded_when_observation_finds_absences(self):
        payload = build(observe=True, probe=CountingProbe())
        readiness = payload["readiness"]
        self.assertEqual(readiness["status"], "degraded")
        self.assertEqual(readiness["observed_present_count"], 0)
        self.assertEqual(readiness["observed_missing_count"], len(payload["repos"]))
        self.assertTrue(
            any("absent on this machine" in reason for reason in readiness["reasons"])
        )

    def test_readiness_unconfigured_and_recovery_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            # root_dir + config_root both point INSIDE the tempdir so no
            # discovery candidate can fall back onto this box's real config repo.
            payload = ei.build_environment_inventory(
                machines_path=os.path.join(tmp, "missing-machines.yaml"),
                root_dir=os.path.join(tmp, "repo"),
                config_root=os.path.join(tmp, "config"),
                env={},
                path_vars={},
                now=1000.0,
            )
        self.assertEqual(payload["readiness"]["status"], "unconfigured")
        codes = {item["code"] for item in payload["recovery"]}
        self.assertIn("machines_config_unreadable", codes)
        self.assertIn("registry_missing", codes)
        self.assertIn("clients_dir_missing", codes)
        for item in payload["recovery"]:
            self.assertTrue(item["message"])
            self.assertTrue(item["hint"])
        self.assertEqual(
            payload["readiness"]["recovery_count"], len(payload["recovery"])
        )

    def test_unsupported_registry_schema_is_reported_not_fatal(self):
        payload = build(registry_document={"schema_version": 99, "repos": []})
        codes = {item["code"] for item in payload["recovery"]}
        self.assertIn("registry_schema_unsupported", codes)

    def test_machine_detection_source_is_recorded(self):
        self.assertEqual(build()["machine"]["detection_source"], "explicit")
        by_hostname = build(machine_id=None, hostname="beta-host")
        self.assertEqual(by_hostname["machine"]["detection_source"], "hostname")
        self.assertEqual(
            by_hostname["machine"]["declared"]["machine_id"], "beta-devbox"
        )
        by_env = build(
            machine_id=None, hostname="nope", env={"SKILLBOX_MACHINE": "alpha-laptop"}
        )
        self.assertEqual(by_env["machine"]["detection_source"], "env")
        undetected = build(machine_id=None, hostname="unknown-host")
        self.assertEqual(undetected["machine"]["detection_source"], "none")
        self.assertIn(
            "machine_undetected", {item["code"] for item in undetected["recovery"]}
        )


@unittest.skipUnless(_HAVE_YAML, "PyYAML is required to parse the fixture configs")
class SupersedesConsumerParsingTests(unittest.TestCase):
    def test_every_field_swimmers_parses_is_claimed(self):
        missing = [
            field
            for field in SWIMMERS_PARSED_FIELDS
            if field not in ei.SUPERSEDED_CONSUMER_FIELDS
        ]
        self.assertEqual(missing, [], f"unclaimed consumer fields: {missing}")

    def test_every_claimed_contract_path_actually_resolves(self):
        payload = build(observe=True, probe=CountingProbe())
        for consumer_field, contract_paths in ei.SUPERSEDED_CONSUMER_FIELDS.items():
            for dotted in contract_paths:
                resolved = resolve_contract_path(payload, dotted)
                self.assertIsNotNone(
                    resolved,
                    f"{consumer_field!r} claims {dotted!r} but it does not resolve",
                )

    def test_supersedes_block_is_emitted(self):
        supersedes = build()["supersedes"]
        self.assertEqual(supersedes["consumer"], "swimmers")
        self.assertEqual(
            sorted(supersedes["fields"]), sorted(ei.SUPERSEDED_CONSUMER_FIELDS)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
