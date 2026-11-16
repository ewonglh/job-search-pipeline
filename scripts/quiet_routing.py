#!/usr/bin/env python3
"""Pure opt-in notification routing policy; it never sends or acknowledges."""
import copy
import re
from datetime import datetime

import notify_state

SCHEMA_VERSION = 2
MAX_SEEN = 10000
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ACTIONABLE = frozenset(("callback", "owner_decision", "persistent_failure", "recovery"))
_ROUTINES = frozenset(("progress", "digest", "status"))


class PolicyError(ValueError):
    """Policy state or synthetic notice is invalid."""


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "quiet_enabled": False,
        "quiet_revision": None,
        "history_before": None,
        "seen_notice_ids": [],
        "recovered_incidents": [],
        "pending_notices": {},
    }


def _timestamp(value):
    if not isinstance(value, str) or not value:
        raise PolicyError("notice time must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError("notice time must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PolicyError("notice time must include a timezone")
    return parsed


def _valid_state(state):
    required = set(empty_state())
    if (not isinstance(state, dict) or set(state) != required
            or state.get("schema_version") != SCHEMA_VERSION
            or not isinstance(state.get("quiet_enabled"), bool)
            or not isinstance(state.get("seen_notice_ids"), list)
            or len(state["seen_notice_ids"]) > MAX_SEEN
            or not isinstance(state.get("recovered_incidents"), list)
            or len(state["recovered_incidents"]) > MAX_SEEN
            or not isinstance(state.get("pending_notices"), dict)
            or len(state["pending_notices"]) > MAX_SEEN):
        raise PolicyError("invalid quiet-routing state")
    if state["history_before"] is not None:
        _timestamp(state["history_before"])
    if state["quiet_enabled"]:
        if not isinstance(state["quiet_revision"], str) or not _REVISION.fullmatch(state["quiet_revision"]):
            raise PolicyError("quiet-routing revision is invalid")
    elif state["quiet_revision"] is not None:
        raise PolicyError("disabled quiet routing cannot retain an active revision")
    for item in state["seen_notice_ids"] + state["recovered_incidents"]:
        if not isinstance(item, str) or not _ID.fullmatch(item):
            raise PolicyError("quiet-routing identity is invalid")
    if len(set(state["seen_notice_ids"])) != len(state["seen_notice_ids"]):
        raise PolicyError("quiet-routing notice ids are duplicated")
    if len(set(state["recovered_incidents"])) != len(state["recovered_incidents"]):
        raise PolicyError("quiet-routing recovery ids are duplicated")
    pending_incidents = set()
    for key, item in state["pending_notices"].items():
        if (not isinstance(key, str) or not _ID.fullmatch(key)
                or key in state["seen_notice_ids"] or not isinstance(item, dict)
                or set(item) != {"category", "incident_id", "created_at", "route"}
                or item["category"] not in _ACTIONABLE | _ROUTINES
                or item["route"] not in ("telegram", "dashboard")):
            raise PolicyError("invalid pending quiet-routing notice")
        _timestamp(item["created_at"])
        incident = item["incident_id"]
        if item["category"] == "recovery":
            if (not isinstance(incident, str) or not _ID.fullmatch(incident)
                    or incident in pending_incidents or incident in state["recovered_incidents"]):
                raise PolicyError("invalid pending recovery")
            pending_incidents.add(incident)
        elif incident is not None:
            raise PolicyError("unexpected pending incident")


def reconcile_deliveries(state, delivery_state):
    """Finalize only validated #9 receipts bound to the same notice decision ID.

    The future transport must stage #9 with decision_id equal to notice_id.
    This function reads receipt state; it never acknowledges or sends a batch.
    """
    _valid_state(state)
    try:
        notify_state._validate(delivery_state)
    except (notify_state.StateError, TypeError, KeyError) as exc:
        raise PolicyError("invalid notification receipt state") from exc
    updated = copy.deepcopy(state)
    for notice_id, notice in list(updated["pending_notices"].items()):
        decision = delivery_state["decisions"].get(notice_id)
        if not decision or decision["resolution"] != "delivered":
            continue
        receipt = delivery_state["acknowledgments"].get(decision["batch_id"])
        if (not receipt or receipt["message_sha256"] != decision["message_sha256"]
                or notice["route"] != "telegram"):
            continue
        if len(updated["seen_notice_ids"]) >= MAX_SEEN:
            raise PolicyError("quiet-routing dedup history reached its bound")
        updated["seen_notice_ids"].append(notice_id)
        if notice["category"] == "recovery":
            if len(updated["recovered_incidents"]) >= MAX_SEEN:
                raise PolicyError("quiet-routing dedup history reached its bound")
            updated["recovered_incidents"].append(notice["incident_id"])
        del updated["pending_notices"][notice_id]
    return updated


def activation_ready(*, revision, dashboard_status, dashboard_working,
                     parity_revision, acceptance):
    """Require a fresh dashboard, same-revision command parity, and both human checks."""
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        return False
    if (dashboard_status != "fresh" or dashboard_working is not True
            or parity_revision != revision):
        return False
    if not isinstance(acceptance, dict):
        return False
    for surface in ("phone", "desktop"):
        entry = acceptance.get(surface)
        if (not isinstance(entry, dict) or entry.get("accepted") is not True
                or entry.get("revision") != revision):
            return False
        try:
            _timestamp(entry.get("recorded_at"))
        except PolicyError:
            return False
    return True


def set_quiet_mode(state, enabled, *, cursor, revision=None,
                   dashboard_status=None, dashboard_working=False,
                   parity_revision=None, acceptance=None):
    """Return a new state and result; toggles establish a no-replay baseline."""
    _valid_state(state)
    if not isinstance(enabled, bool):
        raise PolicyError("quiet-routing mode must be boolean")
    next_state = copy.deepcopy(state)
    _timestamp(cursor)
    if enabled and not activation_ready(
            revision=revision, dashboard_status=dashboard_status,
            dashboard_working=dashboard_working,
            parity_revision=parity_revision, acceptance=acceptance):
        return next_state, {"changed": False, "enabled": state["quiet_enabled"],
                            "reason": "fresh_dashboard_parity_and_recorded_phone_desktop_acceptance_required"}
    if state["quiet_enabled"] and enabled and state["quiet_revision"] != revision:
        next_state["quiet_revision"] = revision
        next_state["history_before"] = cursor
        return next_state, {"changed": True, "enabled": True,
                            "reason": "revision_updated_with_fresh_acceptance"}
    if state["quiet_enabled"] == enabled:
        return next_state, {"changed": False, "enabled": enabled, "reason": "already_set"}
    next_state["quiet_enabled"] = enabled
    next_state["quiet_revision"] = revision if enabled else None
    next_state["history_before"] = cursor
    return next_state, {"changed": True, "enabled": enabled, "reason": "mode_changed"}


def plan_notices(state, notices, *, dashboard_status=None, dashboard_working=False,
                 parity_revision=None):
    """Plan routes and dedup identities only; delivery remains pending until #9 receipt."""
    _valid_state(state)
    if not isinstance(notices, list):
        raise PolicyError("notices must be a list")
    updated = copy.deepcopy(state)
    seen = set(updated["seen_notice_ids"])
    recovered = set(updated["recovered_incidents"])
    pending = updated["pending_notices"]
    quiet_available = (updated["quiet_enabled"] and dashboard_status == "fresh"
                       and dashboard_working is True
                       and parity_revision == updated["quiet_revision"])
    results = []
    for notice in notices:
        if not isinstance(notice, dict):
            raise PolicyError("notice must be an object")
        notice_id = notice.get("notice_id")
        category = notice.get("category")
        if not isinstance(notice_id, str) or not _ID.fullmatch(notice_id):
            raise PolicyError("notice requires a stable notice_id")
        if category not in _ACTIONABLE and category not in _ROUTINES:
            raise PolicyError("notice category is unsupported")
        created_at = notice.get("created_at")
        created = _timestamp(created_at)
        incident_id = notice.get("incident_id") if category == "recovery" else None
        if category == "recovery" and (not isinstance(incident_id, str)
                                       or not _ID.fullmatch(incident_id)):
            raise PolicyError("recovery notice requires a stable incident_id")
        prior_id = notice_id if notice_id in pending else None
        if category == "recovery" and prior_id is None:
            prior_id = next((key for key, item in pending.items()
                             if item["incident_id"] == incident_id), None)
        if prior_id is not None:
            prior = pending[prior_id]
            if (prior["category"] != category or prior["incident_id"] != incident_id
                    or (prior_id == notice_id and prior["created_at"] != created_at)):
                raise PolicyError("pending notice identity changed")
            if prior["route"] == "dashboard" and not quiet_available:
                prior["route"] = "telegram"
            # Pending work predates toggle baselines safely. Reuse its stable
            # #9 decision/batch; a retry must never create another payload.
            results.append({"notice_id": prior_id, "route": prior["route"],
                            "reason": "pending_delivery", "delivery_status": "pending"})
            continue
        if updated["history_before"] is not None and created <= _timestamp(updated["history_before"]):
            results.append({"notice_id": notice_id, "route": "skipped",
                            "reason": "historical_activation_baseline",
                            "delivery_status": "not_attempted"})
            continue
        if notice_id in seen:
            results.append({"notice_id": notice_id, "route": "skipped",
                            "reason": "duplicate_notice", "delivery_status": "not_attempted"})
            continue
        if category == "recovery":
            if incident_id in recovered:
                results.append({"notice_id": notice_id, "route": "skipped",
                                "reason": "recovery_already_emitted",
                                "delivery_status": "not_attempted"})
                continue
        route = ("telegram" if category in _ACTIONABLE or not quiet_available
                 else "dashboard")
        reason = ("actionable" if category in _ACTIONABLE else
                  "quiet_dashboard" if route == "dashboard" else
                  "quiet_gate_failed" if updated["quiet_enabled"] else "default_telegram")
        results.append({"notice_id": notice_id, "route": route,
                        "reason": reason,
                        "delivery_status": "not_attempted"})
        pending[notice_id] = {"category": category, "incident_id": incident_id,
                              "created_at": created_at, "route": route}
        if len(pending) > MAX_SEEN:
            raise PolicyError("quiet-routing dedup history reached its bound")
    return updated, results
