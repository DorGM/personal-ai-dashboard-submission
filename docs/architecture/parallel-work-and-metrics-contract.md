# Parallel work protocol and B-05 metrics contract

**Status:** binding for the parallel phase (Owner directive 2026-10-18, `owner-directive-2026-10-18-priorities.md`).
**Applies to:** the main workstream (B-14 → B-05 → B-07 → integration) and the analytics workstream (B-13 → B-15).

## 1. Roles
- **Integration lead (main workstream).** The only worker that commits or pushes to `main`, and the only one that edits the planning documents. It runs the integration Verifier after every merge.
- **Analytics worker.** Works in its own checkout (a separate clone or a `git worktree`) on branch **`feature/analytics-b13`**, created from the `main` commit that adds this file. It never pushes to `main`, never force-pushes, never rewrites `main` history, and never opens or merges a pull request into `main` itself.

Both follow `CLAUDE.md`, ADR-004 and ADR-005, plus the same rules for secrets and personal data: no keys, personal identifiers or real data in any commit, and a secret scan before every push. Live OpenAI calls stay forbidden until the Owner authorizes O-9.

## 2. File ownership
A file belongs to one workstream at a time. To change a file you don't own, ask the integration lead; don't edit it.

| Path | Owner |
|---|---|
| `app/analytics/__init__.py` | Both may create it. The content must be exactly the single line `"""Personal analytics: metric registry (B-05) and analytics agent (B-13/B-15)."""`, so the two additions merge cleanly. |
| `app/analytics/metrics.py`, `app/analytics/periods.py` (if needed) | Main (B-05) |
| `app/db.py` (including the new `read_only=True` mode) | Main (B-05). The analytics worker uses a local test helper until the mode is merged. |
| `app/analytics/ledger.py`, `schemas.py`, `policy.py`, `validator.py`, `render.py`, `charts.py`, `tools.py` | Analytics |
| `app/ai_client.py`, `app/analytics/agent.py`, `app/analytics/prompts.py` (B-15) | Analytics |
| `app/web/analyst.py`, `app/web/templates/analyst*.html`, `app/web/templates/analytics_macros.html` (B-15) | Analytics, from the B-15 phase. `analyst.html` currently has B-14 edits on `main`; rebase before touching it. |
| `app/main.py`, `app/config.py`, `app/web/deps.py`, `base.html`, `app.css` | Main. B-15 may add **one** router-include line and the AI settings as a patch the integration lead applies; list them in the hand-off. |
| `tests/support/reference.py`, `demo/`, `scripts/build_demo_fixture.py` | Main (B-14). Analytics imports them read-only. |
| `tests/unit/analytics/`, `tests/integration/test_analytics_*.py`, `tests/golden/` | Analytics |
| `tests/unit/test_metrics*.py`, `tests/integration/test_metrics*.py` | Main |
| `PROJECT_PLAN.md`, `DEVELOPMENT_LOG.md`, `RESUME_CHECKPOINT.md`, `PRODUCT_SPEC.md`, `README.md`, `docs/architecture/*` | Integration lead only |
| `docs/analytics/worker-notes.md` | Analytics. Decisions, defects, Verifier results and prompts, which the integration lead copies into the log. |
| `pyproject.toml`, `.env.example`, `.gitignore`, `start.ps1`, `scripts/check.ps1` | Integration lead. No new dependency without the Owner's approval. |

