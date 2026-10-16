# Linkowanie kontekstowe

Moduł wybiera osobno dla każdego SKU maksymalnie jedną kategorię i jeden
potwierdzony produkt uzupełniający. Jev ocenia trafność; Gemini wplata linki
w naturalne zdania. Brak dobrego celu oznacza brak nowego linku.

## Uruchomienie

1. Dodaj do sekretów Streamlit `TYPESAFE_API_KEY = "twój-klucz"`.
   Nie wpisuj klucza do kodu ani CSV. Pozostałe sekrety aplikacji pozostają wymagane.
2. W opcjach opisów zostaw domyślny tryb **Automatyczne (Jev)**.
3. Rozwiń **Katalog celów linkowania**. Pobierz szablon CSV albo listę kategorii
   Akeneo i uzupełnij rzeczywiste adresy docelowe Booklandu.
4. Wczytaj CSV do tabeli lub wpisz cele ręcznie i kliknij **Zapisz katalog celów**.
5. Włącz linkowanie. Zacznij od małej kolejki i sprawdź wyniki w **Dobór linków**.

Tryb ręczny nadal działa, ale domyślnym trybem jest **Automatyczne (Jev)**.
Od wersji 4.13 sitemap Booklandu nie jest już źródłem kandydatów do linkowania.

## Rekomendacje spod karty produktu (źródło kandydatów)

Kandydaci na linki pochodzą z tego, co Bookland sam poleca pod kartą
produktu (karuzela „Inni kupili również”, Luigi's Box, rekomender `item_detail_alternatives`).

1. Produkt jest wyszukiwany w Luigi's Box po SKU/EAN (`f[]=lb_id:<SKU>`), a gdy to się nie uda — po niemal
   identycznym tytule.
2. Z jego atrybutów bierzemy ścieżkę kategorii (`category` + `category_urls`) → kandydaci-kategorie.
3. `POST /v1/recommend` zwraca 16 produktów spod karty wraz z atrybutami (seria, przedmiot, poziom, autor,
   kategorie). Ranking deterministyczny ustala kolejność (premiuje tę samą serię i autora) i pomija tylko
   sam produkt, produkty ukryte (`widoczne_bookland=0`, `use_in_crosslinking=0`) i inne wydania tego samego tytułu.
4. Jev dostaje w jednym requeście wszystkich tych kandydatów: każdą kategorię ze ścieżki produktu i każdy
   pozostały produkt spod karty (zwykle 15–20), plus najwyżej 1 kategorię i 1 produkt z ręcznego katalogu.
   Wybiera najwyżej jedną kategorię i jeden produkt. Limit `jev-1.13.0` to 64k tokenów na request,
   więc taka liczba kandydatów mieści się z dużym zapasem; timeout requestu to 30 s.
5. Gemini dostaje przy produkcie pole „powiazanie” (np. „ta sama seria: Matematyka z plusem”) i może
   opisać relację tylko na jego podstawie.

API Luigi's Box jest publiczne (to samo, którego używa przeglądarka klienta), nie wymaga klucza i odpowiada
w ~100–300 ms. Adres produktu jest budowany z `url_key` (atrybut `url`), bo `web_url` bywa starym adresem
z kategorią, który przekierowuje.

**Firecrawl (opcjonalnie).** Gdy Luigi's Box nie zwróci rekomendacji (np. zmiana API, blokada z hostingu),
a w sekretach jest `FIRECRAWL_API_KEY`, aplikacja renderuje kartę produktu przez Firecrawl
(`/v2/scrape`, `rawHtml`, przewinięcie strony) i odczytuje z HTML karuzelę rekomendacji oraz breadcrumbs.
Adres karty jest wtedy szukany w snapshocie sitemap po slugu tytułu. Linki z Firecrawl przechodzą live-check.

Gdy rekomendacje spod karty nie dadzą żadnego kandydata, Jev ocenia tylko ręczny katalog celów,
a bez niego opis powstaje bez nowego linku. Nie ma już zapasowego dopasowania po sitemap.

## Snapshot sitemap Booklandu

Snapshot sitemap w repozytorium (`data/`, odświeżany przez GitHub Actions) służy już tylko zapasowi
Firecrawl: po tytule produktu odszukuje adres jego karty i kanoniczne adresy produktów z karuzeli.
Gdy w sekretach jest `FIRECRAWL_API_KEY`, aplikacja w razie potrzeby odtwarza z niego lokalny indeks
`.streamlit/bookland_sitemap.sqlite3`.

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

- Kandydaci spod karty produktu trafiają do Jev wszyscy. Ręczny katalog jest ograniczony:
  obok rekomendacji dostaje 1 kategorię i 1 produkt, a bez nich maksymalnie 8 kandydatów
  w dotychczasowej kolejności.
- Jedno wywołanie `jev-1.13.0` ocenia przydatność (`Score`, skala 0–3) i zgodność
  semantyczną (`Noul`). Wybór wymaga Score co najmniej 2.5 oraz Noul co najmniej
  wartości suwaka, domyślnie 0.8. Są to progi pilotażu do oceny na własnych danych.
- Ręcznie wpisany URL musi zwrócić HTTP 200 i HTML; odrzucamy przekierowania, `noindex`
  i canonical wskazujący inny adres. Wynik tej kontroli jest cache'owany na godzinę.
  URL z Luigi's Box (`url_key`) przechodzi walidację domeny i formatu, ale nie jest
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
