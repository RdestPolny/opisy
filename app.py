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

# ------------- AKENEO API ------------- #
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

    # nie tworzymy nowego produktu – tylko update istniejącego
    if not akeneo_product_exists(sku, token):
        raise ValueError(f"Produkt o SKU '{sku}' nie istnieje w Akeneo.")

    # sprawdź konfigurację atrybutu, żeby poprawnie ustawić scope/locale
    attr = akeneo_get_attribute("description", token)
    is_scopable = bool(attr.get("scopable", False))
    is_localizable = bool(attr.get("localizable", False))

    value_obj = {
        "data": html_description,
        # Dla atrybutu scopable wymagany jest scope; dla nie-scopable -> null
        "scope": channel if is_scopable else None,
        # Dla atrybutu nie-lokalizowalnego locale MUSI być null
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

# ------------- POBIERANIE DANYCH (ZAKTUALIZOWANA FUNKCJA) ------------- #
def get_book_data(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = bs(response.text, 'html.parser')

        title = ''
        details_text = ''
        description_text = ''

        # NOWA LOGIKA DLA SMYK.COM
        if 'smyk.com' in url:
            title_tag = soup.find('h1', {'data-testid': 'product-name'})
            title = title_tag.get_text(strip=True) if title_tag else ''

            # Pobieranie opisu z podanej struktury HTML
            description_div = soup.find("div", {"data-testid": "box-attributes__simple"})
            if description_div:
                description_text = description_div.get_text(separator="\n", strip=True)
            
            # Na smyk.com szczegóły są częścią głównego opisu, więc `details_text` pozostaje pusty.

        # ISTNIEJĄCA LOGIKA DLA INNYCH STRON
        else:
            title_tag = soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else ''

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

        # Wspólne przetwarzanie i zwracanie wyniku
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
            {"role": "system", "content": "Jesteś profesjonalnym copywriterem. Tworzysz wyłącznie poprawne, atrakcyjne opisy książek i produktów do księgarni internetowej. Każdy opis ma być zgodny z poleceniem i formą HTML, nie dodawaj nic od siebie."},
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
prompt_romans = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do miłośników historii o miłości i wzruszających relacji.
1. Zawiera sekcje:
 <p>Wprowadzenie, które przedstawia książkę, jej gatunek (np. romans współczesny, historyczny, obyczajowy), ogólną tematykę i klimat (np. pełen emocji, namiętny, wzruszający, pełen czułości i zaskoczeń), główne cechy, takie jak chemia między bohaterami, intensywne uczucia i wyjątkowa atmosfera. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób, które chcą przeżyć historię pełną miłości, wzruszeń i emocji.</p>
 <p>Opis fabuły z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak miłosne napięcia, nieoczekiwane zwroty akcji, przeszkody na drodze do szczęścia, pełne pasji relacje czy poruszające historie bohaterów. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych o fabule, unikaj zdradzania najważniejszych momentów.)</p>
 <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta książka romans wyróżnia się na tle innych — np. dzięki wyjątkowym postaciom, pełnym pasji relacjom lub zaskakującej fabule.</p>
 <h3>Przekonujący call to action, który zachęca do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
- Podkreślić najważniejsze cechy książki
- Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy za pomocą <b>
- Nie używaj znaczników Markdown, tylko HTML
- Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być emocjonalny, pełen uczuć i wciągający
- Używaj języka, który podkreśla miłość, namiętność i wzruszenia
- Akcentuj relacje między bohaterami, ich emocje, konflikty i rozwój uczuć
- Unikaj ogólników — skup się na unikalnych aspektach relacji i historii (jeśli masz takie informacje)
- Pisząc, miej w głowie czytelnika, który szuka historii pełnej miłości, czułości i wielkich emocji
- Nie bój się podkreślać silnych uczuć: ekscytacji, smutku, nadziei, tęsknoty, radości
- Zachowaj profesjonalny, ale ciepły i angażujący ton
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba, do której kierowany jest opis:
Opis książki romans kierowany jest do czytelnika, który pragnie poczuć magię miłości i przeżyć historię pełną emocji. To osoba wrażliwa, romantyczna, często marzycielska, która szuka w literaturze wzruszeń, pięknych relacji i niezapomnianych momentów. Lubi historie, które pozwalają oderwać się od codzienności, wciągnąć się w losy bohaterów i poczuć całą gamę uczuć — od radości po smutek i nadzieję.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_kryminal = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do miłośników kryminałów.
1. Zawiera sekcje:
   <p>Wprowadzenie, które przedstawia książkę, jej gatunek (kryminał, thriller psychologiczny itp.), ogólną tematykę i klimat (np. mroczny, pełen napięcia, psychologiczny), główne cechy, takie jak intensywne zwroty akcji, wciągająca fabuła oraz psychologiczna głębia postaci. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób szukających historii pełnych intryg i emocji.</p>
   <p>Opis fabuły z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak napięcie, tajemnica, zwroty akcji oraz psychologiczne rozgrywki między bohaterami. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych o fabule, unikaj dokładnych spojlerów fabularnych, żeby nie psuć wrażeń czytelnikowi.)</p>
   <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta książka kryminalna wyróżnia się na tle innych tytułów — np. dzięki mistrzowsko budowanemu napięciu, nieprzewidywalnej fabule czy wyjątkowo wyrazistym postaciom.</p>
   <h3>Przekonujący call to action, który skłania do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
    - Podkreślić najważniejsze cechy książki
    - Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
  - Używaj tagów HTML: <h2>, <p>, <b>, <h3>
  - Wyróżniaj kluczowe frazy za pomocą <b>
  - Nie używaj znaczników Markdown, tylko HTML
  - Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być angażujący i intrygujący
