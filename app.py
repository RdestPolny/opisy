import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
from openai import OpenAI
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════
# KONFIGURACJA STRONY
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Generator Opisów Produktów v2.0",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

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
    text = text.replace('—', '-')  # Em dash
    text = text.replace('–', '-')  # En dash
    text = text.replace('…', '...')  # Wielokropek
    return text

def format_product_title(title: str, max_length: int = 60) -> str:
    """Formatuje tytuł produktu dla lepszej czytelności"""
    if len(title) > max_length:
        return title[:max_length-3] + "..."
    return title

# ═══════════════════════════════════════════════════════════════════
# AKENEO API - ROZSZERZONE FUNKCJE
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
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    r.raise_for_status()

def akeneo_search_products(search_query: str, token: str, limit: int = 20) -> List[Dict]:
    """
    Wyszukuje produkty w Akeneo po nazwie/tytule
    
    Args:
        search_query: Fraza do wyszukania
        token: Access token Akeneo
        limit: Maksymalna liczba wyników
        
    Returns:
        Lista produktów z podstawowymi danymi
    """
    url = _akeneo_root() + "/api/rest/v1/products"
    headers = {"Authorization": f"Bearer {token}"}
    
    # Szukamy w różnych możliwych atrybutach nazwy
    # Dostosuj do swoich atrybutów (name, title, product_name, etc.)
    params = {
        "limit": limit,
        "search": json.dumps({
            "identifier": [{"operator": "CONTAINS", "value": search_query}]
        })
    }
    
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        
        products = []
        for item in data.get("_embedded", {}).get("items", []):
            # Próbujemy znaleźć tytuł produktu
            title = item.get("identifier", "")
            
            # Szukamy w values różnych możliwych atrybutów z nazwą
            values = item.get("values", {})
            for attr_name in ["name", "title", "product_name", "nazwa"]:
                if attr_name in values:
                    attr_values = values[attr_name]
                    if attr_values and len(attr_values) > 0:
                        title = attr_values[0].get("data", title)
                        break
            
            products.append({
                "identifier": item.get("identifier", ""),
                "title": title,
                "family": item.get("family", ""),
                "enabled": item.get("enabled", False),
                "raw_data": item
            })
        
        return products
    except Exception as e:
        st.error(f"Błąd wyszukiwania w Akeneo: {str(e)}")
        return []

def akeneo_get_product_details(sku: str, token: str, channel: str = "Bookland", locale: str = "pl_PL") -> Optional[Dict]:
    """
    Pobiera pełne dane produktu z Akeneo
    
    Args:
        sku: Identyfikator produktu
        token: Access token
        channel: Kanał (scope)
        locale: Locale
        
    Returns:
        Dict z danymi produktu lub None
    """
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        product = r.json()
        
        # Ekstrakcja wartości atrybutów
        values = product.get("values", {})
        
        def get_value(attr_name: str) -> str:
            """Pomocnicza funkcja do ekstrakcji wartości atrybutu"""
            if attr_name not in values:
                return ""
            
            attr_values = values[attr_name]
            if not attr_values:
                return ""
            
            # Szukamy wartości dla odpowiedniego scope i locale
            for val in attr_values:
                val_scope = val.get("scope")
                val_locale = val.get("locale")
                
                # Jeśli attr nie jest scopable/localizable, może mieć None
                if (val_scope is None or val_scope == channel) and \
                   (val_locale is None or val_locale == locale):
                    return val.get("data", "")
            
            # Fallback - pierwsza wartość
            return attr_values[0].get("data", "")
        
        # Budujemy strukturę z danymi
        product_data = {
            "identifier": product.get("identifier", ""),
            "family": product.get("family", ""),
            "enabled": product.get("enabled", False),
            "title": get_value("name") or get_value("title") or get_value("product_name") or product.get("identifier", ""),
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
        raise ValueError(f"Produkt o SKU '{sku}' nie istnieje w Akeneo.")
    
    # Sprawdź konfigurację atrybutu description
    attr_desc = akeneo_get_attribute("description", token)
    is_scopable_desc = bool(attr_desc.get("scopable", False))
    is_localizable_desc = bool(attr_desc.get("localizable", False))
    
    value_obj_desc = {
        "data": html_description,
        "scope": channel if is_scopable_desc else None,
        "locale": locale if is_localizable_desc else None,
    }
    
    payload_values = {"description": [value_obj_desc]}

    # Spróbuj zaktualizować opisy_seo jeśli istnieje
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
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            st.warning("⚠️ Atrybut 'opisy_seo' nie istnieje. Aktualizuję tylko opis główny.")
        else:
            raise e

    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    payload = {"values": payload_values}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    
    r = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=30)
    
    if r.status_code in (200, 204):
        return True
    try:
        detail = r.json()
    except Exception:
        detail = r.text
    raise RuntimeError(f"Akeneo zwróciło {r.status_code}: {detail}")

# ═══════════════════════════════════════════════════════════════════
# WEB SCRAPING
# ═══════════════════════════════════════════════════════════════════

