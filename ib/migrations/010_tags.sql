-- 010_tags.sql: Multi-tag taxonomy for fine-grained targeting
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT 'concept',  -- concept | topic | company | difficulty
    created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
);

CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_tags_kind ON tags(kind);

CREATE TABLE IF NOT EXISTS question_tags (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (DATETIME('now')),
    PRIMARY KEY (question_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_qtags_tag ON question_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_qtags_q   ON question_tags(question_id);
