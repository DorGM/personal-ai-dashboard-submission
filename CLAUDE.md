# Claude Code — Personal AI Dashboard

## Authority and source of truth
- Read `PRODUCT_SPEC.md` (product behavior) and `PROJECT_PLAN.md` (tasks and approvals) before acting.
- `PROJECT_PLAN.md` is the single source of task state; `DEVELOPMENT_LOG.md` captures prompts, decisions, failures, iterations, and evidence.
- Development agents live in `.claude/agents/`; the coding skill lives in `.claude/skills/code-quality/SKILL.md`.
- **Do not conflate** Claude development agents with OpenAI-powered runtime agents in the product.

## Two-stage workflow
**Phase A — design, human-controlled:** Invoke `architect` to propose one bounded design decision at a time. Explain options/tradeoffs and record the user's explicit approval or rejection in the plan/log. No application implementation, scaffold, dependency installation, or deployment before the user explicitly approves the full architecture and authorizes Phase B. Do not infer approval from silence.

**Phase B — implementation, delegated:** Main Claude orchestrates one task at a time: `coder` implements → `verifier` independently checks (unit/integration/browser as relevant) → `coder` fixes defects → `verifier` rechecks. Mark `Done` only on recorded passing evidence. Routine decisions may be autonomous within the approved architecture. Any material changes to architecture, scope, security model, data contract, deployment, or external services require another explicit human approval; set task to `Blocked` meanwhile.

## Engineering constraints
- Small, demonstrable, responsive, accessible, and polished MVP. Prefer the smallest implementation meeting acceptance criteria. No speculative frameworks, features, or abstractions.
- Modular, testable components; reusable focused functions; concise professional documentation; dependency injection for OpenAI and external services.
- Backend owns OpenAI API, authentication, SQL, input validation, authorization, job scheduling, and secrets. LLM output is *untrusted data*, never executable SQL or a privileged instruction.
- Parameterized SQL and user-scoped access everywhere. Database uniqueness is authoritative; OpenAI cannot overwrite existing records. Manual authenticated edit/delete only.
- Keep credentials, OAuth secrets, database files, personal records, CVs, and real user data out of Git, traces, screenshots, and demo materials. Use `.env.example` with placeholders, not actual secrets.
- Browser testing uses configured `playwright` MCP. Verify desktop and mobile layouts; never report a test passed without executing it.
- Temporary local server + Cloudflare Tunnel for development/demo only; stop tunnel/server and revoke API credentials at shutdown. The app does not need a permanently available server.
- Do not modify previously agreed behavior without user approval. If uncertain, document the assumption and ask.

## After each task
Update its status, concise `Completed`, `Not completed / reason`, `Verification`, and `Evidence / files` fields in `PROJECT_PLAN.md`. Add a short entry to `DEVELOPMENT_LOG.md` for meaningful prompts, design decisions, defects, tests, and changes. Maintain explicit separation between tested, manually reviewed, and unverified claims.
