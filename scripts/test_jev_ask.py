#!/usr/bin/env python3
"""Offline tests for jev_ask.py — no network (urlopen monkeypatched).

Pins the quota-fallback contract: the free tier (jev-1.13-free, /zen/v1/
systemone) 429s account-wide for long stretches, so 429/402 must re-ask the Go
tier (chat-completions) and return the same {"answers": {qid: {"noul": f}}}
shape the callers (gate_summary.py) already consume. Auth errors and
malformed replies must keep failing instead of silently answering.
"""
import contextlib
import importlib
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
jev = importlib.import_module("jev_ask")


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestGoFallback(unittest.TestCase):
    Q = {"q_action": {"type": "noul", "instructions": "does this need action?"}}

    def setUp(self):
        self.calls = []
        p = mock.patch.dict(os.environ, {"ZEN_API_KEY": "test-key"})
        p.start()
        self.addCleanup(p.stop)

    def _patch(self, primary, fallback=None):
        def fake(req, timeout=None):
            self.calls.append((req.full_url, req))
            if req.full_url == jev.ENDPOINT:
                if isinstance(primary, int):
                    raise urllib.error.HTTPError(req.full_url, primary, "e", {}, None)
                return _Resp(primary)
            self.assertIsNotNone(fallback, "unexpected fallback call")
            body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(body["model"], jev.FALLBACK_MODEL)
            self.assertGreaterEqual(body["max_tokens"], 800)
            self.assertEqual(len(body["messages"]), 2)
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertIn("instructions", body["messages"][1]["content"])
            self.assertTrue(req.get_header("X-opencode-session"))
            return _Resp(fallback)
        return mock.patch.object(jev.urllib.request, "urlopen", side_effect=fake)

    def _ask(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            reply = jev.ask("DIGEST TEXT", self.Q)
        return reply, err.getvalue()

    def test_429_falls_back(self):
        with self._patch(429, _chat('{"answers": {"q_action": {"noul": 0.88}}}')):
            reply, err = self._ask()
        self.assertAlmostEqual(reply["answers"]["q_action"]["noul"], 0.88)
        self.assertIn("falling back to go/", err)
        self.assertEqual(self.calls[-1][0], jev.FALLBACK_ENDPOINT)

    def test_402_falls_back(self):
        with self._patch(402, _chat('{"answers": {"q_action": {"noul": 0.02}}}')):
            reply, err = self._ask()
        self.assertAlmostEqual(reply["answers"]["q_action"]["noul"], 0.02)
        self.assertIn("primary 402", err)

    def test_401_no_fallback(self):
        with self._patch(401):
            with self.assertRaises(RuntimeError):
                self._ask()
        self.assertEqual(len(self.calls), 1)

    def test_fenced_json(self):
        text = '```json\n{"answers": {"q_action": {"noul": 0.6}}}\n```'
        with self._patch(429, _chat(text)):
            reply, _ = self._ask()
        self.assertAlmostEqual(reply["answers"]["q_action"]["noul"], 0.6)

    def test_bare_number_answer_normalised(self):
        with self._patch(429, _chat('{"answers": {"q_action": 0.1}}')):
            reply, _ = self._ask()
        self.assertAlmostEqual(reply["answers"]["q_action"]["noul"], 0.1)

    def test_empty_content_reasoning_starved_raises(self):
        starved = {"choices": [{"message": {"role": "assistant", "content": ""},
                                "finish_reason": "length"}]}
        with self._patch(429, starved):
            with self.assertRaises(RuntimeError) as ctx:
                self._ask()
        self.assertIn("finish_reason=length", str(ctx.exception))

    def test_malformed_reply_raises(self):
        with self._patch(429, _chat("sorry, I cannot answer that")):
            with self.assertRaises(RuntimeError):
                self._ask()

    def test_primary_healthy_wins(self):
        with self._patch({"answers": {"q_action": {"noul": 0.11}}}):
            reply, err = self._ask()
        self.assertAlmostEqual(reply["answers"]["q_action"]["noul"], 0.11)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("falling back", err)

    def test_choice_type(self):
        q = {"q_pick": {"type": "choice", "instructions": "pick"}}
        with self._patch(429, _chat('{"answers": {"q_pick": {"choice": "tech"}}}')):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                reply = jev.ask("STATE", q)
        self.assertEqual(reply["answers"]["q_pick"]["choice"], "tech")


if __name__ == "__main__":
    unittest.main()
