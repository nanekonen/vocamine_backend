"""
PHaVE List / PHRASE List 由来の既存 phrase meaning の source を直す。

先に Supabase SQL editor で scripts/migrate_meaning_sources.sql を実行し、
dictionary_source enum に phave_list / phrase_list を追加しておく。
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parents[1]
PHRASE_LIST_DIR = ROOT / "phrase_list"
BATCH_SLEEP = 0.05

load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def normalize_phrase(value: str) -> str:
    phrase = value.strip().lower().replace("\ufeff", "")
    phrase = re.sub(r"\s+", " ", phrase)
    phrase = re.sub(r"\s+['’]s\b", "'s", phrase)
    return phrase.strip(" ,.;:")


def source_for_path(path: Path) -> str | None:
    if "PHaVE List" in path.name:
        return "phave_list"
    if "PHRASE List" in path.name:
        return "phrase_list"
    return None


def phrase_sources() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(PHRASE_LIST_DIR.glob("*.csv")):
        source = source_for_path(path)
        if not source:
            continue
        with path.open(newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                phrase = normalize_phrase(row.get("entry") or "")
                if phrase and " " in phrase:
                    result[phrase] = source
    return result


def main() -> None:
    db = get_client()
    sources = phrase_sources()
    updated = 0
    skipped = 0

    for index, (phrase, source) in enumerate(sources.items(), start=1):
        word_res = (
            db.table("words")
            .select("id")
            .eq("word", phrase)
            .limit(1)
            .execute()
        )
        if not word_res.data:
            skipped += 1
            continue

        word_id = word_res.data[0]["id"]
        db.table("meanings").update({"source": source}).eq("word_id", word_id).eq(
            "part_of_speech",
            "phrase",
        ).in_("source", ["manual", "phrase_list", "phave_list"]).execute()
        updated += 1

        if index % 50 == 0:
            print(f"{index}/{len(sources)} updated={updated} skipped={skipped}", end="\r")
            time.sleep(BATCH_SLEEP)

    print(f"\nupdated={updated} skipped={skipped}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise
