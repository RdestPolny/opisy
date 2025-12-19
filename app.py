import streamlit as st
import pandas as pd
import requests
from openai import OpenAI
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import time
from datetime import datetime
from pathlib import Path

# Dodajemy obsługę Google Gemini
import google.generativeai as genai

# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STRONY
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Generator Opisów Produktów v3.1.0",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS dla lepszego UI
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
    .metric-card {
        background: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        text-align: center;
    }
    .success-box {
        padding: 1rem;
        background: #d4edda;
        border-left: 4px solid #28a745;
        border-radius: 0.25rem;
        margin: 1rem 0;
    }
    .info-box {
        padding: 1rem;
        background: #d1ecf1;
        border-left: 4px solid #17a2b8;
        border-radius: 0.25rem;
        margin: 1rem 0;
    }
    .warning-box {
        padding: 1rem;
        background: #fff3cd;
        border-left: 4px solid #ffc107;
        border-radius: 0.25rem;
        margin: 1rem 0;
    }
    .error-box {
        padding: 1rem;
        background: #f8d7da;
        border-left: 4px solid #dc3545;
        border-radius: 0.25rem;
        margin: 1rem 0;
    }
    .scrollable-results {
        max-height: 400px;
        overflow-y: auto;
        border: 1px solid #e0e0e0;
        border-radius: 0.5rem;
        padding: 1rem;
        background: #fafafa;
    }
    .sticky-actions {
        position: sticky;
        top: 60px;
        z-index: 100;
        background: white;
        padding: 1rem;
        border: 2px solid #e0e0e0;
        border-radius: 0.5rem;
        margin: 1rem 0;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# BAZA ZOPTYMALIZOWANYCH PRODUKTÓW
# ═══════════════════════════════════════════════════════════════════

DB_PATH = Path(".streamlit/optimized_products.json")

def ensure_db_exists():
    """Tworzy plik bazy jeśli nie istnieje"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        DB_PATH.write_text("[]")

def load_optimized_products() -> List[Dict]:
    """Wczytuje bazę zoptymalizowanych produktów"""
    ensure_db_exists()
    try:
        with open(DB_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

def save_optimized_products(products: List[Dict]):
    """Zapisuje bazę zoptymalizowanych produktów"""
    ensure_db_exists()
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

def add_optimized_product(sku: str, title: str, url: str):
    """Dodaje produkt do bazy zoptymalizowanych"""
    products = load_optimized_products()
    
    existing = next((p for p in products if p['sku'] == sku), None)
    
    if existing:
        existing['last_optimized'] = datetime.now().isoformat()
        existing['title'] = title
        existing['url'] = url
    else:
        products.append({
            'sku': sku,
            'title': title,
            'url': url,
            'first_optimized': datetime.now().isoformat(),
            'last_optimized': datetime.now().isoformat()
        })
    
    save_optimized_products(products)

def generate_product_url(title: str) -> str:
    """Generuje URL produktu na podstawie nazwy"""
    slug = title.lower()
    
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'a', 'Ć': 'c', 'Ę': 'e', 'Ł': 'l', 'Ń': 'n',
        'Ó': 'o', 'Ś': 's', 'Ź': 'z', 'Ż': 'z'
    }
    for old, new in replacements.items():
        slug = slug.replace(old, new)
    
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    slug = slug.strip('-')
    
    return f"https://bookland.com.pl/{slug}"


# ═══════════════════════════════════════════════════════════════════
# FUNKCJE POMOCNICZE
# ═══════════════════════════════════════════════════════════════════

def strip_code_fences(text: str) -> str:
    """Usuwa markdown code fences z odpowiedzi AI"""
    if not text:
        return text
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m:
        return m.group(1).strip()
    text = re.sub(r"^\s*```(?:html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()

def clean_ai_fingerprints(text: str) -> str:
    """Usuwa 'odciski palca AI' - em dash, en dash, etc."""
    text = text.replace('—', '-')
    text = text.replace('–', '-')
    text = text.replace('…', '...')
    return text

def remove_checklist_from_output(text: str) -> str:
    """Usuwa checklist z końca odpowiedzi (dla gpt-4o-mini i gemini)"""
    if "Checklist:" in text or "checklist:" in text:
        text = re.split(r'[Cc]hecklist:', text)[0]
    
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        if line.strip().startswith('☑'):
            continue
        cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines).strip()

def safe_string_value(value) -> str:
    """
    Bezpiecznie konwertuje wartość na string.
    Obsługuje zarówno stringi jak i listy z Akeneo.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        # Jeśli lista, weź pierwszy element lub pusty string
        if len(value) > 0:
            return str(value[0]).strip()
        return ""
    if isinstance(value, str):
        return value.strip()
    # Dla innych typów (int, float, etc.)
    return str(value).strip()

def format_product_title(title: str, max_length: int = 80) -> str:
    """Formatuje tytuł produktu"""
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    return title

def validate_description_quality(description) -> Tuple[str, str]:
    """
    Waliduje jakość oryginalnego opisu
    Zwraca: (status, message)
    status: 'ok', 'warning', 'error'
    """
    # Bezpieczna konwersja na string
    desc_str = safe_string_value(description)
    
    if not desc_str or len(desc_str.strip()) == 0:
        return 'error', '❌ Brak oryginalnego opisu w Akeneo! Wygenerowany opis może być niskiej jakości.'
    
    desc_length = len(desc_str.strip())
    
    if desc_length < 100:
        return 'error', f'❌ Oryginalny opis bardzo krótki ({desc_length} znaków)! AI będzie miał za mało informacji.'
    elif desc_length < 300:
        return 'warning', f'⚠️ Oryginalny opis dość krótki ({desc_length} znaków). Rozważ wzbogacenie opisu w Akeneo.'
    else:
        return 'ok', f'✅ Oryginalny opis ma odpowiednią długość ({desc_length} znaków)'

# ═══════════════════════════════════════════════════════════════════
# AKENEO API
# ═══════════════════════════════════════════════════════════════════

def _akeneo_root():
    """Zwraca root URL Akeneo"""
    base = st.secrets["AKENEO_BASE_URL"].rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[:-len("/api/rest/v1")]
    return base

@st.cache_data(ttl=3000, show_spinner=False)
def akeneo_get_token() -> str:
    """
    Pobiera access token dla Akeneo API z cachem i lepszą obsługą błędów.
    TTL 3000s = 50 minut.
    """
    try:
        token_url = _akeneo_root() + "/api/oauth/v1/token"
        
        # Sprawdzenie czy mamy dane w secrets
        if not all(k in st.secrets for k in ["AKENEO_CLIENT_ID", "AKENEO_SECRET", "AKENEO_USERNAME", "AKENEO_PASSWORD"]):
            st.error("❌ Brak kompletnych danych logowania w secrets.toml")
            st.stop()

        auth = (st.secrets["AKENEO_CLIENT_ID"], st.secrets["AKENEO_SECRET"])
        data = {
            "grant_type": "password",
            "username": st.secrets["AKENEO_USERNAME"],
            "password": st.secrets["AKENEO_PASSWORD"],
        }
        
        r = requests.post(token_url, auth=auth, data=data, timeout=30)
        
        if r.status_code != 200:
            st.error(f"❌ Błąd autoryzacji Akeneo (Kod: {r.status_code})")
            st.markdown(f"**URL:** {token_url}")
            try:
                error_details = r.json()
                st.json(error_details)
            except:
                st.text(r.text)
            st.stop()
            
        return r.json()["access_token"]
        
    except requests.exceptions.ConnectionError:
        st.error(f"❌ Nie można połączyć się z Akeneo pod adresem: {_akeneo_root()}")
        st.info("Sprawdź czy URL w AKENEO_BASE_URL jest poprawny.")
        st.stop()
    except Exception as e:
        st.error(f"❌ Nieoczekiwany błąd podczas pobierania tokenu: {str(e)}")
        st.stop()

def akeneo_get_attribute(code: str, token: str) -> Dict:
    """Pobiera definicję atrybutu z Akeneo"""
    url = _akeneo_root() + f"/api/rest/v1/attributes/{code}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()

def akeneo_product_exists(sku: str, token: str) -> bool:
    """Sprawdza czy produkt istnieje w Akeneo"""
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.status_code == 200

def akeneo_search_products(search_query: str, token: str, limit: int = 20, locale: str = "pl_PL") -> List[Dict]:
    """Wyszukuje produkty w Akeneo po nazwie lub SKU"""
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    
    products_dict = {}
    
    try:
        # Wyszukiwanie po identyfikatorze (SKU)
        params_identifier = {
            "limit": limit,
            "search": json.dumps({
                "identifier": [{"operator": "CONTAINS", "value": search_query}]
            })
        }
        
        r1 = requests.get(url, headers=headers, params=params_identifier, timeout=30)
        r1.raise_for_status()
        data1 = r1.json()
        
        for item in data1.get("_embedded", {}).get("items", []):
            identifier = item.get("identifier", "")
            title = identifier
            values = item.get("values", {})
            if "name" in values:
                name_values = values["name"]
                for val in name_values:
                    if val.get("locale") == locale or val.get("locale") is None:
                        title_data = val.get("data", identifier)
                        title = safe_string_value(title_data) or identifier
                        break
            
            products_dict[identifier] = {
                "identifier": identifier,
                "title": title,
                "family": item.get("family", ""),
                "enabled": item.get("enabled", False),
                "raw_data": item
            }
        
        # Wyszukiwanie po atrybucie "name"
        params_name = {
            "limit": limit,
            "search": json.dumps({
                "name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]
            })
        }
        
        r2 = requests.get(url, headers=headers, params=params_name, timeout=30)
        r2.raise_for_status()
        data2 = r2.json()
        
        for item in data2.get("_embedded", {}).get("items", []):
            identifier = item.get("identifier", "")
            if identifier in products_dict:
                continue
            
            title = identifier
            values = item.get("values", {})
            if "name" in values:
                name_values = values["name"]
                for val in name_values:
                    if val.get("locale") == locale or val.get("locale") is None:
                        title_data = val.get("data", identifier)
                        title = safe_string_value(title_data) or identifier
                        break
            
            products_dict[identifier] = {
                "identifier": identifier,
                "title": title,
                "family": item.get("family", ""),
                "enabled": item.get("enabled", False),
                "raw_data": item
            }
        
        products = list(products_dict.values())
        products.sort(key=lambda x: x['title'].lower())
        
        return products[:limit]
        
    except Exception as e:
        st.error(f"Błąd wyszukiwania: {str(e)}")
        return []

def akeneo_get_product_details(sku: str, token: str, channel: str = "Bookland", locale: str = "pl_PL") -> Optional[Dict]:
    """Pobiera pełne dane produktu z Akeneo"""
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        product = r.json()
        
        values = product.get("values", {})
        
        def get_value(attr_name: str) -> str:
            if attr_name not in values:
                return ""
            attr_values = values[attr_name]
            if not attr_values:
                return ""
            
            for val in attr_values:
                val_scope = val.get("scope")
                val_locale = val.get("locale")
                if (val_scope is None or val_scope == channel) and \
                   (val_locale is None or val_locale == locale):
                    data = val.get("data", "")
                    return safe_string_value(data)
            
            first_data = attr_values[0].get("data", "")
            return safe_string_value(first_data)
        
        product_data = {
            "identifier": product.get("identifier", ""),
            "family": product.get("family", ""),
            "enabled": product.get("enabled", False),
            "title": get_value("name") or product.get("identifier", ""),
            "description": get_value("description"),
            "short_description": get_value("short_description"),
            "ean": get_value("ean"),
            "isbn": get_value("isbn"),
            "author": get_value("author") or get_value("autor"),
            "publisher": get_value("publisher") or get_value("wydawnictwo"),
            "year": get_value("year") or get_value("rok_wydania"),
            "pages": get_value("pages") or get_value("liczba_stron"),
            "cover_type": get_value("cover_type") or get_value("oprawa"),
            "dimensions": get_value("dimensions") or get_value("wymiary"),
            "age": get_value("age") or get_value("wiek"),
            "category": get_value("category") or get_value("kategoria"),
            "raw_values": values
        }
        
        return product_data
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return None
        raise e

def akeneo_update_description(sku: str, html_description: str, channel: str, locale: str = "pl_PL") -> bool:
    """Aktualizuje opis produktu w Akeneo"""
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt '{sku}' nie istnieje w Akeneo.")
    
    attr_desc = akeneo_get_attribute("description", token)
    is_scopable_desc = bool(attr_desc.get("scopable", False))
    is_localizable_desc = bool(attr_desc.get("localizable", False))
    
    value_obj_desc = {
        "data": html_description,
        "scope": channel if is_scopable_desc else None,
        "locale": locale if is_localizable_desc else None,
    }
    
    payload_values = {"description": [value_obj_desc]}

    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        is_scopable_seo = bool(attr_seo.get("scopable", False))
        is_localizable_seo = bool(attr_seo.get("localizable", False))
        
        value_obj_seo = {
            "data": True,
            "scope": channel if is_scopable_seo else None,
            "locale": locale if is_localizable_seo else None,
        }
        payload_values["opisy_seo"] = [value_obj_seo]
    except:
        pass

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    payload = {"values": payload_values}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=30)
    
    if r.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd Akeneo ({r.status_code})")

# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - OBSŁUGA GEMINI I GPT
# ═══════════════════════════════════════════════════════════════════

def generate_description(product_data: Dict, client: OpenAI, model: str = "gpt-5-nano", style_variant: str = "default") -> str:
    """Generuje opis produktu z wykorzystaniem wybranego modelu (GPT lub Gemini)"""
    try:
        # Podstawowy system prompt (wspólny)
        system_prompt = """Jesteś EKSPERTEM copywritingu e-commerce i języka polskiego. Twoje opisy są poprawne gramatycznie, angażujące i konwertują.

╔═══════════════════════════════════════════════════════════════════╗
║  ABSOLUTNIE KRYTYCZNE ZASADY (NAJWAŻNIEJSZE!)                     ║
╚═══════════════════════════════════════════════════════════════════╝

**1. FORMATOWANIE NAZWISK I NAZW (NAJCZĘSTSZE BŁĘDY!):**

✅ ZAWSZE formatuj nazwiska poprawnie:
- Z wielkiej litery: "Joanna Balicka" (NIE "joannabalicka")
- Z spacjami: "Remigiusz Mróz" (NIE "remigiusz mroz")
- Z polskimi znakami: "Zuzanna Kołucka" (NIE "Zuzanna Kolucka")
- Oba imiona osobno: "Ewa Krassowska-Mackiewicz i Karolina Tarasiuk" (NIE "ewakrassowskamackiewicz i karolinatarasiuk")

✅ ZAWSZE formatuj nazwy wydawnictw z wielkiej litery:
- "NieZwykłe" (NIE "niezwykle")
- "Wydawnictwo Edgard" (NIE "edgard")

**2. POPRAWNA ODMIANA (PRZYPADKI GRAMATYCZNE):**

✅ Dopełniacz (kogo? czego?):
- "część Wiedźmina" (NIE "część Wiedźmin")
- "pełnego spisków i machinacji" (NIE "pełnego spiski i machinacje")
- "autorstwa Joanny Balickiej" (NIE "autorstwa Joanna Balicka")

✅ Celownik (komu? czemu?):
- "dzięki dopracowanym komponentom" (NIE "dzięki dopracowanymi komponentami")
- "Helence, Krzysiowi i Wojtusiowi" (NIE "Helence, Krzysia i Wojtuś")

✅ Zgoda rzeczownika z przymiotnikiem:
- "bogata ilustracja" (NIE "bogaty ilustracja")
- "połączenie przygody i nauki" (NIE "połączenie przygody i nauka")

**3. ABSOLUTNY ZAKAZ DUPLICATE CONTENT:**

❌ NIGDY nie powtarzaj tych samych zdań czy fraz w różnych miejscach!
❌ Każde zdanie musi być unikalne i wnosić nowe informacje
❌ Szczególnie uważaj na:
- Powtarzanie danych technicznych (autor, wydawnictwo, oprawa)
- Powtarzanie CTA w różnych miejscach
- Powtarzanie liczb/specyfikacji w różnych akapitach

✅ Sprawdź przed wysłaniem:
- Czy nie ma dwóch identycznych lub bardzo podobnych zdań?
- Czy dane techniczne występują tylko RAZ?
- Czy każdy element wnosi coś nowego?

**4. BOLDOWANIE - OBOWIĄZKOWE W PIERWSZYM AKAPICIE:**

✅ ZAWSZE w pierwszym akapicie zbolduj:
- Tytuł produktu (lub część tytułu)
- Imię i nazwisko autora (formatowane poprawnie!)
- 2-4 inne kluczowe słowa/frazy (nie więcej niż 8-10 bold w całym tekście)

Przykład: "Odkryj <b>Czas Pogardy</b> autorstwa <b>Andrzeja Sapkowskiego</b> - czwartą część <b>sagi o Wiedźminie</b>, która..."

**5. DANE TECHNICZNE - NATURALNE WPLECENIE:**

✅ Wpleć dane techniczne SUBTELNIE i NATURALNIE w drugi akapit
✅ Nigdy nie twórz osobnej sekcji "Dane techniczne:"
✅ Nie wymieniaj wszystkich danych na raz w jednym zdaniu

Przykłady DOBRYCH wplecień:
- "Wydanie w eleganckiej twardej oprawie od SuperNowej to pozycja, która..."
- "Tom z 2023 roku, liczący 320 stron, przenosi czytelników w świat..."
- "Publikacja autorstwa Joanny Balickiej, wydana przez Edgard, łączy..."

Przykłady ZŁYCH wplecień (NIGDY tak nie pisz!):
❌ "Wyjątkowo miękka oprawa i autor joannabalicka gwarantują..."
❌ "Dane techniczne: Autor remigiusz mroz; wydawnictwo wab; oprawa miekka"
❌ "Tom 1, w wersji miękkiej oprawy, autorstwa..."

╔═══════════════════════════════════════════════════════════════════╗
║  STRUKTURA OPISU (ELASTYCZNA!)                                    ║
╚═══════════════════════════════════════════════════════════════════╝

**WARIANT A (bardziej rozbudowany):**
<p>[AKAPIT 1: 4-6 zdań. OBOWIĄZKOWO zbolduj tytuł i autora.]</p>
<h2>[Nagłówek 1 - korzyść/cecha]</h2>
<p>[AKAPIT 2: 5-7 zdań. Tutaj naturalnie wpleć dane techniczne. BEZ CTA na końcu!]</p>
<h2>[Nagłówek 2 - inna korzyść/aspekt]</h2>
<p>[AKAPIT 3: 4-6 zdań. NA KOŃCU dodaj CTA - tylko tutaj!]</p>
<h3>[Krótkie wezwanie do działania]</h3>

**WARIANT B (zwięzły):**
<p>[AKAPIT 1: 4-6 zdań. OBOWIĄZKOWO zbolduj tytuł i autora.]</p>
<h2>[Nagłówek - główna korzyść]</h2>
<p>[AKAPIT 2: 6-9 zdań. Dane techniczne wplecione naturalnie. NA KOŃCU CTA - tylko tutaj!]</p>
<h3>[Krótkie wezwanie do działania]</h3>

**WARIANT C (minimalny - tylko dla prostych produktów):**
<p>[AKAPIT 1: 5-7 zdań. OBOWIĄZKOWO zbolduj tytuł i autora.]</p>
<h2>[Nagłówek]</h2>
<p>[AKAPIT 2: 7-10 zdań. Wszystko tutaj. NA KOŃCU CTA - tylko tutaj!]</p>
<h3>[Wezwanie do działania]</h3>

**KRYTYCZNE ZASADY STRUKTURY:**
- Wybierz wariant A, B lub C w zależności od produktu (RÓŻNICUJ!)
- CTA tylko RAZ - na końcu ostatniego akapitu przed H3
- H3 to ostatni element - nic po nim!
- Nigdy nie duplikuj informacji między akapitami

╔═══════════════════════════════════════════════════════════════════╗
║  CTA (CALL TO ACTION) - TYLKO RAZ!                                ║
╚═══════════════════════════════════════════════════════════════════╝

✅ CTA pojawia się TYLKO JEDEN RAZ - jako ostatnie 1-2 zdania ostatniego akapitu <p>
✅ H3 może być CTA, ale krótkie i różne od CTA w akapicie

❌ NIGDY nie duplikuj CTA:
- NIE kopiuj tego samego zdania CTA w akapit i H3
- NIE używaj bardzo podobnych sformułowań

Przykład DOBRY:
Akapit kończy się: "Zamów teraz i odkryj magiczny świat Wiedźmina. Nie zwlekaj - dodaj do koszyka już dziś."
H3: "Dołącz do legendy"

Przykład ZŁY (NIGDY tak nie rób!):
Akapit kończy się: "Zamów teraz i dołącz do detektywów w poszukiwaniu zdrowia."
H3: "Zamów teraz i dołącz do detektywów w poszukiwaniu zdrowia"

╔═══════════════════════════════════════════════════════════════════╗
║  DŁUGOŚĆ I SZCZEGÓŁY                                               ║
╚═══════════════════════════════════════════════════════════════════╝

- Całość: 1400-2500 znaków (w zależności od wariantu)
- Każdy akapit: minimum 4 zdania, minimum 300 znaków
- 6-10 słów/fraz zboldowanych w całym tekście
- Ton dostosowany do produktu
- Tylko myślnik "-" (NIE em dash "—" ani en dash "–")
"""

        # Modyfikacja promptu dla modeli
        if model == "gpt-5-nano":
            system_prompt += """
╔═══════════════════════════════════════════════════════════════════╗
║  OSTATECZNY CHECKLIST PRZED WYSŁANIEM                              ║
╚═══════════════════════════════════════════════════════════════════╝

☑ Tytuł i autor zboldowane w pierwszym akapicie?
☑ Wszystkie nazwiska z WIELKICH liter i ze spacjami?
☑ Nazwy wydawnictw z wielkich liter?
☑ Wszystkie polskie znaki (ł, ą, ę, etc.)?
☑ Wszystkie przypadki poprawnie odmienione?
☑ Dane techniczne wplecione naturalnie (BEZ "Dane techniczne:")?
☑ BRAK duplicate content - każde zdanie unikalne?
☑ CTA tylko RAZ na końcu ostatniego akapitu?
☑ H3 krótkie i różne od CTA w akapicie?
☑ KONIEC na H3 - nic więcej?
☑ Tylko myślnik "-" (bez em/en dash)?
☑ Wybrany wariant struktury (A, B lub C) pasuje do produktu?

Jeśli któreś NIE - POPRAW przed wysłaniem!

Zwróć TYLKO czysty HTML.
Sprawdź WSZYSTKIE punkty checklisty!
"""
        else:
            # Dla gpt-4o-mini i GEMINI - instrukcja formatowania bez checklisty w output
            system_prompt += """

╔═══════════════════════════════════════════════════════════════════╗
║  TWOJA ODPOWIEDŹ                                                   ║
╚═══════════════════════════════════════════════════════════════════╝

KRYTYCZNE: Zwróć TYLKO i WYŁĄCZNIE czysty kod HTML opisu produktu.
NIE dodawaj NICZEGO więcej - żadnych komentarzy, checklistów ani notatek.
Nie używaj znaczników markdown (```html).

Twoja odpowiedź powinna zaczynać się od <p> i kończyć na </h3>.
Tylko HTML, nic więcej!
"""

        raw_data = f"""
TYTUŁ PRODUKTU (zbolduj w pierwszym akapicie!):
{product_data.get('title', '')}

AUTOR (zbolduj w pierwszym akapicie! Formatuj poprawnie: wielka litera, spacje, polskie znaki!):
{product_data.get('author', '')}

SZCZEGÓŁY TECHNICZNE (wpleć NATURALNIE w jeden z akapitów, NIE wszystkie naraz!):
{product_data.get('details', '')}

ORYGINALNY OPIS (główne źródło informacji o produkcie):
{product_data.get('description', '')}
"""

        # ---------------------------------------------------------
        # OBSŁUGA GOOGLE GEMINI
        # ---------------------------------------------------------
        if "gemini" in model.lower():
            if "GOOGLE_API_KEY" not in st.secrets:
                return "BŁĄD: Brak klucza GOOGLE_API_KEY w secrets.toml"

            genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
            
            # Inicjalizacja modelu z system instruction
            model_instance = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt
            )
            
            # Wywołanie generowania
            response = model_instance.generate_content(raw_data)
            
            result = strip_code_fences(response.text)
            result = remove_checklist_from_output(result)
            result = clean_ai_fingerprints(result)
            return result

        # ---------------------------------------------------------
        # OBSŁUGA OPENAI GPT
        # ---------------------------------------------------------
        else:
            if model == "gpt-5-nano":
                response = client.responses.create(
                    model="gpt-5-nano",
                    input=f"{system_prompt}\n\n{raw_data}",
                    reasoning={"effort": "high"},
                    text={"verbosity": "medium"}
                )
                result = strip_code_fences(response.output_text)
            else:  # gpt-4o-mini
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_data}
                    ],
                    temperature=0.7,
                    max_tokens=2500
                )
                result = strip_code_fences(response.choices[0].message.content)
                result = remove_checklist_from_output(result)
            
            result = clean_ai_fingerprints(result)
            return result
        
    except Exception as e:
        return f"BŁĄD: {str(e)}"

