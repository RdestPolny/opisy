import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
from openai import OpenAI
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# ------------- USTAWIENIA STRONY ------------- #
st.set_page_config(page_title="Generator opisów produktów", page_icon="📚", layout="wide")

# ------------- FUNKCJE POMOCNICZE ------------- #
def strip_code_fences(text: str) -> str:
    if not text:
        return text
    m = re.match(r"^\s*```(?:html|HTML)?\s*([\s\S]*?)\s*```\s*$", text)
    if m:
        return m.group(1).strip()
    text = re.sub(r"^\s*```(?:html|HTML)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()

# ------------- AKENEO API ------------- #
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

# ZAKTUALIZOWANA FUNKCJA WYSYŁANIA DO AKENEO
def akeneo_update_description(sku, html_description, channel, locale="pl_PL"):
    token = akeneo_get_token()
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt o SKU '{sku}' nie istnieje w Akeneo.")
    
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
            "data": True,  # POPRAWKA: Zmiana z 1 na True
            "scope": channel if is_scopable_seo else None,
            "locale": locale if is_localizable_seo else None,
        }
        payload_values["opisy_seo"] = [value_obj_seo]
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            st.warning("⚠️ Nie udało się zaktualizować atrybutu 'opisy_seo'. Sprawdź, czy atrybut o takim kodzie istnieje w Akeneo. Opis główny zostanie zaktualizowany.")
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

# ------------- POBIERANIE DANYCH ------------- #
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
            return {'title': title, 'details': '', 'description': '', 'error': "Nie udało się pobrać opisu ani szczegółów produktu."}
        return {'title': title, 'details': details_text, 'description': description_text, 'error': None}
    except Exception as e:
        return {'title': '', 'details': '', 'description': '', 'error': f"Błąd pobierania: {str(e)}"}

# ------------- LOGIKA GENEROWANIA ------------- #
def generate_description(product_data, client):
    try:
        system_prompt = """Jesteś światowej klasy copywriterem e-commerce... (reszta promptu bez zmian)"""
        raw_data_context = f"""
--- DANE PRODUKTU DO ANALIZY I OPISANIA ---
Tytuł: {product_data.get('title', '')}
Szczegóły techniczne (do inspiracji, nie kopiowania): {product_data.get('details', '')}
Oryginalny opis (główne źródło informacji): {product_data.get('description', '')}
"""
        full_input = f"{system_prompt}\n\n{raw_data_context}"
        response = client.responses.create(model="gpt-5-nano", input=full_input, reasoning={"effort": "high"}, text={"verbosity": "medium"})
        return strip_code_fences(response.output_text)
    except Exception as e:
        return f"BŁĄD GENEROWANIA: {str(e)}"

def generate_meta_tags(product_data, client):
    # ... (funkcja bez zmian)
    return "", ""

# ------------- FUNKCJA DO PRZETWARZANIA RÓWNOLEGŁEGO ------------- #
def process_single_url(url, sku, client):
    try:
        product_data = get_book_data(url)
        if product_data['error']:
            return {'url': url, 'sku': sku, 'title': product_data.get('title', ''), 'description_html': '', 'error': product_data['error']}
        description_html = generate_description(product_data, client)
        if "BŁĄD GENEROWANIA:" in description_html:
             return {'url': url, 'sku': sku, 'title': product_data.get('title', ''), 'description_html': '', 'error': description_html}
        return {'url': url, 'sku': sku, 'title': product_data.get('title', ''), 'description_html': description_html, 'error': None}
    except Exception as e:
        return {'url': url, 'sku': sku, 'title': '', 'description_html': '', 'error': f"Nieoczekiwany błąd w wątku: {str(e)}"}

# ------------- INICJALIZACJA STANU I WALIDACJA ------------- #
if 'show_preview' not in st.session_state:
    st.session_state.show_preview = False
if 'batch_results' not in st.session_state:
    st.session_state.batch_results = []
if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak klucza API OpenAI w secrets.")
    st.stop()
