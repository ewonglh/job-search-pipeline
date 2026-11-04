#!/usr/bin/env python3
"""Offline self-check for gate_summary.py: bypass, suppress, fail-open, state.

No network: ``jev_ask.ask`` and ``summary.fetch_open_tailor_prs`` are
monkeypatched, so the gate logic itself is what runs. Run:
``python3 test_gate_summary.py``
"""
import contextlib
import io
import json
import os
import shutil
import tempfile

import gate_summary as g
import summary as s

NOCHANGES = "- company: Acme\n  role: Intern\n  status: tailored\n"
CHANGED = NOCHANGES + (
    "- company: Beta\n  role: Engineer\n  status: applied\n"
    f"  applied_date: {s.sgt_today().isoformat()}\n")


def run_gate(apps_yaml, jev, extra_env=None):
    """Run gate_summary.main() in a temp repo; jev is a noul value, an
    exception (fail-open), or None (assert-never-called)."""
    tmp = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "data"))
        with open(os.path.join(repo, "applications.yaml"), "w") as fh:
            fh.write(apps_yaml)
        with open(os.path.join(repo, "data", "postings.json"), "w") as fh:
            fh.write("[]")
        state_path = os.path.join(tmp, "state.json")
        calls = []

        def fake_ask(state_text, questions):
            calls.append(state_text)
            if isinstance(jev, Exception):
                raise jev
            return {"answers": {"action_needed": {"type": "noul", "noul": jev}}}

        real_ask, real_prs, real_state = (
            g.jev_ask.ask, g.S.fetch_open_tailor_prs, g.S.STATE_FILE)
        g.jev_ask.ask = fake_ask
        g.S.fetch_open_tailor_prs = lambda log=None: []
        g.S.STATE_FILE = state_path
        env_keys = ("JEV_DISABLE", "JEV_MIN_NOUL")
        env_backup = {k: os.environ.get(k) for k in env_keys}
        for k in env_keys:
            os.environ.pop(k, None)
        for k, v in (extra_env or {}).items():
            os.environ[k] = v
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = g.main(["--repo", repo])
        finally:
            g.jev_ask.ask, g.S.fetch_open_tailor_prs, g.S.STATE_FILE = (
                real_ask, real_prs, real_state)
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        state = None
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)["last_digest"]
        return rc, buf.getvalue(), state, calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# changed digest: announced as before, jev never consulted
rc, out, state, calls = run_gate(CHANGED, None)
assert rc == 0 and "New since" in out and not calls and state, (rc, out, calls)

# no-changes digest + jev low: suppressed silently, state still advances
rc, out, state, calls = run_gate(NOCHANGES, 0.1)
assert rc == 0 and out == "" and len(calls) == 1 and state, (rc, repr(out), calls)

# no-changes digest + jev high: delivered
rc, out, state, calls = run_gate(NOCHANGES, 0.9)
assert rc == 0 and "no changes" in out and len(calls) == 1, (rc, out, calls)

# jev failure: fail open — digest delivered unchanged
rc, out, state, calls = run_gate(NOCHANGES, RuntimeError("zen down"))
assert rc == 0 and "no changes" in out and len(calls) == 1, (rc, out, calls)

# JEV_DISABLE=1: gate bypassed, jev never consulted
rc, out, state, calls = run_gate(NOCHANGES, None, {"JEV_DISABLE": "1"})
assert rc == 0 and "no changes" in out and not calls, (rc, out, calls)

# JEV_MIN_NOUL override: 0.23 clears a 0.1 threshold
rc, out, state, calls = run_gate(NOCHANGES, 0.23, {"JEV_MIN_NOUL": "0.1"})
assert rc == 0 and "no changes" in out and len(calls) == 1, (rc, out, calls)

# empty tracker: silent digest, gate inactive, state written
rc, out, state, calls = run_gate("[]\n", 0.9)
assert rc == 0 and out == "" and not calls and state, (rc, out, calls)

# nudges/newpost pass through ungated: jev never consulted, no state write
for mode_args in (["nudges"], ["--mode", "newpost"]):
    import tempfile as _tf, shutil as _sh
    d = _tf.mkdtemp()
    try:
        r = os.path.join(d, "repo")
        os.makedirs(os.path.join(r, "data"))
        open(os.path.join(r, "applications.yaml"), "w").write(NOCHANGES)
        open(os.path.join(r, "data", "postings.json"), "w").write("[]")
        sp = os.path.join(d, "state.json")
        real_state = g.S.STATE_FILE
        real_tailor = g.S.fetch_open_tailor_prs
        real_ask = g.jev_ask.ask
        g.S.STATE_FILE = sp
        g.S.fetch_open_tailor_prs = lambda log=None: []
        g.jev_ask.ask = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("jev must not be consulted for nudges/newpost"))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = g.main(mode_args + ["--repo", r])
        finally:
            # Restore every patched seam: leaving g.jev_ask.ask raising leaked
            # into later tests in the same process (unittest discover ran
            # test_jev_ask after this file and saw 9 spurious failures).
            g.S.STATE_FILE = real_state
            g.S.fetch_open_tailor_prs = real_tailor
            g.jev_ask.ask = real_ask
        assert rc == 0, (mode_args, rc)
        assert not os.path.exists(sp), (mode_args, "state must not advance")
    finally:
        _sh.rmtree(d, ignore_errors=True)

print("test_gate_summary: all checks passed")
