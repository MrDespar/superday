"""An independent second opinion on the question bank.

`audit` already runs a critic over the bank, but it is one vendor checking its
own extraction. Same model family, correlated failure modes: when the extractor
misreads a formula, the critic tends to misread it the same way. That is a
weaker check than it looks, and the failure it misses is the expensive one --
not a scruffy question, but a confidently wrong answer that gets drilled until
it is memorised.

So this pass is a different critic, and it asks a different question. `audit`
mostly asks "is this a real interview question, cleanly extracted". This asks
"is this answer actually correct for IB", and it is pointed first at the
questions the first pass already approved, because those are the ones being
drilled.

**Different from whoever gave the first opinion**, which is the whole of the
design and is why `second_provider` exists. Claude by preference -- this pass
was built around it and it is the one that pays for the extra effort level --
but "always Claude" silently stops being a second opinion the moment
`llm_provider` is claude, at which point it is the same vendor checking its own
work: the exact failure this command was written to fix, reintroduced by a
setting somewhere else.

Two ways to run it, same table either way:

  claude-code : `cross-audit --export` writes a batch, Claude Code reviews it
                and writes verdicts back, `cross-audit --import` files them.
                No API key, and the reviewer can open the source PDFs.
  <vendor>-api : `cross-audit --api` does the same unattended, through
                `llm.generate(using=...)` like everything else.

Verdicts are stored beside the first pass's, never over them. The output that
matters is not the 800 agreements, it is the handful where the two disagree.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import llm, market
from .db import now

CROSS_AUDIT_VERSION = 1
PROVIDER_CODE = "claude-code"
PROVIDER_API = "claude-api"


def api_provider(name: str = "claude") -> str:
    """What a `--api` verdict from one vendor is filed as.

    Namespaced, because `audits.provider` is what tells a first opinion from a
    second: a bare `claude` row is `audit` running on Claude, and `claude-api`
    is the cross-audit checking somebody else's work. Two different passes
    with two different meanings, and the disagreement query joins one to the
    other -- filed under the same name they would be joined to themselves.
    """
    return f"{name}-api"


# Every name a second opinion can be filed under lives in llm.py beside the
# set of names a *first* one can, because the distinction between the two is
# one rule and the queries that join them are spread over three modules.
SECOND_PROVIDERS = llm.SECOND_PROVIDERS
SECOND_SQL = llm.SECOND_SQL


def second_provider(first: str = "", *, forced: str = "") -> str:
    """Who should give the second opinion, given who gave the first.

    Claude by preference: this pass exists to disagree with a cheap extraction
    model, and that is the job it was built around. But "always Claude" stops
    being a second opinion the moment `llm_provider` is claude -- the pass
    would become the same vendor checking its own work, which is the exact
    failure `audit` already has and the whole reason this one exists. So the
    rule is stated as what it always meant: *somebody else*, preferring
    Claude, and only from the keys you actually hold.

    "" means there is nobody left to ask, which is a sentence to print rather
    than a call to make.
    """
    first = first or llm.provider()
    if forced:
        return forced if forced in llm.PROVIDERS else ""
    for name in ("claude", "openai", "gemini"):
        if name != first and llm.available(name):
            return name
    return ""

# Below this the verdict is advisory only and never applied, matching the
# threshold ib/audit.py already uses to route a judgement call to a human.
AUTO_APPLY_AT = 0.75

TARGETS = ("kept", "needs_review", "active", "all")

INSTRUCTIONS = """You are the second opinion on an investment banking interview
question bank. A different model extracted these questions from interview
guides and has already checked them once. You are not repeating that check.

Its check was mostly structural: is this a real question, is it cleanly
extracted. Yours is substantive: IS THE ANSWER ACTUALLY CORRECT? The person
using this bank memorises these answers and repeats them in real interviews, so
a confidently wrong answer is the worst possible outcome -- far worse than a
scruffy question or a missing one.

Judge each item on the technical merits an IB interviewer would apply:

- Are the formulas right? (enterprise value bridge, WACC, unlevered vs levered
  FCF, accretion/dilution, IRR drivers, the three-statement links)
- Are the directional claims right? (what raises or lowers a multiple, what
  flows where on the statements, what happens to EV vs equity value)
- Is it right *for banking* specifically, not just plausible-sounding finance?
- Does it contradict standard IB interview consensus without justification?
- Is anything hallucinated -- a rule, a convention or a number that is not real?

Verdicts:
- keep    : the answer is correct. Minor stylistic weakness is still a keep.
- fix     : the substance is recoverable but something is wrong or misleading
            and you can state the correction. Supply corrected_answer.
- reject  : the answer is wrong in a way that would burn the candidate, the
            question is unanswerable as written, or it is not a real question.

