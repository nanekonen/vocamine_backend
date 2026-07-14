ALTER TABLE materials
ADD COLUMN IF NOT EXISTS analysis_summary JSONB;

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS analysis_items JSONB;

COMMENT ON COLUMN materials.analysis_summary IS
'Cached lexical counts, coverage, and updated_at.';

COMMENT ON COLUMN materials.analysis_items IS
'Compact lexical item keys used to refresh analysis_summary without rerunning spaCy.';
