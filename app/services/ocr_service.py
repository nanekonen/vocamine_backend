from __future__ import annotations

import asyncio
import csv
from io import BytesIO
from pathlib import Path

import base64
import re
import shutil
import struct
import subprocess
import tempfile
import httpx
from PIL import Image
# Google Cloud Vision is disabled.
# from google.cloud import vision
# from google.oauth2 import service_account
from app.core.config import settings

WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
AZURE_API_VERSION = "2024-11-30"
AZURE_FREE_MAX_BYTES = 4 * 1024 * 1024
AZURE_FREE_MAX_PAGES = 2


# def _get_vision_client() -> vision.ImageAnnotatorClient:
#     credentials = service_account.Credentials.from_service_account_file(...)
#     return vision.ImageAnnotatorClient(credentials=credentials)


async def extract_text_from_image(image_bytes: bytes) -> str:
    """Extract image text locally with Tesseract."""
    return text_from_word_boxes(_tesseract_boxes_from_image(image_bytes))


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
    """Run image OCR through Azure, then local Tesseract."""
    try:
        text, boxes = await extract_text_and_boxes_with_azure([image_bytes])
        if text.strip() or boxes:
            return text, boxes
    except Exception as exc:
        print(f"[ocr] Azure image OCR failed; using Tesseract: {exc!r}")

    boxes = _tesseract_boxes_from_image(image_bytes)
    return text_and_offsets_from_word_boxes(boxes)


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
    # Vision/Tesseract already return boxes in document reading order. Sorting
    # every box globally by top/left breaks multi-column documents and can move
    # a heading from the beginning to the end of the extracted text.
    ordered = word_boxes
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

    with_offsets: list[dict] = []
    cursor = 0
    used_ranges: set[tuple[int, int]] = set()
    # Keep the OCR provider's reading order. The text returned by the same OCR
    # pass follows this order, so cursor-based matching remains aligned.
    for box in word_boxes:
        raw = str(box.get("text") or "").strip()
        if not raw:
            continue
        copied = dict(box)
        copied.pop("start", None)
        copied.pop("end", None)

        candidates: list[tuple[int, int]] = []
        search_from = 0
        while True:
            found = text.find(raw, search_from)
            if found < 0:
                break
            candidates.append((found, found + len(raw)))
            search_from = found + max(1, len(raw))

        # Prefer the OCR reading-order continuation, but if embedded PDF text
        # has moved a heading/column, use the first still-unassigned occurrence
        # instead of dropping every subsequent box.
        unused = [item for item in candidates if item not in used_ranges]
        after_cursor = [item for item in unused if item[0] >= cursor]
        chosen = after_cursor[0] if after_cursor else (unused[0] if unused else None)

        if chosen is None:
            match = re.search(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", raw)
            token = match.group(0) if match else None
            if token:
                token_candidates = [
                    (item.start(), item.end())
                    for item in re.finditer(re.escape(token), text)
                    if (item.start(), item.end()) not in used_ranges
                ]
                token_after_cursor = [item for item in token_candidates if item[0] >= cursor]
                chosen = (
                    token_after_cursor[0]
                    if token_after_cursor
                    else (token_candidates[0] if token_candidates else None)
                )

        start = chosen[0] if chosen else -1
        if start >= 0:
            copied["start"] = start
            copied["end"] = chosen[1]
            used_ranges.add(chosen)
            cursor = copied["end"]
        with_offsets.append(copied)
    return with_offsets


def _png_dimensions(image_bytes: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            return (max(1, int(image.width)), max(1, int(image.height)))
    except Exception:
        pass
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
    tessdata_dir = Path(tesseract).resolve().parent.parent / "share" / "tessdata"
    language = "jpn+eng" if (tessdata_dir / "jpn.traineddata").exists() else "eng"
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
                    language,
                    "--psm",
                    "3",
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
    return _tesseract_boxes_from_page_images(page_images)


async def extract_text_and_boxes_from_pdf_page_images(
    page_images: list[bytes],
) -> tuple[str, list[dict]]:
    """OCR rendered PDF pages locally with Tesseract."""
    boxes = _tesseract_boxes_from_page_images(page_images)
    return text_and_offsets_from_word_boxes(boxes)


def _page_images_to_azure_batches(page_images: list[bytes]) -> list[tuple[int, bytes]]:
    """Build PDFs within Azure F0's two-page and four-MB limits."""
    batches: list[tuple[int, bytes]] = []
    for start in range(0, len(page_images), AZURE_FREE_MAX_PAGES):
        sources = page_images[start:start + AZURE_FREE_MAX_PAGES]
        scale, quality = 1.0, 88
        while True:
            converted: list[Image.Image] = []
            try:
                for raw in sources:
                    image = Image.open(BytesIO(raw)).convert("RGB")
                    if scale < 1.0:
                        image = image.resize(
                            (max(50, int(image.width * scale)), max(50, int(image.height * scale))),
                            Image.Resampling.LANCZOS,
                        )
                    converted.append(image)
                output = BytesIO()
                converted[0].save(
                    output,
                    format="PDF",
                    save_all=True,
                    append_images=converted[1:],
                    resolution=150,
                    quality=quality,
                )
                payload = output.getvalue()
            finally:
                for image in converted:
                    image.close()
            if len(payload) < AZURE_FREE_MAX_BYTES:
                batches.append((start, payload))
                break
            if quality > 55:
                quality -= 12
            elif scale > 0.55:
                scale *= 0.8
            else:
                raise ValueError("A page could not be reduced below Azure F0's 4 MB limit.")
    return batches


def _azure_word_box(word: dict, page: dict, page_index: int) -> dict | None:
    polygon = word.get("polygon") or []
    if len(polygon) < 8:
        return None
    xs = [float(polygon[index]) for index in range(0, len(polygon), 2)]
    ys = [float(polygon[index]) for index in range(1, len(polygon), 2)]
    page_width = float(page.get("width") or 1)
    page_height = float(page.get("height") or 1)
    return {
        "text": str(word.get("content") or "").strip(),
        "page_index": page_index,
        "left": max(0.0, min(xs) / page_width),
        "top": max(0.0, min(ys) / page_height),
        "width": max(0.0, (max(xs) - min(xs)) / page_width),
        "height": max(0.0, (max(ys) - min(ys)) / page_height),
    }


async def _analyze_azure_pdf(payload: bytes) -> dict:
    endpoint = settings.azure_document_intelligence_endpoint.rstrip("/")
    key = settings.azure_document_intelligence_key
    if not endpoint or not key:
        raise ValueError("Azure Document Intelligence is not configured.")
    url = f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze"
    auth = {"Ocp-Apim-Subscription-Key": key}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            url,
            params={"api-version": AZURE_API_VERSION},
            headers={**auth, "Content-Type": "application/pdf"},
            content=payload,
        )
        response.raise_for_status()
        operation_url = response.headers.get("operation-location")
        if not operation_url:
            raise ValueError("Azure did not return operation-location.")
        for _ in range(120):
            await asyncio.sleep(1.0)  # F0: one GET operation per second.
            result_response = await client.get(operation_url, headers=auth)
            result_response.raise_for_status()
            result = result_response.json()
            if result.get("status") == "succeeded":
                return result.get("analyzeResult") or {}
            if result.get("status") in {"failed", "canceled"}:
                raise ValueError(f"Azure analysis failed: {result.get('error')}")
    raise TimeoutError("Azure Document Intelligence analysis timed out.")


async def extract_text_and_boxes_with_azure(
    page_images: list[bytes],
) -> tuple[str, list[dict]]:
    texts: list[str] = []
    boxes: list[dict] = []
    for batch_start, payload in _page_images_to_azure_batches(page_images):
        result = await _analyze_azure_pdf(payload)
        content = str(result.get("content") or "").strip()
        if content:
            texts.append(content)
        for local_index, page in enumerate(result.get("pages") or []):
            page_number = int(page.get("pageNumber") or local_index + 1)
            page_index = batch_start + page_number - 1
            for word in page.get("words") or []:
                box = _azure_word_box(word, page, page_index)
                if box and box["text"]:
                    boxes.append(box)
        await asyncio.sleep(1.0)  # F0: one analyze transaction per second.
    text = "\n\n".join(texts).strip()
    return text, attach_word_box_offsets(boxes, text)
