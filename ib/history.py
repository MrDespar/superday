"""Status and answer changes, recorded so they can be taken back.

Every write to questions.status goes through set_status and every edit to
answers goes through set_answer. That is the whole point: a change that skips
this module is a change `undo` cannot see, which is worse than no undo at all
because it looks like one.

Actions are grouped into batches. One `review` session is one batch, one
`accept-all` is one batch, one `cross-audit --apply` is one batch, so undo
reverses a decision rather than a row.
"""
from __future__ import annotations

import sqlite3
import uuid

from .db import now


def new_batch() -> str:
    return uuid.uuid4().hex[:12]


def set_status(conn: sqlite3.Connection, question_id: int, new_status: str, *,
               action: str, batch_id: str) -> bool:
    """Move one question to new_status and log it. Returns False for a no-op."""
    row = conn.execute(
        "SELECT status FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return False
    old = row["status"] if isinstance(row, sqlite3.Row) else row[0]
    if old == new_status:
        return False
    conn.execute("UPDATE questions SET status = ? WHERE id = ?",
                 (new_status, question_id))
    conn.execute(
        "INSERT INTO question_status_history "
        "(question_id, old_status, new_status, action, batch_id, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (question_id, old, new_status, action, batch_id, now()),
    )
    return True


def set_answer(conn: sqlite3.Connection, question_id: int, new_answer_key: str | None,
               new_rubric_points: str | None = None, *,
               new_common_mistakes: str | None = None,
               extraction_version: int | None = None,
               action: str, batch_id: str) -> bool:
    """Update answers row and log the previous answer. Returns False for a no-op.

    `None` for rubric or mistakes means "leave it alone", not "clear it", so a
    caller that only has one of them cannot wipe the other by omission. There
    may be no answers row at all -- a question can be landed with no answer --
    in which case this inserts one rather than updating nothing.
    """
    row = conn.execute(
        "SELECT answer_key, rubric_points, common_mistakes FROM answers "
        "WHERE question_id = ?", (question_id,),
    ).fetchone()
    old_ans = row["answer_key"] if row else None
    old_rubric = row["rubric_points"] if row else None
    old_mistakes = row["common_mistakes"] if row else None

    target_rubric = new_rubric_points if new_rubric_points is not None else old_rubric
    target_mistakes = (new_common_mistakes if new_common_mistakes is not None
                       else old_mistakes)
    unchanged = (old_ans == new_answer_key and old_rubric == target_rubric
                 and old_mistakes == target_mistakes)
    if unchanged:
        # The version stamp is what stops the next run re-paying for this row,
        # so a bump still lands when the content came back identical. Nothing
        # else did, so there is nothing to log and nothing to undo.
        if extraction_version is not None and row is not None:
            conn.execute(
                "UPDATE answers SET extraction_version = ? WHERE question_id = ?",
                (extraction_version, question_id),
            )
        return False

    if row is None:
        conn.execute(
            "INSERT INTO answers (question_id, answer_key, rubric_points, "
            "common_mistakes, answer_status) VALUES (?, ?, ?, ?, ?)",
            (question_id, new_answer_key, target_rubric, target_mistakes,
             "ok" if new_answer_key else "missing"),
        )
    else:
        conn.execute(
            "UPDATE answers SET answer_key = ?, rubric_points = ?, common_mistakes = ?, "
            "answer_status = CASE WHEN ? IS NOT NULL AND ? != '' THEN 'ok' ELSE answer_status END "
            "WHERE question_id = ?",
            (new_answer_key, target_rubric, target_mistakes, new_answer_key,
             new_answer_key, question_id),
        )
    if extraction_version is not None:
        conn.execute(
            "UPDATE answers SET extraction_version = ? WHERE question_id = ?",
            (extraction_version, question_id),
        )

    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_history'"
    ).fetchone()
    if has_table:
        conn.execute(
            "INSERT INTO answer_history "
            "(question_id, old_answer_key, old_rubric_points, old_common_mistakes, "
            "new_answer_key, new_rubric_points, new_common_mistakes, "
            "action, batch_id, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (question_id, old_ans, old_rubric, old_mistakes,
             new_answer_key, target_rubric, target_mistakes,
             action, batch_id, now()),
        )
    return True


class Collision(Exception):
    """A rewrite that would land on a question that already exists."""