- Używaj języka, który buduje atmosferę tajemnicy i zagadki — operuj słowami kojarzącymi się z intrygą, niepewnością, odkrywaniem sekretów. 
- Akcentuj elementy psychologiczne — odwołuj się do motywacji bohaterów, ich emocji i dylematów moralnych
- Unikaj ogólników — skup się na konkretnych zwrotach akcji, wątkach, unikalnym klimacie danej książki (jeśli masz takie informacje). 
- Pisząc, miej w głowie dorosłego czytelnika , który szuka książki pozwalającej oderwać się od rzeczywistości i zanurzyć w fascynującą historię.
- Nie bój się podkreślać silnych emocji: napięcia, dreszczyku, zaskoczenia, a czasem grozy 
- Zachowaj profesjonalny, ale żywy ton 
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba do której kierowany jest opis:
Opis książki kierowany jest do dorosłego czytelnika, który uwielbia rozwiązywać zagadki i zanurzać się w historie pełne intryg oraz nieoczywistych zwrotów akcji. To osoba ciekawska, poszukująca książek, które odrywają ją od codzienności i pozwalają wejść w mroczny, pełen sekretów świat. Ceni wciągającą, dynamiczną fabułę oraz głęboką psychologię postaci, dzięki której może śledzić motywacje bohaterów i zgłębiać ich moralne dylematy. Czytelnik ten oczekuje od kryminału intensywnych emocji, napięcia i poczucia uczestnictwa w niebezpiecznej, ale fascynującej grze.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_reportaz = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do miłośników prawdziwych historii i czytelników ciekawych świata.
1. Zawiera sekcje:
 <p>Wprowadzenie, które przedstawia książkę, jej gatunek (reportaż literacki, dziennikarski, historyczny itp.), ogólną tematykę i klimat (np. poruszający, odkrywczy, wnikliwy), główne cechy, takie jak prawdziwość historii, rzetelność źródeł, dogłębna analiza i unikalna perspektywa autora. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób, które chcą zrozumieć świat i ludzi w sposób bardziej świadomy i pogłębiony.</p>
 <p>Opis treści z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak autentyczność, szczegółowość, odwaga autora, nieznane fakty, lokalne konteksty czy historie ludzkie. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych, unikaj zdradzania pełnej treści, aby nie odbierać czytelnikowi wrażeń.)</p>
 <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ten reportaż wyróżnia się na tle innych — np. dzięki wyjątkowemu stylowi autora, unikalnym rozmówcom, trudnym tematom lub nowatorskiemu ujęciu znanych zagadnień.</p>
 <h3>Przekonujący call to action, który zachęca do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
