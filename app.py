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
from streamlit_quill import st_quill

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

APP_VERSION = "4.1.2"
APP_NAME = "Generator opisów i metatagów produktów"
PROMPT_VERSION = "meta-v4-seeded-diversity-2026-08-penalty-free"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_CHANNEL = "Bookland"
DEFAULT_LOCALE = "pl_PL"

AKENEO_TIMEOUT = 45
PERPLEXITY_TIMEOUT = 45
AKENEO_MAX_WORKERS = 4
GEMINI_INTERACTIVE_WORKERS = 3
INTERACTIVE_CHUNK_SIZE = 100
BATCH_PRODUCTS_PER_FILE = 5000
AKENEO_SKU_FILTER_CHUNK_SIZE = 50
MAX_META_RETRIES = 2
RESULT_PREVIEW_LIMIT = 200

DB_PATH = Path(".streamlit/product_workflow.sqlite3")
BATCH_DIR = Path(".streamlit/gemini_batches")
IMPORT_REPORT_DIR = Path(".streamlit/import_reports")

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

# Celowo używamy prostego podzbioru JSON Schema zgodnego także ze starszymi
# wersjami endpointu generateContent i pakietu google-genai. Pole
# additionalProperties bywa serializowane jako additional_properties i powoduje
# błąd 400 INVALID_ARGUMENT. Nie jest tu potrzebne: aplikacja odczytuje wyłącznie
# wymagane pole meta_description i ignoruje ewentualne dodatkowe klucze.
META_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_description": {
            "type": "string",
            "description": "Unikalny polski meta description produktu, 140-160 znaków, bez CTA.",
        }
    },
    "required": ["meta_description"],
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


def normalize_quill_html(text: str) -> str:
    text = re.sub(r"<strong>(.*?)</strong>", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r' (class|style|data-[^=]*)="[^"]*"', "", text)
    return text.strip()


def validate_description_quality(description: str) -> Tuple[str, str]:
    length = len(strip_html(description))
    if length == 0:
        return "error", "Brak oryginalnego opisu w Akeneo"
    if length < 100:
        return "error", f"Opis bardzo krótki ({length} zn.)"
    if length < 300:
        return "warning", f"Opis krótki ({length} zn.)"
    return "ok", "Opis OK"


def build_meta_title(title: str, author: str, max_chars: int = 60) -> str:
    title = normalize_spaces(strip_html(title)).strip('"„”')
    author = normalize_spaces(strip_html(author)).strip('"„”')
    if not title:
        return ""

    candidates: List[str] = []
    if author:
        candidates.append(f"{title} - {author}")
    candidates.append(title)

    main_title = re.split(r"\s[:|]\s|:\s|\s[-–—]\s", title, maxsplit=1)[0].strip()
    if main_title and main_title != title:
        if author:
            candidates.append(f"{main_title} - {author}")
        candidates.append(main_title)

    if author:
        author_parts = author.split()
        if len(author_parts) > 1:
            abbreviated = " ".join([f"{part[0]}." for part in author_parts[:-1] if part] + [author_parts[-1]])
            candidates.append(f"{main_title or title} - {abbreviated}")

    for candidate in candidates:
        candidate = normalize_spaces(candidate)
        if len(candidate) <= max_chars:
            return candidate

    suffix = f" - {author}" if author and len(author) <= 24 else ""
    available = max_chars - len(suffix)
    if available < 20:
        suffix = ""
        available = max_chars
    shortened = smart_truncate(main_title or title, available)
    return normalize_spaces(f"{shortened}{suffix}")[:max_chars]


# ═══════════════════════════════════════════════════════════════════
# WALIDACJA METATAGÓW I OCHRONA PRZED POWTARZALNYM SCHEMATEM
# ═══════════════════════════════════════════════════════════════════

