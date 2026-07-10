from __future__ import annotations

import base64
import binascii
import json
import mimetypes
from io import BytesIO
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.db.supabase import get_supabase
from app.schemas.schemas import (
    MaterialCreate,
    MaterialFolderCreate,
    MaterialFolderResponse,
    MaterialLibraryResponse,
    MaterialResponse,
)
from app.services.object_storage_service import get_bytes, put_bytes
from app.services.ocr_service import (
    attach_word_box_offsets,
    render_pdf_pages_as_png_bytes,
    _png_dimensions,
)
from app.services.user_identity_service import resolve_user_id

router = APIRouter(prefix="/materials", tags=["Materials"])


def _decode_base64(value: str | None) -> bytes | None:
    if not value:
        return None
    payload = value.split(",", 1)[1] if "," in value[:64] else value
    try:
        return base64.b64decode(payload)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 payload.") from exc


def _data_url_from_png_base64(value: str) -> str:
    payload = value.split(",", 1)[1] if "," in value[:64] else value
    return f"data:image/png;base64,{payload}"


TEXT_LAYER_VERSION = 2

READABLE_PDF_FONT = "HeiseiMin-W3"
try:
    pdfmetrics.registerFont(UnicodeCIDFont(READABLE_PDF_FONT))
except Exception:
    READABLE_PDF_FONT = "Helvetica"


def _json_key(user_id: str, material_id: str, name: str) -> str:
    return f"users/{user_id}/materials/{material_id}/{name}.json"


def _text_layer_payload(text: str, boxes: list[dict], source: str) -> dict:
    return {
        "version": TEXT_LAYER_VERSION,
        "source": source,
        "text": text,
        "boxes": boxes,
    }


def _save_text_layer(
    user_id: str,
    material_id: str,
    text: str,
    boxes: list[dict],
    source: str,
) -> None:
    put_bytes(
        _json_key(user_id, material_id, "word_boxes"),
        json.dumps(_text_layer_payload(text, boxes, source)).encode("utf-8"),
        "application/json",
    )


def _attach_offsets_preserving_text(text: str, boxes: list[dict]) -> list[dict]:
    """本文は一切作り直さず、保存済みOCRボックスへoffsetだけ付与する。"""
    if not text or not boxes:
        return boxes
    try:
        return attach_word_box_offsets(boxes, text)
    except Exception as exc:
        print(f"[materials] failed to attach word offsets: {exc!r}")
        return boxes


def _load_material_assets(row: dict) -> dict:
    """保存済みの本文・画像・位置情報だけを読む。OCRやPDF再解析は行わない。"""
    user_id = row.get("user_id")
    material_id = row.get("id")
    if not user_id or not material_id:
        row["page_images"] = []
        row["word_boxes"] = []
        return row

    try:
        page_images_raw = json.loads(
            get_bytes(_json_key(user_id, material_id, "page_images")).decode("utf-8")
        )
    except Exception as exc:
        print(f"[materials] failed to load page_images for {material_id}: {exc!r}")
        page_images_raw = []

    try:
        layer_raw = json.loads(
            get_bytes(_json_key(user_id, material_id, "word_boxes")).decode("utf-8")
        )
    except Exception as exc:
        print(f"[materials] failed to load text layer for {material_id}: {exc!r}")
        layer_raw = None

    row["page_images"] = [
        _data_url_from_png_base64(value)
        for value in page_images_raw
        if isinstance(value, str)
    ]

    # DBのextracted_textが登録時OCRの原文。readable PDFから本文を逆生成して
    # 上書きしない。これにより日本語、改行、句読点をそのまま保持する。
    authoritative_text = str(row.get("extracted_text") or "")

    if isinstance(layer_raw, dict):
        boxes_raw = layer_raw.get("boxes")
        boxes = boxes_raw if isinstance(boxes_raw, list) else []
        if layer_raw.get("version") == TEXT_LAYER_VERSION:
            stored_text = str(layer_raw.get("text") or "")
            row["extracted_text"] = stored_text or authoritative_text
            row["word_boxes"] = boxes
            return row
    else:
        boxes = layer_raw if isinstance(layer_raw, list) else []

    # 旧形式は保存済み本文にoffsetを付け直すだけ。OCRもPDF解析も呼ばない。
    boxes = _attach_offsets_preserving_text(authoritative_text, boxes)
    row["extracted_text"] = authoritative_text
    row["word_boxes"] = boxes
    try:
        _save_text_layer(
            user_id,
            material_id,
            authoritative_text,
            boxes,
            "stored_ocr_text",
        )
    except Exception as exc:
        print(f"[materials] failed to persist text layer for {material_id}: {exc!r}")
    return row


