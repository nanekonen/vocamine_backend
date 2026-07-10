"""
既存の wordbook_word_sources.source_type='initial_level' を、
実際のCEFR tier（A1/A2/B1/B2）別の source_type に振り分け直す。

対象: source_type='initial_level' の wordbook_word_sources 行
tier 1〜4 のみ対応（5,6は現状CEFR-Jファイルが無いため対象外・スキップ）

使い方:
    cd vocamine_backend
    .venv/bin/python scripts/backfill_initial_level_source_types.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parents[1]
BATCH_SIZE = 500

load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TIER_SOURCE_TYPES = {
    1: "initial_level_a1",
    2: "initial_level_a2",
    3: "initial_level_b1",
    4: "initial_level_b2",
}


def get_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def fetch_all(db, table: str, select: str, filters=None) -> list[dict]:
    filters = filters or {}
    rows: list[dict] = []
    start = 0
    while True:
        query = db.table(table).select(select)
        for column, value in filters.items():
            query = query.eq(column, value)
        response = query.range(start, start + BATCH_SIZE - 1).execute()
        batch = response.data or []
        rows.extend(batch)
        if len(batch) < BATCH_SIZE:
            break
        start += BATCH_SIZE
    return rows


def fetch_by_ids(db, table: str, select: str, ids: list, id_column: str = "id") -> list[dict]:
    rows: list[dict] = []
    ids = list(ids)
    for index in range(0, len(ids), BATCH_SIZE):
        chunk = ids[index:index + BATCH_SIZE]
        response = db.table(table).select(select).in_(id_column, chunk).execute()
        rows.extend(response.data or [])
    return rows


def main() -> None:
    db = get_client()

    sources = fetch_all(
        db,
        "wordbook_word_sources",
        "id, wordbook_word_id",
        filters={"source_type": "initial_level"},
    )
    print(f"対象ソース: {len(sources)} 件")
    if not sources:
        return

    wordbook_word_ids = list({s["wordbook_word_id"] for s in sources})
    wordbook_words = fetch_by_ids(
        db, "wordbook_words", "id, meaning_id", wordbook_word_ids
    )
    meaning_id_by_wordbook_word_id = {
        w["id"]: w["meaning_id"] for w in wordbook_words
    }

    meaning_ids = list({v for v in meaning_id_by_wordbook_word_id.values() if v is not None})
    meanings = fetch_by_ids(db, "meanings", "id, tier", meaning_ids)
    tier_by_meaning_id = {m["id"]: m.get("tier") for m in meanings}

    updated = 0
    skipped = 0
    for i, source in enumerate(sources, start=1):
        wordbook_word_id = source["wordbook_word_id"]
        meaning_id = meaning_id_by_wordbook_word_id.get(wordbook_word_id)
        tier = tier_by_meaning_id.get(meaning_id) if meaning_id is not None else None
        source_type = TIER_SOURCE_TYPES.get(tier)
        if not source_type:
            skipped += 1
            continue
        db.table("wordbook_word_sources").update(
            {"source_type": source_type}
        ).eq("id", source["id"]).execute()
        updated += 1
        if i % 50 == 0:
            print(f"  {i}/{len(sources)}件...", end="\r")

    print(f"\n完了: updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
