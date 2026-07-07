from fastapi import APIRouter, File, UploadFile, HTTPException
from app.services.ocr_service import (
    extract_text_from_image,
    extract_text_from_pdf,
    extract_text_and_boxes_from_image,
    extract_pdf_word_boxes_from_text_layer,
    extract_word_boxes_from_pdf_page_images,
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
    """Upload an image and get back the extracted text."""
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

    word_boxes = attach_word_box_offsets(word_boxes, text)
    return OCRResponse(text=text, word_boxes=word_boxes)


@router.post("/pdf", response_model=PDFOCRResponse)
async def ocr_pdf(file: UploadFile = File(...)):
    """Upload a PDF and get back extracted text plus inert page previews."""
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted here.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB).")

    page_image_bytes = render_pdf_pages_as_png_bytes(pdf_bytes)
    try:
        text = await extract_text_from_pdf(pdf_bytes, page_images=page_image_bytes)
    except ValueError as e:
        if not page_image_bytes:
            raise HTTPException(status_code=502, detail=str(e))
        text = ""

    word_boxes = extract_pdf_word_boxes_from_text_layer(pdf_bytes)
    if not word_boxes:
        word_boxes = await extract_word_boxes_from_pdf_page_images(page_image_bytes)
    if not text.strip():
        text, word_boxes = text_and_offsets_from_word_boxes(word_boxes)
    else:
        word_boxes = attach_word_box_offsets(word_boxes, text)
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
