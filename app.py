import streamlit as st
import pandas as pd
import requests
from openai import OpenAI
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import time

# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STRONY
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Generator Opisów Produktów v2.0",
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
</style>
""", unsafe_allow_html=True)

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

def format_product_title(title: str, max_length: int = 80) -> str:
    """Formatuje tytuł produktu"""
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    return title

# ═══════════════════════════════════════════════════════════════════
# AKENEO API
# ═══════════════════════════════════════════════════════════════════

def _akeneo_root():
    """Zwraca root URL Akeneo"""
    base = st.secrets["AKENEO_BASE_URL"].rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[:-len("/api/rest/v1")]
    return base

def akeneo_get_token() -> str:
    """Pobiera access token dla Akeneo API"""
    token_url = _akeneo_root() + "/api/oauth/v1/token"
    auth = (st.secrets["AKENEO_CLIENT_ID"], st.secrets["AKENEO_SECRET"])
    data = {
        "grant_type": "password",
        "username": st.secrets["AKENEO_USERNAME"],
        "password": st.secrets["AKENEO_PASSWORD"],
    }
    r = requests.post(token_url, auth=auth, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

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
                        title = val.get("data", identifier)
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
                        title = val.get("data", identifier)
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

def akeneo_get_products_by_skus(skus: List[str], token: str, locale: str = "pl_PL") -> List[Dict]:
    """Pobiera wiele produktów po listach SKU"""
    products = []
    for sku in skus:
        try:
            product = akeneo_get_product_details(sku.strip(), token, "Bookland", locale)
            if product:
                products.append({
                    "identifier": sku.strip(),
                    "title": product.get('title', sku.strip()),
                    "family": product.get('family', ''),
                    "enabled": product.get('enabled', False),
                    "product_details": product
                })
        except:
            pass
    return products

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
                    return val.get("data", "")
            return attr_values[0].get("data", "")
        
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
# GENEROWANIE OPISÓW - GPT-5-NANO
# ═══════════════════════════════════════════════════════════════════

def generate_description(product_data: Dict, client: OpenAI, style_variant: str = "default") -> str:
    """Generuje opis produktu z wykorzystaniem GPT-5-nano"""
    try:
        system_prompt = """Jesteś EKSPERTEM copywritingu e-commerce i języka polskiego. Twoje opisy są poprawne gramatycznie, angażujące i konwertują.

╔═══════════════════════════════════════════════════════════════════╗
║  STRUKTURA OPISU (PROSTA I CZYTELNA)                              ║
╚═══════════════════════════════════════════════════════════════════╝

OBOWIĄZKOWA STRUKTURA (tylko 4 elementy!):

1. <p>[AKAPIT WSTĘPNY: 4-6 zdań przedstawiających produkt, jego główne cechy i pierwsze wrażenie. Core informacje o produkcie. BEZ danych technicznych jak wymiary, rok, ISBN.]</p>

2. <h2>[Jeden konkretny nagłówek z korzyścią lub cechą produktu]</h2>

3. <p>[AKAPIT GŁÓWNY: 5-8 zdań rozwijających temat. Dla kogo jest produkt, co go wyróżnia, jakie emocje wzbudza, dlaczego warto. TUTAJ naturalnie wpleć dane techniczne jeśli są istotne (np. oprawa, rok, wymiary). Buduj hype i entuzjazm.]</p>

4. <h3>[Wezwanie do działania - krótkie i konkretne]</h3>

5. <p>[KRÓTKIE ZAKOŃCZENIE: 1-2 zdania zachęcające do zakupu]</p>

╔═══════════════════════════════════════════════════════════════════╗
║  KRYTYCZNE ZASADY JĘZYKA POLSKIEGO                                ║
╚═══════════════════════════════════════════════════════════════════╝

**POPRAWNOŚĆ GRAMATYCZNA (NAJWAŻNIEJSZE!):**

1. PRZYPADKI:
   ✅ "Ta część Wiedźmina" (dopełniacz!)
   ❌ "Ta część Wiedźmin"
   
   ✅ "pełnego politycznych machinacji i spisków" (dopełniacz!)
   ❌ "pełnego politycznych machinacji i spiski"
   
   ✅ "dzięki dopracowanym komponentom" (celownik!)
   ❌ "dzięki dopracowanymi komponentami"

