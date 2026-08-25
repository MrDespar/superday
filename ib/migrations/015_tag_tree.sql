-- 015_tag_tree.sql: a tag can name a parent, so a broad filter pulls in narrow ones.
--
-- The taxonomy was flat: seventy-odd siblings with no way to say "everything
-- under accounting". Browsing wants the opposite shape -- start broad, get
-- narrower -- so a tag now optionally points at a parent, and filtering on a
-- parent matches every tag beneath it.
--
-- Additive and inert for anything that already exists: parent_id is NULL on
-- every current row, and a tag with no parent behaves exactly as it did. The
-- tree itself is seeded from Python (tagging.ensure_tree) rather than here,
-- because which tags exist changes as the bank grows and a re-runnable seed
-- that only ever fills a NULL parent lets you re-file a tag by hand without
-- the next startup putting it back.
ALTER TABLE tags ADD COLUMN parent_id INTEGER REFERENCES tags(id);

CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags(parent_id);
