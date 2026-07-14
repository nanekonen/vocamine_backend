from __future__ import annotations
import re
from typing import Optional
from app.db.supabase import get_supabase
from app.services.dictionary_service import (
    fetch_wiktionary_meanings,
    generate_fallback_japanese_definition,
    generate_japanese_definitions_batch,
    generate_japanese_definition,
    has_wiktionary_english_entry,
    normalize_part_of_speech,
)

JAPANESE_TEXT_RE = re.compile(r"[ぁ-んァ-ン一-龯]")
ENGLISH_WORD_RE = re.compile(r"^[a-z]+(?:['’-][a-z]+)*$", re.IGNORECASE)
_wiktionary_entry_cache: dict[str, bool] = {}

MEANING_POS_COMPATIBILITY: dict[str, set[str]] = {
    "noun": {"noun", "pronoun", "abbreviation"},
    "pronoun": {"pronoun", "noun", "determiner"},
    "verb": {"verb", "auxiliary"},
    "auxiliary": {"auxiliary", "verb"},
    "article": {"article", "determiner"},
    "determiner": {
        "determiner", "article", "pronoun", "adjective", "numeral",
    },
    "adjective": {"adjective", "determiner"},
    "numeral": {"numeral", "determiner"},
    "abbreviation": {"abbreviation", "noun"},
}


def _meaning_pos_matches(detected_pos: str, meaning_pos: str) -> bool:
    return meaning_pos in MEANING_POS_COMPATIBILITY.get(
        detected_pos,
        {detected_pos},
    )

AUXILIARY_MEANINGS = {
    "be": {
        "definition_en": "Used with a present participle to form the progressive aspect, or with a past participle to form the passive voice.",
        "definition_ja": "進行形や受動態を作る助動詞",
    },
    "do": {
        "definition_en": "Used to form questions, negatives, emphatic statements, and short answers.",
        "definition_ja": "疑問文・否定文・強調・短い応答を作る助動詞",
    },
    "have": {
        "definition_en": "Used with a past participle to form the perfect aspect.",
        "definition_ja": "完了形を作る助動詞",
    },
    "can": {
        "definition_en": "Used to express ability, possibility, or permission.",
        "definition_ja": "能力・可能性・許可を表す助動詞",
    },
    "could": {
        "definition_en": "Used to express past ability, possibility, polite requests, or hypothetical situations.",
        "definition_ja": "過去の能力・可能性・丁寧な依頼・仮定を表す助動詞",
    },
    "may": {
        "definition_en": "Used to express possibility or permission.",
        "definition_ja": "可能性・許可を表す助動詞",
    },
    "might": {
        "definition_en": "Used to express weaker possibility or hypothetical situations.",
        "definition_ja": "弱い可能性・仮定を表す助動詞",
    },
    "must": {
        "definition_en": "Used to express necessity, obligation, or strong inference.",
        "definition_ja": "必要・義務・強い推量を表す助動詞",
    },
    "shall": {
        "definition_en": "Used to express future action, offers, or suggestions.",
        "definition_ja": "未来・申し出・提案を表す助動詞",
    },
    "should": {
        "definition_en": "Used to express obligation, advice, expectation, or probability.",
        "definition_ja": "義務・助言・予想・可能性を表す助動詞",
    },
    "will": {
        "definition_en": "Used to express future action, willingness, or prediction.",
        "definition_ja": "未来・意思・予測を表す助動詞",
    },
    "would": {
        "definition_en": "Used to express hypothetical situations, repeated past actions, polite requests, or future-in-the-past.",
        "definition_ja": "仮定・過去の習慣・丁寧な依頼・時制の一致による未来を表す助動詞",
    },
    "need": {
        "definition_en": "Used as a modal auxiliary, chiefly in questions and negatives, to express necessity.",
        "definition_ja": "主に疑問文・否定文で必要性を表す助動詞",
    },
    "dare": {
        "definition_en": "Used as a modal auxiliary, chiefly in questions and negatives, to express having the courage to do something.",
        "definition_ja": "主に疑問文・否定文で「あえてする・する勇気がある」を表す助動詞",
    },
}

