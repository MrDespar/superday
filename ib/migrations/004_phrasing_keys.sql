-- enrich rewrites canonical_text, so the gate stopped recognising the wording
-- the source actually used: re-extracting an enriched chunk re-admitted 6 of
-- 14 questions as new. Phrasings now carry their own normalised key and the
-- gate matches against them too, so a question is found by any wording it has
-- ever been seen under, not just its latest canonical form.
ALTER TABLE phrasings ADD COLUMN norm_key TEXT;
CREATE INDEX IF NOT EXISTS idx_phrasings_norm ON phrasings(norm_key);
