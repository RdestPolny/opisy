"""Gemini Batch API: import do kolejki, shardy, odbiór i ingest wyników."""
from __future__ import annotations

import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import streamlit as st
from google import genai
from google.genai import types

from akeneo import (
    _prepare_product_data,
    akeneo_existing_attribute_codes,
    akeneo_fetch_products_by_identifiers,
    akeneo_get_product_details,
    akeneo_iter_products_search_after,
)
from config import (
    AKENEO_MAX_WORKERS,
    AKENEO_SKU_FILTER_CHUNK_SIZE,
    BATCH_ACTIVE_JOB_SOFT_CAP,
    BATCH_DIR,
    BATCH_DOWNLOAD_WORKERS,
    BATCH_FAILED_STATES,
    BATCH_MAX_SHARDS_PER_SUBMIT,
    BATCH_PRODUCTS_PER_FILE,
    BATCH_REFRESH_WORKERS,
    BATCH_SUBMIT_WORKERS,
    GEMINI_MODEL,
    IMPORT_REPORT_DIR,
    PROMPT_VERSION,
    TURBO_BATCH_SHARD_SIZE,
)
from job_store import (
    active_batch_job_count,
    bulk_upsert_meta_jobs,
    db_connect,
    existing_meta_title_owners,
    get_meta_jobs_by_keys,
    iter_meta_job_chunks,
    list_batch_jobs,
    list_meta_jobs,
    meta_status_counts,
    register_batch_job,
    save_meta_results_bulk,
)
from meta_titles import (
    extract_meta_title_candidates,
    locked_fields_from_job,
    meta_validation_errors,
    select_best_meta_title_candidate,
)
from metatags import META_SYSTEM_PROMPT, batch_request_for_job, build_meta_prompt
from text_utils import (
    chunks,
    normalize_for_compare,
    normalize_sku_list,
    normalize_spaces,
    strip_code_fences,
    strip_html,
    utcnow_iso,
)


def estimate_batch_input_tokens(run_id: Optional[str], queued_count: int, sample_size: int = 24) -> Dict[str, float]:
    """Lekki estymator planu. Nie wywołuje Gemini ani countTokens."""
    sample = list_meta_jobs(
        statuses=["queued"], limit=max(1, int(sample_size)), order_by="created_at ASC", run_id=run_id
    )
    if not sample or queued_count <= 0:
        return {"avg_input_tokens": 0.0, "estimated_input_tokens": 0.0}
    token_estimates = []
    for job in sample:
        chars = len(META_SYSTEM_PROMPT) + len(build_meta_prompt(job, int(job.get("attempts", 0))))
        # Dla PL/EN w tym zastosowaniu 3.6 znaku/token jest bezpieczniejszym
        # przybliżeniem niż klasyczne 4 znaki/token.
        token_estimates.append(chars / 3.6)
    avg = sum(token_estimates) / len(token_estimates)
    return {"avg_input_tokens": avg, "estimated_input_tokens": avg * int(queued_count)}


@st.cache_data(ttl=300, show_spinner=False)
def cached_batch_input_estimate(run_id: Optional[str], queued_count: int) -> Dict[str, float]:
    # Estymacja buduje 24 prompty; przy każdym kliknięciu wystarczy wynik sprzed kilku minut.
    return estimate_batch_input_tokens(run_id, queued_count)


