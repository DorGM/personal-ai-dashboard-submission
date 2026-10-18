# 02 — Calorie re-estimation: correcting an "exception" into an append-only design

**Phase / task:** Phase A / A-03 schema (edge case E2) · **Date:** 2026-10-16, 10:58–11:15 UTC

## Challenge
The project rule was that the runtime AI never overwrites saved records. The owner also wanted meal calories to be re-estimated whenever a meal changes. The Architect resolved the tension the wrong way, and the error had to be caught before any schema was built.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt`

The owner's amendment to the A-03 schema proposal (11:06 UTC):

> approve A, but E2 needed to be treated more deeply - i want the LLM to calculate the calorie each time a meal is added, edited or deleted on fly, which means that the LLM gets the prompt, extract items, and if it found a meal within the prompt, it calculate the calories and it will be added to the relevant field. […]

The owner's correction (11:13 UTC):

> make sure you align with this: E2 isn't deliberate exception to the "AI never edits a saved record" rule, because that if a meal is added/changed or whatever, the llm opens a new record with the changed calories, which mean, each day's total calorie is represented by sum of calories per date.

## AI response
After the amendment, the Architect recorded E2 as an exception to the rule. Its message to the owner said: "The E2 change is a deliberate exception to the "AI never edits a saved record" rule. It only touches the calorie fields of the meal you're editing." LOG-004 recorded the same framing ("a scoped, user-initiated exception to the "LLM never writes existing records" rule").

After the correction, the Architect redesigned the storage and put two options on a decision card ("Store calorie changes as new estimate rows, with the daily total summed on read?"):
- "Estimates table": "Calories move into a new add-only estimates table, and the daily total becomes a live SQL sum with nothing stored to drift out of sync." (recommended)
- "Daily rows": "Calories stay on the meal row, and each change adds a new daily-total row, with the newest row per date shown."

## Human judgment
The owner rejected the "exception" framing. A rule that admits a convenient exception stops being a rule. The owner restated the requirement as additive: every change creates a new record, and the daily total is derived. The owner then chose "Estimates table" (11:15 UTC). Main Claude logged the misframing as its own error: "I also logged that the "exception" wording was my mistake, which you caught."

## Iteration and verification
- The schema proposal went to v3: an append-only `meal_calorie_estimates` table and a `daily_calories` SQL view. A meal's current value is its newest estimate (LOG-005).
- The correction happened before Phase B was authorized, so no code had to be rewritten.
- B-03 Verifier round 1 later found that estimates were still deletable while their meal existed. The fix added triggers that make estimates undeletable while the meal exists (LOG-009).
- In B-04, the Verifier checked the append-only chain on a real server: "unknown → user_provided 350 → (kcal left empty: no row) → unknown after a description edit → user_provided 0", with earlier rows never modified (B-04 round 1 report).
- Later scope note: AI intake (B-06) was deferred out of the MVP on 2026-10-18 (item 08). Manual calorie entries still use the append-only table.

## Key takeaway
Explaining a requirement as an "exception" was a sign that the model had not yet found a design that satisfied the rule. An append-only data model satisfied both constraints at once, and it also produced an audit trail.

**Sources:** LOG-004, LOG-005, LOG-009, LOG-018; `PROJECT_PLAN.md` A-03, B-03, B-04; `docs/phase-a/A-03-schema-proposal.md` (v2, v3)
