#!/usr/bin/env python3
"""Jev (System One) decision helper — opencode Zen ``jev-1.13-free``.

Posts a state + typed questions to ``https://opencode.ai/zen/v1/systemone``
and prints the returned ``answers`` object as JSON. Jev returns values and
probabilities (no text generation), which callers use as cheap decision gates.

Usage:
  echo "STATE TEXT" | python3 jev_ask.py '{"qid":{"type":"noul","instructions":"..."}}'
  python3 jev_ask.py --questions-file q.json --state-file state.txt
  python3 jev_ask.py --field action_needed '<questions json>' <<< "$state"

Question types (TypeSafe AI): ``noul`` (yes/no), ``choice`` (criteria map),
``score`` (rubric). Multiple questions evaluate in parallel in one request.

Quota fallback: if the free tier answers HTTP 429 (account-wide
``FreeUsageLimitError``) or HTTP 402, the same questions are re-asked as a
plain chat-completions call to the Go tier
(``https://opencode.ai/zen/go/v1/chat/completions``) with model
JEV_FALLBACK_MODEL (default ``glm-5.3-flash``) and a stable
``x-opencode-session`` header; the JSON answer is normalised back to the same
``{"answers": {qid: {"noul": f}}}`` shape. Auth errors (401/403), timeouts and
malformed replies are not retried on the Go tier.

Auth: ``ZEN_API_KEY`` / ``OPENCODE_ZEN_API_KEY`` env, else the 0600 key file
``~/.openclaw/credentials/opencode/zen.key`` (same Zen key as the
``opencode-go`` provider). Egress follows the standard proxy env vars, so the
gateway-environ inheritance pattern in run-*.sh works unchanged.

Exit codes: 0 ok (answers JSON on stdout) · 1 request failed · 2 bad input.
Diagnostics go to stderr only. Env overrides: JEV_ENDPOINT, JEV_MODEL,
JEV_KEY_FILE, JEV_TIMEOUT (seconds, default 15), JEV_FALLBACK_ENDPOINT,
JEV_FALLBACK_MODEL.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

ENDPOINT = os.environ.get("JEV_ENDPOINT", "https://opencode.ai/zen/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-1.13-free")
FALLBACK_ENDPOINT = os.environ.get(
    "JEV_FALLBACK_ENDPOINT", "https://opencode.ai/zen/go/v1/chat/completions")
FALLBACK_MODEL = os.environ.get("JEV_FALLBACK_MODEL", "glm-5.3-flash")
KEY_FILE = os.environ.get(
    "JEV_KEY_FILE",
    os.path.expanduser("~/.openclaw/credentials/opencode/zen.key"))
TIMEOUT = int(os.environ.get("JEV_TIMEOUT", "15"))
# The Go tier 400s with MissingSessionID without this header; one stable id
# per process.
SESSION_ID = f"openclaw-jev-{os.getpid()}-{uuid.uuid4().hex}"
# glm-5.3-flash is a reasoning model: at max_tokens=200 the whole budget goes
# to reasoning_content and message.content comes back empty. Measured
# 2026-10-27: ~450 reasoning tokens for the widen prompt, clean JSON at 800.
FALLBACK_MAX_TOKENS = int(os.environ.get("JEV_FALLBACK_MAX_TOKENS", "1000"))
FALLBACK_CODES = (429, 402)

FALLBACK_SYSTEM = (
    "You score typed decision questions about a given state. Reply with JSON "
    "only: no prose, no explanation, no code fences. Use exactly this shape: "
    '{"answers": {"<qid>": {"noul": <float between 0 and 1>}}}. '
    'For a question whose type is "choice", answer {"choice": "<value>"} '
    'instead of noul. noul is the probability that the answer is yes, or the '
    "rubric score, between 0 and 1."
)


def load_key():
    key = os.environ.get("ZEN_API_KEY") or os.environ.get("OPENCODE_ZEN_API_KEY")
    if key:
        return key.strip()
    with open(KEY_FILE, encoding="utf-8") as fh:
        return fh.read().strip()


def _questions_block(questions):
    lines = []
    for qid, q in questions.items():
        q = q or {}
        lines.append(
            f"- qid {json.dumps(str(qid))}: type={json.dumps(q.get('type', 'noul'))} "
            f"instructions={json.dumps(q.get('instructions', ''))}")
    return "\n".join(lines)


def _fallback_prompt(state, questions):
    return (
        "STATE:\n" + str(state) + "\n\nQUESTIONS:\n" + _questions_block(questions)
        + "\n\nAnswer every qid above. JSON only."
    )


def _extract_json_object(text):
    """First balanced {...} in text, tolerating fences and surrounding prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    raise ValueError("no JSON object in reply")


