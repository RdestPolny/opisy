import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
from openai import OpenAI

# ------------------------#
# Definicje funkcji — globalne
# ------------------------#

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

prompt_romans = """Jako copywriter, przygotuj angażujący opis w HTML dla książki "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Użyj <h2>, <p>, <b>, <ul>, <li>. Zadbaj o romantyczny, emocjonalny ton. 
Sekcje: nagłówek <h2>, wprowadzenie <p>, opis fabuły <p> z <b>ważnymi</b> słowami, korzyści, podsumowanie i call to action <h3>. 
Używaj tylko HTML."""

prompt_kryminal = """Przygotuj opis w HTML dla książki kryminalnej "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Styl: mroczny, pełen napięcia. Sekcje: <h2>, <p>, <b>. Uwzględnij intrygę, zagadki i nieoczywiste zwroty. 
Na końcu <h3> zachęta do zakupu."""

prompt_reportaz = """Stwórz opis w HTML dla książki reportażowej "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Ton: rzetelny, wiarygodny, oparty na faktach. Użyj <h2>, <p>, <b>. Opisz, czego czytelnik się dowie i dlaczego warto przeczytać. 
Dodaj <h3> call to action."""

prompt_young_adult = """Napisz w HTML opis książki Young Adult "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Ton: lekki, dynamiczny, zrozumiały dla młodzieży. Sekcje: <h2>, <p>, <b>, <h3>. 
Podkreśl przygodę, emocje i rozwój bohaterów."""

prompt_beletrystyka = """Napisz opis w HTML dla beletrystyki "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Ton: uniwersalny, literacki. Sekcje: <h2>, <p>, <b>, <h3>. Uwzględnij fabułę, tematykę i wartość emocjonalną."""

prompt_fantastyka = """Przygotuj opis w HTML dla książki fantasy "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Ton: epicki, pełen magii i przygód. Użyj <h2>, <p>, <b>, <h3>. Opisz świat, magię i bohaterów."""

prompt_scifi = """Napisz opis w HTML dla książki science fiction "{taniaksiazka_title}". Dane: {taniaksiazka_details} {taniaksiazka_description}. 
Ton: futurystyczny, inspirujący. Sekcje: <h2>, <p>, <b>, <h3>. Skup się na technologii, przyszłości i odkrywaniu nieznanego."""

system_prompt = "Jesteś doświadczonym copywriterem specjalizującym się w tworzeniu opisów książek w HTML."

# ------------------------#
# Sidebar
# ------------------------#

selected_prompt = st.sidebar.selectbox("Wybierz kategorię", [
    "Romans",
    "Kryminał",
    "Reportaż",
    "Young Adult",
    "Beletrystyka",
    "Fantastyka",
    "Sci-fi"
])

# ------------------------#
# Główna część
# ------------------------#

st.title('Generator Opisów Książek (TaniaKsiazka)')

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

        # Wybór promptu
        if selected_prompt == "Romans":
            prompt_used = prompt_romans
        elif selected_prompt == "Kryminał":
            prompt_used = prompt_kryminal
        elif selected_prompt == "Reportaż":
            prompt_used = prompt_reportaz
        elif selected_prompt == "Young Adult":
            prompt_used = prompt_young_adult
        elif selected_prompt == "Beletrystyka":
            prompt_used = prompt_beletrystyka
        elif selected_prompt == "Fantastyka":
            prompt_used = prompt_fantastyka
        elif selected_prompt == "Sci-fi":
            prompt_used = prompt_scifi
        else:
            prompt_used = prompt_beletrystyka

        for idx, url in enumerate(urls):
            status_text.info(f'Przetwarzanie {idx + 1}/{len(urls)}...')
            progress_bar.progress((idx + 1) / len(urls))

            book_data = get_taniaksiazka_data(url)
            if book_data.get('error'):
                st.error(f"Błąd dla {url}: {book_data['error']}")
                st.stop()  # Zatrzymanie w razie błędu

            new_description = generate_description(book_data, prompt_used, system_prompt)

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
