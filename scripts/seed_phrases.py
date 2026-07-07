"""
phrase_list seed スクリプト

使い方:
    cd vocamine_backend
    .venv/bin/python scripts/seed_phrases.py
"""

from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
PHRASE_LIST_DIR = ROOT / "phrase_list"
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
    phrase = value.strip().lower().replace("\ufeff", "")
    phrase = re.sub(r"\s+", " ", phrase)
    phrase = re.sub(r"\s+['’]s\b", "'s", phrase)
    return phrase.strip(" ,.;:")


def source_for_path(path: Path) -> str:
    if "PHaVE List" in path.name:
        return "phave_list"
    if "PHRASE List" in path.name:
        return "phrase_list"
    return "phrase_list"


def iter_entry_phrase_rows(path: Path):
    source = source_for_path(path)
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            phrase = normalize_phrase(row.get("entry") or "")
            meaning_ja = (row.get("meaning_ja") or row.get("meaning") or "").strip()
            ipa = (row.get("ipa") or "").strip() or None
            example = (row.get("example_sentence") or "").strip()
            translated = (row.get("translated_sentence") or "").strip()
            if phrase and " " in phrase and meaning_ja:
                yield {
                    "phrase": phrase,
                    "definition_en": None,
                    "definition_ja": meaning_ja,
                    "ipa": ipa,
                    "source": source,
                    "source_file": path.name,
                    "example": example,
                    "translated": translated,
                }


def iter_phrase_rows():
    for path in sorted(PHRASE_LIST_DIR.glob("*.csv")):
        if "Academic Collocation List" in path.name:
            print(f"\nスキップ（検出専用・意味なし）: {path.name}")
            continue
        print(f"\n読み込み: {path.name}")
        yield from iter_entry_phrase_rows(path)


def main():
    if not PHRASE_LIST_DIR.exists():
        raise SystemExit(f"phrase_list が見つかりません: {PHRASE_LIST_DIR}")

    db = get_client()
    word_cache: dict[str, int] = {}
    seen_phrases: set[str] = set()
    total_meanings = 0
    total_examples = 0
    skipped = 0

    rows = list(iter_phrase_rows())
    print(f"\n対象 phrase: {len(rows)} 件")

    for i, row in enumerate(rows, start=1):
        phrase = row["phrase"]
        if phrase in seen_phrases:
            skipped += 1
            continue
        seen_phrases.add(phrase)

        if i % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN_BATCHES)
            print(f"  {i}/{len(rows)}件...", end="\r")

        if phrase not in word_cache:
            word_res = upsert_with_retry(
                lambda p=phrase: db.table("words")
                .upsert({"word": p}, on_conflict="word")
                .execute()
            )
            word_cache[phrase] = word_res.data[0]["id"]

        word_id = word_cache[phrase]
        meaning_res = upsert_with_retry(
            lambda wid=word_id, row=row: (
                db.table("meanings").upsert(
                    {
                        "word_id": wid,
                        "part_of_speech": "phrase",
                        "definition_en": row["definition_en"],
                        "definition_ja": row["definition_ja"],
                        "ipa": row["ipa"],
                        "source": row["source"],
                        "inflections": {
                            "source_file": row["source_file"],
                        },
                        "tier": None,
                    },
                    on_conflict="word_id,part_of_speech,definition_ja",
                ).execute()
            )
        )
        meaning_id = meaning_res.data[0]["id"]
        total_meanings += 1

        if row["example"]:
            existing_example = upsert_with_retry(
                lambda mid=meaning_id, s=row["example"]: (
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
                    lambda mid=meaning_id, s=row["example"], ts=row["translated"]: (
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
