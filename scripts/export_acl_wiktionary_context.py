"""
Academic Collocation List を Wiktionary/wiktextract 情報つきCSVにする。

Geminiなどの生成AIは呼ばない。Wiktionary APIは titles をまとめて
バッチ取得し、取得した wikitext をローカルで wiktextract JSON にする。

出力:
    output/academic_collocation_wiktionary_context.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ACL_PATH = ROOT / "phrase_list" / "The_Academic_Collocation_List(Academic Collocation List).csv"
DEFAULT_OUTPUT = ROOT / "output" / "academic_collocation_wiktionary_context.csv"
WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"
TITLE_BATCH_SIZE = 50

TRAILING_POS_RE = re.compile(r"\s*\((?:adj|adv|n|v|vpp)\)\s*$", re.IGNORECASE)

load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.services.dictionary_service import _parse_wiktextract_page  # noqa: E402


FIELDNAMES = [
    "phrase",
    "component_i",
    "component_ii",
    "component_i_lookup",
    "component_ii_lookup",
    "component_i_raw",
    "component_ii_raw",
    "phrase_wiktionary_found",
    "phrase_wiktextract_json",
    "component_i_wiktionary_found",
    "component_i_wiktextract_json",
    "component_ii_wiktionary_found",
    "component_ii_wiktextract_json",
]


def normalize_phrase(value: str) -> str:
    phrase = value.strip().lower().replace("\ufeff", "")
    phrase = re.sub(r"\s+", " ", phrase)
    phrase = re.sub(r"\s+['’]s\b", "'s", phrase)
    return phrase.strip(" ,.;:")


def strip_acl_pos(value: str) -> str:
    text = value.strip().lower().replace("\ufeff", "")
    text = TRAILING_POS_RE.sub("", text)
    text = re.sub(r"\((?:adj|adv|n|v|vpp)\)", "", text, flags=re.IGNORECASE)
    text = text.replace("(", "").replace(")", "")
    return normalize_phrase(text)


def lookup_term_from_acl_component(value: str) -> str:
    text = value.strip().lower().replace("\ufeff", "")
    text = re.sub(r"\([^)]*\)", "", text)
    return normalize_phrase(text)


def iter_acl_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_left = ""
    current_left_raw = ""
    seen: set[str] = set()

    with ACL_PATH.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.reader(file)
        for row in reader:
            if len(row) < 3 or row[0] == "#":
                continue

            left_raw = row[1] or ""
            right_raw = row[2] or ""
            left = strip_acl_pos(left_raw)
            right = strip_acl_pos(right_raw)
            if left:
                current_left = left
                current_left_raw = left_raw
            if not current_left or not right:
                continue

            phrase = normalize_phrase(f"{current_left} {right}")
            if " " not in phrase or phrase in seen:
                continue
            seen.add(phrase)
            rows.append({
                "phrase": phrase,
                "component_i": current_left,
                "component_ii": right,
                "component_i_lookup": lookup_term_from_acl_component(current_left_raw),
                "component_ii_lookup": lookup_term_from_acl_component(right_raw),
                "component_i_raw": current_left_raw,
                "component_ii_raw": right_raw,
            })

    return rows


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


async def fetch_wikitext_batch(titles: list[str], sleep_seconds: float) -> dict[str, str]:
    results: dict[str, str] = {}
    headers = {"User-Agent": settings.wiktionary_user_agent}

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        batches = list(chunks(titles, TITLE_BATCH_SIZE))
        for index, batch in enumerate(batches, start=1):
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(batch),
            }
            for attempt in range(5):
                response = await client.get(WIKTIONARY_API, params=params)
                if response.status_code != 429:
                    break
                wait = 30 * (attempt + 1)
                print(f"429 rate limited. wait {wait}s")
                await asyncio.sleep(wait)
            if response.status_code != 200:
                print(f"batch {index}/{len(batches)} status={response.status_code}")
                continue

            pages = response.json().get("query", {}).get("pages", [])
            for page in pages:
                if page.get("missing"):
                    continue
                title = normalize_phrase(page.get("title") or "")
                revisions = page.get("revisions") or []
                if not title or not revisions:
                    continue
                revision = revisions[0]
                slots = revision.get("slots") or {}
                main_slot = slots.get("main") or {}
                content = main_slot.get("content") or revision.get("content") or ""
                if content:
                    results[title] = content

            print(f"fetched batch {index}/{len(batches)} titles={len(batch)} found_total={len(results)}")
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)

    return results


def parse_wiktextract_cache(wikitexts: dict[str, str]) -> dict[str, list[dict]]:
    parsed: dict[str, list[dict]] = {}
    for index, (title, wikitext) in enumerate(wikitexts.items(), start=1):
        try:
            parsed[title] = _parse_wiktextract_page(title, wikitext)
        except Exception as e:
            parsed[title] = [{"error": f"{type(e).__name__}: {e}"}]
        if index % 50 == 0:
            print(f"parsed {index}/{len(wikitexts)}")
    return parsed


async def run(output_path: Path, sleep_seconds: float) -> None:
    rows = iter_acl_rows()
    titles = sorted({
        value
        for row in rows
        for value in (row["phrase"], row["component_i_lookup"], row["component_ii_lookup"])
        if value
    })
    print(f"rows={len(rows)} unique_titles={len(titles)}")

    wikitexts = await fetch_wikitext_batch(titles, sleep_seconds)
    parsed = parse_wiktextract_cache(wikitexts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            phrase_data = parsed.get(row["phrase"], [])
            component_i_data = parsed.get(row["component_i_lookup"], [])
            component_ii_data = parsed.get(row["component_ii_lookup"], [])
            writer.writerow({
                **row,
                "phrase_wiktionary_found": "true" if phrase_data else "false",
                "phrase_wiktextract_json": compact_json(phrase_data),
                "component_i_wiktionary_found": "true" if component_i_data else "false",
                "component_i_wiktextract_json": compact_json(component_i_data),
                "component_ii_wiktionary_found": "true" if component_ii_data else "false",
                "component_ii_wiktextract_json": compact_json(component_ii_data),
            })
            if index % 250 == 0:
                print(f"wrote {index}/{len(rows)}")

    print(f"done: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--sleep", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(Path(args.output), args.sleep))


if __name__ == "__main__":
    main()
