"""Contract tests for the canonical local self-test gate (skillbox-6r53).

Three things are locked down here:

1. **Trust boundary** - ``.github/workflows/ci.yml`` keeps ``pull_request`` and
   ``workflow_dispatch`` and has *no* ``push`` trigger, while
   ``.github/workflows/release.yml`` keeps its ``v*`` tag trigger, its
   ``id-token: write`` OIDC permission, and the pinned Sigstore identity that
   ``install.sh`` verifies against.
2. **No weakening** - the pins and Python matrix inside ``scripts/self-test.sh``
   are asserted equal to the hosted workflow's, lane by lane, so the local gate
   can never quietly become a smaller matrix than the hosted one.
3. **Fail closed** - the gate is executed for real against a throwaway git repo
   with a stubbed pinned toolchain, once clean and then once per planted
   failure (Ruff, ShellCheck, render, unit, coverage, Python-version, Compose).
   Every planted failure must produce a non-zero exit and a receipt that names
   the failing lane.

The stubbed toolchain only replaces the *tools*; the gate's own orchestration
(exact-SHA resolution, isolated clone, build-once caching, receipt, retention)
runs unmodified.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
GATE = ROOT_DIR / "scripts" / "self-test.sh"
PRE_PUSH = ROOT_DIR / ".githooks" / "pre-push"
CI_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "release.yml"
MAKEFILE = ROOT_DIR / "Makefile"

LANE_IDS = [
    "lint",
    "shellcheck",
    "render",
    "test-3.11",
    "test-3.12-coverage",
    "test-3.13",
    "compose",
]


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    """Return the ``on:`` block. PyYAML resolves the bare key ``on`` to True."""
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


class WorkflowTriggerContractTests(unittest.TestCase):
    """PC-SBX-3: hosted CI keeps the untrusted and manual paths, loses push."""

    def setUp(self) -> None:
        self.ci = _load_workflow(CI_WORKFLOW)
        self.ci_on = _triggers(self.ci)

    def test_pull_request_trigger_is_retained(self) -> None:
        self.assertIn("pull_request", self.ci_on)

    def test_workflow_dispatch_trigger_is_present_for_manual_recovery(self) -> None:
        self.assertIn("workflow_dispatch", self.ci_on)

    def test_push_trigger_is_absent(self) -> None:
        self.assertNotIn(
            "push",
            self.ci_on,
            "trusted-main push CI must be owned by scripts/self-test.sh, not Actions",
        )

    def test_ci_permissions_stay_read_only(self) -> None:
        self.assertEqual({"contents": "read"}, self.ci["permissions"])

    def test_every_hosted_job_is_retained(self) -> None:
        self.assertEqual(
            {"lint", "shellcheck", "render", "test", "compose"},
            set(self.ci["jobs"]),
        )

    def test_hosted_python_matrix_is_not_reduced(self) -> None:
        matrix = self.ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
        self.assertEqual(["3.11", "3.12", "3.13"], matrix)

    def test_hosted_shellcheck_covers_scripts_installer_and_hooks(self) -> None:
        steps = self.ci["jobs"]["shellcheck"]["steps"]
        run_step = next(s for s in steps if s.get("name") == "Run ShellCheck")
        self.assertIn("scripts/*.sh install.sh", run_step["run"])
        self.assertIn(".githooks/pre-commit .githooks/pre-push", run_step["run"])

    def test_hosted_coverage_threshold_is_not_reduced(self) -> None:
        steps = self.ci["jobs"]["test"]["steps"]
        coverage_step = next(s for s in steps if s.get("name") == "Run coverage")
        self.assertIn("--fail-under=80", coverage_step["run"])


class ReleaseWorkflowUnchangedTests(unittest.TestCase):
    """The signing/release trust boundary must survive the CI cutover intact."""

    def setUp(self) -> None:
        self.release = _load_workflow(RELEASE_WORKFLOW)
        self.release_on = _triggers(self.release)
        self.text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    def test_tag_trigger_is_unchanged(self) -> None:
        self.assertEqual(["v*"], self.release_on["push"]["tags"])

    def test_manual_release_trigger_is_unchanged(self) -> None:
        self.assertIn("workflow_dispatch", self.release_on)
        self.assertIn("tag", self.release_on["workflow_dispatch"]["inputs"])

    def test_oidc_and_contents_permissions_are_unchanged(self) -> None:
        self.assertEqual(
            {"contents": "write", "id-token": "write"},
            self.release["permissions"],
        )

    def test_pinned_sigstore_identity_is_unchanged(self) -> None:
        self.assertIn(
            r"^https://github.com/${GITHUB_REPOSITORY}/\.github/workflows/release\.yml@refs/tags/",
            self.text,
        )
        self.assertIn("https://token.actions.githubusercontent.com", self.text)

    def test_installer_identity_regexp_still_points_at_release_workflow(self) -> None:
        installer = (ROOT_DIR / "install.sh").read_text(encoding="utf-8")
        self.assertIn(
            r"workflows/release\\.yml@refs/tags/",
            installer,
            "install.sh must keep verifying the release workflow's OIDC identity",
        )

    def test_deterministic_source_tarball_step_is_unchanged(self) -> None:
        self.assertIn("bash scripts/make-release-tarball.sh", self.text)


class PinParityTests(unittest.TestCase):
    """SC: the local gate may strengthen the hosted matrix, never weaken it."""

    @classmethod
    def setUpClass(cls) -> None:
        result = subprocess.run(
            ["bash", str(GATE), "--print-pins"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        cls.pins = json.loads(result.stdout)
        cls.ci = _load_workflow(CI_WORKFLOW)

    def test_tool_pins_match_the_hosted_workflow_exactly(self) -> None:
        env = self.ci["env"]
        self.assertEqual(env["RUFF_VERSION"], self.pins["ruff"])
        self.assertEqual(env["SHELLCHECK_PY_VERSION"], self.pins["shellcheck_py"])
        self.assertEqual(env["COVERAGE_VERSION"], self.pins["coverage"])
        self.assertEqual(env["PYYAML_VERSION"], self.pins["pyyaml"])
        self.assertEqual(env["CRYPTOGRAPHY_VERSION"], self.pins["cryptography"])

    def test_python_matrix_matches_the_hosted_workflow_exactly(self) -> None:
        matrix = self.ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
        self.assertEqual(matrix, self.pins["python_versions"])

    def test_coverage_threshold_matches_the_hosted_workflow(self) -> None:
        self.assertEqual("80", self.pins["coverage_fail_under"])
        self.assertEqual("3.12", self.pins["coverage_python"])

    def test_pins_are_literal_versions_not_ranges(self) -> None:
        for key in ("ruff", "shellcheck_py", "coverage", "pyyaml", "cryptography"):
            self.assertRegex(self.pins[key], r"^\d+(\.\d+)*$", f"{key} is not pinned")

    def test_gate_covers_every_hosted_lane(self) -> None:
        text = GATE.read_text(encoding="utf-8")
        self.assertIn("ruff", text)
        self.assertIn("--severity=warning scripts/*.sh install.sh", text)
        self.assertIn(".githooks/pre-commit .githooks/pre-push", text)
        self.assertIn("scripts/04-reconcile.py render", text)
        self.assertIn("-m unittest discover -s tests", text)
        self.assertIn("--source=scripts,.env-manager", text)
        self.assertIn("docker compose --env-file .env.example", text)


class MakefileAndHookWiringTests(unittest.TestCase):
    def test_makefile_exposes_the_canonical_gate(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("self-test:", text)
        self.assertIn("./scripts/self-test.sh", text)

    def test_install_hooks_installs_the_pre_push_gate(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(".githooks/pre-push", text)
        self.assertIn("git config core.hooksPath .githooks", text)

    def test_pre_push_is_executable_and_delegates_to_the_gate(self) -> None:
        self.assertTrue(os.access(PRE_PUSH, os.X_OK), "pre-push must be executable")
        text = PRE_PUSH.read_text(encoding="utf-8")
        self.assertIn("scripts/self-test.sh", text)
        self.assertIn("--trigger pre-push", text)

    def test_pre_push_has_no_env_var_bypass(self) -> None:
        text = PRE_PUSH.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\$\{?[A-Z_]*SKIP[A-Z_]*")

    def test_gate_is_executable(self) -> None:
        self.assertTrue(os.access(GATE, os.X_OK), "self-test.sh must be executable")

    def test_docs_describe_the_local_gate(self) -> None:
        agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
        operations = (ROOT_DIR / "docs" / "operations.md").read_text(encoding="utf-8")
        self.assertIn("scripts/self-test.sh", agents)
        self.assertIn("scripts/self-test.sh", operations)


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


FAKE_PY_TEMPLATE = """
#!/usr/bin/env bash
set -uo pipefail
version="__VERSION__"
plant=",${SELFTEST_PLANT:-},"
args="$*"

