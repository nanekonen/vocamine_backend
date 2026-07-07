CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS material_folders (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id  UUID REFERENCES material_folders(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT material_folders_not_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE UNIQUE INDEX IF NOT EXISTS material_folders_unique_sibling_name_idx
ON material_folders (
    user_id,
    COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    name
);

CREATE INDEX IF NOT EXISTS material_folders_user_id_idx
ON material_folders(user_id);

CREATE INDEX IF NOT EXISTS material_folders_parent_id_idx
ON material_folders(parent_id);

CREATE OR REPLACE FUNCTION prevent_material_folder_cycle()
RETURNS trigger AS $$
DECLARE
    has_cycle BOOLEAN;
    parent_user_id UUID;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT user_id INTO parent_user_id
    FROM material_folders
    WHERE id = NEW.parent_id;

    IF parent_user_id IS NULL OR parent_user_id <> NEW.user_id THEN
        RAISE EXCEPTION 'parent folder must belong to the same user';
    END IF;

    IF NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION 'folder cannot be its own parent';
    END IF;

    WITH RECURSIVE ancestors(id, parent_id) AS (
        SELECT id, parent_id
        FROM material_folders
        WHERE id = NEW.parent_id
      UNION ALL
        SELECT f.id, f.parent_id
        FROM material_folders f
        JOIN ancestors a ON f.id = a.parent_id
    )
    SELECT EXISTS (
        SELECT 1 FROM ancestors WHERE id = NEW.id
    )
    INTO has_cycle;

    IF has_cycle THEN
        RAISE EXCEPTION 'folder cycle detected';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS material_folders_prevent_cycle_trg ON material_folders;
CREATE TRIGGER material_folders_prevent_cycle_trg
BEFORE INSERT OR UPDATE OF parent_id, user_id
ON material_folders
FOR EACH ROW
EXECUTE FUNCTION prevent_material_folder_cycle();

CREATE TABLE IF NOT EXISTS materials (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id                       UUID REFERENCES material_folders(id) ON DELETE SET NULL,
    title                           TEXT NOT NULL,
    extracted_text                  TEXT NOT NULL DEFAULT '',
    source_mime_type                TEXT,
    source_object_storage_key       TEXT,
    readable_pdf_object_storage_key TEXT,
    thumbnail_object_storage_key    TEXT,
    created_at                      TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS extracted_text TEXT NOT NULL DEFAULT '';

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS source_mime_type TEXT;

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS source_object_storage_key TEXT;

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS readable_pdf_object_storage_key TEXT;

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS thumbnail_object_storage_key TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'materials'
          AND column_name = 'ocr_text'
    ) THEN
        UPDATE materials
        SET extracted_text = COALESCE(NULLIF(extracted_text, ''), ocr_text, '');
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'materials'
          AND column_name = 'source_r2_key'
    ) THEN
        UPDATE materials
        SET source_object_storage_key = COALESCE(source_object_storage_key, source_r2_key);
    END IF;
END $$;

ALTER TABLE materials
DROP COLUMN IF EXISTS ocr_text;

ALTER TABLE materials
DROP COLUMN IF EXISTS source_word_boxes;

ALTER TABLE materials
DROP COLUMN IF EXISTS page_images_object_storage_keys;

ALTER TABLE materials
DROP COLUMN IF EXISTS page_images_r2_keys;

ALTER TABLE materials
DROP COLUMN IF EXISTS source_r2_key;

CREATE INDEX IF NOT EXISTS materials_user_id_idx
ON materials(user_id);

CREATE INDEX IF NOT EXISTS materials_folder_id_idx
ON materials(folder_id);

CREATE TABLE IF NOT EXISTS wordbook_word_sources (
    id               BIGSERIAL PRIMARY KEY,
    wordbook_word_id BIGINT NOT NULL REFERENCES wordbook_words(id) ON DELETE CASCADE,
    source_type      TEXT NOT NULL DEFAULT 'manual',
    material_id      UUID REFERENCES materials(id) ON DELETE SET NULL,
    folder_id        UUID REFERENCES material_folders(id) ON DELETE SET NULL,
    label            TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS wordbook_word_sources_unique_idx
ON wordbook_word_sources (
    wordbook_word_id,
    source_type,
    COALESCE(material_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(label, '')
);

CREATE INDEX IF NOT EXISTS wordbook_word_sources_wordbook_word_id_idx
ON wordbook_word_sources(wordbook_word_id);

CREATE INDEX IF NOT EXISTS wordbook_word_sources_material_id_idx
ON wordbook_word_sources(material_id);

CREATE INDEX IF NOT EXISTS wordbook_word_sources_folder_id_idx
ON wordbook_word_sources(folder_id);

ALTER TABLE material_folders ENABLE ROW LEVEL SECURITY;
ALTER TABLE materials ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_word_sources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own material folders" ON material_folders;
CREATE POLICY "own material folders"
    ON material_folders FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "own materials" ON materials;
CREATE POLICY "own materials"
    ON materials FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "own wordbook sources" ON wordbook_word_sources;
CREATE POLICY "own wordbook sources"
    ON wordbook_word_sources FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM wordbook_words
            WHERE wordbook_words.id = wordbook_word_sources.wordbook_word_id
              AND wordbook_words.user_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wordbook_words
            WHERE wordbook_words.id = wordbook_word_sources.wordbook_word_id
              AND wordbook_words.user_id = auth.uid()
        )
    );

INSERT INTO wordbook_word_sources (wordbook_word_id, source_type, label)
SELECT id, CASE WHEN is_learned THEN 'initial_level' ELSE 'manual' END, NULL
FROM wordbook_words
ON CONFLICT DO NOTHING;
