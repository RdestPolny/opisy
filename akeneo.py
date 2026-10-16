"""Klient Akeneo PIM: token, atrybuty, produkty, kategorie, zapis opisu."""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin

import requests
import streamlit as st

from akeneo_auth import AKENEO_TOKENS
from config import AKENEO_MAX_WORKERS, DEFAULT_CHANNEL, DEFAULT_LOCALE, LINK_FIELDS
from http_clients import request_with_retry
from text_utils import (
    _clean_option_slug,
    format_contributor_names,
    normalize_for_compare,
    normalize_spaces,
    safe_string_value,
    split_contributor_names,
    strip_html,
)


def _akeneo_root() -> str:
    base = str(st.secrets["AKENEO_BASE_URL"]).rstrip("/")
    if base.endswith("/api/rest/v1"):
        return base[: -len("/api/rest/v1")]
    return base


def _fetch_akeneo_token() -> Tuple[str, int]:
    token_url = _akeneo_root() + "/api/oauth/v1/token"
    response = request_with_retry(
        "POST",
        token_url,
        auth=(st.secrets["AKENEO_CLIENT_ID"], st.secrets["AKENEO_SECRET"]),
        data={
            "grant_type": "password",
            "username": st.secrets["AKENEO_USERNAME"],
            "password": st.secrets["AKENEO_PASSWORD"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Błąd autoryzacji Akeneo {response.status_code}: {response.text[:300]}")
    payload = response.json()
    return payload["access_token"], int(payload.get("expires_in") or 3600)


def akeneo_get_token(*, force: bool = False, stale: str = "") -> str:
    return AKENEO_TOKENS.get(_fetch_akeneo_token, force=force, stale=stale)


def akeneo_request(method: str, url: str, **kwargs) -> requests.Response:
    """request_with_retry z odnowieniem tokenu Akeneo po odpowiedzi 401."""
    return request_with_retry(
        method, url, on_unauthorized=lambda stale: akeneo_get_token(force=True, stale=stale), **kwargs
    )


def akeneo_headers(token: str = "", content_type: str = "") -> Dict[str, str]:
    # Zawsze aktualny token: przekazywany parametr bywa starszy niż godzina w długim przebiegu.
    headers = {"Authorization": f"Bearer {akeneo_get_token()}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_get_attribute(code: str, _token: str = "") -> Dict:
    """Definicja atrybutu (typ, scopable, localizable). Błędy nie trafiają do cache."""
    response = akeneo_request(
        "GET",
        _akeneo_root() + f"/api/rest/v1/attributes/{quote(code, safe='')}",
        headers=akeneo_headers(),
    )
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_existing_attribute_codes(_token: str = "") -> List[str]:
    """Zwraca tylko kody atrybutów istniejących w danym PIM-ie.

    Dzięki temu parametr `attributes` nie wywoła błędu 422, gdy instalacja używa
    np. `autor` zamiast `author` albo `wydawnictwo` zamiast `publisher`.
    """
    candidates = [
        "name", "description", "author", "autor", "editor", "editors", "redaktor",
        "redaktorzy", "redakcja", "redakcja_naukowa", "publisher", "wydawnictwo",
        "year", "rok_wydania", "pages", "liczba_stron", "cover_type", "oprawa",
        "ean", "isbn", "meta_title", "meta_description",
        *(code for codes in LINK_FIELDS.values() for code in codes),
    ]
    existing: List[str] = []
    for code in candidates:
        response = akeneo_request(
            "GET",
            _akeneo_root() + f"/api/rest/v1/attributes/{quote(code, safe='')}",
            headers=akeneo_headers(),
            max_attempts=2,
        )
        if response.status_code == 200:
            existing.append(code)
        elif response.status_code != 404:
            # Wyjątek zamiast częściowej listy: cache_data nie zapamięta błędu na godzinę,
            # a brak np. `description` zmieniłby input_hash i wymusił regenerację.
            raise RuntimeError(f"Nie udało się sprawdzić atrybutu {code}: HTTP {response.status_code}")
    return existing


def akeneo_product_exists(sku: str, token: str) -> bool:
    response = akeneo_request(
        "GET",
        _akeneo_root() + f"/api/rest/v1/products/{quote(sku, safe='')}",
        headers=akeneo_headers(token),
        max_attempts=3,
    )
    return response.status_code == 200


SELECT_ATTRIBUTE_TYPES = {"pim_catalog_simpleselect", "pim_catalog_multiselect"}


def akeneo_is_select_attribute(attribute_code: str) -> bool:
    """Etykiety opcji rozwiązujemy tylko dla atrybutów typu select; tekst zostaje bez zmian."""
    try:
        return akeneo_get_attribute(attribute_code).get("type") in SELECT_ATTRIBUTE_TYPES
    except Exception:
        return False


@st.cache_data(ttl=7200, show_spinner=False)
def _akeneo_option_label(attribute_code: str, option_code: str, locale: str) -> str:
    response = akeneo_request(
        "GET",
        _akeneo_root() + f"/api/rest/v1/attributes/{quote(attribute_code, safe='')}/options/{quote(option_code, safe='')}",
        headers=akeneo_headers(),
        max_attempts=2,
        timeout=10,
    )
    response.raise_for_status()
    labels = response.json().get("labels") or {}
    return str(
        labels.get(locale)
        or labels.get("pl_PL")
        or labels.get("en_US")
        or labels.get("en_GB")
        or labels.get("default")
        or next((value for value in labels.values() if value), "")
    ).strip()


def akeneo_get_option_label(attribute_code: str, option_code: str, token: str, locale: str = DEFAULT_LOCALE) -> str:
    if not option_code or not token:
        return _clean_option_slug(option_code)
    try:
        # Nieudane zapytanie nie trafia do cache, więc kolejna próba może się powieść.
        return _akeneo_option_label(attribute_code, option_code, locale) or _clean_option_slug(option_code)
    except Exception:
        return _clean_option_slug(option_code)


def _value_from_values(
    values: Dict,
    names: Sequence[str],
    channel: str,
    locale: str,
    token: str = "",
    resolve_option: bool = False,
    strict_scope: bool = False,
) -> str:
    for name in names:
        entries = values.get(name) or []
        for entry in entries:
            scope_ok = entry.get("scope") in (None, channel)
            locale_ok = entry.get("locale") in (None, locale)
            if scope_ok and locale_ok:
                return _entry_text(name, entry.get("data", ""), locale, token, resolve_option)
        if entries and not strict_scope:
            return _entry_text(name, entries[0].get("data", ""), locale, token, resolve_option)
    return ""


def _entry_text(name: str, data: Any, locale: str, token: str, resolve_option: bool) -> str:
    items = [str(item).strip() for item in data] if isinstance(data, list) else [safe_string_value(data)]
    items = [item for item in items if item]
    if resolve_option and token and items and akeneo_is_select_attribute(name):
        items = [akeneo_get_option_label(name, item, token, locale) or item for item in items]
    return ", ".join(items)


def _list_from_values(
    values: Dict,
    names: Sequence[str],
    channel: str,
    locale: str,
    *,
    token: str = "",
    resolve_option: bool = False,
) -> Tuple[List[str], str]:
    """Zwraca wszystkie wartości z pierwszego dostępnego atrybutu oraz jego kod."""
    unresolved_option_fallback: Tuple[List[str], str] = ([], "")
    for name in names:
        entries = values.get(name) or []
        selected_entry = None
        for entry in entries:
            if entry.get("scope") in (None, channel) and entry.get("locale") in (None, locale):
                selected_entry = entry
                break
        if selected_entry is None and entries:
            selected_entry = entries[0]
        if selected_entry is None:
            continue

        data = selected_entry.get("data", "")
        raw_items = data if isinstance(data, list) else split_contributor_names(safe_string_value(data))
        resolved: List[str] = []
        has_unresolved_option = False
        for raw_item in raw_items:
            raw_value = safe_string_value(raw_item)
            if not raw_value:
                continue
            value = (
                akeneo_get_option_label(name, raw_value, token, locale)
                if resolve_option and token and isinstance(data, list)
                else raw_value
            )
            if value:
                resolved.append(normalize_spaces(value))
                if (
                    resolve_option
                    and isinstance(data, list)
                    and normalize_for_compare(value) == normalize_for_compare(raw_value)
                    and re.fullmatch(r"[a-z0-9]{6,}", raw_value)
                ):
                    has_unresolved_option = True
        if has_unresolved_option:
            # Gdy API opcji chwilowo nie zwróci etykiety, nie przepuszczamy kodu
            # typu „albertoacerbis”. Preferujemy kolejne tekstowe pole autora.
            unresolved_option_fallback = (list(dict.fromkeys(resolved)), name)
            continue
        return list(dict.fromkeys(resolved)), name
    return unresolved_option_fallback


def parse_akeneo_product(item: Dict, channel: str, locale: str, token: str = "") -> Dict:
    values = item.get("values", {})
    publisher = _value_from_values(values, ["publisher", "wydawnictwo", "brand", "marka"], channel, locale, token=token, resolve_option=True)
    year = _value_from_values(values, ["year", "rok_wydania"], channel, locale)
    pages = _value_from_values(values, ["pages", "liczba_stron"], channel, locale)
    cover = _value_from_values(values, ["cover_type", "oprawa"], channel, locale, token=token, resolve_option=True)
    details = ", ".join(
        f"{label}: {value}"
        for label, value in (("Wydawnictwo", publisher), ("Rok", year), ("Strony", pages), ("Oprawa", cover))
        if value
    )
    contributors, contributor_source = _list_from_values(
        values,
        ["editor", "editors", "redaktor", "redaktorzy", "redakcja", "redakcja_naukowa", "author", "autor"],
        channel,
        locale,
        token=token,
        resolve_option=True,
    )
    author = format_contributor_names(contributors)
    contributor_role = (
        "redakcja naukowa"
        if contributor_source in {"editor", "editors", "redaktor", "redaktorzy", "redakcja", "redakcja_naukowa"}
        else ""
    )
    return {
        "identifier": item.get("identifier", ""),
        "title": _value_from_values(values, ["name"], channel, locale) or item.get("identifier", ""),
        "description": _value_from_values(values, ["description"], channel, locale),
        "author": author,
        "contributors": contributors,
        "contributor_role": contributor_role,
        "publisher": publisher,
        "year": year,
        "pages": pages,
        "cover_type": cover,
        "ean": _value_from_values(values, ["ean"], channel, locale),
        "isbn": _value_from_values(values, ["isbn"], channel, locale),
        "details": details,
        "updated": item.get("updated", ""),
        "enabled": bool(item.get("enabled", False)),
        "categories": item.get("categories", []),
        "associations": item.get("associations", {}),
        **{field: _value_from_values(values, codes, channel, locale, token=token, resolve_option=True, strict_scope=True)
           for field, codes in LINK_FIELDS.items()},
    }


def akeneo_iter_products_search_after(
    token: str,
    channel: str,
    locale: str,
    *,
    category: Optional[str] = None,
    enabled_only: bool = True,
    only_with_description: bool = True,
    max_products: Optional[int] = None,
) -> Iterator[Dict]:
    url = _akeneo_root() + "/api/rest/v1/products"
    search: Dict[str, List[Dict]] = {}
    if enabled_only:
        search["enabled"] = [{"operator": "=", "value": True}]
    if category:
        search["categories"] = [{"operator": "IN", "value": [category]}]
    if only_with_description:
        search["description"] = [{"operator": "NOT EMPTY", "scope": channel, "locale": locale}]

    params = {
        "pagination_type": "search_after",
        "limit": 100,
        "scope": channel,
        "locales": locale,
        "with_count": "false",
    }
    existing_attributes = akeneo_existing_attribute_codes(token)
    if existing_attributes:
        params["attributes"] = ",".join(existing_attributes)
    if search:
        params["search"] = json.dumps(search, ensure_ascii=False)

    yielded = 0
    next_url: Optional[str] = url
    next_params: Optional[Dict] = params
    while next_url:
        response = akeneo_request(
            "GET",
            next_url,
            headers=akeneo_headers(token),
            params=next_params,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("_embedded", {}).get("items", [])
        for item in items:
            parsed = parse_akeneo_product(item, channel, locale, token=token)
            if only_with_description and not strip_html(parsed.get("description", "")):
                continue
            yield parsed
            yielded += 1
            if max_products and yielded >= max_products:
                return
        next_href = payload.get("_links", {}).get("next", {}).get("href")
        next_url = urljoin(_akeneo_root(), next_href) if next_href else None
        next_params = None


def akeneo_search_products(
    search_query: str,
    token: str,
    limit: int = 20,
    locale: str = DEFAULT_LOCALE,
) -> List[Dict]:
    url = _akeneo_root() + "/api/rest/v1/products"
    products: Dict[str, Dict] = {}
    searches = [
        {"identifier": [{"operator": "CONTAINS", "value": search_query}]},
        {"name": [{"operator": "CONTAINS", "value": search_query, "locale": locale}]},
    ]
    for search_filter in searches:
        response = akeneo_request(
            "GET",
            url,
            headers=akeneo_headers(token),
            params={"limit": limit, "search": json.dumps(search_filter)},
            max_attempts=3,
        )
        if response.status_code != 200:
            continue
        for item in response.json().get("_embedded", {}).get("items", []):
            # Do listy wyników wystarczy tytuł; bez rozwiązywania etykiet opcji per produkt.
            parsed = parse_akeneo_product(item, DEFAULT_CHANNEL, locale, token="")
            sku = parsed["identifier"]
            if sku:
                products[sku] = {
                    "identifier": sku,
                    "title": parsed["title"],
                    "family": item.get("family", ""),
                    "enabled": item.get("enabled", False),
                }
    return list(products.values())[:limit]


@st.cache_data(ttl=3600, show_spinner=False)
def akeneo_fetch_categories(_token: str, locale: str) -> List[Dict]:
    url = _akeneo_root() + "/api/rest/v1/categories"
    categories: List[Dict] = []
    params: Optional[Dict] = {"limit": 100}
    next_url: Optional[str] = url
    while next_url:
        response = akeneo_request("GET", next_url, headers=akeneo_headers(), params=params)
        if response.status_code != 200:
            break
        payload = response.json()
        for item in payload.get("_embedded", {}).get("items", []):
            labels = item.get("labels", {})
            categories.append(
                {
                    "code": item.get("code", ""),
                    "label": labels.get(locale) or labels.get("pl_PL") or item.get("code", ""),
                    "parent": item.get("parent"),
                }
            )
        next_href = payload.get("_links", {}).get("next", {}).get("href")
        next_url = urljoin(_akeneo_root(), next_href) if next_href else None
        params = None
    return sorted(categories, key=lambda item: item["label"])


def akeneo_fetch_backlog(
    token: str,
    channel: str,
    locale: str,
    limit: int = 100,
    category: Optional[str] = None,
    exclude_updated_days: Optional[int] = None,
    only_without_desc: bool = False,
    max_desc_len: Optional[int] = None,
) -> List[Dict]:
    products: List[Dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=exclude_updated_days or 0)
    for product in akeneo_iter_products_search_after(
        token,
        channel,
        locale,
        category=category,
        enabled_only=True,
        only_with_description=False,
        max_products=max(limit * 4, limit),
    ):
        description = strip_html(product.get("description", ""))
        if only_without_desc and description:
            continue
        if not only_without_desc and max_desc_len is not None and len(description) >= max_desc_len:
            continue
        if exclude_updated_days and product.get("updated"):
            try:
                updated = datetime.fromisoformat(product["updated"].replace("Z", "+00:00"))
                if updated >= cutoff:
                    continue
            except ValueError:
                pass
        products.append(
            {
                "identifier": product["identifier"],
                "title": product["title"],
                "desc_len": len(description),
                "updated": product.get("updated", "")[:10],
            }
        )
        if len(products) >= limit:
            break
    products.sort(key=lambda item: item["desc_len"])
    return products[:limit]


def akeneo_update_description(
    sku: str,
    html_description: str,
    channel: str,
    locale: str = DEFAULT_LOCALE,
) -> bool:
    token = akeneo_get_token()
    attr_desc = akeneo_get_attribute("description", token)
    payload = {
        "values": {
            "description": [
                {
                    "data": html_description,
                    "scope": channel if attr_desc.get("scopable") else None,
                    "locale": locale if attr_desc.get("localizable") else None,
                }
            ]
        }
    }
    try:
        attr_seo = akeneo_get_attribute("opisy_seo", token)
        payload["values"]["opisy_seo"] = [
            {
                "data": True,
                "scope": channel if attr_seo.get("scopable") else None,
                "locale": locale if attr_seo.get("localizable") else None,
            }
        ]
    except Exception:
        pass

    response = akeneo_request(
        "PATCH",
        _akeneo_root() + f"/api/rest/v1/products/{quote(sku, safe='')}",
        headers=akeneo_headers(token, "application/json"),
        data=json.dumps(payload, ensure_ascii=False),
    )
    if response.status_code in (200, 204):
        return True
    raise RuntimeError(f"Błąd Akeneo {response.status_code}: {response.text[:300]}")


def _prepare_product_data(product_details: Dict) -> Dict:
    contributors = product_details.get("contributors") or split_contributor_names(
        safe_string_value(product_details.get("author"))
    )
    return {
        "identifier": product_details.get("identifier", ""),
        "categories": product_details.get("categories", []),
        "associations": product_details.get("associations", {}),
        **{field: safe_string_value(product_details.get(field)) for field in LINK_FIELDS},
        "title": safe_string_value(product_details.get("title")),
        "author": format_contributor_names(contributors),
        "contributors": contributors,
        "contributor_role": safe_string_value(product_details.get("contributor_role")),
        "publisher": safe_string_value(product_details.get("publisher")),
        "details": safe_string_value(product_details.get("details")),
        "description": safe_string_value(product_details.get("description")),
        "isbn": safe_string_value(product_details.get("isbn")),
        "ean": safe_string_value(product_details.get("ean")),
    }


AKENEO_REQUEST_SEMAPHORE = threading.BoundedSemaphore(AKENEO_MAX_WORKERS)


def akeneo_get_product_details(
    sku: str,
    token: str,
    channel: str = DEFAULT_CHANNEL,
    locale: str = DEFAULT_LOCALE,
) -> Optional[Dict]:
    with AKENEO_REQUEST_SEMAPHORE:
        response = akeneo_request(
            "GET",
            _akeneo_root() + f"/api/rest/v1/products/{quote(sku, safe='')}",
            headers=akeneo_headers(token),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return parse_akeneo_product(response.json(), channel, locale, token=token)


def akeneo_fetch_products_by_identifiers(
    token: str,
    channel: str,
    locale: str,
    identifiers: Sequence[str],
) -> Dict[str, Dict]:
    if not identifiers:
        return {}
    search = {"identifier": [{"operator": "IN", "value": list(identifiers)}]}
    params: Dict[str, object] = {
        "limit": 100,
        "scope": channel,
        "locales": locale,
        "with_count": "false",
        "search": json.dumps(search, ensure_ascii=False),
    }
    existing_attributes = akeneo_existing_attribute_codes(token)
    if existing_attributes:
        params["attributes"] = ",".join(existing_attributes)
    with AKENEO_REQUEST_SEMAPHORE:
        response = akeneo_request(
            "GET",
            _akeneo_root() + "/api/rest/v1/products",
            headers=akeneo_headers(token),
            params=params,
        )

    if response.status_code in {400, 414, 422}:
        products: Dict[str, Dict] = {}
        # Globalny semafor nadal ogranicza faktyczne requesty do 4, nawet jeśli
        # kilka chunków równolegle trafi w fallback.
        with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
            futures = {
                executor.submit(akeneo_get_product_details, sku, token, channel, locale): sku
                for sku in identifiers
            }
            for future in as_completed(futures):
                product = future.result()
                if product and product.get("identifier"):
                    products[product["identifier"]] = product
        return products

    response.raise_for_status()
    products: Dict[str, Dict] = {}
    for item in response.json().get("_embedded", {}).get("items", []):
        parsed = parse_akeneo_product(item, channel, locale, token=token)
        if parsed.get("identifier"):
            products[parsed["identifier"]] = parsed
    return products
