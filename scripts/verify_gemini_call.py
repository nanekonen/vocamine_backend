"""
Gemini API 呼び出し単体の検証スクリプト

使い方:
    cd vocamine_backend
    source .venv/bin/activate
    python scripts/verify_gemini_call.py "run の語義を1つ返して"

オプション:
    --system   systemInstruction に入れる指示
    --model    使用モデル名を上書き
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


load_dotenv(ROOT / ".env")

from app.core.config import settings  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini API 呼び出し単体を検証する")
    parser.add_argument("prompt", help="Gemini に渡す本文")
    parser.add_argument(
        "--system",
        default=(
            "英和辞典の日本語語義を返してください。"
            "漢字1文字だけの要約は禁止です。"
            "動詞なら『走る』『運営する』のように活用語尾まで含めてください。"
        ),
        help="systemInstruction に入れる文",
    )
    parser.add_argument("--model", default=None, help="モデル名の上書き")
    return parser.parse_args()


async def _call_gemini(prompt: str, system_text: str, model: str) -> None:
    key = settings.gemini_api_key
    print(f"api_key_loaded={bool(key)} len={len(key)}")
    print(f"model={model}")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": system_text,
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt,
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 128,
            "thinkingConfig": {
                "thinkingBudget": 0,
            },
        },
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)

    print(f"status={response.status_code}")
    try:
        data = response.json()
    except Exception:
        print(response.text)
        return

    print(json.dumps(data, ensure_ascii=False, indent=2))

    candidates = data.get("candidates") or []
    if not candidates:
        return
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ]
    text = "".join(texts).strip()
    if text:
        print("\n=== generated_text ===")
        print(text)


def main() -> None:
    args = _parse_args()
    model = args.model or settings.gemini_model
    asyncio.run(_call_gemini(args.prompt, args.system, model))


if __name__ == "__main__":
    main()
