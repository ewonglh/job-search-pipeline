# Contributing

1. Start from an approved issue. Create an issue-linked branch such as
   `fix/12-portable-setup` or `feat/27-import-source`.
2. Keep one issue and one lane of work in each pull request. A change that
   touches discovery and submission cannot be reviewed properly in one pass.
3. Use Conventional Commits, for example
   `fix(setup): use external resume repository paths (#12)`.
4. Link the issue in the pull request and show how each acceptance criterion is
   met. Keep the validation note short and list the exact commands and results.
5. Run the offline suites before opening a pull request:
   `python3 test_queue.py`, `python3 test_tailor.py`,
   `python3 test_stages.py`, and `python3 test_dashboard.py` from `scripts/`.
   Run focused tests for any new behavior as well.
6. Never weaken a test to make a change land. If a test is wrong, fix it in its
   own issue-linked commit and explain why in the message.
7. Never commit real data. `demo/` holds synthetic data only. See
   `SANITIZATION.md`.
8. Keep scripts stdlib-only. A new dependency needs a reason in the pull
   request because it can stop the cron track working.
9. Put owner-specific settings in a `JOBSEARCH_*` environment variable with a
   neutral default. Keep private resume data and runtime state outside the
   checkout.
10. Approval gates are not configurable. Do not add a flag that lets a script
    submit without a merged pull request.
11. Keep documentation claims verifiable. Cite a file and line, or a URL.
12. Use terse documentation bullets. Avoid em dashes and en dashes.
13. If a change makes a lane need an agent where it did not before, say so in
    the pull request because it breaks the cron-only track.
