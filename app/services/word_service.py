from __future__ import annotations
import asyncio
import csv
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import spacy
from spacy.matcher import DependencyMatcher, PhraseMatcher
from spacy.util import filter_spans

from app.db.supabase import get_supabase
from app.schemas.schemas import PartOfSpeech
from app.services.word_lookup_service import (
    batch_generate_missing_japanese_definitions,
    ensure_meanings_for_word,
    get_or_create_word,
    has_cached_wiktionary_english_entry,
    record_wordbook_registration,
)

# ユーザーレベル → CEFR-J tier の上限マッピング
LEVEL_TIER_MAP: dict[str, int] = {
    "中学卒業程度":   1,
    "高校卒業程度":   2,
    "英検3級":        1,
    "英検準2級":      2,
    "英検2級":        3,
    "英検準1級":      4,
    "英検1級":        5,
    "TOEIC 400点":    2,
    "TOEIC 600点":    3,
    "TOEIC 730点":    4,
    "TOEIC 860点":    5,
    "TOEIC 990点":    6,
}

# meanings.tier → wordbook_word_registrations.source_type
# tier 5, 6 に対応するCEFR-Jファイルは現状シードされていないため、
# 該当時は "initial_level" にフォールバックする。
CEFR_TIER_SOURCE_TYPES: dict[int, str] = {
    1: "initial_level_a1",
    2: "initial_level_a2",
    3: "initial_level_b1",
    4: "initial_level_b2",
}

PHRASE_LIST_DIR = Path(__file__).resolve().parents[2] / "phrase_list"
ACL_FILENAME = "The_Academic_Collocation_List(Academic Collocation List).csv"
TRAILING_POS_RE = re.compile(r"\s*\((?:adj|adv|n|v|vpp)\)\s*$", re.IGNORECASE)
ENGLISH_WORD_RE = re.compile(r"^[a-z]+(?:['’-][a-z]+)*$", re.IGNORECASE)
ARTICLE_WORDS = {"a", "an", "the"}
DEMONSTRATIVE_WORDS = {"this", "that", "these", "those"}
POSSESSIVE_DETERMINER_WORDS = {
    "my",
    "your",
    "his",
    "her",
    "its",
    "our",
    "their",
    "whose",
}
QUANTIFIER_WORDS = {
    "all",
    "any",
    "both",
    "each",
    "either",
    "enough",
    "every",
    "few",
    "fewer",
    "less",
    "little",
    "many",
    "more",
    "most",
    "much",
    "neither",
    "no",
    "several",
    "some",
}
DIRECT_OBJECT_DEPENDENCIES = {"dobj", "obj"}
PHRASE_TRAILING_PREPOSITIONS = {
    "about",
    "against",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "over",
    "through",
    "to",
    "toward",
    "towards",
    "under",
    "upon",
    "with",
    "without",
}
_LEXICAL_ENRICHMENT_LOCK = asyncio.Lock()


@lru_cache(maxsize=1)
def _get_spacy_nlp():
    model_name = os.getenv("SPACY_MODEL", "en_core_web_trf")
    try:
        # 固有表現抽出は使用しない。品詞、係り受け、原形の処理だけを読み込む。
        return spacy.load(model_name, disable=["ner"])
    except Exception as exc:
        raise RuntimeError(
            f"spaCy model '{model_name}' could not be loaded. "
            "Install the configured model or set SPACY_MODEL to an installed model."
        ) from exc


def _normalize_phrase_text(value: str) -> str:
    phrase = value.strip().lower().replace("\ufeff", "")
    phrase = re.sub(r"\s+", " ", phrase)
    phrase = re.sub(r"\s+['’]s\b", "'s", phrase)
    return phrase.strip(" ,.;:")


def _strip_acl_pos(value: str) -> str:
    text = value.strip().lower().replace("\ufeff", "")
    text = TRAILING_POS_RE.sub("", text)
    text = re.sub(r"\((?:adj|adv|n|v|vpp)\)", "", text, flags=re.IGNORECASE)
    text = text.replace("(", "").replace(")", "")
    return _normalize_phrase_text(text)


