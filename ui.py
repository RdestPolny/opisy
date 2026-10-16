"""Stan sesji Streamlit i komponenty interfejsu wielokrotnego użytku."""
from __future__ import annotations

import hashlib
import io
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

from akeneo import akeneo_get_token
from checkpoints import (
    cached_meta_results_for_skus,
    load_interactive_checkpoint,
    write_interactive_checkpoint,
)
from config import (
    DB_PATH,
    DEFAULT_CHANNEL,
    DEFAULT_LOCALE,
    GEMINI_INTERACTIVE_WORKERS,
    INTERACTIVE_CHUNK_SIZE,
    META_TITLE_HARD_MAX,
    META_TITLE_TARGET_MAX,
    META_TITLE_TARGET_MIN,
)
from description_output import (
    is_meta_only_result,
    is_reusable_result,
    preserve_description_on_failure,
    refresh_description_result,
)
from descriptions import process_product_from_akeneo
from internal_linking import SCORE_THRESHOLD
from related_products import SHOP_STATUS_LABELS, luigis_item_statuses
from job_store import (
    clear_description_workspace,
    db_connect,
    init_db,
    list_description_workspaces,
    load_description_workspace,
    save_description_workspace,
)
from metatags import process_product_meta_only
from visual_editor import visual_html_editor
from workspace_storage import LEGACY_WORKSPACE_ID


def _workspace_from_url() -> str:
    """Kolejka z adresu (?workspace=...), żeby odświeżenie strony nie wracało do kolejki wspólnej."""
    wanted = str(st.query_params.get("workspace", "") or "")
    known = {workspace["workspace_id"] for workspace in list_description_workspaces()}
    return wanted if wanted in known else LEGACY_WORKSPACE_ID


def _restore_workspace_state(workspace_id: str) -> None:
    workspace = load_description_workspace(workspace_id)
    restored_products = {sku: {"title": sku} for sku in workspace["selected_skus"]}
    for result in workspace["results"]:
        if result.get("sku") in restored_products:
            restored_products[result["sku"]]["title"] = result.get("title", result["sku"])
    st.session_state.description_workspace_id = workspace_id
    st.session_state.bulk_results = workspace["results"]
    st.session_state.bulk_selected_products = restored_products
    st.session_state.saved_queue_skus = list(workspace["selected_skus"])


def init_session_state() -> None:
    # Workspace czytamy z bazy tylko przy starcie sesji, nie przy każdym rerunie.
    if "bulk_results" not in st.session_state:
        _restore_workspace_state(st.session_state.get("description_workspace_id") or _workspace_from_url())
    defaults = {
        "generator_mode": "Generator opisów",
        "products_to_send": {},
        "link_active": False,
        "link_only": False,
        "link_url": "",
        "link_category": "",
        "search_res": [],
        "use_research": True,
        "meta_only": False,
        "magento_store_view": "store_view_bookland",
        "backlog_items": [],
        "active_meta_run_id": "",
        "last_import_report_path": "",
        "interactive_seed_results": {},
        "last_interactive_checkpoint_path": "",
        "force_regenerate_meta": False,
        "reuse_warning_meta": True,
        "active_editor_sku": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def switch_description_workspace(workspace_id: str) -> None:
    _restore_workspace_state(workspace_id)
    st.query_params["workspace"] = workspace_id
    st.session_state.products_to_send = {}
    st.session_state.interactive_seed_results = {}
    st.session_state.last_interactive_checkpoint_path = ""
    st.session_state.search_res = []
    st.session_state.manual_product_input = ""
    st.session_state.active_editor_sku = ""
    for key in list(st.session_state):
        if key.startswith(("edit_", "visual_editor_", "send_", "search_")):
            st.session_state.pop(key, None)


def clear_product_queue() -> None:
    if not st.session_state.get("meta_only"):
        clear_description_workspace()
    st.session_state.bulk_selected_products = {}
    st.session_state.bulk_results = []
    st.session_state.products_to_send = {}
    st.session_state.interactive_seed_results = {}
    st.session_state.last_interactive_checkpoint_path = ""
    st.session_state.search_res = []
    st.session_state.manual_product_input = ""
    st.session_state.active_editor_sku = ""
    for key in list(st.session_state):
        if key.startswith(("edit_", "visual_editor_", "send_")):
            st.session_state.pop(key, None)


def reset_interactive_results() -> None:
    if st.session_state.get("generator_mode") == "Generator opisów":
        workspace = load_description_workspace()
        st.session_state.bulk_results = workspace["results"]
        st.session_state.bulk_selected_products = {
            sku: {"title": sku} for sku in workspace["selected_skus"]
        }
    else:
        st.session_state.bulk_results = []
        st.session_state.bulk_selected_products = {}
    st.session_state.interactive_seed_results = {}
    st.session_state.last_interactive_checkpoint_path = ""
    for key in ("interactive_method", "interactive_resume_file", "interactive_existing_checkpoint"):
        st.session_state.pop(key, None)


def get_internal_link() -> Optional[Dict]:
    if st.session_state.get("link_active") and st.session_state.get("link_mode") == "Automatyczne (Jev)":
        return {
            "mode": "automatic",
            "api_key": st.secrets.get("TYPESAFE_API_KEY", ""),
            "threshold": st.session_state.get("link_threshold", 0.8),
            "firecrawl_api_key": st.secrets.get("FIRECRAWL_API_KEY", ""),
            "related_size": 16,
        }
    if (
        st.session_state.get("link_active")
        and st.session_state.get("link_url")
        and st.session_state.get("link_category")
    ):
        return {
            "url": st.session_state["link_url"],
            "category": st.session_state["link_category"],
        }
    return None


BACKUP_DB_NAME = "product_workflow.sqlite3"
def build_app_backup() -> bytes:
    """ZIP ze spójną kopią SQLite (kolejka metatagów, wyniki, kolejki opisów)."""
    snapshot = DB_PATH.with_name(f"backup-{time.time_ns()}.sqlite3")
    buffer = io.BytesIO()
    try:
        target = sqlite3.connect(snapshot)
        try:
            with db_connect() as conn:
                conn.backup(target)
        finally:
            target.close()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, BACKUP_DB_NAME)
    finally:
        snapshot.unlink(missing_ok=True)
    return buffer.getvalue()


