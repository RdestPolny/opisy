# Linkowanie kontekstowe

Moduł wybiera osobno dla każdego SKU maksymalnie jedną kategorię i jeden
produkt uzupełniający spośród tego, co Bookland poleca pod kartą produktu.
Jev ocenia trafność; Gemini wplata linki w naturalne zdania. Brak dobrego celu
oznacza brak nowego linku. Nie trzeba niczego przygotowywać ani wgrywać ręcznie.

## Uruchomienie

1. Dodaj do sekretów Streamlit `TYPESAFE_API_KEY = "twój-klucz"`.
   Nie wpisuj klucza do kodu ani CSV. Pozostałe sekrety aplikacji pozostają wymagane.
2. W opcjach opisów włącz linkowanie i zostaw domyślny tryb **Automatyczne (Jev)**.
3. Zacznij od małej kolejki i sprawdź wyniki w **Dobór linków**.

Tryb **Ręczne** pozwala wpisać jeden stały URL i anchor dla całej kolejki.

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
   pozostały produkt spod karty (zwykle 15–20). Wybiera najwyżej jedną kategorię i jeden produkt. Limit `jev-1.13.0` to 64k tokenów na request,
   więc taka liczba kandydatów mieści się z dużym zapasem; timeout requestu to 30 s.
5. Gemini dostaje przy produkcie pole „powiazanie” (np. „ta sama seria: Matematyka z plusem”) i może
   opisać relację tylko na jego podstawie.

API Luigi's Box jest publiczne (to samo, którego używa przeglądarka klienta), nie wymaga klucza i odpowiada
w ~100–300 ms. Adres produktu jest budowany z `url_key` (atrybut `url`), bo `web_url` bywa starym adresem
z kategorią, który przekierowuje.

**Firecrawl (opcjonalnie).** Gdy Luigi's Box nie zwróci rekomendacji (np. zmiana API, blokada z hostingu),
a w sekretach jest `FIRECRAWL_API_KEY`, aplikacja renderuje kartę produktu przez Firecrawl
(`/v2/scrape`, `rawHtml`, przewinięcie strony) i odczytuje z HTML karuzelę rekomendacji oraz breadcrumbs.
Adres karty bierzemy z Luigi's Box, a gdy go nie ma, budujemy ze slugu tytułu (tak Bookland tworzy `url_key`).
Linki z karuzeli skracamy do ostatniego segmentu ścieżki; linki z Firecrawl przechodzą live-check.

Gdy rekomendacje spod karty nie dadzą żadnego kandydata, opis powstaje bez nowego linku.

## Fakty o produkcie źródłowym

Jev dostaje fakty o produkcie z Akeneo. Obsługiwane nazwy atrybutów Akeneo są określone w `LINK_FIELDS` w `config.py`:

- szkoła: `school_type`, `typ_szkoly`, `szkola`;
- klasa: `grade`, `klasa`;
- przedmiot: `subject`, `przedmiot`;
- seria: `series`, `seria`;
- edycja: `edition`, `edycja`, `wydanie`.

Opcje select są rozwiązywane do etykiet. Jeśli Twoja instalacja używa innych
kodów, uzupełnij tę mapę. Nie wykorzystujemy roku wydania jako domyślnej edycji.

## Kontrola i zachowanie

- Kandydaci spod karty produktu trafiają do Jev wszyscy.
- Jedno wywołanie `jev-1.13.0` ocenia przydatność (`Score`, skala 0–3) i zgodność
  semantyczną (`Noul`). Wybór wymaga Score co najmniej 2.5 oraz Noul co najmniej
  wartości suwaka, domyślnie 0.8. Są to progi pilotażu do oceny na własnych danych.
- URL z Firecrawl musi zwrócić HTTP 200 i HTML; odrzucamy przekierowania, `noindex`
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
adresu za potwierdzony.

## Sprawdzenie

```sh
python -m unittest discover -s tests
```

Testy korzystają z atrap API. Pilotaż na rzeczywistym Akeneo wymaga konfiguracji sekretów. Dokumentacja modelu: [TypeSafe](https://docs.typesafe.ai/models).
