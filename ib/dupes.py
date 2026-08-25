"""Near-duplicate pairs: finding them, settling them, folding one into another.

The admission gate stops a duplicate arriving. It cannot stop one that is
already here -- two books word the same question differently enough to clear
`VARIANT_AT`, `enrich` then canonicalises both toward the same phrasing, and
the bank ends up asking you the same thing twice under two ids with two
schedules.

Finding them is the same lexical similarity the gate uses, run over the bank
against itself rather than against one candidate. Nothing here calls a model
and nothing here needs an API key.

Two things are recorded rather than recomputed. A merge goes through
`history`, so `undo` takes it back. A pair you judged *not* duplicates goes
into `question_pair_review`, so the next scan does not propose it again -- a
scan that cannot reach zero is a scan you stop reading, and then the real
duplicates go by with the false ones.
"""
from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher

from . import history
from .admission import (
    DUPLICATE_AT, VARIANT_AT, _identity, _numbers, _tokens, normalize, similarity,
)
from .db import now

DEFAULT_THRESHOLD = VARIANT_AT


def _floor(threshold: float) -> float:
    """The Jaccard below which a pair cannot reach `threshold`, whatever else.

    `similarity` is 0.6*jaccard + 0.4*sequence_ratio and the ratio cannot
    exceed 1, so a pair under this floor is already short of the threshold
    before SequenceMatcher is asked. Skipping it changes no verdict.

    This matters here more than it does in the gate. The gate compares one
    candidate against the bank; this compares the bank against itself, which
    on 1,086 questions is 589,155 pairs and just under a minute of
    SequenceMatcher -- a minute spent before anything can be drawn on screen.
    """
    return (threshold - 0.4) / 0.6


def key(a: int, b: int) -> tuple[int, int]:
    """A pair's identity, independent of the order it was walked in."""
    return (a, b) if a <= b else (b, a)


# ---------------------------------------------------------------- the scan


def _live(conn: sqlite3.Connection, topic: str | None) -> list[dict]:
    sql = ("SELECT q.id, q.canonical_text, q.topic, q.status, q.norm_key, "
           "q.difficulty FROM questions q WHERE q.status != 'rejected'")
    params: list = []
    if topic:
        sql += " AND q.topic = ?"
        params.append(topic)
    return [dict(r) for r in conn.execute(sql + " ORDER BY q.id", params)]


def pairs(conn: sqlite3.Connection, *, threshold: float = DEFAULT_THRESHOLD,
          topic: str | None = None, include_settled: bool = False) -> list[dict]:
    """Every pair of live questions at or above `threshold`, closest first."""
    rows = _live(conn, topic)
    keys = [r["norm_key"] or normalize(r["canonical_text"]) for r in rows]
    toks = [_tokens(k) for k in keys]
    # Always read, whichever mode this is. `include_settled` decides whether a
    # settled pair is *dropped*, not whether the verdict is known: with the set
    # emptied, every row in `dupes --all` came back marked `settled: False`, so
    # the mode whose entire purpose is showing the ones you have already
    # decided could not tell them from the ones you have not.
    done = settled(conn)
    floor = _floor(threshold)

    out: list[dict] = []
    for i, left in enumerate(rows):
        ta = toks[i]
        for j in range(i + 1, len(rows)):
            tb = toks[j]
            union = len(ta | tb)
            if not union or len(ta & tb) / union < floor:
                continue
            sim = similarity(keys[i], keys[j])
            if sim < threshold:
                continue
            right = rows[j]
            if not include_settled and key(left["id"], right["id"]) in done:
                continue
            out.append({"similarity": sim, "a": left, "b": right,
                        "settled": key(left["id"], right["id"]) in done})
    out.sort(key=lambda p: (-p["similarity"], p["a"]["id"], p["b"]["id"]))
    return out


