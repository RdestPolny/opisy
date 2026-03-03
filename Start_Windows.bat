@echo off
chcp 65001 >nul
title Generator Opisów Produktów - Bookland

echo ========================================
echo  Generator Opisów Produktów v3.2
echo  Powered by Google Gemini
echo ========================================
echo.

:: Sprawdź czy Python jest dostępny
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [BŁĄD] Python nie jest zainstalowany lub nie jest w PATH.
    echo Pobierz Pythona ze strony: https://www.python.org/downloads/
    echo Zaznacz opcję "Add Python to PATH" podczas instalacji!
    pause
    exit /b 1
)

echo [1/3] Sprawdzanie i instalacja wymaganych bibliotek...
pip install -q streamlit pandas requests google-genai >nul 2>&1
IF ERRORLEVEL 1 (
    echo [BŁĄD] Nie udało się zainstalować bibliotek. Sprawdź połączenie z Internetem.
    pause
    exit /b 1
)
echo       OK

echo [2/3] Uruchamianie aplikacji...
echo.
echo Aplikacja otworzy się w przeglądarce za chwilę.
echo Aby zatrzymać aplikację, zamknij to okno (lub wciśnij Ctrl+C).
echo.

:: Przejdź do folderu z app.py (folder skryptu)
cd /d "%~dp0"

:: Uruchom Streamlit
python -m streamlit run app.py --server.headless=false --browser.gatherUsageStats=false

pause