AUXILIARY_FORMS = {
    "am": "be",
    "are": "be",
    "is": "be",
    "was": "be",
    "were": "be",
    "been": "be",
    "being": "be",
    "does": "do",
    "did": "do",
    "done": "do",
    "has": "have",
    "had": "have",
    "needs": "need",
    "needed": "need",
    "dares": "dare",
    "dared": "dare",
}


def _definition_contains_japanese(value: Optional[str]) -> bool:
    return bool(value and JAPANESE_TEXT_RE.search(value))


def _has_english_term_shape(word: str) -> bool:
    normalized = word.strip().lower()
    # ここでは文字種だけを確認し、単語として存在するかはWiktionaryに任せる。
    return bool(normalized and ENGLISH_WORD_RE.fullmatch(normalized))


async def has_cached_wiktionary_english_entry(word: str) -> bool:
    normalized = word.strip().lower()
    if normalized in _wiktionary_entry_cache:
        return _wiktionary_entry_cache[normalized]
    exists = await has_wiktionary_english_entry(normalized)
    _wiktionary_entry_cache[normalized] = exists
    return exists


def _drop_invalid_definition_en(row: dict) -> dict:
    if _definition_contains_japanese(row.get("definition_en")):
        row["definition_en"] = None
    return row


def _cleanup_invalid_meanings(word_id: int, part_of_speech: Optional[str] = None) -> int:
    """
    meanings.definition_en は英語の出典定義専用。
    definition_ja がない行は後で再生成するために残す。
    過去に日本語が入った definition_en は definition_ja に退避できる場合だけ退避して消す。
    """
    db = get_supabase()
    query = (
        db.table("meanings")
        .select("id, part_of_speech, definition_en, definition_ja")
        .eq("word_id", word_id)
    )
    if part_of_speech:
        query = query.eq("part_of_speech", part_of_speech)

    changed = 0
    for row in query.execute().data or []:
        definition_ja = (row.get("definition_ja") or "").strip()
        definition_en = row.get("definition_en")
        if not _definition_contains_japanese(definition_en):
            continue

        payload = {"definition_en": None}
        if not definition_ja:
            payload["definition_ja"] = definition_en
        db.table("meanings").update(payload).eq("id", row["id"]).execute()
        changed += 1
    return changed


async def get_or_create_word(word: str, part_of_speech: Optional[str] = None, enrich_meanings: bool = False) -> dict:
    """
    単語をDBに登録する。
    必要なら Wiktionary 由来の meanings を同一品詞に限定して補完する。

    戻り値: {"word_id": int, "created": bool, "meaning_count": int}
    """
    db = get_supabase()
    word_lower = word.strip().lower()
    normalized_pos = normalize_part_of_speech(part_of_speech)

    existing = db.table("words").select("id").eq("word", word_lower).execute()
    if existing.data:
        word_id = existing.data[0]["id"]
        if enrich_meanings:
            await ensure_meanings_for_word(word_id, word_lower, normalized_pos)
        return {
            "word_id": word_id,
            "created": False,
            "meaning_count": _count_meanings(word_id, normalized_pos),
        }

    inserted = db.table("words").insert({"word": word_lower}).execute()
    word_id = inserted.data[0]["id"]

    if enrich_meanings:
        await ensure_meanings_for_word(word_id, word_lower, normalized_pos)
    return {
        "word_id": word_id,
        "created": True,
        "meaning_count": _count_meanings(word_id, normalized_pos),
    }


def _count_meanings(word_id: int, part_of_speech: Optional[str] = None) -> int:
    db = get_supabase()
    query = (
        db.table("meanings")
        .select("id", count="exact")
        .eq("word_id", word_id)
        .filter("definition_ja", "not.is", "null")
        .neq("definition_ja", "")
    )
    if part_of_speech:
        query = query.eq("part_of_speech", part_of_speech)
    return query.execute().count or 0