2. ODMIANA PRZEZ PRZYPADKI:
   - Mianownik (kto? co?): Wiedźmin
   - Dopełniacz (kogo? czego?): Wiedźmina, spisków, machinacji
   - Celownik (komu? czemu?): Wiedźminowi, komponentom
   - Biernik (kogo? co?): Wiedźmina, opowieść
   
3. ZGODA:
   ✅ "bogata ilustracja"
   ❌ "bogaty ilustracja"
   
   ✅ "dopracowane komponenty"
   ❌ "dopracowany komponenty"

4. INTERPUNKCJA:
   ✅ Używaj tylko "-" (myślnik)
   ❌ NIGDY "—" (em dash) ani "–" (en dash)

**STYL I TREŚĆ:**

1. AKAPITY:
   - Pierwszy akapit: 4-6 zdań (minimum 300 znaków)
   - Akapit główny: 5-8 zdań (minimum 400 znaków)
   - Zakończenie: 1-2 zdania (około 100 znaków)

2. BOLDOWANIE:
   ✅ 6-10 pojedynczych słów lub krótkich fraz (2-4 słowa)
   ✅ Rozmieść równomiernie przez cały tekst
   ❌ NIGDY całych zdań

3. DANE TECHNICZNE:
   - Wpleć naturalnie w akapit główny
   - Np: "Wydanie w twardej oprawie z 2023 roku liczy 320 stron"
   - NIE twórz osobnej sekcji z danymi

4. NIE POWTARZAJ:
   - Sprawdź przed wysłaniem czy nie powtarzasz tych samych informacji
   - Każde zdanie musi wnosić coś nowego

╔═══════════════════════════════════════════════════════════════════╗
║  PRZYKŁAD DOBREJ STRUKTURY                                        ║
╚═══════════════════════════════════════════════════════════════════╝

<p>Zanurz się w <b>mrocznym świecie Wiedźmina</b>, gdzie polityczne intrygi splatają się z magią, a każda decyzja ma nieodwracalne konsekwencje. Tom czwarty sagi Andrzeja Sapkowskiego przenosi czytelnika w sam środek nadciągającej burzy - czas pogardy nadszedł, a bohaterowie muszą stanąć po jednej ze stron konfliktu. To opowieść, która trzyma w napięciu od pierwszej do ostatniej strony, łącząc epicką skalę wydarzeń z intymnymi portretami postaci. Geralt, Yennefer i Ciri stają przed wyborami, które zmienią losy całego kontynentu.</p>

<h2>Dla kogo jest ta książka i co ją wyróżnia</h2>

<p>Ten tom to obowiązkowa lektura dla fanów <b>fantasy</b> i miłośników <b>złożonych intryg politycznych</b>. Sapkowski mistrzowsko łączy wątki fabularne, tworząc mozaikę perspektyw, która buduje napięcie z każdym rozdziałem. Historia nie oszczędza czytelnika - decyzje bohaterów mają realne konsekwencje, a granica między dobrem a złem jest płynna. <b>Dynamiczna narracja</b> przeplatana scenami akcji i dialogami pełnymi subtelnego humoru sprawia, że czas lektury mija niepostrzeżenie. Wydanie w miękkiej oprawie od SuperNowej to poręczny format, idealny do czytania w podróży. Klarowna typografia i dobry papier zapewniają komfort nawet podczas długich sesji czytania, a seria Wiedźmina pozostaje jedną z najważniejszych <b>polskich sag fantasy</b> ostatnich dekad.</p>

<h3>Dodaj do koszyka i dołącz do legendy</h3>

<p>Nie czekaj - zamów teraz i odkryj, dlaczego miliony czytelników na całym świecie pokochało <b>sagę o Wiedźminie</b>. Twoja przygoda z Czasem Pogardy zaczyna się już dziś!</p>

╔═══════════════════════════════════════════════════════════════════╗
║  ANTI-PRZYKŁADY (CZEGO NIE ROBIĆ!)                                ║
╚═══════════════════════════════════════════════════════════════════╝

