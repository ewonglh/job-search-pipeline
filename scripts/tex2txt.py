#!/usr/bin/env python3
"""tex2txt.py — mechanical resume.txt export for a tailored resume variant.

WHY
    InternSG's application form accepts text in a TextArea, with no attachments.
    Each tailored variant therefore needs a plain-text form that is provably
    identical in content to its built PDF.  Hand-writing that text would let it
    drift from the resume source, so it is generated mechanically from the
    variant's own `resume.tex` (the same source the PDF is built from).

SCOPE
    Run this only for **InternSG-sourced** variants: their form is a text area
    with no attachments.  LinkedIn and company-site applications stay PDF-only
    and must not carry a `resume.txt`.  This tool is a pure converter (it does
    not know a variant's source); the gate lives in the tailoring procedure
    (README step 5), keyed on the work item's `source` field.

WHAT
    Reads a variant `variants/<slug>/resume.tex` (the repo's vendored "Jake's
    Resume" body) and writes `variants/<slug>/resume.txt` next to it:

      * the name and contact line,
      * UPPERCASE section headings,
      * `- ` bullets (no bullet glyph, no LaTeX, no markdown),
      * one blank line between sections (and between entries),
      * long lines wrapped at ~100 columns.

    Because the text is derived from the same LaTeX body as the PDF, the two
    cannot diverge: the converter and the PDF share one source of truth.

USAGE
    # generate variants/<slug>/resume.txt in place
    python3 tex2txt.py variants/<slug>/resume.tex

    # write to stdout instead (e.g. to preview what would be pasted)
    python3 tex2txt.py --stdout variants/<slug>/resume.tex

    # regenerate and assert the export is faithful (used by the tailoring lane)
    python3 tex2txt.py --check variants/<slug>/resume.tex

    # override the wrap width (default 100)
    python3 tex2txt.py --width 100 variants/<slug>/resume.tex

EXIT CODES
    0  ok
    1  usage/IO error, or --check failed (LaTeX residue, or a `\\resumeItem`
       that did not survive as a `- ` bullet)
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# LaTeX fragment -> plain text
# ---------------------------------------------------------------------------

_DASH = {
    r"\textendash": "\u2013",  # en dash
    r"\textemdash": "\u2014",  # em dash
}

_ESCAPES = {
    r"\%": "%",
    r"\&": "&",
    r"\#": "#",
    r"\_": "_",
    r"\$": "$",
    r"\{": "{",
    r"\}": "}",
    "~": " ",
}

# Formatting commands whose single braced argument we unwrap.
_WRAPPERS = (
    "textbf", "textit", "textsc", "textrm", "emph", "texttt",
    "small", "large", "Large", "LARGE", "huge", "Huge", "Huge",
    "scshape", "itshape", "bfseries", "mdseries", "mathrm",
)

_LETTER_CMD = re.compile(r"\\[a-zA-Z]+\*?")


def read_group(s: str, i: int) -> tuple[str, int]:
    """Read a balanced ``{...}`` group starting at s[i] == '{'. Return inner, next i."""
    if i >= len(s) or s[i] != "{":
        raise ValueError(f"expected '{{' at {i}: {s[i:i+20]!r}")
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":  # keep escaped braces out of depth accounting
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise ValueError("unbalanced braces")


def _unwrap_href(s: str) -> str:
    """\\href{url}{text} -> text (balanced-brace safe)."""
    out = []
    i = 0
    token = r"\href"
    while True:
        k = s.find(token, i)
        if k == -1:
            out.append(s[i:])
            break
        out.append(s[i:k])
        j = k + len(token)
        while j < len(s) and s[j] in " \t\n":
            j += 1
        try:
            _url, j = read_group(s, j)
            j2 = j
            while j2 < len(s) and s[j2] in " \t\n":
                j2 += 1
            text, j2 = read_group(s, j2)
        except ValueError:
            out.append(s[k:])
            break
        out.append(text)
        i = j2
    return "".join(out)


def latex_to_text(s: str) -> str:
    """Flatten a LaTeX fragment to ATS-plain text (no LaTeX, no markdown)."""
    s = _unwrap_href(s)

    # Unwrap formatting commands; repeat for non-nested cases.
    for cmd in _WRAPPERS:
        s = re.sub(r"\\" + cmd + r"\b", "", s)

    # Drop spacing/geometry commands entirely.
    s = re.sub(r"\\vspace\*?\{[^{}]*\}", " ", s)
    s = re.sub(r"\\hspace\*?\{[^{}]*\}", " ", s)
    s = re.sub(r"\\hfill\b", " ", s)
    s = re.sub(r"\\label\b", " ", s)

    # Dashes and math-mode symbols.
    for k, v in _DASH.items():
        s = s.replace(k, v)
    s = s.replace("---", "\u2014").replace("--", "\u2013")
    s = s.replace("$|$", "|").replace("$<$", "<").replace("$>$", ">")

    # Escaped characters.
    for k, v in _ESCAPES.items():
        s = s.replace(k, v)

    # Any remaining backslash command with no useful argument.
    s = _LETTER_CMD.sub(" ", s)

    # Leftover grouping braces.
    s = s.replace("{", "").replace("}", "")

    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Structural parse of a Jake's-Resume variant body
# ---------------------------------------------------------------------------

_TOKENS = re.compile(r"\\section\{|\\resumeSubheading\b|\\resumeItem\{|\\begin\{itemize\}")


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _strip_optional(s: str) -> str:
    """Drop a leading ``[...]`` options group (balanced)."""
    s = s.lstrip()
    if not s.startswith("["):
        return s
    depth = 0
    for k, c in enumerate(s):
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[k + 1:]
    return s


def parse_skills(block: str) -> list[str]:
    block = _strip_optional(block)
    rows = []
    for frag in block.split("\\\\"):
        t = latex_to_text(frag)
        if t:
            rows.append(t)
    return rows


def parse_body(body: str) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    i = 0
    while True:
        m = _TOKENS.search(body, i)
        if not m:
            break
        start = m.start()
        i = m.end()

        if body.startswith("\\section{", start):
            arg, i = read_group(body, m.end() - 1)
            items.append(("section", latex_to_text(arg)))

        elif body.startswith("\\resumeSubheading", start):
            j = m.end()
            args = []
            for _ in range(4):
                j = _skip_ws(body, j)
                g, j = read_group(body, j)
                args.append(latex_to_text(g))
            items.append(("entry", tuple(args)))
            i = j

        elif body.startswith("\\resumeItem{", start):
            g, i = read_group(body, m.end() - 1)
            items.append(("bullet", latex_to_text(g)))

        elif body.startswith("\\begin{itemize}", start):
            end = body.find("\\end{itemize}", m.end())
            if end == -1:
                break
            block = body[m.end():end]
            i = end + len("\\end{itemize}")
            items.append(("skills", parse_skills(block)))

    return items


def extract_heading(body: str) -> tuple[str, str]:
    m = re.search(r"\\begin\{center\}(.*?)\\end\{center\}", body, re.S)
    if not m:
        return ("", "")
    # Collapse the source's own line breaks to spaces first, so that only a
    # real LaTeX line break (``\\``) starts a new heading line.  Otherwise the
    # contact line, which the source wraps across several physical lines, would
    # be split into separate fields and joined with stray ``|`` separators.
    block = re.sub(r"\s+", " ", m.group(1))
    block = block.replace("\\\\", "\n")
    lines = [latex_to_text(ln) for ln in block.split("\n")]
    lines = [ln for ln in lines if ln]
    name = lines[0] if lines else ""
    contact = " | ".join(lines[1:]) if len(lines) > 1 else ""
    contact = re.sub(r"\s*\|\s*", " | ", contact).strip(" |")
    return (name, contact)


def render(body: str, width: int = 100) -> str:
    name, contact = extract_heading(body)

    # Body content starts after the heading block.
    m = re.search(r"\\end\{center\}", body)
    tail = body[m.end():] if m else body
    items = parse_body(tail)

    logical: list[str] = []
    if name:
        logical.append(name)
    if contact:
        logical.append(contact)

    seen_section = False
    first_entry_in_section = False
    for kind, payload in items:
        if kind == "section":
            logical.append("")
            logical.append(str(payload).upper())
            seen_section = True
            first_entry_in_section = True
        elif kind == "entry":
            a, b, c, d = payload  # type: ignore[misc]
            if not first_entry_in_section:
                logical.append("")
            first_entry_in_section = False
            head = f"{a} | {b}" if (a and b) else (a or b)
            if head:
                logical.append(head)
            if c:
                logical.append(c)
            if d:
                logical.append(d)
        elif kind == "bullet":
            logical.append("- " + str(payload))
        elif kind == "skills":
            for row in payload:  # type: ignore[union-attr]
                logical.append(row)

    # Collapse runs of blank lines to exactly one; trim edges.
    norm: list[str] = []
    for ln in logical:
        if ln == "" and (not norm or norm[-1] == ""):
            continue
        norm.append(ln)
    while norm and norm[-1] == "":
        norm.pop()

    wrapped: list[str] = []
    for ln in norm:
        if ln == "":
            wrapped.append("")
            continue
        wrapped.extend(
            textwrap.wrap(
                ln, width=width, subsequent_indent="  ",
                break_long_words=False, break_on_hyphens=False,
            ) or [""]
        )

    return "\n".join(wrapped) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _extract_items(body: str) -> list[str]:
    m = re.search(r"\\end\{center\}", body)
    tail = body[m.end():] if m else body
    return [str(p) for k, p in parse_body(tail) if k == "bullet"]


def _txt_bullets(txt: str) -> list[str]:
    """Reconstruct logical bullets from wrapped text.

    Wrapped continuation lines are indented with two spaces, but a continuation
    belongs to the immediately preceding logical line only.  Non-bullet logical
    lines (entry tech-stack lines, section content) wrap with the same indent,
    so a continuation must not be folded into an earlier bullet merely because
    one exists; it must directly follow that bullet's own first/continuation
    line.  Any intervening blank or non-continuation line closes the bullet.
    """
    out: list[str] = []
    continuation = False
    for ln in txt.splitlines():
        if ln.startswith("- "):
            out.append(ln[2:])
            continuation = True
        elif continuation and ln.startswith("  ") and ln.strip():
            out[-1] += " " + ln.strip()
        else:
            continuation = False
    return out


def _check(tex: str, txt: str) -> list[str]:
    problems: list[str] = []
    body = tex.split("\\begin{document}", 1)[-1]
    bullets = _extract_items(body)
    txt_bullets = _txt_bullets(txt)
    if len(bullets) != len(txt_bullets):
        problems.append(
            f"bullet count mismatch: tex={len(bullets)} txt={len(txt_bullets)}"
        )
    for tex_b, txt_b in zip(bullets, txt_bullets):
        if tex_b != txt_b:
            problems.append(f"bullet text mismatch:\n  tex: {tex_b}\n  txt: {txt_b}")
    residue = re.findall(r"[\\{}]|\*\*|`", txt)
    if residue:
        problems.append(f"LaTeX/markdown residue in output: {sorted(set(residue))}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("tex", type=Path, help="path to variants/<slug>/resume.tex")
    p.add_argument("--stdout", action="store_true", help="write to stdout, not resume.txt")
    p.add_argument("--width", type=int, default=100, help="wrap width (default 100)")
    p.add_argument("--check", action="store_true",
                   help="after generating, assert fidelity to the .tex")
    args = p.parse_args(argv)

    try:
        tex = args.tex.read_text(encoding="utf-8")
    except OSError as e:
        print(f"tex2txt: cannot read {args.tex}: {e}", file=sys.stderr)
        return 1

    txt = render(tex, width=args.width)

    if args.check:
        problems = _check(tex, txt)
        if problems:
            for pr in problems:
                print(f"tex2txt: CHECK FAILED: {pr}", file=sys.stderr)
            return 1

    if args.stdout:
        sys.stdout.write(txt)
        return 0

    out = args.tex.with_name("resume.txt")
    out.write_text(txt, encoding="utf-8")
    print(f"tex2txt: wrote {out} ({len(txt)} chars, {len(txt.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
