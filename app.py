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

# Obsługa Google Gemini
import google.generativeai as genai

# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STRONY
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Generator Opisów Produktów v3.2.0 (Gemini 3)",
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
        return 'error', '❌ Brak oryginalnego opisu w Akeneo!'
    
    desc_length = len(desc_str.strip())
    
    if desc_length < 50:
        return 'error', f'❌ Oryginalny opis krytycznie krótki ({desc_length} znaków)!'
    elif desc_length < 200:
        return 'warning', f'⚠️ Oryginalny opis dość krótki ({desc_length} znaków).'
    else:
        return 'ok', f'✅ Opis źródłowy OK ({desc_length} znaków)'

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
        
    except Exception as e:
        st.error(f"❌ Błąd połączenia z Akeneo: {str(e)}")
        st.stop()

def akeneo_get_attribute(code: str, token: str) -> Dict:
    url = _akeneo_root() + f"/api/rest/v1/attributes/{code}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()

def akeneo_product_exists(sku: str, token: str) -> bool:
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.status_code == 200

def akeneo_search_products(search_query: str, token: str, limit: int = 20, locale: str = "pl_PL") -> List[Dict]:
    """Wyszukuje produkty w Akeneo"""
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    products_dict = {}
    
    try:
        # 1. Szukaj po SKU
        params_identifier = {
            "limit": limit,
            "search": json.dumps({"identifier": [{"operator": "CONTAINS", "value": search_query}]})
        }
        r1 = requests.get(url, headers=headers, params=params_identifier, timeout=30)
        if r1.status_code == 200:
            for item in r1.json().get("_embedded", {}).get("items", []):
                identifier = item.get("identifier", "")
                title = identifier
                # Próba wyciągnięcia nazwy
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

        # 2. Szukaj po nazwie
        params_name = {
            "limit": limit,
            "search": json.dumps({"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]})
        }
        r2 = requests.get(url, headers=headers, params=params_name, timeout=30)
        if r2.status_code == 200:
            for item in r2.json().get("_embedded", {}).get("items", []):
                identifier = item.get("identifier", "")
                if identifier not in products_dict:
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
        
        return list(products_dict.values())[:limit]
    except Exception as e:
        st.error(f"Błąd wyszukiwania: {str(e)}")
        return []

def akeneo_get_product_details(sku: str, token: str, channel: str = "Bookland", locale: str = "pl_PL") -> Optional[Dict]:
    """Pobiera szczegóły produktu"""
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        product = r.json()
        values = product.get("values", {})
        
        def get_value(attr_name: str) -> str:
            if attr_name not in values: return ""
            attr_values = values[attr_name]
            if not attr_values: return ""
            for val in attr_values:
                # Preferuj dany kanał i locale
                v_scope = val.get("scope")
                v_locale = val.get("locale")
                if (v_scope is None or v_scope == channel) and (v_locale is None or v_locale == locale):
                    return safe_string_value(val.get("data", ""))
            # Fallback
            return safe_string_value(attr_values[0].get("data", ""))
        
        return {
            "identifier": product.get("identifier", ""),
            "title": get_value("name") or product.get("identifier", ""),
            "description": get_value("description"),
            "author": get_value("author") or get_value("autor"),
            "publisher": get_value("publisher") or get_value("wydawnictwo"),
            "year": get_value("year") or get_value("rok_wydania"),
            "pages": get_value("pages") or get_value("liczba_stron"),
            "cover_type": get_value("cover_type") or get_value("oprawa"),
            "enabled": product.get("enabled", False)
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404: return None
        raise e

def akeneo_update_description(sku: str, html_description: str, channel: str, locale: str = "pl_PL") -> bool:
    """Wysyła opis do Akeneo"""
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt '{sku}' nie istnieje.")
    
    attr_desc = akeneo_get_attribute("description", token)
    is_scopable = bool(attr_desc.get("scopable", False))
    is_localizable = bool(attr_desc.get("localizable", False))
    
    value_obj = {
        "data": html_description,
        "scope": channel if is_scopable else None,
        "locale": locale if is_localizable else None,
    }
    
    payload = {"values": {"description": [value_obj]}}
    
    # Próba ustawienia flagi SEO jeśli istnieje
    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        seo_scopable = bool(attr_seo.get("scopable", False))
        seo_localizable = bool(attr_seo.get("localizable", False))
        payload["values"]["opisy_seo"] = [{
            "data": True,
            "scope": channel if seo_scopable else None,
            "locale": locale if seo_localizable else None
        }]
    except:
        pass

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=30)
    if r.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd zapisu Akeneo ({r.status_code}): {r.text}")

# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - ZMODYFIKOWANA SEKCJA
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
Twój output musi być czystym kodem HTML (bez ```html, bez <html>, bez <body>). Użyj poniższej struktury:

<p>[HOOK: Pierwszy akapit (4-5 zdań). Musi chwytać za serce/rozum. W tym akapicie OBOWIĄZKOWO pogrub <b>Tytuł</b> oraz <b>Autora</b> (pełne imię i nazwisko).]</p>

<h2>[NAGŁÓWEK H2: Obietnica korzyści lub intrygujące pytanie związane z treścią]</h2>

<p>[ROZWINIĘCIE: Opis fabuły/treści (bez spoilerów!) lub zastosowania produktu. Tutaj budujesz pożądanie. Wpleć tutaj NATURALNIE dane techniczne (wydawnictwo, rok, oprawa) - nie rób z nich listy, uczyń z nich atut.]</p>

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

4. WPROWADZANIE DANYCH:
   - Nie pisz "Dane techniczne:". Wpleć to w zdanie: "Ta licząca 320 stron powieść, wydana nakładem wydawnictwa Znak, to..."

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

        final_instruction = "\n\nWygeneruj TYLKO kod HTML. Nie dodawaj żadnych komentarzy przed ani po."

        # ---------------------------------------------------------
        # OBSŁUGA GOOGLE GEMINI (DOMYŚLNY MODEL)
        # ---------------------------------------------------------
        if "gemini" in model.lower():
            if "GOOGLE_API_KEY" not in st.secrets:
                return "BŁĄD: Brak klucza GOOGLE_API_KEY w secrets.toml"

            genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
            
            # Konfiguracja generacji dla lepszej jakości (kreatywność + precyzja)
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
            result = clean_ai_fingerprints(result)
            return result

        # ---------------------------------------------------------
        # OBSŁUGA OPENAI GPT
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
            result = clean_ai_fingerprints(result)
            return result
        
    except Exception as e:
        return f"BŁĄD GENEROWANIA: {str(e)}"

def generate_meta_tags(product_data: Dict, client: OpenAI, model: str) -> Tuple[str, str]:
    """Generuje meta title i meta description"""
    try:
        system_prompt = "Ekspert SEO. Meta Title (max 60 znaków), Meta Description (max 160 znaków, CTA). Format:\nMeta title: ...\nMeta description: ..."
        user_prompt = f"Produkt: {product_data.get('title')}\nInfo: {product_data.get('description')}"

        if "gemini" in model.lower():
             if "GOOGLE_API_KEY" not in st.secrets: return "", ""
             genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
             model_instance = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)
             response = model_instance.generate_content(user_prompt)
             result = response.text
        else:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.5,
                max_tokens=300
            )
            result = response.choices[0].message.content
        
        meta_title = ""
        meta_description = ""
        for line in result.splitlines():
            if "Meta title:" in line: meta_title = line.split(":", 1)[1].strip()
            if "Meta description:" in line: meta_description = line.split(":", 1)[1].strip()
            
        return clean_ai_fingerprints(meta_title)[:60], clean_ai_fingerprints(meta_description)[:160]
    except:
        return "", ""

def process_product_from_akeneo(sku: str, client: OpenAI, token: str, channel: str, locale: str, model: str) -> Dict:
    """Przetwarza pojedynczy produkt"""
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        
        if not product_details:
            return {'sku': sku, 'title': '', 'description_html': '', 'error': 'Produkt nie znaleziony'}
        
        # Przygotowanie danych
        details_parts = []
        author = safe_string_value(product_details.get('author'))
        if author: details_parts.append(f"Autor: {author}")
        publisher = safe_string_value(product_details.get('publisher'))
        if publisher: details_parts.append(f"Wydawnictwo: {publisher}")
        year = safe_string_value(product_details.get('year'))
        if year: details_parts.append(f"Rok: {year}")
        pages = safe_string_value(product_details.get('pages'))
        if pages: details_parts.append(f"Strony: {pages}")
        cover = safe_string_value(product_details.get('cover_type'))
        if cover: details_parts.append(f"Oprawa: {cover}")
        
        original_desc = safe_string_value(product_details.get('description', ''))
        quality_status, quality_msg = validate_description_quality(original_desc)
        
        product_title = safe_string_value(product_details['title'])
        product_url = generate_product_url(product_title)
        
        product_data = {
            'title': product_title,
            'author': author,
            'publisher': publisher,
            'details': ', '.join(details_parts),
            'description': original_desc
        }
        
        description_html = generate_description(product_data, client, model)
        
        if "BŁĄD" in description_html:
            return {'sku': sku, 'title': product_title, 'error': description_html, 'description_quality': (quality_status, quality_msg)}
        
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
        return {'sku': sku, 'title': '', 'error': str(e)}

# ═══════════════════════════════════════════════════════════════════
# SESSION STATE & WALIDACJA
# ═══════════════════════════════════════════════════════════════════

if 'bulk_results' not in st.session_state: st.session_state.bulk_results = []
if 'bulk_selected_products' not in st.session_state: st.session_state.bulk_selected_products = {}
if 'products_to_send' not in st.session_state: st.session_state.products_to_send = {}

if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak OPENAI_API_KEY w secrets.")
    st.stop()
if "GOOGLE_API_KEY" not in st.secrets:
    st.error("❌ Brak GOOGLE_API_KEY w secrets.")
    st.stop()

client = OpenAI()

# ═══════════════════════════════════════════════════════════════════
# HEADER & SIDEBAR
# ═══════════════════════════════════════════════════════════════════

col_logo, col_title = st.columns([1, 5])
with col_title:
    st.markdown('<h1 class="main-header">📚 Generator Opisów Produktów</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Powered by Google Gemini 3 & OpenAI</p>', unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Ustawienia")
    
    st.subheader("🤖 Model AI")
    # ZAKTUALIZOWANA LISTA Z GEMINI 3 JAKO DOMYŚLNYM
    model_choice = st.selectbox(
        "Wybierz model:",
        ["gemini-3-flash-preview", "gpt-4o-mini", "gpt-4o"],
        index=0,
        help="Gemini 3 Flash: Najnowszy, najszybszy model Google.\nGPT-4o-mini: Ekonomiczny model OpenAI."
    )
    
    st.markdown("---")
    channel = st.selectbox("Kanał:", ["Bookland", "B2B"], index=0)
    locale = st.text_input("Locale:", value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL"))
    
    st.markdown("---")
    st.header("📊 Baza")
    optimized_products = load_optimized_products()
    st.metric("Zoptymalizowane", len(optimized_products))
    
    if st.button("🗑️ Wyczyść bazę"):
        if st.checkbox("Potwierdź"):
            save_optimized_products([])
            st.rerun()

# ═══════════════════════════════════════════════════════════════════
# GŁÓWNA APLIKACJA
# ═══════════════════════════════════════════════════════════════════

st.subheader("📦 Przetwarzanie produktów")
method = st.radio("Metoda:", ["🔍 Wyszukiwarka", "📋 Lista SKU"], horizontal=True)

if method == "🔍 Wyszukiwarka":
    col_s, col_l = st.columns([4, 1])
    with col_s: search_q = st.text_input("Szukaj:", placeholder="Harry Potter")
    with col_l: limit_q = st.number_input("Limit", 5, 50, 10)
    
    if st.button("🔍 Szukaj") and search_q:
        with st.spinner("Szukam..."):
            res = akeneo_search_products(search_q, akeneo_get_token(), limit_q, locale)
            st.session_state.search_res = res
            if not res: st.warning("Brak wyników")

    if 'search_res' in st.session_state and st.session_state.search_res:
        st.markdown("---")
        if st.button("✅ Zaznacz wszystkie widoczne"):
            for p in st.session_state.search_res:
                st.session_state.bulk_selected_products[p['identifier']] = p
            st.rerun()
            
        for p in st.session_state.search_res:
            sku = p['identifier']
            sel = sku in st.session_state.bulk_selected_products
            c = st.checkbox(f"{sku} - {p['title']}", value=sel, key=f"s_{sku}")
            if c and not sel:
                st.session_state.bulk_selected_products[sku] = p
                st.rerun()
            elif not c and sel:
                del st.session_state.bulk_selected_products[sku]
                st.rerun()

else: # Lista SKU
    txt = st.text_area("SKU (jeden na linię):", height=150)
    if st.button("📋 Załaduj SKU") and txt:
        skus = [s.strip() for s in txt.split('\n') if s.strip()]
        for sku in skus:
            st.session_state.bulk_selected_products[sku] = {'identifier': sku, 'title': sku}
        st.success(f"Dodano {len(skus)} SKU")
        st.rerun()

# KOSZYK
if st.session_state.bulk_selected_products:
    st.markdown("---")
    st.subheader(f"🛒 Koszyk ({len(st.session_state.bulk_selected_products)})")
    
    if st.button("🗑️ Wyczyść koszyk"):
        st.session_state.bulk_selected_products = {}
        st.rerun()
        
    if st.button("🚀 GENERUJ OPISY", type="primary"):
        st.session_state.bulk_results = []
        token = akeneo_get_token()
        skus = list(st.session_state.bulk_selected_products.keys())
        bar = st.progress(0, "Start...")
        
        with ThreadPoolExecutor(max_workers=5) as exe:
            futures = {exe.submit(process_product_from_akeneo, sku, client, token, channel, locale, model_choice): sku for sku in skus}
            results = []
            for i, f in enumerate(as_completed(futures)):
                res = f.result()
                results.append(res)
                if not res.get('error'): st.session_state.products_to_send[res['sku']] = True
                bar.progress((i+1)/len(skus), f"Przetworzono {res['sku']}")
        
        st.session_state.bulk_results = results
        st.success("Gotowe!")
        st.rerun()

# WYNIKI
if st.session_state.bulk_results:
    st.markdown("---")
    st.subheader("📊 Wyniki generowania")
    
    succ = [r for r in st.session_state.bulk_results if not r.get('error')]
    errs = [r for r in st.session_state.bulk_results if r.get('error')]
    
    c1, c2 = st.columns(2)
    c1.metric("Sukces", len(succ))
    c2.metric("Błędy", len(errs))
    
    if succ:
        if st.button(f"📤 Wyślij zaznaczone ({sum(st.session_state.products_to_send.values())}) do Akeneo"):
            pro = st.progress(0)
            sent, fail = 0, 0
            for i, r in enumerate(succ):
                if st.session_state.products_to_send.get(r['sku']):
                    try:
                        akeneo_update_description(r['sku'], r['description_html'], channel, locale)
                        add_optimized_product(r['sku'], r['title'], r['url'])
                        sent += 1
                    except: fail += 1
                pro.progress((i+1)/len(succ))
            st.success(f"Wysłano: {sent}, Błędy: {fail}")
    
    st.markdown("### Podgląd")
    for r in succ:
        with st.expander(f"✅ {r['sku']} - {r['title']}"):
            st.checkbox("Wyślij", key=f"snd_{r['sku']}", value=st.session_state.products_to_send.get(r['sku'], True))
            c1, c2 = st.columns(2)
            with c1: st.markdown(r['description_html'], unsafe_allow_html=True)
            with c2: st.code(r['description_html'], language='html')

    for r in errs:
        st.error(f"{r['sku']}: {r['error']}")
