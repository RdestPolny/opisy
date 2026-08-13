from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st
from google import genai
from google.genai import types

from description_output import is_meta_only_result, is_reusable_result, validate_description_html

try:
    from product_input import ProductInputResolutionError, resolve_product_inputs
except ImportError:
    class ProductInputResolutionError(ValueError):
        pass

    def resolve_product_inputs(raw_inputs: Sequence[str]) -> Tuple[List[Dict], List[str]]:
        """Awaryjny resolver: przyjmuje SKU lub URL kończący się SKU."""
        resolved: List[Dict] = []
        errors: List[str] = []
        for raw in raw_inputs:
            value = raw.strip()
            if not value:
                continue
            if value.startswith("http://") or value.startswith("https://"):
                slug = value.rstrip("/").split("/")[-1]
                match = re.search(r"(?:sku[-_/]?)?([A-Za-z0-9._-]{3,})$", slug)
                if not match:
                    errors.append(f"Nie udało się rozpoznać SKU z URL: {value}")
                    continue
                sku = match.group(1)
                resolved.append({"input": value, "sku": sku, "title": sku, "source": "url"})
            else:
                resolved.append({"input": value, "sku": value, "title": value, "source": "sku"})
        return resolved, errors


# ═══════════════════════════════════════════════════════════════════
# STAŁE I KONFIGURACJA
# ═══════════════════════════════════════════════════════════════════

APP_VERSION = "4.7.0"
APP_NAME = "Generator opisów i metatagów produktów"
PROMPT_VERSION = "meta-v4.4.4-validator-driven-title-autorepair-2026-08"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_CHANNEL = "Bookland"
DEFAULT_LOCALE = "pl_PL"

AKENEO_TIMEOUT = 20
PERPLEXITY_TIMEOUT = 45
AKENEO_MAX_WORKERS = 4
GEMINI_INTERACTIVE_WORKERS = 3
INTERACTIVE_CHUNK_SIZE = 12
GEMINI_HTTP_TIMEOUT_MS = 45_000
AKENEO_MAX_ATTEMPTS = 3
BATCH_PRODUCTS_PER_FILE = 2500
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
INTERACTIVE_CHECKPOINT_EVERY = 1
INTERACTIVE_UI_UPDATE_EVERY = 1

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

# Używamy prostego podzbioru JSON Schema zgodnego także ze starszymi
# wersjami endpointu generateContent i pakietu google-genai. Celowo nie dodajemy
# additionalProperties, ponieważ część wersji API serializuje je niezgodnie.
# Gemini generuje teraz oba pola: zoptymalizowany meta title i meta description.
META_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_title": {
            "type": "string",
            "description": "Unikalny meta title produktu, maksymalnie 75 znaków ze spacjami.",
        },
        "meta_description": {
            "type": "string",
            "description": "Unikalny polski meta description produktu, 140-160 znaków, bez CTA.",
        },
    },
    "required": ["meta_title", "meta_description"],
}

BANNED_STARTERS = (
    "odkryj",
    "poznaj",
    "sprawdź",
    "sprawdz",
    "sięgnij",
    "siegnij",
    "zanurz",
    "przenieś się",
    "przenies sie",
    "daj się",
    "daj sie",
    "ta książka",
    "ta ksiazka",
    "książka",
    "ksiazka",
    "w tej książce",
    "w tej ksiazce",
    "idealna propozycja",
    "to wyjątkowa",
    "to wyjatkowa",
    "jeśli szukasz",
    "jesli szukasz",
    "szukasz",
    "warto sięgnąć",
    "warto siegnac",
    "oto",
    "pozycja, która",
    "pozycja ktora",
)

BANNED_CTA_PHRASES = (
    "sprawdź ofertę",
    "sprawdz oferte",
    "kup teraz",
    "zamów teraz",
    "zamow teraz",
    "zamów już dziś",
    "zamow juz dzis",
    "dodaj do koszyka",
    "nie czekaj",
    "przekonaj się",
    "przekonaj sie",
    "sięgnij po",
    "siegnij po",
    "poznaj ofertę",
    "poznaj oferte",
    "odkryj ofertę",
    "odkryj oferte",
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

OPENING_MODES = (
    "Zacznij od konkretnego konfliktu lub problemu obecnego w opisie źródłowym.",
    "Zacznij od bohatera, autora albo głównego podmiotu i jego sytuacji.",
    "Zacznij od miejsca, epoki lub czasu, o ile źródło rzeczywiście je wskazuje.",
    "Zacznij od stawki: co może zostać utracone, zmienione albo zrozumiane.",
    "Zacznij od najważniejszego motywu, bez używania ogólnej oceny produktu.",
    "Zacznij od relacji między postaciami, ideami lub elementami produktu.",
    "Zacznij od konkretnej obietnicy wiedzy, ale bez języka reklamowego i CTA.",
    "Zacznij od problemu praktycznego, który produkt pomaga zrozumieć lub rozwiązać.",
    "Zacznij od nieoczywistego kontrastu wynikającego z opisu źródłowego.",
    "Zacznij od wydarzenia uruchamiającego fabułę albo tok wywodu.",
    "Zacznij od emocji lub napięcia, ale nazwij ich źródło konkretnie.",
    "Zacznij od pytania merytorycznego, lecz nie od pytania sprzedażowego.",
    "Zacznij od nazwy serii, tomu lub cyklu, jeżeli dane to potwierdzają.",
    "Zacznij od roli odbiorcy i sytuacji, w której treść będzie dla niego użyteczna.",
    "Zacznij od szczegółu, symbolu albo obrazu wyraźnie obecnego w źródle.",
    "Zacznij od tezy lub najważniejszego wniosku, który zapowiada zawartość.",
    "Zacznij od zmiany, przez którą przechodzi bohater, czytelnik lub omawiany proces.",
    "Zacznij od konsekwencji decyzji opisanej w materiale źródłowym.",
    "Zacznij od konkretnego tematu przewodniego połączonego z gatunkiem lub formą.",
    "Zacznij od napięcia między dwoma celami, wartościami albo punktami widzenia.",
    "Zacznij zdaniem krótkim i rzeczowym, a drugie niech rozwinie kontekst.",
    "Zacznij zdaniem złożonym opartym na przyczynie i skutku obecnych w źródle.",
    "Zacznij od faktu technicznego lub zakresu treści, jeśli to produkt użytkowy albo edukacyjny.",
    "Zacznij od najbardziej charakterystycznej informacji, której nie da się łatwo przypisać innemu produktowi.",
)

RHYTHM_MODES = (
    "Dwa zdania: pierwsze krótsze, drugie rozwijające.",
    "Jedno zwarte zdanie z naturalnym rytmem i bez wyliczenia.",
    "Dwa zdania podobnej długości, połączone logicznie.",
    "Pierwsze zdanie informacyjne, drugie pokazujące stawkę lub korzyść poznawczą.",
    "Pierwsze zdanie osadza kontekst, drugie dopowiada główny wyróżnik.",
    "Jedno zdanie z wyraźnym podmiotem i konkretnym czasownikiem.",
)

FOCUS_MODES = (
    "fabuła lub przebieg zdarzeń",
    "bohater albo główny podmiot",
    "temat i sens publikacji",
    "konflikt lub problem",
    "kontekst miejsca i czasu",
    "konkretna użyteczność dla odbiorcy",
    "wyróżnik serii, tomu lub formatu",
    "emocjonalna stawka bez przesadnego wartościowania",
)

POLISH_STOPWORDS = {
    "a", "aby", "albo", "ale", "ani", "aż", "bardziej", "bardzo", "bez", "bo", "by", "być",
    "co", "czy", "dla", "do", "gdy", "gdzie", "go", "ich", "jak", "jako", "jest", "jeśli", "już",
    "która", "które", "który", "ma", "między", "może", "na", "nad", "nie", "o", "od", "oraz", "po",
    "pod", "przez", "przy", "się", "są", "tak", "także", "tego", "tej", "ten", "to", "tu", "tylko",
    "w", "we", "więc", "więcej", "z", "za", "ze", "że", "książka", "książki", "publikacja", "produkt",
}


# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STREAMLIT
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title=f"{APP_NAME} v{APP_VERSION}",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

from visual_editor import visual_html_editor

st.markdown(
    """
<style>
    .main-header { font-size: 2.35rem; font-weight: 700; margin-bottom: .35rem; }
    .sub-header { color: #666; font-size: 1rem; margin-bottom: 1.5rem; }
    .scrollable-results { max-height: 420px; overflow-y: auto; border: 1px solid #e0e0e0;
        border-radius: .5rem; padding: 1rem; background: #fafafa; }
    .small-note { font-size: .86rem; color: #666; }
</style>
""",
    unsafe_allow_html=True,
)

missing_secrets = [key for key in REQUIRED_SECRETS if key not in st.secrets]
if missing_secrets:
    st.error(f"Brak kluczy w secrets.toml: {', '.join(missing_secrets)}")
    st.stop()

GEMINI_MODEL = str(st.secrets.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))
GOOGLE_API_KEY = str(st.secrets["GOOGLE_API_KEY"])


