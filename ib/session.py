"""Resumable drill and mock sittings.

The failure this exists for: forty questions queued, Ctrl-C at question nine
because something came up, and the other thirty-one are gone -- not lost from
the bank, but the *ordering* work is gone, and the next `drill` re-picks from
scratch and hands back things just answered.

A session is opened when the queue is picked, trimmed after every answer, and
closed when the queue empties. An unfinished session is what `--resume` finds.
Reviews are still written the moment they happen, so an abandoned session never
costs a rating: the session only remembers what has *not* been asked yet.
"""
from __future__ import annotations

import json
import sqlite3

from .db import now

OPEN_KINDS = ("drill", "mock")


def open_session(conn: sqlite3.Connection, kind: str, queue: list[int], spec: dict) -> int:
    """Start a sitting. Any older unfinished sitting of the same kind is closed:
    two resumable drills would only make `--resume` ambiguous."""
    conn.execute(
        "UPDATE sessions SET finished_at = ?, note = 'superseded' "
        "WHERE kind = ? AND finished_at IS NULL",
        (now(), kind),
    )
    cur = conn.execute(
        "INSERT INTO sessions (kind, started_at, updated_at, spec_json, queue_json, done_json) "
        "VALUES (?, ?, ?, ?, ?, '[]')",
        (kind, now(), now(), json.dumps(spec), json.dumps(list(queue))),
    )
    conn.commit()
    return int(cur.lastrowid)


def record(conn: sqlite3.Connection, session_id: int, question_id: int,
           rating: int | None, seconds: float, graded: bool = False) -> None:
    """Mark one question answered: off the queue, onto the done list."""
    row = conn.execute(
        "SELECT queue_json, done_json FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return
    queue = [q for q in json.loads(row["queue_json"]) if q != question_id]
    done = json.loads(row["done_json"])
    done.append({"id": question_id, "rating": rating,
                 "seconds": round(seconds, 1), "graded": graded})
    conn.execute(
        "UPDATE sessions SET queue_json = ?, done_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(queue), json.dumps(done), now(), session_id),
    )
    conn.commit()


def skip(conn: sqlite3.Connection, session_id: int, question_id: int) -> None:
    """Move a question to the back rather than dropping it.

    A skip is "not now", not "never": on resume it comes round again, which is
    the whole reason to skip rather than rate it 1.
    """
    row = conn.execute("SELECT queue_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return
    queue = [q for q in json.loads(row["queue_json"]) if q != question_id]
    queue.append(question_id)
    conn.execute("UPDATE sessions SET queue_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(queue), now(), session_id))
    conn.commit()


def close(conn: sqlite3.Connection, session_id: int, note: str = "completed") -> None:
    conn.execute("UPDATE sessions SET finished_at = ?, updated_at = ?, note = ? WHERE id = ?",
                 (now(), now(), note, session_id))
    conn.commit()


def resumable(conn: sqlite3.Connection, kind: str = "drill") -> sqlite3.Row | None:
    """The newest unfinished sitting that still has questions left in it."""
    for row in conn.execute(
        "SELECT * FROM sessions WHERE kind = ? AND finished_at IS NULL "
        "ORDER BY updated_at DESC", (kind,)
    ):
        if json.loads(row["queue_json"]):
            return row
    return None


def queue_of(conn: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    """Rehydrate a saved queue into question rows, in the saved order.

    Anything rejected or deleted since the session was opened is dropped
    silently -- resuming into a question the bank has since thrown out would
    drill a known-bad answer.
    """
    ids = json.loads(row["queue_json"])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    found = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT q.*, (SELECT COUNT(DISTINCT source_id) FROM question_sources "
            f"  WHERE question_id = q.id) AS frequency "
            f"FROM questions q WHERE q.id IN ({marks}) AND q.status = 'active'",
            ids,
        )
    }
    return [found[i] for i in ids if i in found]


def summary(row: sqlite3.Row) -> dict:
    done = json.loads(row["done_json"])
    queue = json.loads(row["queue_json"])
    rated = [d["rating"] for d in done if d.get("rating")]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "done": len(done),
        # The individual answers, not just the count: the sittings list opens
        # a row to show what was actually asked and how it went.
        "done_items": done,
        "left": len(queue),
        "queue": queue,
        "spec": json.loads(row["spec_json"]),
        "avg_rating": (sum(rated) / len(rated)) if rated else None,
        "seconds": sum(d.get("seconds") or 0 for d in done),
    }


def latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The sitting you were last in, finished or not.

    `resumable` is a different question: that one is only the sitting you can
    pick back up. `recap session` wants the one you were just in even when you
    got to the end of it, which is the common case.
    """
    return conn.execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    return [
        summary(r)
        for r in conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
    ]