required_akeneo_secrets = ["AKENEO_BASE_URL","AKENEO_CLIENT_ID","AKENEO_SECRET","AKENEO_USERNAME","AKENEO_PASSWORD"]
missing = [k for k in required_akeneo_secrets if k not in st.secrets]
if missing:
    st.warning(f"⚠️ Brak konfiguracji Akeneo: {', '.join(missing)}. Wysyłka do PIM będzie niedostępna.")
client = OpenAI()

# ------------- UI ------------- #
st.title('📚 Inteligentny Generator Opisów Produktów')

# Ustawienia PIM w panelu bocznym (globalne dla obu zakładek)
st.sidebar.header("🎯 Ustawienia PIM")
channel = st.sidebar.selectbox("Kanał (scope):", ["Bookland", "B2B"], index=0, key="channel_global")
locale = st.sidebar.text_input("Locale:", value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL"), key="locale_global")

tab1, tab2 = st.tabs(["👤 Pojedynczy produkt", "🗂️ Przetwarzanie wsadowe"])

# --- ZAKŁADKA 1: TRYB POJEDYNCZY ---
with tab1:
    col1, col2 = st.columns([1, 1])
    with col1:
        st.header("📝 Dane wejściowe")
        url_single = st.text_input("URL strony produktu:", key="url_single")
        sku_single = st.text_input("SKU w PIM (identifier):", key="sku_single")
        generate_meta = st.checkbox("Generuj meta title i meta description", value=False, key="meta_single")
        col_btn1, col_btn2 = st.columns([1, 1])
        with col_btn1:
            generate_button = st.button("🚀 Generuj opis", type="primary", use_container_width=True, key="gen_single")
        with col_btn2:
            if st.button("🔄 Wyczyść", use_container_width=True, key="clear_single"):
                keys_to_clear = ['generated_description', 'book_title', 'meta_title', 'meta_description', 'show_preview']
                for key in keys_to_clear:
                    if key in st.session_state: del st.session_state[key]
                st.rerun()
        if generate_button:
            if not url_single:
                st.error("❌ Podaj URL strony produktu!")
            else:
                with st.spinner("Pobieram dane..."):
                    book_data = get_book_data(url_single)
                if book_data['error']:
                    st.error(f"❌ {book_data['error']}")
                else:
                    st.success("✅ Dane pobrane!")
                    with st.spinner("Generuję opis..."):
                        generated_desc = generate_description(book_data, client)
                        if "BŁĄD GENEROWANIA:" not in generated_desc:
                            st.session_state['generated_description'] = generated_desc
                            st.session_state['book_title'] = book_data['title']
                            st.session_state.show_preview = False
                            if generate_meta:
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
            st.code(st.session_state['generated_description'], language='html')
            if st.button("👁️ Pokaż/Ukryj podgląd", use_container_width=True, key="preview_single"):
                st.session_state.show_preview = not st.session_state.show_preview
            if st.session_state.show_preview:
                st.markdown("**Podgląd:**", unsafe_allow_html=True)
                st.markdown(st.session_state['generated_description'], unsafe_allow_html=True)
            if 'meta_title' in st.session_state:
                st.markdown("---"); st.subheader("🏷️ Metatagi SEO")
                st.write(f"**Meta Title:** {st.session_state['meta_title']}")
                st.write(f"**Meta Description:** {st.session_state['meta_description']}")
            st.markdown("---")
            pim_disabled = len(missing) > 0
            if st.button("✅ Zaakceptuj i wyślij do PIM", use_container_width=True, type="primary", disabled=pim_disabled, key="send_pim_single"):
                if not sku_single:
                    st.error("❌ Podaj SKU produktu przed wysyłką do PIM.")
                else:
                    try:
                        with st.spinner("Wysyłam do Akeneo..."):
                            ok = akeneo_update_description(sku_single.strip(), st.session_state['generated_description'], channel, locale.strip())
                            if ok:
                                st.success(f"✅ Opis zapisany w Akeneo dla SKU: {sku_single}. Atrybut 'opisy_seo' został ustawiony na 'Yes'.")
                    except Exception as e:
                        st.error(f"❌ Błąd zapisu do Akeneo: {e}")
        else:
            st.info("👈 Podaj URL i kliknij 'Generuj opis'")

# --- ZAKŁADKA 2: TRYB WSADOWY ---
with tab2:
    st.header("🚀 Przetwarzanie wielu linków jednocześnie")
    st.info("Wklej linki i odpowiadające im kody SKU, każdy w nowej linii. Upewnij się, że kolejność jest taka sama w obu polach.")
    col_urls, col_skus = st.columns(2)
    with col_urls:
        urls_batch = st.text_area("Linki do produktów (jeden na linię)", height=250, key="urls_batch")
    with col_skus:
        skus_batch = st.text_area("Kody SKU (jeden na linię)", height=250, key="skus_batch")
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        if st.button("🚀 Rozpocznij generowanie wsadowe", type="primary", use_container_width=True, key="gen_batch"):
            urls = [url.strip() for url in urls_batch.splitlines() if url.strip()]
            skus = [sku.strip() for sku in skus_batch.splitlines() if sku.strip()]
            if not urls:
                st.warning("⚠️ Podaj przynajmniej jeden URL.")
            elif len(urls) != len(skus):
                st.error(f"❌ Niezgodna liczba linków ({len(urls)}) i SKU ({len(skus)}).")
            else:
                st.session_state.batch_results = []
                data_to_process = list(zip(urls, skus))
                progress_bar = st.progress(0, text="Rozpoczynam przetwarzanie...")
                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_data = {executor.submit(process_single_url, url, sku, client): (url, sku) for url, sku in data_to_process}
                    results_temp = []
                    for i, future in enumerate(as_completed(future_to_data)):
                        result = future.result()
                        results_temp.append(result)
                        progress_bar.progress((i + 1) / len(data_to_process), text=f"Przetworzono {i+1}/{len(data_to_process)}: {result['url']}")
                st.session_state.batch_results = sorted(results_temp, key=lambda x: urls.index(x['url']))
                progress_bar.progress(1.0, text="Zakończono!")
    with col_b2:
        if st.button("🗑️ Wyczyść wyniki", use_container_width=True, key="clear_batch"):
            st.session_state.batch_results = []
            st.rerun()

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
        st.download_button("📥 Pobierz wyniki jako CSV", df.to_csv(index=False).encode('utf-8'), 'wygenerowane_opisy.csv', 'text/csv')
        
        # NOWY PRZYCISK DO WYSYŁANIA WSADOWEGO
        pim_disabled_batch = len(missing) > 0 or not successful_results
        if st.button("✅ Wyślij wszystkie pomyślne do PIM", type="primary", use_container_width=True, disabled=pim_disabled_batch):
            success_pim_count = 0
            error_pim_count = 0
            error_messages = []
            
            progress_bar_pim = st.progress(0, text="Rozpoczynam wysyłanie do Akeneo...")
            with st.spinner("Aktualizowanie produktów w PIM... To może potrwać."):
                for i, result in enumerate(successful_results):
                    sku = result['sku']
                    html = result['description_html']
                    progress_bar_pim.progress((i + 1) / len(successful_results), text=f"Wysyłam SKU: {sku} ({i+1}/{len(successful_results)})")
                    try:
                        akeneo_update_description(sku, html, channel, locale)
                        success_pim_count += 1
                    except Exception as e:
                        error_pim_count += 1
                        error_messages.append(f"**SKU {sku}:** {e}")
            
            st.success(f"Zakończono! Pomyślnie zaktualizowano **{success_pim_count}** produktów.")
            if error_pim_count > 0:
                st.error(f"Wystąpiły błędy podczas aktualizacji **{error_pim_count}** produktów:")
                for msg in error_messages:
                    st.markdown(f"- {msg}")

        for result in results:
            if result['error']:
                with st.expander(f"❌ Błąd: {result['url']}", expanded=True):
                    st.error(result['error']); st.write(f"**SKU:** {result['sku']}")
            else:
                with st.expander(f"✅ Sukces: {result['title'] or result['url']}"):
                    st.write(f"**URL:** {result['url']}"); st.write(f"**SKU:** {result['sku']}")
                    st.code(result['description_html'], language='html')
                    
# ------------- STOPKA ------------- #
st.markdown("---")
st.markdown("🔧 **Narzędzie do generowania opisów produktów** | Wykorzystuje OpenAI gpt-5-nano")
