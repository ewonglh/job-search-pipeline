#!/usr/bin/env python3
"""Posting-integrity gate for the job-search pipeline (stdlib only).

Job-posting text is written by strangers and flows into agent contexts (the
tailor agent reads work items' ``jd_text``; future agent layers read the rank
cache) and into regex scoring. A malicious posting can hide text or embed
instructions aimed at the AI reading it. The gate sanitizes and flags; it
never obeys or executes content.

Pipeline order per call: strip invisible/bidi characters -> drop
<script>/<style> blocks and HTML comments -> remove covertly hidden elements
(their content is REMOVED, never unwrapped) -> flag and drop
instruction-shaped lines (one pass while markup is still present, so
pseudo-tags such as <system> are caught, then another after the remaining
tags are stripped, so tag-fragmented injections are caught too) -> strip
remaining tags. Every removal is recorded as a finding; ``clean_text`` is
the fully sanitized result.

Verdict thresholds (documented here as the contract):
  * 0 findings                       -> ``clean``
  * >= 1 finding                     -> ``flagged``
  * HOSTILE when either holds:
      - more than 3 injection lines (``HOSTILE_INJECTION_COUNT = 3``), OR
      - injection lines exceed 25% of the non-empty input lines
        (``HOSTILE_INJECTION_RATIO = 0.25``).

``guard_text`` is idempotent: ``guard_text(guard_text(x)["clean_text"])
["clean_text"] == guard_text(x)["clean_text"]``, and the second pass reports
no findings.

Stdlib only. No secrets, no model calls, no cron jobs created by this code.

Usage:
  python3 posting_guard.py --file PATH | --stdin   # JSON guard dict on stdout
  python3 posting_guard.py --file PATH --digest    # one-line human verdict
"""

import argparse
import json
import re
import sys

# ---------------------------------------------------------------- thresholds
# HOSTILE when injection lines exceed either of these. Kept module-level so
# the tailor/rank integrations and tests share one documented contract.
HOSTILE_INJECTION_COUNT = 3
HOSTILE_INJECTION_RATIO = 0.25

# ---------------------------------------------------------------- patterns
# Invisible / bidi / layout-trick code points stripped by step (a).
# U+200B-200D zero-width space/non-joiner/joiner, U+2060 word joiner,
# U+FEFF zero-width no-break space, U+202A-202E bidi controls,
# U+2066-2069 isolate controls, U+00AD soft hyphen, U+180E mongolian vowel sep.
INVISIBLE_RE = re.compile(
    "[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069\u00ad\u180e]")

# Instruction-shaped line shapes (matched case-insensitively, one line each).
# Each entry is (name, pattern); the name is for maintainers, the finding kind
# is always 'injection'. Kept in one documented constant so new shapes are
# added here, not scattered through call sites.
INJECTION_PATTERNS = (
    ("ignore-previous", r"ignore\s+(all|any|previous|prior|above|earlier)"),
    ("disregard", r"disregard\s+(all|any|previous|prior|the)"),
    ("system-prompt", r"system\s*(prompt|message)"),
    ("pseudo-tag", r"</?\s*(system|assistant)\s*>"),
    ("you-are-ai", r"you\s+are\s+(now\s+)?(an?|the)?\s*"
                   r"(ai|assistant|agent|llm|chatbot|system)"),
    ("act-as", r"act\s+as\s+(an?|the)"),
    ("as-an-ai", r"as\s+an\s+ai"),
    ("important-ignore", r"important[!.:]{0,3}\s*(please\s+)?ignore"),
    ("do-not-follow", r"(do\s+not|don'?t)\s+(follow|obey|reveal|mention|tell)"
                      r"\s+(any\s+)?(the\s+)?(previous|above|user|system|instructions)"),
    ("repeat-system", r"(repeat|print|output)\s+(the\s+)?(system|instructions?)"),
    ("new-instructions", r"new\s+instructions?"),
    ("agent-directive", r"(assistant|ai|agent)\s+(directive|instructions?)\s*:"),
)
INJECTION_RES = [(name, re.compile(pat, re.I))
                 for name, pat in INJECTION_PATTERNS]

# A tag at all (decides whether step-b HTML handling runs).
MARKUP_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
# Any URL (used to count hidden_link findings inside removed constructs).
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)

SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# A hidden element: opening tag carrying a hiding signal, through its matching
# close tag. Applied iteratively (innermost first) until no match remains.
HIDDEN_EL_RE = re.compile(
    r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?"
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0"
    r"|font-size\s*:\s*0(?:px|pt|em|rem|%)?|(?:left|top)\s*:\s*-\d{3,}px"
    r"|aria-hidden\s*=\s*[\"']true[\"'])[^>]*>"
    r"(?:(?!</?\1\b).)*?</\1\s*>",
    re.S | re.I)