def _make_readable_pdf(
    pdf_bytes: bytes,
    word_boxes: list[dict],
    page_images: list[bytes] | None = None,
) -> bytes:
    page_images = page_images or render_pdf_pages_as_png_bytes(pdf_bytes)
    if not page_images:
        return pdf_bytes

    page_boxes: dict[int, list[dict]] = {}
    for box in word_boxes:
        page_index = int(box.get("page_index") or 0)
        page_boxes.setdefault(page_index, []).append(box)

    output = BytesIO()
    pdf = canvas.Canvas(output)
    for page_index, image_bytes in enumerate(page_images):
        width, height = _png_dimensions(image_bytes)
        page_width = float(width)
        page_height = float(height)
        pdf.setPageSize((page_width, page_height))
        pdf.drawImage(
            ImageReader(BytesIO(image_bytes)),
            0,
            0,
            width=page_width,
            height=page_height,
            preserveAspectRatio=False,
            mask="auto",
        )
        pdf.setFillAlpha(0)
        for box in page_boxes.get(page_index, []):
            text = str(box.get("text") or "").strip()
            if not text:
                continue
            left = float(box.get("left") or 0) * page_width
            top = float(box.get("top") or 0) * page_height
            box_width = max(1.0, float(box.get("width") or 0) * page_width)
            box_height = max(1.0, float(box.get("height") or 0) * page_height)
            font_size = max(4.0, min(box_height * 0.85, 32.0))
            text_object = pdf.beginText()
            text_object.setTextOrigin(left, page_height - top - box_height * 0.82)
            text_object.setFont(READABLE_PDF_FONT, font_size)
            rendered_width = pdfmetrics.stringWidth(text, READABLE_PDF_FONT, font_size)
            if rendered_width > 0:
                text_object.setHorizScale(
                    max(35.0, min(160.0, box_width / rendered_width * 100))
                )
            text_object.textOut(text)
            pdf.drawText(text_object)
        pdf.setFillAlpha(1)
        pdf.showPage()
    pdf.save()
    return output.getvalue()



