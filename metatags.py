"""Generowanie metatagów przez Gemini i wysyłka do Akeneo."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence, Tuple

from google.genai import types

from akeneo import (
    AKENEO_REQUEST_SEMAPHORE,
    _akeneo_root,
    _prepare_product_data,
    _value_from_values,
    akeneo_get_attribute,
    akeneo_get_product_details,
    akeneo_get_token,
    akeneo_headers,
    akeneo_request,
)
from akeneo_payloads import (
    build_metatag_product_update,
    parse_collection_response,
    serialize_collection_updates,
)
from config import (
    AKENEO_MAX_WORKERS,
    AKENEO_META_UPDATE_CHUNK_SIZE,
    AKENEO_SKU_FILTER_CHUNK_SIZE,
    GEMINI_META_MAX_OUTPUT_TOKENS,
    GEMINI_MODEL,
    MAX_META_RETRIES,
    META_DESCRIPTION_TARGET_MAX,
    META_DESCRIPTION_TARGET_MIN,
    META_TITLE_HARD_MAX,
)
from http_clients import call_gemini_with_retry
from job_store import (
    db_connect,
    existing_meta_title_owners,
    get_meta_job,
    list_meta_jobs,
    save_akeneo_meta_deliveries,
    save_meta_result,
    save_meta_results_bulk,
    upsert_meta_job,
)
from meta_titles import (
    build_meta_title,
    compact_description_context,
    description_context_limit,
    extract_meta_title_candidates,
    extract_title_signals,
    meta_title_validation_errors,
    meta_validation_errors,
    normalize_generated_meta_title,
    parse_validation_error_list,
    select_best_meta_title_candidate,
    source_product_type,
    split_field_errors,
    title_required_features,
    title_signal_summary,
)
from text_utils import (
    build_style_plan,
    chunks,
    clean_author_for_prompt,
    clean_source_title_for_prompt,
    generate_product_url,
    normalize_for_compare,
    normalize_spaces,
    strip_code_fences,
    strip_html,
    utcnow_iso,
    validate_description_quality,
)


def _validated_metatag_texts(meta_title: str, meta_description: str) -> Tuple[str, str]:
    title = normalize_generated_meta_title(meta_title)
    description = normalize_spaces(strip_html(meta_description)).strip('"„”')
    if not title or not description:
        raise ValueError("meta title i meta description nie mogą być puste")
    if len(title) > META_TITLE_HARD_MAX:
        raise ValueError(f"meta title przekracza {META_TITLE_HARD_MAX} znaków")
    if re.search(r"https?://|www\.", title, flags=re.IGNORECASE):
        raise ValueError("meta title nie może zawierać URL")
    if (
        "..." in title
        or title.endswith("…")
        or title.endswith(("-", "–", "—", "|", ":", ",", ";", "/", "+"))
    ):
        raise ValueError("meta title wygląda na ucięty")
    description_errors = meta_validation_errors(description)
    if description_errors:
        raise ValueError("; ".join(description_errors))
    return title, description


def akeneo_current_metatags(
    token: str, identifiers: Sequence[str], channel: str, locale: str
) -> Dict[str, Dict[str, str]]:
    """Zwraca istniejące SKU z ich obecnymi metatagami (PATCH w Akeneo to upsert, więc
    brak SKU oznacza, że nie wolno go wysyłać)."""
    wanted = list(dict.fromkeys(str(identifier).strip() for identifier in identifiers if str(identifier).strip()))
    if not wanted:
        return set()
    if len(wanted) > AKENEO_SKU_FILTER_CHUNK_SIZE:
        raise ValueError(f"Jednorazowo można sprawdzić maksymalnie {AKENEO_SKU_FILTER_CHUNK_SIZE} SKU")
    search = {"identifier": [{"operator": "IN", "value": wanted}]}
    with AKENEO_REQUEST_SEMAPHORE:
        response = akeneo_request(
            "GET",
            _akeneo_root() + "/api/rest/v1/products",
            headers=akeneo_headers(token),
            params={
                "limit": 100,
                "with_count": "false",
                "attributes": "meta_title,meta_description",
                "search": json.dumps(search, ensure_ascii=False),
            },
        )
    response.raise_for_status()
    current: Dict[str, Dict[str, str]] = {}
    for item in response.json().get("_embedded", {}).get("items", []):
        identifier = str(item.get("identifier", "")).strip()
        if not identifier:
            continue
        values = item.get("values") or {}
        current[identifier] = {
            field: _value_from_values(values, [field], channel, locale)
            for field in ("meta_title", "meta_description")
        }
    return current


def akeneo_update_metatag_chunk(
    items: Sequence[Dict],
    *,
    token: str,
    attribute_definitions: Dict[str, Dict],
    default_channel: str,
    default_locale: str,
) -> List[Tuple[Dict, str]]:
    """Wysyła do 100 istniejących produktów i zwraca błąd osobno dla każdej pozycji."""
    prepared: List[Tuple[Dict, Dict]] = []
    outcomes: List[Tuple[Dict, str]] = []
    for item in items:
        try:
            title, description = _validated_metatag_texts(
                item.get("meta_title", ""),
                item.get("meta_description", ""),
            )
            update = build_metatag_product_update(
                item.get("sku", ""),
                title,
                description,
                title_attribute=attribute_definitions["meta_title"],
                description_attribute=attribute_definitions["meta_description"],
                channel=item.get("channel") or default_channel,
                locale=item.get("locale") or default_locale,
            )
            prepared.append((item, update))
        except Exception as exc:
            outcomes.append((item, str(exc)))

    if not prepared:
        return outcomes

    body = serialize_collection_updates(update for _, update in prepared)
    with AKENEO_REQUEST_SEMAPHORE:
        response = akeneo_request(
            "PATCH",
            _akeneo_root() + "/api/rest/v1/products",
            headers=akeneo_headers(token, "application/vnd.akeneo.collection+json"),
            data=body.encode("utf-8"),
        )
    if response.status_code != 200:
        message = f"Błąd Akeneo {response.status_code}: {response.text[:300]}"
        return [*outcomes, *((item, message) for item, _ in prepared)]

    try:
        by_line = {
            int(result.get("line", 0)): result
            for result in parse_collection_response(response.text)
            if result.get("line")
        }
    except Exception as exc:
        message = f"Niepoprawna odpowiedź Akeneo: {exc}"
        return [*outcomes, *((item, message) for item, _ in prepared)]

    for line_number, (item, _) in enumerate(prepared, start=1):
        result = by_line.get(line_number)
        if not result:
            outcomes.append((item, "Akeneo nie zwróciło statusu dla produktu"))
            continue
        status_code = int(result.get("status_code", 0) or 0)
        if 200 <= status_code < 300:
            outcomes.append((item, ""))
        else:
            outcomes.append((item, str(result.get("message") or f"status {status_code}")))
    return outcomes


def send_metatags_to_akeneo(
    items: Sequence[Dict],
    channel: str,
    locale: str,
    *,
    progress_callback=None,
) -> Dict[str, object]:
    """Wysyła poprawne metatagi równolegle, z jednym tokenem i jednym odczytem schematu."""
    candidates = [
        item for item in items
        if item.get("sku")
        and item.get("meta_title")
        and item.get("meta_description")
        and not item.get("error")
        and not parse_validation_error_list(item.get("validation_errors"))
    ]
    stats: Dict[str, object] = {
        "requested": len(candidates),
        "skipped": len(items) - len(candidates),
        "sent": 0,
        "errors": [],
    }
    if not candidates:
        return stats

    token = akeneo_get_token()
    attributes = {
        "meta_title": akeneo_get_attribute("meta_title", token),
        "meta_description": akeneo_get_attribute("meta_description", token),
    }

    identifier_chunks = list(chunks([item["sku"] for item in candidates], AKENEO_SKU_FILTER_CHUNK_SIZE))
    existing: Dict[str, Dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
        futures = [
            executor.submit(akeneo_current_metatags, token, sku_chunk, channel, locale)
            for sku_chunk in identifier_chunks
        ]
        for future in as_completed(futures):
            existing.update(future.result())

    existing_items: List[Dict] = []
    skipped_deliveries: List[Tuple[str, str, str]] = []
    completed = 0
    stats["unchanged"] = 0
    for item in candidates:
        current = existing.get(item["sku"])
        if current is None:
            error = "produkt nie istnieje w Akeneo - pominięto, aby nie utworzyć pustego produktu"
            stats["errors"].append(f"{item['sku']}: {error}")
            skipped_deliveries.append((item.get("job_key", ""), "failed", error))
            completed += 1
        elif (current["meta_title"].strip() == item["meta_title"].strip()
              and current["meta_description"].strip() == item["meta_description"].strip()):
            # Te same wartości już są w Akeneo – nie ma czego nadpisywać.
            stats["unchanged"] = int(stats["unchanged"]) + 1
            skipped_deliveries.append((item.get("job_key", ""), "sent", ""))
            completed += 1
        else:
            existing_items.append(item)
    save_akeneo_meta_deliveries(skipped_deliveries)
    previous = {item.get("job_key", ""): existing[item["sku"]] for item in existing_items}

    item_chunks = list(chunks(existing_items, AKENEO_META_UPDATE_CHUNK_SIZE))
    with ThreadPoolExecutor(max_workers=AKENEO_MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                akeneo_update_metatag_chunk,
                item_chunk,
                token=token,
                attribute_definitions=attributes,
                default_channel=channel,
                default_locale=locale,
            )
            for item_chunk in item_chunks
        ]
        for future, item_chunk in zip(futures, item_chunks):
            try:
                outcomes = future.result()
            except Exception as exc:
                # Błąd jednej paczki nie może zgubić statusów paczek już wysłanych.
                outcomes = [(item, f"Błąd wysyłki paczki: {exc}") for item in item_chunk]
            delivery_updates: List[Tuple[str, str, str]] = []
            for item, error in outcomes:
                completed += 1
                if error:
                    stats["errors"].append(f"{item['sku']}: {error}")
                    delivery_updates.append((item.get("job_key", ""), "failed", error))
                else:
                    stats["sent"] = int(stats["sent"]) + 1
                    delivery_updates.append((item.get("job_key", ""), "sent", ""))
            save_akeneo_meta_deliveries(delivery_updates, previous)
            if progress_callback:
                progress_callback(completed, len(candidates))
    if progress_callback and completed:
        progress_callback(completed, len(candidates))
    return stats


def process_product_meta_only(
    sku: str,
    token: str,
    channel: str,
    locale: str,
    store_view_code: str,
) -> Dict:
    try:
        product_details = akeneo_get_product_details(sku, token, channel, locale)
        if not product_details:
            return {"sku": sku, "title": "", "error": "Produkt nie znaleziony", "meta_only": True}

        product_data = _prepare_product_data(product_details)
        quality = validate_description_quality(product_data["description"])
        job_key, _ = upsert_meta_job(
            sku=sku,
            channel=channel,
            locale=locale,
            store_view_code=store_view_code,
            product_data=product_data,
            source_updated=product_details.get("updated", ""),
            force_regenerate=True,
        )
        job = get_meta_job(job_key)
        if not job:
            raise RuntimeError("Nie udało się utworzyć zadania metatagów")
        generated = generate_metatags_interactive(job)
        status = "completed" if not generated["validation_errors"] else "validation_failed"
        save_meta_result(
            job_key,
            meta_title=generated["meta_title"],
            meta_description=generated["meta_description"],
            status=status,
            attempts=generated["attempts"],
            validation_errors=generated["validation_errors"],
            error_message=generated["error"],
        )
        return {
            "job_key": job_key,
            "sku": sku,
            "title": product_data["title"],
            "description_html": "",
            "url": generate_product_url(product_data["title"]),
            "old_description": product_data["description"],
            "research": None,
            "meta_title": generated["meta_title"],
            "meta_description": generated["meta_description"],
            "error": generated["error"] or None,
            "description_quality": quality,
            "meta_only": True,
            "validation_errors": generated["validation_errors"],
        }
    except Exception as exc:
        return {
            "sku": sku,
            "title": "",
            "error": str(exc),
            "description_quality": ("error", str(exc)),
            "meta_only": True,
        }


META_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "meta_title_candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Trzy różne kompletne meta title, każdy maksymalnie 75 znaków.",
        },
        "meta_description": {
            "type": "string",
            "description": "Polski meta description produktu, zwykle 135-160 znaków, konkretny i zgodny ze źródłem.",
        },
    },
    "required": ["meta_title_candidates", "meta_description"],
}
META_SYSTEM_PROMPT = """Jesteś seniorem SEO e-commerce i redaktorem informacji produktowej Bookland.
Tworzysz metatagi dla jednego SKU na podstawie danych z Akeneo. Najpierw rozpoznaj tożsamość i wariant produktu, potem redaguj.

