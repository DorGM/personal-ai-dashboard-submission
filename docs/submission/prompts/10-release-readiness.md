# 10 — Release readiness under time pressure: a FAIL, a fix and an explicit "not run" list

**Phase / task:** Integration of B-07 + B-13/B-15 with B-05; release to `main` · **Date:** 2026-10-18, 06:07–06:55 UTC

## Challenge
The owner needed an integrated, demonstrable MVP within a short, fixed window and asked for a yes or no on release readiness. Under that pressure, the obvious failure mode is to call the build ready and leave out what was never tested.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt` (two messages)

06:07 UTC, the integration directive:

> Verify the integrated path:
> Demo login → synthetic data → metrics/dashboard → Analyst question → read-only tools → evidence validation → response/chart.
> Use Playwright at desktop and mobile viewports and run the appropriate automated regression/security tests.
> […]
> If an essential component is not ready, communicate its exact status and arrange an evidence-based fallback. Never mark an unverified capability as complete.
> Do not sacrifice user isolation, database integrity, secret protection or Evidence Validation to meet the checkpoint.

06:53 UTC, the readiness request:

> I need an immediate release readiness decision, not another development estimate.
>
> Please report whether the integrated B-07 + B-13 + B-15 application has actually passed its essential tests and runs end-to-end.
> […]
> If it is not ready, give me the exact blocking defects and the latest safely runnable commit. Do not represent unfinished functionality as complete.

## AI response
Main Claude kept every merge on a local `integration/analytics` branch and ran an integration Verifier, with Playwright, before anything went to `main`. The Verifier's round-1 verdict, at commit `9350e11`: "FAIL (one blocking defect: mobile layout). Demo isolation, the analytics numbers, security and the gates all PASS." At 390×844, the aggregates comparison table had `scrollWidth` 451 against a 390 px viewport, and the Analyst "Show data" table was clipped.

The Coder wrapped the wide tables in focusable `overflow-x: auto` regions (`b6ecca8`). Main Claude then answered the readiness request: "**Decision: you can record from `32d849a` now.**" It said that the mobile recheck was still running and that `main` would move only once the recheck passed. It also gave a section headed "Not done; don't show these as working in the video:"
- live OpenAI answers ("never run, because they need your approval and key");
- the "steps on aerobic days" question (T5 has no filter);
- the friends half-vs-half comparison, which falls back because one period has zero events;
- the golden test set.

## Human judgment
The owner had set the bar that safety properties were not negotiable under time pressure, and that unverified claims were not allowed. Two of the Verifier's findings were left as open owner and Architect decisions rather than patched under pressure:
- adding a filter to the T5 tool, which would be a data-contract change;
- validator rule D-6 rejecting a comparison where one period has 0 events.

## Iteration and verification
- **Round 1, FAIL:** the full suite passed twice (about 1,740 tests, counted from progress dots because no summary line was printed), ruff and mypy were clean, and a targeted B-02/B-04/B-14 regression run passed 539 tests. The real tools on the demo data gave sleep 7.4615 h vs 6.5714 h, matching the reference. The validator rejected a made-up "7.9 h" and the word "because". Demo isolation passed (item 09).
- **Round 2, PASS, at `b6ecca8`:** `scrollWidth` matched the viewport on all 12 aggregates pages and on the Analyst answer at 390 px, desktop did not regress, and the keyboard could reach the scroll regions.
- **Release:** `32d849a` (`b6ecca8` plus a `.gitattributes` rule that keeps the checksummed fixture bytes on Windows) went to `main` after round 2.
- **Not run, as recorded:** live OpenAI, the golden set, the PowerShell scripts, the tunnel, and a screen-reader walkthrough. The Windows fixes were not re-run on Windows (LOG-023).

## Key takeaway
A readiness decision is only useful if it comes with its limits. Here that meant naming the latest safe commit, the check still in progress, and exactly which features must not be shown as working.

**Sources:** LOG-022, LOG-023; `PROJECT_PLAN.md` B-07, B-13, B-15; commits `9350e11`, `b6ecca8`, `32d849a`
