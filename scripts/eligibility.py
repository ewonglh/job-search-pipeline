#!/usr/bin/env python3
"""Conservative, offline eligibility evaluation from a profile and guarded JD.

The public interface is :func:`evaluate`. It accepts a posting, the selected
profile mapping, and JD text, and returns a stable status, reason codes,
evidence snippets, and hashes of the exact profile source and effective JD.
Unsupported or missing required facts stay unknown for review.
"""
import calendar
import hashlib
import json
import math
import re
from datetime import date, timedelta

from posting_guard import guard_text


MONTHS = {name.lower(): index for index, name in enumerate(
    "January February March April May June July August September October "
    "November December".split(), 1)}
MONTHS.update({name[:3]: value for name, value in list(MONTHS.items())})
MONTHS["sept"] = 9
# Month words are matched strictly: "Market" or "Junior" are not months, and
# the modal verb "may" only counts when capitalised.
_MONTH = (r"(?:(?-i:May)|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|Jun(?:e)?|"
          r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
          r"Dec(?:ember)?)")
_SEP = r"(?:\s+(?:to|until|through|thru)\s+|\s*[-–—]\s*)"
_ORD = r"(?:st|nd|rd|th)?"
_BOUNDARY = re.compile(
    r"(?<=[.!?;])\s+|\n+|,?\s+\b(?:although|but|whereas|however)\b\s+", re.I)
_LEAD_CUT = re.compile(
    r"[;()]|\b(?:and|but|while|whereas|then|although)\b", re.I)
# Words that make a date describe something other than the posting period.
# A compound such as "application period" must beat the bare word "period".
_UNRELATED = re.compile(
    r"\b(?:applications?\b(?:\s+\w+){0,3}?\s+"
    r"(?:open\w*|start\w*|begin\w*|clos\w+|due|by|before|until|from|between|"
    r"period|window|dates?)|"
    r"apply(?:ing)?\s+(?:by|before|until|from|between|no later than|on or before)|"
    r"(?:recruitment|selection|interview|registration)s?\s+"
    r"(?:period|window|dates?|phase|rounds?|stage)|"
    r"deadlines?|closing|closes?|closed|interviews?|shortlist\w*|recruit\w*|"
    r"offer\s+(?:date|letters?|release|notification)|notif\w*|"
    r"info(?:rmation)?\s+sessions?|briefings?|registration|submit\w*|due)\b", re.I)
_TRAIL_UNRELATED = re.compile(
    r"^\W*(?:(?:is|are|for|as|being)\s+)*(?:the\s+)?(?:applications?|deadlines?|"
    r"closing|recruit\w*|interviews?|selection|registration|shortlist\w*)\b", re.I)
_ATTACH = re.compile(
    r"\b(?:intern(?:ship)?s?|placements?|attachments?|co-?op|programmes?|programs?|"
    r"work term|period|dates?|duration|runs?|running|starts?|starting|commenc\w+)\b",
    re.I)
_ALTERNATIVE = re.compile(
    r"\bor\b|\balternatively\b|\beither\b|\band/or\b|/|"
    r"\b(?:option|intake|cohort|batch)\s*[A-Za-z0-9]+\b", re.I)
_START_VERB = r"(?:start(?:s|ing)?|commenc(?:e|es|ing)|begin(?:s|ning)?|available\s+from)"
_START_DAY = r"(?:(?:the\s+)?(?P<day>\d{1,2})" + _ORD + r"\s+(?:of\s+)?)?"
_START_BEFORE = re.compile(
    r"\b" + _START_VERB + r"\b(?:\s+date)?\s*(?:is|in|on|from|at|of|:|-|–)?\s*" +
    _START_DAY + r"(?P<month>" + _MONTH + r")\b\.?(?:\s+(?P<day2>\d{1,2})" + _ORD +
    r"(?!\d))?(?:,?\s+(?P<year>20\d{2}))?", re.I)
_START_AFTER = re.compile(
    r"\b" + r"(?P<month>" + _MONTH + r")\b\.?(?:\s+(?P<day>\d{1,2})" + _ORD +
    r"(?!\d))?,?(?:\s+(?P<year>20\d{2}))?\s+(?:start|intake)\b", re.I)
_START_SOFT = re.compile(
    r"\b(?:earliest|latest|flexible|flexibility|negotiable|as early|can start|"
    r"could start|may start|open to)\b", re.I)
YEAR_WORDS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3,
    "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
    "sixth": 6, "6th": 6,
}
COUNTRIES = {
    "singapore": "singapore", "singaporean": "singapore",
    "malaysia": "malaysia", "malaysian": "malaysia",
    "india": "india", "indian": "india", "united states": "united states",
    "us": "united states", "u.s.": "united states", "usa": "united states",
    "united kingdom": "united kingdom", "uk": "united kingdom",
    "australia": "australia", "australian": "australia",
    "canada": "canada", "canadian": "canada",
}


def _text(value):
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    if value is None:
        value = ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _profile_value(profile, *keys):
    for key in keys:
        if key in profile and profile[key] is not None and _text(profile[key]):
            return profile[key]
    return None


