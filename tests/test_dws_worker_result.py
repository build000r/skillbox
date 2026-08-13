from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SBPD = load_module("sbpd_dws_test", ROOT / "scripts" / "sbpd.py")
CLIENT = load_module("sbp_client_dws_test", ROOT / "scripts" / "lib" / "sbp_client.py")


class Authenticator:
    def verify(self, token: str) -> dict[str, str]:
        if token != "valid":
            raise SBPD.AuthenticationError("invalid")
        return {
            "project_id": "project-one",
            "thread_id": "thread-one",
            "user_id": "user-one",
            "jti": "jwt-one",
            "sub": "project:project-one:user:user-one:thread:thread-one",
        }


def handoff() -> dict[str, object]:
    return {
        "repo": "fixture-repo",
        "base_sha": "a" * 40,
        "selected_project_id": "project-one",
        "admission_id": "dws-admission-fixture",
        "handoff_digest": "b" * 64,
        "work": {"bead_id": "fixture-bead"},
        "lease": {
            "lease_id": "dws-lease-fixture",
            "fencing_token": 7,
        },
    }


def result_envelope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "dws-worker-result/v1",
        "outcome": "success",
        "repo_id": "fixture-repo",
        "base_sha": "a" * 40,
        "commit_sha": "c" * 40,
        "pushed_sha": "c" * 40,
        "selected_project_id": "project-one",
        "admission_id": "dws-admission-fixture",
        "bead_id": "fixture-bead",
        "lease_id": "dws-lease-fixture",
        "fencing_token": 7,
        "handoff_digest": "b" * 64,
        "tests": {"status": "passed", "summary": "3 passed"},
        "finished_at": int(time.time()),
    }
    value.update(changes)
    value["result_digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


class Server:
    def __init__(self, inbox: Path, *, enabled: bool = True) -> None:
        self.server = SBPD.ThreadingHTTPServer(("127.0.0.1", 0), SBPD.Handler)
        self.server.require_auth = True
        self.server.authenticator = Authenticator()
        self.server.project_alias = "test/project"
        self.server.worker_writes_enabled = enabled
        self.server.worker_result_inbox = inbox
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(
        self,
        value: dict[str, object],
        admission: str = "dws-admission-fixture",
        *,
        authorized: bool = True,
        raw_body: bytes | None = None,
    ):
        body = raw_body or json.dumps(value, separators=(",", ":")).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        try:
            connection.request(
                "POST",
                f"/v1/dws/complete/{admission}",
                body=body,
                headers={
                    **({"Authorization": "Bearer valid"} if authorized else {}),
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class DwsWorkerResultTests(unittest.TestCase):
    def test_client_builds_compact_result_from_sealed_handoff(self) -> None:
        source = io.BytesIO(json.dumps(handoff()).encode())
        result = CLIENT._worker_result(
            [
                "complete",
                "--handoff",
                "-",
                "--outcome",
                "success",
                "--commit-sha",
                "c" * 40,
                "--pushed-sha",
                "c" * 40,
                "--tests",
                "passed",
                "--test-summary",
                "3 passed",
            ],
            stdin=source,
            now=lambda: 1234,
        )
        self.assertEqual(result["schema_version"], "dws-worker-result/v1")
        self.assertEqual(result["admission_id"], "dws-admission-fixture")
        self.assertNotIn("proofs", result)
        claimed = result.pop("result_digest")
        self.assertEqual(claimed, hashlib.sha256(CLIENT._canonical(result)).hexdigest())

    def test_success_requires_equal_commit_and_pushed_sha(self) -> None:
        with self.assertRaisesRegex(ValueError, "identical commit/pushed SHA"):
            CLIENT._worker_result(
                [
                    "complete",
                    "--handoff",
                    "-",
                    "--outcome",
                    "success",
                    "--commit-sha",
                    "c" * 40,
                    "--pushed-sha",
                    "d" * 40,
                    "--tests",
                    "passed",
                ],
                stdin=io.BytesIO(json.dumps(handoff()).encode()),
            )

    def test_authenticated_intake_is_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary)
            os.chmod(inbox, 0o700)
            server = Server(inbox)
            try:
                first_status, first = server.post(result_envelope())
                replay_status, replay = server.post(result_envelope())
            finally:
                server.close()
            self.assertEqual(first_status, 200)
            self.assertFalse(first["idempotent_replay"])
            self.assertEqual(replay_status, 200)
            self.assertTrue(replay["idempotent_replay"])
            stored = json.loads((inbox / "dws-admission-fixture.json").read_text())
            self.assertEqual(stored["status"] if "status" in stored else stored["outcome"], "success")

    def test_wrong_project_admission_conflict_and_disabled_intake_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary)
            os.chmod(inbox, 0o700)
            server = Server(inbox)
            try:
                project_status, project = server.post(
                    result_envelope(selected_project_id="project-two")
                )
                admission_status, admission = server.post(
                    result_envelope(), admission="dws-admission-other"
                )
            finally:
                server.close()
            disabled = Server(inbox, enabled=False)
            try:
                disabled_status, disabled_body = disabled.post(result_envelope())
            finally:
                disabled.close()
        self.assertEqual((project_status, project["error"]), (403, "worker_result_project_mismatch"))
        self.assertEqual((admission_status, admission["error"]), (409, "worker_result_admission_mismatch"))
        self.assertEqual((disabled_status, disabled_body["error"]), (404, "worker_result_intake_disabled"))

    def test_conflicting_replay_and_symlink_destination_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary)
            os.chmod(inbox, 0o700)
            server = Server(inbox)
            try:
                self.assertEqual(server.post(result_envelope())[0], 200)
                status, body = server.post(
                    result_envelope(tests={"status": "passed", "summary": "different"})
                )
            finally:
                server.close()
            self.assertEqual((status, body["error"]), (409, "worker_result_conflict"))

        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary)
            os.chmod(inbox, 0o700)
            (inbox / "target").write_text("do not overwrite")
            (inbox / "dws-admission-fixture.json").symlink_to(inbox / "target")
            server = Server(inbox)
            try:
                status, body = server.post(result_envelope())
            finally:
                server.close()
            self.assertEqual((status, body["error"]), (409, "worker_result_destination_invalid"))
            self.assertEqual((inbox / "target").read_text(), "do not overwrite")

    def test_unauthenticated_invalid_fence_and_oversize_body_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary)
            os.chmod(inbox, 0o700)
            server = Server(inbox)
            try:
                unauthorized_status, unauthorized = server.post(
                    result_envelope(), authorized=False
                )
                status, body = server.post(result_envelope(fencing_token=0))
                large_status, large = server.post(
                    result_envelope(), raw_body=b"x" * (SBPD.MAX_DWS_RESULT_BYTES + 1)
                )
            finally:
                server.close()
        self.assertEqual((unauthorized_status, unauthorized["error"]), (401, "unauthorized"))
        self.assertEqual((status, body["error"]), (400, "worker_result_fence_invalid"))
        self.assertEqual((large_status, large["error"]), (413, "worker_result_too_large"))


if __name__ == "__main__":
    unittest.main()
