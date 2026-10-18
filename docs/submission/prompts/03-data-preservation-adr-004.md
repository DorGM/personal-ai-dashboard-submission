# 03 — Data-integrity directive: ADR-004 and six Verifier rounds

**Phase / task:** Phase B / F-01 foundational safeguards (inserted before B-02) · **Date:** 2026-10-17, 18:45–19:54 UTC

## Challenge
A read-only schema review had found residual risks (L1–L4), including one where `INSERT OR REPLACE` could silently delete a user's data. The owner made data preservation a project-wide requirement: no automatic deletion during sign-in, updates or migrations, and safe migrations under real concurrency.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt` (the message has about 10,800 characters and seven sections)

> 1. Data Integrity and Deletion Policy — Highest Priority
> Data preservation must be a fundamental architectural requirement throughout the entire project.
>
> * No automatic or unintended deletion of user data is allowed during authentication, updates, synchronization, migrations, or error handling.
> […]
> * Prevent destructive SQL operations, including unsafe INSERT OR REPLACE patterns.
> […]
> Do not implement new deletion features prematurely. Establish the architectural safeguards now and implement the relevant UI behavior when those features are developed.
> 2. L2 — Mandatory Independent Verifier Tests
> […]
> If any potentially destructive behavior exists, fix it before proceeding with B-02.
> […]
> * Automated tests include genuine concurrency scenarios using independent connections or processes.
> * Failed or interrupted migrations cannot leave the database partially modified.

The remaining sections covered URL validation and SSRF, a private GitHub repository with secret hygiene, and commit/push checkpoints after each Verifier PASS.

## AI response
Main Claude (acting as Architect and Coder) wrote ADR-004. Its main decisions:
- every foreign key is `ON DELETE RESTRICT`;
- a `deletion_grants` table plus a `BEFORE DELETE` trigger on every user-owned table, so a stray `DELETE` raises an error;
- `recursive_triggers` so that `REPLACE` fires the guard;
- identity triggers so that ids cannot change;
- sign-in as `INSERT … ON CONFLICT DO NOTHING` followed by `SELECT`;
- a migration runner that runs inside one `BEGIN IMMEDIATE` transaction and checks invariants before `COMMIT`.

The Verifier brief asked for an adversarial check: "Verify each one adversarially and do not trust the tests: write your own probes."

## Human judgment
The owner set the policy and the execution order and approved routine decisions within scope. Main Claude adopted some defaults without escalating them and recorded each one (LOG-011):
- `RESTRICT` plus grant-guarded deletes;
- the soft-delete design deferred to B-04 for owner approval;
- frontend URL validation deferred until forms exist.

## Iteration and verification
Each round's fix was attacked again by the independent Verifier (LOG-012):

| Round | Verdict | What the Verifier broke |
|---|---|---|
| 1 | FAIL (208 tests; 1 flaky in 6 runs) | A `;COMMIT;` line got past the keyword check and committed part of a migration; `UPDATE meals SET rowid=77` renumbered a row; concurrent first open crashed on the WAL pragma; a migration could drop the guard triggers |
| 2 | FAIL (284 pass) | The guard check matched trigger text, so an extra auto-grant trigger or a `WHEN 0` guard passed; app connections could `CREATE TRIGGER` |
| 3 | FAIL (306 pass) | A WHEN clause wrapped in double quotes normalized equal to the template, but SQLite reads it as a string constant, so the guard never fired |
| 4 | FAIL (313 pass) | A regression from the round-3 fix: a quoted `"deletion_grants"` name in an extra trigger was not recognized |
| 5 | FAIL (320 pass) | A migration could insert standing grants and leave rows deletable by any later `DELETE` |
| 6 | PASS | 28 repros from rounds 2–5 rejected; 0 bypasses in a unicode-whitespace fuzz; 326 tests passed 5/5 runs |

The final design replaced text matching with SQLite authorizers, exact guard templates, and a check that the grants table is empty before `COMMIT` and at every startup. The round-1 report also confirmed what already held. From the round-1 report: "Concurrent sign-in from separate processes: 10 rounds × 15 processes × 20 calls, 0 failures, no duplicate users, stable ids, related rows unchanged." Remaining limits, such as reviewed migrations being trusted code, are listed in ADR-004 "Known limits".

## Key takeaway
Each fix created new attack surface, and only repeated adversarial re-verification found it. Checking what SQLite actually does (authorizers, behaviour) proved sturdier than comparing SQL text.

**Sources:** LOG-011, LOG-012; `PROJECT_PLAN.md` F-01; `docs/architecture/ADR-004-data-preservation.md`; `tests/integration/test_data_preservation.py`; `tests/integration/test_migrations.py`; commit `42c21af`