def generate_meta_tags(product_data: Dict, client: OpenAI, model: str = "gpt-5-nano") -> Tuple[str, str]:
    """Generuje meta title i meta description"""
    try:
        system_prompt = """Ekspert SEO.

Meta Title: max 60 znaków, słowo kluczowe na początku, myślnik "-", bez kropek
Meta Description: max 160 znaków, CTA, myślnik "-"

FORMAT:
Meta title: [treść]
Meta description: [treść]"""
        
        user_prompt = f"Produkt: {product_data.get('title', '')}\nDane: {product_data.get('details', '')} {product_data.get('description', '')}"

        # ---------------------------------------------------------
        # OBSŁUGA GOOGLE GEMINI
        # ---------------------------------------------------------
        if "gemini" in model.lower():
             if "GOOGLE_API_KEY" not in st.secrets:
                return "", ""
             
             genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
             model_instance = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt
             )
             response = model_instance.generate_content(user_prompt)
             result = response.text

        # ---------------------------------------------------------
        # OBSŁUGA OPENAI GPT
        # ---------------------------------------------------------
        else:
            if model == "gpt-5-nano":
                response = client.responses.create(
                    model="gpt-5-nano",
                    input=f"{system_prompt}\n\n{user_prompt}",
                    reasoning={"effort": "medium"},
                    text={"verbosity": "low"}
                )
                result = response.output_text
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.5,
                    max_tokens=300
                )
                result = response.choices[0].message.content
        
        meta_title = ""
        meta_description = ""
        
        for line in result.splitlines():
            line = line.strip()
            if line.lower().startswith("meta title:"):
                meta_title = line[len("meta title:"):].strip()
            elif line.lower().startswith("meta description:"):
                meta_description = line[len("meta description:"):].strip()
        
        meta_title = clean_ai_fingerprints(meta_title).rstrip('.')
        meta_description = clean_ai_fingerprints(meta_description)
        
        if len(meta_title) > 60:
            meta_title = meta_title[:57] + "..."
        if len(meta_description) > 160:
            meta_description = meta_description[:157] + "..."
            
        return meta_title, meta_description
        
    except Exception as e:
        return "", ""

