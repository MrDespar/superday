"""The part of ingestion that is the same for every source format.

A PDF, an EPUB, a forum thread and a video transcript differ only in how you
get text out of them. Everything after that -- windowing the text, asking the
model for the questions it actually contains, checking the quote is really in
the source, and putting the result through the admission gate -- is identical,
and was on its way to being copied once per format.

So each format supplies `(locator, text)` chunks and nothing else. This module
owns the loop, the grounding rule, the failure budget and the progress line, so
a fix to any of those lands in every format at once.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .. import llm, ui
from ..admission import admit, kind_for_topic

# Repeated failures are nearly always quota, not bad input. Grinding through
# 200 chunks to collect 200 identical errors wastes minutes and quota alike.
MAX_CONSECUTIVE_FAILURES = 3


COMPLETE = "__complete__"


@dataclass
class Outcome:
    new: int = 0
    duplicate: int = 0
    variant: int = 0
    rejected: int = 0
    ungrounded: int = 0
    chunks: int = 0
    failures: int = 0
    skipped: int = 0
    aborted: bool = False
    tags: dict[str, int] = field(default_factory=dict)

    @property
    def admitted(self) -> int:
        return self.new + self.duplicate + self.variant

    def line(self) -> str:
        return (f"{self.new} new  {self.duplicate} dup  {self.variant} variant  "
                f"{self.ungrounded} ungrounded")


def completed(conn: sqlite3.Connection, source_id: int) -> set[str]:
    """Locators already read for this source, so a re-run does not re-pay."""
    return {r[0] for r in conn.execute(
        "SELECT locator FROM ingest_progress WHERE source_id = ?", (source_id,))}


def _mark(conn: sqlite3.Connection, source_id: int, locator: str) -> None:
    from ..db import now
    conn.execute(
        "INSERT OR REPLACE INTO ingest_progress (source_id, locator, done_at) "
        "VALUES (?, ?, ?)", (source_id, locator, now()))


def is_complete(conn: sqlite3.Connection, source_id: int) -> bool:
    """Did a run of this source finish every chunk?

    Recorded as a sentinel locator so the common "already fully ingested" case
    can be answered without re-parsing the file to find out how many chunks it
    should have had.
    """
    return bool(conn.execute(
        "SELECT 1 FROM ingest_progress WHERE source_id = ? AND locator = ?",
        (source_id, COMPLETE)).fetchone())


def forget(conn: sqlite3.Connection, source_id: int) -> None:
    """Throw the progress away, so --force really does re-read everything."""
    conn.execute("DELETE FROM ingest_progress WHERE source_id = ?", (source_id,))


def window(text: str, chars: int = 9000, overlap: int = 800,
           label: str = "part") -> list[tuple[str, str]]:
    """Split long text into overlapping windows, cutting on paragraph breaks.

    The overlap is what keeps a question and its answer together when they fall
    either side of a cut. Cutting mid-sentence produces a chunk whose questions
    have no answers and a chunk whose answers have no questions, and the
    grounding check then throws both away.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chars:
        return [(f"{label}1", text)]

    out: list[tuple[str, str]] = []
    start = 0
    n = 1
    while start < len(text):
        end = min(start + chars, len(text))
        if end < len(text):
            para = text.rfind("\n\n", start + chars // 2, end)
            if para > 0:
                end = para
        out.append((f"{label}{n}", text[start:end].strip()))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        n += 1
    return out


def run(
    conn: sqlite3.Connection,
    source_id: int,
    chunks: Iterable[tuple[str, str]],
    *,
    extractor: Callable[[str], list[dict]] | None = None,
    status: str = "needs_review",
    origin: str = "published",
    on_progress: Callable[[int, Outcome], None] | None = None,
    autotag: bool = True,
    resume: bool = True,
    partial: bool = False,
) -> Outcome:
    """Extract, ground and admit every chunk. Returns what happened.

    Nothing here decides a question is *good* -- that is `review` and `audit`'s
    job. This decides only that a question was really in the source, which is
    the one thing the source itself can settle.

    The grounding check runs here rather than inside each extractor, and that
    placement is the point: an extractor is the untrusted part, so it cannot be
    the thing that decides whether its own output is trustworthy. Every format,
    and every future extractor, gets checked the same way.
    """
    from .pdf import extract_raw, grounded
    from .. import tagging

    extract = extractor or extract_raw
    out = Outcome()
    consecutive = 0

    chunk_list = list(chunks)
    # What a previous run already read. An abort halfway through a 400-page
    # guide used to mean the whole book had to be re-sent to get the second
    # half, because the sources row already existed and the re-run skipped the
    # file entirely.
    done = completed(conn, source_id) if resume else set()
    for n, (locator, text) in enumerate(chunk_list, 1):
        if locator in done:
            out.skipped += 1
            continue
        out.chunks += 1
        try:
            raw = extract(text)
        except llm.LLMError as e:
            consecutive += 1
            out.failures += 1
            print(ui.warn(f"\n  chunk {locator} failed "
                          f"({consecutive}/{MAX_CONSECUTIVE_FAILURES}): {e}"))
            if not e.retryable or consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(ui.bad("  " + llm.give_up_note(e)))
                out.aborted = True
                break
            continue
        consecutive = 0
        items = []
        for it in raw:
            question = (it.get("question") or "").strip()
            answer = (it.get("answer") or "").strip()
            if len(question) < 12 or len(answer) < 20:
                continue
            if not grounded(it.get("source_quote") or "", text):
                out.ungrounded += 1
                continue
            items.append(it)

        for it in items:
            v = admit(
                conn, source_id=source_id, question_text=it["question"],
                answer_text=it.get("answer"), verbatim=it.get("source_quote"),
                locator=locator, status=status, origin=origin,
            )
            setattr(out, v.kind, getattr(out, v.kind) + 1)
            if v.kind != "new":
                continue
            topic = it.get("topic") or "general"
            conn.execute(
                "UPDATE questions SET topic = ?, difficulty = ?, kind = ? WHERE id = ?",
                (topic, it.get("difficulty", 3), kind_for_topic(topic), v.matched_id),
            )
            if autotag:
                names = tagging.suggest(it["question"], it.get("answer"))
                names += [t for t in it.get("tags", []) if t]
                for name in tagging.attach(conn, v.matched_id, names):
                    out.tags[name] = out.tags.get(name, 0) + 1
        # Recorded whatever it yielded, including nothing: a chunk with no
        # questions in it has still been read, and re-reading it costs the
        # same as reading it did.
        _mark(conn, source_id, locator)
        conn.commit()
        if on_progress:
            on_progress(n, out)
        else:
            print(f"\r  {n}/{len(chunk_list)} chunks   {out.line()}", end="", flush=True)

    # `partial` means the caller deliberately handed over only some of the
    # chunks (--max-chunks), so finishing them is not finishing the source.
    if not out.aborted and not partial:
        _mark(conn, source_id, COMPLETE)
        conn.commit()
    if not on_progress and chunk_list:
        print()
    return out


def report(out: Outcome, label: str = "") -> None:
    """The honest summary: what went in, and what was thrown away and why."""
    if label:
        print(ui.head(f"  {label}"))
    print(f"  {ui.ok(str(out.new))} new   {out.duplicate} duplicate   "
          f"{out.variant} variant   {ui.dim(str(out.chunks) + ' chunks')}")
    if out.skipped:
        print(ui.dim(f"  {out.skipped} chunks already read on an earlier run, "
                     "not sent again"))
    if out.ungrounded:
        total = out.admitted + out.ungrounded
        print(ui.warn(f"  {out.ungrounded} discarded: the quoted text was not in the "
                      f"source ({out.ungrounded / max(total, 1):.0%} of extractions)"))
    if out.tags:
        top = sorted(out.tags.items(), key=lambda kv: -kv[1])[:6]
        print(ui.dim("  tagged: " + "  ".join(f"#{k} {v}" for k, v in top)))
    if out.aborted:
        print(ui.bad("  stopped early after repeated extraction failures"))


# ---------------------------------------------------------------- text cleanup

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


def tidy(text: str) -> str:
    """Normalise whitespace without destroying paragraph structure.

    Paragraph breaks are the only cutting points `window` has, so collapsing
    them all would leave it slicing mid-sentence.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()
