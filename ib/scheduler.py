"""FSRS scheduling.

Card state is stored opaquely as JSON so an fsrs upgrade cannot invalidate the
schema. This table and `reviews` are the half of the database that extraction
must never touch.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

from . import llm
from .config import load
from .db import now

_RATINGS = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}

_cached_scheduler: Scheduler | None = None
_cached_retention: float | None = None


def scheduler() -> Scheduler:
    global _cached_scheduler, _cached_retention
    ret = load()["desired_retention"]
    if _cached_scheduler is None or _cached_retention != ret:
        _cached_scheduler = Scheduler(desired_retention=ret)
        _cached_retention = ret
    return _cached_scheduler


def ensure_card(conn: sqlite3.Connection, question_id: int) -> Card:
    row = conn.execute(
        "SELECT card_json FROM schedule WHERE question_id = ?", (question_id,)
    ).fetchone()
    if row:
        return Card.from_dict(json.loads(row["card_json"]))
    card = Card()
    conn.execute(
        "INSERT INTO schedule (question_id, card_json, due_at) VALUES (?, ?, ?)",
        (question_id, json.dumps(card.to_dict()), card.due.isoformat()),
    )
    conn.commit()
    return card


def record_review(
    conn: sqlite3.Connection,
    question_id: int,
    rating: int,
    *,
    phrasing: str | None = None,
    user_answer: str | None = None,
    score: float | None = None,
    rubric_hits: list[bool] | None = None,
    grader: str = "self",
) -> datetime:
    card = ensure_card(conn, question_id)
    card, _ = scheduler().review_card(card, _RATINGS[rating])

    conn.execute(
        "INSERT INTO reviews (question_id, asked_at, phrasing, user_answer, rating, "
        "score, rubric_hits, grader) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            question_id, now(), phrasing, user_answer, rating, score,
            json.dumps(rubric_hits or []), grader,
        ),
    )
    conn.execute(
        "UPDATE schedule SET card_json = ?, due_at = ?, reps = reps + 1, "
        "lapses = lapses + ? WHERE question_id = ?",
        (
            json.dumps(card.to_dict()),
            card.due.isoformat(),
            1 if rating == 1 else 0,
            question_id,
        ),
    )
    conn.commit()
    return card.due


def quarantine_sql(conn: sqlite3.Connection) -> str:
    """The `AND NOT EXISTS (...)` that keeps a disputed answer out of a sitting.

    One definition, because two things read it: the query that picks the
    sitting, and the sentence that explains why a hand-picked question did not
    make it into one. A second copy would eventually tell you a question was
    held back for a reason the query does not actually apply.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audits'"
    ).fetchone():
        return ""
    return f"""
       AND NOT EXISTS (
           SELECT 1 FROM audits c
            WHERE c.question_id = q.id
              AND c.provider IN {llm.SECOND_SQL}
              AND c.id = (SELECT MAX(c2.id) FROM audits c2
                           WHERE c2.question_id = q.id AND c2.provider = c.provider)
              AND (
                  c.verdict = 'reject'
                  OR (c.verdict = 'fix' AND c.confidence >= 0.75 AND c.corrected_answer IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM answers a
                           WHERE a.question_id = q.id
                             AND TRIM(COALESCE(a.answer_key, '')) != TRIM(COALESCE(c.corrected_answer, ''))
                      ))
              )
       )
    """


def due_phrase(due: datetime | str, *, at: datetime | None = None) -> str:
    """A due timestamp as the words someone deciding whether to drill needs.

    A date is the wrong unit for the first few looks at a card. FSRS's
    learning steps are minutes long, so a question answered at 10:01 comes
    round at 10:11 -- and `due.date()` renders that as *today's date*, which
    reads as "you can have this now". The next `drill` then refuses it, and
    the two screens have told you opposite things about the same card. Inside
    a day the honest unit is how long you have to wait.
    """
    if isinstance(due, str):
        due = datetime.fromisoformat(due)
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    at = at or datetime.now(timezone.utc)
    seconds = (due - at).total_seconds()
    if seconds <= 0:
        return "now"
    if seconds < 90 * 60:
        return f"in {max(1, round(seconds / 60))}m"
    if seconds < 24 * 3600:
        return f"in {round(seconds / 3600)}h"
    return due.astimezone().date().isoformat()


