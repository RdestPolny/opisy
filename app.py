import streamlit as st
import pandas as pd
import requests
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import time
from datetime import datetime
from pathlib import Path

# Obsługa Google Gemini
from google import genai
from google.genai import types

# ═══════════════════════════════════════════════════════════════════
# STAŁE I KONFIGURACJA
# ═══════════════════════════════════════════════════════════════════

APP_VERSION = "3.8.0"
APP_NAME = "Generator Opisów Produktów"
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_CHANNEL = "Bookland"
DEFAULT_LOCALE = "pl_PL"
MAX_WORKERS = 5
AKENEO_TIMEOUT = 30
PERPLEXITY_TIMEOUT = 30

REQUIRED_SECRETS = [
    "AKENEO_BASE_URL", "AKENEO_CLIENT_ID", "AKENEO_SECRET",
    "AKENEO_USERNAME", "AKENEO_PASSWORD", "GOOGLE_API_KEY",
    "PERPLEXITY_API_KEY",
]

DB_PATH = Path(".streamlit/optimized_products.json")

# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STRONY
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title=f"{APP_NAME} v{APP_VERSION} (Gemini)",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: 700;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        color: #666;
        font-size: 1rem;
        margin-bottom: 2rem;
    }
    .scrollable-results {
        max-height: 400px;
        overflow-y: auto;
        border: 1px solid #e0e0e0;
        border-radius: 0.5rem;
        padding: 1rem;
        background: #fafafa;
    }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# BAZA ZOPTYMALIZOWANYCH PRODUKTÓW
# ═══════════════════════════════════════════════════════════════════

def ensure_db_exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        DB_PATH.write_text("[]")

def load_optimized_products() -> List[Dict]:
    ensure_db_exists()
    try:
        with open(DB_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save_optimized_products(products: List[Dict]):
    ensure_db_exists()
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

def add_optimized_product(sku: str, title: str, url: str):
    products = load_optimized_products()
    now_iso = datetime.now().isoformat()
    existing = next((p for p in products if p['sku'] == sku), None)
    if existing:
        existing.update({'last_optimized': now_iso, 'title': title, 'url': url})
    else:
        products.append({
            'sku': sku,
            'title': title,
            'url': url,
            'first_optimized': now_iso,
            'last_optimized': now_iso,
        })
    save_optimized_products(products)

# ═══════════════════════════════════════════════════════════════════
# FUNKCJE POMOCNICZE
# ═══════════════════════════════════════════════════════════════════

_POLISH_CHARS = str.maketrans(
    'ąćęłńóśźżĄĆĘŁŃÓŚŹŻ',
    'acelnoszzACELNOSZZ'
)

def generate_product_url(title: str) -> str:
    slug = title.lower().translate(_POLISH_CHARS)
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug).strip('-')
    return f"https://bookland.com.pl/{slug}"

