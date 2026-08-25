"""Readiness analytics: what the bank knows about how prepared you are.

Every number the dashboard, plan and stats screens quote comes from here, for
one reason: "due" has to mean the same thing everywhere. A question that has
never been scheduled is due -- you have never seen it -- and a dashboard that
counted only rows in `schedule` reported 13 due while the topic table under it
reported 700. Both were "right"; only one was useful.

Read-only. Nothing in here writes.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

# A question is due when it has no card yet, or its card has come round.
DUE = "(s.due_at IS NULL OR s.due_at <= :now)"
UNSEEN = "(s.due_at IS NULL)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def counts(conn: sqlite3.Connection) -> dict:
    """The headline numbers, all against one clock reading."""
    now = _now()
    row = conn.execute(
        f"""
        SELECT
          COUNT(*)                                                    AS active,
          SUM(CASE WHEN {UNSEEN} THEN 1 ELSE 0 END)                   AS unseen,
          SUM(CASE WHEN {DUE} THEN 1 ELSE 0 END)                      AS due_now,
          SUM(CASE WHEN s.due_at IS NOT NULL
                    AND s.due_at <= :d1 THEN 1 ELSE 0 END)            AS sched_24h,
          SUM(CASE WHEN s.due_at IS NOT NULL
                    AND s.due_at <= :d7 THEN 1 ELSE 0 END)            AS sched_7d
          FROM questions q LEFT JOIN schedule s ON s.question_id = q.id
         WHERE q.status = 'active'
        """,
        {
            "now": now,
            "d1": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "d7": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        },
    ).fetchone()

    by_status = {
        r["status"]: r["c"]
        for r in conn.execute("SELECT status, COUNT(*) c FROM questions GROUP BY status")
    }
    return {
        "active": row["active"] or 0,
        "unseen": row["unseen"] or 0,
        "due_now": row["due_now"] or 0,
        "due_24h": (row["sched_24h"] or 0) + (row["unseen"] or 0),
        "due_7d": (row["sched_7d"] or 0) + (row["unseen"] or 0),
        "needs_review": by_status.get("needs_review", 0),
        "rejected": by_status.get("rejected", 0),
        "seen": (row["active"] or 0) - (row["unseen"] or 0),
        "reviews": conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"],
    }


def topic_mastery(conn: sqlite3.Connection) -> list[dict]:
    """Per topic: size, how much is due, and how well it has actually gone.

    Two aggregates rather than one correlated subquery per row: the ratings
    join is over reviews, the counts are over questions, and mixing them in a
    single GROUP BY double-counts a question that has been reviewed twice.
    """
    now = _now()
    rows = conn.execute(
        f"""
        SELECT q.topic AS topic,
               COUNT(*)                                        AS active,
               SUM(CASE WHEN {DUE}    THEN 1 ELSE 0 END)       AS due,
               SUM(CASE WHEN {UNSEEN} THEN 1 ELSE 0 END)       AS unseen
          FROM questions q LEFT JOIN schedule s ON s.question_id = q.id
         WHERE q.status = 'active'
      GROUP BY q.topic
        """,
        {"now": now},
    ).fetchall()

    stats = {
        r["topic"]: r
        for r in conn.execute(
            """
            SELECT q.topic AS topic, COUNT(*) AS n, AVG(rv.rating) AS avg_rating,
                   AVG(CASE WHEN rv.asked_at >= datetime('now','-14 days')
                            THEN rv.rating END)                AS recent_rating,
                   SUM(CASE WHEN rv.rating = 1 THEN 1 ELSE 0 END) AS lapses
              FROM reviews rv JOIN questions q ON q.id = rv.question_id
             WHERE rv.rating IS NOT NULL
          GROUP BY q.topic
            """
        )
    }

    out = []
    for r in rows:
        topic = r["topic"] or "general"
        st = stats.get(r["topic"])
        out.append({
            "topic": topic,
            "active": r["active"],
            "due": r["due"] or 0,
            "unseen": r["unseen"] or 0,
            "reviews": st["n"] if st else 0,
            "avg_rating": st["avg_rating"] if st else None,
            "recent_rating": st["recent_rating"] if st else None,
            "lapses": (st["lapses"] if st else 0) or 0,
            "coverage": (r["active"] - (r["unseen"] or 0)) / r["active"] if r["active"] else 0.0,
        })
    out.sort(key=lambda d: -d["active"])
    return out


def mastery_frac(avg_rating: float | None) -> float | None:
    """Ratings run 1..4; mastery runs 0..1."""
    if avg_rating is None:
        return None
    return max(0.0, min(1.0, (avg_rating - 1) / 3))


def difficulty_grid(conn: sqlite3.Connection) -> tuple[list[str], dict[tuple[str, int], float | None]]:
    """Mastery per (topic, difficulty) -- the heatmap behind the dashboard.

    Where a topic falls apart is usually a difficulty band, not the topic: you
    can be fine on easy accounting and lost on hard accounting, and one average
    hides exactly that.
    """
    topics = [
        r["topic"] or "general"
        for r in conn.execute(
            "SELECT topic, COUNT(*) c FROM questions WHERE status='active' "
            "GROUP BY topic ORDER BY c DESC"
        )
    ]
    grid: dict[tuple[str, int], float | None] = {}
    for r in conn.execute(
        """
        SELECT COALESCE(q.topic,'general') AS topic,
               COALESCE(q.difficulty, 3)   AS diff,
               AVG(rv.rating)              AS avg_rating
          FROM reviews rv JOIN questions q ON q.id = rv.question_id
         WHERE rv.rating IS NOT NULL
      GROUP BY topic, diff
        """
    ):
        grid[(r["topic"], int(r["diff"]))] = mastery_frac(r["avg_rating"])
    return topics, grid


def daily_activity(conn: sqlite3.Connection, days: int = 21) -> list[dict]:
    """One row per calendar day, including the days you did nothing."""
    rows = {
        r["d"]: r
        for r in conn.execute(
            """
            SELECT DATE(asked_at) d, COUNT(*) n, AVG(rating) avg_rating
              FROM reviews WHERE asked_at >= datetime('now', ?)
          GROUP BY d
            """,
            (f"-{days} days",),
        )
    }
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = rows.get(d)
        out.append({
            "date": d,
            "count": r["n"] if r else 0,
            "avg_rating": r["avg_rating"] if r else None,
        })
    return out


def streak(conn: sqlite3.Connection) -> dict:
    """Consecutive days with at least one review, counting back from today.

    Yesterday still counts as an unbroken streak -- today is not over yet, and
    a counter that resets at midnight punishes you for drilling in the evening.
    """
    days = [
        r["d"]
        for r in conn.execute("SELECT DISTINCT DATE(asked_at) d FROM reviews ORDER BY d DESC")
    ]
    if not days:
        return {"current": 0, "longest": 0, "last": None, "today": 0}

    today = datetime.now(timezone.utc).date()
    seen = {datetime.fromisoformat(d).date() for d in days}

    current = 0
    cursor = today if today in seen else today - timedelta(days=1)
    while cursor in seen:
        current += 1
        cursor -= timedelta(days=1)

    longest = run = 1
    ordered = sorted(seen)
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)

    today_n = conn.execute(
        "SELECT COUNT(*) c FROM reviews WHERE DATE(asked_at) = DATE('now')"
    ).fetchone()["c"]
    return {"current": current, "longest": longest, "last": days[0], "today": today_n}


# Buckets are how long the answer had to survive before it was asked again.
_BUCKETS = [
    ("<1d", 0.0, 1.0),
    ("1-3d", 1.0, 3.0),
    ("3-7d", 3.0, 7.0),
    ("1-2w", 7.0, 14.0),
    ("2w-1m", 14.0, 31.0),
    (">1m", 31.0, 1e9),
]


def retention_curve(conn: sqlite3.Connection) -> list[dict]:
    """Measured retention: of the answers recalled after N days, how many held.

    This is the number FSRS is trying to hit (`desired_retention`), measured
    against what actually happened rather than assumed. A review counts as
    retained when it was rated 3 or 4 -- 'again' and 'hard' are both failures
    to recall cleanly.
    """
    rows = list(conn.execute(
        "SELECT question_id, asked_at, rating FROM reviews "
        "WHERE rating IS NOT NULL ORDER BY question_id, asked_at"
    ))
    tally: dict[str, list[int]] = {name: [0, 0] for name, _, _ in _BUCKETS}
    prev_by_q: dict[int, datetime] = {}
    for r in rows:
        at = datetime.fromisoformat(r["asked_at"])
        prev = prev_by_q.get(r["question_id"])
        prev_by_q[r["question_id"]] = at
        if prev is None:
            continue                       # first sighting has no interval to score
        gap = (at - prev).total_seconds() / 86400.0
        for name, lo, hi in _BUCKETS:
            if lo <= gap < hi:
                tally[name][0] += 1
                tally[name][1] += 1 if r["rating"] >= 3 else 0
                break
    return [
        {"bucket": name, "n": tally[name][0],
         "retention": (tally[name][1] / tally[name][0]) if tally[name][0] else None}
        for name, _, _ in _BUCKETS
    ]


def upcoming(conn: sqlite3.Connection, days: int = 14) -> dict:
    """What the next N days actually hold, in the two quantities it comes in.

    A scheduled review has a date on it. A question you have never opened does
    not -- it is due in the sense `counts()` means (you have never seen it),
    but no day owns it until you decide one does. Those are different things
    and this returns them apart.

    They used to be returned as one series over `schedule` alone, which meant
    the pane drawing it reported an empty fortnight while the pane beside it
    reported a thousand due. Both were reading this module; only one of them
    was reading all of it.

    Anything scheduled and already overdue lands on day one, because that is
    when you will actually face it. The unseen pool is not folded in anywhere:
    a day-one bar of a thousand says nothing about day one.
    """
    today = datetime.now(timezone.utc).date()
    rows = {
        r["d"]: r["n"]
        for r in conn.execute(
            "SELECT DATE(due_at) d, COUNT(*) n FROM schedule "
            "JOIN questions q ON q.id = schedule.question_id "
            "WHERE q.status='active' GROUP BY d"
        )
    }
    overdue = sum(n for d, n in rows.items() if datetime.fromisoformat(d).date() < today)
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        n = rows.get(d.isoformat(), 0) + (overdue if i == 0 else 0)
        out.append({"date": d.isoformat(), "weekday": d.strftime("%a"),
                    "reviews": n})

    unseen = conn.execute(
        f"""SELECT COUNT(*) c FROM questions q
              LEFT JOIN schedule s ON s.question_id = q.id
             WHERE q.status = 'active' AND {UNSEEN}""").fetchone()["c"]
    # Everything the schedule has placed anywhere ahead, not only inside the
    # window: "18 reviews in a fortnight" reads very differently when there
    # are another 200 sitting just past the edge of it.
    beyond = sum(n for d, n in rows.items()
                 if datetime.fromisoformat(d).date() >= today + timedelta(days=days))
    return {
        "days": out,
        "scheduled": sum(r["reviews"] for r in out),
        "overdue": overdue,
        "unseen": unseen,
        "beyond": beyond,
        "peak": max((r["reviews"] for r in out), default=0),
    }


def weakest_questions(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """Questions you keep getting wrong, worst and most-repeated first."""
    return list(conn.execute(
        """
        SELECT q.id, q.canonical_text, q.topic, q.difficulty,
               COUNT(*) n, AVG(rv.rating) avg_rating,
               SUM(CASE WHEN rv.rating = 1 THEN 1 ELSE 0 END) lapses
          FROM reviews rv JOIN questions q ON q.id = rv.question_id
         WHERE rv.rating IS NOT NULL AND q.status = 'active'
      GROUP BY q.id
        HAVING AVG(rv.rating) < 3
      ORDER BY avg_rating ASC, lapses DESC, n DESC
         LIMIT ?
        """,
        (limit,),
    ))


# What `recap <window>` accepts, and how far back each one reaches. A window is
# a SQLite datetime modifier or None for "everything".
WINDOWS: dict[str, str | None] = {
    "session": "-12 hours",     # replaced by the sitting's own span when there is one
    "today": "start of day",
    "yesterday": "-1 day",
    "week": "-7 days",
    "fortnight": "-14 days",
    "month": "-30 days",
    "all": None,
}


def parse_window(text: str | None) -> tuple[str, str | None]:
    """`recap` argument → (label, SQLite modifier). Accepts `7d` and `3 weeks`."""
    raw = (text or "today").strip().lower()
    if raw in WINDOWS:
        return raw, WINDOWS[raw]
    m = re.fullmatch(r"(\d+)\s*(d|day|days|w|week|weeks|h|hour|hours)", raw)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        days = n * (7 if unit == "w" else 1)
        if unit == "h":
            return f"last {n}h", f"-{n} hours"
        return f"last {days}d", f"-{days} days"
    raise ValueError(f"don't know the window '{raw}' - try "
                     + ", ".join(WINDOWS) + ", 7d or 3 weeks")


def answered(conn: sqlite3.Connection, *, since: str | None = None,
             session_id: int | None = None, limit: int = 500) -> list[dict]:
    """Every question you have answered in a window, most recent first.

    One row per *review*, not per question: answering the same card twice in a
    fortnight is two things that happened, and collapsing them would hide the
    fact that the second one went better.
    """
    where, params = ["rv.rating IS NOT NULL"], []
    if session_id is not None:
        ids = conn.execute(
            "SELECT done_json, started_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if ids is None:
            return []
        where.append("rv.asked_at >= ?")
        params.append(ids["started_at"])
        picked = [d["id"] for d in json.loads(ids["done_json"] or "[]")]
        if not picked:
            return []
        where.append("rv.question_id IN (" + ",".join("?" * len(picked)) + ")")
        params.extend(picked)
    elif since == "start of day":
        where.append("DATE(rv.asked_at) = DATE('now')")
    elif since:
        where.append("rv.asked_at >= datetime('now', ?)")
        params.append(since)
    params.append(limit)
    rows = conn.execute(f"""
        SELECT rv.id review_id, rv.question_id, rv.asked_at, rv.rating, rv.score,
               rv.grader, rv.user_answer, rv.phrasing, rv.rubric_hits,
               q.canonical_text, q.topic, q.difficulty, q.status, q.parent_id,
               s.due_at
          FROM reviews rv
          JOIN questions q ON q.id = rv.question_id
     LEFT JOIN schedule s ON s.question_id = rv.question_id
         WHERE {' AND '.join(where)}
      ORDER BY rv.asked_at DESC, rv.id DESC
         LIMIT ?""", params).fetchall()
    return [dict(r) for r in rows]


def grader_split(conn: sqlite3.Connection) -> dict:
    """How many reviews were self-rated versus model-graded.

    Self-rating is free; model grading costs an API call. Worth being able to
    see the ratio you are actually running at.
    """
    rows = list(conn.execute(
        "SELECT grader, COUNT(*) c FROM reviews GROUP BY grader"
    ))
    self_n = sum(r["c"] for r in rows if r["grader"] == "self")
    model_n = sum(r["c"] for r in rows if r["grader"] != "self")
    return {"self": self_n, "model": model_n,
            "total": self_n + model_n,
            "by_grader": {r["grader"]: r["c"] for r in rows}}


def card_health(conn: sqlite3.Connection) -> dict:
    """Aggregate FSRS card state: stability and difficulty across the bank.

    Card JSON is opaque by design, so this reads it defensively -- an fsrs
    upgrade that renames a field must degrade to "unknown", never crash the
    dashboard.
    """
    stabilities: list[float] = []
    difficulties: list[float] = []
    for r in conn.execute("SELECT card_json FROM schedule"):
        try:
            card = json.loads(r["card_json"])
        except (TypeError, ValueError):
            continue
        s = card.get("stability")
        d = card.get("difficulty")
        if isinstance(s, (int, float)):
            stabilities.append(float(s))
        if isinstance(d, (int, float)):
            difficulties.append(float(d))
    return {
        "n": len(stabilities),
        "avg_stability": sum(stabilities) / len(stabilities) if stabilities else None,
        "avg_difficulty": sum(difficulties) / len(difficulties) if difficulties else None,
        "fragile": sum(1 for s in stabilities if s < 2.0),
    }


def readiness(conn: sqlite3.Connection) -> dict:
    """One number for "how ready am I", and the two halves it is made of.

    Coverage alone flatters you (seeing a question once is not knowing it) and
    mastery alone flatters you harder (100% on the nine questions you have
    tried is not a prepared candidate). The score is the product, weighted by
    topic size, so it can only be high when most of the bank has been seen
    *and* went well.
    """
    topics = topic_mastery(conn)
    total = sum(t["active"] for t in topics)
    if not total:
        return {"score": 0.0, "coverage": 0.0, "mastery": None, "total": 0}

    score = 0.0
    cov = 0.0
    mastered_weight = 0.0
    mastery_sum = 0.0
    for t in topics:
        m = mastery_frac(t["avg_rating"])
        cov += t["active"] * t["coverage"]
        score += t["active"] * t["coverage"] * (m if m is not None else 0.0)
        if m is not None:
            mastery_sum += t["reviews"] * m
            mastered_weight += t["reviews"]
    return {
        "score": score / total,
        "coverage": cov / total,
        "mastery": (mastery_sum / mastered_weight) if mastered_weight else None,
        "total": total,
    }


def band(score: float) -> str:
    """Plain-language reading of a readiness score."""
    if score >= 0.80:
        return "interview ready"
    if score >= 0.60:
        return "solid, gaps remain"
    if score >= 0.35:
        return "coming together"
    if score >= 0.15:
        return "early"
    return "not started"
