-- ============================================================
-- Vocamine: Supabase schema
-- ============================================================

-- ============================================================
-- ENUM Types
-- ============================================================

CREATE TYPE dictionary_source AS ENUM (
    'wiktionary',
    'cefr_j',
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
    definition       TEXT NOT NULL,
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
ON meanings(word_id, part_of_speech, definition);

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

-- ============================================================
-- Row Level Security
-- ============================================================

ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_words ENABLE ROW LEVEL SECURITY;

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

-- words / meanings / example_sentences は共有データ
-- FastAPI は service role で操作するため RLS は不要