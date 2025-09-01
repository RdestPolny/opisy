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
    # dopasuj cały blok ```[html] ... ```
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m:
        return m.group(1).strip()
    # albo usuń ewentualne pojedyncze płotki na początku/końcu
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
    # spodziewamy się .../api/rest/v1
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
        details_div = soup.find("div", id="szczegoly") or soup.find("div", class_="product-features")
        if details_div:
            ul = details_div.find("ul", class_="bullet") or details_div.find("ul")
            if ul:
                li_elements = ul.find_all("li")
                details_list = [li.get_text(separator=" ", strip=True) for li in li_elements]
                details_text = "\n".join(details_list)

        description_text = ""
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

        if not description_text:
            return {
                'title': title,
                'details': details_text,
                'description': '',
                'error': "Nie udało się pobrać opisu produktu. Zatrzymuję przetwarzanie."
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

# ------------- NOWA LOGIKA GENEROWANIA OPISU ------------- #

def generate_dynamic_prompt(product_data, client):
    """
    Etap 1: Analizuje dane produktu i generuje dedykowany prompt do stworzenia opisu.
    """
    try:
        title = product_data.get('title', '')
        description = product_data.get('description', '')
        
        meta_prompt = f"""Jesteś ekspertem od e-commerce i prompt engineeringu. Twoim zadaniem jest analiza poniższych danych produktu, aby zidentyfikować jego kategorię (np. książka - romans, kryminał, fantastyka; zabawka edukacyjna; gra planszowa strategiczna itp.).

Na podstawie tej analizy, stwórz szczegółowy prompt dla innego AI, które jest profesjonalnym copywriterem. Ten prompt ma posłużyć do wygenerowania kompletnego opisu produktu w formacie HTML.

**Krytyczne wymagania dla promptu, który stworzysz:**
1.  **Struktura HTML:** Prompt musi jasno nakazać użycie następującej struktury:
    - Zaczyna się od nagłówka `<h2>` z chwytliwym, kreatywnym hasłem dopasowanym do kategorii produktu.
    - Zawiera kilka akapitów `<p>` opisujących produkt, jego cechy i korzyści.
    - Używa tagów `<b>` do wyróżnienia najważniejszych słów kluczowych i fraz.
    - Kończy się nagłówkiem `<h3>` z przekonującym wezwaniem do działania (Call To Action).
2.  **Ton i Styl:** Prompt musi określić ton i styl pisania, odpowiedni dla zidentyfikowanej kategorii produktu i jego grupy docelowej (np. emocjonalny dla romansu, budujący napięcie dla kryminału, edukacyjny i przyjazny dla zabawki).
3.  **Wykorzystanie Danych:** Prompt musi instruować, aby copywriter bazował na dostarczonych danych: `{{book_title}}`, `{{book_details}}`, `{{book_description}}`.
4.  **Format Wyjściowy:** Zwróć TYLKO I WYŁĄCZNIE tekst promptu, bez żadnych dodatkowych komentarzy, wstępów czy formatowania typu "```prompt ... ```".

**Dane produktu do analizy:**
Tytuł: "{title}"
Opis: "{description[:1000]}..."

Wygeneruj teraz prompt dla copywritera AI."""

        messages = [
            {"role": "system", "content": "Jesteś światowej klasy strategiem treści i prompt engineerem specjalizującym się w e-commerce."},
            {"role": "user", "content": meta_prompt}
        ]
        
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=messages,
            temperature=0.5,
            max_completion_tokens=1000 # POPRAWKA
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Błąd generowania dynamicznego promptu: {str(e)}")
        return ""

def generate_description(book_data, dynamic_prompt, client):
    """
    Etap 2: Generuje opis produktu na podstawie dynamicznie stworzonego promptu.
    """
    try:
        prompt_filled = dynamic_prompt.format(
            book_title=book_data.get('title', ''),
            book_details=book_data.get('details', ''),
            book_description=book_data.get('description', '')
        )
        messages = [
            {"role": "system", "content": "Jesteś profesjonalnym copywriterem. Tworzysz wyłącznie poprawne, atrakcyjne opisy książek i produktów do księgarni internetowej. Każdy opis ma być zgodny z poleceniem i formą HTML, nie dodawaj nic od siebie."},
            {"role": "user", "content": prompt_filled}
        ]
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=messages,
            temperature=0.7,
            max_completion_tokens=2000 # POPRAWKA
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Błąd generowania opisu: {str(e)}")
        return ""

def generate_meta_tags(product_data, client):
    try:
        title = product_data.get('title', '')
        details = product_data.get('details', '')
        description = product_data.get('description', '')
        prompt_meta = f"""Jako doświadczony copywriter SEO, stwórz meta title oraz meta description dla produktu o tytule "{title}" bazując na następujących danych: {details} {description}. Meta title powinien zaczynać się od silnego słowa kluczowego, zawierać do 60 znaków, a meta description powinien być jednym zdaniem informacyjnym, zawierającym do 160 znaków. Podaj wynik w formacie:
Meta title: [treść]
Meta description: [treść]"""
        messages = [
            {"role": "system", "content": "Jesteś doświadczonym copywriterem SEO."},
            {"role": "user", "content": prompt_meta}
        ]
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=messages,
            temperature=0.7,
            max_completion_tokens=200 # POPRAWKA
        )
        result = response.choices[0].message.content
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

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

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
            keys_to_remove = [key for key in st.session_state.keys() if key != 'some_persistent_key'] # zachowaj co potrzebne
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
                
                with st.spinner("Analizuję produkt i generuję opis... To może chwilę potrwać."):
                    # Etap 1: Generowanie dynamicznego promptu
                    st.info("Krok 1: Identyfikacja kategorii i tworzenie dedykowanego promptu...")
                    dynamic_prompt = generate_dynamic_prompt(book_data, client)
                    
                    if not dynamic_prompt:
                        st.error("❌ Nie udało się wygenerować dynamicznego promptu. Przerwanie operacji.")
                    else:
                        st.session_state['dynamic_prompt'] = dynamic_prompt
                        
                        # Etap 2: Generowanie opisu na podstawie nowego promptu
                        st.info("Krok 2: Generowanie opisu na podstawie nowego promptu...")
                        generated_desc_raw = generate_description(book_data, dynamic_prompt, client)
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


with col2:
    st.header("📄 Wygenerowany opis")

    if 'generated_description' in st.session_state:
        st.subheader(f"📖 {st.session_state.get('book_title', 'Opis produktu')}")
        
        if 'dynamic_prompt' in st.session_state:
            with st.expander("🕵️ Zobacz prompt użyty do generacji"):
                st.text(st.session_state['dynamic_prompt'])

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