def restore_app_backup(raw: bytes) -> List[str]:
    """Przywraca kopię z build_app_backup; najpierw sprawdza całość, potem podmienia dane."""
    restored: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        if BACKUP_DB_NAME not in names:
            raise ValueError("To nie jest kopia tej aplikacji (brak bazy w archiwum).")
        incoming = DB_PATH.with_name(f"restore-{time.time_ns()}.sqlite3")
        incoming.write_bytes(archive.read(BACKUP_DB_NAME))
    try:
        source = sqlite3.connect(incoming)
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "meta_jobs" not in tables:
                raise ValueError("Baza w archiwum nie zawiera kolejki metatagów.")
            with db_connect() as conn:
                source.backup(conn)
        finally:
            source.close()
    finally:
        incoming.unlink(missing_ok=True)
    init_db()  # migracje, gdy kopia pochodzi ze starszej wersji
    restored.append("baza zadań i kolejek")
    return restored


def render_backup_panel() -> None:
    with st.expander("Kopia zapasowa danych"):
        st.caption(
            "Streamlit Cloud nie gwarantuje trwałości dysku: restart lub redeploy może wyczyścić kolejki "
            "i wyniki. Pobieraj kopię przed aktualizacją aplikacji."
        )
        lazy_download_button(
            "kopię danych (ZIP)", "app_backup", build_app_backup,
            f"opisy-kopia-{datetime.now().strftime('%Y%m%d-%H%M')}.zip", "application/zip",
        )
        uploaded = st.file_uploader("Przywróć z kopii", type=["zip"], key="backup_upload")
        if uploaded and st.button("Przywróć dane z kopii", key="restore_backup"):
            try:
                restored = restore_app_backup(uploaded.getvalue())
                st.session_state.pop("bulk_results", None)  # odczytaj kolejkę opisów z przywróconej bazy
                st.success("Przywrócono: " + ", ".join(restored) + ".")
            except Exception as exc:
                st.error(f"Nie udało się przywrócić kopii: {exc}")


def lazy_download_button(label: str, key: str, build, file_name: str, mime: str) -> None:
    """Eksport liczony dopiero po kliknięciu, a nie przy każdym rerunie aplikacji."""
    state_key = f"export_data_{key}"
    if st.button(f"Przygotuj: {label}", key=f"prepare_{key}"):
        with st.spinner("Przygotowuję plik..."):
            st.session_state[state_key] = build()
    if st.session_state.get(state_key) is not None:
        st.download_button(f"Pobierz: {label}", st.session_state[state_key], file_name, mime, key=f"download_{key}")


