#!/bin/bash

# Skrypt startowy dla macOS
# Wywołaj z terminalu lub kliknij dwukrotnie w Finderze
# (Finderze musi mieć uprawnienia: prawy klik → Otwórz)

echo "========================================"
echo " Generator Opisów Produktów v3.2"
echo " Powered by Google Gemini"
echo "========================================"
echo ""

# Przejdź do właściwego katalogu (gdzie leży ten skrypt)
cd "$(dirname "$0")"

# Sprawdź czy Python3 jest dostępny
if ! command -v python3 &>/dev/null; then
    echo "[BŁĄD] Python 3 nie jest zainstalowany."
    echo "Pobierz go ze strony: https://www.python.org/downloads/mac-osx/"
    read -p "Wciśnij Enter, aby zakończyć..."
    exit 1
fi

echo "[1/3] Sprawdzanie i instalacja wymaganych bibliotek..."
python3 -m pip install -q streamlit pandas requests google-genai 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[BŁĄD] Nie udało się zainstalować bibliotek."
    echo "Spróbuj ręcznie: pip3 install streamlit pandas requests google-genai"
    read -p "Wciśnij Enter, aby zakończyć..."
    exit 1
fi
echo "      OK"

echo "[2/3] Uruchamianie aplikacji..."
echo ""
echo "Aplikacja otworzy się w przeglądarce za chwilę."
echo "Aby zatrzymać aplikację, zamknij to okno (lub wciśnij Ctrl+C)."
echo ""

python3 -m streamlit run app.py --server.headless=false --browser.gatherUsageStats=false

read -p "Wciśnij Enter, aby zakończyć..."
