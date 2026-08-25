"""superday: IB interview drilling over your own question bank."""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import random
import re
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import audit as audit_mod
from . import plan as plan_mod
from . import reground as reground_mod
from . import enrich as enrich_mod
from . import grade as grade_mod
from . import config as config_mod
from . import theme as theme_mod
from . import analytics, backup, browse, chains, checks, clip, consult as consult_mod, crossaudit, dupes, history, llm, market, mock, search, session, tagging, tui, ui, usage
from . import views as views_mod
from . import scheduler
from .topics import TOPICS
from .admission import admit, kind_for_topic, rubric_is_the_answer as provisional_rubric
from .admission import normalize as admission_normalize
from .config import corpus_dir
from .db import connect, db_path, migrate, now, upsert_source
from .ingest import epub as epub_mod
from .ingest import pdf as pdf_mod
from .ingest import pipeline
from .ingest import pack, sec as sec_mod
from .ingest import web as web_mod
from .ingest.docx import file_hash, parse as parse_docx
from .scheduler import due_questions, record_review
from .ui import (BOLD, DIM, bad, dim, head, ok, rule, section, verdict,
                 warn, wrap)


def _selftest() -> int:
    from .tests import main as run_tests
    return run_tests()


# ---------------------------------------------------------------- ingest

def cmd_ingest(conn: sqlite3.Connection, args) -> None:
    files = []
    root = corpus_dir()
    if args.path:
        p = Path(args.path).expanduser()
        files = [p] if p.is_file() else sorted(p.glob("**/*.docx"))
    else:
        files = [f for f in sorted(root.glob("*.docx")) if not f.name.startswith("~$")]

    if not files:
        print("nothing to ingest")
        return

    totals = {"new": 0, "duplicate": 0, "variant": 0, "rejected": 0}
    for f in files:
        if f.suffix.lower() != ".docx":
            continue
        sid, created = upsert_source(
            conn, kind="docx", title=f.stem, path=str(f), file_hash=file_hash(f)
        )
        if not created and not args.force:
            print(dim(f"  skip (already ingested)  {f.name}"))
            continue
        _, pairs = parse_docx(f)
        counts = {"new": 0, "duplicate": 0, "variant": 0, "rejected": 0}
        for p in pairs:
            answer = p["answer"]
            if p["answer_de"]:
                answer += "\n\n[DE]\n" + p["answer_de"]
            v = admit(
                conn, source_id=sid, question_text=p["question"],
                answer_text=answer, locator=f.stem,
            )
            if p.get("question_de") and v.matched_id:
                conn.execute(
                    "INSERT OR IGNORE INTO phrasings "
                    "(question_id, text, source_id, norm_key) VALUES (?, ?, ?, ?)",
                    (v.matched_id, p["question_de"], sid,
                     admission_normalize(p["question_de"])),
                )
            counts[v.kind] += 1
            totals[v.kind] += 1
        conn.commit()
        print(f"  {counts['new']:3d} new  {counts['duplicate']:3d} dup  "
              f"{counts['variant']:3d} variant   {f.stem[:44]}")

    print(rule())
    print(f"  {ok(str(totals['new']))} new, {totals['duplicate']} duplicates attached as evidence, "
          f"{totals['variant']} variants")
    pending = conn.execute(
        "SELECT COUNT(*) c FROM questions WHERE status = 'needs_review'"
    ).fetchone()["c"]
    if pending:
        print(warn(f"  {pending} awaiting review. Run: superday review"))


def _already_done(conn: sqlite3.Connection, sid: int, created: bool,
                  force: bool, label: str) -> bool:
    """True when this source needs no further extraction.

    "The sources row exists" is not the same question as "the whole file has
    been read", and treating it as one is why an ingest that ran out of quota
    at chunk 30 of 60 could not be finished: the re-run printed "skip (already
    ingested)" and the second half of the book was never extracted. --force
    was the only way on, and it re-sent the 30 chunks that had already landed.
    """
    if force:
        pipeline.forget(conn, sid)
        return False
    if created:
        return False
    if pipeline.is_complete(conn, sid):
        print(dim(f"  skip (already ingested)  {label}"))
        return True
    print(dim(f"  resuming {label}: an earlier run stopped part way"))
    return False


def cmd_ingest_pdf(conn: sqlite3.Connection, args) -> None:
    if _needs_key():
        return

    root = corpus_dir()
    if args.path:
        p = Path(args.path).expanduser()
        files = [p] if p.is_file() else sorted(p.glob("**/*.pdf"))
    else:
        files = sorted(
            f for pat in ("HandBooks/*.pdf", "Breaking_Into_Wallstreet_Guide/*.pdf")
            for f in root.glob(pat)
        )
    if not files:
        print("no pdfs found")
        return

    for f in files:
        sid, created = upsert_source(
            conn, kind="pdf", title=f.stem, path=str(f),
            file_hash=pdf_mod.file_hash(f),
        )
        if _already_done(conn, sid, created, args.force, f.name):
            continue

        pages = pdf_mod.clean_pages(pdf_mod.page_texts(f))
        conn.execute("UPDATE sources SET page_count = ? WHERE id = ?", (len(pages), sid))
        windows = list(pdf_mod.chunks(pages, args.window))
        if args.max_chunks:
            windows = windows[: args.max_chunks]

        print(f"\n{head(f.name)}  {len(pages)} pages, {len(windows)} chunks")
        out = pipeline.run(conn, sid, windows, partial=bool(args.max_chunks))
        pipeline.report(out)
    _pending_note(conn)


# ---------------------------------------------------------------- epub / web / video / filings

def _needs_key(what: str = "this ingest path",
               why: str = "the extraction step is what turns prose into questions") -> bool:
    """One answer to "you have no key", for every command that needs one.

    `enrich`, `audit` and `reground` each carried their own copy of this
    sentence, and all four told you to hand-edit `.env.local` -- which is the
    one route that skips the validation and loses the 0600 the writer applies.
    `ingest-filing` already pointed at `settings` for its own missing setting,
    so the tool gave two different answers to the same question depending on
    which command you happened to run first.
    """
    if llm.available():
        return False
    name = llm.provider()
    spec = llm.PROVIDERS[name]
    print(warn(f"{what} needs a {spec['label']} API key"))
    print(dim(f"  set one:  settings {spec['setting']} <key>   ·   {spec['console']}"))
    others = [n for n in llm.PROVIDERS if n != name and llm.available(n)]
    if others:
        # A key you already hold is a better suggestion than a list of the
        # three names, and it is one command rather than a decision.
        print(dim("  or switch to one you have a key for:  llm --use "
                  + " | ".join(others)))
    else:
        print(dim("  `llm` lists all three, what each costs and where to get a key"))
    print(dim(f"  {why}"))
    return True


def cmd_ingest_epub(conn: sqlite3.Connection, args) -> None:
    """Vault, M&I and anything else that ships as an EPUB."""
    if _needs_key():
        return
    root = corpus_dir()
    p = Path(args.path).expanduser() if args.path else root
    files = [p] if p.is_file() else sorted(p.glob("**/*.epub"))
    if not files:
        print(f"no epub files under {p}")
        return

    for f in files:
        sid, created = upsert_source(
            conn, kind="epub", title=epub_mod.book_title(f), path=str(f),
            file_hash=epub_mod.file_hash(f))
        if _already_done(conn, sid, created, args.force, f.name):
            continue

        windows = epub_mod.chunks(f, chars=args.window)
        if args.max_chunks:
            windows = windows[: args.max_chunks]
        print(f"\n{head(f.name)}  {len(windows)} chunks")
        if not windows:
            print(warn("  nothing readable in it: no chapters over the size floor"))
            continue
        out = pipeline.run(conn, sid, windows, partial=bool(args.max_chunks))
        pipeline.report(out)
    _pending_note(conn)


def cmd_ingest_web(conn: sqlite3.Connection, args) -> None:
    """A forum thread, an article, or a page you saved from the browser."""
    if _needs_key():
        return
    for target in args.url:
        try:
            title, body, windows = web_mod.load(target)
        except web_mod.Refused as e:
            print(bad(f"  {e}"))
            continue
        except Exception as e:
            print(bad(f"  could not read {target}") + dim(" - " + _why(e)))
            continue

        if not windows:
            print(warn(f"  nothing readable at {target}"))
            continue
        sid, created = upsert_source(
            conn, kind="url", title=title[:120], path=target,
            file_hash=web_mod.source_hash(target, body))
        if _already_done(conn, sid, created, args.force, title[:60]):
            continue

        print(f"\n{head(title[:70])}  {len(windows)} chunks")
        # A forum thread is people reporting what they were actually asked.
        # That outranks a textbook question everywhere in this tool, so it is
        # recorded as such rather than as another published source.
        out = pipeline.run(conn, sid, windows,
                           origin="interviewer_asked" if args.asked else "published")
        pipeline.report(out)
    _pending_note(conn)


def cmd_ingest_filing(conn: sqlite3.Connection, args) -> None:
    """Turn a real company's filed numbers into questions about those numbers.

    No model touches the figures. They come from the filer's own XBRL tags and
    every answer is arithmetic done locally, so this path needs no API key.
    """
    ticker = args.ticker.strip().upper()
    try:
        cik = int(ticker) if ticker.isdigit() else sec_mod.resolve_cik(ticker)
    except sec_mod.NeedsContact as e:
        print(bad(f"  {e}"))
        return
    except Exception as e:
        print(bad("  could not reach the SEC ticker list") + dim(" - " + _why(e)))
        return
    if cik is None:
        print(bad(f"  no SEC filer with ticker {ticker}"))
        return

    try:
        facts = sec_mod.fetch_facts(cik)
    except Exception as e:
        print(bad(f"  could not fetch filings for CIK {cik}") + dim(" - " + _why(e)))
        return

    name = sec_mod.entity_name(facts)
    figures = sec_mod.annual_figures(facts, fiscal_year=args.year)
    if not figures:
        print(warn(f"  no annual figures found for {name}"
                   + (f" in FY{args.year}" if args.year else "")))
        return

    print(section(f"{name}  (CIK {cik})"))
    print(sec_mod.summary(name, figures))

    questions = sec_mod.build_questions(name, figures, args.year)
    if not questions:
        print(warn("  not enough tagged lines to build a question"))
        return

    if args.dry_run:
        print(section(f"{len(questions)} QUESTIONS (not saved)"))
        for q in questions:
            print("\n" + ui.question(q["question"], "  "))
            print(ui.body(q["answer"], "    "))
        return

    sid, _ = upsert_source(
        conn, kind="filing", title=f"{name} FY{figures.get('revenue', {}).get('fy', '')}",
        path=f"CIK{cik:010d}",
        file_hash=f"sec:{cik}:{args.year or figures.get('revenue', {}).get('fy', '')}")

    counts = {"new": 0, "duplicate": 0, "variant": 0, "rejected": 0}
    for q in questions:
        v = admit(conn, source_id=sid, question_text=q["question"],
                  answer_text=q["answer"], locator=f"XBRL FY{args.year or ''}".strip(),
                  status="active",          # the numbers are the filer's, not a model's
                  origin="self_authored")
        counts[v.kind] += 1
        if v.kind != "new":
            continue
        conn.execute(
            "UPDATE questions SET topic = ?, difficulty = ?, kind = ? WHERE id = ?",
            (q["topic"], q["difficulty"], kind_for_topic(q["topic"]), v.matched_id))
        conn.execute(
            "UPDATE answers SET rubric_points = ?, answer_status = 'ok' WHERE question_id = ?",
            (json.dumps(q["rubric_points"]), v.matched_id))
        tagging.attach(conn, v.matched_id, q["tags"] + ["sec-filing", ticker.lower()])
    conn.commit()

    print("\n" + rule())
    print(f"  {ok(str(counts['new']))} new   {counts['duplicate']} duplicate   "
          f"{counts['variant']} variant")
    print(dim(f"  active immediately: the figures are {name}'s own filed numbers, "
              "not an extraction"))
    print(dim(f"  drill them: superday drill --tag {ticker.lower()}"))


SHIPPED_PACKS = config_mod.PACKAGE / "packs"


def _resolve_pack(raw: str) -> list[Path]:
    """A path, a directory of packs, or the name of one that ships with the tool.

    The shipped packs used to be reachable only as `packs/01-dcm-syndicate.json`
    relative to a clone, which is a path an installed copy does not have. They
    travel inside the package now, so `ingest-pack dcm-syndicate` and
    `ingest-pack all` work wherever the tool was installed from.
    """
    if raw in ("all", "shipped"):
        return sorted(SHIPPED_PACKS.glob("*.json"))
    p = Path(raw).expanduser()
    if p.is_dir():
        return sorted(p.glob("*.json"))
    if p.exists():
        return [p]
    stem = raw.removesuffix(".json").lower()
    hits = [f for f in sorted(SHIPPED_PACKS.glob("*.json"))
            if f.stem.lower() == stem or f.stem.lower().split("-", 1)[-1] == stem]
    return hits or [p]


def cmd_ingest_pack(conn: sqlite3.Connection, args) -> None:
    """Land authored question packs. No provider call, no API key."""
    paths: list[Path] = []
    for raw in args.path:
        paths.extend(_resolve_pack(raw))
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(bad(f"no such pack: {p}"))
        return
    if not paths:
        print(warn("no packs found"))
        return

    total = {"new": 0, "duplicate": 0, "variant": 0, "rejected": 0, "promoted": 0}
    batches: list[str] = []
    for p in paths:
        try:
            res = pack.load(conn, p, dry_run=args.dry_run,
                            status_override=args.status)
        except pack.PackError as e:
            print(bad(str(e)))
            continue

        print("\n" + head(res["title"]) + dim(f"  ({p.name})"))
        if res["dry_run"]:
            spread = "  ".join(f"{t} {n}" for t, n in sorted(res["topics"].items()))
            print(dim(f"  {res['items']} items -> {res['status']}"))
            print(dim(f"  {spread}"))
            continue
        for k in total:
            total[k] += res[k]
        batches.append(res["batch"])
        print(f"  {ok(str(res['new']))} new   {res['duplicate']} duplicate   "
              f"{res['variant']} variant   {res['rejected']} rejected")
        if res["status"] == "active":
            print(dim(f"  {res['promoted']} promoted to active"))
        if res.get("note"):
            print(ui.body(res["note"], "  "))

    if args.dry_run:
        print(dim("\n  nothing written"))
        return

    print("\n" + rule())
    print(f"  {ok(str(total['new']))} new across {len(batches)} pack(s)")
    if total["duplicate"] or total["variant"]:
        print(dim(f"  the gate merged {total['duplicate']} duplicates and "
                  f"{total['variant']} variants into questions already in the bank"))
    if total["promoted"]:
        print(dim(f"  undo the promotion with: superday undo"))
    _pending_note(conn)


def _pending_note(conn: sqlite3.Connection) -> None:
    pending = conn.execute(
        "SELECT COUNT(*) c FROM questions WHERE status = 'needs_review'"
    ).fetchone()["c"]
    line = f"\n  {pending} awaiting review"
    print(warn(line) if pending else line)
    if pending:
        print(dim("  next: superday enrich  ·  superday audit  ·  superday review"))


# ---------------------------------------------------------------- enrich

def cmd_enrich(conn: sqlite3.Connection, args) -> None:
    if _needs_key("enrich", "it writes the rubric, topic and difficulty a drill grades against"):
        return
    if getattr(args, "missing_answers", False):
        todo = enrich_mod.pending_missing_answers_count(conn)
        if not todo:
            print(ok("no active questions with missing answers"))
            return
        print(f"{todo} questions need model answers (model: {llm.model_enrich()})")
        n = enrich_mod.draft_missing_answers(conn, batch_size=args.batch, limit=args.limit)
        print(ok(f"done: {n} answers drafted"))
        return
    todo = enrich_mod.pending_count(conn)
    if not todo:
        print(ok("nothing pending, every active question already has a rubric"))
        return
    print(f"{todo} questions need enrichment (model: {llm.model_enrich()})")
    n = enrich_mod.run(conn, batch_size=args.batch, limit=args.limit)
    print(ok(f"done: {n} enriched"))


def cmd_audit(conn: sqlite3.Connection, args) -> None:
    if _needs_key("audit", "it is a second opinion on what extraction produced"):
        return

    def progress(line: str) -> None:
        if "failed" in line or "giving up" in line:
            print(bad(line))
        else:
            print(dim(line))

    tally = audit_mod.run(conn, batch_size=args.batch, limit=args.limit,
                          status=args.status, progress=progress)
    print(rule())
    print(f"  {ok('kept')} {tally['kept']}   {warn('fixed')} {tally['fixed']}   "
          f"{bad('rejected')} {tally['rejected']}   held for you {tally['held']}")
    for r in conn.execute(
        "SELECT canonical_text, audit_reason FROM questions "
        "WHERE audit_verdict = 'reject' AND status = 'rejected' "
        "ORDER BY id DESC LIMIT 8"
    ):
        print(bad(f"\n  REJECTED  {r['canonical_text'][:62]}"))
        print(wrap(r["audit_reason"] or "", "            "))


# ---------------------------------------------------------------- add

def cmd_add(conn: sqlite3.Connection, args) -> None:
    text = " ".join(args.text).strip()
    if not text:
        print("nothing to add")
        return
    use_llm = getattr(args, "llm", False)
    if use_llm and not llm.available():
        # The setting named here is the configured provider's, not Gemini's:
        # this line used to tell an OpenAI user to set a Google key.
        setting = llm.PROVIDERS[llm.provider()]["setting"]
        print(bad(f"  --llm needs a {llm.provider_label()} key"))
        print(dim("  set one with ") + head(f"settings {setting}")
              + dim(", or add it without --llm and let ")
              + head("enrich") + dim(" fill it in later"))
        # Same offer every other key-less path makes: a provider you already
        # hold a key for is one command, not a decision.
        others = [n for n in llm.PROVIDERS
                  if n != llm.provider() and llm.available(n)]
        if others:
            print(dim("  or switch to one you have a key for:  llm --use "
                      + " | ".join(others)))
        return
    sid, _ = upsert_source(conn, kind="manual", title="Manual entry",
                           file_hash="manual-entry-singleton")
    v = admit(
        conn, source_id=sid, question_text=text, answer_text=args.answer,
        locator=now()[:10], origin=args.origin,
        # A model's draft of an answer, or a model's polish of your rough
        # notes, is not a human-typed answer and does not inherit the trust
        # that gets one straight to `active`. It takes the normal audit and
        # review pass like everything else a model wrote.
        status="needs_review" if use_llm else ("active" if args.answer else "needs_review"),
    )
    if v.kind == "new" and use_llm:
        qid = v.matched_id
        print(ok(f"added #{qid}") + f"  [{args.origin}]")
        done = enrich_mod.structure_one(conn, qid, text,
                                        rough_answer=args.answer, progress=print)
        if not done:
            print(dim("  it is in the bank as you typed it   ·   ")
                  + head("enrich") + dim(" will pick it up"))
            _pending_note(conn)
            return
        rec = _question_record(conn, qid)
        print()
        _render_question(conn, rec)
        print()
        print(dim("  it is ") + warn("needs_review")
              + dim(" because a model wrote that answer   ·   ")
              + head(f"review") + dim(" or ") + head(f"audit")
              + dim(" is what promotes it"))
        return
    if v.kind == "new":
        print(ok(f"added #{v.matched_id}") + f"  [{args.origin}]")
        if not args.answer:
            print(warn("  no answer yet: answer_status=missing. It will still be drilled,"))
            print(warn("  and `superday review` is where you fill it in."))
    elif v.kind == "duplicate":
        row = conn.execute("SELECT canonical_text FROM questions WHERE id = ?",
                           (v.matched_id,)).fetchone()
        print(dim(f"already in the bank as #{v.matched_id} (similarity {v.similarity:.2f})"))
        print(wrap(row["canonical_text"], "  "))
        if args.origin == "interviewer_asked":
            conn.execute("UPDATE questions SET origin = 'interviewer_asked' WHERE id = ?",
                         (v.matched_id,))
            conn.commit()
            print(ok("  bumped origin to interviewer_asked, it will now rank higher"))
    else:
        print(dim(f"filed as a variant of #{v.matched_id} (similarity {v.similarity:.2f})"))


# ---------------------------------------------------------------- review

def cmd_review(conn: sqlite3.Connection, args) -> None:
    rows = list(conn.execute(
        "SELECT q.*, a.answer_key, a.rubric_points FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        "WHERE q.status = 'needs_review' ORDER BY q.id LIMIT ?", (args.limit,)
    ))
    if not rows:
        print(ok("review queue empty"))
        return
    accepted = rejected = skipped = 0
    batch = history.new_batch()
    for i, r in enumerate(rows, 1):
        print("\n" + rule("="))
        print(f"{i}/{len(rows)}   {head('#' + str(r['id']))}  [{r['topic']}]  "
              f"difficulty {r['difficulty']}  {r['origin']}")
        if r["audit_verdict"]:
            # The reason is the whole point of showing the verdict, so it
            # wraps rather than being cut off at a fixed 78 columns.
            print(warn(f"  audit: {r['audit_verdict'].upper()} (held, low confidence)"))
            if r["audit_reason"]:
                print(ui.body(r["audit_reason"], "    "))
        print(rule())
        print(ui.question(r["canonical_text"], "  "))
        if r["answer_key"]:
            print()
            print(ui.body(r["answer_key"], "  "))
        # The answer above is the model's rewrite. Show the source's own words
        # next to it so accepting a question is a check, not an act of faith.
        for e in conn.execute(
            "SELECT s.title, qs.locator, qs.verbatim_text FROM question_sources qs "
            "JOIN sources s ON s.id = qs.source_id WHERE qs.question_id = ? "
            "AND qs.verbatim_text IS NOT NULL LIMIT 2", (r["id"],)
        ):
            print(dim(f"\n  source: {e['title'][:44]} {e['locator'] or ''}"))
            print(dim(ui.body('"' + e["verbatim_text"][:420] + '"', "    ")))
        print()
        try:
            choice = input(
                "[a]ccept  [e]dit topic  [r]eject  [s]kip  [q]uit > ").strip().lower()
        except EOFError:
            break
        if choice == "a":
            history.set_status(conn, r["id"], "active", action="review", batch_id=batch)
            accepted += 1
        elif choice == "e":
            topic = input("  topic > ").strip()
            if topic:
                conn.execute("UPDATE questions SET topic = ? WHERE id = ?",
                             (topic, r["id"]))
                history.set_status(conn, r["id"], "active", action="review", batch_id=batch)
                accepted += 1
        elif choice == "r":
            history.set_status(conn, r["id"], "rejected", action="review", batch_id=batch)
            rejected += 1
        elif choice == "q":
            break
        else:
            skipped += 1
        conn.commit()
    print("\n" + rule())
    print(f"  {ok(str(accepted) + ' accepted')}   {bad(str(rejected) + ' rejected')}   "
          f"{dim(str(skipped) + ' skipped')}")
    if accepted or rejected:
        print(dim("  changed your mind? superday undo"))


def cmd_accept_all(conn: sqlite3.Connection, args) -> None:
    rows = list(conn.execute(
        "SELECT topic, COUNT(*) c FROM questions WHERE status = 'needs_review' "
        "GROUP BY topic ORDER BY c DESC"
    ))
    total = sum(r["c"] for r in rows)
    if not total:
        print(ok("review queue empty, nothing to accept"))
        return

    print(f"about to accept {total} questions:")
    for r in rows:
        print(f"  {r['c']:5d}  {r['topic'] or 'untitled'}")
    if not args.yes:
        try:
            choice = input(warn("proceed? [y/N] > ")).strip().lower()
        except EOFError:
            choice = ""
        if choice != "y":
            print(dim("cancelled"))
            return

    batch = history.new_batch()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM questions WHERE status = 'needs_review'")]
    n = sum(history.set_status(conn, qid, "active", action="accept-all", batch_id=batch)
            for qid in ids)
    conn.commit()
    print(ok(f"accepted {n} questions"))
    print(dim("  take it back with: superday undo"))


# ---------------------------------------------------------------- drill

def _grade_market(conn, q, user_answer) -> tuple[str, int | None]:
    """Grade a live-value answer, or say why it could not be graded.

    `None` is "not graded", and it is what every path that did not actually
    compare a number to a print returns. It used to be 3 -- a "good" written
    into the schedule for a question that was never asked properly, and,
    because the reveal was skipped for this kind, without ever showing you
    what the answer was. Six questions in the bank are market-awareness with
    no binding, each with a full answer and a rubric on file, and every one of
    them was unstudiable: press Enter, get told the grade is being skipped,
    get marked "good", and move on. `None` falls through to the same reveal
    and self-rating prompt every other question gets.
    """
    b = conn.execute("SELECT * FROM live_bindings WHERE question_id = ?",
                     (q["id"],)).fetchone()
    if b is None:
        return dim("  no live binding for this one - rate it yourself"), None
    val, as_of, stale = market.value_for(conn, b["provider"], b["series_key"], b["ttl_seconds"])
    if val is None:
        return warn("  could not reach the live feed - rate it yourself"), None
    line = f"  actual: {val} {b['unit']}  (as of {as_of})"
    if stale:
        line += warn(f"  [{stale}]")
    if market.observation_stale(as_of):
        # Grading against a print this old marks a right answer wrong and
        # writes that into the schedule, where it is permanent. Say the number,
        # say why it is not being used, and let you rate it yourself -- the
        # number is on screen, so you can see perfectly well how close you were.
        return line + "\n" + dim("  too old to grade against"
                                 "  ·  superday market --refresh"), None
    if not (user_answer or "").strip():
        # Enter means "reveal it, I will rate myself" on every other question
        # in the bank, and it has to mean that here too. This used to fall
        # through to the numeric compare, find no number in an answer nobody
        # gave, and write a 1 into the schedule -- a lapse recorded for
        # pressing the key that means "show me". The print is still shown,
        # because on a live question that print *is* the answer. `_reveal`
        # says why nothing is graded or stored right underneath this, so
        # saying it here too was the same sentence twice under a rule.
        return line, None
    guess = market.extract_number(user_answer)
    if guess is None:
        return line + "\n  no number found in your answer", 1
    off = abs(guess - val)
    if off <= b["tolerance"]:
        return line + f"\n  you said {guess}, off by {off:.2f}. Good.", 4
    if off <= b["tolerance"] * 3:
        return line + f"\n  you said {guess}, off by {off:.2f}. In the area, not sharp.", 2
    return line + f"\n  you said {guess}, off by {off:.2f}. Wrong.", 1


def _why(e: Exception) -> str:
    """A short, plain reason for a failure that is not a provider error.

    `type(e).__name__: {e}` is what these sites used to print, which for a
    DNS failure reads `URLError: <urlopen error [Errno 8] nodename nor
    servname provided for nodename>`. True, and no help at all.
    """
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return f"the server answered HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"could not connect ({getattr(e, 'reason', e)})"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "it timed out"
    if isinstance(e, FileNotFoundError):
        return "there is no such file"
    if isinstance(e, IsADirectoryError):
        return "that is a directory, not a file"
    if isinstance(e, PermissionError):
        return "permission denied"
    if isinstance(e, UnicodeDecodeError):
        return "the text is not readable as UTF-8"
    if isinstance(e, json.JSONDecodeError):
        return "the response was not valid JSON"
    if isinstance(e, sqlite3.Error):
        return f"the database refused it: {e}"
    return f"{type(e).__name__}: {e}"


