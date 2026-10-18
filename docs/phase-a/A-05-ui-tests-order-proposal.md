# A-05 — Visual system, testing matrix and implementation order (Architect proposal v1)

**Status:** V1 A (English UI), V2 A, V3 A APPROVED 2026-10-16 by Owner. Remaining sections approved 2026-10-16 14:23 ("approve A-05, start Phase B").
**Builds on:** A-01…A-04 (approved; A-03 E2 shape pending your card answer).
This is the last Phase A item; GATE-A (authorizing Phase B) comes after it.

## Choices for you (V1–V3), each with my default in bold

### V1 — UI language and text direction
- **A. English interface, Hebrew messages exactly as the spec words them, all inputs and record text `dir="auto"` so Hebrew renders right-to-left (recommended)** — matches the spec and the English-language presentation; Hebrew free text works naturally.
- B. Full Hebrew RTL interface — mirrored layout; more work and every label needs Hebrew copy; spec doesn't ask for it.
- C. Language toggle — doubles copy and test cases.

### V2 — Navigation
- **A. Desktop: tab bar under a slim header. Mobile (<768 px): fixed bottom tab bar with icon + label. The free-text input is one shared "Add update" panel reachable from every tab (recommended).**
- B. Desktop left sidebar — uses width better but four tabs don't need it.

### V3 — Theme
- **A. Light theme only for the MVP (recommended)** — the spec's "Other = white badge with border" colour rule is defined for light backgrounds; one theme halves visual QA.
- B. Light + dark following the system setting — nicer, but every colour (including task category colours) needs a dark variant and double screenshots.

## Visual system (tokens)
- Type: system font stack (no web-font dependency); 16 px base, 1.25 scale.
- Spacing 4/8/12/16/24/32; radius 8 px cards, 999 px badges; one shadow level.
- Neutral greys + one accent (blue). Status: success green, warning amber, danger red.
- Task category badges: family blue, career yellow, friends green, housework red, other white with 1 px grey border — each paired with text, not colour alone.
- Open job applications (applied / interviewing): accent left border + "Open" badge.
- Every value shows its record date in small muted parentheses, e.g. `72.4 kg (14.10.2026)`.
- Calorie values always labeled "est." with the basis on hover/tap.
- States: skeleton loading, empty state with one call to action, inline error with retry, "No record entered for this day" placeholder in Hebrew as specified.
- Accessibility target: **WCAG 2.2 AA** — 4.5:1 text contrast, visible focus ring, full keyboard path, labeled inputs, `aria-live` for intake receipts and Run-now progress, 44 px touch targets.

## Wireframes
Desktop (≥1024 px):
```
┌ Personal Dashboard ─────────────────────── [Add update] [Owner ▾] ┐
│ Food & Health | Content | Jobs & Career | Tasks                  │
├──────────────────────────────────────────────────────────────────┤
│ [History | Aggregates]   range: [02.10.2026 → 16.10.2026] ◀ ▶    │
│ ┌ Weight ─────┐ ┌ Sleep ──────┐ ┌ Steps ──────┐ ┌ Calories ────┐ │
│ │72.4 (14.10) │ │7.2h (16.10) │ │9,812 (15.10)│ │1,950 est.    │ │
│ └─────────────┘ └─────────────┘ └─────────────┘ └──────────────┘ │
│ Day list: 16.10 meals… workouts… │ 15.10 … │ 14.10 placeholder   │
└──────────────────────────────────────────────────────────────────┘
```
Mobile (390 px): header → History/Aggregates toggle → range chips → stacked cards → day list; bottom bar: 🍎 Food · 📚 Content · 💼 Jobs · ✅ Tasks; floating "Add update" button opens a full-height sheet with the input, clarification chat and per-item receipt.
Jobs tab: Applications (open ones highlighted, status dropdown) · Recommendations (latest run, sources, "Run now — 2 of 3 left today", last-run time) · Trends (Israel/Global and Company/Technology filters, snapshot time).
Tasks tab: 15 day columns on desktop (horizontal scroll), one day per section on mobile.