def _read_entry_phrases(path: Path) -> set[str]:
    phrases: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            for row in reader:
                entry = _normalize_phrase_text(row.get("entry") or "")
                if entry and " " in entry:
                    phrases.add(entry)
    except OSError:
        return set()
    return phrases


def _read_acl_phrases(path: Path) -> set[str]:
    phrases: set[str] = set()
    current_left = ""
    try:
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) < 3 or row[0] == "#":
                    continue
                left = _strip_acl_pos(row[1] or "")
                right = _strip_acl_pos(row[2] or "")
                if left:
                    current_left = left
                if not current_left or not right:
                    continue
                phrase = _normalize_phrase_text(f"{current_left} {right}")
                if " " in phrase:
                    phrases.add(phrase)
    except OSError:
        return set()
    return phrases


@lru_cache(maxsize=1)
def _load_phrase_list_phrases() -> list[str]:
    if not PHRASE_LIST_DIR.exists():
        return []

    phrases: set[str] = set()
    for path in PHRASE_LIST_DIR.glob("*.csv"):
        if path.name == ACL_FILENAME:
            phrases.update(_read_acl_phrases(path))
        else:
            phrases.update(_read_entry_phrases(path))

    return sorted(phrases, key=lambda phrase: len(phrase.split()), reverse=True)


def _normalize_lemma(token) -> str:
    lemma = (token.lemma_ or token.text).strip().lower()
    if lemma == "-pron-":
        return token.text.lower()
    return lemma


def _is_english_word_text(value: str) -> bool:
    return bool(ENGLISH_WORD_RE.fullmatch(value.strip()))


def _is_english_phrase_text(value: str) -> bool:
    words = _normalize_phrase_text(value).split()
    return len(words) > 1 and all(_is_english_word_text(word) for word in words)


def _is_valid_word_token(token) -> bool:
    is_contracted_auxiliary = (
        token.pos_ == "AUX"
        and token.text.startswith(("'", "’"))
        and _is_english_word_text(_normalize_lemma(token))
    )
    if (not token.is_alpha or not token.is_ascii) and not is_contracted_auxiliary:
        return False
    return is_contracted_auxiliary or _is_english_word_text(token.text)


def _determiner_detail(token) -> Optional[str]:
    text = token.text.lower()
    tag = token.tag_
    if tag == "CD" or token.pos_ == "NUM":
        return "numeral"
    if text in ARTICLE_WORDS and (token.pos_ == "DET" or tag == "DT"):
        return "article"
    if (
        text in DEMONSTRATIVE_WORDS
        and (token.pos_ in {"DET", "PRON"} or tag in {"DT", "PDT", "WDT"})
    ):
        return "demonstrative"
    if tag == "PRP$":
        return "possessive_determiner"
    if (
        text in QUANTIFIER_WORDS
        and (token.pos_ == "DET" or tag in {"DT", "PDT", "WDT"})
    ):
        return "quantifier"
    return None


def _auxiliary_detail(token) -> Optional[str]:
    if token.pos_ != "AUX" and token.dep_ not in {"aux", "auxpass"}:
        return None
    if token.tag_ == "MD":
        return "modal_auxiliary"
    if token.dep_ in {"aux", "auxpass"}:
        return "auxiliary"
    if token.lemma_.lower() == "be":
        return "copula"
    return "auxiliary"


