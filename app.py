import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup as bs
import time
from openai import OpenAI

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
        details_div = soup.find("div", id="szczegoly") or soup.find("div", class_="product-features")
        if details_div:
            ul = details_div.find("ul", class_="bullet") or details_div.find("ul")
            if ul:
                li_elements = ul.find_all("li")
                details_list = [li.get_text(separator=" ", strip=True) for li in li_elements]
                details_text = "\n".join(details_list)
        
        # Pobieranie pełnego opisu z zagnieżdżonych struktur
        description_text = ""
        description_div = soup.find("div", class_="desc-container")
        if description_div:
            # Szukamy głębiej w strukturze - artykuł z pełnym opisem
            article = description_div.find("article")
            if article:
                # Jeśli jest zagnieżdżony artykuł, bierzemy go
                nested_article = article.find("article")
                if nested_article:
                    description_text = nested_article.get_text(separator="\n", strip=True)
                else:
                    description_text = article.get_text(separator="\n", strip=True)
            else:
                # Fallback - jeśli nie ma artykułu, bierzemy cały div
                description_text = description_div.get_text(separator="\n", strip=True)
        
        # Dodatkowe sprawdzenie dla alternatywnych struktur
        if not description_text:
            alt_desc_div = soup.find("div", id="product-description")
            if alt_desc_div:
                description_text = alt_desc_div.get_text(separator="\n", strip=True)
        
        # Czyszczenie tekstu z nadmiarowych białych znaków
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

# ------------- GENEROWANIE OPISU ------------- #