META TITLE
Zwróć 3 różne, pełnowartościowe kandydatury. Jedyny twardy limit to 75 znaków ze spacjami.
Nie twórz osobnego wariantu skróconego tylko po to, aby zmieścić się w określonej długości.
Strategie:
1) kandydat pełny: zwykle 65-75 znaków; maksymalnie zachowaj wartościowe cechy produktu,
2) kandydat zbalansowany: zwykle 58-72 znaki; zachowaj kompletność przy naturalnej składni,
3) kandydat alternatywny: zwykle 55-75 znaków; użyj innej naturalnej redakcji, ale nie usuwaj istotnych cech SKU.
Priorytet informacji:
1) oficjalna seria/tytuł/model,
2) poziom/egzamin/tom/część/klasa,
3) komponent i format odróżniający SKU,
4) odbiorca i typ kodu/dostępu,
5) autor tylko dla zwykłych książek, jeśli nie wypiera ważniejszych danych.
Nie skracaj semantycznie ważnego elementu tylko po to, aby uzyskać krótszy title.
Jeżeli pełniejsza wersja do 75 znaków lepiej identyfikuje SKU, preferuj ją nad krótszą, uboższą wersją.
Google może skrócić prezentację w SERP - to akceptowalne; Ty masz dostarczyć kompletny i naturalny title.
Nie ucinaj nazwy mechanicznie. Nie kończ słowem: Kod, Access, Online, Student, Teacher ani przyimkiem.
Dla materiałów językowych zachowuj nazwy własne: MyEnglishLab, Online Practice, Teacher's Portal, eBook, eText, B2 First, C1 Advanced.
Typ dostępu zapisuj naturalnie po polsku: kod uczniowski, kod nauczycielski, kod dostępu, bez kodu / bez klucza.
Jeżeli NAZWA produktu zawiera „no key”, „without key”, „bez kodu” lub „bez klucza”, KAŻDA kandydatura musi zachować tę informację. To cecha wariantu SKU, nie opcjonalny detal.
Jeżeli walidator w retry wskazuje konkretną utraconą cechę, wszystkie nowe kandydatury muszą ją naprawić; nie powtarzaj poprzedniej wersji z tym samym błędem.
Nie twórz zlepków typu: Online Kod, Portal Kod, eBook Kod, Kod Ucznia.
Dla zwykłych książek preferuj „Tytuł – Autor”; przy długim tytule ważniejszy jest pełny sens tytułu niż autor. Nie używaj „praca zbiorowa”.

