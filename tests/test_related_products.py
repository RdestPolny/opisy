import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from internal_linking import select_internal_links
from related_products import (
    canonical_bookland_url,
    fetch_related_products,
    parse_product_page,
    last_segment_url,
    luigis_item_statuses,
    rank_related_products,
    same_work,
    title_slug_url,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "luigis_recommendations.json").read_text(encoding="utf-8"))
MZP = "9788381186841"
KULT = "9788381390989"


def hit(raw):
    return {"url": raw["url"], "type": "product", "attributes": raw["a"]}


class FakeLuigis:
    """Odtwarza /search (filtr lb_id) i /v1/recommend na podstawie zrzutu odpowiedzi."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def _response(self, payload):
        response = Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if self.fail:
            raise requests.ConnectionError("luigis down")
        lb_ids = [value.split(":", 1)[1] for key, value in params if key == "f[]" and value.startswith("lb_id:")]
        hits = [hit(FIXTURE[i]["s"]) for i in lb_ids if i in FIXTURE]
        return self._response({"results": {"hits": hits}})

    def post(self, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url, json))
        if "firecrawl" in url:
            raise AssertionError("Firecrawl nie powinien być wołany")
        if self.fail:
            raise requests.ConnectionError("luigis down")
        item_id = json[0]["item_ids"][0]
        hits = [hit(raw) for raw in FIXTURE[item_id]["r"]][: json[0]["size"]]
        return self._response([{"recommendation_type": "item_detail_alternatives", "hits": hits}])


class LuigisRetrievalTests(unittest.TestCase):
    def test_different_parts_and_grades_are_not_duplicate_editions(self):
        for a, b in (("Wielka saga przygodowa tom 1", "Wielka saga przygodowa tom 2"),
                     ("Wielka saga przygodowa tom I", "Wielka saga przygodowa tom II"),
                     ("Matematyka z kluczem. Klasa 5. Podręcznik", "Matematyka z kluczem. Klasa 6. Podręcznik")):
            with self.subTest(a=a, b=b):
                self.assertFalse(same_work(a, b))
        self.assertTrue(same_work("Wielka saga tom 1. Oprawa twarda 2024", "Wielka saga tom 1. Oprawa miękka 2025"))

    def test_textbook_gets_own_category_path_and_same_series_companions_first(self):
        session = FakeLuigis()
        related = fetch_related_products({"identifier": MZP, "title": "Matematyka z plusem 5"}, session=session)
        self.assertEqual(related["provider"], "luigisbox")
        self.assertEqual(related["categories"][0]["url"],
                         "https://bookland.com.pl/podreczniki-ogolne/szkolne/szkola-podstawowa-klasa-5/matematyka")
        self.assertEqual(related["categories"][0]["label"],
                         "Podręczniki szkolne > Szkolne > Szkoła Podstawowa Klasa 5 > Matematyka")
        self.assertNotIn("https://bookland.com.pl/podreczniki-ogolne",
                         [c["url"] for c in related["categories"]])
        self.assertEqual(len(related["products"]), 16)
        ranked = rank_related_products(related["source"], related["products"], exclude_ids=[MZP], limit=6)
        self.assertTrue(all("Matematyka z plusem" in item["title"] for item in ranked[:4]), [i["title"] for i in ranked])
        self.assertIn("ta sama seria: Matematyka z plusem", ranked[0]["relation"])
        recommend = [call for call in session.calls if call[0] == "POST"][0]
        self.assertEqual(recommend[2][0]["recommendation_type"], "item_detail_alternatives")

    def test_novel_prefers_same_author_and_drops_self_and_duplicate_editions(self):
        related = fetch_related_products({"identifier": KULT, "title": "Kult. Łukasz Orbitowski"}, session=FakeLuigis())
        ranked = rank_related_products(related["source"], related["products"], exclude_ids=[KULT],
                                       source_title="Kult. Łukasz Orbitowski", limit=6)
        titles = [item["title"] for item in ranked]
        self.assertEqual(set(titles[:3]), {"Wróg", "Inna dusza", "Chodź ze mną"})
        self.assertIn("ten sam autor: Łukasz Orbitowski", ranked[0]["relation"])
        all_ranked = rank_related_products(related["source"], related["products"], exclude_ids=[KULT],
                                           source_title="Kult. Łukasz Orbitowski", limit=50)
        all_titles = [item["title"] for item in all_ranked]
        self.assertEqual(sum(same_work(t, "Ślepnąc od świateł") for t in all_titles), 1)
        self.assertEqual(sum(same_work(t, "Informacja zwrotna") for t in all_titles), 1)
        self.assertNotIn("https://bookland.com.pl/kult-lukasz-orbitowski", [item["url"] for item in ranked])

    def test_canonical_url_comes_from_url_key_not_stale_category_web_url(self):
        related = fetch_related_products({"identifier": MZP}, session=FakeLuigis())
        ortograffiti = next(item for item in related["products"] if item["id"] == "9788378798804")
        self.assertEqual(ortograffiti["url"],
                         "https://bookland.com.pl/ortograffiti-matematyka-cwiczenia-klasa-7-czesc-1-szkola-podstawowa")

    def test_placeholder_attributes_do_not_create_relations(self):
        related = fetch_related_products({"identifier": KULT}, session=FakeLuigis())
        dawno = next(item for item in related["products"] if item["title"].startswith("Dawno temu"))
        self.assertEqual(dawno["subject"], "")
        self.assertEqual(dawno["level"], "")
        competitor = next(item for item in fetch_related_products({"identifier": MZP}, session=FakeLuigis())["products"]
                          if item["id"] == "9788302217036")
        self.assertEqual(competitor["authors"], [])

    def test_unknown_product_and_network_failures_return_warnings_not_exceptions(self):
        with patch("related_products.luigis_search_by_title", return_value=None):
            related = fetch_related_products({"identifier": "000", "title": ""}, session=FakeLuigis())
        self.assertEqual(related["products"], [])
        self.assertTrue(related["warnings"])
        related = fetch_related_products({"identifier": MZP}, session=FakeLuigis(fail=True))
        self.assertEqual(related["products"], [])
        self.assertIn("Luigi's Box niedostępny", related["warnings"][0])

    def test_shop_statuses_in_one_request(self):
        session = FakeLuigis()
        statuses = luigis_item_statuses([MZP, KULT, "0000000000000", MZP], session=session)
        self.assertEqual(statuses[MZP]["status"], "ok")
        self.assertTrue(statuses[MZP]["url"].startswith("https://bookland.com.pl/matematyka-z-plusem"))
        self.assertEqual(statuses["0000000000000"], {"status": "missing", "url": ""})
        self.assertEqual(len(session.calls), 1)
        lb_filters = [value for key, value in session.calls[0][2] if key == "f[]" and value.startswith("lb_id:")]
        self.assertEqual(len(lb_filters), 3)

    def test_shop_status_reads_stock_and_visibility(self):
        def statuses(**attrs):
            raw = {**FIXTURE[KULT]["s"], "a": {**FIXTURE[KULT]["s"]["a"], **attrs}}
            session = FakeLuigis()
            session.get = lambda url, params=None, timeout=None: session._response({"results": {"hits": [hit(raw)]}})
            return luigis_item_statuses([KULT], session=session)[KULT]["status"]

        self.assertEqual(statuses(), "ok")  # is_available=0 w indeksie nie oznacza braku towaru
        self.assertEqual(statuses(availability_boolean=["0"]), "out_of_stock")
        self.assertEqual(statuses(availability=1, availability_boolean=["1"]), "ok")
        self.assertEqual(statuses(widoczne_bookland=["0"]), "hidden")

    def test_title_slug_and_last_segment_urls(self):
        self.assertEqual(title_slug_url("Kult. Łukasz Orbitowski"), "https://bookland.com.pl/kult-lukasz-orbitowski")
        self.assertEqual(title_slug_url("Słownik Cytatów Z Biblii"), "https://bookland.com.pl/slownik-cytatow-z-biblii")
        self.assertEqual(title_slug_url("  "), "")
        self.assertEqual(last_segment_url("https://bookland.com.pl/ksiazki/literatura-piekna/wrog"),
                         "https://bookland.com.pl/wrog")
        self.assertEqual(last_segment_url("https://evil.example/ksiazki/wrog"), "")

    def test_url_guard(self):
        self.assertEqual(canonical_bookland_url("/wrog/"), "https://bookland.com.pl/wrog")
        for bad in ("https://evil.example/wrog", "http://bookland.com.pl/wrog", "/", "", "/a/../b", "/a b"):
            with self.subTest(bad=bad):
                self.assertEqual(canonical_bookland_url(bad), "")


RENDERED_PAGE = """
<html><body><main class="ProductPage">
<section class="Breadcrumbs"><ul class="Breadcrumbs-List">
<li class="Breadcrumb"><a class=" Breadcrumb-Link" href="/"><span class="Breadcrumb-Link-Name">Bookland.com.pl</span></a></li>
<li class="Breadcrumb"><a class=" Breadcrumb-Link" href="/ksiazki"><span class="Breadcrumb-Link-Name">Książki</span></a></li>
<li class="Breadcrumb"><a class=" Breadcrumb-Link" href="/ksiazki/literatura-piekna"><span class="Breadcrumb-Link-Name">Literatura piękna</span></a></li>
<li class="Breadcrumb"><a class=" Breadcrumb-Link" href="/ksiazki/literatura-piekna/literatura-piekna-polska"><span class="Breadcrumb-Link-Name">Literatura piękna polska</span></a></li>
<li class="Breadcrumb"><a class=" Breadcrumb-Link" href="/kult-lukasz-orbitowski"><span class="Breadcrumb-Link-Name">Kult. Łukasz Orbitowski</span></a></li>
</ul></section>
<h1>Kult. Łukasz Orbitowski</h1>
<div id="lb-recommend-element-item_detail_alternatives"><div class="lbx-reco-container"><h2 class="lbx-reco-title">Inni kupili również</h2>
<li data-lb-id="9788382891805" class="ProductCard"><a data-lb-id="9788382891805" href="https://bookland.com.pl/ksiazki/literatura-piekna/literatura-piekna-polska/dawno-temu-w-warszawie" class="ProductCard-Link">
<img src="x.jpg" alt="Dawno temu w Warszawie. Jakub Żulczyk"><p class="ProductCard-Name">Dawno temu w Warszawie. Jakub Żulczyk</p></a></li>
<li data-lb-id="9788368218275" class="ProductCard"><a data-lb-id="9788368218275" href="https://bookland.com.pl/wrog" class="ProductCard-Link">
<p class="ProductCard-Name">Wróg</p><span>40,41 zł</span></a></li>
</div></div>
<div class="Footer"><a href="https://bookland.com.pl/regulamin">Regulamin</a></div>
</main></body></html>
"""


class FirecrawlFallbackTests(unittest.TestCase):
    def test_parser_reads_reco_carousel_and_breadcrumbs_only(self):
        page = parse_product_page(RENDERED_PAGE)
        self.assertEqual(page["title"], "Kult. Łukasz Orbitowski")
        self.assertEqual([p["title"] for p in page["products"]], ["Dawno temu w Warszawie. Jakub Żulczyk", "Wróg"])
        self.assertEqual(page["products"][1]["id"], "9788368218275")
        self.assertNotIn("https://bookland.com.pl/regulamin", [p["url"] for p in page["products"]])
        self.assertEqual(page["breadcrumbs"][-2]["name"], "Literatura piękna polska")

    def test_firecrawl_is_used_only_when_luigis_returns_nothing(self):
        session = FakeLuigis(fail=True)
        firecrawl = Mock()
        firecrawl.json.return_value = {"success": True, "data": {"rawHtml": RENDERED_PAGE}}
        firecrawl.raise_for_status.return_value = None

        def post(url, **kwargs):
            if "firecrawl" in url:
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fc-key")
                self.assertEqual(kwargs["json"]["url"], "https://bookland.com.pl/kult-lukasz-orbitowski")
                return firecrawl
            raise requests.ConnectionError("luigis down")

        session.post = post
        related = fetch_related_products({"identifier": KULT}, firecrawl_api_key="fc-key",
                                         product_url="https://bookland.com.pl/kult-lukasz-orbitowski", session=session)
        self.assertEqual(related["provider"], "firecrawl")
        self.assertEqual([p["title"] for p in related["products"]], ["Dawno temu w Warszawie. Jakub Żulczyk", "Wróg"])
        self.assertEqual(related["categories"][0]["label"], "Książki > Literatura piękna > Literatura piękna polska")
        self.assertNotIn("fc-key", json.dumps(related))

    def test_no_firecrawl_without_key_or_url(self):
        related = fetch_related_products({"identifier": KULT}, session=FakeLuigis(fail=True))
        self.assertEqual(related["products"], [])
        related = fetch_related_products({"identifier": KULT}, firecrawl_api_key="k", session=FakeLuigis(fail=True))
        self.assertTrue(any("Firecrawl pominięty" in w for w in related["warnings"]))


def typed_response(values):
    answers = {}
    for index, (score, noul) in enumerate(values):
        answers[f"relevance_{index}"] = {"type": "score", "score": score, "confidence": 0.3}
        answers[f"support_{index}"] = {"type": "noul", "noul": noul}
    return {"answers": answers}


class JevIntegrationTests(unittest.TestCase):
    def run_selection(self, product, values, **config):
        def fetcher(product, **kwargs):
            return fetch_related_products(product, session=FakeLuigis(), **kwargs)

        with patch("internal_linking.requests.post") as post:
            post.return_value.json.return_value = typed_response(values)
            result = select_internal_links(product, {
                "api_key": "secret",
                "related_fetcher": fetcher, **config,
            })
        return result, post

    def test_all_related_candidates_go_to_jev_with_facts(self):
        product = {"identifier": MZP, "title": "Matematyka z plusem. Szkoła podstawowa klasa 5. Podręcznik",
                   "description": "Podręcznik do matematyki dla klasy 5.", "categories": ["mat5"]}
        values = [(2.6, .9), (2.0, .9)] + [(2.9, .95)] + [(2.7, .9)] * 14
        result, post = self.run_selection(product, values)
        body = post.call_args.kwargs["json"]
        candidates = body["state"]["candidates"]
        # 2 kategorie + 15 produktów: różna klasa nie jest innym wydaniem tego samego dzieła.
        self.assertEqual(len(candidates), 17)
        self.assertEqual(len(body["questions"]), 34)
        self.assertEqual(post.call_args.kwargs["timeout"], 30)
        self.assertEqual([c["kind"] for c in candidates[:2]], ["category", "category"])
        self.assertTrue(all(c["origin"] == "luigisbox" for c in candidates))
        self.assertIn("ta sama seria", candidates[2]["relation_to_source"])
        self.assertIn("seria: Matematyka z plusem", candidates[2]["details"])
        self.assertIn("recommender", body["questions"]["support_2"]["instructions"])
        self.assertIn("seria: Matematyka z plusem", body["state"]["source"]["bookland_facts"])
        kinds = {link["kind"]: link for link in result["links"]}
        self.assertEqual(kinds["category"]["name"], "Matematyka")
        self.assertIn(" > ", kinds["category"]["label"])
        self.assertEqual(kinds["category"]["url"],
                         "https://bookland.com.pl/podreczniki-ogolne/szkolne/szkola-podstawowa-klasa-5/matematyka")
        self.assertTrue(kinds["product"]["url"].startswith("https://bookland.com.pl/matematyka-z-plusem-"))
        self.assertIn("Matematyka z plusem", kinds["product"]["label"])
        self.assertIn("ta sama seria", kinds["product"]["relation"])
        self.assertEqual(result["related_source"]["provider"], "luigisbox")
        self.assertNotIn("secret", json.dumps(result))

    def test_rejected_products_use_highest_scored_fallback(self):
        product = {"identifier": KULT, "title": "Kult. Łukasz Orbitowski", "description": "Powieść.", "categories": []}
        result, _ = self.run_selection(product, [(2.0, .5)] * 15)
        self.assertEqual(len(result["links"]), 1)
        self.assertEqual(result["links"][0]["kind"], "product")
        self.assertTrue(result["links"][0]["fallback"])
        self.assertTrue(all(c["reason"] for c in result["candidates"] if c["score"] is not None))

    def test_no_related_candidates_means_no_sitemap_fallback(self):
        with patch("internal_linking.requests.post") as post:
            result = select_internal_links({"identifier": "000", "title": "Nieznany"}, {
                "api_key": "secret",
                "related_fetcher": lambda product, **kwargs: {"source": None, "products": [], "categories": [],
                                                              "warnings": ["brak"], "provider": "luigisbox"},
            })
        post.assert_not_called()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["links"], [])
        self.assertIn("brak", result["warnings"])

    def test_fetcher_crash_never_blocks_description(self):
        def boom(product, **kwargs):
            raise RuntimeError("secret boom")

        with patch("internal_linking.requests.post") as post:
            post.return_value.json.return_value = typed_response([])
            result = select_internal_links({"identifier": "1"}, {"api_key": "secret",
                                                                  "related_fetcher": boom})
        self.assertEqual(result["links"], [])
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
