"""Czyste helpery do budowania wartości zapisywanych w Akeneo."""

from __future__ import annotations

import json
from typing import Dict, Iterable, List


def build_attribute_value(data: str, attribute: Dict, channel: str, locale: str) -> list[Dict]:
    """Buduje pojedynczą wartość zgodnie z konfiguracją scopable/localizable atrybutu."""
    return [
        {
            "data": data,
            "scope": channel if attribute.get("scopable") else None,
            "locale": locale if attribute.get("localizable") else None,
        }
    ]


def build_metatag_payload(
    meta_title: str,
    meta_description: str,
    *,
    title_attribute: Dict,
    description_attribute: Dict,
    channel: str,
    locale: str,
) -> Dict:
    """Buduje PATCH ograniczony wyłącznie do meta title i meta description."""
    title = str(meta_title or "").strip()
    description = str(meta_description or "").strip()
    if not title or not description:
        raise ValueError("meta title i meta description nie mogą być puste")
    return {
        "values": {
            "meta_title": build_attribute_value(title, title_attribute, channel, locale),
            "meta_description": build_attribute_value(
                description,
                description_attribute,
                channel,
                locale,
            ),
        }
    }


def build_metatag_product_update(
    identifier: str,
    meta_title: str,
    meta_description: str,
    *,
    title_attribute: Dict,
    description_attribute: Dict,
    channel: str,
    locale: str,
) -> Dict:
    """Buduje jedną linię kolekcji Akeneo bez pól mogących zmienić produkt poza SEO."""
    code = str(identifier or "").strip()
    if not code:
        raise ValueError("identifier produktu nie może być pusty")
    return {
        "identifier": code,
        **build_metatag_payload(
            meta_title,
            meta_description,
            title_attribute=title_attribute,
            description_attribute=description_attribute,
            channel=channel,
            locale=locale,
        ),
    }


def serialize_collection_updates(updates: Iterable[Dict]) -> str:
    """Serializuje maksymalnie 100 zasobów do formatu NDJSON wymaganego przez Akeneo."""
    rows = list(updates)
    if not rows:
        raise ValueError("kolekcja aktualizacji nie może być pusta")
    if len(rows) > 100:
        raise ValueError("Akeneo przyjmuje maksymalnie 100 zasobów w jednym żądaniu")
    return "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)


def parse_collection_response(value: str) -> List[Dict]:
    """Czyta odpowiedź NDJSON ze statusem każdej linii kolekcji."""
    results: List[Dict] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("niepoprawna odpowiedź kolekcji Akeneo")
        results.append(item)
    return results
