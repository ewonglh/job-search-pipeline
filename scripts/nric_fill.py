#!/usr/bin/env python3
"""Sentinel-fill helper: type the owner's NRIC into the focused form field
of the active managed-Chromium tab via CDP, without the value ever entering
agent context, a log, or a transcript.

Security model
--------------
The value is read ONLY from the environment variable ``NRIC_SG`` (kind ``env``
in the OpenClaw secret store). It is never accepted as a CLI argument, so it
never lands in shell history or a process list. This program prints exactly one
status line and nothing else:

    nric_fill: filled
    nric_fill: present | nric_fill: missing      (--check)
    nric_fill: error: <short reason>             (failure)

It never prints, logs, returns, hashes, or measures-in-output the value, its
length, or any part of it. ``Input.insertText`` carries the value to the page;
that payload is only ever written to the CDP socket, never to stdout/stderr or
a file.

Targeting
---------
The browser is the managed Chromium that the ``openclaw browser`` lane and
``gmail_browser.py`` use (CDP on ``127.0.0.1:18800``, profile ``openclaw``).
Discovery defaults to that endpoint -- the same ``cdpUrl`` reported by
``openclaw browser status --json`` -- and can be overridden with the
environment:

    NRIC_FILL_CDP_HTTP   base HTTP endpoint for target discovery
                         (default http://127.0.0.1:18800)
    NRIC_FILL_CDP_WS     a page-level CDP websocket URL to fill directly
    NRIC_FILL_TARGET     a specific page targetId to fill

"Active tab" resolution: headless Chromium reports ``visibilityState``
``visible`` for every tab, so tab visibility cannot identify the active one.
Instead this helper probes every page target and picks the one whose
CURRENTLY FOCUSED element (``document.activeElement``) is a text-editable
input/textarea/contenteditable -- i.e. the field the operator just focused.
Exactly one such page is expected; if several match (or none) it fails loudly
rather than guessing.

Usage:
    nric_fill.py --check      # present | missing (no browser needed)
    nric_fill.py              # type into the focused element, print status
"""

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse

ENV_NAME = "NRIC_SG"
DEFAULT_CDP_HTTP = "http://127.0.0.1:18800"
HTTP_TIMEOUT = 5.0
WS_TIMEOUT = 15.0

# Input types that are not text entry targets.
NON_TEXT_INPUT_TYPES = frozenset([
    "checkbox", "radio", "file", "submit", "reset", "button", "image",
    "range", "color", "hidden", "date", "time", "datetime-local",
    "month", "week",
])

STATUS_PREFIX = "nric_fill: "


# ------------------------------------------------------------------ output
def _status(line):
    """The only output this program ever produces."""
    sys.stdout.write(STATUS_PREFIX + line + "\n")
    sys.stdout.flush()


def _fail(reason):
    """Print a short error status and exit non-zero. ``reason`` is our text."""
    _status("error: " + reason)
    sys.exit(1)


# -------------------------------------------------------- secret (env only)
def read_secret():
    """Return the NRIC value from the environment, or None if absent/empty."""
    value = os.environ.get(ENV_NAME)
    if value is None or value.strip() == "":
        return None
    return value