META DESCRIPTION
Pisz po polsku, konkretnie i informacyjnie. Celuj w 135-160 znaków, ale naturalność i zgodność z produktem są ważniejsze niż dobicie do konkretnej liczby.
Nie dodawaj faktów, czasu licencji, funkcji, poziomu ani liczb, których nie ma w danych.
Bez bezpośrednich CTA sklepu typu „Sprawdź ofertę”, „Kup teraz”, „Zamów teraz”.
Dla materiałów edukacyjnych możesz zaczynać od serii, typu materiału, odbiorcy lub funkcji. Powtarzalne konstrukcje są akceptowalne, jeśli są naturalne i trafne.
Dla książek możesz zaczynać od bohatera, konfliktu, tematu lub miejsca, jeśli wynikają ze źródła.

Korzystaj wyłącznie z przekazanych danych. Zwróć tylko JSON zgodny ze schematem."""


def revalidate_meta_jobs(run_id: Optional[str] = None) -> Dict[str, int]:
    """Przelicza stare validation_failed nowymi, łagodniejszymi regułami bez kosztu AI."""
    jobs = list_meta_jobs(
        statuses=["validation_failed"],
        order_by="created_at ASC",
        run_id=run_id,
    )
    title_owners = existing_meta_title_owners()
    records: List[Dict] = []
    stats = {"checked": 0, "promoted": 0, "still_failed": 0}

    for job in jobs:
        stats["checked"] += 1
        title_errors = meta_title_validation_errors(
            job.get("meta_title", ""), job, existing_title_owners=title_owners
        )
        description_errors = meta_validation_errors(job.get("meta_description", ""), job=job)
        errors = [*title_errors, *description_errors]
        status = "completed" if not errors else "validation_failed"
        if status == "completed":
            stats["promoted"] += 1
            title_owners[normalize_for_compare(job.get("meta_title", ""))] = str(job.get("sku", ""))
        else:
            stats["still_failed"] += 1
        records.append({
            "job_key": job["job_key"],
            "meta_title": job.get("meta_title", ""),
            "meta_description": job.get("meta_description", ""),
            "status": status,
            "attempts": int(job.get("attempts", 0)),
            "validation_errors": errors,
            "error_message": "; ".join(errors),
        })

    save_meta_results_bulk(records)
    return stats


def audit_meta_jobs_for_repetition(run_id: Optional[str] = None) -> Dict[str, int]:
    """Audyt jakości bez karania meta description za powtarzalność.

    Do poprawy kierowane są wyłącznie krytyczne błędy/duplikaty meta title.
    Duplikaty meta description są tylko liczone diagnostycznie.
    """
    jobs = list_meta_jobs(statuses=["completed"], order_by="created_at ASC", run_id=run_id)
    seen_hash: Dict[str, str] = {}
    seen_titles: Dict[str, str] = {}
    flagged: Dict[str, List[str]] = {}
    duplicate_descriptions = 0

    for job in jobs:
        reasons: List[str] = []
        title_key = normalize_for_compare(job.get("meta_title", ""))
        title_errors = meta_title_validation_errors(job.get("meta_title", ""), job)
        reasons.extend(title_errors)

        if title_key and title_key in seen_titles and seen_titles[title_key] != job["sku"]:
            reasons.append(f"Identyczny meta title jak SKU {seen_titles[title_key]}")
        elif title_key:
            seen_titles[title_key] = job["sku"]

        # Meta description nie jest kluczowym elementem różnicującym URL-e w SEO.
        # Identyczne opisy liczymy, ale nie marnujemy na nie retry i nie blokujemy eksportu.
        norm_hash = job.get("normalized_hash", "")
        if norm_hash and norm_hash in seen_hash and seen_hash[norm_hash] != job["sku"]:
            duplicate_descriptions += 1
        elif norm_hash:
            seen_hash[norm_hash] = job["sku"]

        if reasons:
            flagged[job["job_key"]] = list(dict.fromkeys(reasons))

    if flagged:
        now = utcnow_iso()
        with db_connect() as conn:
            conn.executemany(
                """
                UPDATE meta_jobs SET status='validation_failed', validation_errors=?, updated_at=?
                WHERE job_key=?
                """,
                [
                    (json.dumps(reasons, ensure_ascii=False), now, job_key)
                    for job_key, reasons in flagged.items()
                ],
            )
    return {
        "checked": len(jobs),
        "flagged": len(flagged),
        "duplicate_descriptions": duplicate_descriptions,
    }


def build_meta_prompt(
    job: Dict,
    attempt: int = 0,
    previous_meta_title: str = "",
    previous_meta_description: str = "",
    previous_errors: Sequence[str] = (),
) -> str:
    """Krótszy prompt per SKU, zachowujący pełną logikę SEO v4.4.1."""
    style = build_style_plan(
        job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
    )
    raw_title = normalize_spaces(strip_html(job.get("title", "")))
    cleaned_title = clean_source_title_for_prompt(raw_title)
    context_limit = description_context_limit(job)
    description = compact_description_context(job.get("description", ""), raw_title, context_limit)
    author, author_note = clean_author_for_prompt(job.get("author", ""), description)
    signals = extract_title_signals(raw_title, description)
    cues = ", ".join(style["semantic_cues"][:5]) or "brak"

    prior_errors = list(previous_errors) or parse_validation_error_list(job.get("validation_errors", ""))
    title_errors, description_errors = split_field_errors(prior_errors)
    prior_title = previous_meta_title or (job.get("meta_title", "") if attempt > 0 else "")
    prior_description = previous_meta_description or (job.get("meta_description", "") if attempt > 0 else "")
    locked_title = prior_title if prior_title and not title_errors and attempt > 0 else ""
    locked_description = prior_description if prior_description and not description_errors and attempt > 0 else ""

    retry_block = ""
    if attempt > 0 or prior_errors:
        retry_block = (
            "\nPOPRZEDNIA PRÓBA\n"
            f"Meta title: {prior_title or 'brak'}\n"
            f"Meta description: {prior_description or 'brak'}\n"
            "Napraw wyłącznie:\n"
            + ("\n".join(f"- {error}" for error in prior_errors) or "- brak")
        )

    lock_block = ""
    if locked_title:
        lock_block += f"\nPOPRAWNY META TITLE — NIE ZMIENIAJ: {locked_title}"
    if locked_description:
        lock_block += f"\nPOPRAWNY META DESCRIPTION — NIE ZMIENIAJ: {locked_description}"

    return f"""Przygotuj metatagi dla SKU {job['sku']}.