def _is_number(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _normalise(parsed, questions):
    answers = parsed.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("fallback reply has no answers object")
    out = {}
    for qid, q in questions.items():
        raw = answers.get(str(qid))
        if isinstance(raw, str) and _is_number(raw):
            raw = float(raw)  # small models sometimes emit a bare number
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if str((q or {}).get("type", "noul")) == "choice":
                raise ValueError(f"fallback reply missing choice for {qid!r}")
            out[str(qid)] = {"noul": float(raw)}
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"fallback reply missing qid {qid!r}")
        if str((q or {}).get("type", "noul")) == "choice":
            if "choice" not in raw:
                raise ValueError(f"fallback reply missing choice for {qid!r}")
            out[str(qid)] = {"choice": raw["choice"]}
        else:
            val = raw.get("noul", raw.get("score"))
            if val is None:
                raise ValueError(f"fallback reply missing noul for {qid!r}")
            out[str(qid)] = {"noul": float(val)}
    return {"answers": out}


def _ask_fallback(state, questions, key):
    body = json.dumps({
        "model": FALLBACK_MODEL,
        "max_tokens": FALLBACK_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": FALLBACK_SYSTEM},
            {"role": "user", "content": _fallback_prompt(state, questions)},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        FALLBACK_ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": os.environ.get(
                     "JEV_USER_AGENT", "job-search-lane/1.0"),
                 "x-opencode-session": SESSION_ID})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    try:
        text = payload["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        print(f"jev: fallback reply malformed: {exc}", file=sys.stderr)
        raise RuntimeError(f"jev request failed: fallback reply malformed: {exc}") from None
    if not text.strip():
        finish = payload["choices"][0].get("finish_reason")
        print(f"jev: fallback reply empty (finish_reason={finish})", file=sys.stderr)
        raise RuntimeError(
            f"jev request failed: fallback reply empty (finish_reason={finish}); "
            "raise JEV_FALLBACK_MAX_TOKENS")
    try:
        return _normalise(_extract_json_object(text), questions)
    except ValueError as exc:
        print(f"jev: fallback reply unparseable: {exc}", file=sys.stderr)
        raise RuntimeError(f"jev request failed: fallback reply unparseable: {exc}") from None


def ask(state, questions, retries=1):
    key = load_key()
    body = json.dumps({"model": MODEL, "state": state,
                       "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": os.environ.get(
                     "JEV_USER_AGENT", "job-search-lane/1.0")})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:  # noqa: BLE001 - network errors retried once
            last = exc
    code = getattr(last, "code", None)
    if code in FALLBACK_CODES:
        print(f"jev: primary {code}, falling back to go/{FALLBACK_MODEL}",
              file=sys.stderr)
        return _ask_fallback(state, questions, key)
    raise RuntimeError(f"jev request failed: {last}")


def main(argv):
    args = argv[1:]
    qfile = sfile = field = None
    while args:
        if args[0] == "--questions-file" and len(args) >= 2:
            qfile, args = args[1], args[2:]
        elif args[0] == "--state-file" and len(args) >= 2:
            sfile, args = args[1], args[2:]
        elif args[0] == "--field" and len(args) >= 2:
            field, args = args[1], args[2:]
        else:
            break
    if qfile:
        with open(qfile, encoding="utf-8") as fh:
            questions = json.load(fh)
    elif args:
        questions = json.loads(args[0])
    else:
        print("jev: no questions given (JSON object expected)", file=sys.stderr)
        return 2
    if sfile:
        with open(sfile, encoding="utf-8") as fh:
            state = fh.read()
    else:
        state = sys.stdin.read()
    if not state.strip():
        print("jev: empty state", file=sys.stderr)
        return 2
    try:
        data = ask(state, questions)
    except Exception as exc:  # noqa: BLE001
        print(f"jev: {exc}", file=sys.stderr)
        return 1
    answers = data.get("answers", {})
    if field:
        ans = answers.get(field, {})
        val = ans.get("noul", ans.get("score", ans.get("choice")))
        print(json.dumps(val) if isinstance(val, (dict, list)) else val)
    else:
        print(json.dumps(answers, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
