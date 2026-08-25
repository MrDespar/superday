"""The admission gate.

Nothing enters the bank directly. Every candidate is compared against what is
already there and resolves to exactly one of: duplicate (attach evidence),
variant (new phrasing of a known question), or new.

This runs with no API key. Similarity is lexical, which is good enough for a
corpus this redundant. Swap in embeddings later behind the same interface.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

from .db import now
from .topics import TOPICS  # noqa: F401  (re-exported: callers import it from here too)

DUPLICATE_AT = 0.88
VARIANT_AT = 0.70

_STOP = {
    "the", "a", "an", "is", "are", "do", "does", "you", "your", "of", "to", "and",
    "in", "on", "for", "it", "its", "what", "why", "how", "would", "me", "i",
    "this", "that", "with", "be", "can", "if", "as", "at", "by", "or", "from",
}

# IB technicals use a fixed vocabulary, so rules beat a model here and cost
# nothing. The enrich step can overwrite these with better labels.
TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Without these, no rule can ever return "behavioural", so every fit
    # question from a docx or from `superday add` lands as general/technical.
    ("behavioural", ("tell me about a time", "walk me through your resume",
                     "why investment banking", "why our bank", "why this bank",
                     "your strengths", "your weakness", "greatest strength",
                     "greatest weakness", "greatest achievement",
                     "where do you see yourself", "why should i hire you",
                     "why should we hire you", "tell me about yourself",
                     "describe a situation", "ethical dilemma",
                     "demonstrated leadership", "as a leader", "a team project",
                     "work in a team", "conflict with", "your resume",
                     "biggest failure", "proudest", "outside of work",
                     "questions for me", "why are you here today")),
    # The three product topics sit above the generalist ones on purpose. A tie
    # goes to the first rule that matched, and "what drives credit spreads" hits
    # `markets` on the bare word "spread" as readily as it hits `dcm` on four
    # terms that only a bond desk uses. Product first, generalist second.
    ("dcm", ("bond", "coupon", "yield to maturity", "yield to worst", "credit rating",
             "investment grade", "high yield", "covenant", "syndicate", "orderbook",
             "order book", "mid-swap", "z-spread", "asset swap", "new issue premium",
             "schuldschein", "corporate hybrid", "debt capital markets", "credit spread",
             "leveraged loan", "term loan", "unitranche", "private credit", "indenture",
             "call protection", "duration", "convexity", "rating agency",
             "seniority", "subordinated notes", "mezzanine", "ipts",
             "initial price thoughts", "tighten", "greenium", "reoffer",
             "issuance window", "bookrunner", "issuer", "basis points over")),
    ("ecm", ("ipo", "initial public offering", "greenshoe", "over-allotment",
             "brownshoe", "stabilisation", "stabilization", "rights issue",
             "rights offering", "follow-on", "accelerated bookbuild", "block trade",
             "cornerstone", "free float", "lock-up", "lockup", "prospectus",
             "roadshow", "underwriter", "equity capital markets", "direct listing",
             "spac", "primary shares", "secondary shares", "pre-emption",
             "index inclusion", "bookbuild", "red herring", "shelf offering")),
    ("deal_process", ("locked box", "completion accounts", "cash-free debt-free",
                      "cash free debt free", "working capital peg", "earn-out",
                      "earnout", "vendor loan", "quality of earnings", "takeover code",
                      "scheme of arrangement", "rule 2.7", "put up or shut up",
                      "irrevocable", "mandatory offer", "squeeze-out", "squeeze out",
                      "wpüg", "due diligence", "auction process", "sell-side process",
                      "information memorandum", "data room", "exclusivity", "leakage",
                      "sale and purchase agreement", "warranty and indemnity",
                      "management presentation", "non-binding offer")),
    ("lbo", ("lbo", "leveraged buyout", "sponsor", "irr", "moic", "debt paydown",
             "management rollover", "dividend recap")),
    ("ma", ("merger model", "accretion", "dilution", "synergies", "purchase price allocation",
            "goodwill", "acquirer", "target", "m&a", "hostile", "tender offer", "earnout")),
    ("dcf", ("dcf", "discounted cash flow", "wacc", "terminal value", "perpetuity",
             "unlevered free cash flow", "levered free cash flow", "beta", "capm",
             "mid-year", "discount rate")),
    ("ev_eqv", ("enterprise value", "equity value", "tev", "net debt", "treasury stock",
                "diluted shares", "noncontrolling", "minority interest", "preferred stock")),
    ("valuation", ("multiple", "comparable", "precedent", "ev/ebitda", "p/e", "trading comps",
                   "football field", "premium", "valuation method")),
    ("accounting", ("income statement", "balance sheet", "cash flow statement", "depreciation",
                    "working capital", "deferred", "accrual", "inventory", "ebitda",
                    "net income", "ifrs", "lease", "revenue recognition", "write-down")),
    ("markets", ("s&p", "yield", "fed", "ecb", "inflation", "vix", "index level",
                 "rate cut", "spread", "oil price", "market", "bund", "gilt",
                 "euribor", "itraxx", "sofr", "€str", "dax", "stoxx", "brent")),
]


def normalize(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"^\s*(?:q\s*[:.\-]|\d+[).\-]|[-*•])\s*", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm: str) -> set[str]:
    return {w for w in norm.split() if w not in _STOP and len(w) > 2}


def _numbers(norm: str) -> set[str]:
    return set(re.findall(r"\d+", norm))


# Words that say *which instrument* a question is about rather than what is
# being asked of it. "Where is CHF against the euro" and "Where is GBP against
# the euro" share every other word, score 0.72, and were being merged as
# variants of each other -- at which point one of them silently inherited the
# other's live binding and started grading a Swiss franc answer against
# sterling.
#
# This is the same failure the numeric guard was written for, one level up:
# 10-year versus 2-year is caught because the tenor is a digit, EUR/CHF versus
# EUR/GBP is not because the instrument is a word. The vocabulary is small and
# lexical on purpose, in keeping with the rest of this module -- it names the
# things this bank actually asks "where is X" about, and anything outside it
# falls back to ordinary token similarity.
_IDENTITY: frozenset[str] = frozenset("""
    usd eur gbp chf jpy cny sek nok dkk pln czk huf try
    bund bunds gilt gilts btp btps oat oats treasury treasuries jgb
    euribor estr sofr sonia libor tona saron
    brent wti henry gold silver copper
    dax cac ftse stoxx sensex nikkei nasdaq russell
    itraxx cdx vix
    spx
