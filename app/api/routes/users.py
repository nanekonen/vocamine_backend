from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from app.db.supabase import get_supabase
from app.schemas.schemas import UserLevelUpdate, UserResponse, LevelSetupRequest
from app.services.word_service import bulk_register_cefr_words_background
from app.services.user_identity_service import ensure_user_profile, resolve_user_id

router = APIRouter(prefix="/users", tags=["Users"])


class RecentMaterialUpdate(BaseModel):
    material_id: str


def _dashboard_word(row: dict) -> dict:
    meaning = row.get("meanings") or {}
    word = meaning.get("words") or {}
    return {
        "id": str(row.get("id") or ""),
        "headword": word.get("word") or "",
        "meaning_ja": meaning.get("definition_ja") or "日本語訳が得られませんでした",
        "part_of_speech": meaning.get("part_of_speech") or "",
    }


@router.get("/{user_id}/dashboard")
def get_dashboard(user_id: str):
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    select = "id, is_learned, created_at, updated_at, meanings(definition_ja, part_of_speech, words(word))"
    learned_response = (
        db.table("wordbook_words")
        .select(select, count="exact")
        .eq("user_id", user_id)
        .eq("is_learned", True)
        .order("updated_at", desc=True)
        .limit(5)
        .execute()
    )
    registered_response = (
        db.table("wordbook_words")
        .select(select, count="exact")
        .eq("user_id", user_id)
        .eq("is_learned", False)
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )
    registered_rows = registered_response.data or []
    recent_registered = [
        _dashboard_word(row) for row in registered_rows[:5]
    ]
    registered_count = registered_response.count or 0
    user = db.table("users").select("last_opened_material_id").eq("id", user_id).maybe_single().execute()
    return {
        "learned_count": learned_response.count or 0,
        "registered_count": registered_count,
        "recent_learned": [_dashboard_word(row) for row in learned_response.data or []],
        "recent_registered": recent_registered,
        "recent_material_id": (user.data or {}).get("last_opened_material_id"),
    }


@router.put("/{user_id}/recent-material", status_code=204)
def update_recent_material(user_id: str, payload: RecentMaterialUpdate):
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    material = (
        db.table("materials")
        .select("id")
        .eq("id", payload.material_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not material.data:
        raise HTTPException(status_code=404, detail="Material not found.")
    db.table("users").update({"last_opened_material_id": payload.material_id}).eq(
        "id", user_id
    ).execute()


@router.post("/{user_id}/setup", status_code=201)
def setup_level(
    user_id: str,
    payload: LevelSetupRequest,
    background_tasks: BackgroundTasks,
):
    """
    レベル設定 + CEFR-J単語を学習済み単語帳に一括登録。
    初回登録時に呼ぶ。
    """
    user_id = resolve_user_id(user_id)
    db = get_supabase()

    # users テーブルに upsert
    db.table("users").upsert(
        {
            "id": user_id,
            "level": payload.level,
            "username": payload.username.strip(),
        },
        on_conflict="id"
    ).execute()

    # CEFR-J単語の登録はレスポンス後にthread poolで続行する。
    background_tasks.add_task(
        bulk_register_cefr_words_background,
        user_id,
        payload.level,
    )

    return {
        "level": payload.level,
        "username": payload.username.strip(),
        "registered_words": 0,
        "registration_queued": True,
    }


@router.put("/{user_id}/level", response_model=UserResponse)
async def update_level(user_id: str, payload: UserLevelUpdate):
    """レベルだけ更新する（再設定用）"""
    user_id = resolve_user_id(user_id)
    ensure_user_profile(user_id, payload.level)
    db = get_supabase()
    response = (
        db.table("users")
        .update({"level": payload.level, "updated_at": "now()"})
        .eq("id", user_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="User not found.")
    return response.data[0]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """ユーザー情報を取得"""
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    response = db.table("users").select("*").eq("id", user_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="User not found.")
    return response.data[0]
