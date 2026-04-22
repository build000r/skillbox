from __future__ import annotations

import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.shared import load_skill_repos_config
from runtime_manager.shared_distribution import (
    ConfigError,
    DistributorConfig,
    DistributorSetSource,
    parse_distribution_config,
    validate_distributor_refs,
)


def _write_config(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class ConfigSchemaTests(unittest.TestCase):
    def test_distributor_only_config_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            _write_config(
                config_path,
                "version: 2\n"
                "distributors:\n"
                "  - id: acme-skills\n"
                "    url: https://skills.acme.dev/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                "      key_env: ACME_DISTRIBUTOR_KEY\n"
                "    verification:\n"
                "      public_key: \"ed25519:abc123\"\n"
                "skill_repos:\n"
                "  - distributor: acme-skills\n"
                "    pick: [deploy]\n"
                "    pin:\n"
                "      deploy: 7\n",
            )
            with mock.patch.dict(os.environ, {"ACME_DISTRIBUTOR_KEY": "secret"}, clear=False):
                config = load_skill_repos_config(config_path)
                distributors, distributor_sources = parse_distribution_config(config, config_path)

            self.assertEqual(config["skill_repos"][0]["distributor"], "acme-skills")
            self.assertEqual(config["skill_repos"][0]["pin"]["deploy"], 7)
            self.assertIn("distributors", config)

            self.assertEqual(list(distributors.keys()), ["acme-skills"])
            self.assertIsInstance(distributors["acme-skills"], DistributorConfig)
            self.assertEqual(distributors["acme-skills"].auth.key_env, "ACME_DISTRIBUTOR_KEY")
            self.assertEqual(len(distributor_sources), 1)
            self.assertIsInstance(distributor_sources[0], DistributorSetSource)
            self.assertEqual(distributor_sources[0].distributor, "acme-skills")

    def test_mixed_repo_path_distributor_config_parses_additively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            _write_config(
                config_path,
                "version: 2\n"
                "distributors:\n"
                "  - id: acme-skills\n"
                "    url: https://skills.acme.dev/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                "      key_env: ACME_DISTRIBUTOR_KEY\n"
                "    verification:\n"
                "      public_key: \"ed25519:abc123\"\n"
                "skill_repos:\n"
                "  - repo: build000r/skills\n"
                "    ref: main\n"
                "    pick: [ask-cascade]\n"
                "  - path: ./workspace/skills\n"
                "    pick: [dev-sanity]\n"
                "  - distributor: acme-skills\n"
                "    pick: [deploy]\n",
            )
            with mock.patch.dict(os.environ, {"ACME_DISTRIBUTOR_KEY": "secret"}, clear=False):
                config = load_skill_repos_config(config_path)
                distributors, distributor_sources = parse_distribution_config(config, config_path)

            self.assertEqual(len(config["skill_repos"]), 3)
            self.assertEqual(config["skill_repos"][0]["repo"], "build000r/skills")
            self.assertEqual(config["skill_repos"][1]["path"], "./workspace/skills")
            self.assertEqual(config["skill_repos"][2]["distributor"], "acme-skills")
            self.assertEqual(len(distributors), 1)
            self.assertEqual(len(distributor_sources), 1)

    def test_validate_distributor_refs_raises_for_dangling_reference(self) -> None:
        with self.assertRaises(ConfigError):
            validate_distributor_refs(
                [DistributorSetSource(distributor="missing-dist", pick=["deploy"], pin={})],
                {},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            _write_config(
                config_path,
                "version: 2\n"
                "distributors:\n"
                "  - id: acme-skills\n"
                "    url: https://skills.acme.dev/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                "      key_env: ACME_DISTRIBUTOR_KEY\n"
                "    verification:\n"
                "      public_key: \"ed25519:abc123\"\n"
                "skill_repos:\n"
                "  - distributor: unknown-dist\n"
                "    pick: [deploy]\n",
            )
            with mock.patch.dict(os.environ, {"ACME_DISTRIBUTOR_KEY": "secret"}, clear=False):
                with self.assertRaises(RuntimeError) as ctx:
                    load_skill_repos_config(config_path)
            self.assertIn("SKILL_CONFIG_INVALID", str(ctx.exception))
            self.assertIn("unknown distributor", str(ctx.exception))

    def test_missing_auth_env_is_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "skill-repos.yaml"
            _write_config(
                config_path,
                "version: 2\n"
                "distributors:\n"
                "  - id: acme-skills\n"
                "    url: https://skills.acme.dev/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                "      key_env: ACME_DISTRIBUTOR_KEY\n"
                "    verification:\n"
                "      public_key: \"ed25519:abc123\"\n"
                "skill_repos:\n"
                "  - distributor: acme-skills\n"
                "    pick: [deploy]\n",
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                warnings.catch_warnings(record=True) as captured,
            ):
                warnings.simplefilter("always")
                config = load_skill_repos_config(config_path)

            self.assertEqual(config["distributors"][0]["auth"]["key_env"], "ACME_DISTRIBUTOR_KEY")
            self.assertTrue(
                any("ACME_DISTRIBUTOR_KEY" in str(item.message) for item in captured),
                "expected missing auth env warning",
            )


if __name__ == "__main__":
    unittest.main()
