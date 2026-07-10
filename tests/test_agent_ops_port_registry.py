from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from lib import runtime_model as RM  # noqa: E402
from runtime_manager import command_registry as REG  # noqa: E402
from runtime_manager import port_registry as PR  # noqa: E402
from runtime_manager import runtime_ops as OPS  # noqa: E402
from runtime_manager.context_rendering import generate_context_markdown  # noqa: E402

GOLDEN = ROOT_DIR / "tests" / "goldens" / "port_registry.json"


def _model(**overrides) -> dict[str, object]:
    """A scope-filtered runtime-model shaped dict for registry/doctor tests."""
    base: dict[str, object] = {
        "root_dir": "/repo",
        "manifest_file": "/repo/workspace/runtime.yaml",
        "active_profiles": ["core"],
        "active_clients": [],
        "env": {
            "SKILLBOX_API_PORT": "8000",
            "SKILLBOX_WEB_PORT": "3000",
            "SKILLBOX_INGRESS_PUBLIC_PORT": "8080",
            "SKILLBOX_INGRESS_PUBLIC_HOST": "127.0.0.1",
        },
        "services": [
            {
                "id": "api-stub",
                "kind": "http",
                "profiles": ["core"],
                "command": "python3 scripts/stub_api.py",
                "healthcheck": {"type": "http", "url": "http://127.0.0.1:8000/health"},
            },
            {
                "id": "pulse",
                "kind": "daemon",
                "profiles": ["core"],
                "healthcheck": {"type": "path_exists", "path": "/var/run/pulse.pid"},
            },
        ],
        "ingress_routes": [],
    }
    base.update(overrides)
    return base


class ExtractionHelperTests(unittest.TestCase):
    def test_url_target_yields_host_port_scheme(self) -> None:
        self.assertEqual(RM.extract_host_port("http://127.0.0.1:8000/health"), ("127.0.0.1", 8000, "http"))

    def test_bare_authority_yields_host_port_no_scheme(self) -> None:
        # localhost:8050 with no scheme is still an unambiguous port.
        self.assertEqual(RM.extract_host_port("localhost:8050"), ("localhost", 8050, ""))

    def test_bracketed_ipv6_authority(self) -> None:
        host, port, scheme = RM.extract_host_port("[::1]:9000")
        self.assertEqual((port, scheme), (9000, ""))

    def test_unparseable_targets_return_none_never_guess(self) -> None:
        # No scheme and no port, a filesystem path, and an empty target all
        # refuse to guess.
        for target in ("localhost", "/var/run/x.pid", "", "process-name"):
            self.assertEqual(RM.extract_host_port(target)[1], None, target)

    def test_http_default_port_is_not_inferred(self) -> None:
        # An omitted port is ambiguous for the registry contract: stays None.
        self.assertEqual(RM.extract_host_port("http://localhost/health")[1], None)

    def test_command_port_flag_extraction(self) -> None:
        self.assertEqual(RM.extract_command_port("cm serve --port 3222"), 3222)
        self.assertEqual(RM.extract_command_port("cm serve --port=3222"), 3222)
        self.assertEqual(RM.extract_command_port("cm serve"), None)

    def test_bind_scope_classification(self) -> None:
        self.assertEqual(RM.classify_bind_scope("0.0.0.0"), "wildcard")
        self.assertEqual(RM.classify_bind_scope("::"), "wildcard")
        self.assertEqual(RM.classify_bind_scope("127.0.0.1"), "loopback")
        self.assertEqual(RM.classify_bind_scope("localhost"), "loopback")
        self.assertEqual(RM.classify_bind_scope("100.64.0.5"), "tailnet")
        self.assertEqual(RM.classify_bind_scope(""), "loopback")


