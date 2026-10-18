# 01 — Phase A kickoff and the first scope decision (A-01)

**Phase / task:** Phase A (design only) / A-01 MVP scope · **Date:** 2026-10-16, 10:47–10:56 UTC

## Challenge
Start an AI-assisted project without letting the assistant jump straight to code. The first design decision (MVP scope) had to be proposed with alternatives, and nothing could proceed without an explicit human approval.

## Original prompt
Label: `Verbatim recorded prompt`

> Read CLAUDE.md, PRODUCT_SPEC.md and PROJECT_PLAN.md. Start Phase A with the Architect agent only. Propose the first architecture decision with alternatives and tradeoffs. Wait for my explicit approval before proceeding. Do not write application code.

## AI response
The Architect produced three scope options and recommended one. As sent to the owner:

- "**A. Full spec, all equal priority.** This carries the highest risk, because the live jobs feed may turn out not to be viable late in the work and there's no fallback."
- "**B. Full core with bounded agents (recommended).** It covers every feature in the spec. Jobs and trends each use one approved source, and a manual "Run now" button means the demo doesn't depend on the 09:00 run."
- "**C. Core tracking first, jobs and trends only if time allows.** This is the safest option, but it drops two features the spec requires."

It also made the approval boundary explicit: "The "Run now" button is a small addition to the spec, so approving B approves the button too." It ended by asking for a reply rather than continuing to A-02.

## Human judgment
The owner approved B but changed its behaviour:

> approve B. i want it to be in a way that the Jobs and trends are recommended each time the user clicked the button "Run now". its possible to do it more than once a day, but it can be up to 3 times a day.

Main Claude then noticed that the amendment interacted with the spec's daily 09:00 run and asked a follow-up decision ("Keep the automatic 09:00 jobs and trends run, now that "Run now" exists?"). Its recommended option was "Keep, separate" (09:00 plus three manual runs). The owner rejected the recommendation and chose "Drop 09:00": recommendations only on "Run now", at most three per day, and no scheduler.

## Iteration and verification
- The amendment was recorded verbatim in `PROJECT_PLAN.md` and `DEVELOPMENT_LOG.md` (LOG-002) before any further design work.
- Main Claude flagged a knock-on effect for the next step: the spec allowed one job report per day, so per-run reports had to be handled in the schema decision (A-03).
- Dropping the 09:00 run removed the scheduler library from the A-02 stack proposal before that proposal was approved (LOG-003).
- This was a design decision, so there was no test to run. The evidence is the approval record.
- Later context: the jobs and trends features were deferred out of the MVP on 2026-10-17 (item 05). The approval history was kept, not erased.

## Key takeaway
Having the assistant stop after one bounded decision gave the owner room to amend it. Asking the follow-up question then surfaced a requirement conflict (the scheduled run against the manual cap) that would otherwise have been settled silently by default.

**Sources:** LOG-002, LOG-003; `PROJECT_PLAN.md` A-01, A-02; `docs/phase-a/A-01-mvp-scope-proposal.md`; `docs/phase-a/A-02-stack-modules-api-proposal.md`
