"""Ingest interview questions from the web.

Forum threads are the only source in the corpus that carries what firms are
*currently* asking, and questions marked as asked in a real interview outrank
textbook questions everywhere in this tool. That is what this is for.

Two paths, and the difference matters:

  - **A public API**, where one exists. Reddit serves any thread as JSON by
    appending `.json`; that is a documented, unauthenticated endpoint, and it
    gives structured posts and comments rather than a guess at which div held
    the text.
  - **Readable-text extraction**, for an ordinary page. Strip the chrome, keep
    the prose, chunk it, and let the same grounded extractor decide whether
    there are questions in it.

Sites that block automated access -- Glassdoor is the obvious one -- are not
worked around here. When a fetch is refused, that is reported as a refusal, and
the fallback offered is to save the page from a browser and ingest the file.
Defeating a site's access controls is not a feature this tool should have.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi

from . import pipeline

UA = {"User-Agent": "superday/0.1 (personal interview prep; contact via repo owner)"}
_SSL = ssl.create_default_context(cafile=certifi.where())

_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(
    r"<(script|style|nav|footer|header|aside|form|noscript|svg|iframe)\b.*?</\1>",
    re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|blockquote|section|article)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)

# Lines that are navigation, not content. Forum pages are mostly these.
_CHROME = re.compile(
    r"^(log ?in|sign ?up|register|subscribe|share|reply|quote|report|upvote|"
    r"downvote|follow|bookmark|edit|delete|permalink|save|award|\d+ comments?|"
    r"posted by|cookie|privacy policy|terms of (use|service)|advertisement|"
    r"related (posts|articles)|read more|show more|load more)\b", re.I)


class Refused(Exception):
    """The site declined to serve us. Not a bug: a boundary."""


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            charset = r.headers.get_content_charset() or "utf8"
            return r.read().decode(charset, "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 451):
            raise Refused(
                f"{urllib.parse.urlparse(url).netloc} returned {e.code}. "
                "Save the page from your browser and ingest the file instead."
            ) from e
        raise


def readable(markup: str) -> str:
    """Page HTML to the prose a reader would actually see."""
    text = _DROP.sub(" ", markup or "")
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)

    kept: list[str] = []
    for line in pipeline.tidy(text).split("\n"):
        s = line.strip()
        if not s:
            kept.append("")
            continue
        if _CHROME.match(s) or len(s) < 3:
            continue
        kept.append(s)
    return pipeline.tidy("\n".join(kept))


def page_title(markup: str, fallback: str = "web page") -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", markup or "", re.I | re.S)
    if m:
        t = pipeline.tidy(html.unescape(_TAG.sub(" ", m.group(1))))
        if t:
            return t[:120]
    return fallback


# ---------------------------------------------------------------- reddit

def is_reddit(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host.endswith("reddit.com")


def reddit_json_url(url: str) -> str:
    """Any Reddit thread URL serves the same thread as JSON."""
    clean = url.split("?")[0].rstrip("/")
    return clean + ".json?limit=500&raw_json=1"


def parse_reddit(payload: str) -> tuple[str, list[str]]:
    """(thread title, [post body, comment, comment, ...]).

    Comments are the point on r/FinancialCareers: the interview questions live
    in the replies, not the original post.
    """
    data = json.loads(payload)
    if isinstance(data, dict):
        data = [data]

    title = "reddit thread"
    bodies: list[str] = []

    def walk(node) -> None:
        nonlocal title
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("kind")
        d = node.get("data")
        if not isinstance(d, dict):
            walk(node.get("data")) if node.get("data") else None
            return
        if kind == "t3":
            title = d.get("title") or title
            body = d.get("selftext") or ""
            text = "\n\n".join(x for x in (d.get("title"), body) if x)
            if text.strip():
                bodies.append(text.strip())
        elif kind == "t1":
            body = (d.get("body") or "").strip()
            # `[deleted]` and one-word "this" comments carry nothing.
            if body and body not in ("[deleted]", "[removed]") and len(body) > 40:
                bodies.append(body)
        if isinstance(d.get("children"), list):
            walk(d["children"])
        if isinstance(d.get("replies"), (dict, list)) and d.get("replies"):
            walk(d["replies"])

    walk(data)
    return title, bodies


def reddit_chunks(payload: str, chars: int = 9000) -> tuple[str, list[tuple[str, str]]]:
    title, bodies = parse_reddit(payload)
    # Comments are short; batching them into windows keeps the extractor from
    # being asked to find interview questions in a single sentence.
    joined = "\n\n---\n\n".join(bodies)
    return title, pipeline.window(joined, chars=chars, label="thread ")


# ---------------------------------------------------------------- dispatch

def source_hash(url: str, body: str) -> str:
    """Identity is the URL plus what it said. A thread that gains replies is a
    genuinely different source and should be ingestable again."""
    return hashlib.sha256((url + "\n" + body).encode("utf8", "ignore")).hexdigest()


def load(url_or_path: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(title, raw body, chunks) for a URL or a saved HTML file."""
    p = Path(url_or_path).expanduser()
    if p.exists():
        markup = p.read_text(errors="ignore")
        title = page_title(markup, p.stem)
        return title, markup, pipeline.window(readable(markup), label="part ")

    if is_reddit(url_or_path):
        payload = fetch(reddit_json_url(url_or_path))
        title, chunks = reddit_chunks(payload)
        return title, payload, chunks

    markup = fetch(url_or_path)
    return page_title(markup, url_or_path), markup, pipeline.window(
        readable(markup), label="part ")
