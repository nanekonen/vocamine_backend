from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from app.db.supabase import get_supabase
from app.services.word_service import (
    _meaning_keys_with_japanese,
    extract_unknown_words,
    get_learned_lexical_items,
    pos_matches,
)

_user_refresh_locks: dict[str, asyncio.Lock] = {}


def _compact_items(items: list[dict]) -> list[dict]:
    return [
        {
            "text": str(item.get("text") or "").strip().lower().replace("’", "'"),
            "part_of_speech": str(item.get("part_of_speech") or "").strip().lower(),
            "part_of_speech_detail": item.get("part_of_speech_detail"),
            "kind": item.get("kind") or "word",
        }
        for item in items
        if item.get("text") and item.get("part_of_speech")
    ]


def _summary_for_items(
    items: list[dict],
    user_id: str,
    *,
    learned: Optional[set[tuple[str, str]]] = None,
) -> dict:
    learned_items = learned if learned is not None else get_learned_lexical_items(user_id)
    translated_keys = _meaning_keys_with_japanese(items)
    known_count = 0
    unknown_count = 0
    untranslated_count = 0

    for item in items:
        word = str(item.get("text") or "").strip().lower().replace("’", "'")
        material_pos = str(item.get("part_of_speech") or "").strip().lower()
        if not word or not material_pos:
            continue
        is_learned = any(
            learned_word == word and pos_matches(material_pos, learned_pos)
            for learned_word, learned_pos in learned_items
        )
        has_japanese = (word, material_pos) in translated_keys
        if is_learned:
            known_count += 1
        elif has_japanese:
            unknown_count += 1
        else:
            untranslated_count += 1

    total_words = known_count + unknown_count
    coverage_rate = round(known_count / total_words, 4) if total_words else 0.0
    return {
        "total_words": total_words,
        "known_count": known_count,
        "unknown_count": unknown_count,
        "untranslated_count": untranslated_count,
        "coverage_rate": coverage_rate,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def refresh_material_analysis(
    material_id: str,
    user_id: str,
    *,
    reparse_text: bool = False,
    learned: Optional[set[tuple[str, str]]] = None,
) -> Optional[dict]:
    db = get_supabase()
    material = (
        db.table("materials")
        .select("id, user_id, extracted_text, analysis_items")
        .eq("id", material_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
        .data
    )
    if not material:
        return None

    cached_items = material.get("analysis_items")
    if reparse_text or not isinstance(cached_items, list):
        result = await extract_unknown_words(
            str(material.get("extracted_text") or ""),
            user_id,
            enrich_meanings=False,
        )
        items = result.get("items") or []
    else:
        items = cached_items

    summary = await asyncio.to_thread(
        _summary_for_items,
        items,
        user_id,
        learned=learned,
    )
    updates = {"analysis_summary": summary}
    if reparse_text or not isinstance(cached_items, list):
        updates["analysis_items"] = _compact_items(items)
    db.table("materials").update(updates).eq(
        "id", material_id
    ).eq("user_id", user_id).execute()
    return summary


async def refresh_user_material_analyses(user_id: str) -> None:
    lock = _user_refresh_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        db = get_supabase()
        rows = (
            db.table("materials")
            .select("id")
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
        learned = await asyncio.to_thread(get_learned_lexical_items, user_id)
        for row in rows:
            try:
                await refresh_material_analysis(
                    str(row["id"]),
                    user_id,
                    learned=learned,
                )
            except Exception as exc:
                print(
                    f"[material-analysis] refresh failed for {row['id']}: {exc!r}"
                )
