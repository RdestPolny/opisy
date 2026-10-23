"""Produkty i kategorie powiązane z kartą produktu Bookland.

Główne źródło to publiczne API Luigi's Box, z którego korzysta sama karta
produktu Bookland (karuzela „Inni kupili również”, rekomender
``item_detail_alternatives``). To te same produkty, które klient widzi pod
kartą, ale razem z atrybutami (kategorie, przedmiot, poziom, seria, autor),
więc Jev dostaje do oceny konkretne, opisane cele.

Zapasowe źródło to Firecrawl: renderuje kartę produktu z JavaScriptem i z HTML
odczytujemy karuzelę rekomendacji oraz breadcrumbs. Używamy go tylko wtedy,
gdy API Luigi's Box nie zwróci wyników, a w sekretach jest ``FIRECRAWL_API_KEY``.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests

LUIGIS_TRACKER_ID = "642642-825688"
LUIGIS_API = "https://live.luigisbox.tech"
LUIGIS_RECOMMENDER = "item_detail_alternatives"
BOOKLAND_ORIGIN = "https://bookland.com.pl"
BOOKLAND_HOSTS = {"bookland.com.pl", "www.bookland.com.pl"}
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
RECO_CONTAINER_ID = "lb-recommend-element-item_detail_alternatives"

_TIMEOUT = (5, 12)
_FIRECRAWL_TIMEOUT = 60
# Kategorie zbyt ogólne, by link do nich pomagał czytelnikowi opisu.
_GENERIC_CATEGORY_PATHS = {"/ksiazki", "/podreczniki-ogolne", "/podreczniki-ogolne/szkolne", "/wydawnictwa",
                           "/promocje", "/specjalne", "/zabawki", "/gry", "/serie"}
_EDITION_NOISE = {
    "oprawa", "twarda", "miekka", "wydanie", "wyd", "edycja", "filmowe", "filmowa", "kieszonkowe",
    "kieszonkowa", "specjalna", "specjalne", "elegancka", "rozszerzona", "nowe", "nowa", "ebook",
    "audiobook", "wydawnictwo",
}


# ───────────────────────────── pomocnicze ─────────────────────────────

def _norm(value) -> str:
    text = str(value or "").translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _strip_tags(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(value or ""))).strip()


_EMPTY_VALUES = {"nie dotyczy", "brak", "inna", "inne", "praca zbiorowa", "-", "n/a"}


def _values(value, *, split_pipes: bool = False) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in items:
        parts = str(item or "").split("|") if split_pipes else [item]
        for part in parts:
            text = re.sub(r"\s+", " ", _strip_tags(part))
            if text and text.casefold() not in _EMPTY_VALUES and text not in result:
                result.append(text)
    return result


def _raw_list(value) -> List[str]:
    """Lista bez deduplikacji – potrzebna, gdy dwie listy są równoległe (nazwy i URL-e kategorii)."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [_strip_tags(item) for item in items]


def _first(value) -> str:
    values = _values(value)
    return values[0] if values else ""


def _as_int(value) -> Optional[int]:
    try:
        return int(float(_first(value)))
    except (TypeError, ValueError):
        return None


def canonical_bookland_url(value: str) -> str:
    """Zwraca kanoniczny HTTPS URL Booklandu albo pusty tekst."""
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if "?" in raw or "#" in raw or any(part in {".", ".."} for part in unquote(urlsplit(raw).path).split("/")):
        return ""
    url = urljoin(BOOKLAND_ORIGIN + "/", raw)
    parsed = urlsplit(url)
    path = unquote(parsed.path or "")
    if (parsed.scheme != "https" or parsed.hostname not in BOOKLAND_HOSTS or parsed.port is not None
            or parsed.username or parsed.password or not path or path == "/"
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ch.isspace() for ch in path)):
        return ""
    return f"{BOOKLAND_ORIGIN}{parsed.path.rstrip('/')}"


def title_slug_url(title: str) -> str:
    """Adres karty zbudowany ze slugu tytułu (tak Bookland tworzy url_key)."""
    text = (title or "").translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return canonical_bookland_url("/" + slug) if slug else ""


