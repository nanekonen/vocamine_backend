from __future__ import annotations
import asyncio
import contextlib
import json
import io
import re
import httpx
import sys
import time
from pathlib import Path
from typing import Optional
from app.core.config import settings
from app.schemas.schemas import CountabilityType, PartOfSpeech, TransitivityType

WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"
JAPANESE_TEXT_RE = re.compile(r"[ぁ-んァ-ン一-龯]")
_wiktionary_wikitext_cache: dict[str, Optional[str]] = {}


class WiktionaryUnavailableError(RuntimeError):
    pass

_gemini_key_cooldowns: dict[str, float] = {}
_gemini_key_cursor = 0


def _configured_gemini_keys() -> list[str]:
    """Return configured keys without exposing or using duplicates."""
    keys: list[str] = []
    for value in (settings.gemini_api_key, settings.gemini_api_key2):
        key = value.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _available_gemini_keys() -> list[str]:
    """Round-robin healthy keys so consecutive calls do not favor one project."""
    global _gemini_key_cursor
    now = time.monotonic()
    keys = [
        key
        for key in _configured_gemini_keys()
        if _gemini_key_cooldowns.get(key, 0.0) <= now
    ]
    if not keys:
        return []
    offset = _gemini_key_cursor % len(keys)
    _gemini_key_cursor += 1
    return keys[offset:] + keys[:offset]


def _gemini_retry_delay(response: httpx.Response) -> float:
    retry_header = response.headers.get("retry-after")
    retry_match = re.search(
        r"retry in ([0-9]+(?:\.[0-9]+)?)s",
        response.text,
        flags=re.IGNORECASE,
    )
    try:
        seconds = float(retry_header or "")
    except ValueError:
        seconds = float(retry_match.group(1)) if retry_match else 60.0
    return min(65.0, max(1.0, seconds + 0.5))


def _mark_gemini_key_failure(api_key: str, status_code: int | None) -> None:
    if status_code == 429:
        # The precise retry delay is applied by the caller when available.
        cooldown = 60.0
    elif status_code in {400, 401, 403, 404}:
        cooldown = 300.0
    else:
        cooldown = 15.0
    _gemini_key_cooldowns[api_key] = time.monotonic() + cooldown


def _mark_gemini_key_healthy(api_key: str) -> None:
    _gemini_key_cooldowns.pop(api_key, None)


async def _post_gemini_with_failover(
    payload: dict,
    *,
    timeout: float,
) -> Optional[httpx.Response]:
    """Try healthy keys in rotation, immediately falling over to the other key."""
    keys = _available_gemini_keys()
    if not keys:
        return None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for key_index, api_key in enumerate(keys, start=1):
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{settings.gemini_model}:generateContent?key={api_key}"
            )
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                _mark_gemini_key_failure(api_key, None)
                print(
                    f"[gemini] key {key_index}/{len(keys)} request failed: "
                    f"{type(exc).__name__}"
                )
                continue
            if response.status_code == 200:
                _mark_gemini_key_healthy(api_key)
                return response
            if response.status_code == 429:
                _gemini_key_cooldowns[api_key] = (
                    time.monotonic() + _gemini_retry_delay(response)
                )
            else:
                _mark_gemini_key_failure(api_key, response.status_code)
            print(
                f"[gemini] key {key_index}/{len(keys)} unavailable "
                f"(status={response.status_code}); trying another key."
            )
    return None

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
    if term in _wiktionary_wikitext_cache:
        return _wiktionary_wikitext_cache[term]
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
    response: Optional[httpx.Response] = None
    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        for attempt in range(3):
            try:
                response = await client.get(WIKTIONARY_API, params=params)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise WiktionaryUnavailableError(
                        f"Wiktionary request failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.75 * (2 ** attempt))
                continue
            if response.status_code == 200:
                break
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.75 * (2 ** attempt))
                    continue
            raise WiktionaryUnavailableError(
                f"Wiktionary returned HTTP {response.status_code}."
            )

    if response is None or response.status_code != 200:
        raise WiktionaryUnavailableError("Wiktionary did not return a response.")

    pages = response.json().get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        _wiktionary_wikitext_cache[term] = None
        return None

    revisions = pages[0].get("revisions") or []
    if not revisions:
        _wiktionary_wikitext_cache[term] = None
        return None

    revision = revisions[0]
    slots = revision.get("slots") or {}
    main_slot = slots.get("main") or {}
    result = main_slot.get("content") or revision.get("content")
    _wiktionary_wikitext_cache[term] = result
    return result


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
    # English entryの存在判定と、アプリがその品詞名を正規化できるかは別。
    # character/symbol等の未対応品詞でもEnglish sectionがあれば英語項目として通す。
    variants = [term]
    upper = term.upper()
    if upper != term:
        variants.append(upper)
    for variant in variants:
        wikitext = await fetch_wiktionary_wikitext(variant)
        if not wikitext:
            continue
        data = await asyncio.to_thread(_parse_wiktextract_page, variant, wikitext)
        if any(entry.get("lang_code") == "en" for entry in data):
            return True
        if _auxiliary_meanings_from_wikitext(variant, wikitext):
            return True
    return False


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


def _is_invalid_japanese_definition(text: str) -> bool:
    compact = text.strip().strip("「」『』\"'")
    # 「空」「金」などの1文字で成立する訳を排除しない。
    return not compact or not JAPANESE_TEXT_RE.search(compact)


