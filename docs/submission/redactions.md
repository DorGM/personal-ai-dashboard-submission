# Redactions and editorial changes in the published records

The development records in this repository are sanitized copies of the private records at private commit `90e1f34`. The private history is preserved unchanged. This file lists what was removed or changed, and why. It never repeats a removed value.

## Removed or replaced
| Where | What | Marker | Reason |
|---|---|---|---|
| `DEVELOPMENT_LOG.md`, LOG-013, quoted owner prompt | One sentence about the intended audience of the project presentation | `[REDACTED: personal context]` | Personal context, not engineering content |
| `DEVELOPMENT_LOG.md`, LOG-013 | A company name | `[company name]` | Already redacted in the private log at the owner's request; the project is company-neutral |
| `docs/submission/prompts/` | Parts of owner messages about personal scheduling, deadlines, repository-access questions, and a company name | `[…]` | Personal context or not about the engineering decision; every remaining quote is exact |
| Demo video (release asset) | The browser's top bar (tab strip, address bar, bookmarks) | Cropped from 1280×720 to 1280×600 | It showed personal bookmarks, a mail tab title and a company bookmark |

## Not published
These private files are cited in the records and stay private: `RESUME_CHECKPOINT.md`; the verbatim owner directive and approval files `docs/architecture/owner-*.md` (they mix engineering decisions with personal scheduling context; the engineering parts are quoted in the curated prompts); the analytics worker's notes and hand-off `docs/analytics/*`; and the submission drafts and security/PII audit under `docs/submission/` other than this folder's published files. Raw session transcripts, Verifier probe scripts and screenshots were never committed and are not published.

## Kept on purpose
- The author's name and GitHub username (approved by the owner).
- The application timezone `Asia/Jerusalem`. It is a product setting that is already in the code.
- Commit SHAs of the private repository. They identify the recorded evidence and contain no personal data.
- Dates and times of decisions, and test counts.

## Editorial changes (not redactions)
- `DEVELOPMENT_LOG.md` and `PROJECT_PLAN.md`: a public-copy note at the top. Entry IDs, order and wording are otherwise unchanged. The B-12 record and LOG-024 (publication) were written in the private records first and copied here.
- `docs/submission/evidence-index.md`: brought up to date at publication from the plan and the log (B-05, B-07, B-13 integration, B-15, the submission package and the evidence gaps). The added and changed rows are marked *(updated at publication)*.