def _existing_meaning_rows(word_id: int, part_of_speech: Optional[str] = None) -> list[dict]:
    db = get_supabase()
    query = (
        db.table("meanings")
        .select("id, definition_en, definition_ja")
        .eq("word_id", word_id)
    )
    if part_of_speech:
        query = query.eq("part_of_speech", part_of_speech)
    return query.execute().data or []


def _auxiliary_base(word: str) -> str:
    return AUXILIARY_FORMS.get(word, word)


def _insert_auxiliary_meaning(word_id: int, word: str) -> bool:
    base = _auxiliary_base(word)
    meaning = AUXILIARY_MEANINGS.get(base) or {
        "definition_en": "Used as an auxiliary verb.",
        "definition_ja": "助動詞として用いられる",
    }
    if not meaning:
        return False

    db = get_supabase()
    existing = (
        db.table("meanings")
        .select("id")
        .eq("word_id", word_id)
        .eq("part_of_speech", "auxiliary")
        .eq("definition_en", meaning["definition_en"])
        .execute()
        .data
        or []
    )
    payload = {
        "word_id": word_id,
        "part_of_speech": "auxiliary",
        "definition_en": meaning["definition_en"],
        "definition_ja": meaning["definition_ja"],
        "source": "grammar",
        "tier": None,
    }
    if existing:
        db.table("meanings").update(payload).eq("id", existing[0]["id"]).execute()
    else:
        db.table("meanings").insert(payload).execute()
    return True


async def ensure_meanings_for_word(
    word_id: int,
    word: str,
    part_of_speech: Optional[str] = None,
    generate_japanese: bool = True,
    allow_fallback_generation: bool = True,
) -> int:
    """
    meanings に同一品詞の語義がなければ Wiktionary から取得し、必要なら日本語語義を生成して保存する。
    戻り値は追加後の同一品詞 meaning 数。
    """
    db = get_supabase()
    _cleanup_invalid_meanings(word_id, part_of_speech)
    existing_rows = _existing_meaning_rows(word_id, part_of_speech)
    if existing_rows:
        if generate_japanese and any(not (row.get("definition_ja") or "").strip() for row in existing_rows):
            await ensure_japanese_definitions_for_word(word_id, word, part_of_speech)
        return _count_meanings(word_id, part_of_speech)

    fetched = await fetch_wiktionary_meanings(word, part_of_speech)
    for item in fetched:
        transitivity = item.get("transitivity")
        countability = item.get("countability")
        source_definition = item.get("definition") or ""
        if not source_definition.strip() or _definition_contains_japanese(source_definition):
            continue
        definition_ja = None
        if generate_japanese:
            definition_ja = await generate_japanese_definition(
                word,
                item["part_of_speech"],
                item,
            )
        existing_query = (
            db.table("meanings")
            .select("id, definition_ja")
            .eq("word_id", word_id)
            .eq("part_of_speech", item["part_of_speech"])
            .eq("definition_en", source_definition)
        )
        if transitivity:
            existing_query = existing_query.eq("transitivity", transitivity)
        if countability:
            existing_query = existing_query.eq("countability", countability)
        existing_meaning = existing_query.execute().data or []

        payload = {
            "word_id": word_id,
            "part_of_speech": item["part_of_speech"],
            "definition_en": source_definition,
            "definition_ja": definition_ja,
            "transitivity": transitivity,
            "countability": countability,
            "source": "wiktionary",
            "inflections": {"wiktionary_raw": item.get("raw", {})},
            "tier": None,
        }
        if existing_meaning:
            meaning_id = existing_meaning[0]["id"]
            if not definition_ja and (existing_meaning[0].get("definition_ja") or "").strip():
                payload.pop("definition_ja", None)
            db.table("meanings").update(payload).eq("id", meaning_id).execute()
            meaning_res_data = [{"id": meaning_id}]
        else:
            meaning_res = db.table("meanings").insert(payload).execute()
            meaning_res_data = meaning_res.data or []

        if item.get("examples") and meaning_res_data and not existing_meaning:
            meaning_id = meaning_res_data[0]["id"]
            for sentence in item["examples"][:3]:
                db.table("example_sentences").insert({
                    "meaning_id": meaning_id,
                    "sentence": sentence,
                }).execute()

    if (
        generate_japanese
        and allow_fallback_generation
        and part_of_speech
        and _has_english_term_shape(word)
        and await has_cached_wiktionary_english_entry(word)
        and not _existing_meaning_rows(word_id, part_of_speech)
    ):
        definition_ja = await generate_fallback_japanese_definition(word, part_of_speech)
        if definition_ja:
            db.table("meanings").insert({
                "word_id": word_id,
                "part_of_speech": part_of_speech,
                "definition_en": None,
                "definition_ja": definition_ja,
                "source": "gemini",
                "tier": None,
            }).execute()

    if part_of_speech == "auxiliary" and not _count_meanings(word_id, part_of_speech):
        _insert_auxiliary_meaning(word_id, word)

    return _count_meanings(word_id, part_of_speech)

