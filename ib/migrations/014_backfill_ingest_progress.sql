-- 014_backfill_ingest_progress.sql: every source that predates 013 counts as
-- fully read.
--
-- Without this, nothing in the bank has a progress row, so the resumable
-- ingest would see every book as half-read and re-send every chunk of all of
-- them on the next run -- the exact bill the change was meant to avoid.
-- Today's behaviour for an already-ingested source is "skip", and that is what
-- this preserves; only ingests from here on are genuinely resumable.
--
-- INSERT OR IGNORE, so it is harmless on a database that already has progress
-- rows, and a no-op on a fresh one where sources is still empty.
INSERT OR IGNORE INTO ingest_progress (source_id, locator, done_at)
SELECT id, '__complete__', datetime('now') FROM sources;