- Podkreślić najważniejsze cechy książki
- Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy za pomocą <b>
- Nie używaj znaczników Markdown, tylko HTML
- Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być wciągający, ale rzetelny i pełen szacunku do przedstawianych historii
- Używaj języka, który podkreśla autentyczność, prawdziwość i odwagę autora
- Akcentuj wartość merytoryczną — pokazuj głębię, analityczne podejście i unikalne spojrzenie na temat
- Unikaj ogólników — skup się na konkretnych aspektach reportażu, które wyróżniają książkę (jeśli masz takie informacje)
- Pisząc, miej w głowie czytelnika, który szuka książki poszerzającej wiedzę, wywołującej refleksje i przedstawiającej świat w sposób nieoczywisty
- Nie bój się podkreślać emocji: poruszenia, zaskoczenia, czasem gniewu czy smutku — ale zawsze z wyczuciem
- Zachowaj profesjonalny, ale jednocześnie zaangażowany i świadomy ton
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba, do której kierowany jest opis:
Opis reportażu kierowany jest do czytelnika, który ceni prawdziwe, oparte na faktach historie i chce lepiej rozumieć świat wokół siebie. To osoba świadoma, ciekawa, często zainteresowana tematami społecznymi, politycznymi, kulturowymi lub historycznymi. Lubi książki, które skłaniają do refleksji i zmieniają sposób patrzenia na ludzi i wydarzenia. Czytelnik reportaży szuka głębi, autentycznych przeżyć i nowych perspektyw, które pozwolą mu zobaczyć rzeczywistość w bardziej złożony i prawdziwy sposób.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_young_adult = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażający opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do młodszych czytelników i miłośników historii pełnych emocji, przygód i młodzieńczych dylematów.
1. Zawiera sekcje:
 <p>Wprowadzenie, które przedstawia książkę, jej gatunek (np. young adult fantasy, young adult romance, dystopia, contemporary), ogólną tematykę i klimat (np. pełen emocji, przygód, młodzieńczych rozterek i relacji), główne cechy, takie jak dynamiczna akcja, wyraziste postacie oraz silne emocje. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób szukających historii, z którymi mogą się utożsamić i które poruszają aktualne, ważne tematy.</p>
 <p>Opis fabuły z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak pierwsze miłości, bunt, odkrywanie siebie, przyjaźnie, konflikty rodzinne czy walka o marzenia. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych o fabule, unikaj zdradzania najważniejszych zwrotów akcji.)</p>
 <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta książka young adult wyróżnia się na tle innych — np. dzięki wyjątkowej atmosferze, wiarygodnym postaciom czy odważnemu poruszaniu ważnych tematów dla młodych ludzi.</p>
 <h3>Przekonujący call to action, który zachęca do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
- Podkreślić najważniejsze cechy książki
- Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy za pomocą <b>
- Nie używaj znaczników Markdown, tylko HTML
- Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być dynamiczny, pełen emocji i energii
- Używaj języka, który jest bliski młodszym czytelnikom, lekki, ale jednocześnie angażujący i wyrazisty
- Akcentuj uczucia, relacje i rozwój bohaterów — ich wybory, marzenia i dylematy
- Unikaj ogólników — skup się na unikalnych doświadczeniach, które mogą zainteresować młodego czytelnika (jeśli masz takie informacje)
- Pisząc, miej w głowie czytelnika, który szuka historii, z którymi może się utożsamić, które go poruszą i zainspirują
- Nie bój się podkreślać emocji: radości, smutku, ekscytacji, gniewu, buntu
- Zachowaj profesjonalny, ale świeży i przystępny ton
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba, do której kierowany jest opis:
Opis książki young adult kierowany jest do młodego czytelnika, zazwyczaj w wieku nastoletnim lub wczesnej dorosłości, który szuka historii pełnych emocji, przygód i odkrywania siebie. To osoba wrażliwa, ciekawa świata, poszukująca odpowiedzi na ważne pytania i chcąca poczuć, że nie jest sama w swoich przeżyciach. Ceni książki, które pokazują prawdziwe relacje, poruszają aktualne tematy i inspirują do bycia sobą oraz do podejmowania odważnych decyzji.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
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

prompt_fantastyka = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do miłośników fantastyki i czytelników szukających niezwykłych światów.
1. Zawiera sekcje:
 <p>Wprowadzenie, które przedstawia książkę, jej gatunek (fantasy, science fiction, urban fantasy itp.), ogólną tematykę i klimat (np. epicki, magiczny, mroczny, pełen przygód), główne cechy, takie jak wykreowany świat, niezwykłe postacie, rozbudowane uniwersum oraz motywy przewodnie. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób szukających odskoczni od rzeczywistości i fascynujących podróży do innych światów.</p>
 <p>Opis fabuły z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak magia, niezwykłe moce, rozległe uniwersa, konflikty między rasami lub światami oraz epickie przygody. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych o fabule, unikaj zdradzania kluczowych zwrotów akcji.)</p>
 <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta książka fantasy wyróżnia się na tle innych — np. dzięki oryginalnej wizji autora, zaskakującym zwrotom akcji, czy unikalnemu systemowi magii lub kreacji świata.</p>
 <h3>Przekonujący call to action, który zachęca do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
