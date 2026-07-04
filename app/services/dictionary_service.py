from __future__ import annotations
import asyncio
import contextlib
import json
import io
import httpx
import sys
from pathlib import Path
from typing import Optional
from app.core.config import settings
from app.schemas.schemas import PartOfSpeech, TransitivityType

WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"

POS_MAP = {
    "noun": PartOfSpeech.noun.value,
    "proper noun": PartOfSpeech.noun.value,
    "name": PartOfSpeech.noun.value,
    "verb": PartOfSpeech.verb.value,
    "adjective": PartOfSpeech.adjective.value,
    "adj": PartOfSpeech.adjective.value,
    "adverb": PartOfSpeech.adverb.value,
    "adv": PartOfSpeech.adverb.value,
    "pronoun": PartOfSpeech.pronoun.value,
    "preposition": PartOfSpeech.preposition.value,
    "conjunction": PartOfSpeech.conjunction.value,
    "interjection": PartOfSpeech.interjection.value,
    "determiner": PartOfSpeech.determiner.value,
    "article": PartOfSpeech.article.value,
    "numeral": PartOfSpeech.numeral.value,
    "prefix": PartOfSpeech.prefix.value,
    "suffix": PartOfSpeech.suffix.value,
    "phrase": PartOfSpeech.phrase.value,
    "prepositional phrase": PartOfSpeech.phrase.value,
    "proverb": PartOfSpeech.phrase.value,
    "idiom": PartOfSpeech.phrase.value,
    "abbreviation": PartOfSpeech.abbreviation.value,
    "initialism": PartOfSpeech.abbreviation.value,
    "acronym": PartOfSpeech.abbreviation.value,
}


def normalize_part_of_speech(pos: Optional[str]) -> Optional[str]:
    if not pos:
        return None
    return POS_MAP.get(pos.strip().lower())


async def fetch_wiktionary_wikitext(term: str) -> Optional[str]:
    """Wiktionary の MediaWiki API からページ本文の wikitext を取得する。"""
    headers = {"User-Agent": settings.wiktionary_user_agent}
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": term,
    }
    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        response = await client.get(WIKTIONARY_API, params=params)

    if response.status_code != 200:
        return None

    pages = response.json().get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None

    revisions = pages[0].get("revisions") or []
    if not revisions:
        return None

    revision = revisions[0]
    slots = revision.get("slots") or {}
    main_slot = slots.get("main") or {}
    return main_slot.get("content") or revision.get("content")


def _parse_wiktextract_page(term: str, wikitext: str) -> list[dict]:
    import nltk
    from wiktextract import parse_page
    from wiktextract.config import WiktionaryConfig
    from wikitextprocessor import Wtp

    nltk_data_dir = Path(sys.prefix) / "nltk_data"
    if str(nltk_data_dir) not in nltk.data.path:
        nltk.data.path.insert(0, str(nltk_data_dir))

    config = WiktionaryConfig(
        capture_languages=["English"],
        capture_translations=True,
        capture_pronunciation=True,
        capture_linkages=True,
        capture_compounds=True,
        capture_redirects=False,
        capture_examples=True,
        capture_etymologies=True,
        capture_inflections=True,
    )
    ctx = Wtp(quiet=True)
    ctx.analyze_templates()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return parse_page(ctx, term, wikitext, config)


def _glosses_from_sense(sense: dict) -> list[str]:
    glosses = sense.get("glosses") or []
    if glosses:
        return [gloss for gloss in glosses if isinstance(gloss, str) and gloss.strip()]
    raw_glosses = sense.get("raw_glosses") or []
    return [gloss for gloss in raw_glosses if isinstance(gloss, str) and gloss.strip()]


def _examples_from_sense(sense: dict) -> list[str]:
    examples: list[str] = []
    for example in sense.get("examples") or []:
        if isinstance(example, str):
            text = example
        elif isinstance(example, dict):
            text = example.get("text") or example.get("english") or ""
        else:
            text = ""
        if text:
            examples.append(text)
    return examples


def _sense_payload(entry: dict, sense: dict) -> dict:
    return {
        "definition": (_glosses_from_sense(sense) or [None])[0],
        "glosses": _glosses_from_sense(sense),
        "raw_glosses": list(sense.get("raw_glosses") or []),
        "examples": _examples_from_sense(sense),
        "tags": _collect_tags(entry, sense),
        "transitivity": _infer_transitivity(entry, sense),
    }


def _collect_tags(*sources: dict) -> list[str]:
    tags: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("tags", "categories", "topics"):
            for tag in source.get(key) or []:
                if isinstance(tag, str):
                    normalized = tag.strip().lower()
                    if normalized:
                        tags.append(normalized)
    return tags


def _normalize_transitivity(*sources: dict) -> Optional[str]:
    tags = set(_collect_tags(*sources))
    if "both" in tags or ("transitive" in tags and "intransitive" in tags):
        return TransitivityType.both.value
    if "transitive" in tags:
        return TransitivityType.transitive.value
    if "intransitive" in tags:
        return TransitivityType.intransitive.value
    return None


def _infer_transitivity(entry: dict, sense: dict) -> Optional[str]:
    raw_pos = (entry.get("pos") or "").strip().lower()
    if "transitive" in raw_pos and "intransitive" in raw_pos:
        return TransitivityType.both.value
    if "transitive" in raw_pos:
        return TransitivityType.transitive.value
    if "intransitive" in raw_pos:
        return TransitivityType.intransitive.value
    return _normalize_transitivity(entry, sense)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge_definition_texts(definitions: list[str]) -> str:
    merged = _dedupe_preserve_order([definition.strip() for definition in definitions if definition and definition.strip()])
    return " / ".join(merged)


