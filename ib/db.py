"""SQLite access and migrations."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Inside the package, not beside it: an installed wheel has no repo root to
# look in, and a tool that cannot find its own migrations cannot open a
# database at all.
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def db_path() -> Path:
    """Which database file the tool is pointed at.

    IB_DB overrides it. That exists so an end-to-end run -- drilling, rating, a
    whole mock interview, anything that writes -- can be rehearsed against a
    throwaway copy of the real bank instead of the real bank. Resolved at call
    time so setting IB_DB after import still wins.
    """
    env = os.environ.get("IB_DB")
    return Path(env).expanduser() if env else config.home() / "ib.db"


# There is deliberately no `DB_PATH = db_path()` constant here. Bound at import
# time it is captured before anything has had a chance to set IB_DB, so a
# caller reaching for it would quietly address the real bank while every other
# reader addressed the copy -- the same staleness the accessors in llm.py exist
# to avoid. Call `db_path()`.


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any migration files not yet recorded. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["name"] for r in conn.execute("SELECT name FROM _migrations")}
    ran = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in applied:
            continue
        # Append the migration record to the script so both the DDL and the
        # record land in the same executescript() call. executescript()
        # implicitly commits beforehand, so without this a crash between
        # the DDL and the INSERT leaves the migration applied but unrecorded.
        script = f.read_text()
        ts = now()
        script += (
            f"\nINSERT INTO _migrations (name, applied_at) "
            f"VALUES ('{f.name}', '{ts}');\n"
        )
        conn.executescript(script)
        ran.append(f.name)
    _backfill_phrasing_keys(conn)
    _backfill_primary_audits(conn)
    return ran


def _backfill_phrasing_keys(conn: sqlite3.Connection) -> int:
    """Fill norm_key for phrasings that predate the column. Idempotent, and
    cheap enough to leave on every startup: it only ever touches NULL rows."""
    from .admission import normalize
    rows = conn.execute(
        "SELECT id, text FROM phrasings WHERE norm_key IS NULL"
    ).fetchall()
    if not rows:
        return 0
    conn.executemany(
        "UPDATE phrasings SET norm_key = ? WHERE id = ?",
        [(normalize(r["text"]), r["id"]) for r in rows],
    )
    conn.commit()
    return len(rows)


def _backfill_primary_audits(conn: sqlite3.Connection) -> int:
    """Give every first-opinion verdict on questions a row in audits.
    Idempotent, and only ever touches questions that are missing one.

    audit writes both places now, but questions.audit_verdict predates the
    audits table and migration 005 backfills only what existed when it ran. Any
    audit that lands between that migration and this code -- an `audit` running
    in another terminal, a checkout that skips a release -- would otherwise be
    invisible to cross-audit, which silently reports those questions as having
    no first opinion to disagree with. That is the one failure this pass must
    not have, so it is reconciled on every startup rather than once.

    The "is one already there" test has to name every provider that can produce
    a first opinion, not just Gemini: once `audit` can run on Claude or OpenAI,
    a Gemini-only test sees no row, backfills a second one labelled gemini, and
    invents a Gemini verdict for a pass Gemini never ran. Rows it does write
    stay labelled gemini, because the only rows that reach here are ones that
    predate the provider setting existing, and those really were Gemini's.
    """
    # Imported here rather than at module scope: db.py is the bottom of the
    # stack and every module above it connects, so it stays free of package
    # imports that would have to be ordered.
    from . import llm
    rows = conn.execute(
        "SELECT id, audit_version, audit_verdict, audit_reason FROM questions q "
        "WHERE q.audit_verdict IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM audits a WHERE a.question_id = q.id "
        f"AND a.provider IN {llm.PRIMARY_SQL})"
    ).fetchall()
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO audits (question_id, provider, model, audit_version, verdict, "
        "reason, ran_at) VALUES (?, 'gemini', 'gemini-3.6-flash', ?, ?, ?, ?)",
        [(r["id"], r["audit_version"] or 0, r["audit_verdict"], r["audit_reason"], now())
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_source(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    path: str | None = None,
    file_hash: str | None = None,
    page_count: int | None = None,
) -> tuple[int, bool]:
    """Return (source_id, created). Same file_hash is a no-op, which is what
    makes re-adding a PDF safe."""
    if file_hash:
        row = conn.execute(
            "SELECT id FROM sources WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if row:
            return row["id"], False
    cur = conn.execute(
        "INSERT INTO sources (kind, title, path, file_hash, page_count, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, title, path, file_hash, page_count, now()),
    )
    conn.commit()
    return int(cur.lastrowid), True
