"""Replace the heuristic topic, difficulty and rubric with real ones.

Runs in batches because the free tier is rate limited per minute, and is
resumable because a 2,000 question run will get interrupted. Enrichment only
ever writes to the derived half of the schema, so your review history and
scheduling survive a re-run.
"""
from __future__ import annotations

import json
import sqlite3

from . import checks, history, llm, market
from .admission import kind_for_topic, normalize
from .topics import TOPICS

ENRICH_VERSION = 2
MAX_CONSECUTIVE_FAILURES = 3

PROMPT = """You are preparing an investment banking interview question bank.

For each numbered item below, return an object with:
- canonical_question: the question phrased the way an interviewer would ask it,
  in English. Fix typos and stray capitalisation. Keep it short.
- topic: one of {topics}
- subtopic: two or three words
- difficulty: 1 to 5, where 1 is a definition an intern knows and 5 is
  something that separates strong candidates
- rubric_points: 3 to 5 specific, checkable things a good spoken answer must
  contain. Each must be verifiable as present or absent. Not "shows good
  understanding". Write "states that terminal value must be discounted back to
  the present".
- common_mistakes: up to 3 specific errors candidates make on this question
- tags: 2 to 5 specific concept tags in kebab-case (e.g. ev-bridge, wacc, lbo-returns, working-capital, enterprise-value)

Ground everything in the supplied model answer. Do not invent facts that are
not supported by it. If the model answer is missing, write the rubric from
standard IB interview consensus and keep it conservative.

ITEMS:
{items}
"""

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "canonical_question": {"type": "string"},
                    "topic": {
                        "type": "string",
                        "enum": list(TOPICS),
                    },
                    "subtopic": {"type": "string"},
                    "difficulty": {"type": "integer"},
                    "rubric_points": {"type": "array", "items": {"type": "string"}},
                    "common_mistakes": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "canonical_question", "topic", "difficulty",
                             "rubric_points"],
            },
        }
    },
    "required": ["items"],
}