def last_segment_url(url: str) -> str:
    """Skraca stary adres z kategorią (/ksiazki/.../tytul) do kanonicznego /tytul."""
    canonical = canonical_bookland_url(url)
    return canonical_bookland_url("/" + canonical.rsplit("/", 1)[-1]) if canonical else ""


def product_item_ids(product: Dict) -> List[str]:
    """Identyfikatory, pod którymi Luigi's Box zna produkt (na Booklandzie to EAN/SKU)."""
    ids = []
    for key in ("identifier", "sku", "ean", "isbn"):
        raw = str(product.get(key) or "").strip()
        for candidate in (raw, re.sub(r"[^0-9Xx]", "", raw)):
            if candidate and len(candidate) <= 64 and candidate not in ids:
                ids.append(candidate)
    return ids


def _title_core(title: str) -> str:
    tokens = [t for t in _norm(title).split() if t not in _EDITION_NOISE and not re.fullmatch(r"\d{4}", t)]
    return " ".join(tokens)


def same_work(title_a: str, title_b: str) -> bool:
    """Czy dwa tytuły to w praktyce to samo dzieło w innym wydaniu/oprawie."""
    a, b = _title_core(title_a), _title_core(title_b)
    if not a or not b:
        return False
    # Podobna nazwa nie czyni różnych tomów, części ani klas jednym dziełem.
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    roman_part = r"\b(?:tom|t|czesc|cz|vol|volume|book)\s+([ivx]+)\b"
    if re.findall(roman_part, a) != re.findall(roman_part, b):
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.93


# ───────────────────────────── Luigi's Box ─────────────────────────────

def _is_in_stock(attrs: Dict) -> bool:
    """Stan magazynowy w sklepie z ``availability_boolean``/``availability``. Pole ``is_available``
    ma w indeksie Booklandu zawsze 0, więc go nie używamy; brak danych traktujemy jak dostępny."""
    for key in ("availability_boolean", "availability"):
        raw = attrs.get(key)
        raw = raw[0] if isinstance(raw, list) and raw else raw
        try:
            return int(float(raw)) > 0
        except (TypeError, ValueError):
            continue
    return True


def summarize_luigis_hit(hit: Dict) -> Optional[Dict]:
    """Zamienia trafienie Luigi's Box w zwięzły opis produktu dla Jev i promptu."""
    if not isinstance(hit, dict):
        return None
    attrs = hit.get("attributes") or {}
    if not isinstance(attrs, dict):
        return None
    # Atrybut ``url`` to aktualny url_key. ``web_url`` bywa
    # starym adresem z kategorią/promocją, który przekierowuje.
    url = ""
    for candidate in (_first(attrs.get("url")), _first(attrs.get("web_url")), _first(attrs.get("original_url"))):
        url = canonical_bookland_url(candidate)
        if url:
            break
    title = (_first(attrs.get("title.untouched")) or _first(attrs.get("title_common"))
             or _first(attrs.get("title")) or _first(attrs.get("nazwa_alternatywna")))
    if not url or not title:
        return None
    paths = [path for path in _values(attrs.get("category_upto_lvl_5")) if path]
    category_paths = [" > ".join(part.strip() for part in path.split("|") if part.strip()) for path in paths]
    return {
        "id": str(hit.get("url") or _first(attrs.get("lb_id")) or _first(attrs.get("sku"))),
        "title": title,
        "url": url,
        "categories": category_paths[:4],
        "subject": ", ".join(_values(attrs.get("przedmiotnauczania"))),
        "level": ", ".join(_values(attrs.get("poziomnauczania"))),
        "series": ", ".join(_values(attrs.get("seriawydawnicza"))),
        "authors": _values(attrs.get("autor") or attrs.get("author.untouched"), split_pipes=True)[:4],
        "publisher": _first(attrs.get("wydawnictwo")) or _first(attrs.get("brand")),
        "binding": _first(attrs.get("oprawa")),
        "publication_year": (_first(attrs.get("publication_year")) or "").split(".")[0],
        "available": _is_in_stock(attrs),
        "visible": _first(attrs.get("widoczne_bookland")) != "0",
        "crosslinking": _first(attrs.get("use_in_crosslinking")) != "0",
        "category_names": _raw_list(attrs.get("category")),
        "category_urls": _raw_list(attrs.get("category_urls")),
        "category_tree": paths,
    }


