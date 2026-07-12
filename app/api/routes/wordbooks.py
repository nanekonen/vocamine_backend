from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.db.supabase import get_supabase
from app.schemas.schemas import (
    IndependentWordbookCreate,
    IndependentWordbookFolderCreate,
    IndependentWordbookFolderUpdate,
    IndependentWordbookUpdate,
    MaterialDefaultWordbookUpdate,
)
from app.services.user_identity_service import resolve_user_id

router = APIRouter(prefix="/wordbooks", tags=["Wordbooks"])


def _owned_wordbook(db, user_id: str, wordbook_id: str):
    return db.table("wordbooks").select("*").eq("id", wordbook_id).eq(
        "user_id", user_id
    ).maybe_single().execute().data


@router.get("")
async def list_wordbooks(user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    folders = db.table("wordbook_folders").select("*").eq(
        "user_id", user_id
    ).order("created_at").execute().data or []
    books = db.table("wordbooks").select("*").eq("user_id", user_id).order(
        "created_at"
    ).execute().data or []
    return {"folders": folders, "wordbooks": books}


@router.post("", status_code=201)
async def create_wordbook(payload: IndependentWordbookCreate):
    user_id = resolve_user_id(payload.user_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="単語帳名を入力してください。")
    try:
        return get_supabase().table("wordbooks").insert({
            "user_id": user_id, "name": name, "folder_id": payload.folder_id,
        }).execute().data[0]
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"単語帳を作成できません: {exc}")


@router.patch("/{wordbook_id}")
async def update_wordbook(wordbook_id: str, payload: IndependentWordbookUpdate):
    user_id = resolve_user_id(payload.user_id)
    db = get_supabase()
    if not _owned_wordbook(db, user_id, wordbook_id):
        raise HTTPException(status_code=404, detail="Wordbook not found.")
    updates = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="単語帳名を入力してください。")
        updates["name"] = name
    if payload.update_folder:
        updates["folder_id"] = payload.folder_id
    if not updates:
        return _owned_wordbook(db, user_id, wordbook_id)
    try:
        return db.table("wordbooks").update(updates).eq("id", wordbook_id).eq(
            "user_id", user_id
        ).execute().data[0]
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"単語帳を更新できません: {exc}")


@router.delete("/{wordbook_id}", status_code=204)
async def delete_wordbook(wordbook_id: str, user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    if not _owned_wordbook(db, user_id, wordbook_id):
        raise HTTPException(status_code=404, detail="Wordbook not found.")
    db.table("wordbooks").delete().eq("id", wordbook_id).eq("user_id", user_id).execute()


@router.post("/folders", status_code=201)
async def create_folder(payload: IndependentWordbookFolderCreate):
    user_id = resolve_user_id(payload.user_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="フォルダ名を入力してください。")
    try:
        return get_supabase().table("wordbook_folders").insert({
            "user_id": user_id, "name": name, "parent_id": payload.parent_id,
        }).execute().data[0]
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"フォルダを作成できません: {exc}")


@router.patch("/folders/{folder_id}")
async def update_folder(folder_id: str, payload: IndependentWordbookFolderUpdate):
    user_id = resolve_user_id(payload.user_id)
    db = get_supabase()
    current = db.table("wordbook_folders").select("*").eq("id", folder_id).eq(
        "user_id", user_id
    ).maybe_single().execute().data
    if not current:
        raise HTTPException(status_code=404, detail="Folder not found.")
    updates = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="フォルダ名を入力してください。")
        updates["name"] = name
    if payload.update_parent:
        if payload.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="同じフォルダには移動できません。")
        updates["parent_id"] = payload.parent_id
    if not updates:
        return current
    try:
        return db.table("wordbook_folders").update(updates).eq("id", folder_id).eq(
            "user_id", user_id
        ).execute().data[0]
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"フォルダを更新できません: {exc}")


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(folder_id: str, user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    get_supabase().table("wordbook_folders").delete().eq("id", folder_id).eq(
        "user_id", user_id
    ).execute()


@router.get("/materials/{material_id}/default")
async def get_material_default(material_id: str, user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    row = get_supabase().table("materials").select("default_wordbook_id").eq(
        "id", material_id
    ).eq("user_id", user_id).maybe_single().execute().data
    if not row:
        raise HTTPException(status_code=404, detail="Material not found.")
    return {"wordbook_id": row.get("default_wordbook_id")}


@router.put("/materials/{material_id}/default")
async def set_material_default(material_id: str, payload: MaterialDefaultWordbookUpdate):
    user_id = resolve_user_id(payload.user_id)
    db = get_supabase()
    if payload.wordbook_id and not _owned_wordbook(db, user_id, payload.wordbook_id):
        raise HTTPException(status_code=404, detail="Wordbook not found.")
    rows = db.table("materials").update({
        "default_wordbook_id": payload.wordbook_id
    }).eq("id", material_id).eq("user_id", user_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Material not found.")
    return {"wordbook_id": payload.wordbook_id}
