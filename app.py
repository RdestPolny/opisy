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

prompt_romans = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

Opis powinien:

1. Zawierać sekcje:
   <h2> z romantycznym hasłem nawiązującym do miłości, emocji i relacji.</h2>
   <p>Wprowadzenie o tym, czym jest ta historia miłosna, dla kogo jest przeznaczona.</p>
   <p>Opis fabuły z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi uczucia i zwroty akcji.</p>
   <p>Korzyści emocjonalne dla czytelnika — jakie wartości daje książka.</p>
   <p>Podsumowanie zbudowane na emocjach.</p>
   <h3>Przekonujący call to action</h3>
2. Wykorzystuje pobrane informacje, aby:
   - Podkreślić główne zalety książki
   - Wzmocnić wiarygodność opisu
3. Formatowanie:
   - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
   - Wyróżniaj najważniejsze frazy za pomocą <b>
   - Nie używaj znaczników Markdown, tylko HTML
   - Nie dodawaj komentarzy ani wyjaśnień
4. Styl:
   - Opis ma być romantyczny, emocjonalny i wciągający
   - Dostosowany do czytelników romansów
   - Unikaj powtórzeń
   - Zachowaj spójność tonu
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_kryminal = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, wciągającego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

Opis powinien:

1. Zawierać sekcje:
   <h2> z intrygującym hasłem budującym napięcie.</h2>
   <p>Wprowadzenie do tajemniczej historii, dla kogo książka jest przeznaczona.</p>
   <p>Opis fabuły z <b>wyróżnionymi</b> elementami zagadki, niespodziewanych zwrotów i napięcia.</p>
   <p>Korzyści dla miłośników kryminałów — adrenalina, emocje, dedukcja.</p>
   <p>Podsumowanie i wzbudzenie ciekawości.</p>
   <h3>Przekonujący call to action</h3>
2. Wykorzystuje pobrane informacje, aby:
   - Podkreślić główne atuty książki
   - Zbudować napięcie
3. Formatowanie:
   - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
   - Wyróżniaj kluczowe frazy
   - Nie używaj Markdown
   - Nie dodawaj komentarzy ani wyjaśnień
4. Styl:
   - Mroczny, tajemniczy, wciągający
   - Dostosowany do fanów kryminałów
   - Unikaj powtórzeń
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_reportaz = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, przekonującego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

Opis powinien:

1. Zawierać sekcje:
   <h2> z hasłem oddającym prawdziwą historię lub główny temat.</h2>
   <p>Wprowadzenie mówiące o kontekście książki, dla kogo jest przeznaczona.</p>
   <p>Opis zawartości z <b>wyróżnionymi</b> faktami i tematami, które porusza.</p>
   <p>Korzyści — wiedza, głębsze spojrzenie na świat.</p>
   <p>Podsumowanie i zachęta do refleksji.</p>
   <h3>Call to action</h3>
2. Wykorzystuje dane, aby:
   - Podkreślić unikalność reportażu
   - Wzmocnić autentyczność
3. Formatowanie:
   - Tylko HTML
   - Wyróżniaj ważne słowa
   - Nie używaj Markdown
   - Bez komentarzy
4. Styl:
   - Rzetelny, autentyczny, informacyjny
   - Unikaj powtórzeń
   - Zachowaj spójność tonu
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_young_adult = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie angażującego opisu książki "{taniaksiazka_title}". Informacje: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML.

Opis powinien:

1. Zawierać sekcje:
   <h2> z chwytliwym hasłem dla młodzieży.</h2>
   <p>Wprowadzenie do świata książki, grupy docelowej.</p>
   <p>Opis fabuły z <b>wyróżnionymi</b> przygodami, emocjami i wątkami rozwojowymi.</p>
   <p>Korzyści — rozrywka, inspiracja, rozwój postaci.</p>
   <p>Podsumowanie w energetycznym tonie.</p>
   <h3>Przekonujący call to action</h3>
2. Wykorzystuje dane, aby:
   - Pokazać dynamikę fabuły
   - Wzmocnić autentyczność
3. Formatowanie:
   - Tylko HTML
   - Wyróżniaj ważne frazy
4. Styl:
   - Lekki, nowoczesny, młodzieżowy
   - Unikaj powtórzeń
   - Zachowaj spójność
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_beletrystyka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego opisu książki "{taniaksiazka_title}". Informacje: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML.

Opis powinien:

1. Zawierać sekcje:
   <h2> z literackim hasłem oddającym klimat książki.</h2>
   <p>Wprowadzenie do fabuły, ogólny kontekst.</p>
   <p>Opis treści z <b>wyróżnionymi</b> wątkami i tematami przewodnimi.</p>
   <p>Korzyści emocjonalne i intelektualne.</p>
   <p>Podsumowanie, refleksja.</p>
   <h3>Call to action</h3>
2. Wykorzystuje dane, aby:
   - Podkreślić wartość literacką
3. Formatowanie:
   - HTML
   - Wyróżniaj kluczowe frazy
4. Styl:
   - Literacki, spójny
   - Unikaj powtórzeń
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_fantastyka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie epickiego opisu książki fantasy "{taniaksiazka_title}". Informacje: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML.

Opis powinien:

1. Zawierać sekcje:
   <h2> z magicznym hasłem zachęcającym do podróży po fantastycznych światach.</h2>
   <p>Wprowadzenie do świata fantasy, klimatu książki.</p>
   <p>Opis przygód i bohaterów z <b>wyróżnionymi</b> elementami magii i niezwykłości.</p>
   <p>Korzyści — ucieczka od codzienności, rozwój wyobraźni.</p>
   <p>Podsumowanie z mistycznym akcentem.</p>
   <h3>Call to action</h3>
2. Wykorzystuje dane, aby:
   - Oddać klimat fantasy
3. Formatowanie:
   - HTML
   - Wyróżniaj kluczowe frazy
4. Styl:
   - Epicki, pełen magii
   - Spójny, bez powtórzeń
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""
prompt_scifi = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie futurystycznego opisu książki science fiction "{taniaksiazka_title}". Informacje: {taniaksiazka_details} {taniaksiazka_description}. Stwórz opis w HTML.

Opis powinien:

1. Zawierać sekcje:
   <h2> z hasłem o przyszłości, odkryciach i technologiach.</h2>
   <p>Wprowadzenie do świata sci-fi, kontekstu książki.</p>
   <p>Opis fabuły i technologii z <b>wyróżnionymi</b> futurystycznymi elementami.</p>
   <p>Korzyści — inspiracja, rozbudzenie wyobraźni.</p>
   <p>Podsumowanie, wzbudzenie ciekawości o przyszłość.</p>
   <h3>Call to action</h3>
2. Wykorzystuje dane, aby:
   - Oddać klimat sci-fi
3. Formatowanie:
   - HTML
   - Wyróżniaj ważne frazy
4. Styl:
   - Futurystyczny, dynamiczny
   - Spójny, bez powtórzeń
5. Przykład formatu:

```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

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