""".split())
# `move` and `try` are deliberately NOT in that list, though MOVE is the bond
# volatility index and TRY is the Turkish lira. `normalize` lowercases before
# this is read, so the ticker and the ordinary English verb arrive identical --
# and "how did you move the process forward" and "why does the swap spread
# move" are what the bank actually contains. An identity term that fires on a
# verb caps the similarity of every pair where one side happens to use it,
# which is the false positive this whole mechanism exists to avoid, pointed
# the wrong way.


# The same failure again, in the vocabulary this bank is actually about.
#
# "How do you determine whether a transaction changes Equity Value" and the
# same sentence ending "Enterprise Value" are the two halves of the core mental
# model -- CSE versus NOA -- and they scored 0.837 against a duplicate
# threshold of 0.88. They cleared it by four hundredths. #23/#24, #28/#235 and
# #10/#614 sit in the same band for the same reason: the word that carries the
# whole question is one ordinary word in a sentence of shared ones.
#
# Unlike the currency list above these terms have synonyms -- TEV, EV and
# "enterprise value" are one thing under three spellings -- so a flat token set
# would make a genuine reworded duplicate look like two different instruments,
# which is the same mistake pointing the other way. Each spelling maps to the
# concept instead, and the concept is what gets compared.
_IDENTITY_ALIASES: dict[str, str] = {
    "tev": "enterprise", "ev": "enterprise", "enterprise": "enterprise",
    "eqv": "equity", "equity": "equity",
    "levered": "levered", "unlevered": "unlevered",
    "lfcf": "levered", "ufcf": "unlevered",
    "accretive": "accretive", "accretion": "accretive",
    "dilutive": "dilutive", "dilution": "dilutive",
    "basic": "basic", "diluted": "diluted",
}


def _identity(norm: str) -> set[str]:
    out = {w for w in norm.split() if w in _IDENTITY}
    out |= {_IDENTITY_ALIASES[w] for w in norm.split() if w in _IDENTITY_ALIASES}
    return out


def similarity(a: str, b: str) -> float:
    """Blend token overlap with sequence ratio. Overlap catches reordered
    phrasings, sequence ratio catches small edits.

    Numbers and instrument names are treated as identity, not as words. "Where
    is the 10-year" and "Where is the 2-year" are 0.9 lexically similar and are
    completely different questions; so are "where is CHF against the euro" and
    "where is GBP against the euro", which share every word that is not the
    answer. Whenever either identity set disagrees the pair is capped below the
    duplicate threshold, and when the two sets are wholly disjoint it is capped
    below the variant threshold too, so it can never be silently merged.
    """
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    score = 0.6 * jac + 0.4 * seq

    for get in (_numbers, _identity):
        ia, ib = get(a), get(b)
        if ia != ib:
            score = min(score, DUPLICATE_AT - 0.01)
            if ia and ib and not (ia & ib):
                score = min(score, VARIANT_AT - 0.01)
    return score


def classify_topic(question: str, answer: str = "") -> str:
    blob = f"{question} {answer}".lower()
    best, best_hits = "general", 0
    for topic, terms in TOPIC_RULES:
        hits = sum(1 for t in terms if t in blob)
        if hits > best_hits:
            best, best_hits = topic, hits
    return best


# The bank has three kinds and only ever set one of them. A behavioural
# question filed as technical is drilled by the wrong rounds, hidden from
# `drill -k behavioural`, and judged by an audit prompt that is told to reject
# career narrative -- so it gets thrown away for being what it is.
def kind_for_topic(topic: str | None) -> str:
    return "behavioural" if (topic or "") == "behavioural" else "technical"


def estimate_difficulty(question: str, answer: str = "") -> int:
    q = question.lower()
    score = 2
    if any(p in q for p in ("walk me through", "how would you", "explain why")):
        score += 1
    if len(answer) > 1800:
        score += 1
    if any(p in q for p in ("what is", "what does", "define")):
        score -= 1
    return max(1, min(5, score))


def rubric_from_answer(answer: str, limit: int = 5) -> list[str]:
    """Heuristic rubric: the load-bearing clauses of the model answer. Crude but
    immediately usable for self-grading, and the enrich step replaces it."""
    if not answer:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", answer) if len(p.strip()) > 25]
    return [p[:220] for p in parts[:limit]]


def rubric_is_the_answer(points: list[str] | None, answer: str | None) -> bool:
    """Whether this rubric is still the placeholder cut out of the answer.

    `rubric_from_answer` takes the answer's first few sentences and truncates
    each at 220 characters, so every point it produces is a literal substring
    of the answer. `enrich` replaces them with checkable claims in the grader's
    own words, which are not.

    Worth being able to ask, because the reveal leads with the rubric and
    keeps the prose one keystroke behind it. When the two are the same text
    that keystroke reveals nothing, and the screen has silently printed the
    answer twice -- once as a checklist and once as the wall the checklist
    exists to replace.
    """
    points = [p for p in (points or []) if p and p.strip()]
    if not points or not (answer or "").strip():
        return False
    return all(p.rstrip(".").strip() in answer for p in points)


def _adopt_answer_if_missing(
    conn: sqlite3.Connection, qid: int | None, answer_text: str | None
) -> bool:
    """A later source can supply an answer the first one lacked.

    Only fills a hole; it never overwrites an answer that is already there,
    because the two sources disagreeing is not evidence that the newer one is
    right. The losing text is still on the question_sources row either way.
    """
    if not qid or not answer_text:
        return False
    row = conn.execute(
        "SELECT answer_key FROM answers WHERE question_id = ?", (qid,)
    ).fetchone()
    if row is None or (row["answer_key"] or "").strip():
        return False
    conn.execute(
        "UPDATE answers SET answer_key = ?, rubric_points = ?, answer_status = 'ok' "
        "WHERE question_id = ?",
        (answer_text, json.dumps(rubric_from_answer(answer_text)), qid),
    )
    return True


@dataclass
class Verdict:
    kind: str            # new | duplicate | variant
    matched_id: int | None
    similarity: float


def adjudicate(conn: sqlite3.Connection, question_text: str) -> Verdict:
    norm = normalize(question_text)
    if not norm:
        return Verdict("rejected", None, 0.0)

    hit = conn.execute(
        "SELECT id FROM questions WHERE norm_key = ? AND status != 'rejected'", (norm,)
    ).fetchone()
    if hit:
        return Verdict("duplicate", hit["id"], 1.0)
    # A question is also itself under any wording it has previously been seen
    # under. enrich rewrites canonical_text, so without this the gate stops
    # recognising the phrasing the source actually printed.
    hit = conn.execute(
        "SELECT p.question_id FROM phrasings p JOIN questions q ON q.id = p.question_id "
        "WHERE p.norm_key = ? AND q.status != 'rejected' LIMIT 1", (norm,)
    ).fetchone()
    if hit:
        return Verdict("duplicate", hit["question_id"], 1.0)

    # SequenceMatcher is the expensive half and this loop is O(bank) per
    # candidate. score = 0.6*jaccard + 0.4*seq and seq <= 1, so a row can only
    # reach VARIANT_AT if 0.6*jaccard + 0.4 >= VARIANT_AT. Below that jaccard
    # the row cannot be a variant or a duplicate no matter what seq comes back
    # as, so skipping it changes no verdict -- it only leaves best_sim
    # understated for rows that were never going to match.
    jac_floor = (VARIANT_AT - 0.4) / 0.6
    ta = _tokens(norm)
    best_id, best_sim = None, 0.0
    for row in conn.execute(
        "SELECT id AS qid, norm_key FROM questions WHERE status != 'rejected' "
        "UNION ALL "
        "SELECT p.question_id AS qid, p.norm_key FROM phrasings p "
        "  JOIN questions q ON q.id = p.question_id "
        " WHERE q.status != 'rejected' AND p.norm_key IS NOT NULL"
    ):
        key = row["norm_key"] or ""
        tb = _tokens(key)
        jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
        if jac < jac_floor:
            continue
        s = similarity(norm, key)
        if s > best_sim:
            best_id, best_sim = row["qid"], s

    if best_sim >= DUPLICATE_AT:
        return Verdict("duplicate", best_id, best_sim)
    if best_sim >= VARIANT_AT:
        return Verdict("variant", best_id, best_sim)
    return Verdict("new", None, best_sim)


def admit(
    conn: sqlite3.Connection,
    *,
    source_id: int | None,
    question_text: str,
    answer_text: str | None = None,
    locator: str | None = None,
    verbatim: str | None = None,
    origin: str = "published",
    kind: str | None = None,
    status: str = "needs_review",
) -> Verdict:
    """Run the gate and apply its verdict. Returns the verdict."""
    v = adjudicate(conn, question_text)
    q_text = question_text.strip()
    # Every candidate is logged with the verdict it got, including the ones
    # that never became questions. Without this there is no way to ask what
    # the gate threw away, which is the only way to tell a tight bank from a
    # lossy one.
    conn.execute(
        "INSERT INTO candidates (source_id, question_text, answer_text, locator, "
        "verdict, matched_id, similarity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, q_text, answer_text, locator, v.kind, v.matched_id,
         round(v.similarity, 4), now()),
    )
    # question_sources.verbatim_text is the provenance column: it must hold the
    # source's own words, not the model's rewrite of them, or there is nothing
    # to check a model answer against. Callers that parse the source directly
    # (docx, manual entry) pass no verbatim and their answer text *is* verbatim.
    evidence = verbatim if verbatim is not None else answer_text

    if v.kind == "rejected":
        conn.commit()      # the log row is the only trace this candidate existed
        return v

    if v.kind == "duplicate":
        if source_id is not None:
            conn.execute(
                "INSERT INTO question_sources "
                "(question_id, source_id, locator, verbatim_text) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(question_id, source_id, locator) DO UPDATE SET "
                "verbatim_text = excluded.verbatim_text "
                "WHERE excluded.verbatim_text IS NOT NULL",
                (v.matched_id, source_id, locator or "", evidence),
            )
        _adopt_answer_if_missing(conn, v.matched_id, answer_text)
        # A duplicate that is worded differently is still worth keeping as a phrasing.
        if v.similarity < 1.0:
            conn.execute(
                "INSERT OR IGNORE INTO phrasings (question_id, text, source_id, norm_key) VALUES (?, ?, ?, ?)",
                (v.matched_id, q_text, source_id, normalize(q_text)),
            )
        conn.commit()
        return v

    if v.kind == "variant":
        _adopt_answer_if_missing(conn, v.matched_id, answer_text)
        conn.execute(
            "INSERT OR IGNORE INTO phrasings (question_id, text, source_id, norm_key) VALUES (?, ?, ?, ?)",
            (v.matched_id, q_text, source_id, normalize(q_text)),
        )
        if source_id is not None:
            conn.execute(
                "INSERT INTO question_sources "
                "(question_id, source_id, locator, verbatim_text) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(question_id, source_id, locator) DO UPDATE SET "
                "verbatim_text = excluded.verbatim_text "
                "WHERE excluded.verbatim_text IS NOT NULL",
                (v.matched_id, source_id, locator or "", evidence),
            )
        conn.commit()
        return v

    topic = classify_topic(q_text, answer_text or "")
    cur = conn.execute(
        "INSERT INTO questions (canonical_text, kind, topic, difficulty, origin, "
        "status, created_at, norm_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            q_text,
            kind or kind_for_topic(topic),
            topic,
            estimate_difficulty(q_text, answer_text or ""),
            origin,
            status,
            now(),
            normalize(q_text),
        ),
    )
    qid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO answers (question_id, answer_key, rubric_points, common_mistakes, "
        "answer_status) VALUES (?, ?, ?, ?, ?)",
        (
            qid,
            answer_text,
            json.dumps(rubric_from_answer(answer_text or "")),
            json.dumps([]),
            "ok" if answer_text else "missing",
        ),
    )
    if source_id is not None:
        conn.execute(
            "INSERT INTO question_sources "
            "(question_id, source_id, locator, verbatim_text) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(question_id, source_id, locator) DO UPDATE SET "
            "verbatim_text = excluded.verbatim_text "
            "WHERE excluded.verbatim_text IS NOT NULL",
            (qid, source_id, locator or "", evidence),
        )
    conn.commit()
    v.matched_id = qid
    return v
