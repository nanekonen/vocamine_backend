from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

import httpx

from app.core.config import settings


def normalize_ipa(value: str | None) -> str:
    """IPAの表記揺れだけを正規化し、音韻情報（長音・強勢）は保持する。"""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = normalized.strip("/[] ")
    normalized = normalized.replace("'", "ˈ").replace("’", "ˈ")
    normalized = normalized.replace("ː", "ː")
    return re.sub(r"\s+", " ", normalized)


async def get_pronunciation_audio(word: str, ipa: str | None) -> bytes:
    normalized_ipa = normalize_ipa(ipa)
    synthesis_text = f"[[ {normalized_ipa} ]]" if normalized_ipa else word.strip()
    cache_identity = f"v1\0{settings.piper_voice}\0{synthesis_text}"
    digest = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
    cache_dir = Path(settings.piper_audio_cache_dir)
    cache_path = cache_dir / digest[:2] / f"{digest}.wav"
    if cache_path.exists():
        return cache_path.read_bytes()

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            settings.piper_http_url,
            json={
                "text": synthesis_text,
                "voice": settings.piper_voice,
                "length_scale": 1.0,
            },
        )
        response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_bytes(response.content)
    temporary_path.replace(cache_path)
    return response.content
