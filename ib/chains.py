"""Question lines: the follow-ups that cannot be asked on their own.

A guide written as an interview transcript does not produce a thousand
independent questions. It produces runs of them, where one sets up a scenario
and the next three interrogate it:

    #802  Tell me about the different types of debt you could use in an LBO.
    #803  Wait a minute, how are Call Protection and "Prepayment" different?

Asked cold, #803 is unanswerable - not because it is a bad question but
because half of it is in the question before it. Extraction has no way to know
that: it sees one question-shaped span at a time, and every format's extractor
is the untrusted part of the pipeline anyway.

So the link is recorded afterwards, in `questions.parent_id`, which the schema
has carried since day one for exactly this. What it buys:

- `drill` and `mock` print the lead-in above the question, so the thing you
  are being asked resolves against something.
- a parent that happens to be in the same sitting is asked first, right before
  its child, because that is the order an interviewer would use.

Detection here is lexical and one-sided in the same way `checks.py` is: it
looks for the marks a question leaves when it presupposes a prior turn - "Wait
a minute", "What about", "the last question", a trailing "instead?" - and it
is deliberately quiet about the ones it cannot be sure of. A scan that flags
thirty self-contained questions is a scan you stop reading, and then the real
ones go by with it. Whatever it misses, `chains --link` takes by hand.

No LLM, no API key.
"""
from __future__ import annotations

import re
import sqlite3

# Each signal is (name, pattern, why). The text is lowercased and its curly
# quotes flattened before matching, so patterns spell everything with ASCII.
#
# Two tiers, and the difference is whether a human still has to look:
#   certain - the question names a previous turn. There is nothing to argue.
#   likely  - the question opens on a discourse marker that only exists in
#             reply to something. Almost always a follow-up; occasionally a
#             book's way of starting a section.
CERTAIN = [
    ("prev-question", r"\b(?:the|that|this|from the) (?:last|previous|preceding|earlier) "
                      r"(?:question|scenario|example|case|part|point|one)\b",
     "names the question before it"),
    ("continuing", r"\b(?:continuing|carrying on|following on) (?:with|from) the same\b",
     "says it is continuing the one before"),
    ("just-said", r"\bwhat (?:you|we|i) just (?:described|said|explained|walked|did|covered)\b",
     "refers to an answer that was just given"),
    # "You've explained commercial banks and insurance firms, but what about
    # ...". The pattern above only catches the "what you just X" shape, and a
    # transcript opens this way at least as often.
    ("you-explained",
     r"\byou.?ve (?:explained|described|covered|walked me through)\b",
     "names what the answer before it covered"),
    ("taking-into-account", r"\btaking into account the (?:changes|numbers|figures)\b",
     "carries numbers over from the question before"),
    ("same-scenario", r"\b(?:the same|this same) (?:scenario|example|setup|numbers|facts)\b",
     "reuses a scenario set up earlier"),
    # "This same company now issues $10 in Common Dividends" -- the noun list
    # above stopped at the words for a *setup* and missed the words for the
    # thing the setup is about.
    #
    # `this same`, not `the same`: "Could EV / EBITDA ever be higher than
    # EV / EBIT for the same company?" is ordinary English meaning "one and
    # the same", with no previous turn behind it, and it is the only other
    # match in the bank. The determiner is what separates them.
    ("same-subject",
     r"\bthis same (?:company|business|firm|deal|transaction|issuer)\b",
     "carries over the company set up earlier"),
    ("in-that-case", r"^in (?:that|this) case\b", "conditions on a previous answer"),
    ("as-said", r"\b(?:as|like) (?:i|we|you) (?:said|mentioned|noted) (?:before|earlier|above)\b",
     "points back at something already said"),
    ("the-above", r"\b(?:the above|mentioned above|described above|shown above)\b",
     "points at text above it"),
]

