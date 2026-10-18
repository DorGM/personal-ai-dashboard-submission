# 04 — Google sign-in: identity preservation and a Host-header trust bug

**Phase / task:** Phase B / B-02 Google OAuth, session and CSRF · **Date:** 2026-10-17, 19:59–20:31 UTC

## Challenge
Sign-in is where data-loss bugs often hide: a repeated or concurrent first login can re-create a user row and orphan that user's records. The owner also wanted the OAuth setup fully configurable, with no personal credentials in the code.

## Original prompt
Label: `Verbatim excerpt of a recorded prompt`

> […]
> Continue with B-02 as planned, following our established Architect → Coder → Independent Verifier workflow.
> Make sure the Google OAuth implementation is fully configurable through environment variables, without hardcoded personal credentials or email addresses.
> The Verifier must explicitly test that repeated Google sign-ins preserve the existing user identity and all associated data, including relevant concurrent authentication scenarios.
> As agreed, after independent verification passes, update the documentation, perform security checks, commit, and push the verified checkpoint to GitHub.

The elided opening answered two housekeeping questions about repository content.

## AI response
The Coder built B-02 within the approved A-04 security defaults:
- Authlib OIDC with state, nonce and PKCE;
- an allowlist that binds the stable Google `sub`;
- a signed 8-hour cookie session and CSRF middleware;
- a `TEST_AUTH` fake login meant for localhost only.

Main Claude passed the owner's two requirements to the Verifier word for word, including: "Make sure the Google OAuth implementation is fully configurable through environment variables, without hardcoded personal credentials or email addresses."

## Human judgment
The owner set the acceptance bar: identity had to survive repeated and concurrent sign-ins. Main Claude took three defaults without escalating them and recorded each one (LOG-013):
- an absolute rather than sliding 8-hour session;
- the CSRF token also accepted as a form field, so sign-out works without JavaScript;
- a separate `test-auth:` identity, so a test user can never act as a real Google user.

## Iteration and verification
- **Round 1: FAIL (372 tests pass).** The Verifier found that the localhost check trusted the client: "The check uses `request.url.hostname`, which comes from the client-supplied Host header, plus the absence of proxy headers. It never looks at the peer address." With `TEST_AUTH=1` and a `0.0.0.0` bind, a request from another machine that sent `Host: localhost` got a 303 and a test session. The Verifier noted the preconditions (both non-default) but kept the finding blocking, "because the stated guarantee is false."
- **Data preservation already held in round 1.** The cases included:
  - 25 repeated sign-ins;
  - profile changes;
  - 24 threads doing a first sign-in;
  - 6 OS processes × 4 threads on one SQLite file;
  - sign-ins during concurrent writes;
  - two accounts signing in at once.

  Every case kept one user per Google ID with an unchanged id and `created_at`, and every other table was byte-identical.
- **Fix.** `TEST_AUTH` also requires a loopback peer address (`request.client`), and more proxy headers are refused. Three non-blocking items were fixed at the same time: a 503 page when Google is unreachable, streamed responses after form replay, and the README limits of cookie sessions.
- **Round 2: PASS.** With each fix reverted in a scratch copy, the matching new test failed. The Verifier also found a new non-blocking issue: a body truncated by a client disconnect was passed to the route as if complete. It was fixed before the commit.
- **Round 3: PASS (380 tests).** No route runs after a mid-body disconnect.
- **Not verified:** a real Google sign-in (no network), and browser behaviour. Both are recorded in the B-02 entry.

## Key takeaway
A "localhost only" guard based on request data the client controls offers no protection. The Verifier also kept to the requirement as stated, even though the preconditions made the bug low-impact.

**Sources:** LOG-013, LOG-014; `PROJECT_PLAN.md` B-02; `app/auth.py`; `tests/integration/test_auth.py`; commit `e1f1984`
