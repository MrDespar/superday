"""Grade a spoken-style answer against the question's rubric.

The model never decides what is true. It receives the model answer and the
rubric and reports which points the candidate hit. That is a bounded,
checkable task, unlike "was this a good answer".
"""
from __future__ import annotations

import json
import sqlite3

from . import llm

PROMPT = """You are an investment banking interviewer marking a candidate's
spoken answer. Be exacting but fair: this is a spoken answer, so reward
substance and ignore grammar, filler and phrasing.
{persona}

QUESTION:
{question}

MODEL ANSWER (ground truth, do not contradict it):
{answer_key}

RUBRIC, in order. For each point return true only if the candidate genuinely
conveyed it, not if they merely said an adjacent word:
{rubric}

CANDIDATE ANSWER:
{user_answer}

Return:
- rubric_hits: one boolean per rubric point, same order and length
- score: fraction of rubric hit, 0 to 1
- verdict: strong, adequate, weak or wrong
- missed: the rubric points they did not convey, quoted
- feedback: two sentences maximum, specific and blunt, no praise padding
- followup: one probing follow-up question an interviewer would now ask,
  aimed at the weakest part of the answer
- suggested_rating: 1 again, 2 hard, 3 good, 4 easy, for spaced repetition
- structure: 1 to 5 for how the answer was *delivered*, judged separately from
  whether it was correct. 5 = led with the direct answer, then the mechanism,
  then the caveat, no wasted words. 3 = correct but meandering or buried the
  lead. 1 = rambling, no structure, or never actually answered the question
  asked. A wrong answer can still be well structured; score them separately.
- structure_note: one short clause on the delivery specifically
"""

PERSONAS = {
    "standard": "",
    "skeptical_md": (
        "\nYou are a skeptical Managing Director. You have heard every memorised "
        "answer. Give no credit for buzzwords or textbook recitation unless the "
        "candidate shows they understand the commercial logic underneath. Mark "
        "hedging and vagueness down hard."
    ),
    "exacting_vp": (
        "\nYou are an exacting Vice President. Formulas must be exactly right, "
        "bridges must be complete in both directions, and signs must be correct. "
        "A directionally correct answer with a wrong formula is not a pass."
    ),
}


def structure_floor(user_answer: str) -> int:
    """A local, free reading of delivery, for when nothing graded the answer.

    Crude on purpose -- it measures length and signposting, which is all you
    can honestly get without a model -- and it never claims more than 4, since
    "this looked well organised" is not the same as "this was well argued".
    """
    text = (user_answer or "").strip()
    if not text:
        return 1
    words = text.split()
    signposts = sum(
        1 for w in ("first", "second", "third", "then", "because", "so", "which means",
                    "the reason", "in short", "net net")
        if w in text.lower())
    if len(words) < 15:
        return 2                       # too short to have structure or substance
    if len(words) > 220:
        return 2                       # this is a monologue, not an answer
    return min(4, 3 + (1 if signposts >= 2 else 0))


def grade(conn: sqlite3.Connection, question_id: int, user_answer: str,
          persona: str = "standard") -> dict | None:
    row = conn.execute(
        "SELECT q.canonical_text, a.answer_key, a.rubric_points FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id WHERE q.id = ?",
        (question_id,),
    ).fetchone()
    if row is None:
        return None

    rubric = json.loads(row["rubric_points"] or "[]")
    if not rubric:
        return None

    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(rubric))
    try:
        out = llm.generate(
            PROMPT.format(
                persona=PERSONAS.get(persona, ""),
                question=row["canonical_text"],
                answer_key=(row["answer_key"] or "(none on file)")[:3000],
                rubric=numbered,
                user_answer=user_answer,
            ),
            schema=llm.GRADE_SCHEMA,
            model=llm.model_grade(),
            caller="grade",
            temperature=0.0,
            label="grading your answer",
        )
    except llm.LLMError as e:
        # The caller renders this; it carries a sentence and, where there is
        # one, the thing you can actually do about it.
        return {"error": e.message, "hint": e.hint}

    hits = list(out.get("rubric_hits", []))[: len(rubric)]
    hits += [False] * (len(rubric) - len(hits))
    out["rubric_hits"] = hits
    out["rubric"] = rubric
    out["score"] = sum(hits) / len(rubric) if rubric else 0.0
    out["suggested_rating"] = max(1, min(4, int(out.get("suggested_rating", 3))))
    raw_structure = out.get("structure")
    out["structure"] = (max(1, min(5, int(raw_structure)))
                        if isinstance(raw_structure, int) else structure_floor(user_answer))
    out["persona"] = persona
    return out
