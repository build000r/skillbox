from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import clipboard_route_exec as route_exec


ROOT_DIR = Path(__file__).resolve().parents[1]
HOSTS = ROOT_DIR / "scripts" / "clipboard" / "hosts.json"


class ClipboardRouteExecTests(unittest.TestCase):
    def test_child_observes_exact_direct_terminal_record_then_owner_cleans_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "routes"
            observed = Path(raw) / "observed.json"
            script = (
                "import json,os,pathlib; "
                "p=next(pathlib.Path(os.environ['ROUTE_ROOT']).glob('*.json')); "
                "pathlib.Path(os.environ['OBSERVED']).write_text(p.read_text())"
            )
            env = {
                **os.environ,
                "TERM_SESSION_ID": "ghostty-route-exec-test",
                "ROUTE_ROOT": str(root),
                "OBSERVED": str(observed),
            }
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            rc = route_exec.run_registered(
                [os.environ.get("PYTHON", "python3"), "-c", script],
                profile="d3",
                transport="ssh",
                target="skillbox@skillbox-portfolio-devbox",
                remote_session=None,
                remote_home=None,
                hosts_path=HOSTS,
                state_root=root,
                env=env,
            )
            self.assertEqual(rc, 0)
            record = json.loads(observed.read_text())
            self.assertEqual(record["local"]["terminal_id"], "ghostty-route-exec-test")
            self.assertEqual(record["profile"], "d3")
            self.assertEqual(list(root.glob("*.json")), [])

    def test_tmux_identity_is_derived_from_exact_launching_pane(self) -> None:
        values = {
            "#{client_name}": "/dev/ttys777",
            "#{socket_path}": "/private/tmp/tmux-test/default",
        }
        with mock.patch.object(
            route_exec, "_tmux_value", side_effect=lambda _p, f: values[f]
        ):
            identity = route_exec.identity_from_environment(
                {"TMUX": "/private/tmp/tmux-test/default,1,0", "TMUX_PANE": "%42"}
            )
        self.assertEqual(
            identity,
            {
                "tmux_pane": "%42",
                "tmux_client": "/dev/ttys777",
                "tmux_server": "/private/tmp/tmux-test/default",
                "terminal_id": None,
            },
        )

    def test_tmux_identity_waits_for_new_client_without_guessing(self) -> None:
        with (
            mock.patch.object(
                route_exec,
                "_tmux_value",
                side_effect=[
                    route_exec.RouteExecError("tmux did not report #{client_name}"),
                    "/dev/ttys888",
                    "/private/tmp/tmux-test/default",
                ],
            ) as tmux_value,
            mock.patch.object(route_exec.time, "sleep") as sleep,
        ):
            identity = route_exec.identity_from_environment(
                {"TMUX": "/private/tmp/tmux-test/default,1,0", "TMUX_PANE": "%43"}
            )
        self.assertEqual(identity["tmux_client"], "/dev/ttys888")
        self.assertEqual(tmux_value.call_args_list[0].args, ("%43", "#{client_name}"))
        self.assertEqual(tmux_value.call_args_list[1].args, ("%43", "#{client_name}"))
        sleep.assert_called_once_with(0.05)

    def test_tmux_identity_fails_closed_after_bounded_client_wait(self) -> None:
        with (
            mock.patch.object(
                route_exec,
                "_tmux_value",
                side_effect=route_exec.RouteExecError("client unavailable"),
            ) as tmux_value,
            mock.patch.object(route_exec.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(route_exec.RouteExecError, "client unavailable"):
                route_exec.identity_from_environment(
                    {
                        "TMUX": "/private/tmp/tmux-test/default,1,0",
                        "TMUX_PANE": "%44",
                    }
                )
        self.assertEqual(tmux_value.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    def test_identity_without_tmux_or_terminal_fails_closed(self) -> None:
        with self.assertRaisesRegex(route_exec.RouteExecError, "no stable"):
            route_exec.identity_from_environment({})


if __name__ == "__main__":
    unittest.main()