def link_tags(conn: sqlite3.Connection, question_id: int, tags: list[str],
              kind: str = "concept") -> None:
    """Associate tags with a question.

    Thin wrapper over `tagging.attach`, which is where tag writes live. Kept
    because enrichment is where the model's own tag suggestions arrive, and
    because the name is already in use.
    """
    from . import tagging
    if tags:
        tagging.attach(conn, question_id, tags, kind=kind)


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT q.id, q.canonical_text, a.answer_key FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        f"WHERE q.status != 'rejected' AND {market.UNBOUND_SQL} "
        "AND q.extraction_version < ? ORDER BY q.id"
    )
    params: list = [ENRICH_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def pending_count(conn: sqlite3.Connection) -> int:
    """Just the number, for the line that prints it.

    The caller used to get this with len(pending(conn)) and then call run(),
    which calls pending() again: two full scans of questions LEFT JOIN answers,
    and two full materialisations of every canonical_text and answer_key, to
    display one integer.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM questions q "
        f"WHERE q.status != 'rejected' AND {market.UNBOUND_SQL} "
        "AND q.extraction_version < ?", (ENRICH_VERSION,)
    ).fetchone()[0]


def _apply(conn: sqlite3.Connection, qid: int, item: dict, *,
           batch_id: str | None = None, answer: str | None = None,
           action: str = "enrich") -> None:
    """Write one enrichment. `answer` replaces the answer on file; None keeps it.

    Keeping it is what `run` wants -- enrichment rewrites the rubric around an
    answer that came from a source, and must never invent one over it. Passing
    one is what `structure_one` wants, and it has to happen in this same call:
    writing the rubric here and the answer immediately afterwards is two
    `set_answer`s, which is two rows in the history and an `undo` preview with
    a no-op line in the middle of it.
    """
    batch_id = batch_id or history.new_batch()
    question = item["canonical_question"].strip()
    topic = item["topic"]
    # Canonicalising the wording throws away the phrasing the source printed,
    # and that is the phrasing the admission gate will meet again in the next
    # book. Keep it, so the question stays findable under both.
    previous = conn.execute(
        "SELECT canonical_text FROM questions WHERE id = ?", (qid,)
    ).fetchone()
    if previous and normalize(previous["canonical_text"]) != normalize(question):
        conn.execute(
            "INSERT OR IGNORE INTO phrasings (question_id, text, norm_key) "
            "VALUES (?, ?, ?)",
            (qid, previous["canonical_text"], normalize(previous["canonical_text"])),
        )
    # The wording goes through history like every other write to it, so a run
    # that canonicalised 800 stems is a run `undo` can take back.
    #
    # `set_question` owns the collision guard that used to live here: a rewrite
    # can land on top of a question that already exists, the admission gate
    # only ever runs at ingest, and nothing downstream would notice -- which is
    # how two questions both ended up reading exactly "Walk me through a basic
    # merger model." A colliding wording is refused and the source's own
    # phrasing is kept; the rest of the enrichment still applies, and `dupes`
    # can then surface the pair for a real merge.
    try:
        history.set_question(conn, qid, question, action=action, batch_id=batch_id)
    except history.Collision:
        question = previous["canonical_text"] if previous else question
    conn.execute(
        "UPDATE questions SET topic = ?, subtopic = ?, difficulty = ?, kind = ?, "
        "extraction_version = ? WHERE id = ?",
        (
            topic,
            (item.get("subtopic") or "").strip()[:60],
            max(1, min(5, int(item["difficulty"]))),
            kind_for_topic(topic),
            ENRICH_VERSION,
            qid,
        ),
    )
    # Through history, not a raw UPDATE. A run that rewrites 800 rubrics is
    # exactly the thing `undo` exists for, and the old UPDATE was invisible to
    # it -- worse, it matched zero rows for a question with no answers row, so
    # the rubric was dropped while extraction_version was bumped past it and
    # pending() never offered the question again.
    existing = conn.execute(
        "SELECT answer_key FROM answers WHERE question_id = ?", (qid,)).fetchone()
    history.set_answer(
        conn, qid,
        answer if answer is not None else (existing["answer_key"] if existing else None),
        json.dumps(item.get("rubric_points", [])),
        new_common_mistakes=json.dumps(item.get("common_mistakes", [])),
        extraction_version=ENRICH_VERSION,
        action=action, batch_id=batch_id,
    )
    if item.get("tags"):
        link_tags(conn, qid, item["tags"], kind="concept")
    # Also ensure topic is present as a topic tag
    link_tags(conn, qid, [topic], kind="topic")


def run(conn: sqlite3.Connection, *, batch_size: int = 6, limit: int | None = None,
        progress=print) -> int:
    rows = pending(conn, limit)
    if not rows:
        progress("everything already enriched")
        return 0

    done, fails = 0, 0
    # One run is one batch, so `undo` takes back the run rather than a row.
    batch_id = history.new_batch()
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        items = []
        for i, r in enumerate(chunk):
            answer = (r["answer_key"] or "")[:2600]
            items.append(
                f"[{i}] QUESTION: {r['canonical_text']}\n"
                f"    MODEL ANSWER: {answer or '(none on file)'}"
            )
        try:
            out = llm.generate(
                PROMPT.format(items="\n\n".join(items), topics=", ".join(TOPICS)),
                schema=BATCH_SCHEMA,
                model=llm.model_enrich(),
                temperature=0.1,
                thinking=llm.THINKING_BULK,
                caller="enrich",
            )
        except llm.LLMError as e:
            # One malformed or rate-limited batch is not a reason to abandon
            # the other 700 items. Skip it and let the version gate pick it up
            # on the next run; only a sustained failure means quota is gone.
            fails += 1
            progress(f"  batch failed ({fails}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            # A depleted balance or a rejected key will not come good on the
            # next batch, so there is no point burning two more to find out.
            if not e.retryable or fails >= MAX_CONSECUTIVE_FAILURES:
                progress("  " + llm.give_up_note(e))
                break
            continue
        fails = 0

        by_index = {int(it["index"]): it for it in out.get("items", [])}
        for i, r in enumerate(chunk):
            item = by_index.get(i)
            if not item:
                continue
            _apply(conn, r["id"], item, batch_id=batch_id)
            done += 1
        conn.commit()
        progress(f"  enriched {done}/{len(rows)}")
    return done


# ---------------------------------------------------------------- one question

# `run` and `draft_missing_answers` are both batch passes gated by a scan --
# one on extraction_version, the other on a missing answer -- and neither can
# be aimed at a single id. Pointing them at one question means two round trips
# for one question, which is the wrong shape for something you are typing at a
# prompt with the interview still fresh: you want the card back now, not after
# two sequential calls to a rate-limited free tier.
#
# So this is the third prompt in the file rather than a clever reuse of the
# first two: one call that does the work of both, for exactly one question.
STRUCTURE_PROMPT = """You are preparing an investment banking interview question bank.

A candidate has just been asked this question in a real interview and is
recording it. Return one object with:
- canonical_question: the question phrased the way an interviewer would ask
  it, in English. Fix typos, dictation slips and stray capitalisation. Keep it
  short. Do not change what is being asked.
- topic: one of {topics}
- subtopic: two or three words
- difficulty: 1 to 5, where 1 is a definition an intern knows and 5 is
  something that separates strong candidates
- answer_key: a complete spoken-style model answer, the length a good
  candidate would actually say out loud - roughly 4 to 8 sentences for a
  technical question. Correct formulas and correct three-statement mechanics
  matter more than polish.
- rubric_points: 3 to 5 specific, checkable things a good spoken answer must
  contain. Each must be verifiable as present or absent. Not "shows good
  understanding". Write "states that terminal value must be discounted back to
  the present".
- common_mistakes: up to 3 specific errors candidates make on this question
- tags: 2 to 5 specific concept tags in kebab-case (e.g. ev-bridge, wacc,
  lbo-returns, working-capital, enterprise-value)

{answer_rule}

QUESTION:
{question}
"""

# The two modes, and the difference between them is whose answer it is.
_NO_ANSWER_RULE = """There is no model answer on file. Write the answer from standard IB
interview consensus and keep it conservative. Do not invent specific figures,
company names or deal facts."""

_ROUGH_ANSWER_RULE = """The candidate's own rough notes on the answer are below. Expand and
polish them into the full spoken answer: fix the grammar, complete the
half-finished thoughts, and add the mechanical steps they skipped. You must
preserve what they actually said. Do not contradict it, do not replace their
reasoning with different reasoning, and do not drop a point they made. If
something they wrote is wrong, keep their point but state it correctly, and
name the correction in common_mistakes.

CANDIDATE'S ROUGH ANSWER:
{rough}"""

STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "canonical_question": {"type": "string"},
        "topic": {"type": "string", "enum": list(TOPICS)},
        "subtopic": {"type": "string"},
        "difficulty": {"type": "integer"},
        "answer_key": {"type": "string"},
        "rubric_points": {"type": "array", "items": {"type": "string"}},
        "common_mistakes": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["canonical_question", "topic", "difficulty", "answer_key",
                 "rubric_points"],
}


def structure_one(conn: sqlite3.Connection, qid: int, question_text: str,
                  rough_answer: str | None = None, progress=print) -> int:
    """Topic, difficulty, rubric and an answer for one question, in one call.

    Returns 1 if it was applied and 0 if it was not, which is what `run` and
    `draft_missing_answers` return and for the same reason: the caller wants
    to know whether anything landed, not what.

    The answer is model-authored with no source behind it, so it goes through
    the same mechanical gate `draft_missing_answers` uses before it is stored.
    Storing a reversed EV bridge and finding it later means it may have been
    drilled in between - and this path is the one where that is most likely,
    because a question recorded straight out of an interview is one you are
    about to go and revise.
    """
    rule = (_ROUGH_ANSWER_RULE.format(rough=rough_answer.strip())
            if (rough_answer or "").strip() else _NO_ANSWER_RULE)
    try:
        item = llm.generate(
            STRUCTURE_PROMPT.format(topics=", ".join(TOPICS),
                                    answer_rule=rule, question=question_text),
            schema=STRUCTURE_SCHEMA,
            model=llm.model_enrich(),
            temperature=0.1,
            thinking=llm.THINKING_BULK,
            caller="add_llm",
        )
    except llm.LLMError as e:
        progress(f"  {e}")
        if e.hint:
            progress(f"  {e.hint}")
        return 0

    answer = (item.get("answer_key") or "").strip()
    points = list(item.get("rubric_points") or [])
    findings = checks.inspect("\n".join([answer] + points))
    if findings:
        progress(f"  the drafted answer has a mechanical error: {findings[0]}")
        progress("  nothing was stored against it - the question is in the bank "
                 "unanswered")
        return 0

    # `_apply` owns the question row, the phrasing bookkeeping and the
    # duplicate-wording guard, and none of that is worth writing twice.
    _apply(conn, qid, item, batch_id=history.new_batch(), answer=answer,
           action="add_llm")
    conn.commit()
    return 1


# ---------------------------------------------------------------- missing answers

DRAFT_ANSWER_PROMPT = """You are an expert investment banking interview coach.

For each question below, provide:
- answer: a tight, precise 2-to-5 sentence model answer that an investment banking interviewer expects. Focus on technical accuracy, correct formulas, and three-statement mechanics.
- rubric_points: 3 to 5 specific, verifiable points a good candidate must hit.
- common_mistakes: 1 to 3 common errors candidates make on this question.

QUESTIONS:
{items}
"""

DRAFT_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "answer": {"type": "string"},
                    "rubric_points": {"type": "array", "items": {"type": "string"}},
                    "common_mistakes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "answer", "rubric_points"],
            },
        }
    },
    "required": ["items"],
}


def pending_missing_answers(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT q.id, q.canonical_text, q.topic, a.answer_key FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        f"WHERE q.status != 'rejected' AND {market.UNBOUND_SQL} "
        "AND (a.answer_key IS NULL OR TRIM(a.answer_key) = '' OR a.answer_status = 'missing') "
        "ORDER BY q.id"
    )
    params: list = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def pending_missing_answers_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        f"WHERE q.status != 'rejected' AND {market.UNBOUND_SQL} "
        "AND (a.answer_key IS NULL OR TRIM(a.answer_key) = '' "
        "     OR a.answer_status = 'missing')"
    ).fetchone()[0]


def draft_missing_answers(conn: sqlite3.Connection, *, batch_size: int = 6,
                          limit: int | None = None, progress=print) -> int:
    rows = pending_missing_answers(conn, limit)
    if not rows:
        progress("no questions with missing answers")
        return 0

    done, fails = 0, 0
    batch_id = history.new_batch()
    progress(f"drafting answers for {len(rows)} questions")

    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        items = [f"[{i}] [{r['topic'] or 'general'}] {r['canonical_text']}"
                 for i, r in enumerate(chunk)]
        try:
            out = llm.generate(
                DRAFT_ANSWER_PROMPT.format(items="\n".join(items)),
                schema=DRAFT_ANSWER_SCHEMA,
                model=llm.model_enrich(),
                temperature=0.1,
                thinking=llm.THINKING_BULK,
                caller="draft_answer",
            )
        except llm.LLMError as e:
            fails += 1
            progress(f"  batch failed ({fails}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            # A depleted balance or a rejected key will not come good on the
            # next batch, so there is no point burning two more to find out.
            if not e.retryable or fails >= MAX_CONSECUTIVE_FAILURES:
                progress("  " + llm.give_up_note(e))
                break
            continue
        fails = 0

        by_index = {int(it["index"]): it for it in out.get("items", [])}
        for i, r in enumerate(chunk):
            item = by_index.get(i)
            if not item or not item.get("answer"):
                continue
            # A drafted answer is written by a model into a bank with no source
            # to check it against, so it gets the mechanical check before it is
            # stored rather than after. Storing a reversed EV bridge and finding
            # it later means it may have been drilled in between.
            body = "\n".join([item["answer"]] + list(item.get("rubric_points") or []))
            findings = checks.inspect(body)
            if findings:
                progress(f"  #{r['id']} draft rejected: {findings[0]}")
                continue
            history.set_answer(
                conn, r["id"], item["answer"].strip(),
                json.dumps(item.get("rubric_points", [])),
                new_common_mistakes=(json.dumps(item["common_mistakes"])
                                     if item.get("common_mistakes") else None),
                action="draft_answer", batch_id=batch_id)
            done += 1
        conn.commit()
        progress(f"  drafted {done}/{len(rows)}")
    return done
