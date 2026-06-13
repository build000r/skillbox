"""Shared pytest setup for the ``tests/`` tree.

Besides putting the repo root on ``sys.path``, this exposes the
**fixture-fleet** factory (see ``tests/fixture_fleet.py``) as a pytest fixture
so any future visibility change is provable in one ``pytest`` run.

Usage::

    def test_resolution(fixture_fleet):
        payload = fixture_fleet.run_resolution(fixture_fleet.repo("overlay-repo"))
        assert payload["matched_scope_rules"]

``fixture_fleet`` builds a miniature skill estate (two machine profiles +
``machines.yaml``, OS + managed homes with both symlink-home variants, mini
``skills/`` + ``skills-private/`` source roots with a ``requires_beads`` skill,
the ``_shared`` chain, a ``skill-scope.yaml`` + ``registry/repos.yaml``, and 4
fake repos showing healthy / other-machine / dangling / overlay-gated links).
Its three helpers -- ``run_resolution(cwd)``, ``run_audit()``,
``apply_plan(...)`` -- call the *unchanged* runtime surfaces
(``collect_skill_visibility`` / ``collect_skill_audit`` /
``skill_lifecycle_plan``) and return their parsed payloads. ``Path.home()`` is
patched to the fixture home so global skill roots resolve inside the temp tree.

You can also call the factory directly when not using the fixture::

    from tests.fixture_fleet import build_fixture_fleet
    fleet = build_fixture_fleet(tmp_path)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importable as ``tests.fixture_fleet`` (REPO_ROOT on path) and as
# ``fixture_fleet`` (tests dir on path during collection).
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture
def fixture_fleet(tmp_path):
    """A materialized miniature skill estate (see ``tests/fixture_fleet.py``)."""
    from fixture_fleet import build_fixture_fleet

    return build_fixture_fleet(tmp_path)
