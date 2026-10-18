"""Demo Mode: synthetic fixture, working copies, mode exclusivity (ADR-005 §6–§7, B-14).

Demo Mode lets a reviewer explore the app with synthetic data only: no Google account, no
OpenAI key, no personal database. The pieces:

- **Fixture** (`demo/synthetic_v1.json`, committed, read-only). It is opened only with
  `open(path, "rb")`, its SHA-256 must equal `demo/synthetic_v1.sha256`, and it is validated
  by `Fixture` (strict, `extra="forbid"`). Record dates are day offsets from an anchor (today
  in Asia/Jerusalem when the copy is built): `date = anchor + day`, `day <= 0`, tasks up to +14.
- **Working copy** (`data/demo/working-<generation>.db`, disposable). `build_working_copy`
  creates it through `app.db.connect` → `migrate` → `stamp_demo_application_id` → the demo
  user via `get_or_create_user` → every record through the B-04 repository `create`
  functions in one transaction (no raw INSERTs) → integrity and foreign-key checks.
- **Exclusivity** (content-based): a demo copy carries `DEMO_APPLICATION_ID` and only
  `demo:` users. `assert_demo_database` refuses anything else in demo mode;
  `assert_personal_database` / `assert_personal_path` refuse demo data in personal mode.
- **Cleanup** (`remove_old_copies`) deletes only files directly under a `data/demo/`
  directory, named `working-<n>.db` (with that db's `-wal`/`-shm`), whose SQLite header
  carries the demo application_id. Nothing else is ever deleted.
- **DemoRuntime** holds the current copy for the web app: fresh build on every start (O-4),
  `reset()` builds a new copy and switches to it under a lock, bumping the generation so
  older demo sessions stop working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.auth import DEMO_SUB_PREFIX
from app.db import (
    DEMO_APPLICATION_ID,
    application_id,
    connect,
    migrate,
    stamp_demo_application_id,
    transaction,
)
from app.records import (
    articles,
    job_applications,
    meals,
    movies,
    podcasts,
    series,
    sleep,
    steps,
    tasks,
    weight,
    workouts,
)
from app.users import get_or_create_user

log = logging.getLogger("app.demo")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "demo" / "synthetic_v1.json"
CHECKSUM_PATH = REPO_ROOT / "demo" / "synthetic_v1.sha256"
DEMO_DIR = Path("data") / "demo"
"""Default location of working copies, relative to the working directory (gitignored)."""
DEMO_SUB = DEMO_SUB_PREFIX + "reviewer"
DEMO_EMAIL = "reviewer@demo.invalid"  # .invalid is reserved and can never be a real mailbox
DEMO_NAME = "Demo Reviewer"
ESTIMATE_MODEL = "synthetic-fixture"
_COPY_NAME = re.compile(r"^working-(\d{1,30})\.db$")
_SQLITE_MAGIC = b"SQLite format 3\x00"


class DemoError(RuntimeError):
    """Demo Mode cannot start or continue (bad fixture, wrong database, failed build)."""


# ---------------------------------------------------------------- fixture schema (ADR-005 §7)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


Day = Annotated[int, Field(ge=-29, le=0)]  # 30 days of history (Owner dataset spec, 2026-10-18)
TaskDay = Annotated[int, Field(ge=-29, le=14)]


class WeightItem(_Strict):
    day: Day
    weight_kg: float


class SleepItem(_Strict):
    day: Day
    hours_slept: float


class StepsItem(_Strict):
    day: Day
    steps_count: int


class Calories(_Strict):
    kcal: int | None
    basis: meals.CalorieBasis
    notes: str | None


class MealItem(_Strict):
    day: Day
    meal_type: meals.MealType
    description: str
    calories: Calories


class WorkoutItem(_Strict):
    day: Day
    workout_type: workouts.WorkoutType
    intensity: workouts.Intensity | None
    duration_minutes: int | None
    description: str | None


class ArticleItem(_Strict):
    day: Day
    title: str
    source: str | None
    url: str | None


class PodcastItem(_Strict):
    day: Day
    podcast_name: str
    episode_title: str | None
    duration_minutes: int | None


class MovieItem(_Strict):
    day: Day
    title: str
    release_year: int | None


class SeriesItem(_Strict):
    day: Day
    series_name: str
    season_number: int | None
    episode_number: int | None


class JobItem(_Strict):
    day: Day
    company_name: str
    job_title: str
    company_location: str
    work_model: job_applications.WorkModel
    industry: str
    status: job_applications.Status
    interview_stage: int | None
    job_url: str | None


class TaskItem(_Strict):
    day: TaskDay
    start_time: str | None
    title: str
    description: str | None
    category: tasks.Category
    status: tasks.Status


class Records(_Strict):
    weight_records: list[WeightItem]
    sleep_records: list[SleepItem]
    step_records: list[StepsItem]
    meals: list[MealItem]
    workouts: list[WorkoutItem]
    articles: list[ArticleItem]
    podcasts: list[PodcastItem]
    movies: list[MovieItem]
    series: list[SeriesItem]
    job_applications: list[JobItem]
    tasks: list[TaskItem]


TABLES = tuple(Records.model_fields)
"""Fixture sections, named after their tables."""


class Fixture(_Strict):
    """The whole fixture file. `counts` is the manifest: rows per table (plus calorie estimates)."""

    fixture_version: Literal[1]
    synthetic: Literal[True]
    approved_by_owner: Literal[False] | Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    description: str
    counts: dict[str, int]
    records: Records

    @model_validator(mode="after")
    def _counts_match(self) -> Fixture:
        actual = record_counts(self)
        if self.counts != actual:
            raise ValueError("the manifest counts do not match the records")
        return self


def record_counts(fixture: Fixture) -> dict[str, int]:
    """Rows per table that importing `fixture` creates (meal_calorie_estimates: one per meal)."""
    counts = {t: len(getattr(fixture.records, t)) for t in TABLES}
    counts["meal_calorie_estimates"] = counts["meals"]
    return counts


def load_fixture(path: Path = FIXTURE_PATH, checksum_path: Path = CHECKSUM_PATH) -> Fixture:
    """Read (only `rb`), verify the SHA-256 and validate the fixture. Raises DemoError."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        with open(checksum_path, "rb") as f:
            expected = f.read().decode("ascii").split()[0].lower()
    except (OSError, UnicodeDecodeError, IndexError) as exc:
        raise DemoError(f"Demo fixture missing or unreadable ({type(exc).__name__}): {path.name}") from None
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise DemoError(f"Demo fixture checksum mismatch: {path.name} does not match {checksum_path.name}. "
                        "The fixture was changed; restore it (git checkout demo/) or regenerate it with "
                        "scripts/build_demo_fixture.py.")
    try:
        return Fixture.model_validate(json.loads(raw.decode("utf-8")))
    except (ValueError, ValidationError) as exc:
        problems = exc.errors(include_input=False) if isinstance(exc, ValidationError) else [{"msg": str(exc)}]
        raise DemoError(f"Demo fixture is invalid: {len(problems)} problem(s), first: {problems[0]['msg']}") from None