def strip_code_fences(text: str) -> str:
    if not text:
        return text
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m:
        return m.group(1).strip()
    text = re.sub(r"^\s*```(?:html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()

def clean_ai_fingerprints(text: str) -> str:
    """Zamienia typograficzne znaki AI na zwykłe oraz konwertuje Markdown bold na HTML."""
    text = text.replace('—', '-').replace('–', '-')
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    return text

def safe_string_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip()

def validate_description_quality(description: str) -> Tuple[str, str]:
    desc = description.strip()
    length = len(desc)
    if not desc:
        return 'error', '❌ Brak oryginalnego opisu w Akeneo!'
    if length < 100:
        return 'error', f'❌ Opis b. krótki ({length} zn)!'
    if length < 300:
        return 'warning', f'⚠️ Opis krótki ({length} zn).'
    return 'ok', '✅ Opis OK'

# ═══════════════════════════════════════════════════════════════════
# PROMPTY DO GEMINI
# ═══════════════════════════════════════════════════════════════════

def build_system_prompt_full(internal_link: Optional[Dict] = None) -> str:
    """Buduje system prompt dla pełnego generowania opisu produktu."""

    link_block = ""
    if internal_link and internal_link.get('url') and internal_link.get('category'):
        link_block = f"""
## LINKOWANIE WEWNĘTRZNE (OBOWIĄZKOWE)

Wpleć naturalnie w tekst JEDEN link wewnętrzny do kategorii:
- Kategoria: **{internal_link['category']}**
- URL: `{internal_link['url']}`
- Anchor: Wybierz naturalną odmianę lub frazę powiązaną z kategorią (nie przepisuj dosłownie jej nazwy).
- Miejsce: Najlepiej w 2. lub 3. akapicie, tam gdzie pasuje kontekstowo.
- Format HTML: `<a href="{internal_link['url']}">wybrany anchor</a>`
- ⚠️ Brak tego linku = błąd krytyczny.
"""

    return f"""Jesteś doświadczonym copywriterem e-commerce i ekspertem SEO specjalizującym się w opisach produktów księgarni internetowej Bookland.

## TWOJA ROLA

Piszesz opisy, które:
- Angażują i zachęcają do zakupu (sprzedażowy ton)
- Budują semantyczną strukturę strony (SEO)
- Są unikalne i pozbawione "lania wody"

## OBOWIĄZKOWE ZASADY FORMATOWANIA

- **HTML only**: Używaj WYŁĄCZNIE tagów `<p>`, `<h2>`, `<h3>`, `<b>`, `<a>`.
- **Zakaz Markdown**: Absolutnie nie używaj `**` ani żadnej składni Markdown.
- **Pogrubienia**: W pierwszym `<p>` pogrub tytuł, autora i 2-3 kluczowe cechy. Łącznie max 8-10 `<b>` w całym tekście.
- **Myślniki**: Używaj wyłącznie zwykłego dywizu `-` (nie `—` ani `–`).
- **Dane techniczne**: NIE twórz list. Wplataj dane (rok, strony, oprawa) naturalnie w akapity.
{link_block}
## WYMAGANA STRUKTURA

```
<p>[WSTĘP: 4-6 zdań. Przedstaw produkt, pogrub tytuł i autora, zbuduj zainteresowanie.]</p>

<h2>[Nagłówek z korzyścią dla czytelnika]</h2>
<p>[ROZWINIĘCIE: 5-8 zdań. Wpleć specyfikację techniczną, połącz cechy z korzyściami.]</p>

<!-- Opcjonalnie: -->
<h2>[Drugi nagłówek z inną korzyścią]</h2>
<p>[DALSZY OPIS: 4-6 zdań. Ostatnie 1-2 zdania to wyraźne CTA zachęcające do zakupu.]</p>

<h3>[Krótkie, chwytliwe hasło podsumowujące - OSTATNI element]</h3>
```

## CZEGO UNIKAĆ

- Powtarzania tych samych informacji w różnych akapitach
- Ogólnikowych zwrotów: "Ta książka jest wyjątkowa", "Idealna dla każdego"
- Zaczynania od pytania lub cytatu
- Tworzenia list punktowanych (`<ul>`, `<li>`)

Zwróć TYLKO gotowy kod HTML — bez komentarzy, bez bloków ```html, bez żadnego dodatkowego tekstu."""


def build_system_prompt_link_only(internal_link: Dict) -> str:
    """Buduje system prompt dla trybu 'tylko linkowanie' (minimalna edycja opisu)."""
    return f"""Jesteś ekspertem SEO. Twoim jedynym zadaniem jest dodanie linku wewnętrznego do gotowego opisu produktu.

## ZASADY

1. **Zachowaj oryginalny tekst**: Nie przepisuj, nie zmieniaj stylu ani treści.
2. **Dodaj JEDEN link**: `<a href="{internal_link['url']}">naturalny anchor</a>`
   - Anchor to naturalna odmiana lub fraza związana z kategorią "**{internal_link['category']}**".
3. **Minimalna ingerencja**: Jeśli potrzeba, zmodyfikuj lub dodaj maksymalnie 1-2 zdania, by link brzmiał naturalnie.
4. **Format HTML**: Jeśli oryginał nie ma tagów HTML, otoczyj akapity tagami `<p>` i użyj `<b>` zamiast `**`.
5. **Zakaz Markdown**: Nie używaj `**`. Używaj `<b>`.

Zwróć kompletny, gotowy kod HTML opisu z wplecionym linkiem — bez dodatkowych komentarzy."""


def build_user_message(
    product_data: Dict,
    internal_link: Optional[Dict] = None,
    research: Optional[str] = None,
) -> str:
    """Buduje wiadomość użytkownika (dane produktu + opcjonalny research) dla modelu."""
    parts = [
        f"TYTUŁ PRODUKTU: {product_data.get('title', '')}",
        f"AUTOR/MARKA: {product_data.get('author', '')}",
        f"DANE TECHNICZNE: {product_data.get('details', '')}",
        f"ORYGINALNY OPIS: {product_data.get('description', '')}",
    ]
    if research:
        parts.append(
            f"\nRESEARCH (zweryfikowane informacje o książce - wykorzystaj je w opisie):\n{research}"
        )
    if internal_link:
        parts.append(f"LINK DO WPLECENIA: {internal_link['url']} (Kategoria: {internal_link['category']})")
    parts.append("\nZwróć TYLKO kod HTML opisu.")
    return "\n".join(parts)

# ═══════════════════════════════════════════════════════════════════
# RESEARCH - PERPLEXITY SONAR
# ═══════════════════════════════════════════════════════════════════

PERPLEXITY_SYSTEM_PROMPT = """Jesteś asystentem badającym książki i autorów. Odpowiadaj wyłącznie po polsku.
Podaj TYLKO zweryfikowane, konkretne fakty. Nie generalizuj. Bądź zwięzły."""

def research_book_with_perplexity(title: str, author: str) -> Optional[str]:
    """
    Wywołuje Perplexity Sonar, by zebrać informacje o książce przed generowaniem opisu.
    Zwraca string z wynikami lub None w przypadku błędu.
    """
    api_key = st.secrets.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        return None

    query = (
        f"Podaj kluczowe informacje o książce '{title}'"
        + (f" autorstwa {author}" if author else "")
        + ". Uwzględnij: główne tematy i przesłanie, gatunek literacki, odbiór przez krytyków i czytelników,"
        " najważniejsze cechy wyróżniające tę pozycję, dla kogo jest przeznaczona."
        " Odpowiedz konkretnie i zwięźle, max 300 słów."
    )

    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {"role": "system", "content": PERPLEXITY_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        "max_tokens": 600,
        "return_citations": False,
        "return_images": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(
            PERPLEXITY_API_URL,
            headers=headers,
            json=payload,
            timeout=PERPLEXITY_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return None  # Research nieobowiązkowy - nie blokuje generowania


# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - GEMINI
# ═══════════════════════════════════════════════════════════════════

def generate_description(
    product_data: Dict,
    internal_link: Optional[Dict] = None,
    link_only: bool = False,
    research: Optional[str] = None,
) -> str:
    """Generuje opis produktu przy użyciu Google Gemini."""
    if "GOOGLE_API_KEY" not in st.secrets:
        return "BŁĄD: Brak klucza GOOGLE_API_KEY w secrets.toml"

    try:
        if link_only and internal_link:
            system_prompt = build_system_prompt_link_only(internal_link)
        else:
            system_prompt = build_system_prompt_full(internal_link)

        user_message = build_user_message(product_data, internal_link, research)

        client = genai.Client(api_key=st.secrets["GOOGLE_API_KEY"])
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
            )
        )

        result = strip_code_fences(response.text)
        result = clean_ai_fingerprints(result)
        return result

    except Exception as e:
        return f"BŁĄD GEMINI: {str(e)}"


def generate_meta_fields(product_data: Dict, description_html: str) -> Dict:
    """Generuje meta_title i meta_description dla importu do Magento."""
    if "GOOGLE_API_KEY" not in st.secrets:
        return {'meta_title': '', 'meta_description': ''}

    prompt = (
        f"Na podstawie danych książki wygeneruj meta_title i meta_description dla sklepu e-commerce Bookland.\n\n"
        f"TYTUŁ: {product_data.get('title', '')}\n"
        f"AUTOR: {product_data.get('author', '')}\n"
        f"OPIS (fragment): {description_html[:400]}\n\n"
        f"ZASADY:\n"
        f"- meta_title: max 60 znaków, zawiera tytuł i autora, nie dodawaj nazwy sklepu\n"
        f"- meta_description: 140-160 znaków, zachęcający tekst po polsku, bez cudzysłowów\n\n"
        f'Odpowiedz TYLKO w formacie JSON: {{"meta_title": "...", "meta_description": "..."}}'
    )

    try:
        client = genai.Client(api_key=st.secrets["GOOGLE_API_KEY"])
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = strip_code_fences(response.text)
        data = json.loads(text)
        return {
            'meta_title': str(data.get('meta_title', ''))[:60],
            'meta_description': str(data.get('meta_description', ''))[:160],
        }
    except Exception:
        return {'meta_title': '', 'meta_description': ''}


def _prepare_product_data(product_details: Dict) -> Dict:
    """Buduje słownik danych wejściowych do promptu na podstawie danych z Akeneo."""
    details_parts = []
    for field, label in [
        ('publisher', 'Wydawnictwo'),
        ('year', 'Rok'),
        ('pages', 'Strony'),
        ('cover_type', 'Oprawa'),
    ]:
        value = product_details.get(field)
        if value:
            details_parts.append(f"{label}: {value}")

    return {
        'title': safe_string_value(product_details['title']),
        'author': safe_string_value(product_details['author']),
        'details': ', '.join(details_parts),
        'description': safe_string_value(product_details.get('description', '')),
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
    """Pobiera dane produktu z Akeneo, opcjonalnie bada go przez Perplexity i generuje opis."""
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)

        if not product_details:
            return {
                'sku': sku, 'title': '', 'description_html': '', 'url': '',
                'error': 'Produkt nie znaleziony',
                'description_quality': ('error', 'Produkt nie znaleziony'),
            }

        original_desc = safe_string_value(product_details.get('description', ''))
        quality_status, quality_msg = validate_description_quality(original_desc)
        product_data = _prepare_product_data(product_details)

        # Research przez Perplexity Sonar (jeśli włączony)
        research = None
        if use_research and not link_only:
            research = research_book_with_perplexity(
                product_data['title'],
                product_data['author'],
            )

        description_html = generate_description(
            product_data,
            internal_link=internal_link,
            link_only=link_only,
            research=research,
        )

        meta = generate_meta_fields(product_data, description_html) if "BŁĄD" not in description_html else {}

        return {
            'sku': sku,
            'title': product_data['title'],
            'description_html': description_html,
            'url': generate_product_url(product_data['title']),
            'old_description': original_desc,
            'research': research,
            'meta_title': meta.get('meta_title', ''),
            'meta_description': meta.get('meta_description', ''),
            'error': description_html if "BŁĄD" in description_html else None,
            'description_quality': (quality_status, quality_msg),
        }

    except Exception as e:
        return {
            'sku': sku, 'title': '', 'error': str(e),
            'description_quality': ('error', str(e)),
        }

# ═══════════════════════════════════════════════════════════════════
# AKENEO API
# ═══════════════════════════════════════════════════════════════════

def _akeneo_root() -> str:
    base = st.secrets["AKENEO_BASE_URL"].rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[:-len("/api/rest/v1")]
    return base

@st.cache_data(ttl=3000, show_spinner=False)
def akeneo_get_token() -> str:
    try:
        token_url = _akeneo_root() + "/api/oauth/v1/token"
        auth = (st.secrets["AKENEO_CLIENT_ID"], st.secrets["AKENEO_SECRET"])
        data = {
            "grant_type": "password",
            "username": st.secrets["AKENEO_USERNAME"],
            "password": st.secrets["AKENEO_PASSWORD"],
        }
        r = requests.post(token_url, auth=auth, data=data, timeout=AKENEO_TIMEOUT)
        if r.status_code != 200:
            st.error(f"❌ Błąd autoryzacji Akeneo (Kod: {r.status_code})")
            st.stop()
        return r.json()["access_token"]
    except Exception as e:
        st.error(f"❌ Błąd połączenia z Akeneo: {e}")
        st.stop()

def akeneo_get_attribute(code: str, token: str) -> Dict:
    url = _akeneo_root() + f"/api/rest/v1/attributes/{code}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=AKENEO_TIMEOUT)
    r.raise_for_status()
    return r.json()

def akeneo_product_exists(sku: str, token: str) -> bool:
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=AKENEO_TIMEOUT)
    return r.status_code == 200