def _llm_problem(message: str, hint: str = "", what: str = "") -> None:
    """How every failed provider call reaches the screen.

    One line saying what went wrong, one saying what to do about it, and
    never the provider's raw JSON -- that used to be pasted straight into the
    middle of a drill, where the useful sentence was the fourth line of a
    blob you had to read like a stack trace.
    """
    print()
    print(bad("  " + (f"{what}: " if what else "") + message))
    if hint:
        print(dim("  " + hint))
    if not os.environ.get("IB_DEBUG"):
        print(dim("  IB_DEBUG=1 to see the provider's own words"))


def _grading_live(args) -> bool:
    """Whether this sitting will spend API calls on grading.

    Drilling is free by default in the sense that matters: an empty answer, a
    self-rating, or `--local` never leaves the machine. A call happens only
    when you actually typed an answer and asked for it to be marked. The
    `grade_mode` setting is the standing preference; the flags override it for
    one sitting.
    """
    if getattr(args, "local", False) or getattr(args, "no_grade", False):
        return False
    mode = config_mod.load().get("grade_mode", "auto")
    if mode == "off":
        return False
    return llm.available()


DRILL_KEYS = {
    "Enter": "reveal the answer and rate yourself - never calls anything",
    "<text>": "type your answer to have it marked against the rubric",
    "q": "quit and save the rest of the sitting",
    "s": "skip -- back of the queue, asked again later",
    "n <text>": "note on this question, kept forever",
    "t <tags>": "tag it, comma separated",
    "c": "copy the question to the clipboard",
    "ca": "copy question, rubric and answer as Markdown",
    "f": "the written answer in full, once the rubric is on screen",
    "?": "this list",
}

GRADING_EXPLAINER = [
    "Typing an answer sends it to the model you configured, together with the",
    "rubric already",
    "stored for that question - never to decide what is true, only to report",
    "which of those rubric points you actually conveyed. You get the hits and",
    "misses, two sentences of feedback, a follow-up an interviewer would ask,",
    "and a suggested 1-4 rating you can override.",
    "",
    "It is the only thing in a drill that costs anything. Pressing Enter to",
    "reveal, rating yourself, and --local never leave the machine.",
]


def _drill_help() -> None:
    print()
    for k, v in DRILL_KEYS.items():
        print(f"  {head(k.ljust(10))} {dim(v)}")
    print()
    print("  " + head("what grading is"))
    for line in GRADING_EXPLAINER:
        print(dim("  " + line) if line else "")
    print()


def _print_lead_in(conn: sqlite3.Connection, qid: int) -> None:
    """The question this one follows, when it cannot be asked without it.

    #803 is "Wait a minute, how are Call Protection and Prepayment
    different?", and cold it is unanswerable - half of it is in #802. An
    interviewer asking it has just heard you answer the one before, so the
    drill puts that turn back rather than pretending the question is
    self-contained. Nothing here is the thing being asked, so it is set in the
    dim colour the metadata strip uses, not the question's.
    """
    prior = chains.lead_in(conn, qid)
    if not prior:
        return
    print(dim("  you have just been asked, and answered:"))
    for p in prior:
        for line in wrap(" ".join(p["canonical_text"].split()), "  ",
                         min(ui.width(), ui.PROSE_MAX)).split("\n"):
            print(dim(line))
    print()


def _reveal(conn: sqlite3.Connection, q: sqlite3.Row,
            hits: list[bool] | None = None) -> None:
    """Show what a good answer hits. Free: this is all local.

    The rubric is the answer here, and the prose `answer_key` is deliberately
    not printed. Two thirds of the ones on file are a single unbroken
    paragraph averaging 674 characters; printed under a checklist of the same
    content it meant reading the answer twice, the second time as a wall, and
    the wall was what the eye landed on last. `f` at the rating prompt prints
    it when it is actually wanted.

    `hits` is the grader's per-point verdict when there is one, so a marked
    sitting and a self-rated one are the same card with and without ticks
    rather than two different screens.
    """
    a = conn.execute(
        "SELECT answer_key, rubric_points, common_mistakes FROM answers WHERE question_id = ?",
        (q["id"],)).fetchone()
    points = json.loads(a["rubric_points"] or "[]") if a else []
    traps = json.loads(a["common_mistakes"] or "[]") if a else []
    prose = bool(a and (a["answer_key"] or "").strip())
    # A rubric that is still the placeholder is the answer's own first five
    # sentences, cut at 220 characters. Offering `f` under it promises the
    # written answer and delivers the same words a second time, and the card
    # itself is a checklist you cannot fail: a point phrased in the answer's
    # wording marks nothing that reading the answer would not.
    placeholder = provisional_rubric(points, a["answer_key"] if a else None)
    print("\n" + rule())
    if points:
        print(ui.answer_card(points, hits=hits, traps=traps))
        if placeholder:
            print(dim("\n  this rubric is the answer's own sentences, not "
                      "marking criteria yet"))
            print(dim("  ") + head("enrich") + dim(" writes the real one"))
            prose = False
    elif prose:
        # No rubric on file: the prose is all there is, so it is not hidden
        # behind a key that would reveal nothing.
        print(ui.body(a["answer_key"], "  "))
        if traps:
            print(f"\n  {warn('common mistakes')}")
            for t in traps[:4]:
                print(ui.body("- " + t, "  "))
        # The prose is already on screen, so `f` has nothing left to reveal
        # and offering it would print the same paragraph twice.
        prose = False
    elif conn.execute("SELECT 1 FROM live_bindings WHERE question_id = ?",
                      (q["id"],)).fetchone():
        # A bound question has no stored answer *on purpose* -- the live print
        # above is the answer, and `enrich` skips it for exactly that reason
        # (market.UNBOUND_SQL). Pointing at a command that will never touch it
        # is an instruction that cannot be followed.
        print(dim("  the live figure above is the answer - nothing is stored, "
                  "because it expires"))
    else:
        print(dim("  no answer on file yet")
              + dim("   ·   superday enrich --missing-answers"))
    print("\n" + dim("  ") + _extras_hint(prose))


def _extras_hint(has_prose: bool) -> str:
    """The one line under a revealed answer that says what the keys do."""
    keys = []
    if has_prose:
        keys.append(f"{head('f')} full answer")
    keys.append(f"{head('c')} copy question")
    keys.append(f"{head('ca')} copy all")
    keys.append(f"{head('n')} note")
    keys.append(f"{head('t')} tag")
    return dim("   ·   ").join(keys)


def _copy(text: str, what: str) -> None:
    """Put text on the clipboard and say so, or say why not.

    A copy that silently does nothing is the worst outcome here: you paste
    into a chat window, get the last thing you copied, and only notice after
    sending it. `copy_to_clipboard` is a no-op off a Mac, so a zero is
    reported rather than swallowed.
    """
    if not text.strip():
        print(dim(f"  nothing to copy for {what}"))
        return
    n = tui.copy_to_clipboard(text)
    if n:
        print(ok(f"  copied {what}") + dim(f"  ({n} chars)"))
    else:
        print(warn("  no clipboard on this machine")
              + dim("   ·   pbcopy is macOS only"))


def _answer_extras(conn: sqlite3.Connection, q: sqlite3.Row, raw: str) -> bool:
    """The keys that work once an answer is on screen. True if handled.

    Kept apart from `_drill_side_command` because `f` is not offered before
    you have answered: at the answer prompt Enter already reveals, and a
    second key that reveals *more* quietly would be a way to read the model
    answer while still counting as having answered from memory.
    """
    low = raw.lower()
    if low == "f":
        a = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                         (q["id"],)).fetchone()
        if a and a["answer_key"] and a["answer_key"].strip():
            print()
            print(ui.body(a["answer_key"], "  "))
        else:
            print(dim("  no written answer on file, only the rubric"))
        return True
    return _copy_command(conn, q, raw)


def _copy_command(conn: sqlite3.Connection, q: sqlite3.Row, raw: str) -> bool:
    """`c` and `ca`. Available before answering too: copying the question is
    not a hint, and wanting to paste it somewhere is why you are here."""
    low = raw.lower()
    if low == "c":
        _copy(clip.question(conn, q["id"]), "the question")
        return True
    if low in ("ca", "cc"):
        _copy(clip.markdown(conn, q["id"]), "question, rubric and answer as Markdown")
        return True
    return False


def _drill_side_command(conn: sqlite3.Connection, q: sqlite3.Row, raw: str) -> bool:
    """Handle the in-drill commands that are not an answer. True if handled."""
    low = raw.lower()
    if low == "?":
        _drill_help()
        return True
    if low.startswith("n ") and raw[2:].strip():
        conn.execute("INSERT INTO notes (question_id, body, created_at) VALUES (?, ?, ?)",
                     (q["id"], raw[2:].strip(), now()))
        conn.commit()
        print(ok("  noted"))
        return True
    if low.startswith("t ") and raw[2:].strip():
        names = [t.strip() for t in re.split(r"[,\s]+", raw[2:]) if t.strip()]
        added = tagging.attach(conn, q["id"], names)
        print(ok(f"  tagged {', '.join('#' + a for a in added)}") if added
              else dim("  already tagged"))
        return True
    return _copy_command(conn, q, raw)


