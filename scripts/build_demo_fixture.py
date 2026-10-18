"""Generate the synthetic Demo Mode fixture: python scripts/build_demo_fixture.py [--check]

Writes demo/synthetic_v1.json and demo/synthetic_v1.sha256. Standard library only, seeded
`random`, fixed key order: the output is byte-for-byte reproducible, so a reviewer can read
this file to see exactly how the data is made, and `--check` verifies that the committed files
match it (exit code 1 if not).

The person and every name are invented. Dates are day offsets from the day the demo copy is
built (day 0 = today): history is days −29…0 (30 days), tasks also run up to +14. Because the
weekday of each offset changes with the build date, nothing follows a weekly schedule: events
fall on random days inside each 7-day block, and the sleep/step noise and the workouts are
balanced across `day mod 7` classes, so no weekday pattern can appear for any build date.

Story (Owner's dataset spec, 2026-10-18), with the 7-day blocks W1 = −29…−23, W2 = −22…−16,
W3 = −15…−9, W4 = −8…−2, followed by days −1 and 0:
- Healthy meals, 2–3 per logged day, with calorie estimates (a few unknown).
- Aerobic and weights workouts (one bodyweight session), 3.5 a week on average.
- Friends met twice a week in W1–W2 and not at all from W3 on (task days stay logged).
- Job interviews rise week by week (W1–W4: 0, 1, 2, 4), each a completed career task
  "Interview — <company>", matching the job applications' status and interview stage.
- Family 3–4 times a week; regular chores; dog care (category "other").
- Sleep dips in the interview-heavy second half; steps are higher on aerobic days; sleep and
  the next day's steps are unrelated; weight is flat (about two weigh-ins a week).
- About 10 % of days are missing per domain (forgotten logging, never stored as zero); day −17
  has no sleep, steps or meals at all.

Checked by tests/integration/test_demo_fixture.py with tests/support/reference.py.
The dataset is a Coder draft (O-8): `approved_by_owner` stays false until the Owner approves it.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "demo" / "synthetic_v1.json"
OUT_SHA = ROOT / "demo" / "synthetic_v1.sha256"
SEED = 20261018

HISTORY = list(range(-29, 1))
WEEKS = [range(-29, -22), range(-22, -15), range(-15, -8), range(-8, -1)]  # W1…W4; days −1, 0 follow
FIRST_HALF = range(-29, -14)  # first 15 days; the last 15 are −14…0
FORGOTTEN_DAY = -17  # no sleep, steps or meals logged at all

Record = dict[str, Any]


def _balanced_noise(rng: random.Random, days: list[int], step: float) -> dict[int, float]:
    """Noise per day whose mean is exactly 0 within every `day mod 7` class (no weekday pattern)."""
    noise: dict[int, float] = {}
    for c in range(7):
        members = [d for d in days if d % 7 == c]
        values = [(i - (len(members) - 1) / 2) * step for i in range(len(members))]
        rng.shuffle(values)
        noise.update(zip(members, values, strict=True))
    return noise


def _missing(rng: random.Random, candidates: list[int], extra: int, always: tuple[int, ...] = ()) -> set[int]:
    """Days left unlogged: `always` plus `extra` random ones (≈ 10 % of 30 days in total)."""
    return set(always) | set(rng.sample([d for d in candidates if d not in always], extra))


# ---------------------------------------------------------------- health
def _workouts(rng: random.Random) -> list[Record]:
    # One aerobic and one weights session in every `day mod 7` class, plus one bodyweight session.
    days = [d for d in HISTORY if d != FORGOTTEN_DAY]
    plan: list[tuple[int, str]] = []
    for c in range(7):
        aerobic, weights = rng.sample([d for d in days if d % 7 == c], 2)
        plan += [(aerobic, "aerobic"), (weights, "weights")]
    used = {d for d, _ in plan}
    plan.append((rng.choice([d for d in days if d not in used]), "bodyweight"))
    notes = {"aerobic": ["Morning run", "Brisk walk and jog", "Cycling", "Interval run"],
             "weights": ["Upper body", "Lower body", "Full body"], "bodyweight": ["Home circuit"]}
    return [{"day": d, "workout_type": kind, "intensity": None,
             "duration_minutes": rng.choice([30, 35, 40, 45, 50]), "description": rng.choice(notes[kind])}
            for d, kind in sorted(plan)]


def _sleep(rng: random.Random) -> list[Record]:
    missing = _missing(rng, HISTORY, 2, (FORGOTTEN_DAY,))
    days = [d for d in HISTORY if d not in missing]
    noise = _balanced_noise(rng, days, 0.25)
    base = {d: 7.4 if d in FIRST_HALF else 6.6 for d in days}  # interview-heavy weeks: shorter nights
    # A `day mod 7` class with more first-half days would sleep longer on average (a weekday
    # pattern for some build dates), so each class is shifted to the overall mean.
    overall = sum(base.values()) / len(base)
    shift = {}
    for c in range(7):
        members = [base[d] for d in days if d % 7 == c]
        shift[c] = overall - sum(members) / len(members)
    return [{"day": d, "hours_slept": round((base[d] + shift[d % 7] + noise[d]) * 4) / 4}  # quarter hours
            for d in days]


def _steps(rng: random.Random, workouts: list[Record]) -> list[Record]:
    aerobic = {w["day"] for w in workouts if w["workout_type"] == "aerobic"}
    missing = _missing(rng, [d for d in HISTORY if d not in aerobic], 2, (FORGOTTEN_DAY,))
    days = [d for d in HISTORY if d not in missing]
    noise = _balanced_noise(rng, days, 400)
    return [{"day": d, "steps_count": int(round(7200 + noise[d] + (3200 if d in aerobic else 0), -1))}
            for d in days]


def _weight(rng: random.Random) -> list[Record]:
    days = sorted(d for week in WEEKS for d in rng.sample(list(week), 2))  # about twice a week
    days.append(rng.choice([-1, 0]))
    days.remove(rng.choice(days[1:-1]))  # one forgotten weigh-in
    return [{"day": d, "weight_kg": round(72.4 + rng.choice([-0.2, -0.1, 0.0, 0.1, 0.2]), 1)} for d in days]


MEALS = {
    "breakfast": ["Oatmeal with berries and nuts", "Greek yogurt with fruit", "Whole-grain toast with avocado",
                  "Vegetable omelette", "Cottage cheese and tomatoes"],
    "lunch": ["Quinoa salad with chickpeas", "Lentil soup with a slice of bread", "Grilled chicken salad",
              "Brown rice bowl with tofu", "Tuna and vegetable wrap"],
    "dinner": ["Baked salmon with vegetables", "Chicken breast with sweet potato", "Vegetable stir-fry with tofu",
               "Turkey meatballs with salad", "Shakshuka with whole-grain bread"],
}
KCAL = {"breakfast": (280, 420), "lunch": (420, 620), "dinner": (450, 680)}
NOTES = {"reference_based_estimate": "Synthetic estimate from typical portion sizes",
         "rough_estimate": "Synthetic rough estimate", "unknown": "Synthetic: portion not described"}


def _meals(rng: random.Random) -> list[Record]:
    missing = _missing(rng, HISTORY, 2, (FORGOTTEN_DAY,))
    days = [d for d in HISTORY if d not in missing]
    unknown_days = set(rng.sample(days, 4))  # a few meals whose calories are unknown
    out = []
    for d in days:
        kinds = ["breakfast", "lunch", "dinner"]
        if rng.random() < 0.35:
            kinds.remove(rng.choice(kinds))
        for i, kind in enumerate(kinds):
            basis = "unknown" if d in unknown_days and i == len(kinds) - 1 else \
                rng.choices(["reference_based_estimate", "rough_estimate"], [3, 1])[0]
            low, high = KCAL[kind]
            out.append({"day": d, "meal_type": kind, "description": rng.choice(MEALS[kind]),
                        "calories": {"kcal": None if basis == "unknown" else rng.randrange(low, high + 1, 10),
                                     "basis": basis, "notes": NOTES[basis]}})
    return out


# ---------------------------------------------------------------- content
ARTICLES = [("Interview preparation: a practical checklist", "The Example Ledger", "news.example.com"),
            ("How much sleep do adults need?", "Sample Science Weekly", "science.example.org"),
            ("Five easy high-protein breakfasts", "Demo Daily", None),
            ("Writing SQL that is easy to review", "Placeholder Tech Notes", "blog.example.net"),
            ("Walking more without trying harder", "Sample Science Weekly", "science.example.org"),
            ("Salary negotiation basics", "The Example Ledger", "news.example.com"),
            ("Training a puppy to walk on a loose leash", "Demo Daily", None),
            ("Strength training twice a week: what changes", "Sample Science Weekly", "science.example.org"),
            ("Building a data portfolio", "Placeholder Tech Notes", "blog.example.net"),
            ("Meal prep for a busy week", "Demo Daily", None),
            ("Answering behavioural interview questions", "The Example Ledger", "news.example.com"),
            ("Dashboards that people actually read", "Placeholder Tech Notes", "blog.example.net")]
PODCASTS = ["Quiet Variables", "The Lantern Hour", "Field Notes on Focus"]
EPISODES = ["Starting small", "Measuring what matters", "Rest and recovery", "Habits that stick",
            "Working with data", "Preparing for interviews", "Saying no", "Long walks"]
MOVIES = [("Paper Moons Over Kestrel", 2018), ("A Winter in Vellmar", 2021)]
SERIES = [("Harbor of Glass", 1, 6), ("Starfall Station", 2, 4)]


def _slug(text: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    return "-".join(words)


def _articles(rng: random.Random) -> list[Record]:
    days = sorted(rng.choice(HISTORY) for _ in ARTICLES)
    return [{"day": d, "title": t, "source": s, "url": f"https://{h}/articles/{_slug(t)}" if h else None}
            for d, (t, s, h) in zip(days, ARTICLES, strict=True)]


def _podcasts(rng: random.Random) -> list[Record]:
    days = sorted(rng.sample(HISTORY, 8))
    numbers = dict.fromkeys(PODCASTS, 40)
    out = []
    for d, topic in zip(days, rng.sample(EPISODES, 8), strict=True):
        show = rng.choice(PODCASTS)
        numbers[show] += 1
        out.append({"day": d, "podcast_name": show, "episode_title": f"Episode {numbers[show]}: {topic}",
                    "duration_minutes": rng.choice([28, 35, 42, 47, 55])})
    return out


def _movies(rng: random.Random) -> list[Record]:
    days = sorted(rng.sample(HISTORY, len(MOVIES)))
    return [{"day": d, "title": t, "release_year": y} for d, (t, y) in zip(days, MOVIES, strict=True)]


def _series(rng: random.Random) -> list[Record]:
    out = []
    for name, season, episodes in SERIES:
        days = sorted(rng.sample(HISTORY, episodes))
        out += [{"day": d, "series_name": name, "season_number": season, "episode_number": i + 1}
                for i, d in enumerate(days)]
    return sorted(out, key=lambda r: (r["day"], r["series_name"]))


# ---------------------------------------------------------------- jobs and interviews
# (company, title, location, work model, industry, applied on day, current status)
APPLICATIONS = [
    ("Bluefern Labs", "Data Analyst", "Tel Aviv", "hybrid", "Software", -28, "rejected"),
    ("Copperleaf Health", "BI Developer", "Jerusalem", "hybrid", "Healthcare technology", -27, "accepted"),
    ("Wrenfield Finance", "Data Analyst", "Tel Aviv", "hybrid", "Fintech", -24, "interviewing"),
    ("Quillstone Systems", "Backend Developer", "Haifa", "onsite", "Software", -22, "rejected"),
    ("Granite Peak Software", "Junior Data Scientist", "Remote", "remote", "Software", -20, "rejected"),
    ("Lumenvale Media", "Product Analyst", "Tel Aviv", "hybrid", "Media", -16, "interviewing"),
    ("Harborlight Data", "Analytics Engineer", "Remote", "remote", "Fintech", -12, "applied"),
    ("Saltmarsh Energy", "Reporting Analyst", "Beersheba", "onsite", "Energy", -7, "applied"),
    ("Pinecrest Learning", "Learning Data Analyst", "Remote", "remote", "Education", -2, "applied"),
]
# Interviews held (day, company): W1 0, W2 1, W3 2, W4 4. Upcoming ones are pending tasks.
INTERVIEWS = [(-19, "Copperleaf Health"), (-13, "Wrenfield Finance"), (-10, "Copperleaf Health"),
              (-8, "Granite Peak Software"), (-6, "Wrenfield Finance"), (-4, "Lumenvale Media"),
              (-3, "Copperleaf Health")]
UPCOMING_INTERVIEWS = [(4, "Lumenvale Media"), (6, "Wrenfield Finance")]


def _jobs() -> list[Record]:
    out = []
    for company, title, location, model, industry, day, status in APPLICATIONS:
        held = sum(1 for _, c in INTERVIEWS if c == company)  # interview_stage = interviews held so far
        out.append({"day": day, "company_name": company, "job_title": title, "company_location": location,
                    "work_model": model, "industry": industry, "status": status,
                    "interview_stage": held or None,
                    "job_url": f"https://jobs.example.com/postings/{_slug(company + ' ' + title)}"})
    return out


# ---------------------------------------------------------------- tasks
FRIENDS = ["Dinner with friends", "Coffee with a friend", "Board game night", "Hike with friends"]
FAMILY = ["Family dinner", "Call parents", "Visit grandparents", "Help sibling with moving boxes",
          "Lunch with family", "Video call with cousins"]
CHORES = ["Laundry", "Clean the kitchen", "Grocery shopping", "Vacuum the flat", "Take out recycling"]
CAREER = ["Update CV", "Practice SQL exercises", "Prepare interview notes", "Write a cover letter"]


def _time(rng: random.Random, low: int, high: int) -> str:
    return f"{rng.randint(low, high):02d}:{rng.choice(['00', '15', '30', '45'])}"


def _tasks(rng: random.Random) -> list[Record]:
    out: list[Record] = []
    seen: set[tuple[int, str]] = set()

    def add(day: int, category: str, titles: list[str], status: str, start: str | None) -> None:
        free = [t for t in titles if (day, t) not in seen]  # the same title twice a day is a duplicate
        title = rng.choice(free)
        seen.add((day, title))
        out.append({"day": day, "start_time": start, "title": title, "description": None, "category": category,
                    "status": status})

    interview_days = {d for d, _ in INTERVIEWS}
    friend_days = [d for week in WEEKS[:2] for d in rng.sample([x for x in week if x not in interview_days], 2)]
    unlogged = set(rng.sample([d for d in HISTORY if d not in interview_days and d not in friend_days], 3))
    for d, company in INTERVIEWS:
        add(d, "career", [f"Interview — {company}"], "completed", _time(rng, 9, 16))
    for d in friend_days:
        add(d, "friends", FRIENDS, "completed", _time(rng, 18, 21))
    for i, block in enumerate([*WEEKS, range(-1, 1)]):  # family: 4, 3, 4, 3 a week, then 1 in the last 2 days
        for d in rng.sample([x for x in block if x not in unlogged], [4, 3, 4, 3, 1][i]):
            add(d, "family", FAMILY, "completed", _time(rng, 17, 20))
    for d in HISTORY:
        if d in unlogged:
            continue
        add(d, "other", ["Walk the dog"], "completed" if rng.random() < 0.95 else "pending", _time(rng, 6, 8))
    for i, block in enumerate([*WEEKS, range(-1, 1)]):  # chores: 3 a week, then 1 in the last 2 days
        for d in rng.sample([x for x in block if x not in unlogged], [3, 3, 3, 3, 1][i]):
            add(d, "housework", CHORES, "completed" if rng.random() < 0.75 else "pending", None)
    for d in rng.sample([d for d in HISTORY if d not in unlogged and d not in interview_days], 6):
        add(d, "career", CAREER, "completed", _time(rng, 10, 15))
    logged = [d for d in HISTORY if d not in unlogged]
    add(rng.choice(logged), "other", ["Vet appointment"], "completed", _time(rng, 10, 16))
    add(rng.choice(logged), "other", ["Buy dog food"], "completed", None)
    # The next two weeks: pending.
    for d, company in UPCOMING_INTERVIEWS:
        add(d, "career", [f"Interview — {company}"], "pending", _time(rng, 9, 16))
    for d in rng.sample(range(1, 15), 6):
        add(d, "family", FAMILY, "pending", _time(rng, 17, 20))
    for d in rng.sample(range(1, 15), 5):
        add(d, "housework", CHORES, "pending", None)
    add(rng.randint(8, 14), "other", ["Vet appointment"], "pending", _time(rng, 10, 16))
    add(rng.randint(1, 14), "other", ["Brush the dog"], "pending", None)
    return sorted(out, key=lambda t: (t["day"], t["start_time"] or "99:99", t["title"]))


def build() -> dict[str, Any]:
    """The fixture as a dict (deterministic for a given SEED)."""
    rng = random.Random(SEED)  # noqa: S311 - reproducible synthetic data, not security
    workouts = _workouts(rng)
    records: dict[str, list[Record]] = {
        "weight_records": _weight(rng),
        "sleep_records": _sleep(rng),
        "step_records": _steps(rng, workouts),
        "meals": _meals(rng),
        "workouts": workouts,
        "articles": _articles(rng),
        "podcasts": _podcasts(rng),
        "movies": _movies(rng),
        "series": _series(rng),
        "job_applications": _jobs(),
        "tasks": _tasks(rng),
    }
    counts = {name: len(rows) for name, rows in records.items()}
    counts["meal_calorie_estimates"] = counts["meals"]
    return {
        "fixture_version": 1,
        "synthetic": True,
        "approved_by_owner": False,
        "description": ("Synthetic Demo Mode data: 30 days of invented records for one fictional person, "
                        "generated by scripts/build_demo_fixture.py. Day offsets are relative to the day the "
                        "demo copy is built. Draft prepared by the Coder; not yet approved by the Owner."),
        "counts": counts,
        "records": records,
    }


def render(data: dict[str, Any]) -> bytes:
    """Stable JSON bytes: ASCII only, LF line endings, one trailing newline."""
    return (json.dumps(data, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def checksum_line(raw: bytes) -> bytes:
    """`sha256sum`-compatible line for the fixture."""
    return f"{hashlib.sha256(raw).hexdigest()}  {OUT_JSON.name}\n".encode("ascii")


def main(argv: list[str]) -> int:
    raw = render(build())
    if "--check" in argv:
        same = OUT_JSON.is_file() and OUT_JSON.read_bytes() == raw and OUT_SHA.is_file() and \
            OUT_SHA.read_bytes() == checksum_line(raw)
        print("fixture is up to date" if same else "fixture differs from the generator output")
        return 0 if same else 1
    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_bytes(raw)
    OUT_SHA.write_bytes(checksum_line(raw))
    print(f"wrote {OUT_JSON.relative_to(ROOT)} ({len(raw)} bytes) and {OUT_SHA.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
