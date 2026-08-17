"""Self-contained unit tests for runtime_manager.placement.

Fixture machines.yaml + fake box/profile namespaces. No network, no process
env reads inside decide(), no live host identity.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"

if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

try:
    import yaml  # noqa: F401

    _HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAVE_YAML = False

from runtime_manager import machines as m
from runtime_manager import placement as p


FIXTURE_YAML = textwrap.dedent(
    """
    version: 1

    machines:
      mac-laptop:
        hostnames: [Mac-2, bs-macbook-air]
        home: /Users/operator
        repo_roots:
          - /Users/operator/repos
        caps: [os:darwin, arch:arm64, xcode, durable]
        trust: local

      portfolio-devbox:
        hostnames: [portfolio-devbox]
        home: /home/skillbox
        repo_roots:
          - /srv/skillbox/repos
        caps: [os:linux, arch:amd64, docker, tailnet, durable]
        trust: allowlisted

      conference1-wsl:
        hostnames: [conference1]
        caps: [os:wsl, arch:amd64, docker, durable]
        trust: allowlisted

      prod-linux-box:
        hostnames: [prod-linux-box]
        caps: [os:linux, durable]
        trust: allowlisted
    """
).strip()


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


def _profile(profile_id: str, *, image: str, size: str) -> SimpleNamespace:
    return SimpleNamespace(id=profile_id, image=image, size=size)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class PlacementDecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "machines.yaml"
        path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
        self.config = m.load_machines_config(path)
        self.profiles = [
            _profile("dev-small", image="ubuntu-24-04-x64", size="s-2vcpu-4gb"),
            _profile("dev-xl", image="ubuntu-24-04-x64", size="s-8vcpu-16gb"),
            _profile("dev-large", image="ubuntu-24-04-x64", size="s-8vcpu-32gb-amd"),
        ]
        self.boxes = [
            _box(
                id="portfolio-devbox",
                profile="dev-large",
                state="ready",
                size="s-8vcpu-32gb-amd",
            ),
            _box(
                id="jeremy",
                profile="dev-small",
                state="ready",
                size="s-2vcpu-4gb",
            ),
        ]

    def _decide(
        self,
        needs: dict | None,
        *,
        boxes: list | None = None,
        observations: dict | None = None,
        profiles: list | None = None,
        current_id: str | None = None,
    ) -> dict:
        if observations is None:
            observations = p.gather_observations(
                current_id, boxes if boxes is not None else self.boxes
            )
        return p.decide(
            needs,
            self.config,
            boxes if boxes is not None else self.boxes,
            observations,
            profiles if profiles is not None else self.profiles,
            current_id,
        )

    def _rejected_ids(self, result: dict) -> set[str]:
        return {row["id"] for row in result["rejected"]}

    def _reasons_for(self, result: dict, machine_id: str) -> list[str]:
        for row in result["rejected"]:
            if row["id"] == machine_id:
                return list(row["reasons"])
        self.fail(f"{machine_id} missing from rejected[]")
        return []

    def test_kind_and_needs_echo(self) -> None:
        result = self._decide(
            {"caps": ["xcode"], "allow_unverified": False},
            current_id="mac-laptop",
        )
        self.assertEqual(result["kind"], "machine-placement/v1")
        self.assertEqual(
            result["needs"],
            {
                "caps": ["xcode"],
                "trust": None,
                "allow_unverified": False,
                "allow_provision": False,
            },
        )
        self.assertIn(result["decision"], p.DECISIONS)

    def test_select_xcode_on_current_mac(self) -> None:
        result = self._decide({"caps": ["os:darwin", "xcode"]}, current_id="mac-laptop")
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "mac-laptop")
        self.assertIsNone(result["box_id"])
        self.assertIn("prefer_current", result["reasons"])
        self.assertIn("caps_match", result["reasons"])
        self.assertEqual(result["next_actions"], [])
        self.assertNotIn("mac-laptop", self._rejected_ids(result))
        accounted = self._rejected_ids(result) | {result["machine_id"]}
        self.assertEqual(
            accounted,
            {"mac-laptop", "portfolio-devbox", "conference1-wsl", "prod-linux-box", "jeremy"},
        )

    def test_linux_docker_selects_declared_over_box_only(self) -> None:
        result = self._decide(
            {"caps": ["os:linux", "docker"]},
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "portfolio-devbox")
        self.assertEqual(result["box_id"], "portfolio-devbox")
        self.assertIn("missing_caps:os:linux", self._reasons_for(result, "mac-laptop"))
        self.assertIn("missing_caps:docker", self._reasons_for(result, "prod-linux-box"))

    def test_prefer_current_over_other_declared(self) -> None:
        result = self._decide({"caps": []}, current_id="prod-linux-box")
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "prod-linux-box")

    def test_size_order_prefers_smaller_box_only(self) -> None:
        boxes = [
            _box(id="big", profile="dev-large", size="s-8vcpu-32gb-amd"),
            _box(id="small", profile="dev-small", size="s-2vcpu-4gb"),
        ]
        empty_cfg = m.MachinesConfig(machines={})
        result = p.decide(
            {"caps": ["os:linux"], "allow_unverified": True},
            empty_cfg,
            boxes,
            {"big": {"reachable": True, "source": "inventory-state"},
             "small": {"reachable": True, "source": "inventory-state"}},
            self.profiles,
            None,
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "small")

    def test_lexicographic_tie_break(self) -> None:
        boxes = [
            _box(id="zeta", profile="dev-small", size="s-2vcpu-4gb"),
            _box(id="alpha", profile="dev-small", size="s-2vcpu-4gb"),
        ]
        empty_cfg = m.MachinesConfig(machines={})
        result = p.decide(
            {"caps": ["os:linux"], "allow_unverified": True},
            empty_cfg,
            boxes,
            {"zeta": {"reachable": True, "source": "probe"},
             "alpha": {"reachable": True, "source": "probe"}},
            self.profiles,
            None,
        )
        self.assertEqual(result["machine_id"], "alpha")

    def test_trust_floor_rejects_allowlisted_when_local_required(self) -> None:
        result = self._decide(
            {"caps": [], "trust": "local"},
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "mac-laptop")
        for machine_id in ("portfolio-devbox", "jeremy", "conference1-wsl"):
            self.assertIn("trust_below_floor", self._reasons_for(result, machine_id))

    def test_denied_when_no_candidate_meets_trust_floor(self) -> None:
        empty_cfg = m.MachinesConfig(machines={})
        boxes = [_box(id="jeremy", profile="dev-small", size="s-2vcpu-4gb")]
        result = p.decide(
            {"caps": [], "trust": "local", "allow_unverified": True},
            empty_cfg,
            boxes,
            {"jeremy": {"reachable": True, "source": "inventory-state"}},
            self.profiles,
            None,
        )
        self.assertEqual(result["decision"], "denied")
        self.assertIsNone(result["machine_id"])
        self.assertEqual(result["next_actions"], [])
        self.assertIn("trust_below_floor", result["reasons"])
        self.assertIn("trust_below_floor", self._reasons_for(result, "jeremy"))

    def test_unreachable_rejects_even_when_caps_match(self) -> None:
        result = self._decide(
            {"caps": ["xcode"]},
            observations={"mac-laptop": {"reachable": False, "source": "probe"}},
            current_id=None,
        )
        self.assertEqual(result["decision"], "no_match")
        self.assertIn("unreachable", self._reasons_for(result, "mac-laptop"))
        self.assertEqual(result["next_actions"], [])

    def test_unverified_when_observation_missing(self) -> None:
        result = self._decide(
            {"caps": ["xcode"]},
            observations={},
            current_id=None,
        )
        self.assertEqual(result["decision"], "no_match")
        self.assertIn("unverified", self._reasons_for(result, "mac-laptop"))

    def test_allow_unverified_accepts_null_observation(self) -> None:
        result = self._decide(
            {"caps": ["xcode"], "allow_unverified": True},
            observations={},
            current_id=None,
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "mac-laptop")

    def test_excluded_box_states_are_not_candidates(self) -> None:
        boxes = [
            _box(id="jeremy", profile="dev-small", state="destroyed", size="s-2vcpu-4gb"),
            _box(id="ghost", profile="dev-small", state="draining", size="s-2vcpu-4gb"),
            _box(id="pending", profile="dev-small", state="destroy-pending"),
            _box(id="vol", profile="dev-small", state="volume-cleanup-failed"),
        ]
        result = self._decide(
            {"caps": ["os:linux"], "allow_unverified": True},
            boxes=boxes,
            current_id=None,
        )
        rejected = self._rejected_ids(result)
        self.assertNotIn("jeremy", rejected)
        self.assertNotIn("ghost", rejected)
        self.assertNotIn("pending", rejected)
        self.assertNotIn("vol", rejected)
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "portfolio-devbox")

    def test_declared_machine_survives_excluded_box(self) -> None:
        boxes = [
            _box(
                id="portfolio-devbox",
                profile="dev-large",
                state="destroyed",
                size="s-8vcpu-32gb-amd",
            )
        ]
        result = self._decide(
            {"caps": ["os:linux", "docker"], "allow_unverified": True},
            boxes=boxes,
            current_id=None,
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "portfolio-devbox")
        self.assertIsNone(result["box_id"])

    def test_box_caps_derived_from_image_and_size(self) -> None:
        empty_cfg = m.MachinesConfig(machines={})
        boxes = [_box(id="scratch", profile="dev-large", size="s-8vcpu-32gb-amd")]
        result = p.decide(
            {"caps": ["os:linux", "arch:amd64"], "allow_unverified": True},
            empty_cfg,
            boxes,
            {"scratch": {"reachable": True, "source": "inventory-state"}},
            self.profiles,
            None,
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["machine_id"], "scratch")

    def test_no_match_has_empty_next_actions(self) -> None:
        result = self._decide(
            {"caps": ["gpu"]},
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "no_match")
        self.assertEqual(result["next_actions"], [])
        self.assertIsNone(result["machine_id"])
        for row in result["rejected"]:
            self.assertTrue(row["reasons"], msg=row)

    def test_provision_proposed_uses_smallest_matching_profile(self) -> None:
        result = self._decide(
            {"caps": ["os:linux", "arch:amd64"], "allow_provision": True},
            boxes=[],
            observations={
                "mac-laptop": {"reachable": True, "source": "self"},
            },
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "provision_proposed")
        self.assertEqual(
            result["next_actions"],
            [
                "python3 scripts/box.py up dev-small --profile dev-small "
                "--dry-run --format json"
            ],
        )
        self.assertIsNone(result["machine_id"])
        accounted = self._rejected_ids(result)
        self.assertEqual(
            accounted,
            {"mac-laptop", "portfolio-devbox", "conference1-wsl", "prod-linux-box"},
        )

    def test_allow_provision_false_does_not_propose(self) -> None:
        result = self._decide(
            {"caps": ["os:linux", "arch:amd64"], "allow_provision": False},
            boxes=[],
            observations={"mac-laptop": {"reachable": True, "source": "self"}},
            current_id="mac-laptop",
        )
        self.assertEqual(result["decision"], "no_match")
        self.assertEqual(result["next_actions"], [])

    def test_provision_not_proposed_when_trust_floor_above_explicit(self) -> None:
        result = self._decide(
            {"caps": ["os:linux"], "trust": "local", "allow_provision": True},
            boxes=[],
            observations={},
            current_id=None,
        )
        # mac-laptop meets the local floor but lacks os:linux; a new
        # explicit box cannot satisfy trust=local, so this is no_match.
        self.assertEqual(result["decision"], "no_match")
        self.assertEqual(result["next_actions"], [])

    def test_decide_does_not_read_environ(self) -> None:
        previous = os.environ.get("SKILLBOX_MACHINE")
        os.environ["SKILLBOX_MACHINE"] = "should-not-matter"
        try:
            result = self._decide({"caps": ["xcode"]}, current_id="mac-laptop")
            self.assertEqual(result["machine_id"], "mac-laptop")
        finally:
            if previous is None:
                os.environ.pop("SKILLBOX_MACHINE", None)
            else:
                os.environ["SKILLBOX_MACHINE"] = previous

    def test_source_has_no_provider_literal(self) -> None:
        source = Path(p.__file__).read_text(encoding="utf-8")
        self.assertNotIn("digitalocean", source)
        self.assertNotIn("DigitalOcean", source)
        self.assertNotIn("os.environ", source)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse machines.yaml")
class MachineViewAndObservationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "machines.yaml"
        path.write_text(FIXTURE_YAML + "\n", encoding="utf-8")
        self.config = m.load_machines_config(path)
        self.profiles = [
            _profile("dev-small", image="ubuntu-24-04-x64", size="s-2vcpu-4gb"),
            _profile("dev-large", image="ubuntu-24-04-x64", size="s-8vcpu-32gb-amd"),
        ]
        self.boxes = [
            _box(
                id="portfolio-devbox",
                profile="dev-large",
                state="ready",
                size="s-8vcpu-32gb-amd",
            ),
            _box(id="jeremy", profile="dev-small", state="ready", size="s-2vcpu-4gb"),
            _box(id="retired", profile="dev-small", state="destroyed"),
        ]

    def test_machine_view_unions_by_exact_id(self) -> None:
        rows = {row["id"]: row for row in p.machine_view(self.config, self.boxes, self.profiles)}
        self.assertEqual(rows["mac-laptop"]["kind"], "physical")
        self.assertEqual(rows["mac-laptop"]["trust"], "local")
        self.assertIsNone(rows["mac-laptop"]["box_state"])
        self.assertEqual(rows["mac-laptop"]["sources"], ["machines.yaml"])
        self.assertIn("xcode", rows["mac-laptop"]["caps"])

        self.assertEqual(rows["portfolio-devbox"]["kind"], "persistent")
        self.assertEqual(rows["portfolio-devbox"]["box_state"], "ready")
        self.assertEqual(rows["portfolio-devbox"]["sources"], ["machines.yaml", "boxes"])
        self.assertIn("docker", rows["portfolio-devbox"]["caps"])

        self.assertEqual(rows["jeremy"]["kind"], "ephemeral")
        self.assertIsNone(rows["jeremy"]["trust"])
        self.assertEqual(rows["jeremy"]["sources"], ["boxes"])
        self.assertEqual(rows["jeremy"]["caps"], ["os:linux", "arch:amd64"])

        self.assertEqual(rows["retired"]["box_state"], "destroyed")
        self.assertEqual(rows["conference1-wsl"]["kind"], "persistent")
        self.assertEqual(rows["prod-linux-box"]["kind"], "persistent")

    def test_external_box_is_persistent(self) -> None:
        boxes = [_box(id="shared", profile="dev-small", management_mode="external")]
        empty_cfg = m.MachinesConfig(machines={})
        rows = p.machine_view(empty_cfg, boxes, self.profiles)
        self.assertEqual(rows[0]["kind"], "persistent")

    def test_gather_observations_current_and_ready(self) -> None:
        observations = p.gather_observations("mac-laptop", self.boxes)
        self.assertEqual(
            observations["mac-laptop"],
            {"reachable": True, "source": "self"},
        )
        self.assertEqual(
            observations["portfolio-devbox"],
            {"reachable": True, "source": "inventory-state"},
        )
        self.assertEqual(
            observations["jeremy"],
            {"reachable": True, "source": "inventory-state"},
        )
        self.assertNotIn("retired", observations)
        self.assertNotIn("conference1-wsl", observations)

    def test_gather_observations_current_overwrites_ready_box(self) -> None:
        observations = p.gather_observations("portfolio-devbox", self.boxes)
        self.assertEqual(
            observations["portfolio-devbox"],
            {"reachable": True, "source": "self"},
        )


if __name__ == "__main__":
    unittest.main()