def process_product_from_akeneo(sku: str, client: OpenAI, token: str, channel: str, locale: str, model: str = "gpt-5-nano") -> Dict:
    """Przetwarza pojedynczy produkt z Akeneo"""
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        
        if not product_details:
            return {
                'sku': sku,
                'title': '',
                'description_html': '',
                'url': '',
                'error': 'Produkt nie znaleziony',
                'description_quality': ('error', 'Produkt nie znaleziony')
            }
        
        # Przygotowanie danych z poprawnym formatowaniem i bezpieczną konwersją
        details_parts = []
        
        # Bezpieczne pobieranie wartości (obsługa list i stringów)
        author = safe_string_value(product_details.get('author'))
        if author:
            details_parts.append(f"Autor: {author}")
        
        publisher = safe_string_value(product_details.get('publisher'))
        if publisher:
            details_parts.append(f"Wydawnictwo: {publisher}")
        
        year = safe_string_value(product_details.get('year'))
        if year:
            details_parts.append(f"Rok: {year}")
        
        pages = safe_string_value(product_details.get('pages'))
        if pages:
            details_parts.append(f"Strony: {pages}")
        
        cover_type = safe_string_value(product_details.get('cover_type'))
        if cover_type:
            details_parts.append(f"Oprawa: {cover_type}")
        
        # Sprawdź jakość oryginalnego opisu
        original_desc = product_details.get('description', '') or product_details.get('short_description', '')
        original_desc = safe_string_value(original_desc)
        quality_status, quality_msg = validate_description_quality(original_desc)
        
        # Generuj URL produktu
        product_title = safe_string_value(product_details['title'])
        product_url = generate_product_url(product_title)
        
        product_data = {
            'title': product_title,
            'author': author,
            'details': '\n'.join(details_parts),
            'description': original_desc
        }
        
        # Generowanie - model przekazywany dalej
        description_html = generate_description(product_data, client, model, "default")
        
        if "BŁĄD" in description_html:
            return {
                'sku': sku,
                'title': product_title,
                'description_html': '',
                'url': product_url,
                'error': description_html,
                'description_quality': (quality_status, quality_msg)
            }
        
        return {
            'sku': sku,
            'title': product_title,
            'description_html': description_html,
            'url': product_url,
            'old_description': original_desc,
            'error': None,
            'description_quality': (quality_status, quality_msg)
        }
        
    except Exception as e:
        return {
            'sku': sku,
            'title': '',
            'description_html': '',
            'url': '',
            'error': str(e),
            'description_quality': ('error', str(e))
        }

