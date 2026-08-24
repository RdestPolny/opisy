import re
from html.parser import HTMLParser
from typing import List
from urllib.parse import urlsplit, urlunsplit


ALLOWED_TAGS = {"p", "h2", "h3", "b", "a"}


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

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.tags.append(tag)
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"niedozwolony tag <{tag}>")
        attrs = dict(attrs)
        if tag == "a":
            self.hrefs.append(attrs.get("href", "").strip())
        if tag == "p":
            self._paragraph = {"text": [], "bold": False}
        elif tag in {"h2", "h3"}:
            self._heading = {"tag": tag, "text": []}
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

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._paragraph is not None:
            self._paragraph["text"].append(text)
            if "b" in self.stack:
                self._paragraph["bold"] = True
        if self._heading is not None:
            self._heading["text"].append(text)


def _normalized_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))


def validate_description_html(
    value: str,
    *,
    require_full_structure: bool = True,
    required_link: str = "",
) -> List[str]:
    cleaned_value = sanitize_html(value)
    plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned_value or "")).strip()
    if not plain_text:
        return ["opis jest pusty"]

    parser = _DescriptionParser()
    parser.feed(cleaned_value)
    errors = list(parser.errors)
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
        if parser.paragraphs and any(not paragraph["bold"] for paragraph in parser.paragraphs):
            errors.append("każdy akapit musi zawierać co najmniej jedno wyróżnienie <b>")
        if any(re.search(r"[.!?,;:]$", heading["text"]) for heading in parser.headings):
            errors.append("nagłówki <h2> i <h3> nie mogą kończyć się znakiem interpunkcyjnym")
    if required_link:
        expected = _normalized_url(required_link)
        hrefs = [_normalized_url(href) for href in parser.hrefs if href]
        if hrefs != [expected]:
            errors.append("opis musi zawierać dokładnie jeden link z wymaganym adresem URL")
    return list(dict.fromkeys(errors))