## 3. Phases of the analytics workstream
1. **Now, independent of B-05:**
   - the final-answer schema (ADR-005 §4.3);
   - the evidence ledger (§3.4);
   - policy thresholds (§4.2, read from the registry's `min_delta` once it exists; until then, the values in §4.2 V9);
   - the validator V1–V14 and the repair/degrade decision (§4.4);
   - the placeholder renderer and the verbatim fallback (§4.5);
   - chart view-models.

   Tests use hand-built tool-result dicts in the T1–T6 shapes of §3.3.
2. **After B-05 is merged to `main`:** the analytics worker rebases (or merges `main` into its branch) and builds `tools.py` (T1–T6) on top of §4 below. It also adds the golden tests against `tests/support/reference.py` and the B-14 fixture at anchor date 2026-10-17.
3. **B-15, after B-13 is integrated:** the scripted-model agent loop, `ai_client.py`, and the Analyst route and UI.

## 4. Metrics contract (`app/analytics/metrics.py`, built by B-05)
This interface is frozen once committed. Changing it needs both workstreams to agree, through the integration lead.

```python
Domain = Literal["weight", "sleep", "steps", "meals", "workouts", "articles", "podcasts",
                 "movies", "series", "job_applications", "tasks"]
MetricKind = Literal["daily_value", "event_count", "event_value", "ratio"]

@dataclass(frozen=True)
class MetricDef:
    id: str                       # e.g. "sleep.hours" (ADR-005 §3.3 table, closed set)
    kind: MetricKind
    unit: str                     # "h", "steps", "kg", "kcal", "meals", "min", "items", "tasks", "applications", "%"
    domain: Domain
    label: str                    # English UI label
    min_delta: float              # ADR-005 §4.2 V9 defaults
    subtype_field: str | None     # meal_type, workout_type, category, status, or None
    allowed_filters: Mapping[str, frozenset[str]]   # filter name -> allowed enum values

REGISTRY: Mapping[str, MetricDef]          # read-only (MappingProxyType)

@dataclass(frozen=True)
class Period:
    start: date
    end: date                     # inclusive; start <= end

@dataclass(frozen=True)
class ResolvedPeriod:
    period: Period                # after clipping
    requested_start: date
    clipped: bool                 # True when start was moved to the earliest record

@dataclass(frozen=True)
class Coverage:
    earliest_record: date | None  # whole history of the domain, not only the period
    latest_record: date | None
    documented_days: int          # distinct dates with >= 1 live (not soft-deleted) row
    total_days: int               # calendar days in the period
    ratio: float                  # documented_days / total_days, 0.0 when total_days == 0
    undocumented_ranges: tuple[tuple[date, date], ...]   # <= 10, oldest first
    truncated: bool

@dataclass(frozen=True)
class DailyPoint:
    day: date
    value: float | None           # None when not documented; never 0 for a missing day
    documented: bool
    record_ids: tuple[int, ...]

@dataclass(frozen=True)
class RecordRef:
    day: date
    value: float
    record_id: int

@dataclass(frozen=True)
class Group:
    key: str                      # subtype value, ISO week "2026-W41", or month "2026-10"
    n: int
    documented_days: int
    mean: float | None            # daily_value / event_value
    count: int | None             # event_count
    rate_per_documented_day: float | None
    pct: float | None             # ratio

@dataclass(frozen=True)
class MetricSummary:
    metric: str
    period: ResolvedPeriod
    coverage: Coverage
    n_observations: int
    mean: float | None; median: float | None; min: float | None; max: float | None; sum: float | None
    min_record: RecordRef | None
    max_record: RecordRef | None
    first: RecordRef | None
    last: RecordRef | None
    change: float | None          # last - first, weight only, n >= 2
    rate_per_documented_day: float | None     # event kinds
    ratio_numerator: int | None; ratio_denominator: int | None; ratio_pct: float | None   # ratio kind
    groups: tuple[Group, ...]     # <= 20, empty when group_by == "none"
    groups_truncated: bool
    latest_overall: RecordRef | None          # most recent value in all history (dashboard tile)
    calorie_complete_days: int | None         # calories.daily_total only
    calorie_partial_days: int | None

def resolve_period(conn, user_id: int, metric_id: str, start: date, end: date, *, today: date) -> ResolvedPeriod
def coverage(conn, user_id: int, domain: Domain, period: Period) -> Coverage
def daily_points(conn, user_id: int, metric_id: str, period: Period,
                 filters: Mapping[str, str] | None = None) -> list[DailyPoint]   # one per calendar day
def summarize(conn, user_id: int, metric_id: str, period: Period, *,
              group_by: Literal["none", "subtype", "week", "month"] = "none",
              filters: Mapping[str, str] | None = None) -> MetricSummary
```

**Rules every function keeps:**
- `conn` comes from `app.db.connect`. The dashboard passes a normal connection; the tools pass `read_only=True`.
- `user_id` is the first SQL parameter source. It always comes from the server-side session and is never a tool argument.
- **Fixed SQL only:** module-level constant strings, picked by enum key, with values bound by `?`. No f-strings, `%` or `.format` near SQL.
- Soft-deleted rows (`deleted_at IS NOT NULL`) are excluded.
- **Documented day, per domain:** a date with at least one live row. For `calories.daily_total`, a date with a non-null total in `daily_calories`; `calorie_complete_days` and `calorie_partial_days` split those days (None for other metrics).
- An explicit recorded `0` is a value; a missing day is `None`. There is no NaN and no division by zero; empty input gives `None` statistics and `n_observations == 0`.
- **Tasks:** `tasks.completion_rate` counts only `task_date <= today`. Other metrics' ranges must satisfy `end <= today`.
- **Validation:** an unknown metric id, an unknown filter, or a filter value outside `allowed_filters` raises `MetricError(code, message)`. So does an invalid range: `start > end`, `end > today`, or a span over 366 days.
- **Pure computation:** the module does no logging of values and no network access.

**Analytics-only computations stay in B-13.** These are trend OLS, Pearson, condition splits, period differences and `comparable`, all built from `daily_points` and `summarize`. The policy `assessment` flags also stay there. B-05 provides the registry and per-period facts only.

## 5. Merge protocol
1. **The analytics worker finishes a task:**
   - Coder, then its own independent Verifier PASS on the branch;
   - ruff, `mypy --strict` and the full suite green;
   - secret scan;
   - push `feature/analytics-b13`.
2. **It hands off to the integration lead** with:
   - the branch SHA;
   - the files changed;
   - test counts;
   - the Verifier report;
   - any patch to integration-owned files (§2);
   - an update to `docs/analytics/worker-notes.md`.
3. **The integration lead integrates:**
   - merges into a local integration branch from current `main`, resolving any conflict without dropping either side's work;
   - runs the full suite and an **integration Verifier** (including Playwright when UI changed);
   - updates the planning docs;
   - commits and pushes `main`;
   - reports the SHA.
4. **The analytics worker then syncs:** it rebases on, or merges, the new `main` before its next task.

No work goes to `main` without a passing integration verification.
