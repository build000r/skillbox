from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

BOX = SourceFileLoader("skillbox_box_inventory", str(BOX_SCRIPT.resolve())).load_module()


def _flip_inventory_state(path_text: str, box_id: str, iterations: int) -> None:
    from lib.opslib import locked_inventory_update

    path = Path(path_text)
    for _ in range(iterations):
        def _update(current: object) -> dict[str, object]:
            data = current if isinstance(current, dict) else {}
            boxes = data.get("boxes")
            if not isinstance(boxes, list):
                boxes = [
                    {"id": "box-a", "profile": "dev", "state": "creating"},
                    {"id": "box-b", "profile": "dev", "state": "creating"},
                ]
            by_id = {
                str(item.get("id")): dict(item)
                for item in boxes
                if isinstance(item, dict) and item.get("id")
            }
            box = by_id.setdefault(box_id, {"id": box_id, "profile": "dev", "state": "creating"})
            box["state"] = "ready" if box.get("state") == "creating" else "creating"
            counts = data.setdefault("counts", {})
            if not isinstance(counts, dict):
                counts = {}
                data["counts"] = counts
            counts[box_id] = int(counts.get(box_id, 0)) + 1
            data["boxes"] = [by_id[key] for key in sorted(by_id)]
            return data

        locked_inventory_update(path, _update, default={"boxes": [], "counts": {}})


class BoxInventoryIntegrityTests(unittest.TestCase):
    def test_locked_inventory_update_preserves_concurrent_state_flips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "boxes.json"
            path.write_text(
                json.dumps(
                    {
                        "boxes": [
                            {"id": "box-a", "profile": "dev", "state": "creating"},
                            {"id": "box-b", "profile": "dev", "state": "creating"},
                        ],
                        "counts": {},
                    }
                ),
                encoding="utf-8",
            )

            ctx = multiprocessing.get_context("fork")
            iterations = 25
            processes = [
                ctx.Process(target=_flip_inventory_state, args=(str(path), box_id, iterations))
                for box_id in ("box-a", "box-b")
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["counts"], {"box-a": iterations, "box-b": iterations})
            self.assertEqual({box["id"] for box in data["boxes"]}, {"box-a", "box-b"})

    def test_atomic_write_json_kill_mid_save_leaves_old_or_new_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "boxes.json"
            old_payload = {"boxes": [{"id": "old", "profile": "dev", "state": "ready"}]}
            path.write_text(json.dumps(old_payload), encoding="utf-8")
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
                "from lib.opslib import atomic_write_json\n"
                "payload = {'boxes': [{'id': 'new', 'profile': 'dev', 'state': 'ready', 'pad': 'x' * 5_000_000}]}\n"
                "atomic_write_json(Path(sys.argv[1]), payload)\n"
            )

            for _ in range(8):
                path.write_text(json.dumps(old_payload), encoding="utf-8")
                proc = subprocess.Popen([sys.executable, "-c", script, str(path)])
                time.sleep(0.002)
                proc.kill()
                proc.wait(timeout=5)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn(payload["boxes"][0]["id"], {"old", "new"})

    def test_transition_journal_and_status_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "workspace").mkdir()
            box = BOX.Box(id="box-1", profile="dev", state="creating")
            with mock.patch.object(BOX, "REPO_ROOT", repo):
                BOX.save_inventory([box], actor="test", reason="create")
                BOX.update_box(box, state="ready")
                BOX.save_inventory([box], actor="test", reason="up ready")
                BOX.update_box(box, state="draining")
                BOX.save_inventory([box], actor="test", reason="down start")

                entries = BOX.read_inventory_journal("box-1")
                self.assertEqual(
                    [(entry["from_state"], entry["to_state"]) for entry in entries],
                    [(None, "creating"), ("creating", "ready"), ("ready", "draining")],
                )
                self.assertEqual(entries[-1]["actor"], "test")
                self.assertEqual(entries[-1]["reason"], "down start")

                status_payload = {
                    "id": "box-1",
                    "state": "draining",
                    "profile": "dev",
                    "ssh_reachable": False,
                    "container_running": False,
                    "tailscale_hostname": None,
                    "droplet_id": None,
                    "droplet_ip": None,
                    "network_checks": {},
                }
                stdout = StringIO()
                with (
                    mock.patch.object(BOX, "box_health", return_value=dict(status_payload)),
                    redirect_stdout(stdout),
                ):
                    result = BOX.cmd_status("box-1", fmt="json", write_cache=False, history=True)
                self.assertEqual(result, BOX.EXIT_OK)
                payload = json.loads(stdout.getvalue())
                self.assertEqual(len(payload["history"]), 3)
                self.assertEqual(payload["history"][-1]["to_state"], "draining")

    def test_inventory_rebuild_from_journal_overwrites_corrupt_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "workspace").mkdir()
            box = BOX.Box(id="box-1", profile="dev", state="creating")
            with mock.patch.object(BOX, "REPO_ROOT", repo):
                BOX.save_inventory([box], actor="test", reason="create")
                BOX.update_box(box, state="ready")
                BOX.save_inventory([box], actor="test", reason="ready")
                BOX.inventory_path().write_text("{not json", encoding="utf-8")

                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = BOX.cmd_inventory_rebuild(from_journal=True, fmt="json")

                self.assertEqual(result, BOX.EXIT_OK)
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["rebuilt"], 1)
                loaded = BOX.load_inventory()
                self.assertEqual([(box.id, box.profile, box.state) for box in loaded], [("box-1", "dev", "ready")])

    def test_corrupt_inventory_fails_closed_with_recovery_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "workspace").mkdir()
            (repo / "workspace" / "boxes.json").write_text("{not json", encoding="utf-8")
            stdout = StringIO()
            with mock.patch.object(BOX, "REPO_ROOT", repo), redirect_stdout(stdout):
                result = BOX.main(["status", "--format", "json"])

            self.assertEqual(result, BOX.EXIT_ERROR)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["error"]["type"], "INVENTORY_CORRUPT")
            self.assertIn("inventory-rebuild --from-journal", " ".join(payload["next_actions"]))


if __name__ == "__main__":
    unittest.main()
