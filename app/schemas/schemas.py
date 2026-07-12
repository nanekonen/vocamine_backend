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
    auxiliary = "auxiliary"
    numeral = "numeral"
    prefix = "prefix"
    suffix = "suffix"
    phrase = "phrase"
    abbreviation = "abbreviation"


class DictionarySource(str, Enum):
    wiktionary = "wiktionary"
    grammar = "grammar"
    gemini = "gemini"
    cefr_j = "cefr_j"
    phave_list = "phave_list"
    phrase_list = "phrase_list"
    academic_collocation_list = "academic_collocation_list"
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
class OCRWordBox(BaseModel):
    text: str
    page_index: int = 0
    start: Optional[int] = None
    end: Optional[int] = None
    left: float
    top: float
    width: float
    height: float


class OCRResponse(BaseModel):
    text: str
    word_boxes: list[OCRWordBox] = Field(default_factory=list)


class PDFOCRResponse(OCRResponse):
    page_images: list[str] = Field(default_factory=list)


# --- Materials ---
class MaterialFolderCreate(BaseModel):
    user_id: str
    name: str
    parent_id: Optional[str] = None


class MaterialFolderResponse(BaseModel):
    id: str
    user_id: str
    name: str
    parent_id: Optional[str] = None
    created_at: datetime


class MaterialFolderUpdate(BaseModel):
    user_id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None
    update_parent: bool = False


class MaterialCreate(BaseModel):
    user_id: str
    title: str
    extracted_text: str = ""
    ocr_text: str = ""
    folder_id: Optional[str] = None
    source_mime_type: Optional[str] = None
    source_base64: Optional[str] = None
    readable_pdf_base64: Optional[str] = None
    page_images_base64: list[str] = Field(default_factory=list)
    word_boxes: list[OCRWordBox] = Field(default_factory=list)


class MaterialUpdate(BaseModel):
    user_id: str
    title: Optional[str] = None
    folder_id: Optional[str] = None
    update_folder: bool = False


class MaterialPagesAppend(BaseModel):
    user_id: str
    extracted_text: str
    page_images_base64: list[str] = Field(default_factory=list)
    word_boxes: list[OCRWordBox] = Field(default_factory=list)


class MaterialResponse(BaseModel):
    id: str
    user_id: str
    folder_id: Optional[str] = None
    default_wordbook_id: Optional[str] = None
    title: str
    extracted_text: str = ""
    source_mime_type: Optional[str] = None
    source_object_storage_key: Optional[str] = None
    readable_pdf_object_storage_key: Optional[str] = None
    thumbnail_object_storage_key: Optional[str] = None
    page_images: list[str] = Field(default_factory=list)
    word_boxes: list[OCRWordBox] = Field(default_factory=list)
    created_at: datetime


class MaterialLibraryResponse(BaseModel):
    folders: list[MaterialFolderResponse] = Field(default_factory=list)
    materials: list[MaterialResponse] = Field(default_factory=list)


# --- Example Sentence ---
class ExampleSentenceResponse(BaseModel):
    id: int
    meaning_id: int
    sentence: str
    translated_sentence: Optional[str] = None


# --- Word (embedded in Meaning) ---
class WordInfo(BaseModel):
    id: int
    word: str


# --- Meaning ---
class MeaningResponse(BaseModel):
    id: int
    word_id: int
    part_of_speech: PartOfSpeech
    definition_en: Optional[str] = None
    definition_ja: Optional[str] = None
    ipa: Optional[str] = None
    transitivity: Optional[TransitivityType] = None
    countability: Optional[CountabilityType] = None
    inflections: Optional[Any] = None
    source: DictionarySource
    tier: Optional[int] = None
    words: Optional[WordInfo] = None
    example_sentences: list[ExampleSentenceResponse] = Field(default_factory=list)


class MeaningCreate(BaseModel):
    part_of_speech: PartOfSpeech
    definition_en: Optional[str] = None
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
    sources: list[dict[str, Any]] = Field(default_factory=list)


class WordbookWordCreate(BaseModel):
    meaning_id: int
    source_type: str = "manual"
    source_material_id: Optional[str] = None
    source_folder_id: Optional[str] = None
    source_label: Optional[str] = None
    wordbook_id: Optional[str] = None


class WordbookWordUpdate(BaseModel):
    is_learned: bool


# --- Extract unknown words ---
class LexicalItem(BaseModel):
    text: str
    part_of_speech: PartOfSpeech
    part_of_speech_detail: Optional[str] = None
    surface_forms: list[str] = Field(default_factory=list)
    occurrences: list[dict[str, Any]] = Field(default_factory=list)
    kind: str = "word"  # "word" | "phrase"


class LexicalItemResult(LexicalItem):
    is_learned: bool
    has_meaning: bool = False
    occurrence_count: int = 1


class ExtractWordsRequest(BaseModel):
    text: str
    user_id: str
    enrich_meanings: bool = False
    background_enrich_meanings: bool = True


class StoredMeaningsRequest(BaseModel):
    items: list[LexicalItem] = Field(default_factory=list)


class RegenerateJapaneseRequest(BaseModel):
    meaning_ids: list[int] = Field(default_factory=list)


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
    is_learned: bool = False
    source_type: str = "manual"
    source_material_id: Optional[str] = None
    source_folder_id: Optional[str] = None
    source_label: Optional[str] = None
    wordbook_id: Optional[str] = None


class IndependentWordbookFolderCreate(BaseModel):
    user_id: str
    name: str
    parent_id: Optional[str] = None


class IndependentWordbookFolderUpdate(BaseModel):
    user_id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None
    update_parent: bool = False


class IndependentWordbookCreate(BaseModel):
    user_id: str
    name: str
    folder_id: Optional[str] = None


class IndependentWordbookUpdate(BaseModel):
    user_id: str
    name: Optional[str] = None
    folder_id: Optional[str] = None
    update_folder: bool = False


class MaterialDefaultWordbookUpdate(BaseModel):
    user_id: str
    wordbook_id: Optional[str] = None


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
    username: str
    updated_at: datetime


# --- Level setup ---
class LevelSetupRequest(BaseModel):
    level: str
    username: str


# --- Auth ---
class AuthSessionRequest(BaseModel):
    access_token: str


class AuthSessionResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    username: Optional[str] = None
    level: Optional[str] = None
    setup_completed: bool = False