LIKELY = [
    ("wait", r"^(?:wait|hold on|hang on)\b", "opens by objecting to an answer"),
    ("ok-so", r"^(?:ok|okay|alright|right|fine|got it)\s*[,.:;-]",
     "opens by accepting an answer"),
    ("conjunction", r"^(?:and|but|so|now|then|also|plus)\b[\s,]",
     "opens on a conjunction, so it continues something"),
    ("what-about", r"^(?:what|how) about\b", "asks about a variant of something else"),
    ("why-not", r"^why not\b", "rejects an alternative that was named elsewhere"),
    ("same-for", r"^(?:same|and the same|what.s the same)\b", "asks for the same treatment"),
    ("opposite", r"^(?:now )?let.?s take the opposite\b", "inverts a scenario set up earlier"),
    ("anything-change", r"^does anything change if\b", "asks how a previous answer moves"),
    # Anchored, and deliberately: a bare "instead" closing the *opening*
    # sentence has no alternative to bind to yet, so it must have come from the
    # question before. Later in the question it usually does have one -- #818
    # spends its first sentence setting up "normally you assume full years"
    # and only then asks "…halfway through the year instead?", which is a
    # complete question and not a follow-up at all.
    ("trailing-instead", r"^[^.?!]*\binstead\s*[?.]",
     "a bare 'instead' in the opening sentence needs the alternative"),
]

SIGNALS = [(n, re.compile(p), why, "certain") for n, p, why in CERTAIN] + \
          [(n, re.compile(p), why, "likely") for n, p, why in LIKELY]

# Spelled as escapes rather than as the characters themselves: a source file
# carrying an em dash fails `selftest`, and this is the one place in the tool
# that legitimately needs to name one.
_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'",
                         "\u201c": '"', "\u201d": '"',
                         "\u2013": "-", "\u2014": "-"})


def normalise(text: str) -> str:
    return " ".join(text.translate(_QUOTES).lower().split())


def signals(text: str) -> list[tuple[str, str, str]]:
    """Every mark in this text that it presupposes a prior turn."""
    flat = normalise(text)
    out = []
    for name, pat, why, tier in SIGNALS:
        if pat.search(flat):
            out.append((name, why, tier))
    return out


# ------------------------------------------------------------------ document order


def preceding(conn: sqlite3.Connection, qid: int) -> sqlite3.Row | None:
    """The question immediately before this one in its own source.

    Ids are handed out in extraction order and extraction walks a document
    front to back, so within one source id order *is* document order. A
    question can hang off more than one source; the nearest predecessor across
    all of them is the one whose text was closest on the page.

    Only an active question can be a parent. A follow-up whose lead-in was
    rejected has nothing to attach to, and saying so is more use than silently
    attaching it to whatever came before that.
    """
    rows = conn.execute(
        """SELECT q.* FROM questions q
             JOIN question_sources qs ON qs.question_id = q.id
            WHERE q.status = 'active' AND q.id < ?
              AND qs.source_id IN (SELECT source_id FROM question_sources
                                    WHERE question_id = ?)
         ORDER BY q.id DESC LIMIT 1""", (qid, qid)).fetchall()
    return rows[0] if rows else None


# ------------------------------------------------------------------ the links


class LinkError(Exception):
    """A link that would not survive being walked."""


def link(conn: sqlite3.Connection, child: int, parent: int) -> None:
    """Hang `child` off `parent`. Rejects anything that would not walk."""
    if child == parent:
        raise LinkError(f"#{child} cannot follow itself")
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, status FROM questions WHERE id IN (?, ?)", (child, parent))}
    if child not in rows:
        raise LinkError(f"no question #{child}")
    if parent not in rows:
        raise LinkError(f"no question #{parent}")
    if rows[parent]["status"] == "rejected":
        raise LinkError(f"#{parent} is rejected - it would never be asked")
    # Walking up from the proposed parent must not arrive back at the child,
    # or `lead_in` loops forever the first time the pair is drilled.
    seen, cur = {child}, parent
    while cur is not None:
        if cur in seen:
            raise LinkError(f"#{child} → #{parent} closes a loop")
        seen.add(cur)
        row = conn.execute("SELECT parent_id FROM questions WHERE id = ?", (cur,)).fetchone()
        cur = row["parent_id"] if row else None
    conn.execute("UPDATE questions SET parent_id = ? WHERE id = ?", (parent, child))
    conn.commit()


def unlink(conn: sqlite3.Connection, child: int) -> bool:
    cur = conn.execute(
        "UPDATE questions SET parent_id = NULL WHERE id = ? AND parent_id IS NOT NULL",
        (child,))
    conn.commit()
    return cur.rowcount > 0


