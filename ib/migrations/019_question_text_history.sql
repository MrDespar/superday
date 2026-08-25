-- 019_question_text_history.sql: make a rewritten question undoable.
--
-- `history` covers two of the three things a command can change about a
-- question: its status (006) and its answer (008, 012). The third, the
-- question text itself, was written with a raw UPDATE from three places --
-- `edit`, `audit`'s fix verdict, and `enrich`'s canonicalisation -- and
-- `undo` could not see any of them.
--
-- That is the failure 006's comment names: a mutation that skips history is
-- worse than no undo at all, because `undo` still reports success and puts
-- back everything except the thing you actually wanted back. An enrich run
-- that rewrites 800 stems and an audit that rewrites one are both exactly
-- what undo exists for.
--
-- norm_key travels with the text because it is derived from it and the gate
-- reads it: restoring the words and leaving yesterday's key behind would
-- leave the question findable under a wording it no longer has.
CREATE TABLE IF NOT EXISTS question_text_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    old_text    TEXT,
    old_norm    TEXT,
    new_text    TEXT,
    new_norm    TEXT,
    action      TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_question_text_history_batch
    ON question_text_history(batch_id);
CREATE INDEX IF NOT EXISTS idx_question_text_history_qid
    ON question_text_history(question_id);
