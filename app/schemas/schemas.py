from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime
from enum import Enum


# --- Enums ---
class PartOfSpeech(str, Enum):
    noun = "noun"
    verb = "verb"
    adjective = "adjective"
    adverb = "adverb"
    pronoun = "pronoun"
    preposition = "preposition"
    conjunction = "conjunction"
    interjection = "interjection"
    determiner = "determiner"
    article = "article"
    numeral = "numeral"
    prefix = "prefix"
    suffix = "suffix"
    phrase = "phrase"
    abbreviation = "abbreviation"


class DictionarySource(str, Enum):
    wiktionary = "wiktionary"
    cefr_j = "cefr_j"
    collins = "collins"
    cambridge = "cambridge"
    oxford = "oxford"
    manual = "manual"


class TransitivityType(str, Enum):
    transitive = "transitive"
    intransitive = "intransitive"
    both = "both"


class CountabilityType(str, Enum):
    countable = "countable"
    uncountable = "uncountable"
    both = "both"


# --- OCR ---
class OCRResponse(BaseModel):
    text: str


# --- Example Sentence ---
class ExampleSentenceResponse(BaseModel):
    id: int
    meaning_id: int
    sentence: str
    translated_sentence: Optional[str] = None


# --- Meaning ---
class MeaningResponse(BaseModel):
    id: int
    word_id: int
    part_of_speech: PartOfSpeech
    definition: str
    definition_ja: Optional[str] = None
    ipa: Optional[str] = None
    transitivity: Optional[TransitivityType] = None
    countability: Optional[CountabilityType] = None
    inflections: Optional[Any] = None
    source: DictionarySource
    tier: Optional[int] = None
    example_sentences: list[ExampleSentenceResponse] = Field(default_factory=list)


class MeaningCreate(BaseModel):
    part_of_speech: PartOfSpeech
    definition: str
    definition_ja: Optional[str] = None
    ipa: Optional[str] = None
    transitivity: Optional[TransitivityType] = None
    countability: Optional[CountabilityType] = None
    inflections: Optional[Any] = None
    source: DictionarySource
    tier: Optional[int] = None


# --- Word ---
class WordResponse(BaseModel):
    id: int
    word: str
    created_at: datetime
    meanings: list[MeaningResponse] = Field(default_factory=list)


# --- Wordbook ---
class WordbookWordResponse(BaseModel):
    id: int
    user_id: str
    meaning_id: int
    is_learned: bool
    created_at: datetime
    meaning: Optional[MeaningResponse] = Field(default=None, validation_alias="meanings")


class WordbookWordCreate(BaseModel):
    meaning_id: int


class WordbookWordUpdate(BaseModel):
    is_learned: bool


# --- Extract unknown words ---
class LexicalItem(BaseModel):
    text: str
    part_of_speech: PartOfSpeech
    kind: str = "word"  # "word" | "phrase"


class LexicalItemResult(LexicalItem):
    is_learned: bool
    has_meaning: bool = False


class ExtractWordsRequest(BaseModel):
    text: str
    user_id: str
    enrich_meanings: bool = False
    background_enrich_meanings: bool = True


class ExtractWordsResponse(BaseModel):
    unknown_words: list[str]
    total_words: int
    unknown_count: int
    known_count: int = 0
    coverage_rate: float = 0.0
    items: list[LexicalItemResult] = Field(default_factory=list)
    unknown_items: list[LexicalItemResult] = Field(default_factory=list)


# --- Batch word registration ---
class BatchWordsRequest(BaseModel):
    user_id: str
    words: list[str] = Field(default_factory=list)
    items: list[LexicalItem] = Field(default_factory=list)
    enrich_meanings: bool = True


class BatchWordResult(BaseModel):
    word: str
    part_of_speech: Optional[PartOfSpeech] = None
    status: str  # "registered" | "already_in_wordbook" | "no_meaning_found" | "error"
    meaning_count: int
    error: Optional[str] = None


class BatchWordsResponse(BaseModel):
    total: int
    registered_count: int
    skipped_no_meaning_count: int
    results: list[BatchWordResult]


# --- User ---
class UserLevelUpdate(BaseModel):
    level: str


class UserResponse(BaseModel):
    id: str
    level: str
    updated_at: datetime


# --- Level setup ---
class LevelSetupRequest(BaseModel):
    level: str