# ═══════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════

if 'bulk_results' not in st.session_state:
    st.session_state.bulk_results = []
if 'bulk_selected_products' not in st.session_state:
    st.session_state.bulk_selected_products = {}
if 'products_to_send' not in st.session_state:
    st.session_state.products_to_send = {}

# ═══════════════════════════════════════════════════════════════════
# WALIDACJA
# ═══════════════════════════════════════════════════════════════════

if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak OPENAI_API_KEY w secrets.")
    st.stop()

required = ["AKENEO_BASE_URL", "AKENEO_CLIENT_ID", "AKENEO_SECRET", "AKENEO_USERNAME", "AKENEO_PASSWORD"]
missing = [k for k in required if k not in st.secrets]
if missing:
    st.error(f"❌ Brak konfiguracji Akeneo: {', '.join(missing)}")
    st.stop()

# Initialize OpenAI client (used even if Gemini is selected, passed as argument)
client = OpenAI()

# ═══════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════

col_logo, col_title = st.columns([1, 5])
with col_title:
    st.markdown('<h1 class="main-header">📚 Generator Opisów Produktów</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Masowe generowanie opisów produktów z Akeneo PIM • Powered by OpenAI & Gemini</p>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ Ustawienia")
    
    # Wybór modelu - ZAKTUALIZOWANA LISTA O GEMINI
    st.subheader("🤖 Model AI")
    model_choice = st.selectbox(
        "Wybierz model:",
        ["gpt-4o-mini", "gemini-3-flash-preview"],
        index=0,
        help="gpt-5-nano: szybki/tani OpenAI\ngemini-1.5-flash: szybki/tani Google\ngemini-2.0-flash-exp: najnowszy eksperymentalny Google"
    )

    if "gemini" in model_choice.lower() and "GOOGLE_API_KEY" not in st.secrets:
        st.error("⚠️ Brak GOOGLE_API_KEY w secrets.toml!")
    
    st.markdown("---")
    
    channel = st.selectbox(
        "Kanał (scope):",
        ["Bookland", "B2B"],
        index=0
    )
    
    locale = st.text_input(
        "Locale:",
        value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL")
    )
    
    st.markdown("---")
    
    # BAZA ZOPTYMALIZOWANYCH PRODUKTÓW
    st.header("📊 Baza zoptymalizowanych")
    
    optimized_products = load_optimized_products()
    
    if optimized_products:
        st.metric("Produkty w bazie", len(optimized_products))
        
        with st.expander("📋 Zobacz listę", expanded=False):
            # Sortowanie po dacie (najnowsze pierwsze)
            sorted_products = sorted(
                optimized_products, 
                key=lambda x: x.get('last_optimized', ''), 
                reverse=True
            )
            
            # Pokazuj po 10 na stronę
            items_per_page = 10
            total_pages = (len(sorted_products) + items_per_page - 1) // items_per_page
            
            if 'current_page' not in st.session_state:
                st.session_state.current_page = 0
            
            start_idx = st.session_state.current_page * items_per_page
            end_idx = start_idx + items_per_page
            page_products = sorted_products[start_idx:end_idx]
            
            for prod in page_products:
                with st.container():
                    st.markdown(f"**{prod['sku']}**")
                    st.caption(format_product_title(prod['title'], 60))
                    
                    # Parsuj datę
                    try:
                        dt = datetime.fromisoformat(prod['last_optimized'])
                        date_str = dt.strftime("%d.%m.%Y %H:%M")
                    except:
                        date_str = prod['last_optimized'][:16]
                    
                    st.caption(f"📅 {date_str}")
                    
                    if prod.get('url'):
                        st.caption(f"🔗 [{prod['url']}]({prod['url']})")
                    
                    st.markdown("---")
            
            # Paginacja
            if total_pages > 1:
                col_prev, col_info, col_next = st.columns([1, 2, 1])
                with col_prev:
                    if st.button("◀", disabled=(st.session_state.current_page == 0)):
                        st.session_state.current_page -= 1
                        st.rerun()
                with col_info:
                    st.caption(f"Strona {st.session_state.current_page + 1}/{total_pages}")
                with col_next:
                    if st.button("▶", disabled=(st.session_state.current_page >= total_pages - 1)):
                        st.session_state.current_page += 1
                        st.rerun()
        
        if st.button("🗑️ Wyczyść bazę", type="secondary"):
            if st.checkbox("Potwierdź usunięcie"):
                save_optimized_products([])
                st.success("✅ Baza wyczyszczona")
                st.rerun()
    else:
        st.info("Baza jest pusta")
    
    st.markdown("---")
    
    st.header("ℹ️ Informacje")
    st.info("""
**Jak używać:**
1. Wyszukaj produkty lub wklej listę SKU
2. Zaznacz produkty do generowania
3. Kliknij "Rozpocznij generowanie"
4. **Sprawdź jakość** wygenerowanych opisów
5. **Wybierz które wysłać** do PIM
6. Zaktualizuj zaznaczone w Akeneo

**v3.1.0 - Nowości:**
✅ **Obsługa Gemini Flash** (Google AI)
✅ Naprawiono błąd HTTPError przy logowaniu
✅ Caching tokenu (szybsze działanie)
    """)

# ═══════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJONALNOŚĆ - TRYB ZBIORCZY
# ═══════════════════════════════════════════════════════════════════

st.subheader("📦 Przetwarzanie wielu produktów")

# WYBÓR METODY
method = st.radio(
    "Wybierz metodę:",
    ["🔍 Wyszukaj i zaznacz produkty", "📋 Wklej listę SKU"],
    horizontal=True
)

st.markdown("---")

# METODA 1: WYSZUKIWANIE I ZAZNACZANIE
if method == "🔍 Wyszukaj i zaznacz produkty":
    
    # KOSZYK WYBRANYCH PRODUKTÓW
    if st.session_state.bulk_selected_products:
        with st.expander(f"🛒 Wybrane produkty ({len(st.session_state.bulk_selected_products)})", expanded=True):
            st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
            
            for sku, prod_data in list(st.session_state.bulk_selected_products.items()):
                col_info, col_remove = st.columns([5, 1])
                with col_info:
                    status = "🟢" if prod_data.get('enabled', False) else "🔴"
                    st.write(f"{status} **{sku}** - {format_product_title(prod_data.get('title', sku))}")
                with col_remove:
                    if st.button("🗑️", key=f"remove_{sku}"):
                        del st.session_state.bulk_selected_products[sku]
                        st.rerun()
            
            st.markdown('</div>', unsafe_allow_html=True)
            st.markdown("---")
            
            col_clear, col_info = st.columns([1, 3])
            with col_clear:
                if st.button("🗑️ Wyczyść wszystkie", use_container_width=True):
                    st.session_state.bulk_selected_products = {}
                    st.rerun()
            with col_info:
                st.info(f"Masz {len(st.session_state.bulk_selected_products)} produktów w koszyku")
    
    st.markdown("---")
    
    # WYSZUKIWARKA
    st.subheader("🔎 Wyszukaj i dodaj produkty")
    
    col_search, col_limit = st.columns([4, 1])
    
    with col_search:
        bulk_search = st.text_input(
            "Wyszukaj produkty:",
            placeholder="np. Harry Potter",
            key="bulk_search"
        )
    
    with col_limit:
        bulk_limit = st.number_input(
            "Limit",
            min_value=5,
            max_value=100,
            value=10,
            key="bulk_limit"
        )
    
    if st.button("🔍 Szukaj produktów", type="primary", use_container_width=True):
        if not bulk_search:
            st.warning("⚠️ Wpisz frazę")
        else:
            with st.spinner("Wyszukuję..."):
                token = akeneo_get_token()
                results = akeneo_search_products(bulk_search, token, bulk_limit, locale)
                st.session_state.bulk_search_results = results
                
                if results:
                    st.success(f"✅ Znaleziono {len(results)} produktów")
                else:
                    st.warning("⚠️ Nie znaleziono produktów")
    
    # LISTA PRODUKTÓW DO ZAZNACZENIA
    if 'bulk_search_results' in st.session_state and st.session_state.bulk_search_results:
        st.markdown("---")
        st.subheader("Zaznacz produkty z wyników wyszukiwania:")
        
        col_all1, col_all2, col_all3 = st.columns([1, 1, 4])
        with col_all1:
            if st.button("✅ Zaznacz widoczne", use_container_width=True):
                for prod in st.session_state.bulk_search_results:
                    st.session_state.bulk_selected_products[prod['identifier']] = {
                        'title': prod['title'],
                        'enabled': prod['enabled'],
                        'family': prod['family']
                    }
                st.rerun()
        with col_all2:
            if st.button("❌ Odznacz widoczne", use_container_width=True):
                for prod in st.session_state.bulk_search_results:
                    if prod['identifier'] in st.session_state.bulk_selected_products:
                        del st.session_state.bulk_selected_products[prod['identifier']]
                st.rerun()
        
        st.markdown("---")
        
        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        
        for prod in st.session_state.bulk_search_results:
            col_check, col_info = st.columns([1, 6])
            
            sku = prod['identifier']
            is_selected = sku in st.session_state.bulk_selected_products
            
            with col_check:
                checkbox_key = f"check_{sku}_{bulk_search}"
                checked = st.checkbox("", value=is_selected, key=checkbox_key, label_visibility="collapsed")
                
                if checked and not is_selected:
                    st.session_state.bulk_selected_products[sku] = {
                        'title': prod['title'],
                        'enabled': prod['enabled'],
                        'family': prod['family']
                    }
                    st.rerun()
                elif not checked and is_selected:
                    del st.session_state.bulk_selected_products[sku]
                    st.rerun()
            
            with col_info:
                status = "🟢" if prod['enabled'] else "🔴"
                already_selected = " ✓ (w koszyku)" if is_selected else ""
                st.write(f"{status} **{sku}** - {format_product_title(prod['title'])}{already_selected}")
        
        st.markdown('</div>', unsafe_allow_html=True)

# METODA 2: LISTA SKU
else:
    st.markdown("Wklej listę SKU (jeden na linię):")
    skus_text = st.text_area(
        "SKU:",
        height=200,
        placeholder="BL-001\nBL-002\nBL-003",
        label_visibility="collapsed"
    )
    
    if st.button("📋 Załaduj produkty po SKU", type="primary", use_container_width=True):
        if not skus_text.strip():
            st.warning("⚠️ Wklej listę SKU")
        else:
            skus = [s.strip() for s in skus_text.split('\n') if s.strip()]
            
            with st.spinner(f"Ładuję {len(skus)} produktów..."):
                token = akeneo_get_token()
                for sku in skus:
                    try:
                        product = akeneo_get_product_details(sku, token, channel, locale)
                        if product:
                            st.session_state.bulk_selected_products[sku] = {
                                'title': product.get('title', sku),
                                'enabled': product.get('enabled', False),
                                'family': product.get('family', '')
                            }
                    except:
                        st.session_state.bulk_selected_products[sku] = {
                            'title': sku,
                            'enabled': True,
                            'family': ''
                        }
            
            st.success(f"✅ Załadowano {len(skus)} produktów do koszyka")
            st.rerun()
    
    if st.session_state.bulk_selected_products:
        st.markdown("---")
        st.subheader(f"📋 Załadowane produkty ({len(st.session_state.bulk_selected_products)})")
        
        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        
        for sku, prod_data in list(st.session_state.bulk_selected_products.items()):
            col_info, col_remove = st.columns([5, 1])
            with col_info:
                status = "🟢" if prod_data.get('enabled', False) else "🔴"
                st.write(f"{status} **{sku}** - {format_product_title(prod_data.get('title', sku))}")
            with col_remove:
                if st.button("🗑️", key=f"remove_list_{sku}"):
                    del st.session_state.bulk_selected_products[sku]
                    st.rerun()
        
        st.markdown('</div>', unsafe_allow_html=True)
        st.markdown("---")
        
        if st.button("🗑️ Wyczyść wszystkie", use_container_width=True):
            st.session_state.bulk_selected_products = {}
            st.rerun()

# GENEROWANIE ZBIORCZE
if st.session_state.bulk_selected_products:
    st.markdown("---")
    st.markdown("---")
    st.subheader("🚀 Generowanie opisów")
    
    st.metric("Produkty do przetworzenia", len(st.session_state.bulk_selected_products))
    st.info(f"Wybrany model: {model_choice}")
    
    col_gen, col_clear = st.columns([1, 1])
    
    with col_gen:
        if st.button("🚀 Rozpocznij generowanie zbiorcze", type="primary", use_container_width=True):
            st.session_state.bulk_results = []
            st.session_state.products_to_send = {}
            
            progress_bar = st.progress(0, text="Rozpoczynam...")
            status_text = st.empty()
            
            token = akeneo_get_token()
            skus = list(st.session_state.bulk_selected_products.keys())
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(
                        process_product_from_akeneo,
                        sku,
                        client,
                        token,
                        channel,
                        locale,
                        model_choice
                    ): sku for sku in skus
                }
                
                results_temp = []
                for i, future in enumerate(as_completed(futures)):
                    result = future.result()
                    results_temp.append(result)
                    
                    # Automatycznie zaznacz pomyślne
                    if not result['error']:
                        st.session_state.products_to_send[result['sku']] = True
                    
                    progress = (i + 1) / len(skus)
                    progress_bar.progress(progress, text=f"Przetworzono {i+1}/{len(skus)}")
                    status_text.text(f"Ostatni: {result['sku']}")
            
            st.session_state.bulk_results = results_temp
            progress_bar.progress(1.0, text="✅ Zakończono!")
            st.success(f"✅ Przetworzono {len(results_temp)} produktów")
            time.sleep(1)
            st.rerun()
    
    with col_clear:
        if st.button("🗑️ Wyczyść koszyk", use_container_width=True):
            st.session_state.bulk_selected_products = {}
            st.session_state.bulk_results = []
            st.session_state.products_to_send = {}
            st.rerun()