# ------------------------------------------------------------ CDP plumbing
def _http_json(url, timeout=HTTP_TIMEOUT):
    """GET a JSON document, bypassing any configured egress proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.load(resp)


class CDPWebSocket:
    """Minimal RFC6455 client + CDP request/response (stdlib only)."""

    def __init__(self, url, timeout=WS_TIMEOUT):
        u = urlparse(url)
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self._sock.sendall(handshake.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("handshake closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise OSError("websocket handshake rejected")
        self._buf = rest

    # -- raw framing -----------------------------------------------------
    def _read(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("socket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, payload, opcode=0x1):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header))

    def _recv_message(self):
        data = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if (b2 & 0x80) else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:            # ping -> pong
                self._send_frame(payload, 0xA)
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode == 0x8:            # close
                raise OSError("websocket closed")
            data += payload
            if fin:
                return data.decode("utf-8", "replace")

    # -- CDP -------------------------------------------------------------
    def call(self, mid, method, params=None):
        msg = {"id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send_frame(json.dumps(msg).encode("utf-8"))
        while True:
            try:
                reply = json.loads(self._recv_message())
            except ValueError:
                raise RuntimeError("cdp error")
            if reply.get("id") != mid:
                continue  # unrelated protocol event
            if "error" in reply:
                # Never surface the error body: it could echo request params.
                raise RuntimeError("cdp error")
            return reply.get("result", {})

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


# ------------------------------------------------------- target discovery
_FOCUS_PROBE = (
    "(function(){var ae=document.activeElement;if(!ae){return '{}';}"
    "var tag=(ae.tagName||'').toLowerCase();"
    "var editable=tag==='input'||tag==='textarea'||ae.isContentEditable===true;"
    "var type=tag==='input'?(ae.type||'text'):tag;"
    "var textish=editable&&(tag!=='input'||%s.indexOf(String(type).toLowerCase())===-1);"
    "return JSON.stringify({editable:!!editable,textish:!!textish});})()"
    % json.dumps(sorted(NON_TEXT_INPUT_TYPES))
)

_VALUE_LEN_PROBE = (
    "(function(){var ae=document.activeElement;if(!ae){return -1;}"
    "var v=(ae.value!==undefined?ae.value:String(ae.textContent||''));"
    "return v.length;})()"
)


def _cdp_http_base():
    override = os.environ.get("NRIC_FILL_CDP_HTTP")
    if override:
        return override.rstrip("/")
    return DEFAULT_CDP_HTTP


def _list_pages(base):
    """Return page targets (dicts) from the CDP HTTP discovery endpoint."""
    data = _http_json(base + "/json/list")
    pages = [t for t in data if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")]
    return pages


def _evaluate(ws, base_id, expression, attempts=4):
    """Evaluate and return the value, retrying while the execution context
    is not yet ready (a fresh target can yield ``None`` on the first call)."""
    for i in range(attempts):
        res = ws.call(base_id + i, "Runtime.evaluate",
                      {"expression": expression, "returnByValue": True})
        value = res.get("result", {}).get("value")
        if value is not None:
            return value
        time.sleep(0.2)
    return None


def _probe_page(ws_url):
    """Return focus info for a page, or None if it is not a usable target."""
    ws = None
    try:
        ws = CDPWebSocket(ws_url)
        try:
            ws.call(0, "Runtime.enable")  # required before evaluate on fresh targets
        except Exception:
            pass
        raw = _evaluate(ws, 1, _FOCUS_PROBE)
        info = json.loads(raw) if isinstance(raw, str) else {}
        return ws, info
    except Exception:
        if ws is not None:
            ws.close()
        return None, None


def select_target():
    """Open a CDP session on the page whose focused element is editable.

    Returns an open CDPWebSocket whose activeElement is the fill target.
    """
    env_ws = os.environ.get("NRIC_FILL_CDP_WS")
    env_target = os.environ.get("NRIC_FILL_TARGET")

    if env_ws:
        ws, info = _probe_page(env_ws)
        if ws is None:
            _fail("browser unreachable")
        if not (info or {}).get("textish"):
            ws.close()
            _fail("no focused element")
        return ws

    try:
        pages = _list_pages(_cdp_http_base())
    except Exception:
        _fail("browser unreachable")
    if not pages:
        _fail("browser unreachable")

    if env_target:
        pages = [t for t in pages if t.get("id") == env_target]
        if not pages:
            _fail("target not found")

    matches = []          # (ws, info) for pages with a focused text element
    open_ws = []          # every ws we opened, to close the unused ones
    for page in pages:
        ws, info = _probe_page(page["webSocketDebuggerUrl"])
        if ws is None:
            continue
        open_ws.append(ws)
        if (info or {}).get("textish"):
            matches.append((ws, info))

    if not matches:
        for ws in open_ws:
            ws.close()
        _fail("no focused element")
    if len(matches) > 1:
        for ws in open_ws:
            ws.close()
        _fail("ambiguous active tab")

    chosen = matches[0][0]
    for ws in open_ws:
        if ws is not chosen:
            ws.close()
    return chosen


# ------------------------------------------------------------------- main
def do_check():
    if read_secret() is None:
        _status("missing")
        sys.exit(1)
    _status("present")
    sys.exit(0)


def do_fill():
    secret = read_secret()
    if secret is None:
        _fail("NRIC_SG not set")

    ws = select_target()
    try:
        try:
            ws.call(2, "Input.insertText", {"text": secret})
        except Exception:
            _fail("cdp error")
        # Integrity check: compare the field's value LENGTH only. The value
        # itself never leaves the page, and only an integer crosses the wire.
        try:
            observed = _evaluate(ws, 10, _VALUE_LEN_PROBE)
        except Exception:
            _fail("cdp error")
        if observed != len(secret):
            _fail("verify failed")
    finally:
        ws.close()
    _status("filled")
    sys.exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="nric_fill.py",
        description="Type NRIC_SG into the focused element of the active tab.",
    )
    parser.add_argument("--check", action="store_true",
                        help="report only whether NRIC_SG is present")
    args = parser.parse_args(argv)

    if args.check:
        do_check()
    do_fill()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Never leak exception text that could embed the value.
        _fail("internal error")
