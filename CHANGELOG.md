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
