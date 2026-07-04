from io import BytesIO
from pathlib import Path

import base64
from google.cloud import vision
from google.oauth2 import service_account
from app.core.config import settings


def _get_vision_client() -> vision.ImageAnnotatorClient:
    if settings.google_application_credentials:
        credentials_path = Path(settings.google_application_credentials)
        if not credentials_path.exists():
            raise ValueError(
                f"Google credentials file not found: {settings.google_application_credentials}"
            )
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path)
        )
        return vision.ImageAnnotatorClient(credentials=credentials)
    # Falls back to Application Default Credentials (ADC)
    return vision.ImageAnnotatorClient()


async def extract_text_from_image(image_bytes: bytes) -> str:
    """Send image bytes to Google Cloud Vision and return detected text."""
    client = _get_vision_client()
    image = vision.Image(content=image_bytes)
    response = client.text_detection(image=image)

    if response.error.message:
        raise ValueError(f"Vision API error: {response.error.message}")

    texts = response.text_annotations
    if not texts:
        return ""

    # First annotation contains the full detected text
    return texts[0].description.strip()


async def extract_text_from_pdf_page(pdf_bytes: bytes) -> str:
    """
    Extract embedded text from PDF first. If the PDF is scanned and contains no
    text layer, fall back to Vision API.
    """
    text = _extract_embedded_pdf_text(pdf_bytes)
    if text:
        return text

    client = _get_vision_client()
    image = vision.Image(content=pdf_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise ValueError(f"Vision API error: {response.error.message}")

    return response.full_text_annotation.text.strip()


def _extract_embedded_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        page_texts = [
            page.extract_text() or ""
            for page in reader.pages
        ]
    except Exception:
        return ""

    return "\n\n".join(text.strip() for text in page_texts if text.strip()).strip()
