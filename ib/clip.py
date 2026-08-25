"""What the clipboard gets, composed from the record rather than the screen.

There was already a copy path -- drag-select in the shell -- and it copies the
wrong bytes. It works off `Transcript._compose_lines`, which is text that has
already been wrapped to the terminal, indented two spaces and had a tick or a
number pushed in front of it. Pasted into a chat window that reflows for
itself, a 674-character answer arrives as nine ragged lines broken at whatever
column the terminal happened to be, with the leading spaces preserved. It
looks mangled because it *is* a screenshot of a screen, in text.

So these build from the stored strings instead. Nothing here is wrapped,
nothing is styled, and nothing knows how wide anything is: the destination
decides that, which is the whole point.

Two shapes, because they answer two different questions:

  `question`  the question and nothing else, one line. For asking someone, or
              for pasting into a search box.
  `markdown`  the whole record as Markdown -- heading, rubric as a list, the
              prose underneath. For filing it somewhere, or handing it to a
              model that will render the list as a list.

Both are read-only and neither needs a key.
"""
from __future__ import annotations

import json
import sqlite3

from . import tagging


def _record(conn: sqlite3.Connection, qid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.difficulty, "
        "       a.answer_key, a.rubric_points, a.common_mistakes "
        "  FROM questions q LEFT JOIN answers a ON a.question_id = q.id "
        " WHERE q.id = ?", (qid,)).fetchone()


def question(conn: sqlite3.Connection, qid: int) -> str:
    """The question, bare. No id, no topic, no decoration."""
    row = _record(conn, qid)
    return (row["canonical_text"] or "").strip() if row else ""


def markdown(conn: sqlite3.Connection, qid: int) -> str:
    """The whole record as Markdown.

    The rubric goes above the prose, in the order `answer_card` shows it, so
    what you paste matches what you just read. The grader's voice is left on
    here rather than stripped: on screen it is noise repeated down a column,
    but in a document that someone else may read, "Explains that ..." is the
    sentence that says these are marking criteria and not the answer itself.
    """
    row = _record(conn, qid)
    if not row:
        return ""

    points = json.loads(row["rubric_points"] or "[]")
    traps = json.loads(row["common_mistakes"] or "[]")
    tags = tagging.tags_for(conn, qid)

    out = [f"## {(row['canonical_text'] or '').strip()}", ""]

    meta = [row["topic"] or "general"]
    if row["difficulty"]:
        meta.append(f"difficulty {row['difficulty']}/5")
    meta.append(f"superday #{row['id']}")
    out.append(f"*{' · '.join(meta)}*")
    out.append("")

    if points:
        out.append("**A good answer hits**")
        out.append("")
        out.extend(f"{i}. {p.strip()}" for i, p in enumerate(points, 1))
        out.append("")

    if row["answer_key"] and row["answer_key"].strip():
        if points:
            out.append("**Model answer**")
            out.append("")
        out.append(row["answer_key"].strip())
        out.append("")

    if traps:
        out.append("**Common mistakes**")
        out.append("")
        out.extend(f"- {t.strip()}" for t in traps)
        out.append("")

    if tags:
        out.append(f"*tags: {', '.join('#' + t for t in tags)}*")
        out.append("")

    # One trailing newline, never a run of them: pasting two of these in a row
    # should not leave a gap that looks like something is missing between them.
    while len(out) > 1 and out[-1] == "" and out[-2] == "":
        out.pop()
    return "\n".join(out).rstrip() + "\n"