DANE
Nazwa: {raw_title}
Nazwa oczyszczona: {cleaned_title}
Autor/marka: {author or 'brak'}
Uwaga o autorze: {author_note}
Dodatkowe dane: {job.get('details', '') or 'brak'}
Opis źródłowy: {description or 'brak opisu'}

SYGNAŁY
{title_signal_summary(signals)}
Typ: {source_product_type(job)}
Kontekst semantyczny: {cues}
Twarde cechy wymagane przez walidator: {title_required_features(job)}

META TITLE
- 3 różne pełnowartościowe kandydatury do 75 znaków, chyba że pole jest zablokowane.
- Pełny: zwykle 65-75; zbalansowany: 58-72; alternatywny: 55-75.
- Nie twórz specjalnie krótkiej wersji. Jeżeli dłuższy title do 75 znaków zachowuje ważną cechę SKU, preferuj go.
- Zachowaj twarde cechy z NAZWY: serię, poziom/egzamin, format, komponent, odbiorcę i rodzaj dostępu, jeśli występują.
- Poziom z nazwy jest wiążący; poziomy tylko z opisu nie są obowiązkowe.

META DESCRIPTION
- Celuj w {META_DESCRIPTION_TARGET_MIN}-{META_DESCRIPTION_TARGET_MAX} znaków; 120-170 jest technicznie akceptowalne.
- Podobne lub identycznie zaczynające się opisy innych SKU są dozwolone.
- Bez halucynowanych liczb/funkcji i bez bezpośredniego CTA sklepu.
{retry_block}
{lock_block}