def _toggle_queued_product(key: str, sku: str, title: str) -> None:
    if st.session_state[key]:
        st.session_state.bulk_selected_products[sku] = {"title": title}
    else:
        st.session_state.bulk_selected_products.pop(sku, None)


SHOP_STATUS_ICONS = {"missing": "⛔", "hidden": "⛔", "out_of_stock": "⚠️"}
UNLINKABLE_STATUSES = {"missing", "hidden"}


@st.cache_data(ttl=900, show_spinner=False)
def _cached_shop_statuses(skus: Tuple[str, ...]) -> Dict[str, Dict]:
    return luigis_item_statuses(skus)


def shop_statuses(products: Sequence[Dict]) -> Optional[Dict[str, Dict]]:
    """Status produktów w sklepie (Luigi's Box) albo None, gdy nie udało się sprawdzić."""
    skus = tuple(sorted({str(product["identifier"]) for product in products}))
    if not skus:
        return {}
    try:
        return _cached_shop_statuses(skus)
    except Exception:
        return None


def filter_by_shop_status(products: List[Dict], statuses: Optional[Dict[str, Dict]], key: str) -> List[Dict]:
    """Podsumowanie dostępności w sklepie i opcja ukrycia produktów bez karty na Booklandzie."""
    if statuses is None:
        st.caption("Nie udało się sprawdzić dostępności produktów w sklepie (Luigi's Box).")
        return products
    unlinkable = [p for p in products if statuses.get(str(p["identifier"]), {}).get("status") in UNLINKABLE_STATUSES]
    out_of_stock = sum(statuses.get(str(p["identifier"]), {}).get("status") == "out_of_stock" for p in products)
    if not unlinkable and not out_of_stock:
        st.caption(f"Wszystkie {len(products)} produkty są dostępne w sklepie.")
        return products
    st.caption(
        f"⛔ {len(unlinkable)} bez widocznej karty w sklepie: nie dostaną linków ani URL-a. "
        f"⚠️ {out_of_stock} z kartą, ale bez towaru na stanie."
    )
    if unlinkable and st.checkbox("Ukryj produkty bez karty w sklepie", key=f"hide_unlinkable_{key}"):
        return [p for p in products if p not in unlinkable]
    return products


def shop_status_suffix(statuses: Optional[Dict[str, Dict]], sku: str) -> str:
    status = (statuses or {}).get(str(sku), {}).get("status", "ok")
    return f" · {SHOP_STATUS_ICONS[status]} {SHOP_STATUS_LABELS[status]}" if status in SHOP_STATUS_LABELS else ""


def queue_checkbox(label: str, product: Dict, prefix: str) -> None:
    """Checkbox zawsze zgodny z kolejką (także po „Zaznacz pierwsze N” i czyszczeniu)."""
    sku = product["identifier"]
    key = f"{prefix}_{sku}"
    st.session_state[key] = sku in st.session_state.bulk_selected_products
    st.checkbox(label, key=key, on_change=_toggle_queued_product, args=(key, sku, product["title"]))