def _extract_product_title(item: Dict, locale: str) -> str:
    """Wyciąga tytuł produktu z danych Akeneo."""
    for val in item.get("values", {}).get("name", []):
        if val.get("locale") in (locale, None):
            return safe_string_value(val.get("data", item.get("identifier", "")))
    return item.get("identifier", "")

def akeneo_search_products(search_query: str, token: str, limit: int = 20, locale: str = DEFAULT_LOCALE) -> List[Dict]:
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    products_dict: Dict[str, Dict] = {}

    searches = [
        {"identifier": [{"operator": "CONTAINS", "value": search_query}]},
        {"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]},
    ]

    try:
        for search_filter in searches:
            params = {"limit": limit, "search": json.dumps(search_filter)}
            r = requests.get(url, headers=headers, params=params, timeout=AKENEO_TIMEOUT)
            if r.status_code != 200:
                continue
            for item in r.json().get("_embedded", {}).get("items", []):
                ident = item.get("identifier", "")
                if ident and ident not in products_dict:
                    products_dict[ident] = {
                        "identifier": ident,
                        "title": _extract_product_title(item, locale),
                        "family": item.get("family", ""),
                        "enabled": item.get("enabled", False),
                    }
        return list(products_dict.values())[:limit]
    except Exception as e:
        st.error(f"Błąd wyszukiwania: {e}")
        return []

@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_fetch_categories(token: str, locale: str) -> List[Dict]:
    """Pobiera wszystkie kategorie z Akeneo (z cache 1h). Zwraca [{code, label, parent}]."""
    url = _akeneo_root() + "/api/rest/v1/categories"
    headers = {"Authorization": f"Bearer {token}"}
    categories = []
    page = 1
    while True:
        try:
            r = requests.get(url, headers=headers, params={"limit": 100, "page": page}, timeout=AKENEO_TIMEOUT)
            if r.status_code != 200:
                break
            items = r.json().get("_embedded", {}).get("items", [])
            if not items:
                break
            for item in items:
                labels = item.get("labels", {})
                label = labels.get(locale) or labels.get("pl_PL") or item.get("code", "")
                categories.append({
                    "code": item.get("code", ""),
                    "label": label,
                    "parent": item.get("parent"),
                })
            page += 1
            if len(items) < 100:
                break
        except Exception:
            break
    return sorted(categories, key=lambda x: x["label"])


def akeneo_fetch_backlog(
    token: str, channel: str, locale: str, limit: int = 100,
    category: Optional[str] = None,
) -> List[Dict]:
    """
    Pobiera aktywne (enabled=true) produkty bez zoptymalizowanego opisu.
    Opcjonalnie filtruje po kategorii Akeneo.
    Strategia: dwa zapytania (opisy_seo=false + description EMPTY), merge po SKU.
    Sortuje: brak opisu → krótki opis.
    Uwaga: stan magazynowy trzyma Magento, nie Akeneo — enabled=true to max co można sprawdzić po stronie PIM.
    """
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    products_dict: Dict[str, Dict] = {}

    per_page = 100  # Akeneo max
    pages_needed = max(1, (limit + 99) // 100)

    base: Dict = {"enabled": [{"operator": "=", "value": True}]}
    if category:
        base["categories"] = [{"operator": "IN", "value": [category]}]

    filter_sets = [
        json.dumps({**base, "opisy_seo": [{"operator": "=", "value": False, "scope": channel}]}),
        json.dumps({**base, "description": [{"operator": "EMPTY", "scope": channel, "locale": locale}]}),
    ]

    for search_filter in filter_sets:
        for page in range(1, pages_needed + 1):
            if len(products_dict) >= limit * 2:  # pobierz 2x, przytnij po merge
                break
            params = {"limit": per_page, "page": page, "search": search_filter}
            try:
                r = requests.get(url, headers=headers, params=params, timeout=AKENEO_TIMEOUT)
                if r.status_code != 200:
                    break
                items = r.json().get("_embedded", {}).get("items", [])
                if not items:
                    break
                for item in items:
                    ident = item.get("identifier", "")
                    if not ident or ident in products_dict:
                        continue
                    desc_len = 0
                    for val in item.get("values", {}).get("description", []):
                        if val.get("scope") in (None, channel) and val.get("locale") in (None, locale):
                            desc_len = len(str(val.get("data", "") or ""))
                            break
                    products_dict[ident] = {
                        "identifier": ident,
                        "title": _extract_product_title(item, locale),
                        "desc_len": desc_len,
                    }
            except Exception:
                break

    results = list(products_dict.values())
    results.sort(key=lambda x: x["desc_len"])
    return results[:limit]


def akeneo_get_product_details(sku: str, token: str, channel: str = DEFAULT_CHANNEL, locale: str = DEFAULT_LOCALE) -> Optional[Dict]:
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=AKENEO_TIMEOUT)
        r.raise_for_status()
        product = r.json()
        values = product.get("values", {})

        def get_value(attr_name: str) -> str:
            attr_values = values.get(attr_name)
            if not attr_values:
                return ""
            for val in attr_values:
                scope_ok = val.get("scope") in (None, channel)
                locale_ok = val.get("locale") in (None, locale)
                if scope_ok and locale_ok:
                    return safe_string_value(val.get("data", ""))
            return safe_string_value(attr_values[0].get("data", ""))

        def get_value_joined(attr_name: str, sep: str = ", ") -> str:
            """Jak get_value, ale łączy listy (np. wielu autorów) separatorem."""
            attr_values = values.get(attr_name)
            if not attr_values:
                return ""
            for val in attr_values:
                scope_ok = val.get("scope") in (None, channel)
                locale_ok = val.get("locale") in (None, locale)
                if scope_ok and locale_ok:
                    data = val.get("data", "")
                    if isinstance(data, list):
                        return sep.join(str(v).strip() for v in data if v)
                    return str(data).strip() if data else ""
            data = attr_values[0].get("data", "")
            if isinstance(data, list):
                return sep.join(str(v).strip() for v in data if v)
            return str(data).strip() if data else ""

        def get_author() -> str:
            return get_value_joined("author") or get_value_joined("autor")

        return {
            "identifier": product.get("identifier", ""),
            "title": get_value("name") or product.get("identifier", ""),
            "description": get_value("description"),
            "author": get_author(),
            "publisher": get_value("publisher") or get_value("wydawnictwo"),
            "year": get_value("year") or get_value("rok_wydania"),
            "pages": get_value("pages") or get_value("liczba_stron"),
            "cover_type": get_value("cover_type") or get_value("oprawa"),
            "ean": get_value("ean"),
            "isbn": get_value("isbn"),
        }
    except Exception:
        return None

def akeneo_update_description(sku: str, html_description: str, channel: str, locale: str = DEFAULT_LOCALE) -> bool:
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt '{sku}' nie istnieje w Akeneo.")

    attr_desc = akeneo_get_attribute("description", token)
    payload = {
        "values": {
            "description": [{
                "data": html_description,
                "scope": channel if attr_desc.get("scopable") else None,
                "locale": locale if attr_desc.get("localizable") else None,
            }]
        }
    }

    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        payload["values"]["opisy_seo"] = [{
            "data": True,
            "scope": channel if attr_seo.get("scopable") else None,
            "locale": locale if attr_seo.get("localizable") else None,
        }]
    except Exception:
        pass

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=AKENEO_TIMEOUT)

    if r.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd Akeneo ({r.status_code}): {r.text[:200]}")

