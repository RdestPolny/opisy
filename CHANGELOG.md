# 4.16.0

Status produktów w sklepie widoczny przed generowaniem.

- Wyszukiwarka i backlog oznaczają produkty: ⛔ brak w sklepie / ukryty (bez karty i URL-a, więc bez
  linkowania) oraz ⚠️ brak na stanie. Status pochodzi z Luigi's Box, jedno zapytanie na 50 SKU,
  wynik w cache na 15 minut. Produkty bez karty można ukryć; „Zaznacz pierwsze N” wtedy je pomija.
- Stan magazynowy rekomendacji jest czytany z `availability_boolean`; pole `is_available` ma w indeksie
  Booklandu zawsze 0, przez co wszystkie rekomendacje trafiały do Jev jako „chwilowo niedostępne”.

# 4.15.0

Linkowanie wewnętrzne nie wymaga już niczego ręcznie.

- Usunięty „Katalog celów linkowania” (CSV, tabela, source_skus) i przełącznik źródła rekomendacji:
  kandydaci pochodzą wyłącznie spod karty produktu (Luigi's Box, zapasowo Firecrawl).
- Kopia zapasowa zawiera tylko bazę SQLite.

# 4.14.0

Porządki po audycie: poprawki błędów kolejki i Akeneo, szybsze reruny, podział app.py na moduły.

Błędy
- Ponowny import nie resetuje zadań w trwającym batchu (podwójny koszt Gemini), a wynik batcha trafia
  tylko do zadań, które nadal na niego czekają. Zadania bez odpowiedzi dostają status failed.
- Odzyskiwanie batchy po crashu pomija zadania znane lokalnie i zakończone porażką.
- Token Akeneo jest odnawiany przed wygaśnięciem i po odpowiedzi 401; długie przebiegi nie kończą się serią 401.
- Import SKU odróżnia „brak produktu” od błędu Akeneo w raporcie.
- Wysyłka metatagów zapisuje wartości sprzed nadpisania (nowy eksport do przywrócenia), pomija identyczne
  i nie gubi statusów przy błędzie jednej paczki.
- Etykiety opcji są rozwiązywane tylko dla atrybutów typu select (tekst nie zamienia się w slug);
  cache Akeneo nie zależy od tokenu i nie zapamiętuje błędów.
- Filtr linków działa zawsze: przetrwają tylko wybrane cele i linki z opisu źródłowego.
  Tryb „tylko link” nie usuwa istniejących linków. Ucięty opis (MAX_TOKENS) jest generowany ponownie.
- Checkboxy kolejki nie cofają „Zaznacz pierwsze N” ani czyszczenia kolejki.

Wydajność i dane
- Eksporty CSV i lista wysyłki do Akeneo liczone na żądanie, nie przy każdym kliknięciu.
- Checkpointy zapisywane raz na paczkę; workspace czytany raz na sesję i zapamiętany w adresie (`?workspace=`);
  kolejka opisów zapisuje się od razu. Połączenia SQLite są zamykane.
- Panel „Kopia zapasowa danych”: ZIP z bazą i katalogiem celów oraz przywracanie.

Kod
- app.py podzielony na moduły (opis w README); usunięte zdublowane prompty i stałe, martwy kod,
  licznik opisanych produktów oraz snapshot sitemap z workflow (−11 MB w repozytorium).
- Testy kolejki, wysyłki, kopii, ponowień HTTP i smoke test całej aplikacji; CI w GitHub Actions.
- Wersje zależności mają górne granice.

# 4.13.0

Jev ocenia wszystkich kandydatów spod karty produktu, a sitemap nie jest już źródłem linków.

- Usunięta opcja „Zapasowo uwzględniaj sitemapę Booklandu w doborze Jev” razem z panelem statusu cache.
  Gdy Luigi's Box nic nie zwróci, Jev ocenia tylko ręczny katalog celów.
- Do Jev trafiają wszystkie kategorie ze ścieżki produktu i wszystkie produkty z karuzeli (zwykle 15–20)
  zamiast limitu 8 kandydatów i wstępnego wyboru 6 produktów. Odpadają tylko self-linki, produkty ukryte
  i inne wydania tego samego tytułu. Timeout requestu Jev wydłużony z 15 do 30 s.
- „Automatyczne (Jev)” jest domyślnym trybem doboru celów.
- Snapshot sitemap zostaje tylko dla zapasu Firecrawl (szukanie adresu karty po tytule).

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
