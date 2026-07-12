from __future__ import annotations
import asyncio
import contextlib
import json
import io
import re
import httpx
import sys
from pathlib import Path
from typing import Optional
from app.core.config import settings
from app.schemas.schemas import CountabilityType, PartOfSpeech, TransitivityType

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
    "pron": PartOfSpeech.pronoun.value,
    "preposition": PartOfSpeech.preposition.value,
    "prep": PartOfSpeech.preposition.value,
    "conjunction": PartOfSpeech.conjunction.value,
    "conj": PartOfSpeech.conjunction.value,
    "interjection": PartOfSpeech.interjection.value,
    "intj": PartOfSpeech.interjection.value,
    "determiner": PartOfSpeech.determiner.value,
    "det": PartOfSpeech.determiner.value,
    "article": PartOfSpeech.article.value,
    "auxiliary": PartOfSpeech.auxiliary.value,
    "auxiliary verb": PartOfSpeech.auxiliary.value,
    "modal": PartOfSpeech.auxiliary.value,
    "modal verb": PartOfSpeech.auxiliary.value,
    "numeral": PartOfSpeech.determiner.value,
    "num": PartOfSpeech.determiner.value,
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

REQUESTED_POS_FALLBACKS = {
    PartOfSpeech.determiner.value: [
        PartOfSpeech.pronoun.value,
        PartOfSpeech.adjective.value,
    ],
}

AUXILIARY_LABEL_RE = re.compile(
    r"\{\{(?:lb|label|tlb|senseid)\|en\|[^{}]*(?:auxiliary|modal)[^{}]*\}\}|"
    r"\[\[auxiliary verb(?:\|[^\]]+)?\]\]|"
    r"\[\[modal verb(?:\|[^\]]+)?\]\]|"
    r"\{\{(?:ng|n-g|non-gloss)\|[^{}]*(?:auxiliary|modal)[^{}]*\}\}",
    re.IGNORECASE,
)
AUXILIARY_HEADING_RE = re.compile(r"=+\s*(?:auxiliary|modal)(?:\s+verb)?s?\s*=+", re.IGNORECASE)
AUXILIARY_BLOCK_RE = re.compile(r"\b(?:as an?|used as an?)\s+(?:auxiliary|modal)(?:\s+verb)?\b", re.IGNORECASE)


def normalize_part_of_speech(pos: Optional[str]) -> Optional[str]:
    if not pos:
        return None
    return POS_MAP.get(pos.strip().lower())


def _english_wikitext(wikitext: str) -> str:
    lines = wikitext.splitlines()
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if line.strip() == "==English==":
            start = index + 1
            break
    if start is None:
        return wikitext

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("==") and not lines[index].startswith("==="):
            end = index
            break
    return "\n".join(lines[start:end])


