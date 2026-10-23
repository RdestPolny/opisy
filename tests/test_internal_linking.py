import json
import unittest
from unittest.mock import Mock, patch

import requests

from internal_linking import select_internal_links, verify_target_url

URL = "https://bookland.com.pl/cat.html"


def source(**fields):
    return {"identifier": "SRC", "title": "Matematyka 4", "description": "Oryginalny opis źródłowy.",
            "categories": ["cat"], "school": "Podstawowa", "grade": "4", "edition": "2025", **fields}


def related(categories=1, products=1):
    """Odpowiedź fetchera rekomendacji: kategorie ze ścieżki i produkty spod karty."""
    return lambda product, **kwargs: {
        "provider": "luigisbox", "warnings": [], "source": {"id": "SRC", "title": "Matematyka 4"},
        "categories": [{"url": f"https://bookland.com.pl/kategoria-{i}", "label": f"Kategoria {i}", "depth": 3}
                       for i in range(categories)],
        "products": [{"id": f"SKU{i}", "title": f"Ćwiczenia {i}", "url": f"https://bookland.com.pl/cwiczenia-{i}",
                      "position": i} for i in range(products)],
    }


def typed_response(values):
    answers = {}
    for index, (score, noul) in enumerate(values):
        answers[f"relevance_{index}"] = {"type": "score", "score": score, "confidence": 0.01}
        answers[f"support_{index}"] = {"type": "noul", "noul": noul}
    return {"answers": answers}


