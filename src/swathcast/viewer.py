"""Serve the site locally: ``swathcast viewer --schedule x.parquet --latency y.json``."""

from __future__ import annotations

import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path


def site_assets():
    return files("swathcast") / "site"


def make_handler(data_files: dict[str, Path]):
    assets = site_assets()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            route = self.path.split("?")[0].lstrip("/") or "index.html"
            if route.startswith("data/"):
                path = data_files.get(route[len("data/") :])
                if path is None or not path.exists():
                    self.send_error(404)
                    return
                self._send(
                    path.read_bytes(),
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
                return
            asset = assets / route
            if ".." in route or not asset.is_file():
                self.send_error(404)
                return
            self._send(
                asset.read_bytes(),
                mimetypes.guess_type(route)[0] or "application/octet-stream",
            )

        def _send(self, body: bytes, content_type: str):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(
    schedule: Path,
    latency: Path | None,
    latest: Path | None,
    health: Path | None,
    port: int,
    open_browser: bool,
) -> None:
    data = {
        "upcoming_passes.parquet": schedule.resolve(),
        "upcoming_passes.meta.json": schedule.resolve().with_suffix(".meta.json"),
    }
    if latency:
        data["latency.json"] = latency.resolve()
        sibling = latency.resolve().parent
        data.setdefault("latest.json", (latest or sibling / "latest.json").resolve())
        data.setdefault("health.json", (health or sibling / "health.json").resolve())
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data))
    url = f"http://127.0.0.1:{port}/"
    print(f"Serving {schedule.name} at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