## Testing matrix
| Layer | Tool | What | Live services |
|---|---|---|---|
| Unit | pytest | date parsing (DD.MM.YYYY, Hebrew), canonical keys, denominators, zero-vs-missing, coverage status, quota | none |
| DB integration | pytest + temp SQLite | PK/FK/UNIQUE/CHECK, user isolation, append-only calorie estimates, 4th Run now rejected, cascade rules | none |
| API | pytest + FastAPI TestClient + mocked OpenAI | intake success / clarification / invalid schema / timeout / injection text / multi-item partial receipt / duplicate message; Run now with mocked search results incl. dead URLs | none |
| Browser | Playwright MCP, viewports **1440×900** and **390×844** | the 9 journeys from A-01, task colours, highlighted applications, keyboard path, empty/loading/error states | none (test auth mode, synthetic DB) |
| Manual smoke (only when you authorize) | real OpenAI, real Google login, quick tunnel on phone + desktop | one intake, one Run now, sign-in/out, 403 for non-allowlisted account | yes, paid |

Test-only auth: `TEST_AUTH=1` enables a fake login used by Playwright; the app refuses to start with it if `PUBLIC_BASE_URL` isn't localhost.
Named skips (to list in the PDF): no automated test of real Google OAuth, live OpenAI quality, or tunnel availability; covered by the manual smoke only.

## Implementation order (Phase B)
Reordered so each step is demonstrable and the paid/external parts come last:
1. B-01 scaffold + config → 2. B-03 schema & migrations → 3. B-02 auth (Google + test mode) → 4. B-04 data APIs, manual edit/delete, history → 5. B-05 aggregates → 6. B-07 dashboard UI on synthetic data → 7. B-06 AI intake + calorie estimates → 8. B-08 + B-09 merged: Run now jobs & trends → 9. B-10 full Playwright pass → 10. B-11 quick tunnel demo + shutdown → 11. B-12 evidence & PDF.

On approval I'll update the plan text that still reflects the old spec: B-08 ("09:00 schedule") and B-09 ("daily update") become one "Run now" task (max 3/day), and B-03/B-06 mention the calorie estimates table.

## Demo storyboard (synthetic data, ~5 min)
Sign in → Food tab history with placeholders → free-text update with a meal and a workout → clarification question → receipt → resend same meal → Hebrew duplicate message → edit meal → new calorie estimate and new daily total → aggregates with documented-day denominator → Jobs: highlighted applications, Run now, sources → Trends filters → Tasks colours → mobile view on phone via tunnel → sign out.

## What I need from you
Approve V1 A, V2 A, V3 A and the rest as written, or name what to change. After that, GATE-A needs your explicit go-ahead to start Phase B.

---
## Superseded in part by ADR-005 (Owner directive 2026-10-17)
Approved text above is kept for history. Superseded: the Jobs-tab Recommendations and Trends areas and the Run-now counter, the Run-now aria-live progress, the Run-now rows of the testing matrix, the manual smoke "one Run now" (now "one analytics question"), the implementation order (now PROJECT_PLAN slices S1–S5), and the storyboard (below). V2's four tabs may gain an Analyst view (ADR-005 O-3).

**Revised A-05 storyboard** (replaces the original):
1. `start.ps1 -Demo` → banner → Explore.
2. Food history with placeholders → aggregates and charts with documented-day denominators.
3. Manual edit → new estimate.
4. Analyst: sleep comparison (answered, chart, evidence); steps vs workouts (association caveat); mood (fallback); a sparse-period comparison (partial).
5. Free-text update → clarification → receipt → duplicate Hebrew message.
6. Jobs tab: open applications highlighted. Tasks colours. Mobile viewport.
7. Reset → stop server.
