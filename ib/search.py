"""Full text search over the bank.

FTS5 with a porter stemmer, so "amortising" finds "amortisation" and the
question you half remember is findable by the word you half remember. The
index is kept current by triggers (migration 007), not by a rebuild step,
because an index that silently goes stale is worse than no index.
"""
from __future__ import annotations

import re
import sqlite3

# FTS5 treats these as operators. A question mark or a stray quote in a typed
# query would otherwise be a syntax error rather than a search.
_BARE = re.compile(r'^[\w\s*"]+$')


def _has_fts(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions_fts'"
    ).fetchone())


def _safe_query(query: str) -> str:
    """Pass real FTS5 syntax through; quote anything that would not parse."""
    if _BARE.match(query) and query.count('"') % 2 == 0:
        return query
    return '"' + query.replace('"', '""') + '"'


def find(conn: sqlite3.Connection, query: str, *, limit: int = 20,
         status: str | None = "active") -> list[dict]:
    where = " AND q.status = ?" if status else ""
    params: list = [_safe_query(query)]
    if status:
        params.append(status)
    params.append(limit)

    if _has_fts(conn):
        sql = f"""
            SELECT q.id, q.canonical_text, q.topic, q.status,
                   snippet(questions_fts, 1, '>', '<', ' ... ', 14) AS excerpt
              FROM questions_fts f
              JOIN questions q ON q.id = f.rowid
             WHERE questions_fts MATCH ?{where}
             ORDER BY bm25(questions_fts, 2.0, 1.0)
             LIMIT ?
        """
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.OperationalError:
            pass  # malformed MATCH expression: fall through to the substring path

    like = f"%{query}%"
    params = [like, like] + ([status] if status else []) + [limit]
    return [dict(r) | {"excerpt": None} for r in conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.status FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        "WHERE (q.canonical_text LIKE ? OR a.answer_key LIKE ?)"
        + (" AND q.status = ?" if status else "") + " ORDER BY q.id LIMIT ?", params)]


# ---------------------------------------------------------------- fuzzy

def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


# What a banker types versus what the book wrote. "ev bridge" has to find the
# enterprise value bridge or the search is useless to the person using it.
ABBREVIATIONS = {
    "ev": ["enterprise", "enterprise value"],
    "eqv": ["equity", "equity value"],
    "tev": ["enterprise", "enterprise value"],
    "wc": ["working", "working capital"],
    "nwc": ["working", "working capital"],
    "fcf": ["free cash flow", "cash flow"],
    "ufcf": ["unlevered", "free cash flow"],
    "tv": ["terminal", "terminal value"],
    "da": ["depreciation", "amortization"],
    "capex": ["capital expenditure", "capital expenditures"],
    "ppa": ["purchase price allocation"],
    "ad": ["accretion", "dilution"],
    "cf": ["cash flow"],
    "is": ["income statement"],
    "bs": ["balance sheet"],
    "pf": ["pro forma"],
    "mi": ["minority interest"],
    "nci": ["noncontrolling", "minority interest"],
    "dta": ["deferred tax"],
    "dtl": ["deferred tax"],
    "nol": ["net operating loss"],
    "sbc": ["stock based compensation", "compensation"],
    "tsm": ["treasury stock method"],
    "wacc": ["weighted average cost of capital"],
    "irr": ["internal rate of return"],
    "moic": ["multiple of invested capital"],
    "lbo": ["leveraged buyout"],
    "dcf": ["discounted cash flow"],
    "ma": ["merger", "acquisition"],
    "comps": ["comparable companies", "comparable"],
}


