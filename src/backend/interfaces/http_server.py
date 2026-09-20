"""HTTP interface adapter for the dashboard application."""

import json
from http.server import BaseHTTPRequestHandler
from queue import Empty
from urllib.parse import parse_qs, urlparse

from ..domain.validators import DEFAULT_WORKFLOW, WORKFLOW_NAME_RE
from .presenters import JsonPresenter, SsePresenter


class DashboardHttpHandler(BaseHTTPRequestHandler):
    server_version = "GateBoard/0.1"
    services = None
    json_presenter = JsonPresenter()
    sse_presenter = SsePresenter()

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type=None):
        if isinstance(body, (dict, list)):
            body = self.json_presenter.render(body)
            content_type = content_type or self.json_presenter.content_type
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length <= 0:
            raise ValueError("empty body")
        if length > 1_000_000:
            raise ValueError("body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON: %s" % exc)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, self.services.frontend.read(), "text/html; charset=utf-8")
        elif url.path == "/api/dashboard":
            try:
                limit = int(parse_qs(url.query).get("limit", [15])[0])
            except ValueError:
                limit = 15
            workflow = parse_qs(url.query).get("workflow", [DEFAULT_WORKFLOW])[0]
            self._send(200, self.services.dashboard.get_dashboard(max(1, min(limit, 100)), workflow))
        elif url.path == "/api/health":
            self._send(200, {"ok": True, "queued": self.services.queue.qsize(),
                             "runs": self.services.dashboard.count_runs()})
        elif url.path == "/events":
            self._sse()
        elif url.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/ingest":
                result = self.services.ingest.ingest(self._read_json())
                self._send(202, {"queued": True, "workflow": result.workflow,
                                 "commit": result.commit, "queue_depth": result.queue_depth})
            elif path == "/api/simulate":
                body = self._read_json() if self.headers.get("Content-Length") else {}
                workflow = body.get("workflow") or DEFAULT_WORKFLOW
                if not isinstance(workflow, str) or not WORKFLOW_NAME_RE.match(workflow):
                    raise ValueError("'workflow' must be 1-60 chars and start with a letter or number")
                result = self.services.simulation.simulate(workflow)
                self._send(202, {"queued": True, "workflow": result.workflow,
                                 "commit": result.commit, "queue_depth": result.queue_depth})
            elif path == "/api/checks":
                self._send(200, self.services.checks.upsert(self._read_json()))
                self.services.events.publish({"type": "checks"})
            else:
                self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", self.sse_presenter.content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        subscription = self.services.events.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\nevent: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = subscription.get(timeout=15)
                except Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(self.sse_presenter.render(message["type"], message))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.services.events.unsubscribe(subscription)


def handler_for(services):
    class ConfiguredDashboardHandler(DashboardHttpHandler):
        pass

    ConfiguredDashboardHandler.services = services
    return ConfiguredDashboardHandler