Zwróć wyłącznie JSON zgodny ze schematem."""


def generate_metatags_interactive(job: Dict) -> Dict:
    title_owners = existing_meta_title_owners(job["job_key"])
    last_title = ""
    last_description = ""
    last_title_errors: List[str] = []
    last_description_errors: List[str] = []

    for attempt in range(MAX_META_RETRIES + 1):
        style = build_style_plan(
            job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
        )
        locked_title = last_title if last_title and not last_title_errors else ""
        locked_description = last_description if last_description and not last_description_errors else ""
        try:
            response = call_gemini_with_retry(
                model=GEMINI_MODEL,
                contents=build_meta_prompt(
                    job,
                    attempt,
                    previous_meta_title=last_title,
                    previous_meta_description=last_description,
                    previous_errors=[*last_title_errors, *last_description_errors],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=META_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=META_RESPONSE_SCHEMA,
                    seed=style["seed"],
                    temperature=0.68,
                    top_p=0.90,
                    max_output_tokens=GEMINI_META_MAX_OUTPUT_TOKENS,
                ),
            )
            data = json.loads(strip_code_fences(response.text or ""))
            if locked_title:
                selected_title = locked_title
                title_errors: List[str] = []
            else:
                selected_title, title_errors, _ = select_best_meta_title_candidate(
                    extract_meta_title_candidates(data), job, existing_title_owners=title_owners
                )

            generated_description = normalize_spaces(
                strip_html(str(data.get("meta_description", "")))
            ).strip('"„”')
            selected_description = locked_description or generated_description
            description_errors = [] if locked_description else meta_validation_errors(
                selected_description, job=job
            )

            last_title = selected_title
            last_description = selected_description
            last_title_errors = title_errors
            last_description_errors = description_errors
            if not title_errors and not description_errors:
                return {
                    "meta_title": selected_title,
                    "meta_description": selected_description,
                    "attempts": attempt + 1,
                    "validation_errors": [],
                    "error": "",
                }
        except Exception as exc:
            api_error = f"Błąd Gemini lub JSON: {exc}"
            message = str(exc).lower()
            if not last_title:
                last_title_errors = [api_error]
            if not last_description:
                last_description_errors = [api_error]

            # Błędy trwałe nie mają sensu w retry. Szczególnie 403 potrafił wcześniej
            # wykonać kilka identycznych prób i wydłużyć pozornie zawieszony przebieg.
            fatal_api_error = (
                "permission_denied" in message
                or "project has been denied access" in message
                or "api_key_invalid" in message
                or "invalid api key" in message
            )
            if fatal_api_error:
                break

            # Timeout jest ograniczony przez GEMINI_HTTP_TIMEOUT_MS. Pozwalamy na
            # jedną kolejną próbę, ale nie zużywamy wszystkich retry jakościowych
            # na powtarzające się problemy transportowe.
            timeout_error = "timeout" in message or "timed out" in message or "deadline" in message
            if timeout_error and attempt >= 1:
                break

    errors = [*last_title_errors, *last_description_errors]
    return {
        "meta_title": last_title or normalize_generated_meta_title(
            build_meta_title(job.get("title", ""), job.get("author", ""))
        ),
        "meta_description": last_description,
        "attempts": MAX_META_RETRIES + 1,
        "validation_errors": errors,
        "error": "; ".join(errors),
    }


def batch_request_for_job(job: Dict, attempt: int = 0) -> Dict:
    style = build_style_plan(
        job["sku"], job.get("title", ""), job.get("author", ""), job.get("description", ""), attempt
    )
    return {
        "key": job["job_key"],
        "request": {
            "contents": [{"role": "user", "parts": [{"text": build_meta_prompt(job, attempt)}]}],
            "system_instruction": {"parts": [{"text": META_SYSTEM_PROMPT}]},
            "generation_config": {
                # Gemini 3.5 Flash-Lite ma `minimal` jako domyślny thinking.
                # Nie dodajemy opcjonalnych pól konfiguracyjnych do JSONL, aby
                # zachować maksymalną zgodność Batch API. Sampling params są w
                # Gemini 3.5 deprecated, więc nie wysyłamy temperature/top_p.
                "seed": style["seed"],
                "max_output_tokens": GEMINI_META_MAX_OUTPUT_TOKENS,
                "response_mime_type": "application/json",
                "response_schema": META_RESPONSE_SCHEMA,
            },
        },
    }
