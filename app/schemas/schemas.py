from __future__ import annotations
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# --- OCR ---
class OCRResponse(BaseModel):
    text: str


# --- Example Sentence ---
class ExampleSentenceResponse(BaseModel):
    id: int
    sentence: str
    translated_sentence: Optional[str] = None


# --- Meaning ---
class MeaningResponse(BaseModel):
    id: int
    word_id: int
    part_of_speech: str
    definition: str
    ipa: Optional[str] = None
    tier: Optional[int] = None
    example_sentence: Optional[ExampleSentenceResponse] = None


class MeaningCreate(BaseModel):
    part_of_speech: str
    definition: str
    ipa: Optional[str] = None
    tier: Optional[int] = None
    example_sentence: Optional[str] = None
    translated_sentence: Optional[str] = None


# --- Word ---
class WordResponse(BaseModel):
    id: int
    word: str
    created_at: datetime
    meanings: list[MeaningResponse] = []


# --- Wordbook ---
class WordbookWordResponse(BaseModel):
    id: int
    user_id: str
    meaning_id: int
    is_learned: bool
    created_at: datetime
    meaning: Optional[MeaningResponse] = None


class WordbookWordCreate(BaseModel):
    meaning_id: int


class WordbookWordUpdate(BaseModel):
    is_learned: bool


# --- Extract unknown words ---
class ExtractWordsRequest(BaseModel):
    text: str
    user_id: str


class ExtractWordsResponse(BaseModel):
    unknown_words: list[str]
    total_words: int
    unknown_count: int


# --- Batch word registration (OCR結果 → 単語帳一括登録) ---
class BatchWordsRequest(BaseModel):
    user_id: str
    words: list[str]


class BatchWordResult(BaseModel):
    word: str
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