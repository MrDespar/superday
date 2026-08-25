"""EPUB ingest.

An EPUB is a zip of XHTML documents plus a manifest saying which order they go
in. That is better raw material than a PDF: there are no page-break artifacts,
no repeated running headers to strip, and the chapter structure is declared
rather than inferred, so a chunk locator can say "Chapter 7" instead of
"p143-148".

Parsing is stdlib only -- zipfile and a regex strip -- for the same reason
`llm.py` is: an EPUB is a zip of XML, and a dependency to read one buys nothing
this file cannot do in eighty lines.
"""
from __future__ import annotations

import hashlib
import html
import re
import zipfile
from pathlib import Path

from . import pipeline

CONTAINER = "META-INF/container.xml"

# Chrome that carries no questions and only burns extraction budget.
SKIP_TITLES = re.compile(
    r"\b(copyright|colophon|about the author|acknowledg|index|dedication|"
    r"title page|cover|table of contents|contents|front ?matter)\b", re.I)

_TAG = re.compile(r"<[^>]+>")
_SCRIPTY = re.compile(r"<(script|style|head)\b.*?</\1>", re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|blockquote|section)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_html(markup: str) -> str:
    """XHTML to text, keeping the block structure that paragraph breaks encode."""
    text = _SCRIPTY.sub(" ", markup or "")
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub(" ", text)
    return pipeline.tidy(html.unescape(text))


def _opf_path(zf: zipfile.ZipFile) -> str | None:
    """Where the manifest lives, per the container descriptor."""
    try:
        container = zf.read(CONTAINER).decode("utf8", "ignore")
    except KeyError:
        return None
    m = re.search(r'full-path="([^"]+)"', container)
    return m.group(1) if m else None


def spine(path: Path) -> list[tuple[str, str]]:
    """(document name, XHTML) in reading order.

    Falls back to zip order when the manifest is unreadable. A malformed EPUB
    is still worth ingesting badly-ordered rather than not at all -- chunk
    order affects locators, not correctness.
    """
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist()
                 if n.lower().endswith((".xhtml", ".html", ".htm"))]
        opf = _opf_path(zf)
        ordered = names
        if opf:
            try:
                manifest = zf.read(opf).decode("utf8", "ignore")
                base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
                ids = dict(re.findall(r'<item\b[^>]*id="([^"]+)"[^>]*href="([^"]+)"',
                                      manifest))
                ids.update({v: k for k, v in
                            re.findall(r'<item\b[^>]*href="([^"]+)"[^>]*id="([^"]+)"',
                                       manifest)})
                order = re.findall(r'<itemref\b[^>]*idref="([^"]+)"', manifest)
                resolved = []
                for ref in order:
                    href = ids.get(ref)
                    if not href:
                        continue
                    full = (base + href).replace("//", "/")
                    if full in names:
                        resolved.append(full)
                    elif href in names:
                        resolved.append(href)
                if resolved:
                    ordered = resolved
            except (KeyError, zipfile.BadZipFile):
                pass
        return [(n, zf.read(n).decode("utf8", "ignore")) for n in ordered]


def document_title(markup: str, fallback: str) -> str:
    for pattern in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>",
                    r"<h2[^>]*>(.*?)</h2>"):
        m = re.search(pattern, markup, re.I | re.S)
        if m:
            text = pipeline.tidy(html.unescape(_TAG.sub(" ", m.group(1))))
            if text:
                return text[:80]
    return fallback


def book_title(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        opf = _opf_path(zf)
        if opf:
            try:
                manifest = zf.read(opf).decode("utf8", "ignore")
                m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", manifest, re.I | re.S)
                if m:
                    return pipeline.tidy(html.unescape(_TAG.sub(" ", m.group(1))))[:120]
            except KeyError:
                pass
    return path.stem


def chapters(path: Path, min_chars: int = 600) -> list[tuple[str, str]]:
    """(locator, text) per chapter, front and back matter dropped."""
    out: list[tuple[str, str]] = []
    for i, (name, markup) in enumerate(spine(path), 1):
        title = document_title(markup, f"doc {i}")
        if SKIP_TITLES.search(title):
            continue
        text = strip_html(markup)
        if len(text) < min_chars:
            continue
        out.append((title[:60], text))
    return out


def chunks(path: Path, chars: int = 9000, min_chars: int = 600) -> list[tuple[str, str]]:
    """Extraction-sized windows, labelled by the chapter they came from."""
    out: list[tuple[str, str]] = []
    for title, text in chapters(path, min_chars=min_chars):
        parts = pipeline.window(text, chars=chars, label="§")
        for locator, part in parts:
            label = title if len(parts) == 1 else f"{title} {locator}"
            out.append((label, part))
    return out
