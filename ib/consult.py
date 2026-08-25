"""Hand a batch of the bank to any outside model, and file its reply back.

`cross-audit` already does this for a second provider, in JSON, over whichever questions
come next. That shape works when a program is on both ends. It does not work
when *you* are the transport: you cannot paste 200,000 tokens into a chat
window, and a model answering in prose produces nothing a program can read.

So this module changes three things and keeps the rest:

- **Markdown out.** Pasteable into any chat, readable by you, and it carries
  its own instructions so the model does not need a system prompt.
- **Targeted, not sequential.** Reviewing the bank front to back spends most
  of the effort on questions nothing is wrong with. `select()` ranks by what
  is actually suspect -- the two auditors disagreeing, extraction debris, a
  missing rubric, a question you keep failing -- so a batch of 25 is 25
  questions worth someone's attention.
- **A reply format a regex can read.** One line per verdict. No JSON for a
  human to hand-repair, no table whose columns drift, and nothing that needs
  a model on this end to interpret it.

Verdicts land in `audits` beside the first pass's and the cross-audit's, never
over them, which
is the same rule `crossaudit` follows and for the same reason.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import crossaudit
from . import checks
from . import llm
from . import market

# What makes a question worth an outside opinion, most suspect first. The
# score is additive: a question that is several of these at once should be at
# the top of the batch, not merely in it.
REASONS = [
    ("auditors disagree", 100,
     "SELECT question_id FROM audits GROUP BY question_id "
     "HAVING COUNT(DISTINCT verdict) > 1"),
    ("mechanical finding", 90, None),          # filled in by the checker
    ("extraction debris", 70,
     "SELECT question_id FROM answers WHERE answer_key LIKE '%[DE]%'"),
    ("no answer on file", 60,
     "SELECT question_id FROM answers WHERE COALESCE(answer_key,'') = ''"),
    ("no rubric on file", 50,
     "SELECT question_id FROM answers "
     "WHERE COALESCE(rubric_points,'[]') IN ('', '[]')"),
    ("you keep getting it wrong", 45,
     "SELECT question_id FROM reviews WHERE rating IS NOT NULL "
     "GROUP BY question_id HAVING AVG(rating) < 2"),
    ("single source, never audited", 30,
     "SELECT q.id FROM questions q WHERE COALESCE(q.audit_version,0) = 0 "
     "AND (SELECT COUNT(*) FROM question_sources s WHERE s.question_id = q.id) <= 1"),
    ("never reviewed by an outside model", 10, None),   # the baseline
]


def select(conn: sqlite3.Connection, limit: int = 25,
           include_seen: bool = False) -> list[dict]:
    """The questions most worth an outside opinion, worst first."""
    # "Seen by an outside model" means a provider that is not one of the ones
    # that can produce the *first* opinion. Spelled `NOT IN ('gemini')` this
    # counted the first pass itself as an outside opinion the moment the
    # provider setting moved off Gemini, so every question `audit` had looked
    # at was excluded from the batch -- which is the whole bank, and precisely
    # the questions worth a second reader. `llm.PRIMARY_SQL` is the one
    # definition of that set.
    seen = {r[0] for r in conn.execute(
        f"SELECT DISTINCT question_id FROM audits WHERE provider NOT IN {llm.PRIMARY_SQL}")}
    # Questions with a live binding are excluded on purpose. Their answer expires,
    # so none is stored and none should be: the binding is fetched live at
    # drill time and graded on numeric tolerance. Sending them out would ask an
    # outside model to review seven questions with no answer to review.
    active = {r["id"]: dict(r) for r in conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.subtopic, q.difficulty, q.kind, "
        "a.answer_key, a.rubric_points FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        f"WHERE q.status = 'active' AND {market.UNBOUND_SQL}")}

    score: dict[int, int] = {}
    why: dict[int, list[str]] = {}
    for label, weight, sql in REASONS:
        if sql is None:
            continue
        for row in conn.execute(sql):
            qid = row[0]
            if qid in active:
                score[qid] = score.get(qid, 0) + weight
                why.setdefault(qid, []).append(label)

    for qid, q in active.items():
        findings = checks.inspect(q["answer_key"] or "")
        if findings:
            score[qid] = score.get(qid, 0) + 90
            why.setdefault(qid, []).append("mechanical finding")
            q["findings"] = [f.message for f in findings]
        if qid not in seen:
            score[qid] = score.get(qid, 0) + 10
            why.setdefault(qid, []).append("never reviewed by an outside model")

    # An unresolved disagreement stays eligible however many models have
    # already looked at it -- that it has been seen is the whole problem.
    disputed = {r[0] for r in conn.execute(
        "SELECT question_id FROM audits GROUP BY question_id "
        "HAVING COUNT(DISTINCT verdict) > 1")}
    pool = [qid for qid in active
            if include_seen or qid not in seen or qid in disputed]
    # id as the tiebreak, so the same bank produces the same batch twice.
    pool.sort(key=lambda qid: (-score.get(qid, 0), qid))
    out = []
    for qid in pool[:limit]:
        item = dict(active[qid])
        item["why"] = why.get(qid, [])
        out.append(item)
    return out


HEADER = """# superday - outside review

You are a second opinion on an investment banking interview question bank.
Another model extracted these from interview guides and has already checked
them once for whether they are cleanly extracted. **Your job is different: is
the answer actually correct?**

The person using this bank memorises these answers and repeats them in real
interviews, so a confidently wrong answer is the worst possible outcome - far
worse than a scruffy one.

