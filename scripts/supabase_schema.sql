-- ============================================================
-- Vocamine: Supabase schema
-- Run this in the Supabase SQL Editor
-- ============================================================

-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    id         UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    level      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 2. Words
CREATE TABLE IF NOT EXISTS words (
    id         BIGSERIAL PRIMARY KEY,
    word       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 3. Meanings (words 1対多)
--    tier: NULL = ユーザーが追加した意味
--          1〜6 = CEFR-J Wordlist由来の意味
CREATE TABLE IF NOT EXISTS meanings (
    id             BIGSERIAL PRIMARY KEY,
    word_id        BIGINT NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    part_of_speech TEXT NOT NULL,
    definition     TEXT NOT NULL,
    ipa            TEXT,
    tier           SMALLINT CHECK (tier BETWEEN 1 AND 6)
);

CREATE INDEX IF NOT EXISTS meanings_word_id_idx ON meanings (word_id);
CREATE INDEX IF NOT EXISTS meanings_tier_idx    ON meanings (tier);
CREATE UNIQUE INDEX IF NOT EXISTS meanings_unique_idx ON meanings (word_id, part_of_speech, definition);

-- 4. Example sentences (meanings 1対1または1対0)
CREATE TABLE IF NOT EXISTS example_sentences (
    id                 BIGSERIAL PRIMARY KEY,
    meaning_id         BIGINT NOT NULL UNIQUE REFERENCES meanings(id) ON DELETE CASCADE,
    sentence           TEXT NOT NULL,
    translated_sentence TEXT
);

-- 5. Wordbook words (meaning_id単位, is_learned=trueが学習済み)
CREATE TABLE IF NOT EXISTS wordbook_words (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    meaning_id BIGINT NOT NULL REFERENCES meanings(id) ON DELETE CASCADE,
    is_learned BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, meaning_id)
);

CREATE INDEX IF NOT EXISTS wordbook_words_user_id_idx      ON wordbook_words (user_id);
CREATE INDEX IF NOT EXISTS wordbook_words_user_learned_idx ON wordbook_words (user_id, is_learned);

-- ============================================================
-- Row Level Security
-- ============================================================

ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_words ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own profile" ON users;
CREATE POLICY "own profile"
    ON users FOR ALL
    USING  (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS "own wordbook" ON wordbook_words;
CREATE POLICY "own wordbook"
    ON wordbook_words FOR ALL
    USING  (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

-- words / meanings / example_sentences はユーザー横断共有
-- FastAPI は service role で操作するため RLS 不要