from __future__ import annotations
import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import spacy
from spacy.matcher import PhraseMatcher
from spacy.util import filter_spans

from app.db.supabase import get_supabase
from app.schemas.schemas import PartOfSpeech
from app.services.word_lookup_service import get_or_create_word

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

PHRASE_LIST_DIR = Path(__file__).resolve().parents[2] / "phrase_list"
ACL_FILENAME = "The_Academic_Collocation_List(Academic Collocation List).csv"
TRAILING_POS_RE = re.compile(r"\s*\((?:adj|adv|n|v|vpp)\)\s*$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _get_spacy_nlp():
    try:
        return spacy.load("en_core_web_sm")
    except Exception:
        return spacy.blank("en")


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


def _map_spacy_pos(token) -> Optional[str]:
    pos = token.pos_
    if pos in {"NOUN", "PROPN"}:
        return PartOfSpeech.noun.value
    if pos in {"VERB", "AUX"}:
        return PartOfSpeech.verb.value
    if pos == "ADJ":
        return PartOfSpeech.adjective.value
    if pos == "ADV":
        return PartOfSpeech.adverb.value
    if pos == "PRON":
        return PartOfSpeech.pronoun.value
    if pos == "ADP":
        return PartOfSpeech.preposition.value
    if pos in {"CCONJ", "SCONJ"}:
        return PartOfSpeech.conjunction.value
    if pos == "INTJ":
        return PartOfSpeech.interjection.value
    if pos == "DET":
        return PartOfSpeech.determiner.value
    if pos == "NUM":
        return PartOfSpeech.numeral.value
    if pos == "PART":
        return PartOfSpeech.preposition.value
    if pos == "X":
        return PartOfSpeech.abbreviation.value

    tag = token.tag_
    if tag.startswith("NN"):
        return PartOfSpeech.noun.value
    if tag.startswith("VB"):
        return PartOfSpeech.verb.value
    if tag.startswith("JJ"):
        return PartOfSpeech.adjective.value
    if tag.startswith("RB"):
        return PartOfSpeech.adverb.value
    if tag in {"PRP", "PRP$"}:
        return PartOfSpeech.pronoun.value
    if tag in {"IN", "TO"}:
        return PartOfSpeech.preposition.value
    if tag == "CC":
        return PartOfSpeech.conjunction.value
    if tag == "UH":
        return PartOfSpeech.interjection.value
    if tag in {"DT", "PDT", "WDT"}:
        return PartOfSpeech.determiner.value
    if tag == "CD":
        return PartOfSpeech.numeral.value
    if tag in {"FW", "SYM"}:
        return PartOfSpeech.abbreviation.value
    return None


def _dedupe_items(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for item in items:
        key = (item["text"], item["part_of_speech"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


async def get_learned_lexical_items(user_id: str) -> set[tuple[str, str]]:
    db = get_supabase()
    response = (
        db.table("wordbook_words")
        .select("meanings(part_of_speech, words(word))")
        .eq("user_id", user_id)
        .eq("is_learned", True)
        .execute()
    )
    learned: set[tuple[str, str]] = set()
    for row in (response.data or []):
        meaning = row.get("meanings") or {}
        word = (meaning.get("words") or {}).get("word")
        pos = meaning.get("part_of_speech")
        if word and pos:
            learned.add((word.lower(), pos))
    return learned


def _lookup_existing_pos_map(words: list[str]) -> dict[str, list[str]]:
    if not words:
        return {}
    try:
        db = get_supabase()
        response = (
            db.table("words")
            .select("word, meanings(part_of_speech)")
            .in_("word", words)
            .execute()
        )
    except Exception:
        return {}
    pos_map: dict[str, list[str]] = {}
    for row in (response.data or []):
        word = row.get("word")
        if not word:
            continue
        poses = [
            m.get("part_of_speech")
            for m in (row.get("meanings") or [])
            if m.get("part_of_speech")
        ]
        if poses:
            pos_map[word.lower()] = poses
    return pos_map


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
    doc = nlp(text)
    if not doc:
        return []

    phrase_texts = _phrase_catalog()
    items: list[dict] = []
    consumed_indexes: set[int] = set()

    if phrase_texts:
        matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
        matcher.add("WIKTIONARY_PHRASES", [nlp.make_doc(phrase) for phrase in phrase_texts])
        spans = filter_spans([doc[start:end] for _, start, end in matcher(doc)])
        for span in spans:
            consumed_indexes.update(range(span.start, span.end))
            items.append({
                "text": span.text.lower(),
                "part_of_speech": PartOfSpeech.phrase.value,
                "kind": "phrase",
            })

    remaining_tokens = [
        token for token in doc
        if token.i not in consumed_indexes and (token.is_alpha or token.like_num)
    ]
    pos_map = _lookup_existing_pos_map(list(dict.fromkeys(_normalize_lemma(token) for token in remaining_tokens)))
    for token in remaining_tokens:
        lemma = _normalize_lemma(token)
        pos = _map_spacy_pos(token)
        if not lemma or not pos:
            continue
        if lemma in pos_map:
            pos = pos_map[lemma][0]
        items.append({
            "text": lemma,
            "part_of_speech": pos,
            "kind": "word",
        })

    return _dedupe_items(items)


async def extract_unknown_words(text: str, user_id: str, enrich_meanings: bool = True) -> dict:
    """
    テキスト中の英単語のうち、学習済み単語帳に存在しないものを返す。
    判定: wordbook_words に is_learned=true のエントリーがない単語 = 未知
    """
    lexical_items = analyze_lexical_items(text)
    total = len(lexical_items)

    if total == 0:
        return {
            "unknown_words": [],
            "total_words": 0,
            "unknown_count": 0,
            "known_count": 0,
            "coverage_rate": 0.0,
            "items": [],
            "unknown_items": [],
        }

    learned = await get_learned_lexical_items(user_id)
    item_results: list[dict] = []
    unknown_items: list[dict] = []

    for item in lexical_items:
        key = (item["text"], item["part_of_speech"])
        is_learned = key in learned
        has_meaning = False
        if enrich_meanings:
            lookup = await get_or_create_word(
                item["text"],
                part_of_speech=item["part_of_speech"],
                enrich_meanings=not is_learned,
            )
            has_meaning = lookup["meaning_count"] > 0

        result = {
            **item,
            "is_learned": is_learned,
            "has_meaning": has_meaning,
        }
        item_results.append(result)
        if not is_learned:
            unknown_items.append(result)

    known_count = total - len(unknown_items)
    coverage_rate = round(known_count / total, 4) if total else 0.0

    return {
        "unknown_words": [item["text"] for item in unknown_items],
        "total_words": total,
        "unknown_count": len(unknown_items),
        "known_count": known_count,
        "coverage_rate": coverage_rate,
        "items": item_results,
        "unknown_items": unknown_items,
    }


async def enrich_lexical_items(items: list[dict]) -> None:
    """
    教材中に出てきた語・熟語について、同一単語・同一品詞の meaning がなければ補完する。
    画面表示やカバー率計算を待たせないため、API からは BackgroundTasks で呼び出す。
    """
    seen: set[tuple[str, str]] = set()
    for item in items:
        text = (item.get("text") or "").strip().lower()
        part_of_speech = item.get("part_of_speech")
        if not text or not part_of_speech:
            continue
        key = (text, part_of_speech)
        if key in seen:
            continue
        seen.add(key)
        try:
            await get_or_create_word(
                text,
                part_of_speech=part_of_speech,
                enrich_meanings=True,
            )
        except Exception:
            continue


async def bulk_register_cefr_words(user_id: str, level: str) -> int:
    """
    レベル設定時にCEFR-Jの単語（tier以下）をユーザーの学習済み単語帳に一括登録。
    すでに登録済みのものはスキップ（upsert）。
    """
    tier = LEVEL_TIER_MAP.get(level, 0)
    if tier == 0:
        return 0

    db = get_supabase()

    # 対象tierのmeaningを全取得
    meanings = (
        db.table("meanings")
        .select("id")
        .lte("tier", tier)
        .execute()
    )

    if not meanings.data:
        return 0

    rows = [
        {
            "user_id": user_id,
            "meaning_id": m["id"],
            "is_learned": True,
        }
        for m in meanings.data
    ]

    db.table("wordbook_words").upsert(rows, on_conflict="user_id,meaning_id").execute()
    return len(rows)