async def ensure_japanese_definitions_for_word(
    word_id: int,
    word: str,
    part_of_speech: Optional[str] = None,
) -> int:
    """
    既存 meaning の definition_ja が空なら、保存済み Wiktionary raw を使って日本語語義を補完する。
    """
    db = get_supabase()
    query = (
        db.table("meanings")
        .select("id, part_of_speech, definition_en, definition_ja, transitivity, inflections")
        .eq("word_id", word_id)
    )
    if part_of_speech:
        query = query.eq("part_of_speech", part_of_speech)
    meanings = query.execute().data or []
    updated = 0
    for meaning in meanings:
        if (meaning.get("definition_ja") or "").strip():
            continue
        raw = (meaning.get("inflections") or {}).get("wiktionary_raw") or {}
        source_item = {
            "part_of_speech": meaning.get("part_of_speech"),
            "definition": meaning.get("definition_en"),
            "definitions": (meaning.get("definition_en") or "").split(" / "),
            "transitivity": meaning.get("transitivity"),
            "raw": raw,
        }
        if isinstance(raw, dict):
            source_item["senses"] = raw.get("wiktextract_senses") or []
        definition_ja = await generate_japanese_definition(
            word,
            meaning.get("part_of_speech") or "",
            source_item,
        )
        if not definition_ja:
            continue
        db.table("meanings").update({"definition_ja": definition_ja}).eq(
            "id",
            meaning["id"],
        ).execute()
        updated += 1
    return updated


