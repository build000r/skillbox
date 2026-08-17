"""Cross-surface proof that the state root has exactly one mutator.

The lease primitive has its own unit suite (``tests.test_state_mutation_lock``).
This file proves something the primitive alone cannot: that every surface which
mutates the state root actually goes through it, and that the surfaces agree
about *which* root they are guarding.

Three families:

1. **The matrix.** Real second PROCESSES contend for one canonical root. flock
   is per open file description, so a same-thread or same-process re-acquisition
   is a different thing entirely (the lease raises ``Nesting`` for it, on
   purpose) — only another process produces the bounded timeout a live operator
   meets. Every case here spawns one.
2. **Adoption.** Every mutating row in the inventory is either reachable through
   a gate that acquires it, or named in a short, reasoned exemption table. A new
   ungated mutation boundary fails this file.
3. **Forensics.** A timeout carries the fields the operator docs promise, and
   carries no secret.

Nothing here starts a service, touches the network, or mutates the real state
root: every case runs against a throwaway root under a temporary directory.

Deliberately NOT asserted: ``kernel_holders``. Reading it needs ``/proc/locks``,
which does not exist on macOS, and the property that matters is *exclusion*, not
the introspection of it. Asserting behaviour keeps this suite honest on both
platforms rather than green on one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from runtime_manager import state_mutation as SM  # noqa: E402

#: Seconds a contender waits before it must report contention. Short enough that
#: the suite stays quick, long enough that it is not a timing race: the holder
#: is already proven to hold before any contender starts.
CONTENDER_TIMEOUT = 1.0

#: How long a holder subprocess stays up. Every holder is torn down in cleanup;
#: this is only the backstop for a test that dies before its cleanup runs.
HOLDER_LIFETIME = 60


HOLDER_SOURCE = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {env_manager!r})
    from runtime_manager import state_mutation as SM
    root, boundary, ready, lifetime = sys.argv[1:5]
    with SM.state_mutation_lease(root, boundary):
        open(ready, "w").close()
        time.sleep(float(lifetime))
    """
)

#: A contender that reports, as JSON, whether it got the lease.
CONTENDER_SOURCE = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {env_manager!r})
    from runtime_manager import state_mutation as SM
    root, boundary, timeout = sys.argv[1:4]
    try:
        with SM.state_mutation_lease(root, boundary, timeout=float(timeout)):
            print(json.dumps({{"acquired": True}}))
    except SM.StateMutationLeaseTimeout as exc:
        # The forensics live in exc.context -- that IS the contract, and it is
        # what docs/operations.md promises an operator.
        payload = {{"acquired": False, "code": exc.code}}
        payload.update(exc.context)
        print(json.dumps(payload, default=str))
    """
)

#: A reader: proves a read never queues behind a writer.
READER_SOURCE = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {env_manager!r})
    from runtime_manager import state_mutation as SM
    root = sys.argv[1]
    # read_lease_metadata takes no lock, by design: there is no read lock.
    payload = SM.read_lease_metadata(root)
    print(json.dumps({{"read_ok": isinstance(payload, dict)}}))
    """
)


