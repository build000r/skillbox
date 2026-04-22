"""Tests for pin resolution (WG-005).

Exhaustive test matrix from the plan::

    {client=None, dist=8, min=None} -> v8 manifest_recommendation
    {client=6, dist=8, min=None}    -> v6 client
    {client=6, dist=8, min=7}       -> v7 manifest_floor (reason carried)
    {client=9, dist=8, min=None}    -> v8 manifest_recommendation (can't exceed offered)
    {client=None, dist=8, min=7}    -> v8 manifest_recommendation (min doesn't force upgrade)
"""
from __future__ import annotations

import os
import sys
import unittest

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from runtime_manager.distribution.manifest import ClientManifestSkill
from runtime_manager.distribution.pin_resolver import PinResolution, resolve_pin


def _skill(
    version: int = 8,
    min_version: int | None = None,
    min_version_reason: str | None = None,
) -> ClientManifestSkill:
    return ClientManifestSkill(
        name="deploy",
        version=version,
        sha256="a" * 64,
        size_bytes=28400,
        download_url="/skills/deploy/8/bundle.tar.gz",
        targets=["box"],
        min_version=min_version,
        min_version_reason=min_version_reason,
    )


class TestPinResolutionMatrix(unittest.TestCase):
    """Exhaustive test matrix from the plan."""

    def test_no_pin_no_floor(self) -> None:
        """client=None, dist=8, min=None -> v8, manifest_recommendation."""
        r = resolve_pin(_skill(version=8), client_pin=None)
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_recommendation")
        self.assertIsNone(r.reason)

    def test_client_pin_below_recommended(self) -> None:
        """client=6, dist=8, min=None -> v6, client."""
        r = resolve_pin(_skill(version=8), client_pin=6)
        self.assertEqual(r.version, 6)
        self.assertEqual(r.pinned_by, "client")
        self.assertIsNone(r.reason)

    def test_floor_overrides_client_pin(self) -> None:
        """client=6, dist=8, min=7, reason='CVE' -> v7, manifest_floor."""
        r = resolve_pin(
            _skill(version=8, min_version=7, min_version_reason="CVE-2026-xxxx"),
            client_pin=6,
        )
        self.assertEqual(r.version, 7)
        self.assertEqual(r.pinned_by, "manifest_floor")
        self.assertEqual(r.reason, "CVE-2026-xxxx")

    def test_client_pin_exceeds_offered(self) -> None:
        """client=9, dist=8, min=None -> v8, manifest_recommendation."""
        r = resolve_pin(_skill(version=8), client_pin=9)
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_recommendation")
        self.assertIsNone(r.reason)

    def test_floor_without_client_pin(self) -> None:
        """client=None, dist=8, min=7 -> v8, manifest_recommendation."""
        r = resolve_pin(_skill(version=8, min_version=7), client_pin=None)
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_recommendation")
        self.assertIsNone(r.reason)


class TestPinResolutionEdgeCases(unittest.TestCase):

    def test_client_pin_equals_recommended(self) -> None:
        r = resolve_pin(_skill(version=8), client_pin=8)
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_recommendation")

    def test_client_pin_at_floor(self) -> None:
        r = resolve_pin(_skill(version=8, min_version=5), client_pin=5)
        self.assertEqual(r.version, 5)
        self.assertEqual(r.pinned_by, "client")

    def test_client_pin_one_above_floor(self) -> None:
        r = resolve_pin(_skill(version=8, min_version=5), client_pin=6)
        self.assertEqual(r.version, 6)
        self.assertEqual(r.pinned_by, "client")

    def test_floor_equals_recommended(self) -> None:
        r = resolve_pin(_skill(version=8, min_version=8), client_pin=None)
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_recommendation")

    def test_floor_exceeds_recommended(self) -> None:
        r = resolve_pin(
            _skill(version=5, min_version=8, min_version_reason="security"),
            client_pin=None,
        )
        self.assertEqual(r.version, 8)
        self.assertEqual(r.pinned_by, "manifest_floor")
        self.assertEqual(r.reason, "security")

    def test_client_pin_zero(self) -> None:
        r = resolve_pin(_skill(version=8), client_pin=0)
        self.assertEqual(r.version, 0)
        self.assertEqual(r.pinned_by, "client")

    def test_client_pin_zero_with_floor(self) -> None:
        r = resolve_pin(_skill(version=8, min_version=3), client_pin=0)
        self.assertEqual(r.version, 3)
        self.assertEqual(r.pinned_by, "manifest_floor")

    def test_floor_reason_none_when_not_set(self) -> None:
        r = resolve_pin(_skill(version=8, min_version=7), client_pin=5)
        self.assertEqual(r.version, 7)
        self.assertEqual(r.pinned_by, "manifest_floor")
        self.assertIsNone(r.reason)

    def test_version_one(self) -> None:
        r = resolve_pin(_skill(version=1), client_pin=None)
        self.assertEqual(r.version, 1)
        self.assertEqual(r.pinned_by, "manifest_recommendation")

    def test_floor_and_pin_and_recommended_all_equal(self) -> None:
        r = resolve_pin(_skill(version=5, min_version=5), client_pin=5)
        self.assertEqual(r.version, 5)
        self.assertEqual(r.pinned_by, "manifest_recommendation")


class TestPinResolutionDataclass(unittest.TestCase):

    def test_frozen(self) -> None:
        r = PinResolution(version=8, pinned_by="client")
        with self.assertRaises(AttributeError):
            r.version = 9  # type: ignore[misc]

    def test_default_reason_is_none(self) -> None:
        r = PinResolution(version=8, pinned_by="manifest_recommendation")
        self.assertIsNone(r.reason)


if __name__ == "__main__":
    unittest.main()
