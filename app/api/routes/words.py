from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.db.supabase import get_supabase
from app.schemas.schemas import (
    WordResponse,
    WordbookWordCreate, WordbookWordUpdate, WordbookWordResponse,
    ExtractWordsRequest, ExtractWordsResponse,
)
from app.services.word_service import extract_unknown_words
from app.services.dictionary_service import fetch_word_meanings

router = APIRouter(prefix="/words", tags=["Words"])


# ── Word 登録（Free Dictionary API から意味取得） ────────────────────────────

@router.post("/", response_model=WordResponse, status_code=201)
async def add_word(word: str):
    """
    単語をDBに登録する。
    Free Dictionary API から意味・品詞・例文を取得して meanings / example_sentences に保存。
    すでに存在する場合はそのまま返す。
    """
    db = get_supabase()
    word_lower = word.lower()

    # 既存チェック
    existing = db.table("words").select("id").eq("word", word_lower).execute()
    if existing.data:
        word_id = existing.data[0]["id"]
    else:
        # words に insert
        inserted = db.table("words").insert({"word": word_lower}).execute()
        word_id = inserted.data[0]["id"]

        # Free Dictionary API から意味取得
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

    # 結果を返す（meanings + example_sentences を結合）
    return _get_word_with_meanings(word_id)


def _get_word_with_meanings(word_id: int) -> dict:
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


# ── Wordbook CRUD ─────────────────────────────────────────────────────────────

@router.get("/wordbook/{user_id}", response_model=list[WordbookWordResponse])
async def get_wordbook(
    user_id: str,
    is_learned: Optional[bool] = Query(default=None),
):
    """ユーザーの単語帳を取得（is_learnedでフィルタ可能）"""
    db = get_supabase()
    query = (
        db.table("wordbook_words")
        .select("*, meanings(*, words(*), example_sentences(*))")
        .eq("user_id", user_id)
    )
    if is_learned is not None:
        query = query.eq("is_learned", is_learned)
    response = query.order("created_at", desc=True).execute()
    return response.data or []


@router.post("/wordbook", response_model=WordbookWordResponse, status_code=201)
async def add_to_wordbook(user_id: str, payload: WordbookWordCreate):
    """meaning_idを単語帳（未学習）に追加する"""
    db = get_supabase()
    existing = (
        db.table("wordbook_words")
        .select("id")
        .eq("user_id", user_id)
        .eq("meaning_id", payload.meaning_id)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Already in wordbook.")

    response = db.table("wordbook_words").insert({
        "user_id": user_id,
        "meaning_id": payload.meaning_id,
        "is_learned": False,
    }).execute()
    return response.data[0]


@router.patch("/wordbook/{entry_id}", response_model=WordbookWordResponse)
async def update_wordbook_entry(entry_id: int, payload: WordbookWordUpdate):
    """学習済み/未学習を切り替える"""
    db = get_supabase()
    response = (
        db.table("wordbook_words")
        .update({"is_learned": payload.is_learned})
        .eq("id", entry_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Entry not found.")
    return response.data[0]


@router.delete("/wordbook/{entry_id}", status_code=204)
async def delete_wordbook_entry(entry_id: int):
    """単語帳からエントリーを削除する"""
    db = get_supabase()
    db.table("wordbook_words").delete().eq("id", entry_id).execute()


# ── 未知単語抽出 ──────────────────────────────────────────────────────────────

@router.post("/extract", response_model=ExtractWordsResponse)
async def extract_words(payload: ExtractWordsRequest):
    """
    テキストから未知単語を抽出する。
    学習済み単語帳（is_learned=true）に存在しない単語を返す。
    """
    result = await extract_unknown_words(
        text=payload.text,
        user_id=payload.user_id,
    )
    return ExtractWordsResponse(**result)