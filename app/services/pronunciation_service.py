from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
import wave
from io import BytesIO
from pathlib import Path

from app.core.config import settings

_voice = None
_load_lock = asyncio.Lock()
_synthesis_lock = asyncio.Lock()


def normalize_ipa(value: str | None) -> str:
    """IPAの表記揺れだけを正規化し、音韻情報（長音・強勢）は保持する。"""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = normalized.strip("/[] ")
    normalized = normalized.replace("'", "ˈ").replace("’", "ˈ")
    return re.sub(r"\s+", " ", normalized)


async def initialize_pronunciation_model() -> None:
    """FastAPI起動時にPiperモデルを1回だけメモリへ読み込む。"""
    global _voice
    if _voice is not None:
        return
    async with _load_lock:
        if _voice is not None:
            return
        from piper import PiperVoice

        _voice = await asyncio.to_thread(
            PiperVoice.load,
            settings.piper_model_path,
        )


async def get_pronunciation_audio(word: str, ipa: str | None) -> bytes:
    normalized_ipa = normalize_ipa(ipa)
    synthesis_text = f"[[ {normalized_ipa} ]]" if normalized_ipa else word.strip()
    cache_identity = f"v2\0{settings.piper_voice}\0{synthesis_text}"
    digest = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
    cache_dir = Path(settings.piper_audio_cache_dir)
    cache_path = cache_dir / digest[:2] / f"{digest}.wav"
    if cache_path.exists():
        return cache_path.read_bytes()

    await initialize_pronunciation_model()

    def synthesize() -> bytes:
        buffer = BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            _voice.synthesize_wav(synthesis_text, wav_file)
        return buffer.getvalue()

    async with _synthesis_lock:
        # 同じ音声を待機中の別リクエストが生成済みの場合は再利用する。
        if cache_path.exists():
            return cache_path.read_bytes()
        audio = await asyncio.to_thread(synthesize)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(".tmp")
        temporary_path.write_bytes(audio)
        temporary_path.replace(cache_path)
        return audio
