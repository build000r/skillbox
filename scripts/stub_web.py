#!/usr/bin/env python3
from __future__ import annotations

import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
API_PORT = int(os.environ.get("SKILLBOX_API_PORT", "8000"))
PORT = int(os.environ.get("SKILLBOX_WEB_PORT", "3000"))
REPOS_ROOT = Path(os.environ.get("SKILLBOX_REPOS_ROOT", ROOT / "repos"))
SKILLS_ROOT = Path(os.environ.get("SKILLBOX_SKILLS_ROOT", ROOT / "skills"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        message = "%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args)
        print(message, end="")

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/index.html"}:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>skillbox</title>
    <style>
      body {{
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        margin: 0;
        padding: 2rem;
        background: #f4f0e8;
        color: #181818;
      }}
      main {{
        max-width: 52rem;
      }}
      .card {{
        border: 1px solid #181818;
        background: #fffdfa;
        padding: 1rem;
        margin: 1rem 0;
      }}
      code {{
        background: #ece5d8;
        padding: 0.1rem 0.35rem;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>skillbox</h1>
      <p>Thin Tailnet Docker starter: host-level SSH over Tailscale, one workspace container, optional stub surfaces.</p>
      <div class="card">
        <strong>API</strong>
        <p><a href="http://127.0.0.1:{API_PORT}/health">/health</a></p>
        <p><a href="http://127.0.0.1:{API_PORT}/v1/sandbox">/v1/sandbox</a></p>
      </div>
      <div class="card">
        <strong>Workspace shape</strong>
        <p>Repos live in <code>{html.escape(str(REPOS_ROOT.relative_to(ROOT)))}</code>.</p>
        <p>Local shipped skills live in <code>{html.escape(str(SKILLS_ROOT.relative_to(ROOT)))}</code>.</p>
        <p>Mounted config homes live under <code>home/.claude</code> and <code>home/.codex</code>.</p>
      </div>
    </main>
  </body>
</html>
"""
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"skillbox web stub listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
