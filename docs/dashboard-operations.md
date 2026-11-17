# Private dashboard rollout and acceptance

This is an owner-run deployment gate for issue 35. No deployment or live-data
check was performed as part of the code change.

## Deploy and rollback

- Keep the checkout, projection database, logs, and backups on the owner's
  private host. Keep the cache outside the checkout and readable only by the
  owner account (JOBSEARCH_PROJECTION_DB; [private cache default](../scripts/projection.py#L777)).
- Run the existing explicit offline suites and focused dashboard view/server
  suites before rollout ([contributing checks](../CONTRIBUTING.md#L5)).
- Start the reader from scripts with this command:
    python3 dashboard_server.py --db "$JOBSEARCH_PROJECTION_DB"
  It binds only
  to loopback and serves cached reads ([server bind guard](../scripts/dashboard_server.py#L88),
  [snapshot reader](../scripts/dashboard_server.py#L192)).
- Keep the existing private access layer and its authentication in front of
  loopback. Do not expose the listener publicly ([routes](../scripts/dashboard_server.py#L17),
  [response headers](../scripts/dashboard_server.py#L34)).
- For rollback, stop the dashboard process, restore the last known-good
  application revision using the owner's private deployment procedure, and
  restart against the preserved private cache. No projection schema or source
  collection behavior changes in this issue ([cached reader](../scripts/projection.py#L1283)).

## Owner acceptance gate

- On desktop (1280 × 800), open Runs and confirm failures and incomplete runs
  sort above successful runs; open one detail and check that supported tool
  name, timestamps, duration, result, and retry context render. Confirm joins
  stay unknown when absent and no raw arguments or output appear
  ([Runs rendering](../scripts/dashboard.py#L1325)).
- On phone (390 × 844), navigate Home, Runs, Tailoring, and System health;
  confirm no horizontal page overflow, readable status text, and usable
  run-detail/back links ([responsive shell](../scripts/dashboard.py#L695),
  [navigation](../scripts/dashboard_shell.py#L5)).
- Use an empty trace cache and an unavailable/stale/partial source snapshot.
  Confirm empty states, plain-language impact and next step, with error codes
  and timestamps inside expandable provenance ([source health](../scripts/dashboard.py#L1062)).
- On Tailoring, confirm model and owner review counts remain separate, recorded
  rework codes are visible, potential mistailor cues say “heuristic,” and
  missing owner evidence remains unknown ([review view](../scripts/dashboard.py#L1438)).
- Recheck Applications search submits as a self-targeted GET and that all pages
  remain read-only ([search form](../scripts/dashboard.py#L1197),
  [GET-only handler](../scripts/dashboard_server.py#L174)).

- Limit rollout to the dashboard reader and preserved cache. Follow the
  [owner acceptance checks](#owner-acceptance-gate) before accepting the UI.
