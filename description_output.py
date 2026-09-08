import re
import unicodedata
from dataclasses import dataclass, field
from html import unescape

import nh3
from html.parser import HTMLParser
from typing import List, Sequence
from urllib.parse import urlsplit, urlunsplit


ALLOWED_TAGS = {"p", "h2", "h3", "b", "a"}
VALIDATOR_API_VERSION = 4

BOLD_GENERIC_PHRASES = {
    "autor",
    "autorzy",
    "czytelnik",
    "czytelnicy",
    "ksiazka",
    "publikacja",
    "temat",
    "tresc",
    "wiedza",
}


def is_meta_only_result(result: dict) -> bool:
    return not bool(result.get("description_html"))


def is_reusable_result(result: dict, *, meta_only: bool) -> bool:
    if not result or result.get("error"):
        return False
    if meta_only:
        return bool(result.get("meta_title") and result.get("meta_description"))
    return bool(result.get("description_html"))


def _safe_href(tag: str, attribute: str, value: str):
    if tag != "a" or attribute != "href":
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in {"http", "https"} and parts.hostname:
            return value
    except ValueError:
        pass
    return None


def sanitize_html(value: str) -> str:
    """Parse untrusted HTML before normalization; never reconstruct raw attributes."""
    if not value:
        return ""
    cleaned = nh3.clean(
        value,
        tags=ALLOWED_TAGS | {"strong", "div"},
        attributes={"a": {"href"}},
        attribute_filter=_safe_href,
        clean_content_tags={"script", "style", "iframe", "object", "template", "svg", "math"},
        url_schemes={"http", "https"},
        link_rel=None,
    )
    # Only attribute-free, parser-serialized tags are changed here.
    cleaned = cleaned.replace("<strong>", "<b>").replace("</strong>", "</b>")
    cleaned = cleaned.replace("<div>", "<p>").replace("</div>", "</p>")
    # Reparse after block normalization to repair nested paragraphs safely.
    cleaned = nh3.clean(
        cleaned, tags=ALLOWED_TAGS, attributes={"a": {"href"}},
        attribute_filter=_safe_href, url_schemes={"http", "https"}, link_rel=None,
    )
    cleaned = re.sub(r"<(b|p)>\s*</\1>", "", cleaned)
    return cleaned.strip()


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: List[str] = []
        self.stack: List[str] = []
        self.errors: List[str] = []
        self.hrefs: List[str] = []
        self.paragraphs: List[dict] = []
        self.headings: List[dict] = []
        self._paragraph = None
        self._heading = None
        self._bold_text = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.tags.append(tag)
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"niedozwolony tag <{tag}>")
        attrs = dict(attrs)
        if tag == "a":
            self.hrefs.append(attrs.get("href", "").strip())
            if self._paragraph is not None:
                self._paragraph["link_count"] += 1
        if tag == "p":
            self._paragraph = {"text": [], "bold_phrases": [], "link_count": 0}
        elif tag in {"h2", "h3"}:
            self._heading = {"tag": tag, "text": []}
        elif tag == "b" and self._paragraph is not None:
            self._bold_text = []
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"niepoprawnie domknięty tag </{tag}>")
        if tag == "p" and self._paragraph is not None:
            self._paragraph["text"] = " ".join(self._paragraph["text"]).strip()
            self.paragraphs.append(self._paragraph)
            self._paragraph = None
        elif tag in {"h2", "h3"} and self._heading is not None:
            self._heading["text"] = " ".join(self._heading["text"]).strip()
            self.headings.append(self._heading)
            self._heading = None
        elif tag == "b" and self._bold_text is not None:
            phrase = " ".join(self._bold_text).strip()
            if phrase and self._paragraph is not None:
                self._paragraph["bold_phrases"].append(phrase)
            self._bold_text = None

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._paragraph is not None:
            self._paragraph["text"].append(text)
            if self._bold_text is not None:
                self._bold_text.append(text)
        if self._heading is not None:
            self._heading["text"].append(text)


def _normalized_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))


def _normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", (value or "").replace("ł", "l").replace("Ł", "L"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _weak_bold_phrase(value: str) -> bool:
    """Wskazuje wyłącznie ewidentnie ogólne pojedyncze słowo.

    Dłuższa fraza nie jest automatycznie słaba tylko dlatego, że ma >7 słów.
    To jest heurystyka jakościowa, a nie kryterium poprawności HTML.
    """
    normalized = _normalized_text(value)
    if not normalized:
        return True
    words = normalized.split()
    return len(words) == 1 and normalized in BOLD_GENERIC_PHRASES


@dataclass
class ValidationReport:
    clean_html: str
    errors: List[str] = field(default_factory=list)
    warnings: List[dict] = field(default_factory=list)

    def warn(self, code: str, message: str, paragraph: int = 0) -> None:
        issue = {"code": code, "message": message}
        if paragraph:
            issue["paragraph"] = paragraph
        self.warnings.append(issue)


def analyze_description_html(
    value: str,
    *,
    require_full_structure: bool = True,
    required_link: str = "",
    required_link_paragraph: int = 0,
    required_contributors: Sequence[str] = (),
    required_contributor_role: str = "",
    strict_bold_quality: bool = False,
    **validation_options,
) -> ValidationReport:
    """Return safe HTML with non-blocking editorial diagnostics.

    Only empty/unusable output blocks delivery. Unknown options are programming
    errors and must be fixed before any generation, not retried by the model.
    strict_bold_quality enables additional diagnostics, never rejection.
    """
    if validation_options:
        raise ValueError("Nieobsługiwane opcje walidatora: " + ", ".join(sorted(validation_options)))
    if required_link:
        if not _safe_href("a", "href", required_link):
            raise ValueError("Link kategorii musi być poprawnym adresem http:// lub https://")
    if not isinstance(required_link_paragraph, int) or required_link_paragraph < 0:
        raise ValueError("Numer akapitu linku musi być nieujemną liczbą całkowitą")
    cleaned = sanitize_html(value)
    report = ValidationReport(clean_html=cleaned)
    plain_text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", cleaned))).strip()
    if not plain_text:
        report.errors.append("opis jest pusty po oczyszczeniu HTML")
        return report
    if cleaned != (value or "").strip():
        report.warn("HTML_NORMALIZED", "Oczyszczono lub znormalizowano HTML; zachowano bezpieczną treść.")

    parser = _DescriptionParser()
    parser.feed(cleaned)
    parser.close()
    if parser.errors or parser.stack:
        report.errors.append("Nie udało się uzyskać poprawnego HTML opisu")
        return report
    if require_full_structure:
        if len(plain_text) < 700:
            report.warn("DESCRIPTION_LENGTH", "Opis ma mniej niż zalecane 700 znaków tekstu.")
        if parser.tags.count("h2") + parser.tags.count("h3") < 2:
            report.warn("HEADING_COUNT", "Zalecane są co najmniej dwa śródtytuły <h2> lub <h3>.")
        if len(parser.paragraphs) < 3:
            report.warn("PARAGRAPH_COUNT", "Zalecane są co najmniej trzy akapity.")
        for index, paragraph in enumerate(parser.paragraphs, start=1):
            if len(paragraph["text"]) < 180:
                report.warn("PARAGRAPH_LENGTH", f"Akapit {index} ma mniej niż zalecane 180 znaków.", index)
            if len(paragraph["bold_phrases"]) < 2:
                report.warn("BOLD_COUNT", f"Akapit {index} ma mniej niż dwa zalecane wyróżnienia <b>.", index)
            if strict_bold_quality and any(_weak_bold_phrase(phrase) for phrase in paragraph["bold_phrases"]):
                report.warn("BOLD_QUALITY", f"Akapit {index} zawiera ogólne pojedyncze słowo w pogrubieniu.", index)
        if any(re.search(r"[.!?,;:]$", heading["text"]) for heading in parser.headings):
            report.warn("HEADING_PUNCTUATION", "Zalecane są nagłówki bez końcowego znaku interpunkcyjnego.")
    if required_link:
        expected = _normalized_url(required_link)
        hrefs = [_normalized_url(href) for href in parser.hrefs if href]
        if expected not in hrefs:
            report.warn("LINK_MISSING" if not hrefs else "LINK_TARGET_MISMATCH", "Nie znaleziono linku z wymaganym adresem kategorii.")
        elif hrefs != [expected]:
            report.warn("LINK_COUNT", "Zalecany jest dokładnie jeden link do wskazanej kategorii.")
        elif required_link_paragraph:
            link_paragraphs = [index for index, paragraph in enumerate(parser.paragraphs, start=1) if paragraph["link_count"]]
            if link_paragraphs != [required_link_paragraph]:
                report.warn("LINK_POSITION", f"Zalecane położenie linku: akapit {required_link_paragraph}; obecne akapity: {link_paragraphs or 'poza akapitami'}.")

    normalized_plain_text = _normalized_text(plain_text)
    missing_contributors = [name for name in required_contributors if _normalized_text(name) not in normalized_plain_text]
    if missing_contributors:
        report.warn("CONTRIBUTOR_UNVERIFIED", "Nie potwierdzono dosłownej formy nazwisk (możliwa odmiana): " + ", ".join(missing_contributors))
    if required_contributor_role == "redakcja naukowa":
        markers = ("pod redakcja", "redakcja naukowa", "redaktor naukowy", "redaktorka naukowa", "redaktorzy naukowi", "redaktorki naukowe", "redaktorem naukowym", "redaktorka naukowa", "redaktorami naukowymi", "redaktorkami naukowymi")
        if not any(marker in normalized_plain_text for marker in markers):
            report.warn("ROLE_UNVERIFIED", "Nie potwierdzono informacji o redakcji naukowej.")
    return report


def validate_description_html(value: str, **options) -> List[str]:
    """Compatibility API: only blocking errors. Use analyze_description_html for warnings."""
    return analyze_description_html(value, **options).errors


def description_validation_context(result: dict) -> dict:
    link_only = bool(result.get("link_only"))
    return {
        "require_full_structure": not link_only,
        "required_link": result.get("required_link", ""),
        "required_link_paragraph": 0 if link_only else int(result.get("required_link_paragraph", 0) or 0),
        "required_contributors": () if link_only else (result.get("required_contributors") or ()),
        "required_contributor_role": "" if link_only else result.get("required_contributor_role", ""),
    }


def refresh_description_result(result: dict, value: str) -> ValidationReport:
    """Use the same safe value and context in the editor, export and delivery."""
    report = analyze_description_html(value, **description_validation_context(result))
    result["description_html"] = report.clean_html
    result["validation_errors"] = report.errors
    result["validation_warnings"] = report.warnings + [
        issue for issue in (result.get("generation_warnings") or []) if issue not in report.warnings
    ]
    result["validation_version"] = VALIDATOR_API_VERSION
    result["error"] = "; ".join(report.errors) or None
    result["status"] = "error" if report.errors else ("completed_with_warnings" if result["validation_warnings"] else "completed")
    return report


def current_description_value(result: dict, session_state) -> str:
    """Read component changes before export/delivery widgets render on a rerun."""
    sku = result["sku"]
    seed = result.get("editor_seed")
    if seed:
        state = session_state.get(f"visual_editor_{sku}_{seed}", {})
        if isinstance(state, dict) and "html" in state:
            return state["html"]
        return session_state.get(f"edit_{sku}", result.get("description_html", ""))
    # A new generation has no editor seed yet. Never replace it with an older
    # generation's edit_SKU widget state on the first rerun.
    return result.get("description_html", "")


def preserve_description_on_failure(previous: dict, replacement: dict) -> dict:
    if replacement.get("error") and previous.get("description_html") and not previous.get("error"):
        previous = dict(previous)
        previous["generation_warnings"] = [*(previous.get("generation_warnings") or []), {
            "code": "REGENERATION_FAILED", "message": "Zachowano wcześniejszy opis. Regeneracja nie powiodła się: " + str(replacement["error"]),
        }]
        refresh_description_result(previous, previous["description_html"])
        return previous
    return replacement


def deliver_description_result(result: dict, value: str, send, *, channel: str, locale: str) -> ValidationReport:
    """Editorial warnings never block delivery; send only the analyzed safe HTML."""
    report = refresh_description_result(result, value)
    if report.errors:
        raise ValueError("opis nie przeszedł kontroli: " + "; ".join(report.errors))
    send(result["sku"], report.clean_html, result.get("channel") or channel, result.get("locale") or locale)
    return report