def near(conn: sqlite3.Connection, qid: int, *,
         threshold: float = DEFAULT_THRESHOLD, limit: int = 3) -> list[dict]:
    """The live questions closest to this one, closest first.

    This is the gate's own question asked about a row already in the bank, so
    a result list can offer "compare it with the one it looks like" without
    anybody running a full-bank scan to find out there is one.
    """
    me = conn.execute(
        "SELECT id, canonical_text, norm_key FROM questions "
        "WHERE id = ? AND status != 'rejected'", (qid,)).fetchone()
    if me is None:
        return []
    mine = me["norm_key"] or normalize(me["canonical_text"])
    ta = _tokens(mine)
    floor = _floor(threshold)
    done = settled(conn)

    out: list[dict] = []
    for r in conn.execute(
            "SELECT id, canonical_text, topic, status, norm_key FROM questions "
            "WHERE status != 'rejected' AND id != ?", (qid,)):
        other = r["norm_key"] or normalize(r["canonical_text"])
        tb = _tokens(other)
        union = len(ta | tb)
        if not union or len(ta & tb) / union < floor:
            continue
        if key(qid, r["id"]) in done:
            continue
        sim = similarity(mine, other)
        if sim >= threshold:
            out.append(dict(r) | {"similarity": sim})
    out.sort(key=lambda r: -r["similarity"])
    return out[:limit]


# ---------------------------------------------------------------- settling


def settle(conn: sqlite3.Connection, a: int, b: int,
           verdict: str = "distinct") -> None:
    """Record that these two are not the same question. Stops the scan asking."""
    low, high = key(a, b)
    conn.execute(
        "INSERT OR REPLACE INTO question_pair_review "
        "(low_id, high_id, verdict, decided_at) VALUES (?, ?, ?, ?)",
        (low, high, verdict, now()))
    conn.commit()


def unsettle(conn: sqlite3.Connection, a: int, b: int) -> bool:
    low, high = key(a, b)
    cur = conn.execute(
        "DELETE FROM question_pair_review WHERE low_id = ? AND high_id = ?",
        (low, high))
    conn.commit()
    return cur.rowcount > 0


