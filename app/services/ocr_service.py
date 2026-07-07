from __future__ import annotations

import csv
from io import BytesIO
from pathlib import Path

import base64
import re
import shutil
import struct
import subprocess
import tempfile
from google.cloud import vision
from google.oauth2 import service_account
from app.core.config import settings

WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")


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


def _word_boxes_from_document_response(response, page_index: int = 0) -> list[dict]:
    boxes: list[dict] = []
    annotation = response.full_text_annotation
    for page in annotation.pages:
        page_width = float(page.width or 1)
        page_height = float(page.height or 1)
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    text = "".join(symbol.text for symbol in word.symbols).strip()
                    if not text:
                        continue
                    vertices = word.bounding_box.vertices
                    xs = [vertex.x for vertex in vertices if vertex.x is not None]
                    ys = [vertex.y for vertex in vertices if vertex.y is not None]
                    if not xs or not ys:
                        continue
                    left = max(0.0, min(xs) / page_width)
                    top = max(0.0, min(ys) / page_height)
                    right = min(1.0, max(xs) / page_width)
                    bottom = min(1.0, max(ys) / page_height)
                    boxes.append({
                        "text": text,
                        "page_index": page_index,
                        "left": left,
                        "top": top,
                        "width": max(0.0, right - left),
                        "height": max(0.0, bottom - top),
                    })
    return boxes


async def extract_text_and_boxes_from_image(image_bytes: bytes) -> tuple[str, list[dict]]:
    client = _get_vision_client()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise ValueError(f"Vision API error: {response.error.message}")

    text = response.full_text_annotation.text.strip()
    return text, _word_boxes_from_document_response(response)


async def extract_text_from_pdf(
    pdf_bytes: bytes,
    page_images: list[bytes] | None = None,
) -> str:
    """
    Extract embedded text from PDF first. If the PDF is scanned and contains no
    text layer, render pages and OCR each page image. Sending raw multi-page
    PDFs to Vision as an image payload fails for many PDFs.
    """
    text = _extract_embedded_pdf_text(pdf_bytes)
    if text:
        return text

    images = page_images if page_images is not None else render_pdf_pages_as_png_bytes(pdf_bytes)
    if not images:
        return ""

    try:
        client = _get_vision_client()
    except Exception:
        return text_from_word_boxes(_tesseract_boxes_from_page_images(images))
    page_texts: list[str] = []
    for image_bytes in images:
        try:
            image = vision.Image(content=image_bytes)
            response = client.document_text_detection(image=image)
        except Exception:
            continue
        if response.error.message:
            continue
        page_text = response.full_text_annotation.text.strip()
        if page_text:
            page_texts.append(page_text)

    text = "\n\n".join(page_texts).strip()
    if text:
        return text

    return text_from_word_boxes(_tesseract_boxes_from_page_images(images))


async def extract_text_from_pdf_page(pdf_bytes: bytes) -> str:
    return await extract_text_from_pdf(pdf_bytes)


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


def extract_pdf_word_boxes_from_text_layer(pdf_bytes: bytes) -> list[dict]:
    try:
        from pypdf import PdfReader
    except Exception:
        return []

    boxes: list[dict] = []
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        for page_index, page in enumerate(reader.pages):
            page_width = float(page.mediabox.width or 1)
            page_height = float(page.mediabox.height or 1)

            def visitor_text(text, cm, tm, font_dict, font_size):
                if not text or not text.strip():
                    return
                x = float(tm[4])
                y = float(tm[5])
                size = float(font_size or 10)
                compact = text.rstrip("\n")
                if not compact:
                    return
                estimated_char_width = size * 0.52
                for match in WORD_RE.finditer(compact):
                    word = match.group(0)
                    left_pdf = x + match.start() * estimated_char_width
                    width_pdf = max(size * 0.6, len(word) * estimated_char_width)
                    top_pdf = page_height - y - size
                    boxes.append({
                        "text": word,
                        "page_index": page_index,
                        "left": max(0.0, min(1.0, left_pdf / page_width)),
                        "top": max(0.0, min(1.0, top_pdf / page_height)),
                        "width": max(0.0, min(1.0, width_pdf / page_width)),
                        "height": max(0.0, min(1.0, (size * 1.35) / page_height)),
                    })

            page.extract_text(visitor_text=visitor_text)
    except Exception:
        return []
    return boxes


