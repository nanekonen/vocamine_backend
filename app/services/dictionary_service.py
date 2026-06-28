from __future__ import annotations
import httpx
from typing import Optional

FREE_DICT_API = "https://api.dictionaryapi.dev/api/v2/entries/en"


async def fetch_word_meanings(word: str) -> list[dict]:
    """
    Free Dictionary API から単語の意味・品詞・例文を取得する。
    返り値: [{"part_of_speech": str, "definition": str, "example": str | None}]
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{FREE_DICT_API}/{word.lower()}")

    if response.status_code != 200:
        return []

    data = response.json()
    results: list[dict] = []

    for entry in data:
        for meaning in entry.get("meanings", []):
            pos = meaning.get("partOfSpeech", "")
            for defn in meaning.get("definitions", []):
                results.append({
                    "part_of_speech": pos,
                    "definition": defn.get("definition", ""),
                    "example": defn.get("example"),
                })

    return results