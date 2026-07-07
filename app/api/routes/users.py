from fastapi import APIRouter, HTTPException
from app.db.supabase import get_supabase
from app.schemas.schemas import UserLevelUpdate, UserResponse, LevelSetupRequest
from app.services.word_service import bulk_register_cefr_words
from app.services.user_identity_service import ensure_user_profile, resolve_user_id

router = APIRouter(prefix="/users", tags=["Users"])


@router.post("/{user_id}/setup", status_code=201)
async def setup_level(user_id: str, payload: LevelSetupRequest):
    """
    レベル設定 + CEFR-J単語を学習済み単語帳に一括登録。
    初回登録時に呼ぶ。
    """
    user_id = resolve_user_id(user_id)
    db = get_supabase()

    # users テーブルに upsert
    db.table("users").upsert(
        {"id": user_id, "level": payload.level},
        on_conflict="id"
    ).execute()

    # CEFR-J 単語を学習済みとして一括登録
    count = await bulk_register_cefr_words(user_id, payload.level)

    return {"level": payload.level, "registered_words": count}


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