# ═══════════════════════════════════════════════════════════════════
# SQLITE: TRWAŁE ZADANIA, CHECKPOINTY I WYNIKI
# ═══════════════════════════════════════════════════════════════════

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS optimized_products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                first_optimized TEXT NOT NULL,
                last_optimized TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta_jobs (
                job_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'catalog',
                sku TEXT NOT NULL,
                channel TEXT NOT NULL,
                locale TEXT NOT NULL,
                store_view_code TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                source_updated TEXT NOT NULL DEFAULT '',
                input_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                style_seed INTEGER NOT NULL,
                opening_mode TEXT NOT NULL,
                rhythm_mode TEXT NOT NULL,
                focus_mode TEXT NOT NULL,
                semantic_cues TEXT NOT NULL DEFAULT '',
                source_lead TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                meta_title TEXT NOT NULL DEFAULT '',
                meta_description TEXT NOT NULL DEFAULT '',
                opening_signature TEXT NOT NULL DEFAULT '',
                short_opening_signature TEXT NOT NULL DEFAULT '',
                normalized_hash TEXT NOT NULL DEFAULT '',
                validation_errors TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                batch_job_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meta_jobs_status ON meta_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_batch ON meta_jobs(batch_job_name);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_opening ON meta_jobs(opening_signature);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_short_opening ON meta_jobs(short_opening_signature);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_hash ON meta_jobs(normalized_hash);

            CREATE TABLE IF NOT EXISTS batch_jobs (
                job_name TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL,
                model TEXT NOT NULL,
                input_path TEXT NOT NULL,
                input_file_name TEXT NOT NULL DEFAULT '',
                output_file_name TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'JOB_STATE_PENDING',
                product_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # Migracje dla baz utworzonych przez v4.0.0.
        def ensure_column(table: str, column: str, definition: str) -> None:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        ensure_column("meta_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column("meta_jobs", "source_type", "TEXT NOT NULL DEFAULT 'catalog'")
        ensure_column("batch_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_jobs_run ON meta_jobs(run_id)")


init_db()


def add_optimized_product(sku: str, title: str, url: str) -> None:
    now = utcnow_iso()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO optimized_products(sku, title, url, first_optimized, last_optimized)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                title=excluded.title,
                url=excluded.url,
                last_optimized=excluded.last_optimized
            """,
            (sku, title, url, now, now),
        )


def optimized_products_count() -> int:
    with db_connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM optimized_products").fetchone()[0])


def clear_optimized_products() -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM optimized_products")


def make_job_key(sku: str, channel: str, locale: str) -> str:
    return f"{channel}|{locale}|{sku}"


def upsert_meta_job(
    *,
    sku: str,
    channel: str,
    locale: str,
    store_view_code: str,
    product_data: Dict,
    source_updated: str = "",
    force_regenerate: bool = False,
    run_id: str = "",
    source_type: str = "catalog",
) -> Tuple[str, bool]:
    title = safe_string_value(product_data.get("title"))
    author = safe_string_value(product_data.get("author"))
    description = safe_string_value(product_data.get("description"))
    details = safe_string_value(product_data.get("details"))
    input_hash = product_input_hash(sku, title, author, description, channel, locale)
    style = build_style_plan(sku, title, author, description)
    job_key = make_job_key(sku, channel, locale)
    now = utcnow_iso()

    with db_connect() as conn:
        existing = conn.execute(
            "SELECT input_hash, prompt_version, model, status FROM meta_jobs WHERE job_key=?",
            (job_key,),
        ).fetchone()
        unchanged_completed = bool(
            existing
            and existing["input_hash"] == input_hash
            and existing["prompt_version"] == PROMPT_VERSION
            and existing["model"] == GEMINI_MODEL
            and existing["status"] == "completed"
            and not force_regenerate
        )
        if unchanged_completed:
            conn.execute(
                """
                UPDATE meta_jobs SET run_id=?, source_type=?, store_view_code=?, updated_at=?
                WHERE job_key=?
                """,
                (run_id, source_type, store_view_code, now, job_key),
            )
            return job_key, False

        conn.execute(
            """
            INSERT INTO meta_jobs(
                job_key, run_id, source_type, sku, channel, locale, store_view_code, title, author,
                description, details, source_updated, input_hash, prompt_version, model, style_seed,
                opening_mode, rhythm_mode, focus_mode, semantic_cues, source_lead, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
                run_id=excluded.run_id,
                source_type=excluded.source_type,
                store_view_code=excluded.store_view_code,
                title=excluded.title,
                author=excluded.author,
                description=excluded.description,
                details=excluded.details,
                source_updated=excluded.source_updated,
                input_hash=excluded.input_hash,
                prompt_version=excluded.prompt_version,
                model=excluded.model,
                style_seed=excluded.style_seed,
                opening_mode=excluded.opening_mode,
                rhythm_mode=excluded.rhythm_mode,
                focus_mode=excluded.focus_mode,
                semantic_cues=excluded.semantic_cues,
                source_lead=excluded.source_lead,
                status='queued',
                attempts=0,
                meta_title='',
                meta_description='',
                opening_signature='',
                short_opening_signature='',
                normalized_hash='',
                validation_errors='',
                error_message='',
                batch_job_name='',
                updated_at=excluded.updated_at
            """,
            (
                job_key,
                run_id,
                source_type,
                sku,
                channel,
                locale,
                store_view_code,
                title,
                author,
                description,
                details,
                source_updated,
                input_hash,
                PROMPT_VERSION,
                GEMINI_MODEL,
                style["seed"],
                style["opening_mode"],
                style["rhythm_mode"],
                style["focus_mode"],
                ", ".join(style["semantic_cues"]),
                style["source_lead"],
                now,
                now,
            ),
        )
    return job_key, True


def save_meta_result(
    job_key: str,
    *,
    meta_title: str,
    meta_description: str,
    status: str,
    attempts: int,
    validation_errors: Sequence[str] = (),
    error_message: str = "",
) -> None:
    opening = opening_signature(meta_description, 6)
    short_opening = opening_signature(meta_description, 3)
    normalized_hash = hashlib.sha256(normalize_for_compare(meta_description).encode("utf-8")).hexdigest()
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE meta_jobs SET
                meta_title=?, meta_description=?, status=?, attempts=?,
                opening_signature=?, short_opening_signature=?, normalized_hash=?,
                validation_errors=?, error_message=?, updated_at=?
            WHERE job_key=?
            """,
            (
                meta_title,
                meta_description,
                status,
                attempts,
                opening,
                short_opening,
                normalized_hash,
                json.dumps(list(validation_errors), ensure_ascii=False),
                error_message,
                utcnow_iso(),
                job_key,
            ),
        )


def set_meta_job_error(job_key: str, error_message: str, attempts: int = 0) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE meta_jobs SET status='failed', attempts=?, error_message=?, updated_at=? WHERE job_key=?",
            (attempts, error_message[:2000], utcnow_iso(), job_key),
        )


def get_meta_job(job_key: str) -> Optional[Dict]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM meta_jobs WHERE job_key=?", (job_key,)).fetchone()
    return dict(row) if row else None


def list_meta_jobs(
    statuses: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    order_by: str = "updated_at DESC",
    run_id: Optional[str] = None,
) -> List[Dict]:
    allowed_order = {
        "updated_at DESC",
        "created_at ASC",
        "sku ASC",
        "status ASC, sku ASC",
    }
    if order_by not in allowed_order:
        order_by = "updated_at DESC"

    sql = "SELECT * FROM meta_jobs"
    params: List[object] = []
    conditions: List[str] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if run_id:
        conditions.append("run_id=?")
        params.append(run_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with db_connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def iter_meta_job_chunks(
    *,
    statuses: Sequence[str],
    run_id: Optional[str],
    chunk_size: int,
) -> Iterator[List[Dict]]:
    """Strumieniuje rekordy z SQLite bez ładowania 50k opisów naraz do RAM."""
    if not statuses:
        return
    placeholders = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM meta_jobs WHERE status IN ({placeholders})"
    params: List[object] = list(statuses)
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    sql += " ORDER BY created_at ASC"
    with db_connect() as conn:
        cursor = conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(max(1, int(chunk_size)))
            if not rows:
                break
            yield [dict(row) for row in rows]


def recommended_batch_shard_size(product_count: int) -> int:
    """Dobiera shard pod szybkie duże runy, zostawiając zapas do limitu 100 jobs."""
    n = max(0, int(product_count))
    if n <= 1200:
        return max(100, n or BATCH_PRODUCTS_PER_FILE)
    if n <= 5000:
        return 750
    return 1000


def active_batch_job_count(run_id: Optional[str] = None) -> int:
    terminal = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    count = 0
    for row in list_batch_jobs(run_id=run_id):
        if row.get("ingested_at"):
            continue
        if str(row.get("state", "")) not in terminal:
            count += 1
    return count


def estimate_batch_input_tokens(run_id: Optional[str], queued_count: int, sample_size: int = 24) -> Dict[str, float]:
    """Lekki estymator planu. Nie wywołuje Gemini ani countTokens."""
    sample = list_meta_jobs(
        statuses=["queued"], limit=max(1, int(sample_size)), order_by="created_at ASC", run_id=run_id
    )
    if not sample or queued_count <= 0:
        return {"avg_input_tokens": 0.0, "estimated_input_tokens": 0.0}
    token_estimates = []
    for job in sample:
        chars = len(META_SYSTEM_PROMPT) + len(build_meta_prompt(job, int(job.get("attempts", 0)), ()))
        # Dla PL/EN w tym zastosowaniu 3.6 znaku/token jest bezpieczniejszym
        # przybliżeniem niż klasyczne 4 znaki/token.
        token_estimates.append(chars / 3.6)
    avg = sum(token_estimates) / len(token_estimates)
    return {"avg_input_tokens": avg, "estimated_input_tokens": avg * int(queued_count)}


def meta_status_counts(run_id: Optional[str] = None) -> Dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM meta_jobs"
    params: List[object] = []
    if run_id:
        sql += " WHERE run_id=?"
        params.append(run_id)
    sql += " GROUP BY status"
    with db_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def list_meta_runs(limit: int = 30) -> List[Dict]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, source_type, COUNT(*) AS product_count,
                   MIN(created_at) AS created_at, MAX(updated_at) AS updated_at
            FROM meta_jobs
            WHERE run_id<>''
            GROUP BY run_id, source_type
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def new_run_id(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = hashlib.sha1(f"{time.time_ns()}|{prefix}".encode("utf-8")).hexdigest()[:6]
    return f"{prefix}-{timestamp}-{suffix}"


def existing_opening_signatures(exclude_job_key: str = "") -> Tuple[Set[str], Counter]:
    sql = (
        "SELECT opening_signature, short_opening_signature FROM meta_jobs "
        "WHERE status='completed' AND opening_signature<>''"
    )
    params: List[object] = []
    if exclude_job_key:
        sql += " AND job_key<>?"
        params.append(exclude_job_key)
    with db_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    long_signatures = {row["opening_signature"] for row in rows if row["opening_signature"]}
    short_counts = Counter(
        row["short_opening_signature"] for row in rows if row["short_opening_signature"]
    )
    return long_signatures, short_counts


def recent_opening_examples(limit: int = 20) -> List[str]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT meta_description FROM meta_jobs
            WHERE status='completed' AND meta_description<>''
            ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [first_words(row["meta_description"], 8) for row in rows]


def requeue_jobs(statuses: Sequence[str], reason: str = "", run_id: Optional[str] = None) -> int:
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    conditions = [f"status IN ({placeholders})"]
    params: List[object] = [utcnow_iso(), reason, *statuses]
    if run_id:
        conditions.append("run_id=?")
        params.append(run_id)
    with db_connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE meta_jobs
            SET status='queued', batch_job_name='', updated_at=?, error_message=?
            WHERE {' AND '.join(conditions)}
            """,
            params,
        )
        return int(cur.rowcount)


# ═══════════════════════════════════════════════════════════════════
# FUNKCJE TEKSTOWE, SEED I DYWERSYFIKACJA
# ═══════════════════════════════════════════════════════════════════

def safe_string_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def strip_code_fences(text: str) -> str:
    if not text:
        return ""
    match = re.match(r"^\s*```(?:json|html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if match:
        return match.group(1).strip()
    text = re.sub(r"^\s*```(?:json|html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def strip_html(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", value or "", flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_spaces(html.unescape(text))


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_for_compare(value: str) -> str:
    text = strip_html(value).lower().translate(_POLISH_CHARS)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return normalize_spaces(text)


def first_words(value: str, count: int) -> str:
    words = normalize_spaces(strip_html(value)).split()
    return " ".join(words[:count])


def opening_signature(value: str, count: int = 6) -> str:
    words = normalize_for_compare(value).split()
    return " ".join(words[:count])


def smart_truncate(value: str, max_chars: int) -> str:
    text = normalize_spaces(value)
    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened


def first_meaningful_sentence(description: str, max_chars: int = 240) -> str:
    text = strip_html(description)
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        sentence = normalize_spaces(sentence)
        if len(sentence) >= 35:
            return smart_truncate(sentence, max_chars)
    return smart_truncate(text, max_chars)


def extract_semantic_cues(description: str, title: str = "", limit: int = 8) -> List[str]:
    text = normalize_for_compare(f"{title} {strip_html(description)}")
    tokens = re.findall(r"[a-z0-9]{4,}", text)
    filtered = [token for token in tokens if token not in POLISH_STOPWORDS and not token.isdigit()]
    counts = Counter(filtered)
    first_position: Dict[str, int] = {}
    for index, token in enumerate(filtered):
        first_position.setdefault(token, index)
    ranked = sorted(counts, key=lambda token: (-counts[token], first_position[token], token))
    return ranked[:limit]


def stable_seed(*parts: str, salt: str = PROMPT_VERSION) -> int:
    payload = "\u241f".join([salt, *[part or "" for part in parts]])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2_147_483_647 or 1


def product_input_hash(
    sku: str,
    title: str,
    author: str,
    description: str,
    channel: str,
    locale: str,
) -> str:
    payload = "\u241e".join(
        [sku, title, author, strip_html(description), channel, locale, PROMPT_VERSION, GEMINI_MODEL]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_style_plan(sku: str, title: str, author: str, description: str, attempt: int = 0) -> Dict:
    seed = stable_seed(sku, title, author, strip_html(description), str(attempt))
    opening_mode = OPENING_MODES[seed % len(OPENING_MODES)]
    rhythm_mode = RHYTHM_MODES[(seed // len(OPENING_MODES)) % len(RHYTHM_MODES)]
    focus_mode = FOCUS_MODES[(seed // (len(OPENING_MODES) * len(RHYTHM_MODES))) % len(FOCUS_MODES)]
    cues = extract_semantic_cues(description, title)
    return {
        "seed": seed,
        "opening_mode": opening_mode,
        "rhythm_mode": rhythm_mode,
        "focus_mode": focus_mode,
        "semantic_cues": cues,
        "source_lead": first_meaningful_sentence(description),
    }


def generate_product_url(title: str) -> str:
    slug = title.lower().translate(_POLISH_CHARS)
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return f"https://bookland.com.pl/{slug}"


def clean_ai_fingerprints(text: str) -> str:
    text = (text or "").replace("—", "-").replace("–", "-")
    return re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)


def validate_description_quality(description: str) -> Tuple[str, str]:
    length = len(strip_html(description))
    if length == 0:
        return "error", "Brak oryginalnego opisu w Akeneo"
    if length < 100:
        return "error", f"Opis bardzo krótki ({length} zn.)"
    if length < 300:
        return "warning", f"Opis krótki ({length} zn.)"
    return "ok", "Opis OK"


def build_meta_title(title: str, author: str, max_chars: int = META_TITLE_HARD_MAX) -> str:
    """Awaryjny meta title używany wyłącznie, gdy Gemini nie zwróci wyniku.

    Główny workflow od v4.2.0 generuje meta title przez AI. Fallback nie powinien
    być traktowany jako wynik docelowej optymalizacji.
    """
    title = normalize_spaces(strip_html(title)).strip('"„”')
    author = normalize_spaces(strip_html(author)).strip('"„”')
    if not title:
        return ""

    candidates: List[str] = []
    if author and normalize_for_compare(author) not in {"pracazbiorowa", "praca zbiorowa"}:
        candidates.append(f"{title} - {author}")
    candidates.append(title)

    main_title = re.split(r"\s[:|]\s|:\s|\s[-–—]\s", title, maxsplit=1)[0].strip()
    if main_title and main_title != title:
        candidates.append(main_title)

    for candidate in candidates:
        candidate = normalize_spaces(candidate)
        if len(candidate) <= max_chars:
            return candidate
    return smart_truncate(main_title or title, max_chars)


def clean_source_title_for_prompt(value: str) -> str:
    """Usuwa oczywiste śmieci katalogowe, pozostawiając oficjalną nazwę produktu."""
    cleaned = normalize_spaces(strip_html(value)).strip('"„”')
    for pattern in TITLE_INTERNAL_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpraca\s*zbiorowa\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[|,;:-]\s*$", "", cleaned)
    return normalize_spaces(cleaned)


def clean_author_for_prompt(value: str, description: str = "") -> Tuple[str, str]:
    """Zwraca (autor, ostrzeżenie) bez przepychania błędnych danych do promptu."""
    author = normalize_spaces(strip_html(value)).strip('"„”')
    normalized = normalize_for_compare(author)
    if normalized in {"", "pracazbiorowa", "praca zbiorowa", "various authors", "brak", "none"}:
        return "", "Brak wiarygodnego autora - nie dodawaj autora do meta title."
    if " " not in author and len(author) >= 14 and re.fullmatch(r"[A-Za-zÀ-ž]+", author):
        if normalize_for_compare(author) not in normalize_for_compare(strip_html(description)):
            return "", (
                f"Pole autora wygląda na sklejone lub techniczne ({author}). "
                "Nie używaj go, chyba że poprawna forma imienia i nazwiska występuje w opisie źródłowym."
            )
    return author, "Autor może być użyty wyłącznie wtedy, gdy zwiększa trafność i mieści się bez skracania istoty produktu."




def title_signal_summary(signals: Dict[str, List[str]]) -> str:
    labels = [
        ("Tożsamość / seria", signals.get("identity_tokens", [])),
        ("Poziom / egzamin", signals.get("levels", [])),
        ("Komponent", signals.get("components", [])),
        ("Format", signals.get("formats", [])),
        ("Platforma", signals.get("platforms", [])),
        ("Odbiorca", signals.get("audience", [])),
        ("Dostęp", signals.get("access", [])),
    ]
    rows = [f"- {label}: {', '.join(values)}" for label, values in labels if values]
    return "\n".join(rows) if rows else "- Brak automatycznie wykrytych sygnałów; oprzyj się na nazwie i opisie."


def existing_meta_title_owners(exclude_job_key: Optional[str] = None) -> Dict[str, str]:
    query = "SELECT job_key, sku, meta_title FROM meta_jobs WHERE status='completed' AND meta_title<>''"
    params: List[str] = []
    if exclude_job_key:
        query += " AND job_key<>?"
        params.append(exclude_job_key)
    owners: Dict[str, str] = {}
    with db_connect() as conn:
        for row in conn.execute(query, params).fetchall():
            key = normalize_for_compare(row["meta_title"])
            if key:
                owners.setdefault(key, str(row["sku"]))
    return owners




# ═══════════════════════════════════════════════════════════════════
# WALIDACJA METATAGÓW I OCHRONA PRZED POWTARZALNYM SCHEMATEM
# ═══════════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════════
# PROMPTY
# ═══════════════════════════════════════════════════════════════════

META_SYSTEM_PROMPT = """Jesteś seniorem SEO e-commerce i redaktorem informacji produktowej Bookland.
Tworzysz jednocześnie meta_title i meta_description dla konkretnego SKU.

CEL META TITLE
Meta title ma identyfikować dokładnie ten wariant produktu i odpowiadać na intencję użytkownika:
„Co to dokładnie jest, z jakiej serii, na jakim poziomie, w jakim formacie i dla kogo?”.
Nie przepisuj mechanicznie nazwy magazynowej. Zredaguj ją jak specjalista SEO, ale bez dopowiadania danych.

TWARDE ZASADY META TITLE
- Preferowana długość: 58-72 znaki ze spacjami. Twarde maksimum: 75. Ważniejsza jest kompletna identyfikacja wariantu niż sztuczne skracanie title.
- Nie dodawaj nazwy sklepu Bookland.
- Nie stosuj CTA, języka reklamowego, superlatywów ani zdań opisowych.
- Nie kończ urwanym słowem, przyimkiem, spójnikiem, separatorem ani wielokropkiem.
- Nie obcinaj słowa i nie twórz fragmentów typu „Online Acces”, „with”, „and”, „Kod”.
- Użyj maksymalnie dwóch logicznych separatorów: preferuj „–” i opcjonalnie „|”.
- Nie używaj cudzysłowu wokół całego meta title.
- Usuń oznaczenia wewnętrzne i śmieci katalogowe: „zz”, „OOP”, statusy dostępności, ISBN/EAN, „praca zbiorowa”.
- Popraw oczywistą literówkę tylko wtedy, gdy korekta jest jednoznaczna, np. „Acces Code” → „Access Code”.
- Nie zmieniaj oficjalnej nazwy serii, marki, platformy, poziomu ani numeru części.
- Każdy wariant produktu musi dostać odróżniający meta title. Nie wolno zgubić informacji typu: uczeń/nauczyciel, eBook/eText, kod/dostęp, z kluczem/bez klucza, część lub poziom.

PRIORYTETY INFORMACJI W META TITLE
1. Oficjalna nazwa serii, tytuł lub marka produktu.
2. Poziom, tom, część, klasa albo egzamin, jeśli występuje.
3. Typ komponentu lub format, który odróżnia SKU.
4. Odbiorca albo rodzaj dostępu: uczeń, nauczyciel, kod, dostęp online.
5. Autor - przede wszystkim dla zwykłych książek. Dodaj go tylko, gdy zwiększa trafność i mieści się bez utraty ważniejszego wyróżnika.

MATERIAŁY DO NAUKI JĘZYKÓW I DŁUGIE ANGIELSKIE NAZWY
- Zachowuj dokładnie nazwę serii i poziom, np. Roadmap B2+, Big English 3, Formula B2 First.
- Zachowuj nazwy platform i formatów: MyEnglishLab, eBook, eText.
- Ogólne komponenty możesz naturalnie skrócić lub przetłumaczyć:
  Student's Book → podręcznik ucznia; Workbook/Activity Book → zeszyt ćwiczeń;
  Coursebook → podręcznik; Teacher's Book → książka nauczyciela;
  Student Online Access Code → kod uczniowski;
  Teacher's Online Access Code → kod nauczyciela;
  Online Practice → ćwiczenia online.
- Nie tłumacz nazw własnych serii, platform, egzaminów ani marek.
- Nie poświęcaj poziomu, odbiorcy ani rodzaju licencji tylko po to, by dodać autora.
- Dla kodów i dostępów cyfrowych autor zwykle ma mniejszą wartość SEO niż format, platforma i odbiorca.

ZWYKŁE KSIĄŻKI POLSKIE I OBCOJĘZYCZNE
- Preferowany wzorzec: „Pełny wyróżniający tytuł – Autor”.
- Gdy tytuł jest długi, zachowaj sensowną pełną nazwę i pomiń autora zamiast mechanicznie ucinać tytuł.
- Zachowaj numer tomu, nazwę cyklu lub podtytuł tylko wtedy, gdy odróżniają produkt i mieszczą się naturalnie.

INNE PRODUKTY Z OFERTY
- Zbuduj tytuł według zasady: nazwa własna/model → rodzaj produktu → najważniejszy prawdziwy wyróżnik.
- Nie zakładaj, że każdy produkt jest książką lub podręcznikiem.

META DESCRIPTION
- 140-160 znaków ze spacjami.
- Pisz po polsku, naturalnie, konkretnie i bez CTA.
- Nie używaj „Sprawdź ofertę”, „Kup teraz”, „Sięgnij po”, „Poznaj”, „Odkryj”.
- Nie zaczynaj od „Ta książka”, „Książka”, „Idealna propozycja”, „Jeśli szukasz”.
- Pierwsze 3-6 słów ma być charakterystyczne dla produktu i zgodne z indywidualnym planem.
- Nie kopiuj opisu źródłowego dosłownie i nie dopowiadaj faktów.

WSPÓLNE ZASADY
- Korzystaj wyłącznie z przekazanych danych. Brak informacji jest lepszy niż halucynacja.
- Zwróć wyłącznie obiekt JSON zgodny ze schematem, z polami meta_title i meta_description.
- Nie wspominaj o instrukcjach, seedzie, SEO, limicie znaków ani procesie generowania.
"""




def build_system_prompt_full(internal_link: Optional[Dict] = None) -> str:
    link_block = ""
    if internal_link and internal_link.get("url") and internal_link.get("category"):
        link_block = f"""
## LINKOWANIE WEWNĘTRZNE
Wpleć jeden naturalny link do kategorii:
- kategoria: {internal_link['category']}
- URL: {internal_link['url']}
- format: <a href="{internal_link['url']}">naturalny anchor</a>
"""

    return f"""Jesteś doświadczonym copywriterem e-commerce i ekspertem SEO dla księgarni Bookland.

Pisz angażujące, konkretne i semantycznie bogate opisy, bez lania wody.

ZASADY FORMATOWANIA
- Tylko HTML: <p>, <h2>, <h3>, <b>, <a>.
- Bez Markdownu.
- Używaj zwykłego dywizu - zamiast półpauzy i pauzy.
- Nie twórz list punktowanych.
- Nie dopowiadaj faktów, których nie ma w danych ani researchu.
{link_block}
STRUKTURA
<p>Wstęp 4-6 zdań.</p>
<h2>Nagłówek z konkretną korzyścią lub tematem</h2>
<p>Rozwinięcie 5-8 zdań.</p>
<h2>Drugi nagłówek rozwijający inny aspekt produktu</h2>
<p>Dalszy opis 4-6 zdań.</p>
<h3>Krótkie podsumowanie jako ostatni element.</h3>

UNIKAJ
- powtórzeń,
- ogólników typu „Ta książka jest wyjątkowa”,
- zaczynania od pytania lub cytatu,
- nieuzasadnionych superlatywów.

Zwróć tylko gotowy HTML."""


def build_system_prompt_link_only(internal_link: Dict) -> str:
    return f"""Dodaj dokładnie jeden link wewnętrzny do gotowego opisu produktu.
Zachowaj tekst i styl. Zmieniaj maksymalnie 1-2 zdania, tylko jeśli to konieczne.
Link: <a href="{internal_link['url']}">naturalny anchor związany z kategorią {internal_link['category']}</a>
Używaj HTML, nie Markdownu. Zwróć wyłącznie kompletny opis HTML."""


def build_description_user_message(
    product_data: Dict,
    internal_link: Optional[Dict] = None,
    research: Optional[str] = None,
) -> str:
    parts = [
        f"TYTUŁ PRODUKTU: {product_data.get('title', '')}",
        f"AUTOR/MARKA: {product_data.get('author', '')}",
        f"DANE TECHNICZNE: {product_data.get('details', '')}",
        f"ORYGINALNY OPIS: {product_data.get('description', '')}",
    ]
    if research:
        parts.append(f"RESEARCH: {research}")
    if internal_link:
        parts.append(f"LINK: {internal_link['url']} | KATEGORIA: {internal_link['category']}")
    parts.append("Zwróć tylko kod HTML opisu.")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# KLIENCI API
# ═══════════════════════════════════════════════════════════════════

# Klienci są per-thread. W v4.5.0 jeden współdzielony klient HTTP/Gemini był
# używany przez kilka workerów równocześnie. Przy długich przebiegach mogło to
# prowadzić do zawieszonego połączenia, które blokowało cały chunk w as_completed().
_thread_local = threading.local()


def get_gemini_client() -> genai.Client:
    client = getattr(_thread_local, "gemini_client", None)
    if client is None:
        client = genai.Client(
            api_key=GOOGLE_API_KEY,
            # google-genai interpretuje timeout w milisekundach. Dzięki temu
            # pojedynczy zawieszony request nie może zatrzymać całej kolejki bez końca.
            http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS},
        )
        _thread_local.gemini_client = client
    return client


def get_http_session() -> requests.Session:
    session = getattr(_thread_local, "http_session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": f"BooklandSEOGenerator/{APP_VERSION}"})
        _thread_local.http_session = session
    return session


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = AKENEO_MAX_ATTEMPTS,
    timeout: int = AKENEO_TIMEOUT,
    **kwargs,
) -> requests.Response:
    session = get_http_session()
    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(float(retry_after), 60.0)
                except ValueError:
                    wait = min(2 ** attempt + (attempt * 0.3), 60.0)
                time.sleep(max(wait, 1.0))
                continue
            if response.status_code >= 500:
                time.sleep(min(2 ** attempt + 0.5, 30.0))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(2 ** attempt + 0.5, 30.0))
    if last_error:
        raise last_error
    raise RuntimeError(f"Brak poprawnej odpowiedzi z {url}")


# ═══════════════════════════════════════════════════════════════════
# PERPLEXITY
# ═══════════════════════════════════════════════════════════════════

PERPLEXITY_SYSTEM_PROMPT = """Badaj książki i autorów. Odpowiadaj po polsku.
Podawaj tylko konkretne, możliwe do zweryfikowania fakty. Nie generalizuj."""


def research_book_with_perplexity(title: str, author: str) -> Optional[str]:
    api_key = str(st.secrets.get("PERPLEXITY_API_KEY", ""))
    if not api_key:
        return None

    query = (
        f"Podaj kluczowe informacje o książce „{title}”"
        + (f" autorstwa {author}" if author else "")
        + ". Uwzględnij temat, gatunek, wyróżniki i odbiorcę. Maksymalnie 250 słów."
    )
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {"role": "system", "content": PERPLEXITY_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        "max_tokens": 500,
        "return_citations": False,
        "return_images": False,
    }
    try:
        response = request_with_retry(
            "POST",
            PERPLEXITY_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=PERPLEXITY_TIMEOUT,
            max_attempts=3,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# GEMINI: OPISY ORAZ AI META TITLE + META DESCRIPTION
# ═══════════════════════════════════════════════════════════════════

def generate_description(
    product_data: Dict,
    internal_link: Optional[Dict] = None,
    link_only: bool = False,
    research: Optional[str] = None,
) -> str:
    link_only = bool(link_only and internal_link)
    system_prompt = (
        build_system_prompt_link_only(internal_link)
        if link_only and internal_link
        else build_system_prompt_full(internal_link)
    )
    user_message = build_description_user_message(product_data, internal_link, research)
    last_errors: List[str] = []
    for attempt in range(2):
        try:
            response = get_gemini_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=user_message + (
                    "\nPoprzedni wynik był niepoprawny: " + "; ".join(last_errors) +
                    ". Wygeneruj cały opis ponownie."
                    if last_errors else ""
                ),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.75,
                    max_output_tokens=2400,
                ),
            )
            description = clean_ai_fingerprints(strip_code_fences(response.text or ""))
            last_errors = validate_description_html(
                description,
                require_full_structure=not link_only,
                required_link=(internal_link or {}).get("url", ""),
            )
            if not last_errors:
                return description
        except Exception as exc:
            last_errors = [str(exc)]
            if attempt == 0:
                continue
    return "BŁĄD GEMINI: niepoprawny opis: " + "; ".join(last_errors)




def normalize_sku_list(values: Iterable[str]) -> Tuple[List[str], int]:
    unique: List[str] = []
    seen: Set[str] = set()
    duplicates = 0
    for raw in values:
        value = str(raw or "").replace("\ufeff", "").strip().strip('"').strip("'")
        if not value or value.lower() in {"sku", "identifier", "product_sku", "kod", "kod produktu"}:
            continue
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        unique.append(value)
    return unique, duplicates


def parse_bulk_sku_payload(payload: str) -> Tuple[List[str], int]:
    text = (payload or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], 0

    lines = [line for line in text.split("\n") if line.strip()]
    sample = "\n".join(lines[:20])
    rows: List[List[str]] = []
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t;,|")
        rows = list(csv.reader(lines, dialect))
    except csv.Error:
        rows = [[line] for line in lines]

    if not rows:
        return [], 0

    header = [normalize_for_compare(cell) for cell in rows[0]]
    sku_headers = {"sku", "identifier", "product sku", "kod", "kod produktu"}
    sku_index = next((i for i, cell in enumerate(header) if cell in sku_headers), 0)
    start_index = 1 if any(cell in sku_headers for cell in header) else 0
    values = [row[sku_index] for row in rows[start_index:] if len(row) > sku_index]
    return normalize_sku_list(values)


def chunks(values: Sequence[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _decode_upload_bytes(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1250", errors="replace")


def _validation_errors_from_cell(value: object) -> List[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "[]"}:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [part.strip() for part in re.split(r"\s*;\s*", text) if part.strip()]


def _bool_from_cell(value: object, default: bool = True) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "tak"}:
        return True
    if text in {"0", "false", "no", "nie"}:
        return False
    return default


def parse_resumable_product_file(raw_bytes: bytes) -> Tuple[List[str], Dict[str, Dict], List[str]]:
    """Czyta zwykły TXT/CSV/TSV z SKU albo checkpoint wygenerowany przez aplikację.

    Wyłącznie kompletne metatagi są traktowane jako checkpoint.
    """
    text = _decode_upload_bytes(raw_bytes)
    errors: List[str] = []
    try:
        df = pd.read_csv(
            io.StringIO(text),
            sep=None,
            engine="python",
            dtype=str,
            keep_default_na=False,
        )
    except Exception:
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    if df.empty or len(df.columns) == 0:
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    normalized_columns = {normalize_for_compare(col): col for col in df.columns}
    sku_column = next(
        (normalized_columns[key] for key in ("sku", "identifier", "product sku", "kod", "kod produktu") if key in normalized_columns),
        None,
    )
    if sku_column is None:
        # Plik jednokolumnowy bez nagłówka często zostaje odczytany z pierwszym SKU jako nazwą kolumny.
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    skus, _ = normalize_sku_list(df[sku_column].tolist())
    seed_results: Dict[str, Dict] = {}

    meta_title_col = normalized_columns.get("meta title") or normalized_columns.get("meta_title")
    meta_description_col = normalized_columns.get("meta description") or normalized_columns.get("meta_description")
    error_col = normalized_columns.get("error")
    validation_col = normalized_columns.get("validation errors") or normalized_columns.get("validation_errors")
    title_col = normalized_columns.get("title")
    url_col = normalized_columns.get("url")
    old_desc_col = normalized_columns.get("old description") or normalized_columns.get("old_description")
    status_col = normalized_columns.get("checkpoint status") or normalized_columns.get("checkpoint_status") or normalized_columns.get("status")

    if meta_title_col and meta_description_col:
        for _, row in df.iterrows():
            sku = str(row.get(sku_column, "") or "").strip()
            if not sku:
                continue
            meta_title = str(row.get(meta_title_col, "") or "").strip()
            meta_description = str(row.get(meta_description_col, "") or "").strip()
            error = str(row.get(error_col, "") or "").strip() if error_col else ""
            status = str(row.get(status_col, "") or "").strip().lower() if status_col else ""
            if not meta_title or not meta_description or error or status in {"pending", "error", "failed"}:
                continue
            seed_results[sku] = {
                "sku": sku,
                "title": str(row.get(title_col, "") or "") if title_col else "",
                "description_html": "",
                "url": str(row.get(url_col, "") or "") if url_col else "",
                "old_description": str(row.get(old_desc_col, "") or "") if old_desc_col else "",
                "research": None,
                "meta_title": meta_title,
                "meta_description": meta_description,
                "error": None,
                "validation_errors": _validation_errors_from_cell(row.get(validation_col, "")) if validation_col else [],
                "meta_only": True,
                "checkpoint_source": "uploaded_csv",
            }

    return skus, seed_results, errors


def interactive_checkpoint_key(
    skus: Sequence[str],
    *,
    channel: str,
    locale: str,
    store_view_code: str,
) -> str:
    payload = {
        "skus": sorted(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip())),
        "channel": channel,
        "locale": locale,
        "store_view_code": store_view_code,
        "prompt_version": PROMPT_VERSION,
        "model": GEMINI_MODEL,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"interactive-{digest}"


def interactive_checkpoint_path(checkpoint_key: str) -> Path:
    INTERACTIVE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return INTERACTIVE_CHECKPOINT_DIR / f"{checkpoint_key}.csv"


def _checkpoint_row(sku: str, result: Optional[Dict]) -> Dict:
    if not result:
        return {
            "sku": sku,
            "checkpoint_status": "pending",
            "title": "",
            "meta_title": "",
            "meta_description": "",
            "validation_errors": "[]",
            "error": "",
            "meta_only": True,
            "description_html": "",
            "url": "",
            "old_description": "",
        }
    validation_errors = result.get("validation_errors") or []
    if isinstance(validation_errors, str):
        validation_errors = _validation_errors_from_cell(validation_errors)
    error = str(result.get("error") or "").strip()
    status = "error" if error else ("warning" if validation_errors else "completed")
    return {
        "sku": sku,
        "checkpoint_status": status,
        "title": result.get("title", ""),
        "meta_title": result.get("meta_title", ""),
        "meta_description": result.get("meta_description", ""),
        "validation_errors": json.dumps(list(validation_errors), ensure_ascii=False),
        "error": error,
        "meta_only": True,
        "description_html": "",
        "url": result.get("url", ""),
        "old_description": result.get("old_description", ""),
    }


def write_interactive_checkpoint(path: Path, skus: Sequence[str], results_by_sku: Dict[str, Dict]) -> None:
    """Atomowy checkpoint. Zerwany zapis nie uszkodzi poprzedniej wersji pliku."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_checkpoint_row(sku, results_by_sku.get(sku)) for sku in skus]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    pd.DataFrame(rows).to_csv(tmp_path, index=False, encoding="utf-8-sig")
    tmp_path.replace(path)


def load_interactive_checkpoint(path: Path) -> Dict[str, Dict]:
    if not path.exists():
        return {}
    try:
        _, results, _ = parse_resumable_product_file(path.read_bytes())
        for result in results.values():
            result["checkpoint_source"] = "server_csv"
        return results
    except Exception:
        return {}


def cached_meta_results_for_skus(
    skus: Sequence[str],
    *,
    channel: str,
    locale: str,
    include_warnings: bool = True,
) -> Dict[str, Dict]:
    """Odzyskuje gotowe metatagi z SQLite bez ponownego wywołania Gemini."""
    wanted = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))
    if not wanted:
        return {}
    allowed_statuses = {"completed", "validation_failed"} if include_warnings else {"completed"}
    out: Dict[str, Dict] = {}
    with db_connect() as conn:
        for sku_chunk in chunks(wanted, 400):
            placeholders = ",".join("?" for _ in sku_chunk)
            rows = conn.execute(
                f"""
                SELECT * FROM meta_jobs
                WHERE sku IN ({placeholders}) AND channel=? AND locale=?
                  AND prompt_version=? AND model=?
                """,
                [*sku_chunk, channel, locale, PROMPT_VERSION, GEMINI_MODEL],
            ).fetchall()
            for row in rows:
                job = dict(row)
                if job.get("status") not in allowed_statuses:
                    continue
                if not job.get("meta_title") or not job.get("meta_description"):
                    continue
                try:
                    validation_errors = json.loads(job.get("validation_errors") or "[]")
                except Exception:
                    validation_errors = []
                out[job["sku"]] = {
                    "sku": job["sku"],
                    "title": job.get("title", ""),
                    "description_html": "",
                    "url": generate_product_url(job.get("title", "")) if job.get("title") else "",
                    "old_description": job.get("description", ""),
                    "research": None,
                    "meta_title": job.get("meta_title", ""),
                    "meta_description": job.get("meta_description", ""),
                    "error": None,
                    "validation_errors": validation_errors,
                    "meta_only": True,
                    "checkpoint_source": "sqlite",
                }
    return out


# ═══════════════════════════════════════════════════════════════════
# AKENEO API
# ═══════════════════════════════════════════════════════════════════

def _akeneo_root() -> str:
    base = str(st.secrets["AKENEO_BASE_URL"]).rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[: -len("/api/rest/v1")]
    return base


@st.cache_data(ttl=3000, show_spinner=False)
def akeneo_get_token() -> str:
    token_url = _akeneo_root() + "/api/oauth/v1/token"
    response = request_with_retry(
        "POST",
        token_url,
        auth=(st.secrets["AKENEO_CLIENT_ID"], st.secrets["AKENEO_SECRET"]),
        data={
            "grant_type": "password",
            "username": st.secrets["AKENEO_USERNAME"],
            "password": st.secrets["AKENEO_PASSWORD"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Błąd autoryzacji Akeneo {response.status_code}: {response.text[:300]}")
    return response.json()["access_token"]


def akeneo_headers(token: str, content_type: str = "") -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def akeneo_get_attribute(code: str, token: str) -> Dict:
    response = request_with_retry(
        "GET",
        _akeneo_root() + f"/api/rest/v1/attributes/{code}",
        headers=akeneo_headers(token),
    )
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_existing_attribute_codes(token: str) -> List[str]:
    """Zwraca tylko kody atrybutów istniejących w danym PIM-ie.

    Dzięki temu parametr `attributes` nie wywoła błędu 422, gdy instalacja używa
    np. `autor` zamiast `author` albo `wydawnictwo` zamiast `publisher`.
    """
    candidates = [
        "name", "description", "author", "autor", "publisher", "wydawnictwo",
        "year", "rok_wydania", "pages", "liczba_stron", "cover_type", "oprawa",
        "ean", "isbn",
    ]
    existing: List[str] = []
    for code in candidates:
        try:
            response = request_with_retry(
                "GET",
                _akeneo_root() + f"/api/rest/v1/attributes/{code}",
                headers=akeneo_headers(token),
                max_attempts=2,
            )
            if response.status_code == 200:
                existing.append(code)
        except Exception:
            continue
    return existing


def akeneo_product_exists(sku: str, token: str) -> bool:
    response = request_with_retry(
        "GET",
        _akeneo_root() + f"/api/rest/v1/products/{sku}",
        headers=akeneo_headers(token),
        max_attempts=3,
    )
    return response.status_code == 200


def _value_from_values(
    values: Dict,
    names: Sequence[str],
    channel: str,
    locale: str,
    join_lists: bool = False,
) -> str:
    for name in names:
        entries = values.get(name) or []
        for entry in entries:
            scope_ok = entry.get("scope") in (None, channel)
            locale_ok = entry.get("locale") in (None, locale)
            if scope_ok and locale_ok:
                data = entry.get("data", "")
                if isinstance(data, list):
                    return ", ".join(str(item).strip() for item in data if str(item).strip())
                return safe_string_value(data)
        if entries:
            data = entries[0].get("data", "")
            if isinstance(data, list):
                return ", ".join(str(item).strip() for item in data if str(item).strip())
            return safe_string_value(data)
    return ""


def parse_akeneo_product(item: Dict, channel: str, locale: str) -> Dict:
    values = item.get("values", {})
    publisher = _value_from_values(values, ["publisher", "wydawnictwo"], channel, locale)
    year = _value_from_values(values, ["year", "rok_wydania"], channel, locale)
    pages = _value_from_values(values, ["pages", "liczba_stron"], channel, locale)
    cover = _value_from_values(values, ["cover_type", "oprawa"], channel, locale)
    details = ", ".join(
        f"{label}: {value}"
        for label, value in (("Wydawnictwo", publisher), ("Rok", year), ("Strony", pages), ("Oprawa", cover))
        if value
    )
    return {
        "identifier": item.get("identifier", ""),
        "title": _value_from_values(values, ["name"], channel, locale) or item.get("identifier", ""),
        "description": _value_from_values(values, ["description"], channel, locale),
        "author": _value_from_values(values, ["author", "autor"], channel, locale, join_lists=True),
        "publisher": publisher,
        "year": year,
        "pages": pages,
        "cover_type": cover,
        "ean": _value_from_values(values, ["ean"], channel, locale),
        "isbn": _value_from_values(values, ["isbn"], channel, locale),
        "details": details,
        "updated": item.get("updated", ""),
        "enabled": bool(item.get("enabled", False)),
        "categories": item.get("categories", []),
    }






def akeneo_iter_products_search_after(
    token: str,
    channel: str,
    locale: str,
    *,
    category: Optional[str] = None,
    enabled_only: bool = True,
    only_with_description: bool = True,
    max_products: Optional[int] = None,
) -> Iterator[Dict]:
    url = _akeneo_root() + "/api/rest/v1/products"
    search: Dict[str, List[Dict]] = {}
    if enabled_only:
        search["enabled"] = [{"operator": "=", "value": True}]
    if category:
        search["categories"] = [{"operator": "IN", "value": [category]}]
    if only_with_description:
        search["description"] = [{"operator": "NOT EMPTY", "scope": channel, "locale": locale}]

    params = {
        "pagination_type": "search_after",
        "limit": 100,
        "scope": channel,
        "locales": locale,
        "with_count": "false",
    }
    existing_attributes = akeneo_existing_attribute_codes(token)
    if existing_attributes:
        params["attributes"] = ",".join(existing_attributes)
    if search:
        params["search"] = json.dumps(search, ensure_ascii=False)

    yielded = 0
    next_url: Optional[str] = url
    next_params: Optional[Dict] = params
    while next_url:
        response = request_with_retry(
            "GET",
            next_url,
            headers=akeneo_headers(token),
            params=next_params,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("_embedded", {}).get("items", [])
        for item in items:
            parsed = parse_akeneo_product(item, channel, locale)
            if only_with_description and not strip_html(parsed.get("description", "")):
                continue
            yield parsed
            yielded += 1
            if max_products and yielded >= max_products:
                return
        next_href = payload.get("_links", {}).get("next", {}).get("href")
        next_url = urljoin(_akeneo_root(), next_href) if next_href else None
        next_params = None


def akeneo_search_products(
    search_query: str,
    token: str,
    limit: int = 20,
    locale: str = DEFAULT_LOCALE,
) -> List[Dict]:
    url = _akeneo_root() + "/api/rest/v1/products"
    products: Dict[str, Dict] = {}
    searches = [
        {"identifier": [{"operator": "CONTAINS", "value": search_query}]},
        {"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]},
    ]
    for search_filter in searches:
        response = request_with_retry(
            "GET",
            url,
            headers=akeneo_headers(token),
            params={"limit": limit, "search": json.dumps(search_filter)},
            max_attempts=3,
        )
        if response.status_code != 200:
            continue
        for item in response.json().get("_embedded", {}).get("items", []):
            parsed = parse_akeneo_product(item, DEFAULT_CHANNEL, locale)
            sku = parsed["identifier"]
            if sku:
                products[sku] = {
                    "identifier": sku,
                    "title": parsed["title"],
                    "family": item.get("family", ""),
                    "enabled": item.get("enabled", False),
                }
    return list(products.values())[:limit]


@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_fetch_categories(token: str, locale: str) -> List[Dict]:
    url = _akeneo_root() + "/api/rest/v1/categories"
    categories: List[Dict] = []
    params: Optional[Dict] = {"limit": 100}
    next_url: Optional[str] = url
    while next_url:
        response = request_with_retry("GET", next_url, headers=akeneo_headers(token), params=params)
        if response.status_code != 200:
            break
        payload = response.json()
        for item in payload.get("_embedded", {}).get("items", []):
            labels = item.get("labels", {})
            categories.append(
                {
                    "code": item.get("code", ""),
                    "label": labels.get(locale) or labels.get("pl_PL") or item.get("code", ""),
                    "parent": item.get("parent"),
                }
            )
        next_href = payload.get("_links", {}).get("next", {}).get("href")
        next_url = urljoin(_akeneo_root(), next_href) if next_href else None
        params = None
    return sorted(categories, key=lambda item: item["label"])


def akeneo_fetch_backlog(
    token: str,
    channel: str,
    locale: str,
    limit: int = 100,
    category: Optional[str] = None,
    exclude_updated_days: Optional[int] = None,
    only_without_desc: bool = False,
    max_desc_len: Optional[int] = None,
) -> List[Dict]:
    products: List[Dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=exclude_updated_days or 0)
    for product in akeneo_iter_products_search_after(
        token,
        channel,
        locale,
        category=category,
        enabled_only=True,
        only_with_description=False,
        max_products=max(limit * 4, limit),
    ):
        description = strip_html(product.get("description", ""))
        if only_without_desc and description:
            continue
        if not only_without_desc and max_desc_len is not None and len(description) >= max_desc_len:
            continue
        if exclude_updated_days and product.get("updated"):
            try:
                updated = datetime.fromisoformat(product["updated"].replace("Z", "+00:00"))
                if updated >= cutoff:
                    continue
            except ValueError:
                pass
        products.append(
            {
                "identifier": product["identifier"],
                "title": product["title"],
                "desc_len": len(description),
                "updated": product.get("updated", "")[:10],
            }
        )
        if len(products) >= limit:
            break
    products.sort(key=lambda item: item["desc_len"])
    return products[:limit]


def akeneo_update_description(
    sku: str,
    html_description: str,
    channel: str,
    locale: str = DEFAULT_LOCALE,
) -> bool:
    token = akeneo_get_token()
    attr_desc = akeneo_get_attribute("description", token)
    payload = {
        "values": {
            "description": [
                {
                    "data": html_description,
                    "scope": channel if attr_desc.get("scopable") else None,
                    "locale": locale if attr_desc.get("localizable") else None,
                }
            ]
        }
    }
    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        payload["values"]["opisy_seo"] = [
            {
                "data": True,
                "scope": channel if attr_seo.get("scopable") else None,
                "locale": locale if attr_seo.get("localizable") else None,
            }
        ]
    except Exception:
        pass

    response = request_with_retry(
        "PATCH",
        _akeneo_root() + f"/api/rest/v1/products/{sku}",
        headers=akeneo_headers(token, "application/json"),
        data=json.dumps(payload, ensure_ascii=False),
    )
    if response.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd Akeneo {response.status_code}: {response.text[:300]}")


# ═══════════════════════════════════════════════════════════════════
# PRZETWARZANIE POJEDYNCZYCH PRODUKTÓW
# ═══════════════════════════════════════════════════════════════════

def _prepare_product_data(product_details: Dict) -> Dict:
    return {
        "title": safe_string_value(product_details.get("title")),
        "author": safe_string_value(product_details.get("author")),
        "details": safe_string_value(product_details.get("details")),
        "description": safe_string_value(product_details.get("description")),
    }


def process_product_meta_only(
    sku: str,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
) -> Dict:
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        if not product_details:
            return {"sku": sku, "title": "", "error": "Produkt nie znaleziony", "meta_only": True}

        product_data = _prepare_product_data(product_details)
        quality = validate_description_quality(product_data["description"])
        job_key, _ = upsert_meta_job(
            sku=sku,
            channel=channel,
            locale=locale,
            store_view_code=store_view_code,
            product_data=product_data,
            source_updated=product_details.get("updated", ""),
            force_regenerate=True,
        )
        job = get_meta_job(job_key)
        if not job:
            raise RuntimeError("Nie udało się utworzyć zadania metatagów")
        generated = generate_metatags_interactive(job)
        status = "completed" if not generated["validation_errors"] else "validation_failed"
        save_meta_result(
            job_key,
            meta_title=generated["meta_title"],
            meta_description=generated["meta_description"],
            status=status,
            attempts=generated["attempts"],
            validation_errors=generated["validation_errors"],
            error_message=generated["error"],
        )
        return {
            "sku": sku,
            "title": product_data["title"],
            "description_html": "",
            "url": generate_product_url(product_data["title"]),
            "old_description": product_data["description"],
            "research": None,
            "meta_title": generated["meta_title"],
            "meta_description": generated["meta_description"],
            "error": generated["error"] or None,
            "description_quality": quality,
            "meta_only": True,
            "validation_errors": generated["validation_errors"],
        }
    except Exception as exc:
        return {
            "sku": sku,
            "title": "",
            "error": str(exc),
            "description_quality": ("error", str(exc)),
            "meta_only": True,
        }


def process_product_from_akeneo(
    sku: str,
    token: str,
    channel: str,
    locale: str,
    internal_link: Optional[Dict] = None,
    link_only: bool = False,
    use_research: bool = True,
) -> Dict:
    try:
        link_only = bool(link_only and internal_link)
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        if not product_details:
            return {"sku": sku, "title": "", "error": "Produkt nie znaleziony"}

        product_data = _prepare_product_data(product_details)
        quality = validate_description_quality(product_data["description"])
        research = None
        if use_research and not link_only:
            research = research_book_with_perplexity(product_data["title"], product_data["author"])

        description_html = generate_description(
            product_data,
            internal_link=internal_link,
            link_only=link_only,
            research=research,
        )
        if "BŁĄD GEMINI" in description_html:
            return {
                "sku": sku,
                "title": product_data["title"],
                "description_html": description_html,
                "error": description_html,
                "description_quality": quality,
            }

        return {
            "sku": sku,
            "title": product_data["title"],
            "description_html": description_html,
            "url": generate_product_url(product_data["title"]),
            "old_description": product_data["description"],
            "research": research,
            "meta_title": "",
            "meta_description": "",
            "error": None,
            "description_quality": quality,
            "meta_only": False,
            "validation_errors": [],
        }
    except Exception as exc:
        return {
            "sku": sku,
            "title": "",
            "error": str(exc),
            "description_quality": ("error", str(exc)),
        }


# ═══════════════════════════════════════════════════════════════════
# GEMINI BATCH API DLA DUŻEJ SKALI
# ═══════════════════════════════════════════════════════════════════



def write_batch_jsonl(jobs: Sequence[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for job in jobs:
            attempt = int(job.get("attempts", 0))
            handle.write(json.dumps(batch_request_for_job(job, attempt), ensure_ascii=False) + "\n")


def register_batch_job(
    *,
    job_name: str,
    run_id: str,
    display_name: str,
    input_path: Path,
    input_file_name: str,
    product_count: int,
    state: str,
) -> None:
    now = utcnow_iso()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO batch_jobs(
                job_name, run_id, display_name, model, input_path, input_file_name, state,
                product_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                state=excluded.state,
                input_file_name=excluded.input_file_name,
                updated_at=excluded.updated_at
            """,
            (
                job_name,
                run_id,
                display_name,
                GEMINI_MODEL,
                str(input_path),
                input_file_name,
                state,
                product_count,
                now,
                now,
            ),
        )


def list_batch_jobs(run_id: Optional[str] = None) -> List[Dict]:
    sql = "SELECT * FROM batch_jobs"
    params: List[object] = []
    if run_id:
        sql += " WHERE run_id=?"
        params.append(run_id)
    sql += " ORDER BY created_at DESC"
    with db_connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]




def batch_state_name(batch_job) -> str:
    state = getattr(batch_job, "state", None)
    return getattr(state, "name", None) or str(state or "UNKNOWN")


def extract_batch_response_text(payload: Dict) -> Tuple[str, str]:
    """Zwraca (klucz, tekst JSON) z różnych wariantów odpowiedzi JSONL Gemini."""
    key = str(payload.get("key") or payload.get("metadata", {}).get("key") or "")
    response = payload.get("response") or payload.get("inlineResponse", {}).get("response") or {}
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if response.get("error"):
        raise RuntimeError(str(response["error"]))

    candidates = response.get("candidates") or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                return key, str(part["text"])

    text = response.get("text")
    if text:
        return key, str(text)
    raise RuntimeError("Brak tekstu w odpowiedzi batch")






# ═══════════════════════════════════════════════════════════════════
# IMPORT DUŻEGO KATALOGU I EKSPORT
# ═══════════════════════════════════════════════════════════════════



def import_catalog_to_meta_queue(
    *,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    category: Optional[str],
    enabled_only: bool,
    only_with_description: bool,
    max_products: Optional[int],
    force_regenerate: bool,
    run_id: str = "",
    progress_callback=None,
) -> Dict[str, int]:
    stats = {"seen": 0, "queued": 0, "skipped_unchanged": 0, "without_description": 0}
    for product in akeneo_iter_products_search_after(
        token,
        channel,
        locale,
        category=category,
        enabled_only=enabled_only,
        only_with_description=only_with_description,
        max_products=max_products,
    ):
        stats["seen"] += 1
        if not strip_html(product.get("description", "")):
            stats["without_description"] += 1
            if only_with_description:
                continue
        product_data = _prepare_product_data(product)
        _, queued = upsert_meta_job(
            sku=product["identifier"],
            channel=channel,
            locale=locale,
            store_view_code=store_view_code,
            product_data=product_data,
            source_updated=product.get("updated", ""),
            force_regenerate=force_regenerate,
            run_id=run_id,
            source_type="catalog",
        )
        if queued:
            stats["queued"] += 1
        else:
            stats["skipped_unchanged"] += 1
        if progress_callback and stats["seen"] % 100 == 0:
            progress_callback(stats)
    if progress_callback:
        progress_callback(stats)
    return stats


def export_meta_csv(
    statuses: Sequence[str] = ("completed",),
    run_id: Optional[str] = None,
) -> bytes:
    jobs = list_meta_jobs(statuses=statuses, order_by="sku ASC", run_id=run_id)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["sku", "store_view_code", "meta_title", "meta_description"],
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for job in jobs:
        writer.writerow(
            {
                "sku": job["sku"],
                "store_view_code": job["store_view_code"],
                "meta_title": job["meta_title"],
                "meta_description": job["meta_description"],
            }
        )
    return output.getvalue().encode("utf-8-sig")


def export_quality_report_csv(run_id: Optional[str] = None) -> bytes:
    jobs = list_meta_jobs(order_by="status ASC, sku ASC", run_id=run_id)
    rows = []
    for job in jobs:
        rows.append(
            {
                "sku": job["sku"],
                "status": job["status"],
                "attempts": job["attempts"],
                "meta_title_length": len(job["meta_title"]),
                "meta_description_length": len(job["meta_description"]),
                "opening_signature": job["opening_signature"],
                "opening_mode": job["opening_mode"],
                "validation_errors": job["validation_errors"],
                "error_message": job["error_message"],
            }
        )
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")



# ═══════════════════════════════════════════════════════════════════
# V4.3: KANDYDACI META TITLE + LOKALNY RANKER + BLOKOWANIE POPRAWNYCH PÓL
# ═══════════════════════════════════════════════════════════════════

# W v4.2 model zwracał jeden meta title. Przy długich nazwach kursów często
# wybierał kompromis, który kończył się słowem „Kod”, mieszał języki albo gubił
# wariant uczniowski. V4.3 prosi o cztery kandydatury, a następnie wybiera wynik
# lokalnie na podstawie kompletności, naturalności i unikalności.
META_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "product_type": {
            "type": "string",
            "description": "Krótka klasyfikacja produktu, np. student_access_code, teacher_access_code, ebook, textbook, workbook, dictionary, general_book, other.",
        },
        "canonical_identity": {
            "type": "string",
            "description": "Najkrótsza poprawna nazwa serii, tytułu lub modelu identyfikująca produkt.",
        },
        "meta_title_candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cztery różne, kompletne kandydatury meta title, każda maksymalnie 75 znaków.",
        },
        "meta_description": {
            "type": "string",
            "description": "Unikalny polski meta description produktu, 140-160 znaków, bez CTA i retorycznych pytań.",
        },
    },
    "required": ["product_type", "canonical_identity", "meta_title_candidates", "meta_description"],
}

# Bardziej restrykcyjna lista otwarć. W analizie v4.2 część formalnie poprawnych
# opisów zaczynała się od sztucznych konstrukcji typu „Niezrozumienie...”,
# „Tradycyjna nauka czy...” lub od ukrytego CTA „Rozwiń...”.
BANNED_STARTERS = tuple(dict.fromkeys((*BANNED_STARTERS,
    "rozwiń", "rozwin", "zyskaj", "wybierz", "postaw na", "skorzystaj",
    "naukowe cele", "niezrozumienie", "tradycyjna nauka", "ekscytacja",
    "postępy w", "postepy w", "nowoczesne podejście", "nowoczesne podejscie",
    "czy ", "jak ", "dlaczego ",
)))

V43_PRODUCT_TYPES = (
    "student_access_code",
    "teacher_access_code",
    "digital_course_bundle",
    "ebook",
    "etext",
    "online_practice",
    "textbook",
    "workbook",
    "school_book",
    "dictionary",
    "general_book",
    "other",
)

V43_GENERIC_POLISH_TITLE_WORDS = (
    "Kod", "Uczeń", "Ucznia", "Nauczyciel", "Nauczyciela", "Nauczycielski",
    "Ćwiczenia", "Podręcznik", "Zeszyt", "Dostęp",
)


def parse_validation_error_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [item.strip() for item in value.split(";") if item.strip()]
    return [str(value)]


def split_field_errors(errors: Sequence[str]) -> Tuple[List[str], List[str]]:
    title_errors: List[str] = []
    description_errors: List[str] = []
    for error in errors:
        normalized = normalize_for_compare(error)
        if "meta title" in normalized or "tytul" in normalized or "tytuł" in error.lower():
            title_errors.append(error)
        else:
            description_errors.append(error)
    return title_errors, description_errors


def normalize_generated_meta_title(value: str) -> str:
    title = normalize_spaces(strip_html(value)).strip('"„”')
    title = title.replace("—", "–")
    title = re.sub(r"\s*[-–]\s*", " – ", title)
    title = re.sub(r"\s*\|\s*", " | ", title)
    title = re.sub(r"\s+([,.;:])", r"\1", title)
    title = re.sub(r"([,.;:])(?=[A-Za-zÀ-ž0-9])", r"\1 ", title)
    title = re.sub(r"\s{2,}", " ", title).strip()

    replacements = (
        (r"\bKod\s+Ucznia\b", "kod uczniowski"),
        (r"\bKod\s+Uczniowski\b", "kod uczniowski"),
        (r"\bKod\s+dla\s+Ucznia\b", "kod uczniowski"),
        (r"\bKod\s+Nauczyciela\b", "kod nauczycielski"),
        (r"\bKod\s+Nauczycielski\b", "kod nauczycielski"),
        (r"\bKod\s+dla\s+Nauczyciela\b", "kod nauczycielski"),
        (r"\bKod\s+Dostępu\b", "kod dostępu"),
        (r"\bDostęp\s+Online\b", "dostęp online"),
    )
    for pattern, replacement in replacements:
        title = re.sub(pattern, replacement, title, flags=re.IGNORECASE)

    # Polskie rzeczowniki pospolite nie powinny wyglądać jak angielski Title Case.
    for word in V43_GENERIC_POLISH_TITLE_WORDS:
        title = re.sub(rf"(?<!^)\b{re.escape(word)}\b", word.lower(), title)

    return normalize_spaces(title)






def meta_title_validation_errors(
    meta_title: str,
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> List[str]:
    errors: List[str] = []
    title = normalize_generated_meta_title(meta_title)
    normalized = normalize_for_compare(title)
    source_title = clean_source_title_for_prompt(job.get("title", ""))
    required = title_required_features(job)
    signals = required["signals"]

    if not title:
        return ["Brak meta title"]
    if len(title) > META_TITLE_HARD_MAX:
        errors.append(
            f"Za długi meta title: {len(title)} zn. "
            f"(maks. {META_TITLE_HARD_MAX}; dopuszczamy świadomie więcej niż 60)"
        )
    if len(title) < 20 and len(source_title) >= 35:
        errors.append(f"Meta title jest zbyt ogólny lub zbyt krótki: {len(title)} zn.")
    if "..." in title or title.endswith("…"):
        errors.append("Meta title zawiera wielokropek lub wygląda na ucięty")
    if re.search(r"https?://|www\.", title, flags=re.IGNORECASE):
        errors.append("Meta title zawiera URL")
    if re.search(r"\b(praca\s*zbiorowa|pracazbiorowa)\b", normalized):
        errors.append("Meta title zawiera techniczną wartość autora: praca zbiorowa")
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in TITLE_INTERNAL_NOISE_PATTERNS):
        errors.append("Meta title zawiera oznaczenie wewnętrzne lub status produktu")
    if title.endswith(("-", "–", "—", "|", ":", ",", ";", "/", "+")):
        errors.append("Meta title kończy się separatorem i wygląda na urwany")

    final_word_match = re.search(r"([A-Za-zÀ-ž]+(?:'[A-Za-zÀ-ž]+)?)\s*$", title)
    if final_word_match and normalize_for_compare(final_word_match.group(1)) in TITLE_DANGLING_WORDS:
        errors.append(f"Meta title kończy się niepełnym określeniem: {final_word_match.group(1)}")

    source_identity = [normalize_for_compare(item) for item in signals.get("identity_tokens", [])]
    title_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    if source_identity and not any(token in title_tokens or token in normalized for token in source_identity[:4]):
        errors.append("Meta title zgubił nazwę serii lub główną tożsamość produktu")

    for level in signals.get("levels", []):
        if normalize_for_compare(level) not in normalized:
            errors.append(f"Meta title zgubił poziom lub oznaczenie egzaminu: {level}")

    if required["teacher"] and not re.search(r"teacher|nauczyciel", normalized):
        errors.append("Meta title nie odróżnia wariantu nauczycielskiego")
    if required["student"] and not re.search(r"student|uczen|uczni", normalized):
        errors.append("Meta title nie odróżnia wariantu uczniowskiego")
    if required["ebook"] and "ebook" not in normalized:
        errors.append("Meta title zgubił format eBook")
    if required["etext"] and "etext" not in normalized:
        errors.append("Meta title zgubił format eText")
    if required["access"] and not re.search(r"\b(?:kod|kodem|dostep|access)\b", normalized):
        errors.append("Meta title zgubił informację o kodzie lub dostępie")
    if required["myenglishlab"] and "myenglishlab" not in normalized:
        errors.append("Meta title zgubił platformę MyEnglishLab")
    if required["online_practice"] and not re.search(r"online practice|cwiczenia online", normalized):
        errors.append("Meta title zgubił komponent Online Practice")
    if required["teacher_portal"] and not re.search(r"teacher s portal|portal nauczyciel", normalized):
        errors.append("Meta title zgubił komponent Teacher's Portal")
    if required["without_code"] and not re.search(r"bez\s+(?:kodu|klucza)", normalized):
        errors.append("Meta title zgubił informację, że produkt jest bez kodu lub klucza")

    if any(normalized.startswith(normalize_for_compare(prefix)) for prefix in ("kup", "sprawdz", "odkryj", "poznaj")):
        errors.append("Meta title zaczyna się od CTA")

    if existing_title_owners:
        owner = existing_title_owners.get(normalized)
        if owner and str(owner) != str(job.get("sku", "")):
            errors.append(f"Identyczny meta title istnieje już dla innego SKU: {owner}")

    return list(dict.fromkeys(errors))


def title_candidate_score(
    candidate: str,
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> Tuple[float, List[str]]:
    title = normalize_generated_meta_title(candidate)
    errors = meta_title_validation_errors(title, job, existing_title_owners=existing_title_owners)
    if not title:
        return -10000.0, errors

    required = title_required_features(job)
    signals = required["signals"]
    normalized = normalize_for_compare(title)
    length = len(title)
    score = 100.0 - 35.0 * len(errors)

    # v4.4.3: długość jest tylko jednym z sygnałów rankera; kompletność ma pierwszeństwo.
    # Ranker premiuje kompletne, naturalne title w szerokim zakresie, a 75 znaków
    # jest jedynym twardym maksimum. Tytuł 73-75 znaków nadal może wygrać, jeśli
    # lepiej zachowuje serię, poziom, format, platformę lub typ dostępu.
    if META_TITLE_TARGET_MIN <= length <= META_TITLE_TARGET_MAX:
        score += 32
    elif META_TITLE_TARGET_MAX < length <= META_TITLE_HARD_MAX:
        score += 29
    elif 50 <= length < META_TITLE_TARGET_MIN:
        score += 18
    elif 40 <= length < 50:
        score += 8
    elif length <= META_TITLE_HARD_MAX:
        score += 2
    else:
        score -= 18

    for token in signals.get("identity_tokens", [])[:4]:
        if normalize_for_compare(token) in normalized:
            score += 7
    for level in signals.get("levels", []):
        if normalize_for_compare(level) in normalized:
            score += 10
    for fmt in signals.get("formats", []):
        if normalize_for_compare(fmt) in normalized:
            score += 5
    for platform in signals.get("platforms", []):
        if normalize_for_compare(platform) in normalized:
            score += 5
    if required["teacher_portal"] and "teacher s portal" in normalized:
        score += 9
    if required["online_practice"] and "online practice" in normalized:
        score += 7
    if required["myenglishlab"] and "myenglishlab" in normalized:
        score += 8

    if required["student"] and re.search(r"kod\s+uczni|dla\s+ucznia|uczniowski", normalized):
        score += 12
    if required["teacher"] and re.search(r"kod\s+nauczyciel|dla\s+nauczyciela|nauczycielski", normalized):
        score += 12
    if required["access"] and re.search(r"kod\s+(?:uczni|nauczyciel|dostep)|dostep online", normalized):
        score += 8
    if re.search(r"(?:\||–)\s+kod\s+(?:uczni|nauczyciel|dostep)", title, flags=re.IGNORECASE):
        score += 6

    # Preferujemy jeden czytelny separator. Kilka różnych separatorów wygląda
    # jak zlepek nazwy magazynowej, a nie redakcja SERP.
    dash_count = len(re.findall(r"\s–\s", title))
    pipe_count = title.count("|")
    if dash_count == 1 and pipe_count == 0:
        score += 6
    elif dash_count == 1 and pipe_count == 1:
        score += 5
    elif dash_count + pipe_count == 0 and length >= 40:
        score -= 5
    if dash_count + pipe_count > 2:
        score -= 8
    if "&" in title:
        score -= 4
    if title.count(".") >= 2:
        score -= 4

    awkward_patterns = (
        r"\buczen\s*[–|-]",
        r"\bonline\s+kod\b",
        r"\bportal\s+kod\b",
        r"\bpractice\s+kod\b",
        r"\bebook\s+kod\b",
        r"\betext\s+kod\b",
        r"\bkod\s+online\b",
    )
    for pattern in awkward_patterns:
        if re.search(pattern, normalized):
            score -= 12

    for word in V43_GENERIC_POLISH_TITLE_WORDS:
        if re.search(rf"(?<!^)\b{re.escape(word)}\b", title):
            score -= 1.5

    return score, errors


def extract_meta_title_candidates(data: Dict) -> List[str]:
    raw = data.get("meta_title_candidates", [])
    candidates: List[str] = []
    if isinstance(raw, str):
        candidates.append(raw)
    elif isinstance(raw, list):
        candidates.extend(str(item) for item in raw if str(item).strip())
    legacy = str(data.get("meta_title", "") or "").strip()
    if legacy:
        candidates.append(legacy)
    unique: List[str] = []
    seen: Set[str] = set()
    for candidate in candidates:
        normalized_candidate = normalize_generated_meta_title(candidate)
        key = normalize_for_compare(normalized_candidate)
        if normalized_candidate and key not in seen:
            seen.add(key)
            unique.append(normalized_candidate)
    return unique


def _primary_identity_for_title_repair(job: Dict) -> str:
    """Buduje krótki, bezpieczny rdzeń nazwy do napraw strukturalnych meta title.

    Używamy przede wszystkim pierwszego członu nazwy Akeneo (zwykle seria/model),
    a poziom dopinamy osobno. Funkcja jest uruchamiana wyłącznie jako fallback
    dla tytułu, który już nie przeszedł walidacji - nie zastępuje normalnego rankera.
    """
    raw = clean_source_title_for_prompt(job.get("title", ""))
    raw = normalize_spaces(raw)
    if not raw:
        return ""

    # Pierwszy człon w katalogu najczęściej jest stabilną nazwą serii/modelu.
    parts = [normalize_spaces(part) for part in re.split(r"\s*[.|;]\s*", raw) if normalize_spaces(part)]
    identity = parts[0] if parts else raw

    # Usuń kwalifikatory, które naprawa dopnie później w kontrolowanej postaci.
    identity = re.sub(r"\s*\((?:no\s+key|without\s+key|bez\s+(?:kodu|klucza))\)\s*", " ", identity, flags=re.I)
    identity = re.sub(r"\b(?:Student'?s|Teacher'?s)\s+(?:eBook|Book)\b.*$", "", identity, flags=re.I)
    identity = normalize_spaces(identity).strip(" -–|,;:")

    # Jeżeli pierwszy człon sam jest przesadnie długi, zostawiamy sensowną granicę
    # słowa. To fallback; finalny walidator nadal musi zaakceptować wynik.
    if len(identity) > 48:
        identity = smart_truncate(identity, 48).rstrip(" -–|,;:")
    return identity


def _required_access_phrase(required: Dict[str, object]) -> str:
    if required.get("without_code"):
        return "bez klucza"
    if required.get("teacher") and required.get("access"):
        return "kod nauczycielski"
    if required.get("student") and required.get("access"):
        return "kod uczniowski"
    if required.get("access"):
        return "kod dostępu"
    return ""


def _required_component_phrases(job: Dict, required: Dict[str, object]) -> List[str]:
    """Zwraca tylko twarde cechy wariantu, które odróżniają SKU."""
    source = normalize_for_compare(job.get("title", ""))
    parts: List[str] = []

    # Najbardziej specyficzne platformy/komponenty przed formatem ogólnym.
    if required.get("teacher_portal"):
        parts.append("Teacher's Portal")
    if required.get("myenglishlab"):
        parts.append("MyEnglishLab")
    if required.get("online_practice"):
        parts.append("Online Practice")

    if required.get("etext"):
        parts.insert(0, "eText")
    elif required.get("ebook"):
        # Student's / Teacher's eBook niesie dodatkowo odbiorcę i jest naturalne.
        if required.get("student") and re.search(r"student'?s\s+ebook", source):
            parts.insert(0, "Student's eBook")
        elif required.get("teacher") and re.search(r"teacher'?s\s+ebook", source):
            parts.insert(0, "Teacher's eBook")
        else:
            parts.insert(0, "eBook")

    # Zachowaj kolejność i usuń duplikaty.
    result: List[str] = []
    seen: Set[str] = set()
    for part in parts:
        key = normalize_for_compare(part)
        if key and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def _fit_repair_title(parts: Sequence[str], suffix: str = "") -> str:
    """Składa naprawiony title bez mechanicznego urywania końcówki."""
    clean_parts = [normalize_spaces(p).strip(" -–|,;:") for p in parts if normalize_spaces(p).strip(" -–|,;:")]
    if not clean_parts:
        return ""

    if len(clean_parts) == 1:
        base = clean_parts[0]
    else:
        base = f"{clean_parts[0]} – " + " + ".join(clean_parts[1:])
    candidate = f"{base} | {suffix}" if suffix else base
    candidate = normalize_generated_meta_title(candidate)
    if len(candidate) <= META_TITLE_HARD_MAX:
        return candidate

    # Jeżeli po dodaniu twardego kwalifikatora brakuje miejsca, najpierw usuwamy
    # najmniej specyficzny dodatkowy komponent. Nie usuwamy suffixu ani rdzenia.
    extra = list(clean_parts[1:])
    while extra:
        extra.pop()
        base = clean_parts[0] if not extra else f"{clean_parts[0]} – " + " + ".join(extra)
        candidate = f"{base} | {suffix}" if suffix else base
        candidate = normalize_generated_meta_title(candidate)
        if len(candidate) <= META_TITLE_HARD_MAX:
            return candidate
    return ""


def build_local_meta_title_repairs(meta_title: str, job: Dict) -> List[str]:
    """Tworzy bezkosztowe kandydatury naprawcze na podstawie błędów walidatora.

    Gdy walidator *wie*, że brakuje twardej cechy SKU, aplikacja ma spróbować ją
    naprawić, a nie jedynie wyświetlić ostrzeżenie. Funkcja nie zgaduje cech -
    korzysta wyłącznie z nazwy produktu / title_required_features().
    """
    original = normalize_generated_meta_title(meta_title)
    if not original:
        return []
    errors = meta_title_validation_errors(original, job)
    if not errors:
        return []

    required = title_required_features(job)
    repairs: List[str] = []

    # 1. Najmniej inwazyjna naprawa: dopisz brakujący kwalifikator, jeśli się mieści.
    append_phrases: List[str] = []
    normalized = normalize_for_compare(original)
    levels = required.get("signals", {}).get("levels", []) if isinstance(required.get("signals"), dict) else []
    for level in levels:
        if normalize_for_compare(level) not in normalized:
            append_phrases.append(str(level))
    if required.get("ebook") and "ebook" not in normalized:
        append_phrases.append("eBook")
    if required.get("etext") and "etext" not in normalized:
        append_phrases.append("eText")
    if required.get("myenglishlab") and "myenglishlab" not in normalized:
        append_phrases.append("MyEnglishLab")
    if required.get("online_practice") and "online practice" not in normalized:
        append_phrases.append("Online Practice")
    if required.get("teacher_portal") and "teacher s portal" not in normalized:
        append_phrases.append("Teacher's Portal")

    access_phrase = _required_access_phrase(required)
    if access_phrase and normalize_for_compare(access_phrase) not in normalized:
        append_phrases.append(access_phrase)

    if append_phrases:
        appended = normalize_generated_meta_title(original + " | " + " | ".join(append_phrases))
        if len(appended) <= META_TITLE_HARD_MAX:
            repairs.append(appended)

    # 2. Rekonstrukcja semantyczna: rdzeń serii + poziom + twarde komponenty + dostęp.
    identity = _primary_identity_for_title_repair(job)
    if identity:
        level_tokens = [str(x) for x in levels]
        identity_normalized = normalize_for_compare(identity)
        for level in level_tokens:
            if normalize_for_compare(level) not in identity_normalized:
                identity = normalize_spaces(f"{identity} {level}")

        components = _required_component_phrases(job, required)
        reconstructed = _fit_repair_title([identity, *components], access_phrase)
        if reconstructed:
            repairs.append(reconstructed)

        # 3. Wersja minimalistyczna zachowująca wyłącznie rdzeń + format + twardy dostęp.
        primary_format = ""
        if required.get("etext"):
            primary_format = "eText"
        elif required.get("ebook"):
            if required.get("student"):
                primary_format = "Student's eBook"
            elif required.get("teacher"):
                primary_format = "Teacher's eBook"
            else:
                primary_format = "eBook"
        minimal = _fit_repair_title([identity, primary_format] if primary_format else [identity], access_phrase)
        if minimal:
            repairs.append(minimal)

    unique: List[str] = []
    seen: Set[str] = {normalize_for_compare(original)}
    for candidate in repairs:
        key = normalize_for_compare(candidate)
        if candidate and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def select_best_meta_title_candidate(
    candidates: Sequence[str],
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> Tuple[str, List[str], List[Tuple[str, float, List[str]]]]:
    diagnostics: List[Tuple[str, float, List[str]]] = []
    seen: Set[str] = set()

    def add_candidate(value: str) -> None:
        normalized_candidate = normalize_generated_meta_title(value)
        key = normalize_for_compare(normalized_candidate)
        if not normalized_candidate or key in seen:
            return
        seen.add(key)
        score, errors = title_candidate_score(
            normalized_candidate,
            job,
            existing_title_owners=existing_title_owners,
        )
        diagnostics.append((normalized_candidate, score, errors))

    # Kandydatury AI.
    for candidate in candidates:
        add_candidate(candidate)

    # v4.4.4: walidator-driven auto-repair. Każdy błędny kandydat może stworzyć
    # lokalne, bezkosztowe naprawy. Dzięki temu np. brak „bez klucza” nie kończy
    # się ostrzeżeniem, jeśli da się zbudować poprawny title do 75 znaków.
    initial = list(diagnostics)
    for candidate, _, errors in initial:
        if not errors:
            continue
        for repair in build_local_meta_title_repairs(candidate, job):
            add_candidate(repair)

    if not diagnostics:
        fallback = normalize_generated_meta_title(build_meta_title(job.get("title", ""), job.get("author", "")))
        add_candidate(fallback)
        for repair in build_local_meta_title_repairs(fallback, job):
            add_candidate(repair)

    # Najpierw kandydat bez błędów, dopiero potem score. To pozwala naprawionej
    # wersji wygrać nawet wtedy, gdy pierwotny title stylistycznie miał wyższy score.
    diagnostics.sort(key=lambda item: (not item[2], item[1]), reverse=True)
    best_title, _, best_errors = diagnostics[0]
    return best_title, best_errors, diagnostics




META_SYSTEM_PROMPT = """Jesteś seniorem SEO e-commerce, redaktorem informacji produktowej i specjalistą od katalogów edukacyjnych Bookland.
Dla jednego SKU wykonujesz dwie odrębne czynności: budujesz kandydatury meta title i tworzysz meta description.

NAJWAŻNIEJSZA ZASADA
Najpierw rozpoznaj produkt i jego wariant, dopiero potem redaguj. Nie skracaj nazwy od prawej strony i nie twórz zlepku przypadkowych słów.

KLASYFIKACJA PRODUKTU
Wybierz product_type spośród: student_access_code, teacher_access_code, digital_course_bundle, ebook, etext, online_practice, textbook, workbook, school_book, dictionary, general_book, other.
Ustal canonical_identity: oficjalną nazwę serii, tytułu lub modelu bez śmieci katalogowych.

META TITLE: CZTERY KANDYDATURY
Zwróć cztery różne, pełne kandydatury. Każda musi mieć maksymalnie 75 znaków ze spacjami i być gotowa do publikacji.
- Kandydat 1: najwierniejszy oficjalnej nazwie.
- Kandydat 2: najbardziej naturalny po polsku i czytelny w SERP.
- Kandydat 3: najbardziej zwarty, ale nadal jednoznaczny.
- Kandydat 4: alternatywna redakcja zachowująca wszystkie cechy wariantu.

PRIORYTETY META TITLE
1. Nazwa serii, tytuł lub model.
2. Poziom, egzamin, tom, część albo klasa.
3. Komponent i format odróżniający SKU.
4. Odbiorca oraz typ kodu lub dostępu.
5. Autor tylko dla zwykłych książek i tylko wtedy, gdy nie wypiera ważniejszych danych.

SPÓJNOŚĆ DLA MATERIAŁÓW JĘZYKOWYCH
- Zachowuj oficjalne nazwy: MyEnglishLab, Teacher's Portal, Online Practice, eBook, eText, B2 First, C1 Advanced.
- Tłumacz ogólne komponenty: Student's Book = podręcznik ucznia; Workbook/Activity Book = zeszyt ćwiczeń; Teacher's Book = książka nauczyciela.
- Typ dostępu zapisuj po polsku: kod uczniowski, kod nauczycielski, kod dostępu, bez kodu.
- Nigdy nie kończ tytułu samym słowem „Kod”, „Access”, „Student”, „Teacher”, „Online” ani przyimkiem.
- Nie pisz „Kod Ucznia”, „Uczeń – eBook”, „Online Kod”, „Portal Kod” ani „eBook Kod”.
- Nie mieszaj dowolnie „Online Practice” z „ćwiczeniami online”. Jeśli to oficjalna nazwa komponentu w źródle, preferuj „Online Practice”.
- Polskie rzeczowniki pospolite zapisuj małą literą. Nazwy własne i poziomy zachowuj dokładnie.
- Preferuj jeden separator „ – ”. Drugi separator „ | ” stosuj tylko wtedy, gdy realnie poprawia czytelność. Nie sklejaj kilku kropek i separatorów.

PRZYKŁADOWY KIERUNEK, NIE SZABLON DO KOPIOWANIA
Big English 1. eText + MyEnglishLab Student Online Access Code → Big English 1 – eText + MyEnglishLab | kod uczniowski
Roadmap B2. eBook and Online Practice. Kod uczniowski → Roadmap B2 – eBook + Online Practice | kod uczniowski
Business Partner B1. Teacher's Portal. Kod nauczycielski → Business Partner B1 – Teacher's Portal | kod nauczycielski
1000 chińskich słów(ek). Ilustrowany słownik → 1000 chińskich słów(ek) – słownik ilustrowany

ZWYKŁE KSIĄŻKI
Preferuj „Tytuł – Autor”. Gdy tytuł jest długi, zachowaj pełny sens tytułu i pomiń autora. Nie używaj wartości „praca zbiorowa” ani podejrzanie sklejonych autorów.

META DESCRIPTION
- Celuj w 145-158 znaków; bezwzględny zakres to 140-160.
- Pisz po polsku, konkretnie i informacyjnie. Bez CTA, drugiej osoby, retorycznych pytań i sztucznego problemu.
- Nie zaczynaj od: Odkryj, Poznaj, Sprawdź, Rozwiń, Zyskaj, Wybierz, Niezrozumienie, Tradycyjna nauka, Nowoczesne podejście.
- Dla materiałów edukacyjnych zacznij od nazwy serii, typu materiału, formatu, odbiorcy albo konkretnej funkcji.
- Dla beletrystyki możesz zacząć od bohatera, konfliktu lub miejsca, jeśli wynikają ze źródła.
- Nie dodawaj czasu dostępu, liczby urządzeń, funkcji, poziomu ani żadnego faktu, którego nie ma w danych.
- Nie kopiuj całego zdania źródłowego.

DANE I FORMAT
Korzystaj wyłącznie z przekazanych danych. Zwróć tylko obiekt JSON zgodny ze schematem. Nie komentuj procesu.
"""








def locked_fields_from_job(job: Dict) -> Tuple[str, str]:
    errors = parse_validation_error_list(job.get("validation_errors", ""))
    title_errors, description_errors = split_field_errors(errors)
    locked_title = job.get("meta_title", "") if job.get("meta_title") and not title_errors and int(job.get("attempts", 0)) > 0 else ""
    locked_description = job.get("meta_description", "") if job.get("meta_description") and not description_errors and int(job.get("attempts", 0)) > 0 else ""
    return locked_title, locked_description





# ═══════════════════════════════════════════════════════════════════
# V4.4: TURBO BATCH + ŁAGODNIEJSZA WALIDACJA + POPRAWNE POZIOMY
# ═══════════════════════════════════════════════════════════════════
#
# Najważniejsze zmiany względem v4.3:
# - poziom/egzamin wymagany w meta title pochodzi przede wszystkim z NAZWY SKU;
#   opis źródłowy nie może już narzucić np. B2 produktowi C1 tylko dlatego, że
#   wspomina poziom sąsiedni, serię lub ścieżkę progresji;
# - podobne początki meta description są sygnałem jakościowym, ale NIE blokują
#   produktu. Przy dużych katalogach naturalne jest, że wiele opisów zaczyna się
#   podobnie;
# - twardy zakres meta description jest szerszy (120-170 znaków), natomiast
#   prompt nadal celuje w ok. 135-160;
# - 3 kandydatury meta title zamiast 4: mniej tokenów wyjściowych, ten sam lokalny
#   ranker i praktycznie ten sam poziom bezpieczeństwa;
# - opis wejściowy jest kompresowany semantycznie do ok. 1600 znaków;
# - listy SKU są pobierane z Akeneo czterema równoległymi strumieniami, ale jeden
#   globalny semafor pilnuje maksymalnie 4 równoczesnych requestów;
# - zapisy importu i odbioru batchy korzystają z jednej transakcji / executemany;
# - duży run jest automatycznie shardowany do ok. 1000 produktów na zadanie;
# - do 12 plików jest uploadowanych równolegle, a run 50k może rozłożyć się na
#   ok. 50 niezależnych zadań Batch API (z bezpiecznym soft capem 80 aktywnych);
# - statusy są odpytywane równolegle, a gotowe pliki pobierane osobną pulą,
#   dzięki czemu szybki shard nie czeka na najwolniejszy.

PROMPT_VERSION = "meta-v4.4.4-validator-driven-title-autorepair-2026-08"
BATCH_PRODUCTS_PER_FILE = 1000
TURBO_BATCH_SHARD_SIZE = 1000
BATCH_SUBMIT_WORKERS = 12
BATCH_ACTIVE_JOB_SOFT_CAP = 80
BATCH_MAX_SHARDS_PER_SUBMIT = 80
BATCH_DOWNLOAD_WORKERS = 8
META_DESCRIPTION_HARD_MIN = 120
META_DESCRIPTION_HARD_MAX = 170
META_DESCRIPTION_TARGET_MIN = 135
META_DESCRIPTION_TARGET_MAX = 160
META_RECENT_OPENINGS_HINT = 0
AKENEO_REQUEST_SEMAPHORE = threading.BoundedSemaphore(AKENEO_MAX_WORKERS)


def extract_level_tokens(value: str) -> List[str]:
    """Wyciąga poziomy CEFR/egzaminy bez mieszania poziomów z innych pól."""
    text = normalize_spaces(strip_html(value))
    pattern = (
        r"(?<![A-Za-z0-9])(?:C2\s+Proficiency|C1\s+Advanced|B2\s+First|"
        r"C1\s*[-–]\s*C2|Pre-?A1|A1\+?|A2\+?|B1\+?|B2\+?|C1\+?|C2)(?![A-Za-z0-9])"
    )
    result: List[str] = []
    seen: Set[str] = set()
    for match in re.findall(pattern, text, flags=re.IGNORECASE):
        canonical = normalize_spaces(match).replace("–", "-")
        key = normalize_for_compare(canonical)
        if key not in seen:
            seen.add(key)
            result.append(canonical)
    return result


def extract_title_signals(raw_title: str, description: str = "") -> Dict[str, List[str]]:
    """Sygnały produktu z rozdzieleniem twardych danych nazwy i miękkiego opisu.

    Poziom z nazwy produktu jest wiążący. Dopiero gdy nazwa NIE zawiera poziomu,
    wolno użyć poziomu z opisu, i tylko gdy opis wskazuje dokładnie jeden poziom.
    Dzięki temu Business Partner C1 nie zostanie błędnie odrzucony z powodu B2
    wspomnianego w opisie źródłowym.
    """
    title_only = normalize_spaces(strip_html(raw_title))
    desc_only = normalize_spaces(strip_html(description))
    source = normalize_spaces(f"{title_only} {desc_only[:1200]}")

    title_levels = extract_level_tokens(title_only)
    desc_levels = extract_level_tokens(desc_only[:1800])
    if title_levels:
        levels = title_levels
    else:
        unique_desc = []
        seen_desc: Set[str] = set()
        for level in desc_levels:
            key = normalize_for_compare(level)
            if key not in seen_desc:
                seen_desc.add(key)
                unique_desc.append(level)
        levels = unique_desc if len(unique_desc) == 1 else []

    platforms = [
        name for name in (
            "MyEnglishLab", "Pearson English Portal", "Pearson Practice English App",
            "Teacher's Portal", "Online Practice"
        )
        if re.search(re.escape(name), source, flags=re.IGNORECASE)
    ]
    formats = [
        name for name in ("eBook", "eText", "PDF", "audio", "video", "CD", "DVD", "online")
        if re.search(rf"\b{re.escape(name)}\b", source, flags=re.IGNORECASE)
    ]

    audience: List[str] = []
    if re.search(r"teacher|nauczyciel", source, flags=re.IGNORECASE):
        audience.append("nauczyciel")
    if re.search(r"student|uczni|learner", source, flags=re.IGNORECASE):
        audience.append("uczeń")

    access: List[str] = []
    if re.search(r"access|kod\s+(?:uczniowski|nauczyciel)|code|licenc", source, flags=re.IGNORECASE):
        access.append("kod lub dostęp cyfrowy")
    if re.search(r"without\s+key|no\s+key|bez\s+klucza|bez\s+kodu", source, flags=re.IGNORECASE):
        access.append("bez kodu / klucza")

    components: List[str] = []
    for english, polish in TITLE_COMPONENT_TRANSLATIONS:
        if re.search(re.escape(english), source, flags=re.IGNORECASE):
            components.append(f"{english} → {polish}")

    identity_tokens: List[str] = []
    for token in re.findall(r"[A-Za-zÀ-ž0-9][A-Za-zÀ-ž0-9'+.-]*", clean_source_title_for_prompt(title_only)):
        normalized = normalize_for_compare(token)
        if len(normalized) < 2 or normalized in TITLE_GENERIC_TOKENS:
            continue
        if normalized not in {normalize_for_compare(item) for item in identity_tokens}:
            identity_tokens.append(token)
        if len(identity_tokens) >= 6:
            break

    return {
        "levels": levels,
        "title_levels": title_levels,
        "description_levels": desc_levels,
        "platforms": platforms,
        "formats": formats,
        "audience": audience,
        "access": access,
        "components": components,
        "identity_tokens": identity_tokens,
    }


def source_product_type(job: Dict) -> str:
    """Najpierw klasyfikuj po nazwie; opis jest wyłącznie fallbackiem."""
    title = normalize_for_compare(job.get("title", ""))
    desc = normalize_for_compare(strip_html(job.get("description", ""))[:1200])

    def classify(source: str) -> str:
        has_access = bool(re.search(r"\b(access|code|kod|licenc|portal)\b", source))
        if has_access and re.search(r"teacher|nauczyciel", source):
            return "teacher_access_code"
        if has_access and re.search(r"student|uczen|uczni|learner", source):
            return "student_access_code"
        if "ebook" in source and re.search(r"online practice|myenglishlab|portal|access", source):
            return "digital_course_bundle"
        if "etext" in source:
            return "etext"
        if "ebook" in source:
            return "ebook"
        if re.search(r"online practice|cwiczenia online", source):
            return "online_practice"
        if re.search(r"workbook|activity book|zeszyt cwiczen", source):
            return "workbook"
        if re.search(r"student'?s book|coursebook|podrecznik", source):
            return "textbook"
        if re.search(r"slownik|dictionary", source):
            return "dictionary"
        if re.search(r"klasa\s+\d|liceum|technikum|szkola podstawowa|matura", source):
            return "school_book"
        return "other"

    title_type = classify(title)
    if title_type != "other":
        return title_type
    desc_type = classify(desc)
    if desc_type != "other":
        return desc_type
    if job.get("author") and not re.search(r"access|code|kod|ebook|etext|online practice", title):
        return "general_book"
    return "other"


def title_required_features(job: Dict) -> Dict[str, object]:
    """Twarde wymagania walidatora bierzemy z nazwy SKU, nie z narracji opisu."""
    raw_title = job.get("title", "")
    source = normalize_for_compare(raw_title)
    title_signals = extract_title_signals(raw_title, "")
    return {
        "type": source_product_type(job),
        "signals": title_signals,
        "student": bool(re.search(r"student|uczen|uczni|learner", source)),
        "teacher": bool(re.search(r"teacher|nauczyciel", source)),
        "access": bool(re.search(r"access|code|kod\s+(?:uczni|nauczyciel)|licenc", source)),
        "without_code": bool(re.search(r"without key|no key|bez klucza|bez kodu", source)),
        "ebook": "ebook" in source,
        "etext": "etext" in source,
        "myenglishlab": "myenglishlab" in source,
        "online_practice": "online practice" in source,
        "teacher_portal": bool(re.search(r"teacher\s+s\s+portal|teachers?\s+portal", source)),
    }






def compact_description_context(description: str, title: str = "", max_chars: int = 1600) -> str:
    """Wybiera najbardziej użyteczne fragmenty opisu zamiast wysyłać 3600 znaków."""
    clean = normalize_spaces(strip_html(description))
    if len(clean) <= max_chars:
        return clean

    # Zachowujemy początek oraz zdania zawierające cechy wariantu, poziomy,
    # formaty, platformy i informacje produktowe. Dla zwykłej książki początek
    # opisu nadal niesie fabułę / temat, więc zawsze jest pierwszy.
    sentences = [normalize_spaces(s) for s in re.split(r"(?<=[.!?])\s+", clean) if normalize_spaces(s)]
    selected: List[str] = []
    selected_keys: Set[str] = set()

    def add(sentence: str) -> None:
        key = normalize_for_compare(sentence)
        if sentence and key not in selected_keys:
            selected.append(sentence)
            selected_keys.add(key)

    for sentence in sentences[:3]:
        add(sentence)

    signal_pattern = re.compile(
        r"\b(?:A1|A2|B1|B2|C1|C2|First|Advanced|Proficiency|eBook|eText|"
        r"MyEnglishLab|Online Practice|Teacher'?s Portal|Student|Teacher|uczni|nauczyciel|"
        r"access|code|kod|licenc|Workbook|Student'?s Book|Teacher'?s Book|Coursebook|"
        r"tom|część|czesc|klasa|matura|egzamin|słownik|slownik|dictionary)\b",
        flags=re.IGNORECASE,
    )
    for sentence in sentences[3:]:
        if signal_pattern.search(sentence):
            add(sentence)

    # Dodatkowo wybierz zdania zawierające rzadkie tokeny z nazwy produktu.
    title_tokens = [
        token for token in re.findall(r"[A-Za-zÀ-ž0-9'+.-]+", title)
        if len(normalize_for_compare(token)) >= 4
        and normalize_for_compare(token) not in TITLE_GENERIC_TOKENS
    ][:5]
    for sentence in sentences[3:]:
        normalized_sentence = normalize_for_compare(sentence)
        if any(normalize_for_compare(token) in normalized_sentence for token in title_tokens):
            add(sentence)

    result = " ".join(selected)
    if len(result) < min(900, max_chars) and len(sentences) > len(selected):
        for sentence in sentences:
            add(sentence)
            result = " ".join(selected)
            if len(result) >= min(1100, max_chars):
                break

    return smart_truncate(" ".join(selected), max_chars)


META_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_title_candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Trzy różne kompletne meta title, każdy maksymalnie 75 znaków.",
        },
        "meta_description": {
            "type": "string",
            "description": "Polski meta description produktu, zwykle 135-160 znaków, konkretny i zgodny ze źródłem.",
        },
    },
    "required": ["meta_title_candidates", "meta_description"],
}

META_SYSTEM_PROMPT = """Jesteś seniorem SEO e-commerce i redaktorem informacji produktowej Bookland.
Tworzysz metatagi dla jednego SKU na podstawie danych z Akeneo. Najpierw rozpoznaj tożsamość i wariant produktu, potem redaguj.

META TITLE
Zwróć 3 różne, pełnowartościowe kandydatury. Jedyny twardy limit to 75 znaków ze spacjami.
Nie twórz osobnego wariantu skróconego tylko po to, aby zmieścić się w określonej długości.
Strategie:
1) kandydat pełny: zwykle 65-75 znaków; maksymalnie zachowaj wartościowe cechy produktu,
2) kandydat zbalansowany: zwykle 58-72 znaki; zachowaj kompletność przy naturalnej składni,
3) kandydat alternatywny: zwykle 55-75 znaków; użyj innej naturalnej redakcji, ale nie usuwaj istotnych cech SKU.
Priorytet informacji:
1) oficjalna seria/tytuł/model,
2) poziom/egzamin/tom/część/klasa,
3) komponent i format odróżniający SKU,
4) odbiorca i typ kodu/dostępu,
5) autor tylko dla zwykłych książek, jeśli nie wypiera ważniejszych danych.
Nie skracaj semantycznie ważnego elementu tylko po to, aby uzyskać krótszy title.
Jeżeli pełniejsza wersja do 75 znaków lepiej identyfikuje SKU, preferuj ją nad krótszą, uboższą wersją.
Google może skrócić prezentację w SERP - to akceptowalne; Ty masz dostarczyć kompletny i naturalny title.
Nie ucinaj nazwy mechanicznie. Nie kończ słowem: Kod, Access, Online, Student, Teacher ani przyimkiem.
Dla materiałów językowych zachowuj nazwy własne: MyEnglishLab, Online Practice, Teacher's Portal, eBook, eText, B2 First, C1 Advanced.
Typ dostępu zapisuj naturalnie po polsku: kod uczniowski, kod nauczycielski, kod dostępu, bez kodu / bez klucza.
Jeżeli NAZWA produktu zawiera „no key”, „without key”, „bez kodu” lub „bez klucza”, KAŻDA kandydatura musi zachować tę informację. To cecha wariantu SKU, nie opcjonalny detal.
Jeżeli walidator w retry wskazuje konkretną utraconą cechę, wszystkie nowe kandydatury muszą ją naprawić; nie powtarzaj poprzedniej wersji z tym samym błędem.
Nie twórz zlepków typu: Online Kod, Portal Kod, eBook Kod, Kod Ucznia.
Dla zwykłych książek preferuj „Tytuł – Autor”; przy długim tytule ważniejszy jest pełny sens tytułu niż autor. Nie używaj „praca zbiorowa”.

META DESCRIPTION
Pisz po polsku, konkretnie i informacyjnie. Celuj w 135-160 znaków, ale naturalność i zgodność z produktem są ważniejsze niż dobicie do konkretnej liczby.
Nie dodawaj faktów, czasu licencji, funkcji, poziomu ani liczb, których nie ma w danych.
Bez bezpośrednich CTA sklepu typu „Sprawdź ofertę”, „Kup teraz”, „Zamów teraz”.
Dla materiałów edukacyjnych możesz zaczynać od serii, typu materiału, odbiorcy lub funkcji. Powtarzalne konstrukcje są akceptowalne, jeśli są naturalne i trafne.
Dla książek możesz zaczynać od bohatera, konfliktu, tematu lub miejsca, jeśli wynikają ze źródła.

Korzystaj wyłącznie z przekazanych danych. Zwróć tylko JSON zgodny ze schematem."""








def akeneo_get_product_details(
    sku: str,
    token: str,
    channel: str = DEFAULT_CHANNEL,
    locale: str = DEFAULT_LOCALE,
) -> Optional[Dict]:
    with AKENEO_REQUEST_SEMAPHORE:
        response = request_with_retry(
            "GET",
            _akeneo_root() + f"/api/rest/v1/products/{sku}",
            headers=akeneo_headers(token),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return parse_akeneo_product(response.json(), channel, locale)


def akeneo_fetch_products_by_identifiers(
    token: str,
    channel: str,
    locale: str,
    identifiers: Sequence[str],
) -> Dict[str, Dict]:
    if not identifiers:
        return {}
    search = {"identifier": [{"operator": "IN", "value": list(identifiers)}]}
    params: Dict[str, object] = {
        "limit": 100,
        "scope": channel,
        "locales": locale,
        "with_count": "false",
        "search": json.dumps(search, ensure_ascii=False),
    }
    existing_attributes = akeneo_existing_attribute_codes(token)
    if existing_attributes:
        params["attributes"] = ",".join(existing_attributes)
    with AKENEO_REQUEST_SEMAPHORE:
        response = request_with_retry(
            "GET",
            _akeneo_root() + "/api/rest/v1/products",
            headers=akeneo_headers(token),
            params=params,
        )

    if response.status_code in {400, 414, 422}:
        products: Dict[str, Dict] = {}
        # Globalny semafor nadal ogranicza faktyczne requesty do 4, nawet jeśli
        # kilka chunków równolegle trafi w fallback.
        with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
            futures = {
                executor.submit(akeneo_get_product_details, sku, token, channel, locale): sku
                for sku in identifiers
            }
            for future in as_completed(futures):
                product = future.result()
                if product and product.get("identifier"):
                    products[product["identifier"]] = product
        return products

    response.raise_for_status()
    products: Dict[str, Dict] = {}
    for item in response.json().get("_embedded", {}).get("items", []):
        parsed = parse_akeneo_product(item, channel, locale)
        if parsed.get("identifier"):
            products[parsed["identifier"]] = parsed
    return products




def import_skus_to_meta_queue(
    *,
    skus: Sequence[str],
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    enabled_only: bool,
    only_with_description: bool,
    force_regenerate: bool,
    run_id: str,
    progress_callback=None,
) -> Dict[str, object]:
    unique_skus, duplicate_count = normalize_sku_list(skus)
    stats: Dict[str, object] = {
        "run_id": run_id, "input": len(skus), "unique": len(unique_skus),
        "duplicates": duplicate_count, "found": 0, "queued": 0,
        "skipped_unchanged": 0, "inactive": 0, "without_description": 0,
        "missing": 0, "processed": 0,
    }
    missing_skus: List[str] = []
    inactive_skus: List[str] = []
    without_description_skus: List[str] = []
    sku_chunks = list(chunks(unique_skus, AKENEO_SKU_FILTER_CHUNK_SIZE))

    # Rozgrzej cache listy istniejących atrybutów przed uruchomieniem wątków.
    # Bez tego pierwszy start mógłby równolegle wykonać tę samą serię requestów.
    akeneo_existing_attribute_codes(token)

    # Cztery paczki Akeneo równocześnie. Globalny semafor w funkcjach requestów
    # gwarantuje, że nie przekroczymy AKENEO_MAX_WORKERS.
    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
        futures = {
            executor.submit(akeneo_fetch_products_by_identifiers, token, channel, locale, sku_chunk): sku_chunk
            for sku_chunk in sku_chunks
        }
        for future in as_completed(futures):
            sku_chunk = futures[future]
            try:
                products = future.result()
            except Exception:
                # Jedna paczka nie powinna zatrzymać 10k SKU. Fallback per SKU
                # zachowuje poprawność i nadal podlega globalnemu limitowi 4.
                products = {}
                for sku in sku_chunk:
                    try:
                        product = akeneo_get_product_details(sku, token, channel, locale)
                        if product:
                            products[sku] = product
                    except Exception:
                        pass

            queue_entries: List[Dict] = []
            for sku in sku_chunk:
                stats["processed"] = int(stats["processed"]) + 1
                product = products.get(sku)
                if not product:
                    stats["missing"] = int(stats["missing"]) + 1
                    missing_skus.append(sku)
                    continue
                stats["found"] = int(stats["found"]) + 1
                if enabled_only and not product.get("enabled", False):
                    stats["inactive"] = int(stats["inactive"]) + 1
                    inactive_skus.append(sku)
                    continue
                if only_with_description and not strip_html(product.get("description", "")):
                    stats["without_description"] = int(stats["without_description"]) + 1
                    without_description_skus.append(sku)
                    continue

                queue_entries.append({
                    "sku": sku,
                    "channel": channel,
                    "locale": locale,
                    "store_view_code": store_view_code,
                    "product_data": _prepare_product_data(product),
                    "source_updated": product.get("updated", ""),
                    "force_regenerate": force_regenerate,
                    "run_id": run_id,
                    "source_type": "sku_list",
                })

            queued_map = bulk_upsert_meta_jobs(queue_entries)
            for queued in queued_map.values():
                if queued:
                    stats["queued"] = int(stats["queued"]) + 1
                else:
                    stats["skipped_unchanged"] = int(stats["skipped_unchanged"]) + 1

            if progress_callback:
                progress_callback(stats)

    IMPORT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = IMPORT_REPORT_DIR / f"{run_id}-odrzucone.tsv"
    with report_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["sku", "powod"])
        writer.writerows((sku, "nie znaleziono w Akeneo") for sku in missing_skus)
        writer.writerows((sku, "produkt nieaktywny") for sku in inactive_skus)
        writer.writerows((sku, "brak opisu źródłowego") for sku in without_description_skus)
    stats["report_path"] = str(report_path)
    return stats


def _remote_batches_by_display_name(display_names: Set[str], api_key: str) -> Dict[str, Dict]:
    """Odzyskuje istniejące jobs po deterministycznej nazwie sharda.

    Batch create nie jest idempotentne. Ten lookup chroni przed ponownym
    naliczeniem kosztu, jeśli Streamlit padł po utworzeniu joba, ale przed
    zapisaniem jego `job_name` do SQLite.
    """
    if not display_names:
        return {}
    wanted = set(display_names)
    found: Dict[str, Dict] = {}
    try:
        client = genai.Client(api_key=api_key)
        for batch_job in client.batches.list(config={"page_size": 100}):
            display = str(getattr(batch_job, "display_name", "") or "")
            if display not in wanted:
                continue
            found[display] = {
                "job_name": batch_job.name,
                "display_name": display,
                "input_file_name": "",
                "state": batch_state_name(batch_job),
            }
            if len(found) == len(wanted):
                break
    except Exception:
        # Recovery jest dodatkowym bezpiecznikiem; awaria listowania nie blokuje
        # normalnego submitu.
        return {}
    return found


def _submit_single_batch_file(spec: Dict, api_key: str) -> Dict:
    client = genai.Client(api_key=api_key)
    uploaded_file = client.files.upload(
        file=str(spec["input_path"]),
        config=types.UploadFileConfig(display_name=spec["display_name"], mime_type="jsonl"),
    )
    batch_job = client.batches.create(
        model=GEMINI_MODEL,
        src=uploaded_file.name,
        config={"display_name": spec["display_name"]},
    )
    state = getattr(getattr(batch_job, "state", None), "name", None) or str(
        getattr(batch_job, "state", "JOB_STATE_PENDING")
    )
    return {
        **spec,
        "job_name": batch_job.name,
        "input_file_name": uploaded_file.name,
        "state": state,
    }


def submit_queued_batches(
    products_per_file: int = BATCH_PRODUCTS_PER_FILE,
    run_id: Optional[str] = None,
) -> List[Dict]:
    """Tworzy i wysyła wiele małych shardów bez trzymania całego runu w RAM.

    Google dopuszcza do 100 równoczesnych batch jobs. Aplikacja celowo zostawia
    zapas i utrzymuje soft cap 80 aktywnych zadań.
    """
    queued_count = int(meta_status_counts(run_id).get("queued", 0))
    if queued_count <= 0:
        return []

    requested = max(100, int(products_per_file))
    effective_size = min(requested, TURBO_BATCH_SHARD_SIZE) if queued_count >= 5000 else requested

    active_jobs = active_batch_job_count(run_id)
    available_slots = max(0, BATCH_ACTIVE_JOB_SOFT_CAP - active_jobs)
    max_new_shards = min(BATCH_MAX_SHARDS_PER_SUBMIT, available_slots)
    if max_new_shards <= 0:
        return [{
            "job_name": "",
            "display_name": "soft-cap",
            "products": 0,
            "state": "WAIT_FOR_ACTIVE_BATCHES",
            "error": f"Aktywnych zadań: {active_jobs}. Najpierw odbierz zakończone shardy.",
        }]

    specs: List[Dict] = []
    for part, chunk in enumerate(
        iter_meta_job_chunks(statuses=["queued"], run_id=run_id, chunk_size=effective_size),
        start=1,
    ):
        if len(specs) >= max_new_shards:
            break
        job_keys = [job["job_key"] for job in chunk]
        shard_fingerprint = hashlib.sha1(
            (PROMPT_VERSION + "|" + "|".join(job_keys)).encode("utf-8")
        ).hexdigest()[:12]
        display_name = f"bookland-meta-{(run_id or 'all')[-14:]}-{part:03d}-{shard_fingerprint}"
        input_path = BATCH_DIR / f"{display_name}.jsonl"
        write_batch_jsonl(chunk, input_path)
        specs.append({
            "display_name": display_name,
            "input_path": input_path,
            "job_keys": job_keys,
            "products": len(chunk),
            "input_mb": round(input_path.stat().st_size / (1024 * 1024), 2),
        })
        # `chunk` znika po iteracji; nie kumulujemy opisów 50k produktów w pamięci.

    if not specs:
        return []

    submitted: List[Dict] = []
    api_key = str(st.secrets["GOOGLE_API_KEY"])

    # Najpierw odzyskaj ewentualne zdalne jobs utworzone przed crashem UI.
    recovered = _remote_batches_by_display_name(
        {spec["display_name"] for spec in specs}, api_key
    )
    specs_to_submit: List[Dict] = []
    for spec in specs:
        remote = recovered.get(spec["display_name"])
        if not remote:
            specs_to_submit.append(spec)
            continue
        register_batch_job(
            job_name=remote["job_name"],
            run_id=run_id or "",
            display_name=spec["display_name"],
            input_path=spec["input_path"],
            input_file_name=remote.get("input_file_name", ""),
            product_count=spec["products"],
            state=remote["state"],
        )
        now = utcnow_iso()
        with db_connect() as conn:
            conn.executemany(
                "UPDATE meta_jobs SET status='batch_submitted', batch_job_name=?, updated_at=? WHERE job_key=?",
                [(remote["job_name"], now, key) for key in spec["job_keys"]],
            )
        submitted.append({
            "job_name": remote["job_name"],
            "display_name": spec["display_name"],
            "products": spec["products"],
            "input_mb": spec.get("input_mb", 0),
            "state": f"RECOVERED:{remote['state']}",
        })

    if not specs_to_submit:
        return sorted(submitted, key=lambda item: item["display_name"])

    with ThreadPoolExecutor(max_workers=min(BATCH_SUBMIT_WORKERS, len(specs_to_submit))) as executor:
        futures = {executor.submit(_submit_single_batch_file, spec, api_key): spec for spec in specs_to_submit}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                result = future.result()
                register_batch_job(
                    job_name=result["job_name"],
                    run_id=run_id or "",
                    display_name=result["display_name"],
                    input_path=result["input_path"],
                    input_file_name=result["input_file_name"],
                    product_count=result["products"],
                    state=result["state"],
                )
                now = utcnow_iso()
                with db_connect() as conn:
                    conn.executemany(
                        "UPDATE meta_jobs SET status='batch_submitted', batch_job_name=?, updated_at=? WHERE job_key=?",
                        [(result["job_name"], now, key) for key in result["job_keys"]],
                    )
                submitted.append({
                    "job_name": result["job_name"],
                    "display_name": result["display_name"],
                    "products": result["products"],
                    "input_mb": result.get("input_mb", 0),
                    "state": result["state"],
                })
            except Exception as exc:
                # Rekordy pozostają queued, więc ponowne kliknięcie wyśle wyłącznie
                # shard, którego faktycznie nie udało się zarejestrować.
                submitted.append({
                    "job_name": "",
                    "display_name": spec["display_name"],
                    "products": spec["products"],
                    "input_mb": spec.get("input_mb", 0),
                    "state": "SUBMIT_FAILED",
                    "error": str(exc),
                })
    return sorted(submitted, key=lambda item: item["display_name"])


def get_meta_jobs_by_keys(keys: Sequence[str]) -> Dict[str, Dict]:
    unique = list(dict.fromkeys(key for key in keys if key))
    result: Dict[str, Dict] = {}
    if not unique:
        return result
    with db_connect() as conn:
        for key_chunk in chunks(unique, 500):
            placeholders = ",".join("?" for _ in key_chunk)
            rows = conn.execute(f"SELECT * FROM meta_jobs WHERE job_key IN ({placeholders})", list(key_chunk)).fetchall()
            result.update({row["job_key"]: dict(row) for row in rows})
    return result


def save_meta_results_bulk(records: Sequence[Dict], failed_records: Sequence[Tuple[str, str]] = ()) -> None:
    if not records and not failed_records:
        return
    now = utcnow_iso()
    update_rows = []
    for record in records:
        meta_description = record.get("meta_description", "")
        opening = opening_signature(meta_description, 6)
        short_opening = opening_signature(meta_description, 3)
        normalized_hash = hashlib.sha256(normalize_for_compare(meta_description).encode("utf-8")).hexdigest()
        update_rows.append((
            record.get("meta_title", ""), meta_description, record["status"], int(record.get("attempts", 1)),
            opening, short_opening, normalized_hash,
            json.dumps(list(record.get("validation_errors", ())), ensure_ascii=False),
            record.get("error_message", ""), now, record["job_key"],
        ))

    with db_connect() as conn:
        if update_rows:
            conn.executemany(
                """
                UPDATE meta_jobs SET meta_title=?, meta_description=?, status=?, attempts=?,
                    opening_signature=?, short_opening_signature=?, normalized_hash=?,
                    validation_errors=?, error_message=?, updated_at=?
                WHERE job_key=?
                """,
                update_rows,
            )
        if failed_records:
            conn.executemany(
                "UPDATE meta_jobs SET status='failed', error_message=?, updated_at=? WHERE job_key=?",
                [(message[:2000], now, key) for key, message in failed_records if key],
            )


def ingest_batch_result_bytes(job_name: str, content: bytes) -> Dict[str, int]:
    decoded = content.decode("utf-8")
    raw_lines = [line for line in decoded.splitlines() if line.strip()]
    parsed_payloads: List[Tuple[Dict, str]] = []
    keys: List[str] = []
    stats = {"completed": 0, "validation_failed": 0, "failed": 0}

    for line in raw_lines:
        try:
            payload = json.loads(line)
            key = str(payload.get("key") or payload.get("metadata", {}).get("key") or "")
            parsed_payloads.append((payload, key))
            if key:
                keys.append(key)
        except Exception:
            stats["failed"] += 1

    jobs = get_meta_jobs_by_keys(keys)
    title_owners = existing_meta_title_owners()
    result_records: List[Dict] = []
    failed_records: List[Tuple[str, str]] = []

    for payload, fallback_key in parsed_payloads:
        key = fallback_key
        try:
            extracted_key, text = extract_batch_response_text(payload)
            key = extracted_key or fallback_key
            if not key:
                raise RuntimeError("Brak klucza zadania w odpowiedzi")
            job = jobs.get(key)
            if not job:
                raise RuntimeError(f"Nieznany klucz zadania: {key}")
            data = json.loads(strip_code_fences(text))
            locked_title, locked_description = locked_fields_from_job(job)

            if locked_title:
                meta_title = locked_title
                title_errors: List[str] = []
            else:
                meta_title, title_errors, _ = select_best_meta_title_candidate(
                    extract_meta_title_candidates(data), job, existing_title_owners=title_owners
                )

            generated_description = normalize_spaces(strip_html(str(data.get("meta_description", "")))).strip('"„”')
            meta_description = locked_description or generated_description
            description_errors = [] if locked_description else meta_validation_errors(meta_description, job=job)
            errors = [*title_errors, *description_errors]
            status = "completed" if not errors else "validation_failed"

            result_records.append({
                "job_key": key,
                "meta_title": meta_title,
                "meta_description": meta_description,
                "status": status,
                "attempts": int(job.get("attempts", 0)) + 1,
                "validation_errors": errors,
                "error_message": "; ".join(errors),
            })
            stats[status] += 1
            if status == "completed":
                title_owners[normalize_for_compare(meta_title)] = str(job["sku"])
        except Exception as exc:
            stats["failed"] += 1
            if key:
                failed_records.append((key, str(exc)))

    save_meta_results_bulk(result_records, failed_records)
    with db_connect() as conn:
        conn.execute(
            "UPDATE batch_jobs SET ingested_at=?, updated_at=? WHERE job_name=?",
            (utcnow_iso(), utcnow_iso(), job_name),
        )
    return stats



def revalidate_meta_jobs_v44(run_id: Optional[str] = None) -> Dict[str, int]:
    """Przelicza stare validation_failed nowymi, łagodniejszymi regułami bez kosztu AI."""
    jobs = list_meta_jobs(
        statuses=["validation_failed"],
        order_by="created_at ASC",
        run_id=run_id,
    )
    title_owners = existing_meta_title_owners()
    records: List[Dict] = []
    stats = {"checked": 0, "promoted": 0, "still_failed": 0}

    for job in jobs:
        stats["checked"] += 1
        title_errors = meta_title_validation_errors(
            job.get("meta_title", ""), job, existing_title_owners=title_owners
        )
        description_errors = meta_validation_errors(job.get("meta_description", ""), job=job)
        errors = [*title_errors, *description_errors]
        status = "completed" if not errors else "validation_failed"
        if status == "completed":
            stats["promoted"] += 1
            title_owners[normalize_for_compare(job.get("meta_title", ""))] = str(job.get("sku", ""))
        else:
            stats["still_failed"] += 1
        records.append({
            "job_key": job["job_key"],
            "meta_title": job.get("meta_title", ""),
            "meta_description": job.get("meta_description", ""),
            "status": status,
            "attempts": int(job.get("attempts", 0)),
            "validation_errors": errors,
            "error_message": "; ".join(errors),
        })

    save_meta_results_bulk(records)
    return stats


# ═══════════════════════════════════════════════════════════════════
# V4.4.3 — twardy limit 75 znaków i pełniejsze meta title
# ═══════════════════════════════════════════════════════════════════
# - meta title ma jeden twardy limit 75 znaków; wszystkie 3 kandydatury są pełnowartościowe;
# - ranker preferuje zachowanie kompletnej informacji o wariancie nad agresywnym skracaniem;
# - powtarzalne początki i nawet identyczne meta description są raportowane,
#   ale NIE zmieniają statusu na validation_failed;
# - poziom/egzamin w meta title jest walidowany WYŁĄCZNIE względem nazwy SKU;
#   opis nie może narzucić B2 produktowi C1 przez linkowanie do innych poziomów;
# - import SQLite prefetchuje istniejące rekordy i wykonuje prawdziwy executemany;
# - statusy kilku shardów Batch API są odpytywane równolegle;
# - kontekst opisu jest dynamicznie krótszy dla kodów/dostępów cyfrowych;
# - maksymalny output Gemini zmniejszony, bo schema zawiera tylko 3 tytuły + opis.

PROMPT_VERSION = "meta-v4.4.4-validator-driven-title-autorepair-2026-08"
META_RECENT_OPENINGS_HINT = 0
GEMINI_META_MAX_OUTPUT_TOKENS = 320
BATCH_REFRESH_WORKERS = 20


def description_context_limit(job: Dict) -> int:
    """Mniej tokenów dla prostych wariantów cyfrowych, więcej dla książek."""
    product_type = source_product_type(job)
    if product_type in {
        "student_access_code", "teacher_access_code", "digital_course_bundle",
        "online_practice", "ebook", "etext",
    }:
        return 1050
    if product_type in {"textbook", "workbook", "school_book"}:
        return 1400
    return 1850


def meta_validation_errors(
    meta_description: str,
    *,
    existing_long_signatures: Optional[Set[str]] = None,
    existing_short_signatures: Optional[Counter] = None,
    recent_descriptions: Optional[Sequence[str]] = None,
    job: Optional[Dict] = None,
) -> List[str]:
    """Tylko błędy, które realnie uzasadniają retry.

    Nie blokujemy za podobny początek, pierwsze 3/6 słów ani duplikat opisu.
    Przy katalogu 10k-50k te sygnały są normalne i nie warte kolejnego requestu AI.
    """
    errors: List[str] = []
    text = normalize_spaces(strip_html(meta_description)).strip('"„”')
    normalized = normalize_for_compare(text)

    if not text:
        return ["Brak meta description"]
    if len(text) < META_DESCRIPTION_HARD_MIN:
        errors.append(
            f"Za krótki meta description: {len(text)} zn. (minimum techniczne {META_DESCRIPTION_HARD_MIN})"
        )
    if len(text) > META_DESCRIPTION_HARD_MAX:
        errors.append(
            f"Za długi meta description: {len(text)} zn. (maksimum techniczne {META_DESCRIPTION_HARD_MAX})"
        )

    hard_cta = (
        "sprawdź ofertę", "sprawdz oferte", "kup teraz", "zamów teraz", "zamow teraz",
        "dodaj do koszyka", "zamów już dziś", "zamow juz dzis",
    )
    if any(normalize_for_compare(phrase) in normalized for phrase in hard_cta):
        errors.append("Meta description zawiera bezpośrednie CTA sklepu")
    if re.search(r"https?://|www\.", text, flags=re.IGNORECASE):
        errors.append("Meta description zawiera URL")
    if "..." in text or text.endswith("…"):
        errors.append("Meta description wygląda na ucięty")
    if re.search(r"\b(xyz|lorem|ipsum|placeholder)\b", normalized):
        errors.append("Meta description zawiera placeholder")

    # Liczby nadal chronimy, bo halucynowana długość licencji/czas dostępu jest
    # merytorycznie groźniejsza niż podobieństwo stylistyczne.
    if job:
        source = normalize_for_compare(
            f"{job.get('title', '')} {strip_html(job.get('description', ''))}"
        )
        generated_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", normalized))
        source_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", source))
        unsupported = sorted(generated_numbers - source_numbers)
        if unsupported:
            errors.append(
                "Meta description dodaje liczby nieobecne w źródle: "
                + ", ".join(unsupported[:5])
            )

    return list(dict.fromkeys(errors))


def audit_meta_jobs_for_repetition(run_id: Optional[str] = None) -> Dict[str, int]:
    """Audyt jakości bez karania meta description za powtarzalność.

    Do poprawy kierowane są wyłącznie krytyczne błędy/duplikaty meta title.
    Duplikaty meta description są tylko liczone diagnostycznie.
    """
    jobs = list_meta_jobs(statuses=["completed"], order_by="created_at ASC", run_id=run_id)
    seen_hash: Dict[str, str] = {}
    seen_titles: Dict[str, str] = {}
    flagged: Dict[str, List[str]] = {}
    duplicate_descriptions = 0

    for job in jobs:
        reasons: List[str] = []
        title_key = normalize_for_compare(job.get("meta_title", ""))
        title_errors = meta_title_validation_errors(job.get("meta_title", ""), job)
        reasons.extend(title_errors)

        if title_key and title_key in seen_titles and seen_titles[title_key] != job["sku"]:
            reasons.append(f"Identyczny meta title jak SKU {seen_titles[title_key]}")
        elif title_key:
            seen_titles[title_key] = job["sku"]

        # Meta description nie jest kluczowym elementem różnicującym URL-e w SEO.
        # Identyczne opisy liczymy, ale nie marnujemy na nie retry i nie blokujemy eksportu.
        norm_hash = job.get("normalized_hash", "")
        if norm_hash and norm_hash in seen_hash and seen_hash[norm_hash] != job["sku"]:
            duplicate_descriptions += 1
        elif norm_hash:
            seen_hash[norm_hash] = job["sku"]

        if reasons:
            flagged[job["job_key"]] = list(dict.fromkeys(reasons))

    if flagged:
        now = utcnow_iso()
        with db_connect() as conn:
            conn.executemany(
                """
                UPDATE meta_jobs SET status='validation_failed', validation_errors=?, updated_at=?
                WHERE job_key=?
                """,
                [
                    (json.dumps(reasons, ensure_ascii=False), now, job_key)
                    for job_key, reasons in flagged.items()
                ],
            )
    return {
        "checked": len(jobs),
        "flagged": len(flagged),
        "duplicate_descriptions": duplicate_descriptions,
    }


def bulk_upsert_meta_jobs(entries: Sequence[Dict]) -> Dict[str, bool]:
    """Prawdziwy bulk upsert: jeden SELECT zbiorczy + executemany + jeden commit."""
    results: Dict[str, bool] = {}
    if not entries:
        return results

    now = utcnow_iso()
    prepared: List[Dict] = []
    for entry in entries:
        sku = str(entry["sku"])
        channel = str(entry["channel"])
        locale = str(entry["locale"])
        product_data = entry["product_data"]
        title = safe_string_value(product_data.get("title"))
        author = safe_string_value(product_data.get("author"))
        description = safe_string_value(product_data.get("description"))
        details = safe_string_value(product_data.get("details"))
        input_hash = product_input_hash(sku, title, author, description, channel, locale)
        style = build_style_plan(sku, title, author, description)
        job_key = make_job_key(sku, channel, locale)
        prepared.append({
            "entry": entry,
            "sku": sku,
            "channel": channel,
            "locale": locale,
            "title": title,
            "author": author,
            "description": description,
            "details": details,
            "input_hash": input_hash,
            "style": style,
            "job_key": job_key,
        })

    existing_map: Dict[str, sqlite3.Row] = {}
    keys = [item["job_key"] for item in prepared]
    with db_connect() as conn:
        for key_chunk in chunks(keys, 500):
            placeholders = ",".join("?" for _ in key_chunk)
            rows = conn.execute(
                f"SELECT job_key, input_hash, prompt_version, model, status FROM meta_jobs WHERE job_key IN ({placeholders})",
                list(key_chunk),
            ).fetchall()
            existing_map.update({row["job_key"]: row for row in rows})

        touch_rows: List[Tuple] = []
        upsert_rows: List[Tuple] = []
        for item in prepared:
            entry = item["entry"]
            existing = existing_map.get(item["job_key"])
            unchanged_completed = bool(
                existing
                and existing["input_hash"] == item["input_hash"]
                and existing["prompt_version"] == PROMPT_VERSION
                and existing["model"] == GEMINI_MODEL
                and existing["status"] == "completed"
                and not bool(entry.get("force_regenerate", False))
            )
            if unchanged_completed:
                touch_rows.append((
                    entry.get("run_id", ""), entry.get("source_type", "catalog"),
                    entry.get("store_view_code", ""), now, item["job_key"],
                ))
                results[item["sku"]] = False
                continue

            style = item["style"]
            upsert_rows.append((
                item["job_key"], entry.get("run_id", ""), entry.get("source_type", "catalog"),
                item["sku"], item["channel"], item["locale"], entry.get("store_view_code", ""),
                item["title"], item["author"], item["description"], item["details"],
                entry.get("source_updated", ""), item["input_hash"], PROMPT_VERSION, GEMINI_MODEL,
                style["seed"], style["opening_mode"], style["rhythm_mode"], style["focus_mode"],
                ", ".join(style["semantic_cues"]), style["source_lead"], now, now,
            ))
            results[item["sku"]] = True

        if touch_rows:
            conn.executemany(
                "UPDATE meta_jobs SET run_id=?, source_type=?, store_view_code=?, updated_at=? WHERE job_key=?",
                touch_rows,
            )
        if upsert_rows:
            conn.executemany(
                """
                INSERT INTO meta_jobs(
                    job_key, run_id, source_type, sku, channel, locale, store_view_code, title, author,
                    description, details, source_updated, input_hash, prompt_version, model, style_seed,
                    opening_mode, rhythm_mode, focus_mode, semantic_cues, source_lead, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    run_id=excluded.run_id, source_type=excluded.source_type,
                    store_view_code=excluded.store_view_code, title=excluded.title,
                    author=excluded.author, description=excluded.description, details=excluded.details,
                    source_updated=excluded.source_updated, input_hash=excluded.input_hash,
                    prompt_version=excluded.prompt_version, model=excluded.model,
                    style_seed=excluded.style_seed, opening_mode=excluded.opening_mode,
                    rhythm_mode=excluded.rhythm_mode, focus_mode=excluded.focus_mode,
                    semantic_cues=excluded.semantic_cues, source_lead=excluded.source_lead,
                    status='queued', attempts=0, meta_title='', meta_description='',
                    opening_signature='', short_opening_signature='', normalized_hash='',
                    validation_errors='', error_message='', batch_job_name='', updated_at=excluded.updated_at
                """,
                upsert_rows,
            )
    return results


def build_meta_prompt(
    job: Dict,
    attempt: int = 0,
    avoid_openings: Sequence[str] = (),
    previous_meta_title: str = "",
    previous_meta_description: str = "",
    previous_errors: Sequence[str] = (),
) -> str:
    """Krótszy prompt per SKU, zachowujący pełną logikę SEO v4.4.1."""
    style = build_style_plan(
        job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
    )
    raw_title = normalize_spaces(strip_html(job.get("title", "")))
    cleaned_title = clean_source_title_for_prompt(raw_title)
    context_limit = description_context_limit(job)
    description = compact_description_context(job.get("description", ""), raw_title, context_limit)
    author, author_note = clean_author_for_prompt(job.get("author", ""), description)
    signals = extract_title_signals(raw_title, description)
    cues = ", ".join(style["semantic_cues"][:5]) or "brak"

    prior_errors = list(previous_errors) or parse_validation_error_list(job.get("validation_errors", ""))
    title_errors, description_errors = split_field_errors(prior_errors)
    prior_title = previous_meta_title or (job.get("meta_title", "") if attempt > 0 else "")
    prior_description = previous_meta_description or (job.get("meta_description", "") if attempt > 0 else "")
    locked_title = prior_title if prior_title and not title_errors and attempt > 0 else ""
    locked_description = prior_description if prior_description and not description_errors and attempt > 0 else ""

    retry_block = ""
    if attempt > 0 or prior_errors:
        retry_block = (
            "\nPOPRZEDNIA PRÓBA\n"
            f"Meta title: {prior_title or 'brak'}\n"
            f"Meta description: {prior_description or 'brak'}\n"
            "Napraw wyłącznie:\n"
            + ("\n".join(f"- {error}" for error in prior_errors) or "- brak")
        )

    lock_block = ""
    if locked_title:
        lock_block += f"\nPOPRAWNY META TITLE — NIE ZMIENIAJ: {locked_title}"
    if locked_description:
        lock_block += f"\nPOPRAWNY META DESCRIPTION — NIE ZMIENIAJ: {locked_description}"

    return f"""Przygotuj metatagi dla SKU {job['sku']}.

DANE
Nazwa: {raw_title}
Nazwa oczyszczona: {cleaned_title}
Autor/marka: {author or 'brak'}
Uwaga o autorze: {author_note}
Dodatkowe dane: {job.get('details', '') or 'brak'}
Opis źródłowy: {description or 'brak opisu'}

SYGNAŁY
{title_signal_summary(signals)}
Typ: {source_product_type(job)}
Kontekst semantyczny: {cues}
Twarde cechy wymagane przez walidator: {title_required_features(job)}

META TITLE
- 3 różne pełnowartościowe kandydatury do 75 znaków, chyba że pole jest zablokowane.
- Pełny: zwykle 65-75; zbalansowany: 58-72; alternatywny: 55-75.
- Nie twórz specjalnie krótkiej wersji. Jeżeli dłuższy title do 75 znaków zachowuje ważną cechę SKU, preferuj go.
- Zachowaj twarde cechy z NAZWY: serię, poziom/egzamin, format, komponent, odbiorcę i rodzaj dostępu, jeśli występują.
- Poziom z nazwy jest wiążący; poziomy tylko z opisu nie są obowiązkowe.

META DESCRIPTION
- Celuj w {META_DESCRIPTION_TARGET_MIN}-{META_DESCRIPTION_TARGET_MAX} znaków; 120-170 jest technicznie akceptowalne.
- Podobne lub identycznie zaczynające się opisy innych SKU są dozwolone.
- Bez halucynowanych liczb/funkcji i bez bezpośredniego CTA sklepu.
{retry_block}
{lock_block}

Zwróć wyłącznie JSON zgodny ze schematem."""


def generate_metatags_interactive(job: Dict) -> Dict:
    title_owners = existing_meta_title_owners(job["job_key"])
    last_title = ""
    last_description = ""
    last_title_errors: List[str] = []
    last_description_errors: List[str] = []

    for attempt in range(MAX_META_RETRIES + 1):
        style = build_style_plan(
            job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
        )
        locked_title = last_title if last_title and not last_title_errors else ""
        locked_description = last_description if last_description and not last_description_errors else ""
        try:
            response = get_gemini_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=build_meta_prompt(
                    job,
                    attempt,
                    (),
                    previous_meta_title=last_title,
                    previous_meta_description=last_description,
                    previous_errors=[*last_title_errors, *last_description_errors],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=META_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=META_RESPONSE_SCHEMA,
                    seed=style["seed"],
                    temperature=0.68,
                    top_p=0.90,
                    max_output_tokens=GEMINI_META_MAX_OUTPUT_TOKENS,
                ),
            )
            data = json.loads(strip_code_fences(response.text or ""))
            if locked_title:
                selected_title = locked_title
                title_errors: List[str] = []
            else:
                selected_title, title_errors, _ = select_best_meta_title_candidate(
                    extract_meta_title_candidates(data), job, existing_title_owners=title_owners
                )

            generated_description = normalize_spaces(
                strip_html(str(data.get("meta_description", "")))
            ).strip('"„”')
            selected_description = locked_description or generated_description
            description_errors = [] if locked_description else meta_validation_errors(
                selected_description, job=job
            )

            last_title = selected_title
            last_description = selected_description
            last_title_errors = title_errors
            last_description_errors = description_errors
            if not title_errors and not description_errors:
                return {
                    "meta_title": selected_title,
                    "meta_description": selected_description,
                    "attempts": attempt + 1,
                    "validation_errors": [],
                    "error": "",
                }
        except Exception as exc:
            api_error = f"Błąd Gemini lub JSON: {exc}"
            message = str(exc).lower()
            if not last_title:
                last_title_errors = [api_error]
            if not last_description:
                last_description_errors = [api_error]

            # Błędy trwałe nie mają sensu w retry. Szczególnie 403 potrafił wcześniej
            # wykonać kilka identycznych prób i wydłużyć pozornie zawieszony przebieg.
            fatal_api_error = (
                "permission_denied" in message
                or "project has been denied access" in message
                or "api_key_invalid" in message
                or "invalid api key" in message
            )
            if fatal_api_error:
                break

            # Timeout jest ograniczony przez GEMINI_HTTP_TIMEOUT_MS. Pozwalamy na
            # jedną kolejną próbę, ale nie zużywamy wszystkich retry jakościowych
            # na powtarzające się problemy transportowe.
            timeout_error = "timeout" in message or "timed out" in message or "deadline" in message
            if timeout_error and attempt >= 1:
                break

    errors = [*last_title_errors, *last_description_errors]
    return {
        "meta_title": last_title or normalize_generated_meta_title(
            build_meta_title(job.get("title", ""), job.get("author", ""))
        ),
        "meta_description": last_description,
        "attempts": MAX_META_RETRIES + 1,
        "validation_errors": errors,
        "error": "; ".join(errors),
    }


def batch_request_for_job(job: Dict, attempt: int = 0) -> Dict:
    style = build_style_plan(
        job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
    )
    return {
        "key": job["job_key"],
        "request": {
            "contents": [{"role": "user", "parts": [{"text": build_meta_prompt(job, attempt, ())}]}],
            "system_instruction": {"parts": [{"text": META_SYSTEM_PROMPT}]},
            "generation_config": {
                # Gemini 3.5 Flash-Lite ma `minimal` jako domyślny thinking.
                # Nie dodajemy opcjonalnych pól konfiguracyjnych do JSONL, aby
                # zachować maksymalną zgodność Batch API. Sampling params są w
                # Gemini 3.5 deprecated, więc nie wysyłamy temperature/top_p.
                "seed": style["seed"],
                "max_output_tokens": GEMINI_META_MAX_OUTPUT_TOKENS,
                "response_mime_type": "application/json",
                "response_schema": META_RESPONSE_SCHEMA,
            },
        },
    }


def _refresh_single_batch_remote(stored: Dict, api_key: str) -> Dict:
    """Szybki status check bez pobierania dużego outputu w tym samym workerze."""
    client = genai.Client(api_key=api_key)
    batch_job = client.batches.get(name=stored["job_name"])
    state = batch_state_name(batch_job)
    output_file_name = ""
    error_message = ""
    dest = getattr(batch_job, "dest", None)
    if dest and getattr(dest, "file_name", None):
        output_file_name = dest.file_name
    if getattr(batch_job, "error", None):
        error_message = str(batch_job.error)
    return {
        "stored": stored,
        "state": state,
        "output_file_name": output_file_name,
        "error_message": error_message,
    }


def _download_batch_output(output_file_name: str, api_key: str) -> bytes:
    client = genai.Client(api_key=api_key)
    return client.files.download(file=output_file_name)


def refresh_and_ingest_batch_jobs(run_id: Optional[str] = None) -> List[Dict]:
    """Szybko odpytuje wszystkie shardy, potem równolegle pobiera tylko gotowe.

    Dzięki rozdzieleniu status-check od downloadu jeden duży plik wynikowy nie
    blokuje odpytywania pozostałych 40–50 zadań.
    """
    pending = [stored for stored in list_batch_jobs(run_id=run_id) if not stored["ingested_at"]]
    if not pending:
        return []

    api_key = str(st.secrets["GOOGLE_API_KEY"])
    updates_by_job: Dict[str, Dict] = {}
    succeeded_for_download: List[Dict] = []

    # Etap 1: statusy — lekka operacja, więc większa pula workerów.
    with ThreadPoolExecutor(max_workers=min(BATCH_REFRESH_WORKERS, len(pending))) as executor:
        futures = {executor.submit(_refresh_single_batch_remote, stored, api_key): stored for stored in pending}
        for future in as_completed(futures):
            stored = futures[future]
            try:
                result = future.result()
                state = result["state"]
                output_file_name = result["output_file_name"]
                error_message = result["error_message"]
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE batch_jobs SET state=?, output_file_name=?, error_message=?, updated_at=?
                        WHERE job_name=?
                        """,
                        (state, output_file_name, error_message[:2000], utcnow_iso(), stored["job_name"]),
                    )
                updates_by_job[stored["job_name"]] = {
                    "job_name": stored["job_name"], "state": state, "ingested": False
                }
                if state == "JOB_STATE_SUCCEEDED" and output_file_name:
                    succeeded_for_download.append({**stored, "output_file_name": output_file_name})
                elif state in {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}:
                    with db_connect() as conn:
                        conn.execute(
                            """
                            UPDATE meta_jobs SET status='failed', error_message=?, updated_at=?
                            WHERE batch_job_name=? AND status='batch_submitted'
                            """,
                            (error_message or state, utcnow_iso(), stored["job_name"]),
                        )
            except Exception as exc:
                updates_by_job[stored["job_name"]] = {
                    "job_name": stored["job_name"], "state": "STATUS_ERROR", "error": str(exc), "ingested": False
                }

    # Etap 2: tylko gotowe outputy. Downloady lecą równolegle, a ingest następuje
    # od razu po ukończeniu konkretnego pliku.
    if succeeded_for_download:
        with ThreadPoolExecutor(max_workers=min(BATCH_DOWNLOAD_WORKERS, len(succeeded_for_download))) as executor:
            futures = {
                executor.submit(_download_batch_output, row["output_file_name"], api_key): row
                for row in succeeded_for_download
            }
            for future in as_completed(futures):
                row = futures[future]
                update = updates_by_job[row["job_name"]]
                try:
                    content = future.result()
                    update["stats"] = ingest_batch_result_bytes(row["job_name"], content)
                    update["ingested"] = True
                except Exception as exc:
                    update["download_error"] = str(exc)
                    # ingested_at pozostaje pusty — kolejne odświeżenie spróbuje
                    # pobrać ten sam output bez ponownego generowania przez Gemini.

    return sorted(updates_by_job.values(), key=lambda item: item.get("job_name", ""))


# ═══════════════════════════════════════════════════════════════════
# SESSION STATE I UI HELPERY
# ═══════════════════════════════════════════════════════════════════

def init_session_state() -> None:
    defaults = {
        "bulk_results": [],
        "generator_mode": "Generator opisów",
        "bulk_selected_products": {},
        "products_to_send": {},
        "link_active": False,
        "link_only": False,
        "link_url": "",
        "link_category": "",
        "search_res": [],
        "use_research": True,
        "meta_only": False,
        "magento_store_view": "store_view_bookland",
        "backlog_items": [],
        "backlog_category": "",
        "active_meta_run_id": "",
        "last_import_report_path": "",
        "interactive_seed_results": {},
        "last_interactive_checkpoint_path": "",
        "force_regenerate_meta": False,
        "reuse_warning_meta": True,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_session_state()


def reset_interactive_results() -> None:
    st.session_state.bulk_results = []
    st.session_state.interactive_seed_results = {}
    st.session_state.last_interactive_checkpoint_path = ""
    for key in ("interactive_method", "interactive_resume_file", "interactive_existing_checkpoint"):
        st.session_state.pop(key, None)


def get_internal_link() -> Optional[Dict]:
    if (
        st.session_state.get("link_active")
        and st.session_state.get("link_url")
        and st.session_state.get("link_category")
    ):
        return {
            "url": st.session_state["link_url"],
            "category": st.session_state["link_category"],
        }
    return None


def render_result_preview(result: Dict) -> None:
    sku = result["sku"]
    edit_key = f"edit_{sku}"
    description_html = result.get("description_html", "")
    is_meta_only = is_meta_only_result(result)

    if not is_meta_only:
        editor_key = f"visual_editor_{sku}_{hashlib.sha256(description_html.encode()).hexdigest()[:10]}"
        tabs = st.tabs(["Edytuj wizualnie", "Podgląd", "HTML"] + (["Research"] if result.get("research") else []))
        with tabs[0]:
            st.session_state[edit_key] = visual_html_editor(description_html, key=editor_key)
        with tabs[1]:
            st.markdown(st.session_state.get(edit_key, description_html), unsafe_allow_html=True)
        with tabs[2]:
            st.code(st.session_state.get(edit_key, description_html), language="html")
        if result.get("research") and len(tabs) > 3:
            with tabs[3]:
                st.markdown(result["research"])

    if is_meta_only or result.get("meta_title") or result.get("meta_description"):
        with st.expander("Metatagi Magento", expanded=is_meta_only):
            meta_title = result.get("meta_title", "")
            meta_description = result.get("meta_description", "")
            st.text_input("meta_title", value=meta_title, disabled=True, key=f"mt_{sku}")
            st.text_area("meta_description", value=meta_description, disabled=True, height=90, key=f"md_{sku}")
            col1, col2 = st.columns(2)
            col1.caption(f"{len(meta_title)}/{META_TITLE_HARD_MAX} znaków · cel {META_TITLE_TARGET_MIN}-{META_TITLE_TARGET_MAX}")
            col2.caption(f"{len(meta_description)}/160 znaków")
            if result.get("validation_errors"):
                st.warning("; ".join(result["validation_errors"]))


def process_selected_products(
    skus: Sequence[str],
    *,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    meta_only: bool,
    internal_link: Optional[Dict],
    link_only: bool,
    use_research: bool,
    resume_results: Optional[Dict[str, Dict]] = None,
    checkpoint_path: Optional[Path] = None,
    force_regenerate: bool = False,
    include_warning_checkpoints: bool = True,
) -> List[Dict]:
    """Przetwarzanie interaktywne odporne na odświeżenie Streamlita.

    W generatorze metatagów gotowe wyniki są odzyskiwane kolejno z:
    1) checkpointu wgranego przez użytkownika,
    2) serwerowego CSV zapisywanego w trakcie pracy,
    3) SQLite (dla trybu tylko metatagi).

    Jeśli przekazano checkpoint_path, po każdym SKU powstaje atomowy checkpoint CSV.
    """
    ordered_skus = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))
    total = len(ordered_skus)
    if total == 0:
        return []

    results_by_sku: Dict[str, Dict] = {}
    if not force_regenerate:
        for sku, result in (resume_results or {}).items():
            if sku in ordered_skus and is_reusable_result(result, meta_only=meta_only):
                results_by_sku[sku] = result

        if checkpoint_path:
            for sku, result in load_interactive_checkpoint(checkpoint_path).items():
                if sku in ordered_skus and sku not in results_by_sku and is_reusable_result(result, meta_only=meta_only):
                    results_by_sku[sku] = result

        # SQLite przechowuje metatagi po KAŻDYM produkcie, więc odzyskuje nawet
        # rezultat powstały po ostatnim zapisie pliku CSV.
        if meta_only:
            sqlite_results = cached_meta_results_for_skus(
                ordered_skus,
                channel=channel,
                locale=locale,
                include_warnings=include_warning_checkpoints,
            )
            for sku, result in sqlite_results.items():
                if sku not in results_by_sku and is_reusable_result(result, meta_only=True):
                    results_by_sku[sku] = result

    pending = [sku for sku in ordered_skus if sku not in results_by_sku]
    resumed_count = total - len(pending)

    progress = st.progress(
        resumed_count / total,
        f"Wznowiono: {resumed_count}/{total} gotowych · do zrobienia {len(pending)}",
    )
    status_box = st.empty()
    if resumed_count:
        status_box.info(
            f"Pominięto {resumed_count} już zapisanych SKU. Gemini dostanie tylko {len(pending)} pozostałych."
        )

    newly_processed = 0
    max_workers = GEMINI_INTERACTIVE_WORKERS

    for chunk_start in range(0, len(pending), INTERACTIVE_CHUNK_SIZE):
        chunk = pending[chunk_start : chunk_start + INTERACTIVE_CHUNK_SIZE]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            if meta_only:
                futures = {
                    executor.submit(
                        process_product_meta_only,
                        sku,
                        token,
                        channel,
                        locale,
                        store_view_code,
                    ): sku
                    for sku in chunk
                }
            else:
                futures = {
                    executor.submit(
                        process_product_from_akeneo,
                        sku,
                        token,
                        channel,
                        locale,
                        internal_link,
                        link_only,
                        use_research,
                    ): sku
                    for sku in chunk
                }

            for future in as_completed(futures):
                sku = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"sku": sku, "title": "", "error": str(exc), "meta_only": meta_only}
                results_by_sku[sku] = result
                newly_processed += 1
                done_count = resumed_count + newly_processed

                # Checkpoint jest celowo częsty. Przy 500 produktach oznacza ~100
                # małych, atomowych zapisów, ale najwyżej kilka wyników może zostać
                # utraconych przy brutalnym ubiciu procesu.
                if checkpoint_path and newly_processed % INTERACTIVE_CHECKPOINT_EVERY == 0:
                    write_interactive_checkpoint(checkpoint_path, ordered_skus, results_by_sku)

                # Nie wysyłamy wiadomości do przeglądarki po każdym SKU - to zmniejsza
                # obciążenie websocketu Streamlita przy długich przebiegach.
                if newly_processed % INTERACTIVE_UI_UPDATE_EVERY == 0 or done_count == total:
                    progress.progress(
                        done_count / total,
                        f"Gotowe {done_count}/{total} · nowe {newly_processed} · wznowione {resumed_count}",
                    )

        # Twardy checkpoint po każdej małej paczce produktów.
        if checkpoint_path:
            write_interactive_checkpoint(checkpoint_path, ordered_skus, results_by_sku)

    if checkpoint_path:
        write_interactive_checkpoint(checkpoint_path, ordered_skus, results_by_sku)

    progress.progress(1.0, f"Gotowe {total}/{total}")
    if checkpoint_path:
        status_box.success(f"Checkpoint zapisany: {checkpoint_path.name}")

    # Zachowujemy kolejność wejściowego CSV/SKU.
    return [results_by_sku[sku] for sku in ordered_skus if sku in results_by_sku]


