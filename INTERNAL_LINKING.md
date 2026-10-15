# Linkowanie kontekstowe

Moduł wybiera osobno dla każdego SKU maksymalnie jedną kategorię i jeden
potwierdzony produkt uzupełniający. Jev ocenia trafność; Gemini wplata linki
w naturalne zdania. Brak dobrego celu oznacza brak nowego linku.

## Uruchomienie

1. Dodaj do sekretów Streamlit `TYPESAFE_API_KEY = "twój-klucz"`.
   Nie wpisuj klucza do kodu ani CSV. Pozostałe sekrety aplikacji pozostają wymagane.
2. W opcjach opisów wybierz **Automatyczne (Jev)**.
3. Rozwiń **Katalog celów linkowania**. Pobierz szablon CSV albo listę kategorii
   Akeneo i uzupełnij rzeczywiste adresy docelowe Booklandu.
4. Wczytaj CSV do tabeli lub wpisz cele ręcznie i kliknij **Zapisz katalog celów**.
5. Włącz linkowanie. Zacznij od małej kolejki i sprawdź wyniki w **Dobór linków**.

Tryb ręczny nadal działa. W trybie **Automatyczne (Jev)** źródło sitemap Booklandu jest
domyślnie włączone. Nie dodano nowych zależności.

## Rekomendacje spod karty produktu (domyślne źródło kandydatów)