def render_result_preview(result: Dict) -> None:
    sku = result["sku"]
    edit_key = f"edit_{sku}"
    description_html = result.get("description_html", "")
    is_meta_only = is_meta_only_result(result) and result.get("meta_only", True)

    if not is_meta_only:
        if result.get("link_report") is not None:
            with st.expander("Dobór linków"):
                report = result["link_report"]
                related_source = report.get("related_source")
                if related_source:
                    st.caption(
                        f"Kandydaci spod karty produktu ({related_source.get('provider', '')}): "
                        f"{related_source.get('title', '')} – {related_source.get('url', '')}"
                    )
                for warning in report["warnings"]:
                    st.warning(warning)
                if report["candidates"]:
                    # Starsze raporty nie zapisywały progów; wtedy pokazujemy bieżące ustawienia.
                    thresholds = report.get("thresholds") or {}
                    score_min = thresholds.get("score", SCORE_THRESHOLD)
                    noul_min = thresholds.get("noul", st.session_state.get("link_threshold", 0.8))
                    st.caption(
                        f"Link jest wybierany, gdy trafność ≥ {score_min:.2f} (skala 0–3) "
                        f"i noul ≥ {noul_min:.2f} (skala 0–1, suwak „Minimalne potwierdzenie dopasowania”)."
                    )
                    frame = pd.DataFrame(report["candidates"])
                    preferred = ["selected", "reason", "kind", "origin", "label", "score", "noul", "relation",
                                 "url", "details"]
                    ordered = [c for c in preferred if c in frame.columns]
                    ordered += [c for c in frame.columns if c not in ordered and c != "source_constraints"]
                    st.dataframe(
                        frame[ordered],
                        hide_index=True,
                        column_config={
                            "reason": st.column_config.TextColumn("Powód odrzucenia"),
                            "score": st.column_config.NumberColumn(
                                f"Trafność (0–3, próg {score_min:.2f})", format="%.2f",
                                help="Ocena Jev, jak przydatny jest cel dla czytelnika opisu.",
                            ),
                            "noul": st.column_config.NumberColumn(
                                f"Noul (0–1, próg {noul_min:.2f})", format="%.2f",
                                help="Prawdopodobieństwo, że fakty potwierdzają powiązanie; próg z suwaka.",
                            ),
                        },
                    )
                if not report["links"]:
                    st.caption("Nie wybrano trafnego, potwierdzonego celu linkowania.")
        editor_seed = result.setdefault(
            "editor_seed",
            hashlib.sha256(description_html.encode()).hexdigest()[:10],
        )
        editor_key = f"visual_editor_{sku}_{editor_seed}"
        tabs = st.tabs(["Edytuj wizualnie", "Podgląd", "HTML"] + (["Research"] if result.get("research") else []))
        with tabs[0]:
            edited_html = visual_html_editor(
                description_html,
                key=editor_key,
                on_change=lambda: st.session_state.__setitem__("active_editor_sku", sku),
            )
            st.session_state[edit_key] = edited_html
            if edited_html != description_html:
                refresh_description_result(result, edited_html)
                save_description_workspace(
                    list(st.session_state.bulk_selected_products),
                    st.session_state.bulk_results,
                )
        if result.get("validation_warnings"):
            st.warning("\n\n".join(issue["message"] for issue in result["validation_warnings"]))
        with tabs[1]:
            st.markdown(st.session_state.get(edit_key, description_html), unsafe_allow_html=True)
        with tabs[2]:
            st.code(st.session_state.get(edit_key, description_html), language="html")
        if result.get("research") and len(tabs) > 3:
            with tabs[3]:
                st.markdown(result["research"])

        col_reg, _ = st.columns([3, 4])
        with col_reg:
            if st.button("♻️ Regeneruj ten opis", key=f"regen_btn_{sku}", type="secondary"):
                try:
                    with st.spinner(f"Regenerowanie opisu dla {sku}..."):
                        token = akeneo_get_token()
                        internal_link = get_internal_link()
                        link_only = st.session_state.get("link_only", False)
                        use_research = st.session_state.get("use_research", True)
                        channel = result.get("channel") or st.session_state.get("channel", DEFAULT_CHANNEL)
                        locale = result.get("locale") or st.session_state.get("locale", DEFAULT_LOCALE)
                        new_res = process_product_from_akeneo(
                            sku,
                            token,
                            channel,
                            locale,
                            internal_link=internal_link,
                            link_only=link_only,
                            use_research=use_research,
                        )
                        new_res = preserve_description_on_failure(result, new_res)
                        for idx, item in enumerate(st.session_state.bulk_results):
                            if item["sku"] == sku:
                                st.session_state.bulk_results[idx] = new_res
                                break
                        st.session_state[edit_key] = new_res.get("description_html", "")
                        save_description_workspace(
                            list(st.session_state.bulk_selected_products),
                            st.session_state.bulk_results,
                        )
                        st.session_state.active_editor_sku = sku
                        st.rerun()
                except Exception as exc:
                    st.error(f"Błąd podczas regeneracji opisu: {exc}")

    if is_meta_only or result.get("meta_title") or result.get("meta_description"):
        with st.expander("Metatagi Magento", expanded=is_meta_only):
            meta_title = result.get("meta_title", "")
            meta_description = result.get("meta_description", "")
            st.text_input("meta_title", value=meta_title, disabled=True, key=f"mt_{sku}")
            st.text_area("meta_description", value=meta_description, disabled=True, height=90, key=f"md_{sku}")
            col1, col2 = st.columns(2)
            col1.caption(f"{len(meta_title)}/{META_TITLE_HARD_MAX} znaków · cel {META_TITLE_TARGET_MIN}-{META_TITLE_TARGET_MAX}")
            col2.caption(f"{len(meta_description)}/160 znaków")
            if result.get("validation_errors"):
                st.warning("; ".join(result["validation_errors"]))