Judge each on the technical merits an IB interviewer would apply: are the
formulas right (EV bridge, WACC, levered vs unlevered FCF, accretion/dilution,
the three-statement links), are the directional claims right, is it right *for
banking* specifically rather than merely plausible finance, and is anything
invented - a rule, a convention or a number that is not real?

`Flagged as` tells you why each one was put in front of you. Treat a mechanical
finding as established: it comes from a deterministic checker, not a model.

## How to reply

Reply with **only** the block below, one line per question, nothing else:

```
#<id> <keep|fix|reject> <confidence 0-1> - <one sentence naming the specific error>
```

- `keep` - the answer is correct. Minor stylistic weakness is still a keep.
- `fix` - the substance is recoverable but something is wrong or misleading.
- `reject` - wrong in a way that would burn the candidate, or unanswerable.

Calibrate confidence rather than defaulting high: below 0.75 is routed to a
human, which is the right outcome for a genuine judgement call.

To supply a corrected answer, put it on the following lines, each prefixed
with `> `. Example:

```
#123 fix 0.9 - terminal value is never discounted back to the present
> The Terminal Value is calculated in the final year and must then be
> discounted back at WACC like every other cash flow.
#124 keep 0.95 - bridge and arithmetic both check out
```
"""


def render(items: list[dict]) -> str:
    """The batch as Markdown, instructions included."""
    out = [HEADER, "---", ""]
    out.append(f"## {len(items)} questions to review")
    out.append("")
    for it in items:
        out.append(f"### #{it['id']}  {' '.join((it['canonical_text'] or '').split())}")
        out.append("")
        meta = [f"`{it['topic'] or 'general'}`", f"difficulty {it['difficulty'] or '-'}/5"]
        out.append("*" + "  ·  ".join(meta) + "*")
        out.append("")
        out.append(f"**Flagged as:** {', '.join(it['why']) or 'routine check'}")
        if it.get("findings"):
            out.append("")
            out.append("**Mechanical findings (established, not opinion):**")
            out += [f"- {f}" for f in it["findings"]]
        out.append("")
        out.append("**Answer on file**")
        out.append("")
        out.append((it["answer_key"] or "*(none)*").strip())
        rubric = json.loads(it["rubric_points"] or "[]")
        if rubric:
            out.append("")
            out.append("**Rubric on file**")
            out += [f"- {p}" for p in rubric]
        out.append("")
        out.append("---")
        out.append("")
    ids = " ".join(f"#{it['id']}" for it in items)
    out.append(f"Questions in this batch: {ids}")
    out.append("")
    return "\n".join(out)


# `#12 fix 0.85 - reverses the EV bridge`. Tolerates the dashes a model is
# likely to reach for, a missing dash, and a stray leading bullet.
_VERDICT = re.compile(
    r"^\s*[-*]?\s*#?(?P<id>\d+)\s+"
    r"(?P<verdict>keep|fix|reject)\b\s*"
    r"(?P<conf>[01](?:\.\d+)?|\.\d+)?\s*"
    # \u2014 / \u2013 rather than the characters themselves: this class
    # matches a dash the model wrote, it does not write one.
    r"(?:[\u2014\u2013\-:|]\s*)?(?P<reason>.*?)\s*$",
    re.I)
_CORRECTION = re.compile(r"^\s*>\s?(?P<text>.*)$")


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Read a model's reply. Returns (items, complaints).

    Deliberately forgiving about what surrounds the verdict lines -- models
    wrap them in prose, in a fence, or in a bulleted list -- and deliberately
    strict about the line itself, so a misread is a reported complaint rather
    than a silently wrong verdict written into the bank.
    """
    items: list[dict] = []
    problems: list[str] = []
    current: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("```"):
            continue
        m = _CORRECTION.match(line)
        if m and current is not None:
            current.setdefault("corrected_answer", "")
            current["corrected_answer"] += m.group("text") + "\n"
            continue
        m = _VERDICT.match(line)
        if not m:
            continue
        current = {
            "question_id": int(m.group("id")),
            "verdict": m.group("verdict").lower(),
            "confidence": float(m.group("conf")) if m.group("conf") else 0.7,
            "reason": (m.group("reason") or "").strip() or None,
        }
        if current["verdict"] in ("fix", "reject") and not current["reason"]:
            problems.append(f"#{current['question_id']}: {current['verdict']} with no reason")
        items.append(current)
    if not items:
        problems.append("no verdict lines found - expected lines like "
                        "`#123 keep 0.9 - reason`")
    seen: set[int] = set()
    for it in items:
        it["corrected_answer"] = (it.get("corrected_answer") or "").strip() or None
        if it["question_id"] in seen:
            problems.append(f"#{it['question_id']}: appears twice")
        seen.add(it["question_id"])
    return items, problems


def file_verdicts(conn: sqlite3.Connection, items: list[dict],
                  provider: str) -> tuple[int, list[str]]:
    """Store them beside the other opinions, never over them."""
    good, complaints = crossaudit.validate(conn, items)
    for it in good:
        crossaudit.record(conn, it["question_id"], it, provider=provider, model=None)
    conn.commit()
    return len(good), complaints


def write_batch(conn: sqlite3.Connection, path: Path, limit: int,
                include_seen: bool = False) -> tuple[Path, int]:
    items = select(conn, limit=limit, include_seen=include_seen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(items), encoding="utf-8")
    return path, len(items)
