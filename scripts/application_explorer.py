"""Bounded, URL-addressable application explorer query helpers."""
import hashlib
import re
from datetime import date
from urllib.parse import urlencode

PAGE_SIZE = 25
LANE_SIZE = 10
MAX_PAGE = 10000
MAX_QUERY_LENGTH = 120
SORTS = ("company", "role", "status", "source", "applied_date", "stage_since")
STAGES = ("Found", "Tailored", "Applied", "Callback", "OA", "Interview",
          "Accepted", "Rejected", "Unknown / unmatched")
LANES = ("found", "tailored", "applied", "callback", "oa", "interview",
         "accepted", "rejected", "unknown")


def parse_query(query):
    """Parse an already length-bounded query, rejecting unknown/duplicate keys."""
    from urllib.parse import parse_qsl
    if not isinstance(query, str) or len(query) > 2048:
        raise ValueError("query too long")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=24, encoding="utf-8", errors="strict") if query else []
    except (ValueError, UnicodeError):
        raise ValueError("malformed query")
    allowed = {"q", "stage", "sort", "dir", "page", "view", "period",
               "start", "end", "metric", "from", "to", "company"}
    allowed.update("load_" + lane for lane in LANES)
    values = {}
    for key, value in pairs:
        if key not in allowed or key in values:
            raise ValueError("unsupported query parameter")
        values[key] = value
    q = values.get("q", "")
    if len(q) > MAX_QUERY_LENGTH or any(ord(ch) < 32 or ord(ch) == 127 for ch in q):
        raise ValueError("invalid search query")
    stage = values.get("stage", "all")
    if stage not in ("all",) + STAGES:
        raise ValueError("invalid stage")
    sort = values.get("sort", "company")
    if sort not in SORTS:
        raise ValueError("invalid sort")
    direction = values.get("dir", "asc")
    if direction not in ("asc", "desc"):
        raise ValueError("invalid direction")
    page = _bounded_int(values.get("page", "1"), 1, MAX_PAGE)
    view = values.get("view", "table")
    if view not in ("table", "board"):
        raise ValueError("invalid view")
    loads = {}
    for lane in LANES:
        key = "load_" + lane
        if key in values:
            loads[lane] = _bounded_int(values[key], LANE_SIZE, 10000)
            if loads[lane] % LANE_SIZE:
                raise ValueError("invalid lane load")
    period = values.get("period", "last12weeks")
    if period not in ("last12weeks", "thisweek", "thismonth", "last3months",
                      "alltime", "custom"):
        raise ValueError("invalid period")
    start_day = _query_date(values.get("start"), "start")
    end_day = _query_date(values.get("end"), "end")
    if period == "custom":
        if start_day is None or end_day is None or start_day > end_day:
            raise ValueError("invalid custom range")
    elif start_day is not None or end_day is not None:
        raise ValueError("dates require custom period")
    metric = values.get("metric")
    date_start = _query_date(values.get("from"), "from")
    date_end = _query_date(values.get("to"), "to")
    if metric is not None:
        if metric not in ("confirmed_submissions", "recorded_claims",
                          "initial_tailoring", "retailoring"):
            raise ValueError("invalid metric")
        if date_start is None or date_end is None or date_start > date_end:
            raise ValueError("metric selection needs a valid date range")
    elif date_start is not None or date_end is not None:
        raise ValueError("date selection needs a metric")
    company = values.get("company")
    if company is not None and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", company):
        raise ValueError("invalid company key")
    return {"q": q, "stage": stage, "sort": sort, "dir": direction,
            "page": page, "view": view, "loads": loads, "period": period,
            "start": start_day.isoformat() if start_day else None,
            "end": end_day.isoformat() if end_day else None,
            "metric": metric,
            "from": date_start.isoformat() if date_start else None,
            "to": date_end.isoformat() if date_end else None,
            "company": company}


def _query_date(value, field):
    if value is None:
        return None
    if value == "":
        return None
    if len(value) != 10:
        raise ValueError("invalid {} date".format(field))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid {} date".format(field))
    if parsed.isoformat() != value:
        raise ValueError("invalid {} date".format(field))
    return parsed


def _bounded_int(value, minimum, maximum):
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        raise ValueError("invalid number")
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError("number out of range")
    return number


def opaque_key(application_id):
    if not isinstance(application_id, str) or not application_id:
        return None
    return hashlib.sha256(application_id.encode("utf-8")).hexdigest()


def filtered_rows(rows, state):
    query = state["q"].casefold()
    result = [row for row in rows
              if (state["stage"] == "all" or row.get("status") == state["stage"])
              and (not query or query in " ".join(str(row.get(field) or "")
                                                   for field in ("company", "role", "source", "status")).casefold())]
    field = state["sort"]
    reverse = state["dir"] == "desc"
    present = [row for row in result if row.get(field) not in (None, "")]
    missing = [row for row in result if row.get(field) in (None, "")]
    present.sort(key=lambda row: (str(row.get(field)).casefold(),
                                 str(row.get("company") or "").casefold(),
                                 str(row.get("role") or "").casefold()), reverse=reverse)
    return present + missing


def query_href(state, **updates):
    next_state = dict(state)
    loads = dict(state.get("loads", {}))
    next_state.update(updates)
    if "loads" in updates:
        loads = dict(updates["loads"])
    next_state["loads"] = loads
    pairs = [(key, next_state[key]) for key in ("q", "stage", "sort", "dir", "page", "view",
             "period", "start", "end", "metric", "from", "to", "company")
             if next_state.get(key) not in (None, "", "all" if key == "stage" else None)]
    if next_state.get("stage", "all") == "all":
        pairs.append(("stage", "all"))
    for lane in LANES:
        if lane in loads:
            pairs.append(("load_" + lane, loads[lane]))
    return "/jobs/applications/?" + urlencode(pairs)
