import json
import unittest
from unittest.mock import Mock, patch

import requests

from internal_linking import select_internal_links, validate_targets, verify_target_url


def target(code="cat", kind="category", **fields):
    return {"kind": kind, "code": code, "label": "Matematyka — klasa 4",
            "url": f"https://bookland.com.pl/{code}.html", **fields}


def source(**fields):
    return {"identifier": "SRC", "title": "Matematyka 4", "description": "Oryginalny opis źródłowy.",
            "categories": ["cat"], "school": "Podstawowa", "grade": "4", "edition": "2025", **fields}


def typed_response(values):
    answers = {}
    for index, (score, noul) in enumerate(values):
        answers[f"relevance_{index}"] = {"type": "score", "score": score, "confidence": 0.01}
        answers[f"support_{index}"] = {"type": "noul", "noul": noul}
    return {"answers": answers}


class InternalLinkingTests(unittest.TestCase):
    def select(self, rows, values=(), product=None, **config):
        with patch("internal_linking.requests.post") as post:
            post.return_value.json.return_value = typed_response(values)
            result = select_internal_links(product or source(), {"targets": rows, "api_key": "secret", **config})
        return result, post

    def test_registry_rejects_attacks_and_noncanonical_urls(self):
        urls = ["https://evil.example/cat", "https://bookland.com.pl.evil.example/cat",
                "https://user:secret@bookland.com.pl/cat", "https://bookland.com.pl:444/cat",
                "https://bookland.com.pl:bad/cat", "https://bookland.com.pl/cat?q=1",
                "https://bookland.com.pl/cat#part", "https://bookland.com.pl/cat?",
                " https://bookland.com.pl/cat", "https://bookland.com.pl/c at",
                "https://bookland.com.pl/c\nat", "https://bookland.com.pl/c%0aat",
                "https://bookland.com.pl/%2e%2e/cat", "https://bookland.com.pl\\@evil.example/cat",
                "ftp://bookland.com.pl/cat", "https://bookland.com.pl/%ZZ",
                "https://bookland.com.pl/c\u0080at", "https://bookland.com.pl/c\u200bat"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_targets([target(url=url)])
        self.assertEqual(validate_targets([target(kind="")])[0]["kind"], "category")
        self.assertEqual(len(validate_targets([target(url="https://www.bookland.com.pl/cat")])), 1)

    def test_registry_rejects_missing_fields_duplicates_and_excludes_unknown_metadata(self):
        for field in ("code", "label", "url"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_targets([target(**{field: ""})])
        for rows in ([target(), target(url="https://bookland.com.pl/other")],
                     [target(), target(code="other", url=target()["url"] + "/")]):
            with self.assertRaises(ValueError):
                validate_targets(rows)
        self.assertNotIn("api_key", validate_targets([target(api_key="never-report")])[0])
        with self.assertRaises(ValueError):
            validate_targets([target(grade=4)])

    def test_category_requires_exact_membership_and_all_supplied_constraints(self):
        for product in (source(categories=[]), source(categories="cat"), source(categories=["CAT"]),
                        source(school=""), source(school="Średnia"), source(grade="")):
            with self.subTest(product=product):
                result, post = self.select([target(school="podstawowa", grade="4")], product=product)
                self.assertEqual(result["links"], [])
                post.assert_not_called()
        result, _ = self.select([target(school=" podstawowa ", grade="4")], [(3, 0.9)])
        self.assertEqual(len(result["links"]), 1)

    def test_product_requires_explicit_source_mapping_edition_and_not_self(self):
        rows = [target("OTHER", "product", edition="2025", source_skus="NOT_SRC"),
                target("SRC", "product", edition="2025", source_skus="SRC"),
                target("NO_EDITION", "product", source_skus="SRC"),
                target("OLD_EDITION", "product", edition="2024", source_skus="SRC"),
                target("UNKNOWN_SCHOOL", "product", edition="2025", source_skus="SRC", school="Średnia")]
        result, post = self.select(rows)
        self.assertEqual(result["links"], [])
        post.assert_not_called()
        result, post = self.select([target("DEST", "product", edition="2025", source_skus="SRC")], product=source(edition=""))
        self.assertEqual(result["links"], [])
        post.assert_not_called()
        result, _ = self.select([target("DEST", "product", edition="2025", source_skus="OTHER | SRC")], [(2.8, .9)])
        self.assertEqual(result["links"][0]["kind"], "product")

    def test_zero_one_two_selection_and_low_score_confidence_does_not_reject(self):
        rows = [target(), target("DEST", "product", edition="2025", source_skus="SRC")]
        for values, count in (([(2.49, 1), (3, .79)], 0), ([(2.5, .8), (3, .79)], 1), ([(2.9, .95), (2.8, .9)], 2)):
            with self.subTest(count=count):
                result, post = self.select(rows, values)
                self.assertEqual(len(result["links"]), count)
                self.assertEqual(sum(c["selected"] for c in result["candidates"]), count)
                self.assertNotIn("secret", json.dumps(result))
                post.assert_called_once()
                self.assertEqual(post.call_args.kwargs["timeout"], 15)

    def test_single_best_of_each_kind_and_bounded_request(self):
        categories = [target(f"CAT{i}") for i in range(10)]
        products = [target(f"SKU{i}", "product", edition="2025", source_skus="SRC") for i in range(10)]
        product = source(categories=[r["code"] for r in categories], description="ą" * 10000)
        result, post = self.select(categories + products, [(2.6, .9), (2.7, .9), (2.9, .9), (2.8, .9)] * 2, product=product)
        self.assertEqual([r["code"] for r in result["links"]], ["CAT2", "SKU2"])
        body = post.call_args.kwargs["json"]
        self.assertEqual(len(body["state"]["candidates"]), 8)
        self.assertEqual(len(body["questions"]), 16)
        self.assertEqual(len(body["state"]["source"]["description"]), 6000)
        self.assertIn("`candidates[0].label`", body["questions"]["support_0"]["instructions"])
        self.assertIn("subject and audience", body["questions"]["support_0"]["instructions"])
        self.assertNotIn("source_skus", body["questions"]["support_0"]["instructions"])
        self.assertNotIn("edition", body["questions"]["support_0"]["instructions"])
        self.assertIn("relevant companion material", body["questions"]["support_4"]["instructions"])
        self.assertTrue(body["state"]["candidates"][4]["source_association_confirmed"])
        self.assertEqual(len(body["questions"]["relevance_0"]["criteria"]), 4)

    def test_missing_key_invalid_threshold_and_runtime_failures_fail_closed(self):
        result, post = self.select([target()], api_key="")
        self.assertEqual(result["links"], [])
        self.assertTrue(result["warnings"])
        post.assert_not_called()
        for threshold in (.49, 1.01, True, "0.8", float("nan")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.select([target()], threshold=threshold)
        for error in (requests.Timeout("secret request"), requests.HTTPError("secret response")):
            with patch("internal_linking.requests.post", side_effect=error) as post:
                result = select_internal_links(source(), {"targets": [target()], "api_key": "secret"})
                self.assertEqual(result["links"], [])
                self.assertNotIn("secret", json.dumps(result))
                post.assert_called_once()

    def test_partial_malformed_or_out_of_range_answers_clear_all_links(self):
        rows = [target(), target("DEST", "product", edition="2025", source_skus="SRC")]
        payloads = [None, [], {"answers": {}}, typed_response([(3, 1)]), typed_response([(3, 1), (3, float("nan"))]),
                    typed_response([(3, 1), (4, .9)]), typed_response([(3, 1), (True, .9)])]
        for payload in payloads:
            with self.subTest(payload=payload), patch("internal_linking.requests.post") as post:
                post.return_value.json.return_value = payload
                result = select_internal_links(source(), {"targets": rows, "api_key": "secret"})
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
        self.assertIsNone(verify_target_url(target()["url"], session=session))
        session.get.assert_called_once_with(target()["url"], timeout=10, allow_redirects=False)

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
                verify_target_url(target()["url"], session=session)
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
            verify_target_url(target()["url"], session=session)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