def _clean_generated_definition(text: str) -> str:
    return text.strip().strip("「」『』\"'` \n\t、,。")


async def generate_japanese_definition(term: str, part_of_speech: str, source_item: dict) -> Optional[str]:
    """
    Gemini で英和辞典ふうの日本語語義を生成する。
    未設定または失敗時は None を返す。
    """
    if not _configured_gemini_keys():
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
    response = await _post_gemini_with_failover(payload, timeout=20.0)
    if response is None:
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
    if _is_invalid_japanese_definition(text):
        return None
    return text


async def generate_fallback_japanese_definition(term: str, part_of_speech: str) -> Optional[str]:
    """
    Wiktionary で該当品詞の語義が取れない場合に、見出し語と品詞だけから日本語語義を生成する。
    """
    if not _configured_gemini_keys():
        return None
    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "あなたは英和辞典の編集者です。"
                        "与えられた英語の見出し語と品詞に対応する、日本語の語義だけを返してください。"
                        "出力は日本語のみで、Markdown、箇条書き、引用符、注釈、例文は不要です。"
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
    response = await _post_gemini_with_failover(payload, timeout=20.0)
    if response is None:
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
    if _is_invalid_japanese_definition(text):
        return None
    return text


async def _generate_japanese_definitions_batch_once(
    requests: list[dict],
    api_key: str,
) -> dict[str, str]:
    """
    複数の語義を1回の Gemini 呼び出しで日本語化する。
    requests: [{"id": str, "term": str, "part_of_speech": str, "definition_en": str | None, ...}]
    """
    if not api_key or not requests:
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
            # gemini-2.5-flash の出力上限まで許可する。教材内の不足語義は
            # 初回にすべてまとめて送り、JSONが途中で切れる余地を減らす。
            "maxOutputTokens": 65536,
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": 0,
            },
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={api_key}"
    )
    # 教材内の不足分を1回にまとめるため、通常の単語単体APIより長く待つ。
    # 45秒で切ると、Gemini側では生成中でも応答を捨てて全件再試行してしまう。
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            _mark_gemini_key_failure(api_key, None)
            print(f"[gemini-batch] request failed: {type(exc).__name__}")
            return {}

    if response.status_code != 200:
        if response.status_code == 429:
            retry_seconds = _gemini_retry_delay(response)
            _gemini_key_cooldowns[api_key] = time.monotonic() + retry_seconds
            print(
                f"[gemini-batch] quota throttled; key cooling down for "
                f"{retry_seconds:g}s."
            )
        else:
            _mark_gemini_key_failure(api_key, response.status_code)
        print(
            f"[gemini-batch] status={response.status_code} "
            f"body={response.text[:500]}"
        )
        return {}
    _mark_gemini_key_healthy(api_key)

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return {}
    finish_reason = str(candidates[0].get("finishReason") or "")
    if finish_reason and finish_reason != "STOP":
        usage = data.get("usageMetadata") or {}
        print(
            f"[gemini-batch] finish_reason={finish_reason} "
            f"output_tokens={usage.get('candidatesTokenCount')}"
        )
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
        if not _is_invalid_japanese_definition(cleaned):
            results[key] = cleaned
    return results


async def generate_japanese_definitions_batch(
    requests: list[dict],
    max_attempts: int = 3,
) -> dict[str, str]:
    """Generate all definitions, distributing work over every healthy API key."""
    pending = [
        item
        for item in requests
        if item.get("id") and item.get("term") and item.get("part_of_speech")
    ]
    results: dict[str, str] = {}
    for attempt in range(max_attempts):
        if not pending:
            break
        keys = _available_gemini_keys()
        if not keys:
            configured = _configured_gemini_keys()
            if not configured:
                break
            next_ready_at = min(
                _gemini_key_cooldowns.get(key, 0.0) for key in configured
            )
            wait_seconds = max(0.0, next_ready_at - time.monotonic())
            # Quota retry windows are short enough to wait inside this request.
            # Invalid/disabled keys use a long cooldown and fail fast instead.
            if wait_seconds <= 65.0 and attempt + 1 < max_attempts:
                await asyncio.sleep(wait_seconds)
                keys = _available_gemini_keys()
            if not keys:
                break

        # Two healthy projects process disjoint halves concurrently. With one
        # healthy key, that key receives the whole remaining set.
        groups: list[list[dict]] = [[] for _ in keys]
        for index, item in enumerate(pending):
            groups[index % len(keys)].append(item)
        generated_parts = await asyncio.gather(*(
            _generate_japanese_definitions_batch_once(group, api_key)
            for api_key, group in zip(keys, groups)
            if group
        ))
        generated = {
            request_id: definition
            for part in generated_parts
            for request_id, definition in part.items()
        }
        results.update(generated)
        pending = [
            item
            for item in pending
            if str(item.get("id")) not in results
        ]
        if pending and attempt + 1 < max_attempts:
            print(
                f"[gemini-batch] {len(pending)} item(s) missing; "
                f"retrying ({attempt + 2}/{max_attempts})."
            )
            await asyncio.sleep(0.75 * (2 ** attempt))
    if pending:
        print(
            "[gemini-batch] generation remained incomplete for ids="
            + ",".join(str(item.get("id")) for item in pending)
        )
    return results