def _drill_args(**overrides):
    """A complete drill Namespace, from the parser's own defaults.

    Internal callers (`list <topic>`, `show`'s [d]) used to hand-build a
    Namespace and every new drill flag silently broke them, because argparse
    defaults live in the parser and a hand-built Namespace does not have them.
    """
    defaults = vars(build_parser().parse_args(["drill"]))
    defaults.pop("fn", None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _drill_queue(conn: sqlite3.Connection, args) -> tuple[list[sqlite3.Row], int | None, dict]:
    """Either resume the last unfinished sitting or pick a fresh one."""
    picked = _id_list(getattr(args, "ids", None))
    spec = {"count": args.count, "topic": args.topic, "kind": args.kind,
            "tag": getattr(args, "tag", None), "weak": getattr(args, "weak", False),
            "ids": picked}

    if getattr(args, "resume", False):
        row = session.resumable(conn, "drill")
        if row is None:
            print(dim("  no unfinished drill to resume, starting a new one"))
        else:
            rows = session.queue_of(conn, row)
            s = session.summary(row)
            print(ok(f"  resuming the sitting from {s['started_at'][:16].replace('T', ' ')}")
                  + dim(f"  ({s['done']} done, {len(rows)} left)"))
            return rows, row["id"], s["spec"]

    again = bool(getattr(args, "again", False))
    spec["again"] = again
    rows = due_questions(conn, limit=args.count, topic=args.topic, kind=args.kind,
                         tag=spec["tag"], weak_first=spec["weak"], ids=picked,
                         ignore_schedule=again)
    if picked is not None and (not picked or len(rows) < len(picked)):
        # A hand-picked set that comes back short is not "nothing due" -- it is
        # the schedule holding cards back, or the quarantine pulling one whose
        # answer a cross-audit disputed. A silent shrink here would look like
        # the browse selection was wrong.
        if not picked:
            spec["explained"] = True
            print(warn("  that selection is empty - nothing to drill"))
        else:
            spec["explained"] = not rows
            _report_held(conn, picked, rows, again=again)
    if not rows:
        return [], None, spec
    # A parent that came through the same pick is asked immediately before its
    # child. Everything else keeps the order the scheduler chose.
    rows = chains.order(rows)
    sid = session.open_session(conn, "drill", [r["id"] for r in rows], spec)
    return rows, sid, spec


def _report_held(conn: sqlite3.Connection, picked: list[int],
                 rows: list[sqlite3.Row], *, again: bool) -> None:
    """Say why a hand-picked selection came back short, in the actual reason.

    This used to print "they are scheduled for later, or quarantined by an
    unapplied cross-audit correction" and leave you to guess which. It is the
    schedule almost every time, and the wait is almost always minutes -- FSRS
    puts a freshly-answered card ten minutes out -- so the sentence that reads
    as a wall is really "wait a bit, or say `--again`". `scheduler.held_back`
    knows which of the two it is per question; there is no reason not to say.
    """
    reasons = scheduler.held_back(conn, picked)
    got = {r["id"] for r in rows}
    held = {qid: why for qid, why in reasons.items() if qid not in got}
    if not held:
        return

    scheduled = sorted((due for kind, due in held.values() if kind == "scheduled" and due),
                       key=str)
    counted = collections.Counter(kind for kind, _ in held.values())
    parts = []
    if scheduled:
        soonest = scheduler.due_phrase(scheduled[0])
        parts.append(f"{counted['scheduled']} scheduled" + (
            f", the first {soonest}" if soonest != "now" else ""))
    if counted["quarantined"]:
        parts.append(f"{counted['quarantined']} quarantined by an unapplied "
                     "cross-audit correction")
    if counted["inactive"]:
        parts.append(f"{counted['inactive']} not active in the bank")
    if counted["missing"]:
        parts.append(f"{counted['missing']} not in the bank at all")

    if not rows:
        print(warn(f"  none of those {len(picked)} are askable right now"))
    else:
        print(dim(f"  {len(held)} of {len(picked)} held back"))
    print(dim("  " + "   ·   ".join(parts)))
    # Offered only when it would actually change the outcome. `--again` drops
    # the due window and nothing else, so suggesting it against a quarantined
    # selection would be advice that does not work.
    if counted["scheduled"] and not again:
        print(dim("  ") + head("drill --again") + dim(" asks them anyway"))


# One place, because the fold, the summary bars and the sittings list all name
# the same four ratings and had started to disagree about their colours.
RATINGS = {1: ("again", bad), 2: ("hard", warn), 3: ("good", ok), 4: ("easy", ok)}


def _drill_line(q: sqlite3.Row, rating: int | None, seconds: float, tail: str) -> str:
    """One answered question, folded to a single row of the transcript."""
    word, colour = RATINGS.get(rating or 0, ("", dim))
    mark = colour(f"{rating} {word}") if rating else dim("skipped")
    prefix = ("  " + ui.pad(mark, 9) + dim(f"#{q['id']:<6}")
              + dim(f"{int(seconds):>3}s  "))
    text = " ".join(q["canonical_text"].split())
    room = max(16, ui.width() - ui.vlen(prefix) - ui.vlen(tail) - 6)
    return prefix + ui.pad(ui.truncate(ui.paint(text, "text"), room), room) + dim("  " + tail)


def _collapse_drill() -> bool:
    """Whether a finished question folds to one line, per `settings`."""
    return (config_mod.load().get("drill_scrollback", "fold") != "full"
            and tui.active())


def cmd_drill(conn: sqlite3.Connection, args) -> None:
    rows, sid, spec = _drill_queue(conn, args)
    if not rows:
        # `_drill_queue` says something more specific when an explicit id list
        # came back empty; repeating the generic line under it reads as two
        # different problems rather than one.
        if not spec.get("explained"):
            print(ok("nothing due. `superday stats` to see what is scheduled."))
        return

    graded_mode = _grading_live(args)
    collapsing = _collapse_drill()
    total = len(rows)
    queue = list(rows)
    skips: dict[int, int] = {}
    tally = {"1": 0, "2": 0, "3": 0, "4": 0}
    drill_records: list[tuple[sqlite3.Row, int]] = []
    start_time = time.time()
    asked_n = 0
    quit_early = False

    print(dim(f"  {total} queued   ·   ")
          + (dim(f"typed answers marked by {llm.provider_label()}") if graded_mode
             else ok("self-rated, no API calls"))
          + dim("   ·   ? for keys"))

    while queue:
        q = queue.pop(0)
        asked_n += 1
        phrasings = [p["text"] for p in conn.execute(
            "SELECT text FROM phrasings WHERE question_id = ?", (q["id"],))]
        asked = random.choice([q["canonical_text"], *phrasings]) if phrasings else q["canonical_text"]

        meta = [
            ui.progress(asked_n, total),
            head(f"[{(q['topic'] or 'general').upper()}]"),
            dim(f"Diff: {q['difficulty'] or '-'}/5"),
            dim(f"Sources: {q['frequency']}x"),
            dim(f"#{q['id']}"),
        ]
        if q["origin"] == "interviewer_asked":
            meta.append(warn("ASKED IN REAL INTERVIEW"))
        tags = tagging.tags_for(conn, q["id"])
        if tags:
            meta.append(dim(" ".join("#" + t for t in tags[:3])))

        spot = tui.mark() if collapsing else None
        print("\n" + rule("="))
        # One line: the meta strip wrapping mid-tag split a chip across two
        # rows and pushed the question itself down the screen.
        print(ui.truncate("  " + "   ·   ".join(meta), ui.width()))
        print(rule())
        _print_lead_in(conn, q["id"])
        print(ui.question(asked, indent="  "))
        print()

        t0 = time.time()
        answer = None
        while answer is None:
            try:
                raw = input("  your answer (Enter reveals · s skip · q quit · ? keys) > ").strip()
            except EOFError:
                raw = "q"
            if _drill_side_command(conn, q, raw):
                continue
            answer = raw

        if answer.lower() in {":q", "q", "quit", "exit"}:
            quit_early = True
            queue.insert(0, q)
            break
        if answer.lower() in {"s", "skip", "pass"}:
            skips[q["id"]] = skips.get(q["id"], 0) + 1
            if skips[q["id"]] < 2:
                queue.append(q)
                if sid:
                    session.skip(conn, sid, q["id"])
                tail = "skipped, back in the queue"
            else:
                if sid:
                    session.record(conn, sid, q["id"], None, time.time() - t0)
                tail = "skipped twice, dropped from this sitting"
            if not tui.collapse(spot, [_drill_line(q, None, time.time() - t0, tail)]):
                print(dim("  " + tail))
            asked_n -= 1
            continue

        rating = None
        result = None
        if q["kind"] == "market_awareness":
            feedback, rating = _grade_market(conn, q, answer)
            print(feedback)
        elif answer and graded_mode:
            print(dim("\n  grading your answer against the rubric…"))
            try:
                result = grade_mod.grade(conn, q["id"], answer)
            except KeyboardInterrupt:
                # run_job raises this when you press esc during the call.
                result = None
                print(dim("  stopped waiting - rate it yourself below"))
            if result and "error" in result:
                _llm_problem(result["error"], result.get("hint", ""),
                             "could not grade that answer")
                print(dim("  falling back to self-rating"))
                result = None
            if result:
                print(f"\n  GRADE  {verdict(result['verdict'])}  {result['score']:.0%}" + " " * 12)
                # The same card the self-rated path shows, with the grader's
                # per-point verdict on it. It used to be a second, different
                # rendering of the same thing -- `hit `/`MISS` down the left --
                # so a marked sitting and an unmarked one looked like two
                # different tools.
                traps = json.loads(conn.execute(
                    "SELECT common_mistakes FROM answers WHERE question_id = ?",
                    (q["id"],)).fetchone()["common_mistakes"] or "[]")
                print(ui.answer_card(result["rubric"], hits=result["rubric_hits"],
                                     traps=traps))
                print()
                print(ui.body(result["feedback"], "  "))
                if result.get("followup"):
                    print("\n" + wrap("follow-up: " + result["followup"], "  "))
                    try:
                        input("  > ")
                    except EOFError:
                        pass
                rating = result["suggested_rating"]
            elif graded_mode and answer:
                print(dim("  not graded: no rubric on file for this one")
                      + dim("   enrich --missing-rubrics"))

        # Ungraded means revealed, whatever kind it is. The exception used to
        # be market-awareness, on the reasoning that a live question keeps
        # nothing to reveal -- true of a bound one, which is why `_reveal`
        # says "no answer on file", and false of the six that carry a full
        # answer and a rubric and no binding. Hiding it from them was the
        # reason they could not be studied at all.
        if rating is None:
            _reveal(conn, q)

        if rating is None:
            raw = ""
            while True:
                try:
                    raw = input("\nrate  1 again  2 hard  3 good  4 easy  (q quit) > ").strip().lower()
                except EOFError:
                    raw = "q"
                    break
                # `f`, `c` and `ca` re-prompt rather than counting as a rating.
                # Falling through would have scored a copy as a 3.
                if not _answer_extras(conn, q, raw):
                    break
            if raw == "q":
                quit_early = True
                queue.insert(0, q)
                break
            rating = int(raw) if raw in {"1", "2", "3", "4"} else 3
        else:
            if result:
                print("\n" + dim("  ") + _extras_hint(True))
            print(f"\n  auto-rated {verdict(str(rating))} (1 again, 2 hard, 3 good, 4 easy)")
            ov = ""
            while True:
                try:
                    ov = input("  [Enter to accept, 1-4 to override, q quit] > ").strip().lower()
                except EOFError:
                    ov = ""
                    break
                if not _answer_extras(conn, q, ov):
                    break
            if ov == "q":
                quit_early = True
                queue.insert(0, q)
                break
            if ov in {"1", "2", "3", "4"}:
                rating = int(ov)

        tally[str(rating)] += 1
        drill_records.append((q, rating))
        due = record_review(
            conn, q["id"], rating, phrasing=asked, user_answer=answer or None,
            score=(result or {}).get("score"),
            rubric_hits=(result or {}).get("rubric_hits"),
            grader=llm.model_grade() if result else "self",
        )
        if sid:
            session.record(conn, sid, q["id"], rating, time.time() - t0, graded=bool(result))
        # `due.date()` here read as "next due <today>" for every card in its
        # learning steps, which is most of a first sitting -- and the drill
        # that then refused it said the opposite. `due_phrase` says minutes
        # when it is minutes.
        tail = f"next due {scheduler.due_phrase(due)}"
        folded = tui.collapse(spot, [_drill_line(q, rating, time.time() - t0, tail)])
        if not folded:
            print(dim("  " + tail))
        elif len(drill_records) == 1:
            # Said once per sitting, the first time a block disappears. Without
            # it the fold reads as the drill having eaten the question.
            print(dim("  answered questions fold to one line   ·   ")
                  + head("recap") + dim(" reopens them"))

    if sid:
        if queue and quit_early:
            print(dim(f"\n  {len(queue)} left, saved   ·   ")
                  + head("superday drill --resume") + dim(" picks up here"))
        else:
            session.close(conn, sid)

    _drill_summary(conn, tally, drill_records, time.time() - start_time, len(queue))


def _drill_summary(conn, tally, drill_records, elapsed, left) -> None:
    done = sum(tally.values())
    if not done:
        return
    avg_sec = int(elapsed / done)
    mins, secs = int(elapsed // 60), int(elapsed % 60)
    c = analytics.counts(conn)
    st = analytics.streak(conn)

    lines = [
        f"{done} answered in {mins}m{secs:02d}s " + dim(f"(avg {avg_sec}s/question)"),
        "",
        head("RATINGS"),
    ]
    for key, (name, color) in ((str(k), v) for k, v in RATINGS.items()):
        n = tally[key]
        lines.append(f"  {color(key + ' ' + name.ljust(6))} {ui.meter(n / done, 14)}  "
                     f"{n:2d} " + dim(f"({n / done:4.0%})"))

    session_avg = sum(r for _, r in drill_records) / len(drill_records)
    lines.append("")
    lines.append(f"  session average {ui.style(f'{session_avg:.2f}/4.00', BOLD)}"
                 + dim(f"   ·   streak {st['current']}d   ·   {st['today']} today"))

    weak = [r for r in drill_records if r[1] <= 2]
    if weak:
        lines.append("")
        lines.append(head("WORTH ANOTHER LOOK"))
        for q, rating in sorted(weak, key=lambda x: x[1])[:4]:
            lines.append(f"  {bad(str(rating))}  " + dim(f"#{q['id']:<5}")
                         + ui.truncate(q["canonical_text"], 52))

    lines.append("")
    tail = f"{c['due_now']} still due   ·   {c['unseen']} never seen"
    if left:
        tail = f"{left} left in this sitting   ·   " + tail
    lines.append(dim("  " + tail))

    footer = (dim("next: ") + ui.style("[d] drill on", BOLD) + dim(" · ")
              + ui.style("[home] dashboard", BOLD) + dim(" · ")
              + ui.style("[m] mock", BOLD))
    print("\n" + ui.window("SITTING COMPLETE", lines, footer=footer))


def cmd_recap(conn: sqlite3.Connection, args) -> None:
    """What you have answered, and how it went.

    The other half of the fold: a sitting keeps one line per question on
    screen, and this is where the question, what you typed and how it was
    marked all still are.
    """
    sid = None
    if args.window and args.window.strip().lower() == "session":
        row = session.latest(conn)
        if row is None:
            print(dim("  no sitting yet - `drill` starts one"))
            return
        sid = row["id"]
        label = f"sitting #{sid}"
        since = None
    else:
        try:
            label, since = analytics.parse_window(args.window)
        except ValueError as e:
            print(bad(f"  {e}"))
            return

    rows = analytics.answered(conn, since=since, session_id=sid, limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print(dim(f"  nothing answered {label} - `drill` starts a sitting"))
        return
    _hand_off(views_mod.RecapView(conn, rows, window=label))


def cmd_chains(conn: sqlite3.Connection, args) -> None:
    """Question lines: the follow-ups that cannot be asked on their own."""
    if args.link:
        child, parent = args.link
        try:
            chains.link(conn, child, parent)
        except chains.LinkError as e:
            print(bad(f"  {e}"))
            return
        print(ok(f"  #{child} now follows #{parent}"))
        print(dim("  it will be asked after it when both come up, and the "
                  "lead-in shows above it either way"))
        return

    if args.unlink:
        if chains.unlink(conn, args.unlink):
            print(ok(f"  #{args.unlink} stands on its own again"))
        else:
            print(dim(f"  #{args.unlink} was not linked to anything"))
        return

    if args.standalone:
        chains.mark_standalone(conn, args.standalone)
        print(ok(f"  noted: #{args.standalone} reads like a follow-up but is fine alone"))
        return

    if args.graph:
        nodes = chains.graph(conn, args.graph)
        if not nodes:
            print(bad(f"  no question #{args.graph}"))
            return
        if getattr(args, "json", False):
            print(json.dumps(nodes, indent=2))
            return
        if len(nodes) == 1:
            print(dim(f"  #{args.graph} is not in a line - nothing follows it "
                      "and it follows nothing"))
            print(dim("  ") + head("chains --scan")
                  + dim(" finds the ones that read like a follow-up"))
            return
        _hand_off(views_mod.ChainGraphView(conn, nodes, args.graph))
        return

    if args.scan:
        found = chains.scan(conn, tier=args.tier)
        if getattr(args, "json", False):
            print(json.dumps(found, indent=2))
            return
        if not found:
            print(ok("  nothing unlinked reads like a follow-up"))
            return
        if args.apply:
            _chains_apply(conn, found)
            return
        # Grouped by tier, so each heading is drawn once: sorted by id the two
        # kinds interleave and the list becomes fourteen headings.
        found.sort(key=lambda f: (f["tier"] != "certain", f["id"]))
        rows = [dict(f, group=("names the question before it" if f["tier"] == "certain"
                               else "opens like a reply")) for f in found]
        orphans = sum(1 for f in found if f["orphan"])
        _hand_off(views_mod.ChainsView(
            conn, rows, title="FOLLOW-UPS",
            note="questions that read like a reply to the one before them"
                 + (f" · {orphans} have no active lead-in" if orphans else ""),
            tally=f"{len(found)} unlinked"))
        return

    linked = chains.lines(conn)
    if getattr(args, "json", False):
        print(json.dumps([[dict(r) for r in line] for line in linked], indent=2))
        return
    if not linked:
        print(dim("  no question lines recorded yet   ·   ")
              + head("chains --scan") + dim(" finds the candidates"))
        return
    rows = []
    for line in linked:
        head_q = line[0]
        label = f"#{head_q['id']}  " + " ".join(head_q["canonical_text"].split())[:60]
        for q in line[1:]:
            parent = next((p for p in line if p["id"] == q["parent_id"]), None)
            rows.append({
                "id": q["id"], "text": q["canonical_text"], "group": label,
                "parent_id": q["parent_id"], "linked": True,
                "parent_text": parent["canonical_text"] if parent else None,
                "why": [], "tier": None,
            })
    _hand_off(views_mod.ChainsView(
        conn, rows, title="QUESTION LINES",
        note="each one is asked after its lead-in, and shows it either way",
        tally=f"{len(linked)} lines · {len(rows)} follow-ups"))


def _chains_apply(conn: sqlite3.Connection, found: list[dict]) -> None:
    """Link every candidate that has a lead-in, after saying what that means."""
    doable = [f for f in found if f["parent_id"] and not f["orphan"]]
    if not doable:
        print(warn("  none of them have an active question in front of them"))
        return
    print(head(f"  about to link {len(doable)} questions to the one before them"))
    for f in doable[:8]:
        print(dim(f"    #{f['id']:<5} after #{f['parent_id']:<5} ")
              + ui.truncate(" ".join(f["text"].split()), 60))
    if len(doable) > 8:
        print(dim(f"    … {len(doable) - 8} more"))
    print(dim("  a link changes no text and no schedule: it decides what is "
              "shown above the question, and what order the two are asked in"))
    try:
        if input("  link them? [y/N] > ").strip().lower() not in {"y", "yes"}:
            print(dim("  left alone"))
            return
    except EOFError:
        print(dim("  left alone"))
        return
    done = 0
    for f in doable:
        try:
            chains.link(conn, f["id"], f["parent_id"])
            done += 1
        except chains.LinkError as e:
            print(warn(f"  #{f['id']}: {e}"))
    print(ok(f"  linked {done}"))


def cmd_sessions(conn: sqlite3.Connection, args) -> None:
    """What sittings have happened, and whether one can be picked back up."""
    rows = session.recent(conn, args.limit)
    open_one = session.resumable(conn, "drill")
    _hand_off(views_mod.SessionsView(conn, rows,
                                     open_one["id"] if open_one else None))


# ---------------------------------------------------------------- list

def _topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT q.topic AS topic, COUNT(*) AS active, "
        "SUM(CASE WHEN s.due_at IS NULL OR s.due_at <= ? THEN 1 ELSE 0 END) AS due, "
        "(SELECT AVG(rv.rating) FROM reviews rv JOIN questions q2 ON q2.id = rv.question_id "
        " WHERE q2.topic = q.topic AND rv.rating IS NOT NULL) AS avg_rating "
        "FROM questions q LEFT JOIN schedule s ON s.question_id = q.id "
        "WHERE q.status = 'active' GROUP BY q.topic ORDER BY active DESC",
        (now(),),
    ))


def _resolve_topic(conn: sqlite3.Connection, name: str) -> str | None:
    name = name.strip().lower()
    topics = [r["topic"] for r in _topics(conn)]
    if name in topics:
        return name
    matches = [t for t in topics if t.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(warn(f"  '{name}' matches: {', '.join(matches)} - be more specific"))
        return None
    print(bad(f"  no topic '{name}'. Run `list` to see what is available."))
    return None


def _mastery_bar(avg_rating: float | None, width: int = 16) -> str:
    if avg_rating is None:
        return dim("never drilled".ljust(width))
    frac = (avg_rating - 1) / 3
    filled = round(frac * width)
    bar = "#" * filled + "-" * (width - filled)
    color = ok if frac >= 0.66 else warn if frac >= 0.33 else bad
    return color(bar)


def cmd_list(conn: sqlite3.Connection, args) -> None:
    if not args.topic:
        rows = _topics(conn)
        if not rows:
            print(warn("no active questions yet. `superday review` to work the queue."))
            return
        print(section("TOPICS"))
        for r in rows:
            status = warn(f"{r['due']:>3} due") if r["due"] else dim("  up to date")
            print(f"  {r['topic']:<14} {r['active']:>4} active   {status}   "
                  f"{_mastery_bar(r['avg_rating'])}")
        print(rule())
        print(dim("  list <topic>  to drill a topic  ·  drill  for a mixed set"))
        return

    topic = _resolve_topic(conn, args.topic)
    if topic is None:
        return
    cmd_drill(conn, _drill_args(count=10, topic=topic))


# ---------------------------------------------------------------- stats

_STATUS_COLOR = {"active": ok, "needs_review": warn, "rejected": bad}


def cmd_stats(conn: sqlite3.Connection, args) -> None:
    """What is in the bank, where it came from, and how trustworthy it is.

    Five separate questions, so five tabs rather than five sections you have
    to scroll past to reach the one you wanted.
    """
    _show_tabs("BANK", [
        ("Composition", lambda w: _stats_composition(conn)),
        ("Sources", lambda w: _stats_sources(conn, w)),
        ("Corroborated", lambda w: _stats_corroborated(conn, w)),
        ("Weakest", lambda w: _stats_weakest(conn)),
        ("Provenance", lambda w: _stats_provenance(conn)),
    ])


def _stats_composition(conn: sqlite3.Connection) -> list[str]:
    out = ["  " + head("BY STATUS")]
    for r in conn.execute("SELECT status, COUNT(*) c FROM questions GROUP BY status"):
        colour = _STATUS_COLOR.get(r["status"], dim)
        out.append(f"    {r['c']:5d}  {colour(r['status'])}")
    out += ["", "  " + head("BY TOPIC")]
    rows = list(conn.execute(
        "SELECT topic, COUNT(*) c FROM questions WHERE status='active' "
        "GROUP BY topic ORDER BY c DESC"))
    biggest = max((r["c"] for r in rows), default=1)
    for r in rows:
        out.append(f"    {r['c']:5d}  {ui.pad(r['topic'], 14)}"
                   + ui.meter(r["c"] / biggest, 18))
    return out


def _stats_sources(conn: sqlite3.Connection, w: int) -> list[str]:
    out = []
    for r in conn.execute(
        "SELECT s.title, s.kind, COUNT(DISTINCT qs.question_id) c FROM sources s "
        "LEFT JOIN question_sources qs ON qs.source_id = s.id "
        "GROUP BY s.id ORDER BY c DESC"
    ):
        out.append(f"    {r['c']:5d}  " + ui.pad(dim("[" + r["kind"] + "]"), 10)
                   + ui.truncate(r["title"], max(20, w - 22)))
    return out or ["    " + dim("nothing ingested yet")]


def _stats_corroborated(conn: sqlite3.Connection, w: int) -> list[str]:
    out = [dim("  the same question found in the most independent sources"), ""]
    for r in conn.execute(
        "SELECT q.id, q.canonical_text, COUNT(DISTINCT qs.source_id) f FROM questions q "
        "JOIN question_sources qs ON qs.question_id = q.id WHERE q.status='active' "
        "GROUP BY q.id ORDER BY f DESC, q.id LIMIT 15"
    ):
        out.append(f"    {ok(str(r['f']) + 'x')}  " + ui.pad(dim(f"#{r['id']}"), 7)
                   + ui.truncate(" ".join(r["canonical_text"].split()), max(20, w - 20)))
    return out


def _stats_weakest(conn: sqlite3.Connection) -> list[str]:
    weak = [r for r in _topics(conn) if r["avg_rating"] is not None]
    if not weak:
        return ["    " + dim("nothing graded yet - drill something first")]
    weak.sort(key=lambda r: r["avg_rating"])
    return [dim("  lowest mean rating first - drill these")] + [""] + [
        f"    {ui.pad(r['topic'], 14)} {_mastery_bar(r['avg_rating'])}   "
        f"{r['avg_rating']:.2f}/4.00" for r in weak]


def _stats_provenance(conn: sqlite3.Connection) -> list[str]:
    a = conn.execute(
        "SELECT COUNT(*) n, SUM(audit_version > 0) audited, "
        "SUM(audit_verdict = 'reject') rejected, "
        "SUM(audit_verdict IS NOT NULL AND status = 'needs_review') held "
        "FROM questions WHERE kind != 'market_awareness'"
    ).fetchone()
    unchecked = (a["n"] or 0) - (a["audited"] or 0)
    out = [
        "  " + head("AUDIT"),
        f"    {a['audited'] or 0} of {a['n']} checked   "
        f"{bad(str(a['rejected'] or 0) + ' rejected')}   "
        f"{warn(str(a['held'] or 0) + ' held for you')}",
    ]
    if unchecked:
        out.append(warn(f"    {unchecked} never checked by the critic") + dim("   audit"))
    # Only PDFs ingested after the source-quote change carry a checked quote;
    # earlier rows hold the model's rewrite in this column, so count the
    # sources that can be trusted as evidence rather than the rows.
    g = conn.execute(
        "SELECT COUNT(DISTINCT qs.question_id) n FROM question_sources qs "
        "WHERE qs.verbatim_text IS NOT NULL").fetchone()
    active = conn.execute(
        "SELECT COUNT(*) n FROM questions WHERE status = 'active'").fetchone()["n"]
    out += ["", "  " + head("GROUNDING"),
            f"    {g['n']} of {active} active questions keep their source's own words"]
    if active and g["n"] < active:
        out.append(dim(f"    {active - g['n']} predate the grounding change") + dim("   reground"))
    return out


def _dash_topic_rows(conn: sqlite3.Connection, topics: list[dict]) -> list[str]:
    """Topic table with a difficulty heatmap strip on each row.

    Header and body are laid out from the same widths, so a wider mastery bar
    cannot drift out from under its own column label.
    """
    _, grid = analytics.difficulty_grid(conn)
    W_TOPIC, W_COV, W_MAST = 13, 13, 15
    header = ("  " + "topic".ljust(W_TOPIC) + "coverage".ljust(W_COV)
              + "mastery".ljust(W_MAST) + "1 2 3 4 5".ljust(12)
              + "bank".rjust(5) + "due".rjust(7))
    out = [dim(header)]
    for t in topics:
        m = analytics.mastery_frac(t["avg_rating"])
        cells = " ".join(ui.heat(grid.get((t["topic"], d))) for d in range(1, 6))
        cov = ui.pad(f"{ui.meter(t['coverage'], 8)} {t['coverage']:>3.0%}", W_COV)
        mast = ui.pad(f"{ui.meter(m, 10)} {m:>3.0%}" if m is not None
                      else dim("·" * 10) + "   -", W_MAST)
        due = warn(f"{t['due']:>7}") if t["due"] else ok("      0")
        out.append("  " + t["topic"].ljust(W_TOPIC) + cov + mast
                   + ui.pad(cells, 12) + dim(f"{t['active']:>5}") + due)
    return out


def _dash_retention(conn: sqlite3.Connection) -> list[str]:
    curve = analytics.retention_curve(conn)
    target = config_mod.load().get("desired_retention", 0.9)
    scored = [b for b in curve if b["n"]]
    if not scored:
        return [ui.dim("  not enough repeat reviews yet to measure retention")]
    out = []
    for b in curve:
        if not b["n"]:
            continue
        r = b["retention"]
        flag = ok("on target") if r >= target else warn(f"below {target:.0%}")
        out.append(f"  {b['bucket']:<7}{ui.meter(r, 12)} {r:>4.0%}  "
                   + dim(f"n={b['n']:<4}") + flag)
    return out


def _dash_upcoming(conn: sqlite3.Connection, width: int = 0,
                   days: int = 14) -> list[str]:
    """What the next fortnight holds, in the two quantities it comes in.

    A day row and not a sparkline, because a sparkline of fourteen values
    could only be labelled underneath for the first seven of them, and the
    digits did not line up with the bars they were labelling.

    The first-pass pool is drawn beside the schedule rather than inside it.
    Those questions are due -- you have never opened them -- but no day owns
    them, and the version that only counted `schedule` reported an empty
    fortnight on the same screen as "due now 1081".
    """
    target = plan_mod.target_date()
    today = datetime.now(timezone.utc).date()
    if target:
        # Show every day up to the date when that is close enough to draw, so
        # the last row on the screen is the interview rather than an arbitrary
        # fortnight that stops short of it.
        days = max(2, min(28, (target - today).days + 1))
    f = analytics.upcoming(conn, days)
    pace = plan_mod.seconds_per_question(conn)
    out = [
        "  " + (f"the schedule has {ok(str(f['scheduled']))} review"
                f"{'' if f['scheduled'] == 1 else 's'} dated in the next {days} days"
                if f["scheduled"] else dim("the schedule has nothing dated ahead")),
    ]
    if f["beyond"]:
        out.append(dim(f"  {f['beyond']} more sit past the end of the window"))
    out.append("")

    W_BAR = 22
    month = ""
    for i, r in enumerate(f["days"]):
        n = r["reviews"]
        # A bare day number is ambiguous the moment the window crosses a month
        # boundary, and a 28-day window nearly always does.
        if r["date"][:7] != month:
            month = r["date"][:7]
            out.append(dim(f"  {datetime.fromisoformat(r['date']):%B}".lower()))
        day = f"{r['weekday']} {r['date'][-2:]}"
        label = ui.pad(ui.style(day, BOLD) if i == 0 else dim(day), 7)
        bar = (ui.meter(n / f["peak"], W_BAR) if f["peak"] and n
               else dim("\u00b7" * W_BAR))
        count = ui.pad(warn(f"{n:>4}") if n else dim("   -"), 5)
        mins = dim(f"{round(n * pace / 60):>4}m") if n else dim("     ")
        note = ""
        if i == 0 and f["overdue"]:
            note = dim(f"   {f['overdue']} overdue, folded into today")
        if target and r["date"] == target.isoformat():
            note = "   " + ui.chip("SUPERDAY", "coral")
        out.append(f"  {label}{bar}{count}{mins}{note}")

    out.append("")
    out.append("  " + head("FIRST PASS"))
    if not f["unseen"]:
        out.append("  " + ok("  every active question has been opened at least once"))
        return out
    hours = f["unseen"] * pace / 3600
    left = (target - today).days if target else days
    per_day = -(-f["unseen"] // max(1, left))          # ceil
    out.append(f"    {warn(str(f['unseen']))} never opened"
               + dim(f" \u00b7 {hours:.0f}h at your pace of {pace}s a question"))
    where = (f"in the {left} days to {target.isoformat()}" if target
             else "inside this window")
    out.append(dim(f"    {per_day} a day clears them {where}, "
                   f"about {round(per_day * pace / 60)} min")
               + ("" if target else
                  dim("   settings interview_date sets the real one")))
    for line in ui.wrap("they are on no date at all: the schedule starts "
                        "tracking one the first time you answer it",
                        "", max(30, (width or ui.width()) - 6)).split("\n"):
        out.append(dim("    " + line))
    return out


def cmd_dashboard(conn: sqlite3.Connection, args) -> None:
    """One screen that answers: am I ready, and what do I do next.

    Split into tabs rather than stacked into one forty-row wall. Each pane is
    a separate question and each one now gets the whole frame; the detail
    panes are also built lazily, so opening the dashboard no longer pays for
    a retention curve you were not going to read.

    Every pane reads the database itself rather than closing over numbers
    worked out here. That is what makes the screen survive its own actions: a
    drill started from `DO THIS NEXT` moves the very counts the pane behind it
    is showing, and a closure over a dict computed before the sitting would
    come back quoting the figures that sent you into it.
    """
    if getattr(args, "json", False):
        print(json.dumps({
            "counts": analytics.counts(conn),
            "readiness": analytics.readiness(conn),
            "streak": analytics.streak(conn),
            "topics": analytics.topic_mastery(conn),
            "retention": analytics.retention_curve(conn),
            "upcoming": analytics.upcoming(conn, 14),
            "card_health": analytics.card_health(conn),
        }, indent=2))
        return

    tabs = [
        ("Overview", lambda w: _dash_overview(conn)),
        ("Mastery", lambda w: _dash_topic_rows(conn, analytics.topic_mastery(conn))),
        ("Retention", lambda w: _dash_retention(conn)),
        ("Momentum", lambda w: _dash_momentum(conn, analytics.streak(conn))),
        ("Upcoming", lambda w: _dash_upcoming(conn, w)),
    ]

    def subject() -> str:
        score = analytics.readiness(conn)["score"]
        line = f"{score:.0%} {analytics.band(score)}"
        target = plan_mod.target_date()
        if target:
            days = (target - datetime.now(timezone.utc).date()).days
            line += f"  ·  {days} day{'' if days == 1 else 's'} to {target.isoformat()}"
        return line

    _show_tabs("READINESS", tabs, subject=subject)


def _show_tabs(title: str, tabs: list, subject="") -> None:
    """Hand a tabbed screen to the shell, or print every pane if there is none."""
    view = views_mod.TabsView(title, tabs, subject=subject)
    if tui.attach(view):
        return
    for line in view.flatten(ui.width()):
        print(line)


def _dash_overview(conn: sqlite3.Connection) -> "views_mod.Pane":
    c = analytics.counts(conn)
    topics = analytics.topic_mastery(conn)
    ready = analytics.readiness(conn)
    health = analytics.card_health(conn)
    score = ready["score"]
    mast = ready["mastery"]
    lines = [
        "  " + ui.meter(score, 24) + "  " + ui.style(f"{score:>4.0%}", BOLD)
        + "   " + dim(analytics.band(score)),
        "  " + dim(f"seen {ready['coverage']:.0%} of the bank") + dim(" · ")
        + dim(f"scoring {mast:.0%} on what you have seen" if mast is not None
              else "nothing graded yet"),
        "",
    ]
    left = [
        head("BANK"),
        f"  active        {ok(str(c['active']))}",
        f"  never seen    {warn(str(c['unseen'])) if c['unseen'] else ok('0')}",
        f"  pending QA    {warn(str(c['needs_review'])) if c['needs_review'] else dim('0')}",
        f"  rejected      {dim(str(c['rejected']))}",
    ]
    right = [
        head("SCHEDULE"),
        f"  due now       {warn(str(c['due_now'])) if c['due_now'] else ok('0')}",
        f"  due in 7d     {c['due_7d']}",
        f"  reviews       {c['reviews']}",
        f"  avg stability {health['avg_stability']:.1f}d" if health["avg_stability"]
        else "  avg stability -",
    ]
    lines.extend("  " + row for row in ui.columns(left, right, gap=4, left_w=34))
    return views_mod.Pane(lines, _focus_actions(conn, topics, c))


def _dash_momentum(conn: sqlite3.Connection, st: dict) -> list[str]:
    activity = analytics.daily_activity(conn, 21)
    counts_21 = [float(d["count"]) for d in activity]
    streak_str = (ok(f"{st['current']} day streak") if st["current"] >= 2
                  else dim("no streak"))
    return [
        f"  {ui.sparkline(counts_21)}   {streak_str}"
        + dim(f" · best {st['longest']}d · {st['today']} today"),
        dim(f"  {int(sum(counts_21))} reviews in 21 days"),
    ]


def _focus_actions(conn: sqlite3.Connection, topics: list[dict],
                   c: dict) -> list[views_mod.Action]:
    """The two or three things actually worth doing right now, in order.

    A dashboard that only reports state makes you decide what to do with it
    every time. This decides -- and the order is a claim about cost. Drilling a
    known-wrong answer until it is memorised is the worst outcome this tool can
    produce, so it outranks everything, including having never opened a topic.

    They are `Action`s rather than lines because the version that printed
    `drill -t dcf` in grey had worked the whole thing out and then asked you
    to retype it. Everything that writes to the schedule carries an `arm`, so
    the row nearest the cursor still costs two presses.
    """
    items: list[tuple[int, views_mod.Action]] = []

    wrong = checks.scan(conn)
    if wrong:
        n = len(wrong)
        items.append((0, views_mod.Action(
            "check", bad(f"{n} answer{'s' if n != 1 else ''} provably wrong")
            + dim("   check them"), "check")))

    if c["needs_review"]:
        items.append((1, views_mod.Action(
            "review", f"{c['needs_review']} questions await QA"
            + dim("   work through them"), "review")))

    drilled = [t for t in topics if t["reviews"] >= 3 and t["avg_rating"] is not None]
    if drilled:
        weak = min(drilled, key=lambda t: t["avg_rating"])
        if weak["avg_rating"] < 3.0:
            items.append((2, views_mod.Action(
                "weak", "drill your weakest topic, " + head(weak["topic"])
                + dim(f"   {weak['avg_rating']:.1f}/4 over {weak['reviews']} reviews"),
                f"drill -t {weak['topic']}",
                arm=f"drill {weak['topic']}? ⏎ starts the sitting · ← backs out")))

    untouched = [t for t in topics if t["coverage"] == 0.0]
    if untouched:
        biggest = max(untouched, key=lambda t: t["active"])
        items.append((3, views_mod.Action(
            "unopened", "open " + head(biggest["topic"]) + " for the first time"
            + dim(f"   {biggest['active']} questions"),
            f"drill -t {biggest['topic']}",
            arm=f"drill {biggest['topic']}? ⏎ starts the sitting · ← backs out")))
    elif c["due_now"]:
        items.append((3, views_mod.Action(
            "due", f"drill the {c['due_now']} that are due", "drill",
            arm=f"drill {c['due_now']} due? ⏎ starts the sitting · ← backs out")))

    target = plan_mod.target_date()
    if target:
        pl = plan_mod.build(conn, target)
        if pl["feasible"]:
            items.append((1, views_mod.Action(
                "plan", f"{ok(str(pl['daily_total']))} a day to be ready by "
                + head(target.isoformat())
                + dim(f"   about {pl['minutes_per_day']} min"), "plan")))
        else:
            # Not fitting outranks everything except a wrong answer. It is the
            # one thing on this screen that no amount of drilling fixes, and
            # the longer it goes unsaid the fewer topics can still be reached.
            items.append((0, views_mod.Action(
                "plan", bad(f"{pl['daily_total']}/day does not fit before ")
                + head(target.isoformat())
                + dim(f"   {pl['unreachable']} would go untouched"), "plan")))

    hardest = analytics.weakest_questions(conn, 1)
    if hardest:
        h = hardest[0]
        items.append((4, views_mod.Action(
            "hardest", "look at the one you keep failing, "
            + dim(f"#{h['id']}  ") + ui.truncate(h["canonical_text"], 40),
            f"show {h['id']}")))

    items.sort(key=lambda t: t[0])
    return [act for _, act in items]


def cmd_reground(conn: sqlite3.Connection, args) -> None:
    if _needs_key("reground", "it re-reads the source PDF to repair provenance"):
        return
    root = corpus_dir()
    if args.path:
        p = Path(args.path).expanduser()
        files = [p] if p.is_file() else sorted(p.glob("**/*.pdf"))
    else:
        files = [Path(r["path"]) for r in conn.execute(
            "SELECT path FROM sources WHERE kind = 'pdf' AND path IS NOT NULL ORDER BY id")]

    totals = {"matched": 0, "quotes": 0, "phrasings": 0, "unmatched": 0,
              "ungrounded": 0, "skipped": 0}
    leftover = []
    for f in files:
        if not f.exists():
            print(warn(f"  missing on disk, skipping: {f.name}"))
            continue
        t = reground_mod.run(conn, f, window=args.window)
        for k in totals:
            totals[k] += t.get(k, 0)
        leftover += t.get("unmatched_list", [])

    print(rule())
    print(f"  {ok(str(totals['matched']))} matched into existing questions")
    print(f"  {totals['quotes']} provenance quotes written")
    print(f"  {totals['phrasings']} source phrasings recovered")
    print(f"  {totals['ungrounded']} extractions dropped as ungrounded")
    print(f"  {bad(str(totals['unmatched'])) if totals['unmatched'] else totals['unmatched']}"
          f" could not be matched and were NOT admitted")
    if totals["skipped"]:
        print(dim(f"  {totals['skipped']} chunks skipped: already repaired, no call made"))
    for line in leftover[: args.show]:
        print(dim(f"    {line}"))
    if len(leftover) > args.show:
        print(dim(f"    ... and {len(leftover) - args.show} more"))
    if leftover:
        print(warn("\n  These are either questions the first pass missed, or wordings that"))
        print(warn("  drifted too far to match. Review them before admitting anything:"))
        print(warn("  they are the only place this command could be losing content."))


def cmd_gate(conn: sqlite3.Connection, args) -> None:
    """What the admission gate did, and where it was closest to being wrong.

    The gate's errors are not spread evenly: it is right in the middle of the
    range and uncertain at the thresholds. So rather than dump every verdict,
    show the calls that sat nearest the line in both directions.
    """
    where, params = "", []
    if args.source:
        where = " WHERE c.source_id IN (SELECT id FROM sources WHERE title LIKE ?)"
        params = [f"%{args.source}%"]

    rows = list(conn.execute(
        f"SELECT verdict, COUNT(*) n FROM candidates c{where} GROUP BY verdict "
        "ORDER BY n DESC", params))
    if not rows:
        print(warn("no candidates logged yet. The gate records these from this version on;"))
        print(warn("anything ingested earlier predates the log."))
        return

    total = sum(r["n"] for r in rows)
    print(section(f"ADMISSION GATE   {total} candidates"))
    for r in rows:
        color = ok if r["verdict"] == "new" else dim
        verdict_col = color(f"{r['verdict']:10s}")
        print(f"  {r['n']:5d}  {verdict_col} {r['n'] / total:5.0%}")

    print("\n" + section("ADMITTED AS NEW, BUT CLOSEST TO AN EXISTING QUESTION"))
    print(dim("  (high similarity here means a duplicate the gate let through;"))
    print(dim("   0.00 means nothing in the bank was close enough to score)"))
    print(rule())
    for r in conn.execute(
        f"SELECT c.question_text, c.similarity FROM candidates c{where}"
        + (" AND" if where else " WHERE") +
        " c.verdict = 'new' ORDER BY c.similarity DESC LIMIT ?", params + [args.limit]
    ):
        flag = warn if r["similarity"] >= 0.8 else dim
        sim_col = flag(f"{r['similarity']:.2f}")
        print(f"  {sim_col}  {r['question_text'][:66]}")

    print("\n" + section("MERGED AWAY, BUT LEAST LIKE WHAT THEY MERGED INTO"))
    print(dim("  (low similarity here means a distinct question the gate swallowed)"))
    print(rule())
    for r in conn.execute(
        f"SELECT c.question_text, c.similarity, c.matched_id, q.canonical_text "
        f"FROM candidates c LEFT JOIN questions q ON q.id = c.matched_id{where}"
        + (" AND" if where else " WHERE") +
        " c.verdict IN ('duplicate', 'variant') ORDER BY c.similarity ASC LIMIT ?",
        params + [args.limit]
    ):
        flag = warn if r["similarity"] <= 0.3 else dim
        sim_col = flag(f"{r['similarity']:.2f}")
        print(f"\n  {sim_col}  {r['question_text'][:66]}")
        print(f"        into #{r['matched_id']}  {(r['canonical_text'] or '?')[:60]}")


def cmd_market(conn: sqlite3.Connection, args) -> None:
    if getattr(args, "refresh", False):
        print(dim("  pulling every provider into the cache..."))
        for provider, (went_well, outcome) in market.refresh(conn).items():
            print(f"  {provider:<10} " + (ok(outcome) if went_well else bad(outcome)))
        print(dim("  cached values keep market drills working offline"))

    n = market.seed(conn)
    print(ok(f"seeded {n} market awareness questions") if n
          else dim("no new market questions to seed"))

    print(section("LIVE BINDINGS"))
    for r in conn.execute(
        "SELECT q.canonical_text, b.provider, b.series_key, b.unit FROM live_bindings b "
        "JOIN questions q ON q.id = b.question_id ORDER BY b.provider, b.series_key"
    ):
        val, as_of, stale = market.value_for(conn, r["provider"], r["series_key"])
        # The as-of column is padded because the cadences differ - `2026-07`
        # next to `2026-08-14` walked the question text two cells left and
        # made the list look like two lists.
        stamp = ui.pad(f"({as_of})" if as_of else "(--)", 13)
        if val is None:
            shown, stamp = bad(f"{'--':>8}"), bad(stamp)
        elif stale:
            shown, stamp = warn(f"{val:>8}"), warn(stamp)
        else:
            shown, stamp = ok(f"{val:>8}"), dim(stamp)
        text = ui.truncate(r["canonical_text"], 44)
        print(f"  {shown}  {stamp}"
              + (ui.pad(text, 45) + warn(stale) if stale else text))

    for provider in market.PROVIDERS:
        age = market.cache_age(conn, provider)
        if age is None:
            print(dim(f"  {provider}: never fetched"))
        elif age > 36:
            print(warn(f"  {provider}: cache is {age / 24:.1f} days old"
                       "  ·  superday market --refresh"))


# ---------------------------------------------------------------- usage

# Where the real numbers live, per provider, and all of them behind a login.
# Google stopped publishing per-model free-tier limits in the docs at all. No
# table of limits is shipped in this file: guessing one and drawing a gauge
# against it would be the one thing this whole screen is built not to do.

USAGE_CAVEAT = (
    "these are the calls superday made, not what the provider says you have left. "
    "There is no endpoint that reports remaining quota."
)


def _usage_rows(args) -> list[dict]:
    rows = usage.entries()
    if getattr(args, "provider", None):
        rows = [r for r in rows if r.get("provider") == args.provider]
    return rows


def _usage_bar(used: int, limit: int, w: int = 18) -> str:
    """Only ever drawn against a limit you supplied. No limit, no gauge."""
    if not limit:
        return ""
    return "  " + ui.meter(min(1.0, used / limit), w) + f" {used}/{limit}"


def _usage_now(conn: sqlite3.Connection, rows: list[dict]) -> "views_mod.Pane":
    cfg = config_mod.load()
    rpm = int(cfg.get("rate_limit_rpm") or 0)
    rpd = int(cfg.get("rate_limit_rpd") or 0)
    minute = usage.within(rows, 60)
    hour = usage.within(rows, 3600)
    today = usage.since_midnight(rows)

    out = ["", "  " + head("CALLS MADE"), ""]
    for label, bunch, limit in (("last minute", minute, rpm),
                                ("last hour", hour, 0),
                                ("today, UTC", today, rpd)):
        line = ui.kv(label, f"{len(bunch):>4}", 14) + _usage_bar(len(bunch), limit)
        out.append(line)
    tokens = sum(r["total_tokens"] for r in today
                 if isinstance(r.get("total_tokens"), int))
    counted = sum(1 for r in today if isinstance(r.get("total_tokens"), int))
    out += ["", ui.kv("tokens today", f"{tokens:,}", 14)
            + dim(f"   from {counted} of {len(today)} calls that reported any")]

    if not (rpm or rpd):
        # No example number here. This is the screen whose entire argument is
        # that a guessed limit is worse than no limit, and `settings
        # rate_limit_rpd 1500` printed under that reads as a recommendation.
        out += ["", "  " + warn("no rate limit on file, so nothing is drawn against one"),
                "  " + dim("yours are at ") + head(llm.limits_url()),
                "  " + dim("then: ") + head("settings rate_limit_rpd <your number>")]
    refused = usage.refusals(today)
    # A quota belongs to a key, so a refusal is a statement by one provider and
    # is reported as one. The heading used to name whoever is configured *now*
    # over rows that could have come from anybody -- switch after being rate
    # limited and Gemini's 429 was filed under Claude's name, on the one screen
    # whose whole argument is that it reports what the provider actually said.
    who = sorted({r.get("provider") for r in refused if r.get("provider")})
    heading = _provider_phrase(who) if who else llm.provider_label()
    out += ["", "  " + head(f"WHAT {heading.upper()} ACTUALLY SAID"), ""]
    if refused:
        for name in who:
            mine = [r for r in refused if r.get("provider") == name]
            last = mine[-1]
            out.append("  " + bad(f"{len(mine)} call(s) refused today")
                       + dim(f" by {llm.label_for(name)}"))
            out.append("  " + dim(f"most recent {last['at'][11:16]}Z")
                       + (dim(f", it asked for {last['retry_after']:.0f}s")
                          if last.get("retry_after") else ""))
        out.append("  " + dim("a 429 is the only reading of your quota that is"))
        out.append("  " + dim("not an inference - everything above it is counting"))
    else:
        out.append("  " + ok("nothing refused today"))
    # One pane entry per line, never a string with newlines in it: TabsView
    # windows the pane by counting list entries, so an embedded newline is a
    # row the scroll maths does not know exists -- and it loses its indent.
    caveat = [dim(line) for line in ui.wrap(USAGE_CAVEAT, "  ", 72).split("\n")]
    return views_mod.Pane(out + [""] + caveat)


def _usage_callers(conn: sqlite3.Connection, rows: list[dict], w: int) -> list[str]:
    today = usage.since_midnight(rows)
    if not today:
        return ["", "  " + dim("no calls today")]
    out = ["", "  " + head("TODAY, BY WHAT ASKED"), "",
           "  " + dim(ui.pad("caller", 16) + ui.pad("calls", 7)
                      + ui.pad("failed", 8) + ui.pad("tokens", 12) + "avg")]
    for name, g in usage.tally(today, "caller"):
        avg = f"{g['seconds'] / g['calls']:.1f}s" if g["calls"] else "-"
        out.append("  " + ui.pad(ui.paint(name, "sky"), 16)
                   + ui.pad(str(g["calls"]), 7)
                   + ui.pad(bad(str(g["failed"])) if g["failed"]
                            else dim("0"), 8)
                   + ui.pad(f"{g['total_tokens']:,}" if g["total_tokens"]
                            else dim("-"), 12) + dim(avg))
    # By provider, and only once there is more than one to tell apart. The
    # question this answers exists exactly when you have switched: today's
    # count is one number spread across two vendors' quotas, and neither of
    # them is the number a rate limit applies to. With one provider in the log
    # the heading would restate the total under a second name.
    by_provider = usage.tally(today, "provider")
    if len(by_provider) > 1:
        out += ["", "  " + head("BY PROVIDER"), "",
                "  " + dim("each has its own quota - this is not one budget")]
        for name, g in by_provider:
            live = ok(" ← answering now") if name == llm.provider() else ""
            out.append("  " + ui.pad(ui.paint(llm.label_for(name), "sky"), 16)
                       + ui.pad(str(g["calls"]), 7)
                       + ui.pad(bad(str(g["failed"])) if g["failed"]
                                else dim("0"), 8)
                       + ui.pad(f"{g['total_tokens']:,}" if g["total_tokens"]
                                else dim("-"), 12) + live)

    out += ["", "  " + head("BY MODEL"), ""]
    for name, g in usage.tally(today, "model"):
        out.append("  " + ui.pad(ui.paint(name, "mauve"), 26)
                   + ui.pad(str(g["calls"]), 7)
                   + (f"{g['total_tokens']:,}" if g["total_tokens"] else dim("-")))

    # The log says what was spent. This says how much of the drilling spent
    # anything at all, which is the other half of the same question: pressing
    # Enter and self-rating is free, and typing an answer is not. The split was
    # computed by `analytics.grader_split` and drawn nowhere.
    split = analytics.grader_split(conn)
    if split["total"]:
        share = split["model"] / split["total"]
        out += ["", "  " + head("HOW YOU HAVE BEEN DRILLING"), "",
                "  " + ui.kv("self-rated", f"{split['self']:>5}", 14)
                + dim("   free"),
                "  " + ui.kv("model-graded", f"{split['model']:>5}", 14)
                + dim(f"   {share:.0%} of every answer you have given")]
    return out


def _usage_trouble(conn: sqlite3.Connection, rows: list[dict], w: int) -> list[str]:
    bad_rows = [r for r in rows if r.get("outcome") != "ok"][-30:]
    if not bad_rows:
        return ["", "  " + ok("nothing has failed in the whole log")]
    out = ["", "  " + head("FAILURES AND REFUSALS, NEWEST LAST"), ""]
    for r in bad_rows:
        mark = bad("429") if r.get("status") == 429 else warn(
            str(r.get("status") or "  -"))
        out.append("  " + dim(r["at"][5:16].replace("T", " ")) + "  " + mark
                   + "  " + ui.pad(ui.paint(r.get("caller", "?"), "sky"), 14)
                   + ui.truncate(dim(r.get("reason") or ""), max(20, w - 46)))
    return out


def cmd_usage(conn: sqlite3.Connection, args) -> None:
    """How much of the provider you have used today, from your own log.

    Not a quota reading, and drawn so it cannot be mistaken for one. See
    `USAGE_CAVEAT`.
    """
    rows = _usage_rows(args)
    if getattr(args, "clear", False):
        usage.clear()
        print(ok("  usage log cleared"))
        return
    if getattr(args, "json", False):
        today = usage.since_midnight(rows)
        print(json.dumps({
            "calls_logged": len(rows),
            "last_minute": len(usage.within(rows, 60)),
            "today": len(today),
            "refused_today": len(usage.refusals(today)),
            "by_caller": {name: g for name, g in usage.tally(today, "caller")},
            "note": USAGE_CAVEAT,
        }, indent=2))
        return
    dropped = usage.prune()
    if not rows:
        print(dim("  nothing logged yet - the log fills up as commands call out"))
        print(dim("  it lives at ") + head(str(usage.path())))
        return
    if dropped:
        print(dim(f"  trimmed {dropped} old rows from the log"))
    _show_tabs("USAGE", [
        ("Now", lambda w: _usage_now(conn, rows)),
        ("By caller", lambda w: _usage_callers(conn, rows, w)),
        ("Trouble", lambda w: _usage_trouble(conn, rows, w)),
    ], subject=f"{len(rows)} calls logged")


# ---------------------------------------------------------------- providers

def _probe_line(r: "llm.Probe", width: int) -> list[str]:
    """One endpoint's answer, as the one or two lines it deserves."""
    label = ui.pad(ui.paint(llm.provider_label(r.provider), "text", BOLD), 9)
    job = ui.pad(dim(r.job), 7)
    model = ui.pad(ui.paint(r.model or "-", "mauve"), 24)
    head_room = max(20, width - 42)
    if r.ok:
        return [f"  {label}{job}{model}" + ok("answered")
                + dim(f"   {r.seconds:.1f}s")]
    out = [f"  {label}{job}{model}" + bad(ui.truncate(r.message, head_room))]
    # The hint gets its own full-width lines rather than a column under the
    # model, because what it holds is a command to run and a URL to open, and
    # a truncated URL is not a URL. This is the screen you are on *because*
    # something is broken; it is the one place not to save four columns.
    if r.hint:
        out += [dim(line) for line in ui.wrap(r.hint, " " * 9, width).split("\n")]
    return out


def _llm_test(names: list[str]) -> None:
    """Spend the smallest call there is on each key, and say what came back.

    There is no endpoint that validates a key without using it, so this is a
    real call to a real model -- which is the point. "The key parses" is not
    the question anybody has; "will `enrich` work when I start it" is, and the
    ways that fails are a revoked key, an empty balance, a model this account
    cannot see and a network that cannot reach the host. All four are only
    answerable by asking.
    """
    print()
    for name in names:
        for r in llm.probe(name):
            for line in _probe_line(r, ui.width()):
                print(line)
    print()
    print(dim("  a key that answers here is a key every command can use; "
              "these calls are logged in `usage` like any other"))


def _llm_use(name: str) -> None:
    """Switch provider, and say what that actually changed."""
    entry = _settings_find("llm_provider")
    _settings_set(entry, name)
    spec = llm.PROVIDERS[name]
    print(dim(f"  every job now goes to {spec['label']}:"))
    for job in llm.JOBS:
        model = llm.model_for(job, name)
        print(dim(f"    {ui.pad(job, 8)}") + (ui.paint(model, "mauve") if model
              else dim("- it sells none, `find` searches by keyword -")))
    if not llm.available(name):
        print()
        print(warn(f"  no {spec['key']} yet, so nothing can call out"))
        print(dim(f"  set one:  settings {spec['setting']} <key>   ·   {spec['console']}"))
    else:
        print(dim(f"  check it works:  llm --test {name}"))


def cmd_llm(conn: sqlite3.Connection, args) -> None:
    """Which provider answers, what it would cost to switch, and does the key work.

    The settings table lists eight LLM keys as eight unrelated rows. This is
    the same information arranged around the only question actually asked of
    it -- who is answering, and would somebody else answer if I let them.
    """
    if args.use:
        _llm_use(args.use)
        return
    if args.test:
        if args.test == "all":
            # Every key you actually hold. Probing one you have not configured
            # would spend nothing and report "no key", which `llm` already
            # says on the row without making a request to find out.
            names = llm.configured()
            if not names:
                print(warn("  no keys set, so there is nothing to test"))
                print(dim("  `llm` lists all three and where to get one"))
                return
        elif args.test in llm.PROVIDERS:
            names = [args.test]
        else:
            # Falling through to the configured provider would answer a
            # question about Claude with a call to Gemini and report it as
            # Claude's answer.
            print(bad(f"  no provider called '{args.test}'"))
            print(dim("  it is one of: " + ", ".join(llm.PROVIDERS)))
            return
        _llm_test(names)
        return
    rows = llm.overview()
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    _hand_off(views_mod.ProvidersView(rows))


# ---------------------------------------------------------------- settings

# Every knob superday reads, in one place. "file" settings live in
# config.local.json (via ib/config.py); "env" settings live in .env.local
# (via ib/llm.py's load_env), because a secret has no business in JSON that
# might get glanced at over someone's shoulder in a diff.
SETTINGS = [
    dict(key="corpus_dir", store="file", kind="path", group="corpus",
         help="where ingest / ingest-pdf / reground look for source files"),
    dict(key="inbox_dir", store="file", kind="path", group="corpus",
         help="drop zone directory for new files"),
    dict(key="ingest_globs", store="file", kind="list", group="corpus",
         help="glob patterns under corpus_dir worth ingesting"),
    dict(key="exclude_globs", store="file", kind="list", group="corpus",
         help="glob patterns to skip"),
    dict(key="desired_retention", store="file", kind="float", group="review",
         help="spaced-repetition target retention, 0 to 1"),
    dict(key="grade_mode", store="file", kind="choice", group="review",
         choices=["auto", "off"],
         help="auto grades a typed answer via the LLM; off keeps drilling offline"),
    # Colour lives in its own group rather than under REVIEW: it is the only
    # setting that changes nothing about what the tool does, and burying a
    # look-and-feel knob among the scheduling ones is how it stays unfound.
    dict(key="theme", store="file", kind="choice", group="look",
         choices=list(theme_mod.THEMES),
         help="colour palette and the background the shell paints behind it"),
    dict(key="drill_scrollback", store="file", kind="choice", group="review",
         choices=["fold", "full"],
         help="fold folds an answered question to one line; `recap` reopens it"),
    dict(key="interview_date", store="file", kind="date", group="review",
         help="the date you are preparing for; `plan` and the countdown read it"),
    dict(key="sec_contact", store="file", kind="str", group="corpus",
         help="contact address the SEC requires from automated clients"),
    dict(key="export_md_dir", store="file", kind="path", group="corpus",
         help="set this and the Markdown export refreshes itself on every change"),
    # Who answers. Every other LLM setting is read relative to this one, so
    # this row is first: the model defaults below change when it changes, and
    # a key belonging to a provider you are not using is not a missing key.
    dict(key="llm_provider", store="env", env="IB_PROVIDER", kind="choice",
         group="llm", choices=list(llm.PROVIDERS), default=llm.DEFAULT_PROVIDER,
         help="which provider answers: gemini, claude or openai"),
    dict(key="gemini_api_key", store="env", env="GEMINI_API_KEY", kind="secret",
         group="llm", help="Google AI Studio key - aistudio.google.com/apikey"),
    # `cross-audit --api` was the one key the tool asked for and `settings`
    # could not set, so the only way to supply it was to hand-edit .env.local
    # -- which is exactly what `settings` exists to stop, and which loses the
    # 0600 the writer applies.
    dict(key="anthropic_api_key", store="env", env="ANTHROPIC_API_KEY",
         kind="secret", group="llm",
         help="Anthropic key - console.anthropic.com/settings/keys"),
    dict(key="openai_api_key", store="env", env="OPENAI_API_KEY",
         kind="secret", group="llm",
         help="OpenAI key - platform.openai.com/api-keys"),
    # `default` is a callable here so the row re-reads the provider every time
    # the page is drawn. Bound to a value it would show whichever vendor's
    # model was current at import, which is the same staleness bug the
    # accessors in llm.py exist to avoid, one screen further out.
    #
    # `job` marks the four that are stored *per provider*: a model name only
    # means something next to the vendor that sells it, so one shared setting
    # carried the old vendor's model across a switch and 404'd every call
    # after it. Each of these writes IB_MODEL_<JOB>_<PROVIDER>, and reads the
    # one belonging to whoever is answering -- see `llm.model_for`.
    dict(key="model_enrich", store="env", env="IB_MODEL_ENRICH", kind="str",
         group="llm", job="enrich", default=lambda: llm.default_model("enrich"),
         help="model used by `enrich` and by extraction, for the current provider"),
    dict(key="model_grade", store="env", env="IB_MODEL_GRADE", kind="str",
         group="llm", job="grade", default=lambda: llm.default_model("grade"),
         help="model used by `drill` / `mock` grading, for the current provider"),
    dict(key="model_audit", store="env", env="IB_MODEL_AUDIT", kind="str",
         group="llm", job="audit", default=lambda: llm.default_model("audit"),
         help="model used by `audit`; not the extraction model, on purpose"),
    dict(key="model_embed", store="env", env="IB_MODEL_EMBED", kind="str",
         group="llm", job="embed",
         default=lambda: llm.default_model("embed") or "none",
         help="model used by `find --semantic`; Claude sells no embeddings"),
    dict(key="thinking_level", store="env", env="IB_THINKING_LEVEL",
         kind="choice", group="llm", choices=list(llm.THINKING_LEVELS),
         default=f"{llm.THINKING_BULK} for extract/enrich/audit",
         help="reasoning budget asked for; the main cost lever"),
    dict(key="min_call_interval", store="env", env="IB_MIN_CALL_INTERVAL",
         kind="float", group="llm", default=str(llm.DEFAULT_MIN_CALL_INTERVAL),
         help="floor between LLM calls, in seconds"),
    dict(key="rate_limit_rpm", store="file", kind="int", group="llm",
         help="your key's requests-per-minute cap; 0 leaves `usage` counting only"),
    dict(key="rate_limit_rpd", store="file", kind="int", group="llm",
         help="your key's requests-per-day cap; your provider's console has it"),
]

_SETTING_GROUPS = [("corpus", "CORPUS & INGEST"), ("review", "REVIEW"),
                   ("look", "APPEARANCE"), ("llm", "LLM")]


def _settings_find(key: str) -> dict | None:
    key = key.strip().lower().replace("-", "_")
    for e in SETTINGS:
        if e["key"] == key:
            return e
    matches = [e for e in SETTINGS if e["key"].startswith(key)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(warn(f"  '{key}' matches: {', '.join(m['key'] for m in matches)} "
                    "- be more specific"))
        return None
    print(bad(f"  no such setting '{key}'. Run `settings` to see what's available."))
    return None


def _settings_local() -> dict:
    return json.loads(config_mod.local_config().read_text()) if config_mod.local_config().exists() else {}


def _entry_default(entry: dict) -> str:
    """What a setting falls back to, resolved now rather than at import.

    The model rows depend on `llm_provider`, so their default is a callable:
    read once at import it would show whichever vendor's model happened to be
    configured then and keep showing it after the provider changed -- the same
    staleness the accessors in llm.py exist to avoid, one screen further out.
    """
    d = entry.get("default", "not set")
    return str(d() if callable(d) else d)


def _settings_value(entry: dict, local: dict) -> tuple[str, str]:
    """Return (display value, where it came from)."""
    if entry["store"] == "file":
        if entry["key"] == "corpus_dir" and os.environ.get("IB_CORPUS_DIR"):
            return os.environ["IB_CORPUS_DIR"], "env IB_CORPUS_DIR"
        overridden = entry["key"] in local
        raw = local.get(entry["key"], config_mod.DEFAULTS.get(entry["key"]))
        disp = ", ".join(raw) if entry["kind"] == "list" else str(raw)
        # An empty string left a blank cell, which on a table of configuration
        # reads as a rendering fault rather than as "nothing set here" -- and
        # the env half of this function already answered the same question
        # with the words "not set". Numbers are left alone: `rate_limit_rpm`
        # is deliberately 0 and 0 is its real value, not the absence of one.
        if not disp:
            disp = "not set"
        return disp, ("config.local.json" if overridden else "default")

    llm.load_env()
    if entry.get("job"):
        # Stored per provider, so the variable to read is not a constant. The
        # value shown is the one the next call will really use -- resolved by
        # the same function the call resolves it with, rather than by reading
        # a variable this screen hopes is the right one.
        job = entry["job"]
        own = os.environ.get(llm.model_key(job))
        value = llm.model_for(job) or "none"
        return value, (".env.local" if own else "default")
    val = os.environ.get(entry["env"])
    if val is None:
        return _entry_default(entry), "default"
    if entry["kind"] == "secret":
        disp = f"set (…{val[-4:]})" if len(val) > 4 else "set"
    else:
        disp = val
    return disp, ".env.local"


def _env_file_set(key: str, value: str) -> None:
    path = config_mod.home() / ".env.local"
    lines = path.read_text().splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    # Create it 0600 rather than write-then-chmod: between the two there is a
    # moment where a world-readable file holds an API key, and this is the one
    # file in the repo that holds one.
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    path.write_text("\n".join(out) + "\n")
    os.chmod(path, 0o600)
    os.environ[key] = value


def _env_file_unset(key: str) -> None:
    path = config_mod.home() / ".env.local"
    if not path.exists():
        return
    lines = [l for l in path.read_text().splitlines() if not l.strip().startswith(f"{key}=")]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.environ.pop(key, None)


def _settings_set(entry: dict, raw_value: str) -> None:
    if entry["kind"] == "float":
        try:
            parsed: object = float(raw_value)
        except ValueError:
            print(bad(f"  '{raw_value}' is not a number"))
            return
        if entry["key"] == "desired_retention" and not (0 < parsed <= 1):
            print(bad("  desired_retention must be between 0 and 1"))
            return
    elif entry["kind"] == "int":
        try:
            parsed = int(raw_value.strip().replace(",", ""))
        except ValueError:
            print(bad(f"  '{raw_value}' is not a whole number"))
            return
        if parsed < 0:
            print(bad(f"  {entry['key']} cannot be negative"))
            return
    elif entry["kind"] == "list":
        parsed = [v.strip() for v in raw_value.split(",") if v.strip()]
    elif entry["kind"] == "choice":
        parsed = raw_value.strip().lower()
        if parsed not in entry["choices"]:
            print(bad(f"  {entry['key']} must be one of: {', '.join(entry['choices'])}"))
            return
    elif entry["kind"] == "date":
        # Resolved on the way in, never on the way out. `+3 weeks` stored
        # literally would mean a different day every morning, which is the one
        # thing a deadline may not do -- and the countdown would never move.
        raw = raw_value.strip()
        if not raw:
            parsed = ""
        else:
            day = plan_mod.parse_target(raw)
            if day is None:
                print(bad(f"  could not read {raw!r} as a date"))
                print(dim("  try: 2026-09-15 · +14d · 3 weeks · sep 15 · tomorrow"))
                return
            if day <= datetime.now(timezone.utc).date():
                print(bad(f"  {day.isoformat()} is not in the future"))
                return
            parsed = day.isoformat()
            if parsed != raw:
                print(dim(f"  {raw} resolved to {parsed}"))
    else:
        parsed = raw_value

    if entry["store"] == "file":
        data = _settings_local()
        data[entry["key"]] = parsed
        config_mod.local_config().write_text(json.dumps(data, indent=2) + "\n")
    elif entry.get("job"):
        name = llm.provider()
        owner = llm.vendor_of(str(parsed))
        # A model belongs to exactly one vendor, and setting one vendor's model
        # while another is answering is never what was meant: it is either the
        # wrong model or the wrong provider, and both are worth one sentence
        # now rather than a 404 on every call for the rest of the session.
        # A name nothing recognises is honoured -- a fine-tune or a preview
        # alias is a model name you typed and meant.
        if owner and owner != name:
            print(bad(f"  {parsed} is a {llm.provider_label(owner)} model and "
                      f"{llm.provider_label(name)} is answering"))
            print(dim(f"  switch first:  llm --use {owner}"
                      f"   ·   then:  settings {entry['key']} {parsed}"))
            return
        _env_file_set(llm.model_key(entry["job"], name), str(parsed))
        print(ok(f"  {entry['key']} set for {llm.provider_label(name)}"))
        print(dim(f"  the other providers keep their own; `llm` lists them"))
        return
    else:
        _env_file_set(entry["env"], str(parsed))
    if entry["key"] == "theme":
        _apply_theme(str(parsed))
    print(ok(f"  {entry['key']} set"))


def _apply_theme(name: str) -> None:
    """Swap the palette and make the frame that is already up redraw in it.

    The redraw is not housekeeping: `Screen` only writes the rows that changed
    since the last frame, and a theme change moves no row's *text* -- so
    without dropping that cache the new palette would arrive one row at a
    time, as you happened to scroll each one.

    What repaints is the frame's own chrome and everything printed from here
    on. Scrollback above does not change colour, and that is not a bug being
    tolerated: the transcript stores lines with their styling already resolved,
    which is what lets `find` hand the same bytes to a pipe. Re-tinting text
    that has already been written would mean keeping every line's semantic
    intent alongside it forever, to restyle history nobody is reading.
    """
    ui.set_theme(name)
    tui.repaint()


def _settings_reset(entry: dict) -> None:
    if entry["store"] == "file":
        data = _settings_local()
        if entry["key"] not in data:
            print(dim(f"  {entry['key']} already at default"))
            return
        del data[entry["key"]]
        config_mod.local_config().write_text(json.dumps(data, indent=2) + "\n")
    elif entry.get("job"):
        name = llm.provider()
        _env_file_unset(llm.model_key(entry["job"], name))
        print(ok(f"  {entry['key']} reset to {llm.provider_label(name)}'s default"
                 f": {llm.default_model(entry['job'], name) or 'none'}"))
        return
    else:
        _env_file_unset(entry["env"])
    if entry["key"] == "theme":
        _apply_theme(theme_mod.DEFAULT)
    print(ok(f"  {entry['key']} reset to default"))


def _settings_page() -> None:
    local = _settings_local()
    llm.load_env()
    lines: list[str] = []
    lines.append(head("KEY".ljust(22)) + head("VALUE".ljust(30)) + head("STATUS"))
    lines.append(dim("─" * 20 + "  " + "─" * 28 + "  " + "─" * 12))

    for gid, title in _SETTING_GROUPS:
        lines.append("")
        lines.append(ui.paint(f"▶ {title}", "accent", BOLD))
        for entry in (e for e in SETTINGS if e["group"] == gid):
            disp, source = _settings_value(entry, local)
            status_tag = dim("default") if source == "default" else ok(source)
            # ui.truncate, not a slice: `disp[:28]` cut `corpus_dir` to
            # "/Users/me/Desktop/I" with nothing to say it had been cut, so a
            # truncated path read as a real one and the screen you check a
            # setting on was the screen lying about it. ljust also counts
            # code points rather than cells, and these values are paths a
            # user chooses.
            lines.append(
                ui.paint(entry["key"].ljust(22), "sky")
                + ui.pad(ui.truncate(disp, 28), 30)
                + status_tag
            )
    footer = (ui.style("settings <key> <value>", BOLD) + ui.dim("  ·  ")
              + ui.style("--reset", BOLD) + ui.dim(" reverts") + ui.dim("  ·  ")
              + ui.style("llm", BOLD) + ui.dim(" for providers"))
    print(ui.window("SETTINGS & CONFIGURATION", lines, footer=footer))


def cmd_settings(conn: sqlite3.Connection, args) -> None:
    if not args.key:
        _settings_page()
        return
    entry = _settings_find(args.key)
    if entry is None:
        return
    if args.reset:
        _settings_reset(entry)
        return
    if args.value:
        _settings_set(entry, " ".join(args.value))
        return
    disp, source = _settings_value(entry, _settings_local())
    print(ui.kv(entry["key"], disp, width=18))
    print(dim(f"  {entry['help']}"))
    if entry.get("job"):
        name = llm.provider()
        print(dim(f"  source: {source}  ·  {llm.provider_label(name)} only, "
                  f"stored as {llm.model_key(entry['job'], name)}"))
        stale = llm.stale_override(entry["job"], name)
        if stale:
            # Ignoring it is right for the call and wrong for this screen: a
            # setting that stopped applying and still reads as set is the one
            # thing worse than a setting that was never there.
            print(warn(f"  ignoring IB_MODEL_{entry['job'].upper()}={stale}"
                       f" - that is not a {llm.provider_label(name)} model"))
        return
    print(dim(f"  source: {source}"))


# ---------------------------------------------------------------- cross-audit

def _print_disagreement(conn: sqlite3.Connection, r: dict, n: int, total: int) -> None:
    sev = crossaudit.severity(r["g_verdict"], r["c_verdict"])
    # The banner names the pass, not a vendor: with `llm_provider` on OpenAI
    # and the second opinion from Claude, "CLAUDE REJECTS" over a Gemini-era
    # row was a sentence about whichever vendor happened to be hardcoded here.
    flag = (bad("  DRILLED, THE SECOND PASS REJECTS")
            if sev == 0 and r["status"] == "active" else "")
    print("\n" + rule("="))
    print(f"{n}/{total}   {head('#' + str(r['id']))}  [{r['topic']}]  "
          f"{dim(r['status'])}{flag}")
    print(rule())
    print(ui.question(r["canonical_text"]))
    print()
    # Both sides labelled the same way: which pass, then who gave it. One row
    # read "first pass" and the other read the raw stored name (`claude-code`),
    # so the two halves of a comparison were captioned in two different
    # vocabularies -- one about the pass, one about the transport.
    first = _provider_phrase([r["g_provider"]]) if r.get("g_provider") else ""
    second = _provider_phrase([r["c_provider"]]) if r.get("c_provider") else ""
    # One width for both, because the two lines are read as a pair and a
    # ragged left edge is the thing that stops them reading as one.
    w = 21
    print(ui.kv(f"first pass{f' ({first})' if first else ''}",
                f"{verdict(r['g_verdict'])}  {dim(r['g_reason'] or '')}"[:ui.W + 20],
                width=w))
    conf = r["c_confidence"] or 0.0
    held = warn("  (held: low confidence)") if conf < crossaudit.AUTO_APPLY_AT else ""
    print(ui.kv(f"second pass{f' ({second})' if second else ''}",
                f"{verdict(r['c_verdict'])}  {conf:.2f}{held}", width=w))
    if r["c_reason"]:
        # Indented to the value column the two verdict lines share, not to a
        # hardcoded fifteen that used to match a shorter label.
        print(ui.body(r["c_reason"], " " * (w + 3)))
    a = conn.execute("SELECT answer_key FROM answers WHERE question_id = ?",
                     (r["id"],)).fetchone()
    if a and a["answer_key"]:
        print(f"\n  {dim('answer on file')}")
        print(ui.body(a["answer_key"], "    "))
    if r["corrected_answer"]:
        second = _provider_phrase([r["c_provider"]]) if r.get("c_provider") else ""
        print(f"\n  {ok((second or 'the second pass') + ' would say')}")
        print(ui.body(r["corrected_answer"], "    "))


def cmd_cross_audit(conn: sqlite3.Connection, args) -> None:
    """An independent second opinion, stored beside the first rather than over it."""
    if args.import_path:
        path = Path(args.import_path).expanduser()
        if not path.exists():
            print(bad(f"no such file: {path}"))
            return
        try:
            res = crossaudit.import_verdicts(conn, path)
        except (ValueError, json.JSONDecodeError) as e:
            print(bad(f"could not read that file: {e}"))
            return
        t = res["tally"]
        print(f"  stored {ok(str(res['stored']))} verdicts   "
              f"{ok('keep')} {t['keep']}  {warn('fix')} {t['fix']}  {bad('reject')} {t['reject']}")
        if res["skipped"]:
            print(dim(f"  {res['skipped']} items had no verdict and were skipped"))
        for p in res["problems"][:12]:
            print(warn(f"  ignored  {p}"))
        if len(res["problems"]) > 12:
            print(warn(f"  ... and {len(res['problems']) - 12} more"))
        _cross_audit_summary(conn)
        return

    if args.apply_ids is not None:
        _cross_audit_apply(conn, args)
        return

    if args.export_path is not None:
        path = Path(args.export_path or "cross-audit.json").expanduser()
        res = crossaudit.export_batch(conn, path, target=args.target, limit=args.limit)
        if not res["count"]:
            print(ok("nothing left to cross-audit for that target"))
            return
        print(ok(f"wrote {res['count']} questions to {res['path']}"))
        print(dim(f"  target: {res['target']}"))
        print()
        print("  Next: have Claude Code review it, then file the verdicts:")
        print(f"    {head('read ' + res['path'] + ' and fill in every your_verdict')}")
        print(f"    {head('superday cross-audit --import ' + res['path'])}")
        return

    if args.api:
        _cross_audit_api(conn, args)
        return

    _cross_audit_summary(conn)


# This pass reads a batch of answers and has to actually disagree with them,
# which is the one job in the tool that is not filling in a fixed schema. It is
# the only caller that asks for more than THINKING_BULK.
CROSS_AUDIT_EFFORT = "medium"


def _cross_audit_api(conn: sqlite3.Connection, args) -> None:
    """Run the second opinion unattended, on somebody other than the first.

    Who that is is the one decision this function makes, and it cannot be a
    constant: pinned to Claude it was a genuine second opinion for as long as
    `llm_provider` was not claude, and the same model checking its own work
    the moment it was. `crossaudit.second_provider` states the rule instead --
    somebody else, Claude for preference, from the keys you actually hold.
    """
    first = llm.provider()
    name = crossaudit.second_provider(first, forced=args.using or "")
    if not name:
        # Nobody left to ask. Say which two facts made that true, because the
        # fix is different for each: a second key, or a different primary.
        print(warn(f"no second opinion available: {llm.provider_label(first)} "
                   "gave the first one, and you hold no other provider's key"))
        print(dim("  a key for either of the others:  llm"))
        print(dim("  or the path that needs none:  cross-audit --export"))
        return
    if not llm.available(name):
        print(warn(llm.setup_help(name)))
        print(dim("  the default cross-audit needs no key: it hands the batch "
                  "to Claude Code instead"))
        return
    if name == first:
        # Only reachable through --using, so it proceeds -- but it is the
        # failure this whole command exists to prevent, and it must not happen
        # silently.
        print(warn(f"--using {name} is the provider that gave the first opinion"))
        print(dim("  a model grading its own output agrees with itself, which "
                  "is what `audit` already does"))
    rows = crossaudit.pending(conn, target=args.target, limit=args.limit)
    if not rows:
        print(ok("nothing left to cross-audit for that target"))
        return
    model = llm.model_audit(name)
    filed_as = crossaudit.api_provider(name)
    print(dim(f"cross-auditing {len(rows)} questions with {model}"))
    print(dim(f"  {llm.provider_label(first)} gave the first opinion; this is "
              f"{llm.provider_label(name)}'s, filed as {filed_as}"))
    stored, fails = 0, 0
    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        items = [crossaudit._item(r) for r in chunk]
        prompt = (crossaudit.INSTRUCTIONS + "\n\nITEMS:\n"
                  + json.dumps(items, indent=1, ensure_ascii=False))
        try:
            # `using` pins the provider against the setting: this pass is only
            # worth its cost as somebody else's opinion.
            out = llm.generate(prompt, schema=crossaudit.SCHEMA, model=model,
                               using=name, caller="cross_audit",
                               thinking=CROSS_AUDIT_EFFORT, timeout=300)
        except llm.LLMError as e:
            fails += 1
            print(bad(f"  batch failed ({fails}/3): {e}"))
            if not e.retryable or fails >= 3:
                _llm_problem(e.message, e.hint or "re-run to resume where it stopped")
                break
            continue
        fails = 0
        good, problems = crossaudit.validate(conn, out.get("items", []))
        for it in good:
            crossaudit.record(conn, it["question_id"], it,
                              provider=filed_as, model=model)
            stored += 1
        conn.commit()
        for p in problems[:3]:
            print(warn(f"  ignored  {p}"))
        print(dim(f"  {stored}/{len(rows)} recorded"))
    _cross_audit_summary(conn)


def _cross_audit_apply(conn: sqlite3.Connection, args) -> None:
    """Write corrections that are on file into the bank.

    Deliberately a second step. `cross-audit --import` files an opinion; this
    is where it becomes the answer you will be drilled on, so it shows the
    whole list and asks first, the way every other bulk mutation does.

    Until it runs, a `fix` above the floor is holding its question out of every
    drill. That is the right default -- better to ask nothing than to ask
    something a second reader called wrong -- but it means an unapplied
    correction silently shrinks the pool, which is why `--apply` names the
    count rather than reporting success and moving on.
    """
    ids = _id_list(args.apply_ids)
    rows = crossaudit.pending_corrections(conn, ids=ids or None)
    if not rows:
        print(ok("no corrections waiting to be applied"))
        if ids:
            print(dim("  (those ids carry no correction above the "
                      f"{crossaudit.AUTO_APPLY_AT:.2f} floor, or already have it)"))
        return

    print(f"about to replace {len(rows)} answers:")
    for r in rows:
        topic = ui.pad(r["topic"] or "-", 14)
        print(f"  {head('#' + str(r['id']))}  {dim(topic)}  "
              f"{ui.truncate(r['canonical_text'], 44)}  {r['confidence']:.2f}")
        if r["reason"]:
            print(ui.body(r["reason"], "      "))
    if not args.yes:
        try:
            choice = input(warn("proceed? [y/N] > ")).strip().lower()
        except EOFError:
            choice = ""
        if choice != "y":
            print(dim("cancelled"))
            return

    batch = history.new_batch()
    n = sum(history.set_answer(conn, r["id"], r["corrected_answer"],
                               action="cross-audit", batch_id=batch)
            for r in rows)
    conn.commit()
    print(ok(f"replaced {n} answer{'' if n == 1 else 's'}"))
    print(dim("  take it back with: superday undo"))


def _provider_phrase(names: list[str]) -> str:
    """`['claude-code']` as "Claude", `['claude-code','openai-api']` as both.

    `llm.label_for` does one name; this is the list, for the summary lines that
    have to cope with a bank audited partly under one provider and partly
    under another.
    """
    labels = []
    for raw in names:
        label = llm.label_for(raw)
        if label and label not in labels:
            labels.append(label)
    if len(labels) > 1:
        return ", ".join(labels[:-1]) + " and " + labels[-1]
    return labels[0] if labels else ""


def _cross_audit_summary(conn: sqlite3.Connection) -> None:
    s = crossaudit.summary(conn)
    print("\n" + section("CROSS-AUDIT"))
    if not s["checked"]:
        print(warn("  nothing cross-audited yet."))
        print(dim("  Start with:  superday cross-audit --export"))
        return
    # Who actually gave each side, read off the rows rather than assumed. A
    # bank audited on one provider and cross-audited on another says so, and a
    # bank that switched mid-way says both -- which is worth seeing, because
    # "the second opinion" is only one thing while one model gave all of it.
    print(f"  {s['checked']} checked by "
          f"{_provider_phrase(s['second_by']) or 'a second pass'}, "
          f"{s['unchecked']} not yet")
    print(f"  {ok(str(s['agree']) + ' agree')}   "
          f"{warn(str(s['disagree']) + ' disagree')}   "
          f"{bad(str(s['both_reject']) + ' both call wrong')}")
    if s["second_only_reject"]:
        print(bad(f"  {s['second_only_reject']} kept by the first pass, rejected by "
                  f"the second - these are the ones that teach you a wrong answer"))
    if s["first_only_reject"]:
        print(dim(f"  {s['first_only_reject']} rejected by the first pass, "
                  "the second would keep"))
    if s["held"]:
        print(warn(f"  {s['held']} below the confidence floor, held for you"))
    if s["disagree"]:
        print(dim("  See them:  superday disagreements"))


def cmd_disagreements(conn: sqlite3.Connection, args) -> None:
    rows = crossaudit.disagreements(conn)
    if not rows:
        _cross_audit_summary(conn)
        return

    if not args.review:
        checked = crossaudit.summary(conn)["checked"]
        _hand_off(views_mod.DisagreementsView(conn, rows[: args.limit], checked))
        return

    batch = history.new_batch()
    acted = 0
    for i, r in enumerate(rows[: args.limit], 1):
        _print_disagreement(conn, r, i, min(len(rows), args.limit))
        print()
        try:
            # The prompt names whoever actually gave this verdict, because the
            # row above it does: hardcoded, it offered to apply "claude's
            # answer" over a correction OpenAI wrote.
            whose = llm.label_for(r.get("c_provider")) or "the second pass"
            choice = input(f"[k]eep active  [r]eject  [a]pply {whose}'s answer  "
                           "[s]kip  [q]uit > ").strip().lower()
        except EOFError:
            break
        if choice == "q":
            break
        if choice == "k":
            acted += history.set_status(conn, r["id"], "active",
                                        action="cross-audit", batch_id=batch)
        elif choice == "r":
            acted += history.set_status(conn, r["id"], "rejected",
                                        action="cross-audit", batch_id=batch)
        elif choice == "a":
            if not r["corrected_answer"]:
                print(warn("  no corrected answer on that verdict"))
            else:
                history.set_answer(conn, r["id"], r["corrected_answer"],
                                   action="cross-audit", batch_id=batch)
                history.set_status(conn, r["id"], "active",
                                   action="cross-audit", batch_id=batch)
                acted += 1
                print(ok("  answer replaced"))
        conn.commit()
    print("\n" + rule())
    if acted:
        print(f"  {acted} changed   {dim('take it back with: superday undo')}")
    else:
        print(dim("  nothing changed"))


# ---------------------------------------------------------------- check

def cmd_check(conn: sqlite3.Connection, args) -> None:
    """Sweep the bank for answers that are provably wrong.

    Free and local: this is arithmetic and direction, not judgement. It finds a
    different class of error from `audit` and `cross-audit`, and it finds them
    with certainty rather than with a confidence score.
    """
    status = None if args.all else "active"
    rows = checks.scan(conn, status=status, limit=args.limit)

    if getattr(args, "json", False):
        print(json.dumps([
            {"id": r["id"], "topic": r["topic"], "question": r["question"],
             "findings": [{"kind": f.kind, "message": f.message,
                           "excerpt": f.excerpt, "severity": f.severity}
                          for f in r["findings"]]}
            for r in rows], indent=2))
        return

    scanned = conn.execute(
        "SELECT COUNT(*) c FROM questions" + ("" if args.all else " WHERE status='active'")
    ).fetchone()["c"]

    if not rows:
        print(ok(f"  nothing provably wrong in {scanned} answers"))
        print(dim("  this checks arithmetic, the EV bridge, statement links and "
                  "formula direction"))
        print(dim("  it is not a claim they are right: "
                  "superday audit  ·  superday cross-audit"))
        return

    print(section(f"PROVABLY WRONG   {len(rows)} of {scanned}"))
    for r in rows:
        print(f"\n  {head('#' + str(r['id']))}  [{r['topic'] or '-'}]  {dim(r['status'])}")
        print(wrap(r["question"][:130], "     "))
        for f in r["findings"]:
            colour = bad if f.severity >= 3 else warn
            print(f"     {colour(f.kind.upper())}  {f.message}")
            print(dim(f"        \u201c{f.excerpt[:100]}\u201d"))
    print("\n" + rule())
    print(dim("  fix one: ") + head("superday edit <id>")
          + dim("   ·   see it all: ") + head("superday show <id>"))


# ---------------------------------------------------------------- show

def _question_record(conn: sqlite3.Connection, qid: int) -> dict | None:
    """Everything known about one question, as data. Rendering is separate so
    `--json` and the terminal view cannot drift apart."""
    q = conn.execute(
        "SELECT q.*, a.answer_key, a.rubric_points, a.common_mistakes, a.answer_status "
        "FROM questions q LEFT JOIN answers a ON a.question_id = q.id WHERE q.id = ?",
        (qid,)).fetchone()
    if q is None:
        return None
    rec = dict(q)
    rec["rubric_points"] = json.loads(q["rubric_points"] or "[]")
    rec["common_mistakes"] = json.loads(q["common_mistakes"] or "[]")
    rec["tags"] = tagging.tags_for(conn, qid)
    rec["audits"] = [dict(r) for r in conn.execute(
        "SELECT provider, model, verdict, reason, confidence, ran_at FROM audits "
        "WHERE question_id = ? ORDER BY id", (qid,))]
    rec["sources"] = [dict(r) for r in conn.execute(
        "SELECT s.title, s.kind, qs.locator, qs.verbatim_text FROM question_sources qs "
        "JOIN sources s ON s.id = qs.source_id WHERE qs.question_id = ?", (qid,))]
    rec["phrasings"] = [r["text"] for r in conn.execute(
        "SELECT text FROM phrasings WHERE question_id = ?", (qid,))]
    rec["reviews"] = [dict(r) for r in conn.execute(
        "SELECT asked_at, rating, score, grader FROM reviews "
        "WHERE question_id = ? ORDER BY asked_at", (qid,))]
    rec["notes"] = [dict(r) for r in conn.execute(
        "SELECT body, created_at FROM notes WHERE question_id = ? ORDER BY id", (qid,))]
    sched = conn.execute(
        "SELECT due_at, reps, lapses FROM schedule WHERE question_id = ?", (qid,)).fetchone()
    rec["schedule"] = dict(sched) if sched else None
    rec["lead_in"] = [dict(r) for r in chains.lead_in(conn, qid)]
    rec["follow_ups"] = [dict(r) for r in chains.children(conn, qid)]
    return rec


def _question_panes(conn: sqlite3.Connection, rec: dict) -> list[tuple]:
    """One question, split into the five things you can want to know about it.

    Every pane is a closure so an unopened one costs nothing, and each of them
    returns lines rather than printing, so the same five build the tabbed view
    in the shell and the flat printout everywhere else.
    """
    panes = [("Answer", lambda w: _q_answer(rec, w))]
    # No `Full` when the rubric is still the placeholder: the card above *is*
    # the written answer, cut into its own sentences, so the tab would hold
    # the same words a second time. Piped, where every pane prints, that is
    # the answer twice on one screen.
    if not provisional_rubric(rec["rubric_points"], rec["answer_key"]):
        panes.append(("Full", lambda w: _q_full(rec, w)))
    return panes + [
        ("Card", lambda w: _q_card(rec, w)),
        ("Sources", lambda w: _q_sources(rec, w)),
        ("History", lambda w: _q_history(rec)),
        ("Verdicts", lambda w: _q_verdicts(rec, w)),
    ]


def _q_answer(rec: dict, w: int) -> list[str]:
    """The rubric, as the answer. The prose lives in `Full` next door.

    This pane used to open on the paragraph and put the rubric underneath it,
    which is the wrong way round for the same reason it was wrong in `drill`:
    the paragraph is the longest and least navigable form of the same content,
    so leading with it means scrolling past the answer to reach the answer.
    """
    if not rec["rubric_points"]:
        return _q_full(rec, w)
    out = ui.answer_card(rec["rubric_points"], traps=rec["common_mistakes"],
                         w=w - 2).split("\n")
    if not (rec["answer_key"] or "").strip():
        return out
    if provisional_rubric(rec["rubric_points"], rec["answer_key"]):
        # There is no `Full` tab to point at in this case: this rubric *is*
        # the answer, sliced into sentences.
        return out + ["", "  " + dim("this rubric is the answer's own "
                                     "sentences, not marking criteria yet"),
                      "  " + dim("run ") + head("enrich") + dim(" to write the real one")]
    return out + ["", "  " + dim("the written answer in full is in ") + head("Full")]


def _q_full(rec: dict, w: int) -> list[str]:
    """The written answer as stored, and nothing else."""
    if not (rec["answer_key"] or "").strip():
        return ["  " + dim("no written answer on file, only the rubric")]
    return ui.body(rec["answer_key"], "  ", w - 4).split("\n")


def _q_card(rec: dict, w: int) -> list[str]:
    out = [
        ui.kv("topic", f"{rec['topic'] or '-'}"
                       f"{dim(' / ' + rec['subtopic']) if rec['subtopic'] else ''}"),
        ui.kv("kind", rec["kind"]),
        ui.kv("difficulty", f"{rec['difficulty'] or '-'}/5"),
        ui.kv("status", _STATUS_COLOR.get(rec["status"], dim)(rec["status"])),
        ui.kv("origin", rec["origin"]),
        ui.kv("added", (rec["created_at"] or "")[:10]),
    ]
    if rec["tags"]:
        out.append(ui.kv("tags", " ".join(ui.paint("#" + t, "mauve")
                                          for t in rec["tags"])))
    if rec["schedule"]:
        out += ["",
                ui.kv("next due", scheduler.due_phrase(rec["schedule"]["due_at"])),
                ui.kv("reps", f"{rec['schedule']['reps']}   "
                              f"{dim(str(rec['schedule']['lapses']) + ' lapses')}")]
    if rec["lead_in"] or rec["follow_ups"]:
        out += ["", "  " + head("the line it sits in")]
        for p in rec["lead_in"]:
            out.append(f"    {dim('after  #' + str(p['id']))} "
                       + ui.truncate(" ".join(p["canonical_text"].split()), w - 22))
        out.append(f"    {ui.paint('this   #' + str(rec['id']), 'accent')}")
        for c in rec["follow_ups"]:
            out.append(f"    {dim('then   #' + str(c['id']))} "
                       + ui.truncate(" ".join(c["canonical_text"].split()), w - 22))
    if rec["phrasings"]:
        out += ["", "  " + head("also asked as")]
        out += [f"    {dim('-')} {t}" for t in rec["phrasings"]]
    if rec["notes"]:
        out += ["", "  " + head("your notes")]
        for n in rec["notes"]:
            # A note is prose you wrote, not a field: it wraps like prose. This
            # ran off the edge as one line until the first long one was added.
            out.append(f"    {dim(n['created_at'][:10])}")
            out += ui.body(n["body"], "      ", w - 8).split("\n")
    return out


def _q_sources(rec: dict, w: int) -> list[str]:
    if not rec["sources"]:
        return ["  " + dim("no source links")]
    out = []
    for src in rec["sources"]:
        # The title was clamped and the locator was not, so the composed line
        # ran as much as 62 cells past the pane on the widest source in the
        # bank -- and the locator, being last, was the half that got chopped
        # mid-word with no ellipsis to say so. Both halves get a budget now,
        # and the whole line is clamped after them.
        #
        # The locator also earns its column only when it says something the
        # title did not: `ingest` sets both to the same file stem, so most of
        # these were printing one long heading twice.
        title = (src["title"] or "").strip()
        locator = (src["locator"] or "").strip()
        if locator.lower() == title.lower():
            locator = ""
        lead = f"  {dim('[' + src['kind'] + ']')} "
        room = max(20, w - ui.vlen(lead))
        loc = ui.truncate(locator, room // 3) if locator else ""
        line = lead + ui.truncate(title, room - (ui.vlen(loc) + 1 if loc else 0))
        out.append(ui.truncate(line + (" " + dim(loc) if loc else ""), w))
        if src["verbatim_text"]:
            quoted = '"' + src["verbatim_text"][:400] + '"'
            out += [dim(x) for x in ui.body(quoted, "      ", w - 8).split("\n")]
        out.append("")
    return out


def _q_history(rec: dict) -> list[str]:
    if not rec["reviews"]:
        return ["  " + dim("never drilled")]
    out = []
    for r in rec["reviews"]:
        score = f"{r['score']:.0%}" if r["score"] is not None else "  - "
        label = {1: "again", 2: "hard", 3: "good", 4: "easy"}.get(r["rating"], "?")
        out.append(f"  {dim(r['asked_at'][:10])}  {ui.pad(verdict(label), 10)}"
                   f"{score:>5}  {dim(r['grader'])}")
    return out


def _q_verdicts(rec: dict, w: int) -> list[str]:
    if not rec["audits"]:
        return ["  " + dim("never audited")]
    out = []
    for r in rec["audits"]:
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else " -  "
        out.append(f"  {dim(r['ran_at'][:10])}  {ui.pad(r['provider'], 12)}"
                   f"{ui.pad(verdict(r['verdict']), 10)} {dim(conf)}")
        if r["reason"]:
            out += ui.body(r["reason"], "      ", w - 8).split("\n")
    return out


def _render_question(conn: sqlite3.Connection, rec: dict, position: str = "") -> None:
    """The flat printout: every pane, one after the other."""
    print(section(f"#{rec['id']}" + (dim("   " + position) if position else "")))
    print(ui.question(rec["canonical_text"], "  "))
    for name, build in _question_panes(conn, rec):
        print(section(name.upper()))
        for line in build(ui.width()):
            print(line)


def _browse_neighbours(conn: sqlite3.Connection, rec: dict) -> list[int]:
    """The ids you can page through with n/p: the rest of this topic, in order.

    Browsing a topic is how you actually revise a subject -- reading one card
    and then having to remember the next id is not browsing.
    """
    return [r["id"] for r in conn.execute(
        "SELECT id FROM questions WHERE status = 'active' AND "
        "COALESCE(topic,'general') = ? ORDER BY difficulty IS NULL, difficulty, id",
        (rec["topic"] or "general",))]


def cmd_show(conn: sqlite3.Connection, args) -> None:
    """Everything known about one question, with n/p to walk the topic."""
    qid = args.id
    rec = _question_record(conn, qid)
    if rec is None:
        print(bad(f"no question #{qid}"))
        return

    if getattr(args, "json", False):
        print(json.dumps(rec, indent=2, default=str))
        return

    # Where this whole visit started, and where the transcript stood right
    # after that echo landed. `d`/`e`/`t` write real, kept output -- a
    # drill's saved-progress line, an edit's confirmation -- and the mark
    # comparison at exit is what tells "you just looked" apart from "you did
    # something": only when nothing has moved the transcript past
    # `entry_mark` does the whole session, echo included, fold away.
    echo_mark = tui.echo_mark()
    entry_mark = tui.mark()

    def _exit() -> None:
        if echo_mark is not None and tui.mark() == entry_mark:
            tui.collapse(echo_mark, [])

    tab = 0
    while True:
        neighbours = _browse_neighbours(conn, rec)
        idx = neighbours.index(rec["id"]) if rec["id"] in neighbours else -1
        position = (f"{idx + 1}/{len(neighbours)} in {rec['topic'] or 'general'}"
                    if idx >= 0 else "")
        # In the shell this becomes a tabbed card you can page with ◂ ▸ while
        # the prompt below stays live; with no shell it prints flat. The
        # card's own footer is off (`footer=False`) -- this prompt is the
        # one hint line for the whole screen, `◂▸ tab` folded into it, rather
        # than a second, differently-styled row underneath saying an
        # overlapping thing ("esc done" next to "[Enter] done").
        view = views_mod.TabsView(
            "#" + str(rec["id"]), _question_panes(conn, rec),
            subject=" ".join(rec["canonical_text"].split()), start=tab,
            footer=False)
        if not tui.attach(view):
            _render_question(conn, rec, position)
            print("\n" + rule())

        bits = [ui.style("◂▸ tab", BOLD)] if len(view.tabs) > 1 else []
        if idx > 0:
            bits.append(ui.style("[p] prev", BOLD))
        if 0 <= idx < len(neighbours) - 1:
            bits.append(ui.style("[n] next", BOLD))
        # Offered only when there is a line to see. A question that follows
        # nothing and is followed by nothing would open a tree of one row,
        # which is the card you are already looking at with a gutter.
        in_a_line = bool(rec["lead_in"] or rec["follow_ups"])
        if in_a_line:
            bits.append(ui.style("[c] the line", BOLD))
        bits += [ui.style("[d] drill it", BOLD), ui.style("[e] edit", BOLD),
                 ui.style("[t] tag", BOLD), ui.style("[Enter] done", BOLD)]
        # The card itself never touches the transcript -- it's a view,
        # drawn live and gone on `dismiss`. The prompt line and whatever you
        # typed do, though, once per page you turn, and nothing was ever
        # folding those back up: paging through five questions with n left
        # five dead lines behind for a card that left none. `spot` is where
        # this turn of the loop started, so the whole turn - prompt and
        # echoed keystroke alike - collapses to nothing once it is answered.
        spot = tui.mark()
        try:
            act = input("  " + dim(" · ").join(bits)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            tui.dismiss()
            tui.collapse(spot, [])
            _exit()
            return
        tab = view.idx
        # Whatever happens next gets the screen to itself. The card is a
        # frame drawn over the bottom of the terminal, so a drill started
        # from it asked its question into the transcript *behind* the answer
        # this card is already showing -- the answer visible, the question
        # not. The loop redraws the card at the top of the next pass.
        tui.dismiss()
        tui.collapse(spot, [])

        if act == "n" and 0 <= idx < len(neighbours) - 1:
            rec = _question_record(conn, neighbours[idx + 1])
        elif act == "p" and idx > 0:
            rec = _question_record(conn, neighbours[idx - 1])
        elif act == "c" and in_a_line:
            # The graph attaches a view of its own, so this visit to `show` is
            # over: leaving the card up behind it is the same "two frames, one
            # screen" failure `run_now` exists to prevent.
            cmd_chains(conn, argparse.Namespace(
                graph=rec["id"], scan=False, apply=False, link=None,
                unlink=None, standalone=None, tier="all", json=False))
            _exit()
            return
        elif act == "d":
            cmd_drill(conn, _drill_args(count=1, topic=rec["topic"]))
            rec = _question_record(conn, rec["id"])
        elif act == "e":
            cmd_edit(conn, argparse.Namespace(id=rec["id"]))
            rec = _question_record(conn, rec["id"])
        elif act.startswith("t"):
            names = [t for t in re.split(r"[,\s]+", act[1:]) if t]
            if not names:
                print(dim("  usage: t ev-bridge wacc"))
                continue
            added = tagging.attach(conn, rec["id"], names)
            print(ok("  tagged " + ", ".join("#" + a for a in added)) if added
                  else dim("  already tagged"))
            rec = _question_record(conn, rec["id"])
        else:
            _exit()
            return


# ---------------------------------------------------------------- edit

def _edit_multiline(prompt: str) -> str | None:
    """Blank first line means 'leave unchanged'. A line reading only END closes it."""
    print(dim(f"  {prompt} - blank line to keep as-is, or type it and finish with END"))
    try:
        first = input("  > ")
    except EOFError:
        return None
    if first == "":
        return None
    lines = [first]
    while True:
        try:
            line = input("  > ")
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def _edit_list(prompt: str) -> list[str] | None:
    """Blank first line means 'leave unchanged'. Any later blank line ends the list."""
    print(dim(f"  {prompt} - one per line, blank first line to keep as-is, "
              "blank line to finish"))
    try:
        first = input("  - ")
    except EOFError:
        return None
    if first.strip() == "":
        return None
    items = [first.strip()]
    while True:
        try:
            line = input("  - ")
        except EOFError:
            break
        if line.strip() == "":
            break
        items.append(line.strip())
    return items


def cmd_edit(conn: sqlite3.Connection, args) -> None:
    q = conn.execute(
        "SELECT q.*, a.answer_key, a.rubric_points, a.common_mistakes FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id WHERE q.id = ?",
        (args.id,)).fetchone()
    if q is None:
        print(bad(f"no question #{args.id}"))
        return

    print(section(f"EDIT #{q['id']}"))
    print(ui.question(q["canonical_text"], "  "))
    print()
    print(ui.kv("topic", str(q["topic"] or "-")))
    print(ui.kv("difficulty", f"{q['difficulty'] or '-'}/5"))
    print(f"\n  {head('answer')}")
    print(ui.body(q["answer_key"] or "", "  "))
    print(dim("\n  changes save immediately; reversible with `superday undo`"))

    audited = conn.execute(
        "SELECT COUNT(*) c FROM audits WHERE question_id = ?", (args.id,)).fetchone()["c"]
    if audited:
        print(warn(f"  {audited} audit verdict(s) on file were judged against the wording "
                    "above; they will not update to match an edit."))

    batch_id = history.new_batch()
    changed: list[str] = []
    while True:
        print()
        try:
            choice = input("[q]uestion  [a]nswer  [t]opic  [d]ifficulty  "
                           "[r]ubric points  [m]istakes  [done] > ").strip().lower()
        except EOFError:
            break
        if choice in ("", "done"):
            break

        if choice == "q":
            new = _edit_multiline("new question text")
            if new:
                try:
                    if history.set_question(conn, q["id"], new,
                                            action="edit", batch_id=batch_id):
                        changed.append("question")
                except history.Collision as e:
                    print(bad(f"  {e}"))
                    print(dim("  the gate dedupes on the wording, so two "
                              "questions cannot share one"))
        elif choice == "a":
            new = _edit_multiline("new answer")
            if new:
                history.set_answer(conn, q["id"], new, action="edit", batch_id=batch_id)
                changed.append("answer")
        elif choice == "t":
            try:
                new = input("  new topic > ").strip()
            except EOFError:
                new = ""
            if new and new not in TOPICS:
                print(bad(f"  '{new}' is not a topic"))
                print(dim("  " + "  ".join(TOPICS)))
            elif new:
                # `kind` is derived from `topic` everywhere it is *written*
                # (`admit`, the pipeline, `enrich`, `ingest-pack`) and was the
                # one place it was not, so retopicing a question to
                # `behavioural` by hand left it filed as a technical: drilled
                # by the wrong rounds, missing from `drill -k behavioural`, and
                # judged by an audit prompt told to reject career narrative.
                # A market-awareness question keeps its kind regardless -- that
                # one is about how the answer is graded, not about the topic.
                sets, params = "topic = ?", [new]
                if q["kind"] != "market_awareness":
                    sets, params = sets + ", kind = ?", params + [kind_for_topic(new)]
                conn.execute(f"UPDATE questions SET {sets} WHERE id = ?",
                             params + [q["id"]])
                changed.append("topic")
        elif choice == "d":
            try:
                raw = input("  new difficulty (1-5) > ").strip()
            except EOFError:
                raw = ""
            if raw and raw in {"1", "2", "3", "4", "5"}:
                conn.execute("UPDATE questions SET difficulty = ? WHERE id = ?",
                             (int(raw), q["id"]))
                changed.append("difficulty")
            elif raw:
                print(bad("  difficulty must be 1-5"))
        elif choice == "r":
            items = _edit_list("rubric points")
            if items is not None:
                curr = conn.execute(
                    "SELECT answer_key FROM answers WHERE question_id = ?", (q["id"],)
                ).fetchone()
                curr_ans = curr["answer_key"] if curr else q["answer_key"]
                history.set_answer(conn, q["id"], curr_ans, json.dumps(items),
                                   action="edit", batch_id=batch_id)
                changed.append("rubric")
        elif choice == "m":
            items = _edit_list("common mistakes")
            if items is not None:
                # Through history like the answer and the rubric next to it.
                # A raw UPDATE here was invisible to `undo`, which is worse
                # than no undo: the command reports success, offers to take it
                # back, and then takes back everything except this.
                curr = conn.execute(
                    "SELECT answer_key FROM answers WHERE question_id = ?", (q["id"],)
                ).fetchone()
                history.set_answer(
                    conn, q["id"], curr["answer_key"] if curr else q["answer_key"],
                    new_common_mistakes=json.dumps(items),
                    action="edit", batch_id=batch_id)
                changed.append("mistakes")
        else:
            print(bad(f"  unknown option '{choice}'"))
            continue
        conn.commit()

    if changed:
        print(ok(f"\n  saved: {', '.join(changed)}"))
        print(dim(f"  superday show {q['id']}  to review it"))
    else:
        print(dim("\n  nothing changed"))


# ---------------------------------------------------------------- tags

def cmd_tag(conn: sqlite3.Connection, args) -> None:
    """Add one or more tags to a question."""
    if not conn.execute("SELECT 1 FROM questions WHERE id = ?", (args.id,)).fetchone():
        print(bad(f"no question #{args.id}"))
        return
    names = [t for t in args.tags if t.strip()]
    if not names:
        print(warn("no tags specified"))
        return
    added = tagging.attach(conn, args.id, names,
                           kind=getattr(args, "kind", "concept") or "concept")
    if added:
        print(ok(f"tagged #{args.id}: " + ", ".join("#" + a for a in added)))
    else:
        print(dim(f"#{args.id} already carried all of those"))
    print(dim("  now: " + " ".join("#" + t for t in tagging.tags_for(conn, args.id))))


def cmd_untag(conn: sqlite3.Connection, args) -> None:
    """Remove one or more tags from a question."""
    names = [t for t in args.tags if t.strip()]
    if not names:
        print(warn("no tags specified"))
        return
    removed = tagging.detach(conn, args.id, names)
    print(ok(f"removed from #{args.id}: " + ", ".join("#" + r for r in removed)) if removed
          else dim("nothing to remove"))


def cmd_autotag(conn: sqlite3.Connection, args) -> None:
    """Sweep the taxonomy across the bank. Lexical, local, free, idempotent."""
    scope = "every question" if args.all else "untagged questions"
    print(dim(f"  scanning {scope} against {len(tagging.CONCEPTS)} concepts, "
              f"{len(tagging.INDUSTRIES)} sectors and {len(tagging.FIRMS)} firms..."))
    res = tagging.autotag(conn, limit=args.limit, only_untagged=not args.all)
    if not res["scanned"]:
        print(ok("  everything already tagged"))
        return
    print(ok(f"  {res['tagged']}/{res['scanned']} questions tagged, "
             f"{res['links']} links added"))
    top = sorted(res["per_tag"].items(), key=lambda kv: -kv[1])[:12]
    for name, n in top:
        print(f"    {n:>4}  " + head("#" + name))
    print(rule())
    print(dim("  drill one: ") + head("superday drill --tag ev-bridge"))


def cmd_tags(conn: sqlite3.Connection, args) -> None:
    """The tag map: what exists, how big it is, and where you are weak."""
    if getattr(args, "prune", False):
        gone = tagging.orphans(conn)
        if not gone:
            print(ok("  every tag is carried by something"))
            return
        print(dim(f"  {len(gone)} tag(s) no question carries:"))
        for t in gone:
            print("    " + ui.paint("#" + t["name"], "mauve") + dim(f"   {t['kind']}"))
        print(dim("  a family with nothing under it is not offered here - "
                  "`firms` and `sectors` are seeded empty and fill as you ingest"))
        if not args.yes:
            try:
                if input(warn("  remove them? [y/N] > ")).strip().lower() != "y":
                    print(dim("  cancelled"))
                    return
            except EOFError:
                print(dim("  cancelled"))
                return
        removed = tagging.prune(conn)
        print(ok(f"  removed {len(removed)}"))
        return
    rows = tagging.all_tags(conn, min_count=args.min_count)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return
    _hand_off(views_mod.TagsView(conn, rows))


def _hand_off(view) -> None:
    """Give the view to the shell, or print it flat when there is no shell."""
    if tui.attach(view):
        return
    for line in view.flatten(ui.width()):
        print(line)


# ---------------------------------------------------------------- dupes

def _pair_ids(raw: str) -> tuple[int, int] | None:
    """`124,877` as a pair of ids. Anything else is a complaint, not a guess."""
    parts = [p.strip() for p in (raw or "").replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 2 or not all(p.lstrip("#").isdigit() for p in parts):
        return None
    a, b = (int(p.lstrip("#")) for p in parts)
    return (a, b) if a != b else None


def _phrasing_row(conn: sqlite3.Connection, pid: int):
    return conn.execute(
        "SELECT p.id, p.question_id, p.text, p.norm_key, q.canonical_text "
        "  FROM phrasings p JOIN questions q ON q.id = p.question_id "
        " WHERE p.id = ?", (pid,)).fetchone()


def _detach_phrasing(conn: sqlite3.Connection, pid: int) -> None:
    row = _phrasing_row(conn, pid)
    if row is None:
        print(bad(f"  no phrasing #{pid}"))
        print(dim("  " + "dupes --phrasings" + " lists them with their ids"))
        return
    dupes.detach(conn, pid)
    print(ok(f"  took that wording off #{row['question_id']}"))
    print(dim("  ") + ui.question(row["text"]))
    print(dim("  drill will not ask it again, and the gate will stop treating "
              "it as this question"))


def _keep_phrasing(conn: sqlite3.Connection, pid: int) -> None:
    row = _phrasing_row(conn, pid)
    if row is None:
        print(bad(f"  no phrasing #{pid}"))
        return
    dupes.keep_phrasing(conn, row["question_id"], row["norm_key"] or "")
    print(ok(f"  noted: that wording belongs on #{row['question_id']}"))
    print(dim("  the scan will stop proposing it   ·   ")
          + head("dupes --phrasings --all") + dim(" shows the settled ones again"))


def _show_drifted_phrasings(conn: sqlite3.Connection, *, include_settled: bool) -> None:
    """Wordings that have drifted away from the question they are attached to.

    `drill` serves a random phrasing for realism, so one of these is a
    question you can actually be asked and then marked against a rubric that
    answers something else.
    """
    rows = dupes.drifted(conn, include_settled=include_settled)
    if not rows:
        print(ok("  every wording still asks the question it hangs off"))
        return
    print()
    print(head(f"  PHRASINGS THAT DRIFTED") + dim(f"   {len(rows)} to settle"))
    print(dim("  " + ui.rule().strip()))
    print(dim("  drill serves one of these at random, then marks you against "
              "the card's rubric"))
    for r in rows:
        print()
        sim = warn(f"{r['similarity']:.2f}")
        print(f"  {sim}  {head('#' + str(r['question_id']))}"
              f"   {dim('; '.join(r['why']))}")
        w = max(30, ui.W - 12)
        print(dim("     asks ") + ui.truncate(" ".join(r["text"].split()), w))
        print(dim("     card ") + dim(ui.truncate(" ".join(r["canonical_text"].split()), w)))
        print(dim(f"     dupes --detach {r['id']}")
              + dim("   ·   ")
              + dim(f"dupes --keep-phrasing {r['id']}"))
    print()


def cmd_dupes(conn: sqlite3.Connection, args) -> None:
    """Near-duplicate pairs, and what to do about them.

    This used to walk the pairs in a print-and-input loop that could only go
    forwards and left every pair it had shown behind it in scrollback. It
    hands back a view now, like every other list in the tool, and the three
    decisions are `←` on a row rather than a single-letter prompt.
    """
    threshold = getattr(args, "threshold", None) or dupes.DEFAULT_THRESHOLD

    if getattr(args, "detach", None) is not None:
        _detach_phrasing(conn, args.detach)
        return

    if getattr(args, "keep_phrasing", None) is not None:
        _keep_phrasing(conn, args.keep_phrasing)
        return

    if getattr(args, "phrasings", False):
        _show_drifted_phrasings(conn, include_settled=bool(getattr(args, "all", False)))
        return

    if getattr(args, "merge", None):
        pair = _pair_ids(args.merge)
        if pair is None:
            print(bad("  --merge takes two different ids: --merge 124,877"))
            return
        keeper, dupe = pair
        rows = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM questions WHERE id IN (?, ?)", pair)}
        missing = [q for q in pair if q not in rows]
        if missing:
            print(bad(f"  no question #{missing[0]}"))
            return
        if rows[dupe] == "rejected":
            print(dim(f"  #{dupe} is already rejected - nothing to fold"))
            return
        dupes.merge(conn, keeper, dupe, history.new_batch())
        print(ok(f"  folded #{dupe} into #{keeper}"))
        print(dim(f"  its wording, reviews, notes and tags moved across"
                  f"   ·   take it back with ") + head("undo"))
        return

    if getattr(args, "distinct", None):
        pair = _pair_ids(args.distinct)
        if pair is None:
            print(bad("  --distinct takes two different ids: --distinct 124,877"))
            return
        dupes.settle(conn, *pair)
        print(ok(f"  noted: #{pair[0]} and #{pair[1]} are different questions"))
        print(dim("  the scan will stop proposing them   ·   ")
              + head("dupes --all") + dim(" shows the settled ones again"))
        return

    if getattr(args, "undistinct", None):
        pair = _pair_ids(args.undistinct)
        if pair is None:
            print(bad("  --undistinct takes two different ids: --undistinct 124,877"))
            return
        if dupes.unsettle(conn, *pair):
            print(ok(f"  #{pair[0]} and #{pair[1]} are back in the scan"))
        else:
            print(dim(f"  #{pair[0]} and #{pair[1]} were not settled as different"))
        return

    if getattr(args, "pair", None):
        pair = _pair_ids(args.pair)
        if pair is None:
            print(bad("  --pair takes two different ids: --pair 124,877"))
            return
        a, b = pair
        known = {r["id"] for r in conn.execute(
            "SELECT id FROM questions WHERE id IN (?, ?)", pair)}
        for qid in pair:
            if qid not in known:
                print(bad(f"  no question #{qid}"))
                return
        _hand_off(views_mod.ComparePairView(
            conn, a, b, similarity=dupes.similarity_of(conn, a, b)))
        return

    found = dupes.pairs(conn, threshold=threshold,
                        topic=getattr(args, "topic", None),
                        include_settled=getattr(args, "all", False))
    if getattr(args, "json", False):
        print(json.dumps([{"similarity": round(p["similarity"], 4),
                           "a": p["a"]["id"], "b": p["b"]["id"]}
                          for p in found], indent=2))
        return
    if not found:
        settled = len(dupes.settled(conn))
        print(ok(f"  nothing is closer than {threshold:.0%} to anything else"))
        if settled and not getattr(args, "all", False):
            print(dim(f"  {settled} pair(s) already settled as different   ·   ")
                  + head("dupes --all") + dim(" shows them"))
        return
    _hand_off(views_mod.DupesView(conn, found, threshold=threshold))


# ---------------------------------------------------------------- plan

def cmd_plan(conn: sqlite3.Connection, args) -> None:
    """What you have to do each day to be ready by a date.

    The date is an argument, or the one on file, or a fortnight. Typing the
    same date into every invocation is how it ends up being typed wrong once,
    and a plan built against the wrong week is worse than no plan.
    """
    stored = plan_mod.target_date()
    if args.date:
        target = plan_mod.parse_target(args.date)
        if target is None:
            print(bad(f"could not read {args.date!r} as a date"))
            print(dim("  try: 2026-09-15  ·  +14d  ·  3 weeks  ·  sep 15  ·  tomorrow"))
            return
    elif stored:
        target = stored
    else:
        target = plan_mod.parse_target("+14d")
        print(dim("  no interview_date set, so this is a fortnight out."))
        print(dim("  `settings interview_date sep 15` and every screen counts down to it."))

    today = datetime.now(timezone.utc).date()
    if target <= today:
        print(bad(f"{target.isoformat()} is not in the future"))
        return

    p = plan_mod.build(conn, target, minutes_per_day=getattr(args, "minutes", None))
    if getattr(args, "json", False):
        print(json.dumps({**p, "calendar": plan_mod.calendar(conn, p)}, indent=2))
        return

    ready = p["readiness"]
    subject = (f"{target.isoformat()}  ·  {p['days']} days  ·  "
               f"{ready['score']:.0%} ready")
    if stored and target == stored:
        subject += "  ·  on file"
    _show_tabs("STUDY PLAN", [
        ("Pace", lambda w: _plan_pace(p)),
        ("Triage", lambda w: _plan_triage(p)),
        ("Order of attack", lambda w: _plan_order(p)),
        ("Next 7 days", lambda w: _plan_calendar(conn, p)),
    ], subject=subject)


def _plan_pace(p: dict) -> list[str]:
    left = [
        head("WHAT IS LEFT"),
        f"  never seen      {warn(str(p['unseen'])) if p['unseen'] else ok('0')}",
        f"  reviews overdue {p['backlog']}",
        f"  awaiting QA     {warn(str(p['needs_qa'])) if p['needs_qa'] else dim('0')}",
    ]
    right = [
        head("DAILY PACE"),
        f"  new questions   {ok(str(p['daily_new']))}",
        f"  reviews         {p['daily_reviews']}",
        "  total           " + ui.style(str(p["daily_total"]), BOLD)
        + dim(f"  \u2248 {p['minutes_per_day']} min"),
    ]
    out = ["  " + row for row in ui.columns(left, right, gap=4, left_w=34)]
    measured = p["seconds_per_question"] != plan_mod.DEFAULT_SECONDS_PER_QUESTION
    out += ["", dim(f"  paced at {p['seconds_per_question']}s a question, "
                    + ("measured from your own sittings" if measured
                       else "assumed until you have drilled more")), ""]
    if p["feasible"]:
        out.append(ok("  This fits.")
                   + dim(" Full coverage of the bank by the target date."))
    else:
        out.append(bad(f"  {p['daily_total']}/day is not a plan, it is a wish.")
                   + dim(f" Ceiling is {p['sustainable_daily']}/day."))
        out.append("  Reaches " + ok(str(p["reachable"])) + f"/{p['unseen']} unseen; "
                   + bad(f"{p['unreachable']} untouched") + dim(" by the target date."))
        out.append(dim("  the Triage tab says which topics that costs you"))
    today_n = p["daily_total"] if p["feasible"] else p["sustainable_daily"]
    out += ["", dim("  today: ") + ui.style(f"drill -n {today_n}", BOLD)
            + dim("  ·  ") + ui.style("drill --weak", BOLD)
            + dim("  ·  ") + ui.style("mock superday", BOLD)]
    return out


def _plan_triage(p: dict) -> list[str]:
    if p["feasible"]:
        return ["  " + ok("nothing to triage") + dim(" - the whole bank fits in the time")]
    if not p["triage"]:
        return ["  " + dim("no triage available")]
    out = [dim("  spend the days you have here, in this order"), ""]
    for t in p["triage"]:
        if not t["take"] and not t["dropped"]:
            continue
        covered = ok(f"{t['take']:>4}") if t["take"] else bad(f"{t['take']:>4}")
        drop = bad(f"  {t['dropped']} skipped") if t["dropped"] else ok("  all of it")
        out.append(f"    {ui.pad(t['topic'], 14)}{covered} of {t['of']:<5}{drop}")
    return out


def _plan_order(p: dict) -> list[str]:
    out = [dim(f"  {'topic':<14}{'unseen':>7}{'due':>6}{'coverage':>11}   mastery")]
    for t in p["topics"]:
        m = analytics.mastery_frac(t["avg_rating"])
        mast = f"{ui.meter(m, 10)} {m:>3.0%}" if m is not None else dim("·" * 10 + "   -")
        pace = ok(f"{t['daily_new']:>3}/day") if t["daily_new"] else dim("   done")
        out.append(f"  {t['topic']:<14}{t['unseen']:>7}{t['due']:>6}"
                   f"{t['coverage']:>10.0%}   {mast}  {pace}")
    return out


def _plan_calendar(conn: sqlite3.Connection, p: dict) -> list[str]:
    cal = plan_mod.calendar(conn, p, days_shown=7)
    if not cal:
        return ["  " + dim("nothing scheduled")]
    peak = max((d["minutes"] for d in cal), default=1) or 1
    out = []
    for d in cal:
        out.append(f"  {d['weekday']} {d['date'][5:]}   "
                   + ok(f"{d['new']:>3} new") + dim("  +  ")
                   + f"{d['reviews']:>3} reviews" + dim(f"   \u2248 {d['minutes']} min")
                   + "   " + ui.meter(d["minutes"] / peak, 14))
    return out


# ---------------------------------------------------------------- undo

def cmd_undo(conn: sqlite3.Connection, args) -> None:
    b = history.last_batch(conn)
    if b is None:
        print(dim("nothing to undo. Status, answer and question-text changes "
                  "are recorded from this version on."))
        return
    rows = history.batch_rows(conn, b["batch_id"])
    print(f"about to undo {head(b['action'])} from {dim(b['at'][:16])}, "
          f"{b['n']} change{'s' if b['n'] != 1 else ''}:")
    for r in rows[:8]:
        kind_of = r["change_type"] if "change_type" in r.keys() else None
        if kind_of == "question":
            old_q = " ".join((r["old_text"] or "(empty)").split())[:34]
            new_q = " ".join((r["new_text"] or "(empty)").split())[:34]
            print(f"  {dim('#' + str(r['question_id']))}  "
                  f"{dim('question')} \"{new_q}\" {dim('back to')} \"{old_q}\"")
            continue
        if (isinstance(r, dict) and r.get("change_type") == "answer") or (
            hasattr(r, "keys") and "new_answer_key" in r.keys() and r["new_answer_key"] is not None
        ):
            old_p = (r["old_answer_key"] or "(empty)")[:32]
            new_p = (r["new_answer_key"] or "(empty)")[:32]
            print(f"  {dim('#' + str(r['question_id']))}  "
                  f"{dim('answer')} \"{new_p}\" {dim('back to')} \"{old_p}\""
                  f"   {(r['canonical_text'] or '')[:40]}")
        else:
            print(f"  {dim('#' + str(r['question_id']))}  "
                  f"{verdict(r['new_status'])} {dim('back to')} {verdict(r['old_status'])}"
                  f"   {(r['canonical_text'] or '')[:44]}")
    if len(rows) > 8:
        print(dim(f"  ... and {len(rows) - 8} more"))
    if not args.yes:
        try:
            choice = input(warn("proceed? [y/N] > ")).strip().lower()
        except EOFError:
            choice = ""
        if choice != "y":
            print(dim("cancelled"))
            return
    n = history.undo_batch(conn, b["batch_id"])
    print(ok(f"reverted {n} changes"))


# ---------------------------------------------------------------- find

def cmd_find(conn: sqlite3.Connection, args) -> None:
    if getattr(args, "index_embeddings", False):
        search.index_embeddings(conn)
        return
    query = " ".join(args.query).strip()
    tag = getattr(args, "tag", None)
    if not query and not tag:
        print("nothing to search for")
        return

    status = None if args.all else "active"
    if getattr(args, "semantic", False):
        # Say it before the results rather than labelling keyword hits
        # SEMANTIC: the fallback is correct, pretending it did not happen is
        # not, and with Claude selected there is no embeddings endpoint to
        # fall back *from*.
        why = search.semantic_ready()
        if why:
            print(warn(f"  {why}"))
        rows = search.find_semantic(conn, query, limit=args.limit, status=status)
        mode = "exact" if why else "semantic"
    elif not query:
        rows = _tag_listing(conn, tag, args.limit, status)
        if rows is None:
            return
        mode = "tag"
    elif getattr(args, "exact", False):
        rows, mode = search.find(conn, query, limit=args.limit, status=status), "exact"
    else:
        rows, mode = search.search(conn, query, limit=args.limit, status=status)

    if tag and query:
        resolved = tagging.resolve(conn, tag) or tag
        keep = {r["id"] for r in conn.execute(
            "SELECT qt.question_id id FROM question_tags qt JOIN tags t ON t.id = qt.tag_id "
            "WHERE t.name = ?", (resolved,))}
        rows = [r for r in rows if r["id"] in keep]

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        print(dim(f"nothing matches {query!r}" + (f" tagged #{tag}" if tag else "")))
        print(dim('  FTS5 syntax works: quoted "exact phrase", prefix*, AND/OR/NOT'))
        print(dim("  or try fewer words -- misspellings and abbreviations are handled"))
        return

    label = {"exact": "FIND", "fuzzy": "CLOSEST MATCH", "semantic": "SEMANTIC",
             "tag": "TAGGED"}[mode]
    subject = query or "#" + (tagging.resolve(conn, tag) or tag)
    note = ("no exact match, so this is closest-match: misspellings, "
            "abbreviations and partial words" if mode == "fuzzy" else "")
    _present(conn, rows, title=label, subject=subject, note=note,
             highlight=re.findall(r"[A-Za-z0-9']+", query))


def _present(conn: sqlite3.Connection, rows: list[dict], *, title: str,
             subject: str = "", note: str = "",
             highlight: list[str] | None = None) -> None:
    """Hand a result set to the shell if there is one, print it if not.

    Both branches go through ResultsView so the columns, the ordering and the
    truncation are defined once. A piped `find` and an interactive one differ
    only in whether the arrow keys do anything.
    """
    view = views_mod.ResultsView(conn, rows, title=title, subject=subject,
                                 note=note, highlight=highlight)
    if tui.attach(view):
        return
    for line in view.flatten(ui.width()):
        print(line)
    print()
    print(dim("  show <id>  for the full record"))


def _id_list(raw: str | None) -> list[int] | None:
    """Parse a `--ids` value. None means "no restriction", [] means "none".

    The distinction matters: a browse whose filters matched nothing must hand
    over an empty selection rather than silently falling back to the whole
    bank, which is what None would do.
    """
    if raw is None:
        return None
    out: list[int] = []
    for chunk in str(raw).replace(" ", ",").split(","):
        if chunk.strip().isdigit():
            out.append(int(chunk))
    return out


def cmd_browse(conn: sqlite3.Connection, args) -> None:
    """Walk the bank with filters you stack, then drill what is left.

    Filters given on the command line are just a starting position; the view
    is where they get added and dropped. Both ways in build the same facet
    list, so `browse --topic lbo` and pressing alt-n twice land in the same
    place.
    """
    tagging.ensure_tree(conn)

    facets: list[tuple[str, str]] = []
    for topic in (args.topic or []):
        facets.append(("topic", topic))
    for tag in (args.tag or []):
        # A tag typed at the shell is resolved the way `find --tag` resolves
        # it, so a prefix works; a family name resolves to itself.
        facets.append(("tag", tagging.resolve(conn, tag) or tagging.normalize_name(tag)))
    for kind in (args.kind or []):
        facets.append(("kind", kind))
    for flag in (args.flag or []):
        facets.append(("flag", flag))
    if args.all:
        facets += [("status", s) for s in ("active", "needs_review", "rejected")]

    # How they combine is a starting position too: `--any` opens OR-ed and the
    # view's own ⇄ row flips it, so neither way in can reach a state the other
    # cannot.
    match = browse.Match.of(tags_all=args.tags_all, any_of=args.any)

    if args.json:
        print(json.dumps(browse.matching(conn, facets, match=match),
                         indent=2, default=str))
        return

    unknown = [t for k, t in facets
               if k == "tag" and not tagging.descendants(conn, t) - {t}
               and not conn.execute("SELECT 1 FROM tags WHERE name = ?", (t,)).fetchone()]
    for name in unknown:
        print(warn(f"  no tag matching '{name}' - it will match nothing"))

    view = views_mod.BrowseView(conn, facets, match=match)
    if tui.attach(view):
        return
    for line in view.flatten(ui.width()):
        print(line)
    print()
    print(dim("  " + browse.describe(facets, match)))
    print(dim("  run it inside the shell to walk it with the arrow keys, "
              "or narrow with --topic / --tag / --flag"))


def _tag_listing(conn: sqlite3.Connection, tag: str, limit: int,
                 status: str | None) -> list[dict] | None:
    """Everything carrying a tag, when there is no text query to rank by.

    None means the tag does not exist, which is a different answer from "the
    tag exists and is empty" and deserves a different message.
    """
    resolved = tagging.resolve(conn, tag)
    if resolved is None:
        print(warn(f"  no tag matching '{tag}'"))
        near = [t["name"] for t in tagging.all_tags(conn)
                if any(part in t["name"] for part in tagging.normalize_name(tag).split("-"))]
        if near:
            print(dim("  did you mean: ") + ", ".join(head("#" + n) for n in near[:6]))
        else:
            print(dim("  superday tags   lists every tag in the bank"))
        return None
    sql = ("SELECT q.id, q.canonical_text, q.topic, q.status FROM questions q "
           "JOIN question_tags qt ON qt.question_id = q.id "
           "JOIN tags t ON t.id = qt.tag_id WHERE t.name = ?")
    params: list = [resolved]
    if status:
        sql += " AND q.status = ?"
        params.append(status)
    sql += " ORDER BY q.difficulty IS NULL, q.difficulty, q.id LIMIT ?"
    params.append(limit)
    return [dict(r) | {"excerpt": None} for r in conn.execute(sql, params)]


# ---------------------------------------------------------------- export

# Commands that change what a question *is*. Deliberately not `drill` or
# `mock`: those change how you are doing, which the default export does not
# carry, so re-exporting after every sitting would only ever write nothing.
CONTENT_COMMANDS = {
    "ingest", "ingest-pdf", "ingest-epub", "ingest-web", "ingest-video",
    "ingest-filing", "ingest-pack", "enrich", "reground", "audit", "review", "accept-all",
    "edit", "add", "tag", "untag", "autotag", "dupes", "undo", "market",
    "gate", "cross-audit", "consult",
}


def _refresh_export(conn: sqlite3.Connection, cmd: str, args=None) -> None:
    """Keep the Markdown copy in step, if one has been asked for.

    Costs about six milliseconds and writes nothing when nothing moved, which
    is what makes it safe to hang off every mutating command rather than a
    daily timer that mostly produces empty diffs.
    """
    if cmd not in CONTENT_COMMANDS:
        return
    # A dry run is a command that says what it *would* do. Refreshing the
    # Markdown off the back of one writes files for a change that never
    # happened.
    if getattr(args, "dry_run", False):
        return
    # The export tracks *your* bank, and `export_md_dir` is one path in one
    # config file while `IB_DB` can point anywhere. Rehearsing a write path
    # against a throwaway copy -- which is the documented way to do it -- was
    # therefore rewriting the real bank's Markdown from the copy's contents:
    # ingest a 200-question test database and the export of a 1,086-question
    # bank is replaced wholesale, with no command having said so.
    if db_path() != config_mod.home() / "ib.db":
        return
    target = (config_mod.load().get("export_md_dir") or "").strip()
    if not target:
        return
    try:
        res = backup.export_markdown(conn, Path(target).expanduser())
    except OSError as e:
        print(warn(f"  markdown export skipped - {_why(e)}"))
        return
    if res["written"]:
        print(dim(f"  markdown export: {res['written']} file(s) updated in {res['dir']}"))


def cmd_export(conn: sqlite3.Connection, args) -> None:
    out = Path(args.out).expanduser() if args.out else None
    if getattr(args, "md", False):
        res = backup.export_markdown(conn, out,
                                     with_progress=getattr(args, "with_progress", False))
        print(ok(f"  {res['questions']} questions across {res['topics']} topics"
                 f"  ->  {res['dir']}"))
        if res["written"]:
            print(dim(f"  {res['written']} file(s) written, {res['unchanged']} unchanged"))
        else:
            print(dim("  nothing changed since the last export"))
        if not getattr(args, "with_progress", False):
            print(dim("  no ratings, notes or schedule in here - safe to share"))
        return
    if getattr(args, "anki", False):
        path, count = backup.export_anki(conn, out)
        size = path.stat().st_size / 1e3
        print(ok(f"wrote {path}  ({size:.1f} KB, {count} questions)"))
        print(dim("  ready to import into Anki (Front: Question, Back: Answer + Rubric, Tags: Topic)"))
        return
    if args.sqlite:
        path = backup.snapshot(conn, out)
        size = path.stat().st_size / 1e6
        print(ok(f"wrote {path}  ({size:.1f} MB)"))
        print(dim("  a consistent copy, safe to take while the tool is running"))
        return
    path, counts = backup.export_json(conn, out)
    size = path.stat().st_size / 1e6
    print(ok(f"wrote {path}  ({size:.1f} MB)"))
    for name, n in counts.items():
        print(ui.kv(name, str(n), width=24))
    print(rule())
    print(dim("  reviews, schedule and notes are in here too: this is the half"))
    print(dim("  of the database that cannot be rebuilt by re-running extraction."))


def cmd_consult(conn: sqlite3.Connection, args) -> None:
    """Batch out to any outside model, verdicts back in, no API key either way."""
    if args.import_path:
        path = Path(args.import_path).expanduser()
        if not path.exists():
            print(bad(f"  no such file: {path}"))
            return
        items, problems = consult_mod.parse(path.read_text(encoding="utf-8"))
        for p in problems:
            print(warn(f"  {p}"))
        if not items:
            print(dim("  nothing filed"))
            return
        n, complaints = consult_mod.file_verdicts(conn, items, args.provider)
        for c in complaints:
            print(warn(f"  {c}"))
        counts: dict[str, int] = {}
        for it in items:
            counts[it["verdict"]] = counts.get(it["verdict"], 0) + 1
        print(ok(f"  filed {n} verdicts as '{args.provider}'   ")
              + dim("  ".join(f"{k} {v}" for k, v in sorted(counts.items()))))
        print(dim("  they sit beside the other opinions, never over them"))
        print(dim("  see where they differ:  disagreements"))
        return

    out = Path(args.export or (config_mod.home() / "consult-batch.md")).expanduser()
    path, n = consult_mod.write_batch(conn, out, args.limit,
                                      include_seen=args.include_seen)
    if not n:
        print(ok("  nothing left worth an outside opinion"))
        print(dim("  --include-seen to go over ground already covered"))
        return
    print(ok(f"  {n} questions  ->  {path}"))
    print(rule())
    print(dim("  1. paste that file into any model (it carries its own instructions)"))
    print(dim("  2. save the reply to a file"))
    print(dim(f"  3. superday consult --import <reply> --provider <name>"))


def cmd_completions(conn: sqlite3.Connection, args) -> None:
    """Keep the shell completion honest with the parser that defines it."""
    from . import completions as comp
    parser = build_parser()
    if args.write:
        path = comp.write(parser)
        print(ok(f"wrote {path}"))
        print(dim("  reload it: exec zsh"))
        return
    in_sync, _ = comp.check(parser)
    if in_sync:
        print(ok("completions are current"))
    else:
        print(warn("completions have drifted from the parser"))
        print(dim("  fix it: superday completions --write"))


# ---------------------------------------------------------------- main

class _Parser(argparse.ArgumentParser):
    """argparse, minus the wall of text.

    The stock `error()` prints the full usage line, which for this tool is
    every one of the thirty-odd subcommands listed twice. In a full-screen
    shell that is forty lines of noise burying the one sentence that says
    what you typed wrong.
    """

    _BAD_CHOICE = re.compile(r"invalid choice: '([^']+)' \(choose from (.+)\)")

    def error(self, message: str) -> None:      # type: ignore[override]
        m = self._BAD_CHOICE.search(message)
        if m:
            typed = m.group(1)
            choices = [c.strip().strip("'") for c in m.group(2).split(",")]
            print(bad(f"  no command called {typed!r}"))
            near = difflib.get_close_matches(typed, choices, n=3, cutoff=0.5)
            near += [c for c in choices if c.startswith(typed) and c not in near]
            if near:
                print(dim("  did you mean ")
                      + dim(", ").join(head(c) for c in near[:3]) + dim("?"))
            else:
                print(dim("  ?   for the list of commands"))
            raise SystemExit(2)
        print(bad(f"  {message}"))
        name = self.prog.split()[-1]
        print(dim(f"  {name} -h   for what it takes" if name != "superday"
                  else "  ?   for the list of commands"))
        raise SystemExit(2)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        if message:
            print(dim("  " + message.strip()))
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="superday", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    s = sub.add_parser("ingest", help="parse corpus docx into the bank")
    s.add_argument("path", nargs="?", help="file or directory (default: corpus docx)")
    s.add_argument("--force", action="store_true", help="re-ingest an already seen file")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("ingest-pdf", help="extract questions from the PDF guides")
    s.add_argument("path", nargs="?", help="file or directory (default: HandBooks + BIWS)")
    s.add_argument("--force", action="store_true", help="re-ingest an already seen file")
    s.add_argument("--window", type=int, default=6, help="pages per chunk (default: 6)")
    s.add_argument("--max-chunks", type=int, default=None,
                   help="stop early, for a trial run against a new book")
    s.set_defaults(fn=cmd_ingest_pdf)

    s = sub.add_parser("ingest-epub", help="extract questions from EPUB guides")
    s.add_argument("path", nargs="?", help="file or directory (default: corpus dir)")
    s.add_argument("--force", action="store_true", help="re-ingest an already seen file")
    s.add_argument("--window", type=int, default=9000, metavar="CHARS",
                   help="characters per extraction chunk")
    s.add_argument("--max-chunks", type=int, default=None,
                   help="stop early, for a trial run against a new book")
    s.set_defaults(fn=cmd_ingest_epub)

    s = sub.add_parser("ingest-web",
                       help="extract questions from a forum thread, article or saved page")
    s.add_argument("url", nargs="+", help="URL, or a path to a saved .html file")
    s.add_argument("--asked", action="store_true",
                   help="mark these as asked in a real interview (outranks textbook Qs)")
    s.add_argument("--force", action="store_true", help="re-ingest even if unchanged")
    s.set_defaults(fn=cmd_ingest_web)

    s = sub.add_parser("ingest-filing",
                       help="build questions from a company's filed XBRL figures")
    s.add_argument("ticker", help="ticker symbol or CIK number, e.g. AAPL")
    s.add_argument("--year", type=int, default=None, metavar="FY",
                   help="fiscal year (default: most recent filed)")
    s.add_argument("--dry-run", action="store_true",
                   help="print the questions without saving them")
    s.set_defaults(fn=cmd_ingest_filing)

    s = sub.add_parser("ingest-pack",
                       help="land an authored question pack (JSON, no API key)")
    s.add_argument("path", nargs="+",
                   help="pack file, a directory of them, a shipped pack name, or `all`")
    s.add_argument("--status", default=None, choices=["needs_review", "active"],
                   help="override the status the pack asks for")
    s.add_argument("--dry-run", action="store_true",
                   help="validate and show the spread without saving")
    s.set_defaults(fn=cmd_ingest_pack)

    s = sub.add_parser("reground",
                       help="re-read ingested PDFs to repair provenance and phrasings")
    s.add_argument("path", nargs="?", help="file or directory (default: corpus PDFs)")
    s.add_argument("--window", type=int, default=6,
                   help="pages per chunk, must match the ingest run")
    s.add_argument("--show", type=int, default=5,
                   help="how many unmatched candidate phrases to list at the end")
    s.set_defaults(fn=cmd_reground)

    s = sub.add_parser("enrich",
                       help="real rubrics, topics and difficulty via LLM")
    s.add_argument("--batch", type=int, default=6, help="batch size (default: 6)")
    s.add_argument("--limit", type=int, default=None, help="max to process")
    s.add_argument("--missing-answers", action="store_true",
                   help="auto-draft model answers and rubrics for questions without one")
    s.set_defaults(fn=cmd_enrich)

    s = sub.add_parser("audit",
                       help="second-opinion QA over extracted questions")
    s.add_argument("--batch", type=int, default=8, help="batch size (default: 8)")
    s.add_argument("--limit", type=int, default=None, help="max to process")
    s.add_argument("--status", default="needs_review", choices=["needs_review", "active"],
                   help="status to audit (default: needs_review)")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("review", help="work the review queue")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("accept-all", help="accept everything pending review")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(fn=cmd_accept_all)

    s = sub.add_parser("drill", help="get asked questions")
    s.add_argument("--count", "-n", type=int, default=10, help="how many questions")
    s.add_argument("--topic", "-t", default=None, help="topic filter")
    s.add_argument("--tag", default=None, help="tag filter (e.g. ev-bridge, irr)")
    s.add_argument("--kind", "-k", default=None,
                   choices=["technical", "market_awareness", "behavioural"])
    s.add_argument("--no-grade", action="store_true",
                   help="reveal rubric immediately rather than grading your spoken answer")
    s.add_argument("--local", action="store_true",
                   help="guarantee zero API calls: self-rate against the stored rubric")
    s.add_argument("--weak", "-w", action="store_true",
                   help="worst-rated questions first instead of a fresh spread")
    s.add_argument("--resume", "-r", action="store_true",
                   help="pick up the sitting you walked away from")
    s.add_argument("--ids", default=None, metavar="1,2,3",
                   help="drill exactly these question ids (what `browse` hands over)")
    s.add_argument("--again", "-a", action="store_true",
                   help="ignore the due window and ask anyway (the quarantine still applies)")
    s.set_defaults(fn=cmd_drill)

    s = sub.add_parser("browse",
                       help="walk the bank by topic and tag, stacking filters")
    s.add_argument("--topic", "-t", action="append", default=None,
                   help="start filtered to this topic (repeatable)")
    s.add_argument("--tag", action="append", default=None,
                   help="start filtered to this tag or tag family (repeatable)")
    s.add_argument("--kind", "-k", action="append", default=None,
                   choices=["technical", "market_awareness", "behavioural"],
                   help="start filtered to this question kind (repeatable)")
    s.add_argument("--flag", action="append", default=None,
                   choices=sorted(browse.FLAGS),
                   help="start filtered to due / unseen / weak / disputed / untagged")
    s.add_argument("--all", action="store_true",
                   help="include needs_review and rejected, not just active")
    s.add_argument("--tags-all", action="store_true",
                   help="require every tag rather than any of them")
    s.add_argument("--any", action="store_true",
                   help="OR the filters together instead of AND-ing them")
    s.add_argument("--json", action="store_true", help="machine-readable selection")
    s.set_defaults(fn=cmd_browse)

    s = sub.add_parser("recap", help="the questions you have already answered, and how they went")
    s.add_argument("window", nargs="?", default="today",
                   help="session, today, yesterday, week, month, all, 7d, 3 weeks")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_recap)

    s = sub.add_parser("chains", help="question lines: follow-ups that need the question before them")
    s.add_argument("--scan", action="store_true",
                   help="find questions that read like a reply to the one before")
    s.add_argument("--tier", default="all", choices=["all", "certain", "likely"],
                   help="certain names a previous turn; likely opens like a reply")
    s.add_argument("--apply", action="store_true",
                   help="with --scan: link every candidate that has a lead-in")
    s.add_argument("--link", nargs=2, type=int, metavar=("CHILD", "PARENT"),
                   help="record that CHILD follows PARENT")
    s.add_argument("--unlink", type=int, metavar="CHILD",
                   help="drop a question's lead-in")
    s.add_argument("--standalone", type=int, metavar="ID",
                   help="it reads like a follow-up but is fine alone - stop asking")
    s.add_argument("--graph", type=int, metavar="ID",
                   help="the whole line this question sits in, as a tree")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_chains)

    s = sub.add_parser("sessions", help="drill and mock sittings, and what can be resumed")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("dashboard", help="single-screen executive readiness dashboard")
    s.add_argument("--json", action="store_true", help="machine-readable readiness snapshot")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("tag", help="add tags to a question")
    s.add_argument("id", type=int, help="question ID")
    s.add_argument("tags", nargs="+", help="tag names (e.g. ev-bridge, wacc)")
    s.add_argument("--kind", default="concept", choices=["concept", "topic", "company", "difficulty"],
                   help="tag classification (default: concept)")
    s.set_defaults(fn=cmd_tag)

    s = sub.add_parser("untag", help="remove tags from a question")
    s.add_argument("id", type=int, help="question ID")
    s.add_argument("tags", nargs="+", help="tag names to remove")
    s.set_defaults(fn=cmd_untag)

    s = sub.add_parser("plan",
                       help="daily pace needed to be ready by an interview date")
    s.add_argument("date", nargs="?", default=None,
                   help="target date: 2026-09-15, +14d, 3 weeks, sep 15, tomorrow "
                        "(default: the interview_date setting)")
    s.add_argument("--minutes", type=int, default=None, metavar="N",
                   help="minutes a day you can actually give it")
    s.add_argument("--json", action="store_true", help="machine-readable plan")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("dupes", help="find and merge near-duplicate questions in the bank")
    s.add_argument("--threshold", type=float, default=0.70, help="similarity threshold (default: 0.70)")
    s.add_argument("--topic", "-t", default=None, help="filter to specific topic")
    s.add_argument("--pair", default=None, metavar="A,B",
                   help="put two questions side by side, facet by facet")
    s.add_argument("--merge", default=None, metavar="KEEPER,DUPE",
                   help="fold DUPE into KEEPER, keeping reviews, notes and tags")
    s.add_argument("--distinct", default=None, metavar="A,B",
                   help="they are different questions - stop proposing the pair")
    s.add_argument("--undistinct", default=None, metavar="A,B",
                   help="undo a --distinct verdict - the scan will propose the pair again")
    s.add_argument("--all", action="store_true",
                   help="include pairs already settled as different")
    s.add_argument("--phrasings", action="store_true",
                   help="wordings that no longer ask the question they hang off")
    s.add_argument("--detach", type=int, default=None, metavar="ID",
                   help="take one phrasing off its question")
    s.add_argument("--keep-phrasing", type=int, default=None, metavar="ID",
                   help="that wording belongs where it is - stop proposing it")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_dupes)

    s = sub.add_parser("mock", help="timed mock interview with a scorecard")
    s.add_argument("round", choices=list(mock.ROUNDS.keys()), default="technical", nargs="?")
    s.add_argument("--persona", choices=list(mock.PERSONAS.keys()), default="standard",
                   help="interviewer persona (standard, skeptical_md, exacting_vp)")
    s.add_argument("--local", action="store_true",
                   help="guarantee zero API calls: self-rate at the end")
    s.add_argument("--ids", default=None, metavar="1,2,3",
                   help="draw only from these question ids (what `browse` hands over)")
    s.set_defaults(fn=lambda c, a: mock.run(c, a.round, a.persona, local=a.local,
                                            ids=_id_list(a.ids)))

    s = sub.add_parser("autotag",
                       help="apply the concept taxonomy across the bank, no API key needed")
    s.add_argument("--limit", type=int, default=None, help="stop after N questions")
    s.add_argument("--all", action="store_true",
                   help="re-scan questions that already carry tags")
    s.set_defaults(fn=cmd_autotag)

    s = sub.add_parser("tags", help="every concept tag, with size and how it has gone")
    s.add_argument("--prune", action="store_true",
                   help="remove tags no question carries")
    s.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompt")
    s.add_argument("--min", type=int, default=1, dest="min_count", metavar="N",
                   help="hide tags carried by fewer than N questions")
    s.add_argument("--json", action="store_true", help="machine-readable tag map")
    s.set_defaults(fn=cmd_tags)

    s = sub.add_parser("market",
                       help="seed and refresh market awareness questions")
    s.add_argument("--refresh", action="store_true",
                   help="pull every provider into the cache so drills work offline")
    s.set_defaults(fn=cmd_market)

    s = sub.add_parser("add", help="add a single question you ran into")
    # nargs="+" so an unquoted question works in the shell. cmd_add has always
    # joined this with spaces; as a single string that join ran over the
    # characters, and `add what is EBITDA` stored "w h a t   i s   E B I T D A".
    s.add_argument("text", nargs="+", help="the question")
    s.add_argument("--answer", "-a", default=None, help="model answer, if you have one")
    s.add_argument("--llm", "-l", action="store_true",
                   help="one LLM call: topic, rubric and a full answer "
                        "(polishing --answer if you gave one). Lands needs_review")
    s.add_argument("--origin", "-o", default="self_authored",
                   choices=["self_authored", "interviewer_asked", "published"])
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("list", help="list topics, or drill one: list <topic>")
    s.add_argument("topic", nargs="?", default=None, choices=list(TOPICS))
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("gate", help="what the admission gate admitted, merged and dropped")
    s.add_argument("--source", default=None,
                   help="filter to one source, by title substring")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("cross-audit",
                       help="an independent second opinion, next to the first")
    # dest must be export_path: argparse would otherwise name it `export`,
    # which is not what cmd_cross_audit reads, and the flag silently did
    # nothing. const is "" so a bare --export falls through to the default
    # filename rather than writing a file literally called "default".
    s.add_argument("--export", dest="export_path", nargs="?", const="",
                   metavar="PATH",
                   help="write a batch to JSON for Claude Code to review")
    s.add_argument("--import", dest="import_path",
                   help="file verdicts from a reviewed batch")
    # Same nargs="?"/const="" shape as --export, and for the same reason: a
    # bare --apply means "everything eligible", not a file or an id called "".
    s.add_argument("--apply", dest="apply_ids", nargs="?", const="",
                   metavar="1,2,3",
                   help="write filed corrections into the bank (all, or these ids)")
    s.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompt")
    s.add_argument("--api", action="store_true",
                   help="run it unattended on a provider you hold a key for")
    s.add_argument("--using", choices=list(llm.PROVIDERS), metavar="PROVIDER",
                   help="pin the second opinion to this provider instead of "
                        "letting it pick one that did not give the first")
    s.add_argument("--target", default="kept", choices=list(crossaudit.TARGETS),
                   help="which questions to check (default: ones the first audit kept)")
    s.add_argument("--limit", type=int, default=40, help="how many, per run")
    s.add_argument("--batch", type=int, default=8, help="questions per API call")
    s.set_defaults(fn=cmd_cross_audit)

    s = sub.add_parser("disagreements",
                       help="where the two audit passes do not agree")
    s.add_argument("--review", "-r", action="store_true",
                   help="work through them one at a time")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_disagreements)

    s = sub.add_parser("check",
                       help="find answers that are provably wrong, no API key needed")
    s.add_argument("--all", action="store_true",
                   help="include rejected and unreviewed questions")
    s.add_argument("--limit", type=int, default=None, help="stop after N questions")
    s.add_argument("--json", action="store_true", help="machine-readable findings")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("show", help="everything known about one question")
    s.add_argument("--json", action="store_true", help="the whole record as JSON")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("edit", help="edit a question's text, answer, topic or rubric")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("undo", help="take back the last status change")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("find", help="full text search across the bank")
    s.add_argument("--tag", default=None, help="restrict to a concept tag")
    s.add_argument("--exact", action="store_true",
                   help="FTS only: do not fall back to closest-match")
    s.add_argument("--json", action="store_true", help="machine-readable results")
    s.add_argument("query", nargs="*", default=[])
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--all", action="store_true", help="include rejected and unreviewed")
    s.add_argument("--semantic", action="store_true",
                   help="semantic vector similarity search via embeddings")
    s.add_argument("--index-embeddings", action="store_true",
                   help="backfill vector embeddings for questions")
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("export", help="back the bank up: JSON, Markdown, .sqlite or Anki")
    s.add_argument("out", nargs="?", help="where to write it")
    s.add_argument("--sqlite", action="store_true", help="byte-exact db copy instead of JSON")
    s.add_argument("--anki", action="store_true", help="export active questions to Anki-compatible TSV")
    s.add_argument("--md", action="store_true",
                   help="readable Markdown, one file per topic plus an index")
    s.add_argument("--with-progress", action="store_true",
                   help="include your ratings, notes and schedule (off by default)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("consult",
                       help="batch questions out to any model as Markdown, file the reply back")
    s.add_argument("--export", nargs="?", const="", metavar="PATH",
                   help="write a review batch (default: consult-batch.md)")
    s.add_argument("--import", dest="import_path", metavar="PATH",
                   help="file the verdicts from a reply")
    s.add_argument("--provider", default="external",
                   help="who answered, e.g. gpt-5 (recorded with the verdict)")
    s.add_argument("--limit", type=int, default=25, help="questions per batch")
    s.add_argument("--include-seen", action="store_true",
                   help="include questions an outside model has already judged")
    s.set_defaults(fn=cmd_consult)

    s = sub.add_parser("completions",
                       help="regenerate the zsh completion file from this parser")
    s.add_argument("--write", action="store_true", help="write it, do not just print it")
    s.set_defaults(fn=cmd_completions)

    s = sub.add_parser("selftest", help="run the regression tests")
    s.set_defaults(fn=lambda c, a: sys.exit(_selftest()))

    s = sub.add_parser("stats", help="what is in the bank")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("usage",
                       help="how many provider calls you have made, and what got refused")
    s.add_argument("--provider", default=None, choices=list(llm.PROVIDERS),
                   help="only one provider's calls")
    s.add_argument("--clear", action="store_true", help="empty the log")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("llm",
                       help="which provider answers, and whether its key works")
    s.add_argument("--test", nargs="?", const="all", metavar="PROVIDER",
                   help="spend one small call proving a key works; omit the "
                        "name to test every key you hold")
    s.add_argument("--use", choices=list(llm.PROVIDERS), metavar="PROVIDER",
                   help="switch every job to this provider")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(fn=cmd_llm)

    s = sub.add_parser("settings", help="view or change configuration")
    key_arg = s.add_argument("key", nargs="?",
                             help="e.g. corpus_dir, desired_retention, model_grade")
    # Offered for completion but not enforced by argparse: `settings ret 0.85`
    # resolves by prefix, and choices= would reject it before it got there.
    key_arg.completions = [e["key"] for e in SETTINGS]
    s.add_argument("value", nargs="*", help="new value; omit to just view")
    s.add_argument("--reset", action="store_true", help="revert this key to its default")
    s.set_defaults(fn=cmd_settings)

    return p


def _subcommand_names(p: argparse.ArgumentParser) -> list[str]:
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    return []


# The mark: a small ascending bar-chart, two rows of the same eighth-block
# glyphs `ui.sparkline` draws trend lines with. Growth is the one idea this
# tool is for, so the logo says it before any text does.
_LOGO = ("  ▀█", "▄███")


def _banner(conn: sqlite3.Connection) -> str:
    """The first thing you see, and -- pinned above the transcript rather
    than scrolled past -- the thing you always see.

    Same numbers as the dashboard, on purpose: the banner used to count only
    scheduled cards and greet you with "8 due" while the dashboard behind it
    said 823. Beyond that it earns its space by answering one question --
    what should I do right now -- and then getting out of the way.
    """
    c = analytics.counts(conn)
    st = analytics.streak(conn)

    subtitle = ui.paint("interview bank", "text")
    target = plan_mod.target_date()
    if target:
        days = (target - datetime.now(timezone.utc).date()).days
        subtitle += (dim("  ·  ") + ui.chip(f"{days}d", "coral")
                     + dim(" to your superday"))

    content = [
        ui.paint(_LOGO[0], "mauve", BOLD) + "  " + ui.gradient("superday"),
        ui.paint(_LOGO[1], "accent", BOLD) + "  " + subtitle,
        "",
    ]

    today = ui.paint(datetime.now(timezone.utc).astimezone().strftime("%a, %b %d"),
                      "text", BOLD)
    who = llm.provider_label()
    if llm.available():
        status = ui.dot("mint") + " " + dim(f"{who} · {llm.model_grade()}")
    else:
        status = ui.dot("coral") + " " + dim(f"{who} not configured")
    content.append(today + dim("   ·   ") + status)

    facts = [ui.paint(f"{c['active']}", "text", BOLD) + dim(" in bank")]
    facts.append((warn(f"{c['due_now']} due") if c["due_now"] else ok("all caught up")))
    if st["current"] >= 2:
        facts.append(ok(f"{st['current']}d streak"))
    if c["needs_review"]:
        facts.append(warn(f"{c['needs_review']} awaiting QA"))
    content.append(dim("  ·  ").join(facts))

    resumable = session.resumable(conn, "drill")
    if resumable:
        left = len(json.loads(resumable["queue_json"]))
        # Dated, because a sitting stays resumable forever and this banner had
        # no way of saying whether it was from ten minutes ago or last week --
        # an offer to pick up "your last sitting" reads as something you just
        # put down.
        when = resumable["updated_at"][:10]
        age = (datetime.now(timezone.utc).date()
               - datetime.fromisoformat(resumable["updated_at"]).date()).days
        stamp = {0: "today", 1: "yesterday"}.get(age, when)
        content += ["", ui.chip("resume", "gold") + " "
                    + ui.style(f"{left} question{'' if left == 1 else 's'} left in your "
                               f"sitting from {stamp}", BOLD)
                    + dim("   press r")]

    lines = [""] + ["  " + line if line else "" for line in content] + [""]
    return "\n".join(lines)


_HELP_GROUPS = [
    ("STUDY", ["drill", "mock", "plan", "list", "find", "browse", "show",
               "recap", "sessions", "dashboard"]),
    ("ORGANISE", ["tag", "untag", "tags", "autotag", "edit", "add"]),
    ("INGEST & SOURCES", ["ingest", "ingest-pdf", "ingest-epub", "ingest-web",
                          "ingest-filing", "ingest-pack", "enrich",
                          "reground", "market"]),
    ("QUALITY", ["review", "accept-all", "audit", "check", "cross-audit",
                 "consult", "disagreements", "chains", "gate", "dupes", "undo"]),
    ("BANK", ["stats", "settings", "llm", "usage", "export"]),
    ("DEV", ["selftest", "completions"]),
]

# One keystroke to the thing you were going to type anyway. Kept in one table
# so the dispatcher and the help screen cannot disagree about what `p`
# does -- they used to, and the help was the one that was wrong.
HOTKEYS: dict[str, list[str]] = {
    "d": ["drill"],
    "w": ["drill", "--weak"],
    "r": ["drill", "--resume"],
    "m": ["mock"],
    "l": ["list"],
    "s": ["stats"],
    "t": ["tags"],
    "p": ["plan", "+14d"],
    "g": ["dashboard"],
    "home": ["dashboard"],
    "dash": ["dashboard"],
}

# What the keyboard does in the shell itself, as opposed to what the commands
# do. Written down here because a key nobody can discover may as well not be
# bound at all.
KEYMAP = [
    ("↑ ↓", "move the selection in a list"),
    ("⏎", "open the selected row - a question opens its full record"),
    ("→ ←", "peek · back out - `←` on a row is what you can do with it"),
    ("PgUp PgDn", "a screenful at a time in whatever list is up"),
    # Plain ⇥ belongs to the input line's completion menu and never reaches a
    # view (`Shell._VIEW_KEYS`), so advertising it here sent you to a key that
    # does something else entirely. The sort has always been on ⇧⇥.
    ("⇧⇥", "cycle the sort: relevance, topic, difficulty, due, id"),
    ("^P ^N", "walk your command history - ↑↓ belong to the list while one is up"),
    ("esc", "put the list down - it leaves the screen clean behind it"),
    ("^L", "clear the screen · same as typing clear"),
    ("click", "a row to select it, again to open it · a tab or a filter acts once"),
    ("hover", "lights the row under the pointer - that is what a click would take"),
    ("⌥drag", "select text - rows are clickable, so plain drag is taken"),
    ("scroll", "the list first, then the transcript behind it · ⇧↑ ⇧↓ too"),
    ("^C", "clear the line · ^D or exit to leave"),
]


def _command_help(p: argparse.ArgumentParser) -> dict[str, str]:
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {a.dest: (a.help or "") for a in action._choices_actions}
    return {}


def _help_lines(p: argparse.ArgumentParser, w: int) -> list[str]:
    helps = _command_help(p)
    grouped = {name for _, names in _HELP_GROUPS for name in names}
    leftover = sorted(n for n in helps if n not in grouped)
    groups = _HELP_GROUPS + ([("OTHER", leftover)] if leftover else [])
    name_w = max((len(n) for n in helps), default=8) + 2

    out: list[str] = []
    for title, names in groups:
        names = [n for n in names if n in helps]
        if not names:
            continue
        out.append("")
        out.append("  " + ui.paint(title, "mauve", BOLD))
        for name in names:
            out.append("    " + ui.paint(name.ljust(name_w), "text", BOLD)
                        + dim(ui.truncate(helps[name], w - name_w - 8)))

    out.append("")
    out.append("  " + ui.paint("KEYS", "mauve", BOLD))
    for key, what in KEYMAP:
        # Width comes from the longest key rather than a constant: the column
        # was 7 cells, so "PgUp PgDn" did not fit and the pair was split across
        # both columns -- the only row on the screen whose second key was
        # printed as part of its own description.
        kw = max(ui.vlen(k) for k, _ in KEYMAP) + 2
        out.append("    " + ui.pad(ui.paint(key, "accent", BOLD), kw) + dim(what))

    out.append("")
    out.append("  " + ui.paint("SHORTCUTS", "mauve", BOLD))
    shortcuts = [(k, " ".join(v)) for k, v in HOTKEYS.items()
                 if k not in ("home", "dash")]
    shortcuts += [("/", "find <query>"), ("?", "this screen")]
    cells = [ui.pad(ui.paint(k, "accent", BOLD) + " " + dim(v), 24)
             for k, v in shortcuts]
    per = max(1, (w - 4) // 24)
    for i in range(0, len(cells), per):
        out.append("    " + "".join(cells[i:i + per]).rstrip())

    out.append("")
    out.append("  " + dim("<command> -h  for its full options   ·   clear   ·   exit"))
    return out


def _show_help(p: argparse.ArgumentParser) -> None:
    """`help` used to `print()` straight into the transcript, which is the one
    thing this codebase's own convention says a list-shaped command should
    not do: it left the whole listing sitting in permanent scrollback, ahead
    of whatever view you opened next, with no way to put it down. A `TabsView`
    behaves like every other screen in the shell -- `esc`/`clear`/opening
    another view takes it down cleanly -- and prints flat exactly the same
    when there is no shell to attach it to."""
    _show_tabs("HELP", [("Commands", lambda w: _help_lines(p, w))])


# ---------------------------------------------------------------- completion

ROUNDS = list(mock.ROUNDS.keys())
PERSONAS = ["standard", "skeptical_md", "exacting_vp"]
KINDS = ["technical", "market_awareness", "behavioural"]
TARGETS = ["kept", "needs_review", "active", "all"]
# Every settable key, from the table itself. This was a hand-written subset
# and it had gone stale exactly where it hurt: `llm_provider`,
# `anthropic_api_key` and `openai_api_key` were all absent, so the three
# settings that switch provider were the three the completion menu would not
# tell you existed.
SETTINGS_KEYS = [e["key"] for e in SETTINGS]
PATH_COMMANDS = ("ingest", "ingest-pdf", "ingest-epub", "ingest-pack", "reground", "export")


def _complete_paths(text: str) -> list[str]:
    p = Path(text).expanduser()
    parent = p.parent if not text.endswith("/") else p
    stem = p.name if not text.endswith("/") else ""
    try:
        if not parent.exists():
            return []
        return [str(e) + ("/" if e.is_dir() else "")
                for e in sorted(parent.iterdir()) if e.name.startswith(stem)]
    except OSError:
        return []


def build_completer(parser: argparse.ArgumentParser):
    """One completion brain, driven by the parser.

    Returns `(buffer, cursor) -> (candidates, start)` where a candidate is a
    `tui.Completion` carrying what to insert and what it does -- the shell
    draws both, and there is nowhere else the descriptions could come from
    that would stay in step with the parser.
    """
    commands = sorted(_subcommand_names(parser))
    helps = _command_help(parser)
    options: dict[str, list[tui.Completion]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subp in action.choices.items():
                opts: list[tui.Completion] = []
                for a in subp._actions:
                    for flag in a.option_strings:
                        opts.append(tui.Completion(flag, (a.help or "").strip()))
                options[name] = opts

    def pick(pool: list[tui.Completion], cur: str) -> list[tui.Completion]:
        """Prefix matches, an exact match promoted to the top.

        Without this, typing the single-letter hotkey `g` matched both the
        `g` shortcut itself and the real `gate` command -- `gate` came first
        because `commands` is built before `HOTKEYS` below, so Enter on a
        bare `g` autocompleted to `gate` instead of firing the shortcut the
        hint row promised. Same bug for `r` (-> `review`) and `s` (->
        `sessions`/`settings`/`show`). A stable sort keeps everything else in
        its original order and only moves the thing you typed exactly.
        """
        matches = [c for c in pool if c.value.startswith(cur)]
        matches.sort(key=lambda c: c.value != cur)
        return matches

    def plain(values: list[str], hint: str = "") -> list[tui.Completion]:
        return [tui.Completion(v, hint) for v in values]

    def candidates(buf: str, pos: int) -> tuple[list[tui.Completion], int]:
        head_ = buf[:pos]
        # The word under the cursor, and where it starts, is all that gets
        # replaced -- anything after the cursor is left alone.
        i = len(head_)
        while i > 0 and not head_[i - 1].isspace():
            i -= 1
        cur, start = head_[i:], i
        before = head_[:i].strip()
        tokens = before.split()

        if not tokens:
            if cur.startswith("/"):
                return [], start
            pool = [tui.Completion(c, helps.get(c, "")) for c in commands]
            pool += [tui.Completion(k, " ".join(v)) for k, v in HOTKEYS.items()
                     if k not in ("home", "dash")]
            pool += [tui.Completion("help", "every command and key"),
                     tui.Completion("clear", "empty the transcript"),
                     tui.Completion("exit", "leave the shell")]
            return pick(pool, cur), start

        cmd = HOTKEYS.get(tokens[0], [tokens[0]])[0]
        prev = tokens[-1]

        by_flag = {
            ("-t", "--topic"): (TOPICS, "topic"),
            ("-k", "--kind"): (KINDS, "kind"),
            ("--persona",): (PERSONAS, "interviewer persona"),
            ("--target",): (TARGETS, "which questions"),
            ("-o", "--origin"): (["self_authored", "interviewer_asked", "published"],
                                 "where it came from"),
            ("--status",): (["needs_review", "active"], "status"),
        }
        for flags, (pool, hint) in by_flag.items():
            if prev in flags:
                return pick(plain(pool, hint), cur), start

        if cur.startswith("-"):
            return pick(options.get(cmd, []), cur), start

        positional = len(tokens) == 1
        if positional and cmd == "list":
            return pick(plain(TOPICS, "topic"), cur), start
        if positional and cmd == "mock":
            return pick(plain(ROUNDS, "round"), cur), start
        if positional and cmd == "settings":
            return pick(plain(SETTINGS_KEYS, "setting"), cur), start
        if cmd in PATH_COMMANDS:
            return plain(_complete_paths(cur)), start
        if not cur:
            return sorted(options.get(cmd, [])), start
        return [], start

    return candidates


HIST_PATH = Path.home() / ".superday_history"

# A key is typed once and then lives forever in two places nobody thinks
# about: the transcript on screen, and the history file this writes on the way
# out. The tool goes to some trouble to keep a key out of a URL and to create
# .env.local 0600; typing `settings gemini_api_key sk-...` and then leaving it
# in a plain-text file in $HOME undoes both. So the *line* is redacted at the
# one point it becomes durable -- what gets echoed, what ↑ brings back, and
# what is written to disk are all the redacted form, and only the parser ever
# sees the key itself.
_SECRET_KEYS = None


def _secret_setting(name: str) -> bool:
    """Whether `settings <name> ...` is about to carry a secret.

    Prefix-matched, because `settings gem <key>` is a legal way to write it and
    a redactor that only knows the full spelling is a redactor with a hole in
    it exactly where a hurried user is.
    """
    global _SECRET_KEYS
    if _SECRET_KEYS is None:
        _SECRET_KEYS = [e["key"] for e in SETTINGS if e["kind"] == "secret"]
    name = name.strip().lower().replace("-", "_")
    if not name:
        return False
    matches = [k for k in _SECRET_KEYS if k.startswith(name)]
    # An ambiguous prefix sets nothing, so there is nothing to hide -- but a
    # prefix that matches only secrets is one, whichever it turns out to be.
    return bool(matches) and len(
        [k for k in (e["key"] for e in SETTINGS) if k.startswith(name)]) == len(matches)


def redact(line: str) -> str:
    """One typed line, safe to echo, remember and write down."""
    parts = line.split()
    if len(parts) >= 3 and parts[0] in ("settings", "set") and _secret_setting(parts[1]):
        return " ".join(parts[:2]) + " ‹key hidden›"
    return line


def _load_history() -> list[str]:
    """Read the history file, including anything readline wrote into it.

    readline escapes an entry it cannot store literally -- a leading space
    becomes `\\040` -- and starts timestamp lines with `#`. Loading those raw
    put `/\\040valuation` in front of you when you pressed up.
    """
    try:
        raw = HIST_PATH.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        if not line.strip() or line.startswith("#"):
            continue
        if "\\" in line:
            line = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), line)
            line = line.replace("\\\\", "\\")
        out.append(line)
    return out


def _save_history(lines: list[str] | None = None) -> None:
    """Write the history, with any key that was typed into it taken back out.

    The readline path builds its own list rather than calling
    `write_history_file`, which would write readline's copy of the line and
    never see the redaction.
    """
    try:
        if lines is None:
            import readline
            lines = [readline.get_history_item(i + 1) or ""
                     for i in range(readline.get_current_history_length())]
        safe = [redact(l) for l in lines[-1000:] if l.strip()]
        # 0600 for the same reason .env.local is: this file is in $HOME and it
        # holds everything you have ever typed at this tool.
        if not HIST_PATH.exists():
            os.close(os.open(HIST_PATH, os.O_CREAT | os.O_WRONLY, 0o600))
        HIST_PATH.write_text("\n".join(safe) + "\n")
        os.chmod(HIST_PATH, 0o600)
    except (OSError, ImportError):
        pass


def _install_readline(parser: argparse.ArgumentParser) -> None:
    """The fallback path: same completions, wired to readline's odd protocol."""
    try:
        import readline
    except ImportError:
        return
    if HIST_PATH.exists():
        try:
            readline.read_history_file(str(HIST_PATH))
        except OSError:
            pass
    candidates = build_completer(parser)

    def completer(text: str, state: int):
        buf = readline.get_line_buffer()
        cands, _ = candidates(buf, readline.get_endidx())
        return cands[state].value if state < len(cands) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    for binding in ("bind ^I rl_complete", "tab: complete", "^I: complete"):
        try:
            readline.parse_and_bind(binding)
        except Exception:
            pass


# ---------------------------------------------------------------- dispatch

def _expand(line: str) -> list[str] | None:
    """One typed line to argv. Shared by both shells so they cannot disagree."""
    head_word, _, rest = line.partition(" ")
    if line.startswith("/"):
        return ["find"] + shlex.split(line[1:])
    if head_word in HOTKEYS:
        # `p 2026-09-15` means plan for that date, not plan +14d then a stray
        # argument.
        if head_word == "p" and rest.strip():
            return ["plan"] + shlex.split(rest)
        return HOTKEYS[head_word] + shlex.split(rest)
    return shlex.split(line)


# ---------------------------------------------------------------- the shell

def _shell_status(conn: sqlite3.Connection):
    """The right-hand end of the hint row: what the bank looks like, always.

    Cached on a short timer because the shell repaints on every keystroke and
    the counts are three aggregate queries. They only move when a command
    writes, so a second of staleness costs nothing and a query per keypress
    is felt on a large bank.
    """
    cache: list = [0.0, ""]

    def render() -> str:
        now_ = time.monotonic()
        if now_ - cache[0] < 1.0:
            return cache[1]
        try:
            c = analytics.counts(conn)
        except sqlite3.Error:
            return cache[1]
        due = (warn(f"{c['due_now']} due") if c["due_now"] else ok("caught up"))
        cache[0], cache[1] = now_, due + dim("  ·  ") + dim(f"{c['active']} active")
        return cache[1]
    return render


def _shell_header(conn: sqlite3.Connection):
    """The banner, pinned above the transcript instead of scrolled past.

    Cached on the same short timer as `_shell_status`: the shell repaints on
    every keystroke, and the banner is three aggregate queries plus a lookup
    for a resumable sitting.
    """
    cache: list = [0.0, [""]]

    def render(shell) -> list[str]:
        now_ = time.monotonic()
        if now_ - cache[0] < 1.0:
            return cache[1]
        try:
            lines = _banner(conn).split("\n")
        except sqlite3.Error:
            return cache[1]
        cache[0], cache[1] = now_, lines
        return cache[1]
    return render


def _shell_hints(shell) -> str:
    return " ".join([
        ui.paint("/", "accent") + dim(" search"),
        dim("·"), ui.paint("d", "accent") + dim(" drill"),
        dim("·"), ui.paint("g", "accent") + dim(" dashboard"),
        dim("·"), ui.paint("?", "accent") + dim(" help"),
    ])


def _run_line(conn: sqlite3.Connection, p: argparse.ArgumentParser,
              shell, line: str) -> None:
    """Everything one submitted line can mean, in one place."""
    if line in ("exit", "quit", "q"):
        shell.stop()
        return
    if line in ("clear", "cls"):
        if shell is None:
            # No shell means no frame to wipe -- the old REPL is the terminal's
            # own scrollback, so clearing is the terminal's job. It used to
            # call straight into `shell.clear()` and report `NoneType has no
            # attribute clear` as if the command did not exist.
            print("\033[2J\033[H", end="")
            print(_banner(conn))
            return
        shell.clear()
        return
    if line in ("h", "?", "help"):
        _show_help(p)
        return
    try:
        tokens = _expand(line)
    except ValueError as e:
        print(bad(f"  can't parse that: {e}"))
        return
    if not tokens:
        return
    try:
        args = p.parse_args(tokens)
    except SystemExit:
        return
    args.fn(conn, args)
    _refresh_export(conn, tokens[0], args)


def repl(p: argparse.ArgumentParser) -> int:
    conn = connect()
    migrate(conn)
    if tui.available():
        return _shell(conn, p)
    return _readline_repl(conn, p)


def _shell(conn: sqlite3.Connection, p: argparse.ArgumentParser) -> int:
    history = _load_history()
    shell = tui.Shell(
        on_submit=lambda sh, line: _run_line(conn, p, sh, line.strip()),
        completer=build_completer(p),
        history=history,
        status=_shell_status(conn),
        hints=_shell_hints,
        redact=redact,
        # Pinned above the transcript rather than revealed into it, so it
        # stays on screen through `clear`, ^L and everything you scroll past.
        header=_shell_header(conn),
    )
    try:
        shell.run()
    finally:
        _save_history(shell.editor.history)
        conn.close()
    return 0


def _readline_repl(conn: sqlite3.Connection, p: argparse.ArgumentParser) -> int:
    """No tty, no alternate screen: the old line-at-a-time loop, still here.

    A pipe, a dumb terminal or SUPERDAY_NO_TUI all land here, and every
    command behaves identically -- the shell is a front end, not a fork in
    the logic.
    """
    _install_readline(p)
    print(_banner(conn))
    prompt = ui.paint("superday", "accent", BOLD) + dim(" › ")
    try:
        while True:
            try:
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("exit", "quit", "q"):
                break
            try:
                _run_line(conn, p, None, line)
            except KeyboardInterrupt:
                print()
            except Exception as e:
                print(bad(f"error: {e}"))
    finally:
        _save_history()
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return repl(p)
    args = p.parse_args(argv)
    # Redirected to a file, stdout is block-buffered, so a long enrich or audit
    # writes nothing to the log until it finishes and you cannot tell a slow
    # run from a hung one. These jobs are always redirected.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    conn = connect()
    migrate(conn)
    # One-shot mode has no shell to catch things, and a traceback is not an
    # error message. Anything we can name gets named; anything we cannot is
    # still a bug and still prints its traceback under IB_DEBUG.
    try:
        args.fn(conn, args)
        _refresh_export(conn, argv[0], args)
    except KeyboardInterrupt:
        print()
        print(dim("  stopped"))
        return 130
    except BrokenPipeError:
        return 0                       # piped into head, and head went away
    except llm.LLMError as e:
        _llm_problem(e.message, e.hint)
        return 1
    except Exception as e:
        if os.environ.get("IB_DEBUG"):
            raise
        print(bad(f"  {argv[0]} failed") + dim(" - " + _why(e)))
        print(dim("  IB_DEBUG=1 for the traceback"))
        return 1
    return 0


def cli() -> None:
    """The console-script entry point. `main` returns a status; a script has
    to exit with one."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
