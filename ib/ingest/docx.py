"""Parse the Questions & Answers .docx files into candidate Q&A pairs.

These files are already question-then-answer prose, so there is no generation
here and nothing to hallucinate. Paragraph ending in '?' starts a question,
everything until the next question paragraph is its answer.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

# Enough to spot the inline German translations without a language library.
_DE_MARKERS = {
    "der", "die", "das", "und", "nicht", "eine", "einen", "einem", "wird",
    "werden", "sich", "auf", "von", "dass", "ist", "sind", "kann", "man",
    "unternehmen", "wenn", "über", "für", "bei", "aus", "durch", "es",
}


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def paragraphs(path: Path) -> list[str]:
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'")
    )
    out = []
    for line in text.split("\n"):
        line = re.sub(r"[ \s]+", " ", line).strip()
        if line:
            out.append(line)
    return out


def is_german(par: str) -> bool:
    words = re.findall(r"[a-zäöüß]+", par.lower())
    if len(words) < 6:
        return False
    hits = sum(1 for w in words if w in _DE_MARKERS)
    return hits / len(words) > 0.13


def is_question(par: str) -> bool:
    return par.rstrip().endswith("?") and len(par) < 400


def parse(path: Path) -> tuple[str, list[dict]]:
    """Return (doc_title, [{question, answer, answer_de}])."""
    pars = paragraphs(path)
    title = pars[0] if pars else path.stem

    pairs: list[dict] = []
    current: dict | None = None
    for par in pars[1:]:
        if is_question(par):
            # A German question paragraph is a translation of the question we
            # are already inside, not a new one. Without this it enters the
            # bank as a separate question and gets drilled twice.
            if current is not None and is_german(par):
                current["question_de"] = par
                continue
            if current and (current["answer"] or current["answer_de"]):
                pairs.append(current)
            current = {"question": par, "question_de": "", "answer": [], "answer_de": []}
        elif current is not None:
            (current["answer_de"] if is_german(par) else current["answer"]).append(par)

    if current and (current["answer"] or current["answer_de"]):
        pairs.append(current)

    for p in pairs:
        p["answer"] = "\n\n".join(p["answer"]).strip()
        p["answer_de"] = "\n\n".join(p["answer_de"]).strip()
    return title, [p for p in pairs if p["answer"]]
