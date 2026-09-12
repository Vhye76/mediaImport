import hashlib
import json
import logging
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import provider as providermod, state

log = logging.getLogger("webui")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

POSTER_TIMEOUT = 20
POSTER_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
POSTER_CACHE_CONTROL = "public, max-age=604800, immutable"


#----- the key is also the cache file name, so nothing but hex may reach the path join.
POSTER_KEY = re.compile(r"^[0-9a-f]{64}$")


def poster_key(url):
    return hashlib.sha256(url.encode()).hexdigest()


def fetch_poster_bytes(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": providermod.USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=POSTER_TIMEOUT) as response:
            if response.getcode() != 200:
                return None
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        log.info("poster fetch failed for %s: %s", url, exc)
        return None


class TLSError(RuntimeError):
    pass


#----- TLS
def build_ssl_context(cfg):
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    except (OSError, ssl.SSLError) as exc:
        raise TLSError("cannot load %s and %s: %s" % (cfg.tls_cert, cfg.tls_key, exc))
    return context


#----- Request handling
class Handler(BaseHTTPRequestHandler):
    server_version = "mediaimport"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, content_type="application/json", cache="no-store"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
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
                return self._json(200, [self.app.annotate(r) for r in self.app.store.all()])
            if path == "/api/held":
                return self._json(200, self.app.store.needs_decision())
            if path == "/api/logs":
                return self._send(200, self.app.log_tail(), "text/plain; charset=utf-8")
            if path == "/api/audit":
                return self._json(200, self.app.audit())
            if path.startswith("/api/poster/"):
                found = self.app.poster(path.rsplit("/", 1)[1])
                if found is None:
                    return self._json(404, {"error": "no poster"})
                data, content_type = found
                return self._send(200, data, content_type, POSTER_CACHE_CONTROL)
            if path.startswith("/api/titles/"):
                title_id = int(path.rsplit("/", 1)[1])
                row = self.app.store.get(title_id)
                if row is None:
                    return self._json(404, {"error": "no such title"})
                row["history"] = self.app.store.history(title_id)
                return self._json(200, self.app.annotate(row))
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
        if path == "/api/audit":
            return self._json(200, self.app.sweep(bool(body.get("rescan"))))
        if len(m) == 5 and m[1] == "api" and m[2] == "audit" and m[4] == "import":
            try:
                finding_id = int(m[3])
            except ValueError:
                return self._json(400, {"error": "bad finding id"})
            try:
                result = self.app.orchestrator.import_finding(finding_id)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, result)
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


#----- The server
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

    #----- Library audit
    def audit(self):
        return {
            "status": self.orchestrator.auditor.status(),
            "findings": [self.annotate_finding(f) for f in self.store.findings()],
        }

    def sweep(self, rescan=False):
        if rescan:
            self.orchestrator.auditor.rescan()
            return {"ok": True, "action": "findings wiped, full rescan requested"}
        self.orchestrator.auditor.sweep_now()
        return {"ok": True, "action": "sweep requested"}

    def annotate_finding(self, finding):
        title_id = finding.get("imported_title_id")
        finding["in_pipeline"] = None
        finding["copy"] = self.orchestrator.import_progress(finding["id"])
        if title_id:
            row = self.store.get(title_id)
            if row and state.in_pipeline(row["stage"]):
                finding["in_pipeline"] = {
                    "id": row["id"],
                    "stage": row["stage"],
                    "display_stage": state.display_name(row["stage"]),
                }
        elif finding.get("import_path") and os.path.exists(finding["import_path"]):
            finding["in_pipeline"] = {
                "id": None,
                "stage": state.DETECTED,
                "display_stage": "awaiting detection",
            }
        finding["name"] = os.path.splitext(os.path.basename(finding["path"]))[0]
        return finding

    def log_tail(self, lines=400):
        if not self.log_path or not os.path.isfile(self.log_path):
            return "no log file configured"
        with open(self.log_path, errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])

    #----- Operator decisions
    def annotate(self, row):
        if not row:
            return row
        row["display_stage"] = state.display_name(row.get("stage"))
        if row.get("stage") == state.ROUTED and (row.get("decision") or {}).get("action") == "passthrough":
            row["display_stage"] = "waiting for passthrough"
        row["complete"] = state.is_complete(row.get("stage"))
        row["poster"] = poster_key(row["poster_url"]) if row.get("poster_url") else None
        row["forceable"] = row.get("stage") == state.HELD
        output = row.get("output_path")
        source = row.get("source_path")
        quarantined = row.get("quarantine_path")
        row["output_present"] = bool(output) and os.path.exists(output)
        row["source_present"] = bool(source) and os.path.exists(source)
        row["quarantine_present"] = bool(quarantined) and os.path.exists(quarantined)
        row["files_gone"] = not (
            row["output_present"] or row["source_present"] or row["quarantine_present"]
        )
        return row

    def poster(self, key):
        if not POSTER_KEY.match(key or ""):
            return None
        directory = os.path.join(self.cfg.media_config, "cache", "posters")
        for extension, content_type in POSTER_TYPES.items():
            path = os.path.join(directory, key + extension)
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as fh:
                        return fh.read(), content_type
                except OSError:
                    break
        url = next((u for u in self.store.poster_urls() if poster_key(u) == key), None)
        if not url:
            return None
        extension = os.path.splitext(urlparse(url).path)[1].lower()
        if extension not in POSTER_TYPES:
            extension = ".jpg"
        path = os.path.join(directory, key + extension)
        data = fetch_poster_bytes(url)
        if not data:
            return None
        try:
            os.makedirs(directory, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
            log.debug("cached poster %s at %s", key, path)
        except OSError as exc:
            log.debug("could not cache poster %s: %s", key, exc)
        return data, POSTER_TYPES[extension]

    def decide(self, title_id, action):
        row = self.store.get(title_id)
        if row is None:
            raise ValueError("no such title")

        if action == "forget":
            self.annotate(row)
            if not row["files_gone"]:
                raise ValueError(
                    "title still has files on disk, remove them before forgetting it"
                )
            self.store.forget(title_id)
            log.info("operator forgot title %s, its files are already gone", title_id)
            return {"ok": True, "action": "forgotten"}

        if row["stage"] not in (state.HELD, state.FAILED):
            raise ValueError(
                "title is not awaiting a decision, it is at %s" % row["stage"]
            )

        if action in ("keep", "retry"):
            self.orchestrator.refresh_lookup(title_id)
            self.store.advance(title_id, state.DETECTED, "operator asked for a retry")
            self.orchestrator.queue.put(title_id)
            return {"ok": True, "action": "requeued"}
        if action == "override":
            if row["stage"] != state.HELD:
                raise ValueError(
                    "force cannot apply to a failed title: no output was produced, "
                    "so there is nothing to carry forward; retry instead"
                )
            self.store.update(title_id, overridden=1)
            self.store.advance(title_id, state.DETECTED, "operator overrode the gates")
            self.orchestrator.queue.put(title_id)
            return {"ok": True, "action": "overridden and requeued"}
        if action == "discard":
            outcome = self.orchestrator._quarantine(
                title_id, row["source_path"], "discarded by operator"
            )
            log.info("operator discarded title %s: %s", title_id, outcome)
            return {"ok": True, "action": outcome}
        raise ValueError("action must be one of keep, retry, override, discard, forget")

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