@router.get("", response_model=MaterialLibraryResponse)
async def list_materials(user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    db = get_supabase()
    folders = (
        db.table("material_folders")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    materials = (
        db.table("materials")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    materials = [_load_material_assets(dict(material)) for material in materials]
    return {"folders": folders, "materials": materials}


@router.post("/folders", response_model=MaterialFolderResponse, status_code=201)
async def create_folder(payload: MaterialFolderCreate):
    user_id = resolve_user_id(payload.user_id)
    response = (
        get_supabase()
        .table("material_folders")
        .insert(
            {
                "user_id": user_id,
                "name": payload.name.strip(),
                "parent_id": payload.parent_id,
            }
        )
        .execute()
    )
    return response.data[0]


@router.post("", response_model=MaterialResponse, status_code=201)
async def create_material(payload: MaterialCreate):
    user_id = resolve_user_id(payload.user_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="教材名を入力してください。")
    existing = (
        get_supabase()
        .table("materials")
        .select("id")
        .eq("user_id", user_id)
        .ilike("title", title)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        raise HTTPException(status_code=409, detail="同じ名前の教材は登録できません。")

    material_id = str(uuid4())
    source_key = None
    readable_pdf_key = None

    source_bytes = _decode_base64(payload.source_base64)
    raw_word_boxes = [box.model_dump() for box in payload.word_boxes]
    page_image_values = [
        image.split(",", 1)[1] if "," in image[:64] else image
        for image in (payload.page_images_base64 or [])
    ]
    page_image_bytes = [
        decoded
        for value in page_image_values
        if (decoded := _decode_base64(value)) is not None
    ]

    if source_bytes:
        extension = mimetypes.guess_extension(payload.source_mime_type or "") or ".bin"
        source_key = f"users/{user_id}/materials/{material_id}/source{extension}"
        put_bytes(source_key, source_bytes, payload.source_mime_type)

    supplied_readable_pdf = _decode_base64(payload.readable_pdf_base64)
    readable_pdf_bytes: bytes | None = supplied_readable_pdf
    if (
        readable_pdf_bytes is None
        and source_bytes
        and payload.source_mime_type == "application/pdf"
    ):
        readable_pdf_bytes = _make_readable_pdf(
            source_bytes,
            raw_word_boxes,
            page_images=page_image_bytes or None,
        )

    if readable_pdf_bytes:
        readable_pdf_key = f"users/{user_id}/materials/{material_id}/readable.pdf"
        put_bytes(readable_pdf_key, readable_pdf_bytes, "application/pdf")
    elif payload.source_mime_type == "application/pdf":
        readable_pdf_key = source_key

    # 登録時OCRが返した全文を唯一の本文として保存する。
    # readable PDFのテキストレイヤーから逆抽出して上書きしない。
    extracted_text = payload.extracted_text or payload.ocr_text or ""
    word_boxes = _attach_offsets_preserving_text(extracted_text, raw_word_boxes)
    layer_source = "ocr_import"

    if page_image_values:
        put_bytes(
            _json_key(user_id, material_id, "page_images"),
            json.dumps(page_image_values).encode("utf-8"),
            "application/json",
        )

    _save_text_layer(
        user_id,
        material_id,
        extracted_text,
        word_boxes,
        layer_source,
    )

    row = {
        "id": material_id,
        "user_id": user_id,
        "folder_id": payload.folder_id,
        "title": title,
        "extracted_text": extracted_text,
        "source_mime_type": payload.source_mime_type,
        "source_object_storage_key": source_key,
        "readable_pdf_object_storage_key": readable_pdf_key,
    }
    response = get_supabase().table("materials").insert(row).execute()
    row = dict(response.data[0])
    # 保存直後にOracleを読み直さず、今アップロードしたデータをそのまま返す。
    # put_bytes 直後の get_bytes は read-after-write の一貫性に依存してしまうため、
    # 作成レスポンスでは使わない（一覧取得(list_materials)側は引き続き
    # _load_material_assets 経由でオブジェクトストレージから読み直す）。
    row["page_images"] = [
        _data_url_from_png_base64(value) for value in page_image_values
    ]
    row["word_boxes"] = word_boxes
    return row


@router.get("/{material_id}/source")
async def get_material_source(material_id: str, user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    response = (
        get_supabase()
        .table("materials")
        .select("source_object_storage_key, source_mime_type")
        .eq("id", material_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not response.data or not response.data.get("source_object_storage_key"):
        raise HTTPException(status_code=404, detail="Material source not found.")
    data = get_bytes(response.data["source_object_storage_key"])
    return {
        "mime_type": response.data.get("source_mime_type"),
        "base64": base64.b64encode(data).decode("ascii"),
    }


@router.get("/{material_id}/readable-pdf")
async def get_material_readable_pdf(material_id: str, user_id: str = Query(...)):
    user_id = resolve_user_id(user_id)
    response = (
        get_supabase()
        .table("materials")
        .select("readable_pdf_object_storage_key")
        .eq("id", material_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    key = (response.data or {}).get("readable_pdf_object_storage_key")
    if not key:
        raise HTTPException(status_code=404, detail="Readable PDF not found.")
    data = get_bytes(key)
    return {"mime_type": "application/pdf", "base64": base64.b64encode(data).decode("ascii")}


@router.get("/{material_id}/debug")
async def debug_material(material_id: str, user_id: str = Query(...)):
    """
    指定した教材の page_images / word_boxes がオブジェクトストレージに
    実際に保存されているかを直接確認するための一時的な診断エンドポイント。
    原因切り分けが済んだら削除してよい。
    """
    user_id = resolve_user_id(user_id)
    row = (
        get_supabase()
        .table("materials")
        .select("*")
        .eq("id", material_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    ).data
    if not row:
        raise HTTPException(status_code=404, detail="Material not found.")

    result: dict = {"row": row}
    for name in ("page_images", "word_boxes"):
        key = _json_key(user_id, material_id, name)
        try:
            data = get_bytes(key)
            parsed = json.loads(data.decode("utf-8"))
            result[name] = {
                "key": key,
                "ok": True,
                "byte_length": len(data),
                "item_count": (
                    len(parsed)
                    if isinstance(parsed, list)
                    else len(parsed.get("boxes", []))
                    if isinstance(parsed, dict)
                    else None
                ),
            }
        except Exception as exc:
            result[name] = {"key": key, "ok": False, "error": repr(exc)}
    return result