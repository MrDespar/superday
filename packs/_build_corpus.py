"""Packs 07-14: the rest of the bank, exported from a reviewed database.

The six authored packs are 234 questions and the bank behind this repo is
over a thousand. The other 800 were extracted from guides on disk, enriched,
audited and then reviewed by hand -- which is to say they are as settled as
the authored ones, and there was no reason a fresh install should start
without them beyond the fact that nothing had ever written them out.

This is that writer. It reads a reviewed database and emits one pack per
topic, in the same JSON shape `ingest-pack` already takes, so the road in is
the one every other pack uses: the admission gate, then the pack's own topic,
difficulty, rubric and tags.

Five rules decide what leaves the database, and each of them is about what a
stranger should get rather than about tidiness:

  1. `status = active` only. A question still in the review queue is one
     nobody has agreed with yet.
  2. Nothing that came from a pack. It is already shipping, and re-exporting
     it would hand the gate its own output to deduplicate.
  3. No `market_awareness`. Its answer is a dated snapshot bound to a live
     feed at drill time, so the one thing that must not travel is the number.
  4. An answer and at least two rubric points, because the rubric is what
     grading checks against and `pack.parse` rejects a pack without one.
  5. Concept tags only. Firm tags are derived from the question's own text by
     `autotag`, so they regenerate locally, and a firm list is the one part of
     a bank that says something about its owner rather than about the bank.
  6. No fit answer written in the first person. A guide answers "what is your
     greatest weakness" with a candidate's own story, and the story is that
     candidate's -- an invented one, in the guides these came out of, which is
     no better for a stranger than a real one. Shipping it means shipping a
     rubric that marks the reader down for not having run a tutoring business.
     What travels is the advice about the shape of an answer; the answer is
     the one thing here nobody else can write for you.

No `verbatim` field is written. `pack.load` falls back to the pack's own
answer for provenance, so what ships is the pipeline's wording throughout and
no span of anybody's source text rides along inside a JSON file.

Nothing on the permanent side of the line is read at all: no reviews, no
schedule, no notes, no audits.

    python packs/_build_corpus.py            # writes ib/packs/*.json
    IB_DB=/path/to/copy.db python packs/_build_corpus.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib import db  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "ib" / "packs"

# (filename, title, topics, note). One pack per topic where the topic is deep
# enough to be worth its own file; the three product topics whose bulk already
# ships as authored packs share one, because a nine-question file is a file
# nobody opens.
PACKS = [
    ("07-accounting", "Accounting", ["accounting"],
     "Extracted from the Breaking Into Wall Street Accounting Guide, a 400-question "
     "interview set, chapter Q&A sets on working capital and the statement walkthroughs, "
     "and a set of leasing notes."),
    ("08-valuation", "Valuation", ["valuation"],
     "Extracted from the Breaking Into Wall Street Valuation Guide, a 400-question "
     "interview set and a valuation multiples Q&A set."),
    ("09-ma-merger-model", "M&A and the merger model", ["ma"],
     "Extracted from the Breaking Into Wall Street Merger Model Guide, a 400-question "
     "interview set and a 100-question technical guide."),
    ("10-ev-equity-value", "Equity value and enterprise value", ["ev_eqv"],
     "Extracted from the Breaking Into Wall Street Equity Value / Enterprise Value Guide, "
     "a 400-question interview set and four Q&A sets over the same ground."),
    ("11-lbo", "LBO", ["lbo"],
     "Extracted from the Breaking Into Wall Street LBO Model Guide and a 400-question "
     "interview set."),
    ("12-dcf", "DCF", ["dcf"],
     "Extracted from a 400-question interview set and Q&A sets on DCF factors and multiples."),
    ("13-products", "Products: DCM, ECM and deal process", ["dcm", "ecm", "deal_process"],
     "Extracted from a 400-question interview set. The bulk of these three topics is "
     "authored and ships as packs 01, 02 and 03."),
    ("14-interview-craft", "Fit, and how the interview itself works", ["behavioural", "general"],
     "Extracted from a 400-question interview set and a set of interview and assessment "
     "centre session notes. These are the standard fit questions with a guide's advice on "
     "how to answer them; the answer that matters is your own, so treat the rubric as a "
     "shape to fill rather than a script."),
]

GROUNDING = (
    "Questions were extracted from third-party guides with `ingest-pdf` and `ingest`, then "
    "given topic, difficulty and rubric by `enrich`, checked by a second model in `audit` "
    "and reviewed by hand. Every answer and rubric point here is the pipeline's wording, "
    "not the source's, and no source text is reproduced."
)

SQL = """
SELECT q.id, q.canonical_text, q.topic, q.subtopic, q.difficulty, q.kind,
       a.answer_key, a.rubric_points, a.common_mistakes,
       (SELECT s.title FROM question_sources qs JOIN sources s ON s.id = qs.source_id
         WHERE qs.question_id = q.id ORDER BY qs.source_id LIMIT 1) AS source_title
  FROM questions q
  JOIN answers a ON a.question_id = q.id
 WHERE q.status = 'active'
   AND q.kind != 'market_awareness'
   AND q.topic IN (%s)
   AND COALESCE(a.answer_key, '') != ''
   AND json_array_length(COALESCE(a.rubric_points, '[]')) >= 2
   AND NOT EXISTS (SELECT 1 FROM question_sources qs JOIN sources s ON s.id = qs.source_id
                    WHERE qs.question_id = q.id AND s.kind = 'pack')
 ORDER BY q.id
"""

# Speaking as the candidate rather than about the candidate. The tell is the
# pronoun and not the detail: swapping the tutoring business for a different
# anecdote leaves it exactly as much somebody else's answer as it was.
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|my|me|mine)\b")

TAGS_SQL = """
SELECT t.name FROM question_tags qt JOIN tags t ON t.id = qt.tag_id
 WHERE qt.question_id = ? AND t.kind != 'firm' ORDER BY t.name
"""


def items_for(conn, topics: list[str], code: str) -> list[dict]:
    rows = conn.execute(SQL % ",".join("?" * len(topics)), topics).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "behavioural" and FIRST_PERSON.search(r["answer_key"]):
            continue
        i = len(out) + 1
        tags = [x["name"] for x in conn.execute(TAGS_SQL, (r["id"],))]
        out.append({
            "q": r["canonical_text"].strip(),
            "a": r["answer_key"].strip(),
            "rubric": json.loads(r["rubric_points"]),
            "mistakes": json.loads(r["common_mistakes"] or "[]"),
            "topic": r["topic"],
            "subtopic": r["subtopic"] or None,
            "difficulty": r["difficulty"] or 3,
            "kind": r["kind"],
            "tags": tags,
            "locator": f"{code}{i:03d}",
        })
    return out


def main() -> int:
    conn = db.connect()
    total = 0
    for name, title, topics, note in PACKS:
        code = name.split("-", 1)[1][:2].upper()
        items = items_for(conn, topics, code)
        if not items:
            print(f"  {name}: nothing to write")
            continue
        doc = {
            "title": title,
            "origin": "published",
            # Reviewed in the bank they came out of, so they arrive drillable
            # rather than in a queue the new owner never asked for.
            "status": "active",
            "note": f"{note} {GROUNDING}",
            "items": items,
        }
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        total += len(items)
        print(f"  {path.name}: {len(items)}")
    print(f"  {total} questions written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
