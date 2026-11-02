"""Local HTTP server (127.0.0.1 only) used by the browser extension.

Endpoints
  GET  /ping       -> {"app": "SwiftGet", "version": ...}
  GET  /config     -> capture settings for the extension
  POST /download   -> {"url", "filename", "referrer", "cookies", "userAgent", "fileSize"}
  POST /show       -> bring the window to front (used for single-instance)
  POST /video/info -> {"url"} -> qualities of a video page (or set-up progress)
  POST /video/download -> {"url", "quality", "title", "size"}

POST requests must carry the header "X-SwiftGet: 1". A normal web page cannot
send a custom header to another origin without a CORS pre-flight, which this
server never approves, so only the extension (or local programs) can add
downloads.
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import APP_NAME, VERSION

MAX_BODY = 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802 - never allow cross-origin pre-flights
        self._send(403, {"error": "forbidden"})

    def do_GET(self):  # noqa: N802
        app = self.server.app
        if self.path == "/ping":
            self._send(200, {"app": APP_NAME, "version": VERSION})
        elif self.path == "/config":
            s = app.settings
            self._send(200, {
                "enabled": bool(s["browser_integration"]),
                "extensions": s.capture_extensions(),
                "minSizeKb": int(s["capture_min_size_kb"] or 0),
                "videoButton": bool(s["browser_integration"] and s["video_button"]),
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.headers.get("X-SwiftGet") != "1":
            self._send(403, {"error": "missing X-SwiftGet header"})
            return
        app = self.server.app
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "too large"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "invalid json"})
            return
        if self.path == "/download":
            if not app.settings["browser_integration"]:
                self._send(403, {"error": "browser integration is turned off"})
                return
            url = str(data.get("url") or "")
            if not url.lower().startswith(("http://", "https://", "ftp://")):
                self._send(400, {"error": "unsupported url"})
                return
            app.manager.events.put(("browser_add", data))
            self._send(200, {"ok": True})
        elif self.path == "/video/info":
            self._video_info(app, data)
        elif self.path == "/video/download":
            url = str(data.get("url") or "")
            if not url.lower().startswith(("http://", "https://")):
                self._send(400, {"error": "unsupported url"})
                return
            app.manager.events.put(("video_add", {
                "url": url, "quality": str(data.get("quality") or "best")[:20],
                "title": str(data.get("title") or "")[:300], "size": int(data.get("size") or -1),
            }))
            self._send(200, {"ok": True})
        elif self.path == "/show":
            app.manager.events.put(("show", None))
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


    def _video_info(self, app, data):
        from . import video
        from .engine import friendly_error

        url = str(data.get("url") or "")
        if not url.lower().startswith(("http://", "https://")):
            self._send(400, {"error": "unsupported url"})
            return
        tools = app.manager.video_tools
        if not tools.ready():
            st = tools.ensure_async()
            if not tools.ready():
                self._send(200, {"preparing": st["state"] == "preparing", "progress": st["progress"],
                                 "message": st["message"], "error": st["error"]})
                return
        try:
            info = video.get_info(tools, url)
        except Exception as exc:  # noqa: BLE001 - shown in the page
            self._send(200, {"error": friendly_error(exc)})
            return
        self._send(200, dict(info, ok=True))


class IntegrationServer:
    def __init__(self, app, port):
        self.app = app
        self.port = port
        self.httpd = None
        self.error = None

    def start(self):
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as exc:
            self.error = str(exc)
            return False
        self.httpd.daemon_threads = True
        self.httpd.app = self.app
        threading.Thread(target=self.httpd.serve_forever, name="integration", daemon=True).start()
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def _local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def other_instance_running(port, show=True):
    """True if another SwiftGet already listens on the port (optionally asks it to show itself)."""
    try:
        with _local_opener().open(f"http://127.0.0.1:{port}/ping", timeout=1.5) as resp:
            info = json.loads(resp.read() or b"{}")
        if info.get("app") != APP_NAME:
            return False
        if not show:
            return True
        req = urllib.request.Request(f"http://127.0.0.1:{port}/show", data=b"{}", method="POST",
                                     headers={"X-SwiftGet": "1", "Content-Type": "application/json"})
        _local_opener().open(req, timeout=1.5).close()
        return True
    except Exception:  # noqa: BLE001
        return False
