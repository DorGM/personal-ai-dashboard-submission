# Appendix B — Claude Code Prompts, Iterations, and Engineering Decisions

## Purpose
This appendix is a curated selection, not a transcript dump. It shows ten interactions from the development of the Personal AI Dashboard where a prompt, an AI proposal, a human correction or an independent verification changed the outcome. Each item follows the same structure: Challenge, Original prompt, AI response, Human judgment, Iteration and verification, Key takeaway, Sources.

The project was built by Dor Meir with Claude Code. Main Claude orchestrated three development agents: an Architect that proposes, a Coder that implements and an independent Verifier that tests. These are build-time agents. They are separate from the single OpenAI-powered analytics agent inside the product.

## Selection criteria
An interaction was included only when it meets all three of these conditions:
- It changed a design, a scope or a release decision, or a defect it exposed was fixed and re-verified.
- The record holds evidence for it: a recorded prompt, the AI's actual reply, a Verifier report, a log entry, a test file or a commit.
- It shows a reusable engineering lesson: human steering of an AI proposal, adversarial verification, or honest reporting of what was not tested.

Trivial prompts such as "approve A" or "continue" do not appear as items of their own. Where they matter, they appear inside an item as the approval that closed it.

## Label legend
| Label | Meaning |
|---|---|
| `Verbatim recorded prompt` | The owner's complete message, copied exactly. |
| `Verbatim excerpt of a recorded prompt` | Exact text from a longer owner message. Omitted parts are marked `[…]`. |
| `Faithful summary of a recorded instruction` | A paraphrase of a recorded instruction, used when exact text is unsuitable. Not used in this appendix. |
| `Documented decision without an available original prompt` | No owner prompt triggered the event. The item is reconstructed from Verifier reports, the development log and commits. |

## Items
| # | Title | Phase / task | Label | Link |
|---|---|---|---|---|
| 01 | Phase A kickoff and the first scope decision | Phase A / A-01 | Verbatim recorded prompt | [01-phase-a-kickoff-scope.md](01-phase-a-kickoff-scope.md) |
| 02 | Calorie re-estimation: from "exception" to append-only | Phase A / A-03 (E2) | Verbatim excerpt of a recorded prompt | [02-calorie-estimates-append-only.md](02-calorie-estimates-append-only.md) |
| 03 | Data-integrity directive: ADR-004 and six Verifier rounds | Phase B / F-01 | Verbatim excerpt of a recorded prompt | [03-data-preservation-adr-004.md](03-data-preservation-adr-004.md) |
| 04 | Google sign-in: identity preservation and a Host-header trust bug | Phase B / B-02 | Verbatim excerpt of a recorded prompt | [04-google-signin-identity.md](04-google-signin-identity.md) |
| 05 | Product direction change: Personal AI Analytics | A-06 / ADR-005 | Verbatim excerpt of a recorded prompt | [05-personal-ai-analytics-adr-005.md](05-personal-ai-analytics-adr-005.md) |
| 06 | ADR-005 approvals: soft delete, offline-only evaluation, dataset approval | ADR-005 O-0 to O-12 | Verbatim excerpt of a recorded prompt | [06-adr-005-approvals.md](06-adr-005-approvals.md) |
| 07 | 607 passing tests and every browser form broken | Phase B / B-04 | Documented decision without an available original prompt | [07-csrf-blind-spot-b04.md](07-csrf-blind-spot-b04.md) |
| 08 | Parallel workstreams: frozen contract, single integrator | B-05 / B-13 / B-15 protocol | Verbatim excerpt of a recorded prompt | [08-parallel-workstreams-contract.md](08-parallel-workstreams-contract.md) |
| 09 | Demo-mode isolation defect in the Analyst | B-15 integration | Verbatim excerpt of a recorded prompt | [09-analyst-demo-isolation.md](09-analyst-demo-isolation.md) |
| 10 | Release readiness under time pressure | Integration / release | Verbatim excerpt of a recorded prompt | [10-release-readiness.md](10-release-readiness.md) |

## Honesty note
- **Owner messages** are quoted verbatim from the recorded development session, including their original lowercase and phrasing. Times are in UTC.
- **AI responses** are quoted only where the exact text exists in the session record or the committed log. Everything else is a summary and is not shown in quotation marks.
- **Numbers** (test counts, rounds, measurements) come from the Verifier reports and `DEVELOPMENT_LOG.md` / `PROJECT_PLAN.md`. A number counted indirectly, such as progress dots, is labelled as such.
- **Two workers ran outside this project:** the analytics worker (B-13/B-15) and the submission worker. Their prompts are not recorded here. The only evidence of their work is what they committed: `docs/analytics/worker-notes.md`, `docs/analytics/HANDOFF.md` and the submission files.
- **Redactions:** personal information, identifiers, private links, local paths and material unrelated to the engineering work were removed. Removed passages are marked `[…]`, and any replaced text is marked `[REDACTED: reason]`. No quoted text was otherwise edited.
- **Untested claims:** what was not tested is stated in each item. In particular, no live OpenAI call and no offline golden-set evaluation were run.
