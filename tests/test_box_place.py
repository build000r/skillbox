"""box.py place + list machine_view. Fixture machines.yaml + fake inventory.

No network, no doctl. Isolated from live host identity via SKILLBOX_MACHINES_FILE
and SKILLBOX_MACHINE.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

BOX = SourceFileLoader("skillbox_box_place", str(BOX_SCRIPT.resolve())).load_module()

FIXTURE_YAML = textwrap.dedent(
    """
    version: 1
    machines:
      mac-laptop:
        hostnames: [Mac-2]
        caps: [os:darwin, arch:arm64, xcode, durable]
        trust: local
      local-no-darwin:
        hostnames: [desk]
        caps: [durable]
        trust: local
      portfolio-devbox:
        hostnames: [portfolio-devbox]
        caps: [os:linux, arch:amd64, docker, tailnet, durable]
        trust: allowlisted
      conference1-wsl:
        hostnames: [conference1]
        caps: [os:wsl, arch:amd64, docker, durable]
        trust: allowlisted
    """
).strip()


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class BoxPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.machines_path = Path(self._tmp.name) / "machines.yaml"
        self.machines_path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_MACHINES_FILE": str(self.machines_path),
                "SKILLBOX_MACHINE": "mac-laptop",
            },
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.boxes = [
            BOX.Box(
                id="portfolio-devbox",
                profile="dev-large",
                state="ready",
                size="s-8vcpu-32gb-amd",
                management_mode="managed",
            ),
            BOX.Box(
                id="jeremy",
                profile="dev-small",
                state="ready",
                size="s-2vcpu-4gb",
                management_mode="managed",
            ),
            BOX.Box(
                id="shared-host",
                profile="dev-small",
                state="ready",
                size="s-2vcpu-4gb",
                management_mode="external",
            ),
        ]

    def _place(self, **kwargs):
        payloads: list[dict] = []
        printed: list[str] = []
        defaults = {
            "needs": ["xcode"],
            "need_trust": None,
            "allow_provision": False,
            "allow_unverified": False,
            "fmt": "json",
        }
        defaults.update(kwargs)
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append), \
            mock.patch.object(BOX, "save_inventory") as save, \
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            rc = BOX.cmd_place(**defaults)
        return rc, payloads, printed, save

    def test_place_json_envelope_selects_mac(self) -> None:
        rc, payloads, _, save = self._place(needs=["os:darwin", "xcode"], fmt="json")
        self.assertEqual(rc, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "machine-placement/v1")
        self.assertEqual(payload["decision"], "selected")
        self.assertEqual(payload["machine_id"], "mac-laptop")
        self.assertIn("next_actions", payload)
        self.assertEqual(payload["needs"]["caps"], ["os:darwin", "xcode"])
        save.assert_not_called()

    def test_place_text_uses_decide_tokens(self) -> None:
        rc, _, printed, _ = self._place(needs=["xcode"], fmt="text")
        self.assertEqual(rc, BOX.EXIT_OK)
        text = "\n".join(printed)
        self.assertIn("Selected mac-laptop", text)
        self.assertIn("✓", text)
        self.assertIn("Rejected: jeremy —", text)
        self.assertNotIn("Would provision", text)

    def test_place_repeatable_needs_and_trust_floor(self) -> None:
        rc, payloads, _, _ = self._place(
            needs=["os:linux", "docker"],
            need_trust="local",
            fmt="json",
        )
        self.assertEqual(rc, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertEqual(payload["needs"]["trust"], "local")
        rejected = {row["id"]: row["reasons"] for row in payload["rejected"]}
        self.assertIn("trust_below_floor", rejected["portfolio-devbox"])
        self.assertIn("trust_below_floor", rejected["jeremy"])

    def test_place_allow_provision_emits_dry_run_next_action(self) -> None:
        rc, payloads, _, save = self._place(
            needs=["os:linux", "arch:amd64"],
            allow_provision=True,
            fmt="json",
        )
        # current mac is selected if we also have linux boxes; force no eligible
        # by asking for a cap nobody has, with provision allowed.
        self.assertEqual(rc, BOX.EXIT_OK)
        save.assert_not_called()
        rc, payloads, _, save = self._place(
            needs=["gpu"],
            allow_provision=True,
            fmt="json",
        )
        payload = payloads[-1]
        # ubuntu profiles do not carry gpu, so this stays no_match with empty next_actions
        self.assertEqual(payload["decision"], "no_match")
        self.assertEqual(payload["next_actions"], [])
        save.assert_not_called()

    def test_place_allow_provision_linux_without_current_boxes(self) -> None:
        empty = []
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=empty), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            os.environ["SKILLBOX_MACHINE"] = "mac-laptop"
            rc = BOX.cmd_place(
                needs=["os:linux", "arch:amd64"],
                allow_provision=True,
                fmt="json",
            )
        self.assertEqual(rc, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertEqual(payload["decision"], "provision_proposed")
        self.assertEqual(len(payload["next_actions"]), 1)
        action = payload["next_actions"][0]
        self.assertTrue(action.startswith("python3 scripts/box.py up "))
        self.assertIn(" --profile ", action)
        self.assertTrue(action.endswith(" --dry-run --format json"))

    def test_place_allow_unverified(self) -> None:
        os.environ.pop("SKILLBOX_MACHINE", None)
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[]), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            rc = BOX.cmd_place(
                needs=["xcode"],
                allow_unverified=True,
                fmt="json",
            )
        self.assertEqual(rc, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["decision"], "selected")
        self.assertEqual(payloads[-1]["machine_id"], "mac-laptop")
        self.assertTrue(payloads[-1]["needs"]["allow_unverified"])

    def test_place_never_calls_provider(self) -> None:
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json"), \
            mock.patch.object(BOX, "save_inventory") as save, \
            mock.patch.object(BOX, "do_create_droplet", create=True) as create:
            BOX.cmd_place(needs=["xcode"], allow_provision=True, fmt="json")
        save.assert_not_called()
        create.assert_not_called()

    def test_main_dispatches_place(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            rc = BOX.main(
                ["place", "--need", "xcode", "--need", "os:darwin", "--format", "json"]
            )
        self.assertEqual(rc, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["machine_id"], "mac-laptop")
        self.assertEqual(payloads[-1]["needs"]["caps"], ["xcode", "os:darwin"])

    def test_capabilities_advertises_place(self) -> None:
        payload = BOX.box_capabilities_payload()
        names = [cmd["name"] for cmd in payload["commands"]]
        self.assertIn("place", names)
        place = next(cmd for cmd in payload["commands"] if cmd["name"] == "place")
        self.assertTrue(place["json"])
        self.assertFalse(place["mutates"])
        self.assertFalse(place["destructive"])
        self.assertEqual(
            place["safe_first_try"],
            "python3 scripts/box.py place --need os:linux --format json",
        )
        self.assertEqual(
            payload["read_side_effects"]["place"],
            "does not write workspace/boxes.json; zero lifecycle side effects",
        )

    def test_box_py_does_not_import_runtime_manager_at_module_level(self) -> None:
        source = BOX_SCRIPT.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from runtime_manager") or line.startswith("import runtime_manager"):
                self.fail(f"top-level runtime_manager import: {line}")
        self.assertIn("def _load_placement(", source)
        self.assertIn("sys.path.insert(0, env_manager)", source)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class BoxListMachineViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.machines_path = Path(self._tmp.name) / "machines.yaml"
        self.machines_path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_MACHINES_FILE": str(self.machines_path),
                "SKILLBOX_MACHINE": "mac-laptop",
            },
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.boxes = [
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
            BOX.Box(
                id="shared-host",
                profile="dev-small",
                state="ready",
                management_mode="external",
            ),
        ]

    def test_list_json_includes_machine_view(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            rc = BOX.cmd_list(fmt="json")
        self.assertEqual(rc, BOX.EXIT_OK)
        rows = {row["id"]: row for row in payloads[-1]["machine_view"]}
        self.assertEqual(rows["mac-laptop"]["kind"], "physical")
        self.assertEqual(rows["mac-laptop"]["trust"], "local")
        self.assertIsNone(rows["mac-laptop"]["box_state"])
        self.assertEqual(rows["mac-laptop"]["sources"], ["machines.yaml"])
        self.assertIn("xcode", rows["mac-laptop"]["caps"])

        self.assertEqual(rows["local-no-darwin"]["kind"], "physical")
        self.assertEqual(rows["portfolio-devbox"]["kind"], "persistent")
        self.assertEqual(rows["portfolio-devbox"]["box_state"], "ready")
        self.assertEqual(rows["portfolio-devbox"]["sources"], ["machines.yaml", "boxes"])
        self.assertEqual(rows["jeremy"]["kind"], "ephemeral")
        self.assertIsNone(rows["jeremy"]["trust"])
        self.assertEqual(rows["shared-host"]["kind"], "persistent")
        self.assertEqual(rows["conference1-wsl"]["kind"], "persistent")

    def test_list_machine_filter(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            rc = BOX.cmd_list(fmt="json", machine="mac-laptop")
        self.assertEqual(rc, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertEqual(payload["boxes"], [])
        self.assertEqual([row["id"] for row in payload["machine_view"]], ["mac-laptop"])

    def test_list_text_includes_machine_section(self) -> None:
        printed: list[str] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            rc = BOX.cmd_list(fmt="text")
        self.assertEqual(rc, BOX.EXIT_OK)
        text = "\n".join(printed)
        self.assertIn("Machines:", text)
        self.assertIn("mac-laptop", text)
        self.assertIn("kind=physical", text)

    def test_list_does_not_write_inventory(self) -> None:
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json"), \
            mock.patch.object(BOX, "save_inventory") as save:
            BOX.cmd_list(fmt="json")
        save.assert_not_called()

    def test_main_list_machine_flag(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=self.boxes), \
            mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            rc = BOX.main(["list", "--machine", "jeremy", "--format", "json"])
        self.assertEqual(rc, BOX.EXIT_OK)
        self.assertEqual([row["id"] for row in payloads[-1]["machine_view"]], ["jeremy"])
        self.assertEqual(payloads[-1]["boxes"][0]["id"], "jeremy")


if __name__ == "__main__":
    unittest.main()