if [[ "${args}" == *coverage* ]]; then
  if [[ "${args}" == *report* && "${plant}" == *,coverage,* ]]; then
    echo "TOTAL 12%  (planted coverage regression)"
    exit 2
  fi
  if [[ "${args}" == *run* && "${plant}" == *,unit-${version},* ]]; then
    echo "planted unit failure on ${version}" >&2
    exit 1
  fi
  exit 0
fi

if [[ "${args}" == *"-m unittest"* ]]; then
  if [[ "${plant}" == *,unit-${version},* ]]; then
    echo "planted unit failure on ${version}" >&2
    exit 1
  fi
  echo "OK (${version})"
  exit 0
fi

if [[ "${args}" == *04-reconcile.py*render* ]]; then
  if [[ "${plant}" == *,render,* ]]; then
    echo "planted render failure" >&2
    exit 1
  fi
  echo '{"sandbox": {}}'
  exit 0
fi

if [[ "${args}" == *"import sys"* ]]; then
  echo "${version}"
  exit 0
fi

exit 0
"""

FAKE_RUFF = """
#!/usr/bin/env bash
set -uo pipefail
if [[ ",${SELFTEST_PLANT:-}," == *,lint,* ]]; then
  echo "planted.py:1:1: F401 planted ruff violation" >&2
  exit 1
