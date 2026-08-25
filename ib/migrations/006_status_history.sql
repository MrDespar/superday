-- Every status transition, so a bad reject or a bad bulk accept is reversible.
-- This is the one piece of audit trail that is yours and permanent: it records
-- what *you* did to the bank, not what a model derived from it, so re-running
-- extraction must never clear it.
--
-- batch_id groups the rows written by a single action. `undo` reverts one whole
-- batch, which is what makes an `accept-all` over 500 questions reversible
-- without hand-editing SQLite.
CREATE TABLE question_status_history (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    action      TEXT NOT NULL,   -- review | accept-all | audit | cross-audit | undo
    batch_id    TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);
CREATE INDEX idx_status_history_batch ON question_status_history(batch_id);
CREATE INDEX idx_status_history_q     ON question_status_history(question_id);
