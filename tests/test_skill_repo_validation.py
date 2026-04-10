from __future__ import annotations

import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
HELPERS = SourceFileLoader(
    "runtime_manager_test_helpers",
    str((ROOT_DIR / "tests" / "test_runtime_manager.py").resolve()),
).load_module()


class SkillRepoValidationTests(unittest.TestCase):
    def test_doctor_allows_client_skillset_to_override_default_install(self) -> None:
        helpers = HELPERS.RuntimeManagerTests(methodName="runTest")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            helpers._write_fixture(repo)

            clients_root = helpers._clients_host_root(repo)
            (clients_root / "personal" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [sample-skill]\n",
                encoding="utf-8",
            )
            helpers._write_skill_dir(
                clients_root / "personal" / "skills" / "sample-skill",
                "sample-skill override",
            )

            sync = helpers._run(repo, "sync", "--client", "personal")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            doctor = helpers._run(repo, "doctor", "--client", "personal", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            payload = json.loads(doctor.stdout)
            failure_codes = {item["code"] for item in payload["checks"] if item["status"] == "fail"}
            self.assertNotIn("skill-repo-install", failure_codes, payload["checks"])


if __name__ == "__main__":
    unittest.main()