def held_back(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """Why each of `ids` cannot be asked right now, one reason per id.

    `scheduled` carries the due timestamp; the other three carry None. An id
    that *is* askable is absent. The refusal used to offer a disjunction --
    "scheduled for later, or quarantined by an unapplied cross-audit" -- and
    make you guess which, when the two are one query apart.
    """
    if not ids:
        return {}
    marks = ",".join(str(int(i)) for i in dict.fromkeys(ids))
    live = {
        r["id"]: r["due_at"]
        for r in conn.execute(
            f"SELECT q.id AS id, s.due_at AS due_at FROM questions q "
            f"LEFT JOIN schedule s ON s.question_id = q.id "
            f"WHERE q.id IN ({marks}) AND q.status = 'active'"
        )
    }
    clean = {
        r["id"] for r in conn.execute(
            f"SELECT q.id AS id FROM questions q WHERE q.id IN ({marks}) "
            f"AND q.status = 'active' {quarantine_sql(conn)}"
        )
    }
    known = {
        r["id"] for r in conn.execute(f"SELECT id FROM questions WHERE id IN ({marks})")
    }
    stamp = datetime.now(timezone.utc).isoformat()
    reasons: dict[int, tuple[str, str | None]] = {}
    for qid in dict.fromkeys(int(i) for i in ids):
        if qid not in known:
            reasons[qid] = ("missing", None)
        elif qid not in live:
            reasons[qid] = ("inactive", None)
        elif qid not in clean:
            reasons[qid] = ("quarantined", None)
        elif live[qid] is not None and live[qid] > stamp:
            reasons[qid] = ("scheduled", live[qid])
    return reasons


def due_questions(conn: sqlite3.Connection, limit: int = 20, topic: str | None = None,
                  kind: str | None = None,
                  tag: str | None = None,
                  weak_first: bool = False,
                  include_quarantined: bool = False,
                  ignore_schedule: bool = False,
                  ids: list[int] | None = None) -> list[sqlite3.Row]:
    """What to ask next.

    FSRS decides *when* a card comes round; this decides what order the ones
    that have come round get asked in, which FSRS has no opinion about. The
    ordering is: due before never-seen, then a question a real interviewer
    actually asked ahead of a textbook question regardless of how many books
    carry it, then frequency.

    `weak_first` flips it to remediation: the questions and topics you have
    been rated worst on come first, so a short sitting spends its time where
    the bank says you are losing marks rather than on a fresh random spread.

    Questions with an unresolved second-opinion rejection, or a high-confidence
    correction that has not been applied, are quarantined: drilling a known-bad
    answer is worse than not drilling. Which provider gave that second opinion
    is not this query's business -- `llm.SECOND_SQL` is the set of names one
    can be filed under, and a name missing from it reads as no rejection at
    all.

    `ids` restricts the pool to an explicit set, which is how a filtered
    `browse` hands its selection to a drill. Everything else still applies to
    that set -- the quarantine, the due window, the ordering -- so drilling a
    browse is a normal drill over a smaller pool, not a second code path that
    could disagree with the first about what is safe to ask.

    `ignore_schedule` drops the due window and nothing else, which is `drill
    --again`: wanting another go at what you just answered is a normal thing
    to want, and FSRS's learning steps are minutes long, so the schedule said
    no to it for ten minutes at a time with no way round. It deliberately does
    *not* drop the quarantine -- "not yet" is a pacing decision you may
    overrule, "this answer is known wrong" is not.
    """
    quarantine_clause = "" if include_quarantined else quarantine_sql(conn)

    has_tags = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tags'"
    ).fetchone())
    tag_clause = ""
    if tag:
        # A tag filter that silently matched nothing would look identical to
        # "nothing due", so a bare topic name is accepted as a tag too.
        if has_tags:
            tag_clause = """
               AND (
                   q.topic = ?
                   OR EXISTS (
                       SELECT 1 FROM question_tags qt
                       JOIN tags t ON t.id = qt.tag_id
                       WHERE qt.question_id = q.id AND LOWER(t.name) = ?
                   )
               )
            """
        else:
            tag_clause = " AND q.topic = ?"

    if weak_first:
        order = ("ORDER BY (q_avg IS NULL), q_avg ASC, lapses DESC, "
                 "topic_avg ASC, origin_boost DESC, RANDOM()")
    else:
        order = ("ORDER BY (s.due_at IS NULL), origin_boost DESC, "
                 "frequency DESC, s.due_at ASC, RANDOM()")

    id_clause = ""
    if ids is not None:
        # An explicit but empty selection means "nothing", not "no filter".
        id_clause = (" AND q.id IN (" + ",".join(str(int(i)) for i in ids) + ")"
                     if ids else " AND 0")

    due_clause = "" if ignore_schedule else "AND (s.due_at IS NULL OR s.due_at <= ?)"
    sql = f"""
        SELECT q.*,
               (SELECT COUNT(DISTINCT source_id) FROM question_sources
                 WHERE question_id = q.id) AS frequency,
               s.due_at AS due_at,
               (SELECT AVG(rv.rating) FROM reviews rv
                 WHERE rv.question_id = q.id AND rv.rating IS NOT NULL) AS q_avg,
               (SELECT COUNT(*) FROM reviews rv
                 WHERE rv.question_id = q.id AND rv.rating = 1) AS lapses,
               COALESCE((SELECT AVG(rv.rating) FROM reviews rv
                          JOIN questions q2 ON q2.id = rv.question_id
                         WHERE q2.topic = q.topic AND rv.rating IS NOT NULL), 4.0) AS topic_avg,
               CASE q.origin WHEN 'interviewer_asked' THEN 100
                             WHEN 'self_authored'     THEN 20
                             ELSE 0 END AS origin_boost
          FROM questions q
          LEFT JOIN schedule s ON s.question_id = q.id
         WHERE q.status = 'active'
           {quarantine_clause}
           {tag_clause}
           {id_clause}
           AND (? IS NULL OR q.topic = ?)
           AND (? IS NULL OR q.kind = ?)
           {due_clause}
      {order}
         LIMIT ?
    """
    params: list = []
    if tag:
        clean_tag = tag.strip().lower().lstrip("#")
        params.extend([clean_tag, clean_tag] if has_tags else [clean_tag])
    params.extend([topic, topic, kind, kind])
    if not ignore_schedule:
        params.append(datetime.now(timezone.utc).isoformat())
    params.append(limit)
    return list(conn.execute(sql, params))