def render_pdf_pages_as_png_data_urls(
    pdf_bytes: bytes,
    max_pages: int = 50,
    resolution: int = 130,
) -> list[str]:
    """
    Render PDF pages to inert PNG images. Links inside the PDF become pixels, so
    clicking the preview cannot navigate to external URLs.
    """
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = tmp_path / "source.pdf"
        output_prefix = tmp_path / "page"
        pdf_path.write_bytes(pdf_bytes)
        try:
            subprocess.run(
                [
                    pdftoppm,
                    "-png",
                    "-r",
                    str(resolution),
                    "-f",
                    "1",
                    "-l",
                    str(max_pages),
                    str(pdf_path),
                    str(output_prefix),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            return []

        images: list[str] = []
        for image_path in _sorted_rendered_page_paths(tmp_path):
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            images.append(f"data:image/png;base64,{encoded}")
        return images


def png_bytes_to_data_urls(images: list[bytes]) -> list[str]:
    return [
        f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}"
        for image in images
    ]


def text_from_word_boxes(word_boxes: list[dict]) -> str:
    return text_and_offsets_from_word_boxes(word_boxes)[0]


def text_and_offsets_from_word_boxes(word_boxes: list[dict]) -> tuple[str, list[dict]]:
    ordered = sorted(
        word_boxes,
        key=lambda box: (
            int(box.get("page_index") or 0),
            round(float(box.get("top") or 0) * 100),
            float(box.get("left") or 0),
        ),
    )
    text_parts: list[str] = []
    boxes_with_offsets: list[dict] = []
    cursor = 0
    for box in ordered:
        word = str(box.get("text") or "").strip()
        if not word:
            continue
        if text_parts:
            text_parts.append(" ")
            cursor += 1
        start = cursor
        text_parts.append(word)
        cursor += len(word)
        copied = dict(box)
        copied["start"] = start
        copied["end"] = cursor
        boxes_with_offsets.append(copied)
    return "".join(text_parts).strip(), boxes_with_offsets


def attach_word_box_offsets(word_boxes: list[dict], text: str) -> list[dict]:
    if not word_boxes or not text:
        return word_boxes
    if all(box.get("start") is not None and box.get("end") is not None for box in word_boxes):
        return word_boxes

    with_offsets: list[dict] = []
    cursor = 0
    for box in sorted(
        word_boxes,
        key=lambda item: (
            int(item.get("page_index") or 0),
            round(float(item.get("top") or 0) * 100),
            float(item.get("left") or 0),
        ),
    ):
        raw = str(box.get("text") or "").strip()
        if not raw:
            continue
        start = text.find(raw, cursor)
        if start < 0:
            match = re.search(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", raw)
            start = text.find(match.group(0), cursor) if match else -1
        copied = dict(box)
        if start >= 0:
            copied["start"] = start
            copied["end"] = start + len(raw)
            cursor = copied["end"]
        with_offsets.append(copied)
    return with_offsets


def _png_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return (1, 1)
    try:
        width, height = struct.unpack(">II", image_bytes[16:24])
    except Exception:
        return (1, 1)
    return (max(1, int(width)), max(1, int(height)))


def _tesseract_boxes_from_image(image_bytes: bytes, page_index: int = 0) -> list[dict]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return []

    image_width, image_height = _png_dimensions(image_bytes)
    with tempfile.TemporaryDirectory() as tmpdir:
        image_path = Path(tmpdir) / "page.png"
        image_path.write_bytes(image_bytes)
        try:
            result = subprocess.run(
                [
                    tesseract,
                    str(image_path),
                    "stdout",
                    "-l",
                    "eng",
                    "--psm",
                    "6",
                    "tsv",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
            )
        except Exception:
            return []

    boxes: list[dict] = []
    reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
            left = float(row.get("left") or 0)
            top = float(row.get("top") or 0)
            width = float(row.get("width") or 0)
            height = float(row.get("height") or 0)
        except ValueError:
            continue
        if confidence < 0 or width <= 0 or height <= 0:
            continue
        boxes.append({
            "text": text,
            "page_index": page_index,
            "left": max(0.0, min(1.0, left / image_width)),
            "top": max(0.0, min(1.0, top / image_height)),
            "width": max(0.0, min(1.0, width / image_width)),
            "height": max(0.0, min(1.0, height / image_height)),
        })
    return boxes


def _tesseract_boxes_from_page_images(page_images: list[bytes]) -> list[dict]:
    boxes: list[dict] = []
    for index, image_bytes in enumerate(page_images):
        boxes.extend(_tesseract_boxes_from_image(image_bytes, page_index=index))
    return boxes


def render_pdf_pages_as_png_bytes(
    pdf_bytes: bytes,
    max_pages: int = 50,
    resolution: int = 130,
) -> list[bytes]:
    rendered = _render_pdf_pages_as_png_bytes(
        pdf_bytes,
        max_pages=max_pages,
        resolution=resolution,
    )
    if rendered:
        return rendered

    repaired = repair_pdf_with_ghostscript(pdf_bytes)
    if repaired and repaired != pdf_bytes:
        return _render_pdf_pages_as_png_bytes(
            repaired,
            max_pages=max_pages,
            resolution=resolution,
        )
    return []


def _render_pdf_pages_as_png_bytes(
    pdf_bytes: bytes,
    max_pages: int = 50,
    resolution: int = 130,
) -> list[bytes]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = tmp_path / "source.pdf"
        output_prefix = tmp_path / "page"
        pdf_path.write_bytes(pdf_bytes)
        try:
            subprocess.run(
                [
                    pdftoppm,
                    "-png",
                    "-r",
                    str(resolution),
                    "-f",
                    "1",
                    "-l",
                    str(max_pages),
                    str(pdf_path),
                    str(output_prefix),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            return []

        return [image_path.read_bytes() for image_path in _sorted_rendered_page_paths(tmp_path)]


def repair_pdf_with_ghostscript(pdf_bytes: bytes) -> bytes:
    gs = shutil.which("gs")
    if not gs:
        return b""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        source_path = tmp_path / "source.pdf"
        repaired_path = tmp_path / "repaired.pdf"
        source_path.write_bytes(pdf_bytes)
        try:
            subprocess.run(
                [
                    gs,
                    "-dSAFER",
                    "-dBATCH",
                    "-dNOPAUSE",
                    "-sDEVICE=pdfwrite",
                    "-dCompatibilityLevel=1.7",
                    "-sOutputFile=" + str(repaired_path),
                    str(source_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            return b""
        try:
            repaired = repaired_path.read_bytes()
        except Exception:
            return b""
        return repaired if repaired else b""


def _sorted_rendered_page_paths(tmp_path: Path) -> list[Path]:
    def page_number(path: Path) -> int:
        match = re.search(r"-(\d+)\.png$", path.name)
        return int(match.group(1)) if match else 0

    return sorted(tmp_path.glob("page-*.png"), key=page_number)


async def extract_word_boxes_from_pdf_page_images(page_images: list[bytes]) -> list[dict]:
    boxes: list[dict] = []
    try:
        client = _get_vision_client()
    except Exception:
        return _tesseract_boxes_from_page_images(page_images)
    for index, image_bytes in enumerate(page_images):
        try:
            image = vision.Image(content=image_bytes)
            response = client.document_text_detection(image=image)
        except Exception:
            continue
        if response.error.message:
            continue
        boxes.extend(_word_boxes_from_document_response(response, page_index=index))
    if boxes:
        return boxes
    return _tesseract_boxes_from_page_images(page_images)
