from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import requests

BOOKLAND_SITEMAP_INDEX_URL = "https://bookland.com.pl/pub/sitemap_index.xml"
DEFAULT_SITEMAP_DB_PATH = Path(".streamlit/bookland_sitemap.sqlite3")
BUNDLED_SNAPSHOT_DIR = Path("data/bookland_sitemap_snapshot")
BUNDLED_SNAPSHOT_MANIFEST = BUNDLED_SNAPSHOT_DIR / "manifest.json"
BOOKLAND_HOSTS = {"bookland.com.pl", "www.bookland.com.pl"}
CACHE_SCHEMA_VERSION = "1"
MAX_SITEMAPS = 256
MAX_URLS = 750_000
DEFAULT_WORKERS = 3
_REQUEST_TIMEOUT = (8, 45)
_REFRESH_LOCK = threading.RLock()

_STOPWORDS = {
    "a", "aby", "albo", "ale", "ani", "bez", "dla", "do", "i", "jak", "na", "nie", "o", "od",
    "oraz", "po", "pod", "przez", "się", "to", "w", "z", "ze",
    "book", "books", "ebook", "online", "student", "students", "teacher", "teachers",
    "ksiazka", "ksiazki", "produkt", "wydanie", "podrecznik", "cwiczenia",
}
_BLOCKED_PATH_PREFIXES = (
    "/customer/", "/checkout/", "/catalogsearch/", "/wishlist/", "/sales/",
    "/pub/", "/media/", "/static/",
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _normalize_text(value: str) -> str:
    # NFKD nie rozkłada polskiego ł/Ł, więc transliterujemy je jawnie przed
    # usuwaniem znaków diakrytycznych. Dzięki temu szkoła -> szkola zamiast szko a.
    text = (value or "").translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _tokens(value: str) -> List[str]:
    return [
        token for token in _normalize_text(value).split()
        if len(token) >= 2 and token not in _STOPWORDS
    ]


def _validate_bookland_url(url: str, *, xml: bool = False) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("Nieprawidłowy URL Bookland.")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in BOOKLAND_HOSTS:
        raise ValueError("Sitemap może zawierać wyłącznie adresy HTTPS w domenie bookland.com.pl.")
    if parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment:
        raise ValueError("URL Bookland musi być kanoniczny i bez parametrów.")
    path = unquote(parsed.path or "/")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Nieprawidłowa ścieżka URL Bookland.")
    if xml and not (path.endswith(".xml") or path.endswith(".xml.gz")):
        raise ValueError("Nieprawidłowy adres sitemap XML.")
    return url


def _label_from_url(url: str) -> str:
    path = unquote(urlsplit(url).path).rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(?:html?|php)$", "", slug, flags=re.I)
    label = re.sub(r"[-_]+", " ", slug)
    label = re.sub(r"\s+", " ", label).strip()
    return label or "Bookland"


def _infer_kind(source_sitemap: str, url: str) -> str:
    source = source_sitemap.casefold()
    path = urlsplit(url).path.casefold()
    if "category" in source or "/category/" in path or "/kategoria/" in path:
        return "category"
    if "product" in source or "/product/" in path:
        return "product"
    return "sitemap"


def _read_xml_stream(url: str) -> Tuple[List[str], List[Dict[str, str]]]:
    _validate_bookland_url(url, xml=True)
    headers = {
        "User-Agent": "Bookland-Internal-Linker/4.11 (+https://bookland.com.pl/)",
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    }
    with requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        stream = response.raw
        if urlsplit(url).path.casefold().endswith(".gz"):
            stream = gzip.GzipFile(fileobj=response.raw)

        children: List[str] = []
        rows: List[Dict[str, str]] = []
        context = ET.iterparse(stream, events=("start", "end"))
        try:
            _, root = next(context)
        except StopIteration:
            return children, rows
        root_name = _local_name(root.tag)

        for event, elem in context:
            if event != "end":
                continue
            name = _local_name(elem.tag)
            if root_name == "sitemapindex" and name == "sitemap":
                loc = ""
                for child in elem:
                    if _local_name(child.tag) == "loc":
                        loc = (child.text or "").strip()
                        break
                if loc:
                    try:
                        children.append(_validate_bookland_url(loc, xml=True))
                    except ValueError:
                        pass
                elem.clear()
                continue

            if root_name == "urlset" and name == "url":
                loc = ""
                title = ""
                for child in elem.iter():
                    child_name = _local_name(child.tag)
                    value = (child.text or "").strip()
                    if child_name == "loc" and not loc:
                        loc = value
                    elif child_name in {"title", "caption"} and value and not title:
                        title = value
                if loc:
                    try:
                        canonical = _validate_bookland_url(loc)
                    except ValueError:
                        elem.clear()
                        continue
                    path = unquote(urlsplit(canonical).path or "/")
                    if any(path.casefold().startswith(prefix) for prefix in _BLOCKED_PATH_PREFIXES):
                        elem.clear()
                        continue
                    rows.append({
                        "url": canonical,
                        "label": re.sub(r"\s+", " ", title).strip() or _label_from_url(canonical),
                        "path": path,
                        "kind": _infer_kind(url, canonical),
                        "source_sitemap": url,
                    })
                elem.clear()

        root.clear()
        return children, rows


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sitemap_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sitemap_urls (
            url TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_sitemap TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS sitemap_fts USING fts5(
            url UNINDEXED,
            label,
            path,
            kind UNINDEXED,
            source_sitemap UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def sitemap_cache_status(db_path: Path | str = DEFAULT_SITEMAP_DB_PATH) -> Dict[str, object]:
    path = Path(db_path)
    if not path.exists():
        return {"url_count": 0, "refreshed_at": "", "failed_sitemaps": 0, "sitemap_count": 0}
    try:
        with _connect(path) as conn:
            _init_db(conn)
            count = int(conn.execute("SELECT COUNT(*) FROM sitemap_urls").fetchone()[0])
            meta = dict(conn.execute("SELECT key, value FROM sitemap_meta").fetchall())
        return {
            "url_count": count,
            "refreshed_at": meta.get("refreshed_at", ""),
            "failed_sitemaps": int(meta.get("failed_sitemaps", "0") or 0),
            "sitemap_count": int(meta.get("sitemap_count", "0") or 0),
        }
    except sqlite3.Error:
        return {"url_count": 0, "refreshed_at": "", "failed_sitemaps": 0, "sitemap_count": 0}


def _write_rows(conn: sqlite3.Connection, rows: Sequence[Dict[str, str]], remaining: int) -> int:
    if remaining <= 0 or not rows:
        return 0
    payload = [
        (row["url"], row["label"], row["path"], row["kind"], row["source_sitemap"])
        for row in rows[:remaining]
    ]
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO sitemap_urls(url, label, path, kind, source_sitemap) VALUES (?, ?, ?, ?, ?)",
        payload,
    )
    return conn.total_changes - before


def refresh_sitemap_cache(
    *,
    index_url: str = BOOKLAND_SITEMAP_INDEX_URL,
    db_path: Path | str = DEFAULT_SITEMAP_DB_PATH,
    workers: int = DEFAULT_WORKERS,
) -> Dict[str, object]:
    _validate_bookland_url(index_url, xml=True)
    path = Path(db_path)
    workers = max(1, min(int(workers), 4))

    with _REFRESH_LOCK:
        started = time.monotonic()
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{time.time_ns()}.tmp")
        temporary.unlink(missing_ok=True)
        failed = 0
        sitemap_count = 0
        try:
            with _connect(temporary) as conn:
                _init_db(conn)
                pending = [index_url]
                seen_sitemaps = set()

                while pending and len(seen_sitemaps) < MAX_SITEMAPS:
                    batch: List[str] = []
                    # Każdy gotowy parser może zwrócić dziesiątki tysięcy rekordów.
                    # Trzymamy więc w locie najwyżej tyle dużych list, ilu workerów,
                    # zamiast kolejkować kilkanaście ogromnych sitemap jednocześnie.
                    while pending and len(batch) < workers:
                        candidate = pending.pop(0)
                        if candidate not in seen_sitemaps:
                            seen_sitemaps.add(candidate)
                            batch.append(candidate)
                    if not batch:
                        continue

                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = {executor.submit(_read_xml_stream, url): url for url in batch}
                        for future in as_completed(futures):
                            sitemap_count += 1
                            try:
                                children, rows = future.result()
                            except (requests.RequestException, ET.ParseError, OSError, ValueError):
                                failed += 1
                                continue

                            current = int(conn.execute("SELECT COUNT(*) FROM sitemap_urls").fetchone()[0])
                            if current < MAX_URLS:
                                _write_rows(conn, rows, MAX_URLS - current)
                            for child in children:
                                if child not in seen_sitemaps and len(seen_sitemaps) + len(pending) < MAX_SITEMAPS:
                                    pending.append(child)

                            current = int(conn.execute("SELECT COUNT(*) FROM sitemap_urls").fetchone()[0])
                            if current >= MAX_URLS:
                                pending.clear()
                                break

                conn.execute("DELETE FROM sitemap_fts")
                conn.execute(
                    """
                    INSERT INTO sitemap_fts(rowid, url, label, path, kind, source_sitemap)
                    SELECT rowid, url, label, path, kind, source_sitemap FROM sitemap_urls
                    """
                )
                refreshed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                meta = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "index_url": index_url,
                    "refreshed_at": refreshed_at,
                    "failed_sitemaps": str(failed),
                    "sitemap_count": str(sitemap_count),
                }
                conn.executemany(
                    "INSERT OR REPLACE INTO sitemap_meta(key, value) VALUES (?, ?)",
                    list(meta.items()),
                )
                url_count = int(conn.execute("SELECT COUNT(*) FROM sitemap_urls").fetchone()[0])
                if url_count <= 0:
                    raise RuntimeError("Sitemap Booklandu nie zwróciła żadnych indeksowalnych adresów.")
                conn.commit()

            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, path)
            return {
                "url_count": url_count,
                "refreshed_at": refreshed_at,
                "failed_sitemaps": failed,
                "sitemap_count": sitemap_count,
                "duration_seconds": round(time.monotonic() - started, 2),
            }
        finally:
            temporary.unlink(missing_ok=True)