def _digest(value):
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _month(value):
    return MONTHS.get(value.lower().rstrip("."))


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _range_patterns():
    # "between X and Y" is the only place a bare "and" separates two dates.
    sep = (r"(?:\s+(?:to|until|through|thru)\s+|\s*[-–—]\s*|"
           r"(?(bt)\s+and\s+|(?!)))")

    def day(name):
        return r"(?P<%s>\d{1,2})%s" % (name, _ORD)

    def mon(name):
        return r"(?P<%s>%s)\b\.?" % (name, _MONTH)

    def build(body):
        return re.compile(r"\b(?P<bt>between\s+)?" + body, re.I)

    y1, y2 = r"(?P<y1>20\d{2})", r"(?P<y2>20\d{2})"
    return [
        ("iso", build(y1 + r"-(?P<n1>\d{1,2})-(?P<d1>\d{1,2})" + sep + y2 +
                      r"-(?P<n2>\d{1,2})-(?P<d2>\d{1,2})\b")),
        ("dmy", build(day("d1") + r"\s+" + mon("m1") + r",?\s+" + y1 + sep +
                      day("d2") + r"\s+" + mon("m2") + r",?\s+" + y2 + r"\b")),
        ("mdy", build(mon("m1") + r"\s+" + day("d1") + r",?\s+" + y1 + sep +
                      mon("m2") + r"\s+" + day("d2") + r",?\s+" + y2 + r"\b")),
        ("dm_shared", build(day("d1") + r"\s+" + mon("m1") + sep + day("d2") +
                            r"\s+" + mon("m2") + r",?\s+" + y2 + r"\b")),
        ("md_shared", build(mon("m1") + r"\s+" + day("d1") + sep + mon("m2") +
                            r"\s+" + day("d2") + r",?\s+" + y2 + r"\b")),
        ("month_year", build(mon("m1") + r",?\s+" + y1 + sep + mon("m2") +
                             r",?\s+" + y2 + r"\b")),
        ("months", build(mon("m1") + sep + mon("m2") + r",?\s+" + y2 + r"\b")),
        ("yearless", build(mon("m1") + sep + mon("m2") + r"(?!\s*,?\s*\d)")),
    ]


_RANGE_PATTERNS = _range_patterns()


def _parse_ranges(text):
    """Parse every supported period form into items with source offsets.

    ``candidates`` holds the exact (start, end) readings. A month range with
    one year that runs backwards (``Nov to Feb 2027``) has two readings and is
    ``ambiguous``. An explicit range that ends before it starts is ``invalid``.
    A month range with no year is ``yearless``. None of these is guessed.
    """
    found = []
    occupied = []
    for kind, pattern in _RANGE_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < hi and match.end() > lo for lo, hi in occupied):
                continue
            g = match.groupdict()
            item = {"kind": kind, "start_at": match.start(), "end_at": match.end(),
                    "phrase": match.group(0), "ambiguous": False, "invalid": False,
                    "yearless": kind == "yearless", "candidates": []}
            try:
                if kind == "yearless":
                    pass
                elif kind == "iso":
                    item["candidates"] = [(
                        date(int(g["y1"]), int(g["n1"]), int(g["d1"])),
                        date(int(g["y2"]), int(g["n2"]), int(g["d2"])))]
                elif kind in ("dmy", "mdy", "month_year"):
                    a, b = int(g["y1"]), int(g["y2"])
                    m1, m2 = _month(g["m1"]), _month(g["m2"])
                    d1 = int(g["d1"]) if g.get("d1") else 1
                    d2 = int(g["d2"]) if g.get("d2") else _month_end(b, m2).day
                    item["candidates"] = [(date(a, m1, d1), date(b, m2, d2))]
                else:
                    year = int(g["y2"])
                    m1, m2 = _month(g["m1"]), _month(g["m2"])
                    d1 = int(g["d1"]) if g.get("d1") else 1
                    d2 = int(g["d2"]) if g.get("d2") else None
                    forward = (m1, d1) <= (m2, d2 or 31)
                    spans = [(year, year)] if forward else [(year - 1, year),
                                                           (year, year + 1)]
                    for y_start, y_end in spans:
                        last = d2 or _month_end(y_end, m2).day
                        item["candidates"].append(
                            (date(y_start, m1, d1), date(y_end, m2, last)))
                    item["ambiguous"] = not forward
            except (TypeError, ValueError):
                continue
            if item["candidates"] and not item["ambiguous"]:
                if item["candidates"][0][1] < item["candidates"][0][0]:
                    item["candidates"] = []
                    item["invalid"] = True
            if len(item["candidates"]) == 1:
                item["start"], item["end"] = item["candidates"][0]
            found.append(item)
            occupied.append((match.start(), match.end()))
    return sorted(found, key=lambda item: item["start_at"])


def _date_ranges(text):
    """Return only exactly resolved intervals (used for the profile and cohorts)."""
    return [item for item in _parse_ranges(text) if "start" in item]


def _profile_windows(profile):
    source = " ".join(_text(profile.get(key)) for key in
                      ("availability", "internship_dates", "availability_windows"))
    return [(item["start"], item["end"]) for item in _date_ranges(source)]


def _snippet(text, start, end):
    return _text(text[max(0, start - 75):min(len(text), end + 75)])[:240]


def _sentences(text):
    # Contrast clauses have independent requirement polarity: an optional
    # degree must not cancel a mandatory authorization requirement.
    return [part.strip() for part in re.split(
        r"(?<=[.!?;])\s+|\n+|,?\s+\b(?:although|but|whereas|however)\b\s+",
        text, flags=re.I) if part.strip()]


def _preferred(sentence):
    return bool(re.search(r"\b(preferred|preferably|ideally|optional|would be a plus)\b",
                          sentence, re.I))


def _mandatory(sentence):
    if re.search(r"\b(no need|not needed|not required|not mandatory)\b", sentence, re.I):
        return False
    return bool(re.search(
        r"\b(must|required|requires|requirement|mandatory|only|minimum|need(?:s)?(?: to)?|"
        r"necessary|commitment)\b",
        sentence, re.I))


def _boolean(value):
    text = _text(value).lower()
    if text in ("yes", "true", "1", "approved", "confirmed", "eligible"):
        return True
    if text in ("no", "false", "0", "not approved", "denied", "ineligible"):
        return False
    return None


