-- Align meanings with the current invariant:
--   definition_en: optional English/source gloss only
--   definition_ja: optional Japanese meaning; NULL rows are pending regeneration

ALTER TABLE meanings
    ALTER COLUMN definition_ja DROP NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'meanings'
          AND column_name = 'definition'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'meanings'
          AND column_name = 'definition_en'
    ) THEN
        ALTER TABLE meanings RENAME COLUMN definition TO definition_en;
    END IF;
END $$;

UPDATE meanings
SET definition_ja = definition_en
WHERE definition_en ~ '[ぁ-んァ-ン一-龯]'
  AND (definition_ja IS NULL OR btrim(definition_ja) = '');

UPDATE meanings
SET definition_en = NULL
WHERE definition_en ~ '[ぁ-んァ-ン一-龯]';

ALTER TABLE meanings
    ALTER COLUMN definition_en DROP NOT NULL,
    ALTER COLUMN definition_ja DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS meanings_unique_en_idx
ON meanings(word_id, part_of_speech, definition_en);

CREATE UNIQUE INDEX IF NOT EXISTS meanings_unique_ja_idx
ON meanings(word_id, part_of_speech, definition_ja)
WHERE definition_ja IS NOT NULL AND btrim(definition_ja) <> '';
