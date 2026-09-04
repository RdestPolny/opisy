import re
import unicodedata
from html.parser import HTMLParser
from typing import List, Sequence
from urllib.parse import urlsplit, urlunsplit


ALLOWED_TAGS = {"p", "h2", "h3", "b", "a"}
VALIDATOR_API_VERSION = 2

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


def sanitize_html(value: str) -> str:
    """Oczyszcza HTML: unwrappuje <span>/<div>/<font>, normalizuje <strong> do <b>, usuwa atrybuty style/class."""
    if not value:
        return ""
    text = value
    # Normalizacja tagów semantycznych
    text = re.sub(r"<\s*strong(?:\s[^>]*)?>(.*?)<\s*/\s*strong\s*>", r"<b>\1</b>", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<\s*em(?:\s[^>]*)?>(.*?)<\s*/\s*em\s*>", r"<i>\1</i>", text, flags=re.DOTALL | re.IGNORECASE)

    # Bezpieczne unwrappowanie span (usuwa tag, zachowuje treść)
    for _ in range(5):
        if "<span" not in text.lower():
            break
        text = re.sub(r"<\s*span(?:\s[^>]*)?>(.*?)<\s*/\s*span\s*>", r"\1", text, flags=re.DOTALL | re.IGNORECASE)

    # Zamiana div na p lub unwrapping
    for _ in range(3):
        if "<div" not in text.lower() and "<font" not in text.lower():
            break
        text = re.sub(r"<\s*div(?:\s[^>]*)?>(.*?)<\s*/\s*div\s*>", r"<p>\1</p>", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<\s*font(?:\s[^>]*)?>(.*?)<\s*/\s*font\s*>", r"\1", text, flags=re.DOTALL | re.IGNORECASE)

    # Usuwanie atrybutów style, class, id, data-* ze wszystkich tagów (z wyjątkiem href w <a>)
    def _clean_tag_attrs(match):
        raw_tag = match.group(1).lower()
        full_tag = match.group(0)
        if raw_tag == "a":
            href_m = re.search(r'href\s*=\s*(["\'])(.*?)\1', full_tag, flags=re.IGNORECASE)
            if href_m:
                return f'<a href="{href_m.group(2)}">'
            return "<a>"
        elif raw_tag.startswith("/"):
            return f"<{raw_tag}>"
        else:
            return f"<{raw_tag}>"

    text = re.sub(r"<(/?[a-zA-Z0-9]+)(?:\s+[^>]*)?>", _clean_tag_attrs, text)
    # Czyszczenie pustych tagów
    text = re.sub(r"<b>\s*</b>", "", text)
    text = re.sub(r"<p>\s*</p>", "", text)
    return text.strip()


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
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _weak_bold_phrase(value: str) -> bool:
    normalized = _normalized_text(value)
    if not normalized:
        return True
    words = normalized.split()
    if len(words) > 7:
        return True
    return len(words) == 1 and normalized in BOLD_GENERIC_PHRASES


def validate_description_html(
    value: str,
    *,
    require_full_structure: bool = True,
    required_link: str = "",
    required_link_paragraph: int = 0,
    required_contributors: Sequence[str] = (),
    required_contributor_role: str = "",
    **validation_options,
) -> List[str]:
    """Waliduje opis i zachowuje kompatybilność z rozszerzeniami API walidatora.

    Streamlit może rerunować app.py w tym samym interpreterze. Przy zmianie call-site
    i modułu pomocniczego w jednym deploymencie stara wersja modułu mogła pozostać
    w sys.modules i kończyć się TypeError dla nowego argumentu keyword. Od API v2
    nieznane opcje są raportowane jako błąd walidacji zamiast wywracać cały workflow.
    """
    cleaned_value = sanitize_html(value)
    plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned_value or "")).strip()
    if not plain_text:
        return ["opis jest pusty"]

    parser = _DescriptionParser()
    parser.feed(cleaned_value)
    errors = list(parser.errors)
    if validation_options:
        errors.append(
            "nieobsługiwane opcje walidatora: " + ", ".join(sorted(validation_options))
        )
    if parser.stack:
        errors.append("opis zawiera niedomknięte tagi")
    if require_full_structure:
        if len(plain_text) < 700:
            errors.append("opis jest za krótki (minimum 700 znaków tekstu)")
        h2_count = parser.tags.count("h2")
        h3_count = parser.tags.count("h3")
        if (h2_count + h3_count) < 2:
            errors.append("opis musi zawierać co najmniej dwa śródtytuły (nagłówki <h2> lub <h3>)")
        if len(parser.paragraphs) < 3:
            errors.append("opis musi zawierać co najmniej trzy akapity <p>")
        elif any(len(paragraph["text"]) < 180 for paragraph in parser.paragraphs):
            errors.append("każdy z głównych akapitów musi mieć co najmniej 180 znaków")
        if parser.paragraphs and any(len(paragraph["bold_phrases"]) < 2 for paragraph in parser.paragraphs):
            errors.append("każdy akapit musi zawierać co najmniej dwa merytoryczne wyróżnienia <b>")
        if any(
            _weak_bold_phrase(phrase)
            for paragraph in parser.paragraphs
            for phrase in paragraph["bold_phrases"]
        ):
            errors.append("pogrubienia muszą obejmować konkretne frazy, a nie ogólne pojedyncze słowa")
        if any(re.search(r"[.!?,;:]$", heading["text"]) for heading in parser.headings):
            errors.append("nagłówki <h2> i <h3> nie mogą kończyć się znakiem interpunkcyjnym")
    if required_link:
        expected = _normalized_url(required_link)
        hrefs = [_normalized_url(href) for href in parser.hrefs if href]
        if hrefs != [expected]:
            errors.append("opis musi zawierać dokładnie jeden link z wymaganym adresem URL")
        elif required_link_paragraph:
            link_paragraphs = [
                index
                for index, paragraph in enumerate(parser.paragraphs, start=1)
                if paragraph["link_count"]
            ]
            if link_paragraphs != [required_link_paragraph]:
                errors.append(f"link wewnętrzny musi znajdować się w akapicie {required_link_paragraph}")

    normalized_plain_text = _normalized_text(plain_text)
    missing_contributors = [
        contributor
        for contributor in required_contributors
        if _normalized_text(contributor) not in normalized_plain_text
    ]
    if missing_contributors:
        errors.append("opis pomija twórców: " + ", ".join(missing_contributors))

    if required_contributor_role == "redakcja naukowa":
        role_markers = (
            "pod redakcja",
            "redakcja naukowa",
            "redaktorzy naukowi",
            "redaktorki naukowe",
        )
        if not any(marker in normalized_plain_text for marker in role_markers):
            errors.append("opis musi wskazywać, że wymienione osoby odpowiadają za redakcję naukową")
    return list(dict.fromkeys(errors))
