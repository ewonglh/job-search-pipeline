#!/usr/bin/env python3
"""Loopback server route, bounds, privacy, and restart smoke tests."""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest import mock

import dashboard_server as server_module
import projection
from test_dashboard_views import synthetic_snapshot


def start_server(port=0, db_path=None):
    server = server_module.DashboardHTTPServer(
        (server_module.HOST, port), server_module.DashboardHandler, db_path=db_path)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive(), "server thread did not stop"


def request(url, method="GET"):
    try:
        with urlopen(url, timeout=3) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def raw_request(port, data):
    connection = socket.create_connection((server_module.HOST, port), timeout=3)
    try:
        connection.sendall(data)
        return connection.recv(8192)
    finally:
        connection.close()


def test_only_exact_routes_and_methods_are_available():
    snapshot = synthetic_snapshot()
    with mock.patch.object(projection, "read_snapshot", return_value=snapshot):
        server, thread = start_server()
        try:
            base = "http://{}:{}".format(server_module.HOST, server.server_address[1])
            for path in ("/", "/index.html", "/jobs", "/jobs/", "/jobs/home/"):
                status, headers, body = request(base + path)
                assert status == 200 and b"Private jobs dashboard" in body
                assert b"Daily submission goal" in body
                assert headers.get("Cache-Control") == "no-store, max-age=0"
                assert headers.get("X-Content-Type-Options") == "nosniff"
                assert headers.get("Content-Security-Policy")

            page_routes = (
                ("/jobs/applications/", b"Acme &amp; Co", b"Daily submission goal"),
                ("/jobs/analytics/", b"Eligible applications", b"Acme &amp; Co"),
                ("/jobs/runs/", b"job-alpha", b"Acme &amp; Co"),
                ("/jobs/tailoring/", b"First-pass acceptance", b"Acme &amp; Co"),
                ("/jobs/system-health/", b"Source health", b"Acme &amp; Co"),
            )
            for path, present, absent in page_routes:
                status, headers, body = request(base + path)
                assert status == 200 and present in body and absent not in body, path
                assert headers.get("Cache-Control") == "no-store, max-age=0"
                assert b"aria-current='page'" in body
            status, headers, body = request(base + "/api/snapshot.json")
            assert status == 200 and headers.get_content_type() == "application/json"
            assert json.loads(body)["snapshot_id"] == snapshot["snapshot_id"]
            assert b"no-store" in headers.get("Cache-Control").encode("ascii")
            status, _headers, body = request(base + "/jobs/api/snapshot.json")
            assert status == 200 and json.loads(body)["snapshot_id"] == snapshot["snapshot_id"]
            status, _headers, body = request(base + "/healthz")
            assert status == 200 and json.loads(body)["status"] == "degraded"

            for path in ("/private.sqlite3", "/scripts/dashboard_server.py",
                         "/api/snapshot.json?download=1", "/api%2Fsnapshot.json",
                         "/%2e%2e/private.sqlite3", "/.git/config",
                         "/jobs/applications/?tab=all", "/jobs//applications/"):
                status, _headers, body = request(base + path)
                assert status == 404, (path, status, body)

            connection = http.client.HTTPConnection(
                server_module.HOST, server.server_address[1], timeout=3)
            connection.request("POST", "/", body="SYNTHETIC_POST_CANARY")
            response = connection.getresponse()
            body = response.read()
            assert response.status == 405 and "GET" in response.getheader("Allow", "")
            assert b"SYNTHETIC_POST_CANARY" not in body
            connection.close()

            connection = http.client.HTTPConnection(
                server_module.HOST, server.server_address[1], timeout=3)
            connection.request("HEAD", "/")
            response = connection.getresponse()
            response.read()
            assert response.status == 405
            connection.close()
        finally:
            stop_server(server, thread)


def test_private_files_are_never_served_and_errors_are_generic():
    snapshot = synthetic_snapshot()
    with tempfile.TemporaryDirectory() as root:
        private_path = os.path.join(root, "private.sqlite3")
        with open(private_path, "w", encoding="utf-8") as stream:
            stream.write("SYNTHETIC_PRIVATE_FILE_CANARY")
        with mock.patch.object(projection, "read_snapshot", side_effect=projection.ProjectionError(
                "C:\\synthetic-private\\private.sqlite3")):
            server, thread = start_server(db_path=private_path)
            try:
                base = "http://{}:{}".format(server_module.HOST, server.server_address[1])
                status, _headers, body = request(base + "/healthz")
                assert status == 503
                assert b"C:\\synthetic-private" not in body
                assert b"private.sqlite3" not in body
                assert json.loads(body) == {"code": "snapshot_unavailable",
                                           "status": "unavailable"}
                status, _headers, body = request(base + "/private.sqlite3")
                assert status == 404 and b"SYNTHETIC_PRIVATE_FILE_CANARY" not in body
                assert b"SYNTHETIC_PRIVATE_FILE_CANARY" not in request(base + "/")[2]
            finally:
                stop_server(server, thread)


def test_loopback_only_bind_and_request_bounds():
    try:
        server_module.DashboardHTTPServer(("0.0.0.0", 0), server_module.DashboardHandler)
    except ValueError as exc:
        assert "127.0.0.1" in str(exc)
    else:
        raise AssertionError("non-loopback bind was accepted")

    with mock.patch.object(projection, "read_snapshot", return_value=synthetic_snapshot()):
        server, thread = start_server()
        try:
            port = server.server_address[1]
            too_long = b"GET /" + b"a" * (server_module.MAX_REQUEST_LINE + 10) + b" HTTP/1.1\r\nHost: local\r\n\r\n"
            response = raw_request(port, too_long)
            assert b"414" in response.split(b"\r\n", 1)[0], response[:200]

            large_header = (b"GET /healthz HTTP/1.1\r\nHost: local\r\nX-Large: "
                            + b"a" * (server_module.MAX_HEADER_BYTES + 20)
                            + b"\r\n\r\n")
            response = raw_request(port, large_header)
            assert b"431" in response.split(b"\r\n", 1)[0], response[:200]

            many_headers = (b"GET /healthz HTTP/1.1\r\nHost: local\r\n" +
                            b"".join(b"X-Test: x\r\n" for _ in range(server_module.MAX_HEADER_COUNT + 1)) +
                            b"\r\n")
            response = raw_request(port, many_headers)
            assert b"431" in response.split(b"\r\n", 1)[0], response[:200]
        finally:
            stop_server(server, thread)


def test_restart_smoke_on_same_ephemeral_loopback_port():
    snapshot = synthetic_snapshot()
    with mock.patch.object(projection, "read_snapshot", return_value=snapshot):
        first, first_thread = start_server()
        port = first.server_address[1]
        base = "http://{}:{}/api/snapshot.json".format(server_module.HOST, port)
        status, _headers, body = request(base)
        assert status == 200 and json.loads(body)["snapshot_id"] == snapshot["snapshot_id"]
        stop_server(first, first_thread)

        second, second_thread = start_server(port=port)
        try:
            status, _headers, body = request(base)
            assert status == 200 and json.loads(body)["snapshot_id"] == snapshot["snapshot_id"]
        finally:
            stop_server(second, second_thread)


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("  ok  {}".format(test.__name__))
    print("test_dashboard_server: all assertions passed")


if __name__ == "__main__":
    main()
