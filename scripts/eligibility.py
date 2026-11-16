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
import re
from datetime import date

from posting_guard import guard_text


MONTHS = {name.lower(): index for index, name in enumerate(
    "January February March April May June July August September October "
    "November December".split(), 1)}
MONTHS.update({name[:3]: value for name, value in list(MONTHS.items())})
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
    return MONTHS.get(value.lower()[:3])


def _date_ranges(text):
    """Parse supported full-date and month-range forms with source offsets."""
    patterns = [
        ("iso", re.compile(
            r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\s*(?:to|[-–—])\s*"
            r"(20\d{2})-(\d{1,2})-(\d{1,2})\b", re.I)),
        ("dmy", re.compile(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(20\d{2})\s*"
            r"(?:to|[-–—])\s*(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"([A-Za-z]+)\s+(20\d{2})\b", re.I)),
        ("mdy", re.compile(
            r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\s*"
            r"(?:to|[-–—])\s*([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+"
            r"(20\d{2})\b", re.I)),
        ("months", re.compile(
            r"\b([A-Za-z]+)\s*(?:to|[-–—])\s*([A-Za-z]+)\s+(20\d{2})\b",
            re.I)),
    ]
    found = []
    occupied = []
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < hi and match.end() > lo for lo, hi in occupied):
                continue
            try:
                if kind == "iso":
                    start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    end = date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
                elif kind == "dmy":
                    start = date(int(match.group(3)), _month(match.group(2)), int(match.group(1)))
                    end = date(int(match.group(6)), _month(match.group(5)), int(match.group(4)))
                elif kind == "mdy":
                    start = date(int(match.group(3)), _month(match.group(1)), int(match.group(2)))
                    end = date(int(match.group(6)), _month(match.group(4)), int(match.group(5)))
                else:
                    year = int(match.group(3))
                    first_month, last_month = _month(match.group(1)), _month(match.group(2))
                    start = date(year, first_month, 1)
                    end = date(year, last_month, calendar.monthrange(year, last_month)[1])
            except (TypeError, ValueError):
                continue
            if end < start:
                continue
            found.append({"start": start, "end": end, "start_at": match.start(),
                          "end_at": match.end(), "phrase": match.group(0)})
            occupied.append((match.start(), match.end()))
    return sorted(found, key=lambda item: item["start_at"])


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


def _availability_check(text, profile, windows):
    # Inspect every date clause before accepting any supported interval.
    # A second mandatory range in an unsupported format still needs review.
    unsupported = []
    for sentence in _sentences(text):
        if (re.search(r"\b(intern(ship)?|placement)\b", sentence, re.I)
                and re.search(r"\b(period|dates?|runs?|from|between|duration)\b", sentence, re.I)
                and not _preferred(sentence)
                and re.search(r"\b\d{1,2}[/.]\d{1,2}[/.]20\d{2}\b", sentence)):
            unsupported.append(sentence[:240])
    matches = _date_ranges(text)
    relevant = []
    for item in matches:
        context = text[max(0, item["start_at"] - 85):min(len(text), item["end_at"] + 85)]
        attached = re.search(
            r"\b(intern(ship)?|placement)\b.{0,60}\b(period|dates?|runs?|is|from|between|start|during)\b|"
            r"\b(period|dates?|runs?|from|between|starts?)\b.{0,60}\b(intern(ship)?|placement)\b",
            context, re.I)
        if (not attached or _preferred(context) or
                re.search(r"\b(deadline|applications? close|application closes)\b", context, re.I)):
            continue
        relevant.append((item, context))

    if relevant:
        if not windows:
            if unsupported:
                return "unknown", "unsupported_date_format", unsupported
            return "unknown", "availability_unknown", [
                _snippet(text, item["start_at"], item["end_at"]) for item, _ in relevant]
        if len(relevant) > 1:
            between = text[relevant[0][0]["end_at"]:relevant[-1][0]["start_at"]]
            if not re.search(r"\bor\b|alternatively", between, re.I):
                return "unknown", "availability_unclear", [
                    _snippet(text, item["start_at"], item["end_at"])
                    for item, _ in relevant]
        fits = [any(start >= low and end <= high for low, high in windows)
                for item, _ in relevant for start, end in [(item["start"], item["end"])]]
        if any(fits):
            if unsupported:
                return "unknown", "unsupported_date_format", unsupported
            return "compatible", "availability_fit", [
                _snippet(text, item["start_at"], item["end_at"])
                for item, _ in relevant if any(item["start"] >= low and item["end"] <= high
                                                for low, high in windows)]
        return "conflict", "availability_conflict", [
            _snippet(text, item["start_at"], item["end_at"]) for item, _ in relevant]

    if unsupported:
        return "unknown", "unsupported_date_format", unsupported
    for sentence in _sentences(text):
        if (re.search(r"\b(intern(ship)?|placement)\b", sentence, re.I) and
                re.search(r"\b(period|dates?|runs?|from|between|duration|summer|winter|h[12])\b",
                          sentence, re.I) and not _preferred(sentence)):
            return "unknown", "availability_unclear", [sentence[:240]]
    return None, None, []


def _duration_weeks(text):
    results = []
    range_pattern = re.compile(r"\b(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*"
                               r"(weeks?|months?)\b", re.I)
    single_pattern = re.compile(r"\b(\d{1,2}|six)\+?\s*-?\s*(weeks?|months?)\b", re.I)
    occupied = []
    for match in range_pattern.finditer(text):
        context = text[max(0, match.start() - 75):min(len(text), match.end() + 75)]
        if re.search(r"industry experience|work experience|preferred|ideally", context, re.I):
            continue
        explicit = _mandatory(context) or bool(re.search(r"\bintern(ship)?\s+duration\b", context, re.I))
        if not explicit:
            continue
        results.append((None, match.start(), match.end()))
        occupied.append((match.start(), match.end()))
    for match in single_pattern.finditer(text):
        if any(match.start() < high and match.end() > low for low, high in occupied):
            continue
        context = text[max(0, match.start() - 75):min(len(text), match.end() + 75)]
        if re.search(r"industry experience|work experience|preferred|ideally", context, re.I):
            continue
        explicit = _mandatory(context) or bool(re.search(r"\bintern(ship)?\s+duration\b", context, re.I))
        if not explicit:
            continue
        raw = match.group(1).lower()
        amount = 6 if raw == "six" else int(raw)
        factor = 4.345 if match.group(2).lower().startswith("month") else 1
        results.append((amount * factor, match.start(), match.end()))
    return results


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
    availability_status, availability_code, snippets = _availability_check(
        scan_text, profile, windows)
    if availability_status:
        add("availability", availability_status, availability_code, snippets)
    durations = _duration_weeks(scan_text)
    for weeks, start, end in durations:
        snippet = _snippet(scan_text, start, end)
        if weeks is None:
            add("availability", "unknown", "duration_unclear", [snippet])
        elif not windows:
            add("availability", "unknown", "availability_unknown", [snippet])
        elif weeks > max((high - low).days / 7.0 for low, high in windows):
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
        if not re.search(r"graduate|graduation|class of", sentence, re.I):
            continue
        required_year_match = re.search(r"\b(20\d{2})\b", sentence)
        if not required_year_match:
            add("graduation", "unknown", "graduation_unknown", [sentence])
            continue
        required_year = int(required_year_match.group(1))
        profile_grad_year = _graduation_year(profile)
        if profile_grad_year is None:
            add("graduation", "unknown", "graduation_unknown", [sentence])
        elif re.search(r"\b(by|before|no later than)\b", sentence, re.I):
            status = "compatible" if profile_grad_year <= required_year else "conflict"
            add("graduation", status,
                "graduation_match" if status == "compatible" else "graduation_conflict", [sentence])
        else:
            status = "compatible" if profile_grad_year == required_year else "conflict"
            add("graduation", status,
                "graduation_match" if status == "compatible" else "graduation_conflict", [sentence])

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
            "profile_sha256": _digest(profile_source if profile_source is not None else profile),
            "jd_sha256": _digest(clean_jd),
            "posting_metadata_sha256": _digest(clean_metadata),
            "evaluated_text_sha256": _digest(scan_text),
        },
    }