# Hidden void/self-closing element (<input type=hidden>, <div .../>): no body.
HIDDEN_VOID_RE = re.compile(
    r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?"
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0"
    r"|font-size\s*:\s*0(?:px|pt|em|rem|%)?|(?:left|top)\s*:\s*-\d{3,}px"
    r"|aria-hidden\s*=\s*[\"']true[\"']|(?:\s+|^)hidden(?:\s+|/?>))[^>]*?/?>",
    re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _line_of(text, pos):
    """1-based line number of offset ``pos`` in ``text``."""
    return text.count("\n", 0, pos) + 1


def _evidence(snippet):
    """Finding evidence: one line, at most 120 chars."""
    return re.sub(r"\s+", " ", snippet or "").strip()[:120]


def guard_text(text):
    """Sanitize posting text; return {verdict, clean_text, findings}.

    ``findings`` entries are {kind, line (1-based, -1 if N/A), evidence
    (<=120 chars)}. Kinds: ``invisible_chars``, ``hidden_text``,
    ``hidden_link``, ``injection``.
    """
    if not isinstance(text, str):
        text = str(text or "")
    findings = []
    # Denominator for the hostile-ratio rule: non-empty lines of the input.
    input_non_empty = sum(1 for ln in text.split("\n") if ln.strip())

    # ---- (a) invisible / bidi characters, tracked per original line.
    lines = text.split("\n")
    stripped_lines = []
    for i, ln in enumerate(lines, 1):
        if INVISIBLE_RE.search(ln):
            findings.append({"kind": "invisible_chars", "line": i,
                             "evidence": _evidence(INVISIBLE_RE.sub("", ln))
                             or _evidence(ln)})
            ln = INVISIBLE_RE.sub("", ln)
        stripped_lines.append(ln)
    text = "\n".join(stripped_lines)

    # ---- (b) HTML constructs (only when the text contains markup).
    if MARKUP_RE.search(text):
        def drop(pattern, chunk, kind="hidden_text"):
            """Remove every ``pattern`` match; one finding per match."""
            while True:
                m = pattern.search(chunk)
                if not m:
                    return chunk
                body = m.group(0)
                for url in URL_RE.findall(body):
                    findings.append({"kind": "hidden_link", "line": -1,
                                     "evidence": _evidence(url)})
                findings.append({"kind": kind,
                                 "line": _line_of(chunk, m.start()),
                                 "evidence": _evidence(TAG_RE.sub(" ", body))})
                chunk = chunk[:m.start()] + "\n" + chunk[m.end():]

        text = drop(SCRIPT_STYLE_RE, text)
        text = drop(COMMENT_RE, text)
        prev = None
        while prev != text:
            prev = text
            text = drop(HIDDEN_EL_RE, text)
        text = drop(HIDDEN_VOID_RE, text)
        # NOTE: remaining tags are stripped AFTER the pre-strip injection
        # pass below, so pseudo-tags such as <system> are still detectable.

    # ---- (c) instruction-shaped lines: flagged and dropped. Two passes:
    # pre-strip (catches pseudo-tags such as <system> while the markup is
    # still present) then post-strip (catches injections fragmented across
    # inline tags, e.g. ``Ignore <b>all</b> previous ...``). Tag stripping
    # never adds or removes lines, so line numbers stay comparable.
    def drop_injections(chunk):
        kept, n = [], 0
        for i, ln in enumerate(chunk.split("\n"), 1):
            if ln.strip() and any(rx.search(ln) for _, rx in INJECTION_RES):
                n += 1
                findings.append({"kind": "injection", "line": i,
                                 "evidence": _evidence(TAG_RE.sub(" ", ln))})
                continue
            kept.append(ln)
        return "\n".join(kept), n
    text, n_pre = drop_injections(text)
    # ---- strip whatever tags remain (visible markup is layout, not content).
    text = TAG_RE.sub(" ", text)
    clean_text, n_post = drop_injections(text)
    n_injection = n_pre + n_post

    # ---- verdict.
    if n_injection > HOSTILE_INJECTION_COUNT or (
            input_non_empty
            and n_injection / input_non_empty > HOSTILE_INJECTION_RATIO):
        verdict = "hostile"
    elif findings:
        verdict = "flagged"
    else:
        verdict = "clean"
    return {"verdict": verdict, "clean_text": clean_text, "findings": findings}


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="posting text file to gate")
    src.add_argument("--stdin", action="store_true",
                     help="read posting text from stdin")
    ap.add_argument("--digest", action="store_true",
                    help="print a one-line human verdict instead of JSON")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.stdin:
        text = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    result = guard_text(text)
    if args.digest:
        kinds = sorted({f["kind"] for f in result["findings"]})
        detail = ",".join(kinds) if kinds else "no findings"
        print(f"{result['verdict']}: {len(result['findings'])} finding(s) [{detail}]")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