def _map_spacy_pos(token) -> tuple[Optional[str], Optional[str]]:
    aux_detail = _auxiliary_detail(token)
    if aux_detail:
        return PartOfSpeech.auxiliary.value, aux_detail

    detail = _determiner_detail(token)
    if detail == "article":
        return PartOfSpeech.article.value, detail
    if detail:
        return PartOfSpeech.determiner.value, detail

    if token.text.lower() == "such":
        return PartOfSpeech.determiner.value, "demonstrative"

    pos = token.pos_
    if pos in {"NOUN", "PROPN"}:
        return PartOfSpeech.noun.value, None
    if pos in {"VERB", "AUX"}:
        return PartOfSpeech.verb.value, None
    if pos == "ADJ":
        return PartOfSpeech.adjective.value, None
    if pos == "ADV":
        return PartOfSpeech.adverb.value, None
    if pos == "PRON":
        return PartOfSpeech.pronoun.value, None
    if pos == "ADP":
        return PartOfSpeech.preposition.value, None
    if pos in {"CCONJ", "SCONJ"}:
        return PartOfSpeech.conjunction.value, None
    if pos == "INTJ":
        return PartOfSpeech.interjection.value, None
    if pos == "DET":
        return PartOfSpeech.determiner.value, None
    if pos == "NUM":
        return PartOfSpeech.determiner.value, "numeral"
    if pos == "PART":
        return PartOfSpeech.preposition.value, None
    if pos == "X":
        return PartOfSpeech.abbreviation.value, None

    tag = token.tag_
    if tag.startswith("NN"):
        return PartOfSpeech.noun.value, None
    if tag.startswith("VB"):
        return PartOfSpeech.verb.value, None
    if tag.startswith("JJ"):
        return PartOfSpeech.adjective.value, None
    if tag.startswith("RB"):
        return PartOfSpeech.adverb.value, None
    if tag in {"PRP", "PRP$"}:
        return PartOfSpeech.pronoun.value, None
    if tag in {"IN", "TO"}:
        return PartOfSpeech.preposition.value, None
    if tag == "CC":
        return PartOfSpeech.conjunction.value, None
    if tag == "UH":
        return PartOfSpeech.interjection.value, None
    if tag in {"DT", "PDT", "WDT"}:
        return PartOfSpeech.determiner.value, None
    if tag == "CD":
        return PartOfSpeech.determiner.value, "numeral"
    if tag in {"FW", "SYM"}:
        return PartOfSpeech.abbreviation.value, None
    return None, None


def _dedupe_items(items: list[dict]) -> list[dict]:
    # 同じ単語・品詞でも文法上の役割と本文位置が異なるものは別カードにする。
    seen: set[tuple[str, str, Optional[str]]] = set()
    result: list[dict] = []
    for item in items:
        key = (
            item["text"],
            item["part_of_speech"],
            item.get("part_of_speech_detail"),
        )
        if key in seen:
            for existing in result:
                if (
                    existing["text"],
                    existing["part_of_speech"],
                    existing.get("part_of_speech_detail"),
                ) == key:
                    existing["occurrence_count"] = existing.get("occurrence_count", 1) + item.get("occurrence_count", 1)
                    existing_forms = existing.setdefault("surface_forms", [])
                    for form in item.get("surface_forms") or []:
                        if form not in existing_forms:
                            existing_forms.append(form)
                    existing_occurrences = existing.setdefault("occurrences", [])
                    existing_occurrences.extend(item.get("occurrences") or [])
                    break
            continue
        seen.add(key)
        result.append(item)
    return result


def _dependency_phrase_patterns(
    phrase_texts: list[str],
) -> dict[tuple[str, str], list[tuple[str, Optional[str]]]]:
    """動詞＋目的語として照合できる熟語を、原形の組で索引化する。"""
    patterns: dict[tuple[str, str], list[tuple[str, Optional[str]]]] = {}
    for phrase in phrase_texts:
        normalized = _normalize_phrase_text(phrase)
        words = normalized.split()
        if len(words) not in {2, 3} or not all(
            _is_english_word_text(word) for word in words
        ):
            continue

        trailing_preposition: Optional[str] = None
        if len(words) == 3:
            if words[2] not in PHRASE_TRAILING_PREPOSITIONS:
                continue
            trailing_preposition = words[2]

        key = (words[0], words[1])
        pattern = (normalized, trailing_preposition)
        if pattern not in patterns.setdefault(key, []):
            patterns[key].append(pattern)

    return patterns


def _attached_preposition(verb, obj, lemma: str):
    """動詞または目的語に係り、目的語より後ろにある指定前置詞を返す。"""
    for token in (*verb.children, *obj.children):
        if (
            token.i > obj.i
            and token.pos_ == "ADP"
            and token.dep_ == "prep"
            and _normalize_lemma(token) == lemma
        ):
            return token
    return None


