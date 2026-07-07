"""
meanings を持たない words を削除するスクリプト。

使い方:
    cd vocamine_backend
    .venv/bin/python scripts/cleanup_orphan_words.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
BATCH_SIZE = 500
DELETE_CHUNK_SIZE = 100

load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def fetch_all(db, table: str, select: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        response = (
            db.table(table)
            .select(select)
            .range(start, start + BATCH_SIZE - 1)
            .execute()
        )
        batch = response.data or []
        rows.extend(batch)
        if len(batch) < BATCH_SIZE:
            break
        start += BATCH_SIZE
    return rows


def main():
    db = get_client()
    words = fetch_all(db, "words", "id,word")
    meanings = fetch_all(db, "meanings", "word_id")
    used_word_ids = {
        row["word_id"]
        for row in meanings
        if row.get("word_id") is not None
    }
    orphan_ids = [
        row["id"]
        for row in words
        if row["id"] not in used_word_ids
    ]

    print(f"words={len(words)} used={len(used_word_ids)} orphans={len(orphan_ids)}")
    for index in range(0, len(orphan_ids), DELETE_CHUNK_SIZE):
        chunk = orphan_ids[index:index + DELETE_CHUNK_SIZE]
        db.table("words").delete().in_("id", chunk).execute()
        print(f"deleted {min(index + DELETE_CHUNK_SIZE, len(orphan_ids))}/{len(orphan_ids)}", end="\r")
        time.sleep(0.05)
    print("\n完了")


if __name__ == "__main__":
    main()
