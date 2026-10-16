"""Streamlit: generator opisów i metatagów Bookland (interfejs; logika w modułach obok)."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

from akeneo import (
    akeneo_fetch_backlog,
    akeneo_fetch_categories,
    akeneo_get_token,
    akeneo_product_exists,
    akeneo_search_products,
    akeneo_update_description,
)
from batch import (
    cached_batch_input_estimate,
    import_catalog_to_meta_queue,
    import_skus_to_meta_queue,
    refresh_and_ingest_batch_jobs,
    submit_queued_batches,
)
from checkpoints import (
    cached_meta_results_for_skus,
    interactive_checkpoint_key,
    interactive_checkpoint_path,
    load_interactive_checkpoint,
    parse_resumable_product_file,
)
from config import (
    AKENEO_MAX_WORKERS,
    APP_NAME,
    APP_VERSION,
    BATCH_PRODUCTS_PER_FILE,
    DEFAULT_CHANNEL,
    DEFAULT_LOCALE,
    GEMINI_HTTP_TIMEOUT_MS,
    GEMINI_INTERACTIVE_WORKERS,
    GEMINI_MODEL,
    INTERACTIVE_CHECKPOINT_DIR,
    INTERACTIVE_CHUNK_SIZE,
    PROMPT_VERSION,
    REQUIRED_SECRETS,
    RESULT_PREVIEW_LIMIT,
)
from description_output import (
    current_description_value,
    deliver_description_result,
    is_reusable_result,
    refresh_description_result,
)
from job_store import (
    active_batch_job_count,
    create_description_workspace,
    export_meta_csv,
    export_previous_metatags_csv,
    export_quality_report_csv,
    init_db,
    list_batch_jobs,
    list_description_workspaces,
    list_meta_delivery_jobs,
    list_meta_jobs,
    list_meta_runs,
    meta_status_counts,
    new_run_id,
    recommended_batch_shard_size,
    requeue_jobs,
    save_description_workspace,
)
from meta_titles import parse_validation_error_list
from metatags import audit_meta_jobs_for_repetition, revalidate_meta_jobs, send_metatags_to_akeneo
from product_input import ProductInputResolutionError, resolve_product_inputs
from text_utils import parse_bulk_sku_payload
from ui import (
    clear_product_queue,
    get_internal_link,
    init_session_state,
    lazy_download_button,
    process_selected_products,
    queue_checkbox,
    render_backup_panel,
    render_link_catalog,
    render_result_preview,
    reset_interactive_results,
    switch_description_workspace,
)
from workspace_storage import LEGACY_WORKSPACE_ID


st.set_page_config(
    page_title=f"{APP_NAME} v{APP_VERSION}",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .main-header { font-size: 2.35rem; font-weight: 700; margin-bottom: .35rem; }
    .sub-header { color: #666; font-size: 1rem; margin-bottom: 1.5rem; }
    .small-note { font-size: .86rem; color: #666; }
</style>
""",
    unsafe_allow_html=True,
)

missing_secrets = [key for key in REQUIRED_SECRETS if key not in st.secrets]

if missing_secrets:
    st.error(f"Brak kluczy w secrets.toml: {', '.join(missing_secrets)}")
    st.stop()

init_db()

init_session_state()

st.markdown(f'<h1 class="main-header">📚 {APP_NAME}</h1>', unsafe_allow_html=True)

