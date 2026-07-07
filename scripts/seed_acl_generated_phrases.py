"""
外部生成AIで作った Academic Collocation List の語義CSVをDBに投入する。

入力:
    phrase_list/output_examples_final.csv

使い方:
    cd vocamine_backend
    .venv/bin/python scripts/seed_acl_generated_phrases.py
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "phrase_list" / "output_examples_final.csv"

BATCH_SIZE = 50
SLEEP_BETWEEN_BATCHES = 0.2

load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def upsert_with_retry(fn, retries=3, wait=2.0):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i < retries - 1:
                print(f"    リトライ {i + 1}/{retries}: {e}")
                time.sleep(wait)
            else:
                raise


def normalize_phrase(value: str) -> str:
    return " ".join(value.strip().lower().split())


def main():
    if not INPUT_PATH.exists():
        raise SystemExit(f"入力CSVが見つかりません: {INPUT_PATH}")

    db = get_client()
    word_cache: dict[str, int] = {}
    total_meanings = 0
    total_examples = 0
    skipped = 0

    with INPUT_PATH.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    print(f"対象: {len(rows)} 件")

    for index, row in enumerate(rows, start=1):
        phrase = normalize_phrase(row.get("phrase") or "")
        meaning_ja = (row.get("meaning_ja") or "").strip()
        example = (row.get("example_sentence") or "").strip()
        translated = (row.get("translated_sentence") or "").strip()
        source_url = (row.get("source_url") or "").strip()
        error = (row.get("error") or "").strip()
        deepl_filled_fields = (row.get("deepl_filled_fields") or "").strip()

        if not phrase or not meaning_ja:
            skipped += 1
            continue

        if index % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN_BATCHES)
            print(f"  {index}/{len(rows)}件...", end="\r")

        if phrase not in word_cache:
            word_res = upsert_with_retry(
                lambda p=phrase: (
                    db.table("words")
                    .upsert({"word": p}, on_conflict="word")
                    .execute()
                )
            )
            word_cache[phrase] = word_res.data[0]["id"]

        word_id = word_cache[phrase]
        metadata = {
            "acl_generated": {
                "source_url": source_url or None,
                "error": error or None,
                "deepl_filled_fields": deepl_filled_fields or None,
            }
        }
        meaning_res = upsert_with_retry(
            lambda wid=word_id, d=meaning_ja, meta=metadata: (
                db.table("meanings").upsert(
                    {
                        "word_id": wid,
                        "part_of_speech": "phrase",
                        "definition_en": None,
                        "definition_ja": d,
                        "source": "academic_collocation_list",
                        "inflections": meta,
                        "tier": None,
                    },
                    on_conflict="word_id,part_of_speech,definition_ja",
                ).execute()
            )
        )
        meaning_id = meaning_res.data[0]["id"]
        total_meanings += 1

        if example:
            existing_example = upsert_with_retry(
                lambda mid=meaning_id, s=example: (
                    db.table("example_sentences")
                    .select("id")
                    .eq("meaning_id", mid)
                    .eq("sentence", s)
                    .limit(1)
                    .execute()
                )
            )
            if not existing_example.data:
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

    print(
        f"\n完了: words={len(word_cache)}, meanings={total_meanings}, "
        f"examples={total_examples}, skipped={skipped}"
    )


if __name__ == "__main__":
    main()