def mark_standalone(conn: sqlite3.Connection, qid: int) -> None:
    """Record that this one reads like a follow-up but is fine on its own."""
    from .db import now
    conn.execute("INSERT OR REPLACE INTO question_line_review "
                 "(question_id, verdict, decided_at) VALUES (?, 'standalone', ?)",
                 (qid, now()))
    conn.commit()


def standalone(conn: sqlite3.Connection) -> set[int]:
    return {r["question_id"] for r in conn.execute(
        "SELECT question_id FROM question_line_review WHERE verdict = 'standalone'")}


def lead_in(conn: sqlite3.Connection, qid: int, limit: int = 2) -> list[sqlite3.Row]:
    """The questions that came before this one, oldest first.

    Capped, because the point is to make the question answerable, not to
    replay the chapter. Two turns of context is what an interviewer is holding
    in their head anyway.
    """
    out: list[sqlite3.Row] = []
    seen = {qid}
    row = conn.execute("SELECT parent_id FROM questions WHERE id = ?", (qid,)).fetchone()
    cur = row["parent_id"] if row else None
    while cur is not None and len(out) < limit and cur not in seen:
        seen.add(cur)
        parent = conn.execute(
            "SELECT id, canonical_text, parent_id FROM questions WHERE id = ?", (cur,)).fetchone()
        if parent is None:
            break
        out.append(parent)
        cur = parent["parent_id"]
    out.reverse()
    return out


def children(conn: sqlite3.Connection, qid: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, canonical_text, status FROM questions WHERE parent_id = ? ORDER BY id",
        (qid,)).fetchall()


def root_of(conn: sqlite3.Connection, qid: int) -> int | None:
    """Walk up to the question that starts this line. Itself, if it starts one."""
    row = conn.execute("SELECT id FROM questions WHERE id = ?", (qid,)).fetchone()
    if row is None:
        return None
    seen = {qid}
    cur = qid
    while True:
        parent = conn.execute(
            "SELECT parent_id FROM questions WHERE id = ?", (cur,)).fetchone()
        up = parent["parent_id"] if parent else None
        # `link` refuses to close a loop, so this cannot happen through the
        # command. It can still happen to data edited another way, and a walk
        # that trusts the invariant hangs the shell rather than reporting it.
        if up is None or up in seen:
            return cur
        seen.add(up)
        cur = up


_NODE_COLUMNS = ("SELECT id, canonical_text, topic, status, parent_id, difficulty "
                 "FROM questions")


def graph(conn: sqlite3.Connection, qid: int) -> list[dict]:
    """The whole line `qid` sits in, root first, flattened for drawing.

    Up to the root and then down through everything hanging off it, which is
    not the same shape as `lead_in`: that one walks a single ancestry because
    all `drill` needs is the two turns before the question. A line can fork --
    an interviewer sets up one scenario and asks three different things about
    it - and the fork is invisible from inside any one of the three.

    Each node carries `rails` and `last` rather than glyphs. Which character
    draws a branch is `views.py`'s business; this decides the shape.
    """
    if conn.execute("SELECT 1 FROM questions WHERE id = ?", (qid,)).fetchone() is None:
        return []
    root = root_of(conn, qid)
    out: list[dict] = []
    seen: set[int] = set()

    def walk(row, rails: list[bool], last: bool, depth: int) -> None:
        if row["id"] in seen:
            return
        seen.add(row["id"])
        out.append({
            "id": row["id"], "text": row["canonical_text"], "topic": row["topic"],
            "status": row["status"], "parent_id": row["parent_id"],
            "difficulty": row["difficulty"],
            "rails": list(rails), "last": last, "depth": depth,
            "target": row["id"] == qid,
        })
        kids = conn.execute(
            _NODE_COLUMNS + " WHERE parent_id = ? ORDER BY id", (row["id"],)).fetchall()
        for i, kid in enumerate(kids):
            # The root sits at column zero with no connector of its own, so
            # its children start with a clean gutter rather than inheriting a
            # rail from a branch that was never drawn.
            walk(kid, [] if depth == 0 else rails + [not last],
                 i == len(kids) - 1, depth + 1)

    walk(conn.execute(_NODE_COLUMNS + " WHERE id = ?", (root,)).fetchone(),
         [], True, 0)
    return out