st.markdown(
    f'<p class="sub-header">v{APP_VERSION} · Gemini: {GEMINI_MODEL}</p>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Generator")
    generator_mode = st.radio(
        "Wybierz osobny tryb pracy",
        ["Generator opisów", "Generator metatagów"],
        key="generator_mode",
        on_change=reset_interactive_results,
    )
    st.session_state.meta_only = generator_mode == "Generator metatagów"
    st.caption(
        "Tworzy wyłącznie pełne opisy HTML."
        if not st.session_state.meta_only
        else "Tworzy wyłącznie meta title i meta description. Opisy produktów pozostają bez zmian."
    )

    if not st.session_state.meta_only:
        st.markdown("---")
        st.subheader("Kolejka robocza")
        workspace_rows = list_description_workspaces()
        workspace_names = {item["workspace_id"]: item["workspace_name"] for item in workspace_rows}
        workspace_ids = list(workspace_names)
        current_workspace_id = st.session_state.get("description_workspace_id", LEGACY_WORKSPACE_ID)
        if current_workspace_id not in workspace_names and workspace_ids:
            current_workspace_id = workspace_ids[0]
            switch_description_workspace(current_workspace_id)

        selected_workspace_id = st.selectbox(
            "Wybierz kolejkę",
            workspace_ids,
            index=workspace_ids.index(current_workspace_id) if current_workspace_id in workspace_ids else 0,
            format_func=lambda item: workspace_names[item],
            help="Każda kolejka ma własną listę produktów, wyniki i wersje robocze opisów.",
        )
        if selected_workspace_id != st.session_state.get("description_workspace_id"):
            switch_description_workspace(selected_workspace_id)
            st.rerun()

        with st.expander("Dodaj nową kolejkę"):
            if st.session_state.pop("clear_new_description_workspace_name", False):
                st.session_state["new_description_workspace_name"] = ""
            new_workspace_name = st.text_input(
                "Nazwa kolejki",
                placeholder="np. Bartek, Marcin, BOK 1",
                key="new_description_workspace_name",
            )
            if st.button("Utwórz i przełącz", key="create_description_workspace"):
                try:
                    created_workspace = create_description_workspace(new_workspace_name)
                    st.session_state.clear_new_description_workspace_name = True
                    switch_description_workspace(created_workspace["workspace_id"])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        active_workspace_name = workspace_names.get(
            st.session_state.get("description_workspace_id"),
            st.session_state.get("description_workspace_id", ""),
        )
        st.caption(
            f"Aktywna kolejka: {active_workspace_name}. Wyczyść kolejkę usuwa tylko jej wersję roboczą."
        )

    st.markdown("---")
    st.subheader("Ustawienia wspólne")
    channel = st.selectbox("Kanał", [DEFAULT_CHANNEL, "B2B"], index=0)
    locale = st.text_input("Locale", value=DEFAULT_LOCALE)
    st.session_state.magento_store_view = st.text_input(
        "Magento store_view_code",
        value=st.session_state.magento_store_view,
    )

    if not st.session_state.meta_only:
        st.markdown("---")
        st.subheader("Opcje opisów")
        st.session_state.link_active = st.checkbox("Włącz linkowanie", value=st.session_state.link_active)
        st.session_state.link_mode = st.radio("Dobór celów", ["Automatyczne (Jev)", "Ręczne"], horizontal=True,
                                              index=1 if st.session_state.get("link_mode") == "Ręczne" else 0)
        st.session_state.link_only = st.checkbox(
            "Tylko dodaj link - bez przepisywania opisu",
            value=st.session_state.link_only,
        )
        if st.session_state.link_mode == "Automatyczne (Jev)":
            st.session_state.link_threshold = st.slider("Minimalne potwierdzenie dopasowania", 0.5, 1.0,
                                                        st.session_state.get("link_threshold", 0.8), 0.05)
            st.caption("Jev ocenia w jednym requeście wszystkie kategorie ze ścieżki produktu i wszystkie produkty spod karty (Luigi's Box) i wybiera do 2 linków.")
            if not st.secrets.get("TYPESAFE_API_KEY"):
                st.warning("Dodaj TYPESAFE_API_KEY w sekretach Streamlit. Bez klucza opis powstanie bez automatycznych linków.")
            render_link_catalog(locale)
        else:
            st.session_state.link_url = st.text_input("URL linku", value=st.session_state.link_url)
            st.session_state.link_category = st.text_input("Kategoria / anchor hint", value=st.session_state.link_category)
        st.session_state.use_research = st.checkbox(
            "Wzbogacaj opisy researchem Perplexity",
            value=st.session_state.use_research,
        )
        if st.session_state.use_research and "PERPLEXITY_API_KEY" not in st.secrets:
            st.caption("Brak PERPLEXITY_API_KEY - research będzie pomijany.")

    st.markdown("---")
    render_backup_panel()

interactive_tab, scale_tab, results_tab = st.tabs(
    [generator_mode, "Metatagi 50k / Batch API", "Kontrola metatagów"]
)

with interactive_tab:
    st.header(generator_mode)
    st.info(
        "Ten generator tworzy pełne opisy HTML. Metatagi są generowane w osobnym trybie."
        if not st.session_state.meta_only
        else "Ten generator nie tworzy ani nie zmienia opisów produktów."
    )
    st.subheader("Wybór produktów")
    file_method = "CSV / TXT z checkpointem" if st.session_state.meta_only else "CSV / TXT ze SKU"
    method = st.radio(
        "Metoda",
        ["Wyszukaj", "Wklej SKU lub URL", file_method, "Backlog"],
        horizontal=True,
        key="interactive_method",
    )

    if method == "Wyszukaj":
        col_q, col_l = st.columns([4, 1])
        query = col_q.text_input("Szukaj produktu")
        limit = col_l.number_input("Limit", 5, 50, 10)
        if st.button("Szukaj", type="primary") and query:
            try:
                st.session_state.search_res = akeneo_search_products(query, akeneo_get_token(), int(limit), locale)
            except Exception as exc:
                st.error(str(exc))
        if st.session_state.search_res:
            with st.container(height=420):
                for product in st.session_state.search_res:
                    queue_checkbox(f"{product['identifier']} - {product['title']}", product, "search")

    elif method == "Wklej SKU lub URL":
        loaded_count = st.session_state.pop("manual_products_loaded", 0)
        if loaded_count:
            st.session_state.manual_product_input = ""
            st.success(f"Dodano {loaded_count} produktów.")
        raw_text = st.text_area(
            "SKU lub URL - jeden na linię",
            height=170,
            key="manual_product_input",
        )
        if st.button("Załaduj produkty", type="primary"):
            raw_inputs = [line.strip() for line in raw_text.splitlines() if line.strip()]
            try:
                resolved, input_errors = resolve_product_inputs(raw_inputs)
                url_items = [item for item in resolved if item["source"] == "url"]
                statuses: Dict[str, Optional[bool]] = {}
                if url_items:
                    token = akeneo_get_token()
                    unique_skus = list(dict.fromkeys(item["sku"] for item in url_items))
                    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
                        futures = {executor.submit(akeneo_product_exists, sku, token): sku for sku in unique_skus}
                        for future in as_completed(futures):
                            sku = futures[future]
                            try:
                                statuses[sku] = future.result()
                            except Exception:
                                statuses[sku] = None
                accepted = 0
                for item in resolved:
                    if item["source"] == "url" and statuses.get(item["sku"]) is not True:
                        input_errors.append(f"Nie zweryfikowano SKU z URL: {item['input']}")
                        continue
                    st.session_state.bulk_selected_products[item["sku"]] = {"title": item.get("title", item["sku"])}
                    accepted += 1
                if accepted:
                    if input_errors:
                        st.success(f"Dodano {accepted} produktów.")
                    else:
                        st.session_state.manual_products_loaded = accepted
                        st.rerun()
                if input_errors:
                    st.warning("\n".join(f"- {error}" for error in input_errors))
            except ProductInputResolutionError as exc:
                st.error(str(exc))

    elif method == file_method:
        if st.session_state.meta_only:
            st.caption(
                "Wgraj zwykły plik z kolumną SKU albo wcześniejszy checkpoint CSV. "
                "SKU z gotowymi metatagami zostaną pominięte."
            )
            INTERACTIVE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            existing_checkpoint_files = sorted(
                INTERACTIVE_CHECKPOINT_DIR.glob("interactive-*.csv"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:10]
            if existing_checkpoint_files:
                checkpoint_options = {"": "— wybierz checkpoint —"}
                for checkpoint_file in existing_checkpoint_files:
                    try:
                        checkpoint_df = pd.read_csv(checkpoint_file, dtype=str, keep_default_na=False)
                        done_count = int((checkpoint_df.get("checkpoint_status", pd.Series(dtype=str)).isin(["completed", "warning"])).sum())
                        total_count = len(checkpoint_df)
                    except Exception:
                        done_count, total_count = 0, 0
                    checkpoint_options[str(checkpoint_file)] = (
                        f"{checkpoint_file.name} · {done_count}/{total_count} gotowych"
                    )
                selected_existing_checkpoint = st.selectbox(
                    "Wznów checkpoint zapisany na serwerze",
                    options=list(checkpoint_options),
                    format_func=lambda value: checkpoint_options[value],
                    key="interactive_existing_checkpoint",
                )
                if selected_existing_checkpoint and st.button("Wczytaj checkpoint z serwera", key="load_server_checkpoint"):
                    checkpoint_file = Path(selected_existing_checkpoint)
                    file_skus, seed_results, _ = parse_resumable_product_file(checkpoint_file.read_bytes())
                    st.session_state.bulk_selected_products = {sku: {"title": sku} for sku in file_skus}
                    st.session_state.interactive_seed_results = seed_results
                    st.session_state.last_interactive_checkpoint_path = str(checkpoint_file)
                    st.success(f"Wczytano {len(file_skus)} SKU; gotowe: {len(seed_results)}.")
                    st.rerun()
        else:
            st.caption("Wgraj CSV, TSV lub TXT z kolumną SKU. Wszystkie produkty zostaną wygenerowane od nowa.")

        resume_file = st.file_uploader(
            "CSV / TSV / TXT",
            type=["csv", "tsv", "txt"],
            key="interactive_resume_file",
        )
        if resume_file is not None:
            try:
                file_skus, seed_results, file_errors = parse_resumable_product_file(resume_file.getvalue())
                if file_skus:
                    st.info(f"Plik: {len(file_skus):,} SKU".replace(",", " "))
                    if st.button("Załaduj plik do kolejki", type="primary", key="load_interactive_resume_file"):
                        st.session_state.bulk_selected_products = {sku: {"title": sku} for sku in file_skus}
                        st.session_state.interactive_seed_results = seed_results if st.session_state.meta_only else {}
                        message = f"Załadowano {len(file_skus):,} SKU."
                        if st.session_state.meta_only:
                            message += f" {len(seed_results):,} ma już metatagi i zostanie pominiętych."
                        st.success(message.replace(",", " "))
                        st.rerun()
                else:
                    st.warning("Nie znaleziono SKU w pliku.")
                if file_errors:
                    st.warning("\n".join(file_errors))
            except Exception as exc:
                st.error(f"Nie udało się odczytać pliku: {exc}")

    else:
        try:
            token = akeneo_get_token()
            categories = akeneo_fetch_categories(token, locale)
            category_options = {"": "Wszystkie kategorie"}
            category_options.update({item["code"]: f"{item['label']} ({item['code']})" for item in categories})
            selected_category = st.selectbox(
                "Kategoria",
                options=list(category_options),
                format_func=lambda code: category_options[code],
            )
            col1, col2 = st.columns(2)
            backlog_limit = col1.number_input("Ile załadować", 10, 500, 100, 10)
            exclude_days = col2.number_input("Pomiń aktualizowane przez N dni", 0, 365, 30)
            only_no_desc = st.checkbox("Tylko bez opisu")
            max_len = st.number_input("Maksymalna długość opisu, 0 = bez limitu", 0, 10000, 0, 50)
            if st.button("Załaduj backlog", type="primary"):
                st.session_state.backlog_items = akeneo_fetch_backlog(
                    token,
                    channel,
                    locale,
                    int(backlog_limit),
                    category=selected_category or None,
                    exclude_updated_days=int(exclude_days) or None,
                    only_without_desc=only_no_desc,
                    max_desc_len=int(max_len) if max_len and not only_no_desc else None,
                )
            if st.session_state.backlog_items:
                n_select = st.number_input(
                    "Zaznacz pierwszych N",
                    1,
                    len(st.session_state.backlog_items),
                    min(10, len(st.session_state.backlog_items)),
                )
                if st.button("Zaznacz pierwsze N"):
                    for product in st.session_state.backlog_items[: int(n_select)]:
                        st.session_state.bulk_selected_products[product["identifier"]] = {"title": product["title"]}
                    st.rerun()
                with st.container(height=420):
                    for product in st.session_state.backlog_items:
                        label = f"{product['identifier']} - {product['title']} ({product['desc_len']} zn.)"
                        queue_checkbox(label, product, "backlog")
        except Exception as exc:
            st.error(str(exc))

    if st.session_state.bulk_selected_products:
        st.info(f"W kolejce: {len(st.session_state.bulk_selected_products)} produktów")
        col_clear, _ = st.columns([1, 4])
        col_clear.button("Wyczyść kolejkę", on_click=clear_product_queue)

        st.markdown("---")
        st.subheader("Generowanie opisów" if not st.session_state.meta_only else "Generowanie metatagów")
        if st.session_state.meta_only:
            col_resume_a, col_resume_b = st.columns(2)
            force_regenerate = col_resume_a.checkbox(
                "Wymuś generowanie od zera",
                key="force_regenerate_meta",
                help="Włącz tylko, jeśli chcesz ponownie wygenerować już gotowe metatagi.",
            )
            include_warning_checkpoints = col_resume_b.checkbox(
                "Przy wznowieniu akceptuj też wyniki z ostrzeżeniem",
                key="reuse_warning_meta",
                help="Ostrzeżenie jakościowe nie oznacza błędu API.",
            )
        else:
            force_regenerate = True
            include_warning_checkpoints = False
            st.caption("Każde uruchomienie tworzy wszystkie wybrane opisy od nowa.")

        checkpoint_path: Optional[Path] = None
        if st.session_state.meta_only:
            skus_for_checkpoint = list(st.session_state.bulk_selected_products)
            checkpoint_key = interactive_checkpoint_key(
                skus_for_checkpoint,
                channel=channel,
                locale=locale,
                store_view_code=st.session_state.magento_store_view,
            )
            checkpoint_path = interactive_checkpoint_path(checkpoint_key)
            st.session_state.last_interactive_checkpoint_path = str(checkpoint_path)
            seed_results = set(st.session_state.get("interactive_seed_results", {}))
            server_results = set(load_interactive_checkpoint(checkpoint_path))
            try:
                sqlite_results = set(
                    cached_meta_results_for_skus(
                        skus_for_checkpoint, channel=channel, locale=locale,
                        include_warnings=include_warning_checkpoints,
                    )
                )
            except Exception:
                sqlite_results = set()
            reusable_estimate = 0 if force_regenerate else min(
                len(skus_for_checkpoint), len(seed_results | server_results | sqlite_results)
            )
            st.caption(
                f"Checkpoint: {checkpoint_path.name} · rozpoznane gotowe SKU: około {reusable_estimate}/{len(skus_for_checkpoint)} "
                f"(plik wgrany {len(seed_results)}, serwer CSV {len(server_results)}, SQLite {len(sqlite_results)})."
            )
        else:
            st.session_state.last_interactive_checkpoint_path = ""
        st.caption(
            f"Tryb stabilny v{APP_VERSION}: paczki po {INTERACTIVE_CHUNK_SIZE}, {GEMINI_INTERACTIVE_WORKERS} równoległe workery, "
            f"timeout Gemini {GEMINI_HTTP_TIMEOUT_MS // 1000}s. "
            + (
                "Checkpoint jest zapisywany po każdym SKU."
                if st.session_state.meta_only else "Generator opisów nie używa checkpointów."
            )
        )

        if checkpoint_path and checkpoint_path.exists():
            st.download_button(
                "Pobierz bieżący checkpoint CSV",
                checkpoint_path.read_bytes(),
                file_name=checkpoint_path.name,
                mime="text/csv",
                key="download_interactive_checkpoint_before_run",
            )

        start_label = "Generuj opisy od nowa" if not st.session_state.meta_only else "Start / wznów generowanie metatagów"
        if st.button(start_label, type="primary"):
            skus = list(st.session_state.bulk_selected_products)
            try:
                if not st.session_state.meta_only:
                    save_description_workspace(skus, [])
                st.session_state.bulk_results = process_selected_products(
                    skus,
                    token=akeneo_get_token(),
                    channel=channel,
                    locale=locale,
                    store_view_code=st.session_state.magento_store_view,
                    meta_only=st.session_state.meta_only,
                    internal_link=get_internal_link() if not st.session_state.meta_only else None,
                    link_only=st.session_state.link_only if not st.session_state.meta_only else False,
                    use_research=st.session_state.use_research if not st.session_state.meta_only else False,
                    resume_results=(
                        st.session_state.get("interactive_seed_results", {})
                        if st.session_state.meta_only else {}
                    ),
                    checkpoint_path=checkpoint_path,
                    force_regenerate=force_regenerate,
                    include_warning_checkpoints=include_warning_checkpoints,
                )
                st.session_state.products_to_send = {
                    result["sku"]: True for result in st.session_state.bulk_results if not result.get("error")
                }
                st.session_state.interactive_seed_results = (
                    {
                        result["sku"]: result
                        for result in st.session_state.bulk_results
                        if is_reusable_result(result, meta_only=True)
                    }
                    if st.session_state.meta_only else {}
                )
            except Exception as exc:
                st.error(str(exc))

    if st.session_state.bulk_results:
        results = st.session_state.bulk_results
        if not st.session_state.meta_only:
            st.caption(
                "Wyniki i zmiany w edytorze zapisują się automatycznie w obszarze roboczym. "
                "Aby opublikować je w Akeneo, nadal kliknij „Wyślij zaznaczone”."
            )
        if not st.session_state.meta_only:
            for item in results:
                current_html = current_description_value(item, st.session_state)
                if not item.get("error") or current_html != item.get("description_html", ""):
                    refresh_description_result(item, current_html)
                    st.session_state[f"edit_{item['sku']}"] = item["description_html"]
        ok = [item for item in results if not item.get("error")]
        errors = [item for item in results if item.get("error")]
        warning_count = sum(bool(item.get("validation_warnings")) for item in ok)
        col_ok, col_warn, col_err = st.columns(3)
        col_ok.metric("Gotowe", len(ok) - warning_count)
        col_warn.metric("Gotowe z ostrzeżeniami", warning_count)
        col_err.metric("Błędy", len(errors))

        export_rows = [dict(item) for item in results]
        for row in export_rows:
            if "validation_warnings" in row:
                row["validation_warnings"] = json.dumps(row["validation_warnings"], ensure_ascii=False)
        data_frame = pd.DataFrame(export_rows)
        st.download_button(
            "Pobierz opisy CSV" if not st.session_state.meta_only else "Pobierz metatagi CSV",
            data_frame.to_csv(index=False).encode("utf-8-sig"),
            "opisy_produkty.csv" if not st.session_state.meta_only else "metatagi_produkty.csv",
            "text/csv",
        )

        checkpoint_path_value = st.session_state.get("last_interactive_checkpoint_path", "")
        if st.session_state.meta_only and checkpoint_path_value and Path(checkpoint_path_value).exists():
            checkpoint_file = Path(checkpoint_path_value)
            st.download_button(
                "Pobierz checkpoint do późniejszego wznowienia",
                checkpoint_file.read_bytes(),
                file_name=checkpoint_file.name,
                mime="text/csv",
                key="download_interactive_checkpoint_after_run",
            )
            st.caption(
                "Ten plik możesz później wgrać w trybie „CSV / TXT z checkpointem”. "
                "Wiersze z gotowymi metatagami zostaną pominięte."
            )

        if ok and not any(item.get("meta_only") for item in ok):
            st.subheader("Wysyłka opisów do Akeneo")
            to_send: List[Dict] = []
            for item in ok:
                checked = st.checkbox(
                    f"{item['sku']} - {item['title']}",
                    value=st.session_state.products_to_send.get(item["sku"], True),
                    key=f"send_{item['sku']}",
                )
                st.session_state.products_to_send[item["sku"]] = checked
                if checked:
                    to_send.append(item)
            if st.button(f"Wyślij zaznaczone ({len(to_send)})", type="primary"):
                progress = st.progress(0)
                sent = 0
                send_errors = []
                for index, item in enumerate(to_send, start=1):
                    try:
                        final_html = st.session_state.get(f"edit_{item['sku']}", item["description_html"])
                        deliver_description_result(
                            item, final_html, akeneo_update_description,
                            channel=channel, locale=locale,
                        )
                        sent += 1
                    except Exception as exc:
                        send_errors.append(f"{item['sku']}: {exc}")
                    progress.progress(index / max(len(to_send), 1))
                st.success(f"Wysłano {sent} opisów.")
                if send_errors:
                    st.error("\n".join(send_errors))

        meta_ready = [
            item for item in ok
            if item.get("meta_only")
            and item.get("meta_title")
            and item.get("meta_description")
            and not parse_validation_error_list(item.get("validation_errors"))
        ]
        if meta_ready:
            st.subheader("Wysyłka metatagów do Akeneo")
            st.caption(
                "Aktualizacja zapisuje wyłącznie atrybuty meta_title i meta_description "
                "dla wybranego kanału. Opis produktu i pozostałe pola nie są zmieniane."
            )
            meta_to_send: List[Dict] = []
            for item in meta_ready:
                checked = st.checkbox(
                    f"{item['sku']} - {item.get('title', '')}",
                    value=True,
                    key=f"send_meta_{item['sku']}",
                )
                if checked:
                    meta_to_send.append(item)
            if st.button(
                f"Wyślij metatagi do Akeneo ({len(meta_to_send)})",
                type="primary",
                disabled=not meta_to_send,
                key="send_interactive_meta_to_akeneo",
            ):
                progress = st.progress(0, "Przygotowuję wysyłkę metatagów...")

                def interactive_meta_progress(done: int, total: int) -> None:
                    progress.progress(done / max(total, 1), f"Wysłano {done}/{total}")

                try:
                    stats = send_metatags_to_akeneo(
                        meta_to_send,
                        channel,
                        locale,
                        progress_callback=interactive_meta_progress,
                    )
                    st.success(
                        f"Wysłano metatagi dla {stats['sent']} produktów"
                        f" (bez zmian w Akeneo: {stats.get('unchanged', 0)})."
                    )
                    if stats["errors"]:
                        st.error("\n".join(stats["errors"]))
                except Exception as exc:
                    st.error(f"Nie udało się rozpocząć wysyłki do Akeneo: {exc}")

        st.subheader("Podgląd wyników")
        active_editor_sku = st.session_state.get("active_editor_sku", "")
        preview_results = results
        if not st.session_state.meta_only:
            warning_codes = sorted({issue["code"] for item in results for issue in item.get("validation_warnings", [])})
            selected_warning = st.selectbox("Filtr ostrzeżeń w podglądzie", ["Wszystkie", *warning_codes])
            if selected_warning != "Wszystkie":
                preview_results = [item for item in results if any(issue["code"] == selected_warning for issue in item.get("validation_warnings", []))]
                st.caption(f"Produkty z tym ostrzeżeniem: {len(preview_results)}")
        for result in preview_results[:RESULT_PREVIEW_LIMIT]:
            label = "❌" if result.get("error") else ("⚠️" if result.get("validation_warnings") else "✅")
            is_active = (result["sku"] == active_editor_sku) or (bool(result.get("error")) and not active_editor_sku)
            with st.expander(
                f"{label} {result['sku']} - {result.get('title', '')}",
                expanded=is_active,
            ):
                if result.get("error"):
                    st.error(result["error"])
                render_result_preview(result)
        if len(preview_results) > RESULT_PREVIEW_LIMIT:
            st.caption(f"Wyświetlono pierwsze {RESULT_PREVIEW_LIMIT} wyników, aby nie przeciążać Streamlita.")

with scale_tab:
    st.subheader("Metatagi dla dużej listy SKU: trwała kolejka + Gemini Batch API")
    st.write(
        "Tutaj możesz wkleić lub wgrać nawet dziesiątki tysięcy SKU. Produkty są pobierane z Akeneo "
        "paczkami, zapisywane w SQLite i dopiero potem wysyłane do Gemini Batch API. Lista nie trafia "
        "do interaktywnego session_state jako 50 tys. osobnych elementów."
    )

    try:
        token = akeneo_get_token()
        source_mode = st.radio(
            "Źródło produktów",
            ["Lista SKU", "Cały katalog lub kategoria"],
            horizontal=True,
            key="scale_source_mode",
        )

        col_a, col_b = st.columns(2)
        enabled_only = col_a.checkbox("Tylko aktywne produkty", value=True, key="scale_enabled_only")
        only_with_description = col_b.checkbox(
            "Tylko produkty z opisem źródłowym",
            value=True,
            key="scale_only_with_description",
        )
        force_regenerate = st.checkbox(
            "Wygeneruj ponownie także niezmienione, ukończone produkty",
            value=False,
            key="scale_force_regenerate",
        )

        if source_mode == "Lista SKU":
            st.caption(
                "Obsługiwany format: jeden SKU na linię albo CSV/TSV z kolumną sku, identifier, product_sku lub kod produktu."
            )
            uploaded_skus = st.file_uploader(
                "Plik SKU",
                type=["txt", "csv", "tsv"],
                key="scale_sku_file",
            )
            pasted_skus = st.text_area(
                "Albo wklej SKU - jeden na linię",
                height=180,
                key="scale_sku_text",
            )

            payload_parts: List[str] = []
            if uploaded_skus is not None:
                raw_bytes = uploaded_skus.getvalue()
                try:
                    payload_parts.append(raw_bytes.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    payload_parts.append(raw_bytes.decode("cp1250", errors="replace"))
            if pasted_skus.strip():
                payload_parts.append(pasted_skus)
            parsed_skus, duplicate_count = parse_bulk_sku_payload("\n".join(payload_parts))
            if parsed_skus:
                st.info(
                    f"Rozpoznano {len(parsed_skus):,} unikalnych SKU".replace(",", " ")
                    + (f"; usunięto {duplicate_count:,} duplikatów".replace(",", " ") if duplicate_count else "")
                )

            if st.button("1. Pobierz wskazane SKU i przygotuj kolejkę", type="primary"):
                if not parsed_skus:
                    st.error("Nie znaleziono żadnych poprawnych SKU w polu ani w pliku.")
                else:
                    run_id = new_run_id("sku")
                    progress_text = st.empty()

                    def sku_callback(stats: Dict[str, object]) -> None:
                        progress_text.info(
                            f"Sprawdzono {int(stats['processed']):,}/{int(stats['unique']):,} · "
                            f"znaleziono {int(stats['found']):,} · w kolejce {int(stats['queued']):,} · "
                            f"brak {int(stats['missing']):,}".replace(",", " ")
                        )

                    stats = import_skus_to_meta_queue(
                        skus=parsed_skus,
                        token=token,
                        channel=channel,
                        locale=locale,
                        store_view_code=st.session_state.magento_store_view,
                        enabled_only=enabled_only,
                        only_with_description=only_with_description,
                        force_regenerate=force_regenerate,
                        run_id=run_id,
                        progress_callback=sku_callback,
                    )
                    st.session_state.active_meta_run_id = run_id
                    st.session_state.last_import_report_path = str(stats.get("report_path", ""))
                    st.success(
                        f"Import {run_id}: {int(stats['queued']):,} produktów w kolejce, "
                        f"{int(stats['skipped_unchanged']):,} już gotowych i niezmienionych, "
                        f"{int(stats['missing']):,} nie znaleziono, "
                        f"{int(stats['errors']):,} z błędem Akeneo (szczegóły w raporcie), "
                        f"{int(stats['inactive']):,} nieaktywnych, "
                        f"{int(stats['without_description']):,} bez opisu.".replace(",", " ")
                    )

        else:
            categories = akeneo_fetch_categories(token, locale)
            category_options = {"": "Wszystkie kategorie"}
            category_options.update({item["code"]: f"{item['label']} ({item['code']})" for item in categories})
            selected_category = st.selectbox(
                "Kategoria katalogu",
                options=list(category_options),
                format_func=lambda code: category_options[code],
                key="scale_category",
            )
            max_products_input = st.number_input(
                "Limit importu, 0 = cały katalog",
                min_value=0,
                max_value=500000,
                value=0,
                step=1000,
            )
            if st.button("1. Pobierz katalog i przygotuj kolejkę", type="primary"):
                run_id = new_run_id("catalog")
                progress_text = st.empty()

                def catalog_callback(stats: Dict[str, int]) -> None:
                    progress_text.info(
                        f"Odczytano {stats['seen']} · w kolejce {stats['queued']} · "
                        f"pominięto niezmienione {stats['skipped_unchanged']}"
                    )

                stats = import_catalog_to_meta_queue(
                    token=token,
                    channel=channel,
                    locale=locale,
                    store_view_code=st.session_state.magento_store_view,
                    category=selected_category or None,
                    enabled_only=enabled_only,
                    only_with_description=only_with_description,
                    max_products=int(max_products_input) or None,
                    force_regenerate=force_regenerate,
                    run_id=run_id,
                    progress_callback=catalog_callback,
                )
                st.session_state.active_meta_run_id = run_id
                st.success(
                    f"Import {run_id}: odczytano {stats['seen']}, dodano do kolejki {stats['queued']}, "
                    f"pominięto niezmienione {stats['skipped_unchanged']}."
                )

        runs = list_meta_runs()
        run_ids = [row["run_id"] for row in runs]
        active_run = st.session_state.get("active_meta_run_id", "")
        if run_ids:
            default_index = run_ids.index(active_run) if active_run in run_ids else 0
            selected_run = st.selectbox(
                "Aktywna partia",
                options=run_ids,
                index=default_index,
                format_func=lambda run_id: next(
                    (
                        f"{run_id} · {row['product_count']} produktów · {row['source_type']}"
                        for row in runs if row["run_id"] == run_id
                    ),
                    run_id,
                ),
                key="scale_active_run_select",
            )
            st.session_state.active_meta_run_id = selected_run
        else:
            selected_run = ""

        report_path_value = st.session_state.get("last_import_report_path", "")
        if report_path_value and Path(report_path_value).exists():
            st.download_button(
                "Pobierz raport SKU odrzuconych przy imporcie",
                Path(report_path_value).read_bytes(),
                Path(report_path_value).name,
                "text/tab-separated-values",
            )

        if selected_run:
            counts = meta_status_counts(selected_run)
            metrics = st.columns(5)
            metrics[0].metric("W kolejce", counts.get("queued", 0))
            metrics[1].metric("W batchach", counts.get("batch_submitted", 0))
            metrics[2].metric("Gotowe", counts.get("completed", 0))
            metrics[3].metric("Do poprawy", counts.get("validation_failed", 0))
            metrics[4].metric("Błędy", counts.get("failed", 0))

            queued_count = int(counts.get("queued", 0))
            recommended_size = recommended_batch_shard_size(queued_count)
            auto_shard = st.checkbox(
                "Automatyczny Turbo Sharding (zalecane)",
                value=True,
                help="Dla dużych runów celuje w ok. 1000 SKU/shard. 50k ≈ 50 niezależnych jobs zamiast jednego wielkiego zadania.",
            )
            if auto_shard:
                products_per_batch = recommended_size
                st.text_input("Rozmiar sharda", value=str(products_per_batch), disabled=True)
            else:
                products_per_batch = st.number_input(
                    "Rozmiar sharda Batch API",
                    min_value=250,
                    max_value=5000,
                    value=BATCH_PRODUCTS_PER_FILE,
                    step=250,
                )

            shard_count = (queued_count + int(products_per_batch) - 1) // int(products_per_batch) if queued_count else 0
            active_jobs = active_batch_job_count(selected_run)
            estimate = cached_batch_input_estimate(selected_run, queued_count)
            plan_cols = st.columns(4)
            plan_cols[0].metric("SKU do wysłania", queued_count)
            plan_cols[1].metric("Planowane shardy", shard_count)
            plan_cols[2].metric("Aktywne jobs", active_jobs)
            plan_cols[3].metric(
                "Szac. input",
                f"{estimate['estimated_input_tokens'] / 1_000_000:.1f}M tok." if estimate['estimated_input_tokens'] else "0",
            )
            st.caption(
                "Batch: ok. 1000 SKU/shard, do 12 równoległych uploadów, soft cap 80 aktywnych jobs, "
                "20 równoległych status-checków i 8 downloadów. Gotowe shardy są odbierane niezależnie. "
                "Gemini 3.5 Flash-Lite ma domyślny minimal thinking; nie dokładamy zbędnych parametrów JSONL."
            )

            col_submit, col_refresh, col_repair = st.columns(3)
            if col_submit.button("2. Wyślij wszystkie możliwe shardy", type="primary"):
                with st.spinner("Buduję JSONL i równolegle wysyłam shardy..."):
                    submitted = submit_queued_batches(int(products_per_batch), run_id=selected_run)
                if submitted:
                    sent_products = sum(item.get("products", 0) for item in submitted if item.get("job_name"))
                    sent_jobs = sum(1 for item in submitted if item.get("job_name"))
                    st.success(f"Wysłano {sent_jobs} jobs dla {sent_products:,} produktów.".replace(",", " "))
                    st.dataframe(pd.DataFrame(submitted), width="stretch")
                else:
                    st.info("Ta partia nie ma produktów ze statusem queued.")

            if col_refresh.button("3. Odbierz wszystkie gotowe shardy"):
                with st.spinner("Równolegle sprawdzam statusy i pobieram tylko gotowe outputy..."):
                    updates = refresh_and_ingest_batch_jobs(run_id=selected_run)
                if updates:
                    ingested_now = sum(1 for row in updates if row.get("ingested"))
                    st.success(f"Odebrano teraz {ingested_now} gotowych shardów.")
                    st.dataframe(pd.DataFrame(updates), width="stretch")
                else:
                    st.info("Brak nieodebranych zadań batch w tej partii.")

            repair_count = int(counts.get("validation_failed", 0))
            if col_repair.button(f"4. Przygotuj repair batch ({repair_count})", disabled=repair_count == 0):
                moved = requeue_jobs(
                    ["validation_failed"],
                    "Repair batch po walidacji",
                    run_id=selected_run,
                )
                st.success(f"Do repair queue przeniesiono {moved} produktów. Kliknij ponownie wysyłkę shardów.")

            batch_rows = list_batch_jobs(run_id=selected_run)
            if batch_rows:
                st.subheader("Zadania Batch API aktywnej partii")
                batch_df = pd.DataFrame(batch_rows)
                st.dataframe(
                    batch_df[["display_name", "state", "product_count", "created_at", "ingested_at", "error_message"]],
                    width="stretch",
                    hide_index=True,
                )
        else:
            st.info("Najpierw utwórz partię z listy SKU albo z katalogu.")
    except Exception as exc:
        st.error(str(exc))

with results_tab:
    st.subheader("Kontrola jakości i eksport")
    result_runs = list_meta_runs()
    result_run_ids = [row["run_id"] for row in result_runs]
    result_run = st.selectbox(
        "Partia do kontroli",
        options=[""] + result_run_ids,
        format_func=lambda value: "Wszystkie partie" if not value else value,
        index=(result_run_ids.index(st.session_state.active_meta_run_id) + 1)
        if st.session_state.get("active_meta_run_id") in result_run_ids else 0,
        key="results_run_filter",
    )
    counts = meta_status_counts(result_run or None)
    count_columns = st.columns(max(len(counts), 1))
    for column, (status_name, status_count) in zip(count_columns, sorted(counts.items())):
        column.metric(status_name, f"{int(status_count):,}".replace(",", " "))

    st.caption("Podobne początki i duplikaty meta description nie blokują eksportu. Poziomy meta title są sprawdzane przede wszystkim względem nazwy produktu, nie linków i opisów innych wariantów.")
    col_revalidate, col_audit, col_requeue = st.columns(3)
    if col_revalidate.button("Przelicz walidację bez AI"):
        audit = revalidate_meta_jobs(run_id=result_run or None)
        st.success(
            f"Sprawdzono {audit['checked']} produktów. Bez regenerowania zaakceptowano {audit['promoted']}; "
            f"nadal wymagają poprawy {audit['still_failed']}."
        )

    if col_audit.button("Audytuj duplikaty i błędy krytyczne"):
        audit = audit_meta_jobs_for_repetition(run_id=result_run or None)
        st.success(f"Sprawdzono {audit['checked']} produktów. Do poprawy oznaczono {audit['flagged']} (tylko meta title); identyczne meta description: {audit.get('duplicate_descriptions', 0)} — tylko informacyjnie.")

    if col_requeue.button("Przenieś błędne do kolejki"):
        count = requeue_jobs(
            ["validation_failed", "failed"],
            "Ponowienie po kontroli jakości",
            run_id=result_run or None,
        )
        st.success(f"Do kolejki wróciło {count} produktów.")

    completed_count = counts.get("completed", 0)
    export_scope = result_run or "all"
    if completed_count:
        lazy_download_button(
            f"CSV Magento - {completed_count} poprawnych produktów",
            f"magento_{export_scope}",
            lambda: export_meta_csv(["completed"], run_id=result_run or None),
            "magento_metatagi.tsv",
            "text/tab-separated-values",
        )

        # Lista wysyłki czyta całą partię z bazy, więc ładujemy ją dopiero na żądanie.
        if st.toggle("Wyślij poprawne metatagi bezpośrednio do Akeneo", key=f"show_delivery_{export_scope}"):
            delivery_all = list_meta_delivery_jobs(run_id=result_run or None, include_sent=True)
            delivered_count = sum(1 for job in delivery_all if job.get("akeneo_status") == "sent")
            failed_delivery_count = sum(1 for job in delivery_all if job.get("akeneo_status") == "failed")
            delivery_metrics = st.columns(3)
            delivery_metrics[0].metric("Gotowe w Akeneo", delivered_count)
            delivery_metrics[1].metric("Do wysłania", len(delivery_all) - delivered_count)
            delivery_metrics[2].metric("Ostatnio błędne", failed_delivery_count)

            resend_meta = st.checkbox(
                "Wyślij ponownie także metatagi już oznaczone jako wysłane",
                value=False,
                key=f"resend_meta_{result_run or 'all'}",
            )
            delivery_jobs = [
                job for job in delivery_all
                if resend_meta or job.get("akeneo_status") != "sent"
            ]
            st.caption(
                "PATCH obejmuje tylko meta_title i meta_description. Kanał i locale są brane z każdego zadania; "
                "inne dane produktu pozostają bez zmian."
            )
            confirm_delivery = st.checkbox(
                f"Potwierdzam aktualizację {len(delivery_jobs)} produktów w Akeneo",
                value=False,
                key=f"confirm_meta_delivery_{result_run or 'all'}",
            )
            if st.button(
                f"Wyślij do Akeneo ({len(delivery_jobs)})",
                type="primary",
                disabled=not delivery_jobs or not confirm_delivery,
                key=f"send_batch_meta_{result_run or 'all'}",
            ):
                delivery_progress = st.progress(0, "Przygotowuję wysyłkę do Akeneo...")

                def batch_meta_progress(done: int, total: int) -> None:
                    delivery_progress.progress(done / max(total, 1), f"Wysłano {done}/{total}")

                try:
                    delivery_stats = send_metatags_to_akeneo(
                        delivery_jobs,
                        channel,
                        locale,
                        progress_callback=batch_meta_progress,
                    )
                    st.success(
                        f"Wysłano metatagi dla {delivery_stats['sent']} produktów"
                        f" (bez zmian w Akeneo: {delivery_stats.get('unchanged', 0)})."
                    )
                    if delivery_stats["errors"]:
                        shown_errors = list(delivery_stats["errors"])[:50]
                        st.error("\n".join(shown_errors))
                        if len(delivery_stats["errors"]) > len(shown_errors):
                            st.caption(
                                f"Pokazano 50 z {len(delivery_stats['errors'])} błędów. "
                                "Pełny status jest zapisany przy zadaniach."
                            )
                except Exception as exc:
                    st.error(f"Nie udało się rozpocząć wysyłki do Akeneo: {exc}")
    lazy_download_button(
        "Raport jakości CSV",
        f"quality_{export_scope}",
        lambda: export_quality_report_csv(run_id=result_run or None),
        "raport_jakosci_metatagow.csv",
        "text/csv",
    )
    lazy_download_button(
        "Metatagi z Akeneo sprzed nadpisania (do przywrócenia)",
        f"previous_{export_scope}",
        lambda: export_previous_metatags_csv(run_id=result_run or None),
        "akeneo_metatagi_przed_zmiana.tsv",
        "text/tab-separated-values",
    )

    status_filter = st.multiselect(
        "Statusy do podglądu",
        ["completed", "validation_failed", "failed", "queued", "batch_submitted"],
        default=["validation_failed", "failed"],
    )
    preview_jobs = list_meta_jobs(
        statuses=status_filter or None,
        limit=500,
        run_id=result_run or None,
    )
    if preview_jobs:
        preview_df = pd.DataFrame(preview_jobs)
        visible_columns = [
            "sku",
            "status",
            "meta_title",
            "meta_description",
            "opening_signature",
            "attempts",
            "validation_errors",
            "error_message",
            "akeneo_status",
            "akeneo_sent_at",
            "akeneo_error",
        ]
        st.dataframe(preview_df[visible_columns], width="stretch", hide_index=True)

# Kolejka opisów jest zapisywana od razu po zmianie, nie dopiero przy generowaniu.
if not st.session_state.get("meta_only"):
    current_queue = list(st.session_state.bulk_selected_products)
    if current_queue != st.session_state.get("saved_queue_skus"):
        save_description_workspace(current_queue, st.session_state.bulk_results)
        st.session_state.saved_queue_skus = current_queue

st.markdown("---")

st.caption(
    f"{APP_NAME} v{APP_VERSION} · prompt {PROMPT_VERSION} · model {GEMINI_MODEL}"
)
