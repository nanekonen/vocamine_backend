BEGIN;

-- 単語帳専用フォルダ。教材フォルダとは完全に独立させる。
CREATE TABLE IF NOT EXISTS wordbook_folders (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id  UUID REFERENCES wordbook_folders(id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (btrim(name) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wordbook_folders_user_id_idx
ON wordbook_folders(user_id);

CREATE INDEX IF NOT EXISTS wordbook_folders_parent_id_idx
ON wordbook_folders(parent_id);

CREATE UNIQUE INDEX IF NOT EXISTS wordbook_folders_unique_name_idx
ON wordbook_folders (
    user_id,
    COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    lower(btrim(name))
);

-- 実体を持つ単語帳。教材とは独立して管理する。
CREATE TABLE IF NOT EXISTS wordbooks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id  UUID REFERENCES wordbook_folders(id) ON DELETE SET NULL,
    name       TEXT NOT NULL CHECK (btrim(name) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wordbooks_user_id_idx ON wordbooks(user_id);
CREATE INDEX IF NOT EXISTS wordbooks_folder_id_idx ON wordbooks(folder_id);
CREATE UNIQUE INDEX IF NOT EXISTS wordbooks_unique_name_idx
ON wordbooks (
    user_id,
    COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid),
    lower(btrim(name))
);

-- wordbook_words は従来どおり「ユーザーの語彙・学習状態」。
-- このテーブルが、語彙を1冊以上の単語帳へ所属させる。
CREATE TABLE IF NOT EXISTS wordbook_memberships (
    wordbook_id      UUID NOT NULL REFERENCES wordbooks(id) ON DELETE CASCADE,
    wordbook_word_id BIGINT NOT NULL REFERENCES wordbook_words(id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (wordbook_id, wordbook_word_id)
);

CREATE INDEX IF NOT EXISTS wordbook_memberships_wordbook_id_idx
ON wordbook_memberships(wordbook_id);

CREATE INDEX IF NOT EXISTS wordbook_memberships_wordbook_word_id_idx
ON wordbook_memberships(wordbook_word_id);

-- 教材から通常登録するときの前回選択先。単語帳削除時は未選択へ戻す。
ALTER TABLE materials
ADD COLUMN IF NOT EXISTS default_wordbook_id UUID
REFERENCES wordbooks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS materials_default_wordbook_id_idx
ON materials(default_wordbook_id);

-- 旧版SQLを先に実行していた場合も最小構成へ戻せるようにする。
DROP TABLE IF EXISTS material_wordbook_preferences;
DROP INDEX IF EXISTS wordbooks_unique_material_source_idx;
DROP INDEX IF EXISTS wordbooks_source_material_id_idx;
DROP INDEX IF EXISTS wordbook_memberships_material_id_idx;
ALTER TABLE wordbooks
    DROP COLUMN IF EXISTS kind,
    DROP COLUMN IF EXISTS source_material_id,
    DROP COLUMN IF EXISTS updated_at;
ALTER TABLE wordbook_folders
    DROP COLUMN IF EXISTS updated_at;
ALTER TABLE wordbook_memberships
    DROP COLUMN IF EXISTS added_from_material_id,
    DROP COLUMN IF EXISTS source_label;

-- 旧版で採番IDを作成済みなら、自然な複合主キーへ置き換える。
ALTER TABLE wordbook_memberships
    DROP CONSTRAINT IF EXISTS wordbook_memberships_pkey;
ALTER TABLE wordbook_memberships
    DROP COLUMN IF EXISTS id;
ALTER TABLE wordbook_memberships
    ADD PRIMARY KEY (wordbook_id, wordbook_word_id);

ALTER TABLE wordbook_folders ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE wordbook_memberships ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own wordbook folders" ON wordbook_folders;
CREATE POLICY "own wordbook folders"
    ON wordbook_folders FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "own independent wordbooks" ON wordbooks;
CREATE POLICY "own independent wordbooks"
    ON wordbooks FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "own wordbook memberships" ON wordbook_memberships;
CREATE POLICY "own wordbook memberships"
    ON wordbook_memberships FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM wordbooks
            WHERE wordbooks.id = wordbook_memberships.wordbook_id
              AND wordbooks.user_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wordbooks
            WHERE wordbooks.id = wordbook_memberships.wordbook_id
              AND wordbooks.user_id = auth.uid()
        )
        AND EXISTS (
            SELECT 1 FROM wordbook_words
            WHERE wordbook_words.id = wordbook_memberships.wordbook_word_id
              AND wordbook_words.user_id = auth.uid()
        )
    );

COMMIT;
