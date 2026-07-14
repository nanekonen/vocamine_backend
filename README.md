# Glossalyze Backend

Glossalyzeの教材、OCR、語彙解析、単語帳、学習状況、ユーザー情報を管理するFastAPIバックエンドです。

## 現在の構成

- FastAPI
- Supabase（PostgreSQL・ユーザー情報・単語帳データ）
- Oracle Object StorageのS3互換API（教材原本、PDF、サムネイル、OCR位置情報）
- spaCy `en_core_web_sm`（英語の語彙・品詞解析）
- Wiktionary / wiktextract（英語語義）
- Gemini API（日本語語義の生成・補完）
- Azure Document Intelligence（主OCR）
- Tesseract（ローカルOCRフォールバック）
- Poppler（PDFの画像化）

Google Cloud Visionは使用していません。Piperによるサーバー側発音生成とONNX Runtimeも無効化しています。Web版の発音はブラウザの音声合成を使用します。

## OCRの処理順

画像とPDFはどちらも次の順で処理します。

1. Azure Document Intelligence
2. Azureが未設定、制限超過、または失敗した場合はTesseract

PDFはPopplerの`pdftoppm`でページ画像を生成します。問題のあるPDFはGhostscriptがインストールされていれば修復を試します。PDFに抽出可能なテキストレイヤーがある場合は、それもフォールバックとして使用します。

- 画像: JPEG、PNG、WebP、GIF、最大10 MB
- PDF: 最大50 MB
- PDFプレビュー/OCR: 最大50ページ
- Tesseract: `jpn`データがあれば`jpn+eng`、なければ`eng`

OCR結果と位置情報は教材登録時に保存され、教材表示のたびに再OCRは行いません。

## セットアップ

### 1. システム依存関係

macOS（Homebrew）の例：

```bash
brew install poppler tesseract tesseract-lang ghostscript
```

Ubuntu/Debianの例：

```bash
sudo apt-get update
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn ghostscript
```

Azureだけを使う場合でも、障害時のフォールバックとPDFプレビュー生成のため、PopplerとTesseractの導入を推奨します。Ghostscriptは任意です。

### 2. Python環境

```bash
cd vocamine_backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m nltk.downloader -d .venv/nltk_data brown
```

`en_core_web_sm`は`requirements.txt`からインストールされるため、別途`python -m spacy download`を実行する必要はありません。

### 3. 環境変数

```bash
cp .env.example .env
```

`.env`を編集します。

| 変数 | 必須 | 説明 |
|---|---:|---|
| `SUPABASE_URL` | Yes | SupabaseプロジェクトURL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | バックエンド専用のService Role Key。フロントへ公開しないこと |
| `ORACLE_OBJECT_STORAGE_NAMESPACE` | 教材機能 | Oracle Object StorageのNamespace |
| `ORACLE_OBJECT_STORAGE_REGION` | 教材機能 | リージョン（例: `ap-osaka-1`） |
| `ORACLE_OBJECT_STORAGE_ACCESS_KEY_ID` | 教材機能 | Customer Secret KeyのAccess Key |
| `ORACLE_OBJECT_STORAGE_SECRET_ACCESS_KEY` | 教材機能 | Customer Secret Key |
| `ORACLE_OBJECT_STORAGE_BUCKET` | 教材機能 | 保存先バケット名 |
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | Optional | Azure Document IntelligenceのEndpoint。未設定時はTesseractへフォールバック |
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | Optional | Azure Document IntelligenceのAPI Key |
| `GEMINI_API_KEY` | 日本語語義生成 | Gemini API Key。未設定時は日本語訳の生成・補完不可 |
| `GEMINI_MODEL` | No | 使用モデル。既定値は`gemini-2.5-flash` |
| `WIKTIONARY_USER_AGENT` | No | Wiktionary MediaWiki API用User-Agent |
| `APP_ENV` | No | 実行環境名。既定値は`development` |
| `CORS_ORIGINS` | Yes | 許可するフロントエンドOriginのJSON配列 |

`.env.example`にある`SUPABASE_PUBLISHABLE_KEY`はフロントエンドのSupabase Auth用です。バックエンド本体はService Role Keyを使用します。

### 4. Supabase

新規環境ではSupabase SQL Editorで次を実行します。

```text
scripts/supabase_schema.sql
```

既存DBを更新するためのSQLは`scripts/migrate_*.sql`にあります。既に適用済みのマイグレーションを重ねて実行せず、対象DBの状態に応じたものだけを適用してください。

Supabase Authを使用するため、フロントで利用するメール認証やGoogle Provider、リダイレクトURLもSupabase Dashboard側で設定します。

### 5. 起動

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- APIドキュメント: `http://localhost:8000/docs`
- ヘルスチェック: `http://localhost:8000/health`
- APIベースURL: `http://localhost:8000/api/v1`

本番環境では`.env`をソース管理へ含めず、デプロイ先のSecret/Environment Variablesから設定してください。

## API概要

| Prefix | 主な役割 |
|---|---|
| `/api/v1/auth` | Supabaseアクセストークンからアプリセッションを解決 |
| `/api/v1/ocr` | 画像・PDFのOCR、ページ画像、単語位置情報の生成 |
| `/api/v1/materials` | 教材・教材フォルダ・原本・閲覧用PDFの管理 |
| `/api/v1/wordbooks` | 単語帳・フォルダ・教材ごとの既定単語帳の管理 |
| `/api/v1/words` | 語彙解析、意味検索、単語帳登録、学習状態、4択候補 |
| `/api/v1/users` | 初回レベル設定、ダッシュボード、直近教材、ユーザー情報 |

代表的な教材登録フロー：

```text
画像/PDFをOCR
  → 教材とOCR結果を保存
  → spaCyで単語・熟語と品詞を解析
  → 保存済み語義、Wiktionary、Geminiから意味を補完
  → 既知・未知・訳なしに分類
  → 選択したmeaning単位で単語帳へ登録
```

初回レベル設定ではユーザー情報を先に保存してレスポンスし、CEFR-J相当の既知語登録をバックグラウンドで続行します。

## データ投入・保守スクリプト

- `scripts/seed_cefr.py`: CEFR-J単語データの投入
- `scripts/seed_phrases.py`: 熟語データの投入
- `scripts/seed_acl_generated_phrases.py`: ACL由来熟語データの投入
- `scripts/backfill_pdf_previews.py`: 既存PDFプレビューの補完
- `scripts/backfill_initial_level_source_types.py`: 初期レベル登録データの補完
- `scripts/cleanup_orphan_words.py`: 孤立した単語データの整理
- `scripts/verify_gemini_call.py`: Gemini単体疎通確認
- `scripts/verify_japanese_definition.py`: Wiktionaryから日本語語義生成までの確認

各スクリプトの引数と前提条件は、ファイル先頭の説明を確認してから実行してください。
