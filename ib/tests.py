"""Self-contained regression tests. No pytest, to match the rest of the tool.

    ./superday selftest

Everything runs against a throwaway database, so this never touches ib.db.
The cases here are the ones that were actually wrong at some point: each is a
bug that shipped, not a hypothetical.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .admission import (
    DUPLICATE_AT, TOPIC_RULES, VARIANT_AT, adjudicate, admit, classify_topic,
    kind_for_topic, normalize, similarity,
)
from . import analytics, backup, browse, chains, cli, clip, checks, crossaudit, dupes, enrich, history, llm, market, mock, plan as plan_mod, scheduler, search, session, tagging, tui, ui, usage, views
from .audit import _apply as audit_apply
from .db import connect, migrate, upsert_source
from .enrich import _apply as enrich_apply
from .ingest import epub as epub_mod
from .ingest import pdf as pdf_mod
from .ingest import pack as pack_mod, pipeline, sec as sec_mod, web as web_mod
from . import theme as theme_mod
from .topics import TOPICS, label as topic_label

_checks = 0


def similarity_uncapped(a: str, b: str) -> float:
    """What `admission.similarity` would return with no identity guard.

    Lets a test say which pairs the guard actually touched, rather than
    asserting absolute numbers that move whenever the wording in the case does.
    """
    from difflib import SequenceMatcher
    from .admission import _tokens
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return 0.6 * jac + 0.4 * SequenceMatcher(None, a, b).ratio()


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(label)


def fresh() -> tuple[sqlite3.Connection, int, int]:
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = connect(db)
    migrate(conn)
    a, _ = upsert_source(conn, kind="pdf", title="Guide A", file_hash="ha")
    b, _ = upsert_source(conn, kind="pdf", title="Guide B", file_hash="hb")
    return conn, a, b


@contextlib.contextmanager
def _stub_claude(payload: dict):
    """Answer the Anthropic endpoint with `payload` and capture what was sent.

    Yields the list of request bodies, so a test can assert on the request as
    well as on what the transport made of the response.
    """
    sent: list[dict] = []

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()

    def fake_open(req, **kw):
        sent.append(json.loads(req.data.decode()))
        return R()

    real_open = llm.urllib.request.urlopen
    real_key = os.environ.get("ANTHROPIC_API_KEY")
    llm.urllib.request.urlopen = fake_open
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        yield sent
    finally:
        llm.urllib.request.urlopen = real_open
        if real_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = real_key


# ---------------------------------------------------------------- kind

def test_behavioural_is_not_filed_as_technical() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, answer_text="Use STAR and keep it to ninety seconds.",
              question_text="Tell me about a time you led a team under pressure")
    row = conn.execute("SELECT kind, topic FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()
    check(row["topic"] == "behavioural", "fit question classified as " + row["topic"])
    check(row["kind"] == "behavioural", "fit question filed as kind=" + row["kind"])

    v2 = admit(conn, source_id=a, question_text="Walk me through a DCF and how you get to WACC",
               answer_text="Project unlevered FCF, discount at WACC, add terminal value.")
    check(conn.execute("SELECT kind FROM questions WHERE id = ?",
                       (v2.matched_id,)).fetchone()[0] == "technical",
          "technical question misfiled")
    check(kind_for_topic("behavioural") == "behavioural", "kind_for_topic")
    check(kind_for_topic(None) == "technical", "kind_for_topic default")


def test_rules_can_reach_every_topic() -> None:
    for q, want in [
        ("Walk me through your resume", "behavioural"),
        ("What are your strengths and weaknesses?", "behavioural"),
        ("Walk me through a DCF and how you get to WACC", "dcf"),
        ("How do you get from equity value to enterprise value?", "ev_eqv"),
        ("Walk me through an LBO and the IRR drivers", "lbo"),
        # The three product topics were added after the fact, and the risk was
        # never that they would not match -- it was that `markets` would win on
        # the bare word "spread" and `ma` on the bare word "target". Each of
        # these deliberately contains one of those decoys.
        ("What drives credit spreads on an investment grade bond?", "dcm"),
        ("How do you set IPTs and how far can you tighten the spread?", "dcm"),
        ("What is a greenshoe and how does stabilisation work after an IPO?", "ecm"),
        ("Walk me through a rights issue and how you set the TERP discount", "ecm"),
        ("Locked box or completion accounts for a target with volatile working capital?",
         "deal_process"),
        ("Why would a bidder use a scheme of arrangement rather than a contractual offer?",
         "deal_process"),
    ]:
        got = classify_topic(q)
        check(got == want, f"{q!r} classified {got}, wanted {want}")


def test_the_topic_vocabulary_has_exactly_one_definition() -> None:
    """Adding a topic used to mean editing five lists, and the one everybody
    forgot was a Gemini `responseSchema` enum -- which is validated before the
    quota check, so a missing entry 400'd the whole batch rather than one item.
    """
    for topic, _terms in TOPIC_RULES:
        check(topic in TOPICS, f"TOPIC_RULES names {topic!r}, absent from TOPICS")
    check(llm.ENRICH_SCHEMA["properties"]["topic"]["enum"] == list(TOPICS),
          "llm.ENRICH_SCHEMA topic enum has drifted from TOPICS")
    extract = llm.EXTRACT_SCHEMA["properties"]["questions"]["items"]["properties"]
    check(extract["topic"]["enum"] == list(TOPICS),
          "llm.EXTRACT_SCHEMA topic enum has drifted from TOPICS")
    check(enrich.BATCH_SCHEMA["properties"]["items"]["items"]["properties"]["topic"]["enum"]
          == list(TOPICS), "enrich.SCHEMA topic enum has drifted from TOPICS")
    for _round, spec in mock.ROUNDS.items():
        for t in spec["spread"]:
            check(t in TOPICS, f"mock round spread names unknown topic {t!r}")
    check(len(set(TOPICS)) == len(TOPICS), "TOPICS has a duplicate")
    for t in TOPICS:
        check(topic_label(t) != t, f"{t!r} has no human label")


def test_two_instruments_are_never_merged_into_one_question() -> None:
    """"Where is CHF against the euro" and "Where is GBP against the euro"
    share every word that is not the answer, scored 0.72, and the gate filed
    the second as a variant of the first -- so a Swiss franc answer would have
    been graded against a live sterling binding. The numeric guard did not
    catch it because the instrument is a word, not a digit."""
    conn, a, _ = fresh()
    v1 = admit(conn, source_id=a, question_text="Where is GBP against the euro?",
               kind="market_awareness")
    v2 = admit(conn, source_id=a, question_text="Where is CHF against the euro?",
               kind="market_awareness")
    check(v2.kind == "new", f"CHF filed as {v2.kind} of #{v2.matched_id}")
    check(v1.matched_id != v2.matched_id, "two currencies landed on one question")

    for x, y in [("What is the BTP-Bund spread?", "What is the OAT-Bund spread?"),
                 ("Where is Brent trading?", "Where is WTI trading?"),
                 ("Where is 3-month Euribor?", "Where is 3-month SOFR?")]:
        sim = similarity(normalize(x), normalize(y))
        check(sim < VARIANT_AT, f"{x!r} vs {y!r} scored {sim:.2f}, would merge")

    # And the guard must not fire on a genuine rephrasing, which is the whole
    # value of the gate.
    sim = similarity(normalize("Where is the 10-year Bund yield?"),
                     normalize("Where is the 10-year Bund yield trading?"))
    check(sim >= VARIANT_AT,
          f"a real rephrasing of the same instrument scored {sim:.2f} and would not merge")


# ---------------------------------------------------------------- packs

def _pack(tmp: Path, name: str, **over) -> Path:
    body = {
        "title": "Test Pack",
        "status": "needs_review",
        "items": [
            {"q": "What is a Schuldschein and how does it differ from a bond?",
             "a": "A German-law bilateral loan note, privately placed, "
                  "documented in a few pages and held to maturity at amortised cost.",
             "rubric": ["Says it is a loan note, not a security",
                        "Notes the investor base is held-to-maturity",
                        "Notes documentation is far lighter than a prospectus"],
             "topic": "dcm", "difficulty": 3, "tags": ["schuldschein"],
             "locator": "E1"},
        ],
    }
    body.update(over)
    path = tmp / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_a_malformed_pack_is_rejected_before_anything_is_written() -> None:
    """A pack is hundreds of rows landing on one command. Finding out that
    item 300 has no topic after 299 have been committed is not a position you
    can back out of, so validation is whole-file and happens first."""
    conn, _, _ = fresh()
    tmp = Path(tempfile.mkdtemp())
    before = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]

    for name, over, wanted in [
        ("no_topic.json", {"items": [{"q": "Q?", "a": "A.", "rubric": ["x", "y"],
                                      "topic": "bonds", "difficulty": 2}]}, "not one of"),
        ("bad_diff.json", {"items": [{"q": "Q?", "a": "A.", "rubric": ["x", "y"],
                                      "topic": "dcm", "difficulty": 9}]}, "difficulty"),
        ("thin_rubric.json", {"items": [{"q": "Q?", "a": "A.", "rubric": ["only one"],
                                         "topic": "dcm", "difficulty": 2}]}, "rubric"),
        ("no_items.json", {"items": []}, "items"),
        ("no_title.json", {"title": ""}, "title"),
    ]:
        path = _pack(tmp, name, **over)
        try:
            pack_mod.load(conn, path)
            check(False, f"{name} was accepted")
        except pack_mod.PackError as e:
            check(wanted in str(e), f"{name} complained {e!r}, wanted {wanted!r}")

    after = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    check(before == after, "a rejected pack still wrote rows")


def test_a_pack_keeps_its_own_topic_rubric_and_tags() -> None:
    """The whole point of the pack path is that the fields are decided in the
    file, not guessed. `admit` runs its lexical heuristics regardless, so the
    regression is the loader forgetting to overwrite them afterwards."""
    conn, _, _ = fresh()
    tmp = Path(tempfile.mkdtemp())
    res = pack_mod.load(conn, _pack(tmp, "p.json"))
    check(res["new"] == 1, f"landed {res['new']}")

    row = conn.execute(
        "SELECT q.topic, q.subtopic, q.difficulty, q.kind, q.status, "
        "a.answer_key, a.rubric_points, a.answer_status "
        "FROM questions q JOIN answers a ON a.question_id = q.id").fetchone()
    check(row["topic"] == "dcm", f"topic is {row['topic']}")
    check(row["difficulty"] == 3, f"difficulty is {row['difficulty']}")
    check(row["kind"] == "technical", f"kind is {row['kind']}")
    check(row["status"] == "needs_review", f"status is {row['status']}")
    check(row["answer_status"] == "ok", "answer_status not ok")
    check(len(json.loads(row["rubric_points"])) == 3,
          "rubric was overwritten by the heuristic split of the answer")
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN question_tags qt ON qt.tag_id = t.id")]
    check("schuldschein" in tags, f"tags are {tags}")


def test_a_pack_drop_is_one_batch_that_undo_reverses() -> None:
    """Landing straight at `active` inside `admit` -- which is what
    ingest-filing does -- leaves no history row, so `undo` cannot see a pack
    drop at all. That is worse than no undo, because it looks like it works."""
    conn, _, _ = fresh()
    tmp = Path(tempfile.mkdtemp())
    res = pack_mod.load(conn, _pack(tmp, "p.json", status="active"))
    check(res["promoted"] == 1, f"promoted {res['promoted']}")
    qid = conn.execute("SELECT id FROM questions").fetchone()["id"]
    check(conn.execute("SELECT status FROM questions WHERE id = ?",
                       (qid,)).fetchone()["status"] == "active", "not promoted")

    history.undo_batch(conn, res["batch"])
    check(conn.execute("SELECT status FROM questions WHERE id = ?",
                       (qid,)).fetchone()["status"] == "needs_review",
          "undo did not put the pack back")


def test_a_pack_cannot_smuggle_a_duplicate_past_the_gate() -> None:
    conn, a, _ = fresh()
    tmp = Path(tempfile.mkdtemp())
    admit(conn, source_id=a,
          question_text="What is a Schuldschein and how does it differ from a bond?",
          answer_text="A German-law loan note.")
    res = pack_mod.load(conn, _pack(tmp, "p.json"))
    check(res["new"] == 0 and res["duplicate"] == 1, f"gate returned {res}")
    check(conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"] == 1,
          "the pack landed a second copy")


def test_a_dry_run_pack_writes_nothing() -> None:
    conn, _, _ = fresh()
    tmp = Path(tempfile.mkdtemp())
    res = pack_mod.load(conn, _pack(tmp, "p.json"), dry_run=True)
    check(res["dry_run"] and res["topics"] == {"dcm": 1}, f"dry run said {res}")
    check(conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"] == 0,
          "dry run wrote rows")
    check(conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 2,
          "dry run created a source row")


# ---------------------------------------------------------------- provenance

def test_provenance_holds_source_words_not_the_rewrite() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="What is the mid-year convention in a DCF?",
              answer_text="A MODEL REWRITE", verbatim="THE BOOK'S OWN WORDS", locator="p1")
    got = conn.execute("SELECT verbatim_text FROM question_sources WHERE question_id = ?",
                       (v.matched_id,)).fetchone()[0]
    check(got == "THE BOOK'S OWN WORDS", f"provenance held {got!r}")


def test_callers_that_parse_the_source_keep_their_text_as_evidence() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="What is working capital and why does it matter?",
              answer_text="Current assets less current liabilities.", locator="p1")
    got = conn.execute("SELECT verbatim_text FROM question_sources WHERE question_id = ?",
                       (v.matched_id,)).fetchone()[0]
    check(got == "Current assets less current liabilities.", "docx fallback lost its evidence")


def test_reingest_repairs_provenance_but_never_wipes_it() -> None:
    conn, a, _ = fresh()
    q = "Walk me through a discounted cash flow analysis"
    v = admit(conn, source_id=a, question_text=q, answer_text="rewrite", locator="p1")
    read = lambda: conn.execute(
        "SELECT verbatim_text FROM question_sources WHERE question_id = ?",
        (v.matched_id,)).fetchone()[0]
    admit(conn, source_id=a, question_text=q, answer_text="rewrite",
          verbatim="REAL QUOTE", locator="p1")
    check(read() == "REAL QUOTE", "re-ingest did not repair provenance")
    admit(conn, source_id=a, question_text=q, answer_text=None, verbatim=None, locator="p1")
    check(read() == "REAL QUOTE", "an empty pass wiped good provenance")
    check(conn.execute("SELECT COUNT(*) FROM question_sources").fetchone()[0] == 1,
          "re-ingest duplicated the source link")


def test_grounding_rejects_a_quote_that_is_not_in_the_page() -> None:
    src = ("The mid-year convention assumes cash flows arrive evenly through the "
           "year, so you discount by t minus one half instead of t.")
    check(pdf_mod.grounded(
        "mid-year convention assumes cash flows arrive evenly through the year, "
        "so you discount", src), "a real quote was rejected")
    check(not pdf_mod.grounded(
        "The mid-year conversion assumes cash flows arrive at the very end of "
        "every single period", src), "an invented quote was accepted")
    check(not pdf_mod.grounded("too short", src), "a stub counted as evidence")
    # PDF text mangles whitespace and quote marks; matching must survive that.
    check(pdf_mod.grounded(
        "mid-year   convention\nassumes cash flows arrive evenly through the year, "
        "so you discount", src), "whitespace mangling broke the match")


# ---------------------------------------------------------------- dedup

def test_enrich_rewrites_the_key_the_gate_dedupes_on() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Walk me through a DCF and how you get to WACC",
              answer_text="Project unlevered FCF and discount it at WACC.")
    before = conn.execute("SELECT norm_key FROM questions WHERE id = ?",
                          (v.matched_id,)).fetchone()[0]
    enrich_apply(conn, v.matched_id, {
        "canonical_question": "Walk me through a DCF.", "topic": "dcf",
        "subtopic": "wacc", "difficulty": 3, "rubric_points": ["a"], "common_mistakes": [],
    })
    after = conn.execute("SELECT norm_key FROM questions WHERE id = ?",
                         (v.matched_id,)).fetchone()[0]
    check(after != before, "norm_key was left stale after enrichment")
    check(after == normalize("Walk me through a DCF."), f"norm_key is {after!r}")
    check(adjudicate(conn, "Walk me through a DCF.").kind == "duplicate",
          "the gate stopped recognising its own enriched question")


def test_enrich_moves_kind_with_topic() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Why do you want to do this job exactly?",
              answer_text="Tie the answer to concrete deal exposure.")
    enrich_apply(conn, v.matched_id, {
        "canonical_question": "Why investment banking?", "topic": "behavioural",
        "subtopic": "motivation", "difficulty": 2, "rubric_points": ["a"],
        "common_mistakes": [],
    })
    check(conn.execute("SELECT kind FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()[0] == "behavioural",
          "kind did not follow topic through enrichment")


def test_prefilter_cannot_change_a_verdict() -> None:
    """The gate skips rows below a jaccard floor to avoid the expensive
    comparison. score = 0.6*jac + 0.4*seq and seq <= 1, so the floor is exactly
    the point below which VARIANT_AT is unreachable. Assert that algebra."""
    floor = (VARIANT_AT - 0.4) / 0.6
    for jac in (0.0, 0.1, floor - 0.01):
        check(0.6 * jac + 0.4 * 1.0 < VARIANT_AT,
              f"jaccard {jac} could still have reached variant")
    check(0.6 * floor + 0.4 * 1.0 >= VARIANT_AT, "the floor is set too high")


def test_equity_and_enterprise_are_identity_not_words() -> None:
    """The two halves of the core mental model differ by one ordinary word.

    "How do you determine whether a given transaction changes Equity Value" and
    the same sentence ending "Enterprise Value" scored 0.837 against a
    duplicate threshold of 0.88 -- four hundredths from one silently absorbing
    the other. This is the currency guard's failure in the vocabulary the bank
    is actually about.
    """
    a = normalize("How do you determine whether a given transaction or event "
                  "changes Equity Value?")
    b = normalize("How do you determine whether a given transaction or event "
                  "changes Enterprise Value?")
    check(similarity(a, b) < VARIANT_AT,
          f"the CSE/NOA pair scored {similarity(a, b):.3f} and could be merged")

    # TEV, EV and "enterprise value" are one concept under three spellings, so
    # the guard compares concepts, not tokens. A flat token set would make a
    # genuine reworded duplicate look like two different instruments instead --
    # the same mistake pointing the other way.
    from .admission import _identity
    for spelling in ("enterprise value", "tev", "ev ebitda"):
        check(_identity(normalize(spelling)) == {"enterprise"},
              f"{spelling!r} did not resolve to the enterprise-value concept")
    check(_identity(normalize("what is equity value")) == {"equity"},
          "equity value did not resolve to its own concept")
    # The guard only ever caps a score, so the claim to check is that it caps
    # the pair that names two different things and leaves the pair that names
    # one thing twice alone. Two spellings of enterprise value score whatever
    # their wording earns; enterprise against equity is held below the variant
    # threshold however similar the rest of the sentence is.
    same = normalize("How do you calculate Enterprise Value for this company?")
    synonym = normalize("How do you calculate TEV for this company?")
    other = normalize("How do you calculate Equity Value for this company?")
    check(similarity(same, synonym) == similarity_uncapped(same, synonym),
          "two spellings of one concept were penalised as different instruments")
    check(similarity(same, other) < VARIANT_AT < similarity_uncapped(same, other),
          "enterprise against equity was not held below the variant threshold")

    # Levered and unlevered are the same shape of mistake.
    e = normalize("Why do you discount Unlevered Free Cash Flow at WACC?")
    f = normalize("Why do you discount Levered Free Cash Flow at WACC?")
    check(similarity(e, f) < DUPLICATE_AT,
          f"levered and unlevered scored {similarity(e, f):.3f} as duplicates")


def test_numbers_are_identity_not_words() -> None:
    a = normalize("Where is the 10-year trading?")
    b = normalize("Where is the 2-year trading?")
    check(similarity(a, b) < DUPLICATE_AT, "different tenors merged as duplicates")


def test_a_reworded_duplicate_is_stored_once() -> None:
    conn, a, b = fresh()
    admit(conn, source_id=a, question_text="Walk me through a discounted cash flow analysis",
          answer_text="Project unlevered FCF and discount at WACC.")
    alt = "Walk me through a discounted cash flow analysis for me"
    for _ in range(3):
        v = admit(conn, source_id=b, question_text=alt, answer_text="x" * 40)
    check(v.kind in ("duplicate", "variant"), f"reword admitted as {v.kind}")
    n = conn.execute("SELECT COUNT(*) FROM phrasings WHERE text = ?", (alt,)).fetchone()[0]
    check(n == 1, f"phrasing stored {n} times")


def test_enrich_keeps_the_wording_the_source_used() -> None:
    """The bug this exists for: enrich canonicalised the text, the gate then
    stopped recognising the phrasing the book actually printed, and a re-read
    of the same page re-admitted the question as new."""
    conn, a, _ = fresh()
    printed = ("Let's say you have a company's Diluted Equity Value. How do you "
               "move from Equity Value to Enterprise Value?")
    v = admit(conn, source_id=a, question_text=printed,
              answer_text="Subtract non-operating assets and add funded liabilities.")
    enrich_apply(conn, v.matched_id, {
        "canonical_question": "How do you move from Equity Value to Enterprise Value?",
        "topic": "ev_eqv", "subtopic": "bridge", "difficulty": 2,
        "rubric_points": ["a"], "common_mistakes": [],
    })
    kept = conn.execute(
        "SELECT COUNT(*) FROM phrasings WHERE question_id = ? AND norm_key = ?",
        (v.matched_id, normalize(printed))).fetchone()[0]
    check(kept == 1, "enrich discarded the source's own wording")

    again = adjudicate(conn, printed)
    check(again.kind == "duplicate",
          f"re-reading the same page would admit it as {again.kind}")
    check(again.matched_id == v.matched_id, "matched the wrong question")


def test_the_gate_matches_a_near_miss_on_an_old_wording() -> None:
    conn, a, _ = fresh()
    printed = ("Let's say you have a company's Diluted Equity Value. How do you "
               "move from Equity Value to Enterprise Value?")
    v = admit(conn, source_id=a, question_text=printed, answer_text="x" * 40)
    enrich_apply(conn, v.matched_id, {
        "canonical_question": "How do you move from Equity Value to Enterprise Value?",
        "topic": "ev_eqv", "subtopic": "bridge", "difficulty": 2,
        "rubric_points": ["a"], "common_mistakes": [],
    })
    # Not word for word: the next book prints it slightly differently.
    got = adjudicate(conn, "Say you have a company's Diluted Equity Value. How do "
                           "you move from Equity Value to Enterprise Value?")
    check(got.kind in ("duplicate", "variant"),
          f"a near miss on the old wording came back {got.kind}")


def test_phrasings_carry_a_key_so_the_gate_can_use_them() -> None:
    conn, a, b = fresh()
    admit(conn, source_id=a, question_text="Walk me through a discounted cash flow analysis",
          answer_text="Discount unlevered FCF at WACC.")
    alt = "Walk me through a discounted cash flow analysis for me"
    admit(conn, source_id=b, question_text=alt, answer_text="x" * 40)
    row = conn.execute("SELECT norm_key FROM phrasings WHERE text = ?", (alt,)).fetchone()
    check(row is not None, "phrasing was not stored")
    check(row["norm_key"] == normalize(alt), f"phrasing key is {row['norm_key']!r}")


# ---------------------------------------------------------------- answers

def test_a_later_source_fills_a_missing_answer() -> None:
    conn, a, b = fresh()
    q = "Walk me through a discounted cash flow analysis"
    v = admit(conn, source_id=a, question_text=q, answer_text=None)
    check(conn.execute("SELECT answer_status FROM answers WHERE question_id = ?",
                       (v.matched_id,)).fetchone()[0] == "missing", "setup")
    admit(conn, source_id=b, question_text=q,
          answer_text="Project unlevered FCF, discount at WACC, add terminal value.")
    row = conn.execute("SELECT answer_key, answer_status, rubric_points FROM answers "
                       "WHERE question_id = ?", (v.matched_id,)).fetchone()
    check(row["answer_status"] == "ok", "answer_status not updated")
    check(row["answer_key"].startswith("Project unlevered"), "answer not adopted")
    check(json.loads(row["rubric_points"]), "rubric not rebuilt on adoption")


def test_a_later_source_never_overwrites_an_answer() -> None:
    conn, a, b = fresh()
    q = "Walk me through a discounted cash flow analysis"
    v = admit(conn, source_id=a, question_text=q, answer_text="THE FIRST ANSWER, which is fine.")
    admit(conn, source_id=b, question_text=q, answer_text="A CONTRADICTORY SECOND ANSWER.")
    check(conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                       (v.matched_id,)).fetchone()[0].startswith("THE FIRST"),
          "a disagreeing source overwrote a good answer")


# ---------------------------------------------------------------- gate log

def test_every_candidate_is_logged_with_its_verdict() -> None:
    conn, a, b = fresh()
    q = "Walk me through a discounted cash flow analysis"
    admit(conn, source_id=a, question_text=q, answer_text="Discount unlevered FCF at WACC.")
    admit(conn, source_id=b, question_text=q, answer_text="Discount unlevered FCF at WACC.")
    rows = conn.execute("SELECT verdict FROM candidates ORDER BY id").fetchall()
    check([r[0] for r in rows] == ["new", "duplicate"], f"gate log: {[r[0] for r in rows]}")


# ---------------------------------------------------------------- cross-audit

def _seed(conn, source_id, question, answer="Discount unlevered FCF at WACC."):
    return admit(conn, source_id=source_id, question_text=question,
                 answer_text=answer).matched_id


def test_a_second_opinion_never_overwrites_the_first() -> None:
    """The whole point of the pass: two verdicts, both readable. If Claude's
    landed on top of Gemini's there would be nothing left to disagree with."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    audit_apply(conn, qid, {"verdict": "keep", "confidence": 0.95}, "b1")
    crossaudit.record(conn, qid, {"verdict": "reject", "reason": "wrong formula",
                                  "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="claude-opus-5")
    conn.commit()

    providers = [r[0] for r in conn.execute(
        "SELECT provider FROM audits WHERE question_id = ? ORDER BY id", (qid,))]
    check(providers == ["gemini", "claude-code"], f"providers stored: {providers}")
    kept = conn.execute("SELECT audit_verdict FROM questions WHERE id = ?",
                        (qid,)).fetchone()[0]
    check(kept == "keep", f"cross-audit clobbered Gemini's verdict: {kept!r}")


def test_the_dangerous_disagreement_sorts_first() -> None:
    """Gemini letting something in that Claude rejects is the case that puts a
    wrong answer in front of you. The reverse only costs you a question."""
    conn, a, _ = fresh()
    kept_but_wrong = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    rejected_but_fine = _seed(conn, a, "How do you get from equity value to enterprise value?")

    audit_apply(conn, kept_but_wrong, {"verdict": "keep", "confidence": 0.9}, "b1")
    audit_apply(conn, rejected_but_fine, {"verdict": "reject", "reason": "x",
                                          "confidence": 0.9}, "b1")
    crossaudit.record(conn, kept_but_wrong,
                      {"verdict": "reject", "reason": "reverses the EV bridge",
                       "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    crossaudit.record(conn, rejected_but_fine,
                      {"verdict": "keep", "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()

    rows = crossaudit.disagreements(conn)
    check(len(rows) == 2, f"expected 2 disagreements, got {len(rows)}")
    check(rows[0]["id"] == kept_but_wrong,
          "the question being drilled with a wrong answer did not sort first")
    s = crossaudit.summary(conn)
    check(s["disagree"] == 2 and s["agree"] == 0, f"summary miscounted: {s}")
    check(s["second_only_reject"] == 1,
          f"second_only_reject: {s['second_only_reject']}")


def test_agreement_is_counted_as_agreement() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    audit_apply(conn, qid, {"verdict": "keep", "confidence": 0.9}, "b1")
    crossaudit.record(conn, qid, {"verdict": "keep", "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()
    s = crossaudit.summary(conn)
    check(s["agree"] == 1 and s["disagree"] == 0, f"summary: {s}")
    check(crossaudit.disagreements(conn) == [], "agreement showed up as a disagreement")


def test_only_the_latest_verdict_per_provider_counts() -> None:
    """Re-running the pass must not leave an old opinion arguing with a new one."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    audit_apply(conn, qid, {"verdict": "keep", "confidence": 0.9}, "b1")
    crossaudit.record(conn, qid, {"verdict": "reject", "reason": "first pass",
                                  "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    crossaudit.record(conn, qid, {"verdict": "keep", "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()
    check(crossaudit.disagreements(conn) == [],
          "a superseded verdict was still being compared")


def test_a_malformed_verdict_file_is_refused_row_by_row() -> None:
    """The Claude Code path means hand-editable JSON, so every field is checked
    before anything is written rather than trusted and repaired afterwards."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    good, problems = crossaudit.validate(conn, [
        {"question_id": qid, "verdict": "keep", "confidence": 0.9},
        {"question_id": qid, "verdict": "keep", "confidence": 0.9},      # duplicate
        {"question_id": 999999, "verdict": "keep", "confidence": 0.9},   # no such question
        {"question_id": qid, "verdict": "maybe", "confidence": 0.9},     # not a verdict
        {"question_id": qid, "verdict": "reject", "confidence": 0.9},    # reject with no reason
        {"question_id": qid, "verdict": "keep", "confidence": 4},        # out of range
        {"question_id": qid, "verdict": "keep"},                         # no confidence
        {"verdict": "keep", "confidence": 0.9},                          # no id
    ])
    check(len(good) == 1, f"kept {len(good)} rows, wanted 1")
    check(len(problems) == 7, f"reported {len(problems)} problems, wanted 7")


def test_a_question_is_not_handed_out_twice() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET status = 'active', audit_verdict = 'keep' "
                 "WHERE id = ?", (qid,))
    conn.commit()
    check([r["id"] for r in crossaudit.pending(conn, target="kept")] == [qid], "setup")
    crossaudit.record(conn, qid, {"verdict": "keep", "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()
    check(crossaudit.pending(conn, target="kept") == [],
          "a question already cross-audited was exported again")


def test_the_kept_target_is_what_gemini_let_through() -> None:
    conn, a, _ = fresh()
    kept = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    unreviewed = _seed(conn, a, "How do you get from equity value to enterprise value?")
    conn.execute("UPDATE questions SET status='active', audit_verdict='keep' WHERE id=?",
                 (kept,))
    conn.execute("UPDATE questions SET status='needs_review' WHERE id=?", (unreviewed,))
    conn.commit()
    check([r["id"] for r in crossaudit.pending(conn, target="kept")] == [kept],
          "the kept target picked up something Gemini never approved")
    check(unreviewed in [r["id"] for r in crossaudit.pending(conn, target="needs_review")],
          "the needs_review target missed a queued question")


def test_a_gemini_verdict_written_before_the_audits_table_still_pairs() -> None:
    """audit_verdict predates the audits table, so anything audited outside it
    would silently look like 'Claude has no one to disagree with'."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET audit_verdict='keep', audit_reason='fine', "
                 "audit_version=2 WHERE id=?", (qid,))
    conn.execute("DELETE FROM audits WHERE question_id=?", (qid,))
    conn.commit()
    migrate(conn)   # the reconciliation runs on every startup
    row = conn.execute("SELECT verdict FROM audits WHERE question_id=? AND "
                       "provider='gemini'", (qid,)).fetchone()
    check(row is not None and row[0] == "keep", "orphaned Gemini verdict was not healed")
    migrate(conn)
    n = conn.execute("SELECT COUNT(*) FROM audits WHERE question_id=? AND "
                     "provider='gemini'", (qid,)).fetchone()[0]
    check(n == 1, f"backfill is not idempotent, wrote {n} rows")


def test_an_unrecognised_verdict_is_never_an_acceptance() -> None:
    """Gemini has returned a verdict outside its own schema enum in the wild.
    The fall-through used to treat anything unknown as keep, which promotes a
    question into the drill bank on a verdict nobody gave."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    outcome = audit_apply(conn, qid, {"verdict": "hold", "confidence": 0.99}, "b1")
    conn.commit()
    check(outcome == "held", f"unknown verdict was applied as {outcome!r}")
    status = conn.execute("SELECT status FROM questions WHERE id=?", (qid,)).fetchone()[0]
    check(status == "needs_review", f"unknown verdict promoted the question to {status!r}")
    stored = conn.execute("SELECT verdict FROM audits WHERE question_id=?",
                          (qid,)).fetchone()[0]
    check(stored in ("keep", "fix", "reject"), f"stored an off-schema verdict: {stored!r}")


# ---------------------------------------------------------------- undo

def test_a_bulk_accept_can_be_taken_back() -> None:
    conn, a, _ = fresh()
    ids = [_seed(conn, a, q) for q in (
        "Walk me through a discounted cash flow analysis",
        "How do you get from equity value to enterprise value?",
        "Walk me through an LBO and the IRR drivers",
    )]
    batch = history.new_batch()
    for qid in ids:
        history.set_status(conn, qid, "active", action="accept-all", batch_id=batch)
    conn.commit()
    check(all(conn.execute("SELECT status FROM questions WHERE id=?", (i,)).fetchone()[0]
              == "active" for i in ids), "setup: not all accepted")

    n = history.undo_batch(conn, history.last_batch(conn)["batch_id"])
    check(n == 3, f"undo reverted {n} of 3")
    check(all(conn.execute("SELECT status FROM questions WHERE id=?", (i,)).fetchone()[0]
              == "needs_review" for i in ids), "undo did not restore the old status")


def test_undo_only_reverses_the_last_decision() -> None:
    conn, a, _ = fresh()
    first = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    second = _seed(conn, a, "How do you get from equity value to enterprise value?")
    history.set_status(conn, first, "active", action="review", batch_id=history.new_batch())
    history.set_status(conn, second, "rejected", action="review", batch_id=history.new_batch())
    conn.commit()
    history.undo_batch(conn, history.last_batch(conn)["batch_id"])
    check(conn.execute("SELECT status FROM questions WHERE id=?", (first,)).fetchone()[0]
          == "active", "undo reached back past the last batch")
    check(conn.execute("SELECT status FROM questions WHERE id=?", (second,)).fetchone()[0]
          == "needs_review", "the last batch was not reverted")


def test_a_rewritten_question_can_be_taken_back() -> None:
    """`history` covered status and answers. The question text -- the third
    thing a command can change about a question -- was written with a raw
    UPDATE from `edit`, from `audit`'s fix verdict and from `enrich`'s
    canonicalisation, and `undo` could see none of them. It still reported
    success, which is the failure mode worse than having no undo at all.

    And a rewrite that lands on a question that already exists is refused:
    `norm_key` is what the gate dedupes on and the gate only runs at ingest,
    so nothing downstream would notice two questions sharing one wording.
    """
    conn, a, _ = fresh()
    first = admit(conn, source_id=a, question_text="Walk me through a DCF",
                  answer_text="Project unlevered FCF and discount at WACC." * 3).matched_id
    second = admit(conn, source_id=a, question_text="What is working capital",
                   answer_text="Current assets less current liabilities." * 3).matched_id
    conn.execute("UPDATE questions SET status = 'active'")
    conn.commit()

    batch = history.new_batch()
    check(history.set_question(conn, second, "Define working capital, please",
                               action="edit", batch_id=batch),
          "a real rewrite reported no change")
    conn.commit()
    check(conn.execute("SELECT canonical_text FROM questions WHERE id = ?",
                       (second,)).fetchone()[0] == "Define working capital, please",
          "the rewrite did not land")
    check(conn.execute("SELECT norm_key FROM questions WHERE id = ?",
                       (second,)).fetchone()[0] == normalize("Define working capital, please"),
          "the gate key was left behind by the rewrite")

    n = history.undo_batch(conn, batch)
    check(n == 1, f"undo reverted {n} changes, not the one rewrite")
    check(conn.execute("SELECT canonical_text FROM questions WHERE id = ?",
                       (second,)).fetchone()[0] == "What is working capital",
          "undo left the rewritten question rewritten")

    # A no-op is not a history row, so `undo` has nothing to preview for it.
    check(not history.set_question(conn, second, "What is working capital",
                                   action="edit", batch_id=history.new_batch()),
          "rewriting a question to what it already said was logged as a change")

    # And the collision is refused rather than silently making a duplicate.
    try:
        history.set_question(conn, second, "Walk me through a DCF",
                             action="edit", batch_id=history.new_batch())
        raise AssertionError("a rewrite onto an existing question was allowed")
    except history.Collision as e:
        check(str(first) in str(e), f"the refusal does not name the clash: {e}")


def test_undo_is_itself_in_the_history() -> None:
    """Otherwise the next undo would reverse the undo's own rows and oscillate."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    history.set_status(conn, qid, "active", action="review", batch_id=history.new_batch())
    conn.commit()
    history.undo_batch(conn, history.last_batch(conn)["batch_id"])
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM question_status_history ORDER BY id")]
    check(actions == ["review", "undo"], f"history: {actions}")
    check(history.last_batch(conn)["action"] == "review",
          "last_batch offered an undo as the next thing to undo")


def test_a_status_change_that_changes_nothing_is_not_logged() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    b = history.new_batch()
    check(history.set_status(conn, qid, "active", action="review", batch_id=b), "setup")
    check(not history.set_status(conn, qid, "active", action="review", batch_id=b),
          "a no-op status change was recorded as a change")


# ---------------------------------------------------------------- find

def test_search_finds_a_question_by_a_word_only_in_its_answer() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis",
                "Project unlevered free cash flow, then discount at WACC.")
    conn.execute("UPDATE questions SET status='active' WHERE id=?", (qid,))
    conn.commit()
    hits = [r["id"] for r in search.find(conn, "unlevered")]
    check(hits == [qid], f"answer text was not searchable: {hits}")


def test_search_follows_an_edited_answer() -> None:
    """A stale index is worse than none: it returns the text you already fixed."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis",
                "Project unlevered free cash flow, then discount at WACC.")
    conn.execute("UPDATE questions SET status='active' WHERE id=?", (qid,))
    conn.execute("UPDATE answers SET answer_key=? WHERE question_id=?",
                 ("Use a perpetuity growth terminal value.", qid))
    conn.commit()
    check(search.find(conn, "unlevered") == [], "the index still has the old answer")
    check([r["id"] for r in search.find(conn, "perpetuity")] == [qid],
          "the index did not pick up the new answer")


def test_search_does_not_blow_up_on_punctuation() -> None:
    """A typed question mark is FTS5 syntax, not a search term."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET status='active' WHERE id=?", (qid,))
    conn.commit()
    for q in ('what is a "DCF"?', "cash flow?", "AND", "a OR"):
        search.find(conn, q)   # must not raise
    check(True, "unreachable")


# ---------------------------------------------------------------- export

def test_export_carries_the_half_that_cannot_be_rebuilt() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                 "VALUES (?, '2026-01-01T00:00:00+00:00', 3, 'self')", (qid,))
    conn.commit()
    out = Path(tempfile.mkdtemp()) / "dump.json"
    path, counts = backup.export_json(conn, out)
    data = json.loads(path.read_text())
    check(counts["reviews"] == 1, "reviews were not exported")
    check(data["tables"]["reviews"][0]["question_id"] == qid, "review lost its question")
    check("questions" in data["tables"] and "audits" in data["tables"],
          "export skipped a table")


def test_a_snapshot_is_a_working_database() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.commit()
    out = Path(tempfile.mkdtemp()) / "snap.sqlite"
    backup.snapshot(conn, out)
    restored = connect(out)
    got = restored.execute("SELECT canonical_text FROM questions WHERE id=?",
                           (qid,)).fetchone()[0]
    check(got.startswith("Walk me through"), "snapshot did not restore")


# ---------------------------------------------------------------- rendering

def test_an_answer_keeps_its_list_structure() -> None:
    """Answers arrive hard-wrapped at whatever column the source PDF used, so
    reflowing has to survive a bullet that spans two source lines."""
    out = ui.body("Key points:\n- discount at WACC across the\nprojection period\n"
                  "- add a terminal value\n\nCommon mistake: forgetting to discount it.")
    lines = out.splitlines()
    check(any(l.strip().startswith("- discount at WACC") for l in lines), f"lost the bullet:\n{out}")
    check(not any("projection period" in l and l.strip().startswith("-") and
                  "discount at WACC" not in l for l in lines),
          f"bullet continuation became its own bullet:\n{out}")
    check("" in lines, "paragraph break was dropped")
    check(all(len(l) <= ui.W + 20 for l in lines), "a line ran past the terminal width")


def test_a_placeholder_rubric_does_not_print_the_answer_twice() -> None:
    """Before `enrich` runs, the rubric is the answer's first five sentences
    cut at 220 characters -- so every point is a literal substring of the
    prose. The reveal leads with the rubric and keeps the prose one keystroke
    behind it, which for these questions meant the card and the `Full` tab
    held the same words, and the pointer between them promised a written
    answer that was already on screen. 493 of 1,086 active questions were in
    that state."""
    from .admission import rubric_from_answer, rubric_is_the_answer
    answer = ("A basic LBO model begins by setting assumptions for the purchase "
              "price and the debt equity split. Next you construct a sources "
              "and uses table to size the sponsor cheque. Finally you calculate "
              "the exit equity value to get to an IRR.")
    placeholder = rubric_from_answer(answer)
    check(placeholder, "the fixture produced no heuristic rubric at all")
    check(rubric_is_the_answer(placeholder, answer),
          "the heuristic rubric was not recognised as the answer's own sentences")

    real = ["States that the sponsor cheque is what is left after debt sizing.",
            "Explains that the exit multiple drives the IRR more than paydown."]
    check(not rubric_is_the_answer(real, answer),
          "an enriched rubric was mistaken for the placeholder")
    check(not rubric_is_the_answer([], answer), "an empty rubric is not the answer")
    check(not rubric_is_the_answer(placeholder, ""), "no answer, nothing to duplicate")

    # And the screen acts on it: no `Full` tab when it would hold the same text.
    rec = {"rubric_points": placeholder, "answer_key": answer,
           "common_mistakes": []}
    names = [n for n, _ in cli._question_panes(None, rec)]
    check("Full" not in names, f"the redundant tab survived: {names}")

    rec_real = dict(rec, rubric_points=real)
    check("Full" in [n for n, _ in cli._question_panes(None, rec_real)],
          "a question with a real rubric lost its written answer")


def test_an_empty_answer_says_so_rather_than_printing_nothing() -> None:
    check("nothing on file" in ui.body(""), "empty answer rendered as silence")
    check("nothing on file" in ui.body("   \n  "), "whitespace answer rendered as silence")


# ---------------------------------------------------------------- quarantine & undo & export

def test_due_questions_quarantines_claude_rejections() -> None:
    conn, a, _ = fresh()
    clean_id = _seed(conn, a, "What is WACC and how is it calculated?")
    bad_id = _seed(conn, a, "How does goodwill impairment flow through the three statements?")
    audit_apply(conn, clean_id, {"verdict": "keep", "confidence": 0.95}, "b1")
    audit_apply(conn, bad_id, {"verdict": "keep", "confidence": 0.95}, "b1")

    # Claude rejects bad_id
    crossaudit.record(conn, bad_id, {"verdict": "reject", "reason": "balance sheet does not balance",
                                     "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="claude-opus-5")
    conn.commit()

    due = [r["id"] for r in scheduler.due_questions(conn)]
    check(clean_id in due, "clean question was omitted from due_questions")
    check(bad_id not in due, "rejected question was not quarantined from due_questions")

    # include_quarantined=True includes it
    all_due = [r["id"] for r in scheduler.due_questions(conn, include_quarantined=True)]
    check(bad_id in all_due, "include_quarantined=True did not return quarantined question")


def test_answer_history_and_undo() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through an LBO", answer="Initial answer text.")
    conn.commit()
    batch = history.new_batch()
    history.set_answer(conn, qid, "Edited answer text.", action="edit", batch_id=batch)
    conn.commit()

    row = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?", (qid,)).fetchone()
    check(row["answer_key"] == "Edited answer text.", "set_answer did not update answer")

    last = history.last_batch(conn)
    check(last["batch_id"] == batch, f"last_batch mismatch: {last}")

    reverted = history.undo_batch(conn, batch)
    check(reverted == 1, f"undo_batch reverted {reverted}")

    row_after = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?", (qid,)).fetchone()
    check(row_after["answer_key"] == "Initial answer text.", "undo did not restore old answer")


def test_anki_export_format() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "What is terminal value in a DCF?",
                answer="PV of all future cash flows beyond projection.")
    conn.execute("UPDATE questions SET status = 'active', topic = 'dcf' WHERE id = ?", (qid,))
    conn.commit()
    out = Path(tempfile.mkdtemp()) / "anki.tsv"
    path, count = backup.export_anki(conn, out)
    check(count == 1, f"export_anki count: {count}")
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    check(len(lines) == 1, f"expected 1 line in anki tsv, got: {len(lines)}")
    parts = lines[0].split("\t")
    check(len(parts) == 3, f"expected 3 tab-separated columns, got: {len(parts)}")
    check("What is terminal value" in parts[0], "front column mismatch")
    check("PV of all future cash flows" in parts[1], "back column mismatch")
    check("superday dcf technical" in parts[2], "tags column mismatch")


def test_dupes_scanner_finds_similar_pairs() -> None:
    k1 = normalize("What makes a good LBO candidate?")
    k2 = normalize("What makes a great LBO candidate?")
    sim = similarity(k1, k2)
    check(sim >= 0.70, f"similarity was {sim:.2f}")


def test_dynamic_width_bounds() -> None:
    w = ui.width()
    check(70 <= w <= 96, f"dynamic width out of bounds: {w}")


def test_pdf_chunks_overlap() -> None:
    pages = [f"Page {i} content text with enough characters to pass min filter" for i in range(1, 15)]
    chunk_list = list(pdf_mod.chunks(pages, pages_per_chunk=4, overlap=1, min_chars=10))
    check(len(chunk_list) >= 4, f"expected at least 4 chunks, got: {len(chunk_list)}")
    check("p1-4" in chunk_list[0][0], f"first chunk locator: {chunk_list[0][0]}")
    check("p4-7" in chunk_list[1][0], f"second chunk locator (overlapping): {chunk_list[1][0]}")


def test_pending_missing_answers_filter() -> None:
    conn, a, _ = fresh()
    qid1 = _seed(conn, a, "Question with answer", answer="Detailed model answer.")
    qid2 = _seed(conn, a, "How does depreciation flow through the financial statements?", answer="")
    conn.commit()

    missing = [r["id"] for r in enrich.pending_missing_answers(conn)]
    check(qid2 in missing, "empty answer was omitted from pending_missing_answers")
    check(qid1 not in missing, "populated answer was wrongly marked as missing")


def test_search_vector_packing_and_similarity() -> None:
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [1.0, 0.0, 0.0]
    vec3 = [0.0, 1.0, 0.0]
    packed = search._pack_vector(vec1)
    unpacked = search._unpack_vector(packed)
    check(len(unpacked) == 3 and abs(unpacked[0] - 1.0) < 1e-5, "vector packing roundtrip failed")
    dot1 = search._dot(vec1, vec2)
    dot2 = search._dot(vec1, vec3)
    check(abs(dot1 - 1.0) < 1e-5, f"identical vectors dot product: {dot1}")
    check(abs(dot2 - 0.0) < 1e-5, f"orthogonal vectors dot product: {dot2}")


def test_mock_personas_exist() -> None:
    from . import grade as grade_mod
    for name in ("standard", "skeptical_md", "exacting_vp"):
        check(name in mock.PERSONAS, f"{name} persona missing from mock")
        check(name in grade_mod.PERSONAS,
              f"{name} persona has no grading instructions, so it only changes the label")
        spec = mock.PERSONAS[name]
        check(0 < spec["followup_below"] <= 1, f"{name} follow-up threshold out of range")
        check(spec["followup_rate"] >= 1, f"{name} never follows up")


def test_a_stricter_persona_presses_harder() -> None:
    check(mock.PERSONAS["exacting_vp"]["followup_below"]
          > mock.PERSONAS["standard"]["followup_below"],
          "the exacting VP is no harder to satisfy than the standard interviewer")


def test_the_three_axes_are_scored_separately() -> None:
    """A candidate at 80% technical and 30% resilient is not "a 55% candidate";
    averaging the axes hides the only thing worth acting on."""
    entries = [
        {"score": 0.9, "structure": 2, "followups": [{"score": 0.2}]},
        {"score": 0.8, "structure": 2, "followups": [{"score": 0.3}]},
    ]
    a = mock.axes(entries)
    check(abs(a["technical"] - 0.85) < 1e-6, f"technical was {a['technical']}")
    check(abs(a["communication"] - 0.25) < 1e-6, f"communication was {a['communication']}")
    check(abs(a["resilience"] - 0.25) < 1e-6, f"resilience was {a['resilience']}")
    check(a["pressed"] == 2, "follow-ups were not counted")


def test_an_unmeasured_axis_reads_as_unknown_not_zero() -> None:
    a = mock.axes([{"score": 0.9, "structure": 4, "followups": []}])
    check(a["resilience"] is None,
          "a mock with no follow-ups reported 0% resilience, which is a lie")
    check(a["technical"] is not None and a["communication"] is not None,
          "measurable axes were dropped")


def test_the_verdict_names_the_failure_mode() -> None:
    knows_cant_say = mock._verdict_line(0.6, {"technical": 0.9, "communication": 0.3,
                                              "resilience": None, "pressed": 0})
    check("landing" in knows_cant_say, f"got: {knows_cant_say}")
    folds = mock._verdict_line(0.6, {"technical": 0.9, "communication": 0.9,
                                     "resilience": 0.2, "pressed": 3})
    check("folds" in folds or "pressed" in folds, f"got: {folds}")


def test_local_delivery_scoring_is_honest_about_its_ceiling() -> None:
    from . import grade as grade_mod
    check(grade_mod.structure_floor("") == 1, "an empty answer scored above the floor")
    check(grade_mod.structure_floor("yes") <= 2, "a three-word answer scored well")
    check(grade_mod.structure_floor("word " * 400) <= 2, "a four-minute monologue scored well")
    good = ("First, you take unlevered free cash flow, then you discount it at WACC, "
            "because that reflects the blended cost of capital across the structure.")
    check(grade_mod.structure_floor(good) == 4, "a well signposted answer was not recognised")
    check(max(grade_mod.structure_floor(t) for t in ("", "yes", good)) <= 4,
          "the local reading claimed a 5, which it cannot know")


def test_a_mock_asks_the_question_the_way_a_source_printed_it() -> None:
    """`drill` has served a random phrasing since the table existed. `mock`
    always read the canonical, which is the one screen where being asked it in
    unfamiliar words matters most: you are timed, and you cannot ask for the
    question again."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="What is Working Capital?",
              answer_text="Current assets less current liabilities." * 3)
    qid = v.matched_id
    conn.execute("INSERT INTO phrasings (question_id, text, norm_key) VALUES (?, ?, ?)",
                 (qid, "How do you calculate Working Capital?",
                  normalize("How do you calculate Working Capital?")))
    conn.commit()
    q = conn.execute("SELECT id, canonical_text FROM questions WHERE id = ?",
                     (qid,)).fetchone()
    seen = {mock.phrasing_for(conn, q) for _ in range(60)}
    check(seen == {"What is Working Capital?",
                   "How do you calculate Working Capital?"},
          f"mock served {seen}")

    bare = admit(conn, source_id=a, question_text="Walk me through a DCF model",
                 answer_text="Project unlevered FCF and discount at WACC." * 3)
    q2 = conn.execute("SELECT id, canonical_text FROM questions WHERE id = ?",
                      (bare.matched_id,)).fetchone()
    check(mock.phrasing_for(conn, q2) == "Walk me through a DCF model",
          "a question with no phrasings on file was asked as something else")


def test_a_mock_writes_its_reviews_at_the_scorecard() -> None:
    """The scorecard is where a mock's ratings reach the schedule, and it was
    reading `asked` -- a name that only ever existed inside the question loop
    next door. Every mock therefore ended in a NameError on the first
    question: no scorecard, and not one review written for a whole sitting.

    So this asserts the sitting actually lands, and that the wording carried
    into `reviews.phrasing` is the one that was put on screen rather than the
    canonical, which is what `recap` reads back."""
    conn, a, _ = fresh()
    qid = _active(conn, a, "What is Working Capital, and how is it calculated?")
    q = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    asked = "How do you calculate Working Capital?"

    # graded=True with no result is the "it was answered, nothing graded it"
    # path, which self-rates 3 and asks nothing, so the test needs no input.
    with contextlib.redirect_stdout(io.StringIO()):
        mock.scorecard(conn, [(q, asked, "current assets less current liabilities",
                               12.0, None)],
                       "screen", 90.0, True, "standard")

    row = conn.execute(
        "SELECT question_id, phrasing, rating FROM reviews WHERE question_id = ?",
        (qid,)).fetchone()
    check(row is not None, "a mock sitting recorded no review at all")
    check(row["phrasing"] == asked,
          f"the review kept {row['phrasing']!r}, not the wording that was asked")
    check(conn.execute("SELECT 1 FROM schedule WHERE question_id = ?",
                       (qid,)).fetchone() is not None,
          "a mock answer never reached the schedule")


def test_pressing_enter_on_a_live_question_is_not_a_wrong_answer() -> None:
    """Enter means "reveal it, I will rate myself" everywhere else in a drill,
    and it has to mean that on a bound market question too. It used to fall
    through to the numeric compare, find no number in an answer nobody gave,
    and hand back a 1 -- a lapse written into the schedule for pressing the
    key that means "show me"."""
    conn, a, _ = fresh()
    # No stored answer, which is what a bound question looks like: the print
    # is the answer and it expires, so nothing is written down.
    qid = _active(conn, a, "Where is the US 10-year Treasury yield trading?",
                  answer=None, topic="markets")
    conn.execute("UPDATE questions SET kind = 'market_awareness' WHERE id = ?", (qid,))
    conn.execute(
        "INSERT INTO live_bindings (question_id, provider, series_key, unit, tolerance) "
        "VALUES (?, 'treasury', '10 Yr', '%', 0.10)", (qid,))
    # A fresh cached print, so nothing here touches the network.
    conn.execute(
        "INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
        "VALUES ('treasury', '10 Yr', 4.25, ?, ?)",
        (datetime.now(timezone.utc).date().isoformat(),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    q = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()

    line, rating = cli._grade_market(conn, q, "")
    check(rating is None, f"an unanswered live question was rated {rating}")
    check("4.25" in line, "the print was hidden, so there was nothing to self-rate against")

    # A real answer is still graded, and still graded numerically.
    _, good = cli._grade_market(conn, q, "about 4.3")
    check(good == 4, f"an answer inside tolerance scored {good}")
    _, wrong = cli._grade_market(conn, q, "about 9")
    check(wrong == 1, f"an answer nowhere near the print scored {wrong}")

    # And the reveal that now follows must not send you to a command that
    # skips this question by design: `enrich` leaves bound questions alone
    # (market.UNBOUND_SQL), so "run enrich" was an instruction with nothing
    # behind it.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._reveal(conn, q)
    shown = buf.getvalue()
    check("enrich" not in shown,
          "the reveal pointed a bound question at a pass that never touches it")
    check("expires" in shown, f"the reveal said nothing useful: {shown!r}")


def test_the_export_carries_every_table_that_cannot_be_rebuilt() -> None:
    """`export` is the only backup story this database has, and its own output
    says it carries "the half that cannot be rebuilt by re-running
    extraction". It stopped at `answer_history`, so the sittings, the tag map,
    the pairs judged distinct, the phrasings judged fine and every hand-edited
    stem were quietly left out of it."""
    permanent = {
        "audits", "reviews", "schedule", "notes", "sessions",
        "question_status_history", "answer_history", "question_text_history",
        "question_line_review", "question_pair_review", "phrasing_review",
        "tags", "question_tags",
    }
    missing = permanent - set(backup.TABLES)
    check(not missing, f"the export drops permanent tables: {sorted(missing)}")

    # And it really writes them, rather than merely listing them.
    conn, a, _ = fresh()
    qid = _active(conn, a, "Walk me through the three financial statements")
    tagging.attach(conn, qid, ["3-statement-linkage"])
    other = _active(conn, a, "Walk me through the three statements, briefly")
    session.open_session(conn, "drill", [qid], {"count": 1})
    dupes.settle(conn, qid, other)
    conn.commit()

    out = Path(tempfile.mkdtemp()) / "export.json"
    _, counts = backup.export_json(conn, out)
    data = json.loads(out.read_text())["tables"]
    for name in ("sessions", "tags", "question_tags", "question_pair_review"):
        check(counts.get(name), f"{name} was exported empty")
        check(data.get(name), f"{name} is not in the written file")


def test_a_rejected_key_names_the_key_that_was_rejected() -> None:
    """`classify` is handed the transport's own label -- "Gemini", "Claude",
    "OpenAI" -- and matched it against a lowercase literal, which is never
    equal. So a 401 from Anthropic told you to check GEMINI_API_KEY, and
    OpenAI had no branch at all: the one message whose whole job is naming the
    variable you have to fix named the wrong one on two of three providers."""
    for label, env in (("Gemini", "GEMINI_API_KEY"),
                       ("Claude", "ANTHROPIC_API_KEY"),
                       ("OpenAI", "OPENAI_API_KEY")):
        e = llm.classify(label, 401, "{}")
        check(env in e.hint, f"{label} 401 pointed at {e.hint!r}, not {env}")
        check(env.lower() in e.hint, f"{label} 401 gave no settings key: {e.hint!r}")
    # An unknown label must still produce a sentence rather than a KeyError.
    check(llm.classify("Something", 401, "{}").hint, "an unknown provider lost its hint")


def test_an_outside_opinion_is_one_the_first_pass_did_not_give() -> None:
    """`consult` picks the questions nothing outside the tool has looked at.
    "Outside" was spelled `NOT IN ('gemini')`, so the moment the provider
    setting moved to Claude or OpenAI the *first* audit counted as an outside
    opinion and every question `audit` had touched dropped out of the batch --
    which is the whole bank, and exactly the questions worth a second reader.
    """
    conn, a, _ = fresh()
    qid = _active(conn, a, "Why might two companies have different WACCs?")
    for provider in llm.PRIMARY_PROVIDERS:
        conn.execute(
            "INSERT INTO audits (question_id, provider, model, audit_version, "
            "verdict, confidence, ran_at) VALUES (?, ?, 'm', 1, 'keep', 0.9, ?)",
            (qid, provider, "2026-01-01"))
    conn.commit()
    from . import consult
    picked = [it["id"] for it in consult.select(conn, limit=10)]
    check(qid in picked,
          "a question only the first pass has seen was treated as already reviewed")

    conn.execute(
        "INSERT INTO audits (question_id, provider, model, audit_version, "
        "verdict, confidence, ran_at) VALUES (?, 'gpt-5', 'm', 1, 'keep', 0.9, ?)",
        (qid, "2026-01-02"))
    conn.commit()
    check(qid not in [it["id"] for it in consult.select(conn, limit=10)],
          "a question an outside model has judged was offered again")


def test_settling_a_pair_survives_being_listed_again() -> None:
    """`dupes --all` exists to show the pairs you already settled. It emptied
    the settled set to stop them being filtered out, which also emptied the
    lookup that marks them -- so every row came back reading `settled: False`
    and the mode could not tell a decided pair from an open one."""
    conn, a, _ = fresh()
    q1 = _active(conn, a, "What is an LBO and why does leverage help returns?")
    # Written straight in rather than through the gate, because the gate would
    # have merged it -- which is precisely how these pairs arise: `enrich`
    # canonicalises two questions toward one another long after admission.
    second = "What is an LBO, and why does leverage help the returns?"
    cur = conn.execute(
        "INSERT INTO questions (canonical_text, kind, topic, difficulty, origin, "
        "status, created_at, norm_key) VALUES (?, 'technical', 'lbo', 2, "
        "'published', 'active', ?, ?)",
        (second, "2026-01-01", normalize(second)))
    q2 = int(cur.lastrowid)
    conn.commit()
    open_pairs = dupes.pairs(conn)
    check(len(open_pairs) == 1, f"the scan found {len(open_pairs)} pairs, wanted 1")
    check(open_pairs[0]["settled"] is False, "an undecided pair read as settled")

    dupes.settle(conn, q1, q2)
    check(dupes.pairs(conn) == [], "a settled pair was proposed again")
    shown = dupes.pairs(conn, include_settled=True)
    check(len(shown) == 1, "--all did not show the settled pair back")
    check(shown[0]["settled"] is True,
          "--all showed the settled pair without saying it was settled")


def test_a_provider_with_no_embeddings_says_so_rather_than_failing_batches() -> None:
    """Anthropic sells no embeddings endpoint. `find --semantic` has always
    said so; the backfill discovered it by failing three batches, and the
    sentence it printed on the way named GEMINI_API_KEY whatever the provider
    was."""
    conn, a, _ = fresh()
    _active(conn, a, "What is the cost of equity and how do you get to it?")

    said: list[str] = []
    saved = _with_provider("claude", {"ANTHROPIC_API_KEY": "k"})
    try:
        check(search.index_embeddings(conn, progress=said.append) == 0,
              "the backfill tried to embed on a provider with no endpoint")
    finally:
        _restore(saved)
    check(any("no embeddings endpoint" in line for line in said),
          f"it did not say why it could not run: {said}")
    check(not any("GEMINI" in line for line in said),
          f"it named Gemini's key on a Claude run: {said}")

    # With no key at all, the sentence names the key the *configured* provider
    # wants rather than Gemini's.
    said.clear()
    saved = _with_provider("openai", {})
    try:
        check(search.index_embeddings(conn, progress=said.append) == 0,
              "the backfill ran with no key")
    finally:
        _restore(saved)
    check(any("OPENAI_API_KEY" in line for line in said),
          f"it asked for the wrong key: {said}")


def test_mock_never_asks_the_same_question_twice() -> None:
    conn, a, _ = fresh()
    for i in range(6):
        _active(conn, a, f"Accounting question number {i} on revenue recognition",
                topic="accounting")
    picked = mock.pick(conn, {"minutes": 20, "count": 6,
                              "spread": ["accounting", "accounting", "valuation"]})
    ids = [r["id"] for r in picked]
    check(len(ids) == len(set(ids)), f"the same question was queued twice: {ids}")


def test_completion_is_context_aware() -> None:
    """Completion now runs off a pure function, not readline's callback.

    The old test drove readline directly, which meant the full-screen shell
    could not be covered by it at all.
    """
    from .cli import build_parser, build_completer
    complete = build_completer(build_parser())

    def at(buf: str) -> list[str]:
        return [c.value for c in complete(buf, len(buf))[0]]

    check("accounting" in at("drill -t a"), "drill -t a did not complete accounting")
    check("superday" in at("mock s"), "mock s did not complete superday")
    check("--semantic" in at("find --"), "find -- did not complete --semantic")
    check("drill" in at(""), "an empty line did not offer the commands")
    check("--weak" in at("w --we"), "a hotkey did not resolve to its command's flags")

    # The word under the cursor is what gets replaced, and nothing after it.
    cands, start_ = complete("drill -t acc ounting", len("drill -t acc"))
    check(start_ == len("drill -t "), f"replacement started at {start_}")
    values = [c.value for c in cands]
    check("accounting" in values, f"mid-line completion missed accounting: {values}")

    # The menu shows what each suggestion does, and that text comes off the
    # parser rather than a second hand-kept list that could drift from it.
    drill = next(c for c in complete("dri", 3)[0] if c.value == "drill")
    check(bool(drill.hint), "the command menu had no description to show")
    weak = next(c for c in complete("drill --we", 10)[0] if c.value == "--weak")
    check("fail" in weak.hint.lower() or bool(weak.hint),
          f"flag suggestion had no help: {weak}")


def test_shell_line_editing() -> None:
    """The editor replaces readline, so its bindings are now our regression."""
    from .tui import Editor, Key

    def press(ed, *names):
        for n in names:
            ed.handle(Key("char", n) if len(n) == 1 and n.isprintable() and n != " "
                      else Key(n))

    ed = Editor()
    for ch in "drill -t dcf":
        ed.handle(Key("char", ch))
    check(ed.buf == "drill -t dcf", ed.buf)
    ed.handle(Key("ctrl-w"))
    check(ed.buf == "drill -t ", f"ctrl-w did not kill one word: {ed.buf!r}")
    ed.handle(Key("ctrl-a"))
    check(ed.pos == 0, "ctrl-a did not go home")
    ed.handle(Key("ctrl-k"))
    check(ed.buf == "", f"ctrl-k from home did not clear: {ed.buf!r}")

    # History only rewinds once the buffer has been committed, and ^C-style
    # clearing must not resurrect it.
    ed.remember("drill --weak")
    ed.remember("stats")
    ed.handle(Key("up"))
    check(ed.buf == "stats", ed.buf)
    ed.handle(Key("up"))
    check(ed.buf == "drill --weak", ed.buf)
    ed.handle(Key("down"))
    ed.handle(Key("down"))
    check(ed.buf == "", f"walking off the end of history left {ed.buf!r}")

    # Enter hands back the line and leaves the editor empty for the next one.
    ed.handle(Key("char", "x"))
    check(ed.handle(Key("enter")) == "x", "enter did not commit the line")
    check(ed.buf == "" and ed.pos == 0, "enter left the buffer dirty")


def test_suggestions_open_only_where_a_vocabulary_is_being_typed() -> None:
    """The menu is for command names and flags, not for prose.

    Popping a command list up while you are typing a spoken drill answer
    covers the question you are answering, which is the one thing on screen
    you needed.
    """
    from .cli import build_parser, build_completer
    from .tui import Editor, Key

    ed = Editor(completer=build_completer(build_parser()))

    def typed(text: str) -> Editor:
        ed.clear()
        for ch in text:
            ed.handle(Key("char", ch))
        return ed

    check(typed("dri").menu, "typing a command name offered nothing")
    check(all(c.value.startswith("dri") for c in ed.menu), "menu ignored the prefix")
    check(typed("drill --we").menu, "typing a flag offered nothing")
    check(typed("drill -t acc").menu, "a flag's allowed values offered nothing")
    check(not typed("drill --weak ").menu, "a trailing space left the menu open")
    check(not typed("higher current liabilities").menu,
          "prose opened the command menu")
    check(not typed("/enterprise value").menu, "a search phrase opened a menu")

    # An exact, unambiguous match is not a suggestion worth covering the
    # screen for -- Enter has to submit it, not re-accept it.
    check(not typed("dashboard").menu, "a fully typed command still showed a menu")

    # Escape dismisses it for the word you are on; the next character is a new
    # intention and brings it back, which is what makes it feel like a hint
    # rather than a mode you have to get out of.
    typed("dri")
    ed.menu_dismissed = True
    ed.refresh_menu()
    check(not ed.menu, "escape did not close the menu")
    ed.handle(Key("char", "l"))
    check(ed.menu, "typing after escape did not bring suggestions back")


def test_accepting_a_suggestion_replaces_only_that_word() -> None:
    from .cli import build_parser, build_completer
    from .tui import Editor, Key

    ed = Editor(completer=build_completer(build_parser()))
    for ch in "drill --to":
        ed.handle(Key("char", ch))
    ed.handle(Key("tab"))
    check(ed.buf.startswith("drill --topic"), f"tab produced {ed.buf!r}")
    check(ed.pos == len(ed.buf), "the cursor did not follow the insertion")

    # Mid-line: the tail survives.
    ed.clear()
    for ch in "drill -t acc --weak":
        ed.handle(Key("char", ch))
    ed.pos = len("drill -t acc")
    ed.refresh_menu()
    ed.accept()
    check(ed.buf == "drill -t accounting --weak", f"tail was lost: {ed.buf!r}")


def test_key_decoder_reads_real_terminal_bytes() -> None:
    """Every escape sequence here is one a terminal actually sends."""
    import os
    from .tui import MouseEvent, Reader

    def decode(raw: bytes) -> list:
        r, w = os.pipe()
        os.write(w, raw)
        os.close(w)
        rd = Reader(r)
        out = []
        while True:
            ev = rd.read(0.05)
            if ev is None:
                break
            out.append(ev)
        os.close(r)
        return out

    names = [str(k) for k in decode(b"\x1b[A\x1b[B\x1b[C\x1b[D")]
    check(names == ["up", "down", "right", "left"], f"arrows decoded as {names}")

    names = [str(k) for k in decode(b"\x1b[5~\x1b[6~\x1b[3~\x1b[Z")]
    check(names == ["pgup", "pgdn", "del", "btab"], f"nav keys decoded as {names}")

    # Application cursor mode: the same arrows, a different prefix.
    check([str(k) for k in decode(b"\x1bOA")] == ["up"], "application-mode up failed")

    # A lone ESC is only a lone ESC once nothing follows it.
    check([str(k) for k in decode(b"\x1b")] == ["esc"], "bare ESC did not decode")
    check([str(k) for k in decode(b"\x1bg")] == ["alt-g"], "alt-g did not decode")

    ctrls = [str(k) for k in decode(b"\x01\x05\x17\x7f\r\t")]
    check(ctrls == ["ctrl-a", "ctrl-e", "ctrl-w", "bs", "enter", "tab"],
          f"control keys decoded as {ctrls}")

    # Multi-byte characters survive being split across reads.
    check([str(k) for k in decode("é→".encode())] == ["é", "→"], "utf-8 was mangled")

    ev = decode(b"\x1b[<0;12;7M")
    check(ev == [MouseEvent("press", 6, 11)], f"mouse press decoded as {ev}")
    ev = decode(b"\x1b[<64;1;1M")
    check(ev[0].kind == "wheel-up", f"wheel decoded as {ev}")


def test_no_escape_codes_reach_a_pipe() -> None:
    """Colour is for a terminal. A redirected `find` has to be plain text.

    paint() used to check only whether the palette resolved to a colour, so
    at depth 0 it still emitted the bold attribute and every heading arrived
    in the log file wrapped in escape bytes.
    """
    from . import ui
    ui.reset_depth()
    try:
        ui._DEPTH = 0
        for rendered in (ui.head("BANK"), ui.paint("x", "accent", ui.BOLD),
                         ui.gradient("superday"), ui.dim("quiet"),
                         ui.chip("resume"), ui.spinner_frame(3),
                         ui.meter(0.5, 6), ui.hairline(8)):
            check("\x1b" not in rendered, f"escape leaked: {rendered!r}")
    finally:
        ui.reset_depth()


def test_styled_text_is_measured_not_counted() -> None:
    """The frame is only rectangular if every cut counts cells, not bytes."""
    from . import ui
    ui.reset_depth()
    styled = ui.head("valuation") + " " + ui.dim("methodologies")
    check(ui.vlen(styled) == len("valuation methodologies"),
          f"vlen counted escapes: {ui.vlen(styled)}")
    cut = ui.truncate(styled, 12)
    check(ui.vlen(cut) == 12, f"truncate produced {ui.vlen(cut)} cells")
    check(ui.vlen(ui.pad(cut, 40)) == 40, "pad did not fill to the target")

    parts = ui.hard_wrap(styled, 10)
    check(all(ui.vlen(x) <= 10 for x in parts), f"hard_wrap overflowed: {parts}")
    check(sum(ui.vlen(x) for x in parts) == ui.vlen(styled),
          "hard_wrap dropped or duplicated characters")


def test_prose_reflows_to_the_width_it_is_given() -> None:
    """A gutter narrower than the frame must not leave orphaned words.

    This is the bug where an expanded answer wrapped to the full frame and was
    then cut to the gutter, leaving a ragged second column of two-word lines.
    """
    from . import ui
    text = ("Working Capital tells you whether a company needs more in "
            "Operational Assets or Operational Liabilities to run its business, "
            "which is the whole point of looking at it at all.")
    for w in (40, 62, 90):
        lines = ui.body(text, "", w).split("\n")
        check(all(ui.vlen(x) <= w for x in lines),
              f"body({w}) produced a line of {max(ui.vlen(x) for x in lines)}")
        check(len(lines) > 1, "the sample should have wrapped")


def test_scheduler_cache() -> None:
    s1 = scheduler.scheduler()
    s2 = scheduler.scheduler()
    check(s1 is s2, "scheduler instance was not cached")


def test_tag_crud_and_link() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="How do you calculate Enterprise Value from Equity Value?",
              answer_text="EV = Equity Value + Total Debt + Preferred Stock + Noncontrolling Interest - Cash",
              status="active")
    qid = v.matched_id
    enrich.link_tags(conn, qid, ["ev-bridge", "enterprise-value", "net-debt"])
    conn.commit()

    tags = conn.execute("""
        SELECT t.name, t.kind FROM question_tags qt
        JOIN tags t ON t.id = qt.tag_id
        WHERE qt.question_id = ?
        ORDER BY t.name
    """, (qid,)).fetchall()
    tag_names = [t["name"] for t in tags]
    check("ev-bridge" in tag_names, "ev-bridge tag was not linked")
    check("enterprise-value" in tag_names, "enterprise-value tag was not linked")
    check("net-debt" in tag_names, "net-debt tag was not linked")

    # Test untagging
    conn.execute("""
        DELETE FROM question_tags
        WHERE question_id = ? AND tag_id IN (SELECT id FROM tags WHERE name = 'net-debt')
    """, (qid,))
    conn.commit()
    remaining = [r[0] for r in conn.execute(
        "SELECT t.name FROM question_tags qt JOIN tags t ON t.id = qt.tag_id WHERE qt.question_id = ?", (qid,)
    ).fetchall()]
    check("net-debt" not in remaining, "net-debt was not untagged")
    check("ev-bridge" in remaining, "ev-bridge was wrongly removed")


def test_due_questions_tag_filter() -> None:
    conn, a, _ = fresh()
    v1 = admit(conn, source_id=a, question_text="What are the key levers of an LBO model?",
               answer_text="Debt paydown, EBITDA growth, multiple expansion.", status="active")
    v2 = admit(conn, source_id=a, question_text="Walk me through the three financial statements",
               answer_text="Income statement, balance sheet, cash flow statement.", status="active")

    enrich.link_tags(conn, v1.matched_id, ["lbo-levers", "irr-drivers"])
    enrich.link_tags(conn, v2.matched_id, ["3-statement", "accounting"])
    conn.commit()

    # Filter by specific tag
    lbo_qs = scheduler.due_questions(conn, tag="lbo-levers")
    check(len(lbo_qs) == 1, f"expected 1 question for lbo-levers tag, got {len(lbo_qs)}")
    check(lbo_qs[0]["id"] == v1.matched_id, "wrong question returned for tag filter")

    # Filter by topic as fallback
    acct_qs = scheduler.due_questions(conn, tag="accounting")
    check(len(acct_qs) >= 1, "topic fallback in tag filter failed")


def test_dashboard_queries() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="What is WACC and how is it calculated?",
              answer_text="Cost of equity times weight plus cost of debt times weight times (1-tax).", status="active")
    conn.commit()

    # Verify query for dashboard runs cleanly
    total_q = conn.execute("SELECT COUNT(*) c FROM questions WHERE status = 'active'").fetchone()["c"]
    check(total_q >= 1, "dashboard total_q count query failed")
    due_24h = conn.execute("SELECT COUNT(*) c FROM schedule WHERE due_at <= datetime('now', '+1 day')").fetchone()["c"]
    check(due_24h is not None, "due_24h query failed")



# ---------------------------------------------------------------- layout

def test_window_border_is_rectangular() -> None:
    """The right border used to sit two columns out from the top border, and a
    single overlong row pushed it out further and wrecked the whole frame."""
    lines = ["short", "x" * 400, ui.ok("styled") + " tail", ""]
    out = ui.window("A TITLE", lines, footer="footer text").split("\n")
    widths = {ui.vlen(l) for l in out}
    check(len(widths) == 1, f"window rows have differing widths: {sorted(widths)}")


def test_a_wide_character_is_two_columns_not_one() -> None:
    """`vlen` was `len()` on the stripped string, so it counted characters and
    called them columns. Every boxed layout in the tool is measured through it,
    and a CJK or emoji character draws two cells wide -- so one of them made
    the frame one column narrower than the border it was measured against, two
    panels away from where it was introduced. The bank is Latin today; the
    German sources and a pasted note are not a guarantee it stays that way."""
    check(ui.vlen("abc") == 3, "ascii stopped measuring as itself")
    check(ui.vlen("\u4e2d\u6587") == 4, "a CJK pair did not measure as four columns")
    check(ui.vlen("\U0001f600") == 2, "an emoji did not measure as two columns")
    # Combining marks draw on the character before them and take no column.
    check(ui.vlen("e\u0301") == 1, "a combining accent claimed a column of its own")
    # Ambiguous-width glyphs are one column, which is what a Latin font draws
    # and what every rule and box in ui.SQUARE depends on.
    check(ui.vlen("\u2500" * 10) == 10, "the box-drawing set stopped measuring as one each")
    check(ui.vlen(ui.head("BANK")) == 4, "styling was counted as width again")

    # And the cut agrees with the measure, or a truncated cell tears the frame.
    for n in (1, 2, 3, 4, 5):
        cut = ui.truncate("\u4e2d\u6587\u5b57\u4e2d\u6587", n)
        check(ui.vlen(cut) <= n,
              f"truncate to {n} produced {ui.vlen(cut)} columns: {cut!r}")


def test_truncate_does_not_leak_style_or_miscount() -> None:
    styled = ui.ok("hello") + " " + ui.bad("world")
    cut = ui.truncate(styled, 8)
    check(ui.vlen(cut) == 8, f"truncate produced {ui.vlen(cut)} visible chars, wanted 8")
    check(ui.vlen(ui.truncate("abc", 10)) == 3, "truncate padded a short string")
    check(ui.pad(ui.ok("ab"), 6) and ui.vlen(ui.pad(ui.ok("ab"), 6)) == 6,
          "pad measured escape bytes as width")


def test_heat_separates_no_data_from_bad_data() -> None:
    check(ui.heat(None) != ui.heat(0.0),
          "never-drilled renders the same as drilled-and-failed")


def test_strip_removes_every_escape_code() -> None:
    styled = ui.ok("hi") + " " + ui.style("there", ui.REVERSE)
    check(ui.strip(styled) == "hi there", f"got {ui.strip(styled)!r}")


def test_highlight_range_toggles_reverse_without_touching_the_text() -> None:
    styled = ui.paint("hello world", "accent")
    hl = ui.highlight_range(styled, 2, 5)
    check(ui.strip(hl) == "hello world", f"highlighting changed the text: {ui.strip(hl)!r}")
    check(ui.vlen(hl) == ui.vlen(styled), "highlighting changed the visible width")
    check(ui.REVERSE in hl and ui.UNREVERSE in hl, "reverse-video codes are missing")


def test_highlight_range_empty_span_is_a_no_op() -> None:
    check(ui.highlight_range("plain", 3, 3) == "plain", "a zero-width span changed the line")


# ---------------------------------------------------------------- drag-select

def test_selection_text_spans_whole_lines_in_the_middle() -> None:
    lines = ["first line", "middle line", "last line"]
    text = tui.selection_text(lines, (0, 6), (2, 4))
    check(text == "line\nmiddle line\nlast", f"got {text!r}")


def test_selection_text_on_one_line_is_just_the_slice() -> None:
    lines = ["the quick brown fox"]
    check(tui.selection_text(lines, (0, 4), (0, 9)) == "quick", "single-line slice wrong")


def test_selection_text_strips_styling_before_slicing() -> None:
    lines = [ui.ok("EBITDA") + " bridge"]
    check(tui.selection_text(lines, (0, 0), (0, 6)) == "EBITDA",
          "escape codes threw off the column count")


def test_selection_text_works_in_either_drag_direction() -> None:
    lines = ["abcdef"]
    forward = tui.selection_text(lines, (0, 1), (0, 4))
    backward = tui.selection_text(lines, (0, 4), (0, 1))
    check(forward == backward == "bcd", f"got {forward!r} / {backward!r}")


# ---------------------------------------------------------------- putting a view down

def test_putting_a_view_down_leaves_nothing_in_the_transcript() -> None:
    """A dismissed view used to print itself into the scrollback. For eight
    search hits that looked tidy; for a browse of the whole bank it was a
    thousand rows of dead text above the prompt, and not only on esc -- every
    second view and every drill started from a row parks the current one
    first, so three gestures produced the same wall."""
    shell = tui.Shell(on_submit=lambda s, l: None)
    shell.attach(views.TabsView("BANK", [("Overview", lambda w: ["42 questions"])]))
    shell.detach()
    check(shell.view is None, "the view is still attached")
    check(shell.transcript.lines == [],
          f"putting the view down left behind {shell.transcript.lines!r}")


def _view_shell(lists: str = "browse"):
    """A shell whose `lists` command opens a view and whose others print."""
    shell = tui.Shell(on_submit=lambda s, l: None)

    def submit(sh, line: str) -> None:
        if line.split()[0] == lists:
            sh.attach(views.TabsView("BANK", [("Overview", lambda w: ["42"])]))
        else:
            sh.emit("  a finding worth keeping")

    shell.on_submit = submit
    return shell


def test_a_view_visit_that_printed_nothing_folds_its_echo_away() -> None:
    """The `› browse` line heralds a screen that no longer exists, so it goes
    down with it -- otherwise every visit leaves one more `browse` in the
    scrollback forever, with nothing to tell two of them apart."""
    shell = _view_shell()
    shell._run_one("browse")
    check(any("browse" in ui.strip(l) for l in shell.transcript.lines),
          "the echo was gone while the view it opened was still up")
    shell.detach()
    check(shell.transcript.lines == [],
          f"putting the view down left behind {shell.transcript.lines!r}")


def test_a_typed_command_closes_whatever_view_was_open() -> None:
    """A line typed at the prompt is a fresh place in the tool, even a plain
    print like `check 5` -- it used to leave an already-open view (`browse`,
    say) attached, so the command's own output landed underneath or around
    a list that was supposedly a screen ago. Only a command that opens a
    view of its own is exempt from closing that view, because it is
    immediately replacing it (`attach` calls `detach` too)."""
    shell = _view_shell()
    shell._run_one("browse")
    check(shell.view is not None, "browse did not open a view")
    shell._run_one("check 5")
    check(shell.view is None, "an unrelated view was still open after check 5")
    kept = [ui.strip(l) for l in shell.transcript.lines]
    check("  a finding worth keeping" in kept,
          f"check 5's own output was lost: {kept!r}")
    check(any("check 5" in line for line in kept),
          f"check 5's own echo was lost: {kept!r}")
    check(not any("browse" in line for line in kept),
          f"the browse echo lingered after browse was closed: {kept!r}")


def test_a_command_driving_its_own_prompt_can_silence_the_frames_footer() -> None:
    """`show` runs its own prompt below the card (`[p] prev · [d] drill it ·
    ... · [Enter] done`) and folds a `◂▸ tab` hint into that same line --
    the frame's own auto-footer (`◂ ▸ tab · ↑↓ scroll · esc done`) said an
    overlapping thing right next to it, `esc done` next to `[Enter] done`,
    which is what read as double hints. A `TabsView` built with
    `footer=False` draws none of its own, leaving the command's own prompt
    as the one hint line on screen."""
    loud = views.TabsView("#1", [("Answer", lambda w: ["fine"])])
    check(loud.footer() != "", "the default footer went silent on its own")
    silent = views.TabsView("#1", [("Answer", lambda w: ["fine"])], footer=False)
    check(silent.footer() == "", f"a silenced footer still drew {silent.footer()!r}")


def test_the_pinned_banner_is_not_a_row_of_the_list_behind_it() -> None:
    """A screen row inside the header has no view row under it.

    Clamping it into the body made every row of the banner an alias for the
    body's top row, so on a full screen a click on the countdown fired
    whatever the list happened to have up there. `_abs_pos` already refuses
    to fold the header into the body for the same reason."""
    clicked: list[int] = []

    class _Long(tui.View):
        def render(self, width: int) -> list[str]:
            self.owner = list(range(60))
            return [f"  row {i}" for i in range(60)]

        def click_at(self, item, col, shell) -> bool:
            clicked.append(item)
            return True

    shell = tui.Shell(on_submit=lambda s, l: None,
                      header=lambda sh: ["  superday"] * 8)
    shell.screen.size = lambda: (100, 40)
    shell.attach(_Long())
    shell.compose()
    check(shell._header_rows == 8, "the header did not claim its rows")
    shell._on_mouse(tui.MouseEvent("release", 2, 10))
    check(clicked == [], f"a click on the banner reached the list as row {clicked}")
    shell._on_mouse(tui.MouseEvent("release", shell._header_rows + 1, 10))
    check(len(clicked) == 1,
          "a click just below the banner stopped reaching the list")


def test_a_command_that_owns_the_prompt_gets_the_footer_to_itself() -> None:
    """Two keymaps on screen at once, and the bottom one was the wrong one.

    While `drill` reads your answer, the frame kept drawing the shell's own
    hints -- `/ search · d drill · g dashboard · ? help` -- under the drill's
    "Enter reveals · s skip · q quit". None of those fire during a sitting;
    they go into the answer box. `TabsView(footer=False)` already solved this
    for `show`; a command driving `input()` has no view to hang it on, so the
    frame has to notice that it is not the one reading the keys.
    """
    shell = tui.Shell(on_submit=lambda s, l: None,
                      hints=lambda sh: "/ search · d drill · g dashboard")
    shell.screen.size = lambda: (100, 30)
    check("d drill" in ui.strip(shell._hint_row()),
          "the shell drew no hints when it was the one reading keys")

    shell._in_command = True
    check("d drill" not in ui.strip(shell._hint_row()),
          f"a stale keymap survived into a command: {ui.strip(shell._hint_row())!r}")

    # The right-hand status is not a keymap and stays: it says what the bank
    # holds, which is true whoever is reading the keys.
    shell.status = lambda: "1082 due"
    check("1082 due" in ui.strip(shell._hint_row()),
          "silencing the hints took the status with it")

    shell._in_command = False
    check("d drill" in ui.strip(shell._hint_row()),
          "the hints did not come back when the command finished")


def test_clearing_wipes_the_view_as_well_as_the_transcript() -> None:
    """`clear` has to mean the screen, not the half of it that scrolls."""
    said: list[str] = []
    shell = tui.Shell(on_submit=lambda s, l: None,
                      on_clear=lambda sh: said.append("banner"))
    shell.emit("some earlier output")
    shell.attach(views.TabsView("BANK", [("Overview", lambda w: ["42 questions"])]))
    shell.clear()
    check(shell.view is None, "clear left the view on screen")
    check(shell.transcript.lines == ["banner"] or said == ["banner"],
          "clear did not re-run the on_clear hook")
    check(not any("42 questions" in ui.strip(l) for l in shell.transcript.lines),
          "clear left the list in the transcript")


def test_a_command_a_view_starts_gets_the_screen_to_itself() -> None:
    """`drill` launched from `browse` used to run behind the browse frame: the
    list stayed drawn, the question went into the transcript underneath it, and
    every keystroke answered a sitting you could not see. The view steps off
    for the duration and comes back after -- and steps off without printing
    itself, or the question ends up below a thousand rows of the list it was
    started from."""
    shell = tui.Shell(on_submit=lambda s, l: None)
    view = views.TabsView("BANK", [("Overview", lambda w: ["42 questions"])])
    shell.attach(view)
    shell.run_now("drill --ids 1,2")
    check(shell.view is None, "the view stayed on screen while the command ran")
    check(shell._resume_view is view, "the view was dropped instead of parked")
    check(shell.transcript.lines == [],
          f"parking the view printed it: {shell.transcript.lines!r}")


def test_esc_backs_out_of_a_view_reached_by_drilling_into_another() -> None:
    """A row's `⏎` used to swap the view outright: `dupes` opening a compare
    from a row dropped the list it was opened from with no way back to it, so
    esc from the compare landed at a bare prompt with only the transcript and
    the input box left -- a screen with nothing to say how you got there. A
    view reached by drilling into another is parked instead, and esc is the
    way back to it -- however many screens deep."""
    parent = views.TabsView("DUPLICATES", [("Pairs", lambda w: ["#1 vs #2"])])
    parent._cache[0] = views.Pane(["stale"])      # what on_resume must clear
    child = views.TabsView("COMPARE", [("Question", lambda w: ["70% alike"])])

    shell = tui.Shell(on_submit=lambda s, l: None)
    shell.attach(parent)
    shell.run_now("dupes --pair 1,2")             # parks `parent`, detaches
    shell.attach(child)                            # what cmd_dupes attaches
    shell._after_run(from_view=True)
    check(shell.view is child, "the drilled-into view did not end up on screen")
    check(shell._view_stack == [parent],
          f"the parent was not parked for esc to reach: {shell._view_stack}")

    check(shell._esc_back() is True, "esc found nothing to back out to")
    check(shell.view is parent, "esc did not hand the parent view back")
    check(shell._view_stack == [], "the stack was not drained on the way back")
    check(0 not in parent._cache,
          "the pair decided from the compare screen would still read as open")

    check(shell._esc_back() is False,
          "esc claimed a back-target once the stack was actually empty")


def test_esc_quits_a_sitting_the_same_way_ctrl_d_does() -> None:
    """`drill` and `mock` block on a bare `input()` with no view attached, and
    every one of their prompts already treats ^D as "quit and save the rest
    of the sitting" (`except EOFError: raw = "q"`). Esc used to be swallowed
    right there -- there was no view to back out of and nothing on the input
    line to answer with -- so the only way out of a sitting mid-question was
    ^D or typing q by hand. Esc now raises the same `EOFError`, reusing the
    quit path every one of those prompts already has instead of teaching each
    one a second way to be told to stop."""
    from .tui import Key, Shell

    class FakeReader:
        def __init__(self, keys):
            self.keys = list(keys)

        def read(self, timeout=None):
            return self.keys.pop(0) if self.keys else None

    shell = Shell(on_submit=lambda s, l: None)
    shell.reader = FakeReader([Key("esc")])
    shell._in_command = True
    raised = False
    try:
        shell._read_line("your answer")
    except EOFError:
        raised = True
    check(raised, "esc inside a running command did not raise EOFError")


def test_esc_at_the_top_level_prompt_does_not_quit_the_shell() -> None:
    """The same key means something else entirely one level up: nothing is
    running, so there is no sitting to save out of, and esc there is the
    ordinary "close whatever is open" it always was."""
    from .tui import Key, Shell

    class FakeReader:
        def __init__(self, keys):
            self.keys = list(keys)

        def read(self, timeout=None):
            return self.keys.pop(0) if self.keys else None

    shell = Shell(on_submit=lambda s, l: None)
    shell.attach(views.TabsView("BANK", [("Overview", lambda w: ["42"])]))
    shell.reader = FakeReader([Key("esc"), Key("char", "x"), Key("enter")])
    check(shell._in_command is False, "the fixture started mid-command")
    line = shell._read_line("›")
    check(line == "x", f"esc at the top level ate the next keystroke too: {line!r}")
    check(shell.view is None, "esc at the top level left the view attached")


def test_a_typed_command_starts_a_fresh_place_not_a_deeper_one() -> None:
    """Drilling in with `⏎` and typing a brand new command are not the same
    gesture -- one is "into this", the other is "somewhere else entirely".
    Esc backing out to a screen that has nothing to do with what you just
    typed would be its own kind of confusing exit."""
    grandparent = views.TabsView("A", [("One", lambda w: ["a"])])
    elsewhere = views.TabsView("B", [("One", lambda w: ["b"])])

    shell = tui.Shell(on_submit=lambda s, l: shell.attach(elsewhere))
    shell.attach(grandparent)
    shell._view_stack.append(views.TabsView("STALE", [("One", lambda w: ["s"])]))
    # `_run_one` takes from_view itself now and puts the open view down before
    # the command runs, so `_after_run` no longer needs to be told what was
    # there before -- there is nothing parked to compare against.
    shell._run_one("find something", False)
    shell._after_run(from_view=False)
    check(shell.view is elsewhere, "the typed command did not attach its view")
    check(shell._view_stack == [],
          "a typed command left stale back-history behind it")


def test_a_view_can_still_print_itself_whole_when_there_is_no_shell() -> None:
    """Dismissing prints nothing, but a piped `find` prints everything --
    those are two different jobs and `flatten` is the one that holds nothing
    back."""
    view = views.TabsView("BANK", [("Overview", lambda w: ["42 questions"])])
    check(any("42 questions" in ui.strip(l) for l in view.flatten(80)),
          "flatten dropped the pane it was asked to print")


# ---------------------------------------------------------------- analytics

def _active(conn, source, text, answer="An answer.", topic="accounting", difficulty=2):
    v = admit(conn, source_id=source, question_text=text, answer_text=answer, status="active")
    conn.execute("UPDATE questions SET topic = ?, difficulty = ? WHERE id = ?",
                 (topic, difficulty, v.matched_id))
    conn.commit()
    return v.matched_id


def test_due_means_the_same_thing_on_every_screen() -> None:
    """The dashboard counted only rows in `schedule` while the topic table
    counted never-seen questions too, so one screen said 13 due and the next
    said 700."""
    conn, a, _ = fresh()
    q1 = _active(conn, a, "What is EBITDA and why is it used?")
    _active(conn, a, "Walk me through the three financial statements please")
    scheduler.record_review(conn, q1, 3)

    c = analytics.counts(conn)
    topics = analytics.topic_mastery(conn)
    check(c["due_now"] == sum(t["due"] for t in topics),
          f"counts say {c['due_now']} due, topics say {sum(t['due'] for t in topics)}")
    check(c["unseen"] == sum(t["unseen"] for t in topics), "unseen disagrees across screens")
    check(c["active"] == sum(t["active"] for t in topics), "active disagrees across screens")


def test_upcoming_reports_the_unseen_pool_it_cannot_date() -> None:
    """The Upcoming pane read `schedule` alone and reported an empty fortnight
    on the same screen as "due now 1081". A question nobody has opened is due,
    but no day owns it, so it has to be reported beside the day series rather
    than left out of the answer entirely."""
    conn, a, _ = fresh()
    q1 = _active(conn, a, "What is EBITDA and why is it used as a proxy?")
    for i in range(5):
        _active(conn, a, f"Question number {i} about the enterprise value bridge")
    scheduler.record_review(conn, q1, 3)

    f = analytics.upcoming(conn, 14)
    check(f["unseen"] == 5, f"upcoming reported {f['unseen']} unseen, wanted 5")
    check(f["scheduled"] == 1, f"upcoming dated {f['scheduled']} reviews, wanted 1")
    # Folding them into day one would put a bar on a day that does not own
    # them; the whole point of splitting the two is that it is not a forecast.
    check(sum(d["reviews"] for d in f["days"]) == 1,
          "the unseen pool was folded into the day series")
    check(len(f["days"]) == 14 and f["days"][0]["weekday"],
          "the day series lost its length or its weekday labels")


def test_readiness_needs_both_coverage_and_mastery() -> None:
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Question number {i} about enterprise value bridges")
           for i in range(10)]
    check(analytics.readiness(conn)["score"] == 0.0, "untouched bank scored above zero")

    scheduler.record_review(conn, ids[0], 4)
    partial = analytics.readiness(conn)
    check(partial["score"] < 0.25,
          f"one perfect answer out of ten scored {partial['score']:.2f}")
    check(partial["mastery"] == 1.0, "mastery ignored a perfect rating")

    for qid in ids:
        scheduler.record_review(conn, qid, 4)
    full = analytics.readiness(conn)
    check(full["score"] > 0.9, f"whole bank aced but scored {full['score']:.2f}")


def test_streak_survives_drilling_only_yesterday() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "How do you calculate unlevered free cash flow?")
    conn.execute(
        "INSERT INTO reviews (question_id, asked_at, rating, grader) "
        "VALUES (?, datetime('now','-1 day'), 3, 'self')", (qid,))
    conn.commit()
    check(analytics.streak(conn)["current"] == 1,
          "a streak reset at midnight even though today is not over")

    conn.execute(
        "INSERT INTO reviews (question_id, asked_at, rating, grader) "
        "VALUES (?, datetime('now','-4 days'), 3, 'self')", (qid,))
    conn.commit()
    check(analytics.streak(conn)["current"] == 1, "a four-day-old gap counted as unbroken")


def test_retention_curve_scores_intervals_not_first_sightings() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "Why might a company trade at a premium to its peers?")
    for offset, rating in (("-20 days", 3), ("-16 days", 3), ("-2 days", 1)):
        conn.execute(
            "INSERT INTO reviews (question_id, asked_at, rating, grader) "
            "VALUES (?, datetime('now', ?), ?, 'self')", (qid, offset, rating))
    conn.commit()
    curve = {b["bucket"]: b for b in analytics.retention_curve(conn)}
    check(sum(b["n"] for b in curve.values()) == 2,
          "the first sighting was scored, but it had no interval to survive")
    check(curve["3-7d"]["n"] == 1 and curve["3-7d"]["retention"] == 1.0,
          "a four-day gap recalled cleanly was not counted as retained")
    check(curve["2w-1m"]["n"] == 1 and curve["2w-1m"]["retention"] == 0.0,
          "a two-week lapse was not counted as a failure")


def test_card_health_survives_an_unreadable_card() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "What drives the terminal value in a DCF model?")
    scheduler.record_review(conn, qid, 3)
    conn.execute("INSERT INTO schedule (question_id, card_json, due_at) "
                 "VALUES (?, 'not json at all', ?)",
                 (_active(conn, a, "What is a leveraged buyout in simple terms?"),
                  "2030-01-01T00:00:00+00:00"))
    conn.commit()
    h = analytics.card_health(conn)
    check(h["n"] == 1, f"card_health counted {h['n']} readable cards, wanted 1")



# ---------------------------------------------------------------- sessions

def test_a_walked_away_sitting_can_be_picked_back_up() -> None:
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Sitting question {i} about the enterprise value bridge")
           for i in range(5)]
    sid = session.open_session(conn, "drill", ids, {"count": 5})

    session.record(conn, sid, ids[0], 3, 12.0)
    session.record(conn, sid, ids[1], 1, 40.0)

    row = session.resumable(conn, "drill")
    check(row is not None and row["id"] == sid, "an unfinished sitting was not resumable")
    left = [r["id"] for r in session.queue_of(conn, row)]
    check(left == ids[2:], f"resume handed back {left}, wanted {ids[2:]}")
    check(session.summary(row)["avg_rating"] == 2.0, "session average was not the ratings mean")


def test_a_finished_sitting_is_not_offered_again() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "Why would two companies with identical earnings trade apart?")
    sid = session.open_session(conn, "drill", [qid], {})
    session.record(conn, sid, qid, 4, 5.0)
    session.close(conn, sid)
    check(session.resumable(conn, "drill") is None, "a completed sitting was still resumable")


def test_only_one_sitting_is_ever_resumable() -> None:
    """Two open sittings would make `--resume` a coin flip."""
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Queue question {i} on discounted cash flow mechanics")
           for i in range(4)]
    session.open_session(conn, "drill", ids[:2], {})
    second = session.open_session(conn, "drill", ids[2:], {})
    open_rows = conn.execute(
        "SELECT COUNT(*) c FROM sessions WHERE kind='drill' AND finished_at IS NULL"
    ).fetchone()["c"]
    check(open_rows == 1, f"{open_rows} sittings left open at once")
    check(session.resumable(conn, "drill")["id"] == second, "resume found the wrong sitting")


def test_resume_drops_a_question_the_bank_has_since_rejected() -> None:
    conn, a, _ = fresh()
    keep = _active(conn, a, "How do you value a company with negative EBITDA?")
    drop = _active(conn, a, "A question that turns out to be wrong on inspection")
    row_id = session.open_session(conn, "drill", [keep, drop], {})
    history.set_status(conn, drop, "rejected", action="review", batch_id="b1")
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (row_id,)).fetchone()
    left = [r["id"] for r in session.queue_of(conn, row)]
    check(left == [keep], f"resume handed back a rejected question: {left}")


def test_a_skip_goes_to_the_back_not_the_bin() -> None:
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Skippable question {i} on leveraged buyout returns")
           for i in range(3)]
    sid = session.open_session(conn, "drill", ids, {})
    session.skip(conn, sid, ids[0])
    row = conn.execute("SELECT queue_json FROM sessions WHERE id = ?", (sid,)).fetchone()
    check(json.loads(row["queue_json"]) == [ids[1], ids[2], ids[0]],
          "a skipped question did not come round again")


# ---------------------------------------------------------------- tagging

def test_taxonomy_tags_what_a_question_is_actually_about() -> None:
    got = tagging.suggest(
        "Walk me through the bridge from equity value to enterprise value",
        "Add net debt, minority interest and preferred stock.")
    check("ev-bridge" in got, f"EV bridge question tagged {got}")

    got = tagging.suggest("How do you calculate WACC?", "Weight the cost of equity and debt.")
    check("wacc" in got, f"WACC question tagged {got}")


def test_a_passing_mention_in_an_answer_is_not_a_tag() -> None:
    """`--tag wacc` is worthless if every DCF answer that says the word WACC
    once gets filed under it."""
    got = tagging.suggest(
        "Why might a company trade at a premium to its peers?",
        "Growth, margins and competitive position. It is not about WACC.")
    check("wacc" not in got, f"a single passing mention became a tag: {got}")


def test_bare_multiple_is_not_the_multiples_tag() -> None:
    got = tagging.suggest("There are multiple ways to think about this problem", "")
    check("multiples" not in got, f"ordinary English matched the multiples tag: {got}")
    got = tagging.suggest("Why do you use EV/EBITDA rather than P/E?", "")
    check("multiples" in got, f"a real multiples question was missed: {got}")


def test_autotag_is_idempotent() -> None:
    conn, a, _ = fresh()
    _active(conn, a, "Walk me through the bridge from equity value to enterprise value",
            answer="Add net debt and minority interest.")
    first = tagging.autotag(conn)
    check(first["links"] > 0, "autotag found nothing on an obvious EV bridge question")
    second = tagging.autotag(conn)
    check(second["links"] == 0, f"a second autotag pass added {second['links']} duplicate links")


def test_tag_attach_reports_only_what_was_new() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "What is the treasury stock method and when do you use it?")
    check(tagging.attach(conn, qid, ["sbc", "sbc"]) == ["sbc"], "a repeat tag was reported as new")
    check(tagging.attach(conn, qid, ["#SBC"]) == [], "tag matching was case or hash sensitive")
    check(tagging.tags_for(conn, qid) == ["sbc"], "tag did not stick")
    check(tagging.detach(conn, qid, ["sbc"]) == ["sbc"], "detach reported nothing removed")


# ---------------------------------------------------------------- selection

def test_weak_first_asks_the_ones_you_keep_failing() -> None:
    conn, a, _ = fresh()
    easy = _active(conn, a, "An easy question you have always answered well")
    hard = _active(conn, a, "A hard question you keep getting completely wrong")
    for _ in range(3):
        conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                     "VALUES (?, datetime('now','-9 days'), 4, 'self')", (easy,))
        conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                     "VALUES (?, datetime('now','-9 days'), 1, 'self')", (hard,))
    conn.commit()
    order = [r["id"] for r in scheduler.due_questions(conn, limit=2, weak_first=True)]
    check(order[0] == hard, f"weak-first led with {order[0]}, wanted the failing question")


# ---------------------------------------------------------------- question lines

def test_a_follow_up_is_told_apart_from_a_question_that_sets_itself_up() -> None:
    """#803 ("Wait a minute, how are Call Protection and Prepayment
    different?") cannot be asked cold. #818 opens "What if there's a stub
    period... halfway through the year instead?" and can: it spends its first
    sentence saying what the alternative is. The detector fired on both while
    it matched a bare "instead" anywhere in the text, and a scan that flags
    self-contained questions is a scan that stops being read."""
    danglers = [
        "Wait a minute, how are Call Protection and Prepayment different?",
        "OK, so what factors might cause a company to become stressed?",
        "What about a buyout where you only acquire a 30% stake?",
        "How does the previous scenario change if the debt amortises?",
        "Why would a PE firm prefer High-Yield Debt instead?",
        "Now assume the company repays 75% of the initial Debt balance.",
    ]
    standalone = [
        "Walk me through a DCF.",
        "What is the difference between Bank Debt and High-Yield Debt?",
        "What if there's a stub period? Normally you assume full years, but "
        "what happens if the PE firm acquires a company halfway through the "
        "year instead?",
        "How do Equity Value and Enterprise Value change if a company uses "
        "$100 of new Common Stock to acquire an asset instead of a company?",
        "Would you expect a franchise restaurant to trade at higher multiples?",
    ]
    for text in danglers:
        check(bool(chains.signals(text)), f"missed a follow-up: {text[:50]}")
    for text in standalone:
        check(not chains.signals(text), f"fired on a self-contained question: {text[:50]}")


def test_the_lead_in_is_the_previous_question_in_the_same_source() -> None:
    conn, a, b = fresh()
    first = _active(conn, a, "Tell me about the different types of debt in an LBO.")
    second = _active(conn, a, "Wait a minute, how are Call Protection and Prepayment different?")
    elsewhere = _active(conn, b, "Walk me through a DCF and how you get to WACC.")
    check(chains.preceding(conn, second)["id"] == first, "lead-in was not the question before it")
    check(chains.preceding(conn, first) is None, "the first question in a source found a lead-in")
    check(chains.preceding(conn, elsewhere) is None,
          "a lead-in was taken from a different source")

    found = {c["id"]: c for c in chains.scan(conn)}
    check(second in found and found[second]["parent_id"] == first,
          "the scan did not propose the question before it")
    chains.link(conn, second, first)
    check([r["id"] for r in chains.lead_in(conn, second)] == [first],
          "the recorded lead-in did not come back")
    check(second not in {c["id"] for c in chains.scan(conn)},
          "a linked question was still reported as unlinked")

    # Judgement, not a link: #574 sets up its own scenario and only reads like
    # a follow-up. Without somewhere to record that, the scan is a list that
    # can never reach zero and stops being read.
    third = _active(conn, a, "So can the PE firm still earn a solid return?")
    check(third in {c["id"] for c in chains.scan(conn)}, "the scan missed one")
    chains.mark_standalone(conn, third)
    check(third not in {c["id"] for c in chains.scan(conn)},
          "a question judged standalone came back on the next scan")


def test_a_carried_over_subject_reads_as_a_follow_up() -> None:
    """Two shapes the scan used to walk straight past.

    "This same company now issues $10 in Common Dividends" names a company it
    never introduces; the noun list stopped at the words for a *setup*
    (scenario, example, numbers) and had none of the words for the thing the
    setup is about. "You've explained commercial banks and insurance firms,
    but what about ..." names the answer before it and matched nothing either.

    Both are `certain`, so the determiner has to carry the weight: "for the
    same company" is ordinary English meaning one and the same, with no
    previous turn behind it, and must stay unmatched.
    """
    def marks(text: str) -> set[str]:
        return {name for name, _why, _tier in chains.signals(text)}

    check("same-subject" in marks(
        "This same company now issues $10 in Common Dividends and $10 in "
        "Preferred Dividends. What happens?"),
        "a carried-over company still reads as a fresh question")
    check(not marks("Could EV / EBITDA ever be higher than EV / EBIT for the "
                    "same company?"),
          "'for the same company' was read as a back-reference")
    check("you-explained" in marks(
        "You've explained commercial banks and insurance firms, but what about "
        "other companies in financial services?"),
        "naming what the previous answer covered did not read as a follow-up")
    check(not marks("What have you explained to a client about leverage?"),
          "an ordinary use of 'explained' was read as a back-reference")


def test_a_link_that_would_not_walk_is_refused() -> None:
    """`lead_in` walks parents until it runs out. A loop never runs out, and
    the first drill of either question hangs the shell."""
    conn, a, _ = fresh()
    one = _active(conn, a, "Tell me about the different types of debt in an LBO.")
    two = _active(conn, a, "Wait a minute, how is Call Protection different?")
    three = _active(conn, a, "And what about Excess Cash in a Sources and Uses table?")
    chains.link(conn, two, one)
    chains.link(conn, three, two)
    for child, parent, why in [(one, one, "self-link"), (one, three, "loop"),
                               (one, 9999, "missing parent")]:
        try:
            chains.link(conn, child, parent)
            check(False, f"a {why} was accepted")
        except chains.LinkError:
            check(True, f"{why} refused")
    check([r["id"] for r in chains.lead_in(conn, three)] == [one, two],
          "a two-deep lead-in came back in the wrong order")
    check([r["id"] for r in chains.lead_in(conn, three, limit=1)] == [two],
          "the lead-in ignored its cap")


def test_a_parent_in_the_same_sitting_is_asked_first() -> None:
    """The schedule can easily make a follow-up due before its lead-in. Asked
    in that order the follow-up arrives cold, which is what the link exists to
    prevent."""
    conn, a, _ = fresh()
    parent = _active(conn, a, "Tell me about the different types of debt in an LBO.")
    child = _active(conn, a, "Wait a minute, how is Call Protection different?")
    other = _active(conn, a, "Walk me through a DCF and how you get to WACC.")
    chains.link(conn, child, parent)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, parent_id FROM questions WHERE id IN (?,?,?)", (child, other, parent))]
    rows.sort(key=lambda r: [child, other, parent].index(r["id"]))
    order = [r["id"] for r in chains.order(rows)]
    check(order.index(child) == order.index(parent) + 1,
          f"the follow-up was not asked right after its lead-in: {order}")
    check(set(order) == {parent, child, other} and len(order) == 3,
          f"ordering dropped or duplicated a question: {order}")
    check(order == [parent, child, other],
          f"a question with no lead-in did not keep its place: {order}")

    # Right after, not merely later: three unrelated questions in between is
    # the cold start the link exists to prevent.
    filler = [{"id": 900 + i, "parent_id": None} for i in range(3)]
    spread = chains.order([{"id": child, "parent_id": parent}, *filler,
                           {"id": parent, "parent_id": None}])
    ids = [r["id"] for r in spread]
    check(ids.index(child) == ids.index(parent) + 1,
          f"a queued lead-in did not pull its follow-up along: {ids}")
    check(len(ids) == 5 and set(ids) == {child, parent, 900, 901, 902},
          f"ordering lost a row: {ids}")


# ---------------------------------------------------------------- recap

def test_recap_windows_and_what_they_hold() -> None:
    conn, a, _ = fresh()
    qid = _active(conn, a, "What is Working Capital and how do you interpret it?")
    old = _active(conn, a, "Why might a company trade at a premium to its peers?")
    conn.execute("INSERT INTO reviews (question_id, asked_at, rating, user_answer, "
                 "score, rubric_hits, grader) VALUES (?, datetime('now'), 3, "
                 "'current assets less current liabilities', 0.8, '[true,false]', 'gemini')",
                 (qid,))
    conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                 "VALUES (?, datetime('now','-9 days'), 1, 'self')", (old,))
    conn.commit()

    check(analytics.parse_window("7d") == ("last 7d", "-7 days"), "7d did not parse")
    check(analytics.parse_window("3 weeks") == ("last 21d", "-21 days"), "3 weeks did not parse")
    check(analytics.parse_window(None)[0] == "today", "the default window moved")
    try:
        analytics.parse_window("next tuesday")
        check(False, "a window nobody understands was accepted")
    except ValueError:
        check(True, "unknown window refused")

    today = analytics.answered(conn, since=analytics.parse_window("today")[1])
    check([r["question_id"] for r in today] == [qid], "today held the wrong reviews")
    check(today[0]["user_answer"].startswith("current assets"),
          "what you typed did not come back with the review")
    check(today[0]["score"] == 0.8 and today[0]["grader"] == "gemini",
          "the grade did not come back with the review")
    month = analytics.answered(conn, since=analytics.parse_window("month")[1])
    check([r["question_id"] for r in month] == [qid, old],
          "a month did not come back newest first")

    # One row per review, not per question: answering the same card twice is
    # two things that happened, and the second one usually went better.
    conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                 "VALUES (?, datetime('now'), 4, 'self')", (qid,))
    conn.commit()
    check(len(analytics.answered(conn, since="start of day")) == 2,
          "two sittings of one question collapsed into one row")


def test_a_card_coming_round_in_minutes_does_not_read_as_due() -> None:
    """FSRS gives a card rated "hard" an interval of a few minutes. `.days`
    truncated that to zero, so the list said due next to a question `drill`
    would refuse to ask, and the do-screen offered no way to drill it."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    check("due" not in ui.strip(views.due_label(soon)) or
          ui.strip(views.due_label(soon)) == "<1d",
          f"ten minutes away read as {ui.strip(views.due_label(soon))}")
    check(ui.strip(views.due_label(past)) == "due", "an overdue card did not read as due")
    check(ui.strip(views.due_label(later)) == "3d", "a three-day wait did not read as 3d")
    check("minutes" in views.next_due_phrase(soon),
          f"the sentence said: {views.next_due_phrase(soon)}")
    check(views.next_due_phrase(past) == "due again now", "an overdue card was not due now")
    check(views.next_due_phrase(None) == "never scheduled", "an unseen card claimed a date")


def test_a_finished_question_folds_to_one_line() -> None:
    """The transcript is append-only everywhere else. A drill is the exception:
    twenty questions with a rubric each is four hundred lines you scroll past
    to find the one you are being asked."""
    t = tui.Transcript()
    t.extend(["› drill", "  10 queued"])
    mark = t.mark()
    t.extend([f"  line {i}" for i in range(20)])
    wrapped_before = len(t.wrapped(80))
    check(wrapped_before > 20, "the wrap cache was not warm before the rewind")
    t.rewind(mark)
    t.append("  3 good  #803  42s")
    check(t.lines == ["› drill", "  10 queued", "  3 good  #803  42s"],
          f"rewind left the wrong transcript: {t.lines}")
    check(len(t.wrapped(80)) == 3,
          "the wrap cache still held rows for lines that were taken back")
    t.rewind(99)
    check(len(t.lines) == 3, "rewinding past the end truncated the transcript")


# ---------------------------------------------------------------- market

def test_a_cached_market_value_is_flagged_as_stale() -> None:
    """Answering "the 10-year is 4.7" off a fortnight-old cache is a wrong
    answer, so the caller has to be able to tell fresh from cached."""
    conn, _, _ = fresh()
    conn.execute("INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
                 "VALUES ('treasury', '10 Yr', 4.25, '2020-01-01', ?)",
                 ("2020-01-01T00:00:00+00:00",))
    conn.commit()
    original = market.PROVIDERS["treasury"]
    market.PROVIDERS["treasury"] = lambda: (_ for _ in ()).throw(OSError("no network"))
    try:
        val, as_of, stale = market.value_for(conn, "treasury", "10 Yr", ttl_seconds=60)
    finally:
        market.PROVIDERS["treasury"] = original
    check(val == 4.25, "a stale value was thrown away instead of served")
    check(stale, "a two-year-old cached yield was reported as current")


def test_a_fresh_market_value_is_not_flagged_stale() -> None:
    conn, _, _ = fresh()
    from .db import now as db_now
    conn.execute("INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
                 "VALUES ('treasury', '10 Yr', 4.25, ?, ?)", (_today(), db_now()))
    conn.commit()
    val, _, stale = market.value_for(conn, "treasury", "10 Yr", ttl_seconds=86400)
    check(val == 4.25 and stale is None, "a value fetched seconds ago was called stale")


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


def test_a_market_question_that_was_not_graded_is_not_marked_good() -> None:
    """`_grade_market` returned 3 whenever it could not grade -- a "good"
    written into the schedule for a question that was never actually asked,
    and (because the reveal was skipped for this kind) without ever showing
    the answer. Six questions in the real bank are market-awareness with no
    binding, each carrying a full answer and a rubric, and every one of them
    was unstudiable: press Enter, be told the grade is skipped, be marked
    good, move on. Not graded is `None`, which falls through to the same
    reveal and self-rating prompt every other question gets."""
    from . import cli
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Where are ECB rates?",
              answer_text="Deposit facility 2.25%, MRO 2.40%." + "y" * 40)
    conn.execute("UPDATE questions SET kind = 'market_awareness', topic = 'markets', "
                 "status = 'active' WHERE id = ?", (v.matched_id,))
    conn.commit()
    q = conn.execute("SELECT * FROM questions WHERE id = ?", (v.matched_id,)).fetchone()

    feedback, rating = cli._grade_market(conn, q, "2.25")
    check(rating is None, f"an unbound market question was auto-rated {rating}")
    check("rate it yourself" in ui.strip(feedback),
          f"the reason it was not graded reads as a grade: {ui.strip(feedback)!r}")

    # Bound and gradeable is untouched: a number close to the print still
    # scores, which is the whole point of the kind.
    from .db import now as db_now
    conn.execute("INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
                 "VALUES ('ecb', 'DFR', 2.25, ?, ?)", (_today(), db_now()))
    conn.execute("INSERT INTO live_bindings (question_id, provider, series_key, unit, "
                 "tolerance, ttl_seconds) VALUES (?, 'ecb', 'DFR', '%', 0.1, 86400)",
                 (v.matched_id,))
    conn.commit()
    _, rating = cli._grade_market(conn, q, "2.25")
    check(rating == 4, f"a bang-on answer scored {rating}")

    # And a print too old to grade against says the number and hands it back
    # to you, rather than inventing a verdict from a two-year-old yield.
    conn.execute("UPDATE live_cache SET as_of = '2020-01-01', fetched_at = ? "
                 "WHERE series_key = 'DFR'", (db_now(),))
    conn.commit()
    feedback, rating = cli._grade_market(conn, q, "2.25")
    check(rating is None, f"a two-year-old print still produced a rating of {rating}")
    check("2.25" in ui.strip(feedback), "the number itself was withheld")


def test_a_daily_print_is_stale_long_before_a_monthly_one() -> None:
    """The two cadences cannot share a limit. A euro area benchmark yield
    stamped `2026-07` is the latest print there is well into September, and a
    staleness warning that fires on a correct value is one you learn to
    ignore -- at which point the true ones stop mattering."""
    from datetime import datetime, timedelta, timezone
    day = datetime.now(timezone.utc) - timedelta(days=3)
    old_day = datetime.now(timezone.utc) - timedelta(days=30)
    month = datetime.now(timezone.utc) - timedelta(days=45)
    check(not market.observation_stale(day.date().isoformat()),
          "a print from three days ago was called stale")
    check(market.observation_stale(old_day.date().isoformat()),
          "a month-old daily yield was called current")
    check(not market.observation_stale(month.strftime("%Y-%m")),
          "last month's monthly print was called stale")
    check(market.observation_stale("2020-01"), "a six-year-old monthly print was called current")
    check(not market.observation_stale(None), "a missing stamp was turned into a staleness claim")


def test_an_old_print_fetched_seconds_ago_is_still_stale() -> None:
    """The failure this whole module exists to prevent, and the one the fetch
    clock cannot see: a provider that answers, quickly, with December's curve
    in August. Stored with a fetched_at of now, it read as fresh."""
    conn, _, _ = fresh()
    from .db import now as db_now
    conn.execute("INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
                 "VALUES ('treasury', '10 Yr', 4.18, '2025-12-31', ?)", (db_now(),))
    conn.commit()
    original = market.PROVIDERS["treasury"]
    market.PROVIDERS["treasury"] = lambda: (_ for _ in ()).throw(
        AssertionError("a value inside its TTL should not have hit the feed"))
    try:
        val, as_of, stale = market.value_for(conn, "treasury", "10 Yr", ttl_seconds=86400)
    finally:
        market.PROVIDERS["treasury"] = original
    check(val == 4.18 and as_of == "2025-12-31", "the stored value was not served")
    check(stale, "an eight-month-old yield fetched seconds ago was reported as current")


def test_a_stale_print_past_the_retry_floor_asks_the_feed_again() -> None:
    """A cache poisoned at 09:00 must not still be what a drill grades against
    at 17:00, TTL or no TTL."""
    conn, _, _ = fresh()
    from datetime import datetime, timedelta, timezone
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute("INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
                 "VALUES ('treasury', '10 Yr', 4.18, '2025-12-31', ?)", (hour_ago,))
    conn.commit()
    original = market.PROVIDERS["treasury"]
    market.PROVIDERS["treasury"] = lambda: {"10 Yr": (4.72, _today())}
    try:
        val, as_of, stale = market.value_for(conn, "treasury", "10 Yr", ttl_seconds=86400)
    finally:
        market.PROVIDERS["treasury"] = original
    check(val == 4.72 and stale is None,
          f"an old print inside its TTL was served again instead of refetched ({val}, {stale})")
    check(as_of == _today(), "the refetched value kept the old observation date")


def test_the_treasury_fallback_stays_inside_january() -> None:
    """Last year's file answers "what is the latest print" in the days before
    the new year's first one, and never after that: reaching for it in August
    on a transient failure writes the 31 December curve into the cache with a
    fetched_at of now, which is exactly how the bank came to grade a correct
    4.7 as wrong."""
    from datetime import datetime, timezone
    december = ('Date,"10 Yr","2 Yr"\n12/31/2025,4.18,3.47\n').encode()
    original = market._get
    market._get = lambda url, timeout=25: (december if "2025" in url
                                           else (_ for _ in ()).throw(OSError("500")))
    try:
        august = market.fetch_treasury(datetime(2026, 8, 17, tzinfo=timezone.utc))
        january = market.fetch_treasury(datetime(2026, 1, 2, tzinfo=timezone.utc))
    finally:
        market._get = original
    check(august == {}, f"August reached back to last year's file and got {august}")
    check(january.get("10 Yr") == (4.18, "2025-12-31"),
          "January lost the fallback that covers the days before the new year's first print")



# ---------------------------------------------------------------- completions

def test_the_completion_file_matches_the_parser() -> None:
    """"Every subcommand needs a completion entry" was a convention nothing
    enforced, so it kept being broken and caught late. Now it is a test."""
    from . import completions as comp
    from .cli import build_parser
    in_sync, generated = comp.check(build_parser())
    check(in_sync,
          "completions/_superday has drifted from build_parser() -- "
          "run `superday completions --write`")

    parser = build_parser()
    sub = comp._subparsers(parser)
    for name in sub.choices:
        check(f"    {name})" in generated, f"no completion case for `{name}`")


def test_completion_help_text_cannot_break_the_zsh_quoting() -> None:
    """A help string containing an apostrophe or a bracket ends the zsh quote
    early and the whole completion file stops loading."""
    from . import completions as comp
    spec = comp._zq("Claude's opinion [maybe]")
    check("'" not in spec.replace("'\"'\"'", ""), f"raw apostrophe survived: {spec}")
    check("[" not in spec and "]" not in spec, f"raw bracket survived: {spec}")



# ---------------------------------------------------------------- fuzzy search

def _bank(conn, source, texts):
    for t in texts:
        _active(conn, source, t)


def test_fuzzy_finds_the_question_you_half_remember() -> None:
    conn, a, _ = fresh()
    _bank(conn, a, [
        "Walk me through a discounted cash flow analysis",
        "What is the bridge from equity value to enterprise value?",
        "How do you calculate the weighted average cost of capital?",
        "Tell me about a time you worked on a difficult team",
    ])
    hits, mode = search.search(conn, "walk me thru discounted cashflow")
    check(mode == "fuzzy", f"expected a fuzzy fallback, got {mode}")
    check(hits and "discounted cash flow" in hits[0]["canonical_text"].lower(),
          f"typo query led with {hits[0]['canonical_text'] if hits else 'nothing'}")


def test_an_abbreviation_finds_what_it_stands_for() -> None:
    conn, a, _ = fresh()
    _bank(conn, a, [
        "What is the bridge from equity value to enterprise value?",
        "Tell me about a time you disagreed with your manager",
    ])
    hits, _ = search.search(conn, "eqv to ev bridge")
    check(hits and "enterprise value" in hits[0]["canonical_text"].lower(),
          "the shorthand a banker actually types found nothing")


def test_fuzzy_does_not_match_everything() -> None:
    """Subsequence matching over a whole question makes every short string a
    hit, which ranked pure noise at the top of every search."""
    conn, a, _ = fresh()
    _bank(conn, a, [
        "What is the bridge from equity value to enterprise value?",
        "How does depreciation flow through the three statements?",
        "Why might one company trade at a higher multiple than another?",
    ])
    hits = search.fuzzy(conn, "xylophone quarterly")
    check(hits == [], f"a nonsense query matched {len(hits)} questions")


def test_exact_search_still_wins_when_it_can() -> None:
    conn, a, _ = fresh()
    _bank(conn, a, ["What is the treasury stock method?"])
    _, mode = search.search(conn, "treasury stock method")
    check(mode == "exact", "an exact phrase went through the fuzzy path")



# ---------------------------------------------------------------- planning

def test_plan_reads_the_dates_a_person_types() -> None:
    from datetime import date as _date
    today = _date(2026, 8, 17)
    cases = {
        "2026-09-15": _date(2026, 9, 15),
        "+14d": _date(2026, 8, 31),
        "3 weeks": _date(2026, 9, 7),
        "tomorrow": _date(2026, 8, 18),
        "sep 15": _date(2026, 9, 15),
    }
    for raw, want in cases.items():
        got = plan_mod.parse_target(raw, today=today)
        check(got == want, f"{raw!r} parsed as {got}, wanted {want}")
    check(plan_mod.parse_target("next tuesday", today=today) is None,
          "an unparseable date was silently accepted")


def test_a_bare_month_day_rolls_to_next_year_when_it_has_passed() -> None:
    from datetime import date as _date
    got = plan_mod.parse_target("jan 10", today=_date(2026, 8, 17))
    check(got == _date(2027, 1, 10), f"'jan 10' in August resolved to {got}")


def test_an_impossible_plan_says_so_instead_of_inventing_a_number() -> None:
    conn, a, _ = fresh()
    for i in range(400):
        _active(conn, a, f"Bank question number {i} covering valuation mechanics")
    from datetime import date as _date, timedelta as _td
    target = datetime.now(timezone.utc).date() + _td(days=2)
    p = plan_mod.build(conn, target)
    check(p["feasible"] is False, "400 questions in two days was reported as feasible")
    check(p["unreachable"] > 0, "an infeasible plan claimed to reach everything")
    check(p["reachable"] + p["unreachable"] == p["unseen"],
          "triage lost questions: reachable + unreachable != unseen")


def test_a_roomy_plan_is_feasible_and_covers_everything() -> None:
    conn, a, _ = fresh()
    for i in range(20):
        _active(conn, a, f"A small bank question number {i} about accounting basics")
    from datetime import timedelta as _td
    p = plan_mod.build(conn, datetime.now(timezone.utc).date() + _td(days=60))
    check(p["feasible"] is True, "20 questions in 60 days was called infeasible")
    check(p["unreachable"] == 0, "a feasible plan still dropped questions")
    check(p["daily_total"] >= p["daily_new"], "daily total was under daily new")


def test_the_plan_paces_from_your_own_sittings_once_it_can() -> None:
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Timed question {i} on leveraged buyout mechanics")
           for i in range(12)]
    check(plan_mod.seconds_per_question(conn) == plan_mod.DEFAULT_SECONDS_PER_QUESTION,
          "an empty history produced a measured pace")

    sid = session.open_session(conn, "drill", ids, {})
    for qid in ids:
        session.record(conn, sid, qid, 3, 40.0)
    check(plan_mod.seconds_per_question(conn) == 40,
          "twelve 40-second answers did not move the measured pace")


def test_a_piped_test_run_does_not_become_your_measured_pace() -> None:
    """Sittings driven from a script answer in milliseconds; taking that as
    the real pace would tell you the whole bank fits in an afternoon."""
    conn, a, _ = fresh()
    ids = [_active(conn, a, f"Instant question {i} about discounted cash flow")
           for i in range(12)]
    sid = session.open_session(conn, "drill", ids, {})
    for qid in ids:
        session.record(conn, sid, qid, 3, 0.1)
    check(plan_mod.seconds_per_question(conn) == plan_mod.DEFAULT_SECONDS_PER_QUESTION,
          "sub-second scripted answers were taken as a human pace")


def test_the_plan_leads_with_the_topic_you_are_worst_at() -> None:
    conn, a, _ = fresh()
    weak = _active(conn, a, "A hard question about the debt schedule", topic="lbo")
    _active(conn, a, "Another lbo question you have not seen", topic="lbo")
    strong = _active(conn, a, "An easy question about the income statement",
                     topic="accounting")
    _active(conn, a, "Another accounting question you have not seen", topic="accounting")
    for rating, qid in ((1, weak), (4, strong)):
        conn.execute("INSERT INTO reviews (question_id, asked_at, rating, grader) "
                     "VALUES (?, datetime('now'), ?, 'self')", (qid, rating))
    conn.commit()
    from datetime import timedelta as _td
    p = plan_mod.build(conn, datetime.now(timezone.utc).date() + _td(days=30))
    check(p["topics"][0]["topic"] == "lbo",
          f"plan led with {p['topics'][0]['topic']}, not the topic rated 1/4")



# ---------------------------------------------------------------- ingest pipeline

def _sample_epub(tmp: Path) -> Path:
    import zipfile
    out = tmp / "sample.epub"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<package><metadata><dc:title>Vault Guide</dc:title></metadata>'
                   '<manifest>'
                   '<item id="cov" href="cover.xhtml"/>'
                   '<item id="c1" href="ch1.xhtml"/>'
                   '<item id="idx" href="index.xhtml"/>'
                   '</manifest>'
                   '<spine><itemref idref="c1"/><itemref idref="cov"/>'
                   '<itemref idref="idx"/></spine></package>')
        z.writestr("OEBPS/cover.xhtml", "<html><head><title>Cover</title></head>"
                                        "<body><p>art</p></body></html>")
        z.writestr("OEBPS/ch1.xhtml",
                   "<html><head><title>Chapter 1: Accounting</title></head><body>"
                   "<style>p{color:red}</style><script>x()</script>"
                   + "<p>Walk me through the three statements. Net income flows through. </p>" * 20
                   + "</body></html>")
        z.writestr("OEBPS/index.xhtml", "<html><head><title>Index</title></head><body>"
                   + "<p>entry</p>" * 300 + "</body></html>")
    return out


def test_epub_reads_the_spine_order_not_the_zip_order() -> None:
    tmp = Path(tempfile.mkdtemp())
    book = _sample_epub(tmp)
    check(epub_mod.book_title(book) == "Vault Guide", "epub title not read from the manifest")
    names = [n for n, _ in epub_mod.spine(book)]
    check(names[0].endswith("ch1.xhtml"), f"spine order ignored: {names}")


def test_epub_drops_front_and_back_matter() -> None:
    tmp = Path(tempfile.mkdtemp())
    locators = [loc for loc, _ in epub_mod.chunks(_sample_epub(tmp))]
    check(all("Cover" not in l and "Index" not in l for l in locators),
          f"front/back matter was queued for extraction: {locators}")
    check(any("Accounting" in l for l in locators),
          f"the actual chapter was dropped: {locators}")


def test_epub_strips_script_and_style_bodies() -> None:
    text = epub_mod.strip_html(
        "<html><style>p{color:red}</style><script>alert(1)</script>"
        "<p>Real prose here</p></html>")
    check("color" not in text and "alert" not in text, f"markup leaked into text: {text!r}")
    check("Real prose here" in text, "the prose was stripped along with the markup")


def test_windowing_overlaps_so_a_split_question_survives() -> None:
    """A cut between a question and its answer produces one chunk of orphan
    questions and one of orphan answers, and grounding then discards both."""
    text = "\n\n".join(f"Paragraph number {i} with a reasonable amount of text in it."
                       for i in range(200))
    parts = pipeline.window(text, chars=1000, overlap=200)
    check(len(parts) > 1, "long text was not split at all")
    for (_, a), (_, b) in zip(parts, parts[1:]):
        check(a[-80:] and any(w in b for w in a[-80:].split()[:4]),
              "consecutive windows do not overlap")


def test_windowing_leaves_short_text_alone() -> None:
    parts = pipeline.window("A single short paragraph.", chars=1000)
    check(len(parts) == 1 and parts[0][1] == "A single short paragraph.",
          f"short text was mangled: {parts}")


def test_the_pipeline_grounds_admits_and_tags_in_one_pass() -> None:
    conn, a, _ = fresh()

    def extractor(text):
        quote = " ".join(text.split()[:25])
        return [{"question": "Walk me through the bridge from equity value to "
                             "enterprise value?",
                 "answer": "Add net debt, minority interest and preferred stock.",
                 "source_quote": quote, "topic": "ev_eqv", "difficulty": 2}]

    body = ("The bridge from equity value to enterprise value adds net debt. " * 30)
    out = pipeline.run(conn, a, [("ch1", body), ("ch2", body)], extractor=extractor)
    check(out.new == 1, f"expected 1 new question, got {out.new}")
    check(out.duplicate == 1, "the same question from a second chunk was not deduped")
    check("ev-bridge" in out.tags, f"the pipeline did not autotag: {out.tags}")

    row = conn.execute("SELECT topic, kind FROM questions").fetchone()
    check(row["topic"] == "ev_eqv", f"extractor topic was not applied: {row['topic']}")


def test_the_pipeline_discards_an_ungrounded_extraction() -> None:
    """The one mechanical signal that a question was invented is that its quote
    is not in the source it claims to come from."""
    conn, a, _ = fresh()

    def liar(text):
        return [{"question": "A question the source never actually asked anywhere?",
                 "answer": "An answer with no basis in the text at all.",
                 "source_quote": "words that appear nowhere in the supplied source text",
                 "topic": "general", "difficulty": 3}]

    out = pipeline.run(conn, a, [("ch1", "Entirely unrelated prose about accounting. " * 30)],
                       extractor=liar)
    check(out.new == 0, "an ungrounded question was admitted to the bank")
    check(out.ungrounded == 1, "the discard was not counted as ungrounded")


def test_a_slow_call_keeps_the_frame_alive_and_can_be_abandoned() -> None:
    """The freeze this whole pass started from.

    A graded answer is a network round trip. Run on the main thread it froze
    the shell -- no spinner, no elapsed time, no way out -- and a rate-limited
    provider turned that into minutes of a screen that looked crashed.
    """
    import threading
    from .tui import Key, Shell

    shell = Shell.__new__(Shell)           # no terminal to take in a test
    shell.busy = ""
    shell.tick = 0
    painted = {"n": 0}
    shell.paint = lambda: painted.__setitem__("n", painted["n"] + 1)

    class FakeReader:
        def __init__(self, keys):
            self.keys = list(keys)

        def read(self, timeout=None):
            return self.keys.pop(0) if self.keys else None

    release = threading.Event()

    shell.reader = FakeReader([])
    check(shell.run_job("working", lambda: 42) == 42, "the result was lost")

    # It repaints while the work is in flight, which is what makes the
    # spinner and the elapsed counter move.
    shell.reader = FakeReader([])
    painted["n"] = 0

    def slow():
        release.wait(2.0)
        return "done"

    t = threading.Timer(0.25, release.set)
    t.start()
    try:
        check(shell.run_job("working", slow) == "done", "slow call lost its result")
    finally:
        t.cancel()
    check(painted["n"] > 1, f"the frame never repainted while waiting ({painted['n']})")
    check(shell.busy == "", "the status line was left saying it was busy")

    # Escape gives up on the wait rather than on the shell.
    release.clear()
    shell.reader = FakeReader([Key("esc")])
    raised = False
    try:
        shell.run_job("working", lambda: release.wait(1.0))
    except KeyboardInterrupt:
        raised = True
    finally:
        release.set()
    check(raised, "escape did not abandon the call")

    # An exception on the worker surfaces to the caller, not into the void.
    shell.reader = FakeReader([])
    caught = None
    try:
        shell.run_job("working", lambda: (_ for _ in ()).throw(llm.LLMError("no credit")))
    except llm.LLMError as e:
        caught = e
    check(caught is not None and "no credit" in str(caught),
          "the worker's exception was swallowed")


def test_enrich_cannot_canonicalise_two_questions_into_one() -> None:
    """The gate runs at ingest; enrich rewrites afterwards.

    Nothing re-adjudicated that rewrite, so canonicalising could quietly
    produce two live questions with byte-identical text -- which is how the
    bank ended up with two of "Walk me through a basic merger model."
    """
    conn, a, _ = fresh()
    keep = admit(conn, source_id=a,
                 question_text="Walk me through a basic merger model.",
                 answer_text="Assumptions, purchase price, sources and uses, "
                             "then accretion/dilution.", status="active").matched_id
    other = admit(conn, source_id=a,
                  question_text="Could you take me through a simple merger model?",
                  answer_text="Same steps, different wording entirely.",
                  status="active").matched_id
    check(keep != other, "the gate should have seen these as different questions")

    # enrich now decides the second one's canonical wording is the first's.
    enrich_apply(conn, other, {
        "canonical_question": "Walk me through a basic merger model.",
        "topic": "ma", "subtopic": "", "difficulty": 3,
        "rubric_points": ["sources and uses"], "common_mistakes": [],
    })
    conn.commit()

    texts = [r[0] for r in conn.execute(
        "SELECT canonical_text FROM questions WHERE status != 'rejected'")]
    check(len(texts) == len(set(texts)), f"enrich created a duplicate: {texts}")
    keys = [r[0] for r in conn.execute(
        "SELECT norm_key FROM questions WHERE status != 'rejected'")]
    check(len(keys) == len(set(keys)), "two live questions share a norm_key")

    # The rest of the enrichment still lands -- only the wording is refused.
    row = conn.execute("SELECT topic, difficulty FROM questions WHERE id = ?",
                       (other,)).fetchone()
    check(row["topic"] == "ma" and row["difficulty"] == 3,
          "refusing the rewrite threw away the enrichment with it")


def test_merging_a_duplicate_keeps_what_was_yours() -> None:
    """Reviews, notes and tags are the half of the schema that is not
    reproducible from the sources. A merge that drops them is data loss."""
    from .dupes import merge as merge_questions
    conn, a, _ = fresh()
    keeper = admit(conn, source_id=a, question_text="What is an LBO and how does it work?",
                   answer_text="Buy with debt, pay it down, sell.", status="active").matched_id
    dupe = admit(conn, source_id=a, question_text="What is an LBO, and how does the process go?",
                 answer_text="Same idea, other words.", status="active").matched_id
    check(keeper != dupe, "the gate merged them before the test could")

    scheduler.record_review(conn, dupe, 3)
    conn.execute("INSERT INTO notes (question_id, body, created_at) VALUES (?,?,?)",
                 (dupe, "ask about the debt schedule", "2026-01-01T00:00:00+00:00"))
    tagging.attach(conn, dupe, ["lbo-returns"])
    conn.commit()

    merge_questions(conn, keeper, dupe, history.new_batch())

    for table in ("reviews", "notes", "question_tags"):
        left = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE question_id = ?", (dupe,)).fetchone()[0]
        moved = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE question_id = ?", (keeper,)).fetchone()[0]
        check(left == 0 and moved >= 1, f"{table} did not survive the merge")

    # The merged-away wording still has to resolve, or a re-ingest resurrects it.
    from .admission import adjudicate
    v = adjudicate(conn, "What is an LBO, and how does the process go?")
    check(v.kind == "duplicate" and v.matched_id == keeper,
          f"the old wording no longer maps to the keeper: {v}")
    check(conn.execute("SELECT status FROM questions WHERE id = ?",
                       (dupe,)).fetchone()[0] == "rejected", "the dupe is still live")


def test_rewriting_a_question_never_leaves_its_gate_key_behind() -> None:
    """norm_key is what the admission gate dedupes on.

    Any path that rewrites canonical_text has to rewrite the key with it. When
    one did not, eight questions ended up keyed on a fragment of their own
    text -- one on just "what is its diluted equity value" -- and were
    invisible to the gate under the wording they actually carried.
    """
    from .admission import adjudicate, normalize
    conn, a, _ = fresh()
    qid = admit(conn, source_id=a, question_text="What is its Diluted Equity Value?",
                answer_text="Diluted shares times the share price.",
                status="active").matched_id

    def key_matches(where: str) -> None:
        row = conn.execute(
            "SELECT canonical_text, norm_key FROM questions WHERE id = ?", (qid,)).fetchone()
        check(row["norm_key"] == normalize(row["canonical_text"]),
              f"{where} left norm_key stale: {row['norm_key']!r}")
        # And the gate has to find it under the text it now carries.
        v = adjudicate(conn, row["canonical_text"])
        check(v.kind == "duplicate" and v.matched_id == qid,
              f"{where}: the gate cannot see the question under its own wording ({v})")

    key_matches("admission")

    full = ("A company has 10,000 shares outstanding and a current share price "
            "of $20.00. What is its Diluted Equity Value?")
    enrich_apply(conn, qid, {"canonical_question": full, "topic": "ev_eqv",
                             "subtopic": "", "difficulty": 2,
                             "rubric_points": [], "common_mistakes": []})
    conn.commit()
    key_matches("enrich")

    audit_apply(conn, qid, {"verdict": "fix", "confidence": 0.95,
                            "reason": "tightened",
                            "corrected_question": full + " Assume TSM.",
                            "corrected_answer": None}, history.new_batch())
    conn.commit()
    key_matches("audit")


def test_markdown_export_is_diffable_and_shareable() -> None:
    """The export exists to be read, shared and committed.

    Which means: one file per topic so a changed answer is a three-line diff,
    a stable row order so a diff is never a reshuffle, no personal progress
    unless asked for, and no write at all when nothing moved.
    """
    import tempfile
    conn, a, _ = fresh()
    for i, topic in enumerate(("dcf", "dcf", "lbo")):
        qid = _active(conn, a, f"Question number {i} about {topic} and its drivers",
                      topic=topic)
    conn.commit()
    out = Path(tempfile.mkdtemp())

    first = backup.export_markdown(conn, out)
    check(first["topics"] == 2, f"expected two topic files, got {first['topics']}")
    check((out / "dcf.md").exists() and (out / "lbo.md").exists(),
          "topics were not split into their own files")
    check((out / "index.md").exists(), "no index was written")

    # Idempotent: a second export of an unchanged bank writes nothing, which
    # is what stops a scheduled export filling the history with empty diffs.
    again = backup.export_markdown(conn, out)
    check(again["written"] == 0, f"rewrote {again['written']} unchanged files")

    body = (out / "dcf.md").read_text()
    check(body.count("### #") == 2, "dcf.md did not carry both dcf questions")

    # Personal progress is opt-in, so the default output is safe to hand out.
    qid = conn.execute("SELECT id FROM questions WHERE topic='dcf' ORDER BY id").fetchone()[0]
    scheduler.record_review(conn, qid, 1)
    conn.execute("INSERT INTO notes (question_id, body, created_at) VALUES (?,?,?)",
                 (qid, "SECRET-NOTE", "2026-01-01T00:00:00+00:00"))
    conn.commit()
    backup.export_markdown(conn, out)
    check("SECRET-NOTE" not in (out / "dcf.md").read_text(),
          "a private note leaked into the shareable export")
    backup.export_markdown(conn, out, with_progress=True)
    check("SECRET-NOTE" in (out / "dcf.md").read_text(),
          "--with-progress did not include the note")


def test_consult_asks_about_the_questions_worth_asking_about() -> None:
    """Reviewing the bank front to back spends most of the effort on questions
    nothing is wrong with. The batch is ranked by what is actually suspect."""
    from . import consult
    conn, a, _ = fresh()
    plain = _active(conn, a, "What is the formula for Enterprise Value from Equity Value?",
                    topic="ev_eqv")
    disputed = _active(conn, a, "How do you calculate unlevered Free Cash Flow exactly?",
                       topic="dcf")
    conn.execute("INSERT INTO audits (question_id, provider, verdict, confidence, ran_at) "
                 "VALUES (?,?,?,?,?)", (disputed, "gemini", "keep", 0.9, "2026-01-01"))
    conn.execute("INSERT INTO audits (question_id, provider, verdict, confidence, ran_at) "
                 "VALUES (?,?,?,?,?)", (disputed, "claude-code", "reject", 0.9, "2026-01-01"))
    conn.commit()

    picked = [it["id"] for it in consult.select(conn, limit=10)]
    check(picked and picked[0] == disputed,
          f"an unresolved disagreement did not sort first: {picked}")
    check(plain in picked, "a never-reviewed question was left out entirely")

    # Already having an outside opinion normally takes a question out of the
    # pool -- but not when that is exactly what is unresolved about it.
    conn.execute("INSERT INTO audits (question_id, provider, verdict, confidence, ran_at) "
                 "VALUES (?,?,?,?,?)", (plain, "gpt-5", "keep", 0.9, "2026-01-01"))
    conn.commit()
    picked = [it["id"] for it in consult.select(conn, limit=10)]
    check(plain not in picked, "a question already judged outside was offered again")
    check(disputed in picked, "the disagreement stopped being eligible once seen")

    # A question with a live binding has no stored answer on purpose, so it is
    # not something an outside model can be asked to check.
    live = _active(conn, a, "Where is the 10-year Treasury yield trading today?",
                   topic="markets")
    conn.execute("UPDATE questions SET kind='market_awareness' WHERE id=?", (live,))
    conn.execute("UPDATE answers SET answer_key='' WHERE question_id=?", (live,))
    conn.execute("INSERT INTO live_bindings (question_id, provider, series_key, "
                 "tolerance) VALUES (?, 'treasury', '10y', 0.15)", (live,))
    conn.commit()
    check(live not in [it["id"] for it in consult.select(conn, limit=50)],
          "a question whose answer is fetched live was sent out to be fact-checked")

    # But the kind alone is not the test, and that is the point. Six questions
    # in the real bank were filed market_awareness with no binding at all and a
    # stored answer full of levels ("around 3.16%, as of yesterday"). Skipping
    # by kind made them the only questions no pass had ever looked at.
    stranded = _active(conn, a, "Where are ECB rates and why did they move?",
                       topic="markets")
    conn.execute("UPDATE questions SET kind='market_awareness' WHERE id=?", (stranded,))
    conn.commit()
    check(stranded in [it["id"] for it in consult.select(conn, limit=50)],
          "a market question with a stored answer and no binding was skipped anyway")


def test_consult_reads_a_reply_a_person_pasted() -> None:
    """The reply comes back through a human, so it arrives wrapped in prose,
    in a code fence, bulleted, and with whichever dash the model preferred."""
    from . import consult
    reply = """Sure! Here are my verdicts:

```
#22 keep 0.93 \u2014 bridge is right
- #73 keep 0.9 \u2013 stated correctly
#84 fix 0.88 - never says the asset sold at 2x EBITDA
> The company sells the asset for 2x its $20 of EBITDA, i.e. $40.
> Enterprise Value therefore falls by $40.
#1 keep 0.95: only excess cash is deductible
#9 reject 0.8 | this reverses the bridge
#5 keep
```

Happy to go deeper on any of these."""
    items, problems = consult.parse(reply)
    check(len(items) == 6, f"parsed {len(items)} of 6 verdicts: {items}")
    check(problems == [], f"clean reply produced complaints: {problems}")
    by_id = {i["question_id"]: i for i in items}
    check(by_id[22]["verdict"] == "keep" and by_id[22]["confidence"] == 0.93, "em dash line")
    check(by_id[73]["verdict"] == "keep", "bulleted en-dash line")
    check(by_id[9]["verdict"] == "reject", "pipe-separated line")
    check(by_id[5]["confidence"] == 0.7, "a missing confidence did not default")
    check("falls by $40" in (by_id[84]["corrected_answer"] or ""),
          f"the multi-line correction was lost: {by_id[84]}")
    check(by_id[22]["corrected_answer"] is None,
          "a correction leaked onto the wrong verdict")

    # A reply that is only prose has to be reported, never silently accepted.
    _, problems = consult.parse("I think these all look fine to me, honestly.")
    check(problems, "a reply with no verdicts was accepted silently")

    # And a fix with no stated reason is a complaint, not a stored verdict.
    _, problems = consult.parse("#7 fix 0.9")
    check(any("no reason" in p for p in problems), f"unreasoned fix passed: {problems}")


def test_api_failures_read_as_sentences_not_json() -> None:
    """The 429 that started this: a raw JSON blob pasted into a drill.

    Every branch here has to produce something a person can act on, and has
    to decide correctly whether waiting could possibly help.
    """
    depleted = json.dumps({"error": {
        "code": 429,
        "message": "Your prepayment credits are depleted. Please go to AI "
                   "Studio to manage your project and billing.",
        "status": "RESOURCE_EXHAUSTED"}})

    e = llm.classify("Gemini", 429, depleted)
    check("{" not in str(e), f"raw JSON reached the message: {e}")
    check("credit" in str(e).lower(), f"unhelpful message: {e}")
    check(not e.retryable, "a depleted balance was treated as transient")
    check(bool(e.hint), "no suggestion of what to do about it")

    rate = llm.classify("Gemini", 429, json.dumps(
        {"error": {"message": "Quota exceeded for requests per minute"}}))
    check(rate.retryable, "a per-minute rate limit was treated as permanent")

    for status, word in ((401, "key"), (403, "key"), (404, "model"),
                         (503, "unavailable")):
        e = llm.classify("Gemini", status, "{}", model="gemini-x")
        check(word in str(e).lower(), f"HTTP {status} said {e!r}")
    check(not llm.classify("Gemini", 401, "{}").retryable,
          "a rejected key was retried")
    check(llm.classify("Gemini", 503, "{}").retryable,
          "a temporary outage was not retried")

    # A body that is not JSON at all must not blow up the classifier.
    e = llm.classify("Claude", 500, "<html>502 Bad Gateway</html>")
    check("Claude" in str(e), f"provider name lost: {e}")

    # The raw body stays reachable for debugging, just not by default.
    e = llm.classify("Gemini", 429, depleted)
    check("prepayment" in e.detail, "the provider's own words were thrown away")
    os.environ["IB_DEBUG"] = "1"
    try:
        check("prepayment" in str(e), "IB_DEBUG did not surface the detail")
    finally:
        del os.environ["IB_DEBUG"]


def test_the_pipeline_gives_up_after_repeated_failures() -> None:
    """Grinding through 200 chunks to collect 200 identical quota errors wastes
    minutes and quota alike.

    How fast it gives up depends on whether waiting could help. A rate limit
    is worth a few more tries; a depleted balance or a rejected key is not,
    and burning two more batches to re-learn that is the behaviour that had
    the screen frozen for minutes.
    """
    conn, a, _ = fresh()

    def failing(error):
        calls = {"n": 0}

        def extractor(text):
            calls["n"] += 1
            raise error
        return extractor, calls

    transient, calls = failing(llm.LLMError("rate limited", retryable=True))
    out = pipeline.run(conn, a, [(f"c{i}", "text " * 200) for i in range(50)],
                       extractor=transient)
    check(out.aborted, "the pipeline ground on through every chunk")
    check(calls["n"] == pipeline.MAX_CONSECUTIVE_FAILURES,
          f"gave up after {calls['n']} calls, wanted {pipeline.MAX_CONSECUTIVE_FAILURES}")

    fatal, calls = failing(llm.LLMError("no credit", retryable=False))
    out = pipeline.run(conn, a, [(f"c{i}", "text " * 200) for i in range(50)],
                       extractor=fatal)
    check(out.aborted, "a fatal error did not abort the run")
    check(calls["n"] == 1, f"burned {calls['n']} calls on an error that cannot pass")


# ---------------------------------------------------------------- web ingest

def test_reddit_json_yields_the_comments_not_just_the_post() -> None:
    """The interview questions on r/FinancialCareers are in the replies."""
    payload = json.dumps([
        {"kind": "Listing", "data": {"children": [
            {"kind": "t3", "data": {"title": "Evercore superday",
                                    "selftext": "Here is what they asked me."}}]}},
        {"kind": "Listing", "data": {"children": [
            {"kind": "t1", "data": {"body": "They asked me to walk through a DCF and "
                                            "then how depreciation flows through all "
                                            "three of the statements.",
                                    "replies": {"kind": "Listing", "data": {"children": [
                                        {"kind": "t1", "data": {
                                            "body": "Same at Moelis, plus why you would "
                                                    "use EV/EBITDA over P/E for two "
                                                    "different capital structures.",
                                            "replies": ""}}]}}}},
            {"kind": "t1", "data": {"body": "[deleted]"}},
            {"kind": "t1", "data": {"body": "this"}}]}}])
    title, bodies = web_mod.parse_reddit(payload)
    check(title == "Evercore superday", f"thread title was {title!r}")
    check(len(bodies) == 3, f"expected post + 2 real comments, got {len(bodies)}")
    check(any("Moelis" in b for b in bodies), "a nested reply was not walked")
    check(not any("[deleted]" in b for b in bodies), "a deleted comment was ingested")


def test_reddit_url_becomes_its_json_endpoint() -> None:
    got = web_mod.reddit_json_url("https://www.reddit.com/r/FinancialCareers/comments/abc/x/?utm=1")
    check(got.startswith("https://www.reddit.com/r/FinancialCareers/comments/abc/x.json"),
          f"got {got}")
    check(web_mod.is_reddit("https://old.reddit.com/r/x") is True, "reddit host not matched")
    check(web_mod.is_reddit("https://example.com") is False, "non-reddit host matched")


def test_readable_drops_page_chrome_but_keeps_prose() -> None:
    markup = ("<html><head><title>WSO Thread</title></head><body>"
              "<nav>Log in</nav><script>var x=1;</script>"
              "<p>What is the difference between enterprise value and equity value?</p>"
              "<p>Reply</p>"
              "<footer>Terms of Service</footer></body></html>")
    text = web_mod.readable(markup)
    check("enterprise value" in text, "the actual prose was stripped")
    check("var x" not in text, "a script body survived")
    check("Log in" not in text and "Terms of Service" not in text,
          f"navigation chrome survived: {text!r}")


def test_a_thread_that_gained_replies_is_a_new_source() -> None:
    a = web_mod.source_hash("http://x", "one comment")
    b = web_mod.source_hash("http://x", "one comment\ntwo comments")
    check(a != b, "a thread with new replies hashed identically to the old one")


# ---------------------------------------------------------------- filings

def _facts(**lines) -> dict:
    us_gaap = {}
    tag_for = {k: sec_mod.CONCEPTS[k][0] for k in lines}
    for line, val in lines.items():
        instant = line in sec_mod.INSTANT
        row = {"end": "2024-09-28", "val": val, "form": "10-K", "fp": "FY",
               "fy": 2024, "filed": "2024-11-01"}
        if not instant:
            row["start"] = "2023-10-01"
        us_gaap[tag_for[line]] = {"units": {"USD": [row]}}
    return {"entityName": "Testco Inc.", "facts": {"us-gaap": us_gaap}}


def test_a_quarterly_figure_never_becomes_an_annual_one() -> None:
    """A quarterly revenue in an annual margin is wrong by a factor of four and
    looks entirely plausible."""
    facts = _facts(revenue=400.0)
    tag = sec_mod.CONCEPTS["revenue"][0]
    facts["facts"]["us-gaap"][tag]["units"]["USD"].append(
        {"start": "2024-06-30", "end": "2024-09-28", "val": 100.0,
         "form": "10-K", "fp": "FY", "fy": 2024, "filed": "2024-11-01"})
    figures = sec_mod.annual_figures(facts)
    check(figures["revenue"]["value"] == 400.0,
          f"a 90-day period was used as the year: {figures['revenue']['value']}")


def test_filing_questions_do_the_arithmetic_themselves() -> None:
    figures = sec_mod.annual_figures(_facts(revenue=1000.0, net_income=250.0))
    qs = sec_mod.build_questions("Testco", figures)
    margin = [q for q in qs if "net margin" in q["question"]][0]
    check("25.0%" in margin["answer"], f"margin not computed: {margin['answer']}")
    check(any("25.0%" in r for r in margin["rubric_points"]),
          "the rubric does not carry the checkable figure")


def test_the_three_statement_walkthrough_actually_balances() -> None:
    figures = sec_mod.annual_figures(
        _facts(revenue=1000.0, net_income=250.0, operating_income=300.0,
               d_and_a=100.0, pretax_income=400.0, tax_expense=100.0))
    qs = sec_mod.build_questions("Testco", figures)
    walk = [q for q in qs if "three statements" in q["question"]]
    check(walk, "no three-statement question was generated")
    answer = walk[0]["answer"]
    # D&A up 10.0, tax rate 25%: net income -7.5, cash +2.5, PP&E -10.0.
    for figure in ("$10", "$7", "$2"):
        check(figure in answer, f"{figure} missing from: {answer[:200]}")
    check("balances" in answer, "the walkthrough never confirms the balance sheet balances")


def test_a_filing_with_nothing_tagged_produces_no_questions() -> None:
    check(sec_mod.build_questions("Testco", {}) == [],
          "questions were invented from an empty filing")


def test_filing_figures_are_stated_at_filing_scale() -> None:
    check(sec_mod.money(391_035_000_000) == "$391.04bn", sec_mod.money(391_035_000_000))
    check(sec_mod.money(1_500_000) == "$1.5m", sec_mod.money(1_500_000))
    check(sec_mod.money(-2_000_000_000) == "$-2.00bn", sec_mod.money(-2_000_000_000))



# ---------------------------------------------------------------- mechanical checks

def _kinds(text: str) -> set[str]:
    return {f.kind for f in checks.inspect(text)}


def test_a_reversed_ev_bridge_is_caught() -> None:
    check("bridge" in _kinds("Enterprise Value = Equity Value - Net Debt."),
          "the reversed EV bridge was not caught")
    check("bridge" in _kinds("Equity Value = Enterprise Value + Net Debt."),
          "the reversed reverse-bridge was not caught")
    check("bridge" in _kinds("You subtract net debt to arrive at enterprise value."),
          "the reversed bridge in prose was not caught")


def test_the_correct_ev_bridge_is_left_alone() -> None:
    for good in ("Enterprise Value = Equity Value + Net Debt.",
                 "Equity Value = Enterprise Value - Net Debt.",
                 "You add net debt, preferred stock and minority interest to equity "
                 "value to get enterprise value."):
        check("bridge" not in _kinds(good), f"flagged a correct bridge: {good}")


def test_a_warning_about_an_error_is_not_an_error() -> None:
    """A pattern describing a mistake also matches the sentence warning against
    it. Flagging those teaches you to ignore the whole report."""
    check(_kinds("Goodwill is not amortized under US GAAP.") == set(),
          "flagged the correct statement that goodwill is not amortized")
    check("bridge" not in _kinds(
        "A common mistake is saying enterprise value = equity value - net debt."),
        "flagged a common-mistakes note as if it were the answer")
    check("linkage" not in _kinds(
        "Candidates often say depreciation increases net income, which is wrong."),
        "flagged a stated trap as the claim itself")


def test_a_multi_term_chain_is_evaluated_whole() -> None:
    """Matching the last two terms of `222 - 10 + 30 + 15 = 257` reports a
    correct EV bridge as an arithmetic error three orders of magnitude wide."""
    check(_kinds("Enterprise Value = $222,000 - $10,000 + $30,000 + $15,000 = $257,000.")
          == set(), "a correct four-term chain was flagged")
    check("arithmetic" in _kinds("$222,000 - $10,000 + $30,000 + $15,000 = $260,000."),
          "a wrong four-term chain was not flagged")


def test_a_thousands_separator_does_not_truncate_the_result() -> None:
    check(_kinds("100 * $10.00 = $1,000.") == set(),
          "a trailing full stop truncated $1,000 to $1 and produced a false error")
    check(_kinds("The price was $1,250,000 and 2 * $625,000 = $1,250,000.") == set(),
          "a seven-figure result was truncated")


def test_real_arithmetic_errors_are_caught() -> None:
    check("arithmetic" in _kinds("$100 + $50 = $140."), "simple addition error missed")
    check("arithmetic" in _kinds("EBITDA is $1.2bn + $0.3bn = $1.6bn"),
          "an error in billions was missed")
    check("arithmetic" in _kinds("$500 - $200 = $250 of net debt."),
          "subtraction error missed")


def test_implied_units_are_left_to_the_models() -> None:
    """"$1.5bn less costs of 300 = $1.2bn" is an author writing the second term
    in millions, not an arithmetic mistake."""
    check("arithmetic" not in _kinds("Revenue of $1.5bn less 300 = $1.2bn"),
          "flagged an answer whose units are implied by context")


def test_statement_linkage_errors_are_caught() -> None:
    cases = {
        "Depreciation increases net income because it is a non-cash charge.": "linkage",
        "Adding back D&A reduces cash on the cash flow statement.": "linkage",
        "An increase in working capital increases cash.": "linkage",
        "Deferred revenue is an asset because the cash has been received.": "linkage",
    }
    for text, kind in cases.items():
        check(kind in _kinds(text), f"missed: {text}")


def test_formula_errors_are_caught() -> None:
    cases = [
        "You multiply the cost of debt by (1 + tax rate).",
        "Use the after-tax cost of equity in WACC.",
        "You discount levered free cash flow at the WACC.",
        "Unlevered free cash flow subtracts interest expense.",
    ]
    for text in cases:
        check("formula" in _kinds(text), f"missed: {text}")


def test_correct_finance_prose_is_never_flagged() -> None:
    """The one-sided bar: a check must never fire on a right answer."""
    good = [
        "WACC weights the after-tax cost of debt and the cost of equity.",
        "You multiply the cost of debt by (1 - the tax rate) for the tax shield.",
        "Unlevered free cash flow is before interest, so it is discounted at WACC.",
        "Terminal value must be discounted back to the present like any other "
        "future cash flow.",
        "Depreciation reduces net income but is added back on the cash flow "
        "statement because it is non-cash.",
        "An increase in working capital is a use of cash and decreases cash.",
        "Deferred revenue is a liability until the service is delivered.",
        "Capex appears on the cash flow statement under investing activities.",
    ]
    for text in good:
        check(_kinds(text) == set(), f"flagged correct prose: {text} -> {_kinds(text)}")


def test_the_scan_checks_the_rubric_as_well_as_the_answer() -> None:
    """A rubric point stating the bridge backwards teaches the error even when
    the answer beneath it is right."""
    conn, a, _ = fresh()
    qid = _active(conn, a, "How do you get from equity value to enterprise value?",
                  answer="You add net debt to equity value.")
    conn.execute("UPDATE answers SET rubric_points = ? WHERE question_id = ?",
                 (json.dumps(["States that enterprise value = equity value - net debt"]), qid))
    conn.commit()
    rows = checks.scan(conn)
    check(rows and rows[0]["id"] == qid, "a wrong rubric point went unchecked")
    check(any(f.kind == "bridge" for f in rows[0]["findings"]),
          "the rubric error was not identified as a bridge error")


def test_a_clean_bank_produces_no_findings() -> None:
    conn, a, _ = fresh()
    _active(conn, a, "What is WACC?",
            answer="The weighted average cost of capital: the after-tax cost of debt "
                   "and the cost of equity, weighted by their share of the structure.")
    check(checks.scan(conn) == [], "a clean answer produced findings")



# ---------------------------------------------------------------- cross-audit wiring

def test_cross_audit_export_flag_reaches_the_handler() -> None:
    """`--export` was declared with argparse's default dest (`export`) while
    the handler read `export_path`, so the entire no-API-key cross-audit path
    silently did nothing."""
    from .cli import build_parser
    args = build_parser().parse_args(["cross-audit", "--export", "out.json"])
    check(getattr(args, "export_path", None) == "out.json",
          f"--export landed on {sorted(vars(args))}")
    bare = build_parser().parse_args(["cross-audit", "--export"])
    check(bare.export_path == "",
          f"a bare --export produced {bare.export_path!r}, which would be used as a filename")
    check(build_parser().parse_args(["cross-audit"]).export_path is None,
          "cross-audit without --export looked like an export")


def test_every_command_flag_reaches_its_handler() -> None:
    """The same class of bug anywhere else: a dest no cmd_* function reads.

    Every `args.<name>` in cli.py that is not a known non-argparse attribute
    has to be a dest some subparser actually defines.
    """
    import argparse
    import re as _re
    from pathlib import Path as _Path
    from .cli import build_parser

    source = (_Path(__file__).resolve().parent / "cli.py").read_text()
    used = set(_re.findall(r"\bargs\.([a-z_][a-z0-9_]*)", source))
    used -= {"fn", "func"}
    # getattr(args, "x", default) is the explicitly-optional form.
    used -= set(_re.findall(r'getattr\(args,\s*"([a-z_][a-z0-9_]*)"', source))

    declared = set()
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                declared.update(a.dest for a in child._actions)
    missing = sorted(used - declared)
    check(not missing, f"cli.py reads args.{{{', '.join(missing)}}} but no parser defines them")


def test_a_proven_error_is_handed_to_the_critic() -> None:
    """A critic re-deriving arithmetic sometimes gets it wrong. Findings that
    are decidable belong in the brief, not in the critic's workload."""
    conn, a, _ = fresh()
    qid = _active(conn, a, "How do you get from equity value to enterprise value?",
                  answer="Enterprise Value = Equity Value - Net Debt.")
    row = conn.execute(
        "SELECT q.id, q.kind, q.topic, q.canonical_text, a.answer_key, "
        "NULL AS first_verdict, NULL AS first_reason, 'gemini' AS first_provider "
        "FROM questions q LEFT JOIN answers a ON a.question_id = q.id WHERE q.id = ?",
        (qid,)).fetchone()
    item = crossaudit._item(row)
    check("mechanical_findings" in item, "a proven error was not passed to the critic")
    check(any(f["kind"] == "bridge" for f in item["mechanical_findings"]),
          f"wrong findings attached: {item['mechanical_findings']}")

    clean = conn.execute(
        "SELECT q.id, q.kind, q.topic, q.canonical_text, "
        "'You add net debt to equity value.' AS answer_key, "
        "NULL AS first_verdict, NULL AS first_reason, NULL AS first_provider "
        "FROM questions q WHERE q.id = ?",
        (qid,)).fetchone()
    check("mechanical_findings" not in crossaudit._item(clean),
          "a clean answer was handed a finding")



def test_a_drafted_answer_with_a_proven_error_is_not_stored() -> None:
    """A drafted answer has no source to be checked against, so the mechanical
    check runs before it is stored -- storing it and finding the error later
    means it may have been drilled in between."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "How do you bridge from equity value to enterprise value?",
                answer="")
    conn.commit()

    calls = []
    real_generate = enrich.llm.generate

    def fake_generate(prompt, **kw):
        calls.append(prompt)
        return {"items": [{
            "index": 0,
            "answer": "Enterprise Value = Equity Value - Net Debt.",
            "rubric_points": ["States the bridge"],
            "common_mistakes": ["Forgetting minority interest"]}]}

    enrich.llm.generate = fake_generate
    try:
        enrich.draft_missing_answers(conn, progress=lambda *a: None)
    finally:
        enrich.llm.generate = real_generate

    check(calls, "the drafting call never happened")
    stored = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                          (qid,)).fetchone()
    check(not (stored and (stored["answer_key"] or "").strip()),
          f"a provably wrong draft was stored: {stored['answer_key']!r}")


def test_a_clean_draft_is_stored() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "How do you bridge from equity value to enterprise value?",
                answer="")
    conn.commit()
    real_generate = enrich.llm.generate

    def fake_generate(prompt, **kw):
        return {"items": [{
            "index": 0,
            "answer": "You add net debt, preferred stock and minority interest to "
                      "equity value to reach enterprise value.",
            "rubric_points": ["Adds net debt", "Mentions preferred and minority interest"],
            "common_mistakes": ["Subtracting net debt instead of adding it"]}]}

    enrich.llm.generate = fake_generate
    try:
        enrich.draft_missing_answers(conn, progress=lambda *a: None)
    finally:
        enrich.llm.generate = real_generate

    stored = conn.execute("SELECT answer_key, common_mistakes FROM answers "
                          "WHERE question_id = ?", (qid,)).fetchone()
    check("add net debt" in (stored["answer_key"] or "").lower(),
          "a clean draft was rejected")
    check("Subtracting" in (stored["common_mistakes"] or ""),
          "the common mistakes were not stored alongside the answer")


def test_a_setting_written_to_env_local_reaches_the_call() -> None:
    """The knobs were module constants, bound from os.environ at import time --
    which is before load_env() has ever run. So `settings model_grade x` wrote
    x to .env.local, printed "set", displayed it back with source .env.local,
    and every call still went to the default. The accessors load first now."""
    tmp = Path(tempfile.mkdtemp()) / ".env.local"
    tmp.write_text("IB_MODEL_GRADE=sentinel-model\nIB_MIN_CALL_INTERVAL=9.5\n")
    keys = ("IB_MODEL_GRADE", "IB_MIN_CALL_INTERVAL")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        llm.load_env(tmp)
        check(llm.model_grade() == "sentinel-model",
              f"model_grade came back {llm.model_grade()!r}, not .env.local's")
        check(llm.min_call_interval() == 9.5,
              f"min_call_interval came back {llm.min_call_interval()}")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _with_config(**values):
    """Point the whole of `home()` at a tempdir holding exactly these keys.

    Through SUPERDAY_HOME rather than by rebinding a module attribute, because
    that is the supported way to move it and a test that patches around the
    real mechanism stops testing it.
    """
    from . import config as config_mod
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.local.json").write_text(json.dumps(values))
    saved = os.environ.get("SUPERDAY_HOME")
    os.environ["SUPERDAY_HOME"] = str(tmp)
    return config_mod, saved


def _restore_home(saved: str | None) -> None:
    if saved is None:
        os.environ.pop("SUPERDAY_HOME", None)
    else:
        os.environ["SUPERDAY_HOME"] = saved


def test_an_interview_date_is_stored_absolute() -> None:
    """`+3 weeks` written down literally means a different day every morning,
    which is the one thing a deadline may not do -- and a countdown built on it
    would never move. It is resolved on the way in, never on the way out."""
    from . import cli
    config_mod, saved = _with_config()
    try:
        entry = [e for e in cli.SETTINGS if e["key"] == "interview_date"][0]
        cli._settings_set(entry, "3 weeks")
        stored = json.loads(config_mod.local_config().read_text())["interview_date"]
        want = (datetime.now(timezone.utc).date() + timedelta(days=21)).isoformat()
        check(stored == want, f"stored {stored!r}, wanted the absolute {want!r}")

        # A date that has been and gone is not a target, and a countdown of
        # -3 days is worse than no countdown at all.
        config_mod.local_config().write_text(json.dumps({"interview_date": "2001-01-01"}))
        check(plan_mod.target_date() is None, "a date in the past was still a target")
        config_mod.local_config().write_text(json.dumps({"interview_date": ""}))
        check(plan_mod.target_date() is None, "an unset date resolved to something")
    finally:
        _restore_home(saved)


def test_the_first_pass_is_paced_against_the_date_not_the_window() -> None:
    """The Upcoming pane divided the unseen pool by its own 14-day window,
    which is a number nobody chose. With a date on file it has to be the days
    to that date, or the pane and `plan` quote two different daily paces."""
    from . import cli
    conn, a, _ = fresh()
    for i in range(40):
        _active(conn, a, f"Question number {i} about the enterprise value bridge")
    target = datetime.now(timezone.utc).date() + timedelta(days=10)
    config_mod, saved = _with_config(interview_date=target.isoformat())
    try:
        check(plan_mod.target_date() == target, "the date on file did not come back")
        pane = "\n".join(cli._dash_upcoming(conn, 100))
        check(f"to {target.isoformat()}" in pane,
              "the pane paced the first pass against its own window")
        # 40 unseen over 10 days is 4 a day, and it has to be the same number
        # `plan` would print for the same bank on the same date.
        p = plan_mod.build(conn, target)
        check(f"{p['daily_new']} a day" in pane,
              f"the pane and plan disagree; plan says {p['daily_new']}/day")
    finally:
        _restore_home(saved)


def test_a_thinking_level_typo_is_dropped_rather_than_sent() -> None:
    """The v1beta endpoint validates this enum before it checks quota, so a
    typo would turn every call into a 400 instead of a pricier answer."""
    check(llm.THINKING_BULK in llm.THINKING_LEVELS,
          f"the bulk default {llm.THINKING_BULK!r} is not a real level")
    check(llm.thinking_level("low") == "low", "a valid level was dropped")
    check(llm.thinking_level("banana") == "", "a typo would have been sent")
    check(llm.thinking_level() == "",
          "a caller that asked for nothing got a level anyway")


def test_bulk_extraction_asks_for_less_thinking_than_grading() -> None:
    """Enrich, extract and audit fill in a fixed schema; they were paying for
    thinking tokens with no way to turn them down. Grading is left alone."""
    conn, a, _ = fresh()
    _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.commit()
    seen: dict = {}
    real_generate = enrich.llm.generate

    def fake_generate(prompt, **kw):
        seen.update(kw)
        return {"items": []}

    enrich.llm.generate = fake_generate
    try:
        enrich.run(conn, progress=lambda *a: None)
    finally:
        enrich.llm.generate = real_generate
    check(seen.get("thinking") == llm.THINKING_BULK,
          f"enrich asked for thinking={seen.get('thinking')!r}")


def test_an_enrich_run_can_be_taken_back() -> None:
    """enrich wrote rubric_points and common_mistakes with a raw UPDATE, so a
    run that rewrote 800 rubrics was invisible to `undo` -- which is worse than
    no undo, because it looks like one."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE answers SET rubric_points = ?, common_mistakes = ? "
                 "WHERE question_id = ?",
                 (json.dumps(["the original point"]), json.dumps(["the original slip"]), qid))
    conn.commit()

    enrich_apply(conn, qid, {
        "canonical_question": "Walk me through a DCF.", "topic": "dcf",
        "subtopic": "wacc", "difficulty": 3,
        "rubric_points": ["a replacement point"],
        "common_mistakes": ["a replacement slip"],
    })
    conn.commit()
    row = conn.execute("SELECT rubric_points FROM answers WHERE question_id = ?",
                       (qid,)).fetchone()
    check("replacement" in row["rubric_points"], "the enrichment never landed")

    batch = history.last_batch(conn)
    check(batch is not None and batch["action"] == "enrich",
          f"the last batch was {batch['action'] if batch else None!r}, not enrich")
    history.undo_batch(conn, batch["batch_id"])
    row = conn.execute("SELECT rubric_points, common_mistakes FROM answers "
                       "WHERE question_id = ?", (qid,)).fetchone()
    check("the original point" in row["rubric_points"],
          f"undo left the rubric at {row['rubric_points']!r}")
    check("the original slip" in (row["common_mistakes"] or ""),
          f"undo left common_mistakes at {row['common_mistakes']!r}")


def test_enrichment_is_not_lost_when_there_is_no_answer_row_yet() -> None:
    """The raw UPDATE matched zero rows for a question with no answers row, so
    the rubric was silently discarded while extraction_version was bumped past
    it -- the call was paid for and pending() never offered the question
    again."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("DELETE FROM answers WHERE question_id = ?", (qid,))
    conn.commit()

    enrich_apply(conn, qid, {
        "canonical_question": "Walk me through a DCF.", "topic": "dcf",
        "subtopic": "wacc", "difficulty": 3, "rubric_points": ["states WACC"],
        "common_mistakes": [],
    })
    conn.commit()
    row = conn.execute("SELECT rubric_points FROM answers WHERE question_id = ?",
                       (qid,)).fetchone()
    check(row is not None, "the rubric went nowhere: no answers row was created")
    check("states WACC" in row["rubric_points"],
          f"the rubric came back {row['rubric_points']!r}")


def test_embeddings_are_fetched_a_batch_at_a_time() -> None:
    """One embedContent request per question meant 842 round trips, 842
    throttle waits and 842 chances for the failure budget to abort the run, to
    fetch what batchEmbedContents fetches in nine."""
    conn, a, _ = fresh()
    for i in range(5):
        _seed(conn, a, f"Question number {i} about discounted cash flow analysis")
    conn.commit()

    calls = []
    real_batch, real_available = search.llm.embed_batch, search.llm.available
    search.llm.embed_batch = lambda texts, **kw: (calls.append(len(texts))
                                                  or [[0.1, 0.2, 0.3] for _ in texts])
    search.llm.available = lambda: True
    try:
        done = search.index_embeddings(conn, progress=lambda *a: None)
    finally:
        search.llm.embed_batch, search.llm.available = real_batch, real_available

    check(done == 5, f"embedded {done} of 5 questions")
    check(len(calls) == 1, f"took {len(calls)} calls to embed 5 questions")
    models = {r[0] for r in conn.execute("SELECT DISTINCT model FROM embeddings")}
    check(models == {llm.model_embed()},
          f"stored under {models}, not the model that produced them")


def test_no_em_dashes_anywhere_in_the_tool() -> None:
    """A standing house rule that kept being broken and caught late, in strings
    the terminal actually prints. Same treatment as the completion file: drift
    is a failing test rather than a thing someone notices eventually."""
    from .config import PACKAGE
    ROOT = PACKAGE.parent
    files = sorted(PACKAGE.glob("**/*.py")) + sorted(PACKAGE.glob("migrations/*.sql"))
    files += [ROOT / "CLAUDE.md", ROOT / "README.md"]
    # Built, not written: a literal here would make this file its own first
    # offender.
    dash = chr(0x2014)
    offenders = []
    for f in files:
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if dash in line:
                offenders.append(f"{f.relative_to(ROOT)}:{i}")
    check(not offenders,
          f"em dashes found (use a plain dash): {', '.join(offenders[:5])}")


def _fake_gemini(payload: dict):
    """Stand in for one Gemini HTTP response, without a socket."""
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()
    return lambda req, **kw: R()


def test_a_truncated_answer_is_not_retried_three_times() -> None:
    """A response cut off at the output cap comes back as truncated JSON.
    json.loads raised, classify_transport read the ValueError as transient, and
    the identical oversized prompt went out three times -- three paid calls on
    a prompt that could not succeed, and an error naming neither cause nor
    fix."""
    calls = []
    real = llm.urllib.request.urlopen
    inner = _fake_gemini({"candidates": [{
        "finishReason": "MAX_TOKENS",
        "content": {"parts": [{"text": '{"items": [{"index": 0,'}]}}]})
    llm.urllib.request.urlopen = lambda req, **kw: (calls.append(1) or inner(req, **kw))
    # The transport asks for a key before it opens a socket, so without one
    # this never reaches the stub and the case measures nothing. It passed only
    # because the developer's own .env.local was sitting next to it: on a fresh
    # clone -- which is what CI and every contributor has -- `selftest` failed
    # here, with a message about paid calls that names nothing you could act on.
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "test-key"})
    try:
        llm.generate("x", schema={"type": "object"}, model="m", retries=3)
        raise AssertionError("a truncated response was accepted as complete")
    except llm.LLMError as e:
        check(not e.retryable, "a truncated response was treated as transient")
        check(len(calls) == 1, f"{len(calls)} paid calls on a doomed prompt")
        check("limit" in str(e).lower(), f"the message does not name the cause: {e}")
        check("batch" in e.hint, f"the hint does not name the fix: {e.hint!r}")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)


def test_a_rate_limit_waits_as_long_as_the_provider_asked() -> None:
    """Fixed 2s/4s backoff is shorter than any per-minute quota window, so all
    three attempts landed inside the same exhausted window -- and each rejected
    request still counted against the quota it was waiting on."""
    body = json.dumps({"error": {
        "code": 429, "message": "Quota exceeded for requests per minute",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                     "retryDelay": "37s"}]}})
    e = llm.classify("Gemini", 429, body)
    check(e.retryable, "a per-minute rate limit was treated as permanent")
    check(e.retry_after == 37.0, f"retry_after came back {e.retry_after!r}")
    check("37" in e.hint, f"the hint does not say how long: {e.hint!r}")

    hdr = llm.classify("Gemini", 429, "{}", retry_after=12.0)
    check(hdr.retry_after == 12.0, "the Retry-After header was ignored")
    check(llm.classify("Gemini", 429, "{}").retry_after is None,
          "a wait was invented when the provider named none")

    # A wait longer than the cap is clamped, and the hint has to say the wait
    # that will really happen: promising an hour and retrying after a minute
    # is worse than saying nothing.
    huge = llm.classify("Gemini", 429, json.dumps({"error": {
        "message": "Quota exceeded",
        "details": [{"retryDelay": "3600s"}]}}))
    check(huge.retry_after == llm.MAX_RETRY_WAIT,
          f"an hour-long wait was not clamped: {huge.retry_after}")
    check("3600" in huge.hint and "60s" in huge.hint,
          f"the hint hides the clamp: {huge.hint!r}")


def test_a_truncated_tool_call_is_not_a_complete_batch() -> None:
    """Claude returns a tool_use block even when max_tokens cut it short. Taking
    it at face value made a partial cross-audit print a normal count while the
    questions that fell off the end were never judged at all."""
    payload = {"stop_reason": "max_tokens",
               "content": [{"type": "tool_use", "input": {"items": [{"index": 0}]}}]}

    with _stub_claude(payload):
        try:
            llm.generate("x", schema={"type": "object"}, using="claude", retries=1)
            raise AssertionError("a truncated tool call was accepted as a full batch")
        except llm.LLMError as e:
            check(not e.retryable, "a truncated tool call was treated as transient")
            check("limit" in str(e).lower(),
                  f"the message does not name the cause: {e}")


def test_a_sampling_parameter_is_never_sent_to_a_model_that_refuses_it() -> None:
    """`temperature` is not ignored by the current Claude models, it is a 400.

    The transport sent one unconditionally, so every call it made failed -- and
    it failed invisibly, because nothing here has an Anthropic key and the
    default cross-audit path never goes through this code.
    """
    ok_payload = {"stop_reason": "tool_use",
                  "content": [{"type": "tool_use", "input": {"ok": True}}]}
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"):
        with _stub_claude(ok_payload) as sent:
            llm.generate("x", schema={"type": "object"}, model=model,
                         using="claude", temperature=0.3, retries=1)
        check("temperature" not in sent[0],
              f"{model} was sent a temperature it would have refused")
        check(sent[0]["max_tokens"] == llm.MAX_TOKENS_CLAUDE,
              "the output ceiling is not the one thinking was budgeted for")

    # A model that still takes it keeps taking it: the rule is about the
    # families that removed sampling, not about dropping the knob everywhere.
    with _stub_claude(ok_payload) as sent:
        llm.generate("x", schema={"type": "object"}, model="claude-sonnet-4-6",
                     using="claude", temperature=0.3, retries=1)
    check(sent[0].get("temperature") == 0.3,
          "a model that accepts temperature was not given one")


def test_a_thinking_level_is_clamped_to_what_each_vendor_takes() -> None:
    """`xhigh` and `max` are real efforts on Claude and do not exist on Gemini.

    The shared vocabulary used to stop at `high`, so asking for either was
    treated as a typo and silently dropped -- a setting that reported itself as
    set while doing nothing. Each transport clamps instead, so the level always
    means the closest thing the vendor actually has.
    """
    check("max" in llm.THINKING_LEVELS, "the tool cannot express Claude's top effort")
    check(llm.THINKING_BULK in llm.THINKING_LEVELS,
          f"the bulk default {llm.THINKING_BULK!r} is not a real level")
    check(llm._clamp_level("max", llm._CLAUDE_LEVELS) == "max", "Claude lost its ceiling")
    check(llm._clamp_level("max", llm._GEMINI_LEVELS) == "high",
          "an over-ceiling level was sent to Gemini rather than clamped")
    check(llm._clamp_level("minimal", llm._CLAUDE_LEVELS) == "low",
          "a level below Claude's floor was not raised to it")
    check(llm._clamp_level("", llm._GEMINI_LEVELS) == "",
          "a caller that asked for nothing got a level anyway")

    payload = {"stop_reason": "tool_use",
               "content": [{"type": "tool_use", "input": {}}]}
    with _stub_claude(payload) as sent:
        llm.generate("x", schema={"type": "object"}, model="claude-opus-5",
                     using="claude", thinking="max", retries=1)
    check(sent[0]["output_config"] == {"effort": "max"},
          f"effort reached the wire as {sent[0].get('output_config')!r}")


def test_no_chunk_leaves_the_chunker_over_the_prompt_budget() -> None:
    """The prompt was cut to fit at the point of sending, and the page overlap
    is one page -- far smaller than the span dropped -- so the tail was in no
    chunk at all. `--window 20` threw away about 15k characters per chunk after
    paying for the call in full."""
    pages = ["\n".join(f"line {i} of page {p} about discounted cash flow"
                       for i in range(400)) for p in range(20)]
    got = list(pdf_mod.chunks(pages, 20))
    check(got, "the chunker produced nothing at all")
    over = [loc for loc, text in got if len(text) > pdf_mod.PROMPT_BUDGET]
    check(not over, f"chunks over budget: {over}")
    joined = " ".join(text for _, text in got)
    check("page 19" in joined, "the tail of the window reached no chunk")
    check(len({loc for loc, _ in got}) == len(got), "two chunks share a locator")


def test_an_aborted_ingest_resumes_where_it_stopped() -> None:
    """The sources row existed after the first attempt, so a re-run printed
    "skip (already ingested)" and the chunks after the abort were never read.
    --force was the only way on, and it re-sent everything."""
    conn, a, _ = fresh()
    windows = [(f"p{i}", f"chunk {i} " + "x" * 200) for i in range(1, 7)]
    seen: list[str] = []

    def extractor(text):
        n = int(text.split()[1])
        seen.append(text)
        if n > 3:
            raise llm.LLMError("rate limited", retryable=True)
        return []

    out = pipeline.run(conn, a, windows, extractor=extractor,
                       on_progress=lambda *a: None)
    check(out.aborted, "the run did not stop on repeated failures")
    check(len(seen) == 6, f"read {len(seen)} chunks before giving up")
    check(not pipeline.is_complete(conn, a), "an aborted run was marked complete")

    seen.clear()
    out = pipeline.run(conn, a, windows, extractor=lambda t: (seen.append(t) or []),
                       on_progress=lambda *a: None)
    read = sorted(int(t.split()[1]) for t in seen)
    check(read == [4, 5, 6], f"the resume re-read chunks {read}")
    check(out.skipped == 3, f"reported {out.skipped} skipped, not 3")
    check(pipeline.is_complete(conn, a), "a finished run was not marked complete")

    pipeline.forget(conn, a)
    check(not pipeline.is_complete(conn, a), "--force did not clear the progress")


def test_reground_does_not_re_read_a_chunk_it_already_repaired() -> None:
    """The repair is entirely local, but finding out what to repair cost a
    full extraction call per chunk, unconditionally. So a second reground of
    the same book cost exactly as much as the first, and resuming an aborted
    one re-paid for every chunk that had already succeeded."""
    from . import reground as reground_mod
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE question_sources SET locator = ?, verbatim_text = NULL "
                 "WHERE question_id = ?", ("p1-6", qid))
    conn.commit()
    check(not reground_mod._already_repaired(conn, a, "p1-6"),
          "a chunk with no provenance was reported as already repaired")

    conn.execute("UPDATE question_sources SET verbatim_text = ? "
                 "WHERE question_id = ?", ("the page's own words", qid))
    conn.commit()
    check(not reground_mod._already_repaired(conn, a, "p1-6"),
          "a chunk with no phrasing was reported as already repaired")

    conn.execute("INSERT INTO phrasings (question_id, text, norm_key) VALUES (?, ?, ?)",
                 (qid, "the printed wording", normalize("the printed wording")))
    conn.commit()
    check(reground_mod._already_repaired(conn, a, "p1-6"),
          "a fully repaired chunk would have been paid for again")
    check(not reground_mod._already_repaired(conn, a, "p7-12"),
          "a chunk with no questions at all was skipped as done")


def test_vectors_from_two_models_are_never_scored_against_each_other() -> None:
    """zip stops at the shorter of the two and returns a plausible-looking
    number for vectors that cannot be compared at all."""
    try:
        search._dot([1.0, 2.0, 3.0], [1.0, 2.0])
        raise AssertionError("a 3-dim and a 2-dim vector were scored together")
    except ValueError:
        pass
    unit = search._unit([3.0, 4.0])
    check(abs(search._dot(unit, unit) - 1.0) < 1e-6,
          "a normalised vector is not unit length")


# ---------------------------------------------------------------- browse


def _stocked(conn, source_id) -> dict:
    """A handful of questions with known topics and tags to filter over."""
    made = {}
    for text, topic, tags in [
        ("What are the covenants on a term loan and when do they bite?",
         "lbo", ["covenants", "capital-structure"]),
        ("Why does a PE firm use leverage at all in a buyout structure?",
         "lbo", ["lbo-returns"]),
        ("Walk me through how deferred tax liabilities arise in an acquisition",
         "ma", ["dta-dtl"]),
    ]:
        v = admit(conn, source_id=source_id, question_text=text, answer_text="y" * 60)
        conn.execute("UPDATE questions SET topic = ?, status = 'active' WHERE id = ?",
                     (topic, v.matched_id))
        tagging.attach(conn, v.matched_id, tags)
        made[text[:12]] = v.matched_id
    conn.commit()
    tagging.ensure_tree(conn)
    return made


def test_stacking_two_values_of_one_kind_widens_the_set() -> None:
    """OR within a kind. AND would make the obvious gesture return nothing,
    which teaches you not to use the filter at all."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    lbo = browse.matching(conn, [("topic", "lbo")])
    both = browse.matching(conn, [("topic", "lbo"), ("topic", "ma")])
    check(len(lbo) == 2, f"topic:lbo gave {len(lbo)}")
    check(len(both) == 3, f"two topics gave {len(both)}, expected the union")


def test_a_filter_count_is_what_pressing_it_would_do() -> None:
    """The number beside a topic in the tree is the reason you press it, so it
    has to be what pressing it does. Topics OR within their kind, so marking
    one *widens* -- and the count against every other topic was its overlap
    with what was already marked, which for a column where a question has
    exactly one topic is zero. Mark `accounting` in the real bank and all nine
    remaining chapters read `0` while each is one keystroke from adding
    between 48 and 190 questions."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    at = [("topic", "lbo")]
    base = len(browse.ids(conn, at))
    counts = browse.topic_counts(conn, at)
    check(counts.get("lbo") == base,
          f"the marked topic lost its own contribution: {counts}")
    adds = len(browse.ids(conn, at + [("topic", "ma")])) - base
    check(adds == 1, "the fixture stopped having a second topic to add")
    check(counts.get("ma") == adds,
          f"topic:ma is offered as {counts.get('ma')} but would add {adds}")


def test_a_widening_filter_is_counted_by_what_it_brings_in() -> None:
    """The same rule with the groups OR-ed. `options()` has always counted
    these from outside the current set; `topic_counts` and `kind_counts` never
    took that path, so one screen answered the same question two ways
    depending on which column the row happened to be in."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    m = browse.Match.of(any_of=True)
    at = [("tag", "covenants")]
    base = len(browse.ids(conn, at, match=m))
    counts = browse.topic_counts(conn, at, match=m)
    adds = len(browse.ids(conn, at + [("topic", "ma")], match=m)) - base
    check(counts.get("ma") == adds,
          f"OR-ed, topic:ma reads {counts.get('ma')} but brings in {adds}")


def test_a_narrowing_filter_is_still_counted_inside_the_set() -> None:
    """The third branch, and the one that was always right: a kind that ANDs
    its own values can only narrow, so what survives inside the current set is
    both the honest number and the useful one. Flags are that kind."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    m = browse.DEFAULT
    check(m.all_within("flag"), "flags stopped AND-ing their own values")
    at = [("topic", "lbo")]
    scope, exclude = browse.counting_scope(at, "flag", m)
    check(scope == at and not exclude,
          "an AND-within kind stopped being counted inside the current set")


def test_stacking_across_kinds_narrows_the_set() -> None:
    conn, a, _ = fresh()
    _stocked(conn, a)
    got = browse.matching(conn, [("topic", "lbo"), ("tag", "covenants")])
    check(len(got) == 1, f"topic AND tag gave {len(got)}, expected the overlap")


def test_a_family_filter_matches_a_question_tagged_only_with_a_child() -> None:
    """The whole point of the tree. Without the subtree walk, filtering on
    `credit` would miss a question carrying nothing but `covenants`."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    got = browse.matching(conn, [("tag", "credit")])
    check(any("covenants" in tagging.tags_for(conn, r["id"]) for r in got),
          "filtering on the family missed a question tagged only with its child")


def test_tags_all_requires_every_tag_not_any() -> None:
    conn, a, _ = fresh()
    _stocked(conn, a)
    facets = [("tag", "covenants"), ("tag", "lbo-returns")]
    check(len(browse.matching(conn, facets)) == 2, "any-of should match both")
    check(len(browse.matching(conn, facets,
                              match=browse.Match.of(tags_all=True))) == 0,
          "all-of matched a question missing one of the tags")


def test_the_groups_can_be_or_ed_as_well_as_and_ed() -> None:
    """Narrowing is the default and assembling is the other half: `topic:lbo`
    OR `tag:covenants` is everything on either, which is how a study set gets
    put together rather than cut down."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    facets = [("topic", "lbo"), ("tag", "covenants")]
    both = {r["id"] for r in browse.matching(conn, facets)}
    either = {r["id"] for r in browse.matching(
        conn, facets, match=browse.Match(outer="any"))}
    lbo = {r["id"] for r in browse.matching(conn, [("topic", "lbo")])}
    tagged = {r["id"] for r in browse.matching(conn, [("tag", "covenants")])}
    check(both == lbo & tagged, "AND did not give the overlap")
    check(either == lbo | tagged, f"OR gave {either}, not the union {lbo | tagged}")
    check(len(either) > len(both), "OR-ing the filters did not widen the set")


def test_or_ing_the_filters_cannot_or_its_way_past_the_status_default() -> None:
    """A browse defaults to active so a drill cannot reach a rejected
    question. Status has to sit outside the expression: OR-ed in alongside the
    rest, any filter matching a rejected question would put it back on screen
    and one keystroke from a sitting."""
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    victim = made["What are the"]
    conn.execute("UPDATE questions SET status = 'rejected' WHERE id = ?", (victim,))
    conn.commit()
    got = browse.matching(conn, [("topic", "lbo"), ("tag", "covenants")],
                          match=browse.Match(outer="any"))
    check(victim not in {r["id"] for r in got},
          "a rejected question was OR-ed back into a browse")


def test_a_widening_filter_is_counted_by_what_it_would_add() -> None:
    """While the groups are AND-ed a count is what would survive; while they
    are OR-ed it has to be what would arrive, or the number next to a filter
    is the overlap and the reason to press the key is the opposite."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    facets = [("topic", "lbo")]
    inside = {r["name"]: r["n"] for r in browse.options(conn, facets)["topic"]}
    check("ma" not in inside or inside["ma"] == 0,
          "another topic counted anything inside a single-topic set")
    adds = {r["name"]: r["n"] for r in browse.options(
        conn, facets, match=browse.Match(outer="any"))["topic"]}
    check(adds.get("ma") == 1, f"OR counted topic:ma as {adds.get('ma')}, not what it adds")


def test_the_filter_line_says_what_the_query_does() -> None:
    """It used to write ` or ` between two topics and ` AND ` between the
    kinds whatever the SQL had been asked to do, which made it a caption
    rather than a description."""
    facets = [("topic", "lbo"), ("topic", "ma"), ("tag", "covenants")]
    line = browse.describe(facets)
    check("lbo or ma" in line, f"AND-ed two OR-ed topics: {line}")
    check("AND" in line, f"lost the joiner between the kinds: {line}")
    check("OR" in browse.describe(facets, browse.Match(outer="any")),
          "an OR-ed stack still described itself as AND")
    check("covenants AND" in browse.describe(
        [("tag", "covenants"), ("tag", "lbo-returns")], browse.Match.of(tags_all=True)),
        "all-of tags described themselves as either-of")


def test_browse_defaults_to_active_so_a_drill_cannot_reach_a_reject() -> None:
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    conn.execute("UPDATE questions SET status = 'rejected' WHERE id = ?",
                 (made["What are the"],))
    conn.commit()
    check(len(browse.matching(conn, [("topic", "lbo")])) == 1,
          "a rejected question survived the default status filter")
    everything = [("status", s) for s in ("active", "needs_review", "rejected")]
    check(len(browse.matching(conn, [("topic", "lbo")] + everything)) == 2,
          "--all did not bring the rejected one back")


def test_option_counts_are_within_the_current_set_not_the_bank() -> None:
    """A count that ignored the filters would tell you a filter is worth
    adding when it would leave the set unchanged."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    everywhere = {r["name"]: r["n"] for r in browse.options(conn, [])["topic"]}
    inside = {f["name"]: f["n"] for f in browse.options(conn, [("topic", "lbo")])["tag"]}
    check(everywhere.get("lbo") == 2, "topic count over the whole bank is wrong")
    check(inside.get("credit") == 1,
          f"tag count inside topic:lbo was {inside.get('credit')}, not filtered")


def test_an_empty_selection_drills_nothing_rather_than_everything() -> None:
    """`--ids` with no ids has to mean none. Falling back to the whole bank
    would turn a browse that matched nothing into a full random drill."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    check(scheduler.due_questions(conn, ids=[]) == [],
          "an empty id list drilled the whole bank")
    check(len(scheduler.due_questions(conn, ids=None)) == 3,
          "None should mean no restriction")


def test_a_mock_round_can_spread_over_tags_not_only_topics() -> None:
    """The product rounds name tags -- there is no `dcm` topic -- so a spread
    that only understood topics fell through to the random filler and
    rehearsed the wrong hour."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    got = mock.pick(conn, {"count": 1, "spread": ["covenants"]})
    check(len(got) == 1, "a tag slice picked nothing")
    check("covenants" in tagging.tags_for(conn, got[0]["id"]),
          "a tag slice picked a question that does not carry the tag")


def test_a_mock_slice_tolerates_the_wrong_separator() -> None:
    """Topics use underscores and tags use hyphens, so a round names a slice
    wrongly about half the time. It used to fail silently: the slice matched
    nothing, the round backfilled at random, and the mock rehearsed a
    different interview than the one on the label."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    check(len(mock.pick(conn, {"count": 1, "spread": ["lbo_returns"]})) == 1,
          "an underscored slice missed the hyphenated tag")


class _Keys:
    """Just enough of a key event for a view to route on."""
    def __init__(self, name: str, ch: str = "") -> None:
        self.name, self.ch = name, ch


class _FakeShell:
    def __init__(self) -> None:
        self.ran = self.filled = None

    def run_now(self, line: str) -> None:
        self.ran = line

    def prefill(self, line: str) -> None:
        self.filled = line


def test_browse_reaches_a_drill_with_no_modifier_key() -> None:
    """Alt- and ctrl- chords get eaten by the terminal emulator or the window
    manager before the process sees them, so a browser whose only route to
    "drill this" was a chord does not work on someone's machine. Arrows and
    Enter are the only keys nothing else can claim."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)                    # up into the tree
    check(view.mode == "tree", "left did not walk up a level")
    view.count()
    drill = [i for i, r in enumerate(view._tree_rows) if r.get("act") == "drill"]
    check(drill, "no drill row to select in the tree")
    view._tree_sel = drill[0]
    view.handle(_Keys("enter"), sh)
    check(sh.ran is None, f"one Enter already started a sitting: {sh.ran!r}")
    view.handle(_Keys("enter"), sh)
    check((sh.ran or "").startswith("drill --ids "),
          f"Enter twice on the drill row ran {sh.ran!r}")
    check(not any("⌥" in k or "ctrl" in k for k, _ in view.hints()),
          "the footer still advertises a modifier chord")


def test_walking_into_the_filters_cannot_start_a_drill() -> None:
    """`←` then `→` is the first thing anyone does in `browse`, and it used to
    land the cursor on `drill these 981` and then fire it: a sitting over the
    whole bank, started by two arrow keys, drawn behind the browse frame where
    you could not see the question you were being asked. Nothing that writes to
    the schedule may sit one keystroke from where the cursor comes to rest."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    check(view._tree_rows[view._tree_sel]["group"] != "do",
          "the tree opens with the cursor on an action")
    view.handle(_Keys("right"), sh)
    check(sh.ran is None, f"← → started {sh.ran!r}")

    # And on the action itself, → is not an activation either: it means "into
    # this one", and an action has no inside.
    view.count()
    view._tree_sel = [i for i, r in enumerate(view._tree_rows)
                      if r.get("act") == "drill"][0]
    view.handle(_Keys("right"), sh)
    check(sh.ran is None, f"→ on the drill row started {sh.ran!r}")


def _tabs_with_actions(extra: list = None) -> "views.TabsView":
    acts = extra if extra is not None else [
        views.Action("look", "look at #1", "show 1"),
        views.Action("drill", "drill dcf", "drill -t dcf",
                     arm="drill dcf? \u23ce starts the sitting"),
    ]
    tabs = [("One", lambda w: views.Pane(["  a line"], acts)),
            ("Two", lambda w: ["  another line"] * 40)]
    v = views.TabsView("T", tabs)
    v.viewport = 20
    return v


def test_a_dashboard_recommendation_is_pressable() -> None:
    """The dashboard worked out that `drill -t dcf` was the best use of the
    next hour and then printed those nine characters in grey for you to
    retype. It was the last screen in the tool you could not act from."""
    sh, view = _FakeShell(), _tabs_with_actions()
    view.render(90)
    check(view.actions(90), "the pane handed back no actions")

    # Read-only runs on the first press; anything that writes to the schedule
    # costs a second, identical one, exactly as it does in every other list.
    view.handle(_Keys("enter"), sh)
    check(sh.ran == "show 1", f"a read-only action ran {sh.ran!r}")
    sh.ran = None
    view.handle(_Keys("down"), sh)
    view.handle(_Keys("enter"), sh)
    check(sh.ran is None and view._armed == "drill",
          f"one Enter on a drill row started {sh.ran!r}")
    view.handle(_Keys("enter"), sh)
    check(sh.ran == "drill -t dcf", f"the second Enter ran {sh.ran!r}")


def test_a_long_action_label_is_cut_and_not_wrapped() -> None:
    """An action's label is written by whoever offers the action, and it was
    the one row in these lists nothing measured. A long one wrapped in the
    terminal instead of being cut, and the overflow landed on the input box's
    own border and stayed there -- the frame differ only repaints rows the
    view admits to owning."""
    long = "look at the one you keep failing, #14  " + "why does the bridge " * 6
    view = _tabs_with_actions([views.Action("k", long, "show 14")])
    for w in (60, 82, 110):
        wide = [l for l in view.render(w) if ui.vlen(l) > w]
        check(not wide, f"at width {w} a row came back {ui.vlen(wide[0])} wide"
              if wide else "")

    # Same painter, so the same clamp has to reach the lists that had it first.
    row = views.action_row(long, True, width=40)
    check(ui.vlen(row) <= 40, f"action_row ignored its width: {ui.vlen(row)}")


def test_tabbing_away_disarms_and_never_runs() -> None:
    """`←` `→` are the tabs on this screen, so they are also the keys nearest
    an armed row. Neither may fire it, and the one that backs out is spent
    backing out rather than also changing tab."""
    sh, view = _FakeShell(), _tabs_with_actions()
    view.render(90)
    view.handle(_Keys("down"), sh)
    view.handle(_Keys("enter"), sh)
    check(view._armed == "drill", "Enter did not arm the row")
    view.handle(_Keys("left"), sh)
    check(sh.ran is None, f"backing out of an armed row ran {sh.ran!r}")
    check(view._armed is None, "the row stayed armed")
    check(view.idx == 0, "the key that disarmed also changed tab")

    # And an armed row left behind by a real tab change is not still armed
    # when you come back to it.
    view.handle(_Keys("enter"), sh)
    view.handle(_Keys("right"), sh)
    view.handle(_Keys("left"), sh)
    check(view._armed is None and view.sel == 0,
          "a tab still held the cursor and the arming of the one before it")


def test_a_view_that_ran_a_command_survives_coming_back() -> None:
    """`run_now` parks the view and calls `on_resume` on the way back in, but
    only `ResultsView` had one -- so a drill started from the `tags` list took
    the whole shell down on its way home. The hook belongs on the base."""
    check(hasattr(tui.View, "on_resume"),
          "the base View has no on_resume, so run_now crashes on anything else")
    conn, a, _ = fresh()
    _stocked(conn, a)
    for view in (views.TagsView(conn, tagging.all_tags(conn)),
                 views.SessionsView(conn, [], None),
                 _tabs_with_actions()):
        view.on_resume()          # must not raise


def test_a_dashboard_pane_is_re_read_after_its_own_action() -> None:
    """A pane is a closure so that it can be called again. The dashboard used
    to close over counts worked out before the sitting, so a drill launched
    from `DO THIS NEXT` came back quoting the numbers that sent you into it."""
    calls = []

    def build(w):
        calls.append(w)
        return views.Pane([f"  built {len(calls)}"], [])

    view = views.TabsView("T", [("One", build)])
    view.viewport = 20
    view.render(90)
    view.render(90)
    check(len(calls) == 1, f"a cached pane was rebuilt {len(calls)} times")
    view.on_resume()
    view.render(90)
    check(len(calls) == 2, "the pane was not re-read after the command it ran")


def test_an_armed_action_backs_out_of_its_own_accord() -> None:
    """A row waiting for its second Enter has to be escapable by the key that
    means "back" -- and that key is spent doing so, or arming a drill would
    also throw you out of the tree."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    view._tree_sel = [i for i, r in enumerate(view._tree_rows)
                      if r.get("act") == "mock"][0]
    view.handle(_Keys("enter"), sh)
    check(view._armed == "mock", "Enter did not arm the row")
    view.handle(_Keys("left"), sh)
    check(view._armed is None and view.mode == "tree",
          "left did not disarm in place")
    view.handle(_Keys("enter"), sh)
    view.handle(_Keys("down"), sh)
    check(view._armed is None, "moving off the row left it armed")
    view.handle(_Keys("enter"), sh)
    check(sh.ran is None, f"a stale arming ran {sh.ran!r}")


def test_left_collapses_a_row_before_it_walks_up_a_level() -> None:
    """Left already means "close this" everywhere else in the shell, so it has
    to finish closing before it starts navigating, or an open answer becomes
    impossible to shut without leaving the screen."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.sel = 0
    view.handle(_Keys("right"), None)
    check(0 in view.expanded, "right did not open the row")
    view.handle(_Keys("left"), None)
    check(0 not in view.expanded and view.mode == "results",
          "left navigated away instead of collapsing the open row")
    view.handle(_Keys("left"), None)
    check(view.mode == "tree", "left did not walk up once nothing was open")


def test_every_list_reaches_its_commands_without_a_chord() -> None:
    """`tags` could only drill through `⌥d` and `sessions` could only resume
    through `⌥r`. Alt- and ctrl- chords get eaten by the terminal emulator or
    the window manager before the process sees them, so those two lists did
    not work at all on a machine that swallows them. `←` is the key `browse`
    already spends on "up a level", and up a level from a row is what you can
    do to it."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    rows = tagging.all_tags(conn, min_count=1)
    sh, view = _FakeShell(), views.TagsView(conn, rows)
    view.sel = 0
    view.handle(_Keys("left"), sh)
    check(view._doing is not None, "← opened nothing")
    check(view.action_subject(0).startswith("#"), "the do-screen did not name the tag")

    labels = [a.key for a in view._doing.acts]
    check("drill" in labels and "browse" in labels, f"actions were {labels}")
    view._doing.sel = labels.index("drill")
    view.handle(_Keys("enter"), sh)
    check(sh.ran is None, f"one press already started a sitting: {sh.ran!r}")
    check(view._doing is not None, "the do-screen closed before it ran anything")
    view.handle(_Keys("enter"), sh)
    check((sh.ran or "").startswith("drill --tag "), f"⏎⏎ ran {sh.ran!r}")
    check(view._doing is None, "the do-screen stayed up over the command it started")
    check(not any("⌥" in k for k, _ in view.hints()),
          "the footer still advertises a chord as the way in")


def test_a_do_screen_backs_out_the_way_it_came_in() -> None:
    """`←` opened it, so `←` closes it - and while a row is armed that same
    key is spent disarming, or arming a drill would also throw you out."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.TagsView(conn, tagging.all_tags(conn, min_count=1))
    view.handle(_Keys("left"), sh)
    view.handle(_Keys("enter"), sh)          # arm the drill
    check(view._doing.armed, "Enter did not arm the row")
    view.handle(_Keys("left"), sh)
    check(view._doing is not None and not view._doing.armed,
          "← left the screen instead of disarming in place")
    view.handle(_Keys("left"), sh)
    check(view._doing is None, "← did not close the do-screen")
    check(sh.ran is None, f"backing out ran {sh.ran!r}")


def test_an_action_is_not_offered_when_there_is_nothing_behind_it() -> None:
    """A sitting's questions are mostly not due the moment it ends -- that is
    what rating one "again" does to its schedule -- so "drill these again"
    would answer `0 queued, 12 held back` and read as broken."""
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    qid = next(iter(made.values()))
    sid = session.open_session(conn, "drill", [qid], {"count": 1})
    session.record(conn, sid, qid, 1, 10)
    scheduler.record_review(conn, qid, 1)
    conn.commit()
    rows = session.recent(conn, 5)
    view = views.SessionsView(conn, rows, None)
    keys = [act.key for act in view.actions(0)]
    check("again" not in keys and "fluffed" not in keys,
          f"offered a drill with nothing due behind it: {keys}")

    conn.execute("UPDATE schedule SET due_at = datetime('now','-1 day') "
                 "WHERE question_id = ?", (qid,))
    conn.commit()
    keys = [act.key for act in views.SessionsView(conn, rows, None).actions(0)]
    check("fluffed" in keys, f"a due question you rated 1 was not offered: {keys}")


def test_the_open_sitting_is_the_only_one_that_offers_to_resume() -> None:
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    qid = next(iter(made.values()))
    sid = session.open_session(conn, "drill", [qid], {"count": 2})
    conn.commit()
    rows = session.recent(conn, 5)
    live = views.SessionsView(conn, rows, sid)
    check("resume" in [act.key for act in live.actions(0)],
          "the sitting you walked away from would not offer to resume")
    dead = views.SessionsView(conn, rows, None)
    check("resume" not in [act.key for act in dead.actions(0)],
          "a finished sitting offered to resume")


def test_one_definition_of_an_action_row_across_every_list() -> None:
    """`browse` grew arming first and the other lists came later. Two
    implementations of "press it twice" is two implementations that will
    disagree, and the one that gets it wrong starts a sitting by accident."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    rows = [r for r in view._tree_rows if r.get("act") == "drill"]
    check(rows and isinstance(rows[0].get("action"), views.Action),
          "browse's do-rows are not the shared Action")
    check(rows[0]["action"].arm, "the drill row carries no confirmation")


def test_browse_opens_at_the_top_of_its_own_list() -> None:
    """The list is built in one order and shown in another, and the re-sort
    that gets it there holds the cursor on the row it was on -- which is what
    ⇥ wants and the opposite of what opening the screen wants. `browse` came
    up at row 117 of 1086, with 116 questions scrolled off above the cursor."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    check(view.sel == 0 and view.top == 0,
          f"browse opened at row {view.sel + 1}, not the top")


def test_a_filter_can_be_marked_without_leaving_the_tree() -> None:
    """Stacking filters is the normal case. `space` marks a leaf and stays,
    on a branch the same as on a leaf -- it is `→` and `⏎` that differ by
    row now, since a leaf has nowhere further down in the tree for them to
    open. Neither marks anything -- that is `space`, and only `space`."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    tech = next(i for i, r in enumerate(view._tree_rows)
                if r.get("facet") == ("kind", "technical"))
    view._tree_sel = tech
    view.handle(_Keys("right"), sh)              # open Technicals
    view.count()
    lbo = next(i for i, r in enumerate(view._tree_rows)
               if r.get("facet") == ("topic", "lbo"))
    view._tree_sel = lbo
    view.handle(_Keys("right"), sh)              # open lbo -> its tag families
    view.count()
    fam = lbo + 1
    check(view._tree_rows[fam]["has_children"],
          "lbo opened with no tag families underneath it")
    view._tree_sel = fam
    view.handle(_Keys("right"), sh)              # open the family -> its tags
    view.count()
    leaf = fam + 1
    check(not view._tree_rows[leaf]["has_children"],
          "the family opened with no leaf tags underneath it")
    kind, name = view._tree_rows[leaf]["facet"]
    view._tree_sel = leaf

    view.handle(_Keys("right"), sh)
    check((kind, name) not in view.facets,
          "→ on a leaf marked it -- that is what space is for")
    check(view._preview_focus, "→ on a leaf did not hand the cursor to its preview")
    view.handle(_Keys("left"), sh)                # back out of the preview
    check(not view._preview_focus, "← did not hand the cursor back to the tree")
    check(view._tree_sel == leaf, "← moved the tree cursor off the leaf")

    view.handle(_Keys("char", " "), sh)
    check((kind, name) in view.facets, "space on a leaf did not mark it")
    check(view.mode == "tree", "space threw us into the results list")

    view.count()
    behav = next(i for i, r in enumerate(view._tree_rows)
                 if r.get("facet") == ("kind", "behavioural"))
    behav_facet = view._tree_rows[behav]["facet"]
    behav_path = view._tree_rows[behav]["path"]
    view._tree_sel = behav
    view.handle(_Keys("enter"), sh)
    check(behav_facet not in view.facets,
          "⏎ marked the branch -- that is what space is for")
    check(behav_path in view.open_nodes, "⏎ did not open the branch")
    check(view.mode == "tree", "⏎ on a branch threw us into the results list")


def test_a_preview_row_drops_open_in_place_rather_than_opening_show() -> None:
    """⏎ inside the preview pane used to `shell.run_now(f"show {id}")`,
    swapping the whole tree out for a separate screen just to read what one
    question says. It drops a rubric card open under the row instead --
    `show` stays a screen you reach on purpose, not one browsing lands you
    on by accident."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.open_nodes.add((("kind", "technical"), ("topic", "lbo")))
    view.count()
    leaf = next(i for i, r in enumerate(view._tree_rows)
                if r.get("act") == "node" and r["depth"] == 2 and not r["has_children"]
                and r["path"][:2] == (("kind", "technical"), ("topic", "lbo")))
    view._tree_sel = leaf
    view.handle(_Keys("right"), sh)
    check(view._preview_focus, "→ on a leaf did not focus the preview")

    before = ui.strip("\n".join(view.render(80)))
    check("yyyy" not in before, "the answer was on screen before the row was expanded")
    view.handle(_Keys("enter"), sh)
    check(0 in view._preview_expanded, "⏎ did not drop the row open")
    check(sh.ran is None, "⏎ in the preview launched a command instead of expanding in place")
    after = ui.strip("\n".join(view.render(80)))
    check("yyyy" in after, "expanding the row did not reveal its answer")

    view.handle(_Keys("enter"), sh)
    check(0 not in view._preview_expanded, "a second ⏎ did not close the dropdown")


def test_marking_a_leaf_follows_the_cursor_to_its_new_row() -> None:
    """Two things move a leaf's index the moment it is marked: `options()`
    used to drop a chosen tag from its family's list outright (fixed so a
    marked leaf stays put with its own checkmark instead), and marking the
    very first filter inserts `clear every filter` above the tree, shifting
    every row below it down by one. The cursor has to follow the row it was
    on to its new index rather than land on whatever slid into the old one."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    tech = next(i for i, r in enumerate(view._tree_rows)
                if r.get("facet") == ("kind", "technical"))
    view._tree_sel = tech
    view.handle(_Keys("right"), sh)              # open Technicals
    view.count()
    lbo = next(i for i, r in enumerate(view._tree_rows)
               if r.get("facet") == ("topic", "lbo"))
    view._tree_sel = lbo
    view.handle(_Keys("right"), sh)              # open lbo -> its tag families
    view.count()
    fam = lbo + 1
    view._tree_sel = fam
    view.handle(_Keys("right"), sh)              # open the family -> its tags
    view.count()
    leaf = fam + 1
    facet = view._tree_rows[leaf]["facet"]
    view._tree_sel = leaf
    view.handle(_Keys("char", " "), sh)           # mark it -- inserts `clear`
    view.count()
    check(view._tree_sel != leaf,
          "marking the first filter should have shifted every row below it")
    check(view._tree_rows[view._tree_sel]["facet"] == facet,
          "the cursor did not follow the marked leaf to its new position")
    check(view._tree_rows[view._tree_sel]["selected"],
          "the marked row lost its checkmark")


def test_the_within_toggle_holds_its_place_when_the_set_empties() -> None:
    """The pinned rows above the tree reshape with the set -- the do-rows
    disappear once nothing matches, and the joiner rows come and go with the
    number of kinds. The cursor still has to hold the row it was on rather
    than the number it happened to have."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh = _FakeShell()
    view = views.BrowseView(conn, [("tag", "covenants"), ("tag", "lbo-returns")])
    view.handle(_Keys("left"), sh)
    view.count()
    row = [i for i, r in enumerate(view._tree_rows) if r.get("act") == "within"]
    check(row, "no within-kind toggle offered for two tags")
    view._tree_sel = row[0]
    view.handle(_Keys("enter"), sh)       # tags: any -> all, which empties it
    view.count()
    check(len(view.rows) == 0, "the fixture was supposed to have no overlap")
    check(view._tree_rows[view._tree_sel].get("act") == "within",
          "the cursor slid off the switch it had just thrown")
    view.handle(_Keys("enter"), sh)       # and back
    view.count()
    check(len(view.rows) == 2 and view._tree_rows[view._tree_sel].get("act") == "within",
          "the switch could not be thrown back from where it left the cursor")


def test_a_joiner_is_only_offered_when_it_could_change_something() -> None:
    """A switch that cannot move the set is a row that teaches you the screen
    is decorative."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    one = views.BrowseView(conn, [("topic", "lbo")])
    one.handle(_Keys("left"), None)
    one.count()
    check(not [r for r in one._tree_rows if r.get("act") in ("join", "within")],
          "offered a joiner for a single filter")

    two = views.BrowseView(conn, [("topic", "lbo"), ("tag", "covenants")])
    two.handle(_Keys("left"), None)
    two.count()
    acts = [r.get("act") for r in two._tree_rows]
    check("join" in acts, "two kinds did not offer AND/OR")
    check("within" not in acts, "offered an all-of switch for a single tag")


def test_an_open_question_does_not_unfold_under_the_tree() -> None:
    """`expanded` holds row *positions*, and the two screens do not share a
    numbering. Carried across, position 4 stopped meaning "the fifth question"
    and started meaning "the fifth chapter", so an answer unfolded underneath
    a tag name."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.sel = 0
    view.handle(_Keys("right"), sh)
    check(view.expanded == {0}, "the question did not open")
    view.handle(_Keys("left"), sh)      # collapse
    view.handle(_Keys("left"), sh)      # up into the tree
    check(view.mode == "tree", "did not reach the tree")

    view.mode = "results"
    view.expanded = {0}
    view._to_tree()
    check(not view.expanded, "an expansion followed the cursor into the tree")
    view._to_results()
    check(view.expanded == {0}, "the expansion did not come back with the list")


def test_re_sorting_keeps_the_same_rows_open() -> None:
    """Expansion is tracked by position and a re-sort moves every position, so
    ⇥ used to close the row you had open and unfold two unrelated ones."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.sel = 0
    view.expanded = {0}
    open_id = view.rows[0]["id"]
    view.sort = "id"
    view._reorder()
    check(len(view.expanded) == 1, f"{len(view.expanded)} rows open after a sort")
    check(view.rows[next(iter(view.expanded))]["id"] == open_id,
          "the sort left a different question open")


def test_a_click_selects_a_tree_row_and_acts_on_the_click_that_follows() -> None:
    """A branch and a leaf need different second clicks -- open, or mark --
    so a tree row cannot apply on the first click the way a flat filter row
    used to. Select, then the identical click again, is the same two-step
    every `do` row already takes."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    target = next(i for i, r in enumerate(view._tree_rows)
                  if r.get("facet") == ("kind", "technical"))
    view._tree_sel = 0                    # away from `target`, so the first
                                           # click below is a plain select
    item = views.BrowseView._TREE_BASE + target
    view.click_at(item, 0, sh)
    check(view._tree_sel == target and not view._tree_rows[target]["open"],
          "one click already opened the branch")
    view.click_at(item, 0, sh)
    view.count()
    check(view._tree_rows[target]["open"], "the second click did not open it")

    drill = next(i for i, r in enumerate(view._tree_rows) if r.get("act") == "drill")
    d_item = views.BrowseView._TREE_BASE + drill
    view.click_at(d_item, 0, sh)
    view.click_at(d_item, 0, sh)
    check(sh.ran is None, f"two clicks on a drill row started {sh.ran!r}")
    view.click_at(d_item, 0, sh)
    check((sh.ran or "").startswith("drill --ids "),
          f"select, arm, run should reach the drill; got {sh.ran!r}")


def test_a_chip_is_removed_by_clicking_it() -> None:
    """`space` marks and unmarks a row from inside the tree; the strip above
    it is the mouse's route to the same drop -- click a chip, it is gone."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [("topic", "lbo"), ("tag", "covenants")])
    view.mode = "tree"
    view.render(100)
    check(len(view._chip_spans) == 2, f"{len(view._chip_spans)} chips recorded a span")
    start, end, i = view._chip_spans[1]
    view.click_at(views.BrowseView._CHIPS_ITEM, (start + end) // 2, None)
    check(("tag", "covenants") not in view.facets, "clicking the chip did not drop it")
    check(("topic", "lbo") in view.facets, "clicking one chip dropped the other")


def test_a_tab_is_clicked_by_the_column_it_sits_in() -> None:
    """Five tabs share one line, so the row a click lands on cannot say which
    tab was meant. Before this the tab bar was not clickable at all."""
    view = views.TabsView("BANK", [("Composition", lambda w: ["a"]),
                                   ("Sources", lambda w: ["b"]),
                                   ("Weakest", lambda w: ["c"])])
    view.render(100)
    check(len(view._tab_spans) == 3, "the tab bar did not record its columns")
    start, end, _ = view._tab_spans[2]
    view.click_at(views.TabsView.TAB_BAR, (start + end) // 2, None)
    check(view.idx == 2, f"clicking the third tab opened tab {view.idx}")
    check(any(o == views.TabsView.TAB_BAR for o in view.owner),
          "no line on the tab screen is clickable")


def test_a_page_key_belongs_to_the_list_that_is_up() -> None:
    """PgUp/PgDn scrolled the transcript behind an open list, which left
    walking a thousand rows one ↓ at a time as the only way through."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.viewport = 20
    shell = tui.Shell(on_submit=lambda s, l: None)
    shell.attach(view)
    check(not shell._dispatch_nav(tui.Key("pgdn")),
          "the shell kept the page key for the transcript")
    check(view.handle(tui.Key("pgdn"), shell), "the list ignored the page key")
    check(view.sel > 1, f"a page down moved the cursor to {view.sel}")


def test_no_view_can_draw_a_line_wider_than_the_frame() -> None:
    """`show` hands the whole question in as the card's subject and the longest
    one in the bank is 539 characters. One line a cell too long wraps in the
    terminal, which pushes the input box off the bottom and tears the screen -
    two panels away from whatever produced the line."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    long_text = "Walk me through " + "a very long question " * 30
    card = views.TabsView("#1", [("Answer", lambda w: ["fine"])], subject=long_text)
    card.viewport = 20
    for line in card.render(90):
        check(ui.vlen(line) <= 90, f"tabs view drew {ui.vlen(line)} cells into 90")

    view = views.BrowseView(conn, [])
    view.viewport = 20
    view.subject = long_text
    for line in view.render(90):
        check(ui.vlen(line) <= 90, f"picker drew {ui.vlen(line)} cells into 90")
    view.handle(_Keys("left"), None)
    for line in view.render(90):
        check(ui.vlen(line) <= 90, f"filter screen drew {ui.vlen(line)} cells into 90")


def test_a_wheel_notch_is_one_row() -> None:
    """Three rows a notch was chosen for a notched wheel and is wrong for
    everything else: a trackpad sends a burst of reports per swipe, so three
    rows each threw the cursor most of a screen for a gesture that meant
    "down a bit"."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.scroll_by(tui.Shell.WHEEL)
    check(view.sel == 1, f"one notch moved the cursor {view.sel} rows")


def test_the_pointer_lights_the_row_it_is_over() -> None:
    """Hover is a surface behind the row, not a second cursor bar: the bar
    says what a keystroke would act on, the surface says what a click would,
    and they are different claims about different rows."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.viewport = 20
    check(view.hover_at(2, 0), "the first hover reported nothing to redraw")
    check(not view.hover_at(2, 0),
          "a move onto the row already lit asked for a repaint anyway")
    ui.reset_depth()
    prev, ui._DEPTH = ui._DEPTH, 2
    try:
        lines = view.render(100)
        washed = [l for l in lines if ui.colour("hover", bg=True) in l]
        check(len(washed) == 1, f"{len(washed)} rows carry the hover surface")
        check("▎" not in ui.strip(washed[0])[:2],
              "the hovered row grew a cursor bar as well")
    finally:
        ui._DEPTH = prev


def test_a_hover_off_the_rows_puts_the_light_out() -> None:
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.hover_at(1, 0)
    check(view.hover_at(None, 0), "leaving the rows changed nothing")
    check(view.hover is None, "the light stayed on a row the pointer had left")


def test_hovering_a_tab_lights_the_tab_not_the_line() -> None:
    view = views.TabsView("BANK", [("Composition", lambda w: ["a"]),
                                   ("Sources", lambda w: ["b"])])
    view.render(100)
    start, end, _ = view._tab_spans[1]
    check(view.hover_at(views.TabsView.TAB_BAR, (start + end) // 2),
          "hovering the second tab reported nothing")
    check(view.hover_tab == 1, f"lit tab {view.hover_tab} instead of the one under the pointer")
    check(not view.hover_at(views.TabsView.TAB_BAR, (start + end) // 2),
          "a move inside the same tab asked for a repaint")
    check(view.hover_at(views.TabsView.TAB_BAR, 0), "moving off the tabs changed nothing")
    check(view.hover_tab is None, "a gap between tabs stayed lit")


def test_the_wheel_hands_back_at_the_end_of_a_list() -> None:
    """One gesture runs down the list and then keeps going into the transcript
    behind it. A view that claimed the wheel forever left you scrolling a list
    that could not move, which reads as a list with no more rows."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    check(not view.scroll_by(-3), "the list claimed a wheel-up at its own top")
    check(view.scroll_by(3), "the list ignored a wheel-down it could act on")
    view.sel = view.count() - 1
    check(not view.scroll_by(3), "the list claimed a wheel-down at its own bottom")


def test_the_wheel_stays_the_transcript_s_once_it_has_scrolled_past_the_view() -> None:
    """`Shell._on_mouse` handed every wheel notch to the view first and the
    transcript only once the view refused it -- right for wheel-up, which is
    exactly how you reach the transcript above the view. Wrong for wheel-down
    once you are there: `self.scroll > 0` means the frame has climbed away
    from the view and it is not what the pointer is over any more, but a
    wheel-down notch still moved its cursor instead of walking the frame back
    down -- so getting back to a `browse` of the whole bank took as many
    notches as the list had rows, not the handful that scrolled away from it."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view.viewport = 20
    shell = tui.Shell(on_submit=lambda s, l: None)
    shell.attach(view)
    shell.scroll = 3
    sel_before = view.sel
    shell._on_mouse(tui.MouseEvent("wheel-down", 5, 5))
    check(shell.scroll == 2, f"wheel-down did not walk the frame back down: scroll={shell.scroll}")
    check(view.sel == sel_before,
          "wheel-down moved the list's cursor while the frame was scrolled above it")

    # Once the frame is back at the view, the wheel is the view's again.
    shell.scroll = 0
    shell._on_mouse(tui.MouseEvent("wheel-down", 5, 5))
    check(shell.scroll == 0, "wheel-down at the view scrolled the transcript instead")
    check(view.sel == sel_before + 1, "the wheel stopped reaching the view's cursor")


def test_a_list_re_reads_its_columns_when_it_comes_back() -> None:
    """A drill started from a row moves the due date shown in that row, and
    the list comes back after the sitting still showing what it read before."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    qid = view.rows[0]["id"]
    check(view.rows[0].get("due_at") is None, "fixture already had a schedule")
    scheduler.record_review(conn, qid, 3)
    view.on_resume()
    fresh_row = [r for r in view.rows if r["id"] == qid][0]
    check(fresh_row.get("due_at"), "the list came back with a stale due column")


def test_a_fresh_browse_opens_on_the_tree_inside_a_shell() -> None:
    """A fresh `browse` used to land on the flat, unsorted everything-list,
    with the actual structure -- Type, topic, tags -- one keystroke away
    behind `←`. Inside a shell it now opens on that structure directly; with
    no shell to page a tree through, or with filters already given up front
    (`browse --topic dcf`), it still lands on the flat/targeted list."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    check(views.BrowseView(conn, []).mode == "results",
          "a browse with no shell defaulted to the tree")
    check(views.BrowseView(conn, [("topic", "lbo")]).mode == "results",
          "filters given up front should still land on results, not the tree")
    tui.CURRENT = object()
    try:
        check(views.BrowseView(conn, []).mode == "tree",
              "a fresh browse inside a shell did not open on the tree")
    finally:
        tui.CURRENT = None


def test_the_tree_previews_a_node_without_marking_it() -> None:
    """Moving the cursor over a chapter shows its questions on the right
    without touching `facets` -- you should not have to mark it just to
    look, which was the whole complaint about the old flat list."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    tui.CURRENT = object()
    try:
        view = views.BrowseView(conn, [])
    finally:
        tui.CURRENT = None
    view.open_nodes.add((("kind", "technical"),))
    view.count()
    lbo = next(i for i, r in enumerate(view._tree_rows)
               if r.get("facet") == ("topic", "lbo"))
    view._tree_sel = lbo
    rows = view._preview_rows()
    check({r["topic"] for r in rows} == {"lbo"}, f"the preview mixed in other topics: {rows}")
    check(len(rows) == 2, f"lbo preview had {len(rows)} rows, expected 2")
    check(view.facets == [], "hovering a chapter marked it as a filter")


def test_marking_a_kind_disagreement_topic_carries_its_kind_with_it() -> None:
    """A topic like `markets` only shows up nested under Technicals because
    some technical-kind question was filed under it -- the row's own count is
    scoped to `kind:technical` (`_children` bakes that into the query). Space
    used to commit only the bare topic facet, so the marked set pulled in
    every kind sharing that topic, not just the handful the row counted:
    marking a preview of 1 landed a filter over 2."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    misfiled = admit(conn, source_id=a, question_text="Why did the ECB hike?",
                      answer_text="y" * 60).matched_id
    conn.execute("UPDATE questions SET topic = 'markets', status = 'active' "
                 "WHERE id = ?", (misfiled,))
    real = admit(conn, source_id=a,
                 question_text="Where is the 2s10s spread trading right now?",
                 answer_text="y" * 60).matched_id
    conn.execute("UPDATE questions SET topic = 'markets', kind = 'market_awareness', "
                 "status = 'active' WHERE id = ?", (real,))
    conn.commit()

    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.count()
    row = next(i for i, r in enumerate(view._tree_rows)
               if r.get("facet") == ("topic", "markets"))
    check(view._tree_rows[row]["n"] == 1,
          f"the row said {view._tree_rows[row]['n']}, expected the one misfiled question")

    view._tree_sel = row
    view.handle(_Keys("char", " "), sh)
    check(("topic", "markets") in view.facets, "space did not mark the topic")
    check(("kind", "technical") in view.facets,
          "marking did not carry the kind the preview count was scoped to")
    check(view.selected_ids() == [misfiled],
          f"marked set was {view.selected_ids()}, expected only the misfiled question")

    view.handle(_Keys("char", " "), sh)
    check(view.facets == [], f"unmarking left {view.facets} behind")


def test_a_leaf_tags_preview_does_not_widen_to_its_family() -> None:
    """The tree path down to a leaf tag carries its family too --
    ``(tag:credit, tag:covenants)`` -- and same-kind facets are OR-ed by
    default, so those two used to combine right back into the family's own
    superset: every leaf under `credit` previewed the same rows `credit`
    itself did, because the ancestor facet never stopped applying. Only the
    deepest tag in the path should narrow the preview."""
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    covenants_only = admit(conn, source_id=a,
                            question_text="What is a maintenance covenant, specifically?",
                            answer_text="y" * 60).matched_id
    conn.execute("UPDATE questions SET topic = 'lbo', status = 'active' WHERE id = ?",
                 (covenants_only,))
    tagging.attach(conn, covenants_only, ["covenants"])
    structure_only = admit(conn, source_id=a,
                            question_text="How is the capital structure of a buyout typically layered?",
                            answer_text="y" * 60).matched_id
    conn.execute("UPDATE questions SET topic = 'lbo', status = 'active' WHERE id = ?",
                 (structure_only,))
    tagging.attach(conn, structure_only, ["capital-structure"])
    conn.commit()
    tagging.ensure_tree(conn)

    tui.CURRENT = object()
    try:
        view = views.BrowseView(conn, [])
    finally:
        tui.CURRENT = None
    view.open_nodes.update({
        (("kind", "technical"),),
        (("kind", "technical"), ("topic", "lbo")),
        (("kind", "technical"), ("topic", "lbo"), ("tag", "credit")),
    })
    view.count()

    def preview_ids(facet: tuple[str, str]) -> set[int]:
        idx = next(i for i, r in enumerate(view._tree_rows) if r.get("facet") == facet)
        view._tree_sel = idx
        return {r["id"] for r in view._preview_rows()}

    covenants_ids = preview_ids(("tag", "covenants"))
    structure_ids = preview_ids(("tag", "capital-structure"))
    both_tagged = made["What are the"]
    check(covenants_ids == {both_tagged, covenants_only},
          f"covenants preview was {covenants_ids}")
    check(structure_ids == {both_tagged, structure_only},
          f"capital-structure preview was {structure_ids}")
    check(covenants_ids != structure_ids,
          "two different leaf tags previewed the same set -- the family "
          "ancestor in the path is drowning out the leaf")


def test_the_tree_shows_every_type_row_even_at_zero() -> None:
    """A fixed structure that drops a row the moment nothing is filed under
    it is not a fixed structure any more, it is just another filtered list
    with extra steps."""
    conn, a, _ = fresh()
    _stocked(conn, a)  # only ever touches lbo and ma, both kind=technical
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    top = [r for r in view._tree_rows if r.get("depth") == 0]
    check(len(top) == 3, f"the tree opened with {len(top)} Type rows, expected 3")
    behavioural = next(r for r in top if r["facet"] == ("kind", "behavioural"))
    check(behavioural["n"] == 0, "an empty kind should still show as a row, at zero")

    # And every one of them is a kind. `General` sat up here as a fourth row on
    # `topic:general`, which made the top level sum to more than the bank: a
    # general question is a technical question too, so it was counted in both
    # and opening both showed it twice.
    check(all(r["facet"][0] == "kind" for r in top),
          f"a top-level row that is not a kind: {[r['facet'] for r in top]}")
    view.open_nodes.add((("kind", "technical"),))
    view.count()
    under = [r["facet"] for r in view._tree_rows if r.get("depth") == 1]
    check(("topic", "general") in under,
          f"General is not a chapter of Technicals: {under}")


def test_entering_a_node_with_enter_does_not_mark_it() -> None:
    """Enter used to mark the row under the cursor on its way into the
    combined results -- pressing the key that reads "go into this chapter"
    silently committed it as a filter too, with no way to look inside
    without marking. Enter now only goes in, the same as `→`; marking is
    `space`'s job alone."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.count()
    ma = next(i for i, r in enumerate(view._tree_rows)
              if r.get("facet") == ("topic", "ma"))
    view._tree_sel = ma
    view.handle(_Keys("enter"), sh)
    check(("topic", "ma") not in view.facets, "Enter on a topic marked it")
    view.count()
    check(view._tree_rows[ma]["open"], "Enter on a topic did not open it")
    check(view.mode == "tree", "Enter on a topic left the tree")


def test_right_on_a_branch_opens_it_instead_of_marking_it() -> None:
    """`→` means "look inside" on a branch; on a leaf, which has no inside
    left in the tree, it hands the cursor to the leaf's own preview instead
    of marking it -- marking is `space`'s job either way."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    tech = next(i for i, r in enumerate(view._tree_rows)
                if r.get("facet") == ("kind", "technical"))
    view._tree_sel = tech
    view.handle(_Keys("right"), sh)
    check(("kind", "technical") not in view.facets,
          "→ on a branch marked it instead of opening it")
    view.count()
    check(view._tree_rows[tech]["open"], "→ did not open Technicals")
    check(sh.ran is None, "opening a branch started a command by itself")


def test_left_at_the_tree_root_goes_nowhere_further() -> None:
    """The old flat everything-list behind `←` is gone -- `open N as a list`
    on the pinned rows is the replacement, and the tree's own root has
    nowhere further up to go."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    check(view.mode == "tree", "left did not reach the tree")
    view.handle(_Keys("left"), sh)
    check(view.mode == "tree", "left at the root left the tree")

    view.count()
    lst = next(i for i, r in enumerate(view._tree_rows) if r.get("act") == "list")
    view._tree_sel = lst
    view.handle(_Keys("enter"), sh)
    check(view.mode == "results" and len(view.rows) == 3,
          "the pinned 'list' row did not show everything unfiltered")


def test_the_sort_reaches_the_pane_the_cursor_is_reading() -> None:
    """`⇧⇥` in the tree used to be a key with no visible effect.

    It re-sorted `self.rows`, while the pane beside the tree was built by its
    own query and drawn in bank order whatever the sort said -- and the tree
    then swallowed the key outright, on the grounds that sorting "would
    reorder rows the cursor is sitting on". The cursor sits on chapters, which
    the taxonomy orders; what the sort orders is the pane.
    """
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    hard = made["Walk me thro"]
    conn.execute("UPDATE questions SET difficulty = 5 WHERE id = ?", (hard,))
    conn.execute("UPDATE questions SET difficulty = 1 WHERE id != ?", (hard,))
    conn.commit()

    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view._to_tree()
    view.count()
    view._tree_sel = view._first_browse_row()

    view.sort = "id"
    view._resorted()
    by_id = [r["id"] for r in view._preview_rows()]
    check(by_id == sorted(by_id), f"bank order did not reach the pane: {by_id}")

    view.sort = "difficulty"
    view._resorted()
    hardest = [r["id"] for r in view._preview_rows()]
    check(hardest[0] == hard,
          f"hardest-first put #{hardest[0]} above #{hard} in the pane")

    # And the key itself gets through, rather than being swallowed.
    before = view.sort
    view.handle(_Keys("btab"), sh)
    check(view.sort != before, "the sort key was swallowed in tree mode")
    check(dict(views.SORTS)[view.sort] in ui.strip(view.tally),
          f"the header does not say which sort is on: {ui.strip(view.tally)!r}")


def test_a_pinned_row_never_names_a_question_that_is_not_on_screen() -> None:
    """The pinned group is about the whole set, and it carried one row that
    was not: `tag #N`, resolved through the *results-mode* cursor while the
    tree was up. On a fresh browse that named whichever question the sort had
    put first -- a question the pane was not showing and the cursor was
    nowhere near. Every row in the results list still carries its own correct
    `tag #N`, which is where a per-question action belongs."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    view = views.BrowseView(conn, [])
    view._to_tree()
    view.count()
    pinned = [r for r in view._tree_rows if r.get("group") == "do"]
    check(pinned, "the fixture lost the do group entirely")
    for r in pinned:
        label = ui.strip(r.get("label") or (r["action"].label if r.get("action") else ""))
        check("#" not in label,
              f"a pinned row names one question: {label!r}")

    # It is still reachable, one row down, on the row it is actually about.
    view._to_results()
    keys = {act.key for act in view.actions(0)}
    check("tag" in keys, "a results row lost its own tag action")


def test_the_tree_offers_no_flag_rows() -> None:
    """`due`, `unseen`, `weak` and the rest are questions about the schedule,
    and `browse` is the screen for what the bank *contains*. They cost five
    permanent rows above every chapter to answer something the dashboard and
    `drill` already answer. The command-line flag still works and still shows
    as a chip -- it is only the standing list that is gone."""
    conn, a, _ = fresh()
    _stocked(conn, a)          # three questions, none of them ever drilled
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    check(browse.options(conn, [])["flag"],
          "the fixture was supposed to have a flag worth offering")
    check(not [r for r in view._tree_rows if r.get("kind") == "flag"],
          "the tree still lists the flags")
    check(not any("unseen" in ui.strip(view._row_line(r, 60, False))
                  for r in view._tree_rows),
          "a flag is still drawn above the chapters")

    flagged = views.BrowseView(conn, [("flag", "unseen")])
    check(len(flagged.rows) == 3, "browse --flag unseen stopped filtering")
    check(any("flag:unseen" in ui.strip(l) for l in flagged.header(80)),
          "a flag set from the command line is not shown as a chip")


def test_an_opened_chapter_is_drawn_underneath_the_one_it_opened_from() -> None:
    """A tree row carries a `label` of its own, and `_row_line` tested for one
    before it tested for a node -- so every chapter came out through the
    action-row painter: no indent, no count, no checkmark, the same `▶` an
    action gets. Opening Technicals produced ten more rows that looked exactly
    like Technicals, which is a flat list with extra keystrokes."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.count()
    rows = view._tree_rows
    parent = next(r for r in rows if r.get("facet") == ("kind", "technical"))
    child = next(r for r in rows if r.get("facet") == ("topic", "lbo"))

    def drawn(r: dict) -> str:
        return ui.strip(view._row_line(r, 40, False))

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip())

    check(indent(drawn(child)) > indent(drawn(parent)),
          f"a chapter was not indented under its parent: {drawn(child)!r}")
    check(drawn(child).rstrip().endswith("2"),
          f"a chapter was drawn without its count: {drawn(child)!r}")
    view.add("topic", "lbo")
    view.count()
    marked = next(r for r in view._tree_rows if r.get("facet") == ("topic", "lbo"))
    check("✓" in drawn(marked), f"a marked chapter has no checkmark: {drawn(marked)!r}")


def test_a_chapter_does_not_nest_a_chapter_of_its_own_name() -> None:
    """`lbo` is both a topic slug and a tag-family key in `tagging.TREE` --
    two unrelated namespaces (what a question is about vs. what it is
    tagged) that happen to share a word. Before the fold, opening the LBO
    topic listed a family named `lbo` as its own chapter underneath it,
    which reads as the bank nesting a category inside itself rather than as
    the family it actually is. Folding splices a same-named family's own
    tags straight into the chapter instead of wrapping them in a chapter
    that just repeats its parent's name; a family with a name of its own
    (`credit`) is untouched."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.open_nodes.add((("kind", "technical"), ("topic", "lbo")))
    view.count()
    under_lbo = {r["facet"]: r for r in view._tree_rows
                if r.get("act") == "node"
                and r["path"][:2] == (("kind", "technical"), ("topic", "lbo"))
                and r["depth"] == 2}
    check(("tag", "lbo") not in under_lbo,
          "a family named the same as its topic is still nested as its own chapter")
    check(("tag", "lbo-returns") in under_lbo,
          "folding the family lost its own tag instead of promoting it")
    check(("tag", "credit") in under_lbo,
          "folding a same-named family took an unrelated family with it")


def test_a_chapter_is_coloured_by_how_deep_it_sits_under_its_kind() -> None:
    """Three hues at full strength -- a kind, then its first open level, then
    everything deeper -- rather than one hue fading out with depth. A dimmed
    row reads as unavailable, and every row here is one keystroke from a
    drill.

    It used to be keyed by facet type (kind/topic/tag) instead, which put
    Technicals' first level (a topic) in a different colour from Behavioral's
    first level (a tag, since Behavioral drops straight through to its tags
    with no topic in between) -- the first thing you open read as a different
    kind of row depending on which chapter it was under. Depth relative to the
    kind is what decides now, so both read gold."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    v = admit(conn, source_id=a, question_text="Why investment banking?",
              answer_text="y" * 60)
    conn.execute("UPDATE questions SET topic = 'behavioural', kind = 'behavioural', "
                 "status = 'active' WHERE id = ?", (v.matched_id,))
    tagging.attach(conn, v.matched_id, ["why-banking"])
    conn.commit()
    tagging.ensure_tree(conn)

    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.open_nodes.add((("kind", "technical"), ("topic", "lbo")))
    view.open_nodes.add((("kind", "behavioural"),))
    view.count()
    rows = {r["facet"]: r for r in view._tree_rows if r.get("act") == "node"}
    tag = next(f for f in rows if f[0] == "tag" and rows[f]["path"][0] == ("kind", "technical"))
    behavioural_tag = next(f for f in rows
                           if f[0] == "tag" and rows[f]["path"][0] == ("kind", "behavioural"))
    check(rows[("topic", "lbo")]["depth"] == 1 and rows[tag]["depth"] == 2,
          "the tree is not nested the way this test assumes")
    check(rows[behavioural_tag]["depth"] == 1,
          "Behavioral does not drop straight through to its tags the way this test assumes")

    # Forced to 24-bit, because at the eight-basics depth half the palette
    # resolves to the empty string and every `in` below would pass on nothing.
    was, term = ui.colors_enabled, os.environ.get("COLORTERM")
    ui.colors_enabled = lambda: True
    os.environ["COLORTERM"] = "truecolor"
    ui.reset_depth()
    try:
        def drawn(facet, chosen=False) -> str:
            return view._node_line(rows[facet], 40, chosen)

        seen = {f: c for f, c in (
            (("kind", "technical"), "text"), (("topic", "lbo"), "gold"),
            (tag, "mauve"), (behavioural_tag, "gold")) if ui.colour(c) in drawn(f)}
        check(len(seen) == 4,
              f"a row was not drawn in its depth's colour: {sorted(seen)}")
        check(ui.colour("mauve") not in drawn(behavioural_tag),
              "Behavioral's first level is still coloured as a tag rather than as a first level")
        # `muted` and `faint` were the old depth ladder's second and third
        # rungs. The count is still faint; the name of the row is not.
        check(ui.colour("muted") not in drawn(tag),
              "a tag row is still dimmed for sitting two levels down")
        check(ui.strip(drawn(tag)).split()[-2] not in ("", None)
              and ui.colour("faint") in drawn(tag),
              "the count stopped being the quiet part of the row")

        # Marked is the one thing that overrides it: that is about this row
        # rather than about what kind of row it is. The cursor needs no colour
        # of its own -- it has the bar, the bold and the count.
        check(ui.BOLD in drawn(("topic", "lbo"), True),
              "the cursor row is not bold")
        view.add("topic", "lbo")
        view.count()
        marked = next(r for r in view._tree_rows if r.get("facet") == ("topic", "lbo"))
        check(ui.colour("sky") in view._node_line(marked, 40, False),
              "a marked chapter is not drawn as marked")
    finally:
        ui.colors_enabled = was
        os.environ.pop("COLORTERM", None)
        if term is not None:
            os.environ["COLORTERM"] = term
        ui.reset_depth()


def test_a_tag_is_named_the_way_a_topic_is() -> None:
    """Tag rows were written `#capital-markets` to keep a family from reading
    as a chapter. It made the tree look like two naming schemes -- some rows
    hashed, some not, for a reason nothing on screen explained -- and it spent
    three cells of a 25-cell column. The colour says it instead."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.open_nodes.add((("kind", "technical"),))
    view.open_nodes.add((("kind", "technical"), ("topic", "lbo")))
    view.count()
    for r in view._tree_rows:
        if r.get("act") == "node":
            check(not r["label"].startswith("#"),
                  f"a row is still labelled with a hash: {r['label']!r}")


def test_the_tree_pane_keeps_its_height_whatever_is_in_it() -> None:
    """The frame is anchored to the bottom of the terminal, so a pane drawn to
    fit its contents is a pane that jumps: moving from a chapter with 190
    questions to one with 3 slid the filter list up under the cursor, mid-walk.
    The height belongs to the pane, not to whichever column is longest."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.viewport = 24
    heights = set()
    view.count()
    for i, r in enumerate(view._tree_rows):
        view._tree_sel = i
        heights.add(len(view.render(90)))
    check(len(heights) == 1,
          f"the tree pane was drawn at {sorted(heights)} rows on the same screen")


def test_tree_render_stays_inside_its_width() -> None:
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.viewport = 20
    for w in (60, 90, 160):
        lines = view.render(w)
        for line in lines:
            check(ui.vlen(line) <= w, f"tree view drew {ui.vlen(line)} cells into {w}")
        check(any("Technicals" in line for line in lines),
              f"no Type row showed up at width {w}")


def test_every_line_of_the_tree_view_claims_its_own_row() -> None:
    """`owner` is how a click and a hover resolve to a row: one entry per
    emitted line, or the mouse lands somewhere other than where it points."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.viewport = 30
    lines = view.render(100)
    check(len(view.owner) == len(lines),
          f"{len(lines)} lines carried {len(view.owner)} owner entries")
    view.count()
    for i, item in enumerate(view.owner):
        if item < views.BrowseView._TREE_BASE:
            continue
        idx = item - views.BrowseView._TREE_BASE
        check(0 <= idx < len(view._tree_rows),
              f"line {i} claims tree row {idx}, out of range")
        r = view._tree_rows[idx]
        if r.get("act") == "node":
            check(r["label"] in ui.strip(lines[i]),
                  f"the line claiming {r['label']!r} does not draw it")


def test_the_tree_scrolls_to_keep_the_selection_on_screen() -> None:
    """A short terminal has room for only a few rows once the banner is
    pinned above the transcript. The tree drew the first few and stopped, so
    moving the cursor past that moved a cursor bar off the bottom -- the
    preview on the right changed and nothing else did, which reads as the
    list having lost the selection."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.viewport = 10
    for _ in range(6):
        view.handle(_Keys("down"), sh)
        view.render(100)
        pos = next((i for i, (_, idx) in enumerate(view._left_lines(30))
                    if idx == view._tree_sel), None)
        check(pos is not None, "the cursor fell off the flattened tree")
        check(view._tree_top <= pos,
              f"row at position {pos} scrolled above the top ({view._tree_top})")
        check(pos - view._tree_top < view.viewport,
              f"row at position {pos} is far past the visible window "
              f"(top={view._tree_top})")
    check(any("above" in line or "below" in line
              for line in (ui.strip(x) for x in view.render(100))),
          "a tree with more rows than fit said nothing about the rest")


def test_a_click_in_a_scrolled_tree_lands_on_what_it_points_at() -> None:
    """The row a click arrives on is an offset into what is drawn, and once
    the tree scrolls that is no longer the row's own index."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.viewport = 10
    for _ in range(6):
        view.handle(_Keys("down"), sh)
    view.render(100)
    check(view._tree_top > 0, "six downs on a 10-row viewport did not scroll")
    lines = view.render(100)
    top_item = next(item for item in view.owner if item >= views.BrowseView._TREE_BASE)
    want = top_item - views.BrowseView._TREE_BASE
    view.click_at(top_item, 2, sh)
    check(view._tree_sel == want,
          f"clicking the first visible row selected row {view._tree_sel}, not {want}")


def test_the_wheel_walks_the_tree_view() -> None:
    """`scroll_by` moves the tree's own cursor, not `self.sel` -- a notch
    that moved an invisible number would report the gesture as consumed
    while nothing on screen changed."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    sh, view = _FakeShell(), views.BrowseView(conn, [])
    view.handle(_Keys("left"), sh)
    view.count()
    before = view._tree_sel
    check(view.scroll_by(1), "a notch of the wheel did not move the tree cursor")
    check(view._tree_sel == before + 1,
          f"one notch moved the cursor {view._tree_sel - before} rows")
    n = len(view._tree_rows)
    while view.scroll_by(1):
        pass
    check(view._tree_sel == n - 1,
          f"the wheel stopped at row {view._tree_sel}, short of the last row")
    check(not view.scroll_by(1),
          "the tree kept claiming the wheel with nowhere left to go")


def test_a_seeded_market_question_is_tagged_by_what_it_is_bound_to() -> None:
    """Whether one of these ended up tagged used to be an accident of its
    wording: `What is the 2s10s spread` matched the lexical rules and got
    `market-awareness`, `Where is EURSTR fixing?` matched nothing and got no
    tag at all -- so twelve of the euro panel were missing from the tag map
    and from every `tag:` filter. The seed row knows what they are."""
    conn, _, _ = fresh()
    market.seed(conn)
    tagged = {r["id"]: set(tagging.tags_for(conn, r["id"])) for r in conn.execute(
        "SELECT id FROM questions WHERE kind = 'market_awareness'")}
    check(tagged, "nothing was seeded")
    bare = [qid for qid, tags in tagged.items() if "market-awareness" not in tags]
    check(not bare, f"{len(bare)} market questions carry no market-awareness tag")
    kinds = {frozenset(t & {"rates", "fx"}) for t in tagged.values()}
    check(frozenset({"fx"}) in kinds and frozenset({"rates"}) in kinds,
          f"the rate/cross split did not come through: {kinds}")


def test_seeding_twice_backfills_rather_than_duplicating() -> None:
    """`seed` is the only thing that knows what these questions are, so it has
    to be able to fix one it created before it knew to tag it."""
    conn, _, _ = fresh()
    market.seed(conn)
    before = conn.execute(
        "SELECT COUNT(*) c FROM questions WHERE kind = 'market_awareness'").fetchone()["c"]
    qid = conn.execute(
        "SELECT id FROM questions WHERE kind = 'market_awareness' ORDER BY id").fetchone()["id"]
    tagging.detach(conn, qid, ["market-awareness", "rates", "fx"])
    conn.commit()
    check("market-awareness" not in tagging.tags_for(conn, qid), "the tag did not come off")

    market.seed(conn)
    after = conn.execute(
        "SELECT COUNT(*) c FROM questions WHERE kind = 'market_awareness'").fetchone()["c"]
    check(after == before, f"a second seed added {after - before} questions")
    check("market-awareness" in tagging.tags_for(conn, qid),
          "a second seed did not put the missing tag back")


def test_every_concept_the_taxonomy_knows_has_a_family() -> None:
    """A tag with no parent lands under "unfiled" in `browse`, which is the
    correct amount of pressure to file it and no pressure at all if nobody
    looks. Seventeen concept rules were added over time and none of them were
    ever filed -- including the four biggest tags in the EMEA pack -- so the
    tree quietly stopped being a map of the bank."""
    tree = {child for kids in tagging.TREE.values() for child in kids}
    stray = sorted(set(tagging.CONCEPTS) - tree)
    check(not stray, f"concept rules with no family: {stray}")


def test_a_family_never_lists_a_tag_twice() -> None:
    """Two parents is no parent: `_place` takes the first and the second is a
    silent no-op, so the tag is filed somewhere nobody chose."""
    seen: dict[str, str] = {}
    dupes = []
    for family, kids in tagging.TREE.items():
        for child in kids:
            if child in seen:
                dupes.append(f"{child}: {seen[child]} and {family}")
            seen[child] = family
    check(not dupes, f"tags filed twice: {dupes}")


def test_a_firm_tag_can_actually_be_created() -> None:
    """`FIRMS`, `suggest_firms`, `FIRM_FAMILY` and the `firms` row in `TREE`
    all existed and nothing called the middle one, so the family was seeded on
    every run and could never fill: the browse tree advertised a chapter with
    no way into it, and the bank held zero tags of kind `firm`.

    The patterns are anchored at both ends too. Two are spelled with a
    trailing space -- "gs ", "citi " -- which is the author saying "the whole
    word", and `p.strip()` threw that away: `\bciti` matches "citing"."""
    check(tagging.suggest_firms("We spoke to Citi and Barclays about the trade")
          == ["barclays", "citi"], "a named firm was not recognised")
    check(tagging.suggest_firms("citing the prospectus in several cities") == [],
          "an ordinary word was read as a bank")

    conn, a, _ = fresh()
    v = admit(conn, source_id=a,
              question_text="How did Citi structure the financing on that deal?",
              answer_text="Citi ran the books alongside Barclays." * 4)
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (v.matched_id,))
    conn.commit()
    tagging.autotag(conn, only_untagged=False)

    names = {r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN question_tags qt ON qt.tag_id = t.id "
        "WHERE qt.question_id = ? AND t.kind = 'firm'", (v.matched_id,))}
    check("citi" in names, f"autotag produced no firm tag: {names}")

    # And it lands in the family rather than under "unfiled": a tag the run
    # just invented has no parent until something files it.
    parent = conn.execute(
        "SELECT p.name FROM tags c JOIN tags p ON p.id = c.parent_id "
        "WHERE c.name = 'citi'").fetchone()
    check(parent and parent["name"] == tagging.FIRM_FAMILY,
          "a newly created firm tag was left unfiled")


def test_the_tree_seed_files_firm_tags_as_well_as_sectors() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Why do you want to do DCM at this bank in particular?")
    tagging.attach(conn, qid, ["some-desk"], kind="firm")
    tagging.attach(conn, qid, ["shipping"], kind="industry")
    tagging.ensure_tree(conn)
    check("some-desk" in tagging.descendants(conn, tagging.FIRM_FAMILY),
          "a firm tag was left unfiled")
    check("shipping" in tagging.descendants(conn, tagging.SECTOR_FAMILY),
          "a sector tag was left unfiled")


def test_upstream_is_a_corporate_finance_word_before_it_is_an_oil_one() -> None:
    """An upstream merger, an upstream guarantee, "everything downstream of the
    mandate". Three of the four questions in the bank carrying the bare word
    were M&A and credit questions, and each of them was being filed under a
    sector it has nothing to do with."""
    merger = ("Walk me through getting to 100% of a German listed target.",
              "A merger squeeze-out at 90% works where the target is merged "
              "upstream into the AG bidder, and the bidder cannot upstream the "
              "target's cash without a DPLTA.")
    check("oil-gas" not in tagging.suggest_industries(*merger),
          "an upstream merger was tagged as oil and gas")
    guarantee = ("Explain structural versus contractual subordination.",
                 "The remedy for structural subordination is upstream guarantees "
                 "from the operating companies plus security over their shares.")
    check("oil-gas" not in tagging.suggest_industries(*guarantee),
          "an upstream guarantee was tagged as oil and gas")

    # And the sector sense still lands, which is the half that matters.
    real = ("How would you value a downstream company like an oil refinery operator?",
            "Downstream companies are valued on refining margin and throughput.")
    check("oil-gas" in tagging.suggest_industries(*real),
          "a downstream refiner stopped being an oil and gas question")
    well = ("You are analyzing a new oil well with a 12-month IP rate of 1,000 boe/d.",
            "Decline curves are what drive the value here.")
    check("oil-gas" in tagging.suggest_industries(*well),
          "an oil well is not an oil and gas question")


def test_re_filing_a_tag_by_hand_survives_the_next_seed() -> None:
    conn, a, _ = fresh()
    _stocked(conn, a)
    check(tagging.set_parent(conn, "covenants", "valuation"), "re-filing failed")
    tagging.ensure_tree(conn)
    check("covenants" in tagging.descendants(conn, "valuation"),
          "the seed stomped a hand-filed parent")


def test_a_tag_cannot_be_made_its_own_ancestor() -> None:
    """A cycle turns the descendants CTE into a walk that never ends."""
    conn, a, _ = fresh()
    _stocked(conn, a)
    check(not tagging.set_parent(conn, "credit", "covenants"),
          "a cycle was accepted")
    check(tagging.descendants(conn, "credit") >= {"credit", "covenants"},
          "the tree lost its shape after the refused cycle")


def main() -> int:
    # The usage log is the second real file in this repo the tests could
    # damage, after ib.db. Several cases drive `llm._attempt` through its
    # retry loop with a stubbed transport, and every one of those failures is
    # a row -- so a plain `selftest` was appending invented rate-limit
    # refusals to the log `usage` then reports as fact.
    os.environ["IB_USAGE_LOG"] = str(Path(tempfile.mkdtemp()) / "usage.jsonl")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  pass  {t.__name__}")
        except AssertionError as e:
            failed.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n  {len(tests) - len(failed)}/{len(tests)} tests, {_checks} checks")
    if failed:
        print(f"  {len(failed)} FAILED")
    return 1 if failed else 0


def test_an_applied_correction_stops_being_pending() -> None:
    """`--apply` reads the same predicate the drill quarantine does. If they
    drift, a question is either held out of every drill with nothing left to
    apply, or drillable with a correction still outstanding."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (qid,))
    crossaudit.record(conn, qid,
                      {"verdict": "fix", "reason": "terminal value not discounted",
                       "confidence": 0.9, "corrected_answer": "Discount TV back too."},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()

    pending = crossaudit.pending_corrections(conn)
    check([r["id"] for r in pending] == [qid], "a filed correction was not offered")
    check(scheduler.due_questions(conn, limit=10, ids=[qid]) == [],
          "a question with an unapplied correction was still drillable")

    history.set_answer(conn, qid, pending[0]["corrected_answer"],
                       action="cross-audit", batch_id=history.new_batch())
    conn.commit()
    check(crossaudit.pending_corrections(conn) == [],
          "the correction was still pending after being applied")
    check([r["id"] for r in scheduler.due_questions(conn, limit=10, ids=[qid])] == [qid],
          "applying the correction did not lift the quarantine")


def test_an_advisory_correction_is_never_offered_in_bulk() -> None:
    """Below the floor a verdict is a judgement call routed to a human, so it
    must not turn up in a list that `--apply --yes` would write in one go."""
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (qid,))
    crossaudit.record(conn, qid,
                      {"verdict": "fix", "reason": "arguable", "confidence": 0.6,
                       "corrected_answer": "Something else."},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()
    check(crossaudit.pending_corrections(conn) == [],
          "a sub-floor verdict was offered for bulk apply")


def test_a_fix_with_no_correction_attached_is_not_offered() -> None:
    conn, a, _ = fresh()
    qid = _seed(conn, a, "Walk me through a discounted cash flow analysis")
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (qid,))
    crossaudit.record(conn, qid,
                      {"verdict": "fix", "reason": "stale market number",
                       "confidence": 0.9},
                      provider=crossaudit.PROVIDER_CODE, model="m")
    conn.commit()
    check(crossaudit.pending_corrections(conn) == [],
          "a fix carrying no corrected answer was offered for apply")


def _dcm_clean():
    """The pack builders live outside the package, so load the one under test
    by path rather than adding packs/ to sys.path for every run."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "packs" / "_build_dcm.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_build_dcm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.clean


def test_a_table_cell_does_not_run_into_the_next_one() -> None:
    """The handbook's comparison tables landed as 'Legal natureloan (German
    law)security': </td> hit the blanket tag strip and left no separator, so
    two questions shipped with an unreadable answer."""
    clean = _dcm_clean()
    if clean is None:
        return
    out = clean("<table><tr><td>Legal nature</td><td>loan (German law)</td>"
                "<td>security</td></tr></table>")
    check("Legal nature | loan (German law) | security" in out,
          f"cells concatenated or mis-separated: {out!r}")


def test_a_table_row_survives_the_prose_reflow() -> None:
    """ui.body folds a plain line into the paragraph above it, so a row has to
    arrive as a bullet or the whole table renders as one blob."""
    clean = _dcm_clean()
    if clean is None:
        return
    rows = clean("<table><tr><td>AAA</td><td>Aaa</td></tr>"
                 "<tr><td>BBB</td><td>Baa2</td></tr></table>").split("\n")
    check(all(r.startswith("- ") for r in rows if r.strip()),
          f"a row arrived as a bare line: {rows!r}")
    rendered = ui.body(clean("<table><tr><td>AAA</td><td>Aaa</td></tr>"
                             "<tr><td>BBB</td><td>Baa2</td></tr></table>"))
    check(len([ln for ln in rendered.split("\n") if ln.strip()]) == 2,
          f"rows collapsed into one another when rendered: {rendered!r}")


def test_an_empty_spacer_cell_leaves_no_dangling_separator() -> None:
    """A rowspan label column leaves an empty <th> on the header row and an
    empty <td> on most others, which stacked up as trailing ' | | '."""
    clean = _dcm_clean()
    if clean is None:
        return
    out = clean("<table><thead><tr><th>S&amp;P</th><th>Moody's</th><th></th></tr>"
                "</thead><tbody><tr><td>AAA</td><td>Aaa</td></tr></tbody></table>")
    for line in out.split("\n"):
        check(not line.rstrip().endswith("|"), f"dangling separator: {line!r}")
        check(not line.startswith("- |"), f"leading empty cell: {line!r}")


def test_prose_after_a_table_is_not_swallowed_by_the_last_row() -> None:
    """The blank-line collapse ate the break after </table>, so the sentence
    following the rating scale rendered inside the CC / C / D row."""
    clean = _dcm_clean()
    if clean is None:
        return
    out = clean("<table><tr><td>CC</td><td>Ca</td></tr></table>"
                "<p>The line: BBB- is the lowest investment grade notch.</p>")
    check("\n\n" in out, f"no paragraph break after the table: {out!r}")
    rendered = ui.body(out).split("\n")
    check(any(ln.strip().startswith("The line:") for ln in rendered),
          f"the sentence after the table was folded into a row: {rendered!r}")


# ---------------------------------------------------------------- answer card


def test_the_grader_voice_is_only_stripped_when_a_clause_survives() -> None:
    """`Explains that X` leaves a whole sentence. `Identifies X as Y` does not."""
    check(ui.claim("Explains that the Discount Rate affects the PV of everything.")
          == "The Discount Rate affects the PV of everything.",
          "verb + that was not stripped")
    check(ui.claim("States that both Cost of Equity and WACC should be higher.")
          == "Both Cost of Equity and WACC should be higher.",
          "strip left the clause lowercase")
    # Stripping a bare verb leaves a fragment starting mid-sentence, so it is
    # left alone however grader-ish it reads.
    intact = "Identifies the Discount Rate and Terminal Value as the biggest."
    check(ui.claim(intact) == intact, "a bare verb was stripped into a fragment")
    check(ui.claim("Names the three statements.") == "Names the three statements.",
          "a bare verb was stripped into a fragment")
    # The verb *is* the sentence: there is nothing to promote.
    check(ui.claim("Explains that it is.") == "Explains that it is.",
          "stripped down to a fragment too short to be a claim")


def test_a_wrapped_rubric_point_does_not_read_as_two_points() -> None:
    points = ["Explains that " + "the discount rate matters a great deal " * 4]
    card = ui.answer_card(points, w=60)
    lines = [ui.strip(l) for l in card.split("\n")]
    body = [l for l in lines if l.strip() and "GOOD ANSWER" not in l
            and set(l.strip()) != {"\u2500"}]
    check(len(body) > 1, "the point did not wrap, so this proves nothing")
    # Every continuation starts under the claim, never at the tick column.
    first_col = len(body[0]) - len(body[0].lstrip())
    for cont in body[1:]:
        col = len(cont) - len(cont.lstrip())
        check(col > first_col, f"continuation restarted at column {col}, like a new point")
    # Nothing is broken mid-word. A cell-based hard wrap cuts at the column
    # and would leave "disc" / "ount rate" across the join.
    vocab = {"the", "discount", "rate", "matters", "a", "great", "deal"}
    words = {w.strip(".").lower() for l in body for w in l.split()}
    words -= {"\u00b7", "1", ""}          # the tick column and the point number
    check(words <= vocab, f"wrapping split a word: {sorted(words - vocab)}")


def test_an_unmarked_card_never_invents_a_verdict() -> None:
    """A self-rated sitting has no per-point judgement, so it shows none."""
    points = ["Explains that A is true", "Explains that B is true"]
    plain = ui.strip(ui.answer_card(points))
    check("\u2713" not in plain and "\u2717" not in plain,
          "an ungraded card drew ticks and crosses it had no basis for")
    check("of 2" not in plain, "an ungraded card claimed a score")

    marked = ui.strip(ui.answer_card(points, hits=[True, False]))
    check("\u2713" in marked and "\u2717" in marked, "a graded card lost its marks")
    check("1 of 2" in marked, f"tally missing from: {marked!r}")


def test_a_short_grade_does_not_silently_pass_the_rest() -> None:
    """Fewer hits than points must read as missed, not as unmarked."""
    card = ui.strip(ui.answer_card(["a claim here", "b claim here", "c claim here"],
                                   hits=[True]))
    check("1 of 3" in card, f"a short hit list was not padded to the rubric: {card!r}")


# ---------------------------------------------------------------- clipboard


def _clip_fixture():
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Walk me through a DCF",
              answer_text="Project unlevered free cash flow, discount it at WACC, "
                          "then add the present value of the terminal value.")
    conn.execute("UPDATE answers SET rubric_points = ?, common_mistakes = ? "
                 "WHERE question_id = ?",
                 (json.dumps(["Explains that you project unlevered FCF",
                              "Explains that you discount at WACC"]),
                  json.dumps(["Forgetting to unlever the cash flow"]),
                  v.matched_id))
    conn.commit()
    return conn, v.matched_id


def test_a_copy_payload_is_built_from_the_record_not_the_screen() -> None:
    """The old copy path copied wrapped, indented terminal text. This must not."""
    conn, qid = _clip_fixture()
    md = clip.markdown(conn, qid)
    for line in md.split("\n"):
        check(line == line.lstrip() or line.startswith(("1.", "2.", "- ")),
              f"payload carried terminal indentation: {line!r}")
    # The prose is one paragraph on one line, however wide the terminal is.
    prose = [l for l in md.split("\n") if l.startswith("Project unlevered")]
    check(len(prose) == 1 and len(prose[0]) > 100,
          "the answer was hard-wrapped into the payload")
    check("\033[" not in md, "styling leaked into the clipboard")


def test_copying_the_question_copies_only_the_question() -> None:
    conn, qid = _clip_fixture()
    q = clip.question(conn, qid)
    check(q == "Walk me through a DCF", f"got {q!r}")
    check("\n" not in q, "the bare question spanned more than a line")


def test_a_copy_payload_survives_a_question_with_nothing_on_it() -> None:
    """`clip` is reachable from any row, including one enrich has not reached."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Why investment banking",
              answer_text=None)
    md = clip.markdown(conn, v.matched_id)
    check("Why investment banking" in md, "the question was lost")
    check("Model answer" not in md, "an empty answer got a heading anyway")
    check(md.endswith("\n") and not md.endswith("\n\n"),
          "payload ended in a run of blank lines")
    check(clip.markdown(conn, 999999) == "", "a missing id did not return empty")
    check(clip.question(conn, 999999) == "", "a missing id did not return empty")


# ---------------------------------------------------------------- duplicates


def _pair_bank() -> tuple[sqlite3.Connection, list[int]]:
    """Questions inserted straight in, because the gate would refuse them.

    The whole point of `dupes` is the pairs that are already in the bank, and
    a pair close enough to be worth finding is a pair `admit` would have
    merged on the way in. So these bypass it.
    """
    conn, a, _ = fresh()
    texts = [
        "How do you determine whether an event changes Equity Value?",
        # A real reword of the one above. This used to be the Enterprise Value
        # question, which is not a reword of anything -- it is the other half of
        # the CSE/NOA pair, and the gate now says so. It stays in the bank at
        # index 4 as the lookalike that must never be offered as a twin.
        "How do you determine whether a given event changes Equity Value?",
        "Walk me through a leveraged buyout and what drives the returns",
        "What is working capital and why does it matter for a DCF?",
        "How do you determine whether an event changes Enterprise Value?",
    ]
    ids = []
    for text in texts:
        cur = conn.execute(
            "INSERT INTO questions (canonical_text, kind, topic, difficulty, "
            "origin, status, created_at, norm_key) "
            "VALUES (?, 'technical', 'ev_eqv', 3, 'published', 'active', ?, ?)",
            (text, "2026-01-01T00:00:00+00:00", normalize(text)))
        qid = int(cur.lastrowid)
        conn.execute("INSERT INTO answers (question_id, answer_key, rubric_points, "
                     "common_mistakes, answer_status) VALUES (?, ?, '[]', '[]', 'ok')",
                     (qid, "an answer for " + text))
        conn.execute("INSERT INTO question_sources (question_id, source_id, locator) "
                     "VALUES (?, ?, 'p1')", (qid, a))
        ids.append(qid)
    conn.commit()
    return conn, ids


def test_the_dupe_prefilter_finds_exactly_what_a_full_scan_finds() -> None:
    """The old scan was O(bank squared) SequenceMatcher and took 58 seconds on
    1,086 questions -- fine for a print loop you walked once, not fine in
    front of a view that cannot draw until it returns.

    `similarity` is 0.6*jaccard + 0.4*sequence_ratio and the ratio cannot
    exceed 1, so a pair under (threshold - 0.4) / 0.6 jaccard is already short
    before SequenceMatcher is asked. Skipping those has to change no verdict
    at all, at any threshold, or the speed-up is just a scan that misses
    duplicates.
    """
    conn, ids = _pair_bank()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, norm_key FROM questions WHERE status != 'rejected' ORDER BY id")]
    for threshold in (0.60, 0.70, 0.80, 0.88):
        brute = set()
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                if similarity(left["norm_key"], right["norm_key"]) >= threshold:
                    brute.add((left["id"], right["id"]))
        fast = {(p["a"]["id"], p["b"]["id"])
                for p in dupes.pairs(conn, threshold=threshold)}
        check(fast == brute,
              f"at {threshold}: prefilter missed {brute - fast}, invented {fast - brute}")


def test_pairs_come_back_closest_first() -> None:
    conn, _ids = _pair_bank()
    found = dupes.pairs(conn, threshold=0.50)
    check(found, "nothing paired at all")
    sims = [p["similarity"] for p in found]
    check(sims == sorted(sims, reverse=True), f"pairs came back out of order: {sims}")


def _phrase(conn, qid: int, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO phrasings (question_id, text, norm_key) VALUES (?, ?, ?)",
        (qid, text, normalize(text)))
    conn.commit()
    return int(cur.lastrowid)


def test_a_wording_that_asks_something_else_is_caught_before_a_drill_serves_it() -> None:
    """The gate scores a phrasing against the canonical once, on the way in.

    `enrich` then rewrites the canonical and nothing re-scores what is already
    attached. #88 ended up carrying "What is negative working capital?" on a
    card about how working capital is calculated, and `drill` serves a random
    phrasing -- so one sitting in three asked that and then marked the answer
    against the wrong rubric.
    """
    conn, a, _ = fresh()
    v = admit(conn, source_id=a,
              question_text="Can a company's Equity Value ever be negative?",
              answer_text="Only if the share price or the share count is zero." * 3)
    qid = v.matched_id
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (qid,))

    stranger = _phrase(conn, qid, "Could a company's Enterprise Value ever be negative?")
    reword = _phrase(conn, qid, "Is a negative Equity Value possible for a company?")
    german = _phrase(conn, qid, "Kann der Eigenkapitalwert eines Unternehmens "
                                "jemals negativ werden und was bedeutet das?")

    found = {r["id"]: r for r in dupes.drifted(conn)}
    check(stranger in found,
          "a phrasing naming enterprise value on an equity value card was not caught")
    check(reword not in found,
          "an ordinary reword was reported as a stranger")
    check(german not in found,
          "a deliberate translation was reported as a stranger")

    # A plain floor at the variant threshold is the wrong test: two spellings
    # of one question *are* lexically different, which is why phrasings exist
    # at all, and the reword above sits under it while asking exactly the same
    # thing. What separates the two is whether they name the same things.
    reword_score = similarity(
        normalize("Can a company's Equity Value ever be negative?"),
        normalize("Is a negative Equity Value possible for a company?"))
    check(reword_score < VARIANT_AT,
          f"the fixture's reword scored {reword_score:.2f} and proves nothing")

    # Two ways to reach zero, and both stick.
    dupes.keep_phrasing(conn, qid, normalize(
        "Could a company's Enterprise Value ever be negative?"))
    check(stranger not in {r["id"] for r in dupes.drifted(conn)},
          "a wording settled as fine was proposed again")
    check(stranger in {r["id"] for r in dupes.drifted(conn, include_settled=True)},
          "--all stopped showing the settled ones")

    other = _phrase(conn, qid, "Could a company's Enterprise Value be below zero?")
    check(dupes.detach(conn, other), "detaching a phrasing reported no change")
    check(conn.execute("SELECT COUNT(*) FROM phrasings WHERE id = ?",
                       (other,)).fetchone()[0] == 0,
          "a detached phrasing is still attached, so drill can still serve it")


def test_a_pair_settled_as_different_stops_being_proposed() -> None:
    """`dupes` re-scans the whole bank every run, so a pair you looked at and
    decided were two different questions came back forever. That is the
    failure `chains --standalone` exists for one level down: a scan that
    cannot reach zero is a scan you stop reading."""
    conn, ids = _pair_bank()
    found = dupes.pairs(conn, threshold=0.50)
    first = found[0]
    a, b = first["a"]["id"], first["b"]["id"]

    dupes.settle(conn, b, a)          # settled in the other order on purpose
    again = {(p["a"]["id"], p["b"]["id"]) for p in dupes.pairs(conn, threshold=0.50)}
    check((a, b) not in again, "a settled pair was proposed again")
    check(len(again) == len(found) - 1, "settling one pair dropped more than one")

    everything = {(p["a"]["id"], p["b"]["id"])
                  for p in dupes.pairs(conn, threshold=0.50, include_settled=True)}
    check((a, b) in everything, "--all did not bring the settled pair back")

    check(dupes.unsettle(conn, a, b), "unsettle reported nothing to undo")
    check((a, b) in {(p["a"]["id"], p["b"]["id"])
                     for p in dupes.pairs(conn, threshold=0.50)},
          "unsettling did not put the pair back on the scan")


def test_a_settled_pair_has_one_row_whichever_way_round_it_was_walked() -> None:
    conn, ids = _pair_bank()
    dupes.settle(conn, ids[1], ids[0])
    dupes.settle(conn, ids[0], ids[1])
    n = conn.execute("SELECT COUNT(*) FROM question_pair_review").fetchone()[0]
    check(n == 1, f"the same pair got {n} rows")
    check(dupes.settled(conn) == {(ids[0], ids[1])},
          "a settled pair is not keyed low-id first")


def test_merging_moves_a_follow_up_onto_the_keeper() -> None:
    """`parent_id` pointing at a rejected question is a lead-in that would be
    printed above a follow-up in `drill` and can never itself be asked.
    `chains.link` refuses to create that; a merge must not create it either."""
    conn, ids = _pair_bank()
    keeper, dupe, child = ids[0], ids[1], ids[2]
    chains.link(conn, child, dupe)
    dupes.merge(conn, keeper, dupe, history.new_batch())
    parent = conn.execute("SELECT parent_id FROM questions WHERE id = ?",
                          (child,)).fetchone()[0]
    check(parent == keeper, f"the follow-up was left hanging off #{parent}")
    check(conn.execute("SELECT parent_id FROM questions WHERE id = ?",
                       (keeper,)).fetchone()[0] is None,
          "the keeper was made its own lead-in")


def test_a_merge_clears_anything_recorded_about_the_pair() -> None:
    conn, ids = _pair_bank()
    a, b = ids[0], ids[1]
    dupes.settle(conn, a, b)
    dupes.merge(conn, a, b, history.new_batch())
    check(dupes.settled(conn) == set(),
          "a merged pair is still on file as an open question")


def test_the_diff_lights_only_the_words_that_differ() -> None:
    """Two questions at 0.9 similarity are nine tenths the same sentence and
    the whole decision is in the tenth. The old side-by-side printed both and
    left you to find it."""
    left, right = dupes.diff_words(
        "How do you determine whether an event changes Equity Value?",
        "How do you determine whether an event changes Enterprise Value?")
    lit_l = [w for w, differs in left if differs]
    lit_r = [w for w, differs in right if differs]
    check(lit_l == ["Equity"], f"left lit {lit_l}")
    check(lit_r == ["Enterprise"], f"right lit {lit_r}")
    check([w for w, _ in left][:4] == ["How", "do", "you", "determine"],
          "the original wording did not survive the diff")

    # Case and trailing punctuation are not a difference. "EBITDA?" and
    # "EBITDA" are the same word for the purpose of deciding whether two
    # questions are the same question.
    same, _ = dupes.diff_words("What is EBITDA?", "what is ebitda")
    check(not any(differs for _w, differs in same),
          f"case or punctuation read as a difference: {same}")

    # Nothing on either side is still a well-formed answer, not a crash.
    check(dupes.diff_words("", "") == ([], []), "an empty pair did not diff")


def test_a_compare_column_never_overruns_the_pane() -> None:
    """A compare pane whose right column ran long wrapped in the terminal,
    pushed the input box off the bottom and tore the whole screen, two panels
    from the cause. `TabsView.render` clamps pane lines now as well, but a
    pane that composes its own overlong rows is still building the wrong
    thing -- the clamp would silently eat the end of the column rather than
    lay the comparison out to fit.

    `ui.columns` cannot be used for this on its own: it clamps the left column
    because the padding is measured against it, and leaves the right one
    whole.
    """
    conn, ids = _pair_bank()
    long_text = "Enterprise Value Drivers " * 12
    conn.execute("UPDATE questions SET subtopic = ? WHERE id = ?", (long_text, ids[1]))
    conn.execute("UPDATE answers SET rubric_points = ? WHERE question_id = ?",
                 (json.dumps([long_text, long_text]), ids[1]))
    conn.commit()
    view = views.ComparePairView(conn, ids[0], ids[1])
    for width in (60, 80, 120):
        for i in range(len(view.tabs)):
            view.idx = i
            view._cache.clear()
            for line in view.pane(width).lines:
                check(ui.vlen(line) <= width,
                      f"a {ui.vlen(line)}-cell line in a {width}-cell "
                      f"{view.tabs[i][0]} pane")


def test_every_tab_on_a_screen_is_the_same_height() -> None:
    """The frame is anchored to the bottom of the terminal, so a pane drawn to
    fit its contents is a pane that moves. Tabbing across the dashboard slid
    the tab bar between screen rows 16 and 33 -- out from under the ◂ ▸ the
    reader was in the middle of pressing. `BrowseView` already pins its pane
    to the full body for this reason; `TabsView` was the screen still growing
    and shrinking to its content."""
    view = views.TabsView("T", [
        ("short", lambda w: ["just the one row"]),
        ("long", lambda w: [f"row {i}" for i in range(40)]),
        ("empty", lambda w: []),
    ])
    view.viewport = 20
    heights = []
    for i in range(len(view.tabs)):
        view._go_tab(i)
        heights.append(len(view.render(80)))
    check(len(set(heights)) == 1,
          f"the three tabs came out {heights} rows tall")


def test_a_pane_line_never_overruns_the_frame() -> None:
    """`PickerView.put` clamps every row it draws and says why: one line a
    cell too long wraps in the terminal and pushes the input box off the
    bottom. `TabsView` trusted its pane builders instead, and `show`'s Sources
    pane was 62 cells over on the widest source in the bank."""
    view = views.TabsView("T", [("x", lambda w: ["z" * 400, "short"])])
    view.viewport = 20
    for width in (60, 80, 120):
        for line in view.render(width):
            check(ui.vlen(line) <= width,
                  f"a {ui.vlen(line)}-cell pane line in a {width}-cell frame")


def test_a_source_line_fits_the_pane_it_is_drawn_in() -> None:
    """The title was clamped and the locator was not, so the composed line ran
    past the pane and the locator -- being last -- was the half chopped
    mid-word with no ellipsis to say so."""
    long = "Technical Questions - Equity Value, Enterprise Value and Multiples"
    rec = {"sources": [{"kind": "docx", "title": long,
                        "locator": long + ", part two", "verbatim_text": ""}]}
    for width in (60, 80, 120):
        for line in cli._q_sources(rec, width):
            check(ui.vlen(line) <= width,
                  f"a {ui.vlen(line)}-cell source line in a {width}-cell pane")


def test_a_source_does_not_print_its_own_title_twice() -> None:
    """`ingest` sets `title` and `locator` to the same file stem, so the row
    spent most of its width saying one long heading and then saying it again.
    A locator earns its column when it says something the title did not."""
    long = "Technical Questions - Equity Value and Multiples"
    same = {"sources": [{"kind": "docx", "title": long, "locator": long,
                         "verbatim_text": ""}]}
    line = ui.strip(cli._q_sources(same, 200)[0])
    check(line.count("Equity Value") == 1, f"the locator repeated the title: {line}")
    apart = {"sources": [{"kind": "docx", "title": long, "locator": "page 41",
                          "verbatim_text": ""}]}
    line = ui.strip(cli._q_sources(apart, 200)[0])
    check("page 41" in line, f"a locator with something to say was dropped: {line}")


def test_a_group_heading_rule_ends_where_every_other_rule_does() -> None:
    """`width - len(name) - 5` leaves the rule two cells short of the
    `"  " + hairline(width - 2)` drawn directly above it, so every do-screen
    and every grouped result list had a ragged right edge. It was spelled out
    three times, which is why it was wrong three times."""
    for width in (60, 80, 120):
        header = "  " + ui.hairline(width - 2)
        for name in ("do", "active filters", "today"):
            rule = views.group_rule(name, width)
            check(ui.vlen(rule) == ui.vlen(header),
                  f"`{name}` rule is {ui.vlen(rule)} cells against a "
                  f"{ui.vlen(header)}-cell header rule at width {width}")


def test_a_screen_with_one_tab_does_not_advertise_the_tab_key() -> None:
    """A keymap is the one piece of documentation a reader tests by pressing
    it. `help` is a single pane and offered `◂ ▸ tab`, which moves nothing."""
    one = views.TabsView("HELP", [("Commands", lambda w: ["a"])])
    two = views.TabsView("T", [("a", lambda w: ["a"]), ("b", lambda w: ["b"])])
    check("tab" not in ui.strip(one.footer()),
          f"a one-tab screen still advertises tab: {ui.strip(one.footer())}")
    check("tab" in ui.strip(two.footer()),
          "a two-tab screen stopped advertising tab")


def test_no_theme_says_heading_and_error_in_one_colour() -> None:
    """`FLOORS` measures each token against the background and says nothing
    about two tokens measured against each other, so a palette could clear
    every floor while `ui.head` and `ui.bad` emitted identical bytes.
    `github-light` and `monokai` both did: on `stats` the `BY STATUS` heading
    came out in exactly the red of the `rejected` count underneath it."""
    for name, t in theme_mod.THEMES.items():
        tokens = dict(t.tokens())
        for a, b in theme_mod.OPPOSED:
            check(tokens[a] != tokens[b],
                  f"{name} draws both {a} and {b} as #{tokens[a]}")


def test_a_narrow_terminal_stacks_the_compare_instead_of_columning_it() -> None:
    """Two columns of fourteen cells each is not a comparison, it is two
    ladders of single words."""
    conn, ids = _pair_bank()
    view = views.ComparePairView(conn, ids[0], ids[1])
    body = "\n".join(view.pane(50).lines)
    check(f"#{ids[0]}" in body and f"#{ids[1]}" in body,
          "a narrow pane lost one of the two questions")


def test_a_merged_pair_is_never_offered_a_second_merge() -> None:
    """The compare comes back after the command it started, and the pane is
    rebuilt from the database rather than from what it was holding. A merge
    still on offer there would run a second `set_status` batch that `undo`
    then has to unwind before it can reach the merge you actually meant."""
    conn, ids = _pair_bank()
    a, b = ids[0], ids[1]
    before = {act.key for act in views.pair_actions(conn, a, b)}
    check({"keep-a", "keep-b", "distinct"} <= before, f"missing decisions: {before}")

    dupes.merge(conn, a, b, history.new_batch())
    after = views.pair_actions(conn, a, b)
    check(not any(act.key in ("keep-a", "keep-b", "distinct") for act in after),
          f"a merged pair still offers {[act.key for act in after]}")
    check(all(act.line.startswith("show ") for act in after),
          "a merged pair offers something other than reading it")

    view = views.ComparePairView(conn, a, b)
    check(any("folded away" in ui.strip(line) for line in view.pane(90).lines),
          "the compare did not say the pair had been merged")


def test_a_settled_pair_is_never_offered_a_second_distinct_verdict() -> None:
    """One Enter on "they are different questions" settled it, but the pane
    came back offering the exact same five options -- nothing on screen said
    the first press had done anything, so a second Enter on the same row
    fired `dupes --distinct` a second time. The row a decided pair offers has
    to change, or the screen is lying about what just happened."""
    conn, ids = _pair_bank()
    a, b = ids[0], ids[1]
    before = {act.key for act in views.pair_actions(conn, a, b)}
    check({"keep-a", "keep-b", "distinct"} <= before, f"missing decisions: {before}")

    dupes.settle(conn, a, b)
    after = views.pair_actions(conn, a, b)
    check(not any(act.key in ("keep-a", "keep-b", "distinct") for act in after),
          f"a settled pair still offers {[act.key for act in after]}")
    check(any(act.line == f"dupes --undistinct {a},{b}" for act in after),
          "a settled pair offers no way back to undo the verdict")

    view = views.ComparePairView(conn, a, b)
    check(any("different questions" in ui.strip(line) for line in view.pane(90).lines),
          "the compare did not say the pair had been settled")

    # And the undo round-trips: unsettling it brings the three decisions back.
    dupes.unsettle(conn, a, b)
    restored = {act.key for act in views.pair_actions(conn, a, b)}
    check({"keep-a", "keep-b", "distinct"} <= restored,
          f"unsettling did not restore the decisions: {restored}")


def test_a_decided_pair_drops_out_of_the_list_it_was_opened_from() -> None:
    """The list a compare was opened from is one command out of date once a
    decision is made from inside it -- exactly the screen that still showed
    "they are different questions" as if nothing had been said yet. Coming
    back through `on_resume` has to mean the pair does not still read as
    open, not just that the compare screen agrees with the database."""
    conn, ids = _pair_bank()
    a, b, c, d = ids[0], ids[1], ids[2], ids[3]
    rows = [{"a": {"id": a, "canonical_text": "x", "topic": "t", "status": "active",
                   "norm_key": None, "difficulty": 3},
             "b": {"id": b, "canonical_text": "y", "topic": "t", "status": "active",
                   "norm_key": None, "difficulty": 3},
             "similarity": 0.9},
            {"a": {"id": c, "canonical_text": "x", "topic": "t", "status": "active",
                   "norm_key": None, "difficulty": 3},
             "b": {"id": d, "canonical_text": "y", "topic": "t", "status": "active",
                   "norm_key": None, "difficulty": 3},
             "similarity": 0.9}]
    view = views.DupesView(conn, list(rows), threshold=0.7)
    check(view.count() == 2, "the list did not start with both pairs")

    dupes.settle(conn, a, b)
    dupes.merge(conn, c, d, history.new_batch())
    view.on_resume()
    check(view.count() == 0,
          f"a settled and a merged pair are both still listed as open: {view.rows}")


def test_every_decision_a_pair_offers_writes_to_the_schedule_twice() -> None:
    """A merge rejects a question, which is exactly what `undo` exists for and
    exactly the kind of thing that must not happen on one keystroke. Saying
    they are different is neither, so it does not ask twice."""
    conn, ids = _pair_bank()
    for act in views.pair_actions(conn, ids[0], ids[1]):
        if act.key in ("keep-a", "keep-b"):
            check(act.arm, f"{act.key} merges on a single press")
        else:
            check(not act.arm, f"{act.key} asks twice for something that only reads")


def test_a_result_row_offers_the_question_it_looks_like() -> None:
    """Reaching a near-duplicate used to mean running the whole-bank scan and
    then finding this question in it: a minute of work to answer a question
    about the row already under the cursor."""
    conn, ids = _pair_bank()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, canonical_text, topic, status FROM questions WHERE id IN (?, ?)",
        (ids[0], ids[2]))]
    view = views.ResultsView(conn, rows, title="FIND")
    twinned = [i for i, r in enumerate(view.rows) if r["id"] == ids[0]][0]
    lonely = [i for i, r in enumerate(view.rows) if r["id"] == ids[2]][0]
    compare = [a for a in view.actions(twinned) if a.key == "twin"]
    check(compare, "the row with an obvious twin was offered no compare")
    check(compare[0].line == f"dupes --pair {ids[0]},{ids[1]}",
          f"the compare would open {compare[0].line!r}")
    check(not [a for a in view.actions(lonely) if a.key == "twin"],
          "a question with nothing like it was offered a compare anyway")

    # And the lookalike is not a twin. #20 and #21 in the real bank are this
    # pair and scored 0.837 before the identity guard learned the vocabulary:
    # four hundredths from the compare screen offering to fold one into the
    # other.
    rows2 = [dict(r) for r in conn.execute(
        "SELECT id, canonical_text, topic, status FROM questions WHERE id IN (?, ?)",
        (ids[0], ids[4]))]
    view2 = views.ResultsView(conn, rows2, title="FIND")
    twin_rows = [a for i in range(len(view2.rows))
                 for a in view2.actions(i) if a.key == "twin"]
    check(not [a for a in twin_rows if f",{ids[4]}" in a.line or f"{ids[4]}," in a.line],
          "equity value and enterprise value were offered as each other's twin")

    # And once you have said they are different, it stops being offered -- the
    # do-screen is not a second place the settled decision has to be repeated.
    dupes.settle(conn, ids[0], ids[1])
    view.invalidate()
    check(not [a for a in view.actions(twinned) if a.key == "twin"],
          "a settled pair is still offered as a compare")


# ---------------------------------------------------------------- question lines, whole


def _line_bank() -> tuple[sqlite3.Connection, list[int]]:
    """A line that forks and then goes deep:

        0
        ├─ 1
        ├─ 2
        │  ├─ 4
        │  └─ 5
        │     └─ 6
        └─ 3
    """
    conn, a, _ = fresh()
    ids = []
    for i in range(7):
        v = admit(conn, source_id=a, answer_text="x" * 60,
                  question_text=f"Question number {i} about a distinct subject {i}")
        conn.execute("UPDATE questions SET status = 'active' WHERE id = ?",
                     (v.matched_id,))
        ids.append(v.matched_id)
    conn.commit()
    for child, parent in [(1, 0), (2, 0), (3, 0), (4, 2), (5, 2), (6, 5)]:
        chains.link(conn, ids[child], ids[parent])
    return conn, ids


def test_a_line_is_walked_up_to_its_root_from_anywhere_in_it() -> None:
    conn, ids = _line_bank()
    for i in range(7):
        check(chains.root_of(conn, ids[i]) == ids[0],
              f"from #{ids[i]} the root came back as {chains.root_of(conn, ids[i])}")
    check(chains.root_of(conn, 999999) is None, "an unknown id claimed a root")


def test_the_graph_shows_the_branches_lead_in_cannot() -> None:
    """`lead_in` walks a single ancestry, because two turns of context is all
    `drill` needs to make the question answerable. A line forks -- an
    interviewer sets up one scenario and asks three things about it -- and
    from inside any one of the three the other two are invisible."""
    conn, ids = _line_bank()
    nodes = chains.graph(conn, ids[6])
    check([n["id"] for n in nodes] == [ids[i] for i in (0, 1, 2, 4, 5, 6, 3)],
          f"the tree came out in the order {[n['id'] for n in nodes]}")
    check(sum(1 for n in nodes if n["target"]) == 1, "the target is not marked exactly once")
    check(next(n for n in nodes if n["target"])["id"] == ids[6], "the wrong node is marked")

    # The point of the whole thing: siblings of an ancestor are in the tree,
    # and `lead_in` from the same question does not know about any of them.
    ancestry = {r["id"] for r in chains.lead_in(conn, ids[6], limit=99)}
    check(ids[1] not in ancestry, "the fixture does not actually fork")
    check(ids[1] in {n["id"] for n in nodes}, "the graph missed a branch")


def test_the_graph_draws_a_rail_only_under_a_branch_that_continues() -> None:
    """A nested node under a parent that still has siblings below it needs the
    vertical rail carried down, or the tree reads as if the deep branch hangs
    off the root."""
    conn, ids = _line_bank()
    by_id = {n["id"]: n for n in chains.graph(conn, ids[0])}

    root = by_id[ids[0]]
    check(root["depth"] == 0 and root["rails"] == [],
          "the root was given a gutter to sit behind")
    check(views.tree_prefix(root["rails"], root["last"], root["depth"]) == "",
          "the root was drawn with a branch connector")

    # #3 is the last child of the root, so it closes the branch.
    check(by_id[ids[3]]["last"], "the last child was not marked last")
    check(not by_id[ids[2]]["last"], "a middle child was marked last")

    # #6 is two levels under #2, which is not the root's last child -- so a
    # rail has to be carried at the root's column.
    deep = by_id[ids[6]]
    check(deep["depth"] == 3, f"#{ids[6]} came out at depth {deep['depth']}")
    drawn = ui.strip(views.tree_prefix(deep["rails"], deep["last"], deep["depth"]))
    check(drawn == "│     └─ ", f"the deep branch drew {drawn!r}")
    check(drawn.count("│") == 1, "a rail was drawn under a branch that had closed")


def test_the_graph_glyphs_come_from_the_shared_set() -> None:
    """A tree drawn in the shell and the same tree printed to a pipe have to
    come out of one set of characters, which is why they live in `ui.py`."""
    drawn = ui.strip(views.tree_prefix([True], False, 2))
    for glyph in (ui.SQUARE["v"], ui.SQUARE["lt"], ui.SQUARE["h"]):
        check(glyph in drawn, f"{glyph!r} is not what the tree is drawn with")


def test_a_question_in_no_line_has_a_graph_of_one() -> None:
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="What is WACC and how do you build it up?",
              answer_text="Cost of equity and after-tax cost of debt, weighted.")
    nodes = chains.graph(conn, v.matched_id)
    check(len(nodes) == 1, f"a standalone question graphed to {len(nodes)} rows")
    check(chains.graph(conn, 999999) == [], "an unknown id produced a tree")


def test_a_graph_cannot_hang_on_a_line_that_loops() -> None:
    """`chains.link` refuses to close a loop, so this cannot arrive through
    the command. It can still arrive in data edited another way, and a walk
    that trusts the invariant freezes the shell rather than reporting it."""
    conn, ids = _line_bank()
    conn.execute("UPDATE questions SET parent_id = ? WHERE id = ?", (ids[6], ids[0]))
    conn.commit()
    check(chains.root_of(conn, ids[6]) is not None, "root_of never returned")
    nodes = chains.graph(conn, ids[6])
    check(len(nodes) == len({n["id"] for n in nodes}), "a node was drawn twice")
    check(len(nodes) <= 7, f"the walk produced {len(nodes)} nodes from 7 questions")


def test_the_graph_row_says_which_one_you_asked_about() -> None:
    """Eight questions all rendered the same way do not tell you which one you
    came in on, which is the only thing you already knew."""
    conn, ids = _line_bank()
    view = views.ChainGraphView(conn, chains.graph(conn, ids[6]), ids[6])
    view.viewport = 40
    lines = [ui.strip(l) for l in view.flatten(100)]
    body = [l for l in lines if "#" in l and "Question number" in l]
    check(len(body) == 7, f"the tree drew {len(body)} rows")
    for line in lines:
        check(ui.vlen(line) <= 100, f"a row overran the frame: {line!r}")
    check(any("forks" in l for l in lines), "a forking line did not say so")


def test_the_graph_opens_a_question_rather_than_editing_the_line() -> None:
    """Read-only about the shape: a tree is where you find out a link is
    wrong, and `chains` is where you say so."""
    conn, ids = _line_bank()
    sh = _FakeShell()
    view = views.ChainGraphView(conn, chains.graph(conn, ids[6]), ids[6])
    view.sel = 0
    view.handle(_Keys("enter"), sh)
    check(sh.ran == f"show {ids[0]}", f"enter on a node ran {sh.ran!r}")
    for act in view.actions(0):
        check(not act.line.startswith("chains --"),
              f"the graph offers {act.line!r}, which rewrites the line it is drawing")


# ---------------------------------------------------------------- usage log


def _usage_sandbox():
    """Point the log somewhere disposable and hand back its path.

    `main` already redirects it for the whole run; this narrows it to one test
    so the rows a case writes cannot be seen by the next one.
    """
    p = Path(tempfile.mkdtemp()) / "usage.jsonl"
    os.environ["IB_USAGE_LOG"] = str(p)
    return p


def test_a_provider_call_is_logged_with_what_it_reported() -> None:
    """usageMetadata is the only per-call usage signal the API gives. If it is
    not captured at the moment the response arrives it is gone -- there is no
    endpoint that will tell you afterwards what a call cost."""
    _usage_sandbox()
    real, real_key = llm.urllib.request.urlopen, llm.api_key
    llm.api_key = lambda name="": "k"
    llm.urllib.request.urlopen = _fake_gemini({
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "an answer"}]}}],
        "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 300,
                          "totalTokenCount": 1500, "thoughtsTokenCount": 40}})
    try:
        llm.generate("x", model="m", retries=1, caller="grade")
    finally:
        llm.urllib.request.urlopen, llm.api_key = real, real_key

    rows = usage.entries()
    check(len(rows) == 1, f"{len(rows)} rows logged for one call")
    row = rows[0]
    check(row["outcome"] == "ok" and row["caller"] == "grade", f"logged {row}")
    check(row["prompt_tokens"] == 1200 and row["output_tokens"] == 300,
          f"token counts lost: {row}")
    check(row["total_tokens"] == 1500 and row["thinking_tokens"] == 40, f"{row}")
    check(row["provider"] == "gemini" and row["model"] == "m", f"{row}")
    check(isinstance(row.get("seconds"), (int, float)), "the call was not timed")


def test_a_failed_call_is_logged_once_and_not_twice() -> None:
    """A failure the response body explains is logged where the token counts
    are, which is inside the call; everything else is logged by the retry
    loop. Without a flag saying which happened, the first kind is counted
    twice and the day's call count drifts up by exactly the calls that went
    wrong."""
    _usage_sandbox()
    real, real_key = llm.urllib.request.urlopen, llm.api_key
    llm.api_key = lambda name="": "k"
    llm.urllib.request.urlopen = _fake_gemini({
        "candidates": [{"finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": '{"a":'}]}}],
        "usageMetadata": {"promptTokenCount": 90, "totalTokenCount": 190}})
    try:
        llm.generate("x", schema={"type": "object"}, model="m", retries=3,
                     caller="enrich")
        raise AssertionError("a truncated response was accepted")
    except llm.LLMError:
        pass
    finally:
        llm.urllib.request.urlopen, llm.api_key = real, real_key

    rows = usage.entries()
    check(len(rows) == 1, f"one doomed call produced {len(rows)} rows")
    check(rows[0]["outcome"] == "failed", f"logged as {rows[0]['outcome']}")
    # And it still carries what it burned: a call that failed at the output
    # cap spent its place in the rate limit exactly like one that worked.
    check(rows[0]["total_tokens"] == 190,
          "a failed call was logged without the tokens it burned")


def test_a_refusal_records_what_the_provider_asked_for() -> None:
    """A 429 with the provider's own retryDelay on it is the only
    authoritative statement about your quota that exists anywhere -- there is
    no endpoint that reports remaining quota, so this row is the reading."""
    _usage_sandbox()
    body = json.dumps({"error": {"message": "rate limit", "details": [
        {"retryDelay": "31s"}]}}).encode()

    class Boom(llm.urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 429, "Too Many Requests", {}, None)
        def read(self):
            return body

    real, real_key, real_sleep = (llm.urllib.request.urlopen, llm.api_key,
                                  llm.time.sleep)
    llm.api_key = lambda name="": "k"
    llm.time.sleep = lambda _s: None

    def boom(req, **kw):
        raise Boom()

    llm.urllib.request.urlopen = boom
    try:
        llm.generate("x", model="m", retries=2, caller="audit")
        raise AssertionError("a 429 was accepted")
    except llm.LLMError:
        pass
    finally:
        (llm.urllib.request.urlopen, llm.api_key,
         llm.time.sleep) = real, real_key, real_sleep

    rows = usage.entries()
    check(rows, "a refusal was not logged at all")
    check(all(r["status"] == 429 for r in rows), f"logged {rows}")
    check(all(r["outcome"] == "refused" for r in rows),
          "a refusal was filed as an ordinary failure")
    check(rows[0]["retry_after"] == 31.0,
          f"the wait the provider named was lost: {rows[0]}")
    check(usage.refusals(rows) == rows, "refusals() did not pick them out")


def test_the_usage_log_never_raises_into_a_call() -> None:
    """A logging failure is not a reason to lose a graded answer. The one
    thing worse than no usage log is one that can raise out of `grade`."""
    os.environ["IB_USAGE_LOG"] = "/nonexistent-directory-for-a-test/usage.jsonl"
    try:
        usage.record(provider="gemini", caller="grade", model="m", outcome="ok")
    except Exception as e:                       # noqa: BLE001 - that is the test
        raise AssertionError(f"record() raised {e!r} into the call path")
    check(usage.entries() == [], "an unwritable log invented rows")


def test_a_token_count_the_provider_did_not_send_stays_missing() -> None:
    """Zero is a different claim from silence: it reads as "that call was
    free". batchEmbedContents sends no usageMetadata at all."""
    empty = usage.tokens_from({})
    check(all(v is None for v in empty.values()), f"absent counts became {empty}")
    partial = usage.tokens_from({"usageMetadata": {"promptTokenCount": 10}})
    check(partial["prompt_tokens"] == 10, "a reported count was dropped")
    check(partial["total_tokens"] is None, "an unreported count became a number")

    _usage_sandbox()
    usage.record(provider="gemini", caller="embed", model="e", outcome="ok",
                 **usage.tokens_from({}))
    row = usage.entries()[0]
    check("total_tokens" not in row, f"a missing count was written anyway: {row}")


def test_the_usage_day_starts_at_utc_midnight() -> None:
    """A per-day quota is counted against the provider's day. Reading it from
    local midnight disagrees with them by however many hours you are offset."""
    _usage_sandbox()
    now = datetime.now(timezone.utc)
    rows = [{"at": (now - timedelta(hours=h)).isoformat(), "caller": "enrich"}
            for h in (0, 1, 30)]
    today = usage.since_midnight(rows, end=now)
    expected = sum(1 for r in rows
                   if datetime.fromisoformat(r["at"]).date() == now.date())
    check(len(today) == expected, f"{len(today)} counted, expected {expected}")
    check(len(usage.within(rows, 3600 * 2, end=now)) == 2,
          "the two-hour window did not hold exactly the two recent calls")


def test_a_damaged_log_line_is_skipped_rather_than_fatal() -> None:
    """The log is appended to from a worker thread. A half-written line at the
    end of it must not take `usage` down with it."""
    p = _usage_sandbox()
    usage.record(provider="gemini", caller="enrich", model="m", outcome="ok")
    with p.open("a") as fh:
        fh.write('{"at": "2026-01-01T00:00:00+00:00", "caller": "trunc"\n')
        fh.write("\n")
        fh.write("not json at all\n")
    rows = usage.entries()
    check(len(rows) == 1, f"a damaged log read back {len(rows)} rows")


def test_the_log_is_trimmed_rather_than_growing_for_ever() -> None:
    p = _usage_sandbox()
    for _i in range(40):
        usage.record(provider="gemini", caller="enrich", model="m", outcome="ok")
    check(usage.prune(keep=10) == 30, "prune did not report what it dropped")
    check(len(usage.entries()) == 10, "prune left the wrong number of rows")
    check(usage.prune(keep=10) == 0, "a second prune dropped rows that were kept")


def test_the_usage_screen_names_whoever_is_answering() -> None:
    """It said WHAT GOOGLE ACTUALLY SAID and pointed at AI Studio whichever
    provider was configured. A screen whose whole argument is "these are the
    calls we made, not what the provider says you have left" cannot then
    misname the provider. It also printed `settings rate_limit_rpd 1500`,
    which on this screen of all screens reads as a recommended number."""
    real = os.environ.get("IB_PROVIDER")
    try:
        for name in llm.PROVIDERS:
            os.environ["IB_PROVIDER"] = name
            check(llm.limits_url() == llm.PROVIDERS[name]["limits"],
                  f"{name} was sent to another vendor's rate-limit page")
            check(llm.provider_label() == llm.PROVIDERS[name]["label"],
                  f"{name} was labelled {llm.provider_label()}")
    finally:
        if real is None:
            os.environ.pop("IB_PROVIDER", None)
        else:
            os.environ["IB_PROVIDER"] = real
    pane = cli._usage_now(connect(Path(tempfile.mkdtemp()) / "t.db"), [])
    drawn = "\n".join(ui.strip(l) for l in (pane.lines if hasattr(pane, "lines") else pane))
    check("GOOGLE" not in drawn or llm.provider() == "gemini",
          "the usage heading still hardcodes one vendor")
    check("1500" not in drawn,
          f"the screen still suggests a rate limit it cannot know:\n{drawn}")
    check("<your number>" in drawn,
          "the screen stopped saying what to do about a missing limit")


def test_usage_counts_but_never_claims_to_know_what_is_left() -> None:
    """There is no endpoint that reports remaining quota, every vendor keeps
    its real limits behind a login, and the numbers on the open web disagree
    by a factor of four. So no limit ships in this repo, and nothing is drawn
    against one until you supply it."""
    check(cli.config_mod.DEFAULTS["rate_limit_rpm"] == 0
          and cli.config_mod.DEFAULTS["rate_limit_rpd"] == 0,
          "a rate limit was guessed at and shipped as a default")
    check(cli._usage_bar(9, 0) == "",
          "a gauge was drawn against a limit nobody set")
    check(cli._usage_bar(9, 15), "a gauge was withheld when a limit was set")
    check("not what the provider says you have left" in cli.USAGE_CAVEAT,
          "the screen does not say whose numbers these are")


def test_the_usage_pane_holds_one_line_per_entry() -> None:
    """`TabsView` windows a pane by counting list entries, so an entry with a
    newline inside it is a row the scroll maths does not know exists -- and it
    loses its indent on the way out."""
    _usage_sandbox()
    usage.record(provider="gemini", caller="enrich", model="m", outcome="ok",
                 **usage.tokens_from({"usageMetadata": {"totalTokenCount": 12}}))
    from . import cli
    conn, _a, _b = fresh()
    rows = usage.entries()
    panes = [cli._usage_now(conn, rows).lines,
             cli._usage_callers(conn, rows, 90),
             cli._usage_trouble(conn, rows, 90)]
    for lines in panes:
        for line in lines:
            check("\n" not in line, f"a pane entry carried a newline: {line!r}")
            check(ui.vlen(line) <= 90, f"a pane line overran: {ui.vlen(line)}")


# ---------------------------------------------------------------- add --llm


def _fake_structure(item: dict):
    """Stand in for the one structuring call, capturing what it was asked."""
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        seen["schema"] = kw.get("schema")
        seen["thinking"] = kw.get("thinking")
        seen["caller"] = kw.get("caller")
        return item
    return fake, seen


_GOOD_ITEM = {
    "canonical_question": "Walk me through how a change in D&A flows through the three statements",
    "topic": "accounting", "subtopic": "three statement", "difficulty": 2,
    "answer_key": "A $10 increase in D&A cuts EBIT by $10 and, at a 40% tax "
                  "rate, net income by $6. The cash flow statement adds the "
                  "$10 back, so cash rises by $4.",
    "rubric_points": ["Explains that net income falls by D&A times one minus the tax rate",
                      "Adds the non-cash charge back on the cash flow statement",
                      "Reduces PP&E on the balance sheet"],
    "common_mistakes": ["Forgetting the add-back"],
    "tags": ["three-statement-integration", "depreciation"],
}


def test_add_stores_the_question_you_typed_not_its_letters() -> None:
    """`cmd_add` joined `args.text` with spaces, and the parser handed it a
    single string rather than a list -- so the join ran over the characters
    and `add what is EBITDA` stored "w h a t   i s   E B I T D A"."""
    from .cli import build_parser
    p = build_parser()
    args = p.parse_args(["add", "what", "is", "EBITDA"])
    check(" ".join(args.text) == "what is EBITDA",
          f"unquoted words joined to {' '.join(args.text)!r}")
    args = p.parse_args(["add", "what is EBITDA"])
    check(" ".join(args.text) == "what is EBITDA",
          f"a quoted question joined to {' '.join(args.text)!r}")


def _edited(conn, qid: int, replies: list[str]) -> None:
    """Drive `cmd_edit` through a scripted sitting at its prompt."""
    import argparse
    import builtins
    import contextlib
    import io
    from . import cli
    answers = iter(replies)
    real = builtins.input
    builtins.input = lambda _p="": next(answers)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_edit(conn, argparse.Namespace(id=qid))
    finally:
        builtins.input = real


def test_editing_the_common_mistakes_is_something_undo_can_take_back() -> None:
    """`edit` promises "changes save immediately; reversible with `superday
    undo`" on the way in, and every branch honoured it except this one, which
    wrote a raw UPDATE. An undo that silently keeps one of the six things you
    just changed is worse than no undo, because the command reports success
    either way."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a,
              question_text="Why can two companies have different WACCs?",
              answer_text="Different capital structures and betas." * 3)
    qid = v.matched_id
    conn.execute("UPDATE answers SET common_mistakes = ? WHERE question_id = ?",
                 (json.dumps(["forgets the tax shield"]), qid))
    conn.commit()

    _edited(conn, qid, ["m", "confuses beta with volatility", "", "done"])
    stored = json.loads(conn.execute(
        "SELECT common_mistakes FROM answers WHERE question_id = ?",
        (qid,)).fetchone()["common_mistakes"])
    check(stored == ["confuses beta with volatility"], f"the edit stored {stored}")

    batch = history.last_batch(conn)
    check(batch is not None, "editing the mistakes left nothing in the history")
    history.undo_batch(conn, batch["batch_id"])
    back = json.loads(conn.execute(
        "SELECT common_mistakes FROM answers WHERE question_id = ?",
        (qid,)).fetchone()["common_mistakes"])
    check(back == ["forgets the tax shield"], f"undo left {back}")


def test_retopicing_a_question_by_hand_refiles_it_as_the_right_kind() -> None:
    """`kind` is derived from `topic` in every path that writes one -- `admit`,
    the pipeline, `enrich`, `ingest-pack` -- and `edit` was the one place it
    was not. A fit question retopiced to `behavioural` stayed filed as a
    technical: drilled by the wrong rounds, missing from `drill -k
    behavioural`, and judged by an audit prompt told to reject exactly the
    career narrative it is made of. Five questions in the real bank sat that
    way, and the browser is where you finally see them -- `Behavioural 5`,
    listed inside Technicals."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="Why do you want to be a banker?",
              answer_text="y" * 60)
    _edited(conn, v.matched_id, ["t", "behavioural", "done"])
    row = conn.execute("SELECT topic, kind FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()
    check(row["topic"] == "behavioural" and row["kind"] == "behavioural",
          f"retopiced to behavioural and stayed {row['kind']}")

    # A market-awareness question is the exception: its kind says how the
    # answer is graded (against a live number, not a rubric), which no topic
    # implies and no retopic may quietly take away.
    conn.execute("UPDATE questions SET kind = 'market_awareness' WHERE id = ?",
                 (v.matched_id,))
    _edited(conn, v.matched_id, ["t", "markets", "done"])
    check(conn.execute("SELECT kind FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()["kind"] == "market_awareness",
          "a retopic demoted a market question to a technical")

    # And a topic that is not one of the twelve is refused rather than stored:
    # `selftest` fails on a row naming a topic `topics.py` has never heard of,
    # so a typo at this prompt broke the suite from the database.
    _edited(conn, v.matched_id, ["t", "acounting", "done"])
    check(conn.execute("SELECT topic FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()["topic"] == "markets",
          "a misspelled topic was written into the bank")


def test_structuring_one_question_takes_exactly_one_call() -> None:
    """`run` and `draft_missing_answers` are two batch passes gated by two
    different scans, so doing this with them is two sequential round trips on
    a rate-limited free tier -- for one question, typed at a prompt, with the
    interview still fresh."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="how does D and A flow through the statements")
    calls = []
    fake, seen = _fake_structure(_GOOD_ITEM)
    real = enrich.llm.generate
    enrich.llm.generate = lambda p, **kw: (calls.append(1) or fake(p, **kw))
    try:
        done = enrich.structure_one(conn, v.matched_id,
                                    "how does D and A flow through the statements",
                                    progress=lambda _m: None)
    finally:
        enrich.llm.generate = real

    check(done == 1, f"structure_one reported {done}")
    check(len(calls) == 1, f"one question cost {len(calls)} calls")
    check(seen["thinking"] == llm.THINKING_BULK,
          "a fixed-schema call was billed at the default thinking level")
    check(seen["caller"] == "add_llm", f"logged as caller {seen['caller']!r}")
    check(seen["schema"] is enrich.STRUCTURE_SCHEMA, "the call was not schema-constrained")

    row = conn.execute(
        "SELECT q.canonical_text, q.topic, q.difficulty, a.answer_key, "
        "a.rubric_points FROM questions q JOIN answers a ON a.question_id = q.id "
        "WHERE q.id = ?", (v.matched_id,)).fetchone()
    check(row["topic"] == "accounting", f"topic came out {row['topic']}")
    check(row["difficulty"] == 2, "difficulty was not applied")
    check("EBIT by $10" in row["answer_key"], "the answer was not stored")
    check(len(json.loads(row["rubric_points"])) == 3, "the rubric was not stored")
    check(set(tagging.tags_for(conn, v.matched_id)) >= {"three-statement-integration"},
          "the tags were not attached")


def test_the_two_modes_differ_only_in_whose_answer_it_is() -> None:
    """With nothing to go on it writes from consensus. With your rough notes
    it has to polish them and may not replace them -- what you actually said
    in the room is the part worth keeping."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="term loan A versus term loan B")
    real = enrich.llm.generate

    fake, cold = _fake_structure(_GOOD_ITEM)
    enrich.llm.generate = fake
    try:
        enrich.structure_one(conn, v.matched_id, "term loan A versus term loan B",
                             progress=lambda _m: None)
        cold_prompt = cold["prompt"]

        fake2, warm = _fake_structure(_GOOD_ITEM)
        enrich.llm.generate = fake2
        enrich.structure_one(conn, v.matched_id, "term loan A versus term loan B",
                             rough_answer="TLA amortises, banks buy it",
                             progress=lambda _m: None)
        warm_prompt = warm["prompt"]
    finally:
        enrich.llm.generate = real

    # Flattened, because both rules are wrapped source strings and every
    # phrase worth asserting on straddles a line break in one of them.
    cold_flat = " ".join(cold_prompt.split())
    warm_flat = " ".join(warm_prompt.split())
    check("standard IB interview consensus" in cold_flat,
          "with no notes it was not told to fall back on consensus")
    check("TLA amortises, banks buy it" in warm_flat,
          "the rough answer was not put in front of the model")
    check("preserve what they actually said" in warm_flat,
          "nothing told it to keep what was said")
    check("consensus" not in warm_flat,
          "with notes in hand it was still told to write from consensus")
    check("CANDIDATE'S ROUGH ANSWER" not in cold_flat,
          "the no-notes prompt carried a notes section anyway")


def test_a_drafted_answer_with_a_proven_error_is_never_stored_by_add() -> None:
    """Model-authored, with no source to check it against, and on the one path
    where you are most likely to go and revise it straight away. It gets the
    same mechanical gate `draft_missing_answers` uses."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="how do you get to enterprise value")
    broken = dict(_GOOD_ITEM,
                  answer_key="Enterprise Value equals Equity Value minus Debt "
                             "plus Cash, so 100 + 50 = 200.")
    said = []
    fake, _seen = _fake_structure(broken)
    real = enrich.llm.generate
    enrich.llm.generate = fake
    try:
        done = enrich.structure_one(conn, v.matched_id,
                                    "how do you get to enterprise value",
                                    progress=said.append)
    finally:
        enrich.llm.generate = real

    check(done == 0, "a draft with a mechanical error was applied")
    check(any("mechanical error" in m for m in said),
          f"nothing said why it was refused: {said}")
    row = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                       (v.matched_id,)).fetchone()
    check(not (row["answer_key"] or "").strip(),
          "the refused answer was stored anyway")


def test_a_structured_add_leaves_exactly_one_thing_to_undo() -> None:
    """The rubric and the answer are one decision, so they are one row. Two
    `set_answer` calls put a no-op line in the middle of the undo preview --
    "answer X back to X" -- for a change nobody made."""
    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="what is a covenant",
              answer_text="promises in the loan doc")
    fake, _seen = _fake_structure(_GOOD_ITEM)
    real = enrich.llm.generate
    enrich.llm.generate = fake
    try:
        enrich.structure_one(conn, v.matched_id, "what is a covenant",
                             rough_answer="promises in the loan doc",
                             progress=lambda _m: None)
    finally:
        enrich.llm.generate = real

    rows = [r for r in conn.execute(
        "SELECT old_answer_key, new_answer_key FROM answer_history "
        "WHERE question_id = ?", (v.matched_id,))]
    check(len(rows) == 1, f"one structuring wrote {len(rows)} history rows")
    check(rows[0]["old_answer_key"] == "promises in the loan doc",
          "your own notes were not what the undo goes back to")
    check("EBIT by $10" in rows[0]["new_answer_key"], "the new answer was not recorded")

    # And `enrich` itself must still never invent an answer over one on file.
    enrich._apply(conn, v.matched_id, _GOOD_ITEM, batch_id=history.new_batch())
    kept = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                        (v.matched_id,)).fetchone()["answer_key"]
    check("EBIT by $10" in kept,
          "a plain enrich overwrote the answer that was already there")


def test_an_llm_add_never_skips_the_review_queue() -> None:
    """`add --answer` goes straight to active because a human typed it. A
    model's draft, or a model's polish of your notes, has not earned that --
    it takes the normal audit and review pass like everything else a model
    wrote."""
    from .cli import build_parser
    p = build_parser()
    check(p.parse_args(["add", "-l", "x"]).llm, "-l does not turn the call on")
    check(not p.parse_args(["add", "x"]).llm, "add makes a network call by default")

    conn, a, _ = fresh()
    v = admit(conn, source_id=a, question_text="what is a covenant",
              answer_text="promises in the loan doc", status="needs_review")
    check(conn.execute("SELECT status FROM questions WHERE id = ?",
                       (v.matched_id,)).fetchone()[0] == "needs_review",
          "an LLM-assisted add landed active")


def test_add_without_the_flag_still_needs_no_api_key() -> None:
    """`add` has always worked with no key at all, and a new flag on it does
    not get to change that -- the whole gate is lexical."""
    conn, a, _ = fresh()
    calls = []
    real = enrich.llm.generate
    enrich.llm.generate = lambda *a_, **k: calls.append(1)
    try:
        v = admit(conn, source_id=a, question_text="what is EBITDA and why is it used",
                  answer_text=None)
    finally:
        enrich.llm.generate = real
    check(v.kind == "new" and not calls, "a plain add reached for the provider")


# ---------------------------------------------------------------------------
# Publishing. This repo is public and the working tree is not; the gitignore is
# the only thing between them. Each case below is a leak that a plausible edit
# reintroduces silently, which is exactly the kind that ships.


def _shipped_packs() -> list[tuple[str, dict]]:
    d = Path(__file__).resolve().parent / "packs"
    packs = [(p.name, json.loads(p.read_text(encoding="utf-8")))
             for p in sorted(d.glob("*.json"))]
    # The packs moved inside the package so a wheel would carry them, and this
    # kept reading the builder directory next to it -- which holds `_build_*.py`
    # and no JSON at all. Every guard below it passed on an empty list for as
    # long as that was true, which is the worst way for a guard to fail.
    check(packs, "no shipped packs found: this guard is reading the wrong directory")
    return packs


def test_no_shipped_pack_carries_someone_s_biography() -> None:
    """The DCM pack began as a bilingual handbook built around one person: a
    CV in the answer to "tell me about yourself", a named employer, a named
    university and a class rank. Twenty fit questions whose answers are one
    biography are not a question bank, they are that person's script -- and a
    rubric over them grades whether you are him.

    The check is the shape, never a list of the names to avoid: a denylist of
    real hometowns and employers would publish the biography inside the guard
    meant to keep it out, and would only ever catch the one person it was
    written against.

    It used to stand on "a shipped pack has no fit questions at all", which
    held for as long as no pack had any. Fit questions ship now -- what a
    third-party guide says about the *shape* of an answer travels perfectly
    well -- so the test is the voice instead: an answer speaking as the
    candidate is that candidate's story, and a rubric over it marks the reader
    down for not being him. The pronoun is the tell rather than the detail,
    because swapping the anecdote changes nothing about whose answer it is.
    """
    import re
    autobiography = re.compile(
        r"\bI'm from\b|\bI am from\b|\bI was born\b|\bI grew up\b|"
        r"\bmy (bachelor|master|thesis|degree|internship|CV|r[ée]sum[ée])\b|"
        r"\bI studied at\b|\bI finished my\b|\bIch komme aus\b", re.I)
    first_person = re.compile(r"\b(I|I'm|I've|my|me|mine)\b")
    for name, pack in _shipped_packs():
        for item in pack["items"]:
            if item.get("topic") != "behavioural":
                continue
            m = first_person.search(item.get("a") or "")
            if m:
                check(False, f"{name} answers a fit question in the first person "
                             f"({m.group(0)!r}), which ships one candidate's story: "
                             f"{item['q'][:50]!r}")
        for item in pack["items"]:
            m = autobiography.search(item.get("a") or "")
            # The message is built only on the failing branch: an f-string
            # argument is evaluated before `check` is called, so reading
            # m.group(0) inline raises AttributeError on every passing item.
            if m:
                check(False, f"{name} answer introduces a person: "
                             f"{m.group(0)!r} in {item['q'][:50]!r}")


def test_no_shipped_pack_reproduces_its_source_verbatim() -> None:
    """`verbatim` held up to 600 characters of the source handbook's own
    wording per item. Nothing downstream reads it once a pack has landed, and
    123 items of it was the one field in this repo that republished a
    third-party document rather than describing it."""
    for name, pack in _shipped_packs():
        n = sum(1 for i in pack["items"] if i.get("verbatim"))
        check(n == 0, f"{name} carries {n} verbatim source excerpts")
        check(pack.get("note"), f"{name} has no note saying what it is grounded in")


def test_the_sec_contact_does_not_default_to_a_real_address() -> None:
    """It shipped as the author's own email. Every user's EDGAR traffic would
    have been signed with it -- which misidentifies the client to the SEC and
    hands one person the rate limiting and the abuse mail for everybody. The
    guard already existed; the default was what defeated it."""
    from .config import DEFAULTS
    check(DEFAULTS["sec_contact"] == "",
          f"a contact address ships as the default: {DEFAULTS['sec_contact']!r}")


def test_the_gemini_key_never_travels_in_a_url() -> None:
    """It used to go in the query string as `?key=`. A URL is the part of a
    request that gets logged -- by proxies, by TLS inspection -- and
    `HTTPError` carries the URL it failed on, so a key there is one pasted
    traceback away from being public."""
    # Match the interpolating form the code would actually use, not the bare
    # string: the comment above `_auth_headers` explains the old shape and
    # names it, and a check that reads prose fails on its own explanation.
    src = (Path(__file__).resolve().parent / "llm.py").read_text()
    check("?key={" not in src, "llm.py still interpolates the API key into a URL")

    # load_env is memoised and uses setdefault, so an env value set here wins
    # over whatever .env.local holds and no real key is ever read.
    saved = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "test-key-not-real"
    try:
        h = llm._auth_headers()
    finally:
        if saved is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved
    check(h.get("x-goog-api-key") == "test-key-not-real",
          f"the key is not sent as a header: {sorted(h)}")


def test_writing_a_key_leaves_the_file_unreadable_to_others() -> None:
    """.env.local is the one file in the repo that holds a secret, and it was
    written with whatever the umask happened to be."""
    import stat
    from . import cli as cli_mod
    tmp = Path(tempfile.mkdtemp())
    saved = os.environ.get("SUPERDAY_HOME")
    os.environ["SUPERDAY_HOME"] = str(tmp)
    try:
        cli_mod._env_file_set("GEMINI_API_KEY", "test-key-not-real")
        mode = stat.S_IMODE((tmp / ".env.local").stat().st_mode)
    finally:
        _restore_home(saved)
        os.environ.pop("GEMINI_API_KEY", None)
    check(mode == 0o600, f".env.local was written {oct(mode)}, not 0o600")


def test_every_setting_the_tool_asks_for_can_be_set_through_settings() -> None:
    """`cross-audit --api` asked for ANTHROPIC_API_KEY and `settings` had no
    entry for it, so the only way to supply it was to hand-edit .env.local --
    which is what `settings` exists to stop, and which loses the 0600."""
    from .cli import SETTINGS
    keys = {e.get("env") for e in SETTINGS}
    for env in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        check(env in keys, f"{env} cannot be set through `settings`")
    for e in SETTINGS:
        if e.get("env", "").endswith("_API_KEY"):
            check(e["kind"] == "secret", f"{e['key']} is not masked on screen")


def test_a_highlighted_search_hit_reaches_a_pipe_as_plain_text() -> None:
    """`find wacc | less` arrived with \033[0m welded onto any row short
    enough to survive truncation. _headline built its line from BOLD and
    RESET, which are raw constants rather than palette lookups, so unlike
    paint() they emit at every depth -- and truncate() threw the trailing
    reset away only on rows long enough to cut, which is why the bug looked
    like it affected two rows out of twenty rather than all of them.

    The existing pipe test walked the ui helpers and missed this because the
    leak is in a caller that concatenates the constants itself.
    """
    conn, a, _ = fresh()
    qid = _seed(conn, a, "What does WACC mean intuitively?")
    conn.execute("UPDATE questions SET status = 'active' WHERE id = ?", (qid,))
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, canonical_text, topic, difficulty, status FROM questions")]

    ui.reset_depth()
    try:
        ui._DEPTH = 0
        v = views.ResultsView(conn, rows, title="FIND", highlight=["wacc"])
        # Wide enough that nothing truncates: truncation is what used to hide
        # the leak, so a narrow pane would test the wrong thing.
        for line in v.flatten(200):
            check("\x1b" not in line, f"escape leaked into a pipe: {line!r}")
    finally:
        ui.reset_depth()


def test_the_help_screen_only_advertises_keys_a_view_actually_gets() -> None:
    """The KEYS block said ⇥ cycled the sort. Plain tab belongs to the input
    line's completion menu and is not in Shell._VIEW_KEYS, so it never reaches
    a list at all -- the sort has always been on ⇧⇥. A keymap is the one piece
    of documentation a user tries by pressing it, so a wrong row there costs
    more than a wrong row anywhere else."""
    from .cli import KEYMAP
    from .tui import Shell
    glyphs = {"↑ ↓": ("up", "down"), "⏎": ("enter",), "→ ←": ("right", "left"),
              "PgUp PgDn": ("pgup", "pgdn"), "⇧⇥": ("btab",)}
    advertised = {k for k, _ in KEYMAP}
    for glyph, names in glyphs.items():
        check(glyph in advertised,
              f"the keymap no longer advertises {glyph} - update this test with it")
        for n in names:
            check(n in Shell._VIEW_KEYS,
                  f"KEYS advertises {glyph}, but {n!r} never reaches a view")
    check("⇥" not in advertised,
          "plain tab is advertised again; it is the completion menu's, not a list's")
    # ⌥g and ⌥a are switch rows on the do-screen now. They stay bound for
    # terminals that pass Alt through, but advertising a chord that a window
    # manager may swallow is what put a feature out of reach in the first
    # place -- and every other accelerator in here is already unadvertised.
    for chord in ("⌥g", "⌥a"):
        check(chord not in advertised,
              f"{chord} is advertised again; it is a do-screen switch now")


def test_one_answer_to_having_no_api_key() -> None:
    """enrich, audit and reground each carried their own copy of the sentence,
    and all of them pointed at hand-editing .env.local -- the one route that
    skips validation and loses the 0600 the writer applies. ingest-filing
    already pointed at `settings`, so the tool gave two different answers to
    the same question depending on which command you reached for."""
    src = (Path(__file__).resolve().parent / "cli.py").read_text()
    check("GEMINI_API_KEY in superday/.env.local" not in src,
          "a command still tells you to hand-edit .env.local for the key")
    check(src.count("def _needs_key(") == 1, "there is more than one _needs_key")

    import io, contextlib
    from . import cli as cli_mod
    saved = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = ""
    llm.load_env(force=True)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            blocked = cli_mod._needs_key("enrich", "why it matters")
        out = buf.getvalue()
    finally:
        if saved is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved
        llm.load_env(force=True)
    check(blocked, "_needs_key did not block with no key configured")
    check("settings gemini_api_key" in out,
          f"the message does not point at the safe route: {out!r}")


def test_a_truncated_setting_says_that_it_was_truncated() -> None:
    """The settings table cut its value column with `disp[:28]`, so a long
    corpus_dir arrived as "/Users/me/Desktop/I" with nothing to mark the cut.
    Every other list in the tool truncates through ui.truncate and shows the
    ellipsis, and this is the one screen whose entire job is telling you what
    a setting is currently set to.

    The check renders the real page rather than reading the source for a
    slice: the comment explaining the old form names it, and a guard that
    greps prose fails on its own explanation.
    """
    import io, contextlib
    from . import cli as cli_mod

    long = "/Users/somebody/Documents/a/very/deep/corpus/directory/somewhere"
    _, saved = _with_config(corpus_dir=long)
    ui.reset_depth()
    try:
        ui._DEPTH = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli_mod._settings_page()
        out = buf.getvalue()
    finally:
        _restore_home(saved)
        ui.reset_depth()

    row = next((l for l in out.splitlines() if "corpus_dir" in l), "")
    check(row, "corpus_dir is no longer on the settings page")
    check(long not in row, "the value column did not clamp a long path at all")
    check("…" in row, f"a cut value carried no ellipsis: {row!r}")


def test_an_unset_setting_says_so_rather_than_showing_a_blank() -> None:
    """sec_contact and interview_date are empty by default, and an empty cell
    in a configuration table reads as a broken renderer. The env-backed half
    of _settings_value already answered this with the words "not set"."""
    from .cli import _settings_value
    entry = dict(key="sec_contact", store="file", kind="str", group="corpus", help="")
    disp, source = _settings_value(entry, {})
    check(disp == "not set", f"an unset setting displayed as {disp!r}")
    check(source == "default", f"an unset setting came from {source!r}")

    # A real 0 is a value, not an absence: rate_limit_rpm ships 0 on purpose
    # and `usage` reads it as "no limit configured", so it must not be reworded.
    rl = dict(key="rate_limit_rpm", store="file", kind="int", group="llm", help="")
    disp, _ = _settings_value(rl, {})
    check(disp == "0", f"a deliberate 0 was reworded to {disp!r}")


def _results_view(conn, a, n=3):
    rows = []
    for i in range(n):
        qid = _seed(conn, a, f"Walk me through question number {i} about valuation")
        conn.execute("UPDATE questions SET status='active' WHERE id=?", (qid,))
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, canonical_text, topic, difficulty, status FROM questions")]
    return views.ResultsView(conn, rows, title="FIND")


def test_grouping_and_expand_all_are_reachable_without_a_modifier() -> None:
    """Both were bound only to an alt- chord, and the Help screen advertised
    them. Alt is swallowed by the terminal emulator or the window manager on
    plenty of machines -- macOS Terminal does not send it as Meta by default --
    so those two were not one keystroke away, they were unreachable, which is
    the exact failure the no-modifier rule exists to prevent.

    They are switch rows on the do-screen now: `←` opens it, arrows move,
    `⏎` flips. Same route every other list-wide thing takes."""
    conn, a, _ = fresh()
    v = _results_view(conn, a)
    v.handle(_Keys("left"), None)                 # open the do-screen
    doing = v._doing
    check(doing is not None, "`←` did not open the do-screen at all")
    labels = [act.label for act in doing.acts]
    check(any("group" in l for l in labels),
          f"grouping is not offered without a chord: {labels}")
    check(any("expand every row" in l for l in labels),
          f"expand-all is not offered without a chord: {labels}")

    idx = next(i for i, act in enumerate(doing.acts) if "group" in act.label)
    doing.sel = idx
    was = v.group
    doing.activate(idx, None)
    check(v.group is not was, "the switch row did not change the list behind it")
    check(v.sort == "topic", "grouping did not pull the sort with it")


def test_a_switch_row_runs_nothing_and_needs_no_shell() -> None:
    """A switch is not a command: it has no line to run, and `fire` used to
    hand `act.line` to `shell.run_now` unconditionally. With no shell -- which
    is every test and every pipe -- that silently did nothing at all, and with
    one it would have tried to run the empty string."""
    conn, a, _ = fresh()
    v = _results_view(conn, a)
    flipped = []
    act = views.Action(key="x", label="flip it", do=lambda: flipped.append(1), mark="⇄")
    v._armed = None
    views.fire(v, act, None)
    check(flipped == [1], "a switch did not run with no shell attached")
    check(act.line == "", "a switch carries a command line it should not have")

    drawn = views.action_row(act.label, True, mark=act.mark, width=40)
    check("⇄" in ui.strip(drawn), f"a switch was drawn as a command: {ui.strip(drawn)!r}")


def test_expand_all_is_not_offered_where_nothing_expands() -> None:
    """The do-screen is per-row and a switch that cannot do anything is worse
    than no switch: it reads as broken. ActionsView itself expands nothing, so
    it must not offer to expand everything."""
    doing = views.ActionsView(title="T", subject="s",
                              actions=[views.Action(key="a", label="do it", line="stats")])
    check(doing.switches() == [], "the do-screen offered to expand itself")


# ---------------------------------------------------------------------------
# Provider agnosticism. The tool used to be Gemini with a Claude side door;
# every case here is a way that could quietly come back.


def _capture(payload: dict):
    """A fake transport that records the request it was handed."""
    sent = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()

    def opener(req, **kw):
        sent["url"] = req.full_url
        sent["headers"] = {k.lower(): v for k, v in req.headers.items()}
        sent["body"] = json.loads(req.data.decode())
        return R()

    return opener, sent


def _with_provider(name: str, keys: dict):
    """Point llm at one provider with one set of keys, and put it all back."""
    saved = {k: os.environ.get(k) for k in
             ("IB_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    os.environ["IB_PROVIDER"] = name
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)
    os.environ.update(keys)
    llm.load_env(force=True)
    return saved


def _restore(saved: dict):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    llm.load_env(force=True)


def test_the_provider_setting_moves_every_model_default_with_it() -> None:
    """Switching provider has to move the whole set. A half-switched tool asks
    Claude for `gemini-3.5-flash` and 404s on every call."""
    for name, spec in llm.PROVIDERS.items():
        saved = _with_provider(name, {spec["key"]: "k"})
        try:
            check(llm.provider() == name, f"{name} did not take")
            for job, accessor in (("enrich", llm.model_enrich),
                                  ("grade", llm.model_grade),
                                  ("audit", llm.model_audit),
                                  ("embed", llm.model_embed)):
                check(accessor() == spec["models"][job],
                      f"{name}/{job} resolved to {accessor()!r}")
            check(llm.available(), f"{name} reported no key with one set")
        finally:
            _restore(saved)


def test_a_typo_in_the_provider_costs_the_setting_not_the_tool() -> None:
    """Same rule `thinking_level` follows. An unknown provider name would
    otherwise KeyError out of every accessor, including the ones the banner
    calls before you have run anything."""
    saved = _with_provider("gemni", {"GEMINI_API_KEY": "k"})
    try:
        check(llm.provider() == llm.DEFAULT_PROVIDER,
              f"a typo resolved to {llm.provider()!r}")
        check(llm.model_grade(), "no model came back for a mistyped provider")
    finally:
        _restore(saved)


def test_each_provider_is_asked_for_structure_its_own_way() -> None:
    """One schema in, three request shapes out. This is the whole reason the
    dispatch exists: a caller names a job and never learns which vendor
    answered, so the vendor-shaped part has to be entirely inside llm.py."""
    schema = {"type": "object",
              "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
              "required": ["a"]}
    real = llm.urllib.request.urlopen

    # Gemini: responseSchema, key in a header.
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g"})
    try:
        opener, sent = _capture({"candidates": [{"content": {"parts": [
            {"text": '{"a":"x"}'}]}}]})
        llm.urllib.request.urlopen = opener
        out = llm.generate("p", schema=schema, model="m", caller="t")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    check(out == {"a": "x"}, f"gemini returned {out!r}")
    check(sent["body"]["generationConfig"]["responseSchema"] == schema,
          "gemini was not sent the schema")
    check(sent["headers"].get("X-goog-api-key".lower()) == "g",
          f"gemini key not in its header: {sorted(sent['headers'])}")

    # Claude: one forced tool.
    saved = _with_provider("claude", {"ANTHROPIC_API_KEY": "c"})
    try:
        opener, sent = _capture({"content": [{"type": "tool_use",
                                              "input": {"a": "y"}}],
                                 "stop_reason": "tool_use"})
        llm.urllib.request.urlopen = opener
        out = llm.generate("p", schema=schema, model="m", caller="t")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    check(out == {"a": "y"}, f"claude returned {out!r}")
    check(sent["body"]["tool_choice"]["name"] == llm.TOOL_NAME,
          "claude was not pinned to one tool")
    check(sent["body"]["tools"][0]["input_schema"] == schema,
          "claude's tool did not carry the schema")
    check(sent["headers"].get("x-api-key") == "c", "claude key not in its header")

    # OpenAI: strict json_schema, and the schema rewritten to satisfy it.
    saved = _with_provider("openai", {"OPENAI_API_KEY": "o"})
    try:
        opener, sent = _capture({"choices": [{"finish_reason": "stop",
                                              "message": {"content": '{"a":"z","b":null}'}}]})
        llm.urllib.request.urlopen = opener
        out = llm.generate("p", schema=schema, model="gpt-5", caller="t")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    check(out == {"a": "z"}, f"openai kept a null it should have dropped: {out!r}")
    js = sent["body"]["response_format"]["json_schema"]
    check(js["strict"] is True, "openai was not asked for strict mode")
    check(sent["headers"].get("authorization") == "Bearer o",
          "openai key not in its header")


def test_strict_mode_does_not_turn_optional_fields_into_required_ones() -> None:
    """OpenAI's strict mode refuses a schema unless every property is in
    `required`. Taken literally that makes every optional field mandatory --
    `corrected_answer` is only meant to appear on a fix, and demanding it on
    every verdict would have the model invent one. Widening to accept null is
    the way to say "you may decline", and `_drop_nulls` turns a declined field
    back into the absent key every caller already handles."""
    schema = {"type": "object",
              "properties": {"verdict": {"type": "string"},
                             "corrected_answer": {"type": "string"}},
              "required": ["verdict"]}
    out = llm.strict_schema(schema)
    check(out["additionalProperties"] is False, "the object was left open")
    check(sorted(out["required"]) == ["corrected_answer", "verdict"],
          f"strict mode needs every key required: {out['required']}")
    check(out["properties"]["corrected_answer"]["type"] == ["string", "null"],
          "an optional field was made mandatory rather than nullable")
    check(out["properties"]["verdict"]["type"] == "string",
          "a genuinely required field was widened to accept null")
    check(llm._drop_nulls({"verdict": "keep", "corrected_answer": None})
          == {"verdict": "keep"}, "a declined field did not come back absent")


def test_a_provider_with_no_embeddings_says_so_instead_of_pretending() -> None:
    """Anthropic sells no embeddings endpoint. `find --semantic` has always
    fallen back to the lexical search, which is right -- but it still printed
    SEMANTIC over keyword hits, so a search that had stopped being semantic
    looked exactly like one that had not."""
    saved = _with_provider("claude", {"ANTHROPIC_API_KEY": "c"})
    try:
        check(not llm.embeds(), "Claude claimed an embeddings endpoint")
        why = search.semantic_ready()
        check("no embeddings endpoint" in why, f"nothing explained itself: {why!r}")
        try:
            llm.embed_batch(["x"])
            check(False, "embed_batch pretended to embed with no endpoint")
        except llm.LLMError as e:
            check("embeddings" in str(e), f"unclear refusal: {e}")
    finally:
        _restore(saved)

    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g"})
    try:
        check(llm.embeds(), "Gemini lost its embeddings endpoint")
        check(search.semantic_ready() == "",
              "a working provider was told it could not do semantic search")
    finally:
        _restore(saved)


def test_the_first_audit_is_found_whichever_provider_ran_it() -> None:
    """`audits` tells a first opinion from a second by the provider name: the
    first is bare (`gemini`), the second namespaced (`claude-code`). While the
    first was always Gemini the queries could hardcode it; now that `audit`
    runs on whatever is configured, a hardcoded join reports every question
    audited by OpenAI as having no first opinion to disagree with."""
    for name in llm.PRIMARY_PROVIDERS:
        check(f"'{name}'" in llm.PRIMARY_SQL, f"{name} missing from PRIMARY_SQL")
    for ns in llm.SECOND_PROVIDERS:
        check(ns not in llm.PRIMARY_PROVIDERS,
              f"{ns} would be read as a first opinion as well as a second")
    check(llm.PRIMARY_SQL in crossaudit._PAIRS,
          "the disagreement join still names one provider")


# ---------------------------------------------------------------- colour

def test_every_theme_clears_its_own_contrast_floors() -> None:
    """Colour was decided by eye, against a background the tool never had.

    The palette was tuned to sit above near-black while the shell painted no
    background at all, so on a terminal running any transparency it was drawn
    on whatever was behind the window. Measured against what was actually on
    screen, `faint` came out at 1.04:1 -- not hard to read, the background.

    `theme.FLOORS` is the contract that replaced the eye, and this is what
    holds every shipped theme to it. A ported theme is the case that needs it
    most: the upstream hexes are an editor's, and an editor colours a keyword
    inside a line the eye is already resting on rather than a table column
    read at a glance.
    """
    for name, t in theme_mod.THEMES.items():
        check(t.bg is not None, f"{name} inherits the terminal's background")
        for token, floor in theme_mod.FLOORS.items():
            got = theme_mod.contrast(getattr(t, token), t.bg)
            check(got >= floor,
                  f"{name}.{token} is {got:.2f}:1, under its {floor}:1 floor")


def test_a_hovered_row_lifts_off_the_ground_without_becoming_a_second_cursor() -> None:
    """`hover` used to be one hex for every background there might be, and it
    was darker than most of them. On the Ghostty default it landed at 1.00:1 --
    the mouse wash was drawn and could not be seen at all.

    It is bounded on both sides on purpose: too little and there is no wash,
    too much and it competes with the cursor bar, which is the thing that says
    what a keystroke would actually take.
    """
    lo, hi = theme_mod.HOVER_LIFT
    for name, t in theme_mod.THEMES.items():
        got = theme_mod.contrast(t.hover, t.bg)
        check(lo <= got <= hi,
              f"{name} hover is {got:.2f}:1 off its ground, outside {lo}-{hi}")


def test_the_quiet_end_is_not_dimmed_twice() -> None:
    """`dim()` and `faint()` used to pass SGR 2 on top of a palette colour
    that was already the darkening. A terminal implements that attribute as a
    blend toward the background, so every quiet thing on screen rendered at
    roughly half the contrast it had been designed for -- and the floors above
    would have passed while the screen stayed unreadable.

    The attribute is still right at depth 1, where there is no hex to be quiet
    with, so this asks about the depths that have one.
    """
    ui.reset_depth()
    try:
        for d in (2, 3):
            ui._DEPTH = d
            for fn in (ui.dim, ui.faint):
                out = fn("x")
                check(ui.DIM not in out,
                      f"{fn.__name__} still emits SGR 2 at depth {d}: {out!r}")
        # depth() returns 0 whenever colours are off, so depth 1 always
        # implies a tty. Forcing the depth alone builds a state that cannot
        # occur, and `style` would answer for the pipe the suite runs on.
        ui._DEPTH = 1
        was, ui.colors_enabled = ui.colors_enabled, lambda: True
        try:
            check(ui.DIM in ui.faint("x"),
                  "at depth 1 the attribute is the only quiet there is")
        finally:
            ui.colors_enabled = was
    finally:
        ui.reset_depth()


def test_a_chip_knocks_out_of_the_theme_rather_than_a_hardcoded_black() -> None:
    """The last raw SGR in a render path. Colour 16 is whatever the terminal's
    own palette says it is rather than reliably black, and on a light theme
    black-on-accent is the one combination that cannot work."""
    ui.reset_depth()
    try:
        ui._DEPTH = 3
        for name in ("superday", "github-light", "solarized-light", "dracula"):
            t = ui.set_theme(name)
            out = ui.chip("resume")
            check("38;5;16m" not in out, f"{name} chip still hardcodes colour 16")
            check(theme_mod.contrast(t.bg, t.accent) >= 4.0,
                  f"{name} chip text is unreadable on its own accent")
    finally:
        ui.set_theme(theme_mod.DEFAULT)
        ui.reset_depth()


def test_the_backdrop_survives_a_rows_own_resets_and_stays_under_the_hover() -> None:
    """Two layers of background on one row, and the order is the whole point.

    `ground` re-emits after a *reset* rather than after every escape, because
    a hovered row arrives already washed and `wash` re-emits its own colour
    straight after each reset. Insert the backdrop at the same point and it
    lands before the hover's, so the nearer one wins and the hover survives.
    Re-emit after every escape instead and the backdrop paints over the
    highlight it is supposed to sit beneath.
    """
    ui.reset_depth()
    try:
        ui._DEPTH = 3
        t = ui.set_theme("superday")
        row = ui.accent("#45") + " " + ui.faint("accounting")
        bg = ui.colour("ground", bg=True)

        plain = ui.ground(row, 40)
        check(plain.startswith(bg), "the backdrop does not open the row")
        check(plain.count(bg) > 1, "the backdrop stops at the row's first reset")
        check(ui.strip(plain).rstrip() == "#45 accounting",
              f"grounding changed the text: {ui.strip(plain)!r}")
        check(ui.vlen(plain) == 40, f"grounded row is {ui.vlen(plain)} cells, not 40")

        hovered = ui.ground(ui.wash(row, "hover", 40), 40)
        hov = ui.colour("hover", bg=True)
        # After every reset the hover has to be the last background named --
        # asked of the runs that actually carry text. The tail after the
        # wash's own closing reset re-emits the backdrop with nothing left to
        # draw on it, which is the one run where no hover is the right answer.
        for chunk in hovered.split(ui.RESET):
            if not ui.strip(chunk):
                continue
            check(chunk.find(hov) > chunk.find(bg),
                  f"the backdrop was painted over the hover wash: {chunk!r}")
        check(ui.vlen(hovered) == 40, "a hovered row is not the frame's width")
    finally:
        ui.set_theme(theme_mod.DEFAULT)
        ui.reset_depth()


def test_the_theme_setting_is_offered_and_takes_effect() -> None:
    """A theme you cannot reach from `settings` is a theme nobody finds, and
    one that needs a relaunch is one nobody tries more than once."""
    from .cli import SETTINGS
    entry = [e for e in SETTINGS if e["key"] == "theme"]
    check(entry, "no theme row in SETTINGS")
    check(set(entry[0]["choices"]) == set(theme_mod.THEMES),
          "the settings row and the theme table disagree about what exists")

    ui.reset_depth()
    try:
        ui._DEPTH = 3
        before = ui.colour("accent")
        ui.set_theme("dracula")
        check(ui.active().name == "dracula", "set_theme did not switch")
        check(ui.colour("accent") != before, "the palette did not follow the theme")
    finally:
        ui.set_theme(theme_mod.DEFAULT)
        ui.reset_depth()


# ---------------------------------------------------------------------------
# Swapping provider. Every case here is a way the tool used to half-switch:
# the setting moved and something behind it did not.


def test_a_pinned_call_never_carries_another_vendors_key() -> None:
    """`generate(using=...)` is how `cross-audit` gets a second opinion from
    somebody other than whoever wrote the answer. The dispatch resolved the
    provider and then each transport re-read the *setting* for its key and its
    default model -- so with `llm_provider` on claude, a call pinned to Gemini
    went to Google's endpoint asking for `claude-sonnet-5`, with the Anthropic
    key in the header. A 404 is the harmless half of that: the other half is
    one vendor's secret sent to another vendor's host."""
    real = llm.urllib.request.urlopen
    saved = _with_provider("claude", {"ANTHROPIC_API_KEY": "anthropic-secret",
                                      "GEMINI_API_KEY": "google-secret"})
    try:
        opener, sent = _capture({"candidates": [{"content": {"parts": [
            {"text": "hi"}]}}]})
        llm.urllib.request.urlopen = opener
        llm.generate("p", using="gemini", caller="t")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    check("generativelanguage.googleapis.com" in sent["url"],
          f"a call pinned to Gemini went to {sent['url']}")
    check(sent["headers"].get("x-goog-api-key") == "google-secret",
          "the pinned provider's own key was not the one sent")
    check("x-api-key" not in sent["headers"],
          f"another vendor's key travelled with it: {sorted(sent['headers'])}")
    check("claude" not in sent["url"],
          f"the model came from the setting, not from the pinned provider: {sent['url']}")


def test_a_model_override_belongs_to_the_provider_it_was_set_for() -> None:
    """One shared `IB_MODEL_ENRICH` meant a model name outliving the vendor
    that sold it: set `gemini-3.5-flash`, switch to Claude, and every enrich
    call 404s on a model Anthropic has never heard of -- with nothing on
    screen connecting the failure to a setting made days earlier. Stored per
    provider, each vendor remembers its own."""
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "c"})
    keys = ("IB_MODEL_ENRICH", "IB_MODEL_ENRICH_GEMINI", "IB_MODEL_ENRICH_CLAUDE")
    prior = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ["IB_MODEL_ENRICH_GEMINI"] = "gemini-3.1-pro"
        check(llm.model_enrich() == "gemini-3.1-pro", "the override did not take")
        os.environ["IB_PROVIDER"] = "claude"
        check(llm.model_enrich() == llm.default_model("enrich", "claude"),
              f"Gemini's model followed the switch: {llm.model_enrich()}")
        os.environ["IB_PROVIDER"] = "gemini"
        check(llm.model_enrich() == "gemini-3.1-pro",
              "switching away and back lost the override")

        # The older shared spelling still works -- for the vendor it names.
        os.environ.pop("IB_MODEL_ENRICH_GEMINI")
        os.environ["IB_MODEL_ENRICH"] = "gemini-3.1-pro"
        check(llm.model_enrich() == "gemini-3.1-pro",
              "a shared override stopped applying to the provider it names")
        os.environ["IB_PROVIDER"] = "claude"
        check(llm.model_enrich() == llm.default_model("enrich", "claude"),
              "a Gemini model was handed to Anthropic")
        check(llm.stale_override("enrich") == "gemini-3.1-pro",
              "nothing reported the setting that had stopped applying")

        # A name nothing recognises is honoured. A fine-tune or a preview
        # alias is a model name you typed and meant, and refusing to pass it
        # on would make this guard the thing that breaks a working setup.
        os.environ["IB_MODEL_ENRICH"] = "internal-eval-7"
        check(llm.model_enrich() == "internal-eval-7",
              "an unrecognised model name was second-guessed")
        check(llm.stale_override("enrich") == "",
              "an unrecognised name was reported as stale")
    finally:
        for k, v in prior.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        _restore(saved)


def test_probing_a_key_asks_the_provider_that_key_belongs_to() -> None:
    """`llm --test claude` has to reach Anthropic whatever `llm_provider`
    says, or it answers a question about one key with a call made on another.
    There is no endpoint that validates a key without spending a call, which
    is exactly why the call has to go to the right place."""
    real = llm.urllib.request.urlopen
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "c"})
    try:
        opener, sent = _capture({"content": [{"type": "tool_use",
                                              "input": {"ok": True}}],
                                 "stop_reason": "tool_use"})
        llm.urllib.request.urlopen = opener
        out = llm.probe("claude")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    chat = [p for p in out if p.job == "grade"]
    check(len(chat) == 1 and chat[0].ok, f"probing Claude reported {out}")
    check("api.anthropic.com" in sent["url"], f"the probe went to {sent['url']}")
    check(sent["headers"].get("x-api-key") == "c", "the probe used the wrong key")
    check(chat[0].model == llm.default_model("grade", "claude"),
          f"the probe named {chat[0].model}, not Claude's own model")

    # Anthropic sells no embeddings endpoint, so that line is answered from
    # the table rather than by failing a request to find out.
    emb = [p for p in out if p.job == "embed"]
    check(len(emb) == 1 and not emb[0].ok and "embeddings" in emb[0].message,
          f"the embedding line said {emb}")


def test_probing_a_provider_with_no_key_spends_nothing() -> None:
    """"Not set" is the answer to the question that was asked, and it is
    knowable without a request. A probe that called out anyway would burn a
    call to be told what the absence of an environment variable already
    says."""
    real = llm.urllib.request.urlopen
    calls = []
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g"})
    try:
        def opener(req, **kw):
            calls.append(req.full_url)
            raise AssertionError("a probe with no key made a request")
        llm.urllib.request.urlopen = opener
        out = llm.probe("openai")
    finally:
        llm.urllib.request.urlopen = real
        _restore(saved)
    check(not calls, f"requests were made: {calls}")
    check(len(out) == 1 and not out[0].ok, f"unexpected probe result: {out}")
    check("OPENAI_API_KEY" in out[0].message, f"the reason was {out[0].message!r}")
    check("settings openai_api_key" in out[0].hint,
          f"no way out was offered: {out[0].hint!r}")


def test_the_second_opinion_is_not_the_model_that_gave_the_first() -> None:
    """`cross-audit --api` was pinned to Claude, which made it a genuine
    second opinion for exactly as long as `llm_provider` was not claude. Set
    the provider to claude and the pass became the same vendor checking its
    own work -- which is what `audit` already does, and the whole reason this
    command exists."""
    saved = _with_provider("claude", {"ANTHROPIC_API_KEY": "c", "GEMINI_API_KEY": "g"})
    try:
        pick = crossaudit.second_provider()
        check(pick and pick != "claude",
              f"Claude was asked to check its own work: {pick!r}")
    finally:
        _restore(saved)

    # With somebody else answering, Claude is still the preference: this pass
    # is the one that pays for the extra effort level.
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "c"})
    try:
        check(crossaudit.second_provider() == "claude",
              "Claude was passed over while it was available and not the first")
    finally:
        _restore(saved)

    # One key, and it is the primary's: there is nobody left to ask, and
    # saying so beats spending a call to prove it.
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g"})
    try:
        check(crossaudit.second_provider() == "",
              "a second opinion was promised with no second key to give it")
    finally:
        _restore(saved)


def test_every_second_opinion_name_reaches_the_queries_that_act_on_it() -> None:
    """A verdict is only worth filing if the things that act on it can find
    it. `IN ('claude-code', 'claude-api')` was written out in four queries
    across three modules, and a rejection filed under any other name read as
    no rejection at all -- so a question a second reader called wrong went
    straight back into the drill."""
    from . import cli
    for name in llm.SECOND_PROVIDERS:
        conn, a, _ = fresh()
        qid = _active(conn, a, f"Walk me through a DCF, {name} edition")
        audit_apply(conn, qid, {"verdict": "keep", "confidence": 0.9}, "b1")
        crossaudit.record(conn, qid, {"verdict": "reject", "confidence": 0.9,
                                      "reason": "wrong"},
                          provider=name, model="m")
        conn.commit()

        due = [r["id"] for r in scheduler.due_questions(conn, limit=10)]
        check(qid not in due,
              f"a question rejected by {name} was still offered for drilling")
        picked = browse.ids(conn, [("flag", "disputed")])
        check(qid in picked,
              f"browse --flag disputed could not see a {name} rejection")
        check(len(crossaudit.disagreements(conn)) == 1,
              f"a disagreement filed as {name} was not reported")

    for name in llm.SECOND_PROVIDERS:
        check(name not in llm.PRIMARY_PROVIDERS,
              f"{name} would be read as a first opinion as well as a second")


def test_a_review_batch_names_who_gave_the_first_opinion() -> None:
    """The batch tells its reviewer what the first pass decided so they can
    disagree with it. Which model that was is half the information: the
    field was called `gemini` whoever had actually run the audit, so a batch
    reviewed after a provider switch briefed the reviewer on the wrong
    model."""
    conn, a, _ = fresh()
    qid = _active(conn, a, "Why might two companies with identical growth trade apart?")
    audit_apply(conn, qid, {"verdict": "keep", "confidence": 0.9}, "b1")
    conn.execute("UPDATE audits SET provider = 'openai' WHERE question_id = ?", (qid,))
    conn.commit()
    row = crossaudit.pending(conn, target="kept")[0]
    item = crossaudit._item(row)
    check(item["first_opinion"]["by"] == "OpenAI",
          f"the batch said the first opinion was {item['first_opinion']!r}")
    check(item["first_opinion"]["verdict"] == "keep", "the verdict went missing")


def test_a_typed_api_key_is_not_written_to_the_history_file() -> None:
    """`.env.local` is created 0600 and the key travels in a header rather
    than a URL, and then `settings gemini_api_key sk-...` put the same secret
    in ~/.superday_history in plain text, where it stays for a thousand
    commands. The line is redacted where it becomes durable: what is echoed,
    what ↑ brings back, and what is written down."""
    from . import cli
    check(cli.redact("settings gemini_api_key sk-live-1234")
          == "settings gemini_api_key ‹key hidden›", "the key survived redaction")
    # Prefix spellings are legal -- `settings gem <key>` sets it -- so a
    # redactor that only knows the full name has a hole exactly where a
    # hurried user is.
    check("sk-live" not in cli.redact("settings anth sk-live-1234"),
          "a prefix spelling was not redacted")
    check(cli.redact("settings model_grade gemini-3.5-flash")
          == "settings model_grade gemini-3.5-flash",
          "an ordinary setting was mangled")
    check(cli.redact("drill -n 5") == "drill -n 5", "an ordinary command was mangled")

    # The transcript is the other place the line becomes durable: it stays on
    # screen for the rest of the session and scrolls with everything else.
    shell = tui.Shell(on_submit=lambda s, l: None, redact=cli.redact)
    shell._run_one("settings gemini_api_key sk-live-1234")
    echoed = "\n".join(shell.transcript.lines)
    check("sk-live-1234" not in echoed, f"the key was echoed: {echoed!r}")
    check("key hidden" in echoed, f"nothing was echoed at all: {echoed!r}")

    saved_path = cli.HIST_PATH
    tmp = Path(tempfile.mkdtemp()) / "hist"
    try:
        cli.HIST_PATH = tmp
        cli._save_history(["drill -n 5", "settings openai_api_key sk-live-1234"])
        written = tmp.read_text()
        check("sk-live-1234" not in written, "the key was written to the history file")
        check("settings openai_api_key" in written,
              "the whole line went missing rather than just the key")
        check(tmp.stat().st_mode & 0o077 == 0,
              f"the history file is readable by others: {oct(tmp.stat().st_mode)}")
    finally:
        cli.HIST_PATH = saved_path


def test_the_completion_menu_offers_every_setting_there_is() -> None:
    """The `settings <key>` completions were a hand-written subset, and it had
    gone stale exactly where it hurt: `llm_provider`, `anthropic_api_key` and
    `openai_api_key` were all missing, so the three keys that switch provider
    were the three the menu would not tell you existed."""
    from . import cli
    have = set(cli.SETTINGS_KEYS)
    want = {e["key"] for e in cli.SETTINGS}
    check(have == want, f"the menu and the table disagree: {want ^ have}")


def test_setting_one_vendors_model_while_another_answers_is_refused() -> None:
    """A model belongs to exactly one vendor, so this is either the wrong
    model or the wrong provider. Written down quietly it becomes a 404 on
    every call for the rest of the session, with nothing pointing back at the
    setting that caused it."""
    from . import cli
    entry = [e for e in cli.SETTINGS if e["key"] == "model_grade"][0]
    written = []
    real = cli._env_file_set
    saved = _with_provider("gemini", {"GEMINI_API_KEY": "g"})
    try:
        cli._env_file_set = lambda k, v: written.append((k, v))
        cli._settings_set(entry, "claude-opus-5")
        check(not written, f"a Claude model was stored against Gemini: {written}")
        cli._settings_set(entry, "gemini-3.1-pro")
        check(written == [("IB_MODEL_GRADE_GEMINI", "gemini-3.1-pro")],
              f"the override was not stored per provider: {written}")
    finally:
        cli._env_file_set = real
        _restore(saved)


def test_a_card_minutes_away_is_not_reported_as_due_today() -> None:
    """The bug the whole `--again` change came out of. FSRS's learning steps
    are minutes long, so a question answered at 10:01 comes round at 10:11 --
    and the drill fold printed `next due 2026-08-20`, which is today's date
    and reads as "go ahead". The very next drill then refused it. Two screens,
    opposite claims, same card."""
    at = datetime(2026, 8, 20, 10, 1, tzinfo=timezone.utc)
    soon = at + timedelta(minutes=10)
    check(scheduler.due_phrase(soon, at=at) == "in 10m",
          f"ten minutes out read as {scheduler.due_phrase(soon, at=at)!r}")
    check(scheduler.due_phrase(at + timedelta(hours=5), at=at) == "in 5h",
          "five hours out did not read in hours")
    check(scheduler.due_phrase(at - timedelta(minutes=1), at=at) == "now",
          "an overdue card did not read as askable")
    # Past a day the date is the useful unit again -- that is what you diff
    # against a calendar.
    check(scheduler.due_phrase(at + timedelta(days=4), at=at) == "2026-08-24",
          "four days out stopped being a date")


def test_a_held_back_selection_says_which_reason_applies_to_which() -> None:
    """`drill --ids` used to answer "they are scheduled for later, or
    quarantined by an unapplied cross-audit correction" and leave you to guess
    between two causes one query apart."""
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    later, sooner, fine = sorted(made.values())
    for qid, hours in ((later, 3), (sooner, 1)):
        scheduler.ensure_card(conn, qid)
        conn.execute("UPDATE schedule SET due_at = ? WHERE question_id = ?",
                     ((datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(), qid))
    conn.execute("INSERT INTO audits (question_id, provider, model, verdict, confidence, "
                 "reason, ran_at) VALUES (?, 'claude-api', 'm', 'reject', 0.9, 'no', ?)",
                 (fine, datetime.now(timezone.utc).isoformat()))
    conn.commit()

    held = scheduler.held_back(conn, [later, sooner, fine, 999999])
    check(held[later][0] == "scheduled", f"{later} read as {held[later][0]}")
    check(held[fine][0] == "quarantined", f"a rejected question read as {held[fine][0]}")
    check(held[999999][0] == "missing", "an id not in the bank was not reported as missing")


def test_again_drops_the_due_window_and_nothing_else() -> None:
    """Wanting another go at what you just answered is a normal thing to want.
    `--again` is the only way to get it -- and it must not become a way past
    the quarantine, which is not a pacing decision."""
    conn, a, _ = fresh()
    made = _stocked(conn, a)
    scheduled, banned = sorted(made.values())[:2]
    for qid in (scheduled, banned):
        scheduler.ensure_card(conn, qid)
        conn.execute("UPDATE schedule SET due_at = ? WHERE question_id = ?",
                     ((datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), qid))
    conn.execute("INSERT INTO audits (question_id, provider, model, verdict, confidence, "
                 "reason, ran_at) VALUES (?, 'claude-api', 'm', 'reject', 0.9, 'no', ?)",
                 (banned, datetime.now(timezone.utc).isoformat()))
    conn.commit()

    ids = [scheduled, banned]
    check(not scheduler.due_questions(conn, limit=9, ids=ids),
          "a card three days out was askable")
    got = {r["id"] for r in scheduler.due_questions(conn, limit=9, ids=ids,
                                                    ignore_schedule=True)}
    check(got == {scheduled}, f"--again returned {got}, expected only the scheduled one")


def test_the_two_dependency_lists_cannot_drift_apart() -> None:
    """`requirements.txt` serves the clone, `pyproject.toml` serves the wheel,
    and they name the same three packages. Two hand-maintained copies of one
    list is the completion-file problem again: drift is a failing test rather
    than something someone notices when an install comes up short."""
    from .config import PACKAGE
    root = PACKAGE.parent
    req, proj = root / "requirements.txt", root / "pyproject.toml"
    if not (req.exists() and proj.exists()):
        return
    def names(text: str) -> set[str]:
        found = set()
        for line in text.splitlines():
            line = line.split("#")[0].strip().strip('",')
            if line and not line.startswith("["):
                m = re.match(r"^([A-Za-z0-9_.-]+)\s*[<>=!~]", line)
                if m:
                    found.add(m.group(1).lower())
        return found
    inside = names(proj.read_text().split("dependencies = [")[-1].split("]")[0])
    check(names(req.read_text()) == inside,
          f"requirements.txt and pyproject.toml disagree: "
          f"{names(req.read_text()) ^ inside}")


def test_everything_the_wheel_needs_is_inside_the_package() -> None:
    """An installed copy has no repo root. Migrations it cannot find are a
    database it cannot open, and packs it cannot find are the starter bank a
    fresh install exists to get."""
    from .config import PACKAGE
    check(sorted((PACKAGE / "migrations").glob("*.sql")),
          "the migrations do not ship inside the package")
    check(len(list((PACKAGE / "packs").glob("*.json"))) >= 6,
          "the authored packs do not ship inside the package")
    declared = (PACKAGE.parent / "pyproject.toml").read_text()
    for pattern in ("migrations/*.sql", "packs/*.json"):
        check(pattern in declared, f"{pattern} is not declared as package data")