❌ ZŁE: "Ta część Wiedźmin" → ✅ DOBRE: "Ta część Wiedźmina"
❌ ZŁE: "pełnego spiski" → ✅ DOBRE: "pełnego spisków"
❌ ZŁE: "dzięki komponentami" → ✅ DOBRE: "dzięki komponentom"
❌ ZŁE: Akapit 2 zdania → ✅ DOBRE: Akapit 5+ zdań
❌ ZŁE: 3 nagłówki H2 → ✅ DOBRE: 1 nagłówek H2
❌ ZŁE: Powtórzenie "klimat" 2x → ✅ DOBRE: Każde słowo raz

╔═══════════════════════════════════════════════════════════════════╗
║  DŁUGOŚĆ I TON                                                     ║
╚═══════════════════════════════════════════════════════════════════╝

- Całość: 1500-2500 znaków
- Ton dostosowany do produktu (książka/gra/zabawka)
- Naturalny, płynny język
- Entuzjastyczny ale wiarygodny

╔═══════════════════════════════════════════════════════════════════╗
║  TWOJA ODPOWIEDŹ                                                   ║
╚═══════════════════════════════════════════════════════════════════╝

Zwróć TYLKO czysty HTML.
Struktura: <p> → <h2> → <p> → <h3> → <p>
Sprawdź PRZED wysłaniem:
- Wszystkie przypadki poprawne?
- Akapity długie (4+ zdań)?
- Brak powtórzeń?
- Tylko jeden H2?
- Brak em dash?
"""

        style_additions = {
            "alternative": "\n\nStyl alternatywny: bardziej bezpośredni ton, krótsze zdania w ramach długich akapitów, mocniejsze CTA.",
            "concise": "\n\nStyl zwięzły: informacje bez ozdobników, ale nadal w długich akapitach. 1500-1800 znaków.",
            "detailed": "\n\nStyl szczegółowy: rozbudowany storytelling, głębszy kontekst. 2200-2500 znaków."
        }
        
        if style_variant in style_additions:
            system_prompt += style_additions[style_variant]

        raw_data = f"""
TYTUŁ PRODUKTU:
{product_data.get('title', '')}

SZCZEGÓŁY TECHNICZNE (wpleć naturalnie w akapit główny):
{product_data.get('details', '')}

ORYGINALNY OPIS (główne źródło informacji o produkcie):
{product_data.get('description', '')}

PAMIĘTAJ:
- Pierwsza część Wiedźmina (nie: część Wiedźmin)
- Pełnego spisków (nie: pełnego spiski)
- Dzięki komponentom (nie: dzięki komponentami)
- Długie akapity (5+ zdań)
- Tylko jeden H2
- Brak powtórzeń
"""
        
        response = client.responses.create(
            model="gpt-5-nano",
            input=f"{system_prompt}\n\n{raw_data}",
            reasoning={"effort": "high"},
            text={"verbosity": "medium"}
        )
        
        result = strip_code_fences(response.output_text)
        result = clean_ai_fingerprints(result)
        return result
        
    except Exception as e:
        return f"BŁĄD: {str(e)}"

def generate_meta_tags(product_data: Dict, client: OpenAI) -> Tuple[str, str]:
    """Generuje meta title i meta description"""
    try:
        system_prompt = """Ekspert SEO.

Meta Title: max 60 znaków, słowo kluczowe na początku, myślnik "-", bez kropek
Meta Description: max 160 znaków, CTA, myślnik "-"

