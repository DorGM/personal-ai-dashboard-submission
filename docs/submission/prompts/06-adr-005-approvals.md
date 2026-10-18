# 06 — ADR-005 approvals: soft delete, offline-only evaluation, owner-held dataset approval

**Phase / task:** ADR-005 open decisions O-0 to O-12; Phase B resumes at B-04 · **Date:** 2026-10-18 03:03 UTC

## Challenge
The Architect had proposed a default for each of 13 open decisions. Some of them concerned cost, data ownership and publication, which are decisions an autonomous implementation session should not make. The owner had to approve the design, while keeping the decisions with real-world consequences under human control.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt` (the approvals part is recorded in full in `docs/architecture/owner-approvals-2026-10-18.md`)

> I approve ADR-005 (O-0), subject to the decisions below.
> I also approve the following:
>
> * O-1: Option A — recoverable soft deletion using `deleted_at`, a Recently Deleted view, explicit restoration, and separately confirmed permanent deletion. Preserve all ADR-004 data-integrity safeguards.
> […]
> * O-8: The Coder may prepare the synthetic dataset according to ADR-005. I will review and approve the final dataset before publication and final submission.
> […]
> For O-9, you are authorized to prepare and execute automated offline evaluations using mocks and synthetic data. Real OpenAI API calls requiring my credentials or incurring charges must be approved by me before execution.

From the same message's verification section:

> Use synthetic data and mocked OpenAI responses for automated tests.
> Never report live external-service verification if it has not actually been performed.

## AI response
The ADR-005 §11 table set out the options and defaults:
- **O-1:** soft delete through `ADD COLUMN` (option a), permanent delete only (b), or soft delete for some tables only (c). The default was (a), because it avoids rebuilding parent tables under ADR-004.
- **O-8:** the Coder drafts the dataset, marked unapproved, and the owner approves it with a checksum.
- **O-9 (live evaluation and smoke test):** "authorize (key, model, budget) / skip and document", with the default "authorize a small run".

Main Claude acknowledged the approvals, recorded them, checked the git state against the remote, and started B-04.

## Human judgment
- **O-1:** the owner accepted the recommended soft-delete design and restated that ADR-004 stays binding.
- **O-9:** the owner did not take the Architect's default of a small live run. Only offline, mocked evaluation was authorized, and any paid call needs prior approval. This kept spending and the use of the owner's credentials under owner control.
- **O-8:** the Coder could draft the synthetic data, but approval of the final dataset stayed with the owner.

## Iteration and verification
- All 13 decisions were recorded in `owner-approvals-2026-10-18.md`, the status column of ADR-005 §11, and PROJECT_PLAN A-06 (LOG-017). Before any change, the git state was confirmed at `e267617`, matching the remote, with 380 tests passing.
- **O-1** became B-04: `deleted_at` on 11 MVP tables, a Recently Deleted view, restore, and a purge that works only on an already soft-deleted row and needs explicit confirmation (item 07).
- **O-9:** at release, the record lists live OpenAI calls and the golden evaluation set as not run (item 10). No live result is claimed anywhere.
- **O-8:** the fixture stayed marked unapproved through integration. A note for the owner's later review: "a few invented company names may match real firms".

## Key takeaway
Delegating implementation did not mean delegating every decision. Paid external calls and approval of published data stayed explicit owner gates, and the records keep work that ran apart from claims that were never tested.

**Sources:** LOG-017, LOG-018, LOG-022; `docs/architecture/owner-approvals-2026-10-18.md`; `docs/architecture/ADR-005-personal-ai-analytics.md` §11; `PROJECT_PLAN.md` A-06, B-04, B-14
