-- 009_embeddings_and_tags.sql: Semantic vector embeddings and tags
CREATE TABLE IF NOT EXISTS embeddings (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id),
    vector BLOB NOT NULL,
    model TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