def category_targets_from_item(item: Dict) -> List[Dict]:
    """Kategorie źródłowego produktu z ich pełną ścieżką, od najgłębszych."""
    names, urls = item.get("category_names") or [], item.get("category_urls") or []
    if not names or len(names) != len(urls):
        return []
    by_path = {}
    for name, path in zip(names, urls):
        path = "/" + path.strip("/")
        if path != "/" and name:
            by_path[path] = name
    targets = []
    for path, name in by_path.items():
        if path in _GENERIC_CATEGORY_PATHS or path.startswith("/promocje/") or path.startswith("/specjalne/"):
            continue
        url = canonical_bookland_url(path)
        if not url:
            continue
        segments = path.strip("/").split("/")
        trail = [by_path.get("/" + "/".join(segments[:index])) for index in range(1, len(segments) + 1)]
        label = " > ".join(part for part in trail if part)
        targets.append({
            "url": url,
            "name": name,
            "label": label or name,
            "depth": len(segments),
            "publisher": path.startswith("/wydawnictwa/"),
        })
    # Najpierw najgłębsze kategorie tematyczne, kategoria wydawnictwa na końcu.
    targets.sort(key=lambda t: (t["publisher"], -t["depth"], t["label"]))
    return targets


def luigis_find_item(item_id: str, *, session=None, timeout=_TIMEOUT) -> Optional[Dict]:
    response = (session or requests).get(
        f"{LUIGIS_API}/search",
        params=[("tracker_id", LUIGIS_TRACKER_ID), ("f[]", "type:product"), ("f[]", f"lb_id:{item_id}"), ("size", "1")],
        timeout=timeout,
    )
    response.raise_for_status()
    hits = ((response.json() or {}).get("results") or {}).get("hits") or []
    for hit in hits:
        if str(hit.get("url")) == item_id:
            return hit
    return None


# Status produktu w sklepie dla listy wyboru: brak karty oznacza brak URL-a i rekomendacji do linkowania.
SHOP_STATUS_LABELS = {
    "missing": "brak w sklepie",
    "hidden": "ukryty w sklepie",
    "out_of_stock": "brak na stanie",
}


def luigis_item_statuses(item_ids: Iterable[str], *, session=None, chunk_size: int = 50,
                         timeout=_TIMEOUT) -> Dict[str, Dict]:
    """Sprawdza wiele SKU naraz (filtry ``lb_id`` łączą się przez OR) i zwraca
    {sku: {"status": ok|out_of_stock|hidden|missing, "url": ...}}."""
    ids = list(dict.fromkeys(str(item).strip() for item in item_ids if str(item).strip()))
    statuses: Dict[str, Dict] = {}
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        params = [("tracker_id", LUIGIS_TRACKER_ID), ("f[]", "type:product"),
                  *(("f[]", f"lb_id:{item}") for item in chunk), ("size", str(len(chunk)))]
        response = (session or requests).get(f"{LUIGIS_API}/search", params=params, timeout=timeout)
        response.raise_for_status()
        for hit in ((response.json() or {}).get("results") or {}).get("hits") or []:
            item = summarize_luigis_hit(hit)
            if not item or str(hit.get("url")) not in chunk:
                continue
            status = "hidden" if not item["visible"] else "out_of_stock" if not item["available"] else "ok"
            statuses[str(hit.get("url"))] = {"status": status, "url": item["url"]}
    for item in ids:
        statuses.setdefault(item, {"status": "missing", "url": ""})
    return statuses


def luigis_search_by_title(title: str, *, session=None, timeout=_TIMEOUT) -> Optional[Dict]:
    """Ostatnia deska ratunku: znajdź produkt po tytule, ale tylko przy niemal identycznym tytule."""
    if not _norm(title):
        return None
    response = (session or requests).get(
        f"{LUIGIS_API}/search",
        params=[("tracker_id", LUIGIS_TRACKER_ID), ("f[]", "type:product"), ("q", title[:200]), ("size", "5")],
        timeout=timeout,
    )
    response.raise_for_status()
    hits = ((response.json() or {}).get("results") or {}).get("hits") or []
    wanted = _norm(title)
    for hit in hits:
        attrs = hit.get("attributes") or {}
        found = _norm(_first(attrs.get("title.untouched")) or _first(attrs.get("title")))
        if found and SequenceMatcher(None, wanted, found).ratio() >= 0.96:
            return hit
    return None


