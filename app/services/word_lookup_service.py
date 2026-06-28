from __future__ import annotations
from app.db.supabase import get_supabase
from app.services.dictionary_service import fetch_word_meanings


async def get_or_create_word(word: str) -> dict:
    """
    単語をDBに登録する（存在しなければFree Dictionary APIから意味を取得して保存）。
    既存の場合はそのまま words 行を返す。

    戻り値: {"word_id": int, "created": bool, "meaning_count": int}
    """
    db = get_supabase()
    word_lower = word.strip().lower()

    existing = db.table("words").select("id").eq("word", word_lower).execute()
    if existing.data:
        word_id = existing.data[0]["id"]
        meaning_count = (
            db.table("meanings")
            .select("id", count="exact")
            .eq("word_id", word_id)
            .execute()
            .count
        ) or 0
        return {"word_id": word_id, "created": False, "meaning_count": meaning_count}

    inserted = db.table("words").insert({"word": word_lower}).execute()
    word_id = inserted.data[0]["id"]

    meanings = await fetch_word_meanings(word_lower)
    for m in meanings:
        meaning_row = db.table("meanings").insert({
            "word_id": word_id,
            "part_of_speech": m["part_of_speech"],
            "definition": m["definition"],
            "tier": None,  # ユーザー追加単語はtierなし
        }).execute()
        meaning_id = meaning_row.data[0]["id"]

        if m.get("example"):
            db.table("example_sentences").insert({
                "meaning_id": meaning_id,
                "sentence": m["example"],
            }).execute()

    return {"word_id": word_id, "created": True, "meaning_count": len(meanings)}


def get_word_with_meanings(word_id: int) -> dict:
    db = get_supabase()
    word_row = db.table("words").select("*").eq("id", word_id).execute().data[0]
    meanings = (
        db.table("meanings")
        .select("*, example_sentences(*)")
        .eq("word_id", word_id)
        .execute()
        .data
    )
    word_row["meanings"] = meanings
    return word_row


async def add_meanings_to_wordbook(user_id: str, word_id: int) -> list[int]:
    """
    指定した word_id の全 meaning を、ユーザーの単語帳に「未学習」として追加する。
    すでに登録済みの meaning はスキップ。
    戻り値: 追加された wordbook_words の id のリスト
    """
    db = get_supabase()
    meanings = db.table("meanings").select("id").eq("word_id", word_id).execute().data
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