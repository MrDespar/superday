"""Interview countdown planning.

The question a study plan has to answer is not "how many questions are there"
but "if the superday is on the 15th, what do I have to do tomorrow". That means
two competing demands on the same finite evenings:

  - **new coverage**: questions never seen, which must all be seen at least
    once before the date or they are not preparation, they are decoration.
  - **overdue reviews**: questions already seen whose recall window has come
    round, which is the only thing that makes the first pass stick.

Reviews win when they collide. Seeing 800 questions once and remembering none
of them is the failure mode this whole tool exists to avoid, so new material is
what gets squeezed when the days run short -- and when it has to be squeezed
past the point of fitting, the plan says so out loud rather than quietly
producing a number nobody can hit.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import analytics

# What a day of drilling actually costs. Measured from the bank's own review
# history when there is enough of it, and otherwise assumed.
DEFAULT_SECONDS_PER_QUESTION = 75
SANE_DAILY_CEILING = 120        # beyond this a "plan" is a fantasy


def parse_target(raw: str, today: date | None = None) -> date | None:
    """Accept `2026-09-15`, `+14d`, `2 weeks`, `sep 15`, `tomorrow`."""
    today = today or datetime.now(timezone.utc).date()
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in ("today",):
        return today
    if text in ("tomorrow",):
        return today + timedelta(days=1)

    m = re.fullmatch(r"\+?(\d+)\s*(d|days?|w|weeks?|m|months?)", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        return today + timedelta(days=n * {"d": 1, "w": 7, "m": 30}[unit])

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%b %d %Y", "%b %d", "%d %b"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:                       # "sep 15" means the next one
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed
    return None


def target_date(today: date | None = None) -> date | None:
    """The interview date on file, or None if there is not one.

    One reader, so `plan`, the dashboard countdown and the Upcoming pane
    cannot disagree about whether a date is set. A date that has been and gone
    is not a target any more and reads as None: a countdown of -3 days is
    worse than no countdown.
    """
    from . import config
    raw = (config.load().get("interview_date") or "").strip()
    if not raw:
        return None
    parsed = parse_target(raw, today)
    today = today or datetime.now(timezone.utc).date()
    return parsed if parsed and parsed > today else None


def seconds_per_question(conn: sqlite3.Connection) -> int:
    """How long a question actually takes you, from your own sittings.

    A plan built on a guessed pace is a plan for somebody else. Sittings record
    real per-question seconds; once there are enough of them, they are the
    honest estimate.
    """
    row = conn.execute(
        "SELECT done_json FROM sessions WHERE kind = 'drill' ORDER BY started_at DESC LIMIT 20"
    ).fetchall()
    times: list[float] = []
    for r in row:
        for item in json.loads(r["done_json"] or "[]"):
            secs = item.get("seconds")
            # Sub-second entries are a piped test run, not a person thinking.
            if isinstance(secs, (int, float)) and 3 <= secs <= 600:
                times.append(float(secs))
    if len(times) < 10:
        return DEFAULT_SECONDS_PER_QUESTION
    times.sort()
    return int(times[len(times) // 2])            # median, so one long think does not skew it


def build(conn: sqlite3.Connection, target: date, minutes_per_day: int | None = None) -> dict:
    """The whole plan as data: totals, daily pace, per-topic split, verdict."""
    today = datetime.now(timezone.utc).date()
    days = (target - today).days
    c = analytics.counts(conn)
    topics = analytics.topic_mastery(conn)

    unseen = c["unseen"]
    backlog = max(0, c["due_now"] - unseen)       # seen, and already come round
    pace = seconds_per_question(conn)

    # Every unseen question needs its first pass, and each first pass earns
    # roughly this many follow-up reviews before the date. FSRS intervals grow,
    # so the count is logarithmic in the days remaining rather than linear:
    # a question first seen 30 days out comes back about 4 times, not 30.
    reviews_per_new = max(1.0, math.log2(max(days, 1)) - 0.5) if days > 1 else 1.0
    total_reviews = unseen * reviews_per_new + backlog
    total_touches = unseen + total_reviews

    daily_new = math.ceil(unseen / days) if days > 0 and unseen else 0
    daily_total = math.ceil(total_touches / days) if days > 0 else total_touches
    daily_reviews = max(0, daily_total - daily_new)

    minutes_needed = round(daily_total * pace / 60)
    feasible = daily_total <= SANE_DAILY_CEILING
    if minutes_per_day:
        capacity = int(minutes_per_day * 60 / pace)
        feasible = daily_total <= capacity
    else:
        capacity = None

    # If it does not fit, what does fit -- ranked by where the bank says the
    # marks are. A plan that cannot be followed should degrade into a triage
    # list, not into a bigger number.
    triage: list[dict] = []
    reachable = unseen
    if not feasible:
        budget = (capacity or SANE_DAILY_CEILING) * days
        spent = backlog
        reachable = 0
        for t in sorted(topics, key=_topic_priority):
            room = max(0, int((budget - spent) / max(reviews_per_new + 1, 1)))
            take = min(t["unseen"], room)
            triage.append({"topic": t["topic"], "take": take, "of": t["unseen"],
                           "dropped": t["unseen"] - take})
            reachable += take
            spent += take * (reviews_per_new + 1)

    per_topic = []
    for t in topics:
        per_topic.append({
            "topic": t["topic"],
            "active": t["active"],
            "unseen": t["unseen"],
            "due": t["due"],
            "coverage": t["coverage"],
            "avg_rating": t["avg_rating"],
            "daily_new": math.ceil(t["unseen"] / days) if days > 0 and t["unseen"] else 0,
            "priority": _topic_priority(t),
        })
    per_topic.sort(key=lambda d: d["priority"])

    return {
        "today": today.isoformat(),
        "target": target.isoformat(),
        "days": days,
        "active": c["active"],
        "unseen": unseen,
        "backlog": backlog,
        "needs_qa": c["needs_review"],
        "seconds_per_question": pace,
        "reviews_per_new": round(reviews_per_new, 1),
        "daily_new": daily_new,
        "daily_reviews": daily_reviews,
        "daily_total": daily_total,
        "minutes_per_day": minutes_needed,
        "capacity_per_day": capacity,
        "feasible": feasible,
        "triage": triage,
        "reachable": reachable,
        "unreachable": max(0, unseen - reachable),
        "sustainable_daily": capacity or SANE_DAILY_CEILING,
        "topics": per_topic,
        "readiness": analytics.readiness(conn),
    }


def _topic_priority(t: dict) -> tuple:
    """Which topic to spend the next hour on.

    Weak-and-unfinished first, then big-and-untouched, then everything else.
    A topic you are scoring 1.2/4 on is a hole; a topic you have never opened
    is a risk; a topic at 3.8/4 with full coverage is done.
    """
    mastery = analytics.mastery_frac(t.get("avg_rating"))
    if mastery is not None and mastery < 0.5 and t["coverage"] < 1.0:
        return (0, mastery, -t["active"])
    if t["coverage"] == 0.0:
        return (1, 0.0, -t["active"])
    if t["unseen"]:
        return (2, mastery if mastery is not None else 1.0, -t["unseen"])
    return (3, mastery if mastery is not None else 1.0, 0)


def calendar(conn: sqlite3.Connection, plan: dict, days_shown: int = 14) -> list[dict]:
    """Day by day: how many new, how many reviews, how long it takes.

    The load is front-weighted. Reviews of things learned in week one land in
    week two, so a flat plan under-books the start and over-books the end --
    exactly the shape that has you cramming the night before.
    """
    total_days = plan["days"]
    out = []
    remaining_new = plan["unseen"]
    for i in range(min(days_shown, total_days)):
        day = date.fromisoformat(plan["today"]) + timedelta(days=i)
        left_days = total_days - i
        new_today = min(remaining_new, math.ceil(remaining_new / max(left_days, 1)))
        remaining_new -= new_today
        # Reviews ramp as material accumulates behind you.
        learned = plan["unseen"] - remaining_new
        reviews = int(min(learned * 0.35, plan["daily_total"] - new_today))
        if i == 0:
            reviews += plan["backlog"]
        out.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%a"),
            "new": new_today,
            "reviews": max(0, reviews),
            "minutes": round((new_today + max(0, reviews)) * plan["seconds_per_question"] / 60),
        })
    return out
