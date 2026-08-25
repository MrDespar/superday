"""PDF ingest.

Every book in the corpus has a real text layer, so there is no OCR step. The
work is stripping the per-page boilerplate, chunking on something meaningful,
and having the model lift questions that are actually in the text rather than
inventing new ones.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from .. import llm

EXTRACT_PROMPT = """The following pages are from an investment banking
interview guide.

Extract the interview questions it contains, with their answers.

Rules:
- Only extract questions the text actually poses. Do not invent questions the
  author did not ask.
- If the text is prose with no explicit questions, return an empty list rather
  than manufacturing some.
- Rewrite each answer as a tight, self-contained model answer in your own
  words, faithful to the text. Two to six sentences.
- source_quote: copy one span of 15 to 60 words from the TEXT above,
  character for character, that the answer is based on. Do not paraphrase it,
  do not repair its typos, do not join text from two places. This is checked
  against the source, and an item whose quote is not found there is discarded.
- Skip anything about the guide itself, pricing, marketing or navigation.
- Behavioural questions ("walk me through your resume", "tell me about a time
  you led a team") are real interview questions: extract them. Skip only prose
  *about* interviewing, such as recruiting-timeline advice.

TEXT:
{text}
"""


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_texts(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    return [(p.extract_text() or "") for p in reader.pages]


def boilerplate_lines(pages: list[str], threshold: float = 0.4) -> set[str]:
    """Lines repeating on a large share of pages are headers and footers.
    The BIWS guides carry three such lines on every page."""
    counts: Counter[str] = Counter()
    for text in pages:
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            counts[line] += 1
    n = max(len(pages), 1)
    return {
        line for line, c in counts.items()
        if c / n >= threshold and len(line) < 120
    }


def clean_pages(pages: list[str]) -> list[str]:
    junk = boilerplate_lines(pages)
    out = []
    for text in pages:
        kept = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s in junk:
                continue
            if re.fullmatch(r"\d+\s*(of\s*\d+)?", s):        # bare page numbers
                continue
            if re.fullmatch(r"[.\s…]+\d*", s):           # dotted TOC leaders
                continue
            kept.append(s)
        out.append("\n".join(kept))
    return out


def _split(text: str, limit: int, overlap: int = 800) -> list[str]:
    """Cut over-budget text into pieces, on a line break where there is one.

    The overlap is the same idea as the page overlap: a question and its answer
    either side of a cut would otherwise reach the model as an orphan question
    and an orphan answer, and grounding throws both away.
    """
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + limit // 2, end)
            if cut > 0:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def chunks(pages: list[str], pages_per_chunk: int = 6, overlap: int = 1, min_chars: int = 400):
    """Yield (locator, text). Page-window chunking with 1-page overlap keeps questions
    and answers together even when crossing a page boundary.

    No chunk leaves here over PROMPT_BUDGET. The prompt used to be cut to fit
    at the point of sending, and because the page overlap is a single page --
    far smaller than the span being dropped -- the tail appeared in no other
    chunk either. `--window 20` silently threw away about 15k characters of
    every chunk, after paying for the call in full.
    """
    step = max(1, pages_per_chunk - overlap)
    for start in range(0, len(pages), step):
        window = pages[start:start + pages_per_chunk]
        text = "\n".join(window).strip()
        if len(text) < min_chars:
            continue
        locator = f"p{start + 1}-{start + len(window)}"
        if len(text) <= PROMPT_BUDGET:
            yield locator, text
            continue
        pieces = _split(text, PROMPT_BUDGET)
        for i, piece in enumerate(pieces, 1):
            yield f"{locator}.{i}", piece


PROMPT_BUDGET = 24000


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def grounded(quote: str, source: str) -> bool:
    """Is the model's quote actually in the page text it was given?

    The answers are rewrites, so nothing else in the pipeline can tell a
    faithful summary from an invented one. A quote that does not appear in the
    source is the one mechanical signal that the item was made up. Matching is
    on squashed alphanumerics because PDF extraction mangles whitespace,
    ligatures and quote marks.
    """
    q = _squash(quote)
    if len(q) < 40:                     # too short to be evidence of anything
        return False
    if q in _squash(source):
        return True
    # Tolerate one mangled span in the middle of an otherwise verbatim quote.
    words = q.split()
    head, tail = " ".join(words[:8]), " ".join(words[-8:])
    src = _squash(source)
    return head in src and tail in src


def extract_raw(text: str) -> list[dict]:
    """Ask the model what questions this text contains. Unchecked, by design.

    Grounding lives in `pipeline.run`, not here: an extractor deciding whether
    its own output is grounded is the fox counting the hens. This returns what
    the model said, normalised, and nothing more.
    """
    # Loud rather than silent. Truncating here would pay for the whole call
    # and read only part of the chunk; every in-tree caller now chunks under
    # the budget, so reaching this means a caller is wrong, not a book.
    if len(text) > PROMPT_BUDGET:
        raise llm.LLMError(
            f"chunk is {len(text)} characters, over the {PROMPT_BUDGET} prompt budget",
            retryable=False,
            hint="chunk it smaller before extracting: pdf.chunks / pipeline.window")
    out = llm.generate(
        EXTRACT_PROMPT.format(text=text),
        schema=llm.EXTRACT_SCHEMA,
        model=llm.model_enrich(),
        temperature=0.1,
        thinking=llm.THINKING_BULK,
        caller="extract",
    )
    items = []
    for q in out.get("questions", []):
        items.append({
            "question": (q.get("question") or "").strip(),
            "answer": (q.get("answer") or "").strip(),
            "source_quote": (q.get("source_quote") or "").strip(),
            "topic": q.get("topic", "general"),
            "difficulty": max(1, min(5, int(q.get("difficulty", 3)))),
        })
    return items


def extract(text: str) -> tuple[list[dict], int]:
    """(kept items, ungrounded count). Kept for callers that check inline."""
    items, dropped = [], 0
    for it in extract_raw(text):
        if len(it["question"]) < 12 or len(it["answer"]) < 20:
            continue
        if not grounded(it["source_quote"], text):
            dropped += 1
            continue
        items.append(it)
    return items, dropped
