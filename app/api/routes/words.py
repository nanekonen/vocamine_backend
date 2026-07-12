from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from app.db.supabase import get_supabase
from app.schemas.schemas import (
    WordResponse, MeaningResponse,
    WordbookWordCreate, WordbookWordUpdate, WordbookWordResponse,
    ExtractWordsRequest, ExtractWordsResponse,
    StoredMeaningsRequest,
    BatchWordsRequest, BatchWordsResponse, BatchWordResult,
)
from app.services.word_service import enrich_lexical_items, extract_unknown_words
from app.services.word_lookup_service import (
    get_or_create_word,
    get_word_with_meanings,
    add_meanings_to_wordbook,
    record_wordbook_source,
)
from app.services.dictionary_service import normalize_part_of_speech
from app.services.user_identity_service import resolve_user_id
from app.services.pronunciation_service import get_pronunciation_audio

router = APIRouter(prefix="/words", tags=["Words"])


def add_entries_to_independent_wordbook(
    user_id: str,
    wordbook_id: Optional[str],
    wordbook_word_ids: list[int],
) -> None:
    if not wordbook_id or not wordbook_word_ids:
        return
    db = get_supabase()
    owned = db.table("wordbooks").select("id").eq("id", wordbook_id).eq(
        "user_id", user_id
    ).maybe_single().execute().data
    if not owned:
        raise HTTPException(status_code=404, detail="Wordbook not found.")
    db.table("wordbook_memberships").upsert(
        [
            {"wordbook_id": wordbook_id, "wordbook_word_id": entry_id}
            for entry_id in wordbook_word_ids
        ],
        on_conflict="wordbook_id,wordbook_word_id",
    ).execute()


# ── Word 登録 ─────────────────────────────────────────────────────────────────

@router.post("/", response_model=WordResponse, status_code=201)
async def add_word(word: str):
    """
    単語をwordsテーブルに登録する。
    必要に応じて Wiktionary 由来の meaning も補完する。
    すでに存在する場合はそのまま返す。
    """
    result = await get_or_create_word(word, enrich_meanings=True)
    return get_word_with_meanings(result["word_id"])