class BuildRegistryTests(unittest.TestCase):
    def test_registry_lists_every_active_port_with_owner_and_source(self) -> None:
        entries = PR.build_port_registry(_model())
        declared = [e for e in entries if e["port"] is not None]
        # api-stub service + 3 env-surface keys.
        ports = {(e["owner_id"], e["port"]) for e in declared}
        self.assertIn(("api-stub", 8000), ports)
        self.assertIn(("SKILLBOX_API_PORT", 8000), ports)
        self.assertIn(("SKILLBOX_WEB_PORT", 3000), ports)
        api = next(e for e in declared if e["owner_id"] == "api-stub")
        self.assertEqual(api["owner_kind"], "service")
        self.assertEqual(api["source"], {"file": "workspace/runtime.yaml", "key": "healthcheck.url"})
        self.assertEqual(api["protocol"], "http")
        self.assertEqual(api["bind_scope"], "loopback")

    def test_unparseable_health_target_emits_warning_never_guesses(self) -> None:
        entries = PR.build_port_registry(_model())
        pulse = next(e for e in entries if e["owner_id"] == "pulse")
        self.assertIsNone(pulse["port"])
        self.assertIsNotNone(pulse["warning"])
        self.assertIn("no port", pulse["warning"])
        self.assertEqual(pulse["bind_scope"], "unknown")

    def test_command_port_fallback_when_health_target_has_no_port(self) -> None:
        model = _model(
            services=[
                {
                    "id": "cm-mcp",
                    "kind": "mcp",
                    "profiles": ["core"],
                    "command": "cm serve --port 3222",
                    "healthcheck": {"type": "http", "url": "http://localhost/health"},
                }
            ],
        )
        entries = PR.build_port_registry(model)
        cm = next(e for e in entries if e["owner_id"] == "cm-mcp")
        self.assertEqual(cm["port"], 3222)
        self.assertEqual(cm["source"]["key"], "command --port")

    def test_wildcard_command_host_sets_wildcard_bind_scope(self) -> None:
        model = _model(
            services=[
                {
                    "id": "exposed",
                    "kind": "http",
                    "profiles": ["core"],
                    "command": "server --host 0.0.0.0 --port 8000",
                    "healthcheck": {"type": "http", "url": "http://0.0.0.0:8000/health"},
                }
            ],
        )
        exposed = next(e for e in PR.build_port_registry(model) if e["owner_id"] == "exposed")
        self.assertEqual(exposed["bind_scope"], "wildcard")

    def test_payload_resolve_narrows_to_one_owner(self) -> None:
        payload = PR.port_registry_payload(_model(), resolve="api-stub")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["ports"], [8000])
        self.assertTrue(all(e["owner_id"] == "api-stub" for e in payload["entries"]))

    def test_payload_resolve_missing_owner(self) -> None:
        payload = PR.port_registry_payload(_model(), resolve="does-not-exist")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["ports"], [])

    def test_text_lines_are_compact(self) -> None:
        payload = PR.port_registry_payload(_model())
        lines = PR.port_registry_text_lines(payload)
        self.assertIn("port registry", lines[0])
        for line in lines:
            self.assertLessEqual(len(line), 240)


class DoctorCheckTests(unittest.TestCase):
    def test_clean_scope_passes(self) -> None:
        results = OPS.validate_port_registry(_model())
        codes = {r.code for r in results}
        self.assertNotIn(OPS.PORT_COLLISION, codes)
        self.assertTrue(any(r.code == "port-registry" and r.status == "pass" for r in results)
                        or any(r.code == OPS.PORT_REGISTRY_WARNING for r in results))

    def test_duplicate_port_fails_collision_naming_both(self) -> None:
        model = _model(
            services=[
                {
                    "id": "alpha",
                    "kind": "http",
                    "profiles": ["core"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:9000/health"},
                },
                {
                    "id": "beta",
                    "kind": "http",
                    "profiles": ["core"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:9000/health"},
                },
            ],
        )
        results = OPS.validate_port_registry(model)
        collisions = [r for r in results if r.code == OPS.PORT_COLLISION]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].status, "fail")
        self.assertIn("alpha", collisions[0].message)
        self.assertIn("beta", collisions[0].message)
        owner_ids = {o["owner_id"] for o in collisions[0].details["owners"]}
        self.assertEqual(owner_ids, {"alpha", "beta"})

    def test_wildcard_bind_under_tailnet_only_fails(self) -> None:
        model = _model(
            env={
                "SKILLBOX_NETWORK_POSTURE": "tailnet_only",
                "SKILLBOX_API_PORT": "8000",
            },
            services=[
                {
                    "id": "exposed",
                    "kind": "http",
                    "profiles": ["core"],
                    "command": "server --host 0.0.0.0 --port 8000",
                    "healthcheck": {"type": "http", "url": "http://0.0.0.0:8000/health"},
                }
            ],
        )
        results = OPS.validate_port_registry(model)
        wildcard = [r for r in results if r.code == OPS.PORT_WILDCARD_BIND]
        self.assertEqual(len(wildcard), 1)
        self.assertEqual(wildcard[0].status, "fail")
        self.assertIn("exposed", wildcard[0].message)
        self.assertEqual(wildcard[0].details["posture"], "tailnet_only")

    def test_wildcard_bind_without_tailnet_posture_is_silent(self) -> None:
        model = _model(
            env={"SKILLBOX_API_PORT": "8000"},
            services=[
                {
                    "id": "exposed",
                    "kind": "http",
                    "profiles": ["core"],
                    "command": "server --host 0.0.0.0 --port 8000",
                    "healthcheck": {"type": "http", "url": "http://0.0.0.0:8000/health"},
                }
            ],
        )
        results = OPS.validate_port_registry(model)
        self.assertFalse(any(r.code == OPS.PORT_WILDCARD_BIND for r in results))

    def test_undeclared_reserved_port_fails(self) -> None:
        model = _model(
            port_reserved_ranges=[{"low": 9100, "high": 9102, "label": "agents"}],
        )
        results = OPS.validate_port_registry(model)
        reserved = [r for r in results if r.code == OPS.PORT_UNDECLARED_RESERVED]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0].status, "fail")
        undeclared_ports = {item["port"] for item in reserved[0].details["undeclared"]}
        self.assertEqual(undeclared_ports, {9100, 9101, 9102})

    def test_reserved_range_fully_owned_passes(self) -> None:
        model = _model(
            env={"SKILLBOX_API_PORT": "8000", "SKILLBOX_RESERVED_PORT_RANGES": "8000:apis"},
        )
        results = OPS.validate_port_registry(model)
        self.assertTrue(
            any(r.code == "port-reserved-ranges" and r.status == "pass" for r in results)
        )
        self.assertFalse(any(r.code == OPS.PORT_UNDECLARED_RESERVED for r in results))

    def test_cross_client_overlap_is_advisory_not_failure(self) -> None:
        model = _model(
            active_clients=["acme", "beta-co"],
            services=[
                {
                    "id": "acme-web",
                    "kind": "http",
                    "client": "acme",
                    "profiles": ["local-all"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3000/"},
                },
                {
                    "id": "beta-web",
                    "kind": "http",
                    "client": "beta-co",
                    "profiles": ["local-all"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3000/"},
                },
            ],
        )
        results = OPS.validate_port_registry(model)
        self.assertFalse(any(r.code == OPS.PORT_COLLISION for r in results))
        advisory = [r for r in results if r.code == OPS.PORT_CROSS_CLIENT_OVERLAP]
        self.assertEqual(len(advisory), 1)
        self.assertEqual(advisory[0].status, "warn")
        self.assertTrue(advisory[0].details["advisory"])

    def test_core_vs_client_same_port_is_hard_collision(self) -> None:
        model = _model(
            active_clients=["acme"],
            services=[
                {
                    "id": "web-stub",
                    "kind": "http",
                    "profiles": ["surfaces"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3000/"},
                },
                {
                    "id": "acme-web",
                    "kind": "http",
                    "client": "acme",
                    "profiles": ["local-all"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3000/"},
                },
            ],
        )
        results = OPS.validate_port_registry(model)
        self.assertTrue(any(r.code == OPS.PORT_COLLISION and r.status == "fail" for r in results))


class PortContractTests(unittest.TestCase):
    def _contract_model(self, repo_path: Path, *, port: int = 5173) -> dict[str, object]:
        return _model(
            env={},
            repos=[
                {
                    "id": "app",
                    "host_path": str(repo_path),
                    "source": {"kind": "directory"},
                }
            ],
            services=[
                {
                    "id": "web",
                    "kind": "http",
                    "repo": "app",
                    "profiles": ["core"],
                    "command": f"npm run dev -- --host 127.0.0.1 --port {port}",
                    "healthcheck": {"type": "http", "url": f"http://127.0.0.1:{port}/health"},
                }
            ],
            parity_ledger=[],
        )

    def test_sync_port_contract_writes_updates_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "app"
            repo_path.mkdir()
            target = repo_path / OPS.PORT_CONTRACT_FILE_NAME

            model = self._contract_model(repo_path, port=5173)
            actions = OPS.sync_port_contracts(model, dry_run=False)
            self.assertEqual(actions, [f"render-port-contract: {target} (1 service(s))"])
            text = target.read_text(encoding="utf-8")
            self.assertIn("PORT=5173\n", text)
            self.assertIn("HOST=127.0.0.1\n", text)
            self.assertIn("SKILLBOX_SERVICE_ID=web\n", text)
            self.assertIn("SKILLBOX_PORT_SOURCE=workspace/runtime.yaml:healthcheck.url\n", text)

            self.assertEqual(
                OPS.sync_port_contracts(model, dry_run=False),
                [f"port-contract-unchanged: {target}"],
            )

            updated_model = self._contract_model(repo_path, port=5174)
            OPS.sync_port_contracts(updated_model, dry_run=False)
            self.assertIn("PORT=5174\n", target.read_text(encoding="utf-8"))

    def test_sync_port_contract_skips_non_covered_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "app"
            repo_path.mkdir()
            model = self._contract_model(repo_path)
            model["parity_ledger"] = [
                {
                    "id": "web",
                    "legacy_surface": "web",
                    "surface_type": "service",
                    "ownership_state": "external",
                }
            ]

            self.assertEqual(OPS.sync_port_contracts(model, dry_run=False), [])
            self.assertFalse((repo_path / OPS.PORT_CONTRACT_FILE_NAME).exists())

    def test_validate_port_contracts_warns_for_unadopted_and_is_suppressible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "app"
            repo_path.mkdir()
            model = self._contract_model(repo_path)
            OPS.sync_port_contracts(model, dry_run=False)

            results = OPS.validate_port_contracts(model)
            codes = {result.code for result in results}
            self.assertIn(OPS.PORT_CONTRACT_UNADOPTED, codes)
            self.assertIn(OPS.PORT_CONTRACT_GITIGNORE, codes)
            unadopted = next(result for result in results if result.code == OPS.PORT_CONTRACT_UNADOPTED)
            self.assertIn("strictPort", unadopted.details["adoption_snippet"])
            gitignore = next(result for result in results if result.code == OPS.PORT_CONTRACT_GITIGNORE)
            self.assertEqual(gitignore.details["exact_line"], OPS.PORT_CONTRACT_FILE_NAME)

            service = model["services"][0]
            service["port_contract"] = {"suppress_advisory": True}
            suppressed_codes = {result.code for result in OPS.validate_port_contracts(model)}
            self.assertNotIn(OPS.PORT_CONTRACT_UNADOPTED, suppressed_codes)
            self.assertIn(OPS.PORT_CONTRACT_GITIGNORE, suppressed_codes)

            service["port_contract"] = {}
            (repo_path / ".gitignore").write_text(f"{OPS.PORT_CONTRACT_FILE_NAME}\n", encoding="utf-8")
            (repo_path / "vite.config.ts").write_text(
                "import 'dotenv/config';\n"
                "export default { server: { host: process.env.HOST, "
                "port: Number(process.env.PORT), strictPort: true } };\n",
                encoding="utf-8",
            )
            clean_results = OPS.validate_port_contracts(model)
            self.assertEqual([result.code for result in clean_results], ["port-contracts"])

            target = repo_path / OPS.PORT_CONTRACT_FILE_NAME
            target.write_text("PORT=9999\n", encoding="utf-8")
            stale_results = OPS.validate_port_contracts(model)
            self.assertEqual([result.code for result in stale_results], [OPS.PORT_CONTRACT_STALE])
            self.assertEqual(stale_results[0].details["state"], "stale")

    def test_multi_service_contract_uses_marked_default_without_duplicate_extra_var(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "app"
            repo_path.mkdir()
            model = self._contract_model(repo_path)
            model["services"] = [
                {
                    "id": "auth",
                    "kind": "http",
                    "repo": "app",
                    "profiles": ["core"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:3301/health"},
                    "port_contract": {"suppress_advisory": True},
                },
                {
                    "id": "web",
                    "kind": "http",
                    "repo": "app",
                    "profiles": ["core"],
                    "healthcheck": {"type": "http", "url": "http://127.0.0.1:5173/health"},
                    "port_contract": {"default": True},
                },
            ]

            OPS.sync_port_contracts(model, dry_run=False)
            text = (repo_path / OPS.PORT_CONTRACT_FILE_NAME).read_text(encoding="utf-8")
            self.assertIn("PORT=5173\n", text)
            self.assertIn("SKILLBOX_SERVICE_ID=web\n", text)
            self.assertIn("SKILLBOX_AUTH_PORT=3301\n", text)
            self.assertNotIn("SKILLBOX_WEB_PORT=5173\n", text)

    def test_http_client_blueprints_include_strict_port_contract_hint(self) -> None:
        blueprint_dir = ROOT_DIR / "workspace" / "client-blueprints"
        for name in (
            "git-repo-http-service.yaml",
            "git-repo-http-service-bootstrap.yaml",
            "git-repo-http-service-bootstrap-spaps-auth.yaml",
        ):
            text = (blueprint_dir / name).read_text(encoding="utf-8")
            self.assertIn(OPS.PORT_CONTRACT_FILE_NAME, text, name)
            self.assertIn("strictPort", text, name)


class CapabilitiesTests(unittest.TestCase):
    def test_ports_command_is_registered_with_cli_and_mcp_surfaces(self) -> None:
        registry = REG.load_default_registry()
        self.assertIn("runtime.ports", registry)
        spec = registry["runtime.ports"]
        self.assertEqual(set(spec.surface), {"cli", "mcp"})
        self.assertEqual(spec.mcp_tool, "skillbox_ports")
        self.assertEqual(spec.side_effect, "none")
        self.assertEqual(spec.risk, "low")

    def test_capabilities_payload_includes_ports_command(self) -> None:
        payload = REG.registry_payload()
        ids = {entry["id"] for entry in payload["capabilities"]}
        self.assertIn("runtime.ports", ids)
        entry = next(e for e in payload["capabilities"] if e["id"] == "runtime.ports")
        self.assertEqual(entry["mcp_tool"], "skillbox_ports")


class ContextRenderingTests(unittest.TestCase):
    def test_generated_context_contains_ports_table(self) -> None:
        context = generate_context_markdown(
            {
                "active_clients": [],
                "active_profiles": ["core"],
                "root_dir": str(ROOT_DIR),
                "manifest_file": str(ROOT_DIR / "workspace" / "runtime.yaml"),
                "clients": [],
                "repos": [],
                "services": [
                    {
                        "id": "api-stub",
                        "kind": "http",
                        "profiles": ["core"],
                        "healthcheck": {"type": "http", "url": "http://127.0.0.1:8000/health"},
                    }
                ],
                "tasks": [],
                "skills": [],
                "logs": [],
                "env": {"SKILLBOX_API_PORT": "8000"},
            }
        )
        self.assertIn("## Ports", context)
        self.assertIn("| Port | Owner | Kind | Client | Profiles | Bind | Source |", context)
        self.assertIn("api-stub", context)
        self.assertIn("manage.py ports --format json", context)


class GoldenTests(unittest.TestCase):
    def test_registry_golden_matches(self) -> None:
        payload = PR.port_registry_payload(_model())
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        # Compare the stable, scope-independent slice of the registry.
        actual = {
            "count": payload["count"],
            "entries": [
                {
                    "port": e["port"],
                    "owner_id": e["owner_id"],
                    "owner_kind": e["owner_kind"],
                    "bind_scope": e["bind_scope"],
                    "source": e["source"],
                    "protocol": e["protocol"],
                    "warning": e["warning"],
                }
                for e in payload["entries"]
            ],
            "warnings": payload["warnings"],
        }
        self.assertEqual(actual, golden)


if __name__ == "__main__":
    unittest.main()
