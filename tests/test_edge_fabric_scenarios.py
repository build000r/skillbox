"""Acceptance scenarios S1–S12 for the machine-placement fabric.

End-to-end across placement.py, box.py place/list/down, and the worker
grant-persist path. Fixture machines.yaml + fake boxes/profiles. No network,
no doctl, no real SSH. Does not restub unit coverage from test_placement /
test_box_place / test_worker_placement.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import machines as M  # noqa: E402
from runtime_manager import placement as P  # noqa: E402
from runtime_manager._shared import worker as W  # noqa: E402

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

BOX = SourceFileLoader(
    "skillbox_box_edge_fabric_scenarios",
    str((ROOT_DIR / "scripts" / "box.py").resolve()),
).load_module()

SECRET_TOKEN = "do_secret_token_xyz_should_never_leak"
SECRET_AUTHKEY = "tskey-auth-xyz-should-never-leak"

FIXTURE_YAML = textwrap.dedent(
    """
    version: 1
    machines:
      mac-laptop:
        hostnames: [Mac-2]
        caps: [os:darwin, arch:arm64, xcode, durable]
        trust: local
      portfolio-devbox:
        hostnames: [portfolio-devbox]
        caps: [os:linux, arch:amd64, docker, tailnet, durable]
        trust: allowlisted
      conference1-wsl:
        hostnames: [conference1]
        caps: [os:wsl, arch:amd64, docker, durable]
        trust: allowlisted
      explicit-lab:
        hostnames: [explicit-lab]
        caps: [os:linux, docker, durable]
        trust: explicit
    """
).strip()

PROVISION_LINE = (
    "python3 scripts/box.py up dev-small --profile dev-small --dry-run --format json"
)


def _box(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": "",
        "profile": "dev-small",
        "state": "ready",
        "size": "",
        "management_mode": "managed",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _profile(profile_id: str, **kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": profile_id,
        "image": "ubuntu-24-04-x64",
        "size": "s-2vcpu-4gb",
        "provider": "asciibox",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fleet_boxes() -> list[SimpleNamespace]:
    return [
        _box(
            id="portfolio-devbox",
            profile="dev-large",
            size="s-8vcpu-32gb-amd",
            management_mode="managed",
        ),
        _box(
            id="jeremy",
            profile="dev-small",
            size="s-2vcpu-4gb",
            management_mode="managed",
        ),
        _box(
            id="shared-host",
            profile="dev-small",
            size="s-2vcpu-4gb",
            management_mode="external",
        ),
    ]


def _profiles() -> list[SimpleNamespace]:
    return [
        _profile("dev-small", size="s-2vcpu-4gb"),
        _profile("dev-xl", size="s-8vcpu-16gb"),
        _profile("dev-large", size="s-8vcpu-32gb-amd"),
    ]


def _payload_text(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _active_worker_model(repo_root: str = "/tmp/skills") -> dict[str, object]:
    return {
        "active_profiles": ["core"],
        "clients": [
            {
                "id": "skills",
                "default_cwd_host_path": repo_root,
                "context": {
                    "deploy": {
                        "repo_root": repo_root,
                        "repo_slug": "example/skills",
                    }
                },
            }
        ],
        "repos": [{"id": "skills-repo", "host_path": repo_root}],
    }


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class EdgeFabricScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.machines_path = self.root / "machines.yaml"
        self.machines_path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
        self.config = M.load_machines_config(self.machines_path)
        self.boxes = _fleet_boxes()
        self.profiles = _profiles()
        self._env = mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_MACHINES_FILE": str(self.machines_path),
                "SKILLBOX_MACHINE": "mac-laptop",
                "SKILLBOX_DO_TOKEN": SECRET_TOKEN,
                "SKILLBOX_TS_AUTHKEY": SECRET_AUTHKEY,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _decide(
        self,
        needs: dict,
        *,
        boxes: list | None = None,
        observations: dict | None = None,
        profiles: list | None = None,
        current_id: str | None = "mac-laptop",
    ) -> dict:
        live_boxes = boxes if boxes is not None else self.boxes
        if observations is None:
            observations = P.gather_observations(current_id, live_boxes)
        return P.decide(
            needs,
            self.config,
            live_boxes,
            observations,
            profiles if profiles is not None else self.profiles,
            current_id,
        )

    def _rejected(self, result: dict) -> dict[str, list[str]]:
        return {row["id"]: list(row["reasons"]) for row in result["rejected"]}

    def _candidate_ids(self, result: dict) -> set[str]:
        ids = self._rejected(result).keys()
        selected = result.get("machine_id")
        accounted = set(ids)
        if selected:
            accounted.add(selected)
        return accounted

    def test_s1_xcode_selects_mac_without_provisioning(self) -> None:
        decision = self._decide({"caps": ["os:darwin", "xcode"]})
        self.assertEqual(decision["decision"], "selected")
        self.assertEqual(decision["machine_id"], "mac-laptop")
        self.assertEqual(decision["next_actions"], [])
        self.assertNotIn("provision_proposed", decision["reasons"])

        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "list_profiles", return_value=self.profiles), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append), \
            mock.patch.object(BOX, "save_inventory") as save, \
            mock.patch.object(BOX, "do_create_droplet", create=True) as create:
            rc = BOX.cmd_place(needs=["os:darwin", "xcode"], fmt="json")
        self.assertEqual(rc, BOX.EXIT_OK)
        placed = payloads[-1]
        self.assertTrue(placed["ok"])
        self.assertEqual(placed["kind"], "machine-placement/v1")
        self.assertEqual(placed["decision"], "selected")
        self.assertEqual(placed["machine_id"], "mac-laptop")
        self.assertEqual(placed["next_actions"], [])
        save.assert_not_called()
        create.assert_not_called()

    def test_s2_linux_docker_selects_persistent(self) -> None:
        decision = self._decide({"caps": ["os:linux", "docker"]})
        self.assertEqual(decision["decision"], "selected")
        self.assertEqual(decision["machine_id"], "portfolio-devbox")
        self.assertEqual(decision["box_id"], "portfolio-devbox")
        self.assertNotEqual(decision["machine_id"], "jeremy")

        view_rows = {
            row["id"]: row
            for row in P.machine_view(self.config, self.boxes, self.profiles)
        }
        self.assertIn("docker", view_rows["portfolio-devbox"]["caps"])
        kind = BOX._derive_machine_view_kind(  # noqa: SLF001
            view_rows["portfolio-devbox"],
            next(box for box in self.boxes if box.id == "portfolio-devbox"),
        )
        self.assertEqual(kind, "persistent")

        inventory = [
            BOX.Box(
                id="portfolio-devbox",
                profile="dev-large",
                state="ready",
                management_mode="managed",
            ),
            BOX.Box(
                id="jeremy",
                profile="dev-small",
                state="ready",
                management_mode="managed",
            ),
        ]
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=inventory), \
            mock.patch.object(BOX, "list_profiles", return_value=self.profiles), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            BOX.cmd_list(fmt="json")
        listed = {row["id"]: row for row in payloads[-1]["machine_view"]}
        self.assertEqual(listed["portfolio-devbox"]["kind"], "persistent")
        self.assertEqual(listed["jeremy"]["kind"], "ephemeral")

    def test_s3_provision_only_as_fallback(self) -> None:
        needs = {"caps": ["os:linux", "arch:amd64"], "allow_provision": False}
        forbidden = self._decide(needs, boxes=[], current_id="mac-laptop")
        self.assertEqual(forbidden["decision"], "no_match")
        self.assertEqual(forbidden["next_actions"], [])
        self.assertIsNone(forbidden["machine_id"])

        allowed_needs = {"caps": ["os:linux", "arch:amd64"], "allow_provision": True}
        first = self._decide(allowed_needs, boxes=[], current_id="mac-laptop")
        os.environ["SKILLBOX_MACHINE"] = "should-not-change-pure-decide"
        second = self._decide(allowed_needs, boxes=[], current_id="mac-laptop")
        self.assertEqual(first, second)
        self.assertEqual(first["decision"], "provision_proposed")
        self.assertEqual(first["next_actions"], [PROVISION_LINE])
        self.assertFalse((self.root / "boxes.json").exists())

    def test_s4_offline_mac_explained_rejection(self) -> None:
        observations = {
            "mac-laptop": {"reachable": False, "source": "probe"},
            "portfolio-devbox": {"reachable": True, "source": "inventory-state"},
        }
        result = self._decide(
            {"caps": ["xcode"]},
            observations=observations,
            current_id=None,
        )
        self.assertEqual(result["decision"], "no_match")
        self.assertEqual(result["next_actions"], [])
        self.assertIn("unreachable", self._rejected(result)["mac-laptop"])
        self.assertIn("missing_caps:xcode", self._rejected(result)["portfolio-devbox"])

    def test_s5_external_teardown_and_unsupported_provider(self) -> None:
        shared = BOX.Box(
            id="shared-host",
            profile="shared",
            state="ready",
            management_mode="external",
        )
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[shared]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append), \
            mock.patch.object(BOX, "save_inventory") as save, \
            mock.patch.object(BOX, "do_delete_droplet", create=True) as delete:
            rc = BOX.cmd_down(
                "shared-host",
                dry_run=False,
                fmt="json",
                confirmed=True,
            )
        self.assertEqual(rc, BOX.EXIT_ERROR)
        error = payloads[-1]["error"]
        self.assertEqual(error["type"], "invalid_state")
        self.assertIn("cannot be torn down", error["message"])
        self.assertIn("unregister", error["message"])
        save.assert_not_called()
        delete.assert_not_called()

        profile = BOX.BoxProfile(id="ascii-1", provider="asciibox")
        with self.assertRaises(RuntimeError) as raised:
            BOX.require_profile_storage(profile)
        message = str(raised.exception)
        self.assertIn("Unsupported box provider", message)
        self.assertIn("asciibox", message)

    def test_s6_duplicate_dispatch(self) -> None:
        launches: list[object] = []

        def _no_launch(root_dir, paths, payload):  # type: ignore[no-untyped-def]
            launches.append(payload["run_id"])
            return payload

        with mock.patch.object(W, "build_runtime_model", return_value=_active_worker_model()), \
            mock.patch.object(W, "_launch_worker_if_ready", side_effect=_no_launch):
            first = W.create_worker_run(
                self.root,
                task_class="analysis",
                instruction="Place once.",
                client_id="skills",
                cwd="/tmp/skills/docs",
                needs=["xcode"],
                idempotency_key="job-s6",
            )
            second = W.create_worker_run(
                self.root,
                task_class="analysis",
                instruction="Place again.",
                client_id="skills",
                cwd="/tmp/skills/docs",
                needs=["xcode"],
                idempotency_key="job-s6",
            )
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(len(launches), 1)
            with self.assertRaises(W.WorkerRuntimeError) as raised:
                W.create_worker_run(
                    self.root,
                    task_class="analysis",
                    instruction="Overwrite.",
                    client_id="skills",
                    cwd="/tmp/skills/docs",
                    run_id=first["run_id"],
                )
            self.assertEqual(raised.exception.code, W.WORKER_RUN_EXISTS)

    def test_s7_interrupted_execution_dead_pid(self) -> None:
        run_id = "wr_20260813_000000_a7dead"
        paths = W.worker_run_paths(self.root, run_id)
        paths["run_dir"].mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "runtime": "hermes",
            "state": "running",
            "run": {"state": "running", "blocked_reason": None},
            "launch": {
                "attempted": True,
                "pid": 99999999,
                "command": [sys.executable, "-c", "raise SystemExit(1)"],
            },
            "placement": {
                "kind": "machine-placement/v1",
                "decision": "selected",
                "machine_id": "mac-laptop",
            },
            "result": None,
        }
        paths["run_path"].write_text(json.dumps(payload), encoding="utf-8")
        before = list(paths["runs_root"].glob("*/run.json")) if paths["runs_root"].is_dir() else []
        status = W.worker_status_payload(self.root, run_id)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["placement"]["machine_id"], "mac-laptop")
        persisted = json.loads(paths["run_path"].read_text(encoding="utf-8"))
        self.assertEqual(persisted["result"]["error"]["type"], W.WORKER_LAUNCH_FAILED)
        self.assertIn("exited before writing a result", persisted["result"]["summary"])
        after = list(paths["runs_root"].glob("*/run.json"))
        self.assertEqual(len(after), len(before))

    def test_s8_coordinator_restart_preserves_placement(self) -> None:
        def _no_launch(root_dir, paths, payload):  # type: ignore[no-untyped-def]
            return payload

        with mock.patch.object(W, "build_runtime_model", return_value=_active_worker_model()), \
            mock.patch.object(W, "_launch_worker_if_ready", side_effect=_no_launch):
            created = W.create_worker_run(
                self.root,
                task_class="analysis",
                instruction="Survive restart.",
                client_id="skills",
                cwd="/tmp/skills/docs",
                needs=["os:darwin", "xcode"],
            )
        W._WORKER_ACTIVE_PROCESSES.clear()
        status = W.worker_status_payload(self.root, created["run_id"])
        self.assertEqual(status["placement"]["kind"], "machine-placement/v1")
        self.assertEqual(status["placement"]["machine_id"], "mac-laptop")
        self.assertEqual(status["state"], created["state"])
        disk = json.loads(
            W.worker_run_paths(self.root, created["run_id"])["run_path"].read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(disk["placement"]["machine_id"], "mac-laptop")

    def test_s9_trust_local_rejects_and_no_secrets(self) -> None:
        result = self._decide({"caps": [], "trust": "local"})
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "mac-laptop")
        rejected = self._rejected(result)
        for machine_id in ("portfolio-devbox", "conference1-wsl", "explicit-lab", "jeremy"):
            self.assertIn("trust_below_floor", rejected[machine_id], msg=machine_id)
        dumped = _payload_text(result)
        self.assertNotIn(SECRET_TOKEN, dumped)
        self.assertNotIn(SECRET_AUTHKEY, dumped)
        self.assertNotIn(os.environ["SKILLBOX_DO_TOKEN"], dumped)

        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "list_profiles", return_value=self.profiles), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            BOX.cmd_place(needs=[], need_trust="local", fmt="json")
        self.assertNotIn(SECRET_TOKEN, _payload_text(payloads[-1]))

    def test_s10_classify_remote_result_truth_table(self) -> None:
        table = (
            (0, False, "completed"),
            (255, True, "result_unavailable"),
            (0, True, "result_unavailable"),
            (1, False, "command_failed"),
            (255, False, "command_failed"),
            (None, True, "result_unavailable"),
        )
        for exit_code, transport, expected in table:
            with self.subTest(exit_code=exit_code, transport=transport):
                self.assertEqual(W.classify_remote_result(exit_code, transport), expected)

    def test_s11_no_match_accounts_for_every_candidate(self) -> None:
        result = self._decide({"caps": ["gpu"]})
        self.assertEqual(result["decision"], "no_match")
        accounted = {row["id"] for row in result["rejected"]}
        expected = {
            "mac-laptop",
            "portfolio-devbox",
            "conference1-wsl",
            "explicit-lab",
            "jeremy",
            "shared-host",
        }
        self.assertEqual(accounted, expected)
        for row in result["rejected"]:
            self.assertTrue(row["reasons"], msg=row)

    def test_s12_fake_provider_flows_and_source_is_neutral(self) -> None:
        ascii_profiles = [
            _profile("asciibox-small", provider="asciibox", size="s-2vcpu-4gb"),
        ]
        result = self._decide(
            {"caps": ["os:linux", "arch:amd64"], "allow_provision": True},
            boxes=[],
            profiles=ascii_profiles,
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "provision_proposed")
        self.assertEqual(
            result["next_actions"],
            [
                "python3 scripts/box.py up asciibox-small --profile asciibox-small "
                "--dry-run --format json"
            ],
        )
        source = Path(P.__file__).read_text(encoding="utf-8")
        self.assertNotIn("digitalocean", source)
        self.assertNotIn("DigitalOcean", source)
        dumped = _payload_text(result)
        self.assertNotIn("digitalocean", dumped.lower())


if __name__ == "__main__":
    unittest.main()
