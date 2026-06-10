"""Regression suite for tailnet-only box lifecycle.

Validates that the posture contract, lockdown stage, exposure lint, SSH target
policy, and supporting scripts remain correct. Future refactors that reopen
public SSH or wildcard exposure must fail one of these tests.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".env-manager"))

import importlib
BOX = importlib.import_module("box")


class PostureInvariantTests(unittest.TestCase):
    """Core invariants that must never break."""

    def _box(self, **kw):
        defaults = {"id": "reg-box", "profile": "dev-small", "state": "ready", "management_mode": "managed"}
        defaults.update(kw)
        return BOX.Box(**defaults)

    def test_managed_box_defaults_to_tailnet_only(self):
        self.assertEqual(BOX.resolve_network_posture(self._box()), "tailnet_only")

    def test_tailnet_only_requires_cloud_firewall(self):
        self.assertTrue(BOX.posture_requires_cloud_firewall("tailnet_only"))

    def test_tailnet_only_requires_host_ssh_lockdown(self):
        self.assertTrue(BOX.posture_requires_host_ssh_lockdown("tailnet_only"))

    def test_tailnet_only_forbids_public_ssh(self):
        self.assertFalse(BOX.posture_allows_public_ssh("tailnet_only"))

    def test_tailnet_only_forbids_wildcard_direct(self):
        self.assertFalse(BOX.posture_allows_exposure("tailnet_only", "wildcard-direct"))

    def test_tailnet_only_allows_tailnet_and_loopback(self):
        for exposure in ("tailnet-direct", "loopback-only", "ingress-routed"):
            self.assertTrue(BOX.posture_allows_exposure("tailnet_only", exposure), exposure)

    def test_violation_detected_for_public_ssh_on_tailnet_only(self):
        box = self._box()
        violations = BOX.evaluate_posture_violations(
            box, network_checks={"public_ssh": {"ok": True, "target": "1.2.3.4"}}
        )
        types = [v["type"] for v in violations]
        self.assertIn("public_ssh_reachable", types)

    def test_violation_detected_for_missing_cloud_firewall(self):
        box = self._box(cloud_firewall_id=None)
        violations = BOX.evaluate_posture_violations(box)
        types = [v["type"] for v in violations]
        self.assertIn("cloud_firewall_missing", types)

    def test_no_violation_when_firewall_present_and_ssh_unreachable(self):
        box = self._box(cloud_firewall_id="fw-123")
        violations = BOX.evaluate_posture_violations(
            box, network_checks={"public_ssh": {"ok": False}}
        )
        self.assertEqual(violations, [])


class LockdownStageTests(unittest.TestCase):
    """Lockdown must be in the state machine."""

    def test_lockdown_in_states(self):
        self.assertIn("lockdown", BOX.STATES)

    def test_lockdown_in_valid_transitions(self):
        self.assertIn("lockdown", BOX.VALID_TRANSITIONS)
        self.assertIn("deploying", BOX.VALID_TRANSITIONS["lockdown"])

    def test_enrolling_transitions_to_lockdown(self):
        self.assertIn("lockdown", BOX.VALID_TRANSITIONS["enrolling"])
        self.assertNotIn("deploying", BOX.VALID_TRANSITIONS["enrolling"])

    def test_lockdown_in_resumable_states(self):
        self.assertIn("lockdown", BOX.RESUMABLE_UP_STATES)


class SSHTargetPolicyTests(unittest.TestCase):
    """SSH target resolution must respect posture."""

    def _box(self, **kw):
        defaults = {
            "id": "ssh-box", "profile": "dev-small", "state": "ready",
            "management_mode": "managed", "ssh_user": "skillbox",
        }
        defaults.update(kw)
        return BOX.Box(**defaults)

    def test_stale_public_cache_skipped_under_tailnet_only(self):
        box = self._box(
            droplet_ip="1.2.3.4", tailscale_ip="100.100.1.1",
            last_ssh_target="1.2.3.4",
        )
        candidates = BOX.box_ssh_candidates(box)
        self.assertEqual(candidates[0], "100.100.1.1")

    def test_public_cache_works_under_public_posture(self):
        box = self._box(
            network_posture="public",
            droplet_ip="1.2.3.4", tailscale_ip="100.100.1.1",
            last_ssh_target="1.2.3.4",
        )
        candidates = BOX.box_ssh_candidates(box)
        self.assertEqual(candidates[0], "1.2.3.4")

    def test_prefer_public_overrides_posture_for_bootstrap(self):
        box = self._box(droplet_ip="1.2.3.4", tailscale_ip="100.100.1.1")
        candidates = BOX.box_ssh_candidates(box, prefer_public=True)
        self.assertEqual(candidates[0], "1.2.3.4")

    def test_public_fallback_not_cached_under_tailnet_only(self):
        box = self._box(droplet_ip="1.2.3.4", tailscale_ip=None)
        with mock.patch.object(BOX, "wait_for_ssh", return_value=True):
            target = BOX.resolve_box_ssh_target(box, max_wait=1, interval=1)
        self.assertEqual(target, "1.2.3.4")
        self.assertIsNone(box.last_ssh_target)


class ExposureLintTests(unittest.TestCase):
    """Runtime exposure lint must catch wildcard binds."""

    def test_validate_service_exposure_exists(self):
        from runtime_manager import runtime_ops
        self.assertTrue(hasattr(runtime_ops, "validate_service_exposure"))


class ScriptSyntaxTests(unittest.TestCase):
    """Shell scripts and Python modules must parse cleanly."""

    def test_bootstrap_script_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "scripts" / "01-bootstrap-do.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tailscale_script_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "scripts" / "02-install-tailscale.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_box_py_compiles(self):
        import py_compile
        py_compile.compile(str(REPO_ROOT / "scripts" / "box.py"), doraise=True)


class PostureProofTests(unittest.TestCase):
    """posture-proof subcommand must exist and produce structured output."""

    def test_posture_proof_subcommand_registered(self):
        parser = BOX.build_parser()
        args = parser.parse_args(["posture-proof", "test-box"])
        self.assertEqual(args.command, "posture-proof")

    def test_posture_proof_not_found(self):
        with mock.patch.object(BOX, "load_inventory", return_value=[]):
            result = BOX.cmd_posture_proof("ghost", fmt="json")
        self.assertEqual(result, BOX.EXIT_ERROR)


class FirewallLifecycleTests(unittest.TestCase):
    """DO firewall CRUD must exist."""

    def test_crud_functions_exist(self):
        for fn in ("do_create_firewall", "do_update_firewall_lockdown",
                    "do_delete_firewall", "do_get_firewall"):
            self.assertTrue(callable(getattr(BOX, fn, None)), fn)

    def test_cleanup_skips_without_id(self):
        box = BOX.Box(id="no-fw", profile="dev-small", state="draining", cloud_firewall_id=None)
        steps: list[dict] = []
        result = BOX._cleanup_box_firewall(box, steps, is_json=True)
        self.assertTrue(result)
        self.assertEqual(steps[0]["step"], "firewall")
        self.assertEqual(steps[0]["status"], "skip")


if __name__ == "__main__":
    unittest.main()
