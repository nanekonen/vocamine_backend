import base64
from google.cloud import vision
from google.oauth2 import service_account
from app.core.config import settings


def _get_vision_client() -> vision.ImageAnnotatorClient:
    if settings.google_application_credentials:
        credentials = service_account.Credentials.from_service_account_file(
            settings.google_application_credentials
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
    Use Vision API async batch for PDF. For single pages, DOCUMENT_TEXT_DETECTION
    on the raw bytes works well enough.
    """
    client = _get_vision_client()
    image = vision.Image(content=pdf_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise ValueError(f"Vision API error: {response.error.message}")

    return response.full_text_annotation.text.strip()
