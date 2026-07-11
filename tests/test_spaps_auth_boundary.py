"""SPAPS CLI auth boundary contract tests.

Proves the skillbox-owned slice of the SPAPS auth boundary documented in
docs/spaps-cli-auth-boundary.md:

- the spaps-auth client blueprint keeps every auth-facing default on
  loopback (127.0.0.1/localhost) — the only sanctioned off-loopback path is
  the explicit tailnet override applied at first-box time;
- the blueprint ships no secret-like default values;
- the boundary doc keeps its required sections and never embeds token-like
  strings.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
BLUEPRINT_PATH = (
    ROOT_DIR / "workspace" / "client-blueprints" / "git-repo-http-service-bootstrap-spaps-auth.yaml"
)
BOUNDARY_DOC_PATH = ROOT_DIR / "docs" / "spaps-cli-auth-boundary.md"

LOOPBACK_HOST_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)([:/]|$)")
# Long hex/base64-ish runs or known credential prefixes indicate a leaked token.
TOKEN_LIKE_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]{20,}|[a-f0-9]{48,}|spaps_(pub|secret)_[A-Za-z0-9]{8,})"
)


class SpapsAuthBlueprintBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blueprint = yaml.safe_load(BLUEPRINT_PATH.read_text(encoding="utf-8"))
        cls.variables = {
            str(var["name"]): var for var in cls.blueprint.get("variables", [])
        }

    def _default(self, name: str) -> str:
        self.assertIn(name, self.variables, f"blueprint variable {name} missing")
        return str(self.variables[name].get("default", ""))

    def test_auth_url_defaults_are_loopback_only(self) -> None:
        for name in (
            "SPAPS_AUTH_BASE_URL",
            "SPAPS_BROWSER_API_URL",
            "SPAPS_FIXTURE_BASE_URL",
            "SERVICE_HEALTHCHECK_URL",
        ):
            default = self._default(name)
            self.assertRegex(
                default,
                LOOPBACK_HOST_RE,
                f"{name} default must stay on loopback, got: {default}",
            )

    def test_cors_default_origins_are_loopback_only(self) -> None:
        default = self._default("SPAPS_CORS_ALLOW_ORIGINS")
        for origin in default.split(","):
            origin = origin.strip()
            if not origin or origin.startswith("${SPAPS_FIXTURE_BASE_URL}"):
                # Indirection resolves to another checked loopback default.
                continue
            self.assertRegex(
                origin,
                LOOPBACK_HOST_RE,
                f"CORS default origin must stay on loopback, got: {origin}",
            )

    def test_service_healthchecks_are_loopback(self) -> None:
        services = self.blueprint.get("client", {}).get("services", [])
        self.assertGreaterEqual(len(services), 2, "expected auth + app services")
        for service in services:
            url = str(service.get("healthcheck", {}).get("url", ""))
            resolved = url.replace("${SERVICE_HEALTHCHECK_URL}", self._default("SERVICE_HEALTHCHECK_URL"))
            self.assertTrue(
                LOOPBACK_HOST_RE.search(resolved) or resolved.startswith("${"),
                f"service {service.get('id')} healthcheck must stay on loopback, got: {url}",
            )

    def test_blueprint_ships_no_secret_like_defaults(self) -> None:
        raw = BLUEPRINT_PATH.read_text(encoding="utf-8")
        self.assertIsNone(
            TOKEN_LIKE_RE.search(raw),
            "blueprint must not contain token-like strings",
        )
        for name, var in self.variables.items():
            lowered = name.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "api_key")):
                self.assertFalse(
                    str(var.get("default", "")).strip(),
                    f"secret-shaped variable {name} must not ship a default value",
                )


class SpapsAuthBoundaryDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = BOUNDARY_DOC_PATH.read_text(encoding="utf-8")

    def test_doc_exists_with_required_boundary_sections(self) -> None:
        for required in (
            "Ownership boundary table",
            "Browser PKCE",
            "RFC 8628 device-code",
            "Token refresh",
            "Token revoke",
            "Secure credential storage",
            "CI / machine fallback",
            "Redaction / logging rules",
            "Skillbox invariants",
        ):
            self.assertIn(required, self.text, f"boundary doc missing: {required}")

    def test_doc_marks_every_item_with_an_owner(self) -> None:
        for owner in ("SPAPS (`sweet-potato`)", "Skillbox", "Out of scope"):
            self.assertIn(owner, self.text, f"boundary doc missing owner marker: {owner}")

    def test_doc_contains_no_token_like_strings(self) -> None:
        self.assertIsNone(
            TOKEN_LIKE_RE.search(self.text),
            "boundary doc must not embed token-like strings",
        )


if __name__ == "__main__":
    unittest.main()
