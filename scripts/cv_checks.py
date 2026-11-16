#!/usr/bin/env python3
"""Deterministic structural checks for tailored LaTeX resumes.

The checks compare a supported master entry format with a Jake-resume variant.
They flag missing source entries, reduced bullet coverage, numeric claims,
team-credit loss, reverse chronology, and unsupported templates. Findings are
signals for semantic review; they do not establish that resume content is true.

This module is stdlib-only and avoids including resume text in its findings.
"""

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List

import tex2txt


@dataclass
class Entry:
    section: str
    title: str
    date: str
    details: str = ""
    bullets: List[str] = field(default_factory=list)


def _without_comments(source):
    """Remove TeX comments without treating escaped percent signs as comments."""
    lines = []
    for line in source.splitlines():
        out = []
        i = 0
        while i < len(line):
            if line[i] == "%":
                slashes = 0
                j = i - 1
                while j >= 0 and line[j] == "\\":
                    slashes += 1
                    j -= 1
                if slashes % 2 == 0:
                    break
            out.append(line[i])
            i += 1
        lines.append("".join(out))
    return "\n".join(lines)


def _arg(source, command_end):
    i = command_end
    while i < len(source) and source[i].isspace():
        i += 1
    return tex2txt.read_group(source, i)


def _commands(source, pattern):
    return re.finditer(pattern, source, re.M)


def _parse_master(source):
    source = _without_comments(source)
    headings = list(_commands(source, r"\\resheading\s*\{"))
    if not headings:
        return []
    sections = []
    for index, match in enumerate(headings):
        title, end = _arg(source, match.end() - 1)
        stop = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        sections.append((tex2txt.latex_to_text(title), end, stop))

    entries = []
    for section, start, stop in sections:
        block = source[start:stop]
        found = list(_commands(block, r"\\entry\s*\{"))
        for index, match in enumerate(found):
            title, position = _arg(block, match.end() - 1)
            date, position = _arg(block, position)
            end = found[index + 1].start() if index + 1 < len(found) else len(block)
            entry_block = block[position:end]
            bullets = []
            for itemize in re.finditer(
                    r"\\begin\s*\{itemize\}(.*?)\\end\s*\{itemize\}",
                    entry_block, re.S):
                body = itemize.group(1)
                items = list(re.finditer(r"\\item\b", body))
                for n, item in enumerate(items):
                    item_end = items[n + 1].start() if n + 1 < len(items) else len(body)
                    text = tex2txt.latex_to_text(body[item.end():item_end])
                    if text:
                        bullets.append(text)
            entries.append(Entry(section, tex2txt.latex_to_text(title),
                                 tex2txt.latex_to_text(date), entry_block, bullets))
    return entries


def _parse_variant(source):
    source = _without_comments(source)
    headings = list(_commands(source, r"\\section\s*\{"))
    if not headings:
        return []
    sections = []
    for index, match in enumerate(headings):
        title, end = _arg(source, match.end() - 1)
        stop = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        sections.append((tex2txt.latex_to_text(title), end, stop))

    entries = []
    for section, start, stop in sections:
        block = source[start:stop]
        found = list(_commands(block, r"\\resumeSubheading\b"))
        for index, match in enumerate(found):
            position = match.end()
            args = []
            for _ in range(4):
                value, position = _arg(block, position)
                args.append(tex2txt.latex_to_text(value))
            end = found[index + 1].start() if index + 1 < len(found) else len(block)
            entry_block = block[position:end]
            bullets = []
            for item in _commands(entry_block, r"\\resumeItem\s*\{"):
                value, _ = _arg(entry_block, item.end() - 1)
                text = tex2txt.latex_to_text(value)
                if text:
                    bullets.append(text)
            entries.append(Entry(section, args[0], args[1], " ".join(args[2:]), bullets))
    return entries


def _norm_section(value):
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    if value == "experience":
        return "work experience"
    return value


def _title_key(value):
    value = tex2txt.latex_to_text(value).split("|", 1)[0]
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _match(master, candidate):
    section = _norm_section(candidate.section)
    key = _title_key(candidate.title)
    possible = [entry for entry in master
                if _norm_section(entry.section) == section and _title_key(entry.title) == key]
    if len(possible) == 1:
        return possible[0]
    possible = [entry for entry in master
                if _norm_section(entry.section) == section and
                (key.startswith(_title_key(entry.title)) or
                 _title_key(entry.title).startswith(key))]
    return possible[0] if len(possible) == 1 else None


_NUMBERS = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?\+?(?![A-Za-z])")


def _numbers(text):
    numbers = {match.group(0).replace(",", "").rstrip("+")
               for match in _NUMBERS.finditer(text)}
    numbers.update(_WORD_NUM[match.group(0).lower()] for match in
                   re.finditer(r"\b(?:" + "|".join(_WORD_NUM) + r")\b", text, re.I))
    return numbers


