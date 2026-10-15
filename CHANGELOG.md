# 4.12.1

- Tabela „Dobór linków” pokazuje skale i progi: trafność 0–3 (próg 2,5) oraz noul 0–1 (próg z suwaka).
  Progi użyte przy wyborze są zapisywane w raporcie (`thresholds`).
- Kolumna z powodem odrzucenia jest zaraz po „selected”, więc od razu widać, czemu kandydat odpadł.

# 4.12.0

Linkowanie kontekstowe korzysta z rekomendacji spod karty produktu zamiast dopasowania slugów z sitemap.

- Nowy moduł `related_products.py`: dla SKU (EAN) pobiera z publicznego API Luigi's Box te same produkty,
  które Bookland pokazuje w karuzeli „Inni kupili również” (`item_detail_alternatives`, 16 pozycji),
  razem z atrybutami: kategorie, przedmiot, poziom nauczania, seria, autor.
- Kategorie-kandydaci pochodzą ze ścieżki kategorii samego produktu (np. „Podręczniki szkolne > Szkolne >
  Szkoła Podstawowa Klasa 5 > Matematyka”), z pominięciem zbyt ogólnych poziomów.
- Wstępny ranking przed Jev premiuje tę samą serię i autora, odrzuca self-link oraz inne wydania tego samego tytułu.
- Jev dostaje opis każdego kandydata (`details`) i twarde powiązanie ze źródłem (`relation_to_source`),
  a Gemini dostaje pole „powiazanie”, żeby nie dopowiadał relacji między książkami.
- Adres produktu bierzemy z `url_key` (atrybut `url`), nie z `web_url`, który bywa starym adresem z przekierowaniem.
  URL-e z Luigi's Box nie są ponownie odpytywane per SKU.
- Zapas: gdy Luigi's Box nic nie zwróci, a w sekretach jest `FIRECRAWL_API_KEY`, karta produktu jest renderowana
  przez Firecrawl i parsowana (karuzela + breadcrumbs). Sitemap jest już tylko ostatnim zapasem.
- UI: nowy przełącznik „Dobieraj cele z rekomendacji pod kartą produktu”, czytelniejsza tabela „Dobór linków”.

# 4.11.0

Automatyczne linkowanie Jev może stale korzystać z sitemap Booklandu bez skanowania ich dla każdego SKU.

- Dodano cache `.streamlit/bookland_sitemap.sqlite3` z indeksem SQLite FTS5 dla
  `https://bookland.com.pl/pub/sitemap_index.xml` i sitemap potomnych.
- Pierwsze użycie buduje indeks jednokrotnie; kolejne produkty wykonują tylko lokalny retrieval.
- Do Jev nadal trafia najwyżej 8 kandydatów w jednym requestcie, a wynik nadal zawiera maksymalnie 2 linki.
- URL-e z sitemap nie są ponownie odpytywane HTTP per SKU; cele ręczne zachowują live-check.
- Dodano status i ręczne odświeżanie cache w UI oraz testy retrieval i integracji z Jev.

# 4.9.3

Naprawiono błąd Streamlit przy tworzeniu nowej kolejki. Pole nazwy jest teraz czyszczone dopiero w kolejnym rerunie, zanim widget zostanie utworzony.

# 4.9.2

Generator opisów obsługuje teraz niezależne kolejki robocze. Każda kolejka ma własną listę SKU, wyniki i edycje; wyczyszczenie jednej kolejki nie wpływa na pozostałe. Dotychczasowy wspólny workspace jest zachowany jako „Wspólna (legacy)”.

# 4.9.1

Opisy niespełniające zaleceń redakcyjnych są teraz zwracane z ostrzeżeniami.
Położenie linku, liczba pogrubień, długość, nagłówki i niepewne dopasowanie
nazwisk nie powodują utraty tekstu ani blokady wysyłki do Akeneo.

- Oddzielono ostrzeżenia od błędów technicznych, dodano liczniki, filtr i kolumnę CSV.
- Usunięto ponowne generowanie za ostrzeżenia. Przejściowe błędy API mają wspólny
  budżet trzech wywołań; trwałe błędy dostępu nie są ponawiane.
- Nieudana ręczna regeneracja zachowuje wcześniejszy opis.
- Normalizacja HTML korzysta z `nh3`; edytor odtwarza tylko dozwolone elementy.
  Podgląd, eksport i wysyłka używają oczyszczonej treści.
- Ujednolicono kontekst walidacji trybu „tylko link” i zapisano kanał/język wyniku.
- Poprawiono sprzeczne przykłady odmiany nazwisk w prompcie; nieznane dane należy pominąć.
- Model Gemini, konfiguracja jego wyboru oraz model researchu pozostają bez zmian.

Instalacja zależności: `python -m pip install -r requirements.txt`.
Testy: `python -m unittest discover -s tests -v` (usługi zewnętrzne są mockowane).

Ta poprawka obejmuje walidację, generowanie i dostarczenie opisów. Rozbudowa
wznowienia dużych serii oraz mapowania osobnych ról autorów i redaktorów
pozostaje kolejnym etapem audytu.
