from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import operator_booking as MODULE  # noqa: E402


class OperatorBookingTests(unittest.TestCase):
    def test_http_json_covers_success_http_error_and_network_error(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.body

        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=Response(b'{"ok": true}')) as urlopen:
            self.assertEqual(
                MODULE._http_json(  # noqa: SLF001
                    "http://localhost/api",
                    method="POST",
                    headers={"X-Test": "yes"},
                    body={"hello": "world"},
                    timeout=3,
                ),
                {"ok": True},
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"hello": "world"}')

        http_error = MODULE.urllib.error.HTTPError(
            "http://localhost/api",
            429,
            "Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b'{"error": {"message": "rate limited"}}'),
        )
        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=http_error):
            with self.assertRaises(MODULE.OperatorBookingError) as ctx:
                MODULE._http_json("http://localhost/api", method="GET", headers={})  # noqa: SLF001
        self.assertEqual(ctx.exception.error_type, "operator_booking_http_error")
        self.assertEqual(str(ctx.exception), "rate limited")
        self.assertEqual(ctx.exception.data["status"], 429)

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(MODULE.OperatorBookingError) as ctx:
                MODULE._http_json("http://localhost/api", method="GET", headers={})  # noqa: SLF001
        self.assertEqual(ctx.exception.error_type, "operator_booking_network_error")

    def test_availability_uses_overlay_endpoint_and_publishable_key_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._model(tmpdir)
            response = {
                "success": True,
                "data": {
                    "totalSlots": 2,
                    "bookedSlots": 0,
                    "baseRate": 1000,
                    "slots": [
                        {"date": "2026-05-06", "slot": "AM", "price": 1000, "available": True},
                        {"date": "2026-05-06", "slot": "PM", "price": 1200, "available": False},
                    ],
                },
            }

            with mock.patch.object(MODULE, "_http_json", return_value=response) as http_json:
                payload, exit_code = MODULE.operator_booking_payload(
                    model,
                    action="availability",
                    client_id="personal",
                    limit=5,
                )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["available"], 1)
        self.assertEqual(payload["slots"][0]["date"], "2026-05-06")
        self.assertEqual(http_json.call_args.kwargs["headers"]["X-API-Key"], "spaps_pub_test")
        self.assertEqual(http_json.call_args.kwargs["headers"]["Origin"], "http://localhost:3000")
        self.assertEqual(http_json.call_args.kwargs["headers"]["Authorization"], "Bearer jwt_test")
        self.assertEqual(http_json.call_args.args[0], "http://localhost:3301/api/dayrate/availability")

    def test_book_can_send_magic_link_before_x402_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._model(tmpdir)
            responses = [
                {"success": True, "data": {"email": "customer@example.com", "state": "state-1"}},
                {
                    "success": True,
                    "data": {
                        "bookingId": "booking-1",
                        "resourceKey": "dayrate-book:booking-1",
                        "actionKey": "dayrate-book",
                    },
                },
            ]

            with mock.patch.object(MODULE, "_http_json", side_effect=responses) as http_json:
                payload, exit_code = MODULE.operator_booking_payload(
                    model,
                    action="book",
                    client_id="personal",
                    date="2026-05-06",
                    slot="AM",
                    email="customer@example.com",
                    name="Customer Example",
                    send_magic_link=True,
                )

        self.assertEqual(exit_code, MODULE.EXIT_OK)
        self.assertEqual(payload["magic_link"]["email"], "customer@example.com")
        self.assertEqual(payload["booking"]["resourceKey"], "dayrate-book:booking-1")
        self.assertEqual(http_json.call_count, 2)
        first_call = http_json.call_args_list[0]
        second_call = http_json.call_args_list[1]
        self.assertEqual(first_call.args[0], "http://localhost:3301/api/auth/magic-link")
        self.assertEqual(first_call.kwargs["body"]["email"], "customer@example.com")
        self.assertEqual(second_call.args[0], "http://localhost:3301/api/dayrate/book-x402")
        self.assertEqual(second_call.kwargs["body"]["clientName"], "Customer Example")

    def test_config_and_book_dry_run_do_not_require_publishable_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._model(tmpdir, env_text="")

            with mock.patch.dict(MODULE.os.environ, {}, clear=True):
                config_payload, config_exit = MODULE.operator_booking_payload(
                    model,
                    action="config",
                    client_id="personal",
                )
                dry_payload, dry_exit = MODULE.operator_booking_payload(
                    model,
                    action="book",
                    client_id="personal",
                    date="2026-05-06",
                    slot="AM",
                    email="customer@example.com",
                    name="Customer Example",
                    dry_run=True,
                    send_magic_link=True,
                )

        self.assertEqual(config_exit, MODULE.EXIT_OK)
        self.assertFalse(config_payload["operator_booking"]["api_key_configured"])
        self.assertEqual(dry_exit, MODULE.EXIT_OK)
        self.assertTrue(dry_payload["dry_run"])
        self.assertEqual(dry_payload["booking_body"]["clientEmail"], "customer@example.com")
        self.assertEqual(dry_payload["magic_link_body"]["email"], "customer@example.com")

    def test_availability_still_requires_publishable_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._model(tmpdir, env_text="")

            with mock.patch.dict(MODULE.os.environ, {}, clear=True):
                with self.assertRaises(MODULE.OperatorBookingError) as ctx:
                    MODULE.operator_booking_payload(
                        model,
                        action="availability",
                        client_id="personal",
                    )

        self.assertEqual(ctx.exception.error_type, "operator_booking_api_key_missing")

    def test_credentialed_requests_reject_non_local_http_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            model = self._model(
                tmpdir,
                availability_url="http://example.com/api/dayrate/availability",
            )

            with self.assertRaises(MODULE.OperatorBookingError) as ctx:
                MODULE.operator_booking_payload(
                    model,
                    action="availability",
                    client_id="personal",
                )

        self.assertEqual(ctx.exception.error_type, "operator_booking_insecure_url")
        self.assertEqual(ctx.exception.data["field"], "availability_url")

    def _model(
        self,
        tmpdir: str,
        env_text: str | None = None,
        availability_url: str = "http://localhost:3301/api/dayrate/availability",
    ) -> dict:
        root = Path(tmpdir)
        env_file = root / ".env.local"
        if env_text is None:
            env_text = (
                "NEXT_PUBLIC_SPAPS_PUBLISHABLE_KEY=spaps_pub_test\n"
                "SPAPS_AUTH_ACCESS_TOKEN=jwt_test\n"
            )
        env_file.write_text(env_text, encoding="utf-8")
        overlay = root / "overlay.yaml"
        overlay.write_text(
            f"""\
version: 1
client:
  id: personal
  human_operator:
    env_file: {env_file}
    booking_url: https://buildooor.com/bookme
    availability_url: {availability_url}
    availability_origin: http://localhost:3000
    preferred_session: AI Build Diagnosis
    payment_required_before_handoff: true
""",
            encoding="utf-8",
        )
        return {
            "active_clients": ["personal"],
            "clients": [
                {
                    "id": "personal",
                    "_overlay_path": str(overlay),
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
