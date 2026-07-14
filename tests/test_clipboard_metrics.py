from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.lib import clipboard_metrics as metrics


ROOT_DIR = Path(__file__).resolve().parents[1]


class ClipboardMetricsTests(unittest.TestCase):
    def test_nearest_rank_summary_is_redacted_and_budgeted(self) -> None:
        receipts = [
            {
                "schema_version": 1,
                "ok": True,
                "latency_ms": value,
                "outcome": "image_path",
                "route_id": "route-secret",
                "clipboard": {"text": "must-not-appear"},
            }
            for value in range(100, 2100, 100)
        ]
        report = metrics.summarize(receipts)
        self.assertEqual(report["sample_count"], 20)
        self.assertEqual(report["latency_ms"]["p50"], 1000.0)
        self.assertEqual(report["latency_ms"]["p95"], 1900.0)
        self.assertTrue(report["budget"]["enough_for_rollout"])
        encoded = json.dumps(report)
        self.assertNotIn("route-secret", encoded)
        self.assertNotIn("must-not-appear", encoded)

    def test_rejections_do_not_pollute_success_distribution(self) -> None:
        report = metrics.summarize(
            [
                {"schema_version": 1, "ok": False},
                {
                    "schema_version": 1,
                    "ok": True,
                    "latency_ms": 200,
                    "outcome": "image_path",
                    "route_id": None,
                },
                {
                    "schema_version": 1,
                    "ok": True,
                    "latency_ms": 1,
                    "outcome": "native_text",
                    "route_id": None,
                },
            ]
        )
        self.assertEqual((report["sample_count"], report["rejected_count"]), (1, 1))
        self.assertEqual(report["ignored_count"], 1)

    def test_empty_or_unknown_receipts_fail_closed(self) -> None:
        with self.assertRaises(metrics.MetricsError):
            metrics.summarize([])
        with self.assertRaises(metrics.MetricsError):
            metrics.summarize([{"schema_version": 99, "ok": True}])

    def test_cli_reads_only_named_local_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "receipt.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ok": True,
                        "latency_ms": 321.5,
                        "outcome": "image_path",
                        "route_id": "route",
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [str(ROOT_DIR / "scripts" / "clipboard-metrics"), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["latency_ms"]["p50"], 321.5)


if __name__ == "__main__":
    unittest.main()
