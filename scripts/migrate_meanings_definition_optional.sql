-- Deprecated compatibility migration.
-- The current schema uses definition_en and allows definition_ja to be NULL.
-- Use scripts/migrate_definition_japanese_to_definition_ja.sql instead.

ALTER TABLE meanings
    ALTER COLUMN definition_ja DROP NOT NULL;
