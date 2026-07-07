CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS material_folders (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id  UUID REFERENCES material_folders(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS material_folders_user_id_idx
ON material_folders(user_id);

CREATE INDEX IF NOT EXISTS material_folders_parent_id_idx
ON material_folders(parent_id);

CREATE TABLE IF NOT EXISTS materials (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id                       UUID REFERENCES material_folders(id) ON DELETE SET NULL,
    title                           TEXT NOT NULL,
    ocr_text                        TEXT NOT NULL DEFAULT '',
    source_mime_type                TEXT,
    source_object_storage_key       TEXT,
    page_images_object_storage_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_word_boxes               JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at                      TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS source_object_storage_key TEXT;

ALTER TABLE materials
ADD COLUMN IF NOT EXISTS page_images_object_storage_keys JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'materials'
          AND column_name = 'source_r2_key'
    ) THEN
        UPDATE materials
        SET source_object_storage_key = COALESCE(source_object_storage_key, source_r2_key);
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'materials'
          AND column_name = 'page_images_r2_keys'
    ) THEN
        UPDATE materials
        SET page_images_object_storage_keys =
            CASE
                WHEN page_images_object_storage_keys = '[]'::jsonb
                THEN page_images_r2_keys
                ELSE page_images_object_storage_keys
            END;
    END IF;
END $$;

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