def _subsequence(needle: str, haystack: str) -> bool:
    """Every letter of needle appears in haystack, in order."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _score_one(term: str, tokens: list[str], joined: str) -> float:
    """How well one typed term matches one question. 0 means it does not.

    Subsequence matching is per token, never against the whole text: every
    short string is a subsequence of a 200-character question, so scoring that
    way made "ev" match essentially everything and ranked noise at the top.
    """
    if term in tokens:
        return 1.0
    # A one or two letter term is an abbreviation, not a fragment. Matching it
    # loosely matches the entire bank.
    if len(term) <= 2:
        return 0.0
    for t in tokens:
        if t.startswith(term):
            return 0.85
    if term in joined:
        return 0.7
    # Four characters is the floor for subsequence matching. Three lets "bta"
    # match "EBITDA" as readily as "beta" -- both contain b, t and a in order
    # -- and a search that answers a beta question with an EBITDA one is worse
    # than a search that answers nothing.
    if len(term) >= 4 and any(len(t) <= len(term) + 4 and _subsequence(term, t)
                              for t in tokens):
        return 0.45
    return 0.0


def _token_score(term: str, tokens: list[str], joined: str) -> float:
    """Best score across the term itself and anything it is shorthand for."""
    best = _score_one(term, tokens, joined)
    for expansion in ABBREVIATIONS.get(term, ()):
        parts = expansion.split()
        # An expansion only counts when the whole phrase is there: "enterprise"
        # alone should not satisfy someone who typed "ev".
        got = min((_score_one(part, tokens, joined) for part in parts), default=0.0)
        best = max(best, got * 0.95)
    return best


def fuzzy(conn: sqlite3.Connection, query: str, *, limit: int = 20,
          status: str | None = "active", min_score: float = 0.45) -> list[dict]:
    """Multi-token matching for the query you typed from memory.

    FTS5 is exact about tokens: it will not find "enterprise value bridge" from
    "ev bridg", and half the time that is what you actually type. This scores
    every term against the question, requires most of them to land, and ranks
    by how well. Slower than FTS -- it reads the bank -- but the bank is a few
    thousand rows, and being unable to find a question you know is in there is
    a worse cost than 40ms.
    """
    terms = _tokens(query)
    if not terms:
        return []

    rows = conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.status, a.answer_key "
        "FROM questions q LEFT JOIN answers a ON a.question_id = q.id"
        + (" WHERE q.status = ?" if status else ""),
        ([status] if status else []),
    ).fetchall()

    scored: list[tuple[float, dict]] = []
    for r in rows:
        q_text = r["canonical_text"] or ""
        q_tokens = _tokens(q_text)
        q_joined = " ".join(q_tokens)
        a_joined = " ".join(_tokens((r["answer_key"] or "")[:1200]))

        total = 0.0
        matched = 0
        for term in terms:
            in_q = _token_score(term, q_tokens, q_joined)
            # The answer is corroboration, never the main evidence: a term that
            # only appears in a long answer should not outrank a question that
            # is actually about it.
            in_a = _token_score(term, a_joined.split(), a_joined) * 0.35
            best = max(in_q, in_a)
            if best > 0:
                matched += 1
            total += best
        # Every term has to land when the query is short: "ev bridge" meaning
        # "anything with ev OR bridge" is not what anyone types it for.
        needed = len(terms) if len(terms) <= 3 else (len(terms) * 2 + 2) // 3
        if matched < needed:
            continue
        score = total / len(terms)
        if score >= min_score:
            scored.append((score, {
                "id": r["id"], "canonical_text": q_text, "topic": r["topic"],
                "status": r["status"], "score": round(score, 3), "excerpt": None,
            }))

    scored.sort(key=lambda t: (-t[0], len(t[1]["canonical_text"])))
    return [d for _, d in scored[:limit]]


def search(conn: sqlite3.Connection, query: str, *, limit: int = 20,
           status: str | None = "active", fuzzy_fallback: bool = True) -> tuple[list[dict], str]:
    """FTS first, fuzzy when it comes back empty. Returns (rows, which mode)."""
    rows = find(conn, query, limit=limit, status=status)
    if rows or not fuzzy_fallback:
        return rows, "exact"
    return fuzzy(conn, query, limit=limit, status=status), "fuzzy"


# ---------------------------------------------------------------- semantic search

import array
import math
import struct
from . import llm


def _dot(a, b) -> float:
    # zip would stop at the shorter of the two and return a plausible-looking
    # number for two vectors that cannot be compared at all. Two models'
    # vectors have different lengths, so that has to be an error, not a score.
    if len(a) != len(b):
        raise ValueError(f"cannot compare a {len(a)}-dim vector with a {len(b)}-dim one")
    return sum(x * y for x, y in zip(a, b))


def _norm(vec) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def _unit(vec: list[float]) -> list[float]:
    n = _norm(vec)
    return [x / n for x in vec]


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vector(data: bytes):
    # array is a C-level unpack and returns something that indexes and iterates
    # like a list. find_semantic does this once per stored row per query.
    out = array.array("f")
    out.frombytes(data)
    return out


def semantic_ready() -> str:
    """"" when `--semantic` can really run, else why it cannot.

    `find_semantic` has always fallen back to the lexical search rather than
    failing, which is right -- results beat an error. What was wrong is that
    nothing said so, and the header still read SEMANTIC, so a search that had
    quietly stopped being semantic looked exactly like one that had not. That
    matters more now than it did: Anthropic sells no embedding endpoint at
    all, so selecting Claude makes the fallback the normal case rather than
    the broken one.
    """
    if not llm.embeds():
        return (f"{llm.provider_label()} has no embeddings endpoint - "
                "searching by keyword instead")
    if not llm.available():
        return (f"no {llm.provider_label()} key - searching by keyword instead")
    return ""


def find_semantic(conn: sqlite3.Connection, query: str, *, limit: int = 20,
                  status: str | None = "active") -> list[dict]:
    """Semantic vector search over stored embeddings.

    Falls back to the lexical search whenever it cannot run; `semantic_ready`
    is how a caller finds out that is going to happen before it labels the
    results.
    """
    if not llm.available():
        return find(conn, query, limit=limit, status=status)
    try:
        q_vec = llm.embed(query)
    except Exception:
        return find(conn, query, limit=limit, status=status)

    # Stored vectors are already unit length (index_embeddings normalises), so
    # normalising the query too makes cosine similarity a plain dot product.
    # The old loop recomputed every stored vector's norm on every query: two
    # full passes over 768 floats per row, in Python, per search.
    q_vec = _unit(q_vec)

    has_emb = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone())
    if not has_emb:
        return find(conn, query, limit=limit, status=status)

    # Only this model's rows. Two models' vectors are different lengths, and
    # scoring across them silently returns a depressed, meaningless cosine
    # rather than an error.
    sql = """
        SELECT q.id, q.canonical_text, q.topic, q.status, e.vector
          FROM questions q
          JOIN embeddings e ON e.question_id = q.id
         WHERE (? IS NULL OR q.status = ?) AND e.model = ?
    """
    rows = conn.execute(sql, (status, status, llm.model_embed())).fetchall()
    if not rows:
        return find(conn, query, limit=limit, status=status)

    scored = []
    for r in rows:
        cos_sim = _dot(q_vec, _unpack_vector(r["vector"]))
        scored.append((cos_sim, {
            "id": r["id"],
            "canonical_text": r["canonical_text"],
            "topic": r["topic"],
            "status": r["status"],
            "excerpt": f"semantic match: {cos_sim:.0%}",
        }))

    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:limit]]


MAX_CONSECUTIVE_FAILURES = 3


def index_embeddings(conn: sqlite3.Connection, *, progress=print) -> int:
    """Backfill vector embeddings for questions that lack them.

    A batch at a time, not a question at a time. batchEmbedContents takes 100
    texts per request, so this bank is nine calls rather than eight hundred and
    forty-two, and the inter-call floor is paid nine times instead.

    The model is part of what "lacks one" means. Vectors from two models are
    not comparable -- different dimensions, different geometry -- so a row
    embedded under the old model counts as missing, and find_semantic only ever
    scores one model's rows.
    """
    # Two different "cannot run" answers, and they need different sentences.
    # Anthropic sells no embeddings endpoint at all, which is a capability
    # difference rather than a missing key -- `semantic_ready` already says so
    # for `find`, and this path used to discover it by failing three batches.
    # And the key it names is the configured provider's: hardcoded as
    # GEMINI_API_KEY, it told an OpenAI user their Google key was missing.
    if not llm.embeds():
        progress(f"{llm.provider_label()} has no embeddings endpoint - "
                 "`find` still works lexically")
        return 0
    if not llm.available():
        progress(f"no {llm.key_env()} available for embeddings")
        return 0
    model = llm.model_embed()
    rows = conn.execute("""
        SELECT q.id, q.canonical_text FROM questions q
         WHERE q.status != 'rejected'
           AND NOT EXISTS (SELECT 1 FROM embeddings e
                            WHERE e.question_id = q.id AND e.model = ?)
         ORDER BY q.id
    """, (model,)).fetchall()
    if not rows:
        progress("all questions already embedded")
        return 0
    progress(f"embedding {len(rows)} questions with {model}...")
    done, fails = 0, 0
    from .db import now
    for start in range(0, len(rows), llm.MAX_EMBED_BATCH):
        chunk = rows[start:start + llm.MAX_EMBED_BATCH]
        try:
            vecs = llm.embed_batch([r["canonical_text"] for r in chunk], model=model)
        except Exception as e:
            fails += 1
            progress(f"  batch failed ({fails}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            if fails >= MAX_CONSECUTIVE_FAILURES or not getattr(e, "retryable", True):
                progress("  " + (llm.give_up_note(e) if isinstance(e, llm.LLMError)
                                 else "giving up: repeated failures. Re-run to resume."))
                break
            continue
        fails = 0
        for r, vec in zip(chunk, vecs):
            # Stored unit length, so a query is one dot product per row rather
            # than a norm recomputed on every search for every row.
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (question_id, vector, model, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (r["id"], _pack_vector(_unit(vec)), model, now()),
            )
            done += 1
        conn.commit()
        progress(f"  embedded {done}/{len(rows)}")
    conn.commit()
    return done
