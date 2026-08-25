-- Second opinions, one row per (question, provider, run). Derived and
-- disposable in the sense of 001: every row can be rebuilt by re-running the
-- pass, and nothing here is your progress. It is append-only rather than one
-- row per pair, because the useful question is not only "what does Claude say"
-- but "did it change its mind when the answer was rewritten".
--
-- Kept out of the questions table on purpose. audit_verdict/audit_reason there
-- are Gemini's, written by `audit`, and a second provider must not overwrite
-- them: a disagreement you cannot see is a disagreement that does not help.
CREATE TABLE audits (
    id                 INTEGER PRIMARY KEY,
    question_id        INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,          -- gemini | claude-code | claude-api
    model              TEXT,                   -- the exact model id, when known
    audit_version      INTEGER NOT NULL DEFAULT 0,
    verdict            TEXT NOT NULL,          -- keep | fix | reject
    reason             TEXT,
    confidence         REAL,                   -- 0..1, calibrated by the critic
    corrected_question TEXT,
    corrected_answer   TEXT,
    ran_at             TEXT NOT NULL
);
CREATE INDEX idx_audits_question ON audits(question_id);
CREATE INDEX idx_audits_provider ON audits(provider, verdict);

-- Backfill what Gemini has already decided, so the very first cross-audit run
-- has both sides to compare instead of half a table.
INSERT INTO audits (question_id, provider, model, audit_version, verdict, reason, ran_at)
SELECT id, 'gemini', 'gemini-3.6-flash', COALESCE(audit_version, 0),
       audit_verdict, audit_reason,
       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
  FROM questions
 WHERE audit_verdict IS NOT NULL;
