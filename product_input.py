"""Rozpoznawanie SKU i adresów produktów Bookland w danych wklejanych przez użytkownika."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import requests


BOOKLAND_GRAPHQL_URL = "https://bookland.com.pl/graphql"
BOOKLAND_HOSTS = {"bookland.com.pl", "www.bookland.com.pl"}
BOOKLAND_GRAPHQL_TIMEOUT = 20
BOOKLAND_GRAPHQL_BATCH_SIZE = 100


class ProductInputResolutionError(RuntimeError):
    """Błąd komunikacji z usługą rozwiązującą URL produktu na SKU."""


def _parse_bookland_url(value: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Zwraca (url_key, błąd). Dla zwykłego SKU oba pola są puste.

    Adresy bez schematu są obsługiwane, jeśli zaczynają się od domeny Bookland.
    """
    candidate = value.strip()
    lowered = candidate.lower()
    has_explicit_scheme = "://" in lowered
    is_bookland_without_scheme = lowered.startswith(
        ("bookland.com.pl", "www.bookland.com.pl")
    )

    if not has_explicit_scheme and not is_bookland_without_scheme:
        return None, None

    if is_bookland_without_scheme:
        candidate = "https://" + candidate

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None, f"Nieprawidłowy adres URL: {value}"

    if parsed.scheme.lower() not in {"http", "https"}:
        return None, f"Nieobsługiwany protokół w adresie: {value}"

    host = (parsed.hostname or "").lower()
    if host not in BOOKLAND_HOSTS:
        return None, f"Nieobsługiwana domena: {host or value}. Wklej URL z bookland.com.pl."

    path_parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if not path_parts:
        return None, f"Adres nie wskazuje produktu: {value}"

    return path_parts[-1], None


def _fetch_products_by_url_keys(
    url_keys: Sequence[str],
    session=None,
) -> Dict[str, List[Dict[str, str]]]:
    """Pobiera produkty Magento pogrupowane po url_key."""
    if not url_keys:
        return {}

    http = session or requests
    products_by_key: Dict[str, List[Dict[str, str]]] = {}
    unique_keys = list(dict.fromkeys(url_keys))

    query = """
        query ResolveBooklandProducts($urlKeys: [String]!, $pageSize: Int!) {
          products(filter: {url_key: {in: $urlKeys}}, pageSize: $pageSize) {
            items { sku name url_key }
          }
        }
    """

    for start in range(0, len(unique_keys), BOOKLAND_GRAPHQL_BATCH_SIZE):
        chunk = unique_keys[start:start + BOOKLAND_GRAPHQL_BATCH_SIZE]
        try:
            response = http.post(
                BOOKLAND_GRAPHQL_URL,
                json={
                    "query": query,
                    "variables": {"urlKeys": chunk, "pageSize": len(chunk)},
                },
                timeout=BOOKLAND_GRAPHQL_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProductInputResolutionError(
                "Nie udało się pobrać SKU z Bookland. Spróbuj ponownie."
            ) from exc

        if not isinstance(payload, dict):
            raise ProductInputResolutionError(
                "Odpowiedź Bookland ma nieprawidłowy format."
            )

        if payload.get("errors"):
            raise ProductInputResolutionError(
                "Bookland nie zwrócił poprawnej odpowiedzi dla wklejonych adresów."
            )

        try:
            items = payload["data"]["products"]["items"]
        except (KeyError, TypeError) as exc:
            raise ProductInputResolutionError(
                "Odpowiedź Bookland nie zawiera danych produktów."
            ) from exc

        for item in items or []:
            url_key = str(item.get("url_key") or "").strip()
            sku = str(item.get("sku") or "").strip()
            if not url_key or not sku:
                continue
            products_by_key.setdefault(url_key, []).append({
                "sku": sku,
                "title": str(item.get("name") or sku).strip(),
            })

    return products_by_key


def resolve_product_inputs(
    values: Sequence[str],
    session=None,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Rozpoznaje mieszankę SKU i URL-i Bookland.

    Zwraca gotowe produkty oraz błędy poszczególnych wierszy. Kolejność wejścia
    zostaje zachowana.
    """
    parsed_inputs: List[Dict[str, str]] = []
    errors: List[str] = []
    url_keys: List[str] = []

    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue

        url_key, error = _parse_bookland_url(value)
        if error:
            errors.append(error)
            continue

        if url_key is None:
            parsed_inputs.append({
                "input": value,
                "source": "sku",
                "sku": value,
                "title": value,
            })
        else:
            parsed_inputs.append({
                "input": value,
                "source": "url",
                "url_key": url_key,
            })
            url_keys.append(url_key)

    products_by_key = _fetch_products_by_url_keys(url_keys, session=session)
    resolved: List[Dict[str, str]] = []

    for item in parsed_inputs:
        if item["source"] == "sku":
            resolved.append(item)
            continue

        matches = products_by_key.get(item["url_key"], [])
        if not matches:
            errors.append(f"Nie znaleziono produktu Bookland dla URL: {item['input']}")
            continue
        if len(matches) > 1:
            errors.append(f"URL nie wskazuje jednoznacznie produktu: {item['input']}")
            continue

        match = matches[0]
        resolved.append({
            **item,
            "sku": match["sku"],
            "title": match["title"],
        })

    return resolved, errors
