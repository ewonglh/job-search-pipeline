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

The runner and ownership verifier check `openclaw --version` before acting.
Their shared gate accepts only the exact outputs `OpenClaw 2026.10.1 (e178c39)`
and `OpenClaw 2026.11.8 (2d4ab29)`, with at most one terminal LF or CRLF.
Surrounding whitespace and additional lines are rejected. The newer build has
source-reviewed interface compatibility and passed the controlled acceptance
check recorded below. A deployment must still verify its exact build, code and
browser integration before enabling the application drain. The runner launches
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

The OpenClaw `2026.11.8` (`2d4ab29`) public-source review confirms the CLI and
abort interfaces, agent binding validation, terminal persistence wait, and
signal propagation into Browser Gateway actions. References in that release:

- `dist/sessions-abort-DE6sPSp3.mjs:66-119,323-335,361-369` validates the
  agent binding and waits for terminal lifecycle persistence.
- `dist/chat-abort-lifecycle-internal-ChM03p20.mjs:55-64` handles abort
  lifecycle signaling.
- `dist/browser-request-DayigK53.mjs:1949-1965,2298-2309`,
  `dist/dispatcher-CpOB7hDZ.mjs:26-96`, and
  `dist/routes-BC1rdkyd.mjs:277-302,1229-1317,1420-1437` carry the signal
  through Browser Gateway request dispatch and actions.

This review establishes interface compatibility and signal propagation. It
does not prove cancellation of every in-flight browser side effect. The
separate controlled proof must still observe a pending action, an active-run
abort, terminal read-back, and no later portal action before drain activation.

### Controlled acceptance on 26 November 2026

- Build: `OpenClaw 2026.11.8 (2d4ab29)`, built-in Browser Gateway integration.
  The verifier implementation was the reviewed PR #77 candidate at `c23ec32`.
- A fresh one-use session passed a no-tools readiness turn. Full tool events
  from the connection owning the accepted run established the exact session,
  run, call, target and button arguments while the synthetic click was pending.
- The candidate verifier's first `sessions.abort` returned `aborted`; its
  second returned `no-active-run`. The verdict matched the owner and fresh
  challenge. Terminal-only cleanup was not used as proof of an active abort.
- The test button became actionable after abort. The portal recorded zero
  accepted or rejected actions during 20 seconds of observation (81 reads).
  The synthetic session was terminal and its owned tab was then closed.
- The temporary localhost policy was removed. The original settings matched
  and actual Browser Gateway localhost navigation was blocked again. No
  Gateway restart or scheduler change was part of the test.

Original traces and owner identifiers remain in private deployment evidence.
The acceptance applies to this build and built-in browser path; it does not
extend to detached workers or plugins. Repeat the controlled check after a
runtime or browser-integration change. Green offline tests alone cannot
replace it.

For live observation, human-readable session tails are diagnostic. Current
timestamps include `Z`, deferred browser calls can have a `dispatch_action`
wrapper, and thinking/text updates can be coalesced. Bind full tool events to
the accepted run, match call IDs and exact arguments, and fail on transport
gaps or unexpected tools. Display-only `item` events omit the action arguments
and cannot establish a pending click on the intended target.

For `2026.11.8`, the installed public `docs/hot-reload.md:260,400-404`
documents browser policy hot reload by replacing Browser control, cancelling
pending operations, and closing owned Chrome. Do not apply the older
`2026.10.1` Gateway-restart assumption to that newer build. A temporary policy
change still needs runtime proof of its positive effect and its rejection after
restoration; config-file read-back alone is insufficient.

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

From `scripts/`, run `python3 test_openclaw_version_contract.py`,
`python3 test_openclaw_drain.py`, and `python3 test_openclaw_ownership.py`.
The fake portal integration keeps an action outstanding, applies the session
abort, waits for the action to be rejected, and checks that the old session
cannot submit after owner release.
