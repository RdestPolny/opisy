"""Walidacja i ranking meta title oraz reguły meta description (bez AI)."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from config import (
    META_DESCRIPTION_HARD_MAX,
    META_DESCRIPTION_HARD_MIN,
    META_TITLE_HARD_MAX,
    META_TITLE_TARGET_MAX,
    META_TITLE_TARGET_MIN,
    TITLE_COMPONENT_TRANSLATIONS,
    TITLE_DANGLING_WORDS,
    TITLE_GENERIC_TOKENS,
    TITLE_INTERNAL_NOISE_PATTERNS,
)
from text_utils import (
    clean_source_title_for_prompt,
    normalize_for_compare,
    normalize_spaces,
    smart_truncate,
    strip_html,
)


def build_meta_title(title: str, author: str, max_chars: int = META_TITLE_HARD_MAX) -> str:
    """Awaryjny meta title używany wyłącznie, gdy Gemini nie zwróci wyniku.

    Główny workflow od v4.2.0 generuje meta title przez AI. Fallback nie powinien
    być traktowany jako wynik docelowej optymalizacji.
    """
    title = normalize_spaces(strip_html(title)).strip('"„”')
    author = normalize_spaces(strip_html(author)).strip('"„”')
    if not title:
        return ""

    candidates: List[str] = []
    if author and normalize_for_compare(author) not in {"pracazbiorowa", "praca zbiorowa"}:
        candidates.append(f"{title} - {author}")
    candidates.append(title)

    main_title = re.split(r"\s[:|]\s|:\s|\s[-–—]\s", title, maxsplit=1)[0].strip()
    if main_title and main_title != title:
        candidates.append(main_title)

    for candidate in candidates:
        candidate = normalize_spaces(candidate)
        if len(candidate) <= max_chars:
            return candidate
    return smart_truncate(main_title or title, max_chars)


def title_signal_summary(signals: Dict[str, List[str]]) -> str:
    labels = [
        ("Tożsamość / seria", signals.get("identity_tokens", [])),
        ("Poziom / egzamin", signals.get("levels", [])),
        ("Komponent", signals.get("components", [])),
        ("Format", signals.get("formats", [])),
        ("Platforma", signals.get("platforms", [])),
        ("Odbiorca", signals.get("audience", [])),
        ("Dostęp", signals.get("access", [])),
    ]
    rows = [f"- {label}: {', '.join(values)}" for label, values in labels if values]
    return "\n".join(rows) if rows else "- Brak automatycznie wykrytych sygnałów; oprzyj się na nazwie i opisie."


V43_GENERIC_POLISH_TITLE_WORDS = (
    "Kod", "Uczeń", "Ucznia", "Nauczyciel", "Nauczyciela", "Nauczycielski",
    "Ćwiczenia", "Podręcznik", "Zeszyt", "Dostęp",
)


def parse_validation_error_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [item.strip() for item in value.split(";") if item.strip()]
    return [str(value)]


def split_field_errors(errors: Sequence[str]) -> Tuple[List[str], List[str]]:
    title_errors: List[str] = []
    description_errors: List[str] = []
    for error in errors:
        normalized = normalize_for_compare(error)
        if "meta title" in normalized or "tytul" in normalized or "tytuł" in error.lower():
            title_errors.append(error)
        else:
            description_errors.append(error)
    return title_errors, description_errors


def normalize_generated_meta_title(value: str) -> str:
    title = normalize_spaces(strip_html(value)).strip('"„”')
    title = title.replace("—", "–")
    title = re.sub(r"\s*[-–]\s*", " – ", title)
    title = re.sub(r"\s*\|\s*", " | ", title)
    title = re.sub(r"\s+([,.;:])", r"\1", title)
    title = re.sub(r"([,.;:])(?=[A-Za-zÀ-ž0-9])", r"\1 ", title)
    title = re.sub(r"\s{2,}", " ", title).strip()

    replacements = (
        (r"\bKod\s+Ucznia\b", "kod uczniowski"),
        (r"\bKod\s+Uczniowski\b", "kod uczniowski"),
        (r"\bKod\s+dla\s+Ucznia\b", "kod uczniowski"),
        (r"\bKod\s+Nauczyciela\b", "kod nauczycielski"),
        (r"\bKod\s+Nauczycielski\b", "kod nauczycielski"),
        (r"\bKod\s+dla\s+Nauczyciela\b", "kod nauczycielski"),
        (r"\bKod\s+Dostępu\b", "kod dostępu"),
        (r"\bDostęp\s+Online\b", "dostęp online"),
    )
    for pattern, replacement in replacements:
        title = re.sub(pattern, replacement, title, flags=re.IGNORECASE)

    # Polskie rzeczowniki pospolite nie powinny wyglądać jak angielski Title Case.
    for word in V43_GENERIC_POLISH_TITLE_WORDS:
        title = re.sub(rf"(?<!^)\b{re.escape(word)}\b", word.lower(), title)

    return normalize_spaces(title)


def meta_title_validation_errors(
    meta_title: str,
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> List[str]:
    errors: List[str] = []
    title = normalize_generated_meta_title(meta_title)
    normalized = normalize_for_compare(title)
    source_title = clean_source_title_for_prompt(job.get("title", ""))
    required = title_required_features(job)
    signals = required["signals"]

    if not title:
        return ["Brak meta title"]
    if len(title) > META_TITLE_HARD_MAX:
        errors.append(
            f"Za długi meta title: {len(title)} zn. "
            f"(maks. {META_TITLE_HARD_MAX}; dopuszczamy świadomie więcej niż 60)"
        )
    if len(title) < 20 and len(source_title) >= 35:
        errors.append(f"Meta title jest zbyt ogólny lub zbyt krótki: {len(title)} zn.")
    if "..." in title or title.endswith("…"):
        errors.append("Meta title zawiera wielokropek lub wygląda na ucięty")
    if re.search(r"https?://|www\.", title, flags=re.IGNORECASE):
        errors.append("Meta title zawiera URL")
    if re.search(r"\b(praca\s*zbiorowa|pracazbiorowa)\b", normalized):
        errors.append("Meta title zawiera techniczną wartość autora: praca zbiorowa")
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in TITLE_INTERNAL_NOISE_PATTERNS):
        errors.append("Meta title zawiera oznaczenie wewnętrzne lub status produktu")
    if title.endswith(("-", "–", "—", "|", ":", ",", ";", "/", "+")):
        errors.append("Meta title kończy się separatorem i wygląda na urwany")

    final_word_match = re.search(r"([A-Za-zÀ-ž]+(?:'[A-Za-zÀ-ž]+)?)\s*$", title)
    if final_word_match and normalize_for_compare(final_word_match.group(1)) in TITLE_DANGLING_WORDS:
        errors.append(f"Meta title kończy się niepełnym określeniem: {final_word_match.group(1)}")

    source_identity = [normalize_for_compare(item) for item in signals.get("identity_tokens", [])]
    title_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    if source_identity and not any(token in title_tokens or token in normalized for token in source_identity[:4]):
        errors.append("Meta title zgubił nazwę serii lub główną tożsamość produktu")

    for level in signals.get("levels", []):
        if normalize_for_compare(level) not in normalized:
            errors.append(f"Meta title zgubił poziom lub oznaczenie egzaminu: {level}")

    if required["teacher"] and not re.search(r"teacher|nauczyciel", normalized):
        errors.append("Meta title nie odróżnia wariantu nauczycielskiego")
    if required["student"] and not re.search(r"student|uczen|uczni", normalized):
        errors.append("Meta title nie odróżnia wariantu uczniowskiego")
    if required["ebook"] and "ebook" not in normalized:
        errors.append("Meta title zgubił format eBook")
    if required["etext"] and "etext" not in normalized:
        errors.append("Meta title zgubił format eText")
    if required["access"] and not re.search(r"\b(?:kod|kodem|dostep|access)\b", normalized):
        errors.append("Meta title zgubił informację o kodzie lub dostępie")
    if required["myenglishlab"] and "myenglishlab" not in normalized:
        errors.append("Meta title zgubił platformę MyEnglishLab")
    if required["online_practice"] and not re.search(r"online practice|cwiczenia online", normalized):
        errors.append("Meta title zgubił komponent Online Practice")
    if required["teacher_portal"] and not re.search(r"teacher s portal|portal nauczyciel", normalized):
        errors.append("Meta title zgubił komponent Teacher's Portal")
    if required["without_code"] and not re.search(r"bez\s+(?:kodu|klucza)", normalized):
        errors.append("Meta title zgubił informację, że produkt jest bez kodu lub klucza")

    if any(normalized.startswith(normalize_for_compare(prefix)) for prefix in ("kup", "sprawdz", "odkryj", "poznaj")):
        errors.append("Meta title zaczyna się od CTA")

    if existing_title_owners:
        owner = existing_title_owners.get(normalized)
        if owner and str(owner) != str(job.get("sku", "")):
            errors.append(f"Identyczny meta title istnieje już dla innego SKU: {owner}")

    return list(dict.fromkeys(errors))


def title_candidate_score(
    candidate: str,
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> Tuple[float, List[str]]:
    title = normalize_generated_meta_title(candidate)
    errors = meta_title_validation_errors(title, job, existing_title_owners=existing_title_owners)
    if not title:
        return -10000.0, errors

    required = title_required_features(job)
    signals = required["signals"]
    normalized = normalize_for_compare(title)
    length = len(title)
    score = 100.0 - 35.0 * len(errors)

    # v4.4.3: długość jest tylko jednym z sygnałów rankera; kompletność ma pierwszeństwo.
    # Ranker premiuje kompletne, naturalne title w szerokim zakresie, a 75 znaków
    # jest jedynym twardym maksimum. Tytuł 73-75 znaków nadal może wygrać, jeśli
    # lepiej zachowuje serię, poziom, format, platformę lub typ dostępu.
    if META_TITLE_TARGET_MIN <= length <= META_TITLE_TARGET_MAX:
        score += 32
    elif META_TITLE_TARGET_MAX < length <= META_TITLE_HARD_MAX:
        score += 29
    elif 50 <= length < META_TITLE_TARGET_MIN:
        score += 18
    elif 40 <= length < 50:
        score += 8
    elif length <= META_TITLE_HARD_MAX:
        score += 2
    else:
        score -= 18

    for token in signals.get("identity_tokens", [])[:4]:
        if normalize_for_compare(token) in normalized:
            score += 7
    for level in signals.get("levels", []):
        if normalize_for_compare(level) in normalized:
            score += 10
    for fmt in signals.get("formats", []):
        if normalize_for_compare(fmt) in normalized:
            score += 5
    for platform in signals.get("platforms", []):
        if normalize_for_compare(platform) in normalized:
            score += 5
    if required["teacher_portal"] and "teacher s portal" in normalized:
        score += 9
    if required["online_practice"] and "online practice" in normalized:
        score += 7
    if required["myenglishlab"] and "myenglishlab" in normalized:
        score += 8

    if required["student"] and re.search(r"kod\s+uczni|dla\s+ucznia|uczniowski", normalized):
        score += 12
    if required["teacher"] and re.search(r"kod\s+nauczyciel|dla\s+nauczyciela|nauczycielski", normalized):
        score += 12
    if required["access"] and re.search(r"kod\s+(?:uczni|nauczyciel|dostep)|dostep online", normalized):
        score += 8
    if re.search(r"(?:\||–)\s+kod\s+(?:uczni|nauczyciel|dostep)", title, flags=re.IGNORECASE):
        score += 6

    # Preferujemy jeden czytelny separator. Kilka różnych separatorów wygląda
    # jak zlepek nazwy magazynowej, a nie redakcja SERP.
    dash_count = len(re.findall(r"\s–\s", title))
    pipe_count = title.count("|")
    if dash_count == 1 and pipe_count == 0:
        score += 6
    elif dash_count == 1 and pipe_count == 1:
        score += 5
    elif dash_count + pipe_count == 0 and length >= 40:
        score -= 5
    if dash_count + pipe_count > 2:
        score -= 8
    if "&" in title:
        score -= 4
    if title.count(".") >= 2:
        score -= 4

    awkward_patterns = (
        r"\buczen\s*[–|-]",
        r"\bonline\s+kod\b",
        r"\bportal\s+kod\b",
        r"\bpractice\s+kod\b",
        r"\bebook\s+kod\b",
        r"\betext\s+kod\b",
        r"\bkod\s+online\b",
    )
    for pattern in awkward_patterns:
        if re.search(pattern, normalized):
            score -= 12

    for word in V43_GENERIC_POLISH_TITLE_WORDS:
        if re.search(rf"(?<!^)\b{re.escape(word)}\b", title):
            score -= 1.5

    return score, errors


def extract_meta_title_candidates(data: Dict) -> List[str]:
    raw = data.get("meta_title_candidates", [])
    candidates: List[str] = []
    if isinstance(raw, str):
        candidates.append(raw)
    elif isinstance(raw, list):
        candidates.extend(str(item) for item in raw if str(item).strip())
    legacy = str(data.get("meta_title", "") or "").strip()
    if legacy:
        candidates.append(legacy)
    unique: List[str] = []
    seen: Set[str] = set()
    for candidate in candidates:
        normalized_candidate = normalize_generated_meta_title(candidate)
        key = normalize_for_compare(normalized_candidate)
        if normalized_candidate and key not in seen:
            seen.add(key)
            unique.append(normalized_candidate)
    return unique


def _primary_identity_for_title_repair(job: Dict) -> str:
    """Buduje krótki, bezpieczny rdzeń nazwy do napraw strukturalnych meta title.

    Używamy przede wszystkim pierwszego członu nazwy Akeneo (zwykle seria/model),
    a poziom dopinamy osobno. Funkcja jest uruchamiana wyłącznie jako fallback
    dla tytułu, który już nie przeszedł walidacji - nie zastępuje normalnego rankera.
    """
    raw = clean_source_title_for_prompt(job.get("title", ""))
    raw = normalize_spaces(raw)
    if not raw:
        return ""

    # Pierwszy człon w katalogu najczęściej jest stabilną nazwą serii/modelu.
    parts = [normalize_spaces(part) for part in re.split(r"\s*[.|;]\s*", raw) if normalize_spaces(part)]
    identity = parts[0] if parts else raw

    # Usuń kwalifikatory, które naprawa dopnie później w kontrolowanej postaci.
    identity = re.sub(r"\s*\((?:no\s+key|without\s+key|bez\s+(?:kodu|klucza))\)\s*", " ", identity, flags=re.I)
    identity = re.sub(r"\b(?:Student'?s|Teacher'?s)\s+(?:eBook|Book)\b.*$", "", identity, flags=re.I)
    identity = normalize_spaces(identity).strip(" -–|,;:")

    # Jeżeli pierwszy człon sam jest przesadnie długi, zostawiamy sensowną granicę
    # słowa. To fallback; finalny walidator nadal musi zaakceptować wynik.
    if len(identity) > 48:
        identity = smart_truncate(identity, 48).rstrip(" -–|,;:")
    return identity


def _required_access_phrase(required: Dict[str, object]) -> str:
    if required.get("without_code"):
        return "bez klucza"
    if required.get("teacher") and required.get("access"):
        return "kod nauczycielski"
    if required.get("student") and required.get("access"):
        return "kod uczniowski"
    if required.get("access"):
        return "kod dostępu"
    return ""


def _required_component_phrases(job: Dict, required: Dict[str, object]) -> List[str]:
    """Zwraca tylko twarde cechy wariantu, które odróżniają SKU."""
    source = normalize_for_compare(job.get("title", ""))
    parts: List[str] = []

    # Najbardziej specyficzne platformy/komponenty przed formatem ogólnym.
    if required.get("teacher_portal"):
        parts.append("Teacher's Portal")
    if required.get("myenglishlab"):
        parts.append("MyEnglishLab")
    if required.get("online_practice"):
        parts.append("Online Practice")

    if required.get("etext"):
        parts.insert(0, "eText")
    elif required.get("ebook"):
        # Student's / Teacher's eBook niesie dodatkowo odbiorcę i jest naturalne.
        if required.get("student") and re.search(r"student'?s\s+ebook", source):
            parts.insert(0, "Student's eBook")
        elif required.get("teacher") and re.search(r"teacher'?s\s+ebook", source):
            parts.insert(0, "Teacher's eBook")
        else:
            parts.insert(0, "eBook")

    # Zachowaj kolejność i usuń duplikaty.
    result: List[str] = []
    seen: Set[str] = set()
    for part in parts:
        key = normalize_for_compare(part)
        if key and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def _fit_repair_title(parts: Sequence[str], suffix: str = "") -> str:
    """Składa naprawiony title bez mechanicznego urywania końcówki."""
    clean_parts = [normalize_spaces(p).strip(" -–|,;:") for p in parts if normalize_spaces(p).strip(" -–|,;:")]
    if not clean_parts:
        return ""

    if len(clean_parts) == 1:
        base = clean_parts[0]
    else:
        base = f"{clean_parts[0]} – " + " + ".join(clean_parts[1:])
    candidate = f"{base} | {suffix}" if suffix else base
    candidate = normalize_generated_meta_title(candidate)
    if len(candidate) <= META_TITLE_HARD_MAX:
        return candidate

    # Jeżeli po dodaniu twardego kwalifikatora brakuje miejsca, najpierw usuwamy
    # najmniej specyficzny dodatkowy komponent. Nie usuwamy suffixu ani rdzenia.
    extra = list(clean_parts[1:])
    while extra:
        extra.pop()
        base = clean_parts[0] if not extra else f"{clean_parts[0]} – " + " + ".join(extra)
        candidate = f"{base} | {suffix}" if suffix else base
        candidate = normalize_generated_meta_title(candidate)
        if len(candidate) <= META_TITLE_HARD_MAX:
            return candidate
    return ""


def build_local_meta_title_repairs(meta_title: str, job: Dict) -> List[str]:
    """Tworzy bezkosztowe kandydatury naprawcze na podstawie błędów walidatora.

    Gdy walidator *wie*, że brakuje twardej cechy SKU, aplikacja ma spróbować ją
    naprawić, a nie jedynie wyświetlić ostrzeżenie. Funkcja nie zgaduje cech -
    korzysta wyłącznie z nazwy produktu / title_required_features().
    """
    original = normalize_generated_meta_title(meta_title)
    if not original:
        return []
    errors = meta_title_validation_errors(original, job)
    if not errors:
        return []

    required = title_required_features(job)
    repairs: List[str] = []

    # 1. Najmniej inwazyjna naprawa: dopisz brakujący kwalifikator, jeśli się mieści.
    append_phrases: List[str] = []
    normalized = normalize_for_compare(original)
    levels = required.get("signals", {}).get("levels", []) if isinstance(required.get("signals"), dict) else []
    for level in levels:
        if normalize_for_compare(level) not in normalized:
            append_phrases.append(str(level))
    if required.get("ebook") and "ebook" not in normalized:
        append_phrases.append("eBook")
    if required.get("etext") and "etext" not in normalized:
        append_phrases.append("eText")
    if required.get("myenglishlab") and "myenglishlab" not in normalized:
        append_phrases.append("MyEnglishLab")
    if required.get("online_practice") and "online practice" not in normalized:
        append_phrases.append("Online Practice")
    if required.get("teacher_portal") and "teacher s portal" not in normalized:
        append_phrases.append("Teacher's Portal")

    access_phrase = _required_access_phrase(required)
    if access_phrase and normalize_for_compare(access_phrase) not in normalized:
        append_phrases.append(access_phrase)

    if append_phrases:
        appended = normalize_generated_meta_title(original + " | " + " | ".join(append_phrases))
        if len(appended) <= META_TITLE_HARD_MAX:
            repairs.append(appended)

    # 2. Rekonstrukcja semantyczna: rdzeń serii + poziom + twarde komponenty + dostęp.
    identity = _primary_identity_for_title_repair(job)
    if identity:
        level_tokens = [str(x) for x in levels]
        identity_normalized = normalize_for_compare(identity)
        for level in level_tokens:
            if normalize_for_compare(level) not in identity_normalized:
                identity = normalize_spaces(f"{identity} {level}")

        components = _required_component_phrases(job, required)
        reconstructed = _fit_repair_title([identity, *components], access_phrase)
        if reconstructed:
            repairs.append(reconstructed)

        # 3. Wersja minimalistyczna zachowująca wyłącznie rdzeń + format + twardy dostęp.
        primary_format = ""
        if required.get("etext"):
            primary_format = "eText"
        elif required.get("ebook"):
            if required.get("student"):
                primary_format = "Student's eBook"
            elif required.get("teacher"):
                primary_format = "Teacher's eBook"
            else:
                primary_format = "eBook"
        minimal = _fit_repair_title([identity, primary_format] if primary_format else [identity], access_phrase)
        if minimal:
            repairs.append(minimal)

    unique: List[str] = []
    seen: Set[str] = {normalize_for_compare(original)}
    for candidate in repairs:
        key = normalize_for_compare(candidate)
        if candidate and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def select_best_meta_title_candidate(
    candidates: Sequence[str],
    job: Dict,
    *,
    existing_title_owners: Optional[Dict[str, str]] = None,
) -> Tuple[str, List[str], List[Tuple[str, float, List[str]]]]:
    diagnostics: List[Tuple[str, float, List[str]]] = []
    seen: Set[str] = set()

    def add_candidate(value: str) -> None:
        normalized_candidate = normalize_generated_meta_title(value)
        key = normalize_for_compare(normalized_candidate)
        if not normalized_candidate or key in seen:
            return
        seen.add(key)
        score, errors = title_candidate_score(
            normalized_candidate,
            job,
            existing_title_owners=existing_title_owners,
        )
        diagnostics.append((normalized_candidate, score, errors))

    # Kandydatury AI.
    for candidate in candidates:
        add_candidate(candidate)

    # v4.4.4: walidator-driven auto-repair. Każdy błędny kandydat może stworzyć
    # lokalne, bezkosztowe naprawy. Dzięki temu np. brak „bez klucza” nie kończy
    # się ostrzeżeniem, jeśli da się zbudować poprawny title do 75 znaków.
    initial = list(diagnostics)
    for candidate, _, errors in initial:
        if not errors:
            continue
        for repair in build_local_meta_title_repairs(candidate, job):
            add_candidate(repair)

    if not diagnostics:
        fallback = normalize_generated_meta_title(build_meta_title(job.get("title", ""), job.get("author", "")))
        add_candidate(fallback)
        for repair in build_local_meta_title_repairs(fallback, job):
            add_candidate(repair)

    # Najpierw kandydat bez błędów, dopiero potem score. To pozwala naprawionej
    # wersji wygrać nawet wtedy, gdy pierwotny title stylistycznie miał wyższy score.
    diagnostics.sort(key=lambda item: (not item[2], item[1]), reverse=True)
    best_title, _, best_errors = diagnostics[0]
    return best_title, best_errors, diagnostics


def locked_fields_from_job(job: Dict) -> Tuple[str, str]:
    errors = parse_validation_error_list(job.get("validation_errors", ""))
    title_errors, description_errors = split_field_errors(errors)
    locked_title = job.get("meta_title", "") if job.get("meta_title") and not title_errors and int(job.get("attempts", 0)) > 0 else ""
    locked_description = job.get("meta_description", "") if job.get("meta_description") and not description_errors and int(job.get("attempts", 0)) > 0 else ""
    return locked_title, locked_description


def extract_level_tokens(value: str) -> List[str]:
    """Wyciąga poziomy CEFR/egzaminy bez mieszania poziomów z innych pól."""
    text = normalize_spaces(strip_html(value))
    pattern = (
        r"(?<![A-Za-z0-9])(?:C2\s+Proficiency|C1\s+Advanced|B2\s+First|"
        r"C1\s*[-–]\s*C2|Pre-?A1|A1\+?|A2\+?|B1\+?|B2\+?|C1\+?|C2)(?![A-Za-z0-9])"
    )
    result: List[str] = []
    seen: Set[str] = set()
    for match in re.findall(pattern, text, flags=re.IGNORECASE):
        canonical = normalize_spaces(match).replace("–", "-")
        key = normalize_for_compare(canonical)
        if key not in seen:
            seen.add(key)
            result.append(canonical)
    return result


def extract_title_signals(raw_title: str, description: str = "") -> Dict[str, List[str]]:
    """Sygnały produktu z rozdzieleniem twardych danych nazwy i miękkiego opisu.

    Poziom z nazwy produktu jest wiążący. Dopiero gdy nazwa NIE zawiera poziomu,
    wolno użyć poziomu z opisu, i tylko gdy opis wskazuje dokładnie jeden poziom.
    Dzięki temu Business Partner C1 nie zostanie błędnie odrzucony z powodu B2
    wspomnianego w opisie źródłowym.
    """
    title_only = normalize_spaces(strip_html(raw_title))
    desc_only = normalize_spaces(strip_html(description))
    source = normalize_spaces(f"{title_only} {desc_only[:1200]}")

    title_levels = extract_level_tokens(title_only)
    desc_levels = extract_level_tokens(desc_only[:1800])
    if title_levels:
        levels = title_levels
    else:
        unique_desc = []
        seen_desc: Set[str] = set()
        for level in desc_levels:
            key = normalize_for_compare(level)
            if key not in seen_desc:
                seen_desc.add(key)
                unique_desc.append(level)
        levels = unique_desc if len(unique_desc) == 1 else []

    platforms = [
        name for name in (
            "MyEnglishLab", "Pearson English Portal", "Pearson Practice English App",
            "Teacher's Portal", "Online Practice"
        )
        if re.search(re.escape(name), source, flags=re.IGNORECASE)
    ]
    formats = [
        name for name in ("eBook", "eText", "PDF", "audio", "video", "CD", "DVD", "online")
        if re.search(rf"\b{re.escape(name)}\b", source, flags=re.IGNORECASE)
    ]

    audience: List[str] = []
    if re.search(r"teacher|nauczyciel", source, flags=re.IGNORECASE):
        audience.append("nauczyciel")
    if re.search(r"student|uczni|learner", source, flags=re.IGNORECASE):
        audience.append("uczeń")

    access: List[str] = []
    if re.search(r"access|kod\s+(?:uczniowski|nauczyciel)|code|licenc", source, flags=re.IGNORECASE):
        access.append("kod lub dostęp cyfrowy")
    if re.search(r"without\s+key|no\s+key|bez\s+klucza|bez\s+kodu", source, flags=re.IGNORECASE):
        access.append("bez kodu / klucza")

    components: List[str] = []
    for english, polish in TITLE_COMPONENT_TRANSLATIONS:
        if re.search(re.escape(english), source, flags=re.IGNORECASE):
            components.append(f"{english} → {polish}")

    identity_tokens: List[str] = []
    for token in re.findall(r"[A-Za-zÀ-ž0-9][A-Za-zÀ-ž0-9'+.-]*", clean_source_title_for_prompt(title_only)):
        normalized = normalize_for_compare(token)
        if len(normalized) < 2 or normalized in TITLE_GENERIC_TOKENS:
            continue
        if normalized not in {normalize_for_compare(item) for item in identity_tokens}:
            identity_tokens.append(token)
        if len(identity_tokens) >= 6:
            break

    return {
        "levels": levels,
        "title_levels": title_levels,
        "description_levels": desc_levels,
        "platforms": platforms,
        "formats": formats,
        "audience": audience,
        "access": access,
        "components": components,
        "identity_tokens": identity_tokens,
    }


def source_product_type(job: Dict) -> str:
    """Najpierw klasyfikuj po nazwie; opis jest wyłącznie fallbackiem."""
    title = normalize_for_compare(job.get("title", ""))
    desc = normalize_for_compare(strip_html(job.get("description", ""))[:1200])

    def classify(source: str) -> str:
        has_access = bool(re.search(r"\b(access|code|kod|licenc|portal)\b", source))
        if has_access and re.search(r"teacher|nauczyciel", source):
            return "teacher_access_code"
        if has_access and re.search(r"student|uczen|uczni|learner", source):
            return "student_access_code"
        if "ebook" in source and re.search(r"online practice|myenglishlab|portal|access", source):
            return "digital_course_bundle"
        if "etext" in source:
            return "etext"
        if "ebook" in source:
            return "ebook"
        if re.search(r"online practice|cwiczenia online", source):
            return "online_practice"
        if re.search(r"workbook|activity book|zeszyt cwiczen", source):
            return "workbook"
        if re.search(r"student'?s book|coursebook|podrecznik", source):
            return "textbook"
        if re.search(r"slownik|dictionary", source):
            return "dictionary"
        if re.search(r"klasa\s+\d|liceum|technikum|szkola podstawowa|matura", source):
            return "school_book"
        return "other"

    title_type = classify(title)
    if title_type != "other":
        return title_type
    desc_type = classify(desc)
    if desc_type != "other":
        return desc_type
    if job.get("author") and not re.search(r"access|code|kod|ebook|etext|online practice", title):
        return "general_book"
    return "other"


def title_required_features(job: Dict) -> Dict[str, object]:
    """Twarde wymagania walidatora bierzemy z nazwy SKU, nie z narracji opisu."""
    raw_title = job.get("title", "")
    source = normalize_for_compare(raw_title)
    title_signals = extract_title_signals(raw_title, "")
    return {
        "type": source_product_type(job),
        "signals": title_signals,
        "student": bool(re.search(r"student|uczen|uczni|learner", source)),
        "teacher": bool(re.search(r"teacher|nauczyciel", source)),
        "access": bool(re.search(r"access|code|kod\s+(?:uczni|nauczyciel)|licenc", source)),
        "without_code": bool(re.search(r"without key|no key|bez klucza|bez kodu", source)),
        "ebook": "ebook" in source,
        "etext": "etext" in source,
        "myenglishlab": "myenglishlab" in source,
        "online_practice": "online practice" in source,
        "teacher_portal": bool(re.search(r"teacher\s+s\s+portal|teachers?\s+portal", source)),
    }


def compact_description_context(description: str, title: str = "", max_chars: int = 1600) -> str:
    """Wybiera najbardziej użyteczne fragmenty opisu zamiast wysyłać 3600 znaków."""
    clean = normalize_spaces(strip_html(description))
    if len(clean) <= max_chars:
        return clean

    # Zachowujemy początek oraz zdania zawierające cechy wariantu, poziomy,
    # formaty, platformy i informacje produktowe. Dla zwykłej książki początek
    # opisu nadal niesie fabułę / temat, więc zawsze jest pierwszy.
    sentences = [normalize_spaces(s) for s in re.split(r"(?<=[.!?])\s+", clean) if normalize_spaces(s)]
    selected: List[str] = []
    selected_keys: Set[str] = set()

    def add(sentence: str) -> None:
        key = normalize_for_compare(sentence)
        if sentence and key not in selected_keys:
            selected.append(sentence)
            selected_keys.add(key)

    for sentence in sentences[:3]:
        add(sentence)

    signal_pattern = re.compile(
        r"\b(?:A1|A2|B1|B2|C1|C2|First|Advanced|Proficiency|eBook|eText|"
        r"MyEnglishLab|Online Practice|Teacher'?s Portal|Student|Teacher|uczni|nauczyciel|"
        r"access|code|kod|licenc|Workbook|Student'?s Book|Teacher'?s Book|Coursebook|"
        r"tom|część|czesc|klasa|matura|egzamin|słownik|slownik|dictionary)\b",
        flags=re.IGNORECASE,
    )
    for sentence in sentences[3:]:
        if signal_pattern.search(sentence):
            add(sentence)

    # Dodatkowo wybierz zdania zawierające rzadkie tokeny z nazwy produktu.
    title_tokens = [
        token for token in re.findall(r"[A-Za-zÀ-ž0-9'+.-]+", title)
        if len(normalize_for_compare(token)) >= 4
        and normalize_for_compare(token) not in TITLE_GENERIC_TOKENS
    ][:5]
    for sentence in sentences[3:]:
        normalized_sentence = normalize_for_compare(sentence)
        if any(normalize_for_compare(token) in normalized_sentence for token in title_tokens):
            add(sentence)

    result = " ".join(selected)
    if len(result) < min(900, max_chars) and len(sentences) > len(selected):
        for sentence in sentences:
            add(sentence)
            result = " ".join(selected)
            if len(result) >= min(1100, max_chars):
                break

    return smart_truncate(" ".join(selected), max_chars)


def description_context_limit(job: Dict) -> int:
    """Mniej tokenów dla prostych wariantów cyfrowych, więcej dla książek."""
    product_type = source_product_type(job)
    if product_type in {
        "student_access_code", "teacher_access_code", "digital_course_bundle",
        "online_practice", "ebook", "etext",
    }:
        return 1050
    if product_type in {"textbook", "workbook", "school_book"}:
        return 1400
    return 1850


def meta_validation_errors(
    meta_description: str,
    *,
    job: Optional[Dict] = None,
) -> List[str]:
    """Tylko błędy, które realnie uzasadniają retry.

    Nie blokujemy za podobny początek, pierwsze 3/6 słów ani duplikat opisu.
    Przy katalogu 10k-50k te sygnały są normalne i nie warte kolejnego requestu AI.
    """
    errors: List[str] = []
    text = normalize_spaces(strip_html(meta_description)).strip('"„”')
    normalized = normalize_for_compare(text)

    if not text:
        return ["Brak meta description"]
    if len(text) < META_DESCRIPTION_HARD_MIN:
        errors.append(
            f"Za krótki meta description: {len(text)} zn. (minimum techniczne {META_DESCRIPTION_HARD_MIN})"
        )
    if len(text) > META_DESCRIPTION_HARD_MAX:
        errors.append(
            f"Za długi meta description: {len(text)} zn. (maksimum techniczne {META_DESCRIPTION_HARD_MAX})"
        )

    hard_cta = (
        "sprawdź ofertę", "sprawdz oferte", "kup teraz", "zamów teraz", "zamow teraz",
        "dodaj do koszyka", "zamów już dziś", "zamow juz dzis",
    )
    if any(normalize_for_compare(phrase) in normalized for phrase in hard_cta):
        errors.append("Meta description zawiera bezpośrednie CTA sklepu")
    if re.search(r"https?://|www\.", text, flags=re.IGNORECASE):
        errors.append("Meta description zawiera URL")
    if "..." in text or text.endswith("…"):
        errors.append("Meta description wygląda na ucięty")
    if re.search(r"\b(xyz|lorem|ipsum|placeholder)\b", normalized):
        errors.append("Meta description zawiera placeholder")

    # Liczby nadal chronimy, bo halucynowana długość licencji/czas dostępu jest
    # merytorycznie groźniejsza niż podobieństwo stylistyczne.
    if job:
        source = normalize_for_compare(
            f"{job.get('title', '')} {strip_html(job.get('description', ''))}"
        )
        generated_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", normalized))
        source_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", source))
        unsupported = sorted(generated_numbers - source_numbers)
        if unsupported:
            errors.append(
                "Meta description dodaje liczby nieobecne w źródle: "
                + ", ".join(unsupported[:5])
            )

    return list(dict.fromkeys(errors))