def meta_validation_errors(
    meta_description: str,
    *,
    existing_long_signatures: Optional[Set[str]] = None,
    existing_short_signatures: Optional[Counter] = None,
    recent_descriptions: Optional[Sequence[str]] = None,
) -> List[str]:
    errors: List[str] = []
    text = normalize_spaces(strip_html(meta_description)).strip('"„”')
    normalized = normalize_for_compare(text)

    if not text:
        return ["Brak meta description"]
    if len(text) < 140:
        errors.append(f"Za krótki meta description: {len(text)} zn.")
    if len(text) > 160:
        errors.append(f"Za długi meta description: {len(text)} zn.")

    if any(normalized.startswith(normalize_for_compare(prefix)) for prefix in BANNED_STARTERS):
        errors.append("Zakazane lub szablonowe otwarcie")
    if any(normalize_for_compare(phrase) in normalized for phrase in BANNED_CTA_PHRASES):
        errors.append("Meta description zawiera CTA lub język sklepu")
    if re.search(r"https?://|www\.", text, flags=re.IGNORECASE):
        errors.append("Meta description zawiera URL")
    if "..." in text or text.endswith("…"):
        errors.append("Meta description kończy się wielokropkiem")
    if re.search(r"\b(xyz|lorem|ipsum|placeholder)\b", normalized):
        errors.append("Meta description zawiera placeholder")

    long_sig = opening_signature(text, 6)
    short_sig = opening_signature(text, 3)
    if existing_long_signatures and long_sig in existing_long_signatures:
        errors.append("Powtórzone pierwsze 6 słów")
    if existing_short_signatures and existing_short_signatures.get(short_sig, 0) >= 3:
        errors.append("Trzywyrazowy schemat otwarcia został już wykorzystany co najmniej 3 razy")

    if recent_descriptions:
        prefix = normalize_for_compare(text)[:90]
        for other in recent_descriptions:
            other_prefix = normalize_for_compare(other)[:90]
            if prefix and other_prefix and SequenceMatcher(None, prefix, other_prefix).ratio() >= 0.86:
                errors.append("Początek zbyt podobny do innego meta description")
                break

    return list(dict.fromkeys(errors))


def audit_meta_jobs_for_repetition(run_id: Optional[str] = None) -> Dict[str, int]:
    """Oznacza powtarzalne otwarcia i identyczne meta descriptions do ponownej generacji."""
    jobs = list_meta_jobs(
        statuses=["completed"],
        order_by="created_at ASC",
        run_id=run_id,
    )
    seen_long: Dict[str, str] = {}
    seen_short: Dict[str, int] = Counter()
    seen_hash: Dict[str, str] = {}
    flagged: Dict[str, List[str]] = {}

    for job in jobs:
        long_sig = job.get("opening_signature", "")
        short_sig = job.get("short_opening_signature", "")
        norm_hash = job.get("normalized_hash", "")
        reasons: List[str] = []

        if norm_hash and norm_hash in seen_hash:
            reasons.append(f"Identyczny meta description jak {seen_hash[norm_hash]}")
        elif norm_hash:
            seen_hash[norm_hash] = job["sku"]

        if long_sig and long_sig in seen_long:
            reasons.append(f"Powtórzone pierwsze 6 słów jak {seen_long[long_sig]}")
        elif long_sig:
            seen_long[long_sig] = job["sku"]

        if short_sig:
            seen_short[short_sig] += 1
            if seen_short[short_sig] > 3:
                reasons.append("Trzywyrazowy schemat otwarcia występuje ponad 3 razy")

        if reasons:
            flagged[job["job_key"]] = reasons

    with db_connect() as conn:
        for job_key, reasons in flagged.items():
            conn.execute(
                """
                UPDATE meta_jobs SET status='validation_failed', validation_errors=?, updated_at=?
                WHERE job_key=?
                """,
                (json.dumps(reasons, ensure_ascii=False), utcnow_iso(), job_key),
            )

    return {
        "checked": len(jobs),
        "flagged": len(flagged),
    }