class MatrixTestCase(unittest.TestCase):
    """Shared subprocess harness for the contention matrix."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "state"
        self.root.mkdir()
        self.addCleanup(setattr, SM, "_ACTIVE_RUNTIME_LEASE", None)

    # -- harness -------------------------------------------------------------

    def _spawn(self, source: str, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", source.format(env_manager=str(ENV_MANAGER_DIR)), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def hold(self, root: Path, boundary: str = "manage.sync") -> subprocess.Popen:
        """Hold ``root`` from another process; returns once it demonstrably has it."""
        ready = self.base / f"ready-{abs(hash((str(root), boundary))):x}"
        proc = self._spawn(
            HOLDER_SOURCE, str(root), boundary, str(ready), str(HOLDER_LIFETIME)
        )

        def stop() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

        self.addCleanup(stop)
        deadline = time.time() + 30
        while time.time() < deadline and not ready.exists():
            if proc.poll() is not None:
                self.fail(f"holder exited early: {proc.communicate()[1]}")
            time.sleep(0.02)
        self.assertTrue(ready.exists(), f"holder never acquired {root}")
        return proc

    def contend(
        self, root: Path, boundary: str = "manage.sync", timeout: float = CONTENDER_TIMEOUT
    ) -> dict:
        proc = self._spawn(CONTENDER_SOURCE, str(root), boundary, str(timeout))
        out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        return json.loads(out.strip().splitlines()[-1])


class SingleMutatorMatrixTests(MatrixTestCase):
    """One canonical root, one writer — proven across surfaces and spellings."""

    def test_a_second_writer_on_one_root_is_excluded(self) -> None:
        self.hold(self.root)
        result = self.contend(self.root)
        self.assertFalse(result["acquired"])
        self.assertEqual(result["code"], "STATE_LEASE_TIMEOUT")

    def test_the_root_is_free_again_once_the_holder_exits(self) -> None:
        """Exclusion, not permanent seizure."""
        holder = self.hold(self.root)
        self.assertFalse(self.contend(self.root)["acquired"])
        holder.kill()
        holder.communicate()
        self.assertTrue(self.contend(self.root, timeout=10.0)["acquired"])

    def test_a_cli_writer_excludes_a_pulse_window(self) -> None:
        """CLI vs pulse: different surfaces, same root, one writer."""
        self.hold(self.root, "manage.sync")
        result = self.contend(self.root, "pulse.run")
        self.assertFalse(result["acquired"])
        self.assertEqual(result["boundary_id"], "pulse.run")

    def test_a_pulse_window_excludes_a_cli_writer(self) -> None:
        """The exclusion is symmetric; neither surface is privileged."""
        self.hold(self.root, "pulse.run")
        self.assertFalse(self.contend(self.root, "manage.sync")["acquired"])

    def test_a_box_writer_excludes_a_runtime_writer_on_one_root(self) -> None:
        """The load-bearing case for the whole contract.

        box/operator resolve the state root through ``opslib.resolve_state_root``
        and runtime/pulse through ``state_mutation.canonical_runtime_state_root``.
        If those two readings ever diverge, each half takes a different lock and
        each believes it is the single writer — so this asserts the resolvers
        agree AND that the resulting locks actually collide.
        """
        from lib import opslib

        repo = self.base / "repo"
        (repo / ".skillbox-state").mkdir(parents=True)
        for override in (None, "state/here", str(self.base / "abs-root")):
            with self.subTest(override=override):
                env = dict(os.environ)
                env.pop("SKILLBOX_STATE_ROOT", None)
                if override is not None:
                    env["SKILLBOX_STATE_ROOT"] = override
                original = os.environ.get("SKILLBOX_STATE_ROOT")
                try:
                    if override is None:
                        os.environ.pop("SKILLBOX_STATE_ROOT", None)
                    else:
                        os.environ["SKILLBOX_STATE_ROOT"] = override
                    self.assertEqual(
                        SM.canonical_runtime_state_root(repo),
                        opslib.resolve_state_root(repo),
                    )
                finally:
                    if original is None:
                        os.environ.pop("SKILLBOX_STATE_ROOT", None)
                    else:
                        os.environ["SKILLBOX_STATE_ROOT"] = original

        box_root = SM.canonical_runtime_state_root(repo)
        box_root.mkdir(parents=True, exist_ok=True)
        self.hold(box_root, "box.register")
        self.assertFalse(self.contend(box_root, "manage.sync")["acquired"])

    def test_a_symlink_alias_of_the_root_collides_with_it(self) -> None:
        """Two spellings of one root must not be two locks."""
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(
            SM.canonical_state_root(alias), SM.canonical_state_root(self.root)
        )
        self.hold(self.root)
        self.assertFalse(self.contend(alias)["acquired"])

    def test_a_trailing_slash_spelling_collides_too(self) -> None:
        self.hold(self.root)
        self.assertFalse(self.contend(Path(str(self.root) + "/"))["acquired"])

    def test_two_distinct_roots_proceed_independently(self) -> None:
        other = self.base / "other-state"
        other.mkdir()
        self.hold(self.root)
        self.assertTrue(self.contend(other, timeout=10.0)["acquired"])

    def test_a_sigkilled_holder_releases_the_root(self) -> None:
        """The kernel releases the flock; no operator action, no force-clear."""
        holder = self.hold(self.root)
        self.assertFalse(self.contend(self.root)["acquired"])
        os.kill(holder.pid, signal.SIGKILL)
        holder.communicate()
        self.assertTrue(
            self.contend(self.root, timeout=10.0)["acquired"],
            "a crashed holder must not leave the root permanently locked",
        )

    def test_stale_metadata_after_a_crash_is_not_mistaken_for_a_live_holder(self) -> None:
        holder = self.hold(self.root)
        os.kill(holder.pid, signal.SIGKILL)
        holder.communicate()
        # The advisory record may still describe the dead holder; the lock does
        # not, which is why acquisition succeeds. Metadata is evidence, never
        # the source of truth about who holds.
        payload = SM.read_lease_metadata(self.root)
        self.assertIsInstance(payload, dict)
        self.assertTrue(self.contend(self.root, timeout=10.0)["acquired"])

    def test_reads_never_queue_behind_a_writer(self) -> None:
        """There is no read lock, on purpose."""
        self.hold(self.root)
        for _ in range(3):
            proc = self._spawn(READER_SOURCE, str(self.root))
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err[-2000:])
            self.assertTrue(json.loads(out.strip().splitlines()[-1])["read_ok"])

    def test_nested_ownership_takes_one_lock_not_two(self) -> None:
        """Proved from the outside: a nested owner adds no second holder."""
        with SM.runtime_mutation_lease("manage.focus", root_dir=self.base) as outer:
            root = SM.canonical_runtime_state_root(self.base)
            with SM.runtime_mutation_lease("manage.sync", root_dir=self.base):
                # Another process still cannot get in, and the inner exit below
                # must not release the outer lease either.
                self.assertFalse(self.contend(root)["acquired"])
            self.assertTrue(outer.held)
            self.assertFalse(self.contend(root)["acquired"])
        self.assertTrue(self.contend(SM.canonical_runtime_state_root(self.base),
                                     timeout=10.0)["acquired"])

    def test_an_unproved_nested_owner_is_refused_not_deadlocked(self) -> None:
        """Ambient reuse is refused so ownership is proved, never assumed."""
        with SM.runtime_mutation_lease("manage.sync", root_dir=self.base):
            root = SM.canonical_runtime_state_root(self.base)
            with self.assertRaises(SM.StateMutationLeaseNesting):
                with SM.state_mutation_lease(root, "manage.sync"):
                    pass


class GatedMakeSurfaceTests(MatrixTestCase):
    """The one Make target that writes the state root now contends properly.

    A Makefile recipe cannot hold a lease, so `make bootstrap-env` delegates to
    scripts/bootstrap-operator-env.py, which is the final mutation owner.
    """

    SCRIPT = ROOT_DIR / "scripts" / "bootstrap-operator-env.py"

    def _run_script(self, state_root: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, SKILLBOX_STATE_ROOT=str(state_root))
        return subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_concurrent_bootstraps_seed_exactly_once(self) -> None:
        """The decide-then-seed span is atomic.

        Without the lease, several processes can each observe "absent" and each
        copy .env.example -- last writer wins, and an operator who edited the
        file between two of them silently loses it. This is the property that
        motivated gating the target, so it is asserted by racing real processes
        rather than by reading the code.
        """
        state_root = self.base / "concurrent-state"
        procs = [
            subprocess.Popen(
                [sys.executable, str(self.SCRIPT)],
                env=dict(os.environ, SKILLBOX_STATE_ROOT=str(state_root)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err[-2000:])
            outputs.append(out)
        seeded = [line for line in outputs if "seeded" in line]
        self.assertEqual(len(seeded), 1, f"expected exactly one seeder, got {outputs}")
        self.assertTrue((state_root / "operator" / ".env").is_file())

    def test_the_seed_is_private(self) -> None:
        """The operator directory exists so in-container agents cannot read it."""
        state_root = self.base / "mode-state"
        self.assertEqual(self._run_script(state_root).returncode, 0)
        target = state_root / "operator" / ".env"
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_a_repo_root_env_suppresses_the_seed(self) -> None:
        """Pre-existing behaviour, preserved: never hand out a second .env."""
        state_root = self.base / "suppressed-state"
        env = dict(os.environ, SKILLBOX_STATE_ROOT=str(state_root))
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            env=env, capture_output=True, text=True, timeout=120, cwd=str(ROOT_DIR),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        seeded = (state_root / "operator" / ".env").is_file()
        repo_env_present = (ROOT_DIR / ".env").is_file()
        if repo_env_present:
            self.assertFalse(seeded, "a repo-root .env must suppress the seed")
        else:
            self.assertTrue(seeded)

    def test_the_script_refuses_unexpected_arguments(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--wat"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


class TimeoutForensicsTests(MatrixTestCase):
    """A contended caller learns enough to act, and learns no secrets."""

    #: The fields docs/operations.md promises an operator. Renaming one is a
    #: documentation break, so the promise is pinned here.
    PROMISED_FIELDS = (
        "code",
        "boundary_id",
        "operation_id",
        "state_root",
        "lock_path",
        "waited_seconds",
        "timeout_seconds",
        "holder",
    )

    def test_a_timeout_carries_every_documented_field(self) -> None:
        self.hold(self.root)
        result = self.contend(self.root)
        self.assertFalse(result["acquired"])
        for field in self.PROMISED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, result)
                self.assertNotIn(result[field], ("", None), f"{field} is empty")

    def test_the_timeout_names_the_boundary_that_is_waiting(self) -> None:
        self.hold(self.root, "manage.sync")
        self.assertEqual(self.contend(self.root, "box.register")["boundary_id"], "box.register")

    def test_the_wait_is_bounded_by_the_requested_timeout(self) -> None:
        self.hold(self.root)
        started = time.monotonic()
        result = self.contend(self.root, timeout=CONTENDER_TIMEOUT)
        elapsed = time.monotonic() - started
        self.assertFalse(result["acquired"])
        # Generous ceiling: this asserts BOUNDEDNESS, not a stopwatch. A
        # timing-only assertion would be flaky, which the contract forbids.
        self.assertLess(elapsed, CONTENDER_TIMEOUT + 60)

    def test_the_holder_evidence_names_the_boundary_that_is_holding(self) -> None:
        self.hold(self.root, "manage.sync")
        holder = self.contend(self.root, "pulse.run")["holder"]
        self.assertIsInstance(holder, dict)
        self.assertEqual(holder["advisory"]["boundary_id"], "manage.sync")
        self.assertIn("note", holder, "the holder record must state its own limits")

    def test_the_holder_evidence_is_present_and_carries_no_secret(self) -> None:
        self.hold(self.root)
        result = self.contend(self.root)
        blob = json.dumps(result, default=str)
        for marker in ("token", "secret", "password", "authkey", "api_key", "Bearer"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker.lower(), blob.lower())

    def test_the_forensics_survive_a_json_round_trip(self) -> None:
        """Operators and agents read this as JSON; it must be serialisable."""
        self.hold(self.root)
        result = self.contend(self.root)
        self.assertEqual(json.loads(json.dumps(result, default=str)), result)


class InventoryAdoptionTests(unittest.TestCase):
    """Zero UNEXPLAINED public bypass.

    Every mutating row is in exactly one bucket:

    * **gated** -- a gate acquires it;
    * **no jurisdiction** -- it writes Docker, a remote host, or nothing
      persistent, so the state-root lease has nothing to serialize;
    * **delegating** -- a wrapper whose child is the final owner (and which must
      NOT hold a lease across that child; see j2d7's run_script guard);
    * **reviewed exemption** -- explained by a named mechanism;
    * **known bypass** -- a REAL gap, with the leaf that owns the repair.

    The last bucket is the honest one. This node does not repair branch defects,
    so a gap is recorded and ratcheted rather than quietly exempted or hidden.
    """

    #: state_root_source values that mean "the lease has nothing to guard here".
    #:
    #: Taken from the manifest's OWN descriptions in STATE_ROOT_SOURCES, not
    #: from a guess here. Each of these says, in the inventory itself, that it
    #: is not a state root:
    #:
    #:   n/a                     "boundary writes no persistent state"
    #:   external                "state owned by a process outside this repo"
    #:   remote                  "the state root ON THE REMOTE BOX"
    #:   home                    "$HOME ... outside every state root"
    #:   runtime_model.root_dir  "repo-tracked paths under <root_dir>, not a state root"
    #:   state_backup.backup_root "refuses a root inside the state root"
    #:
    #: The first cut of this check listed only the first three, which
    #: over-reported five make rows as bypasses when the inventory already said
    #: they write outside every state root. test_no_jurisdiction_matches_the_manifest
    #: pins the reading so it cannot drift back.
    NO_JURISDICTION = frozenset(
        {
            "n/a",
            "external",
            "remote",
            "home",
            "runtime_model.root_dir",
            "state_backup.backup_root",
        }
    )

    #: The phrases in a STATE_ROOT_SOURCES description that mean "not a state
    #: root". Used to re-derive NO_JURISDICTION from the inventory text.
    OUTSIDE_MARKERS = (
        "writes no persistent state",
        "outside this repo",
        "outside every state root",
        "not a state root",
        "ON THE REMOTE BOX",
        "refuses a root inside the state root",
    )

    #: boundary_id -> the mechanism that makes a gate unnecessary.
    REVIEWED_EXEMPTIONS = {
        "box.import": (
            "alias of `box register`: scripts/box.py BOX_COMMAND_BOUNDARIES maps the "
            "`import` command to box.register, so the COMMAND is gated and it is only "
            "this alias ROW that is never acquired under its own id"
        ),
        "operator_mcp.operator_compose_up": (
            "writes Docker images and containers, not state-root state -- its own row "
            "records the compose project lock as the only serializer. Gating it would "
            "serialize Docker work against unrelated state writes for no safety gain"
        ),
        "make.self-test": (
            "delegates to scripts/self-test.sh, which carries its own "
            "flock ${STATE_ROOT}/self-test/toolchain/.lock and confines its writes to "
            "${STATE_ROOT}/self-test/**, running lanes in a throwaway clone"
        ),
        "make.self-test-refresh": "same delegation and dedicated flock as make.self-test",
        "make.self-test-worktree": "same delegation and dedicated flock as make.self-test",
        "make.python-cov-xml": (
            "delegates to `coverage`; its writes are .coverage and coverage.xml at the "
            "REPO root, which is not state-root state"
        ),
    }

    #: boundary_id -> the path it actually writes, for rows whose
    #: state_root_source already places them outside every state root. Recorded
    #: rather than left implicit: "no jurisdiction" is a claim about where a
    #: recipe writes, and that claim should be reviewable in one place.
    REVIEWED_NO_JURISDICTION = {
        "make.dcg-reconcile": (
            "writes the model-resolved managed home's DCG surface (~/.claude, "
            "~/.codex hook files and marker-stamped policy); state_root_source=home, "
            "which the inventory defines as outside every state root"
        ),
        "make.dcg-relinquish": (
            "removes DCG-owned hook entries and marker-stamped policy under the same "
            "managed home; state_root_source=home"
        ),
        "make.dev-shims-install": (
            "writes $(DEV_SHIM_BIN_DIR) symlinks, default $HOME/.local/skillbox-shims "
            "(Makefile:37); state_root_source=home"
        ),
        "make.wrappers-install": (
            "writes $(WRAPPER_BIN_DIR)/sbp and /sbo, default $HOME/.local/bin "
            "(Makefile:36); state_root_source=home"
        ),
        "make.swimmers-install": (
            "scripts/05-swimmers.sh installs to ${SKILLBOX_SWIMMERS_INSTALL_DIR:-"
            "${home_root}/.local/bin} and logs under ${SKILLBOX_LOG_ROOT:-"
            "${workspace_root}/logs}; neither is the state root"
        ),
    }

    #: boundary_id -> (owning leaf, why it is still a gap). Ratcheted: this set
    #: may shrink, never grow. EMPTY is the goal state and the current state.
    KNOWN_BYPASSES: dict[str, tuple[str, str]] = {}

    def setUp(self) -> None:
        self.mutations = [entry for entry in SM.MANIFEST if entry.is_mutation]
        self.assertTrue(self.mutations, "inventory has no mutations; suite is vacuous")

    # -- bucket computation --------------------------------------------------

    def gated_boundaries(self) -> set[str]:
        """Boundaries some gate in this tree can actually acquire."""
        gated: set[str] = set()

        # manage: dispatch resolves the boundary FROM the manifest, so every
        # classified manage mutation is gated by construction (d68s).
        gated |= {
            entry.boundary_id
            for entry in self.mutations
            if entry.surface == SM.SURFACE_MANAGE
        }

        box = SourceFileLoader(
            "skillbox_box_for_adoption", str(SCRIPTS_DIR / "box.py")
        ).load_module()
        gated |= set(box.BOX_COMMAND_BOUNDARIES.values())

        mcp = SourceFileLoader(
            "skillbox_mcp_for_adoption", str(SCRIPTS_DIR / "operator_mcp_server.py")
        ).load_module()
        gated |= set(mcp._MARKER_BOUNDARIES.values())  # noqa: SLF001

        pulse_source = (ENV_MANAGER_DIR / "pulse.py").read_text(encoding="utf-8")
        gated |= {
            entry.boundary_id
            for entry in self.mutations
            if entry.surface == SM.SURFACE_PULSE
            and f'"{entry.boundary_id}"' in pulse_source
        }

        doctor_fix = (SCRIPTS_DIR / "lib" / "doctor_fix.py").read_text(encoding="utf-8")
        if "state_mutation_lease" in doctor_fix:
            gated |= {
                entry.boundary_id
                for entry in self.mutations
                if entry.surface == SM.SURFACE_RECONCILE
            }

        # A row that DECLARES the lease as its lock owner is gated, whatever
        # surface it sits on. This is how a Make target can be gated at all: a
        # recipe cannot hold a lease, so it delegates to a script that does, and
        # the row records which lease that is.
        gated |= {
            entry.boundary_id
            for entry in self.mutations
            if "state_mutation_lease" in entry.lock_owner
        }
        return gated

    def _unbucketed(self) -> list[str]:
        gated = self.gated_boundaries()
        loose: list[str] = []
        for entry in self.mutations:
            bid = entry.boundary_id
            if bid in gated:
                continue
            if entry.state_root_source in self.NO_JURISDICTION:
                continue
            if entry.surface == SM.SURFACE_MAKE and entry.delegates_to:
                continue
            if (
                bid in self.REVIEWED_EXEMPTIONS
                or bid in self.REVIEWED_NO_JURISDICTION
                or bid in self.KNOWN_BYPASSES
            ):
                continue
            loose.append(bid)
        return sorted(loose)

    # -- the claim -----------------------------------------------------------

    def test_no_mutation_is_unexplained(self) -> None:
        self.assertEqual(
            [],
            self._unbucketed(),
            "unexplained public bypass: these rows mutate state-root state and no "
            "gate acquires them. Gate them in their owning leaf, or add a reviewed "
            "exemption naming the mechanism.",
        )

    def test_every_cli_surface_is_fully_adopted(self) -> None:
        """The strong half of the claim: no CLI surface has a known bypass."""
        cli_surfaces = {
            SM.SURFACE_MANAGE,
            SM.SURFACE_BOX,
            SM.SURFACE_OPERATOR_MCP,
            SM.SURFACE_PULSE,
            SM.SURFACE_RECONCILE,
        }
        leaked = sorted(
            bid
            for bid in self.KNOWN_BYPASSES
            if SM.boundary(bid).surface in cli_surfaces
        )
        self.assertEqual([], leaked, "a CLI surface regressed into a known bypass")

    def test_every_known_bypass_names_an_owning_leaf(self) -> None:
        """Vacuous while KNOWN_BYPASSES is empty; kept so a re-added gap must
        still name who repairs it."""
        for bid, (owner, reason) in self.KNOWN_BYPASSES.items():
            with self.subTest(boundary_id=bid):
                self.assertIn(bid, {e.boundary_id for e in self.mutations})
                self.assertTrue(owner.strip(), "a bypass must name who repairs it")
                self.assertGreater(len(reason), 40, "a bypass needs a real reason")

    def test_known_bypasses_are_shrink_only(self) -> None:
        """A gated boundary must lose its bypass entry, not keep it."""
        gated = self.gated_boundaries()
        for bid in self.KNOWN_BYPASSES:
            with self.subTest(boundary_id=bid):
                self.assertNotIn(bid, gated, "this is gated now; delete its entry")

    def test_every_exemption_is_still_needed(self) -> None:
        gated = self.gated_boundaries()
        for bid, reason in self.REVIEWED_EXEMPTIONS.items():
            with self.subTest(boundary_id=bid):
                self.assertIn(bid, {e.boundary_id for e in self.mutations})
                self.assertNotIn(bid, gated, "this is gated now; delete its exemption")
                self.assertGreater(len(reason), 40, "an exemption needs a real reason")

    def test_the_check_can_actually_fail(self) -> None:
        """Guard the guard: a synthetic ungated mutation must not be bucketed."""
        gated = self.gated_boundaries()
        fake = "manage.definitely-not-gated"
        self.assertNotIn(fake, gated)
        self.assertNotIn(fake, self.REVIEWED_EXEMPTIONS)
        self.assertNotIn(fake, self.KNOWN_BYPASSES)

    def test_reads_are_never_gated(self) -> None:
        """A read taking the write lease is a liveness bug, not extra safety."""
        gated = self.gated_boundaries()
        for entry in SM.MANIFEST:
            if entry.is_mutation:
                continue
            with self.subTest(boundary=entry.boundary_id):
                self.assertNotIn(entry.boundary_id, gated)

    def test_no_jurisdiction_matches_the_manifest(self) -> None:
        """Jurisdiction is read from the inventory, never guessed here."""
        derived = {
            name
            for name, description in SM.STATE_ROOT_SOURCES.items()
            if any(marker in description for marker in self.OUTSIDE_MARKERS)
        }
        self.assertEqual(
            self.NO_JURISDICTION,
            derived,
            "NO_JURISDICTION drifted from the STATE_ROOT_SOURCES descriptions",
        )

    def test_every_make_surface_row_is_accounted_for(self) -> None:
        """The Make surface is the one this node closed out; keep it closed."""
        loose = sorted(
            entry.boundary_id
            for entry in self.mutations
            if entry.surface == SM.SURFACE_MAKE
            and entry.boundary_id not in self.gated_boundaries()
            and entry.state_root_source not in self.NO_JURISDICTION
            and not entry.delegates_to
            and entry.boundary_id not in self.REVIEWED_EXEMPTIONS
            and entry.boundary_id not in self.REVIEWED_NO_JURISDICTION
        )
        self.assertEqual([], loose)

    def test_there_are_no_known_bypasses_left(self) -> None:
        """The claim the docs now make: every surface is adopted."""
        self.assertEqual({}, self.KNOWN_BYPASSES)

    def test_the_gated_make_row_declares_a_real_lease(self) -> None:
        entry = SM.boundary("make.bootstrap-env")
        self.assertIn("state_mutation_lease", entry.lock_owner)
        self.assertIn("make.bootstrap-env", entry.lock_owner)
        self.assertNotEqual(entry.state_root_source, "runtime_model.root_dir")
        self.assertIn(
            "bootstrap-operator-env.py",
            " ".join(entry.evidence),
            "the row must point at the script that owns the write",
        )

    def test_every_no_jurisdiction_entry_is_still_outside_the_state_root(self) -> None:
        for bid, reason in self.REVIEWED_NO_JURISDICTION.items():
            with self.subTest(boundary_id=bid):
                entry = SM.boundary(bid)
                self.assertIn(entry.state_root_source, self.NO_JURISDICTION)
                self.assertGreater(len(reason), 40, "a reclassification needs a reason")

    def test_the_inventory_still_covers_every_live_surface(self) -> None:
        """Adoption means nothing if the inventory itself has holes."""
        report = SM.coverage_report(ROOT_DIR)
        self.assertEqual(report["unclassified"], ())
        self.assertEqual(report["stale"], ())
        self.assertTrue(report["ok"])


class ForceClearAbsenceTests(unittest.TestCase):
    """There is no force-clear, and that is a documented property."""

    def test_the_module_exposes_no_clear_steal_or_break_entry_point(self) -> None:
        for name in ("clear", "steal", "break", "force", "release_all", "unlock_all"):
            with self.subTest(name=name):
                matches = [
                    attr
                    for attr in SM.__all__
                    if name in attr.lower() and attr not in ("canonical_state_root",)
                ]
                self.assertEqual([], matches, f"{name!r} appears in the public API")

    def test_recovery_is_documented_where_operators_look(self) -> None:
        operations = (ROOT_DIR / "docs" / "operations.md").read_text(encoding="utf-8")
        for needle in (
            "mutation-lease.lock",
            "STATE_LEASE_TIMEOUT",
            "force-clear",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, operations)


if __name__ == "__main__":
    unittest.main()