fi
echo "All checks passed!"
"""

FAKE_SHELLCHECK = """
#!/usr/bin/env bash
set -uo pipefail
if [[ ",${SELFTEST_PLANT:-}," == *,shellcheck,* ]]; then
  echo "In planted.sh line 1: SC2086 planted shellcheck violation" >&2
  exit 1
fi
"""

FAKE_DOCKER = """
#!/usr/bin/env bash
set -uo pipefail
if [[ "${1:-}" == "compose" ]]; then
  if [[ ",${SELFTEST_PLANT:-}," == *,compose,* ]]; then
    echo "planted compose validation error" >&2
    exit 1
  fi
  exit 0
fi
exit 0
"""


@unittest.skipUnless(shutil.which("git"), "git is required")
class GateBehaviorTests(unittest.TestCase):
    """Run the real gate against a throwaway repo with a stubbed toolchain."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.repo = root / "fake-repo"
        cls.toolchain = root / "toolchain"
        cls.stub_bin = root / "stub-bin"
        cls._build_repo(cls.repo)
        cls._build_toolchain(cls.toolchain)
        _write_exec(cls.stub_bin / "docker", FAKE_DOCKER)
        cls.head_sha = subprocess.run(
            ["git", "-C", str(cls.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def _build_repo(cls, repo: Path) -> None:
        repo.mkdir(parents=True)
        (repo / "scripts").mkdir(exist_ok=True)
        shutil.copy2(GATE, repo / "scripts" / "self-test.sh")
        (repo / "scripts" / "self-test.sh").chmod(0o755)
        (repo / "scripts" / "04-reconcile.py").write_text("print('{}')\n", encoding="utf-8")
        (repo / "install.sh").write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
        (repo / ".env.example").write_text("FOO=bar\n", encoding="utf-8")
        (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (repo / "docker-compose.monoserver.yml").write_text("services: {}\n", encoding="utf-8")
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_placeholder.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
            encoding="utf-8",
        )
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        for args in (
            ["config", "user.email", "gate@example.invalid"],
            ["config", "user.name", "Gate Test"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "fixture"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)

    @classmethod
    def _build_toolchain(cls, toolchain: Path) -> None:
        _write_exec(toolchain / "bin" / "ruff", FAKE_RUFF)
        _write_exec(toolchain / "bin" / "shellcheck", FAKE_SHELLCHECK)
        for version in ("3.11", "3.12", "3.13"):
            _write_exec(
                toolchain / "py" / version / "bin" / "python",
                FAKE_PY_TEMPLATE.replace("__VERSION__", version),
            )
        fingerprint = subprocess.run(
            ["bash", str(GATE), "--print-toolchain-fingerprint"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (toolchain / "stamp").write_text(fingerprint + "\n", encoding="utf-8")

    def _run_gate(
        self,
        *extra: str,
        plant: str = "",
        toolchain: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        receipts = Path(tempfile.mkdtemp(dir=self._tmp.name))
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_bin}:{env['PATH']}"
        env["SKILLBOX_SELF_TEST_TOOLCHAIN_DIR"] = str(toolchain or self.toolchain)
        env["SKILLBOX_SELF_TEST_RECEIPT_DIR"] = str(receipts)
        env["SELFTEST_PLANT"] = plant
        env.pop("SKILLBOX_STATE_ROOT", None)
        result = subprocess.run(
            ["bash", str(self.repo / "scripts" / "self-test.sh"), *extra],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=env,
        )
        return result, receipts

    def _receipt(self, receipts: Path) -> dict:
        latest = receipts / "latest.json"
        self.assertTrue(latest.exists(), "gate must always write a receipt")
        return json.loads(latest.read_text(encoding="utf-8"))

    # --- clean run ----------------------------------------------------------

    def test_clean_run_passes_and_emits_a_sha_bound_receipt(self) -> None:
        result, receipts = self._run_gate("--trigger", "unittest")
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = self._receipt(receipts)
        self.assertEqual("pass", receipt["status"])
        self.assertEqual(self.head_sha, receipt["commit"])
        self.assertEqual(40, len(receipt["commit"]))
        self.assertTrue(receipt["canonical"])
        self.assertEqual("git-archive", receipt["source_mode"])
        self.assertEqual("unittest", receipt["trigger"])
        self.assertEqual([], receipt["failed_lanes"])
        self.assertEqual(LANE_IDS, [lane["id"] for lane in receipt["lanes"]])

    def test_receipt_is_written_per_sha_and_redacts_home(self) -> None:
        _, receipts = self._run_gate()
        sha_bound = [p for p in receipts.iterdir() if p.name != "latest.json"]
        self.assertEqual(1, len(sha_bound))
        self.assertTrue(sha_bound[0].name.startswith(self.head_sha))
        home = os.environ.get("HOME", "")
        if home and len(home) > 1:
            self.assertNotIn(home, sha_bound[0].read_text(encoding="utf-8"))

    def test_gate_runs_against_isolated_source_not_the_worktree(self) -> None:
        # A dirty worktree must not leak into a default (canonical) run.
        planted = self.repo / "scripts" / "planted_dirty.sh"
        planted.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        try:
            result, receipts = self._run_gate()
            self.assertEqual(0, result.returncode, result.stderr)
            receipt = self._receipt(receipts)
            self.assertFalse(receipt["worktree_clean"])
            self.assertTrue(receipt["canonical"])
            self.assertEqual(self.head_sha, receipt["commit"])
        finally:
            planted.unlink()

    def test_worktree_mode_is_marked_non_canonical(self) -> None:
        result, receipts = self._run_gate("--worktree")
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = self._receipt(receipts)
        self.assertFalse(receipt["canonical"])
        self.assertIn("source-mode=worktree-overlay", receipt["non_canonical_reason"])

    def test_lane_subset_is_marked_non_canonical(self) -> None:
        result, receipts = self._run_gate("--lane", "lint")
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = self._receipt(receipts)
        self.assertFalse(receipt["canonical"])
        self.assertEqual(["lint"], [lane["id"] for lane in receipt["lanes"]])

    def test_unknown_revision_fails_closed(self) -> None:
        result, _ = self._run_gate("--rev", "definitely-not-a-ref")
        self.assertEqual(2, result.returncode)
        self.assertIn("cannot resolve", result.stderr)

    # --- planted failures ---------------------------------------------------

    def _assert_planted(self, plant: str, lane: str) -> None:
        result, receipts = self._run_gate(plant=plant)
        self.assertEqual(
            1,
            result.returncode,
            f"planted {plant} failure must fail the gate closed:\n{result.stderr}",
        )
        receipt = self._receipt(receipts)
        self.assertEqual("fail", receipt["status"])
        self.assertIn(lane, receipt["failed_lanes"])
        failing = next(item for item in receipt["lanes"] if item["id"] == lane)
        self.assertNotEqual(0, failing["exit_code"])

    def test_planted_ruff_failure_fails_closed(self) -> None:
        self._assert_planted("lint", "lint")

    def test_planted_shellcheck_failure_fails_closed(self) -> None:
        self._assert_planted("shellcheck", "shellcheck")

    def test_planted_render_failure_fails_closed(self) -> None:
        self._assert_planted("render", "render")

    def test_planted_unit_failure_fails_closed_on_every_python(self) -> None:
        self._assert_planted("unit-3.11", "test-3.11")
        self._assert_planted("unit-3.13", "test-3.13")
        self._assert_planted("unit-3.12", "test-3.12-coverage")

    def test_planted_coverage_regression_fails_closed(self) -> None:
        self._assert_planted("coverage", "test-3.12-coverage")

    def test_planted_compose_failure_fails_closed(self) -> None:
        self._assert_planted("compose", "compose")

    def test_missing_pinned_interpreter_fails_closed_instead_of_skipping(self) -> None:
        # "Python-version" planted failure: a reduced matrix is the exact
        # false-confidence mode this gate exists to prevent. With 3.13 gone the
        # gate must re-provision; when re-provisioning is impossible it must
        # abort without running a single lane, and without writing a receipt.
        reduced = Path(tempfile.mkdtemp(dir=self._tmp.name)) / "toolchain"
        shutil.copytree(self.toolchain, reduced, symlinks=True)
        shutil.rmtree(reduced / "py" / "3.13")

        stub_path = Path(tempfile.mkdtemp(dir=self._tmp.name))
        _write_exec(stub_path / "docker", FAKE_DOCKER)
        _write_exec(
            stub_path / "uv",
            """
            #!/usr/bin/env bash
            echo "stub uv refusing to provision" >&2
            exit 7
            """,
        )
        receipts = Path(tempfile.mkdtemp(dir=self._tmp.name))
        env = dict(os.environ)
        env["PATH"] = f"{stub_path}:{env['PATH']}"
        env["SKILLBOX_SELF_TEST_TOOLCHAIN_DIR"] = str(reduced)
        env["SKILLBOX_SELF_TEST_RECEIPT_DIR"] = str(receipts)
        env["SELFTEST_PLANT"] = ""
        env.pop("SKILLBOX_STATE_ROOT", None)
        result = subprocess.run(
            ["bash", str(self.repo / "scripts" / "self-test.sh")],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=env,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("stub uv refusing to provision", result.stderr)
        self.assertNotIn("PASS", result.stderr)
        self.assertFalse((receipts / "latest.json").exists())

    def test_gate_refuses_to_run_lanes_without_the_full_python_matrix(self) -> None:
        # Direct proof of the post-provision guard: even if provisioning claims
        # success, a missing pinned interpreter aborts the run.
        reduced = Path(tempfile.mkdtemp(dir=self._tmp.name)) / "toolchain"
        shutil.copytree(self.toolchain, reduced, symlinks=True)
        shutil.rmtree(reduced / "py" / "3.13")

        stub_path = Path(tempfile.mkdtemp(dir=self._tmp.name))
        _write_exec(stub_path / "docker", FAKE_DOCKER)
        # A lying uv: exits 0 but provisions nothing.
        _write_exec(stub_path / "uv", "#!/usr/bin/env bash\nexit 0\n")
        receipts = Path(tempfile.mkdtemp(dir=self._tmp.name))
        env = dict(os.environ)
        env["PATH"] = f"{stub_path}:{env['PATH']}"
        env["SKILLBOX_SELF_TEST_TOOLCHAIN_DIR"] = str(reduced)
        env["SKILLBOX_SELF_TEST_RECEIPT_DIR"] = str(receipts)
        env["SELFTEST_PLANT"] = ""
        env.pop("SKILLBOX_STATE_ROOT", None)
        result = subprocess.run(
            ["bash", str(self.repo / "scripts" / "self-test.sh")],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=env,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("3.13", result.stderr)
        self.assertFalse((receipts / "latest.json").exists())

    def test_stale_toolchain_stamp_triggers_reprovisioning(self) -> None:
        # Build-once caching must be keyed on the pin fingerprint: a stale stamp
        # must not be reused, even though every stub binary is present.
        stale = Path(tempfile.mkdtemp(dir=self._tmp.name)) / "toolchain"
        shutil.copytree(self.toolchain, stale, symlinks=True)
        (stale / "stamp").write_text("stale-fingerprint\n", encoding="utf-8")

        # Shadow uv with a refusing stub: re-provisioning must be attempted and
        # its failure must abort the run, rather than falling back to the cache.
        stub_path = Path(tempfile.mkdtemp(dir=self._tmp.name))
        _write_exec(stub_path / "docker", FAKE_DOCKER)
        _write_exec(
            stub_path / "uv",
            """
            #!/usr/bin/env bash
            echo "stub uv refusing to provision" >&2
            exit 7
            """,
        )
        env = dict(os.environ)
        env["PATH"] = f"{stub_path}:{env['PATH']}"
        env["SKILLBOX_SELF_TEST_TOOLCHAIN_DIR"] = str(stale)
        env["SKILLBOX_SELF_TEST_RECEIPT_DIR"] = str(
            Path(tempfile.mkdtemp(dir=self._tmp.name))
        )
        env["SELFTEST_PLANT"] = ""
        env.pop("SKILLBOX_STATE_ROOT", None)
        result = subprocess.run(
            ["bash", str(self.repo / "scripts" / "self-test.sh")],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=env,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("stub uv refusing to provision", result.stderr)


@unittest.skipUnless(shutil.which("git"), "git is required")
class PrePushBehaviorTests(unittest.TestCase):
    """The hook must block on gate failure and gate the exact pushed SHA."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=env)
        self.calls = self.repo / "gate-calls.txt"
        self.hook = self.repo / ".githooks" / "pre-push"
        self.hook.parent.mkdir()
        shutil.copy2(PRE_PUSH, self.hook)
        self.hook.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install_gate(self, exit_code: int) -> None:
        _write_exec(
            self.repo / "scripts" / "self-test.sh",
            f"""
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >>"{self.calls}"
            exit {exit_code}
            """,
        )

    def _run_hook(self, stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.hook), "origin", "git@example.invalid:x/y.git"],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            cwd=self.repo,
            timeout=60,
        )

    def test_hook_gates_the_exact_pushed_sha(self) -> None:
        self._install_gate(0)
        sha = "a" * 40
        result = self._run_hook(f"refs/heads/main {sha} refs/heads/main {'b' * 40}\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"--rev {sha} --trigger pre-push", self.calls.read_text())

    def test_hook_blocks_the_push_when_the_gate_fails(self) -> None:
        self._install_gate(1)
        result = self._run_hook(f"refs/heads/main {'c' * 40} refs/heads/main {'0' * 40}\n")
        self.assertEqual(1, result.returncode)
        self.assertIn("push blocked", result.stderr)

    def test_hook_skips_deletions(self) -> None:
        self._install_gate(0)
        result = self._run_hook(f"(delete) {'0' * 40} refs/heads/gone {'d' * 40}\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(self.calls.exists(), "deletions must not invoke the gate")

    def test_hook_deduplicates_repeated_shas(self) -> None:
        self._install_gate(0)
        sha = "e" * 40
        stdin = (
            f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
            f"refs/tags/v1 {sha} refs/tags/v1 {'0' * 40}\n"
        )
        result = self._run_hook(stdin)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(self.calls.read_text().strip().splitlines()))

    def test_hook_refuses_when_the_gate_is_missing(self) -> None:
        result = self._run_hook(f"refs/heads/main {'f' * 40} refs/heads/main {'0' * 40}\n")
        self.assertEqual(1, result.returncode)
        self.assertIn("refusing to push ungated", result.stderr)


class ReceiptRetentionTests(unittest.TestCase):
    def test_gate_declares_a_bounded_receipt_retention(self) -> None:
        text = GATE.read_text(encoding="utf-8")
        match = re.search(r'RECEIPT_RETENTION="(\d+)"', text)
        self.assertIsNotNone(match, "gate must declare RECEIPT_RETENTION")
        self.assertGreater(int(match.group(1)), 0)

    def test_gate_serializes_concurrent_runs(self) -> None:
        text = GATE.read_text(encoding="utf-8")
        self.assertIn("flock", text)


if __name__ == "__main__":
    unittest.main()