def luigis_recommend(item_id: str, *, size: int = 16, session=None, timeout=_TIMEOUT) -> List[Dict]:
    body = [{
        "recommendation_type": LUIGIS_RECOMMENDER,
        "recommender_client_identifier": LUIGIS_RECOMMENDER,
        "size": max(1, min(int(size), 30)),
        "item_ids": [item_id],
        "user_id": "opisy-internal-linking",
    }]
    response = (session or requests).post(
        f"{LUIGIS_API}/v1/recommend",
        params={"tracker_id": LUIGIS_TRACKER_ID},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("Nieoczekiwana odpowiedź Luigi's Box.")
    hits = payload[0].get("hits")
    if not isinstance(hits, list):
        raise ValueError(payload[0].get("reason") or "Luigi's Box nie zwrócił rekomendacji.")
    return hits


def fetch_related_from_luigis(product: Dict, *, size: int = 16, session=None) -> Dict:
    result = {"source": None, "products": [], "categories": [], "warnings": [], "provider": "luigisbox"}
    source_hit = None
    for item_id in product_item_ids(product):
        source_hit = luigis_find_item(item_id, session=session)
        if source_hit:
            break
    if not source_hit:
        source_hit = luigis_search_by_title(str(product.get("title") or ""), session=session)
    if not source_hit:
        result["warnings"].append("Luigi's Box nie zna tego produktu (SKU/EAN ani tytuł); brak rekomendacji spod karty.")
        return result
    source = summarize_luigis_hit(source_hit)
    result["source"] = source
    if source:
        result["categories"] = category_targets_from_item(source)
    hits = luigis_recommend(str(source_hit.get("url")), size=size, session=session)
    products = []
    for position, hit in enumerate(hits):
        item = summarize_luigis_hit(hit)
        if item:
            item["position"] = position
            products.append(item)
    result["products"] = products
    if not products:
        result["warnings"].append("Luigi's Box nie zwrócił produktów powiązanych dla tej karty.")
    return result


# ───────────────────────────── Firecrawl (zapas) ─────────────────────────────

class _ProductPageParser(HTMLParser):
    """Wyciąga breadcrumbs i karuzelę rekomendacji z wyrenderowanej karty produktu."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.breadcrumbs: List[Dict] = []
        self.recommended: List[Dict] = []
        self.title = ""
        self._reco_depth = 0
        self._crumb: Optional[Dict] = None
        self._product: Optional[Dict] = None
        self._capture_name = False
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self._reco_depth:
            self._reco_depth += 1
        elif attrs.get("id") == RECO_CONTAINER_ID:
            self._reco_depth = 1
        if tag == "h1":
            self._in_h1 = True
        if tag == "a" and "Breadcrumb-Link" in classes:
            self._crumb = {"href": attrs.get("href") or "", "name": ""}
        if self._reco_depth and tag == "a" and attrs.get("href"):
            self._product = {"id": attrs.get("data-lb-id") or "", "href": attrs["href"], "name": ""}
        if self._product is not None and tag == "img" and not self._product["name"]:
            self._product["alt"] = attrs.get("alt") or ""
        if self._product is not None and "ProductCard-Name" in classes:
            self._capture_name = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        if tag == "a" and self._crumb is not None:
            if self._crumb["name"].strip():
                self.breadcrumbs.append({"href": self._crumb["href"], "name": self._crumb["name"].strip()})
            self._crumb = None
        if tag == "a" and self._product is not None:
            name = self._product["name"].strip() or self._product.get("alt", "").strip()
            if name:
                self.recommended.append({"id": self._product["id"], "href": self._product["href"], "name": name})
            self._product = None
            self._capture_name = False
        if tag == "p":
            self._capture_name = False
        if self._reco_depth:
            self._reco_depth -= 1

    def handle_data(self, data):
        if self._in_h1:
            self.title += data
        if self._crumb is not None:
            self._crumb["name"] += data
        if self._capture_name and self._product is not None:
            self._product["name"] += data


def parse_product_page(html: str) -> Dict:
    parser = _ProductPageParser()
    parser.feed(html or "")
    parser.close()
    seen, products = set(), []
    for item in parser.recommended:
        url = canonical_bookland_url(item["href"])
        if url and url not in seen:
            seen.add(url)
            products.append({"id": item["id"], "title": re.sub(r"\s+", " ", item["name"]), "url": url})
    crumbs = []
    for crumb in parser.breadcrumbs:
        url = canonical_bookland_url(crumb["href"])
        if url:
            crumbs.append({"url": url, "name": re.sub(r"\s+", " ", crumb["name"])})
    return {"title": re.sub(r"\s+", " ", parser.title).strip(), "breadcrumbs": crumbs, "products": products}


def firecrawl_scrape_html(url: str, api_key: str, *, session=None, timeout=_FIRECRAWL_TIMEOUT) -> str:
    response = (session or requests).post(
        FIRECRAWL_SCRAPE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "url": url,
            "formats": ["rawHtml"],
            "onlyMainContent": False,
            "waitFor": 4000,
            # Karuzela Luigi's Box doładowuje się po przewinięciu strony.
            "actions": [
                {"type": "scroll", "direction": "down"},
                {"type": "wait", "milliseconds": 2500},
                {"type": "scroll", "direction": "down"},
                {"type": "wait", "milliseconds": 2500},
            ],
            "timeout": 45000,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json() or {}
    data = payload.get("data") or {}
    html = data.get("rawHtml") or data.get("html") or ""
    if not payload.get("success", True) or not html:
        raise ValueError("Firecrawl nie zwrócił HTML karty produktu.")
    return html


def fetch_related_via_firecrawl(product_url: str, api_key: str, *, session=None) -> Dict:
    result = {"source": None, "products": [], "categories": [], "warnings": [], "provider": "firecrawl"}
    page = parse_product_page(firecrawl_scrape_html(product_url, api_key, session=session))
    own = canonical_bookland_url(product_url)
    crumbs = [c for c in page["breadcrumbs"] if c["url"] != own]
    names = {urlsplit(c["url"]).path: c["name"] for c in crumbs}
    for crumb in crumbs:
        path = urlsplit(crumb["url"]).path
        if path in _GENERIC_CATEGORY_PATHS:
            continue
        segments = path.strip("/").split("/")
        trail = [names.get("/" + "/".join(segments[:i])) for i in range(1, len(segments) + 1)]
        result["categories"].append({"url": crumb["url"], "name": crumb["name"],
                                     "label": " > ".join(t for t in trail if t) or crumb["name"],
                                     "depth": len(segments), "publisher": path.startswith("/wydawnictwa/")})
    result["categories"].sort(key=lambda t: (t["publisher"], -t["depth"], t["label"]))
    for position, item in enumerate(page["products"]):
        if item["url"] == own:
            continue
        result["products"].append({
            "id": item["id"], "title": item["title"], "url": item["url"], "categories": [], "subject": "",
            "level": "", "series": "", "authors": [], "publisher": "", "binding": "", "publication_year": "",
            "available": True, "visible": True, "crosslinking": True, "position": position,
        })
    result["source"] = {"title": page["title"], "url": own}
    if not result["products"]:
        result["warnings"].append("Firecrawl: na karcie produktu nie znaleziono karuzeli rekomendacji.")
    return result


# ───────────────────────────── API modułu ─────────────────────────────

def fetch_related_products(
    product: Dict,
    *,
    size: int = 16,
    firecrawl_api_key: str = "",
    product_url: str = "",
    session=None,
) -> Dict:
    """Pobiera produkty polecane pod kartą produktu oraz kategorie z jego ścieżki.

    Nigdy nie rzuca wyjątku sieciowego – błędy trafiają do ``warnings``.
    """
    result = {"source": None, "products": [], "categories": [], "warnings": [], "provider": ""}
    try:
        result = fetch_related_from_luigis(product, size=size, session=session)
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        result["warnings"].append(f"Luigi's Box niedostępny ({type(exc).__name__}).")

    if result["products"] or not firecrawl_api_key:
        return result

    # Adres z Luigi's Box jest pewniejszy niż slug zbudowany z tytułu.
    url = ((result.get("source") or {}).get("url") or "") or product_url
    if not url:
        result["warnings"].append("Firecrawl pominięty: nie znamy adresu karty produktu na Booklandzie.")
        return result
    try:
        scraped = fetch_related_via_firecrawl(url, firecrawl_api_key, session=session)
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        result["warnings"].append(f"Firecrawl nie pobrał karty produktu ({type(exc).__name__}).")
        return result
    scraped["warnings"] = result["warnings"] + scraped["warnings"]
    if result.get("categories") and not scraped["categories"]:
        scraped["categories"] = result["categories"]
    if result.get("source"):
        scraped["source"] = result["source"]
    return scraped


def _shared(a: Iterable[str], b: Iterable[str]) -> List[str]:
    norm_b = {_norm(x): x for x in b if _norm(x)}
    return [x for x in a if _norm(x) in norm_b]


def relation_facts(source: Dict, item: Dict) -> List[str]:
    """Twarde, sprawdzalne powiązania (bez zgadywania) – dla Jev i Gemini."""
    facts = []
    if source.get("series") and item.get("series") and _norm(source["series"]) == _norm(item["series"]):
        facts.append(f"ta sama seria: {item['series']}")
    if source.get("subject") and item.get("subject") and _norm(source["subject"]) == _norm(item["subject"]):
        facts.append(f"ten sam przedmiot: {item['subject']}")
    if source.get("level") and item.get("level") and _norm(source["level"]) == _norm(item["level"]):
        facts.append(f"ten sam poziom nauczania: {item['level']}")
    authors = _shared(item.get("authors") or [], source.get("authors") or [])
    if authors:
        facts.append("ten sam autor: " + ", ".join(authors[:2]))
    common = _shared(item.get("categories") or [], source.get("categories") or [])
    if common:
        facts.append(f"wspólna kategoria: {common[0]}")
    return facts


def rank_related_products(source: Optional[Dict], products: List[Dict], *, exclude_ids=(), source_title: str = "",
                          limit: int = 6) -> List[Dict]:
    """Wstępny, deterministyczny ranking przed Jev; odrzuca self-linki i inne wydania tego samego tytułu."""
    source = source or {}
    excluded = {str(x) for x in exclude_ids if x}
    if source.get("id"):
        excluded.add(str(source["id"]))
    title = source_title or source.get("title") or ""
    own_url = source.get("url")
    ranked = []
    for item in products:
        if (str(item.get("id")) in excluded or item.get("url") == own_url or not item.get("visible", True)
                or not item.get("crosslinking", True) or same_work(title, item.get("title", ""))):
            continue
        facts = relation_facts(source, item)
        # Kolejność Luigi's Box to sygnał popularności, nie trafności dla opisu, więc
        # konkretne powiązania (seria, autor) ważą więcej niż pozycja w karuzeli.
        score = -float(item.get("position", 0)) * 0.06
        score += 2.5 * any(f.startswith("ta sama seria") for f in facts)
        score += 2.0 * any(f.startswith("ten sam autor") for f in facts)
        score += 0.8 * any(f.startswith("ten sam przedmiot") for f in facts)
        score += 0.5 * any(f.startswith("ten sam poziom") for f in facts)
        score += 0.2 * any(f.startswith("wspólna kategoria") for f in facts)
        score += 0.3 if item.get("available", True) else 0.0
        ranked.append((score, item.get("position", 0), {**item, "relation": facts}))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    seen, result = set(), []
    for _, _, item in ranked:
        # Bez duplikatów URL i bez kilku wydań tego samego tytułu (oprawa twarda/miękka itp.).
        if item["url"] in seen or any(same_work(item["title"], kept["title"]) for kept in result):
            continue
        seen.add(item["url"])
        result.append(item)
        if len(result) >= limit:
            break
    return result