# ═══════════════════════════════════════════════════════════════════
# SESSION STATE & WALIDACJA SECRETS
# ═══════════════════════════════════════════════════════════════════

def _init_session_state():
    defaults = {
        'bulk_results': [],
        'bulk_selected_products': {},
        'products_to_send': {},
        'link_active': False,
        'link_only': False,
        'link_url': '',
        'link_category': '',
        'search_res': [],
        'use_research': True,
        'magento_store_view': 'store_view_bookland',
        'backlog_items': [],
        'backlog_category': '',
    }
    for key, default in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default

_init_session_state()

missing_secrets = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing_secrets:
    st.error(f"❌ Brak kluczy w secrets.toml: {', '.join(missing_secrets)}")
    st.stop()

# ═══════════════════════════════════════════════════════════════════
# HELPERY UI
# ═══════════════════════════════════════════════════════════════════

def _get_internal_link() -> Optional[Dict]:
    """Zwraca dane o linku wewnętrznym z session_state lub None."""
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

def _render_result_preview(r: Dict, token: str, channel: str, locale: str):
    """Renderuje podgląd jednego wyniku (HTML + preview + edytor + research + przycisk regeneracji)."""
    sku = r['sku']
    edit_key = f"edit_{sku}"

    # Inicjalizuj edytor wartością wygenerowaną (jeśli jeszcze nie edytowano)
    if edit_key not in st.session_state:
        st.session_state[edit_key] = r['description_html']

    research = r.get('research')
    tab_labels = ["HTML", "Podgląd", "✏️ Edytuj"]
    if research:
        tab_labels.append("🔎 Research")

    tabs = st.tabs(tab_labels)
    with tabs[0]:
        st.code(r['description_html'], language='html')
    with tabs[1]:
        preview_html = st.session_state.get(edit_key, r['description_html'])
        st.markdown(preview_html, unsafe_allow_html=True)
    with tabs[2]:
        st.caption("Edytuj HTML przed wysyłką. Zmiany zostaną użyte zamiast oryginału.")
        st.text_area(
            "Opis HTML:",
            key=edit_key,
            height=350,
            label_visibility="collapsed",
        )
        if st.session_state.get(edit_key) != r['description_html']:
            st.info("✏️ Opis zmodyfikowany - zostanie wysłana edytowana wersja.")
    if research and len(tabs) > 3:
        with tabs[3]:
            st.caption(f"Perplexity `{PERPLEXITY_MODEL}` - dane użyte do wygenerowania opisu:")
            st.markdown(research)

    meta_title = r.get('meta_title', '')
    meta_description = r.get('meta_description', '')
    if meta_title or meta_description:
        with st.expander("🔍 Metatagi Magento"):
            st.text_input("meta_title", value=meta_title, disabled=True, key=f"mt_{sku}")
            st.text_area("meta_description", value=meta_description, disabled=True, height=80, key=f"md_{sku}")
            chars_mt = len(meta_title)
            chars_md = len(meta_description)
            col_mt, col_md = st.columns(2)
            col_mt.caption(f"{'✅' if chars_mt <= 60 else '⚠️'} {chars_mt}/60 zn.")
            col_md.caption(f"{'✅' if 140 <= chars_md <= 160 else '⚠️'} {chars_md}/160 zn.")

    if st.button("♻️ Regeneruj", key=f"reg_{sku}"):
        internal_link = _get_internal_link()
        link_only = st.session_state.get("link_only", False)
        use_research = st.session_state.get("use_research", True)
        new_res = process_product_from_akeneo(
            sku, token, channel, locale, internal_link, link_only, use_research
        )
        for i, existing in enumerate(st.session_state.bulk_results):
            if existing['sku'] == sku:
                st.session_state.bulk_results[i] = new_res
                break
        # Resetuj edytor do nowego opisu
        st.session_state.pop(edit_key, None)
        st.rerun()

