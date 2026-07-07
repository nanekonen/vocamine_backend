from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db.supabase import get_supabase
from app.schemas.schemas import AuthSessionRequest, AuthSessionResponse


router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/session", response_model=AuthSessionResponse)
async def resolve_session(payload: AuthSessionRequest):
    try:
        response = get_supabase().auth.get_user(payload.access_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid session: {e}") from e

    user = response.user
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session.")

    return _auth_session_response(user.id, user.email)


def _auth_session_response(user_id: str, email: str | None) -> AuthSessionResponse:
    profile = (
        get_supabase()
        .table("users")
        .select("id, level")
        .eq("id", user_id)
        .execute()
    )
    setup_completed = bool(profile.data and profile.data[0].get("level"))
    return AuthSessionResponse(
        user_id=user_id,
        email=email,
        setup_completed=setup_completed,
    )
