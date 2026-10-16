"""Stałe, ścieżki i ustawienia modeli (bez Streamlit UI)."""
from __future__ import annotations

from pathlib import Path

import streamlit as st


APP_VERSION = "4.16.0"
APP_NAME = "Generator opisów i metatagów produktów"
DESCRIPTION_PROMPT_VERSION = "description-v4.10-contextual-links"
PROMPT_VERSION = "meta-v4.9.0-contributors-description-quality-akeneo-write-2026-09"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_CHANNEL = "Bookland"
DEFAULT_LOCALE = "pl_PL"
AKENEO_TIMEOUT = 20
PERPLEXITY_TIMEOUT = 45
AKENEO_MAX_WORKERS = 4
GEMINI_INTERACTIVE_WORKERS = 2
INTERACTIVE_CHUNK_SIZE = 10
GEMINI_HTTP_TIMEOUT_MS = 60_000
AKENEO_MAX_ATTEMPTS = 3
AKENEO_META_UPDATE_CHUNK_SIZE = 100
BATCH_PRODUCTS_PER_FILE = 1000
AKENEO_SKU_FILTER_CHUNK_SIZE = 50
MAX_META_RETRIES = 2
RESULT_PREVIEW_LIMIT = 100

# Meta title: priorytetem jest kompletna identyfikacja wariantu produktu.
# Nie próbujemy sztucznie mieścić się w klasycznym limicie SERP. Google może
# skrócić prezentację, natomiast źródłowy title ma zachować ważne cechy SKU.
META_TITLE_TARGET_MIN = 58
META_TITLE_TARGET_MAX = 72
META_TITLE_HARD_MAX = 75
DB_PATH = Path(".streamlit/product_workflow.sqlite3")
BATCH_DIR = Path(".streamlit/gemini_batches")
IMPORT_REPORT_DIR = Path(".streamlit/import_reports")
INTERACTIVE_CHECKPOINT_DIR = Path(".streamlit/interactive_checkpoints")
LINK_FIELDS = {
    "school": ["school_type", "typ_szkoly", "szkola"],
    "grade": ["grade", "klasa"],
    "subject": ["subject", "przedmiot"],
    "series": ["series", "seria"],
    "edition": ["edition", "edycja", "wydanie"],
}
REQUIRED_SECRETS = [
    "AKENEO_BASE_URL",
    "AKENEO_CLIENT_ID",
    "AKENEO_SECRET",
    "AKENEO_USERNAME",
    "AKENEO_PASSWORD",
    "GOOGLE_API_KEY",
]
_POLISH_CHARS = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

# Elementy, które nie mogą trafić do meta title. Są to typowe oznaczenia
# magazynowe, statusy i wartości zastępcze spotykane w danych produktowych.
TITLE_INTERNAL_NOISE_PATTERNS = (
    r"^\s*zz[\s._-]+",
    r"\bO\.?O\.?P\.?\b",
    r"\bOUT OF PRINT\b",
    r"\bNIEDOSTĘPNY\b",
    r"\bWYCOFANY\b",
)
TITLE_DANGLING_WORDS = {
    "a", "an", "and", "or", "with", "without", "for", "from", "to", "of", "the",
    "i", "oraz", "lub", "z", "ze", "dla", "do", "od", "bez", "w", "na", "kod", "access",
    "student", "teacher",
}
TITLE_GENERIC_TOKENS = {
    "book", "books", "ebook", "etext", "online", "access", "code", "kod", "dostep", "dostępu",
    "student", "students", "teacher", "teachers", "coursebook", "workbook", "podrecznik",
    "podręcznik", "cwiczenia", "ćwiczenia", "ksiazka", "książka", "wydanie", "new", "plus",
}
TITLE_COMPONENT_TRANSLATIONS = (
    ("Student's Book", "podręcznik ucznia"),
    ("Students' Book", "podręcznik ucznia"),
    ("Student Book", "podręcznik ucznia"),
    ("Workbook", "zeszyt ćwiczeń"),
    ("Activity Book", "zeszyt ćwiczeń"),
    ("Coursebook", "podręcznik"),
    ("Teacher's Book", "książka nauczyciela"),
    ("Teacher Book", "książka nauczyciela"),
    ("Teacher's Online Access Code", "kod nauczyciela"),
    ("Student Online Access Code", "kod uczniowski"),
    ("Online Practice", "ćwiczenia online"),
    ("Practice Book", "zeszyt ćwiczeń"),
    ("Test Book", "testy"),
)
POLISH_STOPWORDS = {
    "a", "aby", "albo", "ale", "ani", "aż", "bardziej", "bardzo", "bez", "bo", "by", "być",
    "co", "czy", "dla", "do", "gdy", "gdzie", "go", "ich", "jak", "jako", "jest", "jeśli", "już",
    "która", "które", "który", "ma", "między", "może", "na", "nad", "nie", "o", "od", "oraz", "po",
    "pod", "przez", "przy", "się", "są", "tak", "także", "tego", "tej", "ten", "to", "tu", "tylko",
    "w", "we", "więc", "więcej", "z", "za", "ze", "że", "książka", "książki", "publikacja", "produkt",
}


def _secret(name: str, default: str = "") -> str:
    # Bez pliku secrets (np. w testach) st.secrets rzuca wyjątek – wtedy wartość domyślna.
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


GEMINI_MODEL = _secret("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
TURBO_BATCH_SHARD_SIZE = 1000
BATCH_FAILED_STATES = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
BATCH_SUBMIT_WORKERS = 12
BATCH_ACTIVE_JOB_SOFT_CAP = 80
BATCH_MAX_SHARDS_PER_SUBMIT = 80
BATCH_DOWNLOAD_WORKERS = 8
META_DESCRIPTION_HARD_MIN = 120
META_DESCRIPTION_HARD_MAX = 170
META_DESCRIPTION_TARGET_MIN = 135
META_DESCRIPTION_TARGET_MAX = 160
GEMINI_META_MAX_OUTPUT_TOKENS = 320
BATCH_REFRESH_WORKERS = 20
