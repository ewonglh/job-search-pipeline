#!/usr/bin/env python3
"""Read-only loopback HTTP entrypoint for the cached jobs dashboard."""
import argparse
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dashboard
import projection
import application_explorer as explorer

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_LINE = 4096
MAX_HEADER_BYTES = 16384
MAX_HEADER_COUNT = 32
ROUTES = {
    "/": "page:home", "/index.html": "page:home",
    "/api/snapshot.json": "api", "/healthz": "health",
    # Exact aliases support mounting this app under a Tailscale Serve path.
    "/jobs": "page:home", "/jobs/": "page:home",
    "/jobs/home": "page:home", "/jobs/home/": "page:home",
    "/jobs/index.html": "page:home",
    "/jobs/applications": "page:applications",
    "/jobs/applications/": "page:applications",
    "/jobs/analytics": "page:analytics", "/jobs/analytics/": "page:analytics",
    "/jobs/runs": "page:runs", "/jobs/runs/": "page:runs",
    "/jobs/tailoring": "page:tailoring", "/jobs/tailoring/": "page:tailoring",
    "/jobs/system-health": "page:system-health",
    "/jobs/system-health/": "page:system-health",
    "/jobs/api/snapshot.json": "api", "/jobs/healthz": "health",
}
SECURITY_HEADERS = (
    ("Cache-Control", "no-store, max-age=0"),
    ("Pragma", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
     "img-src 'self' data:; font-src 'self'; connect-src 'none'; base-uri 'none'; "
     "form-action 'none'; frame-ancestors 'none'"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


class _BoundedReader:
    """Cap the number of header bytes consumed from a single request."""

    def __init__(self, reader, maximum):
        self.reader = reader
        self.maximum = maximum
        self.used = 0
        self.exceeded = False

    def readline(self, size=-1):
        remaining = self.maximum - self.used
        if remaining <= 0:
            self.exceeded = True
            return b""
        limit = remaining + 1
        if size is not None and size >= 0:
            limit = min(limit, size)
        chunk = self.reader.readline(limit)
        self.used += len(chunk)
        if self.used > self.maximum:
            self.exceeded = True
            chunk = chunk[:max(0, len(chunk) - (self.used - self.maximum))]
        elif self.used >= self.maximum and chunk and not chunk.endswith(b"\n"):
            self.exceeded = True
        return chunk


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded stdlib server that refuses any non-loopback bind address."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def handle_error(self, _request, _client_address):
        # Do not print request or parser details to a runtime log.
        return

    def __init__(self, server_address, handler_class, db_path=None):
        if not isinstance(server_address, tuple) or server_address[0] != HOST:
            raise ValueError("dashboard server must bind to 127.0.0.1")
        self.db_path = db_path
        super().__init__(server_address, handler_class)
        if self.server_address[0] != HOST:
            self.server_close()
            raise ValueError("dashboard server must bind to 127.0.0.1")


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PrivateDashboard"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, _format, *_args):
        # Request targets and response details stay out of runtime logs.
        return

    def log_request(self, *_args, **_kwargs):
        return

    def _write_response(self, status, content_type, body, headers=()):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # Never reflect the request target or parser diagnostics.
        status = 405 if code == 501 else code
        if status == 405:
            text = b"method not allowed\n"
            extra = (("Allow", "GET"),)
        elif status == 414:
            text = b"request target too long\n"
            extra = ()
        elif status == 431:
            text = b"request headers too large\n"
            extra = ()
        else:
            status = 400
            text = b"bad request\n"
            extra = ()
        self._write_response(status, "text/plain; charset=utf-8", text, extra)

    def parse_request(self):
        self.request_version = "HTTP/1.1"
        if len(getattr(self, "raw_requestline", b"")) > MAX_REQUEST_LINE:
            self.send_error(414)
            return False
        original = self.rfile
        bounded = _BoundedReader(original, MAX_HEADER_BYTES)
        self.rfile = bounded
        try:
            try:
                parsed = super().parse_request()
            except Exception:
                self.close_connection = True
                self.send_error(431 if bounded.exceeded else 400)
                return False
        finally:
            self.rfile = original
        if not parsed:
            return False
        if bounded.exceeded:
            self.send_error(431)
            return False
        if getattr(self, "headers", None) is not None:
            header_count = len(list(self.headers.raw_items()))
            if header_count > MAX_HEADER_COUNT:
                self.send_error(431)
                return False
        return parsed

    def _safe_get_only(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
            self.send_error(400)
            return False
        if lengths:
            try:
                if int(lengths[0]) != 0:
                    self.send_error(400)
                    return False
            except (TypeError, ValueError):
                self.send_error(400)
                return False
        if self.headers.get("Expect"):
            self.send_error(400)
            return False
        return True

    def _read_snapshot(self):
        return projection.read_snapshot(self.server.db_path)

    def _send_json(self, status, value):
        body = dashboard._safe_json_response(value).encode("utf-8")
        self._write_response(status, "application/json; charset=utf-8", body)

    def do_GET(self):
        target = self.path
        if not isinstance(target, str) or len(target) > MAX_REQUEST_LINE:
            self._write_response(404, "text/plain; charset=utf-8", b"not found\n")
            return
        if any(char in target for char in ("\\", "#", "\x00")):
            self._write_response(404, "text/plain; charset=utf-8", b"not found\n")
            return
        path, separator, query = target.partition("?")
        detail_match = re.fullmatch(r"/jobs/applications/([0-9a-f]{64})", path)
        app_path = path in ("/jobs/applications", "/jobs/applications/")
        if "%" in path or (path not in ROUTES and not detail_match):
            self._write_response(404, "text/plain; charset=utf-8", b"not found\n")
            return
        if separator and (not app_path or detail_match):
            self._write_response(404, "text/plain; charset=utf-8", b"not found\n")
            return
        query_state = None
        if app_path:
            try:
                query_state = explorer.parse_query(query if separator else "")
            except ValueError:
                self._write_response(400, "text/plain; charset=utf-8", b"bad request\n")
                return
        if not self._safe_get_only():
            return
        route = "page:application-detail" if detail_match else ROUTES[path]
        is_page = route.startswith("page:")
        try:
            snapshot = self._read_snapshot()
        except (projection.ProjectionError, OSError, ValueError, KeyError, TypeError):
            if is_page:
                body = dashboard.render_unavailable_html().encode("utf-8")
                self._write_response(503, "text/html; charset=utf-8", body)
            else:
                self._send_json(503, {"status": "unavailable",
                                      "code": "snapshot_unavailable"})
            return

        if is_page:
            try:
                page = route.split(":", 1)[1]
                if page == "application-detail":
                    detail = dashboard._application_detail(snapshot, detail_match.group(1))
                    if detail is None:
                        self._write_response(404, "text/html; charset=utf-8",
                            dashboard.render_snapshot_html(snapshot, page=page,
                                application_key=detail_match.group(1)).encode("utf-8"))
                        return
                    if detail == "ambiguous":
                        self._write_response(409, "text/html; charset=utf-8",
                            dashboard.render_snapshot_html(snapshot, page=page,
                                application_key=detail_match.group(1)).encode("utf-8"))
                        return
                    body = dashboard.render_snapshot_html(snapshot, page=page,
                        application_key=detail_match.group(1)).encode("utf-8")
                elif page == "applications":
                    body = dashboard.render_snapshot_html(snapshot, page=page,
                        query_state=query_state).encode("utf-8")
                else:
                    body = dashboard.render_snapshot_html(snapshot, page=page).encode("utf-8")
            except (ValueError, KeyError, TypeError, ZeroDivisionError):
                self._write_response(503, "text/html; charset=utf-8",
                                     dashboard.render_unavailable_html().encode("utf-8"))
                return
            self._write_response(200, "text/html; charset=utf-8", body)
            return
        if route == "api":
            self._send_json(200, snapshot)
            return
        if route == "health":
            states = {name: value.get("status", "unavailable")
                      for name, value in snapshot.get("sources", {}).items()
                      if isinstance(value, dict)}
            collecting = (snapshot.get("collection", {}).get("status") == "in_progress")
            status = ("ok" if states and all(value == "fresh" for value in states.values())
                      and not collecting else "degraded")
            self._send_json(200, {"status": status,
                                  "snapshot_id": snapshot.get("snapshot_id"),
                                  "sources": states})

    def do_HEAD(self):
        self.send_error(405)

    def do_POST(self):
        self.send_error(405)

    def do_PUT(self):
        self.send_error(405)

    def do_PATCH(self):
        self.send_error(405)

    def do_DELETE(self):
        self.send_error(405)

    def do_OPTIONS(self):
        self.send_error(405)

    def do_TRACE(self):
        self.send_error(405)

    def do_CONNECT(self):
        self.send_error(405)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="loopback port (default: %(default)s)")
    parser.add_argument("--db", default=None,
                        help="projection database path; read-only")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.port < 0 or args.port > 65535:
        print("dashboard_server: port must be between 0 and 65535", file=sys.stderr)
        return 2
    try:
        server = DashboardHTTPServer((HOST, args.port), DashboardHandler,
                                     db_path=args.db)
    except (OSError, ValueError):
        print("dashboard_server: could not bind the private loopback endpoint",
              file=sys.stderr)
        return 2
    actual_port = server.server_address[1]
    print("dashboard_server: listening on http://{}:{}/".format(HOST, actual_port),
          file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