# ═══════════════════════════════════════════════════════════════════
# PROMPTY
# ═══════════════════════════════════════════════════════════════════

META_SYSTEM_PROMPT = """Jesteś specjalistą SEO e-commerce odpowiedzialnym za metatagi książek i produktów Bookland.

Najważniejsze zasady:
- Korzystaj wyłącznie z danych przekazanych w opisie źródłowym. Nie dopowiadaj faktów.
- Zwróć wyłącznie obiekt JSON zgodny ze schematem.
- meta_description ma mieć 140-160 znaków ze spacjami.
- Pisz po polsku, naturalnie i konkretnie.
- Nie używaj CTA, trybu rozkazującego ani języka sklepu: bez „Sprawdź ofertę”, „Kup teraz”, „Sięgnij po”, „Poznaj”, „Odkryj”.
- Nie zaczynaj od ogólników typu „Ta książka”, „Książka”, „Idealna propozycja”, „Jeśli szukasz”.
- Pierwsze zdanie ma wynikać z indywidualnego planu otwarcia i konkretów ze źródła.
- Nie kopiuj zdań źródłowych dosłownie. Zachowaj ich sens.
- Nie wspominaj o seedzie, planie stylu, metatagu ani instrukcjach.
- Nie stosuj cudzysłowu wokół całego meta description.
"""


