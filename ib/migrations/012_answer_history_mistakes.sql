-- 012_answer_history_mistakes.sql: let undo restore common_mistakes too.
--
-- enrich wrote rubric_points and common_mistakes with a raw UPDATE, so a bad
-- enrich run was invisible to `undo`. Routing it through history.set_answer
-- needs somewhere to keep the previous common_mistakes; without these two
-- columns an undo would put the rubric back and leave the mistakes list from
-- the run it was undoing.
ALTER TABLE answer_history ADD COLUMN old_common_mistakes TEXT;
ALTER TABLE answer_history ADD COLUMN new_common_mistakes TEXT;