def lines(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """Every linked chain, each one root first."""
    rows = {r["id"]: r for r in conn.execute(
        """SELECT id, canonical_text, topic, status, parent_id FROM questions
            WHERE parent_id IS NOT NULL
               OR id IN (SELECT parent_id FROM questions WHERE parent_id IS NOT NULL)
         ORDER BY id""")}
    kids: dict[int, list[int]] = {}
    for qid, r in rows.items():
        if r["parent_id"] in rows:
            kids.setdefault(r["parent_id"], []).append(qid)
    roots = [q for q, r in rows.items() if r["parent_id"] not in rows]

    out = []
    for root in sorted(roots):
        chain, stack = [], [root]
        while stack:
            qid = stack.pop(0)
            chain.append(rows[qid])
            stack = sorted(kids.get(qid, [])) + stack
        out.append(chain)
    return out


def _parent_of(row) -> int | None:
    """`parent_id` off a Row or a dict, or None when the query never asked."""
    try:
        return row["parent_id"]
    except (KeyError, IndexError):
        return None


def order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Reorder a drill queue so a parent in it is asked right before its child.

    Right before, not merely earlier: the follow-up only makes sense while the
    lead-in is still what you just answered, and three unrelated questions in
    between is the same cold start the link exists to prevent.

    Only touches questions whose parent is in the same queue; everything else
    keeps the order the scheduler chose, because that order is the schedule's
    opinion and this has no business overruling it.
    """
    present = {r["id"]: r for r in rows}
    kids: dict[int, list[int]] = {}
    for row in rows:
        parent = _parent_of(row)
        if parent in present:
            kids.setdefault(parent, []).append(row["id"])

    placed: set[int] = set()
    out: list[sqlite3.Row] = []

    def emit(row) -> None:
        qid = row["id"]
        if qid in placed:
            return
        placed.add(qid)                     # marked before the recursion, so a
        out.append(row)                     # cycle cannot loop forever here
        for kid in kids.get(qid, []):
            emit(present[kid])

    for row in rows:
        if row["id"] in placed:
            continue
        # The pair is asked where the *earlier* of the two was going to be
        # asked. The scheduler put the follow-up at position one because that
        # is what it thinks is most worth asking; deferring it to wherever its
        # lead-in happened to land would overrule that for no reason.
        top, seen = row, {row["id"]}
        while True:
            parent = _parent_of(top)
            if parent in present and parent not in placed and parent not in seen:
                seen.add(parent)
                top = present[parent]
            else:
                break
        emit(top)
    return out


# ------------------------------------------------------------------ the scan


def scan(conn: sqlite3.Connection, *, tier: str = "all",
         include_linked: bool = False, include_settled: bool = False) -> list[dict]:
    """Questions that read like follow-ups, each with the one before it.

    `orphan` marks the ones nothing can be done about here: the question that
    came before them was rejected or was never extracted, so there is no
    lead-in to attach. Those need the text rewritten, not a link.
    """
    rows = conn.execute(
        "SELECT id, canonical_text, topic, status, parent_id FROM questions "
        "WHERE status = 'active' ORDER BY id").fetchall()
    settled = set() if include_settled else standalone(conn)
    out = []
    for r in rows:
        if r["parent_id"] is not None and not include_linked:
            continue
        if r["id"] in settled:
            continue
        marks = signals(r["canonical_text"])
        if not marks:
            continue
        best = "certain" if any(m[2] == "certain" for m in marks) else "likely"
        if tier != "all" and tier != best:
            continue
        prev = preceding(conn, r["id"])
        out.append({
            "id": r["id"],
            "text": r["canonical_text"],
            "topic": r["topic"],
            "tier": best,
            "linked_to": r["parent_id"],
            "why": [m[1] for m in marks],
            "marks": [m[0] for m in marks],
            "parent_id": prev["id"] if prev else None,
            "parent_text": prev["canonical_text"] if prev else None,
            "orphan": prev is None,
        })
    return out
