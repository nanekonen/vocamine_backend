from fastapi import APIRouter, File, UploadFile, HTTPException
from app.services.ocr_service import (
    extract_text_from_pdf,
    extract_text_and_boxes_from_image,
    extract_pdf_word_boxes_from_text_layer,
    extract_text_and_boxes_from_pdf_page_images,
    extract_text_and_boxes_with_azure,
    png_bytes_to_data_urls,
    render_pdf_pages_as_png_bytes,
    attach_word_box_offsets,
    text_and_offsets_from_word_boxes,
)
from app.schemas.schemas import OCRResponse, PDFOCRResponse

router = APIRouter(prefix="/ocr", tags=["OCR"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_PDF_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/image", response_model=OCRResponse)
async def ocr_image(file: UploadFile = File(...)):
    """Azure、Tesseractの順で画像をOCRする。"""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {file.content_type}. Use JPEG, PNG, WebP, or GIF.",
        )

    image_bytes = await file.read()
    if len(image_bytes) > MAX_IMAGE_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")

    try:
        text, word_boxes = await extract_text_and_boxes_from_image(image_bytes)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # OCRが返した日本語・英語・改行・句読点を含む全文を変更しない。
    # box列から本文を作り直すと、box検出側が拾わなかった日本語が消えるため禁止する。
    if text.strip() and word_boxes:
        word_boxes = attach_word_box_offsets(word_boxes, text)
    elif word_boxes:
        text, word_boxes = text_and_offsets_from_word_boxes(word_boxes)

    return OCRResponse(text=text, word_boxes=word_boxes)


@router.post("/pdf", response_model=PDFOCRResponse)
async def ocr_pdf(file: UploadFile = File(...)):
    """PDF登録時に一度だけOCRし、全文と位置情報を保存用に返す。"""
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted here.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB).")

    page_image_bytes = render_pdf_pages_as_png_bytes(pdf_bytes)

    text = ""
    word_boxes: list[dict] = []
    if page_image_bytes:
        # Azure Document Intelligenceを主系とし、未設定・無料枠制限・通信障害時は
        # 日本語対応Tesseractへフォールバックする。表示時には再OCRしない。
        try:
            text, word_boxes = await extract_text_and_boxes_with_azure(page_image_bytes)
        except Exception as exc:
            print(f"[ocr] Azure failed; using Tesseract fallback: {exc!r}")
            text, word_boxes = await extract_text_and_boxes_from_pdf_page_images(
                page_image_bytes
            )

    if not text.strip():
        try:
            text = await extract_text_from_pdf(pdf_bytes, page_images=[])
        except ValueError as e:
            if not page_image_bytes:
                raise HTTPException(status_code=502, detail=str(e))

    if not word_boxes:
        word_boxes = extract_pdf_word_boxes_from_text_layer(pdf_bytes)

    if text.strip() and word_boxes:
        # 日本語・改行・句読点を含む抽出本文は変更せず、原文上のboxだけを
        # その本文へ対応付ける。
        word_boxes = attach_word_box_offsets(word_boxes, text)
    elif word_boxes:
        text, word_boxes = text_and_offsets_from_word_boxes(word_boxes)

    if not page_image_bytes and not text.strip():
        raise HTTPException(
            status_code=422,
            detail="PDFを読み込めませんでした。別のPDFを書き出してから再度試してください。",
        )

    return PDFOCRResponse(
        text=text,
        page_images=png_bytes_to_data_urls(page_image_bytes),
        word_boxes=word_boxes,
    )
