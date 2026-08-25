-- 016_question_lines.sql: make walking a question line cheap.
--
-- `questions.parent_id` has been in the schema since 001 and unused since 001:
-- extraction sees one question-shaped span at a time and has no way to know
-- that #803 ("Wait a minute, how are Call Protection and Prepayment
-- different?") is the second half of #802. `ib/chains.py` records that link
-- afterwards, and once it is recorded two reads happen constantly - the
-- lead-in above every drilled question, and the children of a question when
-- the whole line is listed.
--
-- Additive: an index and nothing else. Every existing row already has
-- parent_id NULL and behaves exactly as it did.
CREATE INDEX IF NOT EXISTS idx_questions_parent ON questions(parent_id);

-- The other half: which follow-up-shaped questions you have already looked at
-- and judged fine on their own. Without it `chains --scan` is a list that can
-- never reach zero -- #574 sets up its own scenario and only *reads* like a
-- follow-up, so every future scan would report it again and the report would
-- stop being read.
--
-- This is on the "yours and permanent" side of the line in 001. It is a
-- judgement you made about a question, in the same way a review is, and
-- re-extraction has no business discarding it.
CREATE TABLE IF NOT EXISTS question_line_review (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    verdict     TEXT NOT NULL,              -- standalone
    decided_at  TEXT NOT NULL
);
