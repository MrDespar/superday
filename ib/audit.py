"""Second-opinion QA over extracted questions.

Deliberately run with a different model than the one that did the extraction.
A model grading its own output agrees with itself; a different one actually
disagrees, which is the entire point of the pass.

Verdicts are applied automatically at the ends of the confidence range and
left for a human in the middle, so the review queue shrinks to the genuinely
ambiguous cases rather than everything.
"""
from __future__ import annotations

import sqlite3

from . import history, llm, market
from .db import now

AUDIT_VERSION = 2


def audit_model() -> str:
    """The model this pass runs on. Deliberately not the extraction model:
    a model grading its own output agrees with itself.

    The default now comes from whichever provider is configured, so "a
    different model than the one that extracted" stays true after switching
    provider instead of quietly meaning "a Gemini model" forever.
    """
    return llm.model_audit()

PROMPT = """You are quality-checking an investment banking interview question
bank. The items below were extracted automatically from interview guides by a
different model, and some are wrong.

Each item is tagged KIND: technical or behavioural. Judge it as that kind.
A behavioural item is a legitimate question -- "walk me through your resume",
"tell me about a time you led a team", "why our bank" are all real interview
questions and must be kept. Reject a behavioural item only if it is page
furniture or narrative *about* interviewing rather than a question an
interviewer would actually ask.

Reject an item if any of these hold:
- it is not an interview question (marketing copy, a table of contents entry,
  navigation text, page furniture, a heading, prose about the guide itself)
- for a technical item, the answer is factually wrong or misleading for IB
- the question is unanswerable without a figure, exhibit or table that is not
  present in the text
- the question or answer is truncated mid-thought
- it is so vague that no specific answer exists

Fix an item, rather than rejecting it, if the substance is sound but the
wording is poor: a truncated question stem that is still obvious, stray
capitalisation, a fragment of book prose left in the answer, an answer that
refers to "the above" or "this chapter" and needs to be made self-contained.
Fix, do not reject, a heading that has fused two real questions: return the
first one alone.

Keep an item when it is a genuine interview question with a correct,
self-contained answer.

Be strict. A bad question in a drill bank is worse than a missing one,
because it teaches the wrong answer.

Confidence is what decides whether a human sees this, so calibrate it rather
than defaulting high. Use:
- 0.9 to 1.0  only when the verdict is beyond argument, e.g. the item is
  plainly a table of contents line, or plainly a clean correct question
- 0.75 to 0.9 when you are confident but a reasonable reviewer could differ
- below 0.75 when the call is genuinely arguable: you suspect the answer is
  wrong but cannot be sure without the source text, the question may depend on
  a missing exhibit, or the item sits on the line between fix and reject
Items below 0.75 are routed to a human, which is the correct outcome for a
judgement call. Do not inflate confidence to avoid that.

For each item return:
- index: the item number
- verdict: keep, fix, or reject
- reason: one short sentence, required for fix and reject
- corrected_question: only when verdict is fix
- corrected_answer: only when verdict is fix
- confidence: 0 to 1, calibrated as above

ITEMS:
{items}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["keep", "fix", "reject"]},
                    "reason": {"type": "string"},
                    "corrected_question": {"type": "string"},
                    "corrected_answer": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "verdict", "confidence"],
            },
        }
    },
    "required": ["items"],
}

# Below this, the verdict is advisory only and the item stays for a human.
AUTO_APPLY_AT = 0.75
MAX_CONSECUTIVE_FAILURES = 3


def pending(conn: sqlite3.Connection, limit: int | None = None,
            status: str = "needs_review") -> list[sqlite3.Row]:
    # market_awareness answers resolve from a live feed at drill time, so
    # there is no stored fact for a critic to check. Everything else is fair
    # game -- behavioural questions included, which the technical-only filter
    # used to skip entirely.
    sql = (
        "SELECT q.id, q.kind, q.canonical_text, a.answer_key FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        f"WHERE q.status = ? AND {market.UNBOUND_SQL} "
        "AND COALESCE(q.audit_version, 0) < ? ORDER BY q.id"
    )
    params: list = [status, AUDIT_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


VERDICTS = ("keep", "fix", "reject")


def _apply(conn: sqlite3.Connection, qid: int, item: dict, batch_id: str) -> str:
    verdict = item["verdict"]
    confidence = float(item.get("confidence", 0))
    reason = (item.get("reason") or "")[:300]

    # The schema constrains verdict to VERDICTS, but responseSchema is not an
    # airtight guarantee and this has already produced a "hold" in the wild.
    # Without this guard an unrecognised verdict falls through to the keep
    # branch at the bottom, so a verdict the critic never gave would quietly
    # promote a question into the bank. Unknown means ask a human, not accept.
    if verdict not in VERDICTS:
        confidence = 0.0
        reason = f"unrecognised verdict {verdict!r} from the critic; {reason}"[:300]
        verdict = "fix"

    conn.execute(
        "UPDATE questions SET audit_version = ?, audit_verdict = ?, audit_reason = ? "
        "WHERE id = ?",
        (AUDIT_VERSION, verdict, reason, qid),
    )
    # Also recorded row-wise, so `cross-audit` has something to compare against
    # and so a verdict that is later overwritten here is still in the history.
    conn.execute(
        "INSERT INTO audits (question_id, provider, model, audit_version, verdict, "
        "reason, confidence, corrected_question, corrected_answer, ran_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (qid, llm.provider(), audit_model(), AUDIT_VERSION, verdict, reason or None,
         confidence,
         (item.get("corrected_question") or "").strip() or None,
         (item.get("corrected_answer") or "").strip() or None, now()),
    )

    if confidence < AUTO_APPLY_AT:
        return "held"  # ambiguous: leave it in the queue with the note attached

    if verdict == "reject":
        history.set_status(conn, qid, "rejected", action="audit", batch_id=batch_id)
        return "rejected"

    if verdict == "fix":
        q = (item.get("corrected_question") or "").strip()
        a = (item.get("corrected_answer") or "").strip()
        if q:
            # Through history, so `undo` takes a rewritten stem back with the
            # rest of the batch, and refused when the rewrite would collide
            # with a question that already exists -- the gate only runs at
            # ingest, so nothing downstream would notice the duplicate.
            try:
                history.set_question(conn, qid, q, action="audit", batch_id=batch_id)
            except history.Collision:
                pass
        history.set_status(conn, qid, "active", action="audit", batch_id=batch_id)
        if a:
            history.set_answer(conn, qid, a, action="audit", batch_id=batch_id)
        return "fixed"

    history.set_status(conn, qid, "active", action="audit", batch_id=batch_id)
    return "kept"


def run(conn: sqlite3.Connection, *, batch_size: int = 8, limit: int | None = None,
        status: str = "needs_review", progress=print) -> dict[str, int]:
    rows = pending(conn, limit, status)
    tally = {"kept": 0, "fixed": 0, "rejected": 0, "held": 0}
    fails = 0
    batch_id = history.new_batch()
    if not rows:
        progress("nothing to audit")
        return tally

    progress(f"auditing {len(rows)} questions with {audit_model()}")
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        items = [
            f"[{i}] KIND: {r['kind']}\n"
            f"    QUESTION: {r['canonical_text']}\n"
            f"    ANSWER: {(r['answer_key'] or '(none)')[:1800]}"
            for i, r in enumerate(chunk)
        ]
        try:
            out = llm.generate(
                PROMPT.format(items="\n\n".join(items)),
                schema=SCHEMA, model=audit_model(), temperature=0.0,
                thinking=llm.THINKING_BULK,
                caller="audit",
            )
        except llm.LLMError as e:
            fails += 1
            progress(f"  batch failed ({fails}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            if not e.retryable or fails >= MAX_CONSECUTIVE_FAILURES:
                progress("  " + llm.give_up_note(e))
                break
            continue
        fails = 0

        by_index = {int(it["index"]): it for it in out.get("items", [])}
        for i, r in enumerate(chunk):
            item = by_index.get(i)
            if item:
                tally[_apply(conn, r["id"], item, batch_id)] += 1
        conn.commit()
        done = sum(tally.values())
        progress(f"  {done}/{len(rows)}   keep {tally['kept']}  fix {tally['fixed']}  "
                 f"reject {tally['rejected']}  held {tally['held']}")
    return tally