def write_batch_jsonl(jobs: Sequence[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for job in jobs:
            attempt = int(job.get("attempts", 0))
            handle.write(json.dumps(batch_request_for_job(job, attempt), ensure_ascii=False) + "\n")


def batch_state_name(batch_job) -> str:
    state = getattr(batch_job, "state", None)
    return getattr(state, "name", None) or str(state or "UNKNOWN")


def extract_batch_response_text(payload: Dict) -> Tuple[str, str]:
    """Zwraca (klucz, tekst JSON) z różnych wariantów odpowiedzi JSONL Gemini."""
    key = str(payload.get("key") or payload.get("metadata", {}).get("key") or "")
    response = payload.get("response") or payload.get("inlineResponse", {}).get("response") or {}
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if response.get("error"):
        raise RuntimeError(str(response["error"]))

    candidates = response.get("candidates") or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                return key, str(part["text"])

    text = response.get("text")
    if text:
        return key, str(text)
    raise RuntimeError("Brak tekstu w odpowiedzi batch")


def import_catalog_to_meta_queue(
    *,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    category: Optional[str],
    enabled_only: bool,
    only_with_description: bool,
    max_products: Optional[int],
    force_regenerate: bool,
    run_id: str = "",
    progress_callback=None,
) -> Dict[str, int]:
    stats = {"seen": 0, "queued": 0, "skipped_unchanged": 0, "without_description": 0}
    pending: List[Dict] = []

    def flush() -> None:
        for queued in bulk_upsert_meta_jobs(pending).values():
            stats["queued" if queued else "skipped_unchanged"] += 1
        pending.clear()

    for product in akeneo_iter_products_search_after(
        token,
        channel,
        locale,
        category=category,
        enabled_only=enabled_only,
        only_with_description=only_with_description,
        max_products=max_products,
    ):
        stats["seen"] += 1
        if not strip_html(product.get("description", "")):
            stats["without_description"] += 1
            if only_with_description:
                continue
        pending.append({
            "sku": product["identifier"], "channel": channel, "locale": locale,
            "store_view_code": store_view_code, "product_data": _prepare_product_data(product),
            "source_updated": product.get("updated", ""), "force_regenerate": force_regenerate,
            "run_id": run_id, "source_type": "catalog",
        })
        if len(pending) >= 200:
            flush()
        if progress_callback and stats["seen"] % 100 == 0:
            progress_callback(stats)
    flush()
    if progress_callback:
        progress_callback(stats)
    return stats


def import_skus_to_meta_queue(
    *,
    skus: Sequence[str],
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    enabled_only: bool,
    only_with_description: bool,
    force_regenerate: bool,
    run_id: str,
    progress_callback=None,
) -> Dict[str, object]:
    unique_skus, duplicate_count = normalize_sku_list(skus)
    stats: Dict[str, object] = {
        "run_id": run_id, "input": len(skus), "unique": len(unique_skus),
        "duplicates": duplicate_count, "found": 0, "queued": 0,
        "skipped_unchanged": 0, "inactive": 0, "without_description": 0,
        "missing": 0, "errors": 0, "processed": 0,
    }
    missing_skus: List[str] = []
    error_skus: Dict[str, str] = {}
    inactive_skus: List[str] = []
    without_description_skus: List[str] = []
    sku_chunks = list(chunks(unique_skus, AKENEO_SKU_FILTER_CHUNK_SIZE))

    # Rozgrzej cache listy istniejących atrybutów przed uruchomieniem wątków.
    # Bez tego pierwszy start mógłby równolegle wykonać tę samą serię requestów.
    akeneo_existing_attribute_codes(token)

    # Cztery paczki Akeneo równocześnie. Globalny semafor w funkcjach requestów
    # gwarantuje, że nie przekroczymy AKENEO_MAX_WORKERS.
    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
        futures = {
            executor.submit(akeneo_fetch_products_by_identifiers, token, channel, locale, sku_chunk): sku_chunk
            for sku_chunk in sku_chunks
        }
        for future in as_completed(futures):
            sku_chunk = futures[future]
            try:
                products = future.result()
            except Exception:
                # Jedna paczka nie powinna zatrzymać 10k SKU. Fallback per SKU
                # zachowuje poprawność i nadal podlega globalnemu limitowi 4.
                products = {}
                for sku in sku_chunk:
                    try:
                        product = akeneo_get_product_details(sku, token, channel, locale)
                        if product:
                            products[sku] = product
                    except Exception as exc:
                        # Błąd sieci/autoryzacji to nie „brak produktu” – raport musi to rozróżniać.
                        error_skus[sku] = f"błąd Akeneo: {exc}"[:300]

            queue_entries: List[Dict] = []
            for sku in sku_chunk:
                stats["processed"] = int(stats["processed"]) + 1
                product = products.get(sku)
                if not product and sku in error_skus:
                    stats["errors"] = int(stats["errors"]) + 1
                    continue
                if not product:
                    stats["missing"] = int(stats["missing"]) + 1
                    missing_skus.append(sku)
                    continue
                stats["found"] = int(stats["found"]) + 1
                if enabled_only and not product.get("enabled", False):
                    stats["inactive"] = int(stats["inactive"]) + 1
                    inactive_skus.append(sku)
                    continue
                if only_with_description and not strip_html(product.get("description", "")):
                    stats["without_description"] = int(stats["without_description"]) + 1
                    without_description_skus.append(sku)
                    continue

                queue_entries.append({
                    "sku": sku,
                    "channel": channel,
                    "locale": locale,
                    "store_view_code": store_view_code,
                    "product_data": _prepare_product_data(product),
                    "source_updated": product.get("updated", ""),
                    "force_regenerate": force_regenerate,
                    "run_id": run_id,
                    "source_type": "sku_list",
                })

            queued_map = bulk_upsert_meta_jobs(queue_entries)
            for queued in queued_map.values():
                if queued:
                    stats["queued"] = int(stats["queued"]) + 1
                else:
                    stats["skipped_unchanged"] = int(stats["skipped_unchanged"]) + 1

            if progress_callback:
                progress_callback(stats)

    IMPORT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = IMPORT_REPORT_DIR / f"{run_id}-odrzucone.tsv"
    with report_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["sku", "powod"])
        writer.writerows((sku, "nie znaleziono w Akeneo") for sku in missing_skus)
        writer.writerows(error_skus.items())
        writer.writerows((sku, "produkt nieaktywny") for sku in inactive_skus)
        writer.writerows((sku, "brak opisu źródłowego") for sku in without_description_skus)
    stats["report_path"] = str(report_path)
    return stats


def _remote_batches_by_display_name(display_names: Set[str], api_key: str) -> Dict[str, Dict]:
    """Odzyskuje istniejące jobs po deterministycznej nazwie sharda.

    Batch create nie jest idempotentne. Ten lookup chroni przed ponownym
    naliczeniem kosztu, jeśli Streamlit padł po utworzeniu joba, ale przed
    zapisaniem jego `job_name` do SQLite.
    """
    if not display_names:
        return {}
    wanted = set(display_names)
    found: Dict[str, Dict] = {}
    with db_connect() as conn:
        known = {row[0] for row in conn.execute("SELECT job_name FROM batch_jobs")}
    try:
        client = genai.Client(api_key=api_key)
        for batch_job in client.batches.list(config={"page_size": 100}):
            display = str(getattr(batch_job, "display_name", "") or "")
            if display not in wanted or display in found:
                continue
            # Odzyskujemy tylko zadanie utworzone przed crashem (nieznane w SQLite) i niezakończone
            # porażką; ponowne wysłanie shardu po FAILED nie może „odzyskać” starej porażki.
            if batch_job.name in known or batch_state_name(batch_job) in BATCH_FAILED_STATES:
                continue
            found[display] = {
                "job_name": batch_job.name,
                "display_name": display,
                "input_file_name": "",
                "state": batch_state_name(batch_job),
            }
            if len(found) == len(wanted):
                break
    except Exception:
        # Recovery jest dodatkowym bezpiecznikiem; awaria listowania nie blokuje
        # normalnego submitu.
        return {}
    return found


def _submit_single_batch_file(spec: Dict, api_key: str) -> Dict:
    client = genai.Client(api_key=api_key)
    uploaded_file = client.files.upload(
        file=str(spec["input_path"]),
        config=types.UploadFileConfig(display_name=spec["display_name"], mime_type="jsonl"),
    )
    batch_job = client.batches.create(
        model=GEMINI_MODEL,
        src=uploaded_file.name,
        config={"display_name": spec["display_name"]},
    )
    state = getattr(getattr(batch_job, "state", None), "name", None) or str(
        getattr(batch_job, "state", "JOB_STATE_PENDING")
    )
    return {
        **spec,
        "job_name": batch_job.name,
        "input_file_name": uploaded_file.name,
        "state": state,
    }


def submit_queued_batches(
    products_per_file: int = BATCH_PRODUCTS_PER_FILE,
    run_id: Optional[str] = None,
) -> List[Dict]:
    """Tworzy i wysyła wiele małych shardów bez trzymania całego runu w RAM.

    Google dopuszcza do 100 równoczesnych batch jobs. Aplikacja celowo zostawia
    zapas i utrzymuje soft cap 80 aktywnych zadań.
    """
    queued_count = int(meta_status_counts(run_id).get("queued", 0))
    if queued_count <= 0:
        return []

    requested = max(100, int(products_per_file))
    effective_size = min(requested, TURBO_BATCH_SHARD_SIZE) if queued_count >= 5000 else requested

    active_jobs = active_batch_job_count(run_id)
    available_slots = max(0, BATCH_ACTIVE_JOB_SOFT_CAP - active_jobs)
    max_new_shards = min(BATCH_MAX_SHARDS_PER_SUBMIT, available_slots)
    if max_new_shards <= 0:
        return [{
            "job_name": "",
            "display_name": "soft-cap",
            "products": 0,
            "state": "WAIT_FOR_ACTIVE_BATCHES",
            "error": f"Aktywnych zadań: {active_jobs}. Najpierw odbierz zakończone shardy.",
        }]

    specs: List[Dict] = []
    for part, chunk in enumerate(
        iter_meta_job_chunks(statuses=["queued"], run_id=run_id, chunk_size=effective_size),
        start=1,
    ):
        if len(specs) >= max_new_shards:
            break
        job_keys = [job["job_key"] for job in chunk]
        shard_fingerprint = hashlib.sha1(
            (PROMPT_VERSION + "|" + "|".join(job_keys)).encode("utf-8")
        ).hexdigest()[:12]
        display_name = f"bookland-meta-{(run_id or 'all')[-14:]}-{part:03d}-{shard_fingerprint}"
        input_path = BATCH_DIR / f"{display_name}.jsonl"
        write_batch_jsonl(chunk, input_path)
        specs.append({
            "display_name": display_name,
            "input_path": input_path,
            "job_keys": job_keys,
            "products": len(chunk),
            "input_mb": round(input_path.stat().st_size / (1024 * 1024), 2),
        })
        # `chunk` znika po iteracji; nie kumulujemy opisów 50k produktów w pamięci.

    if not specs:
        return []

    submitted: List[Dict] = []
    api_key = str(st.secrets["GOOGLE_API_KEY"])

    # Najpierw odzyskaj ewentualne zdalne jobs utworzone przed crashem UI.
    recovered = _remote_batches_by_display_name(
        {spec["display_name"] for spec in specs}, api_key
    )
    specs_to_submit: List[Dict] = []
    for spec in specs:
        remote = recovered.get(spec["display_name"])
        if not remote:
            specs_to_submit.append(spec)
            continue
        register_batch_job(
            job_name=remote["job_name"],
            run_id=run_id or "",
            display_name=spec["display_name"],
            input_path=spec["input_path"],
            input_file_name=remote.get("input_file_name", ""),
            product_count=spec["products"],
            state=remote["state"],
        )
        now = utcnow_iso()
        with db_connect() as conn:
            conn.executemany(
                "UPDATE meta_jobs SET status='batch_submitted', batch_job_name=?, updated_at=? WHERE job_key=?",
                [(remote["job_name"], now, key) for key in spec["job_keys"]],
            )
        submitted.append({
            "job_name": remote["job_name"],
            "display_name": spec["display_name"],
            "products": spec["products"],
            "input_mb": spec.get("input_mb", 0),
            "state": f"RECOVERED:{remote['state']}",
        })

    if not specs_to_submit:
        return sorted(submitted, key=lambda item: item["display_name"])

    with ThreadPoolExecutor(max_workers=min(BATCH_SUBMIT_WORKERS, len(specs_to_submit))) as executor:
        futures = {executor.submit(_submit_single_batch_file, spec, api_key): spec for spec in specs_to_submit}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                result = future.result()
                register_batch_job(
                    job_name=result["job_name"],
                    run_id=run_id or "",
                    display_name=result["display_name"],
                    input_path=result["input_path"],
                    input_file_name=result["input_file_name"],
                    product_count=result["products"],
                    state=result["state"],
                )
                now = utcnow_iso()
                with db_connect() as conn:
                    conn.executemany(
                        "UPDATE meta_jobs SET status='batch_submitted', batch_job_name=?, updated_at=? WHERE job_key=?",
                        [(result["job_name"], now, key) for key in result["job_keys"]],
                    )
                submitted.append({
                    "job_name": result["job_name"],
                    "display_name": result["display_name"],
                    "products": result["products"],
                    "input_mb": result.get("input_mb", 0),
                    "state": result["state"],
                })
            except Exception as exc:
                # Rekordy pozostają queued, więc ponowne kliknięcie wyśle wyłącznie
                # shard, którego faktycznie nie udało się zarejestrować.
                submitted.append({
                    "job_name": "",
                    "display_name": spec["display_name"],
                    "products": spec["products"],
                    "input_mb": spec.get("input_mb", 0),
                    "state": "SUBMIT_FAILED",
                    "error": str(exc),
                })
    return sorted(submitted, key=lambda item: item["display_name"])


def ingest_batch_result_bytes(job_name: str, content: bytes) -> Dict[str, int]:
    decoded = content.decode("utf-8")
    raw_lines = [line for line in decoded.splitlines() if line.strip()]
    parsed_payloads: List[Tuple[Dict, str]] = []
    keys: List[str] = []
    stats = {"completed": 0, "validation_failed": 0, "failed": 0}

    for line in raw_lines:
        try:
            payload = json.loads(line)
            key = str(payload.get("key") or payload.get("metadata", {}).get("key") or "")
            parsed_payloads.append((payload, key))
            if key:
                keys.append(key)
        except Exception:
            stats["failed"] += 1

    # Wynik trafia tylko do zadań, które nadal czekają na ten konkretny batch. Zadanie
    # zresetowane w międzyczasie (np. wymuszona regeneracja) nie dostanie starego wyniku.
    jobs = {
        key: job for key, job in get_meta_jobs_by_keys(keys).items()
        if job.get("batch_job_name") == job_name and job.get("status") == "batch_submitted"
    }
    stats["skipped"] = 0
    title_owners = existing_meta_title_owners()
    result_records: List[Dict] = []
    failed_records: List[Tuple[str, str]] = []

    for payload, fallback_key in parsed_payloads:
        key = fallback_key
        try:
            extracted_key, text = extract_batch_response_text(payload)
            key = extracted_key or fallback_key
            if not key:
                raise RuntimeError("Brak klucza zadania w odpowiedzi")
            job = jobs.get(key)
            if not job:
                stats["skipped"] += 1
                continue
            data = json.loads(strip_code_fences(text))
            locked_title, locked_description = locked_fields_from_job(job)

            if locked_title:
                meta_title = locked_title
                title_errors: List[str] = []
            else:
                meta_title, title_errors, _ = select_best_meta_title_candidate(
                    extract_meta_title_candidates(data), job, existing_title_owners=title_owners
                )

            generated_description = normalize_spaces(strip_html(str(data.get("meta_description", "")))).strip('"„”')
            meta_description = locked_description or generated_description
            description_errors = [] if locked_description else meta_validation_errors(meta_description, job=job)
            errors = [*title_errors, *description_errors]
            status = "completed" if not errors else "validation_failed"

            result_records.append({
                "job_key": key,
                "meta_title": meta_title,
                "meta_description": meta_description,
                "status": status,
                "attempts": int(job.get("attempts", 0)) + 1,
                "validation_errors": errors,
                "error_message": "; ".join(errors),
            })
            stats[status] += 1
            if status == "completed":
                title_owners[normalize_for_compare(meta_title)] = str(job["sku"])
        except Exception as exc:
            stats["failed"] += 1
            if key:
                failed_records.append((key, str(exc)))

    save_meta_results_bulk(result_records, [(key, message) for key, message in failed_records if key in jobs])
    with db_connect() as conn:
        # Zadania bez odpowiedzi w pliku wynikowym nie mogą zostać na zawsze w batch_submitted.
        missing = conn.execute(
            """
            UPDATE meta_jobs SET status='failed', error_message='Brak odpowiedzi w wyniku batcha', updated_at=?
            WHERE batch_job_name=? AND status='batch_submitted'
            """,
            (utcnow_iso(), job_name),
        ).rowcount
        stats["failed"] += missing
        conn.execute(
            "UPDATE batch_jobs SET ingested_at=?, updated_at=? WHERE job_name=?",
            (utcnow_iso(), utcnow_iso(), job_name),
        )
    return stats


def _refresh_single_batch_remote(stored: Dict, api_key: str) -> Dict:
    """Szybki status check bez pobierania dużego outputu w tym samym workerze."""
    client = genai.Client(api_key=api_key)
    batch_job = client.batches.get(name=stored["job_name"])
    state = batch_state_name(batch_job)
    output_file_name = ""
    error_message = ""
    dest = getattr(batch_job, "dest", None)
    if dest and getattr(dest, "file_name", None):
        output_file_name = dest.file_name
    if getattr(batch_job, "error", None):
        error_message = str(batch_job.error)
    return {
        "stored": stored,
        "state": state,
        "output_file_name": output_file_name,
        "error_message": error_message,
    }


def _download_batch_output(output_file_name: str, api_key: str) -> bytes:
    client = genai.Client(api_key=api_key)
    return client.files.download(file=output_file_name)


def refresh_and_ingest_batch_jobs(run_id: Optional[str] = None) -> List[Dict]:
    """Szybko odpytuje wszystkie shardy, potem równolegle pobiera tylko gotowe.

    Dzięki rozdzieleniu status-check od downloadu jeden duży plik wynikowy nie
    blokuje odpytywania pozostałych 40–50 zadań.
    """
    pending = [stored for stored in list_batch_jobs(run_id=run_id) if not stored["ingested_at"]]
    if not pending:
        return []

    api_key = str(st.secrets["GOOGLE_API_KEY"])
    updates_by_job: Dict[str, Dict] = {}
    succeeded_for_download: List[Dict] = []

    # Etap 1: statusy — lekka operacja, więc większa pula workerów.
    with ThreadPoolExecutor(max_workers=min(BATCH_REFRESH_WORKERS, len(pending))) as executor:
        futures = {executor.submit(_refresh_single_batch_remote, stored, api_key): stored for stored in pending}
        for future in as_completed(futures):
            stored = futures[future]
            try:
                result = future.result()
                state = result["state"]
                output_file_name = result["output_file_name"]
                error_message = result["error_message"]
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE batch_jobs SET state=?, output_file_name=?, error_message=?, updated_at=?
                        WHERE job_name=?
                        """,
                        (state, output_file_name, error_message[:2000], utcnow_iso(), stored["job_name"]),
                    )
                updates_by_job[stored["job_name"]] = {
                    "job_name": stored["job_name"], "state": state, "ingested": False
                }
                if state == "JOB_STATE_SUCCEEDED" and output_file_name:
                    succeeded_for_download.append({**stored, "output_file_name": output_file_name})
                elif state in BATCH_FAILED_STATES:
                    with db_connect() as conn:
                        conn.execute(
                            """
                            UPDATE meta_jobs SET status='failed', error_message=?, updated_at=?
                            WHERE batch_job_name=? AND status='batch_submitted'
                            """,
                            (error_message or state, utcnow_iso(), stored["job_name"]),
                        )
                        # Zakończone porażką zadanie nie jest już odpytywane przy każdym odświeżeniu.
                        conn.execute(
                            "UPDATE batch_jobs SET ingested_at=? WHERE job_name=?",
                            (utcnow_iso(), stored["job_name"]),
                        )
            except Exception as exc:
                updates_by_job[stored["job_name"]] = {
                    "job_name": stored["job_name"], "state": "STATUS_ERROR", "error": str(exc), "ingested": False
                }

    # Etap 2: tylko gotowe outputy. Downloady lecą równolegle, a ingest następuje
    # od razu po ukończeniu konkretnego pliku.
    if succeeded_for_download:
        with ThreadPoolExecutor(max_workers=min(BATCH_DOWNLOAD_WORKERS, len(succeeded_for_download))) as executor:
            futures = {
                executor.submit(_download_batch_output, row["output_file_name"], api_key): row
                for row in succeeded_for_download
            }
            for future in as_completed(futures):
                row = futures[future]
                update = updates_by_job[row["job_name"]]
                try:
                    content = future.result()
                    update["stats"] = ingest_batch_result_bytes(row["job_name"], content)
                    update["ingested"] = True
                except Exception as exc:
                    update["download_error"] = str(exc)
                    # ingested_at pozostaje pusty — kolejne odświeżenie spróbuje
                    # pobrać ten sam output bez ponownego generowania przez Gemini.

    return sorted(updates_by_job.values(), key=lambda item: item.get("job_name", ""))
