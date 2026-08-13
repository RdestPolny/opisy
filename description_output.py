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
    plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()
    if not plain_text:
        return ["opis jest pusty"]

    parser = _DescriptionParser()
    parser.feed(value)
    errors = list(parser.errors)
    if parser.stack:
        errors.append("opis zawiera niedomknięte tagi")
    if require_full_structure:
        if len(plain_text) < 900:
            errors.append("opis jest za krótki (minimum 900 znaków tekstu)")
        if parser.tags.count("h2") != 2:
            errors.append("opis musi zawierać dokładnie dwa nagłówki <h2>")
        if len(parser.paragraphs) != 3:
            errors.append("opis musi zawierać dokładnie trzy akapity <p>")
        elif any(len(paragraph["text"]) < 220 for paragraph in parser.paragraphs):
            errors.append("każdy z trzech akapitów musi mieć co najmniej 220 znaków")
        if parser.paragraphs and any(not paragraph["bold"] for paragraph in parser.paragraphs):
            errors.append("każdy akapit musi zawierać co najmniej jedno wyróżnienie <b>")
        if any(re.search(r"[.!?,;:]$", heading["text"]) for heading in parser.headings):
            errors.append("nagłówki <h2> i <h3> nie mogą kończyć się znakiem interpunkcyjnym")
        if not re.search(r"<h3(?:\s[^>]*)?>.*?</h3>\s*$", value, flags=re.DOTALL | re.IGNORECASE):
            errors.append("ostatnim elementem musi być nagłówek <h3>")
    if required_link:
        expected = _normalized_url(required_link)
        hrefs = [_normalized_url(href) for href in parser.hrefs if href]
        if hrefs != [expected]:
            errors.append("opis musi zawierać dokładnie jeden link z wymaganym adresem URL")
    return list(dict.fromkeys(errors))
