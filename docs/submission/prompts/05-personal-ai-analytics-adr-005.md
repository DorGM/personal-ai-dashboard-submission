# 05 — Product direction change: Personal AI Analytics (ADR-005)

**Phase / task:** Architecture re-scope, A-06 / ADR-005 (planning only) · **Date:** 2026-10-17 21:22 UTC; checkpoint pushed 23:55 UTC

## Challenge
The owner changed the product's purpose: from external job and industry-trend recommendations to analysis of the user's own records. The new core feature was a runtime agent that answers free-form questions about personal data. That agent must not run arbitrary SQL, make up numbers or claim causes, and the change had to be designed and documented without touching code.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt` (the message has about 15,300 characters; the full text is kept in the private development repository; see [redactions](../redactions.md))

> IMPORTANT: This session is for architecture and planning only. Do not invoke the Coder or Verifier, implement features, modify application code, install dependencies, or begin B-04.
> […]
> This is NOT a decision driven by token limitations, development cost, or time constraints.
> […]
> Do not allow the LLM to execute arbitrary SQL.
> Instead, design bounded, read-only, parameterized retrieval and analytical tools. The model may dynamically choose tools and request additional data where necessary.
> All calculations involving counts, averages, comparisons, percentages, date ranges, and numerical metrics must be performed deterministically by the backend.
> […]
> Design a separate, preferably deterministic Evidence Validation layer within the same runtime workflow, not a second autonomous agent.
> […]
> Do not invent missing observations, treat undocumented days as zeros, or imply causation without sufficient evidence.

## AI response
A read-only Architect agent wrote ADR-005 and a revised plan (LOG-016):
- **Agent:** one OpenAI function-calling agent with six read-only tools: coverage, summarize, compare periods, daily series, relate metrics and list records. Every tool runs on a read-only, user-scoped connection.
- **Evidence ledger:** the model writes placeholders, never digits, and the backend renders every number.
- **Validator:** deterministic rules V1–V14, one repair turn, then degrade to the findings that passed or the owner's fallback message.
- **Charts and Demo Mode:** charts are server-rendered SVG with no new dependency. Demo Mode uses a committed synthetic fixture and a local-only demo identity.
- **Open decisions:** the Architect listed O-0 to O-12 together. It flagged this as a deviation from its one-decision-at-a-time role: "The directive asked for a full planning update without blocking, so the open decisions are listed together in §11 instead."

## Human judgment
The scope was decided by the owner and not re-asked. Main Claude did not accept the Architect's draft without checking it. One claim was that git commit metadata contained the owner's email. Main Claude checked the history, found that every commit is authored by the Claude tool identity, and corrected ADR-005. As reported to the owner: "One correction: the Architect said the commit history contains your email. I checked, and every commit is authored as Claude, so I fixed that in ADR-005." The recommendation for a fresh public repository stayed, with its real reasons recorded instead: a company name in an early commit, and private-session details in the log.

## Iteration and verification
- Planning only: no tests and no Verifier ran, and nothing was marked verified. Only documentation changed, in the docs-only commits `ad5d7ed` and `e267617`.
- Earlier decisions were marked superseded or deferred rather than deleted: jobs and trends B-08/B-09 became Deferred, and their tables were kept.
- A knowingly stale `.env.example` comment was recorded as a limitation, because runtime configuration was off-limits that session. B-15 fixed it later.
- The owner approved the design in the next session (item 06).

## Key takeaway
An Architect agent's review can contain confident factual errors about the repository. The orchestrator checked a claim that would have shaped the release strategy against git before passing it on.

**Sources:** LOG-015, LOG-016; `docs/architecture/ADR-005-personal-ai-analytics.md` (§1, §3, §4, §11); `docs/architecture/owner-directive-2026-10-17-personal-ai-analytics.md`; `PROJECT_PLAN.md` A-06; commits `ad5d7ed`, `e267617`
