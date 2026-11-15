"""Shared application identity and conservative tracker/queue reconciliation.

IDs are stable strings: ``link:<normalized posting URL>``, ``pr:<number>``, or
``identity:<exact employer>|<exact role>``. The resolver returns a routing map
for every accepted alias and explicit, inspectable conflicts for anything it
cannot safely reconcile.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid",
    "trk", "tracking", "trackingid", "tracking_id", "referral", "referrer",
    "campaign", "campaignid",
}


def normalize_posting_url(value: object) -> str:
    """Normalize a posting URL while preserving job-identifying query params.

    Scheme and host are lowercased; default ports, fragments, trailing path
    slashes, and known click trackers are removed. Path case and all other
    query parameters are kept. Query pairs are sorted to make parameter order
    irrelevant. URLs with credentials or non-HTTP schemes are rejected.
    """
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return ""
        host = host.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parts.port
        if port and not ((scheme == "http" and port == 80) or
                         (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_")
                 and key.lower() not in TRACKING_QUERY_KEYS]
        query_text = urlencode(sorted(query))
        return urlunsplit((scheme, host, parts.path.rstrip("/"), query_text, ""))
    except ValueError:
        return ""


def pr_number(row: dict) -> int:
    value = row.get("pr_number")
    if value not in (None, ""):
        match = re.fullmatch(r"[1-9]\d*", str(value))
        if match:
            return int(match.group(0))
    legacy=str(row.get('pr') or '')
    if re.fullmatch(r'[1-9]\d*',legacy):return int(legacy)
    match = re.search(r"/pull/(\d+)(?:[/?#]|$)", legacy)
    return int(match.group(1)) if match else 0


def application_id(row: dict) -> str:
    """Return the preferred deterministic ID for a single tracker/queue row."""
    link = normalize_posting_url(row.get("link"))
    if link:
        return "link:" + link
    number = pr_number(row)
    if number:
        return "pr:" + str(number)
    company = str(row.get("company") or "").strip()
    role = str(row.get("role") or "").strip()
    return f"identity:{company}|{role}"


def _identity_alias(row: dict) -> Optional[str]:
    company = str(row.get("company") or "").strip()
    role = str(row.get("role") or "").strip()
    if company and role:
        return f"identity:{company}|{role}"
    return None


def _date_value(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _submission_dates(rows: list[dict], side: str) -> set[date]:
    dates = set()
    for row in rows:
        value = row.get("applied_date") if side == "tracker" else row.get("submitted_at")
        parsed = _date_value(value)
        if parsed:
            dates.add(parsed)
    return dates


def _row_aliases(row: dict) -> set[str]:
    aliases = set()
    link = normalize_posting_url(row.get("link"))
    if link:
        aliases.add("link:" + link)
    number = pr_number(row)
    if number:
        aliases.add("pr:" + str(number))
    identity = _identity_alias(row)
    if identity:
        aliases.add(identity)
    return aliases


def _same_exact_identity(left: dict, right: dict) -> bool:
    return (_identity_alias(left) is not None
            and _identity_alias(left) == _identity_alias(right))


def resolve_applications(tracker_rows: list[dict], queue_rows: list[dict],
                         explicit_aliases=()) -> dict:
    """Reconcile submissions and return canonical rows, aliases, and conflicts.

    Strong link IDs join exact normalized links. A PR may bridge a single
    tracker row and a single queue row, including different ATS/posting URLs,
    only when their dated submission evidence agrees. Same-source duplicate
    PR rows, conflicting dates, multiple links per PR, and ambiguous exact
    employer/role fallback are quarantined. `explicit_aliases` entries require
    ``aliases`` (ID list), ``canonical_id``, nonempty ``reason``, and nonempty
    ``evidence``; they are never inferred from names.

    Return value keys: ``applications`` (canonical ID -> merged record),
    ``alias_to_canonical``, ``conflicts`` (list of reasons/evidence), and
    ``diagnostics`` (simple counts).
    """
    records = ([{"source": "tracker", "row": row} for row in tracker_rows]
               + [{"source": "queue", "row": row} for row in queue_rows])
    for record in records:
        record["aliases"] = _row_aliases(record["row"])

    alias_map: dict[str, str] = {}
    conflicts: list[dict] = []
    diagnostics = Counter()
    consumed: set[int] = set()
    applications = {}

    def emit_conflict(reason, indexes, aliases, detail):
        conflicts.append({"reason": reason,
                          "row_refs": [f"{records[i]['source']}:{i + 1}" for i in indexes],
                          "aliases": sorted(aliases), "detail": detail})
        diagnostics[reason] += 1
        consumed.update(indexes)

    # Explicit, evidence-backed aliases are applied before automatic matching
    # so they can deliberately resolve otherwise quarantined link/PR groups.
    for item in explicit_aliases:
        if not isinstance(item, dict):
            raise ValueError("explicit alias resolution must be an object")
        raw_aliases = item.get("aliases")
        canonical = str(item.get("canonical_id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if (not isinstance(raw_aliases, list) or len(raw_aliases) < 2
                or not canonical or not reason or not evidence):
            raise ValueError("each explicit alias needs aliases[], canonical_id, reason, and evidence")
        requested = {str(alias).strip() for alias in raw_aliases if str(alias).strip()}
        indexes = [i for i, record in enumerate(records)
                   if record["aliases"] & requested]
        if len(indexes) < 2:
            continue
        tracker_indexes = [i for i in indexes if records[i]["source"] == "tracker"]
        queue_indexes = [i for i in indexes if records[i]["source"] == "queue"]
        if len(tracker_indexes) != 1 or len(queue_indexes) != 1:
            emit_conflict("explicit_alias_nonunique_source", indexes, requested,
                          "An explicit alias must identify exactly one tracker row and one queue row.")
            continue
        prs = {alias for i in indexes for alias in records[i]["aliases"]
               if alias.startswith("pr:")}
        if len(prs) > 1:
            emit_conflict("explicit_alias_pr_conflict", indexes, requested | prs,
                          "An explicit alias cannot combine contradictory PR submission facts.")
            continue
        facts_conflict = any(
            str(records[tracker_indexes[0]]["row"].get(key) or "").strip()
            and str(records[queue_indexes[0]]["row"].get(key) or "").strip()
            and str(records[tracker_indexes[0]]["row"].get(key) or "").strip()
                != str(records[queue_indexes[0]]["row"].get(key) or "").strip()
            for key in ("company", "role"))
        if facts_conflict:
            emit_conflict("explicit_alias_identity_conflict", indexes, requested,
                          "An explicit alias cannot hide contradictory employer or role facts.")
            continue
        dates = set()
        for i in indexes:
            side = records[i]["source"]
            dates.update(_submission_dates([records[i]["row"]], side))
        if len(dates) > 1:
            emit_conflict("explicit_alias_date_conflict", indexes, requested,
                          f"Explicit identity evidence does not resolve conflicting dates: {sorted(map(str, dates))}.")
            continue
        if any(i in consumed for i in indexes):
            emit_conflict("overlapping_explicit_alias", indexes, requested,
                          "Explicit resolution overlaps a row already assigned by another resolution.")
            continue
        _merge_component(records, indexes, canonical, alias_map, applications,
                         "explicit_alias", diagnostics)
        applications[canonical]["resolution"] = {
            "method": "explicit_alias", "reason": reason, "evidence": evidence}
        consumed.update(indexes)

    # Link identity groups are definitive only when each source contributes
    # one distinct row. Multiple rows with the same link can be contradictory.
    by_link = defaultdict(list)
    for index, record in enumerate(records):
        link_ids = [alias for alias in record["aliases"] if alias.startswith("link:")]
        if len(link_ids) == 1:
            by_link[link_ids[0]].append(index)
    for link_id, indexes in by_link.items():
        if len(indexes) < 2:
            continue
        tracker = [i for i in indexes if records[i]["source"] == "tracker"]
        queue = [i for i in indexes if records[i]["source"] == "queue"]
        pr_ids = {alias for i in indexes for alias in records[i]["aliases"]
                  if alias.startswith("pr:")}
        if len(pr_ids) > 1:
            emit_conflict("multiple_prs_same_link", indexes, {link_id} | pr_ids,
                          "Different PRs share a posting URL; choose an explicit application alias.")
            continue
        if len(tracker) > 1 or len(queue) > 1:
            emit_conflict("multiple_rows_same_link", indexes, {link_id},
                          "More than one row from a source shares this posting URL.")
            continue
        # Exact link is enough to join, but disagreement in submission dates is not.
        td = _submission_dates([records[i]["row"] for i in tracker], "tracker")
        qd = _submission_dates([records[i]["row"] for i in queue], "queue")
        if td and qd and td != qd:
            emit_conflict("submission_date_conflict", indexes, {link_id},
                          f"tracker dates {sorted(map(str, td))} differ from queue dates {sorted(map(str, qd))}.")
            continue
        _merge_component(records, indexes, link_id, alias_map, applications,
                         "exact_posting_link", diagnostics)
        consumed.update(indexes)

    # A unique PR can bridge one tracker record to one queue record only.
    by_pr = defaultdict(list)
    for index, record in enumerate(records):
        if index in consumed:
            continue
        for alias in record["aliases"]:
            if alias.startswith("pr:"):
                by_pr[alias].append(index)
    for pr_id, indexes in by_pr.items():
        if len(indexes) < 2:
            continue
        tracker = [i for i in indexes if records[i]["source"] == "tracker"]
        queue = [i for i in indexes if records[i]["source"] == "queue"]
        if len(tracker) != 1 or len(queue) != 1 or len(indexes) != 2:
            emit_conflict("nonunique_pr_alias", indexes, {pr_id},
                          "PR reference does not identify exactly one tracker and one queue row.")
            continue
        combined = tracker + queue
        td = _submission_dates([records[i]["row"] for i in tracker], "tracker")
        qd = _submission_dates([records[i]["row"] for i in queue], "queue")
        if td and qd and td != qd:
            emit_conflict("submission_date_conflict", indexes, {pr_id},
                          f"tracker dates {sorted(map(str, td))} differ from queue dates {sorted(map(str, qd))}.")
            continue
        link_ids = {alias for i in indexes for alias in records[i]["aliases"]
                    if alias.startswith("link:")}
        if len(link_ids) > 2:
            emit_conflict("multiple_links_for_pr", indexes, {pr_id} | link_ids,
                          "A unique PR pair contains more than two distinct route URLs.")
            continue
        tracker_links = sorted(alias for i in tracker for alias in records[i]["aliases"]
                               if alias.startswith("link:"))
        queue_links = sorted(alias for i in queue for alias in records[i]["aliases"]
                             if alias.startswith("link:"))
        canonical = (tracker_links[0] if tracker_links else
                     queue_links[0] if queue_links else
                     sorted(link_ids)[0] if link_ids else pr_id)
        _merge_component(records, combined, canonical, alias_map, applications,
                         "unique_pr_cross_source", diagnostics)
        consumed.update(indexes)

    # Fallback identity is exact case-sensitive employer and role text. It may
    # join an unkeyed row to one keyed counterpart only if there is no conflict.
    by_identity = defaultdict(list)
    for index, record in enumerate(records):
        if index in consumed:
            continue
        identity = _identity_alias(record["row"])
        if identity:
            by_identity[identity].append(index)
    for identity, indexes in by_identity.items():
        if len(indexes) == 1:
            i = indexes[0]
            canonical = application_id(records[i]["row"])
            _merge_component(records, [i], canonical, alias_map, applications,
                             "single_exact_identity", diagnostics)
            consumed.add(i)
            continue
        rows = [records[i] for i in indexes]
        tracker = [i for i in indexes if records[i]["source"] == "tracker"]
        queue = [i for i in indexes if records[i]["source"] == "queue"]
        distinct_links = {alias for row in rows for alias in row["aliases"]
                          if alias.startswith("link:")}
        distinct_prs = {alias for row in rows for alias in row["aliases"]
                        if alias.startswith("pr:")}
        if len(distinct_links) > 1 or len(distinct_prs) > 1:
            emit_conflict("identity_has_conflicting_strong_ids", indexes,
                          {identity} | distinct_links | distinct_prs,
                          "Exact company/role maps to rows with strong but different link or PR IDs.")
            continue
        td = _submission_dates([records[i]["row"] for i in tracker], "tracker")
        qd = _submission_dates([records[i]["row"] for i in queue], "queue")
        if td and qd and td != qd:
            emit_conflict("submission_date_conflict", indexes, {identity},
                          f"tracker dates {sorted(map(str, td))} differ from queue dates {sorted(map(str, qd))}.")
            continue
        if distinct_links or distinct_prs:
            if len(tracker) == 1 and len(queue) == 1:
                _merge_component(records, indexes,
                                 next(iter(distinct_links or distinct_prs)), alias_map,
                                 applications, "unique_exact_company_role", diagnostics)
                consumed.update(indexes)
                continue
            emit_conflict("identity_has_conflicting_strong_ids", indexes,
                          {identity} | distinct_links | distinct_prs,
                          "Exact company/role is not a unique cross-source strong-ID pair.")
            continue
        if len(tracker) == 1 and len(queue) == 1:
            _merge_component(records, indexes, identity, alias_map, applications,
                             "unique_exact_company_role", diagnostics)
            consumed.update(indexes)
        else:
            emit_conflict("ambiguous_exact_identity", indexes, {identity},
                          "Exact company/role fallback is not unique across tracker and queue.")

    # Remaining unique rows are independently identifiable and do not merge
    # merely because employer names resemble each other.
    for index, record in enumerate(records):
        if index in consumed:
            continue
        canonical = application_id(record["row"])
        _merge_component(records, [index], canonical, alias_map, applications,
                         "single_source", diagnostics)
        consumed.add(index)

    alias_owners = defaultdict(set)
    for canonical, application in applications.items():
        for alias in application["aliases"]:
            alias_owners[alias].add(canonical)
    for alias, owners in alias_owners.items():
        if len(owners) > 1:
            alias_map.pop(alias, None)
            conflicts.append({"reason": "alias_maps_multiple_applications",
                              "canonical_ids": sorted(owners), "aliases": [alias],
                              "detail": "This alias is shared by distinct application records."})
            diagnostics["alias_maps_multiple_applications"] += 1

    return {"applications": applications, "alias_to_canonical": alias_map,
            "conflicts": conflicts, "diagnostics": dict(diagnostics)}


def _merge_component(records, indexes, canonical, alias_map, applications, method, diagnostics):
    trackers = [records[i]["row"] for i in indexes if records[i]["source"] == "tracker"]
    queues = [records[i]["row"] for i in indexes if records[i]["source"] == "queue"]
    row = {}
    for value in queues:
        row.update(value)
    for value in trackers:
        row.update(value)
    for value in queues:
        for field in ("source", "variant", "provenance", "resume_version_sha256",
                      "resume_version_hash", "resume_sha256", "resume_hash"):
            if not row.get(field) and value.get(field):
                row[field] = value[field]
    aliases = set().union(*(records[i]["aliases"] for i in indexes))
    aliases.add(canonical)
    for alias in aliases:
        alias_map[alias] = canonical
    applications[canonical] = {
        "application_id": canonical,
        "aliases": sorted(aliases),
        "tracker_rows": trackers,
        "queue_rows": queues,
        "row": row,
        "resolution": {"method": method,
                       "reason": ("same exact posting URL" if method == "exact_posting_link"
                                  else "one tracker row and one queue row share this exact PR"
                                  if method == "unique_pr_cross_source"
                                  else "exact company and role identifies a unique row pair"
                                  if method == "unique_exact_company_role"
                                  else method)},
    }
    diagnostics[method] += 1
