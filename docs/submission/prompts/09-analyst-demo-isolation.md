# 09 — Demo-mode isolation defect in the Analyst: fail closed, prove it across a reset

**Phase / task:** B-15 integration (analytics branch into `main`) · **Date:** 2026-10-18, 06:18–06:55 UTC

## Challenge
Demo Mode (B-14) promises that a reviewer's session never opens or changes a personal database. The new Analyst route came from a separate workstream and broke that promise: in demo mode it opened `DATABASE_PATH`. The defect had to be fixed and proven before the branch reached `main`.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt`

> The Analytics worker discovered a B-15 database isolation defect.
> Issue: The new `POST /analyst/ask` route was opening the personal database (`DATABASE_PATH`) instead of the active synthetic Demo Mode working copy when `APP_MODE=demo`.
> […]
> * The Analyst must obtain its database connection through the application's mode-aware database context.
> […]
> * Add regression tests proving that Analyst queries in Demo Mode never open or modify the personal database, including after a demo reset.
> […]
> Do not merge B-15 until the isolation fix passes verification.

**Note on provenance:** the analytics worker ran in a separate Claude Code session outside this project. Its prompts and conversation are not available here. What is available is the owner's relayed message above and the worker's committed `docs/analytics/worker-notes.md` and `HANDOFF.md`.

## AI response
Main Claude replied: "B-15 won't go to `main` until the isolation fix passes. I'm checking that fix on the integration branch first." In the integration commit, it kept the worker's fix (the route uses `app.state.demo.connect()`, hardened read-only) and added a fail-closed guard in `app/web/analyst.py`:

```python
if demo is None and getattr(settings, "is_demo", False):
    raise RuntimeError("demo mode without a demo runtime")  # fail closed: never fall back to DATABASE_PATH
```

It also added `tests/integration/test_analyst_demo_isolation.py`. A spy records every path passed to `sqlite3.connect`, and a synthetic personal database sits at `DATABASE_PATH`. The tests are:
- `test_demo_analyst_never_opens_the_personal_db_also_after_reset`;
- `test_demo_analyst_connection_is_read_only`;
- `test_demo_mode_without_runtime_fails_closed`.

## Human judgment
The owner held the merge gate and named the required evidence: regression tests, including a reset. The record keeps the verification status of each layer apart. The worker's notes record its fix as "Tested by the Coder and the orchestrator's gate run; **not re-verified by the Verifier**". The owner later reported that both analytics tasks had passed their independent Verifier, and said that "real B-05 integration, golden tests and Playwright checks are still required." The integration lead then had the integration Verifier probe isolation independently instead of relying on either report.

## Iteration and verification
The integration Verifier ran this check on a real uvicorn server in demo mode, with a connect spy and a scripted model (LOG-022, `PROJECT_PLAN.md` B-15):
- **Flow:** demo login → ask → reset → the old session gets a 303 → log in again → ask.
- **Paths opened:** only `working-<gen1>.db`, then `working-<gen2>.db`. `DATABASE_PATH` was never opened.
- **Personal database:** its sha256 and mtime were unchanged, checked again after both servers and the browser runs had finished.
- **Other checks:** CSRF on `/analyst/ask` returned 403 for a missing or bogus token. In personal mode only `DATABASE_PATH` was opened, and a decoy user's data never appeared.
- **Verdict:** demo isolation PASS. The overall round 1 still failed, on mobile layout (item 10).

## Key takeaway
When one mode must never touch another mode's data, the safe default is to refuse to run, not to fall back. The regression test proves the guarantee across a reset, and that is where a naive cached path would have failed.

**Sources:** LOG-022; `PROJECT_PLAN.md` B-14, B-15; `app/web/analyst.py`; `tests/integration/test_analyst_demo_isolation.py`; `docs/analytics/worker-notes.md` §8; commits `41f5388`, `9350e11`