# ═══════════════════════════════════════════════════════════════════
# UI – NAGŁÓWEK
# ═══════════════════════════════════════════════════════════════════

_, col_title = st.columns([1, 5])
with col_title:
    st.markdown(f'<h1 class="main-header">📚 {APP_NAME}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-header">Powered by Google Gemini ({GEMINI_MODEL})</p>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# UI – SIDEBAR
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ Ustawienia")
    st.info(f"🤖 Model aktywny:\n**{GEMINI_MODEL}**")
    st.markdown("---")

    channel = st.selectbox("Kanał:", [DEFAULT_CHANNEL, "B2B"], index=0)
    locale = st.text_input("Locale:", value=DEFAULT_LOCALE)

    st.markdown("---")
    st.header("🔗 Linkowanie wewnętrzne")
    st.session_state.link_active = st.checkbox("Włącz linkowanie", value=st.session_state["link_active"])
    st.session_state.link_only = st.checkbox(
        "Tryb: Tylko dopisanie linku (nie zmieniaj opisu)",
        value=st.session_state["link_only"]
    )
    st.session_state.link_url = st.text_input(
        "URL linku:", placeholder="np. https://bookland.com.pl/beletrystyka",
        value=st.session_state["link_url"]
    )
    st.session_state.link_category = st.text_input(
        "Kategoria/Anchor hint:", placeholder="np. Beletrystyka",
        value=st.session_state["link_category"]
    )

    st.markdown("---")
    st.header("� Perplexity Research")
    st.session_state.use_research = st.checkbox(
        "Wzbogacaj opis researche (Sonar)",
        value=st.session_state["use_research"],
        help="Przed wygenerowaniem opisu Gemini pobierze zweryfikowane informacje o książce z Perplexity Sonar.",
    )
    if st.session_state.use_research:
        st.caption(f"📚 Model: `{PERPLEXITY_MODEL}` | Research nie blokuje generowania w razie błędu.")

    st.markdown("---")
    st.header("🛒 Magento")
    st.session_state.magento_store_view = st.text_input(
        "store_view_code:",
        value=st.session_state["magento_store_view"],
        help="Kod widoku sklepu w Magento (np. 'default', 'pl'). Używany w eksporcie CSV metatagów.",
    )

    st.markdown("---")
    st.header("�📊 Baza produktów")
    optimized = load_optimized_products()
    st.metric("Zoptymalizowane", len(optimized))
    if st.button("🗑️ Wyczyść bazę", type="secondary"):
        save_optimized_products([])
        st.rerun()

# ═══════════════════════════════════════════════════════════════════
# UI – WYBÓR PRODUKTÓW
# ═══════════════════════════════════════════════════════════════════

st.subheader("📦 Przetwarzanie produktów")
method = st.radio("Metoda:", ["🔍 Wyszukaj i zaznacz", "📋 Wklej listę SKU", "📦 Backlog"], horizontal=True)

if method == "🔍 Wyszukaj i zaznacz":
    # Koszyk
    if st.session_state.bulk_selected_products:
        with st.expander(f"🛒 Koszyk ({len(st.session_state.bulk_selected_products)})", expanded=True):
            for sku, data in list(st.session_state.bulk_selected_products.items()):
                c1, c2 = st.columns([5, 1])
                c1.write(f"**{sku}** - {data.get('title')}")
                if c2.button("🗑️", key=f"del_{sku}"):
                    del st.session_state.bulk_selected_products[sku]
                    st.rerun()
            if st.button("Wyczyść koszyk"):
                st.session_state.bulk_selected_products = {}
                st.rerun()

    c_s, c_l = st.columns([4, 1])
    query = c_s.text_input("Szukaj:")
    limit = c_l.number_input("Limit:", 5, 50, 10)

    if st.button("🔍 Szukaj", type="primary") and query:
        with st.spinner("Szukam..."):
            token_search = akeneo_get_token()
            st.session_state.search_res = akeneo_search_products(query, token_search, limit, locale)
            if not st.session_state.search_res:
                st.warning("Brak wyników")

    if st.session_state.search_res:
        st.write("---")
        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        for p in st.session_state.search_res:
            sku = p['identifier']
            is_selected = sku in st.session_state.bulk_selected_products
            if st.checkbox(f"{sku} - {p['title']}", value=is_selected, key=f"s_{sku}"):
                st.session_state.bulk_selected_products[sku] = {'title': p['title']}
            elif is_selected:
                del st.session_state.bulk_selected_products[sku]
        st.markdown('</div>', unsafe_allow_html=True)

elif method == "📋 Wklej listę SKU":
    txt = st.text_area("SKU (jeden na linię):", height=150)
    if st.button("Załaduj SKU", type="primary"):
        skus = [s.strip() for s in txt.splitlines() if s.strip()]
        for s in skus:
            st.session_state.bulk_selected_products[s] = {'title': s}
        st.success(f"Dodano {len(skus)} SKU")
        st.rerun()

    if st.session_state.bulk_selected_products:
        st.info(f"W koszyku: {len(st.session_state.bulk_selected_products)}")
        if st.button("Wyczyść"):
            st.session_state.bulk_selected_products = {}
            st.rerun()

else:  # 📦 Backlog
    st.markdown("Produkty **aktywne** (`enabled=true`) bez zoptymalizowanego opisu. Stany magazynowe w Magento — tu filtrujemy tylko po aktywności w Akeneo.")

    # Kategorie
    tok_bl = akeneo_get_token()
    with st.spinner("Ładuję kategorie..."):
        all_categories = akeneo_fetch_categories(tok_bl, locale)

    cat_options = {"": "📂 Wszystkie kategorie"}
    for c in all_categories:
        cat_options[c["code"]] = f"{c['label']} ({c['code']})"

    selected_cat_code = st.selectbox(
        "Filtruj po kategorii:",
        options=list(cat_options.keys()),
        format_func=lambda x: cat_options[x],
        index=0,
        key="backlog_category_select",
    )
    st.session_state.backlog_category = selected_cat_code

    col_bl, col_lim = st.columns([3, 1])
    backlog_limit = col_lim.number_input("Ile załadować:", min_value=10, max_value=500, value=100, step=10)

    if col_bl.button("🔄 Załaduj backlog", type="primary"):
        with st.spinner("Pobieram backlog z Akeneo..."):
            st.session_state.backlog_items = akeneo_fetch_backlog(
                tok_bl, channel, locale, backlog_limit,
                category=selected_cat_code or None,
            )
        st.rerun()

    backlog = st.session_state.backlog_items
    if backlog:
        st.caption(f"Znaleziono **{len(backlog)}** produktów w backlogu. Sortowanie: brak opisu → krótki opis.")

        col_n, col_sel_btn, col_clr = st.columns([2, 2, 2])
        n_select = col_n.number_input("Zaznacz pierwszych N:", min_value=1, max_value=len(backlog), value=min(10, len(backlog)), step=1)
        if col_sel_btn.button(f"✅ Zaznacz pierwsze {n_select}"):
            for p in backlog[:n_select]:
                st.session_state.bulk_selected_products[p['identifier']] = {'title': p['title']}
            st.rerun()
        if col_clr.button("🗑️ Wyczyść zaznaczenie"):
            for p in backlog:
                st.session_state.bulk_selected_products.pop(p['identifier'], None)
            st.rerun()

        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        for p in backlog:
            sku_bl = p['identifier']
            is_sel = sku_bl in st.session_state.bulk_selected_products
            desc_info = f"({p['desc_len']} zn.)" if p['desc_len'] > 0 else "(brak opisu)"
            label = f"{sku_bl} — {p['title']} {desc_info}"
            if st.checkbox(label, value=is_sel, key=f"bl_{sku_bl}"):
                st.session_state.bulk_selected_products[sku_bl] = {'title': p['title']}
            elif is_sel:
                del st.session_state.bulk_selected_products[sku_bl]
        st.markdown('</div>', unsafe_allow_html=True)

        if st.session_state.bulk_selected_products:
            st.success(f"🛒 W kolejce: **{len(st.session_state.bulk_selected_products)}** produktów — przejdź niżej do sekcji Generowanie.")

# ═══════════════════════════════════════════════════════════════════
# UI – GENEROWANIE
# ═══════════════════════════════════════════════════════════════════

if st.session_state.bulk_selected_products:
    st.markdown("---")
    st.subheader("🚀 Generowanie")

    if st.button("▶️ Start Generowania (Gemini)", type="primary"):
        st.session_state.bulk_results = []
        st.session_state.products_to_send = {}
        token = akeneo_get_token()
        skus = list(st.session_state.bulk_selected_products.keys())
        internal_link = _get_internal_link()
        link_only = st.session_state.get("link_only", False)
        use_research = st.session_state.get("use_research", True)

        bar = st.progress(0, "Start...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    process_product_from_akeneo,
                    s, token, channel, locale, internal_link, link_only, use_research
                ): s
                for s in skus
            }
            for i, future in enumerate(as_completed(futures)):
                res = future.result()
                st.session_state.bulk_results.append(res)
                if not res.get('error'):
                    st.session_state.products_to_send[res['sku']] = True
                bar.progress((i + 1) / len(skus), f"Przetworzono {res['sku']}")

        bar.progress(1.0, "Gotowe!")
        st.rerun()

