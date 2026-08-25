-- Drill and mock sessions, so quitting halfway is a pause and not a loss.
--
-- Sits on the "yours and permanent" side of the line: a session records what
-- you were actually doing, and re-running extraction has no business touching
-- it. Kept separate from `reviews` on purpose -- a review is a fact about a
-- question, a session is a fact about a sitting, and the sitting is throwaway
-- once it is finished while the reviews inside it are not.

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,              -- drill | mock
    started_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    finished_at  TEXT,                       -- NULL while resumable
    spec_json    TEXT NOT NULL,              -- the arguments it was opened with
    queue_json   TEXT NOT NULL,              -- question ids still to ask, in order
    done_json    TEXT NOT NULL DEFAULT '[]', -- [{id, rating, seconds, graded}]
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(kind, finished_at, updated_at);