def build_meta_prompt(job: Dict, attempt: int = 0, avoid_openings: Sequence[str] = ()) -> str:
    style = build_style_plan(
        job["sku"],
        job.get("title", ""),
        job.get("author", ""),
        job.get("description", ""),
        attempt,
    )
    description = smart_truncate(strip_html(job.get("description", "")), 2600)
    avoid_block = "\n".join(f"- {item}" for item in avoid_openings[:20]) or "- brak"
    cues = ", ".join(style["semantic_cues"]) or "brak wyraźnych słów kluczowych"

    return f"""Wygeneruj jeden meta description dla poniższego produktu.

DANE PRODUKTU
SKU: {job['sku']}
Tytuł: {job.get('title', '')}
Autor / marka: {job.get('author', '')}
Dane dodatkowe: {job.get('details', '')}
Opis źródłowy: {description}

INDYWIDUALNY PLAN DLA TEGO PRODUKTU
Seed stylu: {style['seed']}
Sposób otwarcia: {style['opening_mode']}
Rytm: {style['rhythm_mode']}
Główny fokus: {style['focus_mode']}
Sygnały semantyczne ze źródła: {cues}
Pierwszy konkretny fragment źródła: {style['source_lead'] or 'brak'}

POCZĄTKI, KTÓRYCH NIE WOLNO POWTARZAĆ
{avoid_block}

DODATKOWE WYMAGANIA
- Nie używaj gotowej formuły sprzedażowej ani CTA.
- Nie otwieraj tekstu nazwą sklepu, słowem „książka” ani czasownikiem w trybie rozkazującym.
- Pierwsze 3-6 słów powinno być charakterystyczne dla tego produktu.
- Przy próbie numer {attempt + 1} zastosuj dokładnie wskazany plan, ale nie cytuj go w odpowiedzi.
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
Opcjonalnie drugi <h2> i <p>.
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

@st.cache_resource(show_spinner=False)
def get_gemini_client() -> genai.Client:
    return genai.Client(api_key=st.secrets["GOOGLE_API_KEY"])


@st.cache_resource(show_spinner=False)
def get_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": f"BooklandSEOGenerator/{APP_VERSION}"})
    return session


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = 6,
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
# GEMINI: OPISY I INTERAKTYWNE METATAGI
# ═══════════════════════════════════════════════════════════════════

def generate_description(
    product_data: Dict,
    internal_link: Optional[Dict] = None,
    link_only: bool = False,
    research: Optional[str] = None,
) -> str:
    try:
        system_prompt = (
            build_system_prompt_link_only(internal_link)
            if link_only and internal_link
            else build_system_prompt_full(internal_link)
        )
        response = get_gemini_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=build_description_user_message(product_data, internal_link, research),
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.75,
                max_output_tokens=2400,
            ),
        )
        return clean_ai_fingerprints(strip_code_fences(response.text or ""))
    except Exception as exc:
        return f"BŁĄD GEMINI: {exc}"


def generate_meta_description_interactive(job: Dict) -> Dict:
    title = build_meta_title(job.get("title", ""), job.get("author", ""))
    long_signatures, short_signatures = existing_opening_signatures(job["job_key"])
    recent_full = [row["meta_description"] for row in list_meta_jobs(statuses=["completed"], limit=40)]
    avoid = recent_opening_examples(20)
    last_text = ""
    last_errors: List[str] = []

    for attempt in range(MAX_META_RETRIES + 1):
        style = build_style_plan(
            job["sku"],
            job.get("title", ""),
            job.get("author", ""),
            job.get("description", ""),
            attempt,
        )
        try:
            # Nie używamy frequency_penalty ani presence_penalty. Część modeli Gemini,
            # w tym używany Flash-Lite, odrzuca te parametry błędem 400. Różnorodność
            # zapewniają seed, plan otwarcia, walidacja sygnatur i kontrolowane retry.
            response = get_gemini_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=build_meta_prompt(job, attempt, avoid),
                config=types.GenerateContentConfig(
                    system_instruction=META_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=META_RESPONSE_SCHEMA,
                    seed=style["seed"],
                    temperature=0.9,
                    top_p=0.95,
                    max_output_tokens=250,
                ),
            )
            raw = strip_code_fences(response.text or "")
            data = json.loads(raw)
            last_text = normalize_spaces(strip_html(str(data.get("meta_description", "")))).strip('"„”')
            last_errors = meta_validation_errors(
                last_text,
                existing_long_signatures=long_signatures,
                existing_short_signatures=short_signatures,
                recent_descriptions=recent_full,
            )
            if not last_errors:
                return {
                    "meta_title": title,
                    "meta_description": last_text,
                    "attempts": attempt + 1,
                    "validation_errors": [],
                    "error": "",
                }
            avoid = [*avoid, first_words(last_text, 8)]
        except Exception as exc:
            last_errors = [f"Błąd Gemini lub JSON: {exc}"]

    return {
        "meta_title": title,
        "meta_description": last_text,
        "attempts": MAX_META_RETRIES + 1,
        "validation_errors": last_errors,
        "error": "; ".join(last_errors),
    }


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


def akeneo_get_product_details(
    sku: str,
    token: str,
    channel: str = DEFAULT_CHANNEL,
    locale: str = DEFAULT_LOCALE,
) -> Optional[Dict]:
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
    """Pobiera jedną małą paczkę SKU jednym zapytaniem do endpointu kolekcji."""
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
    response = request_with_retry(
        "GET",
        _akeneo_root() + "/api/rest/v1/products",
        headers=akeneo_headers(token),
        params=params,
    )

    # Część starszych lub niestandardowo skonfigurowanych instalacji Akeneo
    # może nie obsługiwać operatora IN dla identyfikatora produktu. Wtedy
    # zachowujemy poprawność i przechodzimy na kontrolowany fallback 4-wątkowy.
    if response.status_code in {400, 414, 422}:
        products: Dict[str, Dict] = {}
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
    products = {}
    for item in response.json().get("_embedded", {}).get("items", []):
        parsed = parse_akeneo_product(item, channel, locale)
        if parsed.get("identifier"):
            products[parsed["identifier"]] = parsed
    return products


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
        generated = generate_meta_description_interactive(job)
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
    store_view_code: str,
    internal_link: Optional[Dict] = None,
    link_only: bool = False,
    use_research: bool = True,
) -> Dict:
    try:
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

        job_key, _ = upsert_meta_job(
            sku=sku,
            channel=channel,
            locale=locale,
            store_view_code=store_view_code,
            product_data={**product_data, "description": description_html},
            source_updated=product_details.get("updated", ""),
            force_regenerate=True,
        )
        job = get_meta_job(job_key)
        generated = generate_meta_description_interactive(job) if job else {
            "meta_title": build_meta_title(product_data["title"], product_data["author"]),
            "meta_description": "",
            "attempts": 0,
            "validation_errors": ["Brak zadania"],
            "error": "Brak zadania",
        }
        if job:
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
            "description_html": description_html,
            "url": generate_product_url(product_data["title"]),
            "old_description": product_data["description"],
            "research": research,
            "meta_title": generated["meta_title"],
            "meta_description": generated["meta_description"],
            "error": generated["error"] or None,
            "description_quality": quality,
            "validation_errors": generated["validation_errors"],
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

def batch_request_for_job(job: Dict, attempt: int = 0) -> Dict:
    style = build_style_plan(
        job["sku"],
        job.get("title", ""),
        job.get("author", ""),
        job.get("description", ""),
        attempt,
    )
    return {
        "key": job["job_key"],
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": build_meta_prompt(job, attempt, ())}],
                }
            ],
            "system_instruction": {"parts": [{"text": META_SYSTEM_PROMPT}]},
            # Parametry penalty są celowo pominięte: nie są obsługiwane przez każdy model Gemini.
            "generation_config": {
                "temperature": 0.9,
                "top_p": 0.95,
                "seed": style["seed"],
                "max_output_tokens": 250,
                "response_mime_type": "application/json",
                "response_schema": META_RESPONSE_SCHEMA,
            },
        },
    }


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


def submit_queued_batches(
    products_per_file: int = BATCH_PRODUCTS_PER_FILE,
    run_id: Optional[str] = None,
) -> List[Dict]:
    queued = list_meta_jobs(statuses=["queued"], order_by="created_at ASC", run_id=run_id)
    if not queued:
        return []

    submitted: List[Dict] = []
    client = get_gemini_client()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(0, len(queued), products_per_file):
        chunk = queued[index : index + products_per_file]
        part = index // products_per_file + 1
        display_name = f"bookland-meta-{(run_id or 'all')[-18:]}-{timestamp}-{part:03d}"
        input_path = BATCH_DIR / f"{display_name}.jsonl"
        write_batch_jsonl(chunk, input_path)

        uploaded_file = client.files.upload(
            file=str(input_path),
            config=types.UploadFileConfig(display_name=display_name, mime_type="jsonl"),
        )
        batch_job = client.batches.create(
            model=GEMINI_MODEL,
            src=uploaded_file.name,
            config={"display_name": display_name},
        )
        state = getattr(getattr(batch_job, "state", None), "name", None) or str(
            getattr(batch_job, "state", "JOB_STATE_PENDING")
        )
        register_batch_job(
            job_name=batch_job.name,
            run_id=run_id or "",
            display_name=display_name,
            input_path=input_path,
            input_file_name=uploaded_file.name,
            product_count=len(chunk),
            state=state,
        )
        with db_connect() as conn:
            conn.executemany(
                """
                UPDATE meta_jobs SET status='batch_submitted', batch_job_name=?, updated_at=?
                WHERE job_key=?
                """,
                [(batch_job.name, utcnow_iso(), job["job_key"]) for job in chunk],
            )
        submitted.append(
            {
                "job_name": batch_job.name,
                "display_name": display_name,
                "products": len(chunk),
                "state": state,
            }
        )
    return submitted


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


def ingest_batch_result_bytes(job_name: str, content: bytes) -> Dict[str, int]:
    decoded = content.decode("utf-8")
    lines = [line for line in decoded.splitlines() if line.strip()]
    long_signatures, short_signatures = existing_opening_signatures()
    recent_descriptions = [row["meta_description"] for row in list_meta_jobs(statuses=["completed"], limit=80)]
    stats = {"completed": 0, "validation_failed": 0, "failed": 0}

    for line in lines:
        try:
            payload = json.loads(line)
            key, text = extract_batch_response_text(payload)
            if not key:
                raise RuntimeError("Brak klucza zadania w odpowiedzi")
            job = get_meta_job(key)
            if not job:
                raise RuntimeError(f"Nieznany klucz zadania: {key}")
            data = json.loads(strip_code_fences(text))
            meta_description = normalize_spaces(strip_html(str(data.get("meta_description", "")))).strip('"„”')
            meta_title = build_meta_title(job["title"], job["author"])
            errors = meta_validation_errors(
                meta_description,
                existing_long_signatures=long_signatures,
                existing_short_signatures=short_signatures,
                recent_descriptions=recent_descriptions,
            )
            status = "completed" if not errors else "validation_failed"
            save_meta_result(
                key,
                meta_title=meta_title,
                meta_description=meta_description,
                status=status,
                attempts=int(job.get("attempts", 0)) + 1,
                validation_errors=errors,
                error_message="; ".join(errors),
            )
            if status == "completed":
                long_signatures.add(opening_signature(meta_description, 6))
                short_signatures[opening_signature(meta_description, 3)] += 1
                recent_descriptions.append(meta_description)
                recent_descriptions = recent_descriptions[-80:]
            stats[status] += 1
        except Exception as exc:
            stats["failed"] += 1
            try:
                payload = json.loads(line)
                key = str(payload.get("key") or payload.get("metadata", {}).get("key") or "")
                if key:
                    set_meta_job_error(key, str(exc))
            except Exception:
                pass

    with db_connect() as conn:
        conn.execute(
            "UPDATE batch_jobs SET ingested_at=?, updated_at=? WHERE job_name=?",
            (utcnow_iso(), utcnow_iso(), job_name),
        )
    return stats


def refresh_and_ingest_batch_jobs(run_id: Optional[str] = None) -> List[Dict]:
    client = get_gemini_client()
    updates: List[Dict] = []
    for stored in list_batch_jobs(run_id=run_id):
        if stored["ingested_at"]:
            continue
        try:
            batch_job = client.batches.get(name=stored["job_name"])
            state = batch_state_name(batch_job)
            output_file_name = ""
            error_message = ""
            dest = getattr(batch_job, "dest", None)
            if dest and getattr(dest, "file_name", None):
                output_file_name = dest.file_name
            if getattr(batch_job, "error", None):
                error_message = str(batch_job.error)
            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE batch_jobs SET state=?, output_file_name=?, error_message=?, updated_at=?
                    WHERE job_name=?
                    """,
                    (state, output_file_name, error_message[:2000], utcnow_iso(), stored["job_name"]),
                )
            update = {"job_name": stored["job_name"], "state": state, "ingested": False}
            if state == "JOB_STATE_SUCCEEDED" and output_file_name:
                content = client.files.download(file=output_file_name)
                update["stats"] = ingest_batch_result_bytes(stored["job_name"], content)
                update["ingested"] = True
            elif state in {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}:
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE meta_jobs SET status='failed', error_message=?, updated_at=?
                        WHERE batch_job_name=? AND status='batch_submitted'
                        """,
                        (error_message or state, utcnow_iso(), stored["job_name"]),
                    )
            updates.append(update)
        except Exception as exc:
            updates.append({"job_name": stored["job_name"], "state": "ERROR", "error": str(exc)})
    return updates


# ═══════════════════════════════════════════════════════════════════
# IMPORT DUŻEGO KATALOGU I EKSPORT
# ═══════════════════════════════════════════════════════════════════

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
        "run_id": run_id,
        "input": len(skus),
        "unique": len(unique_skus),
        "duplicates": duplicate_count,
        "found": 0,
        "queued": 0,
        "skipped_unchanged": 0,
        "inactive": 0,
        "without_description": 0,
        "missing": 0,
        "processed": 0,
    }
    missing_skus: List[str] = []
    inactive_skus: List[str] = []
    without_description_skus: List[str] = []

    for sku_chunk in chunks(unique_skus, AKENEO_SKU_FILTER_CHUNK_SIZE):
        products = akeneo_fetch_products_by_identifiers(token, channel, locale, sku_chunk)
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

            product_data = _prepare_product_data(product)
            _, queued = upsert_meta_job(
                sku=sku,
                channel=channel,
                locale=locale,
                store_view_code=store_view_code,
                product_data=product_data,
                source_updated=product.get("updated", ""),
                force_regenerate=force_regenerate,
                run_id=run_id,
                source_type="sku_list",
            )
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
# SESSION STATE I UI HELPERY
# ═══════════════════════════════════════════════════════════════════

def init_session_state() -> None:
    defaults = {
        "bulk_results": [],
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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_session_state()


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


def render_result_preview(result: Dict, channel: str, locale: str) -> None:
    sku = result["sku"]
    edit_key = f"edit_{sku}"
    is_meta_only = result.get("meta_only", False)

    if not is_meta_only:
        if edit_key not in st.session_state:
            st.session_state[edit_key] = result.get("description_html", "")
        tabs = st.tabs(["HTML", "Podgląd", "Edytuj"] + (["Research"] if result.get("research") else []))
        with tabs[0]:
            st.code(result.get("description_html", ""), language="html")
        with tabs[1]:
            st.markdown(st.session_state.get(edit_key, ""), unsafe_allow_html=True)
        with tabs[2]:
            quill_value = st_quill(
                value=st.session_state.get(edit_key, ""),
                html=True,
                key=f"quill_{sku}",
                toolbar=[[{"header": [2, 3, False]}], ["bold", "link"], ["clean"]],
            )
            if quill_value is not None:
                st.session_state[edit_key] = normalize_quill_html(quill_value)
        if result.get("research") and len(tabs) > 3:
            with tabs[3]:
                st.markdown(result["research"])

    with st.expander("Metatagi Magento", expanded=is_meta_only):
        meta_title = result.get("meta_title", "")
        meta_description = result.get("meta_description", "")
        st.text_input("meta_title", value=meta_title, disabled=True, key=f"mt_{sku}")
        st.text_area("meta_description", value=meta_description, disabled=True, height=90, key=f"md_{sku}")
        col1, col2 = st.columns(2)
        col1.caption(f"{len(meta_title)}/60 znaków")
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
) -> List[Dict]:
    results: List[Dict] = []
    max_workers = GEMINI_INTERACTIVE_WORKERS
    progress = st.progress(0, "Start")
    total = len(skus)
    processed = 0

    for chunk_start in range(0, total, INTERACTIVE_CHUNK_SIZE):
        chunk = skus[chunk_start : chunk_start + INTERACTIVE_CHUNK_SIZE]
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
                        store_view_code,
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
                    result = {"sku": sku, "title": "", "error": str(exc)}
                results.append(result)
                processed += 1
                progress.progress(processed / total, f"Przetworzono {processed}/{total}: {sku}")
    progress.progress(1.0, "Gotowe")
    return results


# ═══════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════

st.markdown(f'<h1 class="main-header">📚 {APP_NAME}</h1>', unsafe_allow_html=True)
st.markdown(
    f'<p class="sub-header">v{APP_VERSION} · Gemini: {GEMINI_MODEL} · seed i kontrola powtarzalnych otwarć</p>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Ustawienia")
    channel = st.selectbox("Kanał", [DEFAULT_CHANNEL, "B2B"], index=0)
    locale = st.text_input("Locale", value=DEFAULT_LOCALE)
    st.session_state.magento_store_view = st.text_input(
        "Magento store_view_code",
        value=st.session_state.magento_store_view,
    )

    st.markdown("---")
    st.subheader("Linkowanie wewnętrzne")
    st.session_state.link_active = st.checkbox("Włącz linkowanie", value=st.session_state.link_active)
    st.session_state.link_only = st.checkbox(
        "Tylko dodaj link - bez przepisywania opisu",
        value=st.session_state.link_only,
    )
    st.session_state.link_url = st.text_input("URL linku", value=st.session_state.link_url)
    st.session_state.link_category = st.text_input("Kategoria / anchor hint", value=st.session_state.link_category)

    st.markdown("---")
    st.subheader("Research")
    st.session_state.use_research = st.checkbox(
        "Wzbogacaj pełne opisy researchem Perplexity",
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
    ["Praca interaktywna", "Metatagi 50k / Batch API", "Wyniki i kontrola jakości"]
)

with interactive_tab:
    st.subheader("Wybór produktów")
    method = st.radio(
        "Metoda",
        ["Wyszukaj", "Wklej SKU lub URL", "Backlog"],
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
            st.rerun()

        st.markdown("---")
        st.subheader("Generowanie")
        st.session_state.meta_only = st.checkbox(
            "Tylko metatagi",
            value=st.session_state.meta_only,
            help="Meta title powstaje deterministycznie, a meta description ma indywidualny seed i plan otwarcia.",
        )
        if st.button("Start generowania", type="primary"):
            skus = list(st.session_state.bulk_selected_products)
            try:
                st.session_state.bulk_results = process_selected_products(
                    skus,
                    token=akeneo_get_token(),
                    channel=channel,
                    locale=locale,
                    store_view_code=st.session_state.magento_store_view,
                    meta_only=st.session_state.meta_only,
                    internal_link=get_internal_link(),
                    link_only=st.session_state.link_only,
                    use_research=st.session_state.use_research,
                )
                st.session_state.products_to_send = {
                    result["sku"]: True for result in st.session_state.bulk_results if not result.get("error")
                }
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
            "Pobierz wyniki CSV",
            data_frame.to_csv(index=False).encode("utf-8-sig"),
            "wyniki_interaktywne.csv",
            "text/csv",
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
                render_result_preview(result, channel, locale)
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

            products_per_batch = st.number_input(
                "Produktów w jednym pliku batch",
                min_value=100,
                max_value=20000,
                value=BATCH_PRODUCTS_PER_FILE,
                step=100,
            )
            if st.button("2. Wyślij oczekujące paczki tej partii do Gemini"):
                with st.spinner("Tworzę pliki JSONL i wysyłam zadania..."):
                    submitted = submit_queued_batches(int(products_per_batch), run_id=selected_run)
                if submitted:
                    st.success(
                        f"Utworzono {len(submitted)} zadań batch dla "
                        f"{sum(item['products'] for item in submitted)} produktów."
                    )
                    st.dataframe(pd.DataFrame(submitted), use_container_width=True)
                else:
                    st.info("Ta partia nie ma produktów ze statusem queued.")

            if st.button("3. Odśwież statusy i odbierz wyniki tej partii"):
                with st.spinner("Sprawdzam Batch API i zapisuję wyniki..."):
                    updates = refresh_and_ingest_batch_jobs(run_id=selected_run)
                if updates:
                    st.dataframe(pd.DataFrame(updates), use_container_width=True)
                else:
                    st.info("Brak nieodebranych zadań batch w tej partii.")

            batch_rows = list_batch_jobs(run_id=selected_run)
            if batch_rows:
                st.subheader("Zadania Batch API aktywnej partii")
                st.dataframe(
                    pd.DataFrame(batch_rows)[
                        ["display_name", "state", "product_count", "created_at", "ingested_at", "error_message"]
                    ],
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

    col_audit, col_requeue = st.columns(2)
    if col_audit.button("Audytuj powtarzalne otwarcia"):
        audit = audit_meta_jobs_for_repetition(run_id=result_run or None)
        st.success(f"Sprawdzono {audit['checked']} produktów, oznaczono {audit['flagged']} do poprawy.")

    if col_requeue.button("Przenieś błędne i powtarzalne do kolejki"):
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