def set_question(conn: sqlite3.Connection, question_id: int, new_text: str, *,
                 action: str, batch_id: str) -> bool:
    """Rewrite one question's text and log it. Returns False for a no-op.

    Two things happen here that used to happen in three places and were only
    right in one of them.

    The write is logged, so `undo` can take it back. `edit` and `audit`'s fix
    verdict both wrote `canonical_text` with a raw UPDATE, which `undo` could
    not see -- and an audit that rewrites a stem is exactly what undo is for.

    And a rewrite that would collide with an existing question is refused.
    `norm_key` is what the admission gate dedupes on, and the gate only runs at
    ingest, so nothing downstream would notice: this is how two questions both
    ended up reading "Walk me through a basic merger model." `enrich._apply`
    grew a guard for it; the other two callers never had one.
    """
    from .admission import normalize
    row = conn.execute(
        "SELECT canonical_text, norm_key FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return False
    new_text = (new_text or "").strip()
    if not new_text:
        return False
    new_norm = normalize(new_text)
    if row["canonical_text"] == new_text and row["norm_key"] == new_norm:
        return False
    clash = conn.execute(
        "SELECT id FROM questions WHERE norm_key = ? AND id != ? AND status != 'rejected'",
        (new_norm, question_id)).fetchone()
    if clash:
        raise Collision(
            f"that wording is already question #{clash['id']}")

    conn.execute("UPDATE questions SET canonical_text = ?, norm_key = ? WHERE id = ?",
                 (new_text, new_norm, question_id))
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='question_text_history'"
    ).fetchone():
        conn.execute(
            "INSERT INTO question_text_history "
            "(question_id, old_text, old_norm, new_text, new_norm, action, "
            " batch_id, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (question_id, row["canonical_text"], row["norm_key"], new_text,
             new_norm, action, batch_id, now()))
    return True


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def last_batch(conn: sqlite3.Connection) -> sqlite3.Row | dict | None:
    """The most recent batch that is not itself an undo."""
    has_ans = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_history'"
    ).fetchone()
    if not has_ans:
        return conn.execute(
            "SELECT batch_id, action, COUNT(*) n, MAX(changed_at) at "
            "FROM question_status_history WHERE action != 'undo' "
            "GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT 1"
        ).fetchone()

    text_arm = ""
    if _has(conn, "question_text_history"):
        text_arm = """
            UNION ALL
            SELECT batch_id, action, COUNT(*) as n, MAX(changed_at) as at, MAX(id) as max_id
              FROM question_text_history WHERE action != 'undo'
             GROUP BY batch_id
        """
    row = conn.execute(f"""
        WITH batches AS (
            SELECT batch_id, action, COUNT(*) as n, MAX(changed_at) as at, MAX(id) as max_id
              FROM question_status_history WHERE action != 'undo'
             GROUP BY batch_id
            UNION ALL
            SELECT batch_id, action, COUNT(*) as n, MAX(changed_at) as at, MAX(id) as max_id
              FROM answer_history WHERE action != 'undo'
             GROUP BY batch_id
            {text_arm}
        )
        SELECT batch_id, action, SUM(n) as n, MAX(at) as at
          FROM batches
         GROUP BY batch_id
         ORDER BY MAX(at) DESC, MAX(max_id) DESC
         LIMIT 1
    """).fetchone()
    return row


def batch_rows(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row | dict]:
    rows = list(conn.execute(
        "SELECT h.question_id, h.old_status, h.new_status, q.canonical_text, 'status' AS change_type "
        "FROM question_status_history h "
        "LEFT JOIN questions q ON q.id = h.question_id "
        "WHERE h.batch_id = ? ORDER BY h.id DESC", (batch_id,)
    ))
    has_ans = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_history'"
    ).fetchone()
    if has_ans:
        ans_rows = conn.execute(
            "SELECT h.question_id, h.old_answer_key, h.new_answer_key, "
            "h.old_rubric_points, h.new_rubric_points, q.canonical_text, 'answer' AS change_type "
            "FROM answer_history h "
            "LEFT JOIN questions q ON q.id = h.question_id "
            "WHERE h.batch_id = ? ORDER BY h.id DESC", (batch_id,)
        ).fetchall()
        rows.extend(ans_rows)
    if _has(conn, "question_text_history"):
        rows.extend(conn.execute(
            "SELECT h.question_id, h.old_text, h.new_text, q.canonical_text, "
            "'question' AS change_type FROM question_text_history h "
            "LEFT JOIN questions q ON q.id = h.question_id "
            "WHERE h.batch_id = ? ORDER BY h.id DESC", (batch_id,)
        ).fetchall())
    return rows


def undo_batch(conn: sqlite3.Connection, batch_id: str) -> int:
    """Put every question and answer in a batch back where it was."""
    undo_id = new_batch()
    n = 0
    # Undo status changes
    for r in conn.execute(
        "SELECT question_id, old_status FROM question_status_history "
        "WHERE batch_id = ? ORDER BY id DESC", (batch_id,)
    ).fetchall():
        if r["old_status"] is not None:
            if set_status(conn, r["question_id"], r["old_status"],
                          action="undo", batch_id=undo_id):
                n += 1

    # Undo answer changes
    has_ans = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_history'"
    ).fetchone()
    if has_ans:
        for r in conn.execute(
            "SELECT question_id, old_answer_key, old_rubric_points, old_common_mistakes "
            "FROM answer_history WHERE batch_id = ? ORDER BY id DESC", (batch_id,)
        ).fetchall():
            if set_answer(conn, r["question_id"], r["old_answer_key"],
                          r["old_rubric_points"],
                          new_common_mistakes=r["old_common_mistakes"],
                          action="undo", batch_id=undo_id):
                n += 1

    # Question text last, so a batch that both rewrote a stem and moved a
    # status has the stem put back against the status it was written under.
    if _has(conn, "question_text_history"):
        for r in conn.execute(
            "SELECT question_id, old_text FROM question_text_history "
            "WHERE batch_id = ? ORDER BY id DESC", (batch_id,)
        ).fetchall():
            if not r["old_text"]:
                continue
            try:
                if set_question(conn, r["question_id"], r["old_text"],
                                action="undo", batch_id=undo_id):
                    n += 1
            except Collision:
                # The wording it had is now somebody else's. Leaving the text
                # alone is the only safe answer: forcing it would put two
                # questions back under one key and the gate would then treat
                # them as one.
                continue

    conn.commit()
    return n
