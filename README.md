# Vocamine Backend

FastAPI + Supabase + Google Cloud Vision

---

## セットアップ

### 1. 仮想環境 & 依存関係

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 環境変数

```bash
cp .env.example .env
```

`.env` を編集して以下を設定：

| 変数 | 説明 |
|------|------|
| `SUPABASE_URL` | SupabaseプロジェクトのURL |
| `SUPABASE_SERVICE_ROLE_KEY` | サービスロールキー（Settings → API） |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCPサービスアカウントJSONのパス |

### 3. Supabase テーブル作成

Supabase の SQL Editor で `scripts/supabase_schema.sql` を実行。

### 4. サーバー起動

```bash
uvicorn app.main:app --reload
```

→ http://localhost:8000/docs でSwagger UIが開きます。

---

## API エンドポイント一覧

| Method | Path | 説明 |
|--------|------|------|
| `POST` | `/api/v1/ocr/image` | 画像→テキスト（Google Vision） |
| `POST` | `/api/v1/ocr/pdf` | PDF→テキスト（Google Vision） |
| `POST` | `/api/v1/words/extract` | テキストから未知単語を抽出 |
| `GET`  | `/api/v1/words/{user_id}` | 単語帳取得（?is_learned=true/false） |
| `POST` | `/api/v1/words/` | 単語を1件追加 |
| `POST` | `/api/v1/words/batch` | 未知単語を一括追加 |
| `PATCH`| `/api/v1/words/{word_id}` | 意味・学習済み状態を更新 |
| `DELETE`| `/api/v1/words/{word_id}` | 単語を削除 |
| `PUT`  | `/api/v1/users/{user_id}/level` | ユーザーレベルを保存 |
| `GET`  | `/api/v1/users/{user_id}/level` | ユーザーレベルを取得 |

---

## 典型的なフロー

```
[Flutter] 画像選択
    ↓
POST /ocr/image  →  { text: "..." }
    ↓
POST /words/extract  →  { unknown_words: [...] }
    ↓
POST /words/batch   →  単語帳に一括登録
    ↓
GET  /words/{user_id}?is_learned=false  →  未学習単語一覧表示
```

---

## master_words テーブルの seed

レベル別単語リスト（tier 1〜6）を CSV でインポートするか、
Supabase の Table Editor から手動登録できます。

```sql
-- 例
INSERT INTO master_words (word, tier) VALUES
  ('apple', 1), ('run', 1), ('beautiful', 2), ('negotiate', 4);
```