class InternalLinkingTests(unittest.TestCase):
    def select(self, values=(), fetcher=None, product=None, **config):
        with patch("internal_linking.requests.post") as post:
            post.return_value.json.return_value = typed_response(values)
            result = select_internal_links(product or source(), {
                "api_key": "secret", "related_fetcher": fetcher or related(), **config})
        return result, post

    def test_fallback_one_two_selection_and_low_score_confidence_does_not_reject(self):
        for values, count in (([(2.49, 1), (3, .79)], 1), ([(2.5, .8), (3, .79)], 2), ([(2.9, .95), (2.8, .9)], 2)):
            with self.subTest(count=count):
                result, post = self.select(values)
                self.assertEqual(len(result["links"]), count)
                self.assertEqual(sum(c["selected"] for c in result["candidates"]), count)
                self.assertEqual(result["thresholds"], {"score": 2.5, "noul": 0.8})
                self.assertNotIn("secret", json.dumps(result))
                post.assert_called_once()
                self.assertEqual(post.call_args.kwargs["timeout"], 30)

    def test_fallback_uses_score_then_support_and_is_reported(self):
        result, _ = self.select([(1, .9), (2.2, .1), (2.1, .99), (2.2, .3)],
                                related(categories=1, products=3))
        self.assertEqual([link["label"] for link in result["links"]], ["Ćwiczenia 2"])
        self.assertTrue(result["links"][0]["fallback"])
        self.assertTrue(result["warnings"])
        selected = next(c for c in result["candidates"] if c["selected"])
        self.assertIn("Wybór awaryjny", selected["reason"])

    def test_no_product_candidates_does_not_force_category(self):
        result, _ = self.select([(1, .2)], related(categories=1, products=0))
        self.assertEqual(result["links"], [])

    def test_source_includes_available_catalog_facts(self):
        product = source(author="Jan Kowalski", publisher="PWN", details="Podręcznik akademicki")
        _, post = self.select([(3, 1), (3, 1)], product=product)
        state = post.call_args.kwargs["json"]["state"]["source"]
        for key in ("author", "publisher", "details"):
            self.assertEqual(state[key], product[key])

    def test_single_best_of_each_kind_from_all_candidates(self):
        product = source(description="ą" * 10000)
        values = [(2.6, .9), (2.7, .9), (2.9, .9)] + [(2.6, .9), (2.95, .9)] + [(2.7, .9)] * 7
        result, post = self.select(values, related(categories=3, products=9), product=product)
        self.assertEqual([r["label"] for r in result["links"]], ["Kategoria 2", "Ćwiczenia 1"])
        body = post.call_args.kwargs["json"]
        self.assertEqual(len(body["state"]["candidates"]), 12)
        self.assertEqual(len(body["questions"]), 24)
        self.assertEqual(len(body["state"]["source"]["description"]), 6000)
        self.assertEqual(body["state"]["source"]["grade"], "4")
        self.assertIn("`candidates[0].label`", body["questions"]["support_0"]["instructions"])
        self.assertIn("subject and audience", body["questions"]["support_0"]["instructions"])
        self.assertIn("recommender", body["questions"]["support_3"]["instructions"])
        self.assertNotIn("source_skus", json.dumps(body))
        self.assertEqual(len(body["questions"]["relevance_0"]["criteria"]), 4)

    def test_no_candidates_means_no_request(self):
        result, post = self.select(fetcher=related(categories=0, products=0))
        self.assertEqual((result["links"], result["candidates"]), ([], []))
        post.assert_not_called()

    def test_missing_key_invalid_threshold_and_runtime_failures_fail_closed(self):
        result, post = self.select(api_key="")
        self.assertEqual(result["links"], [])
        self.assertTrue(result["warnings"])
        post.assert_not_called()
        for threshold in (.49, 1.01, True, "0.8", float("nan")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.select(threshold=threshold)
        for error in (requests.Timeout("secret request"), requests.HTTPError("secret response")):
            with patch("internal_linking.requests.post", side_effect=error) as post, patch("internal_linking.time.sleep"):
                result = select_internal_links(source(), {"api_key": "secret", "related_fetcher": related()})
                self.assertEqual(result["links"], [])
                self.assertNotIn("secret", json.dumps(result))
                self.assertEqual(post.call_count, 3 if isinstance(error, requests.Timeout) else 1)

    def test_retryable_http_failures_and_retry_after(self):
        for status in (429, 503, 529):
            with self.subTest(status=status):
                response = Mock(status_code=status, headers={"Retry-After": "2"})
                response.raise_for_status.side_effect = requests.HTTPError(response=response)
                success = Mock()
                success.json.return_value = typed_response([(3, 1), (3, 1)])
                with patch("internal_linking.requests.post", side_effect=[response, success]) as post, patch("internal_linking.time.sleep") as sleep:
                    result = select_internal_links(source(), {"api_key": "secret", "related_fetcher": related()})
                self.assertEqual(len(result["links"]), 2)
                self.assertEqual(post.call_count, 2)
                sleep.assert_called_once_with(2)

    def test_auth_failure_is_not_retried_or_leaked(self):
        response = Mock(status_code=401)
        response.raise_for_status.side_effect = requests.HTTPError("secret response", response=response)
        with patch("internal_linking.requests.post", return_value=response) as post:
            result = select_internal_links(source(), {"api_key": "secret", "related_fetcher": related()})
        post.assert_called_once()
        self.assertIn("HTTP 401", result["warnings"][0])
        self.assertNotIn("secret", json.dumps(result))

    def test_partial_malformed_or_out_of_range_answers_clear_all_links(self):
        payloads = [None, [], {"answers": {}}, typed_response([(3, 1)]), typed_response([(3, 1), (3, float("nan"))]),
                    typed_response([(3, 1), (4, .9)]), typed_response([(3, 1), (True, .9)])]
        for payload in payloads:
            with self.subTest(payload=payload), patch("internal_linking.requests.post") as post:
                post.return_value.json.return_value = payload
                result = select_internal_links(source(), {"api_key": "secret", "related_fetcher": related()})
                self.assertEqual(result["links"], [])
                self.assertTrue(result["warnings"])
                self.assertTrue(all(c["score"] is None for c in result["candidates"]))


class TargetVerificationTests(unittest.TestCase):
    def response(self, html="<html><head></head><body>Produkt</body></html>", **options):
        session = Mock()
        session.get.return_value = Mock(status_code=options.get("status", 200), text=html,
                                       headers=options.get("headers", {"Content-Type": "text/html; charset=utf-8"}))
        return session

    def test_accepts_html_matching_relative_canonical_and_trailing_slash(self):
        session = self.response('<head><link rel="canonical" href="/cat.html/"></head>')
        self.assertIsNone(verify_target_url(URL, session=session))
        session.get.assert_called_once_with(URL, timeout=10, allow_redirects=False)

    def test_rejects_redirect_noindex_other_canonical_and_nonhtml(self):
        sessions = [self.response(status=301), self.response(status=404),
                    self.response('<head><meta name="ROBOTS" content="follow, noindex"></head>'),
                    self.response('<head><meta name="googlebot" content="noindex"></head>'),
                    self.response('<head><meta name="robots" content="none"></head>'),
                    self.response(headers={"X-Robots-Tag": "googlebot: noindex"}),
                    self.response('<head><link rel="canonical" href="/other.html"></head>'),
                    self.response('<head><link rel="canonical" href="https://evil.example/cat"></head>'),
                    self.response('<head><link rel="canonical" href=""></head>'),
                    self.response(headers={"Content-Type": "application/pdf"})]
        for session in sessions:
            with self.subTest(session=session), self.assertRaises(ValueError):
                verify_target_url(URL, session=session)
            session.get.assert_called_once()

    def test_url_guard_runs_before_network_and_network_errors_are_safe(self):
        session = self.response()
        with self.assertRaises(ValueError):
            verify_target_url("https://evil.example", session=session)
        session.get.assert_not_called()
        with self.assertRaises(ValueError):
            verify_target_url(123, session=session)
        session.get.side_effect = requests.Timeout("Authorization secret")
        with self.assertRaisesRegex(ValueError, "Nie udało") as caught:
            verify_target_url(URL, session=session)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
