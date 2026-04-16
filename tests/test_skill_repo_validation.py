from __future__ import annotations

import json
import shutil
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

    def test_doctor_fails_when_client_vendors_planning_skills_without_opt_out(self) -> None:
        helpers = HELPERS.RuntimeManagerTests(methodName="runTest")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            helpers._write_fixture(repo)

            clients_root = helpers._clients_host_root(repo)
            (clients_root / "personal" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [domain-planner, domain-reviewer]\n",
                encoding="utf-8",
            )
            helpers._write_skill_dir(
                clients_root / "personal" / "skills" / "domain-planner",
                "domain-planner",
            )
            helpers._write_skill_dir(
                clients_root / "personal" / "skills" / "domain-reviewer",
                "domain-reviewer",
            )

            sync = helpers._run(repo, "sync", "--client", "personal")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            doctor = helpers._run(repo, "doctor", "--client", "personal", "--format", "json")
            self.assertEqual(doctor.returncode, 2, doctor.stderr)
            payload = json.loads(doctor.stdout)
            failure_codes = {item["code"] for item in payload["checks"] if item["status"] == "fail"}
            self.assertIn("skill-repo-shared-source", failure_codes, payload["checks"])

    def test_doctor_allows_planning_skills_from_shared_source_symlink(self) -> None:
        helpers = HELPERS.RuntimeManagerTests(methodName="runTest")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            helpers._write_fixture(repo)

            clients_root = helpers._clients_host_root(repo)
            (clients_root / "personal" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [domain-planner, domain-reviewer]\n",
                encoding="utf-8",
            )

            shared_skills_root = clients_root / "_shared" / "skills"
            helpers._write_skill_dir(shared_skills_root / "domain-planner", "domain-planner")
            helpers._write_skill_dir(shared_skills_root / "domain-reviewer", "domain-reviewer")

            shutil.rmtree(clients_root / "personal" / "skills")
            (clients_root / "personal" / "skills").symlink_to("../_shared/skills")

            sync = helpers._run(repo, "sync", "--client", "personal")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            doctor = helpers._run(repo, "doctor", "--client", "personal", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            payload = json.loads(doctor.stdout)
            failure_codes = {item["code"] for item in payload["checks"] if item["status"] == "fail"}
            self.assertNotIn("skill-repo-shared-source", failure_codes, payload["checks"])

    def test_doctor_allows_vendored_planning_skills_with_explicit_opt_out(self) -> None:
        helpers = HELPERS.RuntimeManagerTests(methodName="runTest")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            helpers._write_fixture(repo)

            clients_root = helpers._clients_host_root(repo)
            (clients_root / "personal" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "skill_repos:\n"
                "  - path: ./skills\n"
                "    pick: [domain-planner]\n"
                "    allow_vendored_shared_skills: true\n",
                encoding="utf-8",
            )
            helpers._write_skill_dir(
                clients_root / "personal" / "skills" / "domain-planner",
                "domain-planner",
            )

            sync = helpers._run(repo, "sync", "--client", "personal")
            self.assertEqual(sync.returncode, 0, sync.stderr)

            doctor = helpers._run(repo, "doctor", "--client", "personal", "--format", "json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            payload = json.loads(doctor.stdout)
            failure_codes = {item["code"] for item in payload["checks"] if item["status"] == "fail"}
            self.assertNotIn("skill-repo-shared-source", failure_codes, payload["checks"])


if __name__ == "__main__":
    unittest.main()