def _profile_year(profile):
    value = _text(profile.get("current_year") or profile.get("year_of_study") or
                  profile.get("current_year_sem"))
    match = re.search(r"\b(?:year\s*)?([1-6])(?:st|nd|rd|th)?\b", value, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(first|second|third|fourth|fifth|sixth)\s+year\b", value, re.I)
    return YEAR_WORDS.get(match.group(1).lower()) if match else None


def _graduation_year(profile):
    match = re.search(r"\b(20\d{2})\b", _text(profile.get("expected_graduation")))
    return int(match.group(1)) if match else None


def _degree_level(value):
    low = _text(value).lower()
    if re.search(r"\b(ph\.?d\.?|doctorate|doctoral)\b", low):
        return 3
    if re.search(r"\b(master'?s?|msc|m\.s\.)\b", low):
        return 2
    if re.search(r"\b(bachelor'?s?|bsc|b\.s\.|undergraduate)\b", low):
        return 1
    return None


def _country(value):
    low = _text(value).lower()
    if not low or re.search(r"\b(unknown|unspecified|none|permanent resident|non[- ]citizen)\b", low):
        return None
    for name in sorted(COUNTRIES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", low):
            return COUNTRIES[name]
    match = re.search(r"\b(?:citizen|national)\s+of\s+([a-z][a-z .'-]*)", low)
    if not match:
        match = re.search(r"\b([a-z][a-z'-]+)\s+(?:citizen|national)\b", low)
    if match:
        return re.sub(r"[^a-z0-9]", "", match.group(1)) or None
    if re.fullmatch(r"[a-z][a-z .'-]*", low):
        return re.sub(r"[^a-z0-9]", "", low)
    return None


def _country_requirement(sentence):
    candidates = ("United States", "United Kingdom", "Singaporean", "Malaysian",
                  "Australian", "Canadian", "Indian", "Singapore", "Malaysia",
                  "Australia", "Canada", "India", "U.S.", "USA", "US", "UK")
    for country in candidates:
        if re.search(r"\b" + re.escape(country) +
                     r"\b.{0,30}\b(?:citizens?|nationals?)\b|"
                     r"\b(?:citizen|national)\s+of\s+" + re.escape(country) + r"\b",
                     sentence, re.I):
            return _country(country)
        if re.search(r"\b(?:only\s+(?:for\s+)?)" + re.escape(country) +
                     r"s?\b", sentence, re.I) and re.search(
                         r"citizen|national|singaporean|malaysian|indian", sentence, re.I):
            return _country(country)
        if re.search(r"\b" + re.escape(country) + r"\s+citizenship\b", sentence, re.I):
            return _country(country)
        if re.search(r"\b" + re.escape(country) +
                     r"\s+(?:applicants?|candidates?|nationals?)\b.{0,30}\bonly\b",
                     sentence, re.I):
            return _country(country)
    normalized = re.sub(r"[.,;:!?]+", " ", sentence)
    generic = re.search(r"\bcitizens?\s+of\s+([A-Za-z][A-Za-z .'-]*?)"
                        r"(?=\s+(?:only|are|required|must|to apply|$))", normalized, re.I)
    if generic:
        return _country(generic.group(1))
    generic = re.search(r"\b([A-Za-z][A-Za-z'-]+)\s+(?:citizens?|nationals?)\b",
                        normalized, re.I)
    if generic:
        return _country(generic.group(1))
    generic = re.search(r"\b([A-Za-z][A-Za-z'-]+)\s+citizenship\b", normalized, re.I)
    if generic:
        return _country(generic.group(1))
    return None


def _spans(text):
    """Sentence spans, using the same boundaries as :func:`_sentences`."""
    spans, pos = [], 0
    for match in _BOUNDARY.finditer(text):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans or [(0, len(text))]


def _context(text, start, end):
    """Return the sentence prefix, the clause lead and the clause trail.

    The lead and trail stop at brackets, semicolons and joining words, so a
    deadline in a neighbouring clause does not describe this date.
    """
    lo, hi = next(((a, b) for a, b in _spans(text) if a <= start < b),
                  (0, len(text)))
    prefix = text[lo:start]
    cuts = [m.end() for m in _LEAD_CUT.finditer(prefix)]
    lead = prefix[max(cuts):] if cuts else prefix
    trail = text[end:max(end, hi)]
    cut = _LEAD_CUT.search(trail)
    if cut:
        trail = trail[:cut.start()]
    return prefix[-100:], lead, trail


def _unrelated(lead, trail):
    return bool(_UNRELATED.search(lead[-80:]) or _TRAIL_UNRELATED.search(trail))


def _period_groups(text, source, parsed):
    """Group attached periods. Each group is a set of accepted alternatives."""
    groups = []
    previous = None
    unrelated_at = []
    for item in parsed:
        prefix, lead, trail = _context(text, item["start_at"], item["end_at"])
        if _unrelated(lead, trail):
            unrelated_at.append(item["start_at"])
            continue
        if _preferred(lead + item["phrase"] + trail):
            continue
        gap = text[previous["end_at"]:item["start_at"]] if previous else ""
        alternative = bool(previous and len(gap) <= 80 and _ALTERNATIVE.search(gap))
        attached = (source == "title" or alternative or
                    bool(_ATTACH.search(prefix[-60:] + " " + trail[:40])))
        if not attached:
            continue
        item["snippet"] = _snippet(text, item["start_at"], item["end_at"])
        item["source"] = source
        if alternative:
            groups[-1].append(item)
        else:
            groups.append([item])
        previous = item
    return groups, unrelated_at


def _item_fits(item, windows):
    """Return (fits, conflicts) over every reading of one period."""
    verdicts = [any(low <= start and end <= high for low, high in windows)
                for start, end in item["candidates"]]
    return all(verdicts), not any(verdicts)


def _group_outcome(group, windows):
    fits = False
    unresolved = None
    for item in group:
        if item["invalid"]:
            unresolved = unresolved or "availability_unclear"
        elif item["yearless"]:
            unresolved = unresolved or "availability_year_unknown"
        else:
            fit, conflict = _item_fits(item, windows)
            if fit:
                fits = True
            elif not conflict:
                unresolved = unresolved or "availability_year_ambiguous"
    if fits:
        return "fit", "availability_fit"
    if unresolved:
        return "unknown", unresolved
    return "conflict", "availability_conflict"


def _unsupported_dates(text):
    found = []
    for lo, hi in _spans(text):
        sentence = text[lo:hi]
        if not (re.search(r"\b(intern(ship)?|placement)\b", sentence, re.I)
                and re.search(r"\b(period|dates?|runs?|from|between|duration)\b", sentence, re.I)
                and not _preferred(sentence)):
            continue
        for match in re.finditer(r"\b\d{1,2}[/.]\d{1,2}[/.]20\d{2}\b", sentence):
            _, lead, trail = _context(text, lo + match.start(), lo + match.end())
            if not _unrelated(lead, trail):
                found.append(sentence[:240])
                break
    return found


def _start_requirements(text, parsed):
    """Find explicit start dates or months that are not part of a period."""
    taken = [(item["start_at"], item["end_at"]) for item in parsed]
    found = []
    for pattern in (_START_BEFORE, _START_AFTER):
        for match in pattern.finditer(text):
            if any(match.start() < hi and match.end() > lo for lo, hi in taken):
                continue
            if any(match.start() < req["end_at"] and match.end() > req["start_at"]
                   for req in found):
                continue
            _, lead, trail = _context(text, match.start(), match.end())
            clause = lead + match.group(0) + trail
            # The start verb is part of the match, so judge the lead with it.
            if (_unrelated(lead + match.group(0), trail) or _preferred(clause)
                    or _START_SOFT.search(clause)):
                continue
            month = _month(match.group("month"))
            day = match.group("day") or match.groupdict().get("day2")
            year = match.group("year")
            try:
                value = date(int(year), month, int(day)) if (year and day) else None
            except ValueError:
                continue
            found.append({"month": month, "year": int(year) if year else None,
                          "day": int(day) if day else None, "value": value,
                          "snippet": _snippet(text, match.start(), match.end()),
                          "start_at": match.start(), "end_at": match.end()})
    return found


def _window_months(windows):
    months = set()
    for low, high in windows:
        year, month = low.year, low.month
        while (year, month) <= (high.year, high.month):
            months.add(month)
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return months


def _start_outcome(req, windows):
    if req["value"] is not None:
        if any(low <= req["value"] <= high for low, high in windows):
            return "compatible", "start_fit"
        return "conflict", "start_conflict"
    if req["year"] is not None:
        first = date(req["year"], req["month"], 1)
        last = _month_end(req["year"], req["month"])
        if any(low <= first and last <= high for low, high in windows):
            return "compatible", "start_fit"
        if not any(low <= last and first <= high for low, high in windows):
            return "conflict", "start_conflict"
        return "unknown", "start_unclear"
    if req["month"] not in _window_months(windows):
        return "conflict", "start_conflict"
    return "unknown", "start_year_unknown"


def _availability_checks(metadata, jd, windows, minimum_days=0):
    """Evaluate periods, alternatives and start dates from title and body.

    Each group of alternatives is judged on its own. Unrelated deadlines are
    not periods. Different outcomes in the title and the description, or in
    unlinked periods, go to review.
    """
    groups = []
    unsupported = []
    starts = []
    unclear = []
    for source, text in (("title", metadata), ("description", jd)):
        if not text:
            continue
        parsed = _parse_ranges(text)
        found, unrelated_at = _period_groups(text, source, parsed)
        groups.extend(found)
        unsupported.extend(_unsupported_dates(text))
        starts.extend(_start_requirements(text, parsed))
        for lo, hi in _spans(text):
            sentence = text[lo:hi]
            if any(lo <= at < hi for at in unrelated_at):
                continue
            if (re.search(r"\b(intern(ship)?|placement)\b", sentence, re.I) and
                    re.search(r"\b(period|dates?|runs?|from|between|duration|summer|"
                              r"winter|h[12])\b", sentence, re.I) and
                    not _preferred(sentence) and not _UNRELATED.search(sentence)):
                unclear.append(sentence[:240])
                break
    results = []
    unique, seen = [], set()
    for group in groups:
        # The same period stated in the title and the description is one fact.
        key = frozenset([str(c) for item in group for c in item["candidates"]] +
                        ["raw:" + item["phrase"].lower() for item in group
                         if not item["candidates"]])
        if key not in seen:
            seen.add(key)
            unique.append(group)
    snippets = [item["snippet"] for group in unique for item in group]
    if unique:
        if not windows:
            if unsupported:
                results.append(("unknown", "unsupported_date_format", unsupported))
            else:
                results.append(("unknown", "availability_unknown", snippets))
        else:
            outcomes = [(group[0]["source"], group) + _group_outcome(group, windows)
                        for group in unique]
            kinds = {outcome[2] for outcome in outcomes}
            evidence = [item["snippet"] for _, group, status, _ in outcomes
                        for item in group if status == "fit"] or snippets
            if kinds == {"fit"}:
                if unsupported:
                    results.append(("unknown", "unsupported_date_format", unsupported))
                else:
                    results.append(("compatible", "availability_fit", evidence))
            elif kinds == {"conflict"}:
                results.append(("conflict", "availability_conflict", snippets))
            elif "unknown" in kinds:
                code = next(o[3] for o in outcomes if o[2] == "unknown")
                results.append(("unknown", code, snippets))
            else:
                fit_sources = {o[0] for o in outcomes if o[2] == "fit"}
                conflict_sources = {o[0] for o in outcomes if o[2] == "conflict"}
                crossed = ("title" in fit_sources and "description" in conflict_sources or
                           "description" in fit_sources and "title" in conflict_sources)
                results.append(("unknown", "availability_source_conflict" if crossed
                                else "availability_unclear", snippets))
    elif unsupported:
        results.append(("unknown", "unsupported_date_format", unsupported))
    elif unclear and not starts:
        results.append(("unknown", "availability_unclear", unclear))
    distinct_starts = {(req["month"], req["year"], req["day"]) for req in starts}
    for req in starts:
        if not windows:
            results.append(("unknown", "availability_unknown", [req["snippet"]]))
        else:
            status, code = _start_outcome(req, windows)
            if status != "conflict" and minimum_days > 1 and len(distinct_starts) == 1:
                remaining = timedelta(days=minimum_days - 1)
                feasible = [(low, high - remaining) for low, high in windows
                            if high - low >= remaining]
                if not feasible or _start_outcome(req, feasible)[0] == "conflict":
                    status, code = "conflict", "start_duration_conflict"
            results.append((status, code, [req["snippet"]]))
    return results


def _graduation_period(profile):
    text = _text(profile.get("expected_graduation"))
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})\b", text)
    year = month = None
    if match and 1 <= int(match.group(2)) <= 12:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        match = re.search(r"\b(" + _MONTH + r")\b\.?,?\s+(20\d{2})\b", text, re.I)
        if match:
            year, month = int(match.group(2)), _month(match.group(1))
        else:
            match = re.search(r"\b(20\d{2})\b", text)
            if not match:
                return None
            year = int(match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
    return date(year, month, 1), _month_end(year, month)


def _graduation_tokens(sentence):
    tokens = []
    for match in re.finditer(r"(?:\b(" + _MONTH + r")\b\.?,?\s+)?\b(20\d{2})\b",
                             sentence, re.I):
        year = int(match.group(2))
        if match.group(1):
            month = _month(match.group(1))
            interval = (date(year, month, 1), _month_end(year, month))
        else:
            interval = (date(year, 1, 1), date(year, 12, 31))
        tokens.append((match.start(), match.end(), interval))
    return tokens


def _graduation_decision(sentence, period):
    """Return (status, code) for a mandatory graduation-cohort sentence."""
    tokens = _graduation_tokens(sentence)
    if not tokens:
        return "unknown", "graduation_unknown"
    if period is None:
        return "unknown", "graduation_unknown"
    low = sentence.lower()
    unclear = ("unknown", "graduation_unclear")
    if re.search(r"\bnot\b", low):
        return unclear
    one_day = date.resolution
    p_lo, p_hi = period
    upper = bool(re.search(
        r"\b(?:by|before|no later than|not later than|at the latest|until|"
        r"on or before)\b", low))
    lower = bool(re.search(
        r"\b(?:after|no earlier than|not earlier than|at the earliest|since)\b", low))
    spanned = bool(re.search(r"\bbetween\b|\bfrom\b.+\bto\b|20\d{2}\s*[-–—]\s*", low))
    intervals = [item[2] for item in tokens]

    def contained(lo, hi):
        return lo <= p_lo and p_hi <= hi

    def disjoint(lo, hi):
        return p_hi < lo or p_lo > hi

    if len(tokens) == 1:
        lo, hi = intervals[0]
        if upper and lower:
            return unclear
        if upper:
            bound = lo - one_day if re.search(r"\bbefore\b", low) and \
                not re.search(r"on or before", low) else hi
            if p_hi <= bound:
                return "compatible", "graduation_match"
            return ("conflict", "graduation_conflict") if p_lo > bound else unclear
        if lower:
            bound = hi + one_day if re.search(r"\bafter\b", low) else lo
            if p_lo >= bound:
                return "compatible", "graduation_match"
            return ("conflict", "graduation_conflict") if p_hi < bound else unclear
        if contained(lo, hi):
            return "compatible", "graduation_match"
        return ("conflict", "graduation_conflict") if disjoint(lo, hi) else unclear
    if upper or lower:
        return unclear
    for left, right in zip(tokens, tokens[1:]):
        gap = sentence[left[1]:right[0]]
        if len(gap) > 24 or not re.search(
                r"\bor\b|\band\b|,|/|\bto\b|\bthrough\b|[-–—]", gap, re.I):
            return unclear
    if spanned and len(tokens) == 2:
        intervals = [(min(i[0] for i in intervals), max(i[1] for i in intervals))]
    if any(contained(lo, hi) for lo, hi in intervals):
        return "compatible", "graduation_match"
    if all(disjoint(lo, hi) for lo, hi in intervals):
        return "conflict", "graduation_conflict"
    return unclear


_DURATION_NUMBER = (r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve)")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12}
_DURATION_CAP = re.compile(
    r"\b(?:maximum|max\.?|at most|up to|no more than|not exceed\w*|capped|"
    r"no longer than|not longer than)\b", re.I)


def _duration_weeks(text):
    """Return explicit minimum durations as dicts of lower-bound weeks.

    ``weeks`` is the lower bound. A range such as ``3 to 6 months`` sets
    ``range`` because only its lower bound is a firm minimum. A cap such as
    ``up to 6 months`` is not a requirement and is ignored.
    """
    results = []
    occupied = []
    range_pattern = re.compile(
        r"\b(" + _DURATION_NUMBER + r")\s*(?:-|–|to)\s*(" + _DURATION_NUMBER +
        r")\s*(weeks?|months?)\b", re.I)
    single_pattern = re.compile(
        r"\b(" + _DURATION_NUMBER + r")\+?\s*-?\s*(weeks?|months?)\b", re.I)

    def amount(raw):
        raw = raw.lower()
        return _NUMBER_WORDS.get(raw) or int(raw)

    def explicit(match):
        _, lead, trail = _context(text, match.start(), match.end())
        clause = lead + match.group(0) + trail
        if re.search(r"industry experience|work experience|experience in|"
                     r"preferred|ideally", clause, re.I):
            return False
        if _DURATION_CAP.search(clause):
            return False
        return (_mandatory(clause) or bool(re.search(
            r"\bintern(ship)?\s+duration\b|\bat least\b|\bmin\.", clause, re.I)))

    for pattern, is_range in ((range_pattern, True), (single_pattern, False)):
        for match in pattern.finditer(text):
            if any(match.start() < high and match.end() > low for low, high in occupied):
                continue
            if not explicit(match):
                continue
            unit = match.group(3 if is_range else 2).lower()
            factor = 4.345 if unit.startswith("month") else 1
            results.append({"weeks": amount(match.group(1)) * factor, "range": is_range,
                            "start": match.start(), "end": match.end()})
            occupied.append((match.start(), match.end()))
    return sorted(results, key=lambda item: item["start"])


def evaluate(posting, profile, jd_text, profile_source=None):
    """Return ``status``, stable ``reason_codes``, evidence, checks and hashes.

    ``profile_source`` may be the original UTF-8 bytes or text of profile.yaml.
    When omitted, a canonical JSON representation of the mapping is hashed.
    The JD hash covers the guarded body. Separate metadata and combined-text
    hashes cover every guarded role/title/body input used for the decision.
    """
    posting = posting if isinstance(posting, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    # Preserve line boundaries: the posting guard removes instruction-shaped
    # lines, while flattening first would quarantine safe JD text with them.
    clean_jd = guard_text(str(jd_text or ""))["clean_text"]
    metadata = "\n".join(_text(posting.get(key)) for key in ("role", "title"))
    clean_metadata = guard_text(metadata)["clean_text"]
    scan_text = guard_text(clean_metadata + "\n" + clean_jd)["clean_text"]
    sentences = _sentences(scan_text)
    windows = _profile_windows(profile)
    checks = {}
    codes = []
    evidence = []
    conflict = False
    unknown = False
    supported = False

    def add(category, status, code, snippets):
        nonlocal conflict, unknown, supported
        supported = True
        conflict = conflict or status == "conflict"
        unknown = unknown or status == "unknown"
        if code not in codes:
            codes.append(code)
        item = checks.setdefault(category, {"status": "not_assessed", "reason_codes": []})
        priority = {"not_assessed": 0, "compatible": 1, "unknown": 2, "conflict": 3}
        if priority[status] > priority[item["status"]]:
            item["status"] = status
        if code not in item["reason_codes"]:
            item["reason_codes"].append(code)
        for snippet in snippets:
            snippet = _text(snippet)[:240]
            entry = {"code": code, "snippet": snippet}
            if snippet and entry not in evidence:
                evidence.append(entry)

    for sentence in sentences:
        # Unseparated mixed-polarity wording cannot establish eligibility.
        if ((_preferred(sentence) or re.search(r"\bnot required\b", sentence, re.I))
                and re.search(r"\b(must|required|mandatory|only)\b", sentence, re.I)
                and re.search(r"\band\b|,", sentence, re.I)):
            add("general", "unknown", "requirement_scope_unclear", [sentence])

    # Dates and required duration use only complete, explicitly attached ranges.
    # Title and body are evaluated separately so that a conflict between them
    # reaches review, while unrelated deadlines never describe the period.
    durations = _duration_weeks(scan_text)
    minimum_weeks = durations[0]["weeks"] if len(durations) == 1 else 0
    for status_, code_, snippets in _availability_checks(
            clean_metadata, clean_jd, windows, math.ceil(minimum_weeks * 7)):
        add("availability", status_, code_, snippets)
    longest = max(((high - low).days + 1) / 7.0 for low, high in windows) if windows else 0
    for item in durations:
        snippet = _snippet(scan_text, item["start"], item["end"])
        if not windows:
            add("availability", "unknown", "availability_unknown", [snippet])
        elif item["range"]:
            # Only the lower bound of "3 to 6 months" is a firm minimum.
            if item["weeks"] > longest:
                add("availability", "conflict", "duration_conflict", [snippet])
            else:
                add("availability", "unknown", "duration_unclear", [snippet])
        elif item["weeks"] > longest:
            add("availability", "conflict", "duration_conflict", [snippet])
        else:
            add("availability", "compatible", "duration_fit", [snippet])

    # Required degree and completion conditions.
    degree_profile = _text(_profile_value(profile, "degree", "degree_program"))
    profile_level = _degree_level(degree_profile)
    completed_value = _boolean(profile.get("degree_completed"))
    completed_level = _degree_level(profile.get("highest_education_completed"))
    for sentence in sentences:
        if _preferred(sentence) or not _mandatory(sentence):
            continue
        if not re.search(r"\b(degree|bachelor|master|ph\.?d\.?|doctorate)\b",
                         sentence, re.I):
            continue
        if re.search(r"\bor\b", sentence, re.I):
            add("degree", "unknown", "degree_alternatives_unclear", [sentence])
            continue
        required_level = _degree_level(sentence)
        pursuing = bool(re.search(r"\b(pursuing|enrolled in|currently studying|working toward)\b",
                                  sentence, re.I))
        if pursuing and required_level and profile_level != required_level:
            add("degree", "unknown", "degree_enrollment_unknown", [sentence])
            continue
        if required_level:
            if profile_level is None:
                add("degree", "unknown", "degree_unknown", [sentence])
            elif profile_level < required_level:
                add("degree", "conflict", "degree_conflict", [sentence])
            else:
                add("degree", "compatible", "degree_match", [sentence])
        completion_wording = bool(re.search(
            r"\b(completed|earned|hold|holder|already graduated|degree must be completed|"
            r"have a|has a)\b", sentence, re.I))
        if required_level and profile_level is not None and profile_level >= required_level and not pursuing:
            if completed_value is True or (completed_level is not None and
                                           completed_level >= required_level):
                add("degree", "compatible", "degree_completion_match", [sentence])
            elif completed_value is False and completion_wording:
                add("degree", "conflict", "degree_completion_conflict", [sentence])
            else:
                add("degree", "unknown", "degree_completion_unknown", [sentence])
        elif not required_level:
            add("degree", "unknown", "degree_unknown", [sentence])

    # Required year of study, including final/penultimate cohort language.
    current_year = _profile_year(profile)
    grad_year = _graduation_year(profile)
    program_length = _profile_year({"current_year": profile.get("program_length_years")})
    for sentence in sentences:
        if _preferred(sentence) or not _mandatory(sentence):
            continue
        if not re.search(r"\b(year\s*(?:of\s+study\s*)?[1-6]|first year|second year|third year|"
                         r"fourth year|fifth year|sixth year|penultimate[- ]year|final[- ]year)\b",
                         sentence, re.I):
            continue
        if re.search(r"\bor\b", sentence, re.I):
            add("year", "unknown", "year_alternatives_unclear", [sentence])
            continue
        numeric_terms = re.findall(r"\byear\s*(?:of\s+study\s*)?[1-6]\b", sentence, re.I)
        word_terms = re.findall(r"\b(first|second|third|fourth|fifth|sixth)\s+year\b",
                                sentence, re.I)
        cohort_terms = re.findall(r"\b(penultimate[- ]year|final[- ]year)\b", sentence, re.I)
        if len(numeric_terms) + len(word_terms) + len(cohort_terms) > 1:
            add("year", "unknown", "year_unclear", [sentence])
            continue
        required_year = None
        number_match = re.search(r"\byear\s*(?:of\s+study\s*)?([1-6])\b", sentence, re.I)
        word_match = re.search(r"\b(first|second|third|fourth|fifth|sixth)\s+year\b",
                               sentence, re.I)
        if number_match:
            required_year = int(number_match.group(1))
        elif word_match:
            required_year = YEAR_WORDS[word_match.group(1).lower()]
        target_year = None
        dates = _date_ranges(sentence)
        if dates:
            target_year = dates[0]["start"].year
        else:
            year_match = re.search(r"\b(20\d{2})\b", sentence)
            if year_match:
                target_year = int(year_match.group(1))
        if required_year is not None:
            if current_year is None:
                add("year", "unknown", "year_unknown", [sentence])
            elif current_year == required_year:
                add("year", "compatible", "year_match", [sentence])
            else:
                add("year", "conflict", "year_conflict", [sentence])
        elif re.search(r"penultimate", sentence, re.I):
            inferred = None
            if grad_year is not None and target_year is not None:
                inferred = "penultimate" if grad_year - target_year == 1 else "other"
            elif current_year is not None and program_length is not None:
                inferred = "penultimate" if current_year == program_length - 1 else "other"
            if inferred is None:
                add("year", "unknown", "year_unknown", [sentence])
            elif inferred == "penultimate":
                add("year", "compatible", "year_match", [sentence])
            else:
                add("year", "conflict", "year_conflict", [sentence])
        elif re.search(r"final[- ]year", sentence, re.I):
            inferred = None
            if grad_year is not None and target_year is not None:
                inferred = "final" if grad_year == target_year else "other"
            elif current_year is not None and program_length is not None:
                inferred = "final" if current_year == program_length else "other"
            if inferred is None:
                add("year", "unknown", "year_unknown", [sentence])
            elif inferred == "final":
                add("year", "compatible", "year_match", [sentence])
            else:
                add("year", "conflict", "year_conflict", [sentence])

    # Explicit expected graduation year or deadline.
    for sentence in sentences:
        if _preferred(sentence) or not _mandatory(sentence):
            continue
        if not re.search(r"graduat\w*|class of", sentence, re.I):
            continue
        # Month, range, bound and alternative cohorts are compared as periods.
        status, code = _graduation_decision(sentence, _graduation_period(profile))
        add("graduation", status, code, [sentence])

    # Citizenship is compared only with a direct profile fact; residence and
    # student authorization never imply nationality.
    citizenship_fact = _profile_value(profile, "citizenship", "nationality")
    profile_country = _country(citizenship_fact)
    for sentence in sentences:
        if _preferred(sentence) or not re.search(r"citizenship|citizen|nationality|national|singaporean",
                                                 sentence, re.I):
            continue
        if not _mandatory(sentence) and not re.search(r"citizens? only|nationals? only", sentence, re.I):
            continue
        if re.search(r"\bor\b", sentence, re.I):
            add("citizenship", "unknown", "citizenship_alternatives_unclear", [sentence])
            continue
        required_country = _country_requirement(sentence)
        if profile_country is None or required_country is None:
            add("citizenship", "unknown", "citizenship_unknown", [sentence])
        elif profile_country == required_country:
            add("citizenship", "compatible", "citizenship_match", [sentence])
        else:
            add("citizenship", "conflict", "citizenship_conflict", [sentence])

    # Work authorization jurisdiction and sponsorship are separate from
    # nationality. Missing or vague authorization text remains unknown.
    auth_value = _text(_profile_value(profile, "work_authorisation", "work_authorization"))
    auth_country = _country(_profile_value(profile, "work_authorisation_country",
                                          "work_authorization_country"))
    if auth_country is None:
        auth_country = _country(auth_value)
    auth_low = auth_value.lower()
    auth_yes = bool(re.search(r"\b(authorized|authorised|eligible|valid|permit|pass|right to work)\b",
                              auth_low))
    auth_no = bool(re.search(r"\b(not authorized|not authorised|no right to work|expired)\b", auth_low))
    sponsorship_needed = bool(re.search(r"\b(needs?|requires?)\s+(?:employer\s+)?sponsorship\b|"
                                        r"\bsponsorship required\b", auth_low))
    sponsorship_not_needed = bool(re.search(r"\b(no|without|does not require|do not need|not need)\s+"
                                            r"(?:employer\s+)?sponsorship\b", auth_low))
    for sentence in sentences:
        if _preferred(sentence) or not _mandatory(sentence):
            continue
        low = sentence.lower()
        mentions_auth = re.search(r"work authori[sz]ation|authorized to work|authorised to work|"
                                  r"eligible to work|right to work|sponsor(?:ship)?", low)
        if not mentions_auth:
            continue
        no_sponsor_rule = bool(re.search(
            r"without (?:employer )?sponsorship|no (?:employer )?sponsorship|"
            r"will not sponsor|does not provide sponsorship|sponsorship is not available", low))
        if no_sponsor_rule:
            if sponsorship_needed:
                add("work_authorisation", "conflict", "work_authorisation_conflict", [sentence])
                continue
            if not sponsorship_not_needed:
                add("work_authorisation", "unknown", "work_authorisation_unknown", [sentence])
                continue
            has_jurisdiction_requirement = bool(re.search(
                r"work authori[sz]ation|authorized to work|authorised to work|"
                r"eligible to work|right to work", low))
            if not has_jurisdiction_requirement:
                add("work_authorisation", "compatible", "work_authorisation_match", [sentence])
                continue
        required_country = None
        country_match = re.search(r"\b(?:work in|in|for)\s+(Singapore|Malaysia|India|Australia|Canada|"
                                  r"the United States|the United Kingdom|US|UK)\b", sentence, re.I)
        if not country_match:
            generic_country = re.search(
                r"\b(?:work in|in|for)\s+(?:the\s+)?([A-Za-z][A-Za-z-]+)\b", sentence, re.I)
            if generic_country and generic_country.group(1).lower() not in ("a", "an"):
                required_country = _country(generic_country.group(1))
        if country_match:
            required_country = _country(country_match.group(1))
        if auth_no:
            add("work_authorisation", "conflict", "work_authorisation_conflict", [sentence])
        elif not auth_value or not auth_yes:
            add("work_authorisation", "unknown", "work_authorisation_unknown", [sentence])
        elif required_country and auth_country is None:
            add("work_authorisation", "unknown", "work_authorisation_unknown", [sentence])
        elif required_country and auth_country != required_country:
            add("work_authorisation", "conflict", "work_authorisation_conflict", [sentence])
        else:
            add("work_authorisation", "compatible", "work_authorisation_match", [sentence])

    # Required school approval and academic credit are independent profile facts.
    for sentence in sentences:
        if _preferred(sentence) or not _mandatory(sentence):
            continue
        if re.search(r"school[- ]approved|university[- ]approved|school approval|university approval",
                     sentence, re.I):
            approval = _boolean(_profile_value(profile, "school_approval", "university_approval"))
            if approval is True:
                add("school_approval", "compatible", "school_approval_match", [sentence])
            elif approval is False:
                add("school_approval", "conflict", "school_approval_conflict", [sentence])
            else:
                add("school_approval", "unknown", "school_approval_unknown", [sentence])
        if re.search(r"credit[- ]bearing|academic credit|for credit", sentence, re.I):
            credit = _boolean(profile.get("credit_bearing_internship"))
            if credit is True:
                add("credit", "compatible", "credit_match", [sentence])
            elif credit is False:
                add("credit", "conflict", "credit_conflict", [sentence])
            else:
                add("credit", "unknown", "credit_unknown", [sentence])

    # Unconfirmed clearances remain visible for review rather than becoming a
    # nationality veto.
    for sentence in sentences:
        if (re.search(r"security clearance", sentence, re.I) and _mandatory(sentence) and
                not _preferred(sentence)):
            clearance = _boolean(profile.get("security_clearance"))
            if clearance is True:
                add("security_clearance", "compatible", "security_clearance_match", [sentence])
            elif clearance is False:
                add("security_clearance", "conflict", "security_clearance_conflict", [sentence])
            else:
                add("security_clearance", "unknown", "security_clearance_unknown", [sentence])

    if not supported:
        add("general", "unknown", "insufficient_evidence", [])

    status = "ineligible" if conflict else ("unknown" if unknown else "eligible")
    return {
        "status": status,
        "reason_codes": codes,
        "reasons": list(codes),
        "evidence": evidence,
        "checks": checks,
        "provenance": {
            "policy_revision": 2,
            "profile_sha256": _digest(profile_source if profile_source is not None else profile),
            "jd_sha256": _digest(clean_jd),
            "posting_metadata_sha256": _digest(clean_metadata),
            "evaluated_text_sha256": _digest(scan_text),
        },
    }
