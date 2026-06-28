from __future__ import annotations
import re
from typing import Optional
from app.db.supabase import get_supabase

# ユーザーレベル → CEFR-J tier の上限マッピング
LEVEL_TIER_MAP: dict[str, int] = {
    "中学卒業程度":   1,
    "高校卒業程度":   2,
    "英検3級":        1,
    "英検準2級":      2,
    "英検2級":        3,
    "英検準1級":      4,
    "英検1級":        5,
    "TOEIC 400点":    2,
    "TOEIC 600点":    3,
    "TOEIC 730点":    4,
    "TOEIC 860点":    5,
    "TOEIC 990点":    6,
}


def _tokenize_english(text: str) -> list[str]:
    """テキストから重複なしの英単語リストを返す（小文字化）"""
    words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text)
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        lower = w.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


async def get_learned_words(user_id: str) -> set[str]:
    """
    ユーザーの学習済み単語帳に含まれる単語セットを返す。
    wordbook_words(is_learned=true) → meanings → words と辿る。
    """
    db = get_supabase()
    response = (
        db.table("wordbook_words")
        .select("meanings(words(word))")
        .eq("user_id", user_id)
        .eq("is_learned", True)
        .execute()
    )
    learned: set[str] = set()
    for row in (response.data or []):
        word = row.get("meanings", {}).get("words", {}).get("word")
        if word:
            learned.add(word.lower())
    return learned


async def extract_unknown_words(text: str, user_id: str) -> dict:
    """
    テキスト中の英単語のうち、学習済み単語帳に存在しないものを返す。
    判定: wordbook_words に is_learned=true のエントリーがない単語 = 未知
    """
    all_words = _tokenize_english(text)
    total = len(all_words)

    if total == 0:
        return {"unknown_words": [], "total_words": 0, "unknown_count": 0}

    learned = await get_learned_words(user_id)
    unknown = [w for w in all_words if w not in learned]

    return {
        "unknown_words": unknown,
        "total_words": total,
        "unknown_count": len(unknown),
    }


async def bulk_register_cefr_words(user_id: str, level: str) -> int:
    """
    レベル設定時にCEFR-Jの単語（tier以下）をユーザーの学習済み単語帳に一括登録。
    すでに登録済みのものはスキップ（upsert）。
    """
    tier = LEVEL_TIER_MAP.get(level, 0)
    if tier == 0:
        return 0

    db = get_supabase()

    # 対象tierのmeaningを全取得
    meanings = (
        db.table("meanings")
        .select("id")
        .lte("tier", tier)
        .execute()
    )

    if not meanings.data:
        return 0

    rows = [
        {
            "user_id": user_id,
            "meaning_id": m["id"],
            "is_learned": True,
        }
        for m in meanings.data
    ]

    db.table("wordbook_words").upsert(rows, on_conflict="user_id,meaning_id").execute()
    return len(rows)