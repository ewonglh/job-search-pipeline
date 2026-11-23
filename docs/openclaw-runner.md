# OpenClaw queue runner

`scripts/openclaw_drain.py run` claims work through `queue.py claim --next`,
starts one OpenClaw Gateway turn on a new session key, then asks
`finish-attempt` to release the owner through its claim-pinned verifier. The
runner never selects rows or interprets agent prose as a submission outcome.

## Host setup

Use the same private queue, repository, tracker, profile, skip, and cap settings
as the existing queue lane. Keep queue state and submission snapshots outside
the checkout. Configure the verifier before the first claim so #53 can pin its
absolute command and executable digest in the durable owner record:

```sh
export JOBSEARCH_OPENCLAW_BIN=/absolute/path/to/openclaw
export JOBSEARCH_BROWSER_OWNERSHIP_VERIFIER="/absolute/path/to/scripts/openclaw_ownership.py --openclaw-bin /absolute/path/to/openclaw"
export JOBSEARCH_OPENCLAW_AGENT=main
# Optional: preserve a scheduled agent's model override for the claimed turn.
export JOBSEARCH_OPENCLAW_MODEL=provider/model
python3 scripts/openclaw_drain.py run --max-attempts 1
```

The verifier command starts with the executable helper itself so the claim pin
can bind its file digest. Keep the `--openclaw-bin` argument absolute and fixed
in that pinned command.

`JOBSEARCH_RUNNER_ID` can pin a stable runner name. Otherwise each invocation
creates one. `JOBSEARCH_DRAIN_MAX_ATTEMPTS` defaults to `1`; each claimed turn
uses a fresh `agent:<agent-id>:jobs-runner-...` session key, which must never be
reused. Increase the limit only when the schedule is intended to process more
than one authorized queue row per invocation.

The runner checks `openclaw --version` before claiming; it currently accepts
only `OpenClaw 2026.10.1 (e178c39)`. It launches
`openclaw agent --agent ... --session-key ... --message-file ... --json`. The
prompt carries the exact PR, attempt, runner, and session IDs
and directs the agent to follow the absolute `skills/job-queue-drain/SKILL.md`
path next to the queue script. The agent
still needs the owner-approved merge, current eligibility and owner controls,
verified answers, and the prepared and verified snapshot before Submit.

## Browser ownership verifier

The host verifier calls OpenClaw's `sessions.abort` RPC for the exact one-use
session key, clears queued follow-ups, and requires a second call to report
`no-active-run`. The Gateway's abort handler waits for terminal run lifecycle
persist before replying. Browser Gateway requests receive the turn invocation's
abort signal and reject work after the requester is no longer current. These
are the observed controls in OpenClaw 2026.10.1 (`e178c39`):

- The installed CLI documents explicit `--session-key` selection in
  `/home/cittagazze/.npm-global/lib/node_modules/openclaw/docs/cli/agent.md:18,351-359`.
- `gateway call` forwards the requested method and params, and the CLI assigns
  session-write scope to `sessions.abort`, in
  `/home/cittagazze/.npm-global/lib/node_modules/openclaw/dist/gateway-cli-CrxL6kiG.mjs:380-388`
  and `/home/cittagazze/.npm-global/lib/node_modules/openclaw/dist/call-5IX6yT5Q.mjs:735-740`.
- `sessions.abort` persists the cancelled event and waits for terminal
  persistence before replying in
  `/home/cittagazze/.npm-global/lib/node_modules/openclaw/dist/sessions-abort-pB4nadyQ.mjs:224-253,292-298,363-373`.
- Browser requests bind to the turn abort signal and check current requester
  authority in
  `/home/cittagazze/.npm-global/lib/node_modules/openclaw/dist/browser-request-DqgU87BY.mjs:2223-2227,2257-2262,2323-2325`.

The unique session key is the fence scope. Do not route an attempt through a
detached browser worker or plugin that outlives the Gateway invocation signal.
If a browser integration does not cancel its outstanding request when that
signal aborts, the verifier cannot prove the boundary is clear and must not be
used for that integration. This adapter does not claim protection against
arbitrary host filesystem writers.

If the runner exits before finalization, the next invocation reads
`browser-owner` and calls `finish-attempt` for that exact owner before claiming
new work. If `sessions.abort` or its terminal read-back fails, the queue retains
the owner. An `outcome_unknown` row remains protected for reconciliation, while
a separately selected queued row can proceed after the owner is read back free.

## Installed schedule read-back

After an owner installs or changes a scheduled job, compare the effective
configuration against its intended field digests without printing the payload:

```sh
openclaw cron list --all --json --timeout 10000 |
  python3 scripts/openclaw_drain.py verify-readback \
    --job-id JOB_ID --expected expected-digests.json
```

The expected JSON maps `schedule`, `payload`, and `enabled` to SHA-256 digests
from `expected_digests()`; `agentId`, `sessionTarget`, and `delivery` are also
checked when present. Incomplete or ambiguous listings fail closed.

## Offline verification

From `scripts/`, run `python3 test_openclaw_drain.py` and
`python3 test_openclaw_ownership.py`. The fake portal integration keeps an
action outstanding, applies the session abort, waits for the action to be
rejected, and checks that the old session cannot submit after owner release.
