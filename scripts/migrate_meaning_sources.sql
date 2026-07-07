-- Add non-manual sources for imported lists.
-- `manual` is reserved for user-entered meanings.

ALTER TYPE dictionary_source ADD VALUE IF NOT EXISTS 'phrase_list';
ALTER TYPE dictionary_source ADD VALUE IF NOT EXISTS 'phave_list';
ALTER TYPE dictionary_source ADD VALUE IF NOT EXISTS 'academic_collocation_list';

UPDATE meanings
SET source = 'academic_collocation_list'
WHERE source = 'manual'
  AND part_of_speech = 'phrase'
  AND inflections ? 'acl_generated';

UPDATE meanings
SET source = 'phrase_list'
WHERE source = 'manual'
  AND part_of_speech = 'phrase'
  AND NOT (inflections ? 'acl_generated');