def get_book_data(url: str) -> Dict:
    """
    Pobiera dane produktu ze strony zewnętrznej (scraping)
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = bs(response.text, 'html.parser')

        title_tag = soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else ''

        details_text = ""
        description_text = ""

        # Parsowanie dla smyk.com
        if 'smyk.com' in url:
            smyk_desc_div = soup.find("div", attrs={"data-testid": "box-attributes__simple"})
            if smyk_desc_div:
                for p_tag in smyk_desc_div.find_all("p"):
                    if p_tag.find("span", string=re.compile(r"Nr produktu:")):
                        p_tag.decompose()
                description_text = smyk_desc_div.get_text(separator="\n", strip=True)

            smyk_attributes_div = soup.find("div", class_="box-attributes__not-simple")
            if smyk_attributes_div:
                attributes_list = []
                items = smyk_attributes_div.find_all("div", class_="box_attributes__spec-item")
                for item in items:
                    label_tag = item.find("span", class_="box-attributes-list__label--L")
                    value_tag = item.find("span", class_="box-attributes-list__atribute--L")
                    if label_tag and value_tag:
                        label = label_tag.get_text(strip=True)
                        value = value_tag.get_text(strip=True)
                        if label and value:
                            attributes_list.append(f"{label}: {value}")
                
                if attributes_list:
                    details_text = "\n".join(attributes_list)
        
        # Parsowanie ogólne
        if not description_text:
            details_div = soup.find("div", id="szczegoly") or soup.find("div", class_="product-features")
            if details_div:
                ul = details_div.find("ul", class_="bullet") or details_div.find("ul")
                if ul:
                    li_elements = ul.find_all("li")
                    details_list = [li.get_text(separator=" ", strip=True) for li in li_elements]
                    details_text = "\n".join(details_list)
            
            description_div = soup.find("div", class_="desc-container")
            if description_div:
                article = description_div.find("article")
                if article:
                    nested_article = article.find("article")
                    if nested_article:
                        description_text = nested_article.get_text(separator="\n", strip=True)
                    else:
                        description_text = article.get_text(separator="\n", strip=True)
                else:
                    description_text = description_div.get_text(separator="\n", strip=True)

        if not description_text:
            alt_desc_div = soup.find("div", id="product-description")
            if alt_desc_div:
                description_text = alt_desc_div.get_text(separator="\n", strip=True)

        description_text = " ".join(description_text.split())

        if not description_text and not details_text:
            return {
                'title': title,
                'details': '',
                'description': '',
                'error': "Nie udało się pobrać opisu ani szczegółów produktu."
            }
            
        return {
            'title': title,
            'details': details_text,
            'description': description_text,
            'error': None
        }
        
    except Exception as e:
        return {
            'title': '',
            'details': '',
            'description': '',
            'error': f"Błąd pobierania: {str(e)}"
        }

# ═══════════════════════════════════════════════════════════════════
# GENEROWANIE OPISÓW - GPT-5-NANO
# ═══════════════════════════════════════════════════════════════════

def generate_description(product_data: Dict, client: OpenAI, style_variant: str = "default") -> str:
    """
    Generuje opis produktu z wykorzystaniem GPT-5-nano
    
    Args:
        product_data: Słownik z danymi produktu (title, details, description)
        client: Klient OpenAI
        style_variant: Wariant stylistyczny
        
    Returns:
        HTML opis produktu
    """
    try:
        system_prompt = """Jesteś EKSPERTEM copywritingu e-commerce i SEO. Twoje opisy są angażujące, semantycznie zoptymalizowane i konwertują odwiedzających w kupujących.

╔═══════════════════════════════════════════════════════════════════╗
║  KROK 1: WEWNĘTRZNA ANALIZA PRODUKTU (NIE WYPISUJ TEJ CZĘŚCI)     ║
╚═══════════════════════════════════════════════════════════════════╝

Przeanalizuj dane produktu i zidentyfikuj:
- Typ produktu (książka/gra/zabawka/edukacja)
- Grupę docelową (dzieci/młodzież/dorośli/profesjonaliści)
- Kluczowe korzyści i USP (Unique Selling Points)
- Główne słowa kluczowe SEO do wplecenia naturalnie

╔═══════════════════════════════════════════════════════════════════╗
║  KROK 2: GENEROWANIE OPISU - STRUKTURA HTML                       ║
╚═══════════════════════════════════════════════════════════════════╝

OBOWIĄZKOWA STRUKTURA (bez odstępstw!):

1. <h2>[Chwytliwy nagłówek z głównym słowem kluczowym]</h2>
   
2. <p>[Akapit wprowadzający - emocjonalne otwarcie, 2-3 zdania, BEZ danych technicznych]</p>

3. <h2>[Nagłówek sekcji głównej]</h2>

4. <p>[Główna treść z korzyściami i funkcjami - 3-4 zdania]</p>

5. <h2>[Nagłówek drugiej sekcji]</h2>

6. <p>[Rozwinięcie, szczegóły - 3-4 zdania, TUTAJ wpleć dane techniczne naturalnie]</p>

7. <h3>[Wezwanie do działania / Podsumowanie]</h3>

8. <p>[Zachęta do zakupu - 1-2 zdania]</p>

╔═══════════════════════════════════════════════════════════════════╗
║  KRYTYCZNE ZASADY (BEZWZGLĘDNIE PRZESTRZEGAJ!)                    ║
╚═══════════════════════════════════════════════════════════════════╝

1. JĘZYK I INTERPUNKCJA:
   ✅ ZAWSZE używaj standardowego myślnika "-" (minus)
   ❌ NIGDY nie używaj em dash "—" ani en dash "–"
   ❌ NIGDY nie używaj wielokropka "…" - używaj trzech kropek "..."
   ✅ Nienaganna polska gramatyka i ortografia

2. BOLDOWANIE (KLUCZOWE!):
   ✅ Pogrubiaj 6-10 kluczowych fraz w całym opisie
   ✅ Bold: pojedyncze słowa LUB frazy 2-4 słowa
   ✅ Bold: nazwy produktów, kategorie, korzyści
   ✅ Przykłady: <b>książka edukacyjna</b>, <b>rozwój dziecka</b>, <b>ilustracje</b>
   ❌ NIGDY nie pogrubiaj całych zdań ani fraz dłuższych niż 4 słowa
   ✅ Rozmieść bold równomiernie przez cały opis

3. NAGŁÓWKI:
   ✅ H2 na początku ZAWSZE - chwytliwy, z głównym słowem kluczowym
   ✅ Minimum 2x <h2> i 1x <h3> w opisie
   ✅ Nagłówki konkretne, opisowe (nie ogólne jak "O produkcie")
   ✅ Przykłady dobrych H2: "Fascynująca przygoda w krainie fantasy", "Edukacyjna zabawa dla małych odkrywców"

