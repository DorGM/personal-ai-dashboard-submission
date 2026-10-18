---
name: code-quality
description: Apply compact modular, reusable, documented, and testable engineering standards when implementing or reviewing Personal AI Dashboard code.
---
# Coding standard

1. **Single responsibility:** small self-contained components/functions; explicit contracts; compose, don't duplicate.
2. **Minimal architecture:** add abstractions/dependencies only for a concrete need; keep UI, API, domain logic, DB, provider adapters, and scheduler separate.
3. **Reliable interfaces:** types/schemas, input validation, predictable errors, parameterized SQL, user scoping, transactions, PK/FK/UNIQUE/CHECK constraints.
4. **Professional docs:** concise docstrings for public/nontrivial functions (purpose, args, return, exceptions); README/setup notes for new integration points; comments explain *why*, not obvious *what*.
5. **AI safety:** OpenAI responses are untrusted structured suggestions; validate before insert; ambiguous data → clarification; existing record → reject rather than overwrite; no API keys in frontend or source control.
6. **Tests:** boundary/negative/unit tests for domain rules; integration tests for data/auth/API; Playwright desktop + mobile journeys where applicable; honest evidence and explicit untested risks.
7. **UI:** polished responsive hierarchy, accessible labels/contrast/focus, loading/empty/error states; prefer lightweight assets and reuse components.
8. **Runtime AI capabilities:** implement ONLY the extraction, clarification, calorie-estimation, daily job recommendations, and trend-refresh contracts in `PRODUCT_SPEC.md`; they are product behaviors, **not** Claude Code development agents.
