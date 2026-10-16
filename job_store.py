"""SQLite: kolejka metatagów, batche Gemini, workspace opisów, eksporty."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pandas as pd
import streamlit as st

from config import BATCH_PRODUCTS_PER_FILE, DB_PATH, GEMINI_MODEL, PROMPT_VERSION
from text_utils import (
    build_style_plan,
    chunks,
    normalize_for_compare,
    opening_signature,
    product_input_hash,
    safe_string_value,
    utcnow_iso,
)
from workspace_storage import (
    LEGACY_WORKSPACE_ID,
    clear_workspace as storage_clear_workspace,
    create_workspace as storage_create_workspace,
    ensure_workspace_schema,
    list_workspaces as storage_list_workspaces,
    load_workspace as storage_load_workspace,
    save_workspace as storage_save_workspace,
)


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    """Transakcja z commitem/rollbackiem i zamknięciem połączenia (sqlite3 sam go nie zamyka)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db_connect() as conn:
        # Tryb WAL jest trwały dla pliku bazy, więc wystarczy ustawić go raz.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta_jobs (
                job_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'catalog',
                sku TEXT NOT NULL,
                channel TEXT NOT NULL,
                locale TEXT NOT NULL,
                store_view_code TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                source_updated TEXT NOT NULL DEFAULT '',
                input_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                style_seed INTEGER NOT NULL,
                opening_mode TEXT NOT NULL,
                rhythm_mode TEXT NOT NULL,
                focus_mode TEXT NOT NULL,
                semantic_cues TEXT NOT NULL DEFAULT '',
                source_lead TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                meta_title TEXT NOT NULL DEFAULT '',
                meta_description TEXT NOT NULL DEFAULT '',
                opening_signature TEXT NOT NULL DEFAULT '',
                short_opening_signature TEXT NOT NULL DEFAULT '',
                normalized_hash TEXT NOT NULL DEFAULT '',
                validation_errors TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                akeneo_status TEXT NOT NULL DEFAULT '',
                akeneo_sent_at TEXT NOT NULL DEFAULT '',
                akeneo_error TEXT NOT NULL DEFAULT '',
                batch_job_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meta_jobs_status ON meta_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_batch ON meta_jobs(batch_job_name);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_opening ON meta_jobs(opening_signature);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_short_opening ON meta_jobs(short_opening_signature);
            CREATE INDEX IF NOT EXISTS idx_meta_jobs_hash ON meta_jobs(normalized_hash);

            CREATE TABLE IF NOT EXISTS batch_jobs (
                job_name TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL,
                model TEXT NOT NULL,
                input_path TEXT NOT NULL,
                input_file_name TEXT NOT NULL DEFAULT '',
                output_file_name TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'JOB_STATE_PENDING',
                product_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # Migracje dla baz utworzonych przez v4.0.0.
        def ensure_column(table: str, column: str, definition: str) -> None:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        ensure_column("meta_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column("meta_jobs", "source_type", "TEXT NOT NULL DEFAULT 'catalog'")
        ensure_column("meta_jobs", "akeneo_status", "TEXT NOT NULL DEFAULT ''")
        ensure_column("meta_jobs", "akeneo_sent_at", "TEXT NOT NULL DEFAULT ''")
        ensure_column("meta_jobs", "akeneo_error", "TEXT NOT NULL DEFAULT ''")
        # Wartości z Akeneo sprzed pierwszego nadpisania – do ewentualnego przywrócenia.
        ensure_column("meta_jobs", "akeneo_previous_meta_title", "TEXT NOT NULL DEFAULT ''")
        ensure_column("meta_jobs", "akeneo_previous_meta_description", "TEXT NOT NULL DEFAULT ''")
        ensure_column("batch_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_jobs_run ON meta_jobs(run_id)")
        ensure_workspace_schema(conn)


def active_description_workspace_id() -> str:
    return st.session_state.get("description_workspace_id", LEGACY_WORKSPACE_ID)


def load_description_workspace(workspace_id: Optional[str] = None) -> Dict:
    with db_connect() as conn:
        return storage_load_workspace(conn, workspace_id or active_description_workspace_id())


def save_description_workspace(
    selected_skus: Sequence[str],
    results: Sequence[Dict],
    workspace_id: Optional[str] = None,
) -> None:
    with db_connect() as conn:
        storage_save_workspace(
            conn,
            workspace_id or active_description_workspace_id(),
            selected_skus,
            results,
        )


def clear_description_workspace(workspace_id: Optional[str] = None) -> None:
    with db_connect() as conn:
        storage_clear_workspace(conn, workspace_id or active_description_workspace_id())


def list_description_workspaces() -> List[Dict]:
    with db_connect() as conn:
        return storage_list_workspaces(conn)


def create_description_workspace(name: str) -> Dict:
    with db_connect() as conn:
        return storage_create_workspace(conn, name)


def make_job_key(sku: str, channel: str, locale: str) -> str:
    return f"{channel}|{locale}|{sku}"


def upsert_meta_job(
    *,
    sku: str,
    channel: str,
    locale: str,
    store_view_code: str,
    product_data: Dict,
    source_updated: str = "",
    force_regenerate: bool = False,
    run_id: str = "",
    source_type: str = "catalog",
) -> Tuple[str, bool]:
    queued = bulk_upsert_meta_jobs([{
        "sku": sku, "channel": channel, "locale": locale, "store_view_code": store_view_code,
        "product_data": product_data, "source_updated": source_updated,
        "force_regenerate": force_regenerate, "run_id": run_id, "source_type": source_type,
    }])
    return make_job_key(sku, channel, locale), queued[str(sku)]


def save_meta_result(
    job_key: str,
    *,
    meta_title: str,
    meta_description: str,
    status: str,
    attempts: int,
    validation_errors: Sequence[str] = (),
    error_message: str = "",
) -> None:
    opening = opening_signature(meta_description, 6)
    short_opening = opening_signature(meta_description, 3)
    normalized_hash = hashlib.sha256(normalize_for_compare(meta_description).encode("utf-8")).hexdigest()
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE meta_jobs SET
                meta_title=?, meta_description=?, status=?, attempts=?,
                opening_signature=?, short_opening_signature=?, normalized_hash=?,
                validation_errors=?, error_message=?, akeneo_status='',
                akeneo_sent_at='', akeneo_error='', updated_at=?
            WHERE job_key=?
            """,
            (
                meta_title,
                meta_description,
                status,
                attempts,
                opening,
                short_opening,
                normalized_hash,
                json.dumps(list(validation_errors), ensure_ascii=False),
                error_message,
                utcnow_iso(),
                job_key,
            ),
        )


def get_meta_job(job_key: str) -> Optional[Dict]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM meta_jobs WHERE job_key=?", (job_key,)).fetchone()
    return dict(row) if row else None


def list_meta_jobs(
    statuses: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    order_by: str = "updated_at DESC",
    run_id: Optional[str] = None,
) -> List[Dict]:
    allowed_order = {
        "updated_at DESC",
        "created_at ASC",
        "sku ASC",
        "status ASC, sku ASC",
    }
    if order_by not in allowed_order:
        order_by = "updated_at DESC"

    sql = "SELECT * FROM meta_jobs"
    params: List[object] = []
    conditions: List[str] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if run_id:
        conditions.append("run_id=?")
        params.append(run_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with db_connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def export_previous_metatags_csv(run_id: Optional[str] = None) -> bytes:
    """Wartości z Akeneo sprzed pierwszego nadpisania przez aplikację."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(["sku", "channel", "locale", "meta_title", "meta_description", "akeneo_sent_at"])
    sql = """
        SELECT sku, channel, locale, akeneo_previous_meta_title, akeneo_previous_meta_description, akeneo_sent_at
        FROM meta_jobs
        WHERE (akeneo_previous_meta_title != '' OR akeneo_previous_meta_description != '')
    """
    params: List[object] = []
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    with db_connect() as conn:
        writer.writerows(tuple(row) for row in conn.execute(sql + " ORDER BY sku", params))
    return output.getvalue().encode("utf-8-sig")


def list_meta_delivery_jobs(
    *,
    run_id: Optional[str] = None,
    include_sent: bool = False,
) -> List[Dict]:
    """Zwraca najnowszy poprawny wynik dla SKU/kanału/locale, bez ciężkich pól źródłowych."""
    sql = """
        SELECT job_key, sku, channel, locale, meta_title, meta_description,
               akeneo_status, akeneo_sent_at, akeneo_error, created_at, updated_at
        FROM meta_jobs
        WHERE status='completed' AND meta_title<>'' AND meta_description<>''
    """
    params: List[object] = []
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    sql += " ORDER BY created_at DESC, updated_at DESC"
    with db_connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    latest: List[Dict] = []
    seen: Set[Tuple[str, str, str]] = set()
    for row in rows:
        identity = (str(row["sku"]), str(row["channel"]), str(row["locale"]))
        if identity in seen:
            continue
        seen.add(identity)
        if not include_sent and row.get("akeneo_status") == "sent":
            continue
        latest.append(row)
    return latest


def iter_meta_job_chunks(
    *,
    statuses: Sequence[str],
    run_id: Optional[str],
    chunk_size: int,
) -> Iterator[List[Dict]]:
    """Strumieniuje rekordy z SQLite bez ładowania 50k opisów naraz do RAM."""
    if not statuses:
        return
    placeholders = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM meta_jobs WHERE status IN ({placeholders})"
    params: List[object] = list(statuses)
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    sql += " ORDER BY created_at ASC"
    with db_connect() as conn:
        cursor = conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(max(1, int(chunk_size)))
            if not rows:
                break
            yield [dict(row) for row in rows]


def recommended_batch_shard_size(product_count: int) -> int:
    """Dobiera shard pod szybkie duże runy, zostawiając zapas do limitu 100 jobs."""
    n = max(0, int(product_count))
    if n <= 1200:
        return max(100, n or BATCH_PRODUCTS_PER_FILE)
    if n <= 5000:
        return 750
    return 1000


def active_batch_job_count(run_id: Optional[str] = None) -> int:
    terminal = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    count = 0
    for row in list_batch_jobs(run_id=run_id):
        if row.get("ingested_at"):
            continue
        if str(row.get("state", "")) not in terminal:
            count += 1
    return count


def meta_status_counts(run_id: Optional[str] = None) -> Dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM meta_jobs"
    params: List[object] = []
    if run_id:
        sql += " WHERE run_id=?"
        params.append(run_id)
    sql += " GROUP BY status"
    with db_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def list_meta_runs(limit: int = 30) -> List[Dict]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, source_type, COUNT(*) AS product_count,
                   MIN(created_at) AS created_at, MAX(updated_at) AS updated_at
            FROM meta_jobs
            WHERE run_id<>''
            GROUP BY run_id, source_type
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def new_run_id(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = hashlib.sha1(f"{time.time_ns()}|{prefix}".encode("utf-8")).hexdigest()[:6]
    return f"{prefix}-{timestamp}-{suffix}"


def requeue_jobs(statuses: Sequence[str], reason: str = "", run_id: Optional[str] = None) -> int:
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    conditions = [f"status IN ({placeholders})"]
    params: List[object] = [utcnow_iso(), reason, *statuses]
    if run_id:
        conditions.append("run_id=?")
        params.append(run_id)
    with db_connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE meta_jobs
            SET status='queued', batch_job_name='', updated_at=?, error_message=?
            WHERE {' AND '.join(conditions)}
            """,
            params,
        )
        return int(cur.rowcount)


def existing_meta_title_owners(exclude_job_key: Optional[str] = None) -> Dict[str, str]:
    query = "SELECT job_key, sku, meta_title FROM meta_jobs WHERE status='completed' AND meta_title<>''"
    params: List[str] = []
    if exclude_job_key:
        query += " AND job_key<>?"
        params.append(exclude_job_key)
    owners: Dict[str, str] = {}
    with db_connect() as conn:
        for row in conn.execute(query, params).fetchall():
            key = normalize_for_compare(row["meta_title"])
            if key:
                owners.setdefault(key, str(row["sku"]))
    return owners


def save_akeneo_meta_deliveries(
    deliveries: Sequence[Tuple[str, str, str]],
    previous: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    """Zapisuje status wysyłki; przy pierwszym udanym nadpisaniu także wartości sprzed zmiany."""
    rows = [(job_key, status, error) for job_key, status, error in deliveries if job_key]
    if not rows:
        return
    previous = previous or {}
    now = utcnow_iso()
    with db_connect() as conn:
        conn.executemany(
            """
            UPDATE meta_jobs
            SET akeneo_status=?, akeneo_sent_at=?, akeneo_error=?, updated_at=?,
                akeneo_previous_meta_title=CASE WHEN ?='sent' AND akeneo_previous_meta_title=''
                    AND akeneo_previous_meta_description='' THEN ? ELSE akeneo_previous_meta_title END,
                akeneo_previous_meta_description=CASE WHEN ?='sent' AND akeneo_previous_meta_title=''
                    AND akeneo_previous_meta_description='' THEN ? ELSE akeneo_previous_meta_description END
            WHERE job_key=?
            """,
            [
                (
                    status, now if status == "sent" else "", error[:2000], now,
                    status, previous.get(job_key, {}).get("meta_title", ""),
                    status, previous.get(job_key, {}).get("meta_description", ""),
                    job_key,
                )
                for job_key, status, error in rows
            ],
        )


def register_batch_job(
    *,
    job_name: str,
    run_id: str,
    display_name: str,
    input_path: Path,
    input_file_name: str,
    product_count: int,
    state: str,
) -> None:
    now = utcnow_iso()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO batch_jobs(
                job_name, run_id, display_name, model, input_path, input_file_name, state,
                product_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_name) DO UPDATE SET
                state=excluded.state,
                input_file_name=excluded.input_file_name,
                updated_at=excluded.updated_at
            """,
            (
                job_name,
                run_id,
                display_name,
                GEMINI_MODEL,
                str(input_path),
                input_file_name,
                state,
                product_count,
                now,
                now,
            ),
        )


def list_batch_jobs(run_id: Optional[str] = None) -> List[Dict]:
    sql = "SELECT * FROM batch_jobs"
    params: List[object] = []
    if run_id:
        sql += " WHERE run_id=?"
        params.append(run_id)
    sql += " ORDER BY created_at DESC"
    with db_connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def export_meta_csv(
    statuses: Sequence[str] = ("completed",),
    run_id: Optional[str] = None,
) -> bytes:
    jobs = list_meta_jobs(statuses=statuses, order_by="sku ASC", run_id=run_id)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["sku", "store_view_code", "meta_title", "meta_description"],
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for job in jobs:
        writer.writerow(
            {
                "sku": job["sku"],
                "store_view_code": job["store_view_code"],
                "meta_title": job["meta_title"],
                "meta_description": job["meta_description"],
            }
        )
    return output.getvalue().encode("utf-8-sig")


def export_quality_report_csv(run_id: Optional[str] = None) -> bytes:
    jobs = list_meta_jobs(order_by="status ASC, sku ASC", run_id=run_id)
    rows = []
    for job in jobs:
        rows.append(
            {
                "sku": job["sku"],
                "status": job["status"],
                "attempts": job["attempts"],
                "meta_title_length": len(job["meta_title"]),
                "meta_description_length": len(job["meta_description"]),
                "opening_signature": job["opening_signature"],
                "validation_errors": job["validation_errors"],
                "error_message": job["error_message"],
            }
        )
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")


def get_meta_jobs_by_keys(keys: Sequence[str]) -> Dict[str, Dict]:
    unique = list(dict.fromkeys(key for key in keys if key))
    result: Dict[str, Dict] = {}
    if not unique:
        return result
    with db_connect() as conn:
        for key_chunk in chunks(unique, 500):
            placeholders = ",".join("?" for _ in key_chunk)
            rows = conn.execute(f"SELECT * FROM meta_jobs WHERE job_key IN ({placeholders})", list(key_chunk)).fetchall()
            result.update({row["job_key"]: dict(row) for row in rows})
    return result


def save_meta_results_bulk(records: Sequence[Dict], failed_records: Sequence[Tuple[str, str]] = ()) -> None:
    if not records and not failed_records:
        return
    now = utcnow_iso()
    update_rows = []
    for record in records:
        meta_description = record.get("meta_description", "")
        opening = opening_signature(meta_description, 6)
        short_opening = opening_signature(meta_description, 3)
        normalized_hash = hashlib.sha256(normalize_for_compare(meta_description).encode("utf-8")).hexdigest()
        update_rows.append((
            record.get("meta_title", ""), meta_description, record["status"], int(record.get("attempts", 1)),
            opening, short_opening, normalized_hash,
            json.dumps(list(record.get("validation_errors", ())), ensure_ascii=False),
            record.get("error_message", ""), now, record["job_key"],
        ))

    with db_connect() as conn:
        if update_rows:
            conn.executemany(
                """
                UPDATE meta_jobs SET meta_title=?, meta_description=?, status=?, attempts=?,
                    opening_signature=?, short_opening_signature=?, normalized_hash=?,
                    validation_errors=?, error_message=?, akeneo_status='',
                    akeneo_sent_at='', akeneo_error='', updated_at=?
                WHERE job_key=?
                """,
                update_rows,
            )
        if failed_records:
            conn.executemany(
                "UPDATE meta_jobs SET status='failed', error_message=?, updated_at=? WHERE job_key=?",
                [(message[:2000], now, key) for key, message in failed_records if key],
            )


def bulk_upsert_meta_jobs(entries: Sequence[Dict]) -> Dict[str, bool]:
    """Prawdziwy bulk upsert: jeden SELECT zbiorczy + executemany + jeden commit."""
    results: Dict[str, bool] = {}
    if not entries:
        return results

    now = utcnow_iso()
    prepared: List[Dict] = []
    for entry in entries:
        sku = str(entry["sku"])
        channel = str(entry["channel"])
        locale = str(entry["locale"])
        product_data = entry["product_data"]
        title = safe_string_value(product_data.get("title"))
        author = safe_string_value(product_data.get("author"))
        description = safe_string_value(product_data.get("description"))
        details = safe_string_value(product_data.get("details"))
        input_hash = product_input_hash(sku, title, author, description, channel, locale)
        style = build_style_plan(sku, title, author, description)
        job_key = make_job_key(sku, channel, locale)
        prepared.append({
            "entry": entry,
            "sku": sku,
            "channel": channel,
            "locale": locale,
            "title": title,
            "author": author,
            "description": description,
            "details": details,
            "input_hash": input_hash,
            "style": style,
            "job_key": job_key,
        })

    existing_map: Dict[str, sqlite3.Row] = {}
    keys = [item["job_key"] for item in prepared]
    with db_connect() as conn:
        for key_chunk in chunks(keys, 500):
            placeholders = ",".join("?" for _ in key_chunk)
            rows = conn.execute(
                f"SELECT job_key, input_hash, prompt_version, model, status FROM meta_jobs WHERE job_key IN ({placeholders})",
                list(key_chunk),
            ).fetchall()
            existing_map.update({row["job_key"]: row for row in rows})

        touch_rows: List[Tuple] = []
        upsert_rows: List[Tuple] = []
        for item in prepared:
            entry = item["entry"]
            existing = existing_map.get(item["job_key"])
            force = bool(entry.get("force_regenerate", False))
            unchanged_completed = bool(
                existing
                and existing["input_hash"] == item["input_hash"]
                and existing["prompt_version"] == PROMPT_VERSION
                and existing["model"] == GEMINI_MODEL
                and existing["status"] == "completed"
                and not force
            )
            # Zadanie w trwającym batchu zostaje; reset oznaczałby drugi, płatny request,
            # a stary wynik nadpisałby nowy. Zmienione dane wejdą przy kolejnym imporcie.
            in_running_batch = bool(existing and existing["status"] == "batch_submitted" and not force)
            if unchanged_completed or in_running_batch:
                touch_rows.append((
                    entry.get("run_id", ""), entry.get("source_type", "catalog"),
                    entry.get("store_view_code", ""), now, item["job_key"],
                ))
                results[item["sku"]] = False
                continue

            style = item["style"]
            upsert_rows.append((
                item["job_key"], entry.get("run_id", ""), entry.get("source_type", "catalog"),
                item["sku"], item["channel"], item["locale"], entry.get("store_view_code", ""),
                item["title"], item["author"], item["description"], item["details"],
                entry.get("source_updated", ""), item["input_hash"], PROMPT_VERSION, GEMINI_MODEL,
                style["seed"], style["opening_mode"], style["rhythm_mode"], style["focus_mode"],
                ", ".join(style["semantic_cues"]), style["source_lead"], now, now,
            ))
            results[item["sku"]] = True

        if touch_rows:
            conn.executemany(
                "UPDATE meta_jobs SET run_id=?, source_type=?, store_view_code=?, updated_at=? WHERE job_key=?",
                touch_rows,
            )
        if upsert_rows:
            conn.executemany(
                """
                INSERT INTO meta_jobs(
                    job_key, run_id, source_type, sku, channel, locale, store_view_code, title, author,
                    description, details, source_updated, input_hash, prompt_version, model, style_seed,
                    opening_mode, rhythm_mode, focus_mode, semantic_cues, source_lead, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    run_id=excluded.run_id, source_type=excluded.source_type,
                    store_view_code=excluded.store_view_code, title=excluded.title,
                    author=excluded.author, description=excluded.description, details=excluded.details,
                    source_updated=excluded.source_updated, input_hash=excluded.input_hash,
                    prompt_version=excluded.prompt_version, model=excluded.model,
                    style_seed=excluded.style_seed, opening_mode=excluded.opening_mode,
                    rhythm_mode=excluded.rhythm_mode, focus_mode=excluded.focus_mode,
                    semantic_cues=excluded.semantic_cues, source_lead=excluded.source_lead,
                    status='queued', attempts=0, meta_title='', meta_description='',
                    opening_signature='', short_opening_signature='', normalized_hash='',
                    validation_errors='', error_message='', batch_job_name='', updated_at=excluded.updated_at
                """,
                upsert_rows,
            )
    return results