# ---------------------------------------------------------------- import (B-04 write paths only)
def _import(conn: sqlite3.Connection, user_id: int, fixture: Fixture, anchor: date) -> None:
    def on(day: int) -> date:
        return anchor + timedelta(days=day)

    r = fixture.records
    for w in r.weight_records:
        weight.create(conn, user_id, weight.WeightIn(record_date=on(w.day), weight_kg=w.weight_kg), today=anchor)
    for s in r.sleep_records:
        sleep.create(conn, user_id, sleep.SleepIn(record_date=on(s.day), hours_slept=s.hours_slept), today=anchor)
    for st in r.step_records:
        steps.create(conn, user_id, steps.StepsIn(record_date=on(st.day), steps_count=st.steps_count), today=anchor)
    for m in r.meals:
        estimate = meals.EstimateIn(estimated_calories=m.calories.kcal, calorie_basis=m.calories.basis,
                                    calorie_notes=m.calories.notes, model=ESTIMATE_MODEL)
        meals.create(conn, user_id, meals.MealIn(record_date=on(m.day), meal_type=m.meal_type,
                                                 description=m.description), today=anchor, estimate=estimate)
    for wo in r.workouts:
        workouts.create(conn, user_id, workouts.WorkoutIn(
            record_date=on(wo.day), workout_type=wo.workout_type, intensity=wo.intensity,
            duration_minutes=wo.duration_minutes, description=wo.description), today=anchor)
    for a in r.articles:
        articles.create(conn, user_id, articles.ArticleIn(record_date=on(a.day), title=a.title, source=a.source,
                                                          url=a.url), today=anchor)
    for p in r.podcasts:
        podcasts.create(conn, user_id, podcasts.PodcastIn(
            record_date=on(p.day), podcast_name=p.podcast_name, episode_title=p.episode_title,
            duration_minutes=p.duration_minutes), today=anchor)
    for mv in r.movies:
        movies.create(conn, user_id, movies.MovieIn(record_date=on(mv.day), title=mv.title,
                                                    release_year=mv.release_year), today=anchor)
    for se in r.series:
        series.create(conn, user_id, series.SeriesIn(
            record_date=on(se.day), series_name=se.series_name, season_number=se.season_number,
            episode_number=se.episode_number), today=anchor)
    for j in r.job_applications:
        job_applications.create(conn, user_id, job_applications.JobApplicationIn(
            application_date=on(j.day), company_name=j.company_name, job_title=j.job_title,
            company_location=j.company_location, work_model=j.work_model, industry=j.industry, status=j.status,
            interview_stage=j.interview_stage, job_url=j.job_url), today=anchor)
    for t in r.tasks:
        tasks.create(conn, user_id, tasks.TaskIn(
            task_date=on(t.day), start_time=t.start_time, title=t.title, description=t.description,
            category=t.category, status=t.status), today=anchor)


