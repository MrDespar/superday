"""Authored question packs: the road into the bank that spends nothing.

Every other ingest path hands a document to a model and trusts it to find the
questions. That is the right shape for a 284-page PDF and the wrong shape for
two cases this bank actually has:

  1. A source that is *already* question-shaped. A desk handbook laid out as
     questions with written answers is parsed, not extracted: paying a model
     to read them back to us would be worse, not better, because the parse is
     exact and the extraction is a paraphrase.
  2. A gap with no source. Nothing in the corpus covers the UK Takeover Code
     or a locked box. Those questions have to be written, and writing them is
     not an extraction problem.

Both land here. A pack is a JSON file, human-readable and diffable, holding
questions with their answers, rubrics, topics and tags already decided. This
module does no judgement of its own -- it is a loader, and the pack file is
the artefact you review.

The landing sequence mirrors `ingest-filing`, which is the other no-LLM path:
run the admission gate, then fill in the fields the gate's lexical heuristics
guess at (topic, difficulty, rubric) with the pack's own values. Nothing here
bypasses the gate, so a pack cannot smuggle a duplicate past it, and every
candidate is logged in `candidates` with the verdict it got.

Status: a pack says what it wants and the loader promotes through
`history.set_status`, in one batch, so `undo` reverses a whole pack drop
rather than a hundred individual rows. Landing straight at `active` in `admit`
would have made the drop invisible to `undo`, which is worse than no undo:
it looks like it works.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .. import history, tagging
from ..admission import admit, kind_for_topic
from ..db import upsert_source
from ..topics import TOPICS

VALID_STATUS = {"needs_review", "active"}


class PackError(Exception):
    """The pack file is malformed. Says which item and what is wrong."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PackError(msg)


def parse(path: Path) -> dict:
    """Read and validate a pack. Raises PackError with a locating message.

    Validation is strict and happens before anything is written. A pack is
    hundreds of rows landing in one command; discovering item 300 is missing a
    topic after 299 have been committed is not a recoverable position.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackError(f"{path.name}: not valid JSON ({e})") from e

    _require(isinstance(data, dict), f"{path.name}: top level must be an object")
    title = data.get("title")
    _require(bool(title), f"{path.name}: needs a \"title\"")

    status = data.get("status", "needs_review")
    _require(status in VALID_STATUS,
             f"{path.name}: status {status!r} must be one of {sorted(VALID_STATUS)}")

    items = data.get("items")
    _require(isinstance(items, list) and items, f"{path.name}: needs a non-empty \"items\" list")

    for i, it in enumerate(items, 1):
        where = f"{path.name} item {i}"
        _require(isinstance(it, dict), f"{where}: must be an object")
        _require(bool((it.get("q") or "").strip()), f"{where}: empty \"q\"")
        _require(bool((it.get("a") or "").strip()), f"{where}: empty \"a\"")
        topic = it.get("topic")
        _require(topic in TOPICS, f"{where}: topic {topic!r} is not one of {list(TOPICS)}")
        d = it.get("difficulty")
        _require(isinstance(d, int) and 1 <= d <= 5, f"{where}: difficulty must be 1-5")
        rubric = it.get("rubric") or []
        _require(isinstance(rubric, list), f"{where}: \"rubric\" must be a list")
        # A rubric is what grading and self-rating check against. An item with
        # fewer than two points is not gradeable, and the heuristic fallback
        # that `admit` would leave in place is chopped-up answer prose.
        _require(len(rubric) >= 2, f"{where}: needs at least 2 rubric points")
        _require(all(isinstance(r, str) and r.strip() for r in rubric),
                 f"{where}: rubric points must be non-empty strings")
        tags = it.get("tags") or []
        _require(isinstance(tags, list) and all(isinstance(t, str) for t in tags),
                 f"{where}: \"tags\" must be a list of strings")

    return data


def load(
    conn: sqlite3.Connection,
    path: Path,
    *,
    dry_run: bool = False,
    status_override: str | None = None,
) -> dict:
    """Land one pack. Returns a counts dict."""
    data = parse(path)
    status = status_override or data.get("status", "needs_review")
    _require(status in VALID_STATUS, f"status {status!r} is not landable")

    items = data["items"]
    if dry_run:
        topics: dict[str, int] = {}
        for it in items:
            topics[it["topic"]] = topics.get(it["topic"], 0) + 1
        return {"dry_run": True, "title": data["title"], "items": len(items),
                "status": status, "topics": topics,
                "new": 0, "duplicate": 0, "variant": 0, "rejected": 0, "promoted": 0}

    sid, _ = upsert_source(
        conn, kind="pack", title=data["title"], path=str(path),
        # The bytes are the identity, so editing a pack and re-running it
        # creates a new source row rather than silently reusing the old one
        # under a title that no longer describes it.
        file_hash="pack:" + hashlib.sha256(path.read_bytes()).hexdigest()[:32],
    )

    counts = {"new": 0, "duplicate": 0, "variant": 0, "rejected": 0, "promoted": 0}
    batch = history.new_batch()
    landed: list[int] = []

    for it in items:
        v = admit(
            conn,
            source_id=sid,
            question_text=it["q"].strip(),
            answer_text=it["a"].strip(),
            locator=it.get("locator") or data.get("title", "")[:40],
            # The pack's own words are the provenance. There is no model
            # rewrite standing between the source and the stored answer, which
            # is the whole reason this path exists.
            verbatim=it.get("verbatim") or it["a"].strip(),
            origin=it.get("origin") or data.get("origin") or "published",
            kind=it.get("kind") or kind_for_topic(it["topic"]),
        )
        counts[v.kind] += 1
        if v.kind != "new" or v.matched_id is None:
            continue

        qid = int(v.matched_id)
        landed.append(qid)
        conn.execute(
            "UPDATE questions SET topic = ?, subtopic = ?, difficulty = ?, kind = ? "
            "WHERE id = ?",
            (it["topic"], it.get("subtopic"), it["difficulty"],
             it.get("kind") or kind_for_topic(it["topic"]), qid),
        )
        # Through history, so an edited rubric is recoverable and the drop is
        # one reversible batch rather than N unlogged writes.
        history.set_answer(
            conn, qid, it["a"].strip(),
            json.dumps(it["rubric"]),
            new_common_mistakes=json.dumps(it.get("mistakes") or []),
            action="ingest-pack", batch_id=batch,
        )
        conn.execute(
            "UPDATE answers SET answer_status = 'ok' WHERE question_id = ?", (qid,))
        if it.get("tags"):
            tagging.attach(conn, qid, it["tags"])

    if status == "active":
        for qid in landed:
            if history.set_status(conn, qid, "active",
                                  action="ingest-pack", batch_id=batch):
                counts["promoted"] += 1

    conn.commit()
    counts.update({"title": data["title"], "source_id": sid, "batch": batch,
                   "status": status, "items": len(items), "dry_run": False,
                   "note": data.get("note")})
    return counts
