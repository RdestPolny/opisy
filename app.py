import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
import random
from openai import OpenAI

# ------------------------#
# Definicje funkcji – globalne
# ------------------------#

def get_lubimyczytac_data(url):
    MOBILE_USER_AGENTS = [
        "Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 9; SAMSUNG SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/10.1 Chrome/71.0.3578.99 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
    ]
    headers = {
        'User-Agent': random.choice(MOBILE_USER_AGENTS),
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = bs(response.text, 'html.parser')
        title_tag = soup.find('h1', class_='book__title')
        title = title_tag.get_text(strip=True) if title_tag else ''
        description_div = soup.find('div', id='book-description')
        description = description_div.get_text(strip=True) if description_div else ''
        reviews = []
        for review in soup.select('p.expandTextNoJS.p-expanded.js-expanded'):
            text = review.get_text(strip=True)
            if len(text) > 50:
                reviews.append(text)
        return {
            'title': title,
            'description': description,
            'reviews': "\n\n---\n\n".join(reviews) if reviews else '',
            'error': None
        }
    except Exception as e:
        return {
            'title': '',
            'description': '',
            'reviews': '',
            'error': f"Błąd pobierania: {str(e)}"
        }

def get_taniaksiazka_data(url):
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
        description_div = soup.find("div", class_="desc-container") or soup.find("div", id="product-description")
        if description_div:
            description_text = description_div.get_text(separator="\n", strip=True)
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

def generate_description(book_data, prompt_template, system_prompt):
    try:
        prompt_filled = prompt_template.format(
            taniaksiazka_title=book_data.get('title', ''),
            taniaksiazka_details=book_data.get('details', ''),
            taniaksiazka_description=book_data.get('description', '')
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_filled}
        ]
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Błąd generowania opisu: {str(e)}")
        return ""

def generate_meta_tags(product_data):
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
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=200
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

# ------------------------#
# Prompty — pełne
# ------------------------#

default_prompt_taniaksiazka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>.

Opis powinien zawierać:
1. Nagłówek <h2> z kreatywnym hasłem nawiązującym do tematu.
2. Wprowadzenie <p> czym jest ten produkt, dla kogo jest przeznaczony.
3. Szczegółowy opis z <b>wyróżnionymi</b> słowami kluczowymi.
4. Korzyści i zalety.
5. Podsumowanie z wezwaniem do działania <h3>.

Używaj tylko HTML. Nie dodawaj komentarzy ani wyjaśnień. Nie wymyślaj informacji, jeśli nie są dostępne w opisie lub szczegółach."""

system_prompt_tk = "Jesteś doświadczonym copywriterem specjalizującym się w opisach produktów księgarni online. Piszesz atrakcyjne i poprawne opisy w HTML."

# ------------------------#
# Sidebar
# ------------------------#

selected_prompt = st.sidebar.selectbox("Wybierz prompt", [
    "TK - Podręczniki",
    "TK - gry planszowe",
    "TK - beletrystyka",
    "TK - Zabawki"
])

# ------------------------#
# Główna część
# ------------------------#

st.title('Generator Opisów Produktów (TaniaKsiazka)')

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

with st.form("url_form"):
    urls_input = st.text_area('Wprowadź adresy URL (po jednym w linii):')
    generate_meta = st.checkbox("Generuj też metatagi")
    submit_button = st.form_submit_button("Uruchom")

if submit_button:
    if urls_input:
        urls = [url.strip() for url in urls_input.split('\n') if url.strip()]
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        for idx, url in enumerate(urls):
            status_text.info(f'Przetwarzanie {idx + 1}/{len(urls)}...')
            progress_bar.progress((idx + 1) / len(urls))

            book_data = get_taniaksiazka_data(url)
            if book_data.get('error'):
                st.error(f"Błąd dla {url}: {book_data['error']}")
                st.stop()  # Zatrzymanie w razie błędu
            new_description = generate_description(book_data, default_prompt_taniaksiazka, system_prompt_tk)

            meta_title, meta_description = ("", "")
            if generate_meta:
                meta_title, meta_description = generate_meta_tags(book_data)

            results.append({
                'URL': url,
                'Tytuł': book_data.get('title', ''),
                'Szczegóły': book_data.get('details', ''),
                'Opis oryginalny': book_data.get('description', ''),
                'Nowy opis': new_description,
                'Meta title': meta_title,
                'Meta description': meta_description
            })

            time.sleep(3)

        if results:
            df = pd.DataFrame(results)
            st.dataframe(df, use_container_width=True)

            for row in results:
                with st.expander(f"Pełny oryginalny opis — {row['Tytuł']}"):
                    st.markdown(row['Opis oryginalny'], unsafe_allow_html=True)
                with st.expander(f"Nowy opis — {row['Tytuł']}"):
                    st.markdown(row['Nowy opis'], unsafe_allow_html=True)

            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="Pobierz dane",
                data=csv,
                file_name='wygenerowane_opisy.csv',
                mime='text/csv'
            )
        else:
            st.warning("Nie udało się wygenerować żadnych opisów.")
    else:
        st.warning("Proszę wprowadzić adresy URL.")
