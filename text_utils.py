"""Czyste funkcje tekstowe wspólne dla opisów i metatagów."""
from __future__ import annotations

import csv
import hashlib
import html
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Sequence, Set, Tuple

from config import (
    GEMINI_MODEL,
    POLISH_STOPWORDS,
    PROMPT_VERSION,
    TITLE_INTERNAL_NOISE_PATTERNS,
    _POLISH_CHARS,
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def opening_signature(value: str, count: int = 6) -> str:
    words = normalize_for_compare(value).split()
    return " ".join(words[:count])


def smart_truncate(value: str, max_chars: int) -> str:
    text = normalize_spaces(value)
    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened


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
    # Prompt korzysta tylko z seed i semantic_cues. Pozostałe pola zostają puste,
    # bo kolumny meta_jobs są NOT NULL w istniejących bazach.
    return {
        "seed": stable_seed(sku, title, author, strip_html(description), str(attempt)),
        "opening_mode": "",
        "rhythm_mode": "",
        "focus_mode": "",
        "semantic_cues": extract_semantic_cues(description, title),
        "source_lead": "",
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
    author = _clean_option_slug(author)
    normalized = normalize_for_compare(author)
    if normalized in {"", "pracazbiorowa", "praca zbiorowa", "various authors", "brak", "none"}:
        return "", "Brak pojedynczego autora (praca zbiorowa) - sformułuj to naturalnie lub pomiń w meta title."
    return author, "Autor może być użyty wyłącznie wtedy, gdy zwiększa trafność i mieści się bez skracania istoty produktu."


def split_contributor_names(value: str) -> List[str]:
    """Rozdziela awaryjny tekst autorów; podstawowym źródłem pozostaje lista opcji Akeneo."""
    text = normalize_spaces(strip_html(value))
    if not text:
        return []
    parts = re.split(r"\s*(?:,|;|\s+i\s+|\s+and\s+|\s*&\s*)\s*", text, flags=re.IGNORECASE)
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))


def format_contributor_names(contributors: Sequence[str]) -> str:
    names = list(dict.fromkeys(normalize_spaces(name) for name in contributors if normalize_spaces(name)))
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " i " + names[-1]


def detect_contributor_role(*sources: str) -> str:
    """Rozpoznaje rolę redakcyjną tylko wtedy, gdy źródło mówi o niej wprost."""
    text = normalize_for_compare(" ".join(source for source in sources if source))
    markers = (
        "redakcja naukowa",
        "pod redakcja",
        "redaktor naukowy",
        "redaktorzy naukowi",
        "redaktorka naukowa",
        "redaktorki naukowe",
        "redaktorami naukowymi",
        "redaktorkami naukowymi",
        "edited by",
        "edited",
        "scientific editor",
        "scientific editors",
    )
    return "redakcja naukowa" if any(marker in text for marker in markers) else ""


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


def _clean_option_slug(code: str) -> str:
    """Format fallback dla kodów opcji Akeneo, jeśli brak etykiety w API."""
    if not code:
        return ""
    if "_" in code or "-" in code:
        parts = re.split(r"[_\-]+", code)
        cleaned = " ".join(part.capitalize() for part in parts if part)
        cleaned = re.sub(r"\bPwn\b", "PWN", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bOop\b", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()
    return code