4. TREŚĆ I STRUKTURA:
   ✅ Dane techniczne (wymiary, rok, strony) TYLKO w środkowej/dolnej części opisu
   ✅ NIGDY nie powtarzaj tych samych informacji (sprawdź przed wysłaniem!)
   ✅ Opis marketingowy - emocje, korzyści, storytelling
   ✅ Wpleć dane techniczne NATURALNIE w zdania
   ❌ NIGDY nie twórz list punktowanych z danymi technicznymi
   ❌ NIGDY nie powtarzaj ISBN, EAN, kodów produktu w treści

5. OPTYMALIZACJA SEO (SEMANTYCZNA):
   ✅ Używaj synonimów i powiązanych fraz
   ✅ Naturalne wplecenie słów kluczowych (bez keyword stuffing)
   ✅ Długie frazy (long-tail keywords) w naturalnym kontekście
   ✅ Pytania, które mogą zadawać klienci

6. DŁUGOŚĆ:
   ✅ 1500-2500 znaków (ze spacjami)
   ✅ Akapity po 3-4 zdania (nie dłuższe!)

7. TON I STYL - dostosuj automatycznie:
   - Kryminał: napięcie, tajemnica, intrygujące pytania
   - Fantasy: magiczny świat, epicka przygoda
   - Edukacja: korzyści rozwojowe, bezpieczeństwo, radość nauki
   - Gra planszowa: emocje, interakcja, zasady w akcji

╔═══════════════════════════════════════════════════════════════════╗
║  ANTI-PRZYKŁADY (CZEGO NIE ROBIĆ!)                                ║
╚═══════════════════════════════════════════════════════════════════╝

❌ ZŁE: "Książka ma wymiary 20x15 cm i 320 stron. Wymiary: 20x15 cm."
✅ DOBRE: "Format książki (20x15 cm) idealnie pasuje do plecaka..."

❌ ZŁE: "Produkt o <b>wysokiej jakości wykonania oraz doskonałej...</b>" (całe zdanie)
✅ DOBRE: "Produkt wyróżnia się <b>wysoką jakością</b> wykonania..."

❌ ZŁE: "To książka — idealna na prezent — dla każdego." (em dash)
✅ DOBRE: "To książka - idealna na prezent - dla każdego."

❌ ZŁE: Brak H2 na początku
✅ DOBRE: Zawsze H2 jako pierwszy element

╔═══════════════════════════════════════════════════════════════════╗
║  TWOJA ODPOWIEDŹ                                                   ║
╚═══════════════════════════════════════════════════════════════════╝