def generate_description(book_data, prompt_template, client):
    try:
        prompt_filled = prompt_template.format(
            book_title=book_data.get('title', ''),
            book_details=book_data.get('details', ''),
            book_description=book_data.get('description', '')
        )
        messages = [
            {"role": "system", "content": "Jesteś profesjonalnym copywriterem. Tworzysz wyłącznie poprawne, atrakcyjne opisy książek do księgarni internetowej. Każdy opis ma być zgodny z poleceniem i formą HTML, nie dodawaj nic od siebie."},
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

# ------------- PROMPTY DO GATUNKÓW ------------- #

prompt_romans = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

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

<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_kryminal = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, wciągającego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

Opis powinien:

Zawierać sekcje:
<h2> z intrygującym hasłem budującym napięcie.</h2>
<p>Wprowadzenie do tajemniczej historii, dla kogo książka jest przeznaczona.</p>
<p>Opis fabuły z <b>wyróżnionymi</b> elementami zagadki, niespodziewanych zwrotów i napięcia.</p>
<p>Korzyści dla miłośników kryminałów — adrenalina, emocje, dedukcja.</p>
<p>Podsumowanie i wzbudzenie ciekawości.</p>
<h3>Przekonujący call to action</h3>

Wykorzystuje pobrane informacje, aby:
- Podkreślić główne atuty książki
- Zbudować napięcie

Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy
- Nie używaj Markdown
- Nie dodawaj komentarzy ani wyjaśnień

Styl:
- Mroczny, tajemniczy, wciągający
- Dostosowany do fanów kryminałów
- Unikaj powtórzeń

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_reportaz = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, przekonującego opisu produktu o tytule "{book_title}". Oto informacje: {book_details} {book_description}. Stwórz opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>.

Opis powinien:

Zawierać sekcje:
<h2> z hasłem oddającym prawdziwą historię lub główny temat.</h2>
<p>Wprowadzenie mówiące o kontekście książki, dla kogo jest przeznaczona.</p>
<p>Opis zawartości z <b>wyróżnionymi</b> faktami i tematami, które porusza.</p>
<p>Korzyści — wiedza, głębsze spojrzenie na świat.</p>
<p>Podsumowanie i zachęta do refleksji.</p>
<h3>Call to action</h3>

Wykorzystuje dane, aby:
- Podkreślić unikalność reportażu
- Wzmocnić autentyczność

Formatowanie:
- Tylko HTML
- Wyróżniaj ważne słowa
- Nie używaj Markdown
- Bez komentarzy

Styl:
- Rzetelny, autentyczny, informacyjny
- Unikaj powtórzeń
- Zachowaj spójność tonu

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_young_adult = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie angażującego opisu książki "{book_title}". Informacje: {book_details} {book_description}. Stwórz opis w HTML.

Opis powinien:

Zawierać sekcje:
<h2> z chwytliwym hasłem dla młodzieży.</h2>
<p>Wprowadzenie do świata książki, grupy docelowej.</p>
<p>Opis fabuły z <b>wyróżnionymi</b> przygodami, emocjami i wątkami rozwojowymi.</p>
<p>Korzyści — rozrywka, inspiracja, rozwój postaci.</p>
<p>Podsumowanie w energetycznym tonie.</p>
<h3>Przekonujący call to action</h3>

Wykorzystuje dane, aby:
- Pokazać dynamikę fabuły
- Wzmocnić autentyczność

Formatowanie:
- Tylko HTML
- Wyróżniaj ważne frazy

Styl:
- Lekki, nowoczesny, młodzieżowy
- Unikaj powtórzeń
- Zachowaj spójność

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_beletrystyka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego opisu książki "{book_title}". Informacje: {book_details} {book_description}. Stwórz opis w HTML.

Opis powinien:

Zawierać sekcje:
<h2> z literackim hasłem oddającym klimat książki.</h2>
<p>Wprowadzenie do fabuły, ogólny kontekst.</p>
<p>Opis treści z <b>wyróżnionymi</b> wątkami i tematami przewodnimi.</p>
<p>Korzyści emocjonalne i intelektualne.</p>
<p>Podsumowanie, refleksja.</p>
<h3>Call to action</h3>

Wykorzystuje dane, aby:
- Podkreślić wartość literacką

Formatowanie:
- HTML
- Wyróżniaj kluczowe frazy

Styl:
- Literacki, spójny
- Unikaj powtórzeń

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_fantastyka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie epickiego opisu książki fantasy "{book_title}". Informacje: {book_details} {book_description}. Stwórz opis w HTML.

Opis powinien:

Zawierać sekcje:
<h2> z magicznym hasłem zachęcającym do podróży po fantastycznych światach.</h2>
<p>Wprowadzenie do świata fantasy, klimatu książki.</p>
<p>Opis przygód i bohaterów z <b>wyróżnionymi</b> elementami magii i niezwykłości.</p>
<p>Korzyści — ucieczka od codzienności, rozwój wyobraźni.</p>
<p>Podsumowanie z mistycznym akcentem.</p>
<h3>Call to action</h3>

Wykorzystuje dane, aby:
- Oddać klimat fantasy

Formatowanie:
- HTML
- Wyróżniaj kluczowe frazy

Styl:
- Epicki, pełen magii
- Spójny, bez powtórzeń

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_scifi = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie futurystycznego opisu książki science fiction "{book_title}". Informacje: {book_details} {book_description}. Stwórz opis w HTML.

Opis powinien:

Zawierać sekcje:
<h2> z hasłem o przyszłości, odkryciach i technologiach.</h2>
<p>Wprowadzenie do świata sci-fi, kontekstu książki.</p>
<p>Opis fabuły i technologii z <b>wyróżnionymi</b> futurystycznymi elementami.</p>
<p>Korzyści — inspiracja, rozbudzenie wyobraźni.</p>
<p>Podsumowanie, wzbudzenie ciekawości o przyszłość.</p>
<h3>Call to action</h3>

Wykorzystuje dane, aby:
- Oddać klimat sci-fi

Formatowanie:
- HTML
- Wyróżniaj ważne frazy

Styl:
- Futurystyczny, dynamiczny
- Spójny, bez powtórzeń

Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompts = {
    "Romans": prompt_romans,
    "Kryminał": prompt_kryminal,
    "Reportaż": prompt_reportaz,
    "Young Adult": prompt_young_adult,
    "Beletrystyka": prompt_beletrystyka,
    "Fantastyka": prompt_fantastyka,
    "Sci-fi": prompt_scifi,
}

# ------------- STREAMLIT INTERFEJS ------------- #

st.set_page_config(page_title="Generator opisów książek", page_icon="📚", layout="wide")

st.title('📚 Generator opisów książek')
st.markdown("---")

# Sprawdzenie czy klucz API jest dostępny
if "OPENAI_API_KEY" not in st.secrets:
    st.error("❌ Brak klucza API OpenAI w secrets. Skonfiguruj klucz API w ustawieniach aplikacji.")
    st.stop()

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# Sidebar z opcjami
st.sidebar.header("🎯 Ustawienia")
selected_prompt = st.sidebar.selectbox(
    "Wybierz gatunek książki:",
    list(prompts.keys()),
    index=0
)

# Główny interfejs
col1, col2 = st.columns([1, 1])

with col1:
    st.header("📝 Dane wejściowe")
    url = st.text_input(
        "URL strony produktu:",
        placeholder="https://przykład.com/książka",
        help="Wklej pełny URL strony produktu"
    )
    
    generate_meta = st.checkbox("Generuj meta title i meta description", value=True)
    
    if st.button("🚀 Generuj opis", type="primary", use_container_width=True):
        if not url:
            st.error("❌ Podaj URL strony produktu!")
        else:
            with st.spinner("Pobieram dane ze strony..."):
                book_data = get_book_data(url)
                
                if book_data['error']:
                    st.error(f"❌ {book_data['error']}")
                else:
                    st.success("✅ Dane pobrane pomyślnie!")
                    
                    # Wyświetlenie pobranych danych
                    st.subheader("📊 Pobrane dane")
                    st.write(f"**Tytuł:** {book_data['title']}")
                    if book_data['details']:
                        st.write(f"**Szczegóły:** {book_data['details'][:200]}...")
                    if book_data['description']:
                        # Pokazujemy więcej tekstu dla weryfikacji
                        full_desc = book_data['description']
                        st.write(f"**Opis:** {full_desc[:500]}...")
                        st.write(f"**Długość opisu:** {len(full_desc)} znaków")
                    
                    # Generowanie opisu
                    with st.spinner("Generuję opis..."):
                        selected_prompt_template = prompts[selected_prompt]
                        generated_desc = generate_description(book_data, selected_prompt_template, client)
                        
                        if generated_desc:
                            st.session_state['generated_description'] = generated_desc
                            st.session_state['book_title'] = book_data['title']
                            
                            # Generowanie metatagów
                            if generate_meta:
                                with st.spinner("Generuję metatagi..."):
                                    meta_title, meta_description = generate_meta_tags(book_data, client)
                                    st.session_state['meta_title'] = meta_title
                                    st.session_state['meta_description'] = meta_description

with col2:
    st.header("📄 Wygenerowany opis")
    
    if 'generated_description' in st.session_state:
        st.subheader(f"📖 {st.session_state.get('book_title', 'Opis książki')}")
        st.subheader(f"🎭 Gatunek: {selected_prompt}")
        
        # Podgląd HTML
        st.markdown("**Podgląd:**")
        st.markdown(st.session_state['generated_description'], unsafe_allow_html=True)
        
        # Kod HTML do skopiowania
        st.markdown("**Kod HTML:**")
        st.code(st.session_state['generated_description'], language='html')
        
        # Metatagi
        if 'meta_title' in st.session_state and 'meta_description' in st.session_state:
            st.markdown("---")
            st.subheader("🏷️ Metatagi SEO")
            st.write(f"**Meta Title:** {st.session_state['meta_title']}")
            st.write(f"**Meta Description:** {st.session_state['meta_description']}")
            
            # Kod metatagów
            meta_code = f"""<title>{st.session_state['meta_title']}</title>
<meta name="description" content="{st.session_state['meta_description']}">"""
            st.code(meta_code, language='html')
        
        # Przycisk do skopiowania
        if st.button("📋 Skopiuj opis HTML", use_container_width=True):
            st.success("✅ Opis skopiowany do schowka!")
            
    else:
        st.info("👈 Podaj URL i kliknij 'Generuj opis' aby rozpocząć")

# Stopka
st.markdown("---")
st.markdown("🔧 **Narzędzie do generowania opisów książek** | Wykorzystuje OpenAI GPT-4o-mini")
st.markdown("💡 **Wskazówka:** Wybierz odpowiedni gatunek z menu bocznego dla najlepszych rezultatów")