def _dependency_phrase_items(doc, phrase_texts: list[str]) -> list[dict]:
    """本文の動詞と直接目的語の係り受けから、非連続を含む熟語を検出する。"""
    patterns = _dependency_phrase_patterns(phrase_texts)
    items: list[dict] = []
    matcher = DependencyMatcher(doc.vocab)
    matcher.add("VERB_DIRECT_OBJECT", [[
        {
            "RIGHT_ID": "verb",
            "RIGHT_ATTRS": {"POS": {"IN": ["VERB", "AUX"]}},
        },
        {
            "LEFT_ID": "verb",
            "REL_OP": ">",
            "RIGHT_ID": "object",
            "RIGHT_ATTRS": {
                "DEP": {"IN": sorted(DIRECT_OBJECT_DEPENDENCIES)},
                "POS": {"IN": ["NOUN", "PROPN", "PRON"]},
            },
        },
    ]])

    for _, token_ids in matcher(doc):
        verb, obj = (doc[token_id] for token_id in token_ids)
        verb_lemma = _normalize_lemma(verb)
        if obj.i <= verb.i:
            continue

        key = (verb_lemma, _normalize_lemma(obj))
        for canonical, trailing_preposition in patterns.get(key, []):
            last_token = obj
            if trailing_preposition:
                prep = _attached_preposition(verb, obj, trailing_preposition)
                if prep is None:
                    continue
                last_token = prep

            span = doc[verb.i:last_token.i + 1]
            surface = span.text.lower()
            items.append({
                "text": canonical,
                "part_of_speech": PartOfSpeech.phrase.value,
                "part_of_speech_detail": None,
                "surface_forms": [surface],
                "occurrences": [{
                    "form": surface,
                    "start": span.start_char,
                    "end": span.end_char,
                }],
                "kind": "phrase",
                "occurrence_count": 1,
            })

    return items


