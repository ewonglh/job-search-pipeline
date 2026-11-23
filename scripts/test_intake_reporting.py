#!/usr/bin/env python3
"""Synthetic offline checks for sanitized intake run history."""
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

import intake_reporting


with tempfile.TemporaryDirectory(prefix="synthetic-intake-history-") as temp:
    with mock.patch.dict(os.environ, {"JOBSEARCH_INTAKE_DIR": temp}):
        identity = intake_reporting.source_id("company", "https://private.invalid/job/1")
        summary = intake_reporting.sanitize_summary({
            "outcome": "partial_failure",
            "counts": {"processed": 4, "accepted": 2, "raw_text": 99},
            "reasons": {"source_unavailable": 1, "private reason": 4},
            "sources": [{"kind": "company", "source_id": identity,
                         "fetch": "ok", "parse": "ok", "processed": 4,
                         "accepted": 2, "url": "https://private.invalid/job/1"},
                        {"kind": "company", "source_id": "company:raw-private-source",
                         "fetch": "ok", "parse": "ok"}],
            "private_field": "PRIVATE-CANARY",
        })
        assert summary == {
            "outcome": "partial_failure",
            "counts": {"processed": 4, "accepted": 2},
            "reasons": {"source_unavailable": 1},
            "sources": [{"kind": "company", "source_id": identity,
                         "fetch": "ok", "parse": "ok", "processed": 4,
                         "accepted": 2}],
        }, summary

        for index in range(42):
            intake_reporting.record_run("discovery", {
                "outcome": "healthy_empty", "counts": {"processed": index},
                "reasons": {"no_postings": 1}, "sources": [],
            })
        intake_reporting.record_run("enqueue", {
            "outcome": "healthy_empty",
            "counts": {"observed": 12, "admitted": 0, "excluded": 3,
                       "pending_metadata": 1, "closed_history": 3, "active_work": 2},
            "reasons": {"no_eligible_prs": 1},
            "notes": "PRIVATE-CANARY",
        })

        path = Path(temp) / "discovery.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == 1
        assert len(document["runs"]) == intake_reporting.HISTORY_LIMIT
        assert document["runs"][-1]["counts"] == {"processed": 41}
        assert "PRIVATE-CANARY" not in path.read_text(encoding="utf-8")
        assert len(intake_reporting.read_history("discovery")) == 40
        enqueue = intake_reporting.read_history("enqueue")
        assert enqueue[0]["counts"] == {"observed": 12, "admitted": 0, "excluded": 3,
                                         "pending_metadata": 1,
                                         "closed_history": 3, "active_work": 2}
        assert "sources" not in enqueue[0]

print("test_intake_reporting: sanitized schema, source IDs and bounded history passed")
