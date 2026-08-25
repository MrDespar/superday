-- 018_phrasing_review.sql: which drifted phrasings you have already settled.
--
-- A phrasing is scored against the canonical *at ingest time* and never again.
-- `enrich` then rewrites the canonical, and nothing re-checks what is already
-- attached -- so #88 ("What is Working Capital, and how is it calculated for
-- valuation purposes?") carries the phrasing "What is negative working
-- capital?", which is a different question with a different answer. `drill`
-- serves a random phrasing for realism, so one sitting in three asked it and
-- then graded the answer against the wrong rubric.
--
-- `dupes --phrasings` re-scores every phrasing against the canonical it
-- currently hangs off. Two outcomes reach zero: detach it, which deletes the
-- row, or say it is fine, which lands here.
--
-- Saying it is fine is a real answer rather than a shrug. A deliberate
-- translation scores near zero against its own canonical and is exactly
-- right; the language check in `dupes` catches the obvious ones, and this is
-- where the rest are recorded.
--
-- On the "yours and permanent" side of the line in 001, for the same reason
-- `question_line_review` and `question_pair_review` are: it is a judgement you
-- made, and re-extraction has no business discarding it. Keyed by the text
-- rather than by phrasings.id, because the derived half may be rebuilt in
-- place and the row would come back with a new id and no memory.
CREATE TABLE IF NOT EXISTS phrasing_review (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    norm_key    TEXT NOT NULL,
    verdict     TEXT NOT NULL,              -- keep
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (question_id, norm_key)
);