FORMAT:
Meta title: [treść]
Meta description: [treść]"""
        
        user_prompt = f"Produkt: {product_data.get('title', '')}\nDane: {product_data.get('details', '')} {product_data.get('description', '')}"

        response = client.responses.create(
            model="gpt-5-nano",
            input=f"{system_prompt}\n\n{user_prompt}",
            reasoning={"effort": "medium"},
            text={"verbosity": "low"}
        )
        
        result = response.output_text
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

def process_product_from_akeneo(sku: str, client: OpenAI, token: str, channel: str, locale: str, style_variant: str = "default") -> Dict:
    """Przetwarza pojedynczy produkt z Akeneo"""
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        
        if not product_details:
            return {
                'sku': sku,
                'title': '',
                'description_html': '',
                'error': 'Produkt nie znaleziony'
            }
        
        # Przygotowanie danych
        details_parts = []
        if product_details.get('author'):
            details_parts.append(f"Autor: {product_details['author']}")
        if product_details.get('publisher'):
            details_parts.append(f"Wydawnictwo: {product_details['publisher']}")
        if product_details.get('year'):
            details_parts.append(f"Rok: {product_details['year']}")
        if product_details.get('pages'):
            details_parts.append(f"Strony: {product_details['pages']}")
        if product_details.get('cover_type'):
            details_parts.append(f"Oprawa: {product_details['cover_type']}")
        
        product_data = {
            'title': product_details['title'],
            'details': '\n'.join(details_parts),
            'description': product_details.get('description', '') or product_details.get('short_description', '')
        }
        
        # Generowanie
        description_html = generate_description(product_data, client, style_variant)
        
        if "BŁĄD" in description_html:
            return {
                'sku': sku,
                'title': product_details['title'],
                'description_html': '',
                'error': description_html
            }
        
        return {
            'sku': sku,
            'title': product_details['title'],
            'description_html': description_html,
            'old_description': product_details.get('description', ''),
            'error': None
        }
        
    except Exception as e:
        return {
            'sku': sku,
            'title': '',
            'description_html': '',
            'error': str(e)
        }

# ═══════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════

if 'search_results' not in st.session_state:
    st.session_state.search_results = []
if 'selected_product' not in st.session_state:
    st.session_state.selected_product = None
if 'generated_description' not in st.session_state:
    st.session_state.generated_description = None
if 'bulk_results' not in st.session_state:
    st.session_state.bulk_results = []
if 'bulk_selected_products' not in st.session_state:
    st.session_state.bulk_selected_products = {}  # Dict: {sku: product_data}

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

client = OpenAI()

# ═══════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════

col_logo, col_title = st.columns([1, 5])
with col_title:
    st.markdown('<h1 class="main-header">📚 Generator Opisów Produktów</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Inteligentne opisy produktów z Akeneo PIM • Powered by GPT-5-nano</p>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ Ustawienia")
    
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
    
    st.header("📊 Warianty stylistyczne")
    st.caption("**default** - standardowy, zbalansowany")
    st.caption("**alternative** - bezpośredni, mocne CTA")
    st.caption("**concise** - zwięzły, konkretny")
    st.caption("**detailed** - szczegółowy, storytelling")
    
    st.markdown("---")
    
    st.header("ℹ️ Informacje")
    st.info("""
**Jak używać:**
1. Wyszukaj produkt w Akeneo
2. Wybierz z listy
3. Wygeneruj opis
4. Zaktualizuj w PIM

