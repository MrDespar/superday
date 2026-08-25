-- Full text search over the bank. Contentful FTS5 rather than an external
-- content table, because the searchable text spans questions and answers and
-- the triggers below are simpler than a join-backed external index.
-- rowid is always questions.id, so a hit maps straight back to `show <id>`.
CREATE VIRTUAL TABLE questions_fts USING fts5(
    question,
    answer,
    tokenize = 'porter unicode61'
);

INSERT INTO questions_fts (rowid, question, answer)
SELECT q.id, q.canonical_text, COALESCE(a.answer_key, '')
  FROM questions q LEFT JOIN answers a ON a.question_id = q.id;

-- Each trigger deletes before inserting: FTS5 has no upsert, and a bare insert
-- on an existing rowid would leave the old text findable forever.
CREATE TRIGGER questions_fts_ai AFTER INSERT ON questions BEGIN
    DELETE FROM questions_fts WHERE rowid = new.id;
    INSERT INTO questions_fts (rowid, question, answer) VALUES (new.id, new.canonical_text, '');
END;

CREATE TRIGGER questions_fts_au AFTER UPDATE OF canonical_text ON questions BEGIN
    DELETE FROM questions_fts WHERE rowid = new.id;
    INSERT INTO questions_fts (rowid, question, answer)
    VALUES (new.id, new.canonical_text,
            COALESCE((SELECT answer_key FROM answers WHERE question_id = new.id), ''));
END;

CREATE TRIGGER questions_fts_ad AFTER DELETE ON questions BEGIN
    DELETE FROM questions_fts WHERE rowid = old.id;
END;

CREATE TRIGGER answers_fts_ai AFTER INSERT ON answers BEGIN
    DELETE FROM questions_fts WHERE rowid = new.question_id;
    INSERT INTO questions_fts (rowid, question, answer)
    VALUES (new.question_id,
            COALESCE((SELECT canonical_text FROM questions WHERE id = new.question_id), ''),
            COALESCE(new.answer_key, ''));
END;

CREATE TRIGGER answers_fts_au AFTER UPDATE OF answer_key ON answers BEGIN
    DELETE FROM questions_fts WHERE rowid = new.question_id;
    INSERT INTO questions_fts (rowid, question, answer)
    VALUES (new.question_id,
            COALESCE((SELECT canonical_text FROM questions WHERE id = new.question_id), ''),
            COALESCE(new.answer_key, ''));
END;