def _table_counts(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    # Table names come from the fixed TABLES tuple, never from input.
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id = ?", (user_id,)).fetchone()[0]  # noqa: S608
              for t in (*TABLES, "meal_calorie_estimates")}
    return counts


@dataclass(frozen=True)
class WorkingCopy:
    path: Path
    generation: int
    user_id: int
    anchor: date


def build_working_copy(demo_dir: Path, generation: int, anchor: date, *, fixture_path: Path = FIXTURE_PATH,
                       checksum_path: Path = CHECKSUM_PATH, forbidden: tuple[Path, ...] = ()) -> WorkingCopy:
    """Build `demo_dir/working-<generation>.db` from the fixture. Raises DemoError.

    `anchor` is the date that day 0 maps to. `forbidden` lists paths the copy must never be
    (e.g. the personal DATABASE_PATH). An existing file is never reused or overwritten.
    """
    fixture = load_fixture(fixture_path, checksum_path)  # verified before anything is written
    _check_demo_dir(demo_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)
    path = demo_dir / f"working-{generation}.db"
    if any(_same_file(path, other) for other in forbidden):
        raise DemoError("The demo working copy would be the personal database; refusing to start.")
    if any(Path(f"{path}{suffix}").exists() for suffix in ("", "-wal", "-shm")):
        raise DemoError(f"Demo working copy {path.name} already exists; refusing to overwrite it.")
    conn = connect(path)
    try:
        migrate(conn)
        stamp_demo_application_id(conn)
        user_id = get_or_create_user(conn, DEMO_SUB, DEMO_EMAIL, DEMO_NAME)
        with transaction(conn):
            _import(conn, user_id, fixture, anchor)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DemoError("Demo working copy failed integrity_check.")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise DemoError("Demo working copy failed foreign_key_check.")
        if _table_counts(conn, user_id) != fixture.counts:
            raise DemoError("Demo working copy row counts do not match the fixture manifest.")
        assert_demo_database(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the header (application_id) reaches the main file
    except Exception as exc:
        conn.close()
        remove_copy(path)  # only if it already carries the demo marker
        if isinstance(exc, DemoError):
            raise
        raise DemoError(f"Demo working copy could not be built ({type(exc).__name__}: {exc}).") from None
    conn.close()
    return WorkingCopy(path, generation, user_id, anchor)


# ---------------------------------------------------------------- mode exclusivity (ADR-005 §6.2)
def assert_demo_database(conn: sqlite3.Connection) -> None:
    """Demo mode opens only a demo copy: demo application_id and nothing but `demo:` users."""
    if application_id(conn) != DEMO_APPLICATION_ID:
        raise DemoError("This database is not a demo working copy; demo mode refuses to open it.")
    other = conn.execute("SELECT COUNT(*) FROM users WHERE substr(google_sub, 1, ?) <> ?",
                         (len(DEMO_SUB_PREFIX), DEMO_SUB_PREFIX)).fetchone()[0]
    if other:
        raise DemoError("This database has non-demo users; demo mode refuses to open it.")


def assert_personal_path(path: Path) -> None:
    """Personal mode never uses a file under a `data/demo/` directory (checked before opening)."""
    parts = [p.casefold() for p in path.resolve().parts]
    if any(a == "data" and b == "demo" for a, b in zip(parts, parts[1:], strict=False)):
        raise DemoError("DATABASE_PATH is inside data/demo/, which holds demo copies; personal mode refuses it.")


def assert_personal_database(conn: sqlite3.Connection) -> None:
    """Personal mode refuses a demo copy: the demo application_id or any `demo:` user."""
    if application_id(conn) == DEMO_APPLICATION_ID:
        raise DemoError("DATABASE_PATH is a demo working copy; personal mode refuses to open it.")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone() and \
            conn.execute("SELECT 1 FROM users WHERE google_sub LIKE ? LIMIT 1", (DEMO_SUB_PREFIX + "%",)).fetchone():
        raise DemoError("The database contains a demo identity; personal mode refuses to open it.")


# ---------------------------------------------------------------- cleanup (disposable copies only)
def _check_demo_dir(demo_dir: Path) -> None:
    resolved = demo_dir.resolve()
    if resolved.name.casefold() != "demo" or resolved.parent.name.casefold() != "data":
        raise DemoError("Demo working copies must live in a data/demo/ directory.")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


def is_demo_copy(path: Path) -> bool:
    """True if `path` is a regular SQLite file whose header carries the demo application_id.

    Reads the 100-byte header directly (no SQLite connection, so nothing is created or changed).
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(path, "rb") as f:
            header = f.read(100)
    except OSError:
        return False
    return len(header) == 100 and header.startswith(_SQLITE_MAGIC) and \
        int.from_bytes(header[68:72], "big") == DEMO_APPLICATION_ID


def remove_copy(path: Path) -> bool:
    """Delete one working copy and its -wal/-shm, only if every cleanup condition holds."""
    if not _COPY_NAME.match(path.name):
        return False
    try:
        _check_demo_dir(path.parent)
    except DemoError:
        return False
    if not is_demo_copy(path):
        return False
    try:
        for suffix in ("-wal", "-shm"):
            side = Path(f"{path}{suffix}")
            if side.is_file() and not side.is_symlink():
                side.unlink()
        path.unlink()
    except OSError as exc:  # e.g. still open on Windows: left for the next cleanup
        log.warning("demo copy %s not removed yet: %s", path.name, type(exc).__name__)
        return False
    return True


def remove_old_copies(demo_dir: Path, keep: Path | None = None) -> list[str]:
    """Delete stale demo working copies in `demo_dir` (not recursive). Returns the removed names."""
    if not demo_dir.is_dir():
        return []
    _check_demo_dir(demo_dir)
    removed = []
    for entry in sorted(demo_dir.iterdir()):
        if keep is not None and _same_file(entry, keep):
            continue
        if remove_copy(entry):
            removed.append(entry.name)
    return removed


# ---------------------------------------------------------------- runtime (web app state)
class DemoRuntime:
    """The current demo working copy of a running app; thread-safe switch on reset."""

    def __init__(self, demo_dir: Path, anchor: Callable[[], date], *, forbidden: tuple[Path, ...] = (),
                 fixture_path: Path = FIXTURE_PATH, checksum_path: Path = CHECKSUM_PATH) -> None:
        self.demo_dir = demo_dir
        self._anchor = anchor
        self._forbidden = forbidden
        self._fixture = (fixture_path, checksum_path)
        self._lock = threading.Lock()  # guards _current and file removal against new connections
        self._build_lock = threading.Lock()  # one build at a time
        self._current: WorkingCopy | None = None
        self._last_generation = 0

    def _next_generation(self) -> int:
        self._last_generation = max(time.time_ns(), self._last_generation + 1)
        return self._last_generation

    @property
    def generation(self) -> int | None:
        current = self._current
        return current.generation if current else None

    @property
    def user_id(self) -> int:
        current = self._current
        if current is None:
            raise DemoError("The demo working copy is not ready.")
        return current.user_id

    def start(self) -> WorkingCopy:
        """Remove stale copies, then build a fresh one (every app start, O-4)."""
        removed = remove_old_copies(self.demo_dir)
        if removed:
            log.info("removed %d old demo working copies", len(removed))
        return self.reset()

    def reset(self) -> WorkingCopy:
        """Build a new copy from the fixture, switch to it and remove the old one."""
        with self._build_lock:
            fixture_path, checksum_path = self._fixture
            copy = build_working_copy(self.demo_dir, self._next_generation(), self._anchor(),
                                      fixture_path=fixture_path, checksum_path=checksum_path,
                                      forbidden=self._forbidden)
            with self._lock:
                old, self._current = self._current, copy
                if old is not None:
                    remove_copy(old.path)
            log.info("demo working copy ready (generation %d)", copy.generation)
            return copy

    def connect(self) -> sqlite3.Connection:
        """A guarded connection to the current copy, after checking it is a demo copy."""
        with self._lock:
            current = self._current
            if current is None or not current.path.is_file():
                raise DemoError("The demo working copy is not available; restart the app.")
            conn = connect(current.path)
        try:
            assert_demo_database(conn)
        except BaseException:
            conn.close()
            raise
        return conn
