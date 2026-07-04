"""
CEFR-J Wordlist seed スクリプト

使い方:
    1. CSVファイルを vocamine_backend/cefr-j_wordlist/ に配置
       - CEFR-J_A1.csv / CEFR-J_A2.csv / CEFR-J_B1.csv / CEFR-J_B2.csv
    2. cd vocamine_backend
    3. source .venv/bin/activate
    4. python scripts/seed_cefr.py
"""

import csv
import os
import time
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

WORDLIST_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cefr-j_wordlist")

CSV_FILES = [
    ("CEFR-J_A1.csv", 1),
    ("CEFR-J_A2.csv", 2),
    ("CEFR-J_B1.csv", 3),
    ("CEFR-J_B2.csv", 4),
]

BATCH_SIZE = 50
SLEEP_BETWEEN_BATCHES = 0.3

# CEFR-J の pos 表記 → DB ENUM 値
POS_MAP = {
    "noun":         "noun",
    "verb":         "verb",
    "adjective":    "adjective",
    "adverb":       "adverb",
    "pronoun":      "pronoun",
    "preposition":  "preposition",
    "conjunction":  "conjunction",
    "interjection": "interjection",
    "determiner":   "determiner",
    "article":      "article",
    "numeral":      "numeral",
    "prefix":       "prefix",
    "suffix":       "suffix",
    "phrase":       "phrase",
    "abbreviation": "abbreviation",
}


def normalize_pos(pos: str) -> str | None:
    return POS_MAP.get(pos.lower().strip())


def get_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def upsert_with_retry(fn, retries=3, wait=2.0):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i < retries - 1:
                print(f"    リトライ {i+1}/{retries}: {e}")
                time.sleep(wait)
            else:
                raise


def main():
    word_cache: dict[str, int] = {}
    total_meanings = 0
    total_examples = 0
    skipped = 0

    for filename, tier in CSV_FILES:
        path = os.path.join(WORDLIST_DIR, filename)
        if not os.path.exists(path):
            print(f"スキップ（ファイルなし）: {filename}")
            continue

        print(f"\n処理中: {filename} (tier={tier})")

        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        print(f"  {len(rows)} 件のエントリ")

        for i, row in enumerate(rows):
            headword  = row["headword"].strip().lower()
            pos_raw   = row["pos"].strip()
            definition = row["meaning_ja"].strip()
            ipa       = row.get("ipa", "").strip() or None
            example   = row.get("example_sentence", "").strip()
            translated = row.get("translated_sentence", "").strip()

            pos = normalize_pos(pos_raw)
            if not headword or not pos or not definition:
                skipped += 1
                continue

            if i % BATCH_SIZE == 0:
                db = get_client()
                if i > 0:
                    time.sleep(SLEEP_BETWEEN_BATCHES)
                print(f"  {i}/{len(rows)}件...", end="\r")

            # words
            if headword not in word_cache:
                res = upsert_with_retry(
                    lambda h=headword: db.table("words")
                        .upsert({"word": h}, on_conflict="word")
                        .execute()
                )
                word_cache[headword] = res.data[0]["id"]

            word_id = word_cache[headword]

            # meanings
            meaning_res = upsert_with_retry(
                lambda wid=word_id, p=pos, d=definition, ip=ipa, t=tier: (
                    db.table("meanings").upsert(
                        {
                            "word_id": wid,
                            "part_of_speech": p,
                            "definition": d,
                            "definition_ja": d,
                            "ipa": ip,
                            "source": "cefr_j",
                            "tier": t,
                        },
                        on_conflict="word_id,part_of_speech,definition"
                    ).execute()
                )
            )
            meaning_id = meaning_res.data[0]["id"]
            total_meanings += 1

            # example_sentences
            if example:
                upsert_with_retry(
                    lambda mid=meaning_id, s=example, ts=translated: (
                        db.table("example_sentences").insert(
                            {
                                "meaning_id": mid,
                                "sentence": s,
                                "translated_sentence": ts or None,
                            }
                        ).execute()
                    )
                )
                total_examples += 1

        print(f"  {len(rows)}/{len(rows)}件... 完了")

    print(f"\n全完了: words={len(word_cache)}, meanings={total_meanings}, examples={total_examples}, skipped={skipped}")


if __name__ == "__main__":
    main()