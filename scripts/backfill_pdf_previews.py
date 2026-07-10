"""
pdftoppm が使えなかった時期にアップロードされたPDF教材は、
ページ画像(page_images)・単語ボックス(word_boxes)がオブジェクトストレージに
一切保存されていない（空のまま作成された）。

このスクリプトは、元のPDF本体（source_object_storage_key）はあるのに
page_images が無い教材を探し、今のロジック（pdftoppmでのレンダリング +
画像ベースの単語ボックス検出）で作り直して同じキーに保存し直す。
material_id は変えないので、既に登録済みの単語帳（wordbook_word_sources の
material_id 紐付け）には影響しない。

使い方:
    cd vocamine_backend
    .venv/bin/python scripts/backfill_pdf_previews.py
    # 対象を絞りたい場合
    .venv/bin/python scripts/backfill_pdf_previews.py --material-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.supabase import get_supabase  # noqa: E402
from app.services.object_storage_service import get_bytes, put_bytes  # noqa: E402
from app.services.ocr_service import (  # noqa: E402
    attach_word_box_offsets,
    extract_pdf_word_boxes_from_text_layer,
    extract_text_from_pdf,
    extract_word_boxes_from_pdf_page_images,
    png_bytes_to_data_urls,
    render_pdf_pages_as_png_bytes,
    text_and_offsets_from_word_boxes,
)

PAGE_SIZE = 500


def _json_key(user_id: str, material_id: str, name: str) -> str:
    return f"users/{user_id}/materials/{material_id}/{name}.json"


def _has_stored_page_images(user_id: str, material_id: str) -> bool:
    try:
        raw = get_bytes(_json_key(user_id, material_id, "page_images"))
    except Exception:
        return False
    try:
        return bool(json.loads(raw.decode("utf-8")))
    except Exception:
        return False


async def _rebuild_material(db, material: dict) -> bool:
    user_id = material["user_id"]
    material_id = material["id"]
    source_key = material.get("source_object_storage_key")
    if not source_key:
        print(f"  skip {material_id}: 元のPDFが保存されていません")
        return False

    pdf_bytes = get_bytes(source_key)

    page_image_bytes = render_pdf_pages_as_png_bytes(pdf_bytes)
    if not page_image_bytes:
        print(f"  skip {material_id}: pdftoppmでレンダリングできませんでした（環境を確認してください）")
        return False

    word_boxes = await extract_word_boxes_from_pdf_page_images(page_image_bytes)
    if not word_boxes:
        word_boxes = extract_pdf_word_boxes_from_text_layer(pdf_bytes)

    text = material.get("extracted_text") or ""
    if not text.strip():
        text = await extract_text_from_pdf(pdf_bytes, page_images=page_image_bytes)
    if not text.strip():
        text, word_boxes = text_and_offsets_from_word_boxes(word_boxes)
    else:
        word_boxes = attach_word_box_offsets(word_boxes, text)

    page_images_b64 = [
        data_url.split(",", 1)[1]
        for data_url in png_bytes_to_data_urls(page_image_bytes)
    ]
    put_bytes(
        _json_key(user_id, material_id, "page_images"),
        json.dumps(page_images_b64).encode("utf-8"),
        "application/json",
    )
    put_bytes(
        _json_key(user_id, material_id, "word_boxes"),
        json.dumps(word_boxes).encode("utf-8"),
        "application/json",
    )

    update_payload: dict = {}
    if text.strip() and text.strip() != (material.get("extracted_text") or "").strip():
        update_payload["extracted_text"] = text
    if update_payload:
        db.table("materials").update(update_payload).eq("id", material_id).execute()

    print(f"  updated {material_id}: pages={len(page_image_bytes)} word_boxes={len(word_boxes)}")
    return True


async def main(material_id: str | None) -> None:
    db = get_supabase()

    query = db.table("materials").select(
        "id, user_id, source_object_storage_key, source_mime_type, extracted_text"
    ).eq("source_mime_type", "application/pdf")
    if material_id:
        query = query.eq("id", material_id)

    materials: list[dict] = []
    start = 0
    while True:
        page = query.range(start, start + PAGE_SIZE - 1).execute()
        batch = page.data or []
        materials.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    print(f"PDF教材: {len(materials)} 件")

    updated = 0
    skipped = 0
    for material in materials:
        if not material_id and _has_stored_page_images(material["user_id"], material["id"]):
            skipped += 1
            continue
        print(f"processing {material['id']}...")
        if await _rebuild_material(db, material):
            updated += 1
        else:
            skipped += 1

    print(f"\n完了: updated={updated} skipped={skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--material-id", default=None, help="このIDの教材だけ強制的に再生成する")
    args = parser.parse_args()
    asyncio.run(main(args.material_id))
