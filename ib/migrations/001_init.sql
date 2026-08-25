-- Two halves, never mixed:
--   derived and disposable : sources, questions, answers, question_sources, phrasings
--   yours and permanent    : reviews, schedule, notes
-- Re-running extraction must never touch the second half. Everything in the
-- first half carries extraction_version so it can be rebuilt in place.

PRAGMA foreign_keys = ON;

CREATE TABLE sources (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,              -- pdf | docx | manual | voice | url | live
    title         TEXT NOT NULL,
    path          TEXT,
    file_hash     TEXT UNIQUE,                -- idempotency: same bytes, same source
    page_count    INTEGER,
    added_at      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE questions (
    id                 INTEGER PRIMARY KEY,
    canonical_text     TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'technical',  -- technical | market_awareness | behavioural
    topic              TEXT,
    subtopic           TEXT,
    difficulty         INTEGER,               -- 1 easy .. 5 hard
    origin             TEXT NOT NULL DEFAULT 'published',  -- published | interviewer_asked | self_authored
    parent_id          INTEGER REFERENCES questions(id),   -- harder follow-up hangs off its parent
    status             TEXT NOT NULL DEFAULT 'needs_review', -- needs_review | active | rejected
    extraction_version INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    norm_key           TEXT                   -- normalised text, exact-dup fast path
);
CREATE INDEX idx_questions_norm   ON questions(norm_key);
CREATE INDEX idx_questions_status ON questions(status);
CREATE INDEX idx_questions_kind   ON questions(kind);

CREATE TABLE answers (
    question_id        INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    answer_key         TEXT,
    rubric_points      TEXT,                  -- JSON array of strings
    common_mistakes    TEXT,                  -- JSON array of strings
    answer_status      TEXT NOT NULL DEFAULT 'ok',  -- ok | missing | drafted | volatile
    extraction_version INTEGER NOT NULL DEFAULT 1
);

-- The evidence table. Frequency score is count(distinct source_id) over this.
-- A new book should mostly land here, not in questions.
CREATE TABLE question_sources (
    question_id   INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    locator       TEXT,                       -- page number, section heading
    verbatim_text TEXT,
    PRIMARY KEY (question_id, source_id, locator)
);

-- Same question, different wording. Drill serves a random one for realism.
CREATE TABLE phrasings (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    source_id   INTEGER REFERENCES sources(id)
);
CREATE INDEX idx_phrasings_q ON phrasings(question_id);

-- Market awareness questions resolve their answer at drill time from a live
-- feed, because the answer expires. Nothing here is a stored fact.
CREATE TABLE live_bindings (
    question_id   INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,              -- treasury | fred | ecb
    series_key    TEXT NOT NULL,
    unit          TEXT,
    tolerance     REAL,                       -- how close counts as "you knew it"
    ttl_seconds   INTEGER NOT NULL DEFAULT 86400
);

CREATE TABLE live_cache (
    provider    TEXT NOT NULL,
    series_key  TEXT NOT NULL,
    value       REAL,
    as_of       TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (provider, series_key)
);

-- ---- yours, permanent, never regenerated ----------------------------------

CREATE TABLE reviews (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    asked_at    TEXT NOT NULL,
    phrasing    TEXT,
    user_answer TEXT,
    rating      INTEGER,                      -- 1 again 2 hard 3 good 4 easy
    score       REAL,                         -- 0..1 when graded
    rubric_hits TEXT,                         -- JSON array of booleans
    grader      TEXT NOT NULL DEFAULT 'self'  -- self | model name
);
CREATE INDEX idx_reviews_q ON reviews(question_id);

CREATE TABLE schedule (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
    card_json   TEXT NOT NULL,                -- opaque FSRS card state
    due_at      TEXT NOT NULL,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_schedule_due ON schedule(due_at);

CREATE TABLE notes (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Staging. Everything entering the bank passes the admission gate from here.
CREATE TABLE candidates (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    question_text TEXT NOT NULL,
    answer_text   TEXT,
    locator       TEXT,
    verdict       TEXT,                       -- new | duplicate | variant | rejected
    matched_id    INTEGER REFERENCES questions(id),
    similarity    REAL,
    created_at    TEXT NOT NULL
);
