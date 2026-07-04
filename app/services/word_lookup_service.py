from __future__ import annotations
from typing import Optional
from app.db.supabase import get_supabase
from app.services.dictionary_service import (
    fetch_wiktionary_meanings,
    generate_japanese_definition,
    normalize_part_of_speech,
)


async def get_or_create_word(word: str, part_of_speech: Optional[str] = None, enrich_meanings: bool = False) -> dict:
    """
    単語をDBに登録する。
    必要なら Wiktionary 由来の meanings を同一品詞に限定して補完する。

    戻り値: {"word_id": int, "created": bool, "meaning_count": int}
    """
    db = get_supabase()
    word_lower = word.strip().lower()
    normalized_pos = normalize_part_of_speech(part_of_speech)

    existing = db.table("words").select("id").eq("word", word_lower).execute()
    if existing.data:
        word_id = existing.data[0]["id"]
        if enrich_meanings:
            await ensure_meanings_for_word(word_id, word_lower, normalized_pos)
        return {
            "word_id": word_id,
            "created": False,
            "meaning_count": _count_meanings(word_id, normalized_pos),
        }

    inserted = db.table("words").insert({"word": word_lower}).execute()
    word_id = inserted.data[0]["id"]

    if enrich_meanings:
        await ensure_meanings_for_word(word_id, word_lower, normalized_pos)
    return {
        "word_id": word_id,
        "created": True,
        "meaning_count": _count_meanings(word_id, normalized_pos),
    }


def _count_meanings(word_id: int, part_of_speech: Optional[str] = None) -> int:
    db = get_supabase()
    query = db.table("meanings").select("id", count="exact").eq("word_id", word_id)
    if part_of_speech:
        query = query.eq("part_of_speech", part_of_speech)
    return query.execute().count or 0


async def ensure_meanings_for_word(word_id: int, word: str, part_of_speech: Optional[str] = None) -> int:
    """
    meanings に同一品詞の語義がなければ Wiktionary から取得し、必要なら日本語語義を生成して保存する。
    戻り値は追加後の同一品詞 meaning 数。
    """
    if part_of_speech and part_of_speech != "verb" and _count_meanings(word_id, part_of_speech) > 0:
        return _count_meanings(word_id, part_of_speech)

    db = get_supabase()
    fetched = await fetch_wiktionary_meanings(word, part_of_speech)
    for item in fetched:
        transitivity = item.get("transitivity")
        existing_query = (
            db.table("meanings")
            .select("id")
            .eq("word_id", word_id)
            .eq("part_of_speech", item["part_of_speech"])
            .eq("definition", item["definition"])
        )
        if transitivity:
            existing_query = existing_query.eq("transitivity", transitivity)
        existing_meaning = existing_query.execute().data or []

        definition_ja = await generate_japanese_definition(
            word,
            item["part_of_speech"],
            item,
        )
        meaning_res = db.table("meanings").upsert(
            {
                "word_id": word_id,
                "part_of_speech": item["part_of_speech"],
                "definition": item["definition"],
                "definition_ja": definition_ja,
                "transitivity": transitivity,
                "source": "wiktionary",
                "inflections": {"wiktionary_raw": item.get("raw", {})},
                "tier": None,
            },
            on_conflict="word_id,part_of_speech,definition",
        ).execute()

        if item.get("examples") and meaning_res.data and not existing_meaning:
            meaning_id = meaning_res.data[0]["id"]
            for sentence in item["examples"][:3]:
                db.table("example_sentences").insert({
                    "meaning_id": meaning_id,
                    "sentence": sentence,
                }).execute()

    return _count_meanings(word_id, part_of_speech)


def get_word_with_meanings(word_id: int) -> dict:
    db = get_supabase()
    word_row = db.table("words").select("*").eq("id", word_id).execute()
    if not word_row.data:
        return {}
    result = word_row.data[0]
    meanings = (
        db.table("meanings")
        .select("*, example_sentences(*)")
        .eq("word_id", word_id)
        .execute()
        .data or []
    )
    result["meanings"] = meanings
    return result


async def add_meanings_to_wordbook(user_id: str, word_id: int, part_of_speech: Optional[str] = None) -> list[int]:
    """
    指定した word_id の全 meaning を、ユーザーの単語帳に「未学習」として追加する。
    すでに登録済みの meaning はスキップ。
    戻り値: 追加された wordbook_words の id のリスト
    """
    db = get_supabase()
    query = db.table("meanings").select("id").eq("word_id", word_id)
    normalized_pos = normalize_part_of_speech(part_of_speech)
    if normalized_pos:
        query = query.eq("part_of_speech", normalized_pos)
    meanings = query.execute().data
    if not meanings:
        return []

    meaning_ids = [m["id"] for m in meanings]
    existing = (
        db.table("wordbook_words")
        .select("meaning_id")
        .eq("user_id", user_id)
        .in_("meaning_id", meaning_ids)
        .execute()
        .data
    )
    existing_ids = {row["meaning_id"] for row in existing}

    rows_to_insert = [
        {"user_id": user_id, "meaning_id": mid, "is_learned": False}
        for mid in meaning_ids
        if mid not in existing_ids
    ]
    if not rows_to_insert:
        return []

    inserted = db.table("wordbook_words").insert(rows_to_insert).execute()
    return [row["id"] for row in inserted.data]
