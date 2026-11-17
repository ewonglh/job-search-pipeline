"""Bounded, URL-addressable application explorer query helpers."""
import hashlib
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
                          max_num_fields=16, encoding="utf-8", errors="strict") if query else []
    except (ValueError, UnicodeError):
        raise ValueError("malformed query")
    allowed = {"q", "stage", "sort", "dir", "page", "view"}
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
    return {"q": q, "stage": stage, "sort": sort, "dir": direction,
            "page": page, "view": view, "loads": loads}


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
    pairs = [(key, next_state[key]) for key in ("q", "stage", "sort", "dir", "page", "view")
             if next_state.get(key) not in (None, "", "all" if key == "stage" else None)]
    if next_state.get("stage", "all") == "all":
        pairs.append(("stage", "all"))
    for lane in LANES:
        if lane in loads:
            pairs.append(("load_" + lane, loads[lane]))
    return "/jobs/applications/?" + urlencode(pairs)
