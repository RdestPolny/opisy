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
    page_title="Generator Opisów Produktów v3.2.0",
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
    """Usuwa checklist z końca odpowiedzi (zabezpieczenie)"""
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
    """Bezpiecznie konwertuje wartość na string."""
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) > 0:
            return str(value[0]).strip()
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()

def format_product_title(title: str, max_length: int = 80) -> str:
    """Formatuje tytuł produktu"""
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    return title

def validate_description_quality(description) -> Tuple[str, str]:
    """Waliduje jakość oryginalnego opisu"""
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
    """Pobiera access token dla Akeneo API z cachem"""
    try:
        token_url = _akeneo_root() + "/api/oauth/v1/token"
        
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
            st.stop()
            
        return r.json()["access_token"]
        
    except requests.exceptions.ConnectionError:
        st.error(f"❌ Nie można połączyć się z Akeneo pod adresem: {_akeneo_root()}")
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
            "search": json.dumps({"identifier": [{"operator": "CONTAINS", "value": search_query}]})
        }
        r1 = requests.get(url, headers=headers, params=params_identifier, timeout=30)
        r1.raise_for_status()
        
        for item in r1.json().get("_embedded", {}).get("items", []):
            identifier = item.get("identifier", "")
            title = identifier
            values = item.get("values", {})
            if "name" in values:
                for val in values["name"]:
                    if val.get("locale") == locale or val.get("locale") is None:
                        title = safe_string_value(val.get("data", identifier)) or identifier
                        break
            products_dict[identifier] = {
                "identifier": identifier, "title": title, 
                "family": item.get("family", ""), "enabled": item.get("enabled", False)
            }
        
        # Wyszukiwanie po atrybucie "name"
        params_name = {
            "limit": limit,
            "search": json.dumps({"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]})
        }
        r2 = requests.get(url, headers=headers, params=params_name, timeout=30)
        r2.raise_for_status()
        
        for item in r2.json().get("_embedded", {}).get("items", []):
            identifier = item.get("identifier", "")
            if identifier not in products_dict:
                title = identifier
                if "name" in item.get("values", {}):
                    for val in item["values"]["name"]:
                        if val.get("locale") == locale or val.get("locale") is None:
                            title = safe_string_value(val.get("data", identifier)) or identifier
                            break
                products_dict[identifier] = {
                    "identifier": identifier, "title": title, 
                    "family": item.get("family", ""), "enabled": item.get("enabled", False)
                }
        
        return sorted(list(products_dict.values()), key=lambda x: x['title'].lower())[:limit]
        
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
            if attr_name not in values: return ""
            for val in values[attr_name]:
                if (val.get("scope") in [None, channel]) and (val.get("locale") in [None, locale]):
                    return safe_string_value(val.get("data", ""))
            return safe_string_value(values[attr_name][0].get("data", "")) if values[attr_name] else ""
        
        return {
            "identifier": product.get("identifier", ""),
            "title": get_value("name") or product.get("identifier", ""),
            "description": get_value("description"),
            "author": get_value("author") or get_value("autor"),
            "publisher": get_value("publisher") or get_value("wydawnictwo"),
            "year": get_value("year") or get_value("rok_wydania"),
            "pages": get_value("pages") or get_value("liczba_stron"),
            "cover_type": get_value("cover_type") or get_value("oprawa"),
            "raw_values": values
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404: return None
        raise e

def akeneo_update_description(sku: str, html_description: str, channel: str, locale: str = "pl_PL") -> bool:
    """Aktualizuje opis produktu w Akeneo"""
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt '{sku}' nie istnieje w Akeneo.")
    
    attr_desc = akeneo_get_attribute("description", token)
    
    value_obj_desc = {
        "data": html_description,
        "scope": channel if attr_desc.get("scopable") else None,
        "locale": locale if attr_desc.get("localizable") else None,
    }
    
    payload = {"values": {"description": [value_obj_desc]}}
    
    # Próba ustawienia flagi SEO jeśli istnieje
    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        payload["values"]["opisy_seo"] = [{
            "data": True,
            "scope": channel if attr_seo.get("scopable") else None,
            "locale": locale if attr_seo.get("localizable") else None,
        }]
    except:
        pass

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    r = requests.patch(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(payload), timeout=30)
    
    if r.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd Akeneo ({r.status_code})")

# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - ZAKTUALIZOWANE O SUPREME SELLING MODE
# ═══════════════════════════════════════════════════════════════════

def generate_description(product_data: Dict, client: OpenAI, model: str = "gemini-3-flash-preview", style_variant: str = "default") -> str:
    """Generuje opis produktu z wykorzystaniem wybranego modelu (GPT lub Gemini)"""
    try:
        # ═══════════════════════════════════════════════════════════════════
        # PROMPT INŻYNIERYJNY - SUPREME SELLING MODE
        # ═══════════════════════════════════════════════════════════════════
        
        system_prompt = """
Jesteś GŁÓWNYM REDAKTOREM w prestiżowej księgarni internetowej Bookland. Twoim zadaniem jest tworzenie magnetycznych opisów produktów, które zamieniają odwiedzających w kupujących.

TWOJE CELE:
1. Wzbudzić emocje i ciekawość od pierwszego zdania.
2. Używać "Języka Korzyści" – nie pisz tylko, że książka ma twardą oprawę, napisz, że "elegancka twarda oprawa sprawia, że to idealny prezent".
3. Zachować absolutną poprawność językową i typograficzną (polska gramatyka).

KRĘGOSŁUP STRUKTURALNY (Ściśle przestrzegaj HTML):
Twój output musi być czystym kodem HTML (bez ```html, bez <html>, bez <body>). Użyj dokładnie tej struktury:

<p>[HOOK: Pierwszy akapit (4-5 zdań). Musi chwytać za serce/rozum. W tym akapicie OBOWIĄZKOWO pogrub <b>Tytuł</b> oraz <b>Autora</b> (pełne imię i nazwisko).]</p>

<h2>[NAGŁÓWEK H2: Obietnica korzyści lub intrygujące pytanie związane z treścią]</h2>

<p>[ROZWINIĘCIE: Opis fabuły/treści (bez spoilerów!) lub zastosowania produktu. Tutaj budujesz pożądanie. Wpleć tutaj NATURALNIE dane techniczne (wydawnictwo, rok, oprawa) - nie rób z nich listy, uczyń z nich atut narracji.]</p>

<h3>[NAGŁÓWEK H3: Krótkie wezwanie do działania (CTA) - np. "Sprawdź, dlaczego warto", "Dołącz do czytelników"]</h3>

<p>[ZAKOŃCZENIE + CTA: Ostatni krótki akapit (2-3 zdania). Podsumowanie emocjonalne i bezpośrednie polecenie dodania do koszyka/zakupu.]</p>

ZASADY ŻELAZNE (Błędy krytyczne):
1. FORMATOWANIE NAZWISK: To najważniejsza zasada.
   - ŹLE: "autorstwa remigiusz mroz", "książka joanny balickiej"
   - DOBRZE: "autorstwa Remigiusza Mroza", "książka Joanny Balickiej"
   - ZAWSZE wielkie litery, ZAWSZE pełna odmiana przez przypadki.

2. STYL I TON:
   - Unikaj słów-wypełniaczy AI: "warto zauważyć", "godnym podkreślenia jest", "w dzisiejszych czasach".
   - Pisz konkretnie. Używaj czasowników dynamicznych (Odkryj, Poczuj, Zrozum).
   - Myślniki: Używaj tylko dywizu "-" (minus), nie używaj półpauzy (–) ani pauzy (—).

3. UNIKALNOŚĆ:
   - Nigdy nie powtarzaj tych samych fraz w różnych akapitach.
   - Dane techniczne (strony, rok) wymień TYLKO RAZ w całym tekście.

4. CZYSTY HTML:
   - Zwróć tylko kod HTML zaczynający się od <p> i kończący na </p>. Żadnych markdownów.

Oto dane produktu do opisania:
"""

        raw_data = f"""
TYTUŁ: {product_data.get('title', '')}
AUTOR: {product_data.get('author', '')}
WYDAWNICTWO: {product_data.get('publisher', 'Brak danych')}
SZCZEGÓŁY (rok, strony, oprawa, itp.): {product_data.get('details', '')}

ORYGINALNY OPIS (Baza wiedzy - przetwórz to swoimi słowami, nie kopiuj 1:1):
{product_data.get('description', '')}
"""
        final_instruction = "\n\nWygeneruj TYLKO kod HTML. Nie dodawaj żadnych komentarzy."

        # ---------------------------------------------------------
        # OBSŁUGA GOOGLE GEMINI (DEFAULT & PREFERRED)
        # ---------------------------------------------------------
        if "gemini" in model.lower():
            if "GOOGLE_API_KEY" not in st.secrets:
                return "BŁĄD: Brak klucza GOOGLE_API_KEY w secrets.toml"

            genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
            
            # Konfiguracja generowania dla maksymalnej jakości
            generation_config = {
                "temperature": 0.7,
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": 8192,
            }

            model_instance = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt,
                generation_config=generation_config
            )
            
            response = model_instance.generate_content(raw_data + final_instruction)
            
            result = strip_code_fences(response.text)
            result = remove_checklist_from_output(result)
            result = clean_ai_fingerprints(result)
            return result

        # ---------------------------------------------------------
        # OBSŁUGA OPENAI GPT (FALLBACK)
        # ---------------------------------------------------------
        else:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": raw_data + final_instruction}
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

def process_product_from_akeneo(sku: str, client: OpenAI, token: str, channel: str, locale: str, model: str) -> Dict:
    """Przetwarza pojedynczy produkt z Akeneo"""
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        
        if not product_details:
            return {
                'sku': sku, 'title': '', 'description_html': '', 'url': '',
                'error': 'Produkt nie znaleziony',
                'description_quality': ('error', 'Produkt nie znaleziony')
            }
        
        details_parts = []
        author = safe_string_value(product_details.get('author'))
        if author: details_parts.append(f"Autor: {author}")
        
        publisher = safe_string_value(product_details.get('publisher'))
        if publisher: details_parts.append(f"Wydawnictwo: {publisher}")
        
        year = safe_string_value(product_details.get('year'))
        if year: details_parts.append(f"Rok: {year}")
        
        pages = safe_string_value(product_details.get('pages'))
        if pages: details_parts.append(f"Strony: {pages}")
        
        cover_type = safe_string_value(product_details.get('cover_type'))
        if cover_type: details_parts.append(f"Oprawa: {cover_type}")
        
        original_desc = product_details.get('description', '') or product_details.get('short_description', '')
        original_desc = safe_string_value(original_desc)
        quality_status, quality_msg = validate_description_quality(original_desc)
        
        product_title = safe_string_value(product_details['title'])
        product_url = generate_product_url(product_title)
        
        product_data = {
            'title': product_title,
            'author': author,
            'details': '\n'.join(details_parts),
            'publisher': publisher,
            'description': original_desc
        }
        
        description_html = generate_description(product_data, client, model, "default")
        
        if "BŁĄD" in description_html:
            return {
                'sku': sku, 'title': product_title, 'description_html': '', 'url': product_url,
                'error': description_html, 'description_quality': (quality_status, quality_msg)
            }
        
        return {
            'sku': sku, 'title': product_title, 'description_html': description_html,
            'url': product_url, 'old_description': original_desc, 'error': None,
            'description_quality': (quality_status, quality_msg)
        }
        
    except Exception as e:
        return {
            'sku': sku, 'title': '', 'description_html': '', 'url': '',
            'error': str(e), 'description_quality': ('error', str(e))
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

# Initialize OpenAI client (required arg for functions, even if unused with Gemini)
if "OPENAI_API_KEY" in st.secrets:
    client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
else:
    client = None # Obsługa braku klucza OpenAI jeśli używamy tylko Gemini

required = ["AKENEO_BASE_URL", "AKENEO_CLIENT_ID", "AKENEO_SECRET", "AKENEO_USERNAME", "AKENEO_PASSWORD"]
missing = [k for k in required if k not in st.secrets]
if missing:
    st.error(f"❌ Brak konfiguracji Akeneo: {', '.join(missing)}")
    st.stop()

# ═══════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════

col_logo, col_title = st.columns([1, 5])
with col_title:
    st.markdown('<h1 class="main-header">📚 Generator Opisów Produktów</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Masowe generowanie opisów produktów z Akeneo PIM • Powered by Google Gemini</p>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ Ustawienia")
    
    # Wybór modelu - GEMINI 3 FLASH PREVIEW DOMYŚLNY
    st.subheader("🤖 Model AI")
    model_choice = st.selectbox(
        "Wybierz model:",
        ["gemini-3-flash-preview", "gpt-4o-mini"],
        index=0,
        help="Gemini 3 Flash: Super szybki model Google\nGPT-4o Mini: Ekonomiczny model OpenAI"
    )

    if "gemini" in model_choice.lower() and "GOOGLE_API_KEY" not in st.secrets:
        st.error("⚠️ Brak GOOGLE_API_KEY w secrets.toml!")
    
    st.markdown("---")
    
    channel = st.selectbox("Kanał (scope):", ["Bookland", "B2B"], index=0)
    locale = st.text_input("Locale:", value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL"))
    
    st.markdown("---")
    
    # BAZA ZOPTYMALIZOWANYCH
    st.header("📊 Baza zoptymalizowanych")
    optimized_products = load_optimized_products()
    if optimized_products:
        st.metric("Produkty w bazie", len(optimized_products))
        if st.button("🗑️ Wyczyść bazę"):
            save_optimized_products([])
            st.rerun()
    else:
        st.info("Baza jest pusta")
        
    st.markdown("---")
    st.info("**v3.2.0**\nSupreme Selling Mode Prompts\nGemini 3 Flash Default")

# ═══════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJONALNOŚĆ - TRYB ZBIORCZY
# ═══════════════════════════════════════════════════════════════════

st.subheader("📦 Przetwarzanie wielu produktów")

method = st.radio("Wybierz metodę:", ["🔍 Wyszukaj i zaznacz produkty", "📋 Wklej listę SKU"], horizontal=True)
st.markdown("---")

# METODA 1: WYSZUKIWANIE
if method == "🔍 Wyszukaj i zaznacz produkty":
    if st.session_state.bulk_selected_products:
        with st.expander(f"🛒 Wybrane produkty ({len(st.session_state.bulk_selected_products)})", expanded=True):
            st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
            for sku, prod_data in list(st.session_state.bulk_selected_products.items()):
                c1, c2 = st.columns([5, 1])
                c1.write(f"**{sku}** - {format_product_title(prod_data.get('title', sku))}")
                if c2.button("🗑️", key=f"del_{sku}"):
                    del st.session_state.bulk_selected_products[sku]
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
            if st.button("🗑️ Wyczyść koszyk"):
                st.session_state.bulk_selected_products = {}
                st.rerun()
    
    st.subheader("🔎 Szukaj")
    c_s, c_l = st.columns([4, 1])
    search_q = c_s.text_input("Szukaj:", key="search_q")
    limit = c_l.number_input("Limit", 5, 100, 10)
    
    if st.button("🔍 Szukaj", type="primary"):
        with st.spinner("Szukam..."):
            token = akeneo_get_token()
            st.session_state.bulk_search_results = akeneo_search_products(search_q, token, limit, locale)
    
    if 'bulk_search_results' in st.session_state and st.session_state.bulk_search_results:
        st.markdown("---")
        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        for prod in st.session_state.bulk_search_results:
            sku = prod['identifier']
            checked = st.checkbox(
                f"{sku} - {format_product_title(prod['title'])}",
                value=(sku in st.session_state.bulk_selected_products),
                key=f"check_{sku}"
            )
            if checked:
                st.session_state.bulk_selected_products[sku] = {'title': prod['title']}
            elif sku in st.session_state.bulk_selected_products:
                del st.session_state.bulk_selected_products[sku]
        st.markdown('</div>', unsafe_allow_html=True)

# METODA 2: SKU LISTA
else:
    skus_text = st.text_area("Wklej SKU (jeden w linii):", height=200)
    if st.button("📋 Załaduj", type="primary"):
        skus = [s.strip() for s in skus_text.split('\n') if s.strip()]
        with st.spinner(f"Ładuję {len(skus)}..."):
            token = akeneo_get_token()
            for sku in skus:
                prod = akeneo_get_product_details(sku, token, channel, locale)
                if prod:
                    st.session_state.bulk_selected_products[sku] = {'title': prod['title']}
        st.success("Załadowano!")
        st.rerun()

# GENEROWANIE
if st.session_state.bulk_selected_products:
    st.markdown("---")
    st.subheader("🚀 Generowanie")
    if st.button("🚀 START", type="primary"):
        st.session_state.bulk_results = []
        st.session_state.products_to_send = {}
        prog = st.progress(0)
        token = akeneo_get_token()
        skus = list(st.session_state.bulk_selected_products.keys())
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_product_from_akeneo, sku, client, token, channel, locale, model_choice): sku for sku in skus}
            for i, future in enumerate(as_completed(futures)):
                res = future.result()
                st.session_state.bulk_results.append(res)
                if not res['error']:
                    st.session_state.products_to_send[res['sku']] = True
                prog.progress((i+1)/len(skus))
        st.rerun()

# WYNIKI
if st.session_state.bulk_results:
    st.markdown("---")
    st.subheader("📊 Wyniki")
    results = st.session_state.bulk_results
    successful = [r for r in results if not r['error']]
    
    # CSV
    df = pd.DataFrame(results)
    st.download_button("📥 CSV", df.to_csv(index=False).encode('utf-8'), 'wyniki.csv')
    
    # WYSYŁKA
    if successful:
        st.markdown("---")
        st.subheader("📤 Wysyłka do PIM")
        
        for r in successful:
            st.session_state.products_to_send[r['sku']] = st.checkbox(
                f"{r['sku']} - {format_product_title(r['title'])}",
                value=st.session_state.products_to_send.get(r['sku'], True),
                key=f"send_{r['sku']}"
            )
            
        if st.button("✅ Wyślij zaznaczone"):
            to_send = [r for r in successful if st.session_state.products_to_send.get(r['sku'])]
            prog = st.progress(0)
            cnt = 0
            for i, r in enumerate(to_send):
                try:
                    akeneo_update_description(r['sku'], r['description_html'], channel, locale)
                    add_optimized_product(r['sku'], r['title'], r['url'])
                    cnt += 1
                except: pass
                prog.progress((i+1)/len(to_send))
            st.success(f"Wysłano {cnt}")

    # SZCZEGÓŁY
    st.markdown("---")
    for r in results:
        with st.expander(f"{'✅' if not r['error'] else '❌'} {r['sku']} - {format_product_title(r['title'])}"):
            if r['error']:
                st.error(r['error'])
            else:
                st.code(r['description_html'], language='html')
                st.markdown(r['description_html'], unsafe_allow_html=True)
                if st.button("♻️ Regeneruj", key=f"regen_{r['sku']}"):
                    token = akeneo_get_token()
                    new_res = process_product_from_akeneo(r['sku'], client, token, channel, locale, model_choice)
                    # Update results list logic needed here in real app
                    st.info("Regeneracja w toku - odśwież wyniki")