def settled(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {(r["low_id"], r["high_id"]) for r in conn.execute(
        "SELECT low_id, high_id FROM question_pair_review WHERE verdict = 'distinct'")}


# ---------------------------------------------------------------- the merge


def merge(conn: sqlite3.Connection, keeper_id: int, dupe_id: int,
          batch_id: str) -> None:
    """Fold one question into another, keeping everything that was yours.

    The dupe's wording survives as a phrasing, so the gate still recognises
    the merged-away version and a later re-ingest cannot resurrect it.

    Everything on the "yours and permanent" side of the schema has to come
    across or the merge quietly destroys it: reviews, notes and the card. Tags
    move too -- they are cheap to recompute, but losing them silently makes a
    merged question fall out of every `--tag` drill it belonged to.
    """
    conn.execute(
        "INSERT OR IGNORE INTO phrasings (question_id, text, source_id, norm_key) "
        "SELECT ?, canonical_text, NULL, norm_key FROM questions WHERE id = ?",
        (keeper_id, dupe_id))
    conn.execute("UPDATE phrasings SET question_id = ? WHERE question_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("UPDATE OR IGNORE question_sources SET question_id = ? WHERE question_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("DELETE FROM question_sources WHERE question_id = ?", (dupe_id,))
    conn.execute("UPDATE reviews SET question_id = ? WHERE question_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("UPDATE notes SET question_id = ? WHERE question_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("UPDATE OR IGNORE question_tags SET question_id = ? WHERE question_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("DELETE FROM question_tags WHERE question_id = ?", (dupe_id,))
    # A follow-up hanging off the question being folded away would be left
    # pointing at a rejected lead-in, which `chains.link` refuses to create and
    # `drill` would print above the question anyway. The line survives the
    # merge by moving to the keeper.
    conn.execute("UPDATE questions SET parent_id = ? WHERE parent_id = ?",
                 (keeper_id, dupe_id))
    conn.execute("UPDATE questions SET parent_id = NULL WHERE id = parent_id")
    # The keeper's own card wins if it has one -- it is the row the scheduler
    # has been driving -- but a keeper that was never drilled inherits the
    # dupe's place in the schedule rather than resetting to new.
    keeper_card = conn.execute(
        "SELECT 1 FROM schedule WHERE question_id = ?", (keeper_id,)).fetchone()
    if keeper_card is None:
        conn.execute("UPDATE OR IGNORE schedule SET question_id = ? WHERE question_id = ?",
                     (keeper_id, dupe_id))
    conn.execute("DELETE FROM schedule WHERE question_id = ?", (dupe_id,))
    # A pair that has been merged is not a pair any more, so anything recorded
    # about it as an open question goes with it.
    unsettle(conn, keeper_id, dupe_id)
    history.set_status(conn, dupe_id, "rejected",
                       action="dupe_merge", batch_id=batch_id)
    conn.commit()


# ---------------------------------------------------------------- the diff

_PUNCT = " \t\n.,;:!?()[]{}\"'`"


def diff_words(a: str, b: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Both texts as (word, differs) spans, so a compare can light up the delta.

    Two questions at 0.9 similarity are nine tenths the same sentence, and the
    whole decision is in the tenth. Printing them one above the other and
    leaving you to spot it is what the old side-by-side did.

    Matching is on the word stripped of case and punctuation -- "EBITDA?" and
    "EBITDA" are the same word for this purpose -- while what comes back is
    always the original, so nothing on screen is a normalised rewrite of what
    is actually stored.
    """
    ta, tb = (a or "").split(), (b or "").split()
    ka = [w.strip(_PUNCT).lower() for w in ta]
    kb = [w.strip(_PUNCT).lower() for w in tb]
    left = [(w, True) for w in ta]
    right = [(w, True) for w in tb]
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, ka, kb).get_opcodes():
        if tag != "equal":
            continue
        for i in range(i1, i2):
            left[i] = (ta[i], False)
        for j in range(j1, j2):
            right[j] = (tb[j], False)
    return left, right


def similarity_of(conn: sqlite3.Connection, a: int, b: int) -> float | None:
    """How alike two named questions are, for a compare opened by id.

    `dupes --pair 124,877` can be typed for any two questions, including a
    pair no scan ever proposed, so the headline number is worked out here
    rather than carried over from the list that happened to open it.
    """
    rows = {r["id"]: (r["norm_key"] or normalize(r["canonical_text"]))
            for r in conn.execute(
                "SELECT id, canonical_text, norm_key FROM questions "
                "WHERE id IN (?, ?)", (a, b))}
    if a not in rows or b not in rows:
        return None
    return similarity(rows[a], rows[b])


def verdict_of(sim: float) -> str:
    """What the gate would have called this pair, had it met it at ingest."""
    if sim >= DUPLICATE_AT:
        return "duplicate"
    return "variant" if sim >= VARIANT_AT else "distinct"


# ---------------------------------------------------------------- drifted phrasings

# Function words that mean the text is not in English. A phrasing that is a
# deliberate translation scores near zero against its own canonical and is
# exactly right, so the scan has to be able to tell "this is the same question
# in German" from "this is a different question". Lexical, like everything else
# here, and only used to *suppress* a row -- a translation the list misses is a
# row you settle by hand, not a wrong answer written into the bank.
_NOT_ENGLISH = frozenset("""
    der die das dem den des und oder nicht ist sind wird werden wurde
    ein eine einen einem eines nach bei aus vom zum zur beim
    wie warum wann wenn welche welcher welches sich auch noch
    unternehmen bilanz anleihe eigenkapital fremdkapital gesellschaft
    le la les des du un une et ou pas est sont dans pour avec
    el los las una y no es son para con
""".split())


def _language(norm: str) -> str:
    """"en" or "other", decided on function words alone."""
    words = norm.split()
    if len(words) < 4:
        return "en"
    return "other" if sum(1 for w in words if w in _NOT_ENGLISH) >= 2 else "en"


def settled_phrasings(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phrasing_review'"
    ).fetchone():
        return set()
    return {(r["question_id"], r["norm_key"]) for r in conn.execute(
        "SELECT question_id, norm_key FROM phrasing_review WHERE verdict = 'keep'")}


# Below this a phrasing and its canonical have almost no wording in common,
# which for two spellings of one question is not a thing that happens.
STRANGER_AT = 0.45


def drifted(conn: sqlite3.Connection, *, include_settled: bool = False) -> list[dict]:
    """Phrasings that no longer ask the question they hang off.

    The gate scores a phrasing against the canonical once, on the way in.
    `enrich` then rewrites the canonical and nothing re-scores what is already
    attached, so a phrasing can end up asking something else entirely -- and
    `drill` serves one at random, so you are asked one question and marked
    against another's rubric. #88 carried "What is negative working capital?"
    on a card about how working capital is calculated.

    A plain similarity floor is the wrong test and finds seventy rows: two
    genuine spellings of one question *are* lexically different, which is the
    entire reason phrasings exist. What separates a reworded question from a
    different one is whether the two name the same things, so the test is the
    same one `similarity` already uses to keep EUR/CHF apart from EUR/GBP:

    - the identity sets disagree, so the two name different instruments or
      different concepts (Equity Value against Enterprise Value, an E&P NAV
      model against a gold miner's)
    - the figures disagree on both sides, so the stored answer's arithmetic
      cannot be right for both. One side having no figures at all is a
      rewording that dropped them, not a different question
    - or they share almost no wording, which for one question twice does not
      happen

    Translations are excluded by language, not by score: they are supposed to
    share no words with the canonical, and reporting them would make this a
    list of the seven rows that are fine plus the one that is not.
    """
    settled = set() if include_settled else settled_phrasings(conn)
    out: list[dict] = []
    for r in conn.execute(
        "SELECT p.id, p.question_id, p.text, p.norm_key, q.canonical_text "
        "  FROM phrasings p JOIN questions q ON q.id = p.question_id "
        " WHERE q.status != 'rejected' ORDER BY p.question_id, p.id"
    ):
        key_ = r["norm_key"] or normalize(r["text"])
        canon = normalize(r["canonical_text"])
        if (r["question_id"], key_) in settled:
            continue
        if _language(key_) != _language(canon):
            continue
        why = []
        ia, ib_ = _identity(key_), _identity(canon)
        if ia != ib_:
            named = ", ".join(sorted(ia ^ ib_))
            why.append(f"only one of them names {named}")
        na, nb = _numbers(key_), _numbers(canon)
        if na and nb and na != nb:
            why.append("the figures do not match")
        score = similarity(key_, canon)
        if score < STRANGER_AT:
            why.append("almost no wording in common")
        if not why:
            continue
        out.append({"id": r["id"], "question_id": r["question_id"],
                    "text": r["text"], "canonical_text": r["canonical_text"],
                    "norm_key": key_, "why": why, "similarity": round(score, 3)})
    out.sort(key=lambda d: d["similarity"])
    return out


def detach(conn: sqlite3.Connection, phrasing_id: int) -> bool:
    """Take a phrasing off its question.

    Deleting rather than flagging, and that is the point: `phrasings` is what
    the admission gate matches a new candidate against, so a wording left
    attached keeps teaching the gate that this question and that one are the
    same. Phrasings are on the derived side of the line, so re-extraction can
    put back anything that should not have gone.
    """
    cur = conn.execute("DELETE FROM phrasings WHERE id = ?", (phrasing_id,))
    conn.commit()
    return cur.rowcount > 0


def keep_phrasing(conn: sqlite3.Connection, question_id: int, norm_key: str) -> None:
    """Record that a phrasing reads oddly but belongs where it is."""
    conn.execute(
        "INSERT OR REPLACE INTO phrasing_review "
        "(question_id, norm_key, verdict, decided_at) VALUES (?, ?, 'keep', ?)",
        (question_id, norm_key, now()))
    conn.commit()
