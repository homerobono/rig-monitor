"""Read-only HTTP API + static dashboard, served to the whole LAN."""

from __future__ import annotations

import gzip
import json
import logging
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import metrics
from .config import Config, WEB_DIR
from .collector import Collector
from .storage import Store

log = logging.getLogger("rigmon.server")

GZIP_MIN = 1024
MAX_POINTS = 2000
MAX_KEYS = 80

RANGES = [
    {"id": "5m", "label": "5 min", "seconds": 300},
    {"id": "15m", "label": "15 min", "seconds": 900},
    {"id": "30m", "label": "30 min", "seconds": 1800},
    {"id": "1h", "label": "1 hour", "seconds": 3600},
    {"id": "3h", "label": "3 hours", "seconds": 10800},
    {"id": "6h", "label": "6 hours", "seconds": 21600},
    {"id": "12h", "label": "12 hours", "seconds": 43200},
]


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "rigmon"
    protocol_version = "HTTP/1.1"
    # Keep-alive means one thread and one SQLite handle per connection. Without a
    # timeout a client that opens a socket and goes quiet would pin both forever.
    timeout = 30

    cfg: Config
    store: Store
    collector: Collector

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, content_type: str, status: int = 200, cache: str = "no-store"):
        headers = [("Content-Type", content_type), ("Cache-Control", cache),
                   ("Access-Control-Allow-Origin", "*")]
        if len(body) >= GZIP_MIN and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)
            headers.append(("Content-Encoding", "gzip"))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        self._send(_json_bytes(payload), "application/json; charset=utf-8", status)

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _window(self, q: dict) -> tuple[int, int]:
        now = int(time.time())
        # Nothing older than the rollup retention exists, so a wider window would only
        # buy an expensive scan of the whole table.
        longest = self.store.rollup_retention
        if "range" in q:
            span = min(longest, max(60, int(float(q["range"][0]))))
            return now - span, now
        t1 = int(float(q.get("to", [now])[0]))
        t0 = int(float(q.get("from", [t1 - 3600])[0]))
        if t0 >= t1:
            t0 = t1 - 60
        return max(t0, t1 - longest), t1

    # ------------------------------------------------------------------- routing
    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api(path[5:], q)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("request failed: %s", self.path)
            try:
                self._error(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _api(self, route: str, q: dict):
        if route == "config":
            return self._json({
                "metrics": [
                    {"key": m.key, "label": m.label, "short": m.short, "unit": m.unit,
                     "group": m.group, "color": m.color, "default_on": m.default_on,
                     "available": self.store.known(m.key)}
                    for m in metrics.CATALOG
                ],
                "groups": metrics.GROUPS,
                "ranges": RANGES,
                "temp_alert_c": self.cfg.temp_alert_c,
                "poll_interval": self.cfg.poll_interval,
                "coverage": self.store.coverage(),
                "server_time": int(time.time()),
            })

        if route == "status":
            st = self.collector.status()
            st["coverage"] = self.store.coverage()
            st["db_bytes"] = self.store.db_size_bytes()
            st["server_time"] = int(time.time())
            return self._json(st)

        if route == "latest":
            live = self.collector.snapshot()
            if not live:
                live = self.store.latest()
            return self._json({"server_time": int(time.time()), "values": live,
                               "ok": self.collector.status()["ok"]})

        if route == "sensors":
            return self._json({"sensors": self.collector.sensor_list()})

        if route == "series":
            keys = [k for k in ",".join(q.get("keys", [])).split(",") if k][:MAX_KEYS]
            if not keys:
                keys = [m.key for m in metrics.CATALOG if m.default_on]
            t0, t1 = self._window(q)
            points = max(10, min(MAX_POINTS, int(float(q.get("points", [900])[0]))))
            aggs = tuple(a for a in ",".join(q.get("aggs", ["avg,max"])).split(",") if a)
            return self._json(self.store.query(keys, t0, t1, points, aggs))

        if route == "summary":
            keys = [k for k in ",".join(q.get("keys", [])).split(",") if k][:MAX_KEYS]
            if not keys:
                keys = metrics.temp_keys()
            t0, t1 = self._window(q)
            threshold = float(q.get("threshold", [self.cfg.temp_alert_c])[0])
            return self._json({
                "from": t0, "to": t1, "threshold": threshold,
                "metrics": self.store.summarize(keys, t0, t1, threshold),
            })

        return self._error(404, f"unknown endpoint /api/{route}")

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error(403, "forbidden")
        if not target.is_file():
            return self._error(404, "not found")
        ctype, _ = mimetypes.guess_type(str(target))
        cache = "no-cache" if target.suffix in (".html",) else "public, max-age=3600"
        self._send(target.read_bytes(), ctype or "application/octet-stream", cache=cache)


def serve(cfg: Config, store: Store, collector: Collector) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,),
                   {"cfg": cfg, "store": store, "collector": collector})
    httpd = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), handler)
    httpd.daemon_threads = True
    return httpd
