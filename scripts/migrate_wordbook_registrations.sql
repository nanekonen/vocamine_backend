BEGIN;

-- 単語帳への所属と、その登録元を1行で表す。
-- 同じ単語・同じ単語帳でも、教材や出典が異なれば複数行を保持する。
CREATE TABLE IF NOT EXISTS wordbook_word_registrations (
    id               BIGSERIAL PRIMARY KEY,
    wordbook_word_id BIGINT NOT NULL REFERENCES wordbook_words(id) ON DELETE CASCADE,
    wordbook_id      UUID REFERENCES wordbooks(id) ON DELETE CASCADE,
    source_type      TEXT NOT NULL DEFAULT 'manual',
    material_id      UUID REFERENCES materials(id) ON DELETE SET NULL,
    folder_id        UUID REFERENCES material_folders(id) ON DELETE SET NULL,
    label            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS wordbook_word_registrations_unique_idx
ON wordbook_word_registrations (
    wordbook_word_id,
    COALESCE(wordbook_id, '00000000-0000-0000-0000-000000000000'::uuid),
    source_type,
    COALESCE(material_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(label, '')
);

CREATE INDEX IF NOT EXISTS wordbook_word_registrations_wordbook_idx
ON wordbook_word_registrations(wordbook_id);
CREATE INDEX IF NOT EXISTS wordbook_word_registrations_word_idx
ON wordbook_word_registrations(wordbook_word_id);
CREATE INDEX IF NOT EXISTS wordbook_word_registrations_material_idx
ON wordbook_word_registrations(material_id);

-- 既存教材のdefault_wordbook_idを使い、その単語帳へ登録されたと
-- 判断できる教材だけを紐付ける。他単語帳の教材履歴は混ぜない。
INSERT INTO wordbook_word_registrations (
    wordbook_word_id, wordbook_id, source_type,
    material_id, folder_id, label, created_at
)
SELECT
    membership.wordbook_word_id,
    membership.wordbook_id,
    src.source_type,
    src.material_id,
    src.folder_id,
    src.label,
    LEAST(membership.created_at, src.created_at)
FROM wordbook_memberships AS membership
JOIN wordbook_word_sources AS src
  ON src.wordbook_word_id = membership.wordbook_word_id
LEFT JOIN materials AS source_material
  ON source_material.id = src.material_id
WHERE src.material_id IS NULL
   OR source_material.default_wordbook_id = membership.wordbook_id
ON CONFLICT DO NOTHING;

-- 登録元のない所属も失わない。
INSERT INTO wordbook_word_registrations (
    wordbook_word_id, wordbook_id, source_type, label, created_at
)
SELECT
    membership.wordbook_word_id,
    membership.wordbook_id,
    'manual',
    NULL,
    membership.created_at
FROM wordbook_memberships AS membership
WHERE NOT EXISTS (
    SELECT 1 FROM wordbook_word_sources AS src
    WHERE src.wordbook_word_id = membership.wordbook_word_id
)
ON CONFLICT DO NOTHING;

-- 独立単語帳へ未所属の履歴（初期レベル等）も保持する。
INSERT INTO wordbook_word_registrations (
    wordbook_word_id, wordbook_id, source_type,
    material_id, folder_id, label, created_at
)
SELECT
    src.wordbook_word_id,
    NULL,
    src.source_type,
    src.material_id,
    src.folder_id,
    src.label,
    src.created_at
FROM wordbook_word_sources AS src
LEFT JOIN materials AS source_material
  ON source_material.id = src.material_id
LEFT JOIN wordbook_memberships AS matched_membership
  ON matched_membership.wordbook_word_id = src.wordbook_word_id
 AND (
     src.material_id IS NULL
     OR source_material.default_wordbook_id = matched_membership.wordbook_id
 )
WHERE matched_membership.wordbook_word_id IS NULL
ON CONFLICT DO NOTHING;

ALTER TABLE wordbook_word_registrations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own wordbook registrations" ON wordbook_word_registrations;
CREATE POLICY "own wordbook registrations"
    ON wordbook_word_registrations FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM wordbook_words
            WHERE wordbook_words.id = wordbook_word_registrations.wordbook_word_id
              AND wordbook_words.user_id = auth.uid()
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM wordbook_words
            WHERE wordbook_words.id = wordbook_word_registrations.wordbook_word_id
              AND wordbook_words.user_id = auth.uid()
        )
        AND (
            wordbook_id IS NULL OR EXISTS (
                SELECT 1 FROM wordbooks
                WHERE wordbooks.id = wordbook_word_registrations.wordbook_id
                  AND wordbooks.user_id = auth.uid()
            )
        )
    );

DROP TABLE IF EXISTS wordbook_memberships;
DROP TABLE IF EXISTS wordbook_word_sources;

COMMIT;
