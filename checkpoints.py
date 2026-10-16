"""Pliki wznowienia i checkpointy trybu interaktywnego."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from config import GEMINI_MODEL, INTERACTIVE_CHECKPOINT_DIR, PROMPT_VERSION
from job_store import db_connect
from text_utils import (
    chunks,
    generate_product_url,
    normalize_for_compare,
    normalize_sku_list,
    parse_bulk_sku_payload,
)


def _decode_upload_bytes(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1250", errors="replace")


def _validation_errors_from_cell(value: object) -> List[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "[]"}:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [part.strip() for part in re.split(r"\s*;\s*", text) if part.strip()]


def parse_resumable_product_file(raw_bytes: bytes) -> Tuple[List[str], Dict[str, Dict], List[str]]:
    """Czyta zwykły TXT/CSV/TSV z SKU albo checkpoint wygenerowany przez aplikację.

    Wyłącznie kompletne metatagi są traktowane jako checkpoint.
    """
    text = _decode_upload_bytes(raw_bytes)
    errors: List[str] = []
    try:
        df = pd.read_csv(
            io.StringIO(text),
            sep=None,
            engine="python",
            dtype=str,
            keep_default_na=False,
        )
    except Exception:
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    if df.empty or len(df.columns) == 0:
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    normalized_columns = {normalize_for_compare(col): col for col in df.columns}
    sku_column = next(
        (normalized_columns[key] for key in ("sku", "identifier", "product sku", "kod", "kod produktu") if key in normalized_columns),
        None,
    )
    if sku_column is None:
        # Plik jednokolumnowy bez nagłówka często zostaje odczytany z pierwszym SKU jako nazwą kolumny.
        skus, _ = parse_bulk_sku_payload(text)
        return skus, {}, errors

    skus, _ = normalize_sku_list(df[sku_column].tolist())
    seed_results: Dict[str, Dict] = {}

    meta_title_col = normalized_columns.get("meta title") or normalized_columns.get("meta_title")
    meta_description_col = normalized_columns.get("meta description") or normalized_columns.get("meta_description")
    error_col = normalized_columns.get("error")
    validation_col = normalized_columns.get("validation errors") or normalized_columns.get("validation_errors")
    title_col = normalized_columns.get("title")
    url_col = normalized_columns.get("url")
    old_desc_col = normalized_columns.get("old description") or normalized_columns.get("old_description")
    status_col = normalized_columns.get("checkpoint status") or normalized_columns.get("checkpoint_status") or normalized_columns.get("status")

    if meta_title_col and meta_description_col:
        for _, row in df.iterrows():
            sku = str(row.get(sku_column, "") or "").strip()
            if not sku:
                continue
            meta_title = str(row.get(meta_title_col, "") or "").strip()
            meta_description = str(row.get(meta_description_col, "") or "").strip()
            error = str(row.get(error_col, "") or "").strip() if error_col else ""
            status = str(row.get(status_col, "") or "").strip().lower() if status_col else ""
            if not meta_title or not meta_description or error or status in {"pending", "error", "failed"}:
                continue
            seed_results[sku] = {
                "sku": sku,
                "title": str(row.get(title_col, "") or "") if title_col else "",
                "description_html": "",
                "url": str(row.get(url_col, "") or "") if url_col else "",
                "old_description": str(row.get(old_desc_col, "") or "") if old_desc_col else "",
                "research": None,
                "meta_title": meta_title,
                "meta_description": meta_description,
                "error": None,
                "validation_errors": _validation_errors_from_cell(row.get(validation_col, "")) if validation_col else [],
                "meta_only": True,
                "checkpoint_source": "uploaded_csv",
            }

    return skus, seed_results, errors


def interactive_checkpoint_key(
    skus: Sequence[str],
    *,
    channel: str,
    locale: str,
    store_view_code: str,
) -> str:
    payload = {
        "skus": sorted(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip())),
        "channel": channel,
        "locale": locale,
        "store_view_code": store_view_code,
        "prompt_version": PROMPT_VERSION,
        "model": GEMINI_MODEL,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"interactive-{digest}"


def interactive_checkpoint_path(checkpoint_key: str) -> Path:
    INTERACTIVE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return INTERACTIVE_CHECKPOINT_DIR / f"{checkpoint_key}.csv"


def _checkpoint_row(sku: str, result: Optional[Dict]) -> Dict:
    if not result:
        return {
            "sku": sku,
            "checkpoint_status": "pending",
            "title": "",
            "meta_title": "",
            "meta_description": "",
            "validation_errors": "[]",
            "error": "",
            "meta_only": True,
            "description_html": "",
            "url": "",
            "old_description": "",
        }
    validation_errors = result.get("validation_errors") or []
    if isinstance(validation_errors, str):
        validation_errors = _validation_errors_from_cell(validation_errors)
    error = str(result.get("error") or "").strip()
    status = "error" if error else ("warning" if validation_errors else "completed")
    return {
        "sku": sku,
        "checkpoint_status": status,
        "title": result.get("title", ""),
        "meta_title": result.get("meta_title", ""),
        "meta_description": result.get("meta_description", ""),
        "validation_errors": json.dumps(list(validation_errors), ensure_ascii=False),
        "error": error,
        "meta_only": True,
        "description_html": "",
        "url": result.get("url", ""),
        "old_description": result.get("old_description", ""),
    }


def write_interactive_checkpoint(path: Path, skus: Sequence[str], results_by_sku: Dict[str, Dict]) -> None:
    """Atomowy checkpoint. Zerwany zapis nie uszkodzi poprzedniej wersji pliku."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_checkpoint_row(sku, results_by_sku.get(sku)) for sku in skus]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    pd.DataFrame(rows).to_csv(tmp_path, index=False, encoding="utf-8-sig")
    tmp_path.replace(path)


