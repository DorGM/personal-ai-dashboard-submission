# A-01 — MVP scope boundary (Architect proposal v1)

**Status:** APPROVED 2026-10-16 by Owner as Option B, amended: each "Run now" click generates job and trend recommendations, max 3 runs per day.
**Decides:** what from `PRODUCT_SPEC.md` v0.1 is in the MVP, what is explicitly out, and what "done" means.
**Does not decide:** stack (A-02), DDL/uniqueness edge cases (A-03), AI/search provider, OAuth hostname, scheduler catch-up (A-04), visuals/test matrix (A-05). Each of those comes back to you separately.

## Why this is first
Every later decision (stack, schema, services, test plan) is sized by scope. The spec describes 17 tables, 4 tabs, AI intake with clarification, two scheduled external-data agents, Google OAuth, a Cloudflare demo and a PDF. The highest-risk items are the ones that depend on outside services: a live jobs feed (most job boards forbid scraping and have no open API), trend sources, and OAuth through a tunnel.

## Options

### Option A — Full spec, everything equal priority
All 17 tables, all four tabs, AI intake, live job and trend agents, OAuth, tunnel.
- **Pro:** matches the spec literally; nothing to explain away in the PDF.
- **Con:** highest delivery risk; the live-jobs feed may turn out legally or technically unviable late, with no fallback; the demo depends on a 09:00 run having happened.

### Option B — Full core, external agents in scope but bounded (recommended)
In scope:
1. Four tabs, responsive desktop + mobile, accessible states (loading / empty / error / placeholder text).
2. All 17 tables, manual create / edit / delete, 15-day history, aggregates with the documented-day denominator.
3. AI free-text intake with explicit-date rule, structured output, clarification loop, per-item receipt, duplicate rejection message.
4. Job Recommendation and Trend Refresh agents using **one** approved source each (chosen in A-04), daily 09:00 Asia/Jerusalem while running, **plus a manual "Run now" button** so the demo does not depend on the clock.
5. Google sign-in with an allowlist; temporary Cloudflare tunnel for the demo only.
6. Synthetic demo data seed; evidence + single PDF.

Explicitly **out** of the MVP:
- Scraping job boards, or more than one job source / trend source.
- Notifications (email, push), PWA/offline mode, native mobile app.
- Charts beyond simple numeric tiles and lists (no charting library unless A-05 justifies one).
- CV formats other than PDF; CV used as extracted text only, never displayed publicly.
- Catch-up runs for missed 09:00 slots beyond what A-04 approves (default proposal there: skip + show staleness).
- Multi-user features beyond the allowlist (no sharing, roles, admin UI).
- Import from other apps (Fitbit, Google Fit, etc.) and any ChatGPT history access.

- **Pro:** keeps every spec feature visible in the demo; isolates the risky external part behind one source and a manual trigger; small dependency budget.
- **Con:** jobs/trends quality depends on a single source; a "Run now" button is an addition to the spec (small, but it is a scope change that needs your OK).

### Option C — Tracking core first, jobs/trends as stretch
Tabs, tables, history, aggregates, AI intake, OAuth. Job recommendations and trends only if time remains.
- **Pro:** lowest risk; strongest polish on the core.
- **Con:** drops two spec-required features; the Jobs tab would only show applications; weaker demo of "autonomous runtime intelligence".

## Recommendation
**Option B.** It delivers the whole product contract while containing the two features most likely to slip. If A-04 later finds no viable job source, we fall back to C for jobs only, with your approval.

## Core user journeys (both viewports)
1. Sign in with Google (allowlisted) → land on Food & Health, today.
2. Type a dated free-text update → see per-item receipt → answer a clarification → see new rows with their record date.
3. Re-submit the same meal → see "נתון זה כבר הוכנס, אם תרצה להוסיף אליו נתונים תכניס זאת ידנית".
4. Open history (default trailing 15 days, placeholder on undocumented days) → move the window → switch to aggregates → change range.
5. Manually edit / delete a record → derived daily calories recalculate.
6. Jobs tab: open applications highlighted, update status; view today's report with sources; press "Run now".
7. Trends tab section: filter Israel/global, company/technology; see last-updated time and staleness.
8. Tasks tab: today + 14 days, category colours (Other = white with border), mark complete.
9. Sign out; unauthenticated URL access is rejected.

## Definition of done (MVP)
- Every in-scope item above has a Phase B task marked Done with a Verifier PASS and recorded evidence.
- Verification contract in spec §6 executed; any test not run is listed with risk and mitigation.
- Demo runs on synthetic data only; tunnel and server stopped and API key revoked afterwards.

## What I need from you
Approve Option B (including the "Run now" button and the exclusion list), pick A or C instead, or change specific lines.

---
## Superseded in part by ADR-005 (Owner directive 2026-10-17)
Approved text above is kept for history. Item 4 and the Run-now amendment (fresh job and trend recommendations per click, max 3/day), and journeys 6 (report and Run now) and 7 (trends), are **Deferred / out of MVP**. The "single PDF" deliverable is replaced by a public repository, a PDF report and a recorded video. Simple predefined SVG charts are now in scope (still no chart library). See `docs/architecture/ADR-005-personal-ai-analytics.md` §1.
