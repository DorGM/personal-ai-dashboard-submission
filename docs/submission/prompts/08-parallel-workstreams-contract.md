# 08 — Parallel workstreams: a frozen metrics contract and a single integrator

**Phase / task:** Planning amendment during B-14; B-05 contract; B-13/B-15 analytics workstream · **Date:** 2026-10-18 04:47 UTC; protocol pushed 04:53 UTC

## Challenge
The owner wanted a second, independent Claude Code thread to build the analytics agent (B-13/B-15) while the main thread finished Demo Mode, the metrics registry (B-05) and the dashboard. Two AI workers editing one repository can easily overwrite each other's work or merge unverified code.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt` (the full text is in `docs/architecture/owner-directive-2026-10-18-priorities.md`)

> I intend to start a second independent Claude Code thread for B-13/B-15.
> You are the integration lead and the only worker authorized to integrate verified changes into main.
> The analytics worker must use an isolated worktree or checkout and a separate feature branch.
> Coordinate the B-05 metric registry contract with the analytics worker. Avoid overlapping edits. Do not let both workers modify shared application files or planning documents simultaneously.
> The analytics worker can independently build schemas, the evidence ledger, validator, and tests while B-05 is being developed. Final tool integration must wait for the shared metrics contract.
> Preserve Independent Verifier review for each task and a full integration verification after merging. Only verified integrated work may be pushed to main.

The same message deferred AI free-text intake (B-06) out of the MVP and replaced the 90-day fixture spec with about 30 fictional days.

## AI response
Main Claude first redirected the B-14 Coder that was already running to the new 30-day spec, keeping its existing work. It then wrote and pushed `docs/architecture/parallel-work-and-metrics-contract.md` (commit `bb465b1`). The protocol has three parts:
- **Roles:** the integration lead is "The only worker that commits or pushes to `main`, and the only one that edits the planning documents."
- **File ownership:** a table that assigns each path to one workstream, and a rule for the files in between. `app/main.py`, `app/web/deps.py` and the config belong to the main workstream; "B-15 may add **one** router-include line and the AI settings as a patch the integration lead applies".
- **Contract and merge protocol:** a frozen §4 interface for `app/analytics/metrics.py`, and the rule "No work goes to `main` without a passing integration verification."

The message to the owner summed it up: "The analytics thread never pushes to `main`, and only I edit the planning docs."

## Human judgment
The owner set the boundaries: one integrator, isolated branches, a verifier per task plus an integration verifier. The owner also corrected the plan with facts from outside this session. The analytics branch was actually `feature/analytics-agent`, not the documented `feature/analytics-b13`. The owner warned: "Do not treat B-13 as fully integrated or production-ready merely because its branch was pushed."

## Iteration and verification
- **Building against the contract:** the analytics worker built tools T1–T5 against the frozen contract through a port and tested them with an in-memory fake (`docs/analytics/worker-notes.md`).
- **B-05 Verifier PASS** (round 1): 1016 tests passed; 119,256 randomized checks and 53,704 demo-fixture checks against an independent calculator found 0 mismatches; all 5 injected mutations were caught.
- **Contract changes:** four small deviations from the contract, such as no `industry` filter and future tasks excluded from the completion rate, were accepted by the integration lead and sent to the analytics worker.
- **Merge:** the analytics branch merged cleanly into a local integration branch, and the handoff patches to integration-owned files were applied there.
- **Integration fix:** FastAPI's wrapped routers made swapping routes impossible, so the router is now injected through `create_app(analyst_router=)` (LOG-022).
- **Not available here:** the analytics worker's own prompts. Only its committed notes and handoff are in this repository.

## Key takeaway
Parallel AI work stayed safe because the coordination rules were written down in the repository (ownership, a frozen interface, one merger) and did not depend on conversation memory. The contract let the analytics worker build against a fake backend and integrate later.

**Sources:** LOG-019, LOG-021, LOG-022; `docs/architecture/parallel-work-and-metrics-contract.md`; `docs/architecture/owner-directive-2026-10-18-priorities.md`; `docs/analytics/worker-notes.md`; `PROJECT_PLAN.md` B-05, B-13; commits `bb465b1`, `fcc64ba`