- Podkreślić najważniejsze cechy książki
- Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy za pomocą <b>
- Nie używaj znaczników Markdown, tylko HTML
- Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być angażujący, pełen emocji i obrazowy
- Używaj języka, który pobudza wyobraźnię, buduje atmosferę przygody i magii
- Akcentuj unikalność świata przedstawionego, niezwykłość bohaterów i epickość opowieści
- Unikaj ogólników — skup się na konkretnych elementach świata, magii czy konfliktach (jeśli masz takie informacje)
- Pisząc, miej w głowie czytelnika, który kocha fantastyczne światy, epickie przygody i chce całkowicie oderwać się od codzienności
- Nie bój się podkreślać emocji: ekscytacji, wzruszenia, podziwu czy niepokoju
- Zachowaj profesjonalny, ale dynamiczny i barwny ton
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba, do której kierowany jest opis:
Opis książki fantasy kierowany jest do czytelnika, który pragnie uciec od rzeczywistości i zanurzyć się w całkowicie nowym, wykreowanym świecie. To osoba pełna wyobraźni, otwarta na niezwykłe przygody, magiczne moce i epickie konflikty. Ceni oryginalność, bogactwo detali i rozbudowaną mitologię. Czytelnik ten szuka emocji, które pozwalają mu poczuć się częścią historii — przeżywać losy bohaterów, odkrywać tajemnice i wyruszać w podróże, o których w prawdziwym życiu można tylko marzyć.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
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

prompt_gry_planszowe = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem:<h2>, <p>, <b>, <ul>, <li>. Opis powinien:
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
5. Osoba do której kierowany jest opis:
Osoba, która dopiero zaczyna swoją przygodę z planszówkami i nie jest zaznajomiona z światem gier planszowych, ktoś kto poszukuję planszówek na prezent np. rodzic kupujący planszówkę dla dziecka.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_biografie = """Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}". Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z wykorzystaniem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
Zaczyna się od nagłówka <h2> z kreatywnym hasłem, które oddaje emocje i charakter książki oraz odwołuje się do miłośników historii prawdziwych i inspirujących opowieści.
1. Zawiera sekcje:
 <p>Wprowadzenie, które przedstawia książkę, jej gatunek (biografia, autobiografia, wspomnienia), ogólną tematykę i klimat (np. inspirujący, motywujący, szczery), główne cechy, takie jak autentyczność historii, dokładność przedstawienia faktów, osobisty charakter opowieści. Dodatkowo zaznacz, do jakiego czytelnika jest skierowana — np. dla osób szukających prawdziwych historii pełnych wartościowych lekcji i inspiracji.</p>
 <p>Opis życia bohatera z <b>wyróżnionymi</b> słowami kluczowymi, podkreślającymi unikalne elementy, takie jak przełomowe momenty, sukcesy i porażki, inspirujące wybory oraz wpływ na innych. (Trzymaj się informacji zawartych w dotychczasowym opisie książki, jeśli nie masz szczegółowych danych, unikaj zbyt dużej ilości szczegółów, żeby nie zdradzać całej historii.)</p>
 <p>Podsumowanie, które zachęca do zakupu i podkreśla, dlaczego ta biografia wyróżnia się na tle innych — np. dzięki wyjątkowej szczerości, głębokiemu przedstawieniu postaci lub nieznanym dotąd faktom.</p>
 <h3>Przekonujący call to action, który zachęca do sięgnięcia po książkę i natychmiastowego zamówienia.</h3>
2. Wykorzystuje pobrane informacje, aby:
- Podkreślić najważniejsze cechy książki
- Wzmocnić wiarygodność opisu poprzez konkretne przykłady
3. Formatowanie:
- Używaj tagów HTML: <h2>, <p>, <b>, <h3>
- Wyróżniaj kluczowe frazy za pomocą <b>
- Nie używaj znaczników Markdown, tylko HTML
- Nie dodawaj komentarzy ani wyjaśnień, tylko sam opis
4. Styl:
- Opis powinien być angażujący, ale rzetelny i autentyczny
- Używaj języka, który podkreśla prawdziwość historii, inspiruje i budzi emocje
- Akcentuj elementy związane z psychologią postaci, drogą do sukcesu i wyciąganymi lekcjami
- Unikaj ogólników — skup się na konkretnych momentach i doświadczeniach (jeśli masz takie informacje)
- Pisząc, miej w głowie czytelnika, który szuka autentycznych, wartościowych historii i pragnie dowiedzieć się więcej o życiu innych
- Nie bój się podkreślać emocji: wzruszeń, momentów przełomowych, triumfów i porażek
- Zachowaj profesjonalny, ale bliski i inspirujący ton
- Unikaj powtórzeń
- Zachowaj spójność tonu
5. Osoba, do której kierowany jest opis:
Opis książki biograficznej kierowany jest do dorosłego czytelnika, który ceni prawdziwe, autentyczne historie i pragnie dowiedzieć się więcej o życiu innych ludzi. To osoba ciekawa świata i ludzi, szukająca inspiracji i motywacji, gotowa uczyć się na doświadczeniach innych i odkrywać kulisy sukcesów oraz porażek. Taki czytelnik oczekuje wartościowych lekcji, głębokiej psychologii postaci i możliwości wniknięcia w nieznane dotąd aspekty życia znanych osób.
Przykład formatu:
<h2>nagłówek</h2>
<p>dwa akapity</p>
<p>akapit</p>
<h3>CTA</h3>
"""