def process_selected_products(
    skus: Sequence[str],
    *,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
    meta_only: bool,
    internal_link: Optional[Dict],
    link_only: bool,
    use_research: bool,
    resume_results: Optional[Dict[str, Dict]] = None,
    checkpoint_path: Optional[Path] = None,
    force_regenerate: bool = False,
    include_warning_checkpoints: bool = True,
) -> List[Dict]:
    """Przetwarzanie interaktywne odporne na odświeżenie Streamlita.

    W generatorze metatagów gotowe wyniki są odzyskiwane kolejno z:
    1) checkpointu wgranego przez użytkownika,
    2) serwerowego CSV zapisywanego w trakcie pracy,
    3) SQLite (dla trybu tylko metatagi).

    Checkpoint CSV i workspace opisów są zapisywane po każdej paczce
    (INTERACTIVE_CHUNK_SIZE produktów); metatagi trafiają do SQLite po każdym SKU.
    """
    ordered_skus = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))
    total = len(ordered_skus)
    if total == 0:
        return []

    results_by_sku: Dict[str, Dict] = {}
    if not force_regenerate:
        for sku, result in (resume_results or {}).items():
            if sku in ordered_skus and is_reusable_result(result, meta_only=meta_only):
                results_by_sku[sku] = result

        if checkpoint_path:
            for sku, result in load_interactive_checkpoint(checkpoint_path).items():
                if sku in ordered_skus and sku not in results_by_sku and is_reusable_result(result, meta_only=meta_only):
                    results_by_sku[sku] = result

        # SQLite przechowuje metatagi po KAŻDYM produkcie, więc odzyskuje nawet
        # rezultat powstały po ostatnim zapisie pliku CSV.
        if meta_only:
            sqlite_results = cached_meta_results_for_skus(
                ordered_skus,
                channel=channel,
                locale=locale,
                include_warnings=include_warning_checkpoints,
            )
            for sku, result in sqlite_results.items():
                if sku not in results_by_sku and is_reusable_result(result, meta_only=True):
                    results_by_sku[sku] = result

    pending = [sku for sku in ordered_skus if sku not in results_by_sku]
    resumed_count = total - len(pending)

    progress = st.progress(
        resumed_count / total,
        f"Wznowiono: {resumed_count}/{total} gotowych · do zrobienia {len(pending)}",
    )
    status_box = st.empty()
    if resumed_count:
        status_box.info(
            f"Pominięto {resumed_count} już zapisanych SKU. Gemini dostanie tylko {len(pending)} pozostałych."
        )

    newly_processed = 0
    max_workers = GEMINI_INTERACTIVE_WORKERS

    for chunk_start in range(0, len(pending), INTERACTIVE_CHUNK_SIZE):
        chunk = pending[chunk_start : chunk_start + INTERACTIVE_CHUNK_SIZE]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            if meta_only:
                futures = {
                    executor.submit(
                        process_product_meta_only,
                        sku,
                        token,
                        channel,
                        locale,
                        store_view_code,
                    ): sku
                    for sku in chunk
                }
            else:
                futures = {
                    executor.submit(
                        process_product_from_akeneo,
                        sku,
                        token,
                        channel,
                        locale,
                        internal_link,
                        link_only,
                        use_research,
                    ): sku
                    for sku in chunk
                }

            for future in as_completed(futures):
                sku = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"sku": sku, "title": "", "error": str(exc), "meta_only": meta_only}
                results_by_sku[sku] = result
                newly_processed += 1
                done_count = resumed_count + newly_processed
                progress.progress(
                    done_count / total,
                    f"Gotowe {done_count}/{total} · nowe {newly_processed} · wznowione {resumed_count}",
                )

        # Zapis raz na paczkę: pełny zapis po każdym SKU rósł kwadratowo z długością listy.
        if not meta_only:
            save_description_workspace(
                ordered_skus,
                [results_by_sku[item] for item in ordered_skus if item in results_by_sku],
            )
        if checkpoint_path:
            write_interactive_checkpoint(checkpoint_path, ordered_skus, results_by_sku)

    progress.progress(1.0, f"Gotowe {total}/{total}")
    if checkpoint_path:
        status_box.success(f"Checkpoint zapisany: {checkpoint_path.name}")

    # Zachowujemy kolejność wejściowego CSV/SKU.
    return [results_by_sku[sku] for sku in ordered_skus if sku in results_by_sku]
