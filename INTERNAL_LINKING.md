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

Tryb ręczny nadal działa. Domyślny tryb nie zmienia się przy aktualizacji.
Nie dodano nowych zależności.

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

- Reguły zawężają listę do maksymalnie ośmiu kandydatów. Kolejność katalogu ma
  znaczenie przy większej liczbie dopuszczonych celów; umieszczaj preferowane wcześniej.
- Jedno wywołanie `jev-1.13.0` ocenia przydatność (`Score`, skala 0–3) i zgodność
  semantyczną (`Noul`). Wybór wymaga Score co najmniej 2.5 oraz Noul co najmniej
  wartości suwaka, domyślnie 0.8. Są to progi pilotażu do oceny na własnych danych.
- Wybrany URL musi zwrócić HTTP 200 i HTML. Odrzucamy przekierowania, `noindex`
  i canonical wskazujący inny adres. Wynik tej kontroli jest cache'owany na godzinę.
  Nie jest to pełny audyt indeksacji ani potwierdzenie widoczności w Google.
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
