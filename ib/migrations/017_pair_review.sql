-- 017_pair_review.sql: which near-duplicate pairs you have already settled.
--
-- `dupes` re-scans the whole bank on every run, so a pair you looked at and
-- decided were two different questions was proposed again the next time, and
-- the time after that, forever. That is the failure 016 was written for one
-- level down: a scan that cannot reach zero is a scan you stop reading, and
-- when you stop reading it the real duplicates go by with the false ones.
--
-- "What is an LBO" and "Walk me through an LBO" score 0.74 and are not the
-- same question. Saying so once has to be enough.
--
-- This is on the "yours and permanent" side of the line in 001, for the same
-- reason `question_line_review` is: it records a judgement you made, not
-- something extraction derived, and re-extraction has no business discarding
-- it. The ids are stored low-first so a pair has exactly one row whichever
-- order the scan happened to walk it in.
CREATE TABLE IF NOT EXISTS question_pair_review (
    low_id     INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    high_id    INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL,               -- distinct
    decided_at TEXT NOT NULL,
    PRIMARY KEY (low_id, high_id)
);
