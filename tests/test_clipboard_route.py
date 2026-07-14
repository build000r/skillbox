from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.lib import clipboard_route as cr


ROOT_DIR = Path(__file__).resolve().parent.parent
HOSTS = ROOT_DIR / "scripts" / "clipboard" / "hosts.json"
CLI = ROOT_DIR / "scripts" / "clipboard-route"


class ClipboardRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = cr.load_host_config(HOSTS)

    def test_all_profiles_have_typed_capabilities_and_security_strategy(self) -> None:
        for name, raw in self.data["profiles"].items():
            with self.subTest(profile=name):
                resolved = cr.resolve_profile(
                    name,
                    data=self.data,
                    target="user@devbox-1" if raw.get("dynamic_target") else None,
                )
                self.assertIn(resolved["broker_strategy"], cr.BROKER_STRATEGIES)
                self.assertIn(resolved["display_strategy"], cr.DISPLAY_STRATEGIES)
                self.assertIn(resolved["trust"], cr.TRUST_LEVELS)
                for capability in cr.CAPABILITY_KEYS:
                    directions = resolved["capabilities"]
                    self.assertTrue(
                        capability in directions["outbound"]
                        or capability in directions["inbound"]
                    )

    def test_supported_profile_and_alias_matrix(self) -> None:
        cases = {
            "d": ("d3", "skillbox@skillbox-portfolio-devbox", True),
            "sweet-potato-prod": ("sweet", "aiops@sweet-potato-prod", True),
            "skillbox-jeremy-3": ("jeremy", "skillbox@skillbox-jeremy-3", True),
            "conference1-wsl": ("conference1", "worker@conference1-wsl", True),
            "conference1-ssh": ("conference1-fallback", "conference1-ssh", False),
        }
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                route = cr.resolve_profile(alias, data=self.data)
                self.assertEqual(route["profile"], expected[0])
                self.assertEqual(route["ssh_target"], expected[1])
                self.assertEqual(cr.capability(route, "smart_path_paste"), expected[2])

    def test_named_devbox_requires_exact_target(self) -> None:
        with self.assertRaises(cr.HostConfigError):
            cr.resolve_profile("devbox", data=self.data)
        route = cr.resolve_profile("devbox", data=self.data, target="skillbox@devbox-7")
        self.assertEqual(route["profile"], "devbox")
        self.assertEqual(route["parent_profile"], "d3")
        self.assertEqual(route["ssh_target"], "skillbox@devbox-7")

    def test_raw_target_uses_explicit_generic_profile(self) -> None:
        route = cr.resolve_profile("me@example.test", data=self.data)
        self.assertEqual(route["profile"], "generic")
        self.assertEqual(route["ssh_target"], "me@example.test")
        self.assertFalse(cr.capability(route, "smart_path_paste"))

    def test_unknown_alias_and_contradictory_config_fail(self) -> None:
        with self.assertRaises(cr.HostConfigError):
            cr.resolve_profile("not-a-real-profile", data=self.data)
        duplicate = copy.deepcopy(self.data)
        duplicate["aliases"]["d3"] = "sweet"
        with self.assertRaisesRegex(cr.HostConfigError, "contradicts"):
            cr.validate_host_config(duplicate)
        missing = copy.deepcopy(self.data)
        del missing["profiles"]["d3"]["capabilities"]["inbound"]["native_image_paste"]
        with self.assertRaisesRegex(cr.HostConfigError, "missing capabilities"):
            cr.validate_host_config(missing)

    def test_cli_resolves_tsv_without_hardcoded_shell_map(self) -> None:
        proc = subprocess.run(
            [str(CLI), "d", "--hosts", str(HOSTS), "--format", "tsv"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(
            proc.stdout.strip().split("\t"),
            ["skillbox@skillbox-portfolio-devbox", "ssh", "/home/skillbox", "d3"],
        )
        clipimg = (ROOT_DIR / "scripts" / "clipboard" / "clipimg-put").read_text(
            encoding="utf-8"
        )
        self.assertIn("clipboard-route", clipimg)
        self.assertNotIn('case "$target_arg"', clipimg)

    def test_invalid_registry_cli_fails_with_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "hosts.json"
            bad.write_text(
                json.dumps({"profiles": {}, "aliases": {}}), encoding="utf-8"
            )
            proc = subprocess.run(
                [str(CLI), "d", "--hosts", str(bad)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "route_invalid")


if __name__ == "__main__":
    unittest.main()
