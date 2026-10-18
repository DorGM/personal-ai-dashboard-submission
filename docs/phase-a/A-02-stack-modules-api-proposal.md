# A-02 — Stack, modules and API contracts (Architect proposal v1)

**Status:** APPROVED 2026-10-16 by Owner ("approve A").
**Builds on:** A-01 approved (Option B; "Run now" generates job + trend recommendations, max 3 runs/day; 09:00 schedule dropped, so no background scheduler).
**Does not decide:** exact DDL (A-03), OpenAI model / search source / OAuth hostname (A-04), visual design and test matrix (A-05).

## Constraints that drive the choice
- One local process that owns OpenAI, auth, SQL, validation, scheduling and secrets (CLAUDE.md).
- Run-now insight jobs can take tens of seconds, so the server must run them as background tasks and let the page poll for the result.
- Strict structured-output validation of untrusted LLM data; deterministic parameterized SQL.
- Small dependency budget, polished responsive UI, Playwright browser tests.

## Options

### Option A — Python: FastAPI + SQLite + server-rendered HTML with HTMX (recommended)
- Backend: FastAPI, Pydantic v2 (request validation **and** the OpenAI structured-output schema from the same models), stdlib `sqlite3` with hand-written parameterized SQL and numbered `.sql` migrations, `PRAGMA foreign_keys=ON`.
- UI: Jinja2 templates + HTMX for tab switching, range controls, intake/clarification panel; hand-written CSS with design tokens. No JS build step.
- Background work: FastAPI background task for each Run now; no scheduler library.
- Auth: Authlib (Google OIDC) + signed, HttpOnly, Secure session cookie.
- Tests: pytest (unit + DB integration with temp SQLite), mocked OpenAI client via dependency injection, Playwright MCP for browser.
- **Pro:** smallest stack; one language; Pydantic gives schema validation for both API and LLM output; SQL stays visible and auditable; trivial local start.
- **Con:** HTMX is less suited to highly interactive client state (fine for tabs, forms, filters here); fewer ready-made UI components, so polish is hand-built CSS.

### Option B — TypeScript: Fastify + React (Vite) SPA + SQLite (better-sqlite3)
- Shared zod schemas for API and LLM output; React UI with a small component library.
- **Pro:** end-to-end types; richest UI interactivity; large component ecosystem.
- **Con:** two build pipelines (server + client), more dependencies, CSRF/session wiring split across SPA and API; more surface to verify.

### Option C — Next.js full-stack + Prisma + SQLite
- **Pro:** one framework for UI and API; good DX.
- **Con:** long-running background runs fit poorly with its request-scoped route handlers; Prisma ORM hides SQL the spec wants explicit and reviewable; heaviest dependency footprint.

### Database engine (applies to A or B)
**SQLite** (single local file, no extra service, FK enforcement on, WAL mode). Postgres considered and rejected for the MVP: it adds a service to run and secure with no requirement that needs it (single user, local, low concurrency).

## Recommendation
**Option A with SQLite.** It best matches "backend owns everything", keeps SQL explicit for the aggregate semantics, and has the smallest dependency budget.

## Module boundaries (Option A)
| Module | Owns | Depends on |
|---|---|---|
| `app/main.py` | app factory, wiring, dependency injection | all |
| `auth/` | Google OIDC login/callback/logout, allowlist, session, CSRF | `db` |
| `db/` | connection, migrations runner, transaction helper | — |
| `repos/` | one repository per domain (food, content, career, tasks, users); parameterized SQL only; every query takes `user_id` | `db` |
| `services/aggregates.py` | history windows, documented-day denominators, weight first/last | `repos` |
| `services/intake/` | date parsing (DD.MM.YYYY + Hebrew), OpenAI extraction, clarification state, validation, per-item receipt | `ai_client`, `repos` |
| `services/insights/` | job + trend runs, Run-now quota (3/day), dedupe by canonical identity | `ai_client`, `search` (A-04), `repos` |
| `ai_client/` | thin injected wrapper around OpenAI; mockable; logs model + schema version | — |
| `web/` | routes, Jinja templates, HTMX partials, static CSS | services |

Rule: LLM output only ever enters `services/intake` or `services/insights` as Pydantic-validated data, and only `repos/` touches SQL.

## API surface (typed routes; HTML responses for pages/partials, JSON where noted)
Auth: `GET /login`, `GET /auth/callback`, `POST /logout`.
Pages: `GET /` → redirect to `/food`; `GET /food`, `/content`, `/career`, `/tasks`.
History / aggregates (per domain `d` in food·content·career·tasks): `GET /{d}/history?start&end` (≤15 days, validated), `GET /{d}/aggregates?start&end`.
Records (per table, e.g. `meals`, `workouts`, `articles`, `job_applications`, `tasks`): `POST /api/{table}`, `PATCH /api/{table}/{id}`, `DELETE /api/{table}/{id}` — manual only, CSRF-protected, user-scoped.
Intake: `POST /api/intake` `{text}` → receipt `[{item, outcome: inserted|duplicate_rejected|needs_clarification|not_recorded, message}]` + optional `clarification_id`; `POST /api/intake/{clarification_id}/answer` `{text}`.
Insights: `POST /api/insights/run` → starts a job+trend run, returns `429` with the remaining-quota message after 3 manual runs that day; `GET /career/report?run_id`, `GET /career/trends?region&type`.
Profile: `GET/PUT /api/job-profile` (preferences text), `POST /api/job-profile/cv` (PDF upload, stored outside the web root and Git).
Health: `GET /healthz` (no data).

All routes except `/login`, `/auth/callback`, `/healthz` require a session; `user_id` comes only from the session.

## Repo structure
```
personal-ai-dashboard/
  app/ (modules above)
  migrations/ 001_init.sql …
  tests/ unit/ integration/ e2e/
  scripts/ seed_synthetic.py, start.ps1, stop.ps1
  data/            (gitignored: SQLite file, CV)
  .env.example     (placeholders only)
  pyproject.toml
```
Windows-friendly start/stop scripts, since your machine is Windows.

## Dependency budget (runtime)
fastapi, uvicorn, jinja2, pydantic, authlib, httpx, itsdangerous, openai, pypdf, python-multipart. HTMX vendored as one static file. **Amendment 2026-10-16:** `tzdata` added with the owner's approval (Windows has no IANA time zone database). Dev: pytest, ruff, mypy. **Nothing else without a new approval.**

## What I need from you
Approve Option A (Python/FastAPI/HTMX/SQLite), pick B or C, or change specific lines.