def bundled_snapshot_status(
    snapshot_dir: Path | str = BUNDLED_SNAPSHOT_DIR,
) -> Dict[str, object]:
    directory = Path(snapshot_dir)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return {
            "available": False,
            "url_count": 0,
            "refreshed_at": "",
            "sitemap_count": 0,
            "failed_sitemaps": 0,
            "parts": 0,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        parts = manifest.get("parts") or []
        available = bool(parts) and all((directory / str(name)).is_file() for name in parts)
        return {
            "available": available,
            "url_count": int(manifest.get("url_count", 0) or 0),
            "refreshed_at": str(manifest.get("refreshed_at") or ""),
            "sitemap_count": int(manifest.get("sitemap_count", 0) or 0),
            "failed_sitemaps": int(manifest.get("failed_sitemaps", 0) or 0),
            "parts": len(parts),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "available": False,
            "url_count": 0,
            "refreshed_at": "",
            "sitemap_count": 0,
            "failed_sitemaps": 0,
            "parts": 0,
        }


def restore_bundled_snapshot(
    *,
    db_path: Path | str = DEFAULT_SITEMAP_DB_PATH,
    snapshot_dir: Path | str = BUNDLED_SNAPSHOT_DIR,
    force: bool = False,
) -> Dict[str, object]:
    target = Path(db_path)
    directory = Path(snapshot_dir)
    manifest_path = directory / "manifest.json"
    if not force:
        current = sitemap_cache_status(target)
        if int(current.get("url_count", 0)) > 0:
            return current
    if not manifest_path.exists():
        raise FileNotFoundError("Brak snapshotu sitemap w repozytorium.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    part_names = manifest.get("parts") or []
    if not isinstance(part_names, list) or not part_names:
        raise RuntimeError("Manifest snapshotu sitemap nie zawiera części archiwum.")

    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}.{time.time_ns()}"
    packed_tmp = target.with_suffix(target.suffix + f".{stamp}.snapshot.gz")
    db_tmp = target.with_suffix(target.suffix + f".{stamp}.snapshot.tmp")
    packed_hash = hashlib.sha256()
    db_hash = hashlib.sha256()

    try:
        with packed_tmp.open("wb") as packed_out:
            for raw_name in part_names:
                name = str(raw_name)
                if Path(name).name != name:
                    raise RuntimeError("Nieprawidłowa nazwa części snapshotu.")
                part = directory / name
                if not part.is_file():
                    raise FileNotFoundError(f"Brak części snapshotu: {name}")
                with part.open("rb") as src:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        packed_hash.update(chunk)
                        packed_out.write(chunk)

        expected_packed = str(manifest.get("gzip_sha256") or "")
        if expected_packed and packed_hash.hexdigest() != expected_packed:
            raise RuntimeError("Snapshot sitemap ma nieprawidłową sumę kontrolną archiwum.")

        with gzip.open(packed_tmp, "rb") as src, db_tmp.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                db_hash.update(chunk)
                dst.write(chunk)

        expected_db = str(manifest.get("sqlite_sha256") or "")
        if expected_db and db_hash.hexdigest() != expected_db:
            raise RuntimeError("Snapshot sitemap ma nieprawidłową sumę kontrolną bazy.")

        restored = sitemap_cache_status(db_tmp)
        if int(restored.get("url_count", 0)) <= 0:
            raise RuntimeError("Snapshot sitemap nie zawiera indeksowalnych adresów.")

        os.replace(db_tmp, target)
        return sitemap_cache_status(target)
    finally:
        packed_tmp.unlink(missing_ok=True)
        db_tmp.unlink(missing_ok=True)


def ensure_sitemap_cache(
    *,
    index_url: str = BOOKLAND_SITEMAP_INDEX_URL,
    db_path: Path | str = DEFAULT_SITEMAP_DB_PATH,
) -> Dict[str, object]:
    status = sitemap_cache_status(db_path)
    if int(status.get("url_count", 0)) > 0:
        return status
    with _REFRESH_LOCK:
        status = sitemap_cache_status(db_path)
        if int(status.get("url_count", 0)) > 0:
            return status
        try:
            restored = restore_bundled_snapshot(db_path=db_path)
            if int(restored.get("url_count", 0)) > 0:
                return restored
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error):
            pass
        return refresh_sitemap_cache(index_url=index_url, db_path=db_path)