async def fetch_wiktionary_meanings(term: str, part_of_speech: Optional[str] = None) -> list[dict]:
    """
    Wiktionary API から取得した wikitext を本物の wiktextract.parse_page に通す。
    返り値: [{"part_of_speech": str, "transitivity": str | None, "definition": str, "definitions": list[str], ...}]
    """
    wikitext = await fetch_wiktionary_wikitext(term)
    if not wikitext:
        return []

    data = await asyncio.to_thread(_parse_wiktextract_page, term, wikitext)
    requested_pos = normalize_part_of_speech(part_of_speech)
    results: list[dict] = []
    grouped_indexes: dict[tuple[str, Optional[str]], int] = {}

    for entry in data:
        raw_pos = entry.get("pos")
        pos = normalize_part_of_speech(raw_pos)
        if not pos:
            continue
        if requested_pos and pos != requested_pos:
            continue

        for sense in entry.get("senses") or []:
            glosses = _glosses_from_sense(sense)
            if not glosses:
                continue

            transitivity = _infer_transitivity(entry, sense)
            key = (pos, transitivity)
            if key not in grouped_indexes:
                grouped_indexes[key] = len(results)
                sense_item = _sense_payload(entry, sense)
                results.append({
                    "part_of_speech": pos,
                    "transitivity": transitivity,
                    "definition": _merge_definition_texts(glosses),
                    "definition_group": _merge_definition_texts(glosses),
                    "definitions": list(glosses),
                    "glosses": list(glosses),
                    "raw_glosses": list(sense.get("raw_glosses") or []),
                    "examples": _examples_from_sense(sense),
                    "senses": [sense_item],
                    "raw": {
                        "wiktextract_entries": [entry],
                        "wiktextract_senses": [sense],
                    },
                })
                continue

            item = results[grouped_indexes[key]]
            item["definitions"] = _dedupe_preserve_order((item.get("definitions") or []) + glosses)
            item["definition"] = _merge_definition_texts(item["definitions"])
            item["definition_group"] = item["definition"]
            item["glosses"] = _dedupe_preserve_order((item.get("glosses") or []) + glosses)
            item["raw_glosses"] = _dedupe_preserve_order(
                (item.get("raw_glosses") or []) + list(sense.get("raw_glosses") or [])
            )
            item["examples"] = _dedupe_preserve_order(
                (item.get("examples") or []) + _examples_from_sense(sense)
            )
            item["senses"] = (item.get("senses") or []) + [_sense_payload(entry, sense)]
            raw = item.get("raw") or {}
            entries = raw.get("wiktextract_entries") or []
            entries.append(entry)
            raw["wiktextract_entries"] = entries
            senses = raw.get("wiktextract_senses") or []
            senses.append(sense)
            raw["wiktextract_senses"] = senses
            item["raw"] = raw

    return results


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _build_generation_context(term: str, part_of_speech: str, source_item: dict) -> str:
    payload = {
        "term": term,
        "part_of_speech": part_of_speech,
        "transitivity": source_item.get("transitivity"),
        "definition": source_item.get("definition"),
        "definitions": source_item.get("definitions") or [],
        "definition_group": _merge_definition_texts(source_item.get("definitions") or []),
        "senses": source_item.get("senses") or [],
    }
    return _compact_json(payload)


def _is_too_short_japanese_definition(text: str) -> bool:
    compact = text.strip().strip("「」『』\"'")
    if not compact:
        return True
    return len(compact) <= 1


def _clean_generated_definition(text: str) -> str:
    return text.strip().strip("「」『』\"'` \n\t、,。")


async def generate_japanese_definition(term: str, part_of_speech: str, source_item: dict) -> Optional[str]:
    """
    Gemini で英和辞典ふうの日本語語義を生成する。
    未設定または失敗時は None を返す。
    """
    if not settings.gemini_api_key:
        return None
    context_json = _build_generation_context(term, part_of_speech, source_item)
    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "あなたは英和辞典の編集者です。"
                        "英語の見出し語や説明文を繰り返さず、日本語の語義だけを返してください。"
                        "出力は日本語のみで、Markdown や箇条書き、引用符、注釈は不要です。"
                        "漢字1文字だけの要約は禁止です。必ず辞書形の語または短い句にしてください。"
                        "動詞なら『走る』『運営する』のように活用語尾まで含めてください。"
                        "1つに絞らなくてよく、必要なら『走る、速く進む、疾走する』のように、"
                        "短い語義をベタ書きで並べてください。"
                        "語義は入力された定義文群から必要なものを落とさず、例文や説明文は書かないでください。"
                        "同じ品詞・同じ transitivity の定義文群は1つのまとまりとして扱ってください。"
                        "definition_group を中心に、必要なら definitions と senses も参照してください。"
                    ),
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "以下は Wiktionary から取得した情報です。"
                            "definition_group をまず見て、同じ品詞・同じ transitivity に属する定義文群としてまとめてください。"
                            "そのうえで definitions, transitivity, senses を含めて、"
                            "この情報をすべて参考にして日本語の語義を返してください。"
                            "必要なら複数の短い語義を1行でベタ書きしてかまいません。"
                            "ただし例文や解説は含めず、同じ意味の重複だけを自然にまとめてください。\n"
                            "悪い例: 走\n"
                            "良い例: 走る、速く進む\n"
                            f"{context_json}"
                        ),
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "thinkingConfig": {
                "thinkingBudget": 0,
            },
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        return None

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ]
    text = _clean_generated_definition("".join(texts))
    if _is_too_short_japanese_definition(text):
        return None
    return text