prompt_metodyka = '''Jako autor opisów w księgarni internetowej, twoim zadaniem jest przygotowanie rzetelnego, zoptymalizowanego opisu produktu o tytule "{book_title}" z zakresu metodyki i pedagogiki. Oto informacje, na których powinieneś bazować: {book_details} {book_description}. Stwórz angażujący opis w HTML z użyciem: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
- Zawierać <h2> z hasłem akcentującym praktyczne podejście i wartość metodyczną.
- Mieć sekcję <p> przedstawiającą cel książki, jej grupę docelową (nauczyciele, trenerzy, edukatorzy) i korzyści z zastosowania opisanych metod.
- Mieć <ul> z <li> najważniejszymi cechami metodyki, takimi jak: organizacja zajęć, innowacyjne techniki dydaktyczne, przykłady ćwiczeń.
- Zakończyć <p> podsumowaniem, podkreślającym praktyczność i wpływ na efektywność nauczania.
- Zawierać <h3> CTA zachęcające do zakupu i wdrożenia nowych rozwiązań metodycznych.
'''  

prompt_edukacyjne = '''Jako doświadczony copywriter w księgarni internetowej, przygotuj opis edukacyjnej książki pod tytułem "{book_title}". Dane do wykorzystania: {book_details} {book_description}. Użyj HTML: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
- Rozpoczynać się od <h2> z hasłem podkreślającym rozwój umiejętności i zdobywanie wiedzy.
- Sekcja <p> opisująca tematykę i grupę wiekową czytelników (dzieci, młodzież, dorośli).
- <ul> zawierające kluczowe korzyści edukacyjne: rozwój logicznego myślenia, kreatywności, umiejętności językowych itp.
- <p> z informacją o formacie książki (ćwiczenia, testy, ilustracje), oraz jej unikalnych atutach.
- <h3> CTA zachęcające do nauki i samodoskonalenia.
'''

prompt_lektury = '''Jako autor opisów w księgarni internetowej, twoim zadaniem jest stworzenie opisu klasycznej lektury szkolnej o tytule "{book_title}". Wykorzystaj: {book_details} {book_description}. Format HTML: <h2>, <p>, <b>. Opis powinien:
- Zaczynać się od <h2> z literackim hasłem oddającym esencję utworu.
- <p> wprowadzające w kontekst historyczno-kulturowy dzieła.
- <p> omawiające główne wątki i motywy (<b>miłość</b>, <b>konflikt</b>, <b>wartości</b>).
- <p> podsumowujące, dlaczego warto przeczytać tę lekturę i jakie lekcje daje.
- <h3> CTA zapraszające do odkrycia ponadczasowego dzieła.
'''

