import re
from html.parser import HTMLParser
from typing import List


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

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag)
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"niedozwolony tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"niepoprawnie domknięty tag </{tag}>")


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
        if len(plain_text) < 600:
            errors.append("opis jest za krótki (minimum 600 znaków tekstu)")
        if parser.tags.count("h2") != 2:
            errors.append("opis musi zawierać dokładnie dwa nagłówki <h2>")
        if parser.tags.count("p") < 3:
            errors.append("opis musi zawierać co najmniej trzy akapity <p>")
        if not re.search(r"<h3(?:\s[^>]*)?>.*?</h3>\s*$", value, flags=re.DOTALL | re.IGNORECASE):
            errors.append("ostatnim elementem musi być nagłówek <h3>")
    if required_link and required_link not in value:
        errors.append("brakuje wymaganego linku wewnętrznego")
    return list(dict.fromkeys(errors))