async def batch_generate_missing_japanese_definitions(
    items: list[dict],
    chunk_size: int | None = None,
) -> int:
    """
    教材内の語義について、definition_ja が空の既存 meaning と
    Wiktionary で該当品詞が取れなかった単語品詞をまとめて Gemini 生成する。
    """
    db = get_supabase()
    requests: list[dict] = []
    fallback_targets: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()

    for item in items:
        word = (item.get("text") or "").strip().lower()
        part_of_speech = normalize_part_of_speech(item.get("part_of_speech"))
        if not word or not part_of_speech or not _has_english_term_shape(word):
            continue
        key = (word, part_of_speech)
        if key in seen:
            continue
        seen.add(key)

        word_rows = db.table("words").select("id").eq("word", word).execute().data or []
        if not word_rows:
            continue
        word_id = word_rows[0]["id"]
        all_meaning_rows = (
            db.table("meanings")
            .select("id, part_of_speech, definition_en, definition_ja, transitivity, countability")
            .eq("word_id", word_id)
            .execute()
            .data
            or []
        )
        meaning_rows = [
            row
            for row in all_meaning_rows
            if _meaning_pos_matches(
                part_of_speech,
                normalize_part_of_speech(row.get("part_of_speech")) or "",
            )
        ]
        if not meaning_rows:
            # DBに互換品詞のmeaningがない場合に限りWiktionaryで英語見出し語を
            # 確認する。DBにある語を外部判定の失敗で落とさない。
            if not await has_cached_wiktionary_english_entry(word):
                continue
            # 同じ品詞の保存済み定義がなければ、termと品詞から直接生成する。
            # 生成に失敗したものだけを「訳なし」として残す。
            request_id = f"fallback:{word_id}:{part_of_speech}"
            requests.append({
                "id": request_id,
                "term": word,
                "part_of_speech": part_of_speech,
                "definition_en": None,
                "transitivity": None,
                "countability": None,
            })
            fallback_targets[request_id] = {
                "word_id": word_id,
                "part_of_speech": part_of_speech,
            }
            continue

        for meaning in meaning_rows:
            if (meaning.get("definition_ja") or "").strip():
                continue
            requests.append({
                "id": f"meaning:{meaning['id']}",
                "term": word,
                "part_of_speech": meaning.get("part_of_speech") or part_of_speech,
                "definition_en": meaning.get("definition_en"),
                "transitivity": meaning.get("transitivity"),
                "countability": meaning.get("countability"),
            })

    updated = 0
    failed_count = 0
    # 初回生成は不足分をすべて1回で送る。欠落したIDだけを内部で再試行する。
    effective_chunk_size = chunk_size or max(1, len(requests))
    for start in range(0, len(requests), effective_chunk_size):
        chunk = requests[start:start + effective_chunk_size]
        generated = await generate_japanese_definitions_batch(chunk)
        requested_ids = {str(request["id"]) for request in chunk}
        failed_count += len(requested_ids - set(generated))
        for request_id, definition_ja in generated.items():
            if request_id.startswith("meaning:"):
                meaning_id = int(request_id.split(":", 1)[1])
                db.table("meanings").update({"definition_ja": definition_ja}).eq("id", meaning_id).execute()
                updated += 1
                continue

            fallback = fallback_targets.get(request_id)
            if not fallback:
                continue
            db.table("meanings").insert({
                "word_id": fallback["word_id"],
                "part_of_speech": fallback["part_of_speech"],
                "definition_en": None,
                "definition_ja": definition_ja,
                "source": "gemini",
                "tier": None,
            }).execute()
            updated += 1
    if failed_count:
        # 生成済み分は保存したうえで、画面へ部分失敗を明示する。黙って成功扱いに
        # すると不足語が「訳なし」に残った理由を利用者が判断できない。
        raise RuntimeError(f"Gemini failed to generate {failed_count} meaning(s).")
    return updated


def get_word_with_meanings(word_id: int) -> dict:
    db = get_supabase()
    word_row = db.table("words").select("*").eq("id", word_id).execute()
    if not word_row.data:
        return {}
    _cleanup_invalid_meanings(word_id)
    result = word_row.data[0]
    meanings = (
        db.table("meanings")
        .select("*, example_sentences(*)")
        .eq("word_id", word_id)
        .execute()
        .data or []
    )
    result["meanings"] = [_drop_invalid_definition_en(meaning) for meaning in meanings]
    return result


async def regenerate_missing_japanese_definitions(
    meaning_ids: list[int], chunk_size: int = 20
) -> int:
    """指定された語義のうち日本語訳が空のものだけをGeminiで再生成する。"""
    ids = sorted(set(meaning_ids))
    if not ids:
        return 0
    db = get_supabase()
    rows = (
        db.table("meanings")
        .select(
            "id, part_of_speech, definition_en, definition_ja, "
            "transitivity, countability, words(word)"
        )
        .in_("id", ids)
        .execute()
        .data
        or []
    )
    requests = []
    for row in rows:
        if (row.get("definition_ja") or "").strip():
            continue
        word = (row.get("words") or {}).get("word") or ""
        if not word:
            continue
        requests.append({
            "id": f"meaning:{row['id']}",
            "term": word,
            "part_of_speech": row.get("part_of_speech"),
            "definition_en": row.get("definition_en"),
            "transitivity": row.get("transitivity"),
            "countability": row.get("countability"),
        })
    updated = 0
    for start in range(0, len(requests), chunk_size):
        generated = await generate_japanese_definitions_batch(
            requests[start:start + chunk_size]
        )
        for request_id, definition in generated.items():
            meaning_id = int(request_id.split(":", 1)[1])
            db.table("meanings").update({"definition_ja": definition}).eq(
                "id", meaning_id
            ).execute()
            updated += 1
    return updated


