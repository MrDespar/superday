-- Audit is derived state: which model checked an item, what it decided, why.
-- Stored on the question so a held verdict can be shown to a human in the
-- review queue rather than silently discarded.
ALTER TABLE questions ADD COLUMN audit_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE questions ADD COLUMN audit_verdict TEXT;
ALTER TABLE questions ADD COLUMN audit_reason TEXT;