def load_interactive_checkpoint(path: Path) -> Dict[str, Dict]:
    if not path.exists():
        return {}
    try:
        _, results, _ = parse_resumable_product_file(path.read_bytes())
        for result in results.values():
            result["checkpoint_source"] = "server_csv"
        return results
    except Exception:
        return {}


def cached_meta_results_for_skus(
    skus: Sequence[str],
    *,
    channel: str,
    locale: str,
    include_warnings: bool = True,
) -> Dict[str, Dict]:
    """Odzyskuje gotowe metatagi z SQLite bez ponownego wywołania Gemini."""
    wanted = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))
    if not wanted:
        return {}
    allowed_statuses = {"completed", "validation_failed"} if include_warnings else {"completed"}
    out: Dict[str, Dict] = {}
    with db_connect() as conn:
        for sku_chunk in chunks(wanted, 400):
            placeholders = ",".join("?" for _ in sku_chunk)
            rows = conn.execute(
                f"""
                SELECT * FROM meta_jobs
                WHERE sku IN ({placeholders}) AND channel=? AND locale=?
                  AND prompt_version=? AND model=?
                """,
                [*sku_chunk, channel, locale, PROMPT_VERSION, GEMINI_MODEL],
            ).fetchall()
            for row in rows:
                job = dict(row)
                if job.get("status") not in allowed_statuses:
                    continue
                if not job.get("meta_title") or not job.get("meta_description"):
                    continue
                try:
                    validation_errors = json.loads(job.get("validation_errors") or "[]")
                except Exception:
                    validation_errors = []
                out[job["sku"]] = {
                    "job_key": job.get("job_key", ""),
                    "sku": job["sku"],
                    "title": job.get("title", ""),
                    "description_html": "",
                    "url": generate_product_url(job.get("title", "")) if job.get("title") else "",
                    "old_description": job.get("description", ""),
                    "research": None,
                    "meta_title": job.get("meta_title", ""),
                    "meta_description": job.get("meta_description", ""),
                    "error": None,
                    "validation_errors": validation_errors,
                    "meta_only": True,
                    "checkpoint_source": "sqlite",
                }
    return out