def record_wordbook_registration(
    wordbook_word_id: int,
    wordbook_id: Optional[str] = None,
    source_type: str = "manual",
    source_material_id: Optional[str] = None,
    source_folder_id: Optional[str] = None,
    source_label: Optional[str] = None,
) -> None:
    db = get_supabase()
    source = source_type.strip() or "manual"
    try:
        query = (
            db.table("wordbook_word_registrations")
            .select("id")
            .eq("wordbook_word_id", wordbook_word_id)
            .eq("source_type", source)
        )
        query = query.eq("wordbook_id", wordbook_id) if wordbook_id else query.is_("wordbook_id", "null")
        query = query.eq("material_id", source_material_id) if source_material_id else query.is_("material_id", "null")
        query = query.eq("folder_id", source_folder_id) if source_folder_id else query.is_("folder_id", "null")
        query = query.eq("label", source_label) if source_label else query.is_("label", "null")
        if query.limit(1).execute().data:
            return
        db.table("wordbook_word_registrations").insert(
            {
                "wordbook_word_id": wordbook_word_id,
                "wordbook_id": wordbook_id,
                "source_type": source,
                "material_id": source_material_id,
                "folder_id": source_folder_id,
                "label": source_label,
            }
        ).execute()
    except Exception:
        # Keep existing wordbook behavior working before the source migration is applied.
        return


async def add_meanings_to_wordbook(
    user_id: str,
    word_id: int,
    part_of_speech: Optional[str] = None,
    source_type: str = "manual",
    source_material_id: Optional[str] = None,
    source_folder_id: Optional[str] = None,
    source_label: Optional[str] = None,
    is_learned: bool = False,
    wordbook_id: Optional[str] = None,
) -> list[int]:
    """
    指定した word_id の全 meaning を、ユーザーの単語帳に「未学習」として追加する。
    すでに登録済みの meaning はスキップ。
    戻り値: 追加された wordbook_words の id のリスト
    """
    db = get_supabase()
    query = (
        db.table("meanings")
        .select("id")
        .eq("word_id", word_id)
        .filter("definition_ja", "not.is", "null")
        .neq("definition_ja", "")
    )
    normalized_pos = normalize_part_of_speech(part_of_speech)
    if normalized_pos:
        query = query.eq("part_of_speech", normalized_pos)
    # 同じ単語・同じ品詞に複数の意味がある場合、画面上で先頭になる
    # 最小IDのmeaningだけを単語帳へ登録する。
    # spaCy由来の品詞詳細がDBになくても、それを包含する
    # meanings.part_of_speech が一致する先頭meaningへマッチさせる。
    meanings = query.order("id").limit(1).execute().data
    if not meanings:
        return []

    meaning_ids = [m["id"] for m in meanings]
    existing = (
        db.table("wordbook_words")
        .select("id, meaning_id")
        .eq("user_id", user_id)
        .in_("meaning_id", meaning_ids)
        .execute()
        .data
    )
    existing_by_meaning_id = {row["meaning_id"]: row["id"] for row in existing}
    if existing_by_meaning_id:
        (
            db.table("wordbook_words")
            .update({
                "is_learned": is_learned,
            })
            .in_("id", list(existing_by_meaning_id.values()))
            .execute()
        )

    rows_to_insert = [
        {
            "user_id": user_id,
            "meaning_id": mid,
            "is_learned": is_learned,
        }
        for mid in meaning_ids
        if mid not in existing_by_meaning_id
    ]
    inserted_ids: list[int] = []

    if rows_to_insert:
        inserted = db.table("wordbook_words").insert(rows_to_insert).execute()
        inserted_ids = [row["id"] for row in inserted.data]
        for row in inserted.data:
            existing_by_meaning_id[row["meaning_id"]] = row["id"]

    for wordbook_word_id in existing_by_meaning_id.values():
        record_wordbook_registration(
            wordbook_word_id,
            wordbook_id=wordbook_id,
            source_type=source_type,
            source_material_id=source_material_id,
            source_folder_id=source_folder_id,
            source_label=source_label,
        )
    # 呼び出し側が独立単語帳への所属を作れるよう、既存分も含む全IDを返す。
    return list(existing_by_meaning_id.values())
