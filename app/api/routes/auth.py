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

    metadata = user.user_metadata or {}
    return _auth_session_response(user.id, user.email, metadata)


def _auth_session_response(
    user_id: str,
    email: str | None,
    metadata: dict,
) -> AuthSessionResponse:
    profile = (
        get_supabase()
        .table("users")
        .select("id, level, username")
        .eq("id", user_id)
        .execute()
    )
    row = profile.data[0] if profile.data else {}
    setup_completed = bool(row.get("level"))
    username = row.get("username")
    metadata_username = metadata.get("display_name") or metadata.get("full_name")
    if setup_completed and not username and metadata_username:
        username = str(metadata_username).strip()
        get_supabase().table("users").update({"username": username}).eq(
            "id", user_id
        ).execute()
    return AuthSessionResponse(
        user_id=user_id,
        email=email,
        username=username or metadata_username,
        level=row.get("level"),
        setup_completed=setup_completed,
    )
