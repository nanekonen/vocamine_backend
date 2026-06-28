from fastapi import APIRouter, File, UploadFile, HTTPException
from app.services.ocr_service import extract_text_from_image, extract_text_from_pdf_page
from app.schemas.schemas import OCRResponse

router = APIRouter(prefix="/ocr", tags=["OCR"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


@router.post("/image", response_model=OCRResponse)
async def ocr_image(file: UploadFile = File(...)):
    """Upload an image and get back the extracted text."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {file.content_type}. Use JPEG, PNG, WebP, or GIF.",
        )

    image_bytes = await file.read()
    if len(image_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")

    try:
        text = await extract_text_from_image(image_bytes)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OCRResponse(text=text)


@router.post("/pdf", response_model=OCRResponse)
async def ocr_pdf(file: UploadFile = File(...)):
    """Upload a single-page PDF and get back the extracted text."""
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted here.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB).")

    try:
        text = await extract_text_from_pdf_page(pdf_bytes)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OCRResponse(text=text)
