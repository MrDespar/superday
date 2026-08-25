-- 008_answer_history.sql: Track answer edits and allow undo on answer changes
CREATE TABLE IF NOT EXISTS answer_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    old_answer_key TEXT,
    old_rubric_points TEXT,
    new_answer_key TEXT,
    new_rubric_points TEXT,
    action TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answer_history_batch ON answer_history(batch_id);
CREATE INDEX IF NOT EXISTS idx_answer_history_qid ON answer_history(question_id);