**Tryb zbiorczy:**
- Wyszukuj różne produkty
- Zaznaczaj interesujące
- Wszystkie trafiają do "koszyka"
- Generuj wszystkie naraz
- Lub wklej listę SKU
    """)

# ═══════════════════════════════════════════════════════════════════
# MAIN TABS
# ═══════════════════════════════════════════════════════════════════

tab1, tab2 = st.tabs(["🔍 Wyszukaj produkt", "📦 Tryb zbiorczy"])

# ═══════════════════════════════════════════════════════════════════
# TAB 1: POJEDYNCZY PRODUKT
# ═══════════════════════════════════════════════════════════════════

with tab1:
    # WYSZUKIWARKA
    with st.container():
        st.subheader("🔎 Wyszukiwanie produktu")
        
        col_search, col_limit = st.columns([4, 1])
        
        with col_search:
            search_query = st.text_input(
                "Wpisz nazwę produktu lub SKU:",
                placeholder="np. Harry Potter",
                label_visibility="collapsed"
            )
        
        with col_limit:
            search_limit = st.number_input(
                "Limit",
                min_value=5,
                max_value=50,
                value=20,
                label_visibility="collapsed"
            )
        
        col_btn1, col_btn2 = st.columns([1, 1])
        
        with col_btn1:
            if st.button("🔍 Szukaj", type="primary", use_container_width=True):
                if not search_query:
                    st.warning("⚠️ Wpisz frazę do wyszukania")
                else:
                    with st.spinner(f"Wyszukuję '{search_query}'..."):
                        token = akeneo_get_token()
                        results = akeneo_search_products(search_query, token, search_limit, locale)
                        st.session_state.search_results = results
                        st.session_state.selected_product = None
                        st.session_state.generated_description = None
                        
                        if results:
                            st.success(f"✅ Znaleziono {len(results)} produktów!")
                        else:
                            st.warning("⚠️ Nie znaleziono produktów")
        
        with col_btn2:
            if st.button("🗑️ Wyczyść", use_container_width=True):
                st.session_state.search_results = []
                st.session_state.selected_product = None
                st.session_state.generated_description = None
                st.rerun()
    
    st.markdown("---")
    
    # WYNIKI WYSZUKIWANIA
    if st.session_state.search_results:
        st.subheader("📋 Wybierz produkt")
        
        product_options = {}
        for prod in st.session_state.search_results:
            display = f"{prod['identifier']} - {format_product_title(prod['title'])}"
            if not prod['enabled']:
                display += " [WYŁĄCZONY]"
            product_options[display] = prod
        
        selected_display = st.selectbox(
            "Produkt:",
            options=list(product_options.keys()),
            label_visibility="collapsed"
        )
        
        if selected_display:
            selected = product_options[selected_display]
            st.session_state.selected_product = selected
            
            # INFO BOX
            col_info1, col_info2, col_info3 = st.columns(3)
            with col_info1:
                st.metric("SKU", selected['identifier'])
            with col_info2:
                st.metric("Rodzina", selected['family'] or "N/A")
            with col_info3:
                status = "✅ Aktywny" if selected['enabled'] else "❌ Wyłączony"
                st.metric("Status", status)
            
            st.markdown("---")
            
            # GENEROWANIE
            st.subheader("✨ Generowanie opisu")
            
            col_gen1, col_gen2, col_gen3 = st.columns([2, 2, 1])
            
            with col_gen1:
                style_variant = st.selectbox(
                    "Wariant:",
                    ["default", "alternative", "concise", "detailed"],
                    index=0
                )
            
            with col_gen2:
                generate_meta = st.checkbox("Generuj metatagi SEO", value=False)
            
            with col_gen3:
                st.write("")  # spacer
                st.write("")
                if st.button("🚀 Generuj", type="primary", use_container_width=True):
                    with st.spinner("Pobieram dane i generuję..."):
                        token = akeneo_get_token()
                        result = process_product_from_akeneo(
                            selected['identifier'],
                            client,
                            token,
                            channel,
                            locale,
                            style_variant
                        )
                        
                        if result['error']:
                            st.error(f"❌ {result['error']}")
                        else:
                            st.session_state.generated_description = result
                            
                            if generate_meta:
                                product_data = {
                                    'title': result['title'],
                                    'details': '',
                                    'description': result['description_html']
                                }
                                meta_title, meta_desc = generate_meta_tags(product_data, client)
                                st.session_state.meta_title = meta_title
                                st.session_state.meta_description = meta_desc
                            
                            st.success("✅ Opis wygenerowany!")
                            st.rerun()
    
    # WYNIK GENEROWANIA
    if st.session_state.generated_description:
        st.markdown("---")
        st.subheader("📄 Wygenerowany opis")
        
        result = st.session_state.generated_description
        
        # Tabs dla kodu i podglądu
        tab_code, tab_preview = st.tabs(["💻 Kod HTML", "👁️ Porównanie"])
        
        with tab_code:
            st.code(result['description_html'], language='html')
            st.caption(f"Długość: {len(result['description_html'])} znaków")
        
        with tab_preview:
            if result.get('old_description'):
                col_old, col_new = st.columns(2)
                with col_old:
                    st.markdown("### Stary opis (Akeneo)")
                    st.caption(f"📏 {len(result['old_description'])} znaków")
                    st.markdown("---")
                    old_desc_display = result['old_description'][:800] + "..." if len(result['old_description']) > 800 else result['old_description']
                    st.markdown(old_desc_display, unsafe_allow_html=True)
                with col_new:
                    st.markdown("### Nowy opis (AI)")
                    st.caption(f"📏 {len(result['description_html'])} znaków")
                    st.markdown("---")
                    st.markdown(result['description_html'], unsafe_allow_html=True)
            else:
                st.info("Brak starego opisu w Akeneo - wyświetlam tylko nowy")
                st.markdown(result['description_html'], unsafe_allow_html=True)
        
        # Metatagi
        if 'meta_title' in st.session_state:
            st.markdown("---")
            col_meta1, col_meta2 = st.columns(2)
            with col_meta1:
                title_len = len(st.session_state.meta_title)
                color = "🟢" if title_len <= 60 else "🔴"
                st.markdown(f"**Meta Title** {color} ({title_len}/60)")
                st.text(st.session_state.meta_title)
            with col_meta2:
                desc_len = len(st.session_state.meta_description)
                color = "🟢" if desc_len <= 160 else "🔴"
                st.markdown(f"**Meta Description** {color} ({desc_len}/160)")
                st.text(st.session_state.meta_description)
        
        # Akcje
        st.markdown("---")
        col_act1, col_act2 = st.columns([1, 1])
        
        with col_act1:
            if st.button("♻️ Przeredaguj opis", use_container_width=True):
                with st.spinner("Przeredagowuję..."):
                    import random
                    variants = ["default", "alternative", "concise", "detailed"]
                    random_variant = random.choice(variants)
                    
                    token = akeneo_get_token()
                    new_result = process_product_from_akeneo(
                        result['sku'],
                        client,
                        token,
                        channel,
                        locale,
                        random_variant
                    )
                    
                    if not new_result['error']:
                        st.session_state.generated_description = new_result
                        st.success(f"✅ Przeredagowano! (wariant: {random_variant})")
                        st.rerun()
                    else:
                        st.error(f"❌ {new_result['error']}")
        
        with col_act2:
            if st.button("✅ Zaktualizuj w PIM", type="primary", use_container_width=True):
                try:
                    with st.spinner("Aktualizuję w Akeneo..."):
                        akeneo_update_description(
                            result['sku'],
                            result['description_html'],
                            channel,
                            locale
                        )
                        st.success(f"✅ Zaktualizowano produkt: {result['sku']}")
                        st.balloons()
                except Exception as e:
                    st.error(f"❌ Błąd: {str(e)}")

# ═══════════════════════════════════════════════════════════════════
# TAB 2: TRYB ZBIORCZY
# ═══════════════════════════════════════════════════════════════════

with tab2:
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
                for sku, prod_data in list(st.session_state.bulk_selected_products.items()):
                    col_info, col_remove = st.columns([5, 1])
                    with col_info:
                        status = "🟢" if prod_data.get('enabled', False) else "🔴"
                        st.write(f"{status} **{sku}** - {format_product_title(prod_data.get('title', sku))}")
                    with col_remove:
                        if st.button("🗑️", key=f"remove_{sku}"):
                            del st.session_state.bulk_selected_products[sku]
                            st.rerun()
                
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
                value=50,
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
                    # NIE resetujemy bulk_selected_products!
                    
                    if results:
                        st.success(f"✅ Znaleziono {len(results)} produktów")
                    else:
                        st.warning("⚠️ Nie znaleziono produktów")
        
        # LISTA PRODUKTÓW DO ZAZNACZENIA
        if 'bulk_search_results' in st.session_state and st.session_state.bulk_search_results:
            st.markdown("---")
            st.subheader("Zaznacz produkty z wyników wyszukiwania:")
            
            # Select All / Deselect All dla aktualnych wyników
            col_all1, col_all2, col_all3 = st.columns([1, 1, 4])
            with col_all1:
                if st.button("✅ Zaznacz widoczne", use_container_width=True, help="Zaznacz wszystkie produkty z obecnych wyników"):
                    for prod in st.session_state.bulk_search_results:
                        st.session_state.bulk_selected_products[prod['identifier']] = {
                            'title': prod['title'],
                            'enabled': prod['enabled'],
                            'family': prod['family']
                        }
                    st.rerun()
            with col_all2:
                if st.button("❌ Odznacz widoczne", use_container_width=True, help="Odznacz wszystkie z obecnych wyników"):
                    for prod in st.session_state.bulk_search_results:
                        if prod['identifier'] in st.session_state.bulk_selected_products:
                            del st.session_state.bulk_selected_products[prod['identifier']]
                    st.rerun()
            
            st.markdown("---")
            
            # Lista z checkboxami - NOWA LOGIKA
            for prod in st.session_state.bulk_search_results:
                col_check, col_info = st.columns([1, 6])
                
                sku = prod['identifier']
                is_selected = sku in st.session_state.bulk_selected_products
                
                with col_check:
                    checkbox_key = f"check_{sku}_{bulk_search}"  # Unikalny key z frazą wyszukiwania
                    checked = st.checkbox("", value=is_selected, key=checkbox_key, label_visibility="collapsed")
                    
                    # Aktualizacja stanu
                    if checked and not is_selected:
                        # Dodaj do wybranych
                        st.session_state.bulk_selected_products[sku] = {
                            'title': prod['title'],
                            'enabled': prod['enabled'],
                            'family': prod['family']
                        }
                        st.rerun()
                    elif not checked and is_selected:
                        # Usuń z wybranych
                        del st.session_state.bulk_selected_products[sku]
                        st.rerun()
                
                with col_info:
                    status = "🟢" if prod['enabled'] else "🔴"
                    already_selected = " ✓ (w koszyku)" if is_selected else ""
                    st.write(f"{status} **{sku}** - {format_product_title(prod['title'])}{already_selected}")
    
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
                
                # Pobierz info o produktach i dodaj do wybranych
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
                            # Jeśli nie można pobrać, dodaj z podstawowymi danymi
                            st.session_state.bulk_selected_products[sku] = {
                                'title': sku,
                                'enabled': True,
                                'family': ''
                            }
                
                st.success(f"✅ Załadowano {len(skus)} produktów do koszyka")
                st.rerun()
        
        # Pokaż załadowane
        if st.session_state.bulk_selected_products:
            st.markdown("---")
            st.subheader(f"📋 Załadowane produkty ({len(st.session_state.bulk_selected_products)})")
            
            for sku, prod_data in list(st.session_state.bulk_selected_products.items()):
                col_info, col_remove = st.columns([5, 1])
                with col_info:
                    status = "🟢" if prod_data.get('enabled', False) else "🔴"
                    st.write(f"{status} **{sku}** - {format_product_title(prod_data.get('title', sku))}")
                with col_remove:
                    if st.button("🗑️", key=f"remove_list_{sku}"):
                        del st.session_state.bulk_selected_products[sku]
                        st.rerun()
            
            if st.button("🗑️ Wyczyść wszystkie", use_container_width=True):
                st.session_state.bulk_selected_products = {}
                st.rerun()
    
    # GENEROWANIE ZBIORCZE
    if st.session_state.bulk_selected_products:
        st.markdown("---")
        st.markdown("---")
        st.subheader("🚀 Generowanie opisów")
        
        col_count, col_variant = st.columns([1, 2])
        
        with col_count:
            st.metric("Produkty do przetworzenia", len(st.session_state.bulk_selected_products))
        
        with col_variant:
            bulk_style = st.selectbox(
                "Wariant stylistyczny:",
                ["default", "alternative", "concise", "detailed"],
                index=0,
                key="bulk_style"
            )
        
        col_gen, col_clear = st.columns([1, 1])
        
        with col_gen:
            if st.button("🚀 Rozpocznij generowanie zbiorcze", type="primary", use_container_width=True):
                st.session_state.bulk_results = []
                
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
                            bulk_style
                        ): sku for sku in skus
                    }
                    
                    results_temp = []
                    for i, future in enumerate(as_completed(futures)):
                        result = future.result()
                        results_temp.append(result)
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
                st.rerun()
    
    # WYNIKI ZBIORCZE
    if st.session_state.bulk_results:
        st.markdown("---")
        st.subheader("📊 Wyniki")
        
        results = st.session_state.bulk_results
        successful = [r for r in results if not r['error']]
        errors = [r for r in results if r['error']]
        
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Wszystkie", len(results))
        col_m2.metric("Sukces", len(successful), delta=f"+{len(successful)}")
        col_m3.metric("Błędy", len(errors), delta=f"-{len(errors)}" if errors else "0")
        
        # CSV Export
        df = pd.DataFrame(results)
        st.download_button(
            "📥 Pobierz CSV",
            df.to_csv(index=False).encode('utf-8'),
            'opisy_zbiorcze.csv',
            'text/csv',
            use_container_width=True
        )
        
        # Wysyłka do PIM
        if successful:
            st.markdown("---")
            if st.button("✅ Wyślij wszystkie pomyślne do PIM", type="primary", use_container_width=True):
                success_count = 0
                error_count = 0
                error_msgs = []
                
                progress_pim = st.progress(0, text="Wysyłam do PIM...")
                
                for i, result in enumerate(successful):
                    try:
                        akeneo_update_description(
                            result['sku'],
                            result['description_html'],
                            channel,
                            locale
                        )
                        success_count += 1
                    except Exception as e:
                        error_count += 1
                        error_msgs.append(f"{result['sku']}: {str(e)}")
                    
                    progress_pim.progress((i + 1) / len(successful))
                
                st.success(f"✅ Zaktualizowano {success_count} produktów")
                
                if error_count > 0:
                    st.error(f"❌ Błędy: {error_count}")
                    for msg in error_msgs:
                        st.text(msg)
        
        # Szczegóły wyników
        st.markdown("---")
        st.subheader("Szczegóły")
        
        for idx, result in enumerate(results):
            if result['error']:
                with st.expander(f"❌ {result['sku']}", expanded=False):
                    st.error(result['error'])
            else:
                with st.expander(f"✅ {result['sku']} - {format_product_title(result['title'])}"):
                    # Przycisk regeneracji dla tego konkretnego produktu
                    col_regen_info, col_regen_btn = st.columns([3, 1])
                    with col_regen_info:
                        st.info(f"💡 Nie podoba Ci się ten opis? Wygeneruj nowy tylko dla tego produktu")
                    with col_regen_btn:
                        if st.button("♻️ Przeredaguj ten", key=f"regen_bulk_{result['sku']}_{idx}", use_container_width=True):
                            with st.spinner(f"Przeredagowuję {result['sku']}..."):
                                import random
                                variants = ["default", "alternative", "concise", "detailed"]
                                random_variant = random.choice(variants)
                                
                                token = akeneo_get_token()
                                new_result = process_product_from_akeneo(
                                    result['sku'],
                                    client,
                                    token,
                                    channel,
                                    locale,
                                    random_variant
                                )
                                
                                if not new_result['error']:
                                    # Aktualizuj wynik w liście
                                    st.session_state.bulk_results[idx] = new_result
                                    st.success(f"✅ Przeredagowano! (wariant: {random_variant})")
                                    st.rerun()
                                else:
                                    st.error(f"❌ {new_result['error']}")
                    
                    st.markdown("---")
                    
                    # Tabs z kodem i porównaniem
                    tab_c, tab_p = st.tabs(["💻 Kod HTML", "👁️ Porównanie"])
                    
                    with tab_c:
                        st.code(result['description_html'], language='html')
                        st.caption(f"📏 Długość: {len(result['description_html'])} znaków")
                    
                    with tab_p:
                        if result.get('old_description'):
                            col_old, col_new = st.columns(2)
                            with col_old:
                                st.markdown("**🕰️ Stary opis (Akeneo)**")
                                st.caption(f"📏 {len(result['old_description'])} znaków")
                                st.markdown("---")
                                old_desc_display = result['old_description'][:600] + "..." if len(result['old_description']) > 600 else result['old_description']
                                st.markdown(old_desc_display, unsafe_allow_html=True)
                            with col_new:
                                st.markdown("**✨ Nowy opis (AI)**")
                                st.caption(f"📏 {len(result['description_html'])} znaków")
                                st.markdown("---")
                                st.markdown(result['description_html'], unsafe_allow_html=True)
                        else:
                            st.info("Brak starego opisu w Akeneo")
                            st.markdown(result['description_html'], unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════════════

st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; padding: 20px;'>
    <p><strong>Generator Opisów Produktów v2.0</strong></p>
    <p>Powered by OpenAI GPT-5-nano | Akeneo PIM Integration</p>
</div>
""", unsafe_allow_html=True)
