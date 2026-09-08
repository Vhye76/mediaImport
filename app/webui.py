import json
import logging
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import state

log = logging.getLogger("webui")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class TLSError(RuntimeError):
    pass


def build_ssl_context(cfg):
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    except (OSError, ssl.SSLError) as exc:
        raise TLSError("cannot load %s and %s: %s" % (cfg.tls_cert, cfg.tls_key, exc))
    return context


class Handler(BaseHTTPRequestHandler):
    server_version = "mediaimport"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, data):
        self._send(status, json.dumps(data, default=str, indent=2))

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(200, self.app.status())
            if path == "/api/titles":
                return self._json(200, self.app.store.all())
            if path == "/api/held":
                return self._json(200, self.app.store.held())
            if path == "/api/logs":
                return self._send(200, self.app.log_tail(), "text/plain; charset=utf-8")
            if path.startswith("/api/titles/"):
                title_id = int(path.rsplit("/", 1)[1])
                row = self.app.store.get(title_id)
                if row is None:
                    return self._json(404, {"error": "no such title"})
                row["history"] = self.app.store.history(title_id)
                return self._json(200, row)
        except ValueError:
            return self._json(400, {"error": "bad request"})
        except Exception as exc:
            log.exception("GET %s failed", path)
            return self._json(500, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (TypeError, ValueError):
            return self._json(400, {"error": "body must be JSON"})

        m = path.split("/")
        if len(m) == 5 and m[1] == "api" and m[2] == "held" and m[4] == "decision":
            try:
                title_id = int(m[3])
            except ValueError:
                return self._json(400, {"error": "bad title id"})
            action = (body.get("action") or "").lower()
            try:
                result = self.app.decide(title_id, action)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, result)
        return self._json(404, {"error": "not found"})

    def _static(self, name, content_type):
        target = os.path.join(STATIC, name)
        if not os.path.isfile(target):
            return self._json(404, {"error": "missing static asset"})
        with open(target, "rb") as fh:
            return self._send(200, fh.read(), content_type)


class WebUI:
    def __init__(self, cfg, orchestrator, store, log_path=None):
        self.cfg = cfg
        self.orchestrator = orchestrator
        self.store = store
        self.log_path = log_path
        self.httpd = None
        self.thread = None

    def status(self):
        return self.orchestrator.status()

    def log_tail(self, lines=400):
        if not self.log_path or not os.path.isfile(self.log_path):
            return "no log file configured"
        with open(self.log_path, errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])

    def decide(self, title_id, action):
        row = self.store.get(title_id)
        if row is None:
            raise ValueError("no such title")
        if row["stage"] != state.HELD:
            raise ValueError("title is not held, it is at %s" % row["stage"])

        if action in ("keep", "retry"):
            self.store.advance(title_id, state.DETECTED, "operator asked for a retry")
            self.orchestrator.queue.put(title_id)
            return {"ok": True, "action": "requeued"}
        if action == "override":
            self.store.update(title_id, overridden=1)
            self.store.advance(title_id, state.DETECTED, "operator overrode the standards gate")
            self.orchestrator.queue.put(title_id)
            return {"ok": True, "action": "overridden and requeued"}
        if action == "discard":
            outcome = self.orchestrator._quarantine(
                title_id, row["source_path"], "discarded by operator"
            )
            log.info("operator discarded title %s: %s", title_id, outcome)
            return {"ok": True, "action": outcome}
        raise ValueError("action must be one of keep, retry, override, discard")

    def start(self):
        context = build_ssl_context(self.cfg)
        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.cfg.web_port), Handler)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.httpd.app = self
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="webui", daemon=True
        )
        self.thread.start()
        log.info("web UI listening on https://0.0.0.0:%d", self.cfg.web_port)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
