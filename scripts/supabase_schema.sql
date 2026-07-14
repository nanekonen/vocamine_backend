-- ============================================================
-- Glossalyze: Supabase schema
-- ============================================================

-- ============================================================
-- ENUM Types
-- ============================================================

CREATE TYPE dictionary_source AS ENUM (
    'wiktionary',
    'grammar',
    'gemini',
    'cefr_j',
    'phave_list',
    'phrase_list',
    'academic_collocation_list',
    'collins',
    'cambridge',
    'oxford',
    'manual'
);

CREATE TYPE part_of_speech AS ENUM (
    'noun',
    'verb',
    'adjective',
    'adverb',
    'pronoun',
    'preposition',
    'conjunction',
    'interjection',
    'determiner',
    'article',
    'auxiliary',
    'numeral',
    'prefix',
    'suffix',
    'phrase',
    'abbreviation'
);

CREATE TYPE transitivity_type AS ENUM (
    'transitive',
    'intransitive',
    'both'
);

CREATE TYPE countability_type AS ENUM (
    'countable',
    'uncountable',
    'both'
);

-- ============================================================
-- Users
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id         UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    level      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Words
-- ============================================================

CREATE TABLE IF NOT EXISTS words (
    id         BIGSERIAL PRIMARY KEY,
    word       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Meanings
-- ============================================================

CREATE TABLE IF NOT EXISTS meanings (
    id               BIGSERIAL PRIMARY KEY,
    word_id          BIGINT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    part_of_speech   part_of_speech NOT NULL,
    definition_en    TEXT,
    ipa              TEXT,
    transitivity     transitivity_type,
    countability     countability_type,
    inflections      JSONB,
    definition_ja    TEXT,
    source           dictionary_source NOT NULL,
    tier             SMALLINT CHECK (tier IS NULL OR tier BETWEEN 1 AND 6)
);

CREATE INDEX IF NOT EXISTS meanings_word_id_idx ON meanings(word_id);
CREATE INDEX IF NOT EXISTS meanings_tier_idx    ON meanings(tier);
CREATE INDEX IF NOT EXISTS meanings_source_idx  ON meanings(source);

CREATE UNIQUE INDEX IF NOT EXISTS meanings_unique_idx
ON meanings(word_id, part_of_speech, definition_en);

CREATE UNIQUE INDEX IF NOT EXISTS meanings_unique_ja_idx
ON meanings(word_id, part_of_speech, definition_ja);

-- ============================================================
-- Example sentences (1 meaning : N examples)
-- ============================================================

CREATE TABLE IF NOT EXISTS example_sentences (
    id                  BIGSERIAL PRIMARY KEY,
    meaning_id          BIGINT NOT NULL REFERENCES meanings(id) ON DELETE CASCADE,
    sentence            TEXT NOT NULL,
    translated_sentence TEXT
);

CREATE INDEX IF NOT EXISTS example_sentences_meaning_idx
ON example_sentences(meaning_id);

-- ============================================================
-- Materials
-- ============================================================

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
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id           UUID REFERENCES material_folders(id) ON DELETE SET NULL,
    title               TEXT NOT NULL,
    ocr_text            TEXT NOT NULL DEFAULT '',
    source_mime_type    TEXT,
    source_object_storage_key       TEXT,
    page_images_object_storage_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_word_boxes   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS materials_user_id_idx
ON materials(user_id);

CREATE INDEX IF NOT EXISTS materials_folder_id_idx
ON materials(folder_id);

-- ============================================================
-- Wordbook
-- ============================================================

CREATE TABLE IF NOT EXISTS wordbook_words (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    meaning_id BIGINT NOT NULL REFERENCES meanings(id) ON DELETE CASCADE,
    is_learned BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, meaning_id)
);

CREATE INDEX IF NOT EXISTS wordbook_words_user_id_idx      ON wordbook_words(user_id);
CREATE INDEX IF NOT EXISTS wordbook_words_user_learned_idx ON wordbook_words(user_id, is_learned);

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

-- ============================================================
-- Row Level Security
-- ============================================================

ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_words ENABLE ROW LEVEL SECURITY;
ALTER TABLE material_folders ENABLE ROW LEVEL SECURITY;
ALTER TABLE materials ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_word_sources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own profile" ON users;
CREATE POLICY "own profile"
    ON users FOR ALL
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS "own wordbook" ON wordbook_words;
CREATE POLICY "own wordbook"
    ON wordbook_words FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

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

-- words / meanings / example_sentences は共有データ
-- FastAPI は service role で操作するため RLS は不要
