"""Export and snapshot.

ib/config.py keeps the database out of iCloud on purpose, because SQLite and
cloud sync corrupt each other. That is the right call and it leaves the bank
with no backup story at all, which these two commands close.

Two formats, on purpose:
  json   readable, diffable, survives a schema change, and is what you would
         hand to another tool.
  sqlite a byte-exact copy taken through SQLite's own backup API, so it is
         consistent even if something is mid-write. This is the one to restore.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import home

# Every table worth carrying out of the database, in two groups.
#
# The permanent half is why this command exists: it cannot be rebuilt by
# re-running extraction, so anything on that side of the line that is missing
# here is data the only backup story silently drops. It used to stop at
# `answer_history`, which left five tables of your own decisions out of the
# export while the command underneath it claimed to be carrying "the half of
# the database that cannot be rebuilt": the sittings you walked away from, the
# tag map, the pairs you judged distinct, the phrasings you judged fine, and
# every question you rewrote by hand.
#
# A table that does not exist yet is skipped rather than fatal (`_exists`), so
# adding a row here is safe against a database that predates its migration.
TABLES = [
    # derived and disposable: rebuildable from the corpus, kept because a JSON
    # export nobody can read the questions out of is not a backup.
    "sources", "questions", "answers", "question_sources", "phrasings",
    "live_bindings", "candidates",
    # yours and permanent: the reason to have a backup at all.
    "audits", "reviews", "schedule", "notes", "sessions",
    "question_status_history", "answer_history", "question_text_history",
    "question_line_review", "question_pair_review", "phrasing_review",
    "tags", "question_tags",
]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def export_json(conn: sqlite3.Connection, out: Path | None = None) -> tuple[Path, dict]:
    path = out or home() / f"superday-export-{_stamp()}.json"
    data: dict = {
        "superday_export": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tables": {},
    }
    counts: dict[str, int] = {}
    for t in TABLES:
        if not _exists(conn, t):
            continue
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
        data["tables"][t] = rows
        counts[t] = len(rows)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return path, counts


def snapshot(conn: sqlite3.Connection, out: Path | None = None) -> Path:
    """A consistent .sqlite copy, taken while the database is in use."""
    path = out or home() / f"superday-backup-{_stamp()}.sqlite"
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path


def export_anki(conn: sqlite3.Connection, out: Path | None = None) -> tuple[Path, int]:
    """Export active questions to Anki-compatible TSV (Front, Back, Tags)."""
    path = out or home() / f"superday-anki-{_stamp()}.tsv"
    rows = conn.execute("""
        SELECT q.id, q.canonical_text, q.topic, q.subtopic, q.kind,
               a.answer_key, a.rubric_points
          FROM questions q
          LEFT JOIN answers a ON a.question_id = q.id
         WHERE q.status = 'active'
         ORDER BY q.topic, q.id
    """).fetchall()

    lines = []
    for r in rows:
        front = (r["canonical_text"] or "").strip().replace("\t", " ").replace("\n", "<br>")
        back_parts = []
        if r["answer_key"]:
            back_parts.append((r["answer_key"] or "").strip().replace("\t", " ").replace("\n", "<br>"))
        rubric = json.loads(r["rubric_points"] or "[]")
        if rubric:
            back_parts.append("<br><b>Key Rubric Points:</b><ul>" +
                              "".join(f"<li>{p.replace(chr(9), ' ')}</li>" for p in rubric) +
                              "</ul>")
        back = "".join(back_parts)
        tag = f"superday {r['topic'] or 'general'} {r['kind'] or 'technical'}"
        if r["subtopic"]:
            tag += f" {r['subtopic'].replace(' ', '_')}"
        lines.append(f"{front}\t{back}\t{tag}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, len(rows)


# ---------------------------------------------------------------- markdown

def _slug(topic: str | None) -> str:
    return (topic or "general").strip().lower().replace(" ", "-").replace("_", "-")


def _fence(text: str) -> str:
    """Answers contain $ and _ and stray pipes. Keep them as prose, not markup."""
    return (text or "").replace("\r", "").strip()


def _question_md(r: sqlite3.Row, tags: list[str], sources: list[sqlite3.Row],
                 phrasings: list[str], progress: dict | None) -> list[str]:
    out = [f"### #{r['id']}  {' '.join((r['canonical_text'] or '').split())}", ""]

    meta = [f"`{r['topic'] or 'general'}`"]
    if r["subtopic"]:
        meta.append(f"`{r['subtopic']}`")
    meta.append(f"difficulty {r['difficulty'] or '-'}/5")
    meta.append(r["kind"] or "technical")
    if tags:
        meta.append(" ".join("#" + t for t in tags))
    out += ["*" + "  ·  ".join(meta) + "*", ""]

    answer = _fence(r["answer_key"])
    out += [answer if answer else "*(no answer on file)*", ""]

    rubric = json.loads(r["rubric_points"] or "[]")
    if rubric:
        out.append("**A good answer hits**")
        out += [f"- {p}" for p in rubric]
        out.append("")
    traps = json.loads(r["common_mistakes"] or "[]")
    if traps:
        out.append("**Common mistakes**")
        out += [f"- {t}" for t in traps]
        out.append("")
    if phrasings:
        out.append("**Also asked as**")
        out += [f"- {p}" for p in phrasings]
        out.append("")
    if sources:
        out.append("**Sources**")
        for s in sources:
            # docx sources carry the document title as their locator, which
            # otherwise prints the same name twice on one line.
            title = (s["title"] or "").strip()
            loc = (s["locator"] or "").strip()
            # docx sources carry the document title as their locator, sometimes
            # differing only by trailing whitespace, so compare them trimmed.
            label = title if not loc or loc == title else f"{title} {loc}"
            out.append(f"- {label}")
        out.append("")
    if progress:
        out.append("**Your progress**")
        out.append(f"- {progress['reps']} reviews, mean rating "
                   f"{progress['avg']:.2f}/4" if progress["reps"] else "- never drilled")
        if progress["due"]:
            out.append(f"- next due {progress['due'][:10]}")
        for note in progress["notes"]:
            out.append(f"- note: {' '.join(note.split())}")
        out.append("")
    return out


def export_markdown(conn: sqlite3.Connection, out: Path | None = None, *,
                    with_progress: bool = False) -> dict:
    """The bank as readable Markdown, one file per topic plus an index.

    One file per topic rather than one big file, for two reasons that pull the
    same way. A change to a DCF answer should touch `dcf.md` and show up as a
    three-line diff, not as a hunk somewhere inside a megabyte. And a single
    topic is small enough to paste into another model, where the whole bank --
    about 200,000 tokens -- is not.

    Row order is by id within topic and never by anything that drifts, so a
    diff between two exports is a real content change rather than a reshuffle.

    Personal progress is opt-in. The default output is the shareable one: no
    ratings, no notes, nothing about how you are doing.
    """
    root = Path(out) if out else home() / "export"
    root.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        "SELECT q.id, q.canonical_text, q.topic, q.subtopic, q.difficulty, q.kind, "
        "a.answer_key, a.rubric_points, a.common_mistakes "
        "FROM questions q LEFT JOIN answers a ON a.question_id = q.id "
        "WHERE q.status = 'active' ORDER BY q.topic, q.id"
    ).fetchall()

    tags: dict[int, list[str]] = {}
    for t in conn.execute(
        "SELECT qt.question_id qid, tg.name FROM question_tags qt "
        "JOIN tags tg ON tg.id = qt.tag_id ORDER BY tg.kind, tg.name"
    ):
        tags.setdefault(t["qid"], []).append(t["name"])
    srcs: dict[int, list] = {}
    for s in conn.execute(
        "SELECT qs.question_id qid, so.title, qs.locator FROM question_sources qs "
        "JOIN sources so ON so.id = qs.source_id ORDER BY so.title, qs.locator"
    ):
        srcs.setdefault(s["qid"], []).append(s)
    phr: dict[int, list[str]] = {}
    for p in conn.execute("SELECT question_id qid, text FROM phrasings ORDER BY id"):
        phr.setdefault(p["qid"], []).append(" ".join((p["text"] or "").split()))

    prog: dict[int, dict] = {}
    if with_progress:
        for r in conn.execute(
            "SELECT question_id qid, COUNT(*) reps, AVG(rating) avg FROM reviews "
            "WHERE rating IS NOT NULL GROUP BY question_id"
        ):
            prog[r["qid"]] = {"reps": r["reps"], "avg": r["avg"] or 0.0,
                              "due": None, "notes": []}
        for r in conn.execute("SELECT question_id qid, due_at FROM schedule"):
            prog.setdefault(r["qid"], {"reps": 0, "avg": 0.0, "due": None, "notes": []})
            prog[r["qid"]]["due"] = r["due_at"]
        for r in conn.execute("SELECT question_id qid, body FROM notes ORDER BY id"):
            prog.setdefault(r["qid"], {"reps": 0, "avg": 0.0, "due": None, "notes": []})
            prog[r["qid"]]["notes"].append(r["body"])

    by_topic: dict[str, list] = {}
    for r in rows:
        by_topic.setdefault(_slug(r["topic"]), []).append(r)

    written, unchanged, files = 0, 0, []
    for slug, group in sorted(by_topic.items()):
        body = [f"# {slug}", "",
                f"{len(group)} active questions.", "",
                "---", ""]
        for r in group:
            body += _question_md(r, tags.get(r["id"], []), srcs.get(r["id"], []),
                                 phr.get(r["id"], []), prog.get(r["id"]))
            body.append("---")
            body.append("")
        path = root / f"{slug}.md"
        text = "\n".join(body).rstrip() + "\n"
        # Only touch the file when the content actually moved. An export that
        # rewrites every file on every run turns the git history into noise and
        # makes a real change impossible to spot.
        if path.exists() and path.read_text(encoding="utf-8") == text:
            unchanged += 1
        else:
            path.write_text(text, encoding="utf-8")
            written += 1
        files.append((slug, len(group)))

    index = ["# superday", "",
             f"{len(rows)} active questions across {len(files)} topics.", ""]
    if with_progress:
        index += ["*Includes personal review history.*", ""]
    index += ["| topic | questions |", "|---|---|"]
    index += [f"| [{s}]({s}.md) | {n} |" for s, n in files]
    index.append("")
    ipath = root / "index.md"
    itext = "\n".join(index)
    if ipath.exists() and ipath.read_text(encoding="utf-8") == itext:
        unchanged += 1
    else:
        ipath.write_text(itext, encoding="utf-8")
        written += 1

    return {"dir": root, "questions": len(rows), "topics": len(files),
            "written": written, "unchanged": unchanged}
