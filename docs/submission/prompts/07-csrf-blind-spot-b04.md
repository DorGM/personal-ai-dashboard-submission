# 07 — 607 passing tests and every browser form broken: the B-04 CSRF blind spot

**Phase / task:** Phase B / B-04 data operations (manual create, edit, soft delete, restore, history) · **Date:** 2026-10-18, 03:05–04:33 UTC

## Challenge
The Coder reported B-04 as complete, with 607 tests passing, ruff and mypy clean, and a curl walkthrough on a real server. The question was whether a green suite meant a working product.

## Original prompt
Label: `Documented decision without an available original prompt`

No owner message targeted this defect. The closest recorded instruction is a general requirement from the owner's 2026-10-18 03:03 UTC message:

> The Independent Verifier must evaluate complete user journeys, not just individual functions.

Main Claude carried this into the Verifier brief as "Verify complete user journeys, not just functions." The brief also asked for Playwright at 1440×900 and 390×844, with the instruction: "Never claim a browser check you didn't execute."

## AI response
The Verifier ran the suite three times (607 passed each time) and then probed the running app as a browser would, by posting only the fields that each rendered form contains. Its round-1 report:

> ## B-04 verdict: **FAIL**
>
> There is one blocking defect: in a real browser you can't add, edit, or mark a task done/pending.

The cause: `{% from "macros.html" import record_form, record_item %}` does not pass the page context in Jinja, so `csrf_token` rendered empty inside the macro forms, and the CSRF middleware returned 403. Why the tests missed it, in the report's words: "the test helpers take the token from the `<meta name="csrf-token">` tag (`csrf_of`), never from the form's own hidden field."

The Verifier also reported the browser checks as "**NOT RUN**", because the Playwright MCP could not find Chrome. It did not substitute another claim.

## Human judgment
Main Claude treated the miss as a flaw in how the tests were designed, not only as a template bug, and accepted the Verifier's suggested fix: a regression test that submits each rendered form's own fields. To unblock browser testing, Main Claude added a wrapper script for the preinstalled Chromium. The change was to the environment only, not the repository. The Coder's assumption about task dates (past dates allowed, up to today+14) was accepted by the orchestrator and recorded.

## Iteration and verification
- **Fix:** the macros are now imported `with context`.
- **New test:** `tests/integration/test_records_forms.py` parses every rendered POST form and submits exactly its own fields. The Coder's report: "With the fix temporarily reverted, all 13 tests fail; with it, all pass."
- **Also fixed:** a one-day history window, text made only of invisible characters, and 78 `ResourceWarning`s in the tests. The suite went to 690 tests.
- **Round 2 PASS.**
  - HTTP probes: two-user isolation across all 11 domains, the full delete, restore and purge cycle with forged confirmations refused, and concurrent same-key creates (1 × 303 and 11 × 409).
  - Playwright desktop and mobile journeys: 23 screenshots of synthetic data, and `scrollWidth` 390 on mobile pages.
- **A flaky older test.** The Verifier also found a pre-existing migration-concurrency test that failed about 1 run in 15–40. It assumed one process applies every migration. Main Claude fixed the assertion, and the test then passed 15/15.

## Key takeaway
Tests that use the same shortcut as the implementation, here the meta-tag token, can stay green while the user path is broken. Black-box probing of rendered output found what the white-box suite could not.

**Sources:** LOG-018; `PROJECT_PLAN.md` B-04; `tests/integration/test_records_forms.py`; `app/web/templates/tab.html`, `form.html`; commit `dc0cfa8`
