from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.db.supabase import get_supabase
from app.schemas.schemas import (
    WordResponse,
    WordbookWordCreate, WordbookWordUpdate, WordbookWordResponse,
    ExtractWordsRequest, ExtractWordsResponse,
    BatchWordsRequest, BatchWordsResponse, BatchWordResult,
)
from app.services.word_service import extract_unknown_words
from app.services.word_lookup_service import (
    get_or_create_word,
    get_word_with_meanings,
    add_meanings_to_wordbook,
)

router = APIRouter(prefix="/words", tags=["Words"])


# ── Word 登録（Free Dictionary API から意味取得） ────────────────────────────

@router.post("/", response_model=WordResponse, status_code=201)
async def add_word(word: str):
    """
    単語をDBに登録する。
    Free Dictionary API から意味・品詞・例文を取得して meanings / example_sentences に保存。
    すでに存在する場合はそのまま返す。
    """
    result = await get_or_create_word(word)
    return get_word_with_meanings(result["word_id"])


# ── 未知単語の一括登録（OCR結果 → 単語帳へ） ──────────────────────────────────

@router.post("/batch", response_model=BatchWordsResponse, status_code=201)
async def add_words_batch(payload: BatchWordsRequest):
    """
    未知単語のリストを受け取り、それぞれ:
      1. words/meanings テーブルに存在しなければ Free Dictionary API から取得して登録
      2. 取得できた meaning を user_id の単語帳（未学習）に追加
    フレーズ（スペースを含む語）は Free Dictionary API では引けないことが多いため、
    意味が見つからなかった単語も results に含め、呼び出し側で判断できるようにする。
    """
    results: list[BatchWordResult] = []
    registered_count = 0
    skipped_no_meaning_count = 0

    # 重複を除去しつつ順序を保持
    seen: set[str] = set()
    unique_words = []
    for w in payload.words:
        lower = w.strip().lower()
        if lower and lower not in seen:
            seen.add(lower)
            unique_words.append(lower)

    for word in unique_words:
        try:
            lookup = await get_or_create_word(word)
        except Exception as e:
            results.append(BatchWordResult(
                word=word, status="error", meaning_count=0, error=str(e),
            ))
            continue

        if lookup["meaning_count"] == 0:
            skipped_no_meaning_count += 1
            results.append(BatchWordResult(
                word=word, status="no_meaning_found", meaning_count=0,
            ))
            continue

        added_ids = await add_meanings_to_wordbook(payload.user_id, lookup["word_id"])
        registered_count += 1
        results.append(BatchWordResult(
            word=word,
            status="registered" if added_ids else "already_in_wordbook",
            meaning_count=lookup["meaning_count"],
        ))

    return BatchWordsResponse(
        total=len(unique_words),
        registered_count=registered_count,
        skipped_no_meaning_count=skipped_no_meaning_count,
        results=results,
    )


def _get_word_with_meanings(word_id: int) -> dict:
    return get_word_with_meanings(word_id)


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