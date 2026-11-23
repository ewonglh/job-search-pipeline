# Private intake run history

`intake_reporting.py` records bounded, sanitized summaries for the `discovery`
and `enqueue` lanes. The default directory is
`~/.local/state/job-search-pipeline/intake`; set `JOBSEARCH_INTAKE_DIR` to
override it. Keep the files outside the repository.

Each lane has one `<lane>.json` file with this schema:

```json
{
  "schema_version": 1,
  "runs": [
    {
      "recorded_at": "2026-11-23T00:00:00Z",
      "outcome": "healthy_empty",
      "counts": {"processed": 0, "accepted": 0, "added": 0},
      "reasons": {"no_postings": 1},
      "sources": []
    }
  ]
}
```

`sources` is optional. Discovery entries use a hashed `source_id` and the
allowlisted fields `kind`, `fetch`, `parse`, `processed`, and `accepted`.
Enqueue entries can omit source coverage and use the allowlisted counts for
observed PRs, admissions, exclusions, pending metadata, active work and prior
closures, plus the reason codes in `intake_reporting.py`. A zero-admission run
without a reason is shown as `unknown` by the report. Free text, URLs, paths, and unknown keys
are discarded at write time. The writer retains the latest 40 runs per lane.

The projection reads the same schema as a private source. Missing lane history
is reported as unknown or partial coverage; it does not become a zero count.
Malformed history is isolated to its lane, and the other lane remains usable.