def _product_query_terms(product: Dict) -> List[str]:
    ordered_values = [
        str(product.get("title") or ""),
        str(product.get("series") or ""),
        str(product.get("subject") or ""),
        str(product.get("school") or ""),
        str(product.get("grade") or ""),
        str(product.get("edition") or ""),
    ]
    result: List[str] = []
    seen = set()
    for value in ordered_values:
        for token in _tokens(value):
            if token not in seen:
                seen.add(token)
                result.append(token)
            if len(result) >= 14:
                return result
    return result


def _fts_query(terms: Sequence[str]) -> str:
    escaped = [term.replace('"', '""') for term in terms]
    return " OR ".join(f'"{term}"' for term in escaped)


def search_sitemap_candidates(
    product: Dict,
    *,
    limit: int = 8,
    db_path: Path | str = DEFAULT_SITEMAP_DB_PATH,
    index_url: str = BOOKLAND_SITEMAP_INDEX_URL,
) -> List[Dict[str, object]]:
    if not isinstance(product, dict):
        return []
    limit = max(1, min(int(limit), 16))
    try:
        ensure_sitemap_cache(index_url=index_url, db_path=db_path)
    except (requests.RequestException, ET.ParseError, sqlite3.Error, OSError, RuntimeError, ValueError):
        return []

    terms = _product_query_terms(product)
    if not terms:
        return []
    title_norm = _normalize_text(str(product.get("title") or ""))
    source_tokens = set(terms)
    path = Path(db_path)

    try:
        with _connect(path) as conn:
            rows = conn.execute(
                """
                SELECT url, label, path, kind, source_sitemap, bm25(sitemap_fts) AS rank
                FROM sitemap_fts
                WHERE sitemap_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (_fts_query(terms), max(40, limit * 6)),
            ).fetchall()
    except sqlite3.Error:
        return []

    ranked = []
    for row in rows:
        label = str(row["label"] or "")
        label_norm = _normalize_text(label)
        if title_norm and label_norm == title_norm:
            continue
        candidate_tokens = set(_tokens(label + " " + str(row["path"] or "")))
        if not candidate_tokens:
            continue
        overlap = len(source_tokens & candidate_tokens) / max(1, min(len(source_tokens), len(candidate_tokens)))
        similarity = SequenceMatcher(None, title_norm, label_norm).ratio() if title_norm and label_norm else 0.0
        if similarity >= 0.97:
            continue
        retrieval_score = round((0.72 * overlap) + (0.28 * similarity), 4)
        if retrieval_score < 0.12:
            continue
        url = str(row["url"])
        ranked.append((
            retrieval_score,
            float(row["rank"] if row["rank"] is not None else 0.0),
            {
                "origin": "sitemap",
                "kind": str(row["kind"] or "sitemap"),
                "code": "sitemap-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
                "label": label,
                "url": url,
                "path": str(row["path"] or ""),
                "source_sitemap": str(row["source_sitemap"] or ""),
                "source_skus": "",
                "school": "",
                "grade": "",
                "subject": "",
                "series": "",
                "edition": "",
                "retrieval_score": retrieval_score,
            },
        ))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]["url"]))
    return [item[2] for item in ranked[:limit]]
