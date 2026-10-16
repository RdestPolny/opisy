"""Token Akeneo współdzielony przez wątki i kolejne reruny Streamlit.

Moduł jest importowany, więc jego stan przeżywa reruny skryptu app.py. Token jest
odnawiany przed wygaśnięciem i na żądanie (po odpowiedzi 401), dzięki czemu długi
przebieg nie kończy się serią błędów autoryzacji po godzinie.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Tuple

# Odnawiamy token z zapasem, żeby nie wygasł w trakcie requestu.
REFRESH_MARGIN_SECONDS = 300


class TokenStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def get(self, fetch: Callable[[], Tuple[str, int]], *, force: bool = False, stale: str = "") -> str:
        """Zwraca ważny token. ``stale`` to token odrzucony przez API: odświeżamy tylko,
        jeśli inny wątek jeszcze tego nie zrobił."""
        with self._lock:
            expired = self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS
            rejected = force and (not stale or stale == self._token)
            if not self._token or expired or rejected:
                token, expires_in = fetch()
                self._token = token
                self._expires_at = self._clock() + max(int(expires_in or 0), 60)
            return self._token

    def clear(self) -> None:
        with self._lock:
            self._token, self._expires_at = "", 0.0


AKENEO_TOKENS = TokenStore()
