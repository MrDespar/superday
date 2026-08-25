"""Re-read a source that is already in the bank and repair what was lost.

Two things need repairing on anything ingested before the grounding change:

  provenance  question_sources.verbatim_text holds the model's rewrite of the
              page rather than the page's own words, so no answer can be
              checked against the book it came from.
  phrasings   enrich rewrites canonical_text, and the wording the source
              actually printed was simply dropped. That is the wording the
              admission gate will meet again in the next book.

This never inserts a question. A --force re-ingest would, and measurably does:
re-extracting an enriched chunk put 6 of 14 questions back through the gate as
new, because the gate no longer recognised its own canonicalised text. So this
matches into what is already there and updates in place, and reports whatever
it could not match rather than admitting it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import llm
from .admission import adjudicate, normalize, similarity
from .ingest import pdf as pdf_mod


# Matching inside one chunk is a much easier problem than matching against the
# whole bank: the candidates are the handful of questions already linked to
# these same six pages, so a wording that drifted too far for the global gate
# is still unambiguous here.
LOCAL_AT = 0.45


def _local_match(conn: sqlite3.Connection, source_id: int, locator: str,
                 question: str) -> int | None:
    norm = normalize(question)
    best_id, best = None, 0.0
    for row in conn.execute(
        "SELECT q.id, q.norm_key FROM questions q "
        "  JOIN question_sources qs ON qs.question_id = q.id "
        " WHERE qs.source_id = ? AND qs.locator = ? AND q.status != 'rejected'",
        (source_id, locator),
    ):
        s = similarity(norm, row["norm_key"] or "")
        if s > best:
            best_id, best = row["id"], s
    return best_id if best >= LOCAL_AT else None


def _repair(conn: sqlite3.Connection, qid: int, source_id: int, locator: str,
            item: dict) -> tuple[bool, bool]:
    """Returns (quote_written, phrasing_added)."""
    quoted = conn.execute(
        "UPDATE question_sources SET verbatim_text = ? "
        "WHERE question_id = ? AND source_id = ? AND locator = ?",
        (item["source_quote"], qid, source_id, locator),
    ).rowcount > 0
    if not quoted:
        # The question moved chunks between runs, or the link was never made.
        conn.execute(
            "INSERT INTO question_sources (question_id, source_id, locator, verbatim_text) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(question_id, source_id, locator) "
            "DO UPDATE SET verbatim_text = excluded.verbatim_text",
            (qid, source_id, locator, item["source_quote"]),
        )
        quoted = True

    norm = normalize(item["question"])
    known = conn.execute(
        "SELECT 1 FROM questions WHERE id = ? AND norm_key = ? "
        "UNION ALL SELECT 1 FROM phrasings WHERE question_id = ? AND norm_key = ?",
        (qid, norm, qid, norm),
    ).fetchone()
    if known:
        return quoted, False
    conn.execute(
        "INSERT OR IGNORE INTO phrasings (question_id, text, source_id, norm_key) "
        "VALUES (?, ?, ?, ?)",
        (qid, item["question"].strip(), source_id, norm),
    )
    return quoted, True


def _already_repaired(conn: sqlite3.Connection, source_id: int, locator: str) -> bool:
    """Is there anything left for this chunk to fix?

    The repair is entirely local -- fill verbatim_text, add a phrasing -- but
    finding out what to fix used to cost a full extraction call per chunk,
    every time. So a second reground of the same book paid exactly as much as
    the first, and resuming an aborted one re-paid for every chunk that had
    already succeeded. This is the SQL question the call was answering.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN qs.verbatim_text IS NULL OR TRIM(qs.verbatim_text) = '' "
        "                THEN 1 ELSE 0 END) AS missing_quote, "
        "       SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM phrasings p "
        "                                  WHERE p.question_id = q.id) "
        "                THEN 1 ELSE 0 END) AS missing_phrasing "
        "  FROM question_sources qs "
        "  JOIN questions q ON q.id = qs.question_id "
        " WHERE qs.source_id = ? AND qs.locator = ? AND q.status != 'rejected'",
        (source_id, locator),
    ).fetchone()
    # No questions from this chunk at all means it was never successfully
    # extracted, so there is real work to do.
    if not row or not row["n"]:
        return False
    return not (row["missing_quote"] or row["missing_phrasing"])


def run(conn: sqlite3.Connection, path: Path, *, window: int = 6,
        progress=print) -> dict:
    row = conn.execute(
        "SELECT id, title FROM sources WHERE path = ? OR title = ?",
        (str(path), path.stem),
    ).fetchone()
    if row is None:
        progress(f"  not in the bank yet, ingest it first: {path.name}")
        return {}
    sid = row["id"]

    pages = pdf_mod.clean_pages(pdf_mod.page_texts(path))
    windows = list(pdf_mod.chunks(pages, window))
    progress(f"\n{path.name}  {len(pages)} pages, {len(windows)} chunks")

    t = {"matched": 0, "quotes": 0, "phrasings": 0, "unmatched": 0,
         "ungrounded": 0, "skipped": 0}
    unmatched: list[str] = []
    fails = 0
    for n, (locator, text) in enumerate(windows, 1):
        if _already_repaired(conn, sid, locator):
            t["skipped"] += 1
            continue
        try:
            items, dropped = pdf_mod.extract(text)
        except llm.LLMError as e:
            fails += 1
            progress(f"\n  chunk {locator} failed ({fails}/3): {e}")
            if not e.retryable or fails >= 3:
                progress("  giving up on this file: repeated failures.")
                break
            continue
        fails = 0
        t["ungrounded"] += dropped

        for it in items:
            v = adjudicate(conn, it["question"])
            qid = v.matched_id if v.kind in ("duplicate", "variant") else None
            if qid is None:
                qid = _local_match(conn, sid, locator, it["question"])
            if qid is not None:
                q, ph = _repair(conn, qid, sid, locator, it)
                t["matched"] += 1
                t["quotes"] += int(q)
                t["phrasings"] += int(ph)
            else:
                t["unmatched"] += 1
                unmatched.append(f"{locator}  {it['question'][:64]}")
        conn.commit()
        progress(f"\r  {n}/{len(windows)} chunks   {t['matched']} matched  "
                 f"{t['quotes']} quotes  {t['phrasings']} phrasings  "
                 f"{t['unmatched']} unmatched", end="", flush=True)
    progress("")
    t["unmatched_list"] = unmatched
    return t