Od wersji 4.12 kandydaci na linki pochodzą przede wszystkim z tego, co Bookland sam poleca pod kartą
produktu (karuzela „Inni kupili również”, Luigi's Box, rekomender `item_detail_alternatives`).

1. Produkt jest wyszukiwany w Luigi's Box po SKU/EAN (`f[]=lb_id:<SKU>`), a gdy to się nie uda — po niemal
   identycznym tytule.
2. Z jego atrybutów bierzemy ścieżkę kategorii (`category` + `category_urls`) → kandydaci-kategorie.
3. `POST /v1/recommend` zwraca 16 produktów spod karty wraz z atrybutami (seria, przedmiot, poziom, autor,
   kategorie). Ranking deterministyczny wybiera 6: premiuje tę samą serię i autora, pomija sam produkt,
   produkty ukryte (`widoczne_bookland=0`, `use_in_crosslinking=0`) i inne wydania tego samego tytułu.
4. Jev dostaje maksymalnie 8 kandydatów (1 kategoria + 1 produkt z ręcznego katalogu, jeśli są, potem
   2 kategorie i produkty spod karty) i wybiera najwyżej jedną kategorię i jeden produkt.
5. Gemini dostaje przy produkcie pole „powiazanie” (np. „ta sama seria: Matematyka z plusem”) i może
   opisać relację tylko na jego podstawie.

API Luigi's Box jest publiczne (to samo, którego używa przeglądarka klienta), nie wymaga klucza i odpowiada
w ~100–300 ms. Adres produktu jest budowany z `url_key` (atrybut `url`), bo `web_url` bywa starym adresem
z kategorią, który przekierowuje.

**Firecrawl (opcjonalnie).** Gdy Luigi's Box nie zwróci rekomendacji (np. zmiana API, blokada z hostingu),
a w sekretach jest `FIRECRAWL_API_KEY`, aplikacja renderuje kartę produktu przez Firecrawl
(`/v2/scrape`, `rawHtml`, przewinięcie strony) i odczytuje z HTML karuzelę rekomendacji oraz breadcrumbs.
Adres karty jest wtedy szukany w snapshocie sitemap po slugu tytułu. Linki z Firecrawl przechodzą live-check.

**Sitemap** jest używana już tylko wtedy, gdy rekomendacje spod karty nie dały żadnego kandydata.

## Sitemap Booklandu i wydajność

Stałym źródłem retrieval jest `https://bookland.com.pl/pub/sitemap_index.xml`. Aplikacja nie
przekazuje całej sitemap Jevowi i nie pobiera jej ponownie dla każdego SKU.

- Przy pierwszym użyciu lub po ręcznym kliknięciu **Odśwież cache sitemap Booklandu**
  sitemap index i jego pliki potomne są pobierane równolegle, maksymalnie 3 requesty naraz.
- Adresy są zapisywane do `.streamlit/bookland_sitemap.sqlite3` i indeksowane przez SQLite FTS5.
- Dla każdego produktu lokalny retrieval wykorzystuje tytuł, serię, przedmiot, szkołę, klasę
  i edycję. Z dużego katalogu wybierana jest mała pula semantycznie zbliżonych URL-i.
- Do jednego requestu Jev trafia nadal najwyżej 8 kandydatów łącznie z ręcznym rejestrem.
  Jeżeli sitemap zwróci kandydatów, połowa budżetu jest rezerwowana dla ręcznego katalogu,
  a połowa dla lokalnego retrieval.
- URL pochodzący bezpośrednio z sitemap nie jest ponownie odpytywany HTTP dla każdego SKU.
  Ręcznie wpisane cele zachowują dotychczasowy live-check 200/canonical/noindex.
- Cache nie odświeża się automatycznie przy każdym przebiegu. Istniejący indeks jest używany
  natychmiast; operator może odświeżyć go przyciskiem w ustawieniach linkowania.

## Katalog celów

CSV obsługuje UTF-8 z BOM lub bez, separator `;`, przecinek albo tabulator.
Wymagane kolumny: `code`, `label`, `url`. `kind` domyślnie wynosi `category`.

| Kolumna | Znaczenie |
| --- | --- |
| `kind` | `category` albo `product` |
| `code` | Dokładny kod kategorii Akeneo albo SKU docelowego produktu |
| `label` | Rzetelna nazwa celu, określająca jego zakres |
| `url` | Rzeczywisty adres HTTP(S) w domenie Booklandu, bez parametrów i fragmentu |
| `school`, `grade`, `subject`, `series`, `edition` | Opcjonalne ograniczenia zgodne z wartościami produktu w Akeneo |
| `source_skus` | Dla produktu: potwierdzone źródłowe SKU rozdzielone `\|` |

Kategoria musi należeć do kategorii źródłowego produktu. Każde podane ograniczenie
musi mieć zgodną wartość w danych źródłowych. Brak wartości nie oznacza zgodności.
Numer tomu lub poziomu kursu nie jest używany jako klasa szkolna.

Link do produktu wymaga jawnego `source_skus` oraz znanej, zgodnej `edition`
po obu stronach. Wpisanie mapowania oznacza potwierdzenie relacji przez operatora;
sam podobny tytuł ani ogólna asocjacja cross-sell nie wystarcza. Nie linkujemy SKU do siebie.

Obsługiwane nazwy atrybutów Akeneo są określone w `LINK_FIELDS` w `app.py`:

- szkoła: `school_type`, `typ_szkoly`, `szkola`;
- klasa: `grade`, `klasa`;
- przedmiot: `subject`, `przedmiot`;
- seria: `series`, `seria`;
- edycja: `edition`, `edycja`, `wydanie`.

Opcje select są rozwiązywane do etykiet. Jeśli Twoja instalacja używa innych
kodów, uzupełnij tę mapę. Nie wykorzystujemy roku wydania jako domyślnej edycji.

Katalog jest wspólny dla kolejek i zapisywany atomowo w
`.streamlit/internal_link_targets.json`. Nie jest commitowany. Przy przenoszeniu
aplikacji zachowaj ten plik lub wyeksportuj i ponownie wczytaj CSV; trwałość
dysku zależy od hostingu, tak jak istniejące lokalne dane aplikacji.

## Kontrola i zachowanie

- Reguły zawężają listę do maksymalnie ośmiu kandydatów. Przy samym katalogu ręcznym
  zachowana jest dotychczasowa kolejność. Gdy włączona jest sitemap, Jev dostaje maksymalnie
  4 kandydatów z ręcznego rejestru i pozostałe miejsca z lokalnego retrieval sitemap.
- Jedno wywołanie `jev-1.13.0` ocenia przydatność (`Score`, skala 0–3) i zgodność
  semantyczną (`Noul`). Wybór wymaga Score co najmniej 2.5 oraz Noul co najmniej
  wartości suwaka, domyślnie 0.8. Są to progi pilotażu do oceny na własnych danych.
- Ręcznie wpisany URL musi zwrócić HTTP 200 i HTML; odrzucamy przekierowania, `noindex`
  i canonical wskazujący inny adres. Wynik tej kontroli jest cache'owany na godzinę.
  URL z sitemap przechodzi walidację domeny i formatu przy indeksowaniu, ale nie jest
  ponownie pobierany per SKU. Nie jest to pełny audyt indeksacji ani potwierdzenie widoczności w Google.
- Przed edycją, eksportem i wysyłką linki poza wybraną listą oraz duplikaty są
  usuwane z zachowaniem tekstu. Usunięcie proponowanego linku przez użytkownika
  pozostaje ostrzeżeniem, bez blokowania opisu.
- Brak klucza, błąd API albo brak dobrego celu nie zatrzymuje pełnego generatora.
  W trybie **Tylko dodaj link** brak celu zachowuje istniejący opis i nie wywołuje
  Gemini. Pusty opis źródłowy zgłasza błąd.
- Przy braku dopasowania zachowane linki źródłowe mają osobną listę dozwolonych
  adresów; późniejsza edycja nie może dodać dowolnego nowego celu.

Podczas lokalnej próby publiczna strona Booklandu zwróciła HTTP 403. Zweryfikuj
dostęp z hostingu aplikacji: taki status powoduje pominięcie linku, a nie uznanie
adresu za potwierdzony. Nie testowano rzeczywistego katalogu Akeneo bez jego konfiguracji.

## Sprawdzenie

```sh
python -m unittest discover -s tests
```

Testy korzystają z atrap API. Pilotaż na rzeczywistym Akeneo wymaga konfiguracji
sekretów i katalogu celów. Dokumentacja modelu: [TypeSafe](https://docs.typesafe.ai/models).