_WORD_NUM = {"one": "1", "two": "2", "three": "3", "four": "4",
             "five": "5", "six": "6", "seven": "7", "eight": "8",
             "nine": "9", "ten": "10"}


def _team_size(text):
    low = text.casefold()
    patterns = (
        r"\bteam\s+of\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        r"\bone\s+of\s+(?:the\s+)?(\d+|two|three|four|five|six|seven|eight|nine|ten)[- ](?:person|member|engineer)s?\b",
        r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)[- ]person\s+team\b",
    )
    for pattern in patterns:
        match = re.search(pattern, low)
        if match:
            return _WORD_NUM.get(match.group(1), match.group(1))
    return None


def _date_rank(value):
    clean = tex2txt.latex_to_text(value).casefold().strip()
    if "present" in clean or "current" in clean:
        return 9999 * 12 + 12
    months = {name: index for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    found = [(months[match.group(1)[:3]], int(match.group(2)))
             for match in re.finditer(
                 r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|"
                 r"july|jul|august|aug|september|sep|october|oct|november|nov|"
                 r"december|dec)\s+(20\d{2})\b", clean)]
    if not found:
        return None
    month, year = found[-1]
    return year * 12 + month


def _finding(code, message, entry_index=None):
    finding = {"code": code, "message": message}
    if entry_index is not None:
        finding["entry_index"] = entry_index
    return finding


def check(master_text, resume_text):
    """Return stable, content-free findings for supported structural checks."""
    try:
        master = _parse_master(master_text)
        candidate = _parse_variant(resume_text)
    except (ValueError, IndexError):
        return [_finding("template_unsupported",
                         "Malformed or unsupported LaTeX structure; manual review required.")]
    if not master or not candidate:
        return [_finding("template_unsupported",
                         "Could not parse supported master and variant templates; manual review required.")]

    findings = []
    matched = []
    for index, item in enumerate(candidate, 1):
        source = _match(master, item)
        if source is None:
            findings.append(_finding("unmatched_entry",
                                     "No unique source entry was found; manual review required.", index))
            continue
        matched.append((index, source, item))
        floor = min(2, len(source.bullets))
        if len(item.bullets) < floor:
            findings.append(_finding("bullet_floor",
                                     "Selected entry has fewer bullets than its source supports.", index))
        team = _team_size(source.title + " " + source.details)
        if team and _team_size(item.title + " " + item.details + " " +
                               " ".join(item.bullets)) != team:
            findings.append(_finding("team_attribution",
                                     "Team attribution from the source may have been lost.", index))
        supported = _numbers(source.title + " " + source.date + " " + source.details + " " +
                             " ".join(source.bullets))
        candidate_numbers = _numbers(item.title + " " + item.date + " " + item.details + " " +
                                     " ".join(item.bullets))
        if candidate_numbers - supported:
            findings.append(_finding("unsupported_number",
                                     "A numeric claim is absent from this source entry; review it.", index))

    by_section = {}
    for index, _source, item in matched:
        by_section.setdefault(_norm_section(item.section), []).append((index, item))
    for rows in by_section.values():
        ranks = [_date_rank(item.date) for _index, item in rows]
        for position in range(1, len(rows)):
            previous_rank, current_rank = ranks[position - 1], ranks[position]
            if (previous_rank is not None and current_rank is not None and
                    current_rank > previous_rank):
                findings.append(_finding("chronology",
                                         "Entries may not be ordered newest first.", rows[position][0]))
    return findings


def check_pdf(path, which_fn=None, runner=None):
    """Return layout findings for a PDF, with injectable tool lookups for tests."""
    which_fn = which_fn or shutil.which
    runner = runner or subprocess.run
    pdfinfo = which_fn("pdfinfo")
    if not pdfinfo:
        return [_finding("layout_checker_unavailable",
                         "pdfinfo is unavailable; PDF page count was not checked.")]
    try:
        result = runner([pdfinfo, str(path)], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return [_finding("layout_tool_failed", "pdfinfo could not be started.")]
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout or "", re.M)
    if result.returncode or not match:
        return [_finding("layout_artifact_unreadable",
                         "pdfinfo could not read the supplied PDF.")]
    if int(match.group(1)) != 1:
        return [_finding("layout_page_count", "PDF page count differs from the expected one page.")]
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--master", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--pdf")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with open(args.master, "r", encoding="utf-8") as source_file:
            master_text = source_file.read()
        with open(args.resume, "r", encoding="utf-8") as resume_file:
            resume_text = resume_file.read()
    except OSError as exc:
        print("cv_checks: cannot read input: %s" % exc, file=sys.stderr)
        return 2
    findings = check(master_text, resume_text)
    if args.pdf:
        findings.extend(check_pdf(args.pdf))
    if args.json:
        import json
        print(json.dumps({"ok": not findings, "findings": findings}, indent=2))
    else:
        for finding in findings:
            print("%s: %s" % (finding["code"], finding["message"]))
        if not findings:
            print("CV structural checks passed; semantic review is still required.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