# WYNIKI ZBIORCZE
if st.session_state.bulk_results:
    st.markdown("---")
    st.subheader("📊 Wyniki")
    
    results = st.session_state.bulk_results
    successful = [r for r in results if not r['error']]
    errors = [r for r in results if r['error']]
    
    quality_warnings = [r for r in successful if 'description_quality' in r and r['description_quality'][0] in ['warning', 'error']]
    
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("Wszystkie", len(results))
    col_m2.metric("Sukces", len(successful), delta=f"+{len(successful)}")
    col_m3.metric("Błędy", len(errors), delta=f"-{len(errors)}" if errors else "0")
    col_m4.metric("Ostrzeżenia", len(quality_warnings))
    
    # CSV Export
    df = pd.DataFrame(results)
    st.download_button(
        "📥 Pobierz CSV",
        df.to_csv(index=False).encode('utf-8'),
        'opisy_zbiorcze.csv',
        'text/csv',
        use_container_width=True
    )
    
    # Wybór i wysyłka do PIM
    if successful:
        st.markdown("---")
        st.subheader("📤 Wysyłka do Akeneo PIM")
        
        # Checkboxy wyboru
        col_select_all, col_deselect_all, col_info_select = st.columns([1, 1, 2])
        with col_select_all:
            if st.button("✅ Zaznacz wszystkie", use_container_width=True):
                for r in successful:
                    st.session_state.products_to_send[r['sku']] = True
                st.rerun()
        with col_deselect_all:
            if st.button("❌ Odznacz wszystkie", use_container_width=True):
                for r in successful:
                    st.session_state.products_to_send[r['sku']] = False
                st.rerun()
        with col_info_select:
            selected_count = sum(1 for v in st.session_state.products_to_send.values() if v)
            st.info(f"Wybrano: {selected_count}/{len(successful)}")
        
        st.markdown("---")
        
        # Lista z checkboxami
        for idx, result in enumerate(successful):
            col_check, col_sku, col_title = st.columns([0.5, 1.5, 4])
            
            with col_check:
                checked = st.checkbox(
                    "",
                    value=st.session_state.products_to_send.get(result['sku'], True),
                    key=f"send_check_{result['sku']}_{idx}",
                    label_visibility="collapsed"
                )
                st.session_state.products_to_send[result['sku']] = checked
            
            with col_sku:
                warning_icon = ""
                if 'description_quality' in result and result['description_quality'][0] in ['warning', 'error']:
                    warning_icon = " ⚠️"
                st.write(f"**{result['sku']}**{warning_icon}")
            
            with col_title:
                st.write(format_product_title(result['title']))
        
        st.markdown("---")
        
        # Przycisk wysyłki
        selected_to_send = [r for r in successful if st.session_state.products_to_send.get(r['sku'], False)]
        
        if selected_to_send:
            if st.button(f"✅ Wyślij zaznaczone do PIM ({len(selected_to_send)})", type="primary", use_container_width=True):
                success_count = 0
                error_count = 0
                error_msgs = []
                
                progress_pim = st.progress(0, text="Wysyłam do PIM...")
                
                for i, result in enumerate(selected_to_send):
                    try:
                        akeneo_update_description(
                            result['sku'],
                            result['description_html'],
                            channel,
                            locale
                        )
                        # Dodaj do bazy
                        add_optimized_product(result['sku'], result['title'], result['url'])
                        success_count += 1
                    except Exception as e:
                        error_count += 1
                        error_msgs.append(f"{result['sku']}: {str(e)}")
                    
                    progress_pim.progress((i + 1) / len(selected_to_send))
                
                st.success(f"✅ Zaktualizowano {success_count} produktów")
                
                if error_count > 0:
                    st.error(f"❌ Błędy: {error_count}")
                    for msg in error_msgs:
                        st.text(msg)
                
                st.balloons()
        else:
            st.warning("⚠️ Nie wybrano żadnych produktów do wysyłki")
    
    # Szczegóły wyników
    st.markdown("---")
    st.subheader("📋 Szczegóły wszystkich wyników")
    
    for idx, result in enumerate(results):
        if result['error']:
            with st.expander(f"❌ {result['sku']}", expanded=False):
                st.error(result['error'])
        else:
            warning_icon = ""
            if 'description_quality' in result and result['description_quality'][0] in ['warning', 'error']:
                warning_icon = " ⚠️"
            
            with st.expander(f"✅ {result['sku']} - {format_product_title(result['title'])}{warning_icon}"):
                
                # Ostrzeżenie o jakości
                if 'description_quality' in result:
                    quality_status, quality_msg = result['description_quality']
                    if quality_status in ['warning', 'error']:
                        if quality_status == 'error':
                            st.error(quality_msg)
                        else:
                            st.warning(quality_msg)
                
                # URL produktu
                if result.get('url'):
                    st.info(f"🔗 [{result['url']}]({result['url']})")
                
                col_regen_info, col_regen_btn = st.columns([3, 1])
                with col_regen_info:
                    st.info(f"💡 Nie podoba Ci się ten opis? Wygeneruj nowy")
                with col_regen_btn:
                    if st.button("♻️ Przeredaguj", key=f"regen_bulk_{result['sku']}_{idx}", use_container_width=True):
                        with st.spinner(f"Przeredagowuję {result['sku']}..."):
                            token = akeneo_get_token()
                            new_result = process_product_from_akeneo(
                                result['sku'],
                                client,
                                token,
                                channel,
                                locale,
                                model_choice
                            )
                            
                            if not new_result['error']:
                                st.session_state.bulk_results[idx] = new_result
                                st.success(f"✅ Przeredagowano!")
                                st.rerun()
                            else:
                                st.error(f"❌ {new_result['error']}")
                
                st.markdown("---")
                
                tab_c, tab_p = st.tabs(["💻 Kod HTML", "👁️ Porównanie"])
                
                with tab_c:
                    st.code(result['description_html'], language='html')
                    
                    col_s1, col_s2, col_s3 = st.columns(3)
                    with col_s1:
                        st.metric("Długość", f"{len(result['description_html'])} zn")
                    with col_s2:
                        bold_count = result['description_html'].count('<b>')
                        st.metric("Bold", bold_count)
                    with col_s3:
                        h2_count = result['description_html'].count('<h2>')
                        st.metric("H2", h2_count)
                
                with tab_p:
                    if result.get('old_description'):
                        col_old, col_new = st.columns(2)
                        with col_old:
                            st.markdown("**🕰️ Stary opis**")
                            st.caption(f"📏 {len(result['old_description'])} znaków")
                            st.markdown("---")
                            st.markdown(result['old_description'], unsafe_allow_html=True)
                        with col_new:
                            st.markdown("**✨ Nowy opis**")
                            st.caption(f"📏 {len(result['description_html'])} znaków")
                            st.markdown("---")
                            st.markdown(result['description_html'], unsafe_allow_html=True)
                    else:
                        st.warning("⚠️ Brak starego opisu")
                        st.markdown(result['description_html'], unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════════════

st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; padding: 20px;'>
    <p><strong>Generator Opisów Produktów v3.1.0</strong></p>
    <p>Powered by OpenAI & Google Gemini | Akeneo PIM Integration</p>
</div>
""", unsafe_allow_html=True)
