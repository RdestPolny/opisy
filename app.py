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
    """Pobiera tytuł, opis i opinie z Lubimy Czytac z losowym user-agentem mobilnym."""
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
        
        # Pobieranie tytułu książki z <h1 class="book__title">
        title_tag = soup.find('h1', class_='book__title')
        title = title_tag.get_text(strip=True) if title_tag else ''
        
        # Pobieranie opisu książki
        description_div = soup.find('div', id='book-description')
        description = description_div.get_text(strip=True) if description_div else ''
        
        # Pobieranie opinii
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
    """
    Pobiera dane ze strony taniaksiazka.pl:
      - Tytuł (z <h1>)
      - Szczegóły (z <div id="szczegoly"> lub <div class="product-features">)
      - Opis (najpierw z <article>, potem fallbacki)
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = bs(response.text, 'html.parser')
        
        # Tytuł
        title_tag = soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else ''
        
        # Szczegóły
        details_text = ""
        details_div = soup.find("div", id="szczegoly") or soup.find("div", class_="product-features")
        if details_div:
            ul = details_div.find("ul", class_="bullet") or details_div.find("ul")
            if ul:
                li_elements = ul.find_all("li")
                details_list = [li.get_text(separator=" ", strip=True) for li in li_elements]
                details_text = "\n".join(details_list)
        
        # Opis — najpierw <article>
        description_text = ""
        description_div = soup.find("article")
        if description_div:
            description_text = description_div.get_text(separator="\n", strip=True)
        else:
            # Fallback: desc-container lub product-description
            description_div = soup.find("div", class_="desc-container") or soup.find("div", id="product-description")
            if description_div:
                description_text = description_div.get_text(separator="\n", strip=True)
        
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



def generate_description_lubimyczytac(book_data, prompt_template):
    """
    Generuje nowy opis na podstawie danych z Lubimy Czytac.
    W miejsce placeholderów {lubimy_title}, {lubimy_description} i {lubimy_reviews} w prompt_template wstawiane są dane.
    """
    try:
        prompt_filled = prompt_template.format(
            lubimy_title=book_data.get('title', ''),
            lubimy_description=book_data.get('description', ''),
            lubimy_reviews=book_data.get('reviews', '')
        )
        messages = [
            {
                "role": "system",
                "content": "Jesteś profesjonalnym copywriterem specjalizującym się w tworzeniu opisów książek. Twórz angażujące opisy w HTML z wykorzystaniem tagów <h2>, <p>, <b>, <ul>, <li>."
            },
            {
                "role": "user",
                "content": prompt_filled
            }
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

def generate_description_taniaksiazka(book_data, prompt_template):
    """
    Generuje nowy opis produktu na podstawie danych ze strony taniaksiazka.pl.
    W miejsce placeholderów {taniaksiazka_title}, {taniaksiazka_details} oraz {taniaksiazka_description} w prompt_template wstawiane są dane.
    """
    try:
        prompt_filled = prompt_template.format(
            taniaksiazka_title=book_data.get('title', ''),
            taniaksiazka_details=book_data.get('details', ''),
            taniaksiazka_description=book_data.get('description', '')
        )
        messages = [
            {
                "role": "system",
                "content": "Jesteś doświadczonym copywriterem specjalizującym się w tworzeniu opisów produktów dla księgarni internetowej."
            },
            {
                "role": "user",
                "content": prompt_filled
            }
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
    """
    Generuje meta title oraz meta description na podstawie danych produktu.
    Jeśli dostępne, używa zarówno 'details' jak i 'description' (lub tylko 'description' dla Lubimy Czytac).
    Meta title powinien zaczynać się od silnego słowa kluczowego i zawierać do 60 znaków.
    Meta description to jedno zdanie informacyjne, do 160 znaków.
    """
    try:
        title = product_data.get('title', '')
        details = product_data.get('details', '')
        description = product_data.get('description', '')
        prompt_meta = f"""Jako doświadczony copywriter SEO, stwórz meta title oraz meta description dla produktu o tytule "{title}" bazując na następujących danych: {details} {description}. Meta title powinien zaczynać się od silnego słowa kluczowego, zawierać do 60 znaków, a meta description powinien być jednym zdaniem informacyjnym, zawierającym do 160 znaków. Podaj wynik w formacie:
Meta title: [treść]
Meta description: [treść]"""
        messages = [
            {
                "role": "system",
                "content": "Jesteś doświadczonym copywriterem SEO."
            },
            {
                "role": "user",
                "content": prompt_meta
            }
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
# Domyślne prompt'y z unikalnymi zmiennymi
# ------------------------#

default_prompt_lubimyczytac = """Jako autor opisów w księgarni internetowej, twoim zdaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{lubimy_title}". Oto informacje, na których powinieneś bazować: {lubimy_description} oraz opinie czytelników, które będą Ci potrzebne: {lubimy_reviews}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:

Dane:
Tytuł książki: {lubimy_title}
Opis książki: {lubimy_description}
Opinie czytelników: {lubimy_reviews}

Opis powinien zawierać:
1. Zawiera sekcje:
   <h2> z kreatywnym hasłem nawiązującym do treści książki.</h2>
   <p>Wprowadzenie mówiące ogólnie o tym czym jest ta książka</p>
   <p>Szczegółowy opis fabuły/treści z <b>wyróżnionymi</b> słowami kluczowymi (wykorzystaj informacje z opinii i starego opisu aby poznać fabułę</p>
   <p>Wartości i korzyści dla czytelnika</p>
   <p>Podsumowanie opinii czytelników {lubimy_reviews} z konkretnymi przykładami (nie używaj imion autorów)</p>
   <h3>Przekonujący call to action</h3>
2. Wykorzystuje opinie czytelników, aby:
   - Podkreślić najczęściej wymieniane zalety książki
   - Wzmocnić wiarygodność opisu
   - Dodać emocje i autentyczność
3. Formatowanie:
   - Używaj tagów HTML: <h2>, <p>, <b>, <ul>, <li>
   - Wyróżniaj kluczowe frazy za pomocą <b>
   - Nie używaj znaczników Markdown, tylko HTML
   - Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
   - Opis ma być angażujący, ale profesjonalny
   - Używaj słownictwa dostosowanego do gatunku książki
   - Unikaj powtórzeń
   - Zachowaj spójność tonu
5. Przykład formatu:
```html
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
```"""

default_prompt_taniaksiazka = """Jako autor opisów w księgarni internetowej, twoim zdaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:

1. Zawiera sekcje:
   <h2> z kreatywnym hasłem nawiązującym do przedmiotu nauki, z którym związany jest podręcznik oraz jego targetem np. dla uczniów 2 klasy szkoły podstawowej.
   <p>Wprowadzenie z opisem tego, czym jest dany podręcznik / ćwiczenie / zeszyt ćwiczeń itd. (w zależności od tego, czym jest dany tytuł), informacje na temat jego zawartości, docelowego targetu i tym, co uznasz za stosowne do opisania w kluczowym pierwszym akapicie.</p>
   <p>Zalety / szczególne cechy warte podkreślenia, z <b>wyróżnionymi</b> słowami kluczowymi</p>
   <p>Wartości i korzyści dla ucznia</p>
   <p>Podsumowanie</p>
   <h3>Przekonujący call to action</h3>
2. Wykorzystuje pobrane informacje, aby:
   - Podkreślić najczęściej wymieniane zalety książki
   - Wzmocnić wiarygodność opisu
3. Formatowanie:
   - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
   - Wyróżniaj kluczowe frazy lub informacje godne wzmocnienia za pomocą <b>
   - Nie używaj znaczników Markdown, tylko HTML
   - Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
   - Opis ma być angażujący, ale profesjonalny
   - Używaj słownictwa dostosowanego do odbiorcy
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

default_prompt_gry_planszowe = """Jako autor opisów w księgarni internetowej, twoim zdaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:

Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter gry planszowej oraz wskazuje na grupę docelową, np. dla miłośników strategii i rozgrywek rodzinnych.
1. Zawiera sekcje:
    <p>Wprowadzenie, które przedstawia grę, jej tematykę, mechanikę (jeśli masz na jej temat informacje w pobranych danych) oraz główne cechy, takie jak czas rozgrywki i poziom trudności.</p>
    <p>Opis rozgrywki z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak interakcja, strategia i rywalizacja. (trzymaj się informacji jakie pobrałeś z dotychczasowego opisu, jeśli nie wiesz jaka jest mechanika lub na czym polegają zasady, to nie pisz o nich szczegółowo, żeby nie wprowadzić nikogo w błąd)</p>
    <p>Korzyści dla graczy, np. rozwój umiejętności logicznego myślenia, budowanie relacji rodzinnych oraz doskonała zabawa.</p>
    <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta gra planszowa jest wyjątkowa.</p>
    <h3>Przekonujący call to action</h3>
2. Wykorzystuje pobrane informacje, aby:
    - Podkreślić najważniejsze cechy gry planszowej
    - Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
  - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
  - Wyróżniaj kluczowe frazy za pomocą <b>
  - Nie używaj znaczników Markdown, tylko HTML
  - Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
  - Opis ma być angażujący, ale profesjonalny
  - Używaj słownictwa dostosowanego do miłośników gier planszowych
  - Unikaj powtórzeń
  - Zachowaj spójność tonu
Przykład formatu:

<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>

"""

default_prompt_beletrystyka = """Jako autor opisów w księgarni internetowej, twoim zdaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produkty, który jest książką o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:

    Zawiera sekcje: <h2> z kreatywnym hasłem nawiązującym do treści książki.</h2> <p>Wprowadzenie mówiące ogólnie o tym czym jest ta książka</p> <p>Opis fabuły/treści z <b>wyróżnionymi</b> słowami kluczowymi (wykorzystaj dostępne informacje aby poznać fabułę, staraj się nie wymyślać szczegółów, których nie jesteś pewny)</p> <p>Wartości i korzyści dla czytelnika</p> <p>Podsumowanie opinii czytelników (opisz co się podobało czytelnikom w danej pozycji, co chwalą itd.)</p> <h3>Przekonujący call to action</h3>
    Wykorzystuje opinie czytelników, aby:
        Podkreślić najczęściej wymieniane zalety książki
        Wzmocnić wiarygodność opisu
        Dodać emocje i autentyczność
    Formatowanie:
        Używaj tagów HTML: <h2>, <p>, <b>, <h3>
        Wyróżniaj kluczowe frazy za pomocą <b>
        Nie używaj znaczników Markdown, tylko HTML
        Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
    Styl:
        Opis ma być angażujący, ale profesjonalny
        Używaj słownictwa dostosowanego do gatunku książki
        Unikaj powtórzeń
        Zachowaj spójność tonu
    Przykład formatu:

<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
""" 

default_prompt_zabawki = """Jako autor opisów w księgarni internetowej, twoim zdaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{taniaksiazka_title}". Oto informacje, na których powinieneś bazować: {taniaksiazka_details} {taniaksiazka_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:

Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter zabawki oraz wskazuje na grupę docelową, np. dla dzieci w wieku 3-7 lat lub dla entuzjastów interaktywnych zabawek.
1. Zawiera sekcje:
    <p>Wprowadzenie, które przedstawia zabawkę, jej funkcjonalność, przeznaczenie oraz główne cechy, takie jak bezpieczeństwo i rozwój kreatywności.</p>
    <p>Opis działania lub interakcji z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak interaktywność, edukacyjność i doskonała zabawa.</p>
    <p>Korzyści dla dzieci, np. rozwój wyobraźni, zdolności manualnych oraz wspomaganie nauki poprzez zabawę.</p>
    <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta zabawka jest wyjątkowa.</p>
    <h3>Przekonujący call to action</h3>
2. Wykorzystuje pobrane informacje, aby:
    - Podkreślić najważniejsze cechy zabawki
    - Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
  - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
  - Wyróżniaj kluczowe frazy za pomocą <b>
  - Nie używaj znaczników Markdown, tylko HTML
  - Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
  - Opis ma być angażujący, ale profesjonalny
  - Używaj słownictwa dostosowanego do odbiorców poszukujących zabawek
  - Unikaj powtórzeń
  - Zachowaj spójność tonu
Przykład formatu:

<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

# ------------------------#
# Sidebar – wybór promptu
# ------------------------#

selected_prompt = st.sidebar.selectbox("Wybierz prompt", [
    "LC - książki", 
    "TK - Podręczniki", 
    "TK - gry planszowe", 
    "TK - beletrystyka", 
    "TK - Zabawki",
    "TK - Podręczniki - Manual"
])

if selected_prompt == "LC - książki":
    st.sidebar.markdown("**Opis:** Prompt przeznaczony do tworzenia angażujących opisów książek, oparty na danych z Lubimy Czytać.")
elif selected_prompt == "TK - Podręczniki":
    st.sidebar.markdown("**Opis:** Prompt do opisu podręczników szkolnych, który uwzględnia specyfikę treści edukacyjnych.")
elif selected_prompt == "TK - gry planszowe":
    st.sidebar.markdown("**Opis:** Prompt do opisu gier planszowych, skupiający się na mechanice, emocjach i unikalnych cechach rozgrywki.")
elif selected_prompt == "TK - beletrystyka":
    st.sidebar.markdown("**Opis:** Prompt do opisu beletrystyki książkowej, wykorzystujący dane z Tania Ksiażka.")
elif selected_prompt == "TK - Zabawki":
    st.sidebar.markdown("**Opis:** Prompt do opisu zabawek, oparty na strukturze opisu dla gier planszowych, dostosowany do cech i funkcjonalności zabawek.")
elif selected_prompt == "TK - Podręczniki - Manual":
    st.sidebar.markdown("**Opis:** Prompt do opisu podręczników, gdzie informacje o książce wprowadza użytkownik ręcznie.")

# ------------------------#
# Główna część aplikacji
# ------------------------#

st.title('Generator Opisów Książek')
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# Formularz dla ręcznego wprowadzania informacji o książce (tylko dla "TK - Podręczniki - Manual")
if selected_prompt == "TK - Podręczniki - Manual":
    with st.form("manual_form"):
        book_info = st.text_area('Informacje o książce:')
        submit_manual = st.form_submit_button("Generuj opis")
    if submit_manual:
        if book_info.strip():
            # Wykorzystujemy te same dane dla wszystkich placeholderów
            book_data_manual = {
                'title': book_info,
                'details': book_info,
                'description': book_info
            }
            new_description = generate_description_taniaksiazka(book_data_manual, default_prompt_taniaksiazka)
            st.markdown("### Wygenerowany opis podręcznika:")
            st.markdown(new_description, unsafe_allow_html=True)
        else:
            st.warning("Proszę wprowadzić informacje o książce.")
else:
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
                status_text.info(f'Przetwarzanie {idx+1}/{len(urls)}...')
                progress_bar.progress((idx + 1) / len(urls))
                url_lower = url.lower()
                
                # Dla Lubimy Czytac – oczekiwany prompt to "LC - książki"
                if "lubimyczytac" in url_lower:
                    if selected_prompt != "LC - książki":
                        st.error(f"Wybrano prompt '{selected_prompt}', ale URL '{url}' pochodzi z Lubimy Czytać. Pomijam ten URL.")
                        continue
                    book_data = get_lubimyczytac_data(url)
                    if book_data.get('error'):
                        st.error(f"Błąd dla {url}: {book_data['error']}")
                        continue
                    new_description = generate_description_lubimyczytac(book_data, default_prompt_lubimyczytac)
                    meta_title, meta_description = ("", "")
                    if generate_meta:
                        meta_title, meta_description = generate_meta_tags(book_data)
                    results.append({
                        'URL': url,
                        'Tytuł': book_data.get('title', ''),
                        'Stary opis': book_data.get('description', ''),
                        'Opinie': book_data.get('reviews', ''),
                        'Nowy opis': new_description,
                        'Meta title': meta_title,
                        'Meta description': meta_description
                    })
                # Dla taniaksiazka.pl – oczekiwany prompt to "TK - Podręczniki", "TK - gry planszowe", "TK - beletrystyka" lub "TK - Zabawki"
                elif "taniaksiazka.pl" in url_lower:
                    if selected_prompt not in ["TK - Podręczniki", "TK - gry planszowe", "TK - beletrystyka", "TK - Zabawki"]:
                        st.error(f"Wybrano prompt '{selected_prompt}', ale URL '{url}' pochodzi z taniaksiazka.pl. Pomijam ten URL.")
                        continue
                    book_data = get_taniaksiazka_data(url)
                    if book_data.get('error'):
                        st.error(f"Błąd dla {url}: {book_data['error']}")
                        continue
                    if selected_prompt == "TK - Podręczniki":
                        prompt_used = default_prompt_taniaksiazka
                    elif selected_prompt == "TK - gry planszowe":
                        prompt_used = default_prompt_gry_planszowe
                    elif selected_prompt == "TK - beletrystyka":
                        prompt_used = default_prompt_beletrystyka
                    elif selected_prompt == "TK - Zabawki":
                        prompt_used = default_prompt_zabawki
                    new_description = generate_description_taniaksiazka(book_data, prompt_used)
                    meta_title, meta_description = ("", "")
                    if generate_meta:
                        meta_title, meta_description = generate_meta_tags(book_data)
                    results.append({
                        'URL': url,
                        'Tytuł': book_data.get('title', ''),
                        'Szczegóły': book_data.get('details', ''),
                        'Opis': book_data.get('description', ''),
                        'Nowy opis': new_description,
                        'Meta title': meta_title,
                        'Meta description': meta_description
                    })
                else:
                    st.error(f"Nieobsługiwana domena dla {url}")
                    continue
                    
                time.sleep(3)  # Ograniczenie częstotliwości zapytań
                
            if results:
                df = pd.DataFrame(results)
                st.dataframe(df, use_container_width=True)
                
                csv = df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="Pobierz dane",
                    data=csv,
                    file_name='wygenerowane_opisy.csv',
                    mime='text/csv'
                )
            else:
                st.warning("Nie udało się wygenerować żadnych opisów")
        else:
            st.warning("Proszę wprowadzić adresy URL.")
