-- Glossalyze migration: auxiliary meanings
-- Run this in Supabase SQL Editor before storing auxiliary meanings.
-- Safe to run more than once.

-- Sentence-level POS for words used as auxiliary/modal verbs.
ALTER TYPE part_of_speech ADD VALUE IF NOT EXISTS 'auxiliary';

-- Fallback source used only when Wiktionary does not expose an auxiliary/modal sense.
ALTER TYPE dictionary_source ADD VALUE IF NOT EXISTS 'grammar';

-- Fallback source used when no Wiktionary meaning exists for a spaCy-detected part of speech.
ALTER TYPE dictionary_source ADD VALUE IF NOT EXISTS 'gemini';

-- Stop here. Supabase/Postgres must commit the ALTER TYPE statements before
-- the new enum values can be referenced in a query.
--
-- Optional verification: run the SELECT statements below as a separate query
-- after this migration has completed.
/*
SELECT value AS part_of_speech
FROM unnest(enum_range(NULL::part_of_speech)) AS value
WHERE value::text = 'auxiliary';

SELECT value AS dictionary_source
FROM unnest(enum_range(NULL::dictionary_source)) AS value
WHERE value::text IN ('grammar', 'gemini');
*/
