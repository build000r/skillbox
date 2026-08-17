"""The hermetic-join contract for ``sbp git`` (bead skillbox-era-program-v6ac.7.1).

``git_estate`` grew joins that read state **outside** the repo under test: the
reconcile receipts store, the two amp guard scripts, the fleet_convergence
script. Every one is env-overridable and every one defaults to a real path on
the operator's machine, so a fixture that forgets to pin one silently scans the
host. On 2026-08-15 a real receipts store leaked into fixture envelopes the same
day the receipts join shipped, and the goldens began failing on one machine and
passing on another.

"Remember to pin them" is not a fix — it is the thing that already failed. So:

* one registry, ``tests.helpers.HERMETIC_JOIN_ENVS``, used by all six fixture
  suites;
* :class:`RegistryCompletenessTests`, which derives the true set from
  ``git_estate``'s own constants, so an unregistered join fails here rather than
  on someone's laptop three weeks later;
* :class:`HermeticInvarianceTests`, which plants a populated store at the
  default location and proves the pinned envelope is byte-identical anyway —
  the assertion that would have caught the original leak on day one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_manager import git_estate  # noqa: E402

from tests import helpers  # noqa: E402


class RegistryCompletenessTests(unittest.TestCase):
    """The registry must not fall behind the code it is supposed to cover.

    Derived from ``git_estate``'s own module constants rather than restated, so
    adding a join without registering its env fails *here* — which is the whole
    "the regression test will fail you" contract in the source checklist.
    """

    #: Env constants in ``git_estate`` that do NOT redirect an external-state
    #: join, with the reason each is exempt. Anything not listed and not in the
    #: registry is a bug in one of the two.
    NON_JOIN_ENVS = {
        # Scoping inputs, not external-state joins: every fixture already pins
        # them to its own tmp tree, and an absent config root is a *declared*
        # deterministic outcome ("registry unavailable"), not host leakage.
        "SKILLBOX_CONFIG_ROOT": "scan scoping, pinned per fixture",
        "SKILLBOX_MONOSERVER_ROOT": "scan scoping, pinned per fixture",
    }

    def module_env_names(self) -> set[str]:
        """Every ``SKILLBOX_*`` env name git_estate actually reads."""
        source = (ENV_MANAGER_DIR / "runtime_manager" / "git_estate.py").read_text(
            encoding="utf-8"
        )
        found = set()
        for line in source.splitlines():
            for chunk in line.split('"'):
                if chunk.startswith("SKILLBOX_") and chunk.replace("_", "").isalnum():
                    found.add(chunk)
        return found

    def test_every_join_env_in_the_module_is_registered(self) -> None:
        registered = set(helpers.HERMETIC_JOIN_ENVS) | set(
            helpers.HERMETIC_JOIN_BUDGET_ENVS
        )
        unregistered = self.module_env_names() - registered - set(self.NON_JOIN_ENVS)
        self.assertEqual(
            set(),
            unregistered,
            "git_estate reads env var(s) that no fixture pins. Add them to "
            "HERMETIC_JOIN_ENVS in tests/helpers.py, or to NON_JOIN_ENVS here "
            "with the reason they cannot leak host state.",
        )

    def test_the_registry_names_no_env_the_module_ignores(self) -> None:
        """A stale registry entry is a lie about what is being guarded."""
        module_envs = self.module_env_names()
        for name in (*helpers.HERMETIC_JOIN_ENVS, *helpers.HERMETIC_JOIN_BUDGET_ENVS):
            with self.subTest(env=name):
                self.assertIn(name, module_envs)

    def test_the_four_named_joins_are_registered(self) -> None:
        """Pinned by name so a refactor cannot quietly drop one."""
        for name in (
            "SKILLBOX_RECONCILE_RECEIPTS_DIR",
            "SKILLBOX_AMP_CAPSULE_GUARD",
            "SKILLBOX_AMP_CAMPAIGN_GUARD",
            "SKILLBOX_FLEET_CONVERGENCE",
        ):
            with self.subTest(env=name):
                self.assertIn(name, helpers.HERMETIC_JOIN_ENVS)

    def test_the_helper_pins_every_registered_env(self) -> None:
        env = helpers.hermetic_join_env("/tmp/example")
        for name in (*helpers.HERMETIC_JOIN_ENVS, *helpers.HERMETIC_JOIN_BUDGET_ENVS):
            with self.subTest(env=name):
                self.assertIn(name, env)
                self.assertTrue(env[name])

    def test_join_paths_point_at_nothing_that_exists(self) -> None:
        """Absent, not empty: an empty dir is a different code path."""
        with tempfile.TemporaryDirectory() as tmp:
            env = helpers.hermetic_join_env(tmp)
            for name in helpers.HERMETIC_JOIN_ENVS:
                with self.subTest(env=name):
                    self.assertFalse(Path(env[name]).exists())

    def test_an_unregistered_override_is_refused(self) -> None:
        """Typos and new joins cannot sneak in through the override kwarg."""
        with self.assertRaises(AssertionError) as caught:
            helpers.hermetic_join_env("/tmp/example", SKILLBOX_NOT_A_JOIN="/x")
        self.assertIn("HERMETIC_JOIN_ENVS", str(caught.exception))

    def test_the_source_checklist_is_present(self) -> None:
        """The join section must tell the next maintainer where to register."""
        source = (ENV_MANAGER_DIR / "runtime_manager" / "git_estate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("adding a join?", source.lower())
        self.assertIn("HERMETIC_JOIN_ENVS", source)


class HermeticInvarianceTests(unittest.TestCase):
    """A populated store on the host must not be able to move the envelope.

    This is the assertion that would have caught the 2026-08-15 leak the day the
    receipts join shipped: it does not ask "did we remember to pin it", it plants
    the exact state that leaks and proves the envelope is byte-identical anyway.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-hermetic-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[init]\n\tdefaultBranch = main\n[user]\n"
            "\tname = Fixture\n\temail = fixture@example.invalid\n",
            encoding="utf-8",
        )
        self.base_env = {
            "HOME": str(self.tmp / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "SKILLBOX_CONFIG_ROOT": str(self.tmp / "config"),
            "SKILLBOX_STATE_ROOT": str(self.tmp / "state"),
        }
        self._make_repo("clean")
        self._make_repo("dirty", dirty=True)

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ("git", *args), cwd=cwd, check=True,
            capture_output=True, text=True,
            env={**os.environ, **self.base_env},
        )

    def _make_repo(self, name: str, *, dirty: bool = False) -> Path:
        repo = self.estate / name
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-qm", "seed")
        if dirty:
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
        return repo

    def _scan(self, join_env: dict[str, str]) -> str:
        with mock.patch.dict(os.environ, {**self.base_env, **join_env}):
            report = git_estate.build_report(
                roots=[str(self.estate)], depth=2, cwd=str(self.estate)
            )
        report.pop("generated_at", None)
        report.pop("elapsed_seconds", None)
        return json.dumps(report, indent=2, sort_keys=True)

    def _plant_receipts_store(self, path: Path) -> None:
        """A receipts store shaped like the real one, at ``path``."""
        path.mkdir(parents=True, exist_ok=True)
        (path / "clean.json").write_text(
            json.dumps({
                "repo": str(self.estate / "clean"),
                "status": "passed",
                "finished_at": "2026-08-15T09:00:00+00:00",
            }),
            encoding="utf-8",
        )

    def test_a_populated_store_cannot_change_a_pinned_scan(self) -> None:
        """The regression proper: same bytes, store or no store."""
        pinned = helpers.hermetic_join_env(self.tmp)
        absent = self._scan(pinned)
        # Plant a real-looking store at a path the fixture does NOT point at,
        # exactly as a host store sits beside a test that forgot to pin.
        self._plant_receipts_store(self.tmp / "host-receipts")
        self.assertEqual(absent, self._scan(pinned))

    def test_unpinning_the_receipts_join_changes_the_envelope(self) -> None:
        """The meta-test: prove the pin is load-bearing, not decoration.

        Deliberately un-pin one env and point it at a populated store. If this
        ever stops differing, the regression above has stopped guarding
        anything and every other assertion in this file is theatre.
        """
        store = self.tmp / "host-receipts"
        self._plant_receipts_store(store)
        pinned = helpers.hermetic_join_env(self.tmp)
        unpinned = {**pinned, "SKILLBOX_RECONCILE_RECEIPTS_DIR": str(store)}
        self.assertNotEqual(
            self._scan(pinned),
            self._scan(unpinned),
            "a populated receipts store did not change the envelope, so the "
            "hermetic pin proves nothing",
        )

    def test_the_scan_is_stable_across_repeated_pinned_runs(self) -> None:
        pinned = helpers.hermetic_join_env(self.tmp)
        self.assertEqual(self._scan(pinned), self._scan(pinned))

    def test_every_registered_join_is_absent_during_a_pinned_scan(self) -> None:
        """Cheap belt-and-braces: nothing the scan reads may exist."""
        pinned = helpers.hermetic_join_env(self.tmp)
        self._scan(pinned)
        for name in helpers.HERMETIC_JOIN_ENVS:
            with self.subTest(env=name):
                self.assertFalse(Path(pinned[name]).exists())


if __name__ == "__main__":
    unittest.main()
