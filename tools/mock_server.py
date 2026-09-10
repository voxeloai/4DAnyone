"""Local dev server for the web client: serves app/web + a scans directory with the same routes as the API.

    python tools/mock_server.py --scans <dir-with-scan-folders> --port 8123

Implements only what the client needs: /, /view/<id>, /assets/*, /scans/*, /api/v1/scans[/<id>],
/api/v1/presets, /api/v1/health, /api/v1/jobs (empty). No uploads, no GPU: purely for viewer/UI work.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
mimetypes.add_type("application/octet-stream", ".sog")
mimetypes.add_type("text/javascript", ".mjs")


def make_handler(scans_dir: Path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter
            if "/api/" in fmt % args or ".sog" in fmt % args:
                super().log_message(fmt, *args)

        def send_json(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path, status=200):
            if not path.is_file():
                self.send_json({"detail": "not found"}, 404)
                return
            data = path.read_bytes()
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def scans(self):
            items = []
            for d in sorted(scans_dir.iterdir()):
                m = d / "manifest.json"
                if d.is_dir() and m.is_file():
                    j = json.loads(m.read_text())
                    seq, stats, preset = j.get("sequence", {}), j.get("stats", {}), j.get("preset", {})
                    items.append({
                        "id": j["id"], "title": j.get("title"), "created": j.get("created"), "preset": preset.get("name"),
                        "views": preset.get("views_per_layer", 0) * len(preset.get("layer_pitches", [1])),
                        "frames": seq.get("count"), "fps": seq.get("fps"), "bytes_total": stats.get("bytes_total"),
                        "gaussians_per_frame": stats.get("gaussians_per_frame"),
                        "poster": f"/scans/{j['id']}/poster.jpg", "preview": f"/scans/{j['id']}/preview.mp4", "url": f"/view/{j['id']}",
                    })
            return items

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/v1/health":
                return self.send_json({"ok": True, "gpu": "mock", "ready": False, "smplx_installed": False, "checkpoint_installed": True, "spirula_installed": True, "queue": {"queued": 0, "running": None}})
            if path == "/api/v1/presets":
                from app.pipeline.presets import public_presets
                return self.send_json(public_presets())
            if path == "/api/v1/jobs":
                return self.send_json([])
            if path == "/api/v1/scans":
                return self.send_json(self.scans())
            if path.startswith("/api/v1/scans/"):
                sid = path.rsplit("/", 1)[1]
                m = scans_dir / sid / "manifest.json"
                if not m.is_file():
                    return self.send_json({"detail": "no such scan"}, 404)
                j = json.loads(m.read_text())
                j["base_url"] = f"/scans/{sid}/"
                return self.send_json(j)
            if path.startswith("/scans/"):
                return self.send_file(scans_dir / path[len("/scans/"):])
            if path.startswith("/assets/"):
                return self.send_file(WEB / path[1:])
            if path.startswith("/view/"):
                return self.send_file(WEB / "view.html")
            if path in ("/", "/index.html"):
                return self.send_file(WEB / "index.html")
            return self.send_json({"detail": "not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scans", required=True)
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args()
    import sys

    sys.path.insert(0, str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Path(args.scans).resolve()))
    print(f"mock server on http://127.0.0.1:{args.port}  scans={args.scans}")
    server.serve_forever()


if __name__ == "__main__":
    main()