def get_learned_lexical_items(user_id: str) -> set[tuple[str, str]]:
    db = get_supabase()
    learned: set[tuple[str, str]] = set()
    page_size = 1000
    offset = 0

    # Supabase/PostgRESTは指定しない場合、取得結果を最大1,000行に制限する。
    # 初期レベル登録だけで1,000件を超えるため、全ページを取得しないと
    # 後半の学習済み単語が未知語として誤判定される。
    while True:
        response = (
            db.table("wordbook_words")
            .select("meanings(part_of_speech, words(word))")
            .eq("user_id", user_id)
            .eq("is_learned", True)
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = response.data or []
        for row in rows:
            meaning = row.get("meanings") or {}
            word = (meaning.get("words") or {}).get("word")
            pos = meaning.get("part_of_speech")
            if word and pos:
                learned.add((word.strip().lower().replace("’", "'"), pos))

        if len(rows) < page_size:
            break
        offset += page_size
    return learned


def _phrase_catalog() -> list[str]:
    phrases = set(_load_phrase_list_phrases())
    try:
        db = get_supabase()
        response = (
            db.table("meanings")
            .select("words(word)")
            .eq("part_of_speech", PartOfSpeech.phrase.value)
            .execute()
        )
    except Exception:
        return sorted(phrases, key=lambda p: len(p.split()), reverse=True)
    for row in (response.data or []):
        word = (row.get("words") or {}).get("word")
        if word and " " in word:
            phrases.add(_normalize_phrase_text(word))
    return sorted(phrases, key=lambda p: len(p.split()), reverse=True)


def analyze_lexical_items(text: str) -> list[dict]:
    nlp = _get_spacy_nlp()
    # spaCyの英語モデルは曲線アポストロフィの短縮形を安定して原形化しないため、
    # 文字数を変えずにASCIIアポストロフィへ揃えてから解析する。
    doc = nlp(text.replace("’", "'"))
    if not doc:
        return []

    phrase_texts = _phrase_catalog()
    # 動詞＋目的語型は係り受けを正とし、活用形や間に入る修飾語も検出する。
    # PhraseMatcherは、構文解析が失敗した場合とその他の連続熟語のフォールバック。
    dependency_items = _dependency_phrase_items(doc, phrase_texts)
    items: list[dict] = list(dependency_items)
    dependency_occurrences = {
        (
            item["text"],
            occurrence["start"],
            occurrence["end"],
        )
        for item in dependency_items
        for occurrence in item["occurrences"]
    }

    if phrase_texts:
        matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
        matcher.add(
            "WIKTIONARY_PHRASES",
            [nlp.make_doc(phrase) for phrase in phrase_texts if _is_english_phrase_text(phrase)],
        )
        spans = filter_spans([doc[start:end] for _, start, end in matcher(doc)])
        for span in spans:
            canonical = _normalize_phrase_text(span.text)
            if (canonical, span.start_char, span.end_char) in dependency_occurrences:
                continue
            items.append({
                "text": canonical,
                "part_of_speech": PartOfSpeech.phrase.value,
                "part_of_speech_detail": None,
                "surface_forms": [span.text.lower()],
                "occurrences": [{
                    "form": span.text.lower(),
                    "start": span.start_char,
                    "end": span.end_char,
                }],
                "kind": "phrase",
                "occurrence_count": 1,
            })

    remaining_tokens = [
        token for token in doc
        if _is_valid_word_token(token)
    ]
    candidate_items: list[dict] = []
    for token in remaining_tokens:
        lemma = _normalize_lemma(token)
        pos, pos_detail = _map_spacy_pos(token)
        if not lemma or not _is_english_word_text(lemma):
            continue
        if not pos:
            # spaCyが英字トークンをPUNCT等にした場合もここでは捨てない。
            # 実在判定は後段のWiktionaryへ任せ、該当品詞が取れなければ
            # Gemini生成へ進められる汎用カテゴリとして保持する。
            pos = PartOfSpeech.abbreviation.value
            pos_detail = None
        candidate_items.append({
            "text": lemma,
            "part_of_speech": pos,
            "part_of_speech_detail": pos_detail,
            "surface_forms": [token.text.lower()],
            "occurrences": [{
                "form": token.text.lower(),
                "start": token.idx,
                "end": token.idx + len(token.text),
            }],
            "kind": "word",
            "occurrence_count": 1,
        })

    for item in candidate_items:
        items.append(item)

    return _dedupe_items(items)


async def _filter_to_wiktionary_entries(items: list[dict]) -> list[dict]:
    """DBにないspaCy候補だけをWiktionaryで英語見出し語か確認する。"""
    stored_keys = await asyncio.to_thread(_meaning_keys_in_database, items)
    terms = sorted({
        str(item.get("text") or "").strip().lower()
        for item in items
        if item.get("kind") != "phrase"
        and item.get("text")
        and (
            str(item.get("text") or "").strip().lower().replace("’", "'"),
            str(item.get("part_of_speech") or "").strip().lower(),
        ) not in stored_keys
    })
    if not terms:
        return items

    semaphore = asyncio.Semaphore(4)

    async def validate(term: str) -> tuple[str, bool]:
        async with semaphore:
            try:
                return term, await has_cached_wiktionary_english_entry(term)
            except Exception as exc:
                print(f"[wiktionary-validation] failed for {term!r}: {exc!r}")
                # API制限や通信障害は「English entryなし」ではない。
                # 一時失敗で教材から語を消さず、後続の再取得に残す。
                return term, True

    validity = dict(await asyncio.gather(*(validate(term) for term in terms)))
    return [
        item
        for item in items
        if item.get("kind") == "phrase"
        or (
            str(item.get("text") or "").strip().lower().replace("’", "'"),
            str(item.get("part_of_speech") or "").strip().lower(),
        ) in stored_keys
        or validity.get(str(item.get("text") or "").strip().lower(), False)
    ]

POS_SUPERTYPES = {
    PartOfSpeech.article.value: {
        PartOfSpeech.article.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.determiner.value: {
        PartOfSpeech.determiner.value,
        PartOfSpeech.article.value,
        PartOfSpeech.pronoun.value,
        PartOfSpeech.adjective.value,
    },
    PartOfSpeech.pronoun.value: {
        PartOfSpeech.pronoun.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.noun.value: {
        PartOfSpeech.noun.value,
    },
    PartOfSpeech.verb.value: {
        PartOfSpeech.verb.value,
    },
    PartOfSpeech.adjective.value: {
        PartOfSpeech.adjective.value,
    },
    PartOfSpeech.adverb.value: {
        PartOfSpeech.adverb.value,
    },
    PartOfSpeech.preposition.value: {
        PartOfSpeech.preposition.value,
    },
    PartOfSpeech.conjunction.value: {
        PartOfSpeech.conjunction.value,
    },
    PartOfSpeech.interjection.value: {
        PartOfSpeech.interjection.value,
    },
    PartOfSpeech.abbreviation.value: {
        PartOfSpeech.abbreviation.value,
    },
    PartOfSpeech.phrase.value: {
        PartOfSpeech.phrase.value,
    },
    PartOfSpeech.auxiliary.value: {
        PartOfSpeech.auxiliary.value,
        PartOfSpeech.verb.value,
    },
}


def pos_matches(material_pos: str, learned_pos: str) -> bool:
    return learned_pos in POS_SUPERTYPES.get(material_pos, {material_pos})


MEANING_POS_COMPATIBILITY: dict[str, set[str]] = {
    PartOfSpeech.noun.value: {
        PartOfSpeech.noun.value,
        PartOfSpeech.pronoun.value,
        PartOfSpeech.abbreviation.value,
    },
    PartOfSpeech.pronoun.value: {
        PartOfSpeech.pronoun.value,
        PartOfSpeech.noun.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.verb.value: {
        PartOfSpeech.verb.value,
        PartOfSpeech.auxiliary.value,
    },
    PartOfSpeech.auxiliary.value: {
        PartOfSpeech.auxiliary.value,
        PartOfSpeech.verb.value,
    },
    PartOfSpeech.article.value: {
        PartOfSpeech.article.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.determiner.value: {
        PartOfSpeech.determiner.value,
        PartOfSpeech.article.value,
        PartOfSpeech.pronoun.value,
        PartOfSpeech.adjective.value,
        PartOfSpeech.numeral.value,
    },
    PartOfSpeech.adjective.value: {
        PartOfSpeech.adjective.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.numeral.value: {
        PartOfSpeech.numeral.value,
        PartOfSpeech.determiner.value,
    },
    PartOfSpeech.abbreviation.value: {
        PartOfSpeech.abbreviation.value,
        PartOfSpeech.noun.value,
    },
}


def meaning_pos_matches(detected_pos: str, meaning_pos: str) -> bool:
    """spaCyが混同し得る品詞だけを表示用meaningとして互換扱いする。"""
    return meaning_pos in MEANING_POS_COMPATIBILITY.get(
        detected_pos,
        {detected_pos},
    )


def _has_compatible_japanese_meaning(word_id: int, detected_pos: str) -> bool:
    rows = (
        get_supabase()
        .table("meanings")
        .select("part_of_speech")
        .eq("word_id", word_id)
        .filter("definition_ja", "not.is", "null")
        .neq("definition_ja", "")
        .execute()
        .data
        or []
    )
    return any(
        meaning_pos_matches(
            detected_pos,
            str(row.get("part_of_speech") or "").strip().lower(),
        )
        for row in rows
    )


def _meaning_keys_in_database(
    items: list[dict],
    *,
    require_japanese: bool = False,
) -> set[tuple[str, str]]:
    """DBに互換品詞のmeaningがある単語・検出品詞を一括取得する。"""
    db = get_supabase()
    words = sorted({
        item["text"].strip().lower().replace("’", "'")
        for item in items
        if item.get("text")
    })
    if not words:
        return set()

    word_ids: dict[int, str] = {}
    chunk_size = 200
    for start in range(0, len(words), chunk_size):
        rows = (
            db.table("words")
            .select("id, word")
            .in_("word", words[start:start + chunk_size])
            .execute()
            .data
            or []
        )
        for row in rows:
            word_ids[int(row["id"])] = str(row.get("word") or "").strip().lower()

    meaning_positions_by_word: dict[str, set[str]] = {}
    ids = list(word_ids)
    for start in range(0, len(ids), chunk_size):
        query = (
            db.table("meanings")
            .select("word_id, part_of_speech")
            .in_("word_id", ids[start:start + chunk_size])
        )
        if require_japanese:
            query = (
                query.filter("definition_ja", "not.is", "null")
                .neq("definition_ja", "")
            )
        rows = query.execute().data or []
        for row in rows:
            word = word_ids.get(int(row["word_id"]))
            if word:
                meaning_positions_by_word.setdefault(word, set()).add(
                    str(row.get("part_of_speech") or "").strip().lower()
                )

    return {
        (
            item["text"].strip().lower().replace("’", "'"),
            str(item.get("part_of_speech") or "").strip().lower(),
        )
        for item in items
        if item.get("text")
        and item.get("part_of_speech")
        and any(
            meaning_pos_matches(
                str(item.get("part_of_speech") or "").strip().lower(),
                meaning_pos,
            )
            for meaning_pos in meaning_positions_by_word.get(
                item["text"].strip().lower().replace("’", "'"),
                set(),
            )
        )
    }


def _meaning_keys_with_japanese(items: list[dict]) -> set[tuple[str, str]]:
    """保存済み日本語訳の有無を単語ごとのN+1ではなく一括取得する。

    spaCyの品詞が辞書側の細分類とずれる場合があるため、互換品詞に限り
    表示用訳として利用する。明らかに異なる品詞は訳あり扱いにしない。
    """
    return _meaning_keys_in_database(items, require_japanese=True)


async def extract_unknown_words(text: str, user_id: str, enrich_meanings: bool = True) -> dict:
    """
    テキスト中の英単語のうち、学習済み単語帳に存在しないものを返す。
    判定: wordbook_words に is_learned=true のエントリーがない単語 = 未知
    """
    lexical_items = await asyncio.to_thread(analyze_lexical_items, text)
    lexical_items = await _filter_to_wiktionary_entries(lexical_items)
    if not lexical_items:
        return {
            "unknown_words": [],
            "total_words": 0,
            "unknown_count": 0,
            "known_count": 0,
            "coverage_rate": 0.0,
            "items": [],
            "unknown_items": [],
        }

    learned = await asyncio.to_thread(get_learned_lexical_items, user_id)
    stored_meaning_keys = (
        set()
        if enrich_meanings
        else await asyncio.to_thread(_meaning_keys_with_japanese, lexical_items)
    )
    item_results: list[dict] = []
    unknown_items: list[dict] = []

    for item in lexical_items:
        word = item["text"].strip().lower().replace("’", "'")
        material_pos = item["part_of_speech"].strip().lower()

        is_learned = any(
            learned_word == word
            and pos_matches(material_pos, learned_pos)
            for learned_word, learned_pos in learned
        )

        # enrich_meanings=false の高速解析でも、保存済みの日本語訳があるかは
        # 必ず確認する。_count_meanings は definition_ja が空のmeaningを数えない。
        if enrich_meanings:
            lookup = await get_or_create_word(
                item["text"],
                part_of_speech=item["part_of_speech"],
                enrich_meanings=not is_learned,
            )
            # 検出品詞の生成を試したうえで、spaCyが混同し得る互換品詞だけを
            # フォールバックにする。明らかに別品詞しかなければ訳なし。
            has_meaning = (
                lookup["meaning_count"] > 0
                or _has_compatible_japanese_meaning(
                    lookup["word_id"],
                    material_pos,
                )
            )
        else:
            has_meaning = (word, material_pos) in stored_meaning_keys

        result = {
            **item,
            "is_learned": is_learned,
            "has_meaning": has_meaning,
        }
        item_results.append(result)
        if not is_learned and has_meaning:
            unknown_items.append(result)

    unknown_count = len(unknown_items)
    known_count = sum(1 for item in item_results if item["is_learned"])
    # 日本語訳を取得できていない語は、既知・未知・習得率の分母から除外する。
    total = known_count + unknown_count
    coverage_rate = round(known_count / total, 4) if total else 0.0

    return {
        "unknown_words": [item["text"] for item in unknown_items],
        "total_words": total,
        "unknown_count": unknown_count,
        "known_count": known_count,
        "coverage_rate": coverage_rate,
        "items": item_results,
        "unknown_items": unknown_items,
    }


async def enrich_lexical_items(items: list[dict]) -> None:
    async with _LEXICAL_ENRICHMENT_LOCK:
        await _enrich_lexical_items_locked(items)


async def _enrich_lexical_items_locked(items: list[dict]) -> None:
    """
    教材中に出てきた語・熟語について、同一単語・同一品詞の meaning がなければ補完する。
    画面表示やカバー率計算を待たせないため、API からは BackgroundTasks で呼び出す。
    """
    stored_keys = _meaning_keys_with_japanese(items)
    target_items = [
        item
        for item in items
        if (
            (item.get("text") or "").strip().lower().replace("’", "'"),
            str(item.get("part_of_speech") or "").strip().lower(),
        ) not in stored_keys
    ]
    if not target_items:
        return

    print(
        f"[background-enrichment] processing {len(target_items)}/"
        f"{len(items)} untranslated lexical items."
    )
    seen: set[tuple[str, str]] = set()
    unique_targets: list[dict] = []
    for item in target_items:
        text = (item.get("text") or "").strip().lower()
        part_of_speech = item.get("part_of_speech")
        if not text or not part_of_speech:
            continue
        key = (text, part_of_speech)
        if key in seen:
            continue
        seen.add(key)
        unique_targets.append(item)

    semaphore = asyncio.Semaphore(8)

    async def enrich_item(item: dict) -> None:
        text = (item.get("text") or "").strip().lower()
        part_of_speech = item.get("part_of_speech")
        async with semaphore:
            try:
                await get_or_create_word(
                    text,
                    part_of_speech=part_of_speech,
                    enrich_meanings=False,
                )
            except Exception as exc:
                print(
                    "[background-enrichment] "
                    f"failed for {text!r} ({part_of_speech}): {exc!r}"
                )

    await asyncio.gather(*(enrich_item(item) for item in unique_targets))
    updated = await batch_generate_missing_japanese_definitions(unique_targets)
    print(
        f"[background-enrichment] completed; generated {updated} Japanese meanings."
    )


async def enrich_lexical_items_background(items: list[dict]) -> None:
    """レスポンス送信後も同じイベントループで意味補完を継続する。"""
    try:
        await enrich_lexical_items(items)
    except Exception as exc:
        # レスポンス送信後の補完失敗をASGIリクエスト例外にしない。
        print(f"[background-enrichment] failed: {exc!r}")


async def bulk_register_cefr_words(user_id: str, level: str) -> int:
    """
    レベル設定時にCEFR-Jの単語（tier以下）をユーザーの学習済み単語帳に一括登録。
    すでに登録済みのものはスキップ（upsert）。
    source_type は各単語の実際のCEFR tier（A1/A2/B1/B2）ごとに分ける。
    """
    tier = LEVEL_TIER_MAP.get(level, 0)
    if tier == 0:
        return 0

    db = get_supabase()

    # 対象tierのmeaningを全取得（Supabaseの1000件制限対策）
    page_size = 1000
    offset = 0
    all_meanings = []

    while True:
        response = (
            db.table("meanings")
            .select("id, tier")
            .lte("tier", tier)
            .range(offset, offset + page_size - 1)
            .execute()
        )

        rows = response.data or []

        if not rows:
            break

        all_meanings.extend(rows)

        if len(rows) < page_size:
            break

        offset += page_size

    if not all_meanings:
        return 0

    meaning_tier_by_id: dict[int, Optional[int]] = {
        m["id"]: m.get("tier")
        for m in all_meanings
    }

    upsert_rows = [
        {
            "user_id": user_id,
            "meaning_id": meaning_id,
            "is_learned": True,
        }
        for meaning_id in meaning_tier_by_id.keys()
    ]

    response = (
        db.table("wordbook_words")
        .upsert(
            upsert_rows,
            on_conflict="user_id,meaning_id",
        )
        .execute()
    )

    for row in response.data or []:
        meaning_tier = meaning_tier_by_id.get(row["meaning_id"])

        source_type = CEFR_TIER_SOURCE_TYPES.get(
            meaning_tier,
            "initial_level",
        )

        record_wordbook_registration(
            row["id"],
            source_type=source_type,
            source_label=level,
        )

    return len(upsert_rows)


def bulk_register_cefr_words_background(user_id: str, level: str) -> None:
    """初期語彙登録をthread poolで実行し、レベル設定画面を待たせない。"""
    try:
        asyncio.run(bulk_register_cefr_words(user_id, level))
    except Exception as exc:
        print(f"[background-level-registration] failed: {exc!r}")
