# Generator opisów i metatagów Bookland

Aplikacja Streamlit, która pobiera produkty z Akeneo PIM, generuje przez Gemini opisy HTML
(z linkowaniem wewnętrznym dobieranym przez Jev) oraz meta title i meta description,
i zapisuje wyniki z powrotem w Akeneo albo eksportuje je do Magento.

## Uruchomienie lokalne

```sh
pip install -r requirements.txt
streamlit run app.py
```

Sekrety w `.streamlit/secrets.toml` (lokalnie) albo w ustawieniach aplikacji na Streamlit Cloud:

| Klucz | Wymagany | Do czego |
| --- | --- | --- |
| `AKENEO_BASE_URL`, `AKENEO_CLIENT_ID`, `AKENEO_SECRET`, `AKENEO_USERNAME`, `AKENEO_PASSWORD` | tak | odczyt i zapis produktów w Akeneo |
| `GOOGLE_API_KEY` | tak | Gemini (opisy, metatagi, Batch API) |
| `GEMINI_MODEL` | nie | nadpisuje domyślny model z `config.py` |
| `TYPESAFE_API_KEY` | nie | automatyczny dobór linków przez Jev |
| `PERPLEXITY_API_KEY` | nie | research książki przed pisaniem opisu |
| `FIRECRAWL_API_KEY` | nie | zapasowe pobranie rekomendacji z karty produktu |

Nie commituj sekretów: `.streamlit/` jest w `.gitignore` (poza `config.toml`).

## Wdrożenie

Streamlit Community Cloud buduje aplikację z gałęzi `main`. Po zmianie modułów innych niż
`app.py` zrestartuj aplikację (**Manage app → ⋮ → Reboot app**), bo przeładowanie na żywo
nie wczytuje ponownie zaimportowanych modułów.

Dysk Streamlit Cloud nie jest trwały: restart lub redeploy może usunąć kolejki, wyniki
i katalog celów linkowania z `.streamlit/`. Przed aktualizacją pobierz kopię w panelu
**Kopia zapasowa danych** (pasek boczny) i w razie potrzeby przywróć ją po restarcie.

## Struktura

| Plik | Zawartość |
| --- | --- |
| `app.py` | interfejs Streamlit (pasek boczny i zakładki) |
| `ui.py` | stan sesji, kolejki opisów, katalog celów, podgląd wyniku, kopia zapasowa |
| `config.py` | stałe, ścieżki i modele |
| `akeneo.py`, `akeneo_auth.py`, `akeneo_payloads.py` | klient Akeneo, token, payloady zapisu |
| `descriptions.py`, `description_generation.py`, `description_output.py`, `visual_editor.py` | generowanie, walidacja i edycja opisów |
| `internal_linking.py`, `related_products.py` | dobór linków (Luigi's Box, Jev) — zob. `INTERNAL_LINKING.md` |
| `metatags.py`, `meta_titles.py` | generowanie, walidacja i wysyłka metatagów |
| `batch.py` | Gemini Batch API dla dużych katalogów |
| `job_store.py`, `workspace_storage.py`, `checkpoints.py` | SQLite, kolejki robocze, pliki wznowienia |
| `http_clients.py`, `text_utils.py`, `product_input.py` | HTTP/Gemini/Perplexity, funkcje tekstowe, wejście SKU/URL |

## Testy

```sh
python -m unittest discover -s tests
```

Testy nie łączą się z siecią. `tests/test_app_smoke.py` uruchamia cały `app.py`
w Streamlit AppTest z fikcyjnymi sekretami. Te same testy uruchamia GitHub Actions
(`.github/workflows/tests.yml`) przy każdym pushu na `main` i w pull requestach.
