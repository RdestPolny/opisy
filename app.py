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
    page_title="Generator Opisów Produktów v3.2.0 (Gemini)",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
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
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# BAZA ZOPTYMALIZOWANYCH PRODUKTÓW
# ═══════════════════════════════════════════════════════════════════

DB_PATH = Path(".streamlit/optimized_products.json")

def ensure_db_exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        DB_PATH.write_text("[]")

def load_optimized_products() -> List[Dict]:
    ensure_db_exists()
    try:
        with open(DB_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

def save_optimized_products(products: List[Dict]):
    ensure_db_exists()
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

def add_optimized_product(sku: str, title: str, url: str):
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
    if not text: return text
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m: return m.group(1).strip()
    text = re.sub(r"^\s*```(?:html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()

def clean_ai_fingerprints(text: str) -> str:
    # Wymuszenie polskich myślników zgodnie z promptem
    text = text.replace('—', '-') 
    text = text.replace('–', '-')
    # Konwersja Markdown bold (**) na HTML (<b>) jako safety fallback
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    return text

def safe_string_value(value) -> str:
    if value is None: return ""
    if isinstance(value, list):
        return str(value[0]).strip() if len(value) > 0 else ""
    return str(value).strip()

def format_product_title(title: str, max_length: int = 80) -> str:
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    return title

def validate_description_quality(description) -> Tuple[str, str]:
    desc_str = safe_string_value(description)
    if not desc_str or len(desc_str.strip()) == 0:
        return 'error', '❌ Brak oryginalnego opisu w Akeneo!'
    desc_length = len(desc_str.strip())
    if desc_length < 100:
        return 'error', f'❌ Opis b. krótki ({desc_length} zn)!'
    elif desc_length < 300:
        return 'warning', f'⚠️ Opis krótki ({desc_length} zn).'
    else:
        return 'ok', '✅ Opis OK'

# ═══════════════════════════════════════════════════════════════════
# AKENEO API
# ═══════════════════════════════════════════════════════════════════

def _akeneo_root():
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
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    products_dict = {}
    
    try:
        # Search by identifier
        params_id = {"limit": limit, "search": json.dumps({"identifier": [{"operator": "CONTAINS", "value": search_query}]})}
        r1 = requests.get(url, headers=headers, params=params_id, timeout=30)
        if r1.status_code == 200:
            for item in r1.json().get("_embedded", {}).get("items", []):
                ident = item.get("identifier", "")
                title = ident
                if "name" in item.get("values", {}):
                    for val in item["values"]["name"]:
                        if val.get("locale") == locale or val.get("locale") is None:
                            title = safe_string_value(val.get("data", ident))
                            break
                products_dict[ident] = {"identifier": ident, "title": title, "family": item.get("family", ""), "enabled": item.get("enabled", False)}

        # Search by name
        params_name = {"limit": limit, "search": json.dumps({"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]})}
        r2 = requests.get(url, headers=headers, params=params_name, timeout=30)
        if r2.status_code == 200:
            for item in r2.json().get("_embedded", {}).get("items", []):
                ident = item.get("identifier", "")
                if ident not in products_dict:
                    title = ident
                    if "name" in item.get("values", {}):
                        for val in item["values"]["name"]:
                            if val.get("locale") == locale or val.get("locale") is None:
                                title = safe_string_value(val.get("data", ident))
                                break
                    products_dict[ident] = {"identifier": ident, "title": title, "family": item.get("family", ""), "enabled": item.get("enabled", False)}

        return list(products_dict.values())[:limit]
    except Exception as e:
        st.error(f"Błąd wyszukiwania: {str(e)}")
        return []

def akeneo_get_product_details(sku: str, token: str, channel: str = "Bookland", locale: str = "pl_PL") -> Optional[Dict]:
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
                if (val.get("scope") is None or val.get("scope") == channel) and \
                   (val.get("locale") is None or val.get("locale") == locale):
                    return safe_string_value(val.get("data", ""))
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
            "ean": get_value("ean"),
            "isbn": get_value("isbn")
        }
    except:
        return None

def akeneo_update_description(sku: str, html_description: str, channel: str, locale: str = "pl_PL") -> bool:
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt '{sku}' nie istnieje w Akeneo.")
    
    attr_desc = akeneo_get_attribute("description", token)
    is_scopable = bool(attr_desc.get("scopable", False))
    is_localizable = bool(attr_desc.get("localizable", False))
    
    value_obj = {
        "data": html_description,
        "scope": channel if is_scopable else None,
        "locale": locale if is_localizable else None,
    }
    
    payload = {"values": {"description": [value_obj]}}
    
    # Try updating SEO flag if exists
    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        seo_obj = {
            "data": True,
            "scope": channel if bool(attr_seo.get("scopable", False)) else None,
            "locale": locale if bool(attr_seo.get("localizable", False)) else None,
        }
        payload["values"]["opisy_seo"] = [seo_obj]
    except:
        pass

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=30)
    
    if r.status_code in (200, 204): return True
    raise RuntimeError(f"Błąd Akeneo ({r.status_code})")

# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - TYLKO GEMINI
# ═══════════════════════════════════════════════════════════════════

def generate_description(product_data: Dict, model: str = "gemini-3-flash-preview", internal_link: Optional[Dict] = None, link_only: bool = False) -> str:
    """
    Generuje opis produktu korzystając WYŁĄCZNIE z Google Gemini.
    """
    try:
        # 1. Sprawdzenie klucza API
        if "GOOGLE_API_KEY" not in st.secrets:
            return "BŁĄD: Brak klucza GOOGLE_API_KEY w secrets.toml"

        # 2. Promt z opcjonalnym linkowaniem
        link_instruction = ""
        if internal_link and internal_link.get('url') and internal_link.get('category'):
            link_instruction = f"""
6. LINKOWANIE WEWNĘTRZNE (KRYTYCZNE): MÓJ SYSTEM SEO WYMAGA, abyś ABSOLUTNIE I BEZWYJĄTKOWO wplótł w tekst naturalnie brzmiący link do kategorii: {internal_link['category']}.
   - URL linku: {internal_link['url']}
   - Zadanie: Jako wybitny ekspert SEO i Semantic SEO, dobierz naturalny kontekst i anchor. Anchor nie musi być identyczny z nazwą kategorii (może być odmianą, frazą powiązaną), ale musi pasować do tematu.
   - Umiejscowienie: Wpleć link tam, gdzie pasuje najlepiej (najlepiej w 2. lub 3. akapicie).
   - Format HTML: <a href="{internal_link['url']}">wybrany przez Ciebie naturalny anchor</a>.
   - UWAGA: Brak linku w tekście zostanie uznany za błąd krytyczny.
"""

        if link_only:
            system_prompt = f"""Jesteś wybitnym ekspertem SEO i Semantic SEO. Twoim zadaniem jest EDYCJA istniejącego opisu produktu w celu dodania linku wewnętrznego.

ZASADY TRYBU "TYLKO LINKOWANIE":
1. ZACHOWAJ ORYGINALNY OPIS: Nie zmieniaj stylu, nie przepisuj całego tekstu. Pozostaw treść z sekcji "ORYGINALNY OPIS" prawie nienaruszoną.
2. DODAJ LINK: Znajdź najbardziej naturalne miejsce w tekście, aby wpleść link wewnętrzny.
3. KONTEKST: Jeśli to konieczne, możesz dodać lub zmodyfikować JEDNO LUB DWA zdania, aby stworzyć naturalne przejście do linku.
4. WYMAGANIA TECHNICZNE: Zwróć opis w formacie HTML (<p>, <b>, <a>, <h2>). Jeśli oryginalny opis nie ma HTML, dodaj podstawowe tagi <p> i <b>.
5. ZAKAZ MARKDOWNA: Nie używaj składni Markdown (np. brak **). Używaj <b>.
{link_instruction}

Zwróć kompletny, gotowy kod HTML opisu z wplecionym linkiem."""
        else:
            system_prompt = f"""Jesteś wybitnym ekspertem SEO, specjalistą od Semantic SEO i doświadczonym copywriterem e-commerce. Tworzysz angażujące, sprzedażowe opisy produktów, które nie tylko konwertują, ale budują silną strukturę semantyczną sklepu.

WYTYCZNE DOTYCZĄCE TREŚCI I STYLU:
1. Unikalność: Każde zdanie musi wnosić nową wartość. Unikaj powtórzeń (duplicate content) i "lania wody".
2. Dane techniczne: NIGDY nie twórz listy danych technicznych. Wpleć je naturalnie w treść akapitów.
3. Formatowanie (HTML): Używaj WYŁĄCZNIE tagów <p>, <h2>, <h3>, <b>, <a>. ABSOLUTNY ZAKAZ używania składni Markdown (np. NIGDY nie używaj ** do pogrubień).
4. Boldowanie: W pierwszym akapicie <p> pogrub tagiem <b>: tytuł produktu, autora/markę oraz 2-3 kluczowe frazy. W całym tekście użyj max 8-10 pogrubień tagiem <b>.
5. Interpunkcja: Używaj wyłącznie dywizu (półpauzy) "-" jako myślnika.{link_instruction}

STRUKTURA OPISU (Elastyczna: 2 lub 3 sekcje główne):

[SEKCJA 1]
<p> Wstęp (4-6 zdań). Przedstaw produkt, zbolduj nazwę i autora. Zbuduj zainteresowanie. </p>

[SEKCJA 2]
<h2> Nagłówek mówiący o korzyści </h2>
<p> Rozwinięcie (5-8 zdań). Tutaj naturalnie wpleć specyfikację techniczną, łącząc cechy z korzyściami dla klienta. </p>

[SEKCJA 3 - Opcjonalna]
<h2> Drugi nagłówek z inną korzyścią </h2>
<p> Dalszy opis (4-6 zdań). </p>

[ZAKOŃCZENIE I CTA]
W ostatnim akapicie <p> (niezależnie czy jest to sekcja 2 czy 3) ostatnie 1-2 zdania to Call To Action.
<h3> Krótkie hasło podsumowujące - to jest ostatni element tekstu. </h3>"""

        # 3. Przygotowanie danych wejściowych
        raw_data = f"""
TYTUŁ PRODUKTU: {product_data.get('title', '')}
AUTOR/MARKA: {product_data.get('author', '')}
DANE TECHNICZNE: {product_data.get('details', '')}
ORYGINALNY OPIS: {product_data.get('description', '')}
"""
        if internal_link:
            raw_data += f"\nLINK DO WPLECENIA: {internal_link['url']} (Kategoria: {internal_link['category']})\n"

        raw_data += "\nZwróć TYLKO kod HTML."

        # 4. Konfiguracja i wywołanie Gemini
        genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
        model_instance = genai.GenerativeModel(
            model_name="gemini-3-flash-preview",
            system_instruction=system_prompt
        )
        
        response = model_instance.generate_content(raw_data)
        
        # 5. Cleaning
        result = strip_code_fences(response.text)
        result = clean_ai_fingerprints(result)
        
        return result

    except Exception as e:
        return f"BŁĄD GEMINI: {str(e)}"

def process_product_from_akeneo(sku: str, token: str, channel: str, locale: str, internal_link: Optional[Dict] = None, link_only: bool = False) -> Dict:
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        
        if not product_details:
            return {
                'sku': sku, 'title': '', 'description_html': '', 'url': '',
                'error': 'Produkt nie znaleziony', 'description_quality': ('error', 'Produkt nie znaleziony')
            }
        
        # Przygotowanie details
        details_list = []
        if product_details.get('publisher'): details_list.append(f"Wydawnictwo: {product_details['publisher']}")
        if product_details.get('year'): details_list.append(f"Rok: {product_details['year']}")
        if product_details.get('pages'): details_list.append(f"Strony: {product_details['pages']}")
        if product_details.get('cover_type'): details_list.append(f"Oprawa: {product_details['cover_type']}")
        
        original_desc = safe_string_value(product_details.get('description', ''))
        quality_status, quality_msg = validate_description_quality(original_desc)
        
        product_data = {
            'title': safe_string_value(product_details['title']),
            'author': safe_string_value(product_details['author']),
            'details': ', '.join(details_list),
            'description': original_desc
        }
        
        # Wywołanie generowania (bez wyboru modelu - zawsze Gemini)
        description_html = generate_description(product_data, internal_link=internal_link, link_only=link_only)
        
        return {
            'sku': sku,
            'title': product_data['title'],
            'description_html': description_html,
            'url': generate_product_url(product_data['title']),
            'old_description': original_desc,
            'error': description_html if "BŁĄD" in description_html else None,
            'description_quality': (quality_status, quality_msg)
        }
        
    except Exception as e:
        return {'sku': sku, 'title': '', 'error': str(e), 'description_quality': ('error', str(e))}

# ═══════════════════════════════════════════════════════════════════
# SESSION STATE & INIT
# ═══════════════════════════════════════════════════════════════════

if 'bulk_results' not in st.session_state: st.session_state.bulk_results = []
if 'bulk_selected_products' not in st.session_state: st.session_state.bulk_selected_products = {}
if 'products_to_send' not in st.session_state: st.session_state.products_to_send = {}

required_secrets = ["AKENEO_BASE_URL", "AKENEO_CLIENT_ID", "AKENEO_SECRET", "AKENEO_USERNAME", "AKENEO_PASSWORD", "GOOGLE_API_KEY"]
missing = [k for k in required_secrets if k not in st.secrets]
if missing:
    st.error(f"❌ Brak kluczy w secrets.toml: {', '.join(missing)}")
    st.stop()

# ═══════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════

col_logo, col_title = st.columns([1, 5])
with col_title:
    st.markdown('<h1 class="main-header">📚 Generator Opisów Produktów</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Powered by Google Gemini (gemini-3-flash-preview)</p>', unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Ustawienia")
    
    # INFO O MODELU (Sztywno ustawiony)
    st.info("🤖 Model aktywny:\n**gemini-3-flash-preview**")
    
    st.markdown("---")
    channel = st.selectbox("Kanał:", ["Bookland", "B2B"], index=0)
    locale = st.text_input("Locale:", value="pl_PL")
    
    st.markdown("---")
    st.header("🔗 Linkowanie wewnętrzne")
    st.session_state.link_active = st.checkbox("Włącz linkowanie", value=st.session_state.get("link_active", False))
    st.session_state.link_only = st.checkbox("Tryb: Tylko dopisanie linku (nie zmieniaj opisu)", value=st.session_state.get("link_only", False))
    st.session_state.link_url = st.text_input("URL linku:", placeholder="np. https://bookland.com.pl/beletrystyka", value=st.session_state.get("link_url", ""))
    st.session_state.link_category = st.text_input("Kategoria/Anchor hint:", placeholder="np. Beletrystyka", value=st.session_state.get("link_category", ""))
    
    st.markdown("---")
    st.header("📊 Baza produktów")
    optimized = load_optimized_products()
    st.metric("Zoptymalizowane", len(optimized))
    
    if st.button("🗑️ Wyczyść bazę", type="secondary"):
        save_optimized_products([])
        st.rerun()

# ═══════════════════════════════════════════════════════════════════
# LOGIKA GŁÓWNA
# ═══════════════════════════════════════════════════════════════════

st.subheader("📦 Przetwarzanie produktów")
method = st.radio("Metoda:", ["🔍 Wyszukaj i zaznacz", "📋 Wklej listę SKU"], horizontal=True)

if method == "🔍 Wyszukaj i zaznacz":
    # Koszyk
    if st.session_state.bulk_selected_products:
        with st.expander(f"🛒 Koszyk ({len(st.session_state.bulk_selected_products)})", expanded=True):
            for sku, data in list(st.session_state.bulk_selected_products.items()):
                c1, c2 = st.columns([5,1])
                c1.write(f"**{sku}** - {data.get('title')}")
                if c2.button("🗑️", key=f"del_{sku}"):
                    del st.session_state.bulk_selected_products[sku]
                    st.rerun()
            if st.button("Wyczyść koszyk"):
                st.session_state.bulk_selected_products = {}
                st.rerun()

    # Szukanie
    c_s, c_l = st.columns([4, 1])
    query = c_s.text_input("Szukaj:")
    limit = c_l.number_input("Limit:", 5, 50, 10)
    
    if st.button("🔍 Szukaj", type="primary"):
        if query:
            with st.spinner("Szukam..."):
                token = akeneo_get_token()
                res = akeneo_search_products(query, token, limit, locale)
                st.session_state.search_res = res
                if not res: st.warning("Brak wyników")
    
    # Wyniki szukania
    if 'search_res' in st.session_state and st.session_state.search_res:
        st.write("---")
        st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)
        for p in st.session_state.search_res:
            sku = p['identifier']
            sel = sku in st.session_state.bulk_selected_products
            if st.checkbox(f"{sku} - {p['title']}", value=sel, key=f"s_{sku}"):
                st.session_state.bulk_selected_products[sku] = {'title': p['title']}
            elif sel:
                del st.session_state.bulk_selected_products[sku]
        st.markdown('</div>', unsafe_allow_html=True)

else:
    # Lista SKU
    txt = st.text_area("SKU (jeden na linię):", height=150)
    if st.button("Załaduj SKU", type="primary"):
        skus = [s.strip() for s in txt.split('\n') if s.strip()]
        for s in skus:
            st.session_state.bulk_selected_products[s] = {'title': s}
        st.success(f"Dodano {len(skus)} SKU")
        st.rerun()
    
    if st.session_state.bulk_selected_products:
        st.info(f"W koszyku: {len(st.session_state.bulk_selected_products)}")
        if st.button("Wyczyść"):
            st.session_state.bulk_selected_products = {}
            st.rerun()

# GENEROWANIE
if st.session_state.bulk_selected_products:
    st.markdown("---")
    st.subheader("🚀 Generowanie")
    
    if st.button("Start Generowania (Gemini)", type="primary"):
        st.session_state.bulk_results = []
        token = akeneo_get_token()
        skus = list(st.session_state.bulk_selected_products.keys())
        bar = st.progress(0, "Start...")
        
        # Przygotowanie danych o linkowaniu w głównym wątku
        internal_link = None
        link_only = st.session_state.get("link_only", False)
        if st.session_state.get("link_active") and st.session_state.get("link_url") and st.session_state.get("link_category"):
            internal_link = {
                "url": st.session_state.link_url,
                "category": st.session_state.link_category
            }

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(process_product_from_akeneo, s, token, channel, locale, internal_link, link_only): s for s in skus}
            for i, f in enumerate(as_completed(futs)):
                res = f.result()
                st.session_state.bulk_results.append(res)
                if not res.get('error'):
                    st.session_state.products_to_send[res['sku']] = True
                bar.progress((i+1)/len(skus), f"Przetworzono {res['sku']}")
        
        bar.progress(1.0, "Gotowe!")
        st.rerun()

# WYNIKI
if st.session_state.bulk_results:
    st.markdown("---")
    results = st.session_state.bulk_results
    ok = [r for r in results if not r.get('error')]
    err = [r for r in results if r.get('error')]
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Sukces", len(ok))
    c2.metric("Błędy", len(err))
    
    # CSV
    df = pd.DataFrame(results)
    st.download_button("Pobierz CSV", df.to_csv(index=False).encode('utf-8'), 'wyniki.csv', 'text/csv')
    
    # WYSYŁKA
    if ok:
        st.subheader("📤 Wysyłka do Akeneo")
        
        # Select all buttons
        c_all, c_none = st.columns(2)
        if c_all.button("Zaznacz wszystko"):
            for r in ok: st.session_state.products_to_send[r['sku']] = True
            st.rerun()
        if c_none.button("Odznacz wszystko"):
            for r in ok: st.session_state.products_to_send[r['sku']] = False
            st.rerun()

        # Checkboxy
        to_send_list = []
        for r in ok:
            chk = st.checkbox(f"{r['sku']} - {r['title']}", value=st.session_state.products_to_send.get(r['sku'], True), key=f"send_{r['sku']}")
            st.session_state.products_to_send[r['sku']] = chk
            if chk: to_send_list.append(r)
        
        if st.button(f"Wyślij zaznaczone ({len(to_send_list)})", type="primary"):
            bar_s = st.progress(0, "Wysyłanie...")
            cnt = 0
            errs = []
            
            for i, item in enumerate(to_send_list):
                try:
                    akeneo_update_description(item['sku'], item['description_html'], channel, locale)
                    add_optimized_product(item['sku'], item['title'], item['url'])
                    cnt += 1
                except Exception as e:
                    errs.append(f"{item['sku']}: {e}")
                bar_s.progress((i+1)/len(to_send_list))
            
            st.success(f"Wysłano {cnt} produktów")
            if errs: st.error('\n'.join(errs))
            
    # PODGLĄD
    st.markdown("---")
    st.subheader("Podgląd wyników")
    for r in results:
        label = "✅" if not r.get('error') else "❌"
        with st.expander(f"{label} {r['sku']} - {r['title']}"):
            if r.get('error'):
                st.error(r['error'])
            else:
                c_html, c_preview = st.tabs(["HTML", "Podgląd"])
                with c_html: st.code(r['description_html'], language='html')
                with c_preview: st.markdown(r['description_html'], unsafe_allow_html=True)
                
                # REGENERATE BUTTON
                if st.button("♻️ Regeneruj", key=f"reg_{r['sku']}"):
                    token = akeneo_get_token()
                    # Przygotowanie danych o linkowaniu
                    internal_link = None
                    link_only = st.session_state.get("link_only", False)
                    if st.session_state.get("link_active") and st.session_state.get("link_url") and st.session_state.get("link_category"):
                        internal_link = {
                            "url": st.session_state.link_url,
                            "category": st.session_state.link_category
                        }
                    new_res = process_product_from_akeneo(r['sku'], token, channel, locale, internal_link, link_only)
                    # Update result in list
                    for i, existing in enumerate(st.session_state.bulk_results):
                        if existing['sku'] == r['sku']:
                            st.session_state.bulk_results[i] = new_res
                    st.rerun()

st.markdown("---")
st.caption("Generator Opisów Produktów v3.2.0 | Powered by Gemini 3 Flash Preview")