# ═══════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════

st.markdown(f'<h1 class="main-header">📚 {APP_NAME}</h1>', unsafe_allow_html=True)
st.markdown(
    f'<p class="sub-header">v{APP_VERSION} · Gemini: {GEMINI_MODEL} · AI meta title + seed i kontrola powtarzalności</p>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Generator")
    generator_mode = st.radio(
        "Wybierz osobny tryb pracy",
        ["Generator opisów", "Generator metatagów"],
        key="generator_mode",
        on_change=reset_interactive_results,
    )
    st.session_state.meta_only = generator_mode == "Generator metatagów"
    st.caption(
        "Tworzy wyłącznie pełne opisy HTML."
        if not st.session_state.meta_only
        else "Tworzy wyłącznie meta title i meta description. Opisy produktów pozostają bez zmian."
    )

    st.markdown("---")
    st.subheader("Ustawienia wspólne")
    channel = st.selectbox("Kanał", [DEFAULT_CHANNEL, "B2B"], index=0)
    locale = st.text_input("Locale", value=DEFAULT_LOCALE)
    st.session_state.magento_store_view = st.text_input(
        "Magento store_view_code",
        value=st.session_state.magento_store_view,
    )

    if not st.session_state.meta_only:
        st.markdown("---")
        st.subheader("Opcje opisów")
        st.session_state.link_active = st.checkbox("Włącz linkowanie", value=st.session_state.link_active)
        st.session_state.link_only = st.checkbox(
            "Tylko dodaj link - bez przepisywania opisu",
            value=st.session_state.link_only,
        )
        st.session_state.link_url = st.text_input("URL linku", value=st.session_state.link_url)
        st.session_state.link_category = st.text_input("Kategoria / anchor hint", value=st.session_state.link_category)
        st.session_state.use_research = st.checkbox(
            "Wzbogacaj opisy researchem Perplexity",
            value=st.session_state.use_research,
        )
        if st.session_state.use_research and "PERPLEXITY_API_KEY" not in st.secrets:
            st.caption("Brak PERPLEXITY_API_KEY - research będzie pomijany.")

        st.markdown("---")
        st.metric("Opisane produkty", optimized_products_count())
        if st.button("Wyczyść licznik opisanych produktów"):
            clear_optimized_products()
            st.rerun()

interactive_tab, scale_tab, results_tab = st.tabs(
    [generator_mode, "Metatagi 50k / Batch API", "Kontrola metatagów"]
)

with interactive_tab:
    st.header(generator_mode)
    st.info(
        "Ten generator tworzy pełne opisy HTML. Metatagi są generowane w osobnym trybie."
        if not st.session_state.meta_only
        else "Ten generator nie tworzy ani nie zmienia opisów produktów."
    )
    st.subheader("Wybór produktów")
    file_method = "CSV / TXT z checkpointem" if st.session_state.meta_only else "CSV / TXT ze SKU"
    method = st.radio(
        "Metoda",
        ["Wyszukaj", "Wklej SKU lub URL", file_method, "Backlog"],
        horizontal=True,
        key="interactive_method",
    )

    if method == "Wyszukaj":
        col_q, col_l = st.columns([4, 1])
        query = col_q.text_input("Szukaj produktu")
        limit = col_l.number_input("Limit", 5, 50, 10)
        if st.button("Szukaj", type="primary") and query:
            try:
                st.session_state.search_res = akeneo_search_products(query, akeneo_get_token(), int(limit), locale)
            except Exception as exc:
                st.error(str(exc))
        if st.session_state.search_res:
            st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
            for product in st.session_state.search_res:
                sku = product["identifier"]
                selected = sku in st.session_state.bulk_selected_products
                if st.checkbox(f"{sku} - {product['title']}", value=selected, key=f"search_{sku}"):
                    st.session_state.bulk_selected_products[sku] = {"title": product["title"]}
                elif selected:
                    st.session_state.bulk_selected_products.pop(sku, None)
            st.markdown("</div>", unsafe_allow_html=True)

    elif method == "Wklej SKU lub URL":
        raw_text = st.text_area("SKU lub URL - jeden na linię", height=170)
        if st.button("Załaduj produkty", type="primary"):
            raw_inputs = [line.strip() for line in raw_text.splitlines() if line.strip()]
            try:
                resolved, input_errors = resolve_product_inputs(raw_inputs)
                url_items = [item for item in resolved if item["source"] == "url"]
                statuses: Dict[str, Optional[bool]] = {}
                if url_items:
                    token = akeneo_get_token()
                    unique_skus = list(dict.fromkeys(item["sku"] for item in url_items))
                    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
                        futures = {executor.submit(akeneo_product_exists, sku, token): sku for sku in unique_skus}
                        for future in as_completed(futures):
                            sku = futures[future]
                            try:
                                statuses[sku] = future.result()
                            except Exception:
                                statuses[sku] = None
                accepted = 0
                for item in resolved:
                    if item["source"] == "url" and statuses.get(item["sku"]) is not True:
                        input_errors.append(f"Nie zweryfikowano SKU z URL: {item['input']}")
                        continue
                    st.session_state.bulk_selected_products[item["sku"]] = {"title": item.get("title", item["sku"])}
                    accepted += 1
                if accepted:
                    st.success(f"Dodano {accepted} produktów.")
                if input_errors:
                    st.warning("\n".join(f"- {error}" for error in input_errors))
            except ProductInputResolutionError as exc:
                st.error(str(exc))

    elif method == file_method:
        if st.session_state.meta_only:
            st.caption(
                "Wgraj zwykły plik z kolumną SKU albo wcześniejszy checkpoint CSV. "
                "SKU z gotowymi metatagami zostaną pominięte."
            )
            INTERACTIVE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            existing_checkpoint_files = sorted(
                INTERACTIVE_CHECKPOINT_DIR.glob("interactive-*.csv"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:10]
            if existing_checkpoint_files:
                checkpoint_options = {"": "— wybierz checkpoint —"}
                for checkpoint_file in existing_checkpoint_files:
                    try:
                        checkpoint_df = pd.read_csv(checkpoint_file, dtype=str, keep_default_na=False)
                        done_count = int((checkpoint_df.get("checkpoint_status", pd.Series(dtype=str)).isin(["completed", "warning"])).sum())
                        total_count = len(checkpoint_df)
                    except Exception:
                        done_count, total_count = 0, 0
                    checkpoint_options[str(checkpoint_file)] = (
                        f"{checkpoint_file.name} · {done_count}/{total_count} gotowych"
                    )
                selected_existing_checkpoint = st.selectbox(
                    "Wznów checkpoint zapisany na serwerze",
                    options=list(checkpoint_options),
                    format_func=lambda value: checkpoint_options[value],
                    key="interactive_existing_checkpoint",
                )
                if selected_existing_checkpoint and st.button("Wczytaj checkpoint z serwera", key="load_server_checkpoint"):
                    checkpoint_file = Path(selected_existing_checkpoint)
                    file_skus, seed_results, _ = parse_resumable_product_file(checkpoint_file.read_bytes())
                    st.session_state.bulk_selected_products = {sku: {"title": sku} for sku in file_skus}
                    st.session_state.interactive_seed_results = seed_results
                    st.session_state.last_interactive_checkpoint_path = str(checkpoint_file)
                    st.success(f"Wczytano {len(file_skus)} SKU; gotowe: {len(seed_results)}.")
                    st.rerun()
        else:
            st.caption("Wgraj CSV, TSV lub TXT z kolumną SKU. Wszystkie produkty zostaną wygenerowane od nowa.")

        resume_file = st.file_uploader(
            "CSV / TSV / TXT",
            type=["csv", "tsv", "txt"],
            key="interactive_resume_file",
        )
        if resume_file is not None:
            try:
                file_skus, seed_results, file_errors = parse_resumable_product_file(resume_file.getvalue())
                if file_skus:
                    st.info(f"Plik: {len(file_skus):,} SKU".replace(",", " "))
                    if st.button("Załaduj plik do kolejki", type="primary", key="load_interactive_resume_file"):
                        st.session_state.bulk_selected_products = {sku: {"title": sku} for sku in file_skus}
                        st.session_state.interactive_seed_results = seed_results if st.session_state.meta_only else {}
                        message = f"Załadowano {len(file_skus):,} SKU."
                        if st.session_state.meta_only:
                            message += f" {len(seed_results):,} ma już metatagi i zostanie pominiętych."
                        st.success(message.replace(",", " "))
                        st.rerun()
                else:
                    st.warning("Nie znaleziono SKU w pliku.")
                if file_errors:
                    st.warning("\n".join(file_errors))
            except Exception as exc:
                st.error(f"Nie udało się odczytać pliku: {exc}")

    else:
        try:
            token = akeneo_get_token()
            categories = akeneo_fetch_categories(token, locale)
            category_options = {"": "Wszystkie kategorie"}
            category_options.update({item["code"]: f"{item['label']} ({item['code']})" for item in categories})
            selected_category = st.selectbox(
                "Kategoria",
                options=list(category_options),
                format_func=lambda code: category_options[code],
            )
            col1, col2 = st.columns(2)
            backlog_limit = col1.number_input("Ile załadować", 10, 500, 100, 10)
            exclude_days = col2.number_input("Pomiń aktualizowane przez N dni", 0, 365, 30)
            only_no_desc = st.checkbox("Tylko bez opisu")
            max_len = st.number_input("Maksymalna długość opisu, 0 = bez limitu", 0, 10000, 0, 50)
            if st.button("Załaduj backlog", type="primary"):
                st.session_state.backlog_items = akeneo_fetch_backlog(
                    token,
                    channel,
                    locale,
                    int(backlog_limit),
                    category=selected_category or None,
                    exclude_updated_days=int(exclude_days) or None,
                    only_without_desc=only_no_desc,
                    max_desc_len=int(max_len) if max_len and not only_no_desc else None,
                )
            if st.session_state.backlog_items:
                n_select = st.number_input(
                    "Zaznacz pierwszych N",
                    1,
                    len(st.session_state.backlog_items),
                    min(10, len(st.session_state.backlog_items)),
                )
                if st.button("Zaznacz pierwsze N"):
                    for product in st.session_state.backlog_items[: int(n_select)]:
                        st.session_state.bulk_selected_products[product["identifier"]] = {"title": product["title"]}
                    st.rerun()
                st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
                for product in st.session_state.backlog_items:
                    sku = product["identifier"]
                    selected = sku in st.session_state.bulk_selected_products
                    label = f"{sku} - {product['title']} ({product['desc_len']} zn.)"
                    if st.checkbox(label, value=selected, key=f"backlog_{sku}"):
                        st.session_state.bulk_selected_products[sku] = {"title": product["title"]}
                    elif selected:
                        st.session_state.bulk_selected_products.pop(sku, None)
                st.markdown("</div>", unsafe_allow_html=True)
        except Exception as exc:
            st.error(str(exc))

    if st.session_state.bulk_selected_products:
        st.info(f"W kolejce: {len(st.session_state.bulk_selected_products)} produktów")
        col_clear, _ = st.columns([1, 4])
        if col_clear.button("Wyczyść kolejkę"):
            st.session_state.bulk_selected_products = {}
            st.session_state.interactive_seed_results = {}
            st.session_state.last_interactive_checkpoint_path = ""
            st.rerun()

        st.markdown("---")
        st.subheader("Generowanie opisów" if not st.session_state.meta_only else "Generowanie metatagów")
        if st.session_state.meta_only:
            col_resume_a, col_resume_b = st.columns(2)
            force_regenerate = col_resume_a.checkbox(
                "Wymuś generowanie od zera",
                key="force_regenerate_meta",
                help="Włącz tylko, jeśli chcesz ponownie wygenerować już gotowe metatagi.",
            )
            include_warning_checkpoints = col_resume_b.checkbox(
                "Przy wznowieniu akceptuj też wyniki z ostrzeżeniem",
                key="reuse_warning_meta",
                help="Ostrzeżenie jakościowe nie oznacza błędu API.",
            )
        else:
            force_regenerate = True
            include_warning_checkpoints = False
            st.caption("Każde uruchomienie tworzy wszystkie wybrane opisy od nowa.")

        checkpoint_path: Optional[Path] = None
        if st.session_state.meta_only:
            skus_for_checkpoint = list(st.session_state.bulk_selected_products)
            checkpoint_key = interactive_checkpoint_key(
                skus_for_checkpoint,
                channel=channel,
                locale=locale,
                store_view_code=st.session_state.magento_store_view,
            )
            checkpoint_path = interactive_checkpoint_path(checkpoint_key)
            st.session_state.last_interactive_checkpoint_path = str(checkpoint_path)
            seed_results = set(st.session_state.get("interactive_seed_results", {}))
            server_results = set(load_interactive_checkpoint(checkpoint_path))
            try:
                sqlite_results = set(
                    cached_meta_results_for_skus(
                        skus_for_checkpoint, channel=channel, locale=locale,
                        include_warnings=include_warning_checkpoints,
                    )
                )
            except Exception:
                sqlite_results = set()
            reusable_estimate = 0 if force_regenerate else min(
                len(skus_for_checkpoint), len(seed_results | server_results | sqlite_results)
            )
            st.caption(
                f"Checkpoint: {checkpoint_path.name} · rozpoznane gotowe SKU: około {reusable_estimate}/{len(skus_for_checkpoint)} "
                f"(plik wgrany {len(seed_results)}, serwer CSV {len(server_results)}, SQLite {len(sqlite_results)})."
            )
        else:
            st.session_state.last_interactive_checkpoint_path = ""
        st.caption(
            f"Tryb stabilny v{APP_VERSION}: paczki po {INTERACTIVE_CHUNK_SIZE}, {GEMINI_INTERACTIVE_WORKERS} równoległe workery, "
            f"timeout Gemini {GEMINI_HTTP_TIMEOUT_MS // 1000}s. "
            + (
                "Checkpoint jest zapisywany po każdym SKU."
                if st.session_state.meta_only else "Generator opisów nie używa checkpointów."
            )
        )

        if checkpoint_path and checkpoint_path.exists():
            st.download_button(
                "Pobierz bieżący checkpoint CSV",
                checkpoint_path.read_bytes(),
                file_name=checkpoint_path.name,
                mime="text/csv",
                key="download_interactive_checkpoint_before_run",
            )

        start_label = "Generuj opisy od nowa" if not st.session_state.meta_only else "Start / wznów generowanie metatagów"
        if st.button(start_label, type="primary"):
            skus = list(st.session_state.bulk_selected_products)
            try:
                st.session_state.bulk_results = process_selected_products(
                    skus,
                    token=akeneo_get_token(),
                    channel=channel,
                    locale=locale,
                    store_view_code=st.session_state.magento_store_view,
                    meta_only=st.session_state.meta_only,
                    internal_link=get_internal_link() if not st.session_state.meta_only else None,
                    link_only=st.session_state.link_only if not st.session_state.meta_only else False,
                    use_research=st.session_state.use_research if not st.session_state.meta_only else False,
                    resume_results=(
                        st.session_state.get("interactive_seed_results", {})
                        if st.session_state.meta_only else {}
                    ),
                    checkpoint_path=checkpoint_path,
                    force_regenerate=force_regenerate,
                    include_warning_checkpoints=include_warning_checkpoints,
                )
                st.session_state.products_to_send = {
                    result["sku"]: True for result in st.session_state.bulk_results if not result.get("error")
                }
                st.session_state.interactive_seed_results = (
                    {
                        result["sku"]: result
                        for result in st.session_state.bulk_results
                        if is_reusable_result(result, meta_only=True)
                    }
                    if st.session_state.meta_only else {}
                )
            except Exception as exc:
                st.error(str(exc))

    if st.session_state.bulk_results:
        results = st.session_state.bulk_results
        ok = [item for item in results if not item.get("error")]
        errors = [item for item in results if item.get("error")]
        col_ok, col_err = st.columns(2)
        col_ok.metric("Poprawne", len(ok))
        col_err.metric("Błędy / do kontroli", len(errors))

        data_frame = pd.DataFrame(results)
        st.download_button(
            "Pobierz opisy CSV" if not st.session_state.meta_only else "Pobierz metatagi CSV",
            data_frame.to_csv(index=False).encode("utf-8-sig"),
            "opisy_produkty.csv" if not st.session_state.meta_only else "metatagi_produkty.csv",
            "text/csv",
        )

        checkpoint_path_value = st.session_state.get("last_interactive_checkpoint_path", "")
        if st.session_state.meta_only and checkpoint_path_value and Path(checkpoint_path_value).exists():
            checkpoint_file = Path(checkpoint_path_value)
            st.download_button(
                "Pobierz checkpoint do późniejszego wznowienia",
                checkpoint_file.read_bytes(),
                file_name=checkpoint_file.name,
                mime="text/csv",
                key="download_interactive_checkpoint_after_run",
            )
            st.caption(
                "Ten plik możesz później wgrać w trybie „CSV / TXT z checkpointem”. "
                "Wiersze z gotowymi metatagami zostaną pominięte."
            )

        if ok and not any(item.get("meta_only") for item in ok):
            st.subheader("Wysyłka opisów do Akeneo")
            to_send: List[Dict] = []
            for item in ok:
                checked = st.checkbox(
                    f"{item['sku']} - {item['title']}",
                    value=st.session_state.products_to_send.get(item["sku"], True),
                    key=f"send_{item['sku']}",
                )
                st.session_state.products_to_send[item["sku"]] = checked
                if checked:
                    to_send.append(item)
            if st.button(f"Wyślij zaznaczone ({len(to_send)})", type="primary"):
                progress = st.progress(0)
                sent = 0
                send_errors = []
                for index, item in enumerate(to_send, start=1):
                    try:
                        final_html = st.session_state.get(f"edit_{item['sku']}", item["description_html"])
                        akeneo_update_description(item["sku"], final_html, channel, locale)
                        add_optimized_product(item["sku"], item["title"], item["url"])
                        sent += 1
                    except Exception as exc:
                        send_errors.append(f"{item['sku']}: {exc}")
                    progress.progress(index / max(len(to_send), 1))
                st.success(f"Wysłano {sent} opisów.")
                if send_errors:
                    st.error("\n".join(send_errors))

        st.subheader("Podgląd wyników")
        for result in results[:RESULT_PREVIEW_LIMIT]:
            label = "✅" if not result.get("error") else "⚠️"
            with st.expander(f"{label} {result['sku']} - {result.get('title', '')}"):
                if result.get("error"):
                    st.error(result["error"])
                render_result_preview(result)
        if len(results) > RESULT_PREVIEW_LIMIT:
            st.caption(f"Wyświetlono pierwsze {RESULT_PREVIEW_LIMIT} wyników, aby nie przeciążać Streamlita.")

with scale_tab:
    st.subheader("Metatagi dla dużej listy SKU: trwała kolejka + Gemini Batch API")
    st.write(
        "Tutaj możesz wkleić lub wgrać nawet dziesiątki tysięcy SKU. Produkty są pobierane z Akeneo "
        "paczkami, zapisywane w SQLite i dopiero potem wysyłane do Gemini Batch API. Lista nie trafia "
        "do interaktywnego session_state jako 50 tys. osobnych elementów."
    )

    try:
        token = akeneo_get_token()
        source_mode = st.radio(
            "Źródło produktów",
            ["Lista SKU", "Cały katalog lub kategoria"],
            horizontal=True,
            key="scale_source_mode",
        )

        col_a, col_b = st.columns(2)
        enabled_only = col_a.checkbox("Tylko aktywne produkty", value=True, key="scale_enabled_only")
        only_with_description = col_b.checkbox(
            "Tylko produkty z opisem źródłowym",
            value=True,
            key="scale_only_with_description",
        )
        force_regenerate = st.checkbox(
            "Wygeneruj ponownie także niezmienione, ukończone produkty",
            value=False,
            key="scale_force_regenerate",
        )

        if source_mode == "Lista SKU":
            st.caption(
                "Obsługiwany format: jeden SKU na linię albo CSV/TSV z kolumną sku, identifier, product_sku lub kod produktu."
            )
            uploaded_skus = st.file_uploader(
                "Plik SKU",
                type=["txt", "csv", "tsv"],
                key="scale_sku_file",
            )
            pasted_skus = st.text_area(
                "Albo wklej SKU - jeden na linię",
                height=180,
                key="scale_sku_text",
            )

            payload_parts: List[str] = []
            if uploaded_skus is not None:
                raw_bytes = uploaded_skus.getvalue()
                try:
                    payload_parts.append(raw_bytes.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    payload_parts.append(raw_bytes.decode("cp1250", errors="replace"))
            if pasted_skus.strip():
                payload_parts.append(pasted_skus)
            parsed_skus, duplicate_count = parse_bulk_sku_payload("\n".join(payload_parts))
            if parsed_skus:
                st.info(
                    f"Rozpoznano {len(parsed_skus):,} unikalnych SKU".replace(",", " ")
                    + (f"; usunięto {duplicate_count:,} duplikatów".replace(",", " ") if duplicate_count else "")
                )

            if st.button("1. Pobierz wskazane SKU i przygotuj kolejkę", type="primary"):
                if not parsed_skus:
                    st.error("Nie znaleziono żadnych poprawnych SKU w polu ani w pliku.")
                else:
                    run_id = new_run_id("sku")
                    progress_text = st.empty()

                    def sku_callback(stats: Dict[str, object]) -> None:
                        progress_text.info(
                            f"Sprawdzono {int(stats['processed']):,}/{int(stats['unique']):,} · "
                            f"znaleziono {int(stats['found']):,} · w kolejce {int(stats['queued']):,} · "
                            f"brak {int(stats['missing']):,}".replace(",", " ")
                        )

                    stats = import_skus_to_meta_queue(
                        skus=parsed_skus,
                        token=token,
                        channel=channel,
                        locale=locale,
                        store_view_code=st.session_state.magento_store_view,
                        enabled_only=enabled_only,
                        only_with_description=only_with_description,
                        force_regenerate=force_regenerate,
                        run_id=run_id,
                        progress_callback=sku_callback,
                    )
                    st.session_state.active_meta_run_id = run_id
                    st.session_state.last_import_report_path = str(stats.get("report_path", ""))
                    st.success(
                        f"Import {run_id}: {int(stats['queued']):,} produktów w kolejce, "
                        f"{int(stats['skipped_unchanged']):,} już gotowych i niezmienionych, "
                        f"{int(stats['missing']):,} nie znaleziono, "
                        f"{int(stats['inactive']):,} nieaktywnych, "
                        f"{int(stats['without_description']):,} bez opisu.".replace(",", " ")
                    )

        else:
            categories = akeneo_fetch_categories(token, locale)
            category_options = {"": "Wszystkie kategorie"}
            category_options.update({item["code"]: f"{item['label']} ({item['code']})" for item in categories})
            selected_category = st.selectbox(
                "Kategoria katalogu",
                options=list(category_options),
                format_func=lambda code: category_options[code],
                key="scale_category",
            )
            max_products_input = st.number_input(
                "Limit importu, 0 = cały katalog",
                min_value=0,
                max_value=500000,
                value=0,
                step=1000,
            )
            if st.button("1. Pobierz katalog i przygotuj kolejkę", type="primary"):
                run_id = new_run_id("catalog")
                progress_text = st.empty()

                def catalog_callback(stats: Dict[str, int]) -> None:
                    progress_text.info(
                        f"Odczytano {stats['seen']} · w kolejce {stats['queued']} · "
                        f"pominięto niezmienione {stats['skipped_unchanged']}"
                    )

                stats = import_catalog_to_meta_queue(
                    token=token,
                    channel=channel,
                    locale=locale,
                    store_view_code=st.session_state.magento_store_view,
                    category=selected_category or None,
                    enabled_only=enabled_only,
                    only_with_description=only_with_description,
                    max_products=int(max_products_input) or None,
                    force_regenerate=force_regenerate,
                    run_id=run_id,
                    progress_callback=catalog_callback,
                )
                st.session_state.active_meta_run_id = run_id
                st.success(
                    f"Import {run_id}: odczytano {stats['seen']}, dodano do kolejki {stats['queued']}, "
                    f"pominięto niezmienione {stats['skipped_unchanged']}."
                )

        runs = list_meta_runs()
        run_ids = [row["run_id"] for row in runs]
        active_run = st.session_state.get("active_meta_run_id", "")
        if run_ids:
            default_index = run_ids.index(active_run) if active_run in run_ids else 0
            selected_run = st.selectbox(
                "Aktywna partia",
                options=run_ids,
                index=default_index,
                format_func=lambda run_id: next(
                    (
                        f"{run_id} · {row['product_count']} produktów · {row['source_type']}"
                        for row in runs if row["run_id"] == run_id
                    ),
                    run_id,
                ),
                key="scale_active_run_select",
            )
            st.session_state.active_meta_run_id = selected_run
        else:
            selected_run = ""

        report_path_value = st.session_state.get("last_import_report_path", "")
        if report_path_value and Path(report_path_value).exists():
            st.download_button(
                "Pobierz raport SKU odrzuconych przy imporcie",
                Path(report_path_value).read_bytes(),
                Path(report_path_value).name,
                "text/tab-separated-values",
            )

        if selected_run:
            counts = meta_status_counts(selected_run)
            metrics = st.columns(5)
            metrics[0].metric("W kolejce", counts.get("queued", 0))
            metrics[1].metric("W batchach", counts.get("batch_submitted", 0))
            metrics[2].metric("Gotowe", counts.get("completed", 0))
            metrics[3].metric("Do poprawy", counts.get("validation_failed", 0))
            metrics[4].metric("Błędy", counts.get("failed", 0))

            queued_count = int(counts.get("queued", 0))
            recommended_size = recommended_batch_shard_size(queued_count)
            auto_shard = st.checkbox(
                "Automatyczny Turbo Sharding (zalecane)",
                value=True,
                help="Dla dużych runów celuje w ok. 1000 SKU/shard. 50k ≈ 50 niezależnych jobs zamiast jednego wielkiego zadania.",
            )
            if auto_shard:
                products_per_batch = recommended_size
                st.text_input("Rozmiar sharda", value=str(products_per_batch), disabled=True)
            else:
                products_per_batch = st.number_input(
                    "Rozmiar sharda Batch API",
                    min_value=250,
                    max_value=5000,
                    value=BATCH_PRODUCTS_PER_FILE,
                    step=250,
                )

            shard_count = (queued_count + int(products_per_batch) - 1) // int(products_per_batch) if queued_count else 0
            active_jobs = active_batch_job_count(selected_run)
            estimate = estimate_batch_input_tokens(selected_run, queued_count)
            plan_cols = st.columns(4)
            plan_cols[0].metric("SKU do wysłania", queued_count)
            plan_cols[1].metric("Planowane shardy", shard_count)
            plan_cols[2].metric("Aktywne jobs", active_jobs)
            plan_cols[3].metric(
                "Szac. input",
                f"{estimate['estimated_input_tokens'] / 1_000_000:.1f}M tok." if estimate['estimated_input_tokens'] else "0",
            )
            st.caption(
                "Batch Turbo v4.6: ok. 1000 SKU/shard, do 12 równoległych uploadów, soft cap 80 aktywnych jobs, "
                "20 równoległych status-checków i 8 downloadów. Gotowe shardy są odbierane niezależnie. "
                "Gemini 3.5 Flash-Lite ma domyślny minimal thinking; nie dokładamy zbędnych parametrów JSONL."
            )

            col_submit, col_refresh, col_repair = st.columns(3)
            if col_submit.button("2. Wyślij wszystkie możliwe shardy", type="primary"):
                with st.spinner("Buduję JSONL i równolegle wysyłam shardy..."):
                    submitted = submit_queued_batches(int(products_per_batch), run_id=selected_run)
                if submitted:
                    sent_products = sum(item.get("products", 0) for item in submitted if item.get("job_name"))
                    sent_jobs = sum(1 for item in submitted if item.get("job_name"))
                    st.success(f"Wysłano {sent_jobs} jobs dla {sent_products:,} produktów.".replace(",", " "))
                    st.dataframe(pd.DataFrame(submitted), use_container_width=True)
                else:
                    st.info("Ta partia nie ma produktów ze statusem queued.")

            if col_refresh.button("3. Odbierz wszystkie gotowe shardy"):
                with st.spinner("Równolegle sprawdzam statusy i pobieram tylko gotowe outputy..."):
                    updates = refresh_and_ingest_batch_jobs(run_id=selected_run)
                if updates:
                    ingested_now = sum(1 for row in updates if row.get("ingested"))
                    st.success(f"Odebrano teraz {ingested_now} gotowych shardów.")
                    st.dataframe(pd.DataFrame(updates), use_container_width=True)
                else:
                    st.info("Brak nieodebranych zadań batch w tej partii.")

            repair_count = int(counts.get("validation_failed", 0))
            if col_repair.button(f"4. Przygotuj repair batch ({repair_count})", disabled=repair_count == 0):
                moved = requeue_jobs(
                    ["validation_failed"],
                    "Repair batch po walidacji",
                    run_id=selected_run,
                )
                st.success(f"Do repair queue przeniesiono {moved} produktów. Kliknij ponownie wysyłkę shardów.")

            batch_rows = list_batch_jobs(run_id=selected_run)
            if batch_rows:
                st.subheader("Zadania Batch API aktywnej partii")
                batch_df = pd.DataFrame(batch_rows)
                st.dataframe(
                    batch_df[["display_name", "state", "product_count", "created_at", "ingested_at", "error_message"]],
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info("Najpierw utwórz partię z listy SKU albo z katalogu.")
    except Exception as exc:
        st.error(str(exc))

with results_tab:
    st.subheader("Kontrola jakości i eksport")
    result_runs = list_meta_runs()
    result_run_ids = [row["run_id"] for row in result_runs]
    result_run = st.selectbox(
        "Partia do kontroli",
        options=[""] + result_run_ids,
        format_func=lambda value: "Wszystkie partie" if not value else value,
        index=(result_run_ids.index(st.session_state.active_meta_run_id) + 1)
        if st.session_state.get("active_meta_run_id") in result_run_ids else 0,
        key="results_run_filter",
    )
    counts = meta_status_counts(result_run or None)
    st.json(counts)

    st.caption("v4.6: podobne początki i duplikaty meta description nie blokują eksportu. Poziomy meta title są sprawdzane przede wszystkim względem nazwy produktu, nie linków i opisów innych wariantów.")
    col_revalidate, col_audit, col_requeue = st.columns(3)
    if col_revalidate.button("Przelicz walidację v4.6 bez AI"):
        audit = revalidate_meta_jobs_v44(run_id=result_run or None)
        st.success(
            f"Sprawdzono {audit['checked']} produktów. Bez regenerowania zaakceptowano {audit['promoted']}; "
            f"nadal wymagają poprawy {audit['still_failed']}."
        )

    if col_audit.button("Audytuj duplikaty i błędy krytyczne"):
        audit = audit_meta_jobs_for_repetition(run_id=result_run or None)
        st.success(f"Sprawdzono {audit['checked']} produktów. Do poprawy oznaczono {audit['flagged']} (tylko meta title); identyczne meta description: {audit.get('duplicate_descriptions', 0)} — tylko informacyjnie.")

    if col_requeue.button("Przenieś błędne do kolejki"):
        count = requeue_jobs(
            ["validation_failed", "failed"],
            "Ponowienie po kontroli jakości",
            run_id=result_run or None,
        )
        st.success(f"Do kolejki wróciło {count} produktów.")

    completed_count = counts.get("completed", 0)
    if completed_count:
        st.download_button(
            f"Pobierz CSV Magento - {completed_count} poprawnych produktów",
            export_meta_csv(["completed"], run_id=result_run or None),
            "magento_metatagi.tsv",
            "text/tab-separated-values",
        )
    st.download_button(
        "Pobierz raport jakości CSV",
        export_quality_report_csv(run_id=result_run or None),
        "raport_jakosci_metatagow.csv",
        "text/csv",
    )

    status_filter = st.multiselect(
        "Statusy do podglądu",
        ["completed", "validation_failed", "failed", "queued", "batch_submitted"],
        default=["validation_failed", "failed"],
    )
    preview_jobs = list_meta_jobs(
        statuses=status_filter or None,
        limit=500,
        run_id=result_run or None,
    )
    if preview_jobs:
        preview_df = pd.DataFrame(preview_jobs)
        visible_columns = [
            "sku",
            "status",
            "meta_title",
            "meta_description",
            "opening_mode",
            "opening_signature",
            "attempts",
            "validation_errors",
            "error_message",
        ]
        st.dataframe(preview_df[visible_columns], use_container_width=True, hide_index=True)

st.markdown("---")
st.caption(
    f"{APP_NAME} v{APP_VERSION} · prompt {PROMPT_VERSION} · model {GEMINI_MODEL}"
)
