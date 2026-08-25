-- 013_ingest_progress.sql: which chunks of a source have already been read.
--
-- Derived and disposable, like the rest of extraction: re-running extraction
-- may rebuild it.
--
-- An aborted ingest could not be resumed. The sources row was created on the
-- first attempt, so a re-run printed "skip (already ingested)" and the chunks
-- after the abort were never extracted -- the bank silently held half the
-- book. The only way forward was --force, which re-sent every chunk that had
-- already landed and paid for it twice.
--
-- question_sources.locator cannot answer this on its own: a chunk that
-- legitimately yielded no questions leaves no row, and would be re-read (and
-- re-paid for) on every subsequent run. So a chunk is recorded here when it
-- has been read, whatever it yielded.
CREATE TABLE IF NOT EXISTS ingest_progress (
    source_id INTEGER NOT NULL REFERENCES sources(id),
    locator TEXT NOT NULL,
    done_at TEXT NOT NULL,
    PRIMARY KEY (source_id, locator)
);

-- The backfill for sources that predate this table is 014, not here: this one
-- had already been applied to the real bank by the time it was needed, and an
-- applied migration is never re-run.