def _plain_wikitext_definition(line: str) -> str:
    text = re.sub(r"^#+\s*", "", line.strip())
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"\{\{(?:lb|label|tlb|senseid|sid|defdate)\|[^{}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{m\|en\|([^{}|]*)(?:\|[^{}]*)?\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{l\|en\|([^{}|]*)(?:\|[^{}]*)?\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{w\|([^{}|]*)(?:\|([^{}]*))?\}\}", lambda match: match.group(2) or match.group(1), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{glossary\|([^{}|]*)(?:\|([^{}]*))?\}\}", lambda match: match.group(2) or match.group(1), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{cap\|([^{}|]*)\}\}", lambda match: match.group(1).capitalize(), text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[[^]|]+\|([^]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{(?:ng|n-g|non-gloss)\|([^{}]*)\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ;:.")


def _auxiliary_meanings_from_wikitext(term: str, wikitext: str) -> list[dict]:
    definitions: list[str] = []
    raw_lines: list[str] = []
    in_auxiliary_heading = False
    in_auxiliary_block = False
    for line in _english_wikitext(wikitext).splitlines():
        stripped = line.strip()
        if re.match(r"^={3,}", stripped):
            in_auxiliary_heading = bool(AUXILIARY_HEADING_RE.search(stripped))
            in_auxiliary_block = False
            continue
        if stripped and not stripped.startswith("#") and AUXILIARY_LABEL_RE.search(stripped):
            in_auxiliary_heading = True
            in_auxiliary_block = False
            continue
        if not stripped.startswith("#"):
            continue
        if re.match(r"^#+[:*]", stripped):
            continue
        has_auxiliary_label = bool(AUXILIARY_LABEL_RE.search(stripped))
        if stripped.startswith("# ") and AUXILIARY_BLOCK_RE.search(stripped):
            in_auxiliary_block = True
            continue
        if stripped.startswith("# ") and not has_auxiliary_label:
            in_auxiliary_block = False
        if not (in_auxiliary_heading or in_auxiliary_block or has_auxiliary_label):
            continue
        definition = _plain_wikitext_definition(stripped)
        if not definition or AUXILIARY_BLOCK_RE.search(definition):
            continue
        definitions.append(definition)
        raw_lines.append(stripped)

    definitions = _dedupe_preserve_order(definitions)
    if not definitions:
        return []

    return [{
        "part_of_speech": PartOfSpeech.auxiliary.value,
        "transitivity": None,
        "definition": _merge_definition_texts(definitions),
        "definition_group": _merge_definition_texts(definitions),
        "definitions": definitions,
        "glosses": definitions,
        "raw_glosses": raw_lines,
        "examples": [],
        "senses": [{"glosses": [definition]} for definition in definitions],
        "raw": {
            "wiktionary_wikitext_lines": raw_lines,
            "wiktextract_part_of_speech": PartOfSpeech.auxiliary.value,
        },
    }]


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
    def is_valid_gloss(gloss: str) -> bool:
        text = gloss.strip()
        if not text or text in {".", "…", "..."}:
            return False
        if "Template:" in text:
            return False
        if len(text.replace(".", "").strip()) <= 1:
            return False
        return True

    glosses = sense.get("glosses") or []
    if glosses:
        return [
            gloss.strip()
            for gloss in glosses
            if isinstance(gloss, str) and is_valid_gloss(gloss)
        ]
    raw_glosses = sense.get("raw_glosses") or []
    return [
        gloss.strip()
        for gloss in raw_glosses
        if isinstance(gloss, str) and is_valid_gloss(gloss)
    ]


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
        "countability": _infer_countability(entry, sense),
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


def _normalize_countability(*sources: dict) -> Optional[str]:
    tags = set(_collect_tags(*sources))
    has_countable = bool({"countable", "count", "count noun"} & tags)
    has_uncountable = bool({"uncountable", "uncount", "mass noun", "mass"} & tags)
    if has_countable and has_uncountable:
        return CountabilityType.both.value
    if has_countable:
        return CountabilityType.countable.value
    if has_uncountable:
        return CountabilityType.uncountable.value
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


def _infer_countability(entry: dict, sense: dict) -> Optional[str]:
    raw_pos = (entry.get("pos") or "").strip().lower()
    if "countable" in raw_pos and "uncountable" in raw_pos:
        return CountabilityType.both.value
    if "uncountable" in raw_pos:
        return CountabilityType.uncountable.value
    if "countable" in raw_pos:
        return CountabilityType.countable.value
    return _normalize_countability(entry, sense)


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
    auxiliary_items = _auxiliary_meanings_from_wikitext(term, wikitext)
    results: list[dict] = []
    grouped_indexes: dict[tuple[str, Optional[str]], int] = {}

    for entry in data:
        raw_pos = entry.get("pos")
        pos = normalize_part_of_speech(raw_pos)
        if not pos:
            continue

        for sense in entry.get("senses") or []:
            glosses = _glosses_from_sense(sense)
            if not glosses:
                continue

            transitivity = _infer_transitivity(entry, sense)
            countability = _infer_countability(entry, sense)
            key = (pos, transitivity, countability)
            if key not in grouped_indexes:
                grouped_indexes[key] = len(results)
                sense_item = _sense_payload(entry, sense)
                results.append({
                    "part_of_speech": pos,
                    "transitivity": transitivity,
                    "countability": countability,
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
                        "wiktextract_part_of_speech": pos,
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
            raw["wiktextract_part_of_speech"] = pos
            item["raw"] = raw

    if not requested_pos:
        return auxiliary_items + results

    exact = [item for item in results if item["part_of_speech"] == requested_pos]
    if exact:
        return exact
    if requested_pos == PartOfSpeech.auxiliary.value and auxiliary_items:
        return auxiliary_items

    for fallback_pos in REQUESTED_POS_FALLBACKS.get(requested_pos, []):
        fallback = [
            {**item, "part_of_speech": requested_pos}
            for item in results
            if item["part_of_speech"] == fallback_pos
        ]
        if fallback:
            return fallback

    return []


async def has_wiktionary_english_entry(term: str) -> bool:
    wikitext = await fetch_wiktionary_wikitext(term)
    if not wikitext:
        return False
    data = await asyncio.to_thread(_parse_wiktextract_page, term, wikitext)
    for entry in data:
        if entry.get("lang_code") == "en" and normalize_part_of_speech(entry.get("pos")):
            return True
    return bool(_auxiliary_meanings_from_wikitext(term, wikitext))


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


async def generate_fallback_japanese_definition(term: str, part_of_speech: str) -> Optional[str]:
    """
    Wiktionary で該当品詞の語義が取れない場合に、見出し語と品詞だけから日本語語義を生成する。
    """
    if not settings.gemini_api_key:
        return None
    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "あなたは英和辞典の編集者です。"
                        "与えられた英語の見出し語と品詞に対応する、日本語の語義だけを返してください。"
                        "出力は日本語のみで、Markdown、箇条書き、引用符、注釈、例文は不要です。"
                        "漢字1文字だけは禁止です。必ず辞書形の語または短い句にしてください。"
                        "その品詞として一般的に使われる意味に限定し、別品詞の意味を混ぜないでください。"
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
                            "Wiktionary からこの品詞の語義を取得できませんでした。"
                            "以下の見出し語と品詞に対応する英和辞典ふうの日本語語義を生成してください。\n"
                            f"term: {term}\n"
                            f"part_of_speech: {part_of_speech}"
                        ),
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 256,
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


async def generate_japanese_definitions_batch(requests: list[dict]) -> dict[str, str]:
    """
    複数の語義を1回の Gemini 呼び出しで日本語化する。
    requests: [{"id": str, "term": str, "part_of_speech": str, "definition_en": str | None, ...}]
    """
    if not settings.gemini_api_key or not requests:
        return {}

    compact_requests = [
        {
            "id": item.get("id"),
            "term": item.get("term"),
            "part_of_speech": item.get("part_of_speech"),
            "definition_en": item.get("definition_en"),
            "transitivity": item.get("transitivity"),
            "countability": item.get("countability"),
        }
        for item in requests
        if item.get("id") and item.get("term") and item.get("part_of_speech")
    ]
    if not compact_requests:
        return {}

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "あなたは英和辞典の編集者です。"
                        "入力JSONの各項目について、日本語の語義だけを生成してください。"
                        "definition_en がある場合はそれを最優先し、ない場合は term と part_of_speech から一般的な語義を生成してください。"
                        "別品詞の意味を混ぜないでください。"
                        "出力は JSON オブジェクトのみで、キーは入力の id、値は日本語語義文字列にしてください。"
                        "Markdown、箇条書き、注釈、例文は不要です。"
                        "漢字1文字だけは禁止です。"
                    ),
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": _compact_json({"items": compact_requests}),
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": 0,
            },
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    )
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            print(f"[gemini-batch] request failed: {exc!r}")
            return {}

    if response.status_code != 200:
        print(
            f"[gemini-batch] status={response.status_code} "
            f"body={response.text[:500]}"
        )
        return {}

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return {}
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(f"[gemini-batch] invalid JSON: {text[:500]}")
        return {}
    if not isinstance(parsed, dict):
        return {}

    results: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        cleaned = _clean_generated_definition(value)
        if not _is_too_short_japanese_definition(cleaned):
            results[key] = cleaned
    return results
