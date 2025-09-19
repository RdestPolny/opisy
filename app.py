import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
from openai import OpenAI
import json
import re

# ------------- USTAWIENIA STRONY ------------- #
st.set_page_config(page_title="Generator opisów książek", page_icon="📚", layout="wide")

def strip_code_fences(text: str) -> str:
    if not text:
        return text
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m:
        return m.group(1).strip()
    text = re.sub(r"^\s*```(?:html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()

# ------------- AKENEO API (bez zmian) ------------- #
def akeneo_get_attribute(code, token):
    url = _akeneo_root() + f"/api/rest/v1/attributes/{code}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()
    
def _akeneo_root():
    base = st.secrets["AKENEO_BASE_URL"].rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[:-len("/api/rest/v1")]
    return base

def akeneo_get_token():
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

def akeneo_product_exists(sku, token):
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    r.raise_for_status()

def akeneo_update_description(sku, html_description, channel, locale="pl_PL"):
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt o SKU '{sku}' nie istnieje w Akeneo.")
    attr = akeneo_get_attribute("description", token)
    is_scopable = bool(attr.get("scopable", False))
    is_localizable = bool(attr.get("localizable", False))
    value_obj = {
        "data": html_description,
        "scope": channel if is_scopable else None,
        "locale": locale if is_localizable else None,
    }
    url = _akeneo_root() + f"/api/rest/v1/products/{sku}"
    payload = {"values": {"description": [value_obj]}}
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

# ------------- POBIERANIE DANYCH (bez zmian) ------------- #
def get_book_data(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
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

        # Logika specyficzna dla smyk.com
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
        
        # Stara logika (jeśli nie znaleziono opisu dla Smyka lub to inna strona)
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
                'title': title, 'details': '', 'description': '',
                'error': "Nie udało się pobrać opisu ani szczegółów produktu. Sprawdź strukturę strony."
            }
        return {
            'title': title, 'details': details_text, 'description': description_text,
            'error': None
        }
    except Exception as e:
        return {
            'title': '', 'details': '', 'description': '',
            'error': f"Błąd pobierania: {str(e)}"
        }

# ------------- ZMODYFIKOWANA LOGIKA GENEROWANIA OPISU (JEDEN KROK) ------------- #
def generate_description(product_data, client):
    """
    Generuje opis produktu w jednym kroku, analizując produkt i dostosowując styl.
    """
    try:
        system_prompt = """Jesteś światowej klasy copywriterem e-commerce, specjalizującym się w tworzeniu angażujących, czytelnych i zoptymalizowanych pod SEO opisów produktów.

--- TWOJE ZADANIE ---
Twoim pierwszym zadaniem jest **wewnętrzna analiza** dostarczonych danych, aby zrozumieć, czym jest produkt. Na podstawie tej analizy musisz **automatycznie dostosować ton i styl** opisu. Przykładowo:
-   Dla **kryminału** użyj języka budującego napięcie i tajemnicę.
-   Dla **zabawki edukacyjnej** pisz w sposób przyjazny i informacyjny, podkreślając korzyści dla rozwoju dziecka.
-   Dla **powieści fantasy** skup się na budowaniu niezwykłego klimatu i świata przedstawionego.
-   Dla **gry planszowej** opisz dynamicznie zasady i emocje towarzyszące rozgrywce.

Po tej analizie, stwórz opis produktu, bezwzględnie przestrzegając poniższych zasad.

--- KRYTYCZNE ZASADY, KTÓRYCH MUSISZ ZAWSZE PRZESTRZEGAĆ ---

1.  **JĘZYK I POPRAWNOŚĆ:**
    -   Używaj WYŁĄCZNIE nienagannej polszczyzny. Dbaj o gramatykę, ortografię i interpunkcję. Tekst musi być absolutnie wolny od literówek i błędów.
    -   Absolutnie nie wolno wstawiać zwrotów w innych językach. Cały tekst musi być po polsku.

2.  **STRUKTURA I FORMAT HTML:**
    -   Zwróć wyłącznie gotowy kod HTML, bez żadnych dodatkowych komentarzy czy wyjaśnień.
    -   Zastosuj poniższą strukturę:
        -   `<p>`: Krótki, chwytliwy akapit wprowadzający (2-3 zdania).
        -   `<h2>`: Pierwszy nagłówek, który rozwija myśl z wprowadzenia.
        -   `<p>`: 1-2 krótkie akapity (maks. 3-4 zdania każdy).
        -   `<h2>`: Drugi, inny nagłówek, wprowadzający kolejny aspekt produktu.
        -   `<p>`: 1-2 krótkie akapity (maks. 3-4 zdania każdy).
        -   `<h3>`: Nagłówek końcowy z wezwaniem do działania (Call To Action).
    -   Dzielenie tekstu nagłówkami jest OBOWIĄZKOWE.

3.  **ZASADY POGRUBiania (BARDZO WAŻNE!):**
    -   Używaj tagów `<b>` oszczędnie.
    -   Pogrubiaj **TYLKO pojedyncze, kluczowe słowa lub bardzo krótkie frazy (2-4 słowa)**.
    -   **NIGDY nie pogrubiaj całych zdań ani długich fragmentów akapitów.**

4.  **TREŚĆ I UNIKANIE POWTÓRZEŃ:**
    -   Napisz opis marketingowy, a NIE streszczenie techniczne. Wykorzystaj dane techniczne, aby wpleść je w treść (np. "zabawka od marki Dumel jest idealna dla dzieci powyżej roku"), ale NIE twórz listy atrybutów.
    -   **Kategorycznie unikaj powtarzania w tekście danych katalogowych takich jak numer ISBN, EAN, wydawnictwo, liczba stron, format, typ oprawy.**

5.  **DŁUGOŚĆ OPISU:**
    -   Celuj w wyczerpujący, ale zwięzły opis o długości około 1500-2500 znaków.
"""
        raw_data_context = f"""
--- DANE PRODUKTU DO ANALIZY I OPISANIA ---
Tytuł: {product_data.get('title', '')}
Szczegóły techniczne (do inspiracji, nie kopiowania): {product_data.get('details', '')}
Oryginalny opis (główne źródło informacji): {product_data.get('description', '')}
"""
        
        full_input = f"{system_prompt}\n\n{raw_data_context}"

        response = client.responses.create(
            model="gpt-5-nano",
            input=full_input,
            reasoning={"effort": "high"}, # Zwiększamy effort, aby AI lepiej przeanalizowało dane
            text={"verbosity": "medium"}
        )
        return response.output_text
    except Exception as e:
        st.error(f"Błąd generowania opisu: {str(e)}")
        return ""

def generate_meta_tags(product_data, client):
    try:
        title = product_data.get('title', '')
        details = product_data.get('details', '')
        description = product_data.get('description', '')
        
        system_prompt = "Jesteś doświadczonym copywriterem SEO. Pisz wyłącznie po polsku."
        user_prompt = f"""Stwórz meta title oraz meta description dla produktu o tytule "{title}" bazując na danych: {details} {description}. 
Meta title: do 60 znaków, zaczynający się od słowa kluczowego.
Meta description: do 160 znaków, jedno zdanie informacyjne.
Format wyjściowy:
Meta title: [treść]
Meta description: [treść]"""

        full_input = f"{system_prompt}\n\n{user_prompt}"

        response = client.responses.create(
            model="gpt-5-nano",
            input=full_input,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"}
        )
        result = response.output_text
        meta_title = ""
        meta_description = ""
        for line in result.splitlines():
            if line.lower().startswith("meta title:"):
                meta_title = line[len("meta title:"):].strip()
            elif line.lower().startswith("meta description:"):
                meta_description = line[len("meta description:"):].strip()
        return meta_title, meta_description
    except Exception as e:
        st.error(f"Błąd generowania metatagów: {str(e)}")
        return "", ""

# ------------- INICJALIZACJA STANU ------------- #
if 'show_preview' not in st.session_state:
    st.session_state.show_preview = False

# ------------- WALIDACJA SEKRETÓW ------------- #
if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak klucza API OpenAI w secrets. Skonfiguruj OPENAI_API_KEY.")
    st.stop()

required_akeneo_secrets = ["AKENEO_BASE_URL","AKENEO_CLIENT_ID","AKENEO_SECRET","AKENEO_USERNAME","AKENEO_PASSWORD"]
missing = [k for k in required_akeneo_secrets if k not in st.secrets]
if missing:
    st.warning(f"⚠️ Brak konfiguracji Akeneo w secrets: {', '.join(missing)}. Wysyłka do PIM będzie niedostępna.")

client = OpenAI()

# ------------- UI ------------- #
st.title('📚 Inteligentny Generator Opisów Produktów')
st.markdown("---")

st.sidebar.header("🎯 Ustawienia")
channel = st.sidebar.selectbox(
    "Kanał (scope) do zapisu w PIM:",
    ["Bookland", "B2B"],
    index=0
)
locale = st.sidebar.text_input(
    "Locale:",
    value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL")
)
st.sidebar.info("Aplikacja automatycznie wykryje kategorię produktu i dostosuje styl opisu.")

col1, col2 = st.columns([1, 1])

with col1:
    st.header("📝 Dane wejściowe")
    url = st.text_input(
        "URL strony produktu:",
        placeholder="https://przyklad.com/ksiazka-lub-gra",
        help="Wklej pełny URL strony produktu"
    )
    sku = st.text_input(
        "SKU w PIM (identifier):",
        placeholder="np. BL-12345",
        help="Kod identyfikatora produktu w Akeneo"
    )

    generate_meta = st.checkbox("Generuj meta title i meta description", value=False)

    col_btn1, col_btn2 = st.columns([1, 1])
    with col_btn1:
        generate_button = st.button("🚀 Generuj opis", type="primary", use_container_width=True)
    with col_btn2:
        if st.button("🔄 Generuj kolejny", use_container_width=True):
            keys_to_remove = [key for key in st.session_state.keys() if key != 'some_persistent_key']
            for key in keys_to_remove:
                del st.session_state[key]
            st.session_state.show_preview = False
            st.rerun()

    if generate_button:
        if not url:
            st.error("❌ Podaj URL strony produktu!")
        else:
            with st.spinner("Pobieram dane ze strony..."):
                book_data = get_book_data(url)
            
            if book_data['error']:
                st.error(f"❌ {book_data['error']}")
            else:
                st.success("✅ Dane pobrane pomyślnie!")
                st.subheader("📊 Pobrane dane")
                st.write(f"**Tytuł:** {book_data['title']}")
                if book_data['description']:
                    full_desc = book_data['description']
                    st.write("**Opis (pierwsze 500 znaków):**")
                    st.text_area("Opis", (full_desc[:500] + "...") if len(full_desc) > 500 else full_desc, height=150, disabled=True)
                
                if book_data['details']:
                    with st.expander("Zobacz pobrane szczegóły techniczne"):
                        st.text(book_data['details'])
                
                with st.spinner("Analizuję produkt i generuję opis... To może chwilę potrwać."):
                    generated_desc_raw = generate_description(book_data, client)
                    generated_desc = strip_code_fences(generated_desc_raw)
                    
                    if generated_desc:
                        st.session_state['generated_description'] = generated_desc
                        st.session_state['book_title'] = book_data['title']
                        st.session_state.show_preview = False

                        if generate_meta:
                            with st.spinner("Generuję metatagi..."):
                                meta_title, meta_description = generate_meta_tags(book_data, client)
                                st.session_state['meta_title'] = meta_title
                                st.session_state['meta_description'] = meta_description
                        
                        st.success("✅ Opis wygenerowany pomyślnie!")
                    else:
                        st.error("❌ Nie udało się wygenerować opisu. Spróbuj ponownie.")

with col2:
    st.header("📄 Wygenerowany opis")

    if 'generated_description' in st.session_state:
        st.subheader(f"📖 {st.session_state.get('book_title', 'Opis produktu')}")
        
        st.markdown("**Kod HTML:**")
        html_code = st.session_state['generated_description']
        st.code(html_code, language='html')

        if st.button("📋 Skopiuj kod HTML", use_container_width=True):
            st.success("✅ Kod HTML jest gotowy do skopiowania z pola powyżej!")

        if st.button("👁️ Pokaż/Ukryj podgląd", use_container_width=True):
            st.session_state.show_preview = not st.session_state.show_preview

        if st.session_state.show_preview:
            st.markdown("---")
            st.markdown("**Podgląd:**")
            st.markdown(st.session_state['generated_description'], unsafe_allow_html=True)

        if 'meta_title' in st.session_state and 'meta_description' in st.session_state:
            st.markdown("---")
            st.subheader("🏷️ Metatagi SEO")
            st.write(f"**Meta Title:** {st.session_state['meta_title']}")
            st.write(f"**Meta Description:** {st.session_state['meta_description']}")
            meta_code = f"""<title>{st.session_state['meta_title']}</title>
<meta name="description" content="{st.session_state['meta_description']}">"""
            st.code(meta_code, language='html')

        st.markdown("---")
        pim_disabled = len(missing) > 0 if 'missing' in locals() else False
        if st.button("✅ Zaakceptuj i wyślij do PIM", use_container_width=True, type="primary", disabled=pim_disabled):
            if pim_disabled:
                st.error("❌ Konfiguracja Akeneo niepełna. Uzupełnij sekrety i odśwież aplikację.")
            elif not sku:
                st.error("❌ Podaj SKU produktu (identifier) przed wysyłką do PIM.")
            else:
                try:
                    ok = akeneo_update_description(
                        sku=sku.strip(),
                        html_description=st.session_state['generated_description'],
                        channel=channel,
                        locale=locale.strip() or "pl_PL"
                    )
                    if ok:
                        st.success(f"✅ Opis zapisany w Akeneo dla SKU: {sku} (kanał: {channel}, locale: {locale}).")
                except Exception as e:
                    st.error(f"❌ Błąd zapisu do Akeneo: {e}")
    else:
        st.info("👈 Podaj URL i kliknij 'Generuj opis' aby rozpocząć")

# ------------- STOPKA ------------- #
st.markdown("---")
st.markdown("🔧 **Narzędzie do generowania opisów produktów** | Wykorzystuje OpenAI gpt-5-nano")
st.markdown("💡 **Wskazówka:** Aplikacja sama analizuje produkt i dobiera najlepszy styl opisu.")
