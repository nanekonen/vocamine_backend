from __future__ import annotations

from app.db.supabase import get_supabase


GUEST_USER_ALIAS = "guest"
GUEST_USER_EMAIL = "guest@vocamine.local"
GUEST_USER_PASSWORD = "vocamine-local-guest-password"
DEFAULT_LEVEL = "高校卒業程度"


def resolve_user_id(user_id: str) -> str:
    """
    API内のユーザーIDをDBのUUIDへ解決する。
    フロントのローカル開発用 alias "guest" は Supabase Auth ユーザーに変換する。
    """
    if user_id != GUEST_USER_ALIAS:
        return user_id
    return ensure_guest_user()


def ensure_guest_user() -> str:
    db = get_supabase()
    users = db.auth.admin.list_users(page=1, per_page=1000)
    for user in users:
        if user.email == GUEST_USER_EMAIL:
            ensure_user_profile(user.id)
            return user.id

    response = db.auth.admin.create_user({
        "email": GUEST_USER_EMAIL,
        "password": GUEST_USER_PASSWORD,
        "email_confirm": True,
        "user_metadata": {"name": "Guest User"},
    })
    user = response.user
    ensure_user_profile(user.id)
    return user.id


def ensure_user_profile(user_id: str, level: str = DEFAULT_LEVEL) -> None:
    db = get_supabase()
    existing = db.table("users").select("id").eq("id", user_id).execute()
    if existing.data:
        return
    db.table("users").insert({"id": user_id, "level": level}).execute()
