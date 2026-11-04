#!/usr/bin/env python3
"""Notify gate for the nightly summary lane (jev-1.13-free decision gate).

Computes the digest exactly as ``summary.py`` does (same functions), then:

  * new/changed digests, ``--json`` and ``--dry-run`` runs: announced as
    before, gate bypassed;
  * a "no changes" digest: additionally evaluated by Jev (opencode Zen
    System One, ``jev-1.13-free``) — announced only when Jev scores it
    ``action_needed >= JEV_MIN_NOUL`` (default 0.5). Fail-open: any Jev
    error or unexpected reply delivers the digest unchanged;
  * the state file advances the same way as before regardless of the gate
    outcome, so ``new since`` windows keep daily semantics.

Only digest mode is gated. ``nudges`` / ``newpost`` pass through ungated
(``--mode {digest,nudges,newpost}`` or the positional subcommand; bare
invocation = digest).

Set ``JEV_DISABLE=1`` to bypass the gate entirely. Jev auth/proxy follow
``jev_ask.py`` (Zen key file / ``ZEN_API_KEY``, standard proxy env — the
run-*.sh gateway-environ inheritance covers scheduled lanes).

Exit codes: 0 ok (digest text on stdout when delivered, silent otherwise)
· 2 bad args. Diagnostics on stderr, prefixed ``summary:`` (the cron payload
strips those, so they never reach delivery).
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jev_ask  # noqa: E402
import summary as S  # noqa: E402


def log(*a):
    print("summary:", *a, file=sys.stderr)


def jev_actionable(digest_text):
    """True when Jev says the no-changes digest is worth announcing.

    Raises on any failure — callers treat that as fail-open.
    """
    questions = {
        "action_needed": {
            "type": "noul",
            "instructions": (
                "The owner receives this nightly job-search digest only when it "
                "is worth reading. Does it contain anything new, unusual, or "
                "requiring action from him today? A routine, unchanged "
                "status — standing counts with nothing new — means no."
            ),
        }
    }
    data = jev_ask.ask(digest_text, questions)
    ans = (data.get("answers") or {}).get("action_needed") or {}
    if "noul" not in ans:
        raise ValueError(f"unexpected jev reply shape: {list(ans)}")
    return float(ans["noul"])


def main(argv=None):
    args = S.build_parser().parse_args(argv)
    if args.days < 0:
        log("--days must be >= 0")
        return 2
    mode = S.resolve_mode(args)
    if mode == "nudges":
        return S.run_nudges(args, log)
    if mode == "newpost":
        return S.run_newpost(args, log)

    apps, postings = S.load_inputs(args.repo, log=log)
    today = S.sgt_today()
    stored = S.read_state(S.STATE_FILE)
    last = stored if stored is not None else today - timedelta(days=1)
    if last > today:
        last = today - timedelta(days=1)

    prs = S.fetch_open_tailor_prs(log=log)
    if prs is None:
        log("tailor-PR check unavailable; tracker-only digest")
    res = S.compute(apps, postings, last, today, args.days, prs=prs)
    out = S.render_json(res) if args.json else S.render_text(res)

    gate_active = (
        not args.json and not args.dry_run
        and not os.environ.get("JEV_DISABLE")
        and bool(out) and not res["changed"])
    if gate_active:
        try:
            try:
                threshold = float(os.environ.get("JEV_MIN_NOUL", "0.5"))
            except ValueError:
                threshold = 0.5
            p = jev_actionable(out)
            log(f"jev gate action_needed={p:.2f} (threshold {threshold})")
            if p < threshold:
                if not args.dry_run:
                    S.write_state(S.STATE_FILE, today,
                                  S.datetime.now(S._SGT).isoformat(timespec="seconds"))
                log("no-changes digest suppressed by jev gate")
                return 0
        except Exception as exc:  # noqa: BLE001 - fail open
            log(f"jev gate unavailable ({exc}); delivering digest")

    if out:
        print(out)
    if not args.dry_run:
        S.write_state(S.STATE_FILE, today,
                      S.datetime.now(S._SGT).isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
