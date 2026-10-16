"""Klienci HTTP, Gemini i Perplexity z ponowieniami."""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Optional

import requests
import streamlit as st
from google import genai
from google.genai import types

from config import (
    AKENEO_MAX_ATTEMPTS,
    AKENEO_TIMEOUT,
    APP_VERSION,
    GEMINI_HTTP_TIMEOUT_MS,
    PERPLEXITY_API_URL,
    PERPLEXITY_MODEL,
    PERPLEXITY_TIMEOUT,
)


# Klienci są per-thread. W v4.5.0 jeden współdzielony klient HTTP/Gemini był
# używany przez kilka workerów równocześnie. Przy długich przebiegach mogło to
# prowadzić do zawieszonego połączenia, które blokowało cały chunk w as_completed().
_thread_local = threading.local()


def get_gemini_client() -> genai.Client:
    client = getattr(_thread_local, "gemini_client", None)
    if client is None:
        client = genai.Client(
            api_key=str(st.secrets["GOOGLE_API_KEY"]),
            # google-genai interpretuje timeout w milisekundach. Dzięki temu
            # pojedynczy zawieszony request nie może zatrzymać całej kolejki bez końca.
            http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS},
        )
        _thread_local.gemini_client = client
    return client


def get_http_session() -> requests.Session:
    session = getattr(_thread_local, "http_session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": f"BooklandSEOGenerator/{APP_VERSION}"})
        _thread_local.http_session = session
    return session


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = AKENEO_MAX_ATTEMPTS,
    timeout: int = AKENEO_TIMEOUT,
    on_unauthorized: Optional[Callable[[str], str]] = None,
    **kwargs,
) -> requests.Response:
    """Request z ponowieniami dla 429/5xx/błędów sieci. ``on_unauthorized`` dostaje
    odrzucony token Bearer i zwraca nowy – wtedy 401 jest powtarzane raz z nowym tokenem."""
    session = get_http_session()
    last_error: Optional[Exception] = None
    reauthorized = False
    for attempt in range(max_attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            headers = kwargs.get("headers") or {}
            if response.status_code == 401 and on_unauthorized and not reauthorized and "Authorization" in headers:
                reauthorized = True
                stale = headers["Authorization"].removeprefix("Bearer ")
                kwargs["headers"] = {**headers, "Authorization": f"Bearer {on_unauthorized(stale)}"}
                response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(float(retry_after), 60.0)
                except ValueError:
                    wait = min(2 ** attempt + (attempt * 0.3), 60.0)
                if attempt + 1 < max_attempts:
                    time.sleep(max(wait, 1.0))
                continue
            if response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code} z {url}: {response.text[:200]}")
                if attempt + 1 < max_attempts:
                    time.sleep(min(2 ** attempt + 0.5, 30.0))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(min(2 ** attempt + 0.5, 30.0))
    if last_error:
        raise last_error
    raise RuntimeError(f"Brak poprawnej odpowiedzi z {url} (limit zapytań HTTP 429)")


PERPLEXITY_SYSTEM_PROMPT = """Badaj książki i autorów. Odpowiadaj po polsku.
Podawaj tylko konkretne, możliwe do zweryfikowania fakty. Nie generalizuj."""


def research_book_with_perplexity(title: str, author: str, isbn: str = "") -> Optional[str]:
    api_key = str(st.secrets.get("PERPLEXITY_API_KEY", ""))
    if not api_key:
        return None

    query = (
        f"Podaj kluczowe informacje o książce „{title}”"
        + (f"; osoby przypisane w katalogu: {author}" if author else "")
        + (f", ISBN/EAN {isbn}" if isbn else "")
        + ". Najpierw jednoznacznie ustal, czy wymienione osoby są autorami, redaktorami lub odpowiadają za redakcję naukową. "
        "Następnie uwzględnij temat, gatunek, wyróżniki i odbiorcę. Nie pomijaj żadnej osoby. Maksymalnie 250 słów."
    )
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {"role": "system", "content": PERPLEXITY_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        "max_tokens": 500,
        "return_images": False,
    }
    try:
        response = request_with_retry(
            "POST",
            PERPLEXITY_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=PERPLEXITY_TIMEOUT,
            # Research jest dodatkiem: najwyżej jedno ponowienie, żeby timeout nie kosztował 3×45 s.
            max_attempts=2,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def call_gemini_with_retry(
    model: str,
    contents: Any,
    config: types.GenerateContentConfig,
    max_attempts: int = 4,
):
    client = get_gemini_client()
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            last_exc = exc
            err_msg = str(exc).lower()
            if "429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg:
                wait_sec = 3.5 * (attempt + 1)
                match = re.search(r"retry\s+(?:in|after)\s+([0-9.]+)\s*s", str(exc), re.IGNORECASE)
                if match:
                    try:
                        wait_sec = max(float(match.group(1)) + 0.5, 2.0)
                    except ValueError:
                        pass
                time.sleep(min(wait_sec, 30.0))
                continue
            if "503" in err_msg or "500" in err_msg or "unavailable" in err_msg:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Nie udało się uzyskać odpowiedzi z Gemini")