# ═══════════════════════════════════════════════════════════════════
# UI – WYNIKI
# ═══════════════════════════════════════════════════════════════════

if st.session_state.bulk_results:
    st.markdown("---")
    results = st.session_state.bulk_results
    ok = [r for r in results if not r.get('error')]
    err = [r for r in results if r.get('error')]

    c1, c2, _ = st.columns(3)
    c1.metric("✅ Sukces", len(ok))
    c2.metric("❌ Błędy", len(err))

    df = pd.DataFrame(results)
    col_csv1, col_csv2 = st.columns(2)
    col_csv1.download_button("⬇️ Pobierz CSV (wyniki)", df.to_csv(index=False).encode('utf-8'), 'wyniki.csv', 'text/csv')

    # CSV do importu metatagów w Magento
    store_view = st.session_state.get("magento_store_view", "default")
    meta_rows = [
        {
            'sku': r['sku'],
            'store_view_code': store_view,
            'meta_title': r.get('meta_title', ''),
            'meta_description': r.get('meta_description', ''),
        }
        for r in ok
        if r.get('meta_title') or r.get('meta_description')
    ]
    if meta_rows:
        meta_df = pd.DataFrame(meta_rows, columns=['sku', 'store_view_code', 'meta_title', 'meta_description'])
        col_csv2.download_button(
            "⬇️ Pobierz CSV Magento (metatagi)",
            meta_df.to_csv(index=False, sep='\t').encode('utf-8'),
            'magento_metatagi.csv',
            'text/csv',
        )

    # Wysyłka do Akeneo
    if ok:
        st.subheader("📤 Wysyłka do Akeneo")

        c_all, c_none = st.columns(2)
        if c_all.button("Zaznacz wszystko"):
            for r in ok:
                st.session_state.products_to_send[r['sku']] = True
            st.rerun()
        if c_none.button("Odznacz wszystko"):
            for r in ok:
                st.session_state.products_to_send[r['sku']] = False
            st.rerun()

        to_send_list = []
        for r in ok:
            checked = st.checkbox(
                f"{r['sku']} - {r['title']}",
                value=st.session_state.products_to_send.get(r['sku'], True),
                key=f"send_{r['sku']}"
            )
            st.session_state.products_to_send[r['sku']] = checked
            if checked:
                to_send_list.append(r)

        if st.button(f"📤 Wyślij zaznaczone ({len(to_send_list)})", type="primary"):
            bar_s = st.progress(0, "Wysyłanie...")
            successes, errors = 0, []
            for i, item in enumerate(to_send_list):
                try:
                    final_html = st.session_state.get(f"edit_{item['sku']}", item['description_html'])
                    akeneo_update_description(item['sku'], final_html, channel, locale)
                    add_optimized_product(item['sku'], item['title'], item['url'])
                    successes += 1
                except Exception as e:
                    errors.append(f"{item['sku']}: {e}")
                bar_s.progress((i + 1) / len(to_send_list))

            st.success(f"✅ Wysłano {successes} produktów")
            if errors:
                st.error('\n'.join(errors))

    # Podgląd wyników
    st.markdown("---")
    st.subheader("Podgląd wyników")
    token_preview = akeneo_get_token()

    for r in results:
        label = "✅" if not r.get('error') else "❌"
        with st.expander(f"{label} {r['sku']} - {r.get('title', '')}"):
            if r.get('error'):
                st.error(r['error'])
            else:
                _render_result_preview(r, token_preview, channel, locale)

# ═══════════════════════════════════════════════════════════════════
# UI – STOPKA
# ═══════════════════════════════════════════════════════════════════

st.markdown("---")
st.caption(f"{APP_NAME} v{APP_VERSION} | Powered by {GEMINI_MODEL}")
