from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts.lib import clipboard_session as cs


ROOT_DIR = Path(__file__).resolve().parents[1]
HOSTS = ROOT_DIR / "scripts" / "clipboard" / "hosts.json"


class FakeTmux:
    def __init__(self) -> None:
        self.options: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        args = command[1:]
        if args[0:2] == ["set-option", "-p"] and "-u" not in args:
            pane = args[args.index("-t") + 1]
            option = args[-2]
            value = args[-1]
            self.options[(pane, option)] = value
            return subprocess.CompletedProcess(command, 0, "", "")
        if args[0:2] == ["set-option", "-p"] and "-u" in args:
            pane = args[args.index("-t") + 1]
            option = args[-1]
            self.options.pop((pane, option), None)
            return subprocess.CompletedProcess(command, 0, "", "")
        if args[0:2] == ["show-option", "-p"]:
            pane = args[args.index("-t") + 1]
            option = args[-1]
            return subprocess.CompletedProcess(
                command, 0, self.options.get((pane, option), "") + "\n", ""
            )
        return subprocess.CompletedProcess(
            command, 1, "", "unexpected fake tmux command"
        )


class ClipboardSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "routes"
        self.tmux = FakeTmux()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def register(self, **overrides: object) -> tuple[dict[str, object], Path]:
        args: dict[str, object] = {
            "profile": "d3",
            "transport": "ssh",
            "tmux_pane": "%1",
            "tmux_client": "/dev/ttys001",
            "tmux_server": "default",
            "root": self.state,
            "hosts_path": HOSTS,
            "now": 1_000.0,
            "ttl_seconds": 100,
            "tmux_runner": self.tmux,
        }
        args.update(overrides)
        return cs.register(**args)

    def test_d3_direct_ssh_registers_exact_pane_and_canonical_route(self) -> None:
        record, path = self.register()
        self.assertEqual(record["profile"], "d3")
        self.assertEqual(record["ssh_target"], "skillbox@skillbox-portfolio-devbox")
        self.assertEqual(record["remote_home"], "/home/skillbox")
        self.assertEqual(record["transport"], "ssh")
        self.assertEqual(record["local"]["tmux_pane"], "%1")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(self.tmux.options[("%1", cs.TMUX_ROUTE_OPTION)], str(path))

    def test_d2_numbered_devbox_records_remote_session_and_parent_target(self) -> None:
        record, _ = self.register(
            profile="devbox",
            target="skillbox@skillbox-portfolio-devbox",
            transport="mosh",
            remote_session="devbox-1",
        )
        self.assertEqual(record["profile"], "devbox")
        self.assertEqual(record["remote_session"], "devbox-1")
        self.assertEqual(record["transport"], "mosh")
        self.assertTrue(record["capabilities"]["inbound"]["smart_path_paste"])

    def test_mosh_and_ssh_are_explicit_not_inferred(self) -> None:
        ssh, _ = self.register(transport="ssh", tmux_pane="%2")
        mosh, _ = self.register(transport="mosh", tmux_pane="%3")
        self.assertEqual(ssh["transport"], "ssh")
        self.assertEqual(mosh["transport"], "mosh")

    def test_direct_ghostty_without_local_tmux_uses_terminal_id(self) -> None:
        record, path = self.register(
            profile="local",
            transport="local",
            tmux_pane=None,
            tmux_client=None,
            tmux_server=None,
            terminal_id="ghostty:surface-42",
            stamp_tmux=False,
        )
        self.assertIsNone(record["local"]["tmux_pane"])
        self.assertEqual(record["local"]["terminal_id"], "ghostty:surface-42")
        self.assertTrue(path.exists())

    def test_missing_exact_tmux_or_terminal_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(cs.SessionError, "requires both pane and client"):
            self.register(tmux_client=None)
        with self.assertRaisesRegex(cs.SessionError, "requires an exact"):
            self.register(
                tmux_pane=None,
                tmux_client=None,
                tmux_server=None,
                terminal_id=None,
                stamp_tmux=False,
            )

    def test_conference_direct_and_fallback_capabilities_are_distinct(self) -> None:
        direct, _ = self.register(
            profile="conference1", transport="ssh", tmux_pane="%4"
        )
        fallback, _ = self.register(
            profile="conference1-fallback", transport="wsl", tmux_pane="%5"
        )
        self.assertTrue(direct["capabilities"]["inbound"]["smart_path_paste"])
        self.assertFalse(fallback["capabilities"]["inbound"]["smart_path_paste"])
        self.assertEqual(fallback["trust"], "unsupported")

    def test_reconnect_replaces_generation_for_same_pane(self) -> None:
        first, first_path = self.register(generation=str(uuid.uuid4()))
        second, second_path = self.register(generation=str(uuid.uuid4()), now=1_010.0)
        self.assertEqual(first_path, second_path)
        self.assertNotEqual(first["generation"], second["generation"])
        with self.assertRaisesRegex(cs.SessionError, "generation changed"):
            cs.load_record(
                second_path,
                now=1_020.0,
                expected_generation=str(first["generation"]),
            )
        resolved, _ = cs.resolve_tmux(
            pane="%1", client="/dev/ttys001", now=1_020.0, runner=self.tmux
        )
        self.assertEqual(resolved["generation"], second["generation"])

    def test_focus_race_client_or_pane_mismatch_fails_closed(self) -> None:
        _, path = self.register()
        with self.assertRaisesRegex(cs.SessionError, "focused client"):
            cs.load_record(path, now=1_010.0, expected_client="/dev/ttys999")
        with self.assertRaisesRegex(cs.SessionError, "focused pane"):
            cs.load_record(path, now=1_010.0, expected_pane="%9")

    def test_concurrent_panes_have_independent_records(self) -> None:
        first, first_path = self.register(tmux_pane="%10")
        second, second_path = self.register(tmux_pane="%11")
        self.assertNotEqual(first["route_id"], second["route_id"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(len(list(self.state.glob("*.json"))), 2)

    def test_unregister_clears_pane_options_and_record(self) -> None:
        _, path = self.register()
        cs.unregister(path, tmux_runner=self.tmux)
        self.assertFalse(path.exists())
        self.assertNotIn(("%1", cs.TMUX_ROUTE_OPTION), self.tmux.options)
        self.assertNotIn(("%1", cs.TMUX_GENERATION_OPTION), self.tmux.options)

    def test_unregister_removes_record_even_after_pane_disappears(self) -> None:
        _, path = self.register()

        def missing_pane(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "can't find pane: %1")

        with self.assertRaisesRegex(cs.SessionError, "can't find pane"):
            cs.unregister(path, tmux_runner=missing_pane)
        self.assertFalse(path.exists())

    def test_cleanup_removes_stale_and_keeps_current(self) -> None:
        _, stale = self.register(tmux_pane="%12", now=100.0, ttl_seconds=10)
        _, current = self.register(tmux_pane="%13", now=200.0, ttl_seconds=100)
        report = cs.cleanup(self.state, now=250.0)
        self.assertEqual(report, {"removed": 1, "kept": 1})
        self.assertFalse(stale.exists())
        self.assertTrue(current.exists())

    def test_mode_schema_and_symlink_tampering_are_rejected(self) -> None:
        _, path = self.register()
        path.chmod(0o644)
        with self.assertRaisesRegex(cs.SessionError, "unsafe ownership or mode"):
            cs.load_record(path, now=1_010.0)
        path.chmod(0o600)
        payload = json.loads(path.read_text())
        payload["unknown"] = True
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        with self.assertRaisesRegex(cs.SessionError, "unknown schema"):
            cs.load_record(path, now=1_010.0)
        path.unlink()
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("{}")
        path.symlink_to(outside)
        with self.assertRaisesRegex(cs.SessionError, "regular file"):
            cs.load_record(path, now=1_010.0)

    def test_contradictory_home_and_unsafe_metadata_are_rejected(self) -> None:
        with self.assertRaisesRegex(cs.SessionError, "contradicts"):
            self.register(remote_home="/root")
        with self.assertRaisesRegex(cs.SessionError, "unsafe remote session"):
            self.register(remote_session="devbox-1;id")

    def test_loaded_route_rejects_tampered_authorization_fields(self) -> None:
        mutations = (
            ("remote_home", lambda item: item.__setitem__("remote_home", "/home/u;id")),
            (
                "ssh_target",
                lambda item: item.__setitem__("ssh_target", "-oProxyCommand=id"),
            ),
            ("generation", lambda item: item.__setitem__("generation", "not-a-uuid")),
            (
                "local_identity",
                lambda item: item["local"].__setitem__("tmux_pane", "%999"),
            ),
            (
                "capability_type",
                lambda item: item["capabilities"]["inbound"].__setitem__(
                    "smart_path_paste", 1
                ),
            ),
            (
                "timestamps",
                lambda item: item.__setitem__(
                    "expires_at", item["updated_at"]
                ),
            ),
        )
        for index, (name, mutate) in enumerate(mutations, start=30):
            with self.subTest(name=name):
                _, path = self.register(tmux_pane=f"%{index}")
                payload = json.loads(path.read_text())
                mutate(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaises(cs.SessionError):
                    cs.load_record(path, now=1_010.0)

    def test_route_record_is_bound_to_private_parent_filename_and_size(self) -> None:
        _, path = self.register(tmux_pane="%50")
        renamed = path.with_name(f"{'0' * 32}.json")
        path.rename(renamed)
        with self.assertRaisesRegex(cs.SessionError, "path does not match"):
            cs.load_record(renamed, now=1_010.0)

        renamed.write_bytes(b" " * (cs.MAX_RECORD_BYTES + 1))
        renamed.chmod(0o600)
        with self.assertRaisesRegex(cs.SessionError, "size limit"):
            cs.load_record(renamed, now=1_010.0)

        renamed.write_text("{}", encoding="utf-8")
        renamed.chmod(0o600)
        self.state.chmod(0o755)
        with self.assertRaisesRegex(cs.SessionError, "parent has unsafe"):
            cs.load_record(renamed, now=1_010.0)


if __name__ == "__main__":
    unittest.main()
