# Contributing

Ten lines, because this repo is small and the rules are short.

1. One lane per pull request. A change that touches discovery and submission
   cannot be reviewed properly in one pass.
2. Run the offline tests before opening the PR: `python3 test_queue.py`,
   `python3 test_tailor.py`, `python3 test_stages.py`,
   `python3 test_dashboard.py`. All four must pass.
3. Never weaken a test to make a change land. If a test is wrong, fix the test in
   its own commit and say why in the message.
4. Never commit real data. `demo/` holds the only data that ships, and it is
   synthetic. See `SANITIZATION.md`.
5. Keep the scripts stdlib-only. A new dependency needs a reason in the PR body,
   because a dependency is what stops the cron track working.
6. Any new owner-specific default goes in an environment variable with a neutral
   placeholder, matching the `JOBSEARCH_*` pattern.
7. The approval gates are not configurable. Do not add a flag that lets a script
   submit without a merged PR. That is the whole design.
8. Keep documentation claims verifiable. Cite the file and line, or the URL.
9. Terse bullets in docs. No em dashes or en dashes. Use a comma, full stop,
   colon or parentheses.
10. If a change makes a lane need an agent where it did not before, say so in the
    PR body, because it breaks the cron-only track.