@router.get("/lookup", response_model=WordResponse)
async def lookup_word(
    word: str = Query(..., min_length=1),
    part_of_speech: Optional[str] = Query(default=None),
    enrich_meanings: bool = Query(default=True),
):
    """
    単語・熟語と品詞から meanings を取得する。
    同一単語・同一品詞の meaning が未登録なら補完してから返す。
    """
    try:
        lookup = await get_or_create_word(
            word,
            part_of_speech=part_of_speech,
            enrich_meanings=enrich_meanings,
        )
        result = get_word_with_meanings(lookup["word_id"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Word lookup failed: {e}") from e
    return result


@router.post("/stored-meanings", response_model=list[WordResponse])
async def get_stored_meanings(payload: StoredMeaningsRequest):
    """教材画面向けに、DB保存済みの意味だけを一括取得する。外部辞書は呼ばない。"""
    words = sorted({
        item.text.strip().lower().replace("’", "'")
        for item in payload.items
        if item.text.strip()
    })
    if not words:
        return []
    response = (
        get_supabase()
        .table("words")
        .select("*, meanings(*, example_sentences(*))")
        .in_("word", words)
        .execute()
    )
    return response.data or []


@router.get("/pronunciation/{meaning_id}")
async def pronunciation(meaning_id: int):
    meaning = (
        get_supabase()
        .table("meanings")
        .select("ipa, words(word)")
        .eq("id", meaning_id)
        .maybe_single()
        .execute()
    )
    if not meaning or not meaning.data:
        raise HTTPException(status_code=404, detail="Meaning not found.")
    row = meaning.data
    word = (row.get("words") or {}).get("word") or ""
    try:
        audio = await get_pronunciation_audio(word, row.get("ipa"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Piper synthesis failed: {exc}") from exc
    return Response(content=audio, media_type="audio/wav")


@router.get("/distractors", response_model=list[MeaningResponse])
async def get_distractors(
    part_of_speech: str = Query(...),
    exclude_meaning_ids: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
):
    """4択用に、DB保存済みの同品詞の意味を返す。外部検索・生成は行わない。"""
    excluded = {
        int(value) for value in exclude_meaning_ids.split(",")
        if value.strip().isdigit()
    }
    query = (
        get_supabase()
        .table("meanings")
        .select("*, words(*), example_sentences(*)")
        .eq("part_of_speech", part_of_speech)
    )
    if excluded:
        query = query.not_.in_("id", sorted(excluded))
    return query.limit(limit).execute().data or []


@router.get("/{word_id}", response_model=WordResponse)
async def get_word(word_id: int):
    """単語をmeanings・example_sentencesつきで取得"""
    result = get_word_with_meanings(word_id)
    if not result:
        raise HTTPException(status_code=404, detail="Word not found.")
    return result


# ── 未知単語の一括登録（OCR結果 → 単語帳へ） ─────────────────────────────────

@router.post("/batch", response_model=BatchWordsResponse, status_code=201)
async def add_words_batch(payload: BatchWordsRequest):
    """
    未知単語のリストを受け取り、それぞれ:
      1. words テーブルに存在しなければ登録
      2. 取得できた meaning をユーザーの単語帳（未学習）に追加
    意味がまだ登録されていない単語も results に含め、呼び出し側で判断できるようにする。
    """
    results: list[BatchWordResult] = []
    user_id = resolve_user_id(payload.user_id)
    registered_count = 0
    skipped_no_meaning_count = 0

    seen: set[tuple[str, Optional[str]]] = set()
    unique_items: list[tuple[str, Optional[str]]] = []
    raw_items = [(item.text, item.part_of_speech.value) for item in payload.items]
    raw_items.extend((word, None) for word in payload.words)
    for w, pos in raw_items:
        lower = w.strip().lower()
        normalized_pos = normalize_part_of_speech(pos)
        key = (lower, normalized_pos)
        if lower and key not in seen:
            seen.add(key)
            unique_items.append(key)

    for word, part_of_speech in unique_items:
        try:
            lookup = await get_or_create_word(
                word,
                part_of_speech=part_of_speech,
                enrich_meanings=payload.enrich_meanings,
            )
        except Exception as e:
            results.append(BatchWordResult(
                word=word,
                part_of_speech=part_of_speech,
                status="error",
                meaning_count=0,
                error=str(e),
            ))
            continue

        if lookup["meaning_count"] == 0:
            skipped_no_meaning_count += 1
            results.append(BatchWordResult(
                word=word,
                part_of_speech=part_of_speech,
                status="no_meaning_found",
                meaning_count=0,
            ))
            continue

        added_ids = await add_meanings_to_wordbook(
            user_id,
            lookup["word_id"],
            part_of_speech=part_of_speech,
            source_type=payload.source_type,
            source_material_id=payload.source_material_id,
            source_folder_id=payload.source_folder_id,
            source_label=payload.source_label,
            is_learned=payload.is_learned,
        )
        add_entries_to_independent_wordbook(user_id, payload.wordbook_id, added_ids)
        registered_count += 1
        results.append(BatchWordResult(
            word=word,
            part_of_speech=part_of_speech,
            status="registered" if added_ids else "already_in_wordbook",
            meaning_count=lookup["meaning_count"],
        ))

    return BatchWordsResponse(
        total=len(unique_items),
        registered_count=registered_count,
        skipped_no_meaning_count=skipped_no_meaning_count,
        results=results,
    )


# ── Wordbook CRUD ─────────────────────────────────────────────────────────────

@router.get("/wordbook/{user_id}", response_model=list[WordbookWordResponse])
async def get_wordbook(
    user_id: str,
    is_learned: Optional[bool] = Query(default=None),
    source_type: Optional[str] = Query(default=None),
    source_material_id: Optional[str] = Query(default=None),
    source_folder_id: Optional[str] = Query(default=None),
    wordbook_id: Optional[str] = Query(default=None),
):
    """ユーザーの単語帳を取得（is_learnedでフィルタ可能）"""
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    def build_query(include_sources: bool):
        select = "*, meanings(*, words(*), example_sentences(*))"
        if wordbook_id:
            select += ", wordbook_memberships!inner(wordbook_id)"
        if include_sources:
            select += ", wordbook_word_sources(*)"
        query = db.table("wordbook_words").select(select).eq("user_id", user_id)
        if is_learned is not None:
            query = query.eq("is_learned", is_learned)
        if wordbook_id:
            query = query.eq("wordbook_memberships.wordbook_id", wordbook_id)
        return query.order("created_at", desc=True)

    include_sources = True
    try:
        response = build_query(include_sources=True).execute()
    except Exception:
        include_sources = False
        response = build_query(include_sources=False).execute()
    rows = []
    for row in response.data or []:
        sources = row.pop("wordbook_word_sources", []) or []
        if not include_sources and (source_type or source_material_id or source_folder_id):
            continue
        if source_type and not any(source.get("source_type") == source_type for source in sources):
            continue
        if source_material_id and not any(source.get("material_id") == source_material_id for source in sources):
            continue
        if source_folder_id and not any(source.get("folder_id") == source_folder_id for source in sources):
            continue
        row["sources"] = sources
        rows.append(row)
    return rows


@router.post("/wordbook", response_model=WordbookWordResponse, status_code=201)
async def add_to_wordbook(user_id: str, payload: WordbookWordCreate):
    """meaning_idを単語帳（未学習）に追加する"""
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    try:
        meaning = (
            db.table("meanings")
            .select("id")
            .eq("id", payload.meaning_id)
            .maybe_single()
            .execute()
        )
        if not meaning.data:
            raise HTTPException(status_code=404, detail="Meaning not found.")
        existing = (
            db.table("wordbook_words")
            .select("id")
            .eq("user_id", user_id)
            .eq("meaning_id", payload.meaning_id)
            .execute()
        )
        if existing.data:
            updated = (
                db.table("wordbook_words")
                .update({"is_learned": False})
                .eq("id", existing.data[0]["id"])
                .execute()
            )
            record_wordbook_source(
                existing.data[0]["id"],
                source_type=payload.source_type,
                source_material_id=payload.source_material_id,
                source_folder_id=payload.source_folder_id,
                source_label=payload.source_label,
            )
            add_entries_to_independent_wordbook(
                user_id, payload.wordbook_id, [existing.data[0]["id"]]
            )
            return updated.data[0] if updated.data else existing.data[0]
        response = db.table("wordbook_words").insert({
            "user_id": user_id,
            "meaning_id": payload.meaning_id,
            "is_learned": False,
        }).execute()
        record_wordbook_source(
            response.data[0]["id"],
            source_type=payload.source_type,
            source_material_id=payload.source_material_id,
            source_folder_id=payload.source_folder_id,
            source_label=payload.source_label,
        )
        add_entries_to_independent_wordbook(
            user_id, payload.wordbook_id, [response.data[0]["id"]]
        )
        return response.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Wordbook insert failed: {e}") from e


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
async def extract_words(payload: ExtractWordsRequest, background_tasks: BackgroundTasks):
    """
    テキストから未知単語を抽出する。
    学習済み単語帳（is_learned=true）に存在しない同一単語・同一品詞を未知語として返す。
    必要に応じて、レスポンス後に教材全体の語義補完をバックグラウンドで走らせる。
    """
    result = await extract_unknown_words(
        text=payload.text,
        user_id=resolve_user_id(payload.user_id),
        enrich_meanings=payload.enrich_meanings,
    )
    if payload.background_enrich_meanings and not payload.enrich_meanings:
        background_tasks.add_task(enrich_lexical_items, result["items"])
    return ExtractWordsResponse(**result)
