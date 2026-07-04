"""
Wiktionary -> Gemini 日本語語義生成の検証スクリプト

使い方:
    cd vocamine_backend
    source .venv/bin/activate
    python scripts/verify_japanese_definition.py run --pos verb

オプション:
    --pos        品詞を絞る。例: noun, verb, adjective, phrase
    --limit      取得した候補のうち何件まで検証するか
    --show-raw   Wiktionary の raw 情報も表示する
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


load_dotenv(ROOT / ".env")

from app.services.dictionary_service import (  # noqa: E402
    fetch_wiktionary_meanings,
    generate_japanese_definition,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wiktionary と Gemini の意味生成を検証する")
    parser.add_argument("term", help="検証したい英単語")
    parser.add_argument("--pos", default=None, help="品詞フィルタ (noun, verb, adjective, phrase など)")
    parser.add_argument("--limit", type=int, default=5, help="検証する候補数")
    parser.add_argument("--show-raw", action="store_true", help="raw 情報も表示する")
    return parser.parse_args()


def _print_item(index: int, item: dict, generated: str | None, show_raw: bool) -> None:
    print(f"\n[{index}] part_of_speech: {item.get('part_of_speech')}")
    print(f"    transitivity : {item.get('transitivity')}")
    print(f"    definition   : {item.get('definition')}")
    if item.get("definition_group"):
        print(f"    def_group    : {item.get('definition_group')}")
    if item.get("definitions"):
        print(f"    definitions  : {item.get('definitions')}")
    if item.get("glosses"):
        print(f"    glosses      : {item.get('glosses')}")
    if item.get("senses"):
        print(f"    senses       : {len(item.get('senses') or [])} item(s)")
    if item.get("examples"):
        print(f"    examples     : {item.get('examples')}")
    print(f"    generated_ja : {generated}")

    if show_raw:
        raw = item.get("raw") or {}
        print("    raw:")
        print(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True))


async def _run(term: str, pos: str | None, limit: int, show_raw: bool) -> None:
    meanings = await fetch_wiktionary_meanings(term, pos)
    print(f"term={term!r} pos={pos!r} fetched={len(meanings)}")

    if not meanings:
        print("候補が見つかりませんでした")
        return

    for index, item in enumerate(meanings[:limit], start=1):
        generated = await generate_japanese_definition(term, item["part_of_speech"], item)
        _print_item(index, item, generated, show_raw)


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args.term, args.pos, args.limit, args.show_raw))


if __name__ == "__main__":
    main()
