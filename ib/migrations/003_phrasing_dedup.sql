-- phrasings had no uniqueness, so any --force re-ingest stacked another copy
-- of every alternate wording. Collapse what is already there, then make the
-- INSERT OR IGNORE in the admission gate mean something.
DELETE FROM phrasings
 WHERE id NOT IN (SELECT MIN(id) FROM phrasings GROUP BY question_id, text);

CREATE UNIQUE INDEX IF NOT EXISTS idx_phrasings_unique
    ON phrasings(question_id, text);

-- The gate logs every candidate now, including rejects and dedups. These are
-- the columns the `gate` view reads.
CREATE INDEX IF NOT EXISTS idx_candidates_verdict ON candidates(verdict);
CREATE INDEX IF NOT EXISTS idx_candidates_source  ON candidates(source_id);
