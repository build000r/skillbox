#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REPOS_ROOT = Path(os.environ.get("SKILLBOX_REPOS_ROOT", ROOT / "repos"))
SKILLS_ROOT = Path(os.environ.get("SKILLBOX_SKILLS_ROOT", ROOT / "skills"))
LOG_ROOT = Path(os.environ.get("SKILLBOX_LOG_ROOT", ROOT / "logs"))
HOME_ROOT = Path(os.environ.get("SKILLBOX_HOME_ROOT", "/home/sandbox"))
PORT = int(os.environ.get("SKILLBOX_API_PORT", "8000"))


def list_directories(root: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not root.exists():
        return items

    for child in sorted(root.iterdir()):
        if child.is_dir():
            items.append({"id": child.name, "path": str(child)})
    return items


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        message = "%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args)
        print(message, end="")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "skillbox-api",
                    "workspace_root": str(ROOT),
                    "repos_root": str(REPOS_ROOT),
                    "skills_root": str(SKILLS_ROOT),
                    "home_root": str(HOME_ROOT),
                    "log_root": str(LOG_ROOT),
                },
            )
            return

        if self.path == "/v1/sandbox":
            self._json(
                200,
                {
                    "name": "skillbox",
                    "entrypoints": ["ssh", "manual", "api", "web"],
                    "repos": list_directories(REPOS_ROOT),
                    "skills": list_directories(SKILLS_ROOT),
                    "home_mounts": {
                        "claude": str(HOME_ROOT / ".claude"),
                        "codex": str(HOME_ROOT / ".codex"),
                    },
                },
            )
            return

        self._json(404, {"ok": False, "error": "not_found", "path": self.path})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"skillbox api stub listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