Zwróć TYLKO czysty kod HTML (bez ```html, bez komentarzy).
Zacznij od <h2>, zakończ na </p>.
Sprawdź PRZED wysłaniem: brak powtórzeń, brak em dash, odpowiednia liczba bold, H2 na początku.
"""

        # Warianty stylistyczne
        style_additions = {
            "alternative": "\n\nUżyj ALTERNATYWNEGO PODEJŚCIA: bardziej bezpośredniego tonu, krótszych zdań i mocniejszych CTA.",
            "concise": "\n\nUżyj ZWIĘZŁEGO STYLU: krótkie zdania, maksimum informacji, minimum ozdobników. Celuj w 1500-1800 znaków.",
            "detailed": "\n\nUżyj SZCZEGÓŁOWEGO STYLU: rozbudowane opisy, więcej kontekstu, storytelling. Celuj w 2200-2500 znaków."
        }
        
        if style_variant != "default" and style_variant in style_additions:
            system_prompt += style_additions[style_variant]

        raw_data_context = f"""
╔═══════════════════════════════════════════════════════════════════╗
║  DANE PRODUKTU DO PRZEANALIZOWANIA                                ║
╚═══════════════════════════════════════════════════════════════════╝

TYTUŁ PRODUKTU:
{product_data.get('title', '')}

SZCZEGÓŁY TECHNICZNE (do wplecenia naturalnie w środkowej części opisu):
{product_data.get('details', '')}

ORYGINALNY OPIS (główne źródło treści marketingowej):
{product_data.get('description', '')}

PAMIĘTAJ:
- Przeanalizuj produkt i dostosuj ton
- Zacznij od <h2>
- 6-10 wyrazów/fraz bold (2-4 słowa)
- Dane techniczne w środku/na końcu, wplecione naturalnie
- NIE powtarzaj informacji
- TYLKO myślniki "-", NIGDY em dash "—"
"""
        full_input = f"{system_prompt}\n\n{raw_data_context}"

        response = client.responses.create(
            model="gpt-5-nano",
            input=full_input,
            reasoning={"effort": "high"},
            text={"verbosity": "medium"}
        )
        
        result = strip_code_fences(response.output_text)
        result = clean_ai_fingerprints(result)
        
        return result
        
    except Exception as e:
        return f"BŁĄD GENEROWANIA: {str(e)}"

def generate_meta_tags(product_data: Dict, client: OpenAI) -> Tuple[str, str]:
    """Generuje meta title i meta description"""
    try:
        title = product_data.get('title', '')
        details = product_data.get('details', '')
        description = product_data.get('description', '')
        
        system_prompt = """Jesteś ekspertem SEO i copywriterem metatagów.

KRYTYCZNE ZASADY:

Meta Title:
- Maksymalnie 60 znaków
- Rozpocznij od głównego słowa kluczowego
- NIE dodawaj nazwy sklepu
- Używaj TYLKO standardowego myślnika "-"
- NIGDY nie używaj em dash "—" ani en dash "–"
- NIE używaj kropek na końcu

Meta Description:
- Maksymalnie 160 znaków
- Jedno lub dwa konkretne zdania
- Zawiera wezwanie do działania
- Używaj TYLKO standardowego myślnika "-"

FORMAT ODPOWIEDZI (DOKŁADNIE):
Meta title: [treść]
Meta description: [treść]
"""
        
        user_prompt = f"""Produkt: {title}
Dane: {details} {description}

Wygeneruj metatagi SEO."""

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
        
        # Post-processing
        meta_title = clean_ai_fingerprints(meta_title).rstrip('.')
        meta_description = clean_ai_fingerprints(meta_description)
        
        # Walidacja długości
        if len(meta_title) > 60:
            meta_title = meta_title[:57] + "..."
        if len(meta_description) > 160:
            meta_description = meta_description[:157] + "..."
            
        return meta_title, meta_description
        
    except Exception as e:
        st.error(f"Błąd generowania metatagów: {str(e)}")
        return "", ""

# ═══════════════════════════════════════════════════════════════════
# PRZETWARZANIE WSADOWE
# ═══════════════════════════════════════════════════════════════════

def process_single_url(url: str, sku: str, client: OpenAI) -> Dict:
    """Przetwarza pojedynczy URL w trybie wsadowym"""
    try:
        product_data = get_book_data(url)
        if product_data['error']:
            return {
                'url': url,
                'sku': sku,
                'title': product_data.get('title', ''),
                'description_html': '',
                'error': product_data['error']
            }
            
        description_html = generate_description(product_data, client)
        
        if "BŁĄD GENEROWANIA:" in description_html:
            return {
                'url': url,
                'sku': sku,
                'title': product_data.get('title', ''),
                'description_html': '',
                'error': description_html
            }
            
        return {
            'url': url,
            'sku': sku,
            'title': product_data.get('title', ''),
            'description_html': description_html,
            'error': None
        }
        
    except Exception as e:
        return {
            'url': url,
            'sku': sku,
            'title': '',
            'description_html': '',
            'error': f"Nieoczekiwany błąd: {str(e)}"
        }

# ═══════════════════════════════════════════════════════════════════
# INICJALIZACJA I WALIDACJA
# ═══════════════════════════════════════════════════════════════════

# Session state
if 'show_preview' not in st.session_state:
    st.session_state.show_preview = False
if 'batch_results' not in st.session_state:
    st.session_state.batch_results = []
if 'regeneration_count' not in st.session_state:
    st.session_state.regeneration_count = 0
if 'akeneo_search_results' not in st.session_state:
    st.session_state.akeneo_search_results = []
if 'selected_product_from_akeneo' not in st.session_state:
    st.session_state.selected_product_from_akeneo = None

# Walidacja API keys
if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak klucza API OpenAI w secrets. Skonfiguruj OPENAI_API_KEY.")
    st.stop()

required_akeneo_secrets = [
    "AKENEO_BASE_URL",
    "AKENEO_CLIENT_ID",
    "AKENEO_SECRET",
    "AKENEO_USERNAME",
    "AKENEO_PASSWORD"
]
missing = [k for k in required_akeneo_secrets if k not in st.secrets]
if missing:
    st.warning(f"⚠️ Brak konfiguracji Akeneo: {', '.join(missing)}. Funkcje PIM będą niedostępne.")

client = OpenAI()

# ═══════════════════════════════════════════════════════════════════
# INTERFEJS UŻYTKOWNIKA
# ═══════════════════════════════════════════════════════════════════

st.title('📚 Generator Opisów Produktów v2.0')
st.caption("✨ Nowy prompt z optymalizacją SEO | 🔍 Nowy tryb: Wyszukiwanie w Akeneo PIM")

# ─────────────────────────────────────────────────────────────────
# SIDEBAR - USTAWIENIA
# ─────────────────────────────────────────────────────────────────

st.sidebar.header("🎯 Ustawienia PIM")
channel = st.sidebar.selectbox(
    "Kanał (scope):",
    ["Bookland", "B2B"],
    index=0,
    key="channel_global"
)
locale = st.sidebar.text_input(
    "Locale:",
    value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL"),
    key="locale_global"
)

st.sidebar.markdown("---")
st.sidebar.header("ℹ️ Informacje")
st.sidebar.info("""
**Dostępne tryby:**
1. 🌐 Ze strony zewnętrznej
2. 🔍 Wyszukaj w Akeneo
3. 🗂️ Przetwarzanie wsadowe

**Warianty stylistyczne:**
- default: standardowy
- alternative: bezpośredni
- concise: zwięzły
- detailed: szczegółowy
""")

# ─────────────────────────────────────────────────────────────────
# ZAKŁADKI GŁÓWNE
# ─────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs([
    "🌐 Ze strony zewnętrznej",
    "🔍 Wyszukaj w Akeneo",
    "🗂️ Przetwarzanie wsadowe"
])

# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 1: ZE STRONY ZEWNĘTRZNEJ (URL)
# ═══════════════════════════════════════════════════════════════════

with tab1:
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.header("📝 Dane wejściowe")
        
        url_single = st.text_input(
            "URL strony produktu:",
            placeholder="https://przyklad.com/produkt",
            key="url_single"
        )
        
        sku_single = st.text_input(
            "SKU w PIM (identifier):",
            placeholder="np. BL-12345",
            key="sku_single"
        )
        
        style_variant = st.selectbox(
            "Wariant stylistyczny:",
            ["default", "alternative", "concise", "detailed"],
            index=0,
            help="Wybierz styl opisu",
            key="style_variant_url"
        )
        
        generate_meta = st.checkbox(
            "Generuj meta title i meta description",
            value=False,
            key="meta_single_url"
        )
        
        col_btn1, col_btn2 = st.columns([1, 1])
        
        with col_btn1:
            generate_button = st.button(
                "🚀 Generuj opis",
                type="primary",
                use_container_width=True,
                key="gen_single_url"
            )
        
        with col_btn2:
            if st.button("🔄 Wyczyść", use_container_width=True, key="clear_single_url"):
                keys_to_clear = [
                    'generated_description',
                    'book_title',
                    'meta_title',
                    'meta_description',
                    'show_preview',
                    'book_data_cached',
                    'regeneration_count'
                ]
                for key in keys_to_clear:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()

        if generate_button:
            if not url_single:
                st.error("❌ Podaj URL strony produktu!")
            else:
                with st.spinner("Pobieram dane ze strony..."):
                    book_data = get_book_data(url_single)
                
                if book_data['error']:
                    st.error(f"❌ {book_data['error']}")
                else:
                    st.success("✅ Dane pobrane pomyślnie!")
                    st.session_state['book_data_cached'] = book_data
                    st.session_state.regeneration_count = 0
                    
                    with st.spinner("Generuję opis..."):
                        generated_desc = generate_description(book_data, client, style_variant)
                        
                        if "BŁĄD GENEROWANIA:" not in generated_desc:
                            st.session_state['generated_description'] = generated_desc
                            st.session_state['book_title'] = book_data['title']
                            st.session_state.show_preview = False
                            
                            if generate_meta:
                                with st.spinner("Generuję metatagi..."):
                                    meta_title, meta_description = generate_meta_tags(book_data, client)
                                    st.session_state['meta_title'] = meta_title
                                    st.session_state['meta_description'] = meta_description

                            st.success("✅ Opis wygenerowany!")
                        else:
                            st.error(f"❌ {generated_desc}")

    with col2:
        st.header("📄 Wygenerowany opis")
        
        if 'generated_description' in st.session_state:
            st.subheader(f"📖 {st.session_state.get('book_title', 'Opis produktu')}")
            
            if st.session_state.regeneration_count > 0:
                st.caption(f"♻️ Regenerowano: {st.session_state.regeneration_count} raz(y)")
            
            st.code(st.session_state['generated_description'], language='html')

            col_preview, col_regen = st.columns([1, 1])
            
            with col_preview:
                if st.button(
                    "👁️ Pokaż/Ukryj podgląd",
                    use_container_width=True,
                    key="preview_single_url"
                ):
                    st.session_state.show_preview = not st.session_state.show_preview
            
            with col_regen:
                if st.button(
                    "♻️ Przeredaguj",
                    use_container_width=True,
                    help="Wygeneruj nową wersję",
                    key="regen_single_url"
                ):
                    if 'book_data_cached' in st.session_state:
                        with st.spinner("Przeredagowuję..."):
                            import random
                            variants = ["default", "alternative", "concise", "detailed"]
                            random_variant = random.choice(variants)
                            
                            generated_desc = generate_description(
                                st.session_state['book_data_cached'],
                                client,
                                random_variant
                            )
                            
                            if "BŁĄD GENEROWANIA:" not in generated_desc:
                                st.session_state['generated_description'] = generated_desc
                                st.session_state.regeneration_count += 1
                                st.success(f"✅ Przeredagowano! (wariant: {random_variant})")
                                st.rerun()
                            else:
                                st.error(f"❌ {generated_desc}")
                    else:
                        st.warning("⚠️ Brak danych do regeneracji.")
            
            if st.session_state.show_preview:
                st.markdown("**Podgląd:**")
                st.markdown(st.session_state['generated_description'], unsafe_allow_html=True)

            if 'meta_title' in st.session_state:
                st.markdown("---")
                st.subheader("🏷️ Metatagi SEO")
                
                title_len = len(st.session_state['meta_title'])
                desc_len = len(st.session_state['meta_description'])
                
                title_color = "green" if title_len <= 60 else "red"
                desc_color = "green" if desc_len <= 160 else "red"
                
                st.markdown(f"**Meta Title** (:{title_color}[{title_len}/60]): {st.session_state['meta_title']}")
                st.markdown(f"**Meta Description** (:{desc_color}[{desc_len}/160]): {st.session_state['meta_description']}")
            
            st.markdown("---")
            pim_disabled = len(missing) > 0
            
            if st.button(
                "✅ Zaakceptuj i wyślij do PIM",
                use_container_width=True,
                type="primary",
                disabled=pim_disabled,
                key="send_pim_single_url"
            ):
                if not sku_single:
                    st.error("❌ Podaj SKU przed wysyłką do PIM.")
                else:
                    try:
                        with st.spinner("Wysyłam do Akeneo..."):
                            ok = akeneo_update_description(
                                sku_single.strip(),
                                st.session_state['generated_description'],
                                channel,
                                locale.strip()
                            )
                            if ok:
                                st.success(f"✅ Opis zapisany dla SKU: {sku_single}")
                    except Exception as e:
                        st.error(f"❌ Błąd zapisu: {e}")
        else:
            st.info("👈 Podaj URL i kliknij 'Generuj opis'")

# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 2: WYSZUKIWANIE W AKENEO (NOWA!)
# ═══════════════════════════════════════════════════════════════════

with tab2:
    st.header("🔍 Wyszukaj produkt w Akeneo PIM")
    st.info("Wyszukaj produkt bezpośrednio w Akeneo, wybierz z listy i wygeneruj nowy opis na podstawie danych z PIM.")
    
    if len(missing) > 0:
        st.error(f"❌ Brak konfiguracji Akeneo. Skonfiguruj: {', '.join(missing)}")
    else:
        col_search, col_results = st.columns([1, 1])
        
        with col_search:
            st.subheader("🔎 Wyszukiwanie")
            
            search_query = st.text_input(
                "Wpisz nazwę, tytuł lub identyfikator produktu:",
                placeholder="np. Harry Potter",
                key="akeneo_search_query"
            )
            
            search_limit = st.slider(
                "Maksymalna liczba wyników:",
                min_value=5,
                max_value=50,
                value=20,
                key="akeneo_search_limit"
            )
            
            if st.button("🔍 Szukaj w Akeneo", type="primary", use_container_width=True, key="btn_search_akeneo"):
                if not search_query:
                    st.warning("⚠️ Wpisz frazę do wyszukania.")
                else:
                    with st.spinner(f"Wyszukuję '{search_query}' w Akeneo..."):
                        try:
                            token = akeneo_get_token()
                            results = akeneo_search_products(search_query, token, search_limit)
                            st.session_state.akeneo_search_results = results
                            
                            if results:
                                st.success(f"✅ Znaleziono {len(results)} produktów!")
                            else:
                                st.warning("⚠️ Nie znaleziono produktów pasujących do zapytania.")
                        except Exception as e:
                            st.error(f"❌ Błąd wyszukiwania: {str(e)}")
            
            if st.button("🗑️ Wyczyść wyniki", use_container_width=True, key="clear_akeneo_search"):
                st.session_state.akeneo_search_results = []
                st.session_state.selected_product_from_akeneo = None
                st.rerun()
        
        with col_results:
            st.subheader("📋 Wyniki wyszukiwania")
            
            if st.session_state.akeneo_search_results:
                st.write(f"Znaleziono **{len(st.session_state.akeneo_search_results)}** produktów:")
                
                # Tworzymy opcje selectbox
                product_options = {}
                for prod in st.session_state.akeneo_search_results:
                    display_name = f"{prod['identifier']} - {format_product_title(prod['title'])}"
                    if not prod['enabled']:
                        display_name += " [WYŁĄCZONY]"
                    product_options[display_name] = prod
                
                selected_display = st.selectbox(
                    "Wybierz produkt:",
                    options=list(product_options.keys()),
                    key="akeneo_product_selector"
                )
                
                if selected_display:
                    selected_product = product_options[selected_display]
                    st.session_state.selected_product_from_akeneo = selected_product
                    
                    # Wyświetlamy info o produkcie
                    st.markdown("---")
                    st.write(f"**Identyfikator:** {selected_product['identifier']}")
                    st.write(f"**Tytuł:** {selected_product['title']}")
                    st.write(f"**Rodzina:** {selected_product['family']}")
                    st.write(f"**Status:** {'✅ Aktywny' if selected_product['enabled'] else '❌ Wyłączony'}")
            else:
                st.info("👈 Wyszukaj produkt aby zobaczyć wyniki")
        
        # ─────────────────────────────────────────────────────────────
        # SEKCJA GENEROWANIA OPISU DLA WYBRANEGO PRODUKTU
        # ─────────────────────────────────────────────────────────────
        
        if st.session_state.selected_product_from_akeneo:
            st.markdown("---")
            st.header("✨ Generowanie nowego opisu")
            
            col_gen1, col_gen2 = st.columns([1, 1])
            
            with col_gen1:
                st.subheader("⚙️ Opcje generowania")
                
                selected_sku = st.session_state.selected_product_from_akeneo['identifier']
                st.text_input(
                    "SKU produktu:",
                    value=selected_sku,
                    disabled=True,
                    key="akeneo_selected_sku_display"
                )
                
                style_variant_akeneo = st.selectbox(
                    "Wariant stylistyczny:",
                    ["default", "alternative", "concise", "detailed"],
                    index=0,
                    key="style_variant_akeneo"
                )
                
                generate_meta_akeneo = st.checkbox(
                    "Generuj meta title i meta description",
                    value=False,
                    key="meta_akeneo"
                )
                
                if st.button(
                    "🚀 Pobierz dane i generuj opis",
                    type="primary",
                    use_container_width=True,
                    key="gen_from_akeneo"
                ):
                    with st.spinner("Pobieram pełne dane produktu z Akeneo..."):
                        try:
                            token = akeneo_get_token()
                            product_details = akeneo_get_product_details(
                                selected_sku,
                                token,
                                channel,
                                locale
                            )
                            
                            if not product_details:
                                st.error("❌ Nie znaleziono produktu w Akeneo.")
                            else:
                                st.success("✅ Dane produktu pobrane!")
                                
                                # Przygotowujemy dane do generowania
                                # Łączymy wszystkie dostępne informacje
                                details_parts = []
                                
                                if product_details.get('author'):
                                    details_parts.append(f"Autor: {product_details['author']}")
                                if product_details.get('publisher'):
                                    details_parts.append(f"Wydawnictwo: {product_details['publisher']}")
                                if product_details.get('year'):
                                    details_parts.append(f"Rok wydania: {product_details['year']}")
                                if product_details.get('pages'):
                                    details_parts.append(f"Liczba stron: {product_details['pages']}")
                                if product_details.get('cover_type'):
                                    details_parts.append(f"Oprawa: {product_details['cover_type']}")
                                if product_details.get('dimensions'):
                                    details_parts.append(f"Wymiary: {product_details['dimensions']}")
                                if product_details.get('age'):
                                    details_parts.append(f"Wiek: {product_details['age']}")
                                if product_details.get('category'):
                                    details_parts.append(f"Kategoria: {product_details['category']}")
                                
                                product_data_for_gen = {
                                    'title': product_details['title'],
                                    'details': '\n'.join(details_parts),
                                    'description': product_details.get('description', '') or product_details.get('short_description', ''),
                                    'error': None
                                }
                                
                                # Cache'ujemy dane
                                st.session_state['akeneo_product_data_cached'] = product_data_for_gen
                                st.session_state['akeneo_product_details'] = product_details
                                st.session_state.regeneration_count_akeneo = 0
                                
                                with st.spinner("Generuję nowy opis..."):
                                    generated_desc = generate_description(
                                        product_data_for_gen,
                                        client,
                                        style_variant_akeneo
                                    )
                                    
                                    if "BŁĄD GENEROWANIA:" not in generated_desc:
                                        st.session_state['generated_description_akeneo'] = generated_desc
                                        st.session_state.show_preview_akeneo = False
                                        
                                        if generate_meta_akeneo:
                                            with st.spinner("Generuję metatagi..."):
                                                meta_title, meta_description = generate_meta_tags(
                                                    product_data_for_gen,
                                                    client
                                                )
                                                st.session_state['meta_title_akeneo'] = meta_title
                                                st.session_state['meta_description_akeneo'] = meta_description
                                        
                                        st.success("✅ Nowy opis wygenerowany!")
                                        st.rerun()
                                    else:
                                        st.error(f"❌ {generated_desc}")
                                        
                        except Exception as e:
                            st.error(f"❌ Błąd: {str(e)}")
            
            with col_gen2:
                st.subheader("📊 Obecne dane w Akeneo")
                
                if 'akeneo_product_details' in st.session_state:
                    details = st.session_state['akeneo_product_details']
                    
                    with st.expander("🔍 Zobacz szczegóły produktu", expanded=False):
                        if details.get('description'):
                            st.markdown("**Obecny opis:**")
                            st.markdown(details['description'][:500] + "..." if len(details.get('description', '')) > 500 else details.get('description', ''))
                        
                        if details.get('short_description'):
                            st.markdown("**Krótki opis:**")
                            st.write(details['short_description'])
                        
                        st.markdown("**Atrybuty:**")
                        attr_display = []
                        for key in ['author', 'publisher', 'year', 'pages', 'cover_type', 'dimensions', 'age', 'category', 'ean', 'isbn']:
                            if details.get(key):
                                attr_display.append(f"- **{key.title()}:** {details[key]}")
                        
                        if attr_display:
                            st.markdown('\n'.join(attr_display))
                else:
                    st.info("Kliknij 'Pobierz dane i generuj opis' aby zobaczyć obecne dane")
            
            # ─────────────────────────────────────────────────────────
            # WYŚWIETLANIE WYGENEROWANEGO OPISU
            # ─────────────────────────────────────────────────────────
            
            if 'generated_description_akeneo' in st.session_state:
                st.markdown("---")
                st.header("📄 Nowy wygenerowany opis")
                
                col_desc1, col_desc2 = st.columns([3, 2])
                
                with col_desc1:
                    if hasattr(st.session_state, 'regeneration_count_akeneo') and st.session_state.regeneration_count_akeneo > 0:
                        st.caption(f"♻️ Regenerowano: {st.session_state.regeneration_count_akeneo} raz(y)")
                    
                    st.code(st.session_state['generated_description_akeneo'], language='html')
                
                with col_desc2:
                    if st.button(
                        "👁️ Pokaż/Ukryj podgląd",
                        use_container_width=True,
                        key="preview_akeneo"
                    ):
                        st.session_state.show_preview_akeneo = not st.session_state.get('show_preview_akeneo', False)
                    
                    if st.button(
                        "♻️ Przeredaguj",
                        use_container_width=True,
                        key="regen_akeneo"
                    ):
                        if 'akeneo_product_data_cached' in st.session_state:
                            with st.spinner("Przeredagowuję..."):
                                import random
                                variants = ["default", "alternative", "concise", "detailed"]
                                random_variant = random.choice(variants)
                                
                                generated_desc = generate_description(
                                    st.session_state['akeneo_product_data_cached'],
                                    client,
                                    random_variant
                                )
                                
                                if "BŁĄD GENEROWANIA:" not in generated_desc:
                                    st.session_state['generated_description_akeneo'] = generated_desc
                                    st.session_state.regeneration_count_akeneo = st.session_state.get('regeneration_count_akeneo', 0) + 1
                                    st.success(f"✅ Przeredagowano! (wariant: {random_variant})")
                                    st.rerun()
                                else:
                                    st.error(f"❌ {generated_desc}")
                
                if st.session_state.get('show_preview_akeneo', False):
                    st.markdown("**Podgląd:**")
                    st.markdown(st.session_state['generated_description_akeneo'], unsafe_allow_html=True)
                
                # Porównanie starych i nowych opisów
                if 'akeneo_product_details' in st.session_state:
                    old_desc = st.session_state['akeneo_product_details'].get('description', '')
                    if old_desc:
                        with st.expander("📊 Porównanie: Stary vs Nowy opis"):
                            col_old, col_new = st.columns(2)
                            with col_old:
                                st.markdown("**Stary opis (Akeneo):**")
                                st.markdown(f"*Długość: {len(old_desc)} znaków*")
                                st.markdown(old_desc[:500] + "..." if len(old_desc) > 500 else old_desc, unsafe_allow_html=True)
                            with col_new:
                                st.markdown("**Nowy opis (AI):**")
                                new_desc = st.session_state['generated_description_akeneo']
                                st.markdown(f"*Długość: {len(new_desc)} znaków*")
                                st.markdown(new_desc, unsafe_allow_html=True)
                
                # Metatagi
                if 'meta_title_akeneo' in st.session_state:
                    st.markdown("---")
                    st.subheader("🏷️ Metatagi SEO")
                    
                    title_len = len(st.session_state['meta_title_akeneo'])
                    desc_len = len(st.session_state['meta_description_akeneo'])
                    
                    title_color = "green" if title_len <= 60 else "red"
                    desc_color = "green" if desc_len <= 160 else "red"
                    
                    st.markdown(f"**Meta Title** (:{title_color}[{title_len}/60]): {st.session_state['meta_title_akeneo']}")
                    st.markdown(f"**Meta Description** (:{desc_color}[{desc_len}/160]): {st.session_state['meta_description_akeneo']}")
                
                # Przycisk wysyłki
                st.markdown("---")
                if st.button(
                    "✅ Zaakceptuj i zaktualizuj w PIM",
                    use_container_width=True,
                    type="primary",
                    key="send_pim_akeneo"
                ):
                    try:
                        with st.spinner("Aktualizuję produkt w Akeneo..."):
                            ok = akeneo_update_description(
                                selected_sku,
                                st.session_state['generated_description_akeneo'],
                                channel,
                                locale
                            )
                            if ok:
                                st.success(f"✅ Opis zaktualizowany dla SKU: {selected_sku}")
                                st.balloons()
                    except Exception as e:
                        st.error(f"❌ Błąd aktualizacji: {e}")

# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 3: PRZETWARZANIE WSADOWE
# ═══════════════════════════════════════════════════════════════════

with tab3:
    st.header("🚀 Przetwarzanie wielu produktów jednocześnie")
    st.info("Wklej linki URL i odpowiadające im SKU, każdy w nowej linii. Kolejność musi być identyczna.")
    
    col_urls, col_skus = st.columns(2)
    
    with col_urls:
        urls_batch = st.text_area(
            "Linki do produktów (jeden na linię)",
            height=250,
            placeholder="https://.../produkt1\nhttps://.../produkt2",
            key="urls_batch"
        )
    
    with col_skus:
        skus_batch = st.text_area(
            "Kody SKU (jeden na linię)",
            height=250,
            placeholder="SKU-001\nSKU-002",
            key="skus_batch"
        )
    
    col_b1, col_b2 = st.columns(2)
    
    with col_b1:
        if st.button(
            "🚀 Rozpocznij generowanie wsadowe",
            type="primary",
            use_container_width=True,
            key="gen_batch"
        ):
            urls = [url.strip() for url in urls_batch.splitlines() if url.strip()]
            skus = [sku.strip() for sku in skus_batch.splitlines() if sku.strip()]
            
            if not urls:
                st.warning("⚠️ Podaj przynajmniej jeden URL.")
            elif len(urls) != len(skus):
                st.error(f"❌ Niezgodna liczba linków ({len(urls)}) i SKU ({len(skus)}).")
            else:
                st.session_state.batch_results = []
                data_to_process = list(zip(urls, skus))
                progress_bar = st.progress(0, text="Rozpoczynam...")
                
                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_data = {
                        executor.submit(process_single_url, url, sku, client): (url, sku)
                        for url, sku in data_to_process
                    }
                    results_temp = []
                    
                    for i, future in enumerate(as_completed(future_to_data)):
                        result = future.result()
                        results_temp.append(result)
                        progress_bar.progress(
                            (i + 1) / len(data_to_process),
                            text=f"Przetworzono {i+1}/{len(data_to_process)}"
                        )
                
                st.session_state.batch_results = sorted(
                    results_temp,
                    key=lambda x: urls.index(x['url'])
                )
                progress_bar.progress(1.0, text="Zakończono!")
    
    with col_b2:
        if st.button("🗑️ Wyczyść wyniki", use_container_width=True, key="clear_batch"):
            st.session_state.batch_results = []
            st.rerun()

    # Wyświetlanie wyników
    if st.session_state.batch_results:
        st.markdown("---")
        st.subheader("📊 Wyniki generowania")

        results = st.session_state.batch_results
        successful_results = [r for r in results if r['error'] is None]
        error_count = len(results) - len(successful_results)
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Liczba linków", len(results))
        c2.metric("Wygenerowano pomyślnie", len(successful_results))
        c3.metric("Błędy", error_count)
        
        df = pd.DataFrame(results)
        st.download_button(
            "📥 Pobierz wyniki jako CSV",
            df.to_csv(index=False).encode('utf-8'),
            'wygenerowane_opisy.csv',
            'text/csv'
        )
        
        pim_disabled_batch = len(missing) > 0 or not successful_results
        
        if st.button(
            "✅ Wyślij wszystkie pomyślne do PIM",
            type="primary",
            use_container_width=True,
            disabled=pim_disabled_batch,
            key="send_batch_pim"
        ):
            success_pim_count = 0
            error_pim_count = 0
            error_messages = []
            
            progress_bar_pim = st.progress(0, text="Rozpoczynam wysyłanie...")
            
            with st.spinner("Aktualizowanie produktów w PIM..."):
                for i, result in enumerate(successful_results):
                    sku = result['sku']
                    html = result['description_html']
                    progress_bar_pim.progress(
                        (i + 1) / len(successful_results),
                        text=f"Wysyłam SKU: {sku} ({i+1}/{len(successful_results)})"
                    )
                    
                    try:
                        akeneo_update_description(sku, html, channel, locale)
                        success_pim_count += 1
                    except Exception as e:
                        error_pim_count += 1
                        error_messages.append(f"**SKU {sku}:** {e}")
            
            st.success(f"✅ Zaktualizowano **{success_pim_count}** produktów.")
            
            if error_pim_count > 0:
                st.error(f"❌ Błędy podczas aktualizacji **{error_pim_count}** produktów:")
                for msg in error_messages:
                    st.markdown(f"- {msg}")

        # Ekspandery z wynikami
        for result in results:
            if result['error']:
                with st.expander(f"❌ Błąd: {result['url']}", expanded=False):
                    st.error(result['error'])
                    st.write(f"**SKU:** {result['sku']}")
            else:
                with st.expander(f"✅ {result['title'] or result['url']}"):
                    st.write(f"**URL:** {result['url']}")
                    st.write(f"**SKU:** {result['sku']}")
                    st.code(result['description_html'], language='html')

# ═══════════════════════════════════════════════════════════════════
# STOPKA
# ═══════════════════════════════════════════════════════════════════

st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; padding: 20px;'>
    <p><strong>Generator Opisów Produktów v2.0</strong></p>
    <p>Powered by OpenAI GPT-5-nano | Built with Streamlit | Integrated with Akeneo PIM</p>
    <p>🆕 Nowy tryb: Wyszukiwanie produktów w Akeneo</p>
</div>
""", unsafe_allow_html=True)
