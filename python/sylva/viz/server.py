"""Local HTTP server for the visualisation UI (stdlib only).

Routes:
  GET /              -> the self-contained index.html
  GET /graph.json    -> the live graph built from the database
  anything else      -> 404 JSON

`make_server(db_path, port)` returns a server object (testable on an ephemeral
port); `serve(...)` is the blocking CLI entry that also opens the browser.

Error handling matches the spec: a database that does not exist yields a 500
response with a JSON error body; a port already in use raises a clear OSError.
"""

import http.server
import json
import os
import webbrowser
from functools import partial

from .export import build_graph, export_graph_json, graph_version

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")


class _Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, db_path=None, **kwargs):
        self._db_path = db_path
        super().__init__(*args, **kwargs)

    def log_message(self, *args):  # keep the server quiet
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_asset("index.html", "text/html; charset=utf-8")
        elif self.path == "/graph.json":
            self._serve_graph()
        elif self.path == "/version":
            self._serve_version()
        else:
            self._send_json(404, {"error": f"not found: {self.path}"})

    def _serve_asset(self, name, content_type):
        try:
            with open(os.path.join(ASSETS_DIR, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json(404, {"error": f"asset not found: {name}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_graph(self):
        try:
            graph = build_graph(self._db_path)
        except FileNotFoundError as e:
            self._send_json(500, {"error": str(e)})
            return
        except Exception as e:  # any query failure -> 500, never a crash
            self._send_json(500, {"error": f"failed to build graph: {e}"})
            return
        self._send_json(200, graph)

    def _serve_version(self):
        try:
            self._send_json(200, graph_version(self._db_path))
        except FileNotFoundError as e:
            self._send_json(500, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"failed to read version: {e}"})

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(db_path, port):
    """Create (but do not start) the HTTP server bound to `port`.

    Raises OSError if the port is already in use.
    """
    handler = partial(_Handler, db_path=db_path)
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(db_path, port=7700, open_browser=True):
    """Start the visualisation server (blocking). Returns a process exit code."""
    # Best-effort artifact refresh; if the db is missing the server still starts
    # and reports the error per-request as a 500.
    try:
        export_graph_json(db_path)
    except FileNotFoundError:
        pass

    try:
        httpd = make_server(db_path, port)
    except OSError as e:
        raise OSError(
            f"cannot bind port {port} (already in use?). "
            f"Try a different port with --port. Underlying error: {e}"
        )

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        webbrowser.open(url)
    print(f"sylva: serving visualisation at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