Confidence is what decides whether a human looks at this, so calibrate it
rather than defaulting high:
- 0.9-1.0  beyond argument (a plainly correct answer, or a plainly wrong formula)
- 0.75-0.9 confident, but a reasonable banker could differ
- below 0.75 genuinely arguable, or you would need the source text to be sure
Anything below 0.75 is routed to a human for review, which is the correct
outcome for a judgement call. Do not inflate confidence to avoid that.

You are shown what that first pass decided. Do not defer to it. Agreeing where
it is right is useful; the run only earns its cost where you disagree, so say
so plainly when you do.

reason is required for fix and reject: one sentence, naming the specific error
("terminal value is not discounted back", "reverses the EV bridge"), not a
grade ("weak answer").

Some items carry `mechanical_findings`. Those come from a deterministic checker
-- arithmetic that does not add up, a reversed EV bridge, a statement link
stated backwards -- not from a model, so treat them as established rather than
as a suggestion. An item with a mechanical finding is at least a `fix`, and the
correction should address the finding specifically. Their absence means nothing:
the checker only reports what it can prove, and most wrong answers are wrong in
ways it cannot see."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["keep", "fix", "reject"]},
                    "reason": {"type": "string"},
                    "corrected_question": {"type": "string"},
                    "corrected_answer": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["question_id", "verdict", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- selection

# The first opinion's *name* comes out of `audits`, not out of the two
# denormalised columns on `questions`: those record what the first pass said
# and not who said it, and the columns were written under the name
# `gemini_verdict` back when there was only one answer to that question. A
# batch that tells its reviewer "Gemini already checked this" when OpenAI did
# is briefing them on the wrong thing -- the whole value of a second opinion is
# knowing whose the first one was.
_BASE_SELECT = f"""
    SELECT q.id, q.kind, q.topic, q.status, q.canonical_text,
           a.answer_key, a.rubric_points,
           q.audit_verdict AS first_verdict, q.audit_reason AS first_reason,
           (SELECT x.provider FROM audits x
             WHERE x.question_id = q.id AND x.provider IN {llm.PRIMARY_SQL}
             ORDER BY x.id DESC LIMIT 1) AS first_provider
      FROM questions q
      LEFT JOIN answers a ON a.question_id = q.id
     WHERE {market.UNBOUND_SQL}
"""

# A question with a live binding resolves its answer at drill time, so there is
# no stored fact for a critic to check. One without a binding has a stored
# answer whatever its `kind` says, and those are exactly the ones worth
# checking -- see market.UNBOUND_SQL.
_WHERE = {
    # The dangerous set: the first pass looked at these, let them into the
    # bank, and they are being drilled right now.
    "kept": " AND q.status = 'active' AND q.audit_verdict IN ('keep', 'fix')",
    "needs_review": " AND q.status = 'needs_review'",
    "active": " AND q.status = 'active'",
    "all": " AND q.status != 'rejected'",
}


def pending(conn: sqlite3.Connection, *, target: str = "kept",
            limit: int | None = None) -> list[sqlite3.Row]:
    """Questions this pass has not looked at yet, hardest-hitting first."""
    if target not in _WHERE:
        raise ValueError(f"unknown target {target!r}, want one of {TARGETS}")
    sql = _BASE_SELECT + _WHERE[target] + f"""
       AND NOT EXISTS (
           SELECT 1 FROM audits x
            WHERE x.question_id = q.id
              AND x.provider IN {SECOND_SQL}
              AND x.audit_version >= ?
       )
     ORDER BY (SELECT COUNT(DISTINCT source_id) FROM question_sources
                WHERE question_id = q.id) DESC, q.id
    """
    params: list = [CROSS_AUDIT_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def _item(row: sqlite3.Row) -> dict:
    from . import checks

    item = {
        "question_id": row["id"],
        "kind": row["kind"],
        "topic": row["topic"],
        "question": row["canonical_text"],
        "answer": (row["answer_key"] or "")[:4000] or None,
        "first_opinion": {
            # Named rather than assumed. A reviewer told "the first pass" is
            # being asked to weigh an opinion whose author they cannot see.
            "by": llm.provider_label(row["first_provider"])
                  if row["first_provider"] in llm.PROVIDERS else "an earlier pass",
            "verdict": row["first_verdict"],
            "reason": row["first_reason"],
        },
        "your_verdict": None,
        "reason": None,
        "confidence": None,
    }
    # Anything already *proven* wrong is handed over rather than left to be
    # re-derived. A critic that has to spot the arithmetic itself sometimes
    # does not, and these findings are decidable rather than a judgement --
    # so they belong in the brief, not in the critic's workload.
    findings = checks.inspect(row["answer_key"] or "")
    if findings:
        item["mechanical_findings"] = [
            {"kind": f.kind, "message": f.message, "excerpt": f.excerpt}
            for f in findings
        ]
    return item


# ---------------------------------------------------------------- storage

def record(conn: sqlite3.Connection, question_id: int, item: dict, *,
           provider: str, model: str | None) -> None:
    conn.execute(
        "INSERT INTO audits (question_id, provider, model, audit_version, "
        "verdict, reason, confidence, corrected_question, corrected_answer, ran_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            question_id, provider, model, CROSS_AUDIT_VERSION,
            item["verdict"],
            (item.get("reason") or "")[:400] or None,
            float(item.get("confidence", 0.0)),
            (item.get("corrected_question") or "").strip() or None,
            (item.get("corrected_answer") or "").strip() or None,
            now(),
        ),
    )


def validate(conn: sqlite3.Connection, items: list) -> tuple[list[dict], list[str]]:
    """Take only rows that are well formed and point at a real question.

    A malformed verdict file is the likely failure mode of the Claude Code path
    (hand-edited JSON, a stale export, an id that has since been deleted), so it
    is checked before anything is written rather than after.
    """
    good: list[dict] = []
    problems: list[str] = []
    seen: set[int] = set()
    for n, it in enumerate(items):
        if not isinstance(it, dict):
            problems.append(f"item {n}: not an object")
            continue
        qid = it.get("question_id")
        verdict = it.get("verdict") or it.get("your_verdict")
        if not isinstance(qid, int):
            problems.append(f"item {n}: question_id missing or not an integer")
            continue
        if verdict not in ("keep", "fix", "reject"):
            problems.append(f"#{qid}: verdict {verdict!r} is not keep/fix/reject")
            continue
        if qid in seen:
            problems.append(f"#{qid}: appears more than once, later copy ignored")
            continue
        if not conn.execute("SELECT 1 FROM questions WHERE id = ?", (qid,)).fetchone():
            problems.append(f"#{qid}: no such question")
            continue
        try:
            confidence = float(it.get("confidence"))
        except (TypeError, ValueError):
            problems.append(f"#{qid}: confidence missing or not a number")
            continue
        if not 0.0 <= confidence <= 1.0:
            problems.append(f"#{qid}: confidence {confidence} is outside 0..1")
            continue
        if verdict in ("fix", "reject") and not (it.get("reason") or "").strip():
            problems.append(f"#{qid}: {verdict} needs a reason")
            continue
        seen.add(qid)
        good.append({**it, "question_id": qid, "verdict": verdict,
                     "confidence": confidence})
    return good, problems


# ---------------------------------------------------------------- export/import

def export_batch(conn: sqlite3.Connection, path: Path, *, target: str = "kept",
                 limit: int | None = 40) -> dict:
    rows = pending(conn, target=target, limit=limit)
    payload = {
        "superday_cross_audit": CROSS_AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": target,
        "how_to_use": (
            "Fill in your_verdict (keep|fix|reject), reason and confidence (0-1) "
            "for every item, then run: superday cross-audit --import <this file>. "
            "Add corrected_answer on a fix. Leave the rest of the file alone."
        ),
        "instructions": INSTRUCTIONS,
        "items": [_item(r) for r in rows],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return {"count": len(rows), "path": str(path), "target": target}


def import_verdicts(conn: sqlite3.Connection, path: Path, *,
                    provider: str = PROVIDER_CODE,
                    model: str | None = None) -> dict:
    raw = json.loads(path.read_text())
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("no 'items' array in that file")

    scored = [it for it in items
              if isinstance(it, dict) and (it.get("verdict") or it.get("your_verdict"))]
    good, problems = validate(conn, scored)
    tally = {"keep": 0, "fix": 0, "reject": 0}
    for it in good:
        record(conn, it["question_id"], it, provider=provider,
               model=model or raw.get("model"))
        tally[it["verdict"]] += 1
    conn.commit()
    return {
        "stored": len(good),
        "skipped": len(items) - len(scored),
        "problems": problems,
        "tally": tally,
    }


# ---------------------------------------------------------------- comparison

# The latest verdict per (question, provider): re-running the pass should not
# make an old opinion resurface next to a new one.
_LATEST = """
    SELECT a.* FROM audits a
     WHERE a.id = (SELECT MAX(b.id) FROM audits b
                    WHERE b.question_id = a.question_id AND b.provider = a.provider)
"""

_PAIRS = f"""
    WITH latest AS ({_LATEST})
    SELECT q.id, q.canonical_text, q.status, q.topic,
           g.verdict AS g_verdict, g.reason AS g_reason, g.provider AS g_provider,
           c.verdict AS c_verdict, c.reason AS c_reason,
           c.confidence AS c_confidence, c.corrected_answer, c.provider AS c_provider
      FROM questions q
      JOIN latest c ON c.question_id = q.id AND c.provider IN {SECOND_SQL}
      LEFT JOIN latest g ON g.question_id = q.id AND g.provider IN {llm.PRIMARY_SQL}
"""

# How much a given disagreement should worry you. The first pass keeping
# something the second rejects is the case that actually puts a wrong answer in
# front of you, so it sorts first; the reverse is only a question you are
# missing out on.
_SEVERITY = {
    ("keep", "reject"): 0,
    ("fix", "reject"): 0,
    ("keep", "fix"): 1,
    ("reject", "keep"): 2,
    ("reject", "fix"): 2,
    ("fix", "keep"): 3,
}


def severity(first: str | None, second: str | None) -> int:
    return _SEVERITY.get((first or "", second or ""), 4)


# A `fix` at or above the floor that carries a correction holds its question
# out of every drill until the correction lands (scheduler.py's quarantine
# clause). This is the same predicate read from the other end: these are the
# questions the quarantine is currently holding, and applying is what releases
# them. If the two ever disagree, a question is either quarantined with nothing
# to apply or drillable with a correction outstanding.
_PENDING_FIX = f"""
    WITH latest AS ({_LATEST})
    SELECT q.id, q.topic, q.canonical_text, q.status,
           c.confidence, c.reason, c.provider, c.corrected_answer,
           a.answer_key
      FROM questions q
      JOIN latest c ON c.question_id = q.id
                   AND c.provider IN {SECOND_SQL}
      LEFT JOIN answers a ON a.question_id = q.id
     WHERE c.verdict = 'fix'
       AND c.confidence >= ?
       AND TRIM(COALESCE(c.corrected_answer, '')) != ''
       AND TRIM(COALESCE(a.answer_key, '')) != TRIM(c.corrected_answer)
     ORDER BY q.id
"""


def pending_corrections(conn: sqlite3.Connection,
                        ids: list[int] | None = None) -> list[dict]:
    """Corrections that are on file and not yet in the bank.

    Below `AUTO_APPLY_AT` a verdict is advisory and never offered here, which
    matches what the quarantine holds back: a judgement call routed to a human
    is not something to apply in bulk.
    """
    rows = [dict(r) for r in conn.execute(_PENDING_FIX, (AUTO_APPLY_AT,))]
    if ids is not None:
        wanted = set(ids)
        rows = [r for r in rows if r["id"] in wanted]
    return rows


def disagreements(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    rows = [dict(r) for r in conn.execute(_PAIRS)]
    out = [r for r in rows if r["g_verdict"] and r["c_verdict"]
           and r["g_verdict"] != r["c_verdict"]]
    out.sort(key=lambda r: (severity(r["g_verdict"], r["c_verdict"]),
                            -(r["c_confidence"] or 0.0), r["id"]))
    return out[:limit] if limit else out


def summary(conn: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in conn.execute(_PAIRS)]
    paired = [r for r in rows if r["g_verdict"] and r["c_verdict"]]
    agree = [r for r in paired if r["g_verdict"] == r["c_verdict"]]
    disagree = [r for r in paired if r["g_verdict"] != r["c_verdict"]]
    total_q = conn.execute(
        "SELECT COUNT(*) c FROM questions q WHERE " + market.UNBOUND_SQL
        + " AND q.status != 'rejected'").fetchone()["c"]
    return {
        "checked": len(rows),
        "unchecked": max(total_q - len(rows), 0),
        "agree": len(agree),
        "disagree": len(disagree),
        "both_reject": len([r for r in agree if r["g_verdict"] == "reject"]),
        # Named by which *pass* held the view, not by which vendor. The vendor
        # is a setting and can differ from row to row; which of the two passes
        # rejected is what decides whether a wrong answer is being drilled.
        "second_only_reject": len([r for r in disagree if r["c_verdict"] == "reject"]),
        "first_only_reject": len([r for r in disagree if r["g_verdict"] == "reject"]),
        "no_first_verdict": len([r for r in rows if not r["g_verdict"]]),
        "held": len([r for r in rows
                     if (r["c_confidence"] or 0.0) < AUTO_APPLY_AT]),
        # Who actually gave each side, as they are filed. Empty when nothing
        # has been checked yet, and more than one entry after a provider
        # switch -- both of which the screen has to be able to say.
        "first_by": sorted({r["g_provider"] for r in rows if r.get("g_provider")}),
        "second_by": sorted({r["c_provider"] for r in rows if r.get("c_provider")}),
    }