prompt_zabawki = '''Jako copywriter w księgarni internetowej, twoim zadaniem jest przygotowanie atrakcyjnego opisu zabawki o nazwie "{book_title}". Dane: {book_details} {book_description}. Format HTML: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
- Mieć <h2> z hasłem przyciągającym uwagę rodziców i dzieci.
- <p> opisujące rodzaj zabawki, zalecany wiek i sposób zabawy.
- <ul> z <li> głównymi cechami: bezpieczeństwo, rozwój umiejętności, materiał, łatwość montażu.
- <p> podkreślający korzyści dla rozwoju dziecka.
- <h3> CTA zachęcające do zakupu jako idealnego prezentu.
'''

prompt_komiksy = '''Jako autor opisów w księgarni internetowej, twoim zadaniem jest stworzenie angażującego opisu komiksu "{book_title}". Wykorzystaj: {book_details} {book_description}. Użyj HTML: <h2>, <p>, <b>, <ul>, <li>. Opis powinien:
- Rozpoczynać się od <h2> z dynamicznym hasłem oddającym klimat opowieści.
- <p> prezentujące gatunek (superbohaterski, manga, humorystyczny), styl graficzny i głównych bohaterów.
- <ul> z <li> opisującymi: fabułę, grafikę, format (liczba stron, kolor).
- <p> podsumowujące unikalne atuty: narracja obrazkowa, kolekcjonerska wartość.
- <h3> CTA zachęcające do zanurzenia się w świecie ilustracji.
'''

prompts = {
    "Beletrystyka": prompt_beletrystyka,
    "Biografie": prompt_biografie,
    "Edukacyjne": prompt_edukacyjne,
    "Fantastyka": prompt_fantastyka,
    "Gry planszowe": prompt_gry_planszowe,
    "Komiksy": prompt_komiksy,
    "Kryminał": prompt_kryminal,
    "Lektury": prompt_lektury,
    "Metodyka": prompt_metodyka,
    "Reportaż": prompt_reportaz,
    "Romans": prompt_romans,
    "Sci-fi": prompt_scifi,
    "Young Adult": prompt_young_adult,
    "Zabawki": prompt_zabawki,
}

# ------------- INICJALIZACJA STANU ------------- #
if 'selected_prompt' not in st.session_state:
    st.session_state.selected_prompt = "Romans"
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
st.title('📚 Generator opisów produktów')
st.markdown("---")

st.sidebar.header("🎯 Ustawienia")
prompt_keys = list(prompts.keys())
default_prompt = st.session_state.selected_prompt if st.session_state.selected_prompt in prompt_keys else prompt_keys[0]

selected_prompt = st.sidebar.selectbox(
    "Wybierz kategorię produktu:",
    prompt_keys,
    index=prompt_keys.index(default_prompt)
)
st.session_state.selected_prompt = selected_prompt

channel = st.sidebar.selectbox(
    "Kanał (scope) do zapisu w PIM:",
    ["Bookland", "B2B"],
    index=0
)
locale = st.sidebar.text_input(
    "Locale:",
    value=st.secrets.get("AKENEO_DEFAULT_LOCALE", "pl_PL")
)

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
            keys_to_remove = [key for key in st.session_state.keys() if key not in ['selected_prompt']]
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
                    if book_data['details']:
                        st.write("**Szczegóły:**")
                        st.text_area("Szczegóły", book_data['details'], height=100, disabled=True)
                    if book_data['description']:
                        full_desc = book_data['description']
                        st.write("**Opis (pierwsze 500 znaków):**")
                        st.text_area("Opis", (full_desc[:500] + "...") if len(full_desc) > 500 else full_desc, height=150, disabled=True)
                        st.write(f"**Długość pobranego opisu:** {len(full_desc)} znaków")

                    with st.spinner("Generuję opis..."):
                        selected_prompt_template = prompts[selected_prompt]
                        generated_desc_raw = generate_description(book_data, selected_prompt_template, client)
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

with col2:
    st.header("📄 Wygenerowany opis")

    if 'generated_description' in st.session_state:
        st.subheader(f"📖 {st.session_state.get('book_title', 'Opis produktu')}")
        st.subheader(f"🎭 Kategoria: {selected_prompt}")

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
        # --- wysyłka do PIM ---
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
st.markdown("🔧 **Narzędzie do generowania opisów produktów** | Wykorzystuje OpenAI GPT-4o-mini")
st.markdown("💡 **Wskazówka:** Wybierz odpowiednią kategorię z menu bocznego dla najlepszych rezultatów")
