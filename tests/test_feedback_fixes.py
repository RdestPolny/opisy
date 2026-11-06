"""Testy poprawek z feedbacku redakcji (inicjały, praca zbiorowa, linki, warianty, regeneracja)."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from description_generation import generate_description_result
from descriptions import (
    _normalize_description_links,
    build_system_prompt_full,
    editor_link_targets,
    pick_style_variant,
    style_variant_instructions,
)
from text_utils import _clean_option_slug, fix_collective_work_phrasing, is_collective_work
from test_internal_linking import related, source, typed_response
from internal_linking import select_internal_links

CAT = "https://bookland.com.pl/fantasy"
BODY = "<p>Pierwszy akapit.</p><h2>Treść</h2><p>Drugi akapit.</p><h2>Dla kogo</h2><p>Trzeci akapit.</p>"


class AuthorInitialsTests(unittest.TestCase):
    def test_slug_initials_keep_dots(self):
        self.assertEqual(_clean_option_slug("s_t_abby"), "S.T. Abby")
        self.assertEqual(_clean_option_slug("j_r_r_tolkien"), "J.R.R. Tolkien")
        self.assertEqual(_clean_option_slug("jan_kowalski"), "Jan Kowalski")


class CollectiveWorkTests(unittest.TestCase):
    def test_detects_collective_work(self):
        self.assertTrue(is_collective_work("Praca zbiorowa"))
        self.assertFalse(is_collective_work("Jan Kowalski"))

    def test_fixes_unnatural_phrase(self):
        text = "Kolorowa publikacja zatytułowana <b>Magiczne ścieżki. Wieś</b>, za której przygotowanie odpowiada <b>praca zbiorowa</b>, ukazała się"
        fixed = fix_collective_work_phrasing(text)
        self.assertIn("przygotowana w ramach <b>pracy zbiorowej</b>", fixed)
        self.assertNotIn("odpowiada", fixed)
        self.assertTrue(fixed.startswith("Kolorowa publikacja <b>Magiczne"))

    def test_prompt_forbids_collective_subject(self):
        self.assertIn("za której przygotowanie odpowiada praca zbiorowa", build_system_prompt_full())
        self.assertIn("pracy zbiorowej", build_system_prompt_full())


class RequiredLinkTests(unittest.TestCase):
    def test_manual_category_link_is_inserted_into_second_paragraph(self):
        html = _normalize_description_links(BODY, {"url": CAT, "category": "Fantasy"})
        second = html.split("<p>")[2]
        self.assertIn(f'<a href="{CAT}">Fantasy</a>', second)

    def test_existing_link_is_not_duplicated(self):
        body = BODY.replace("Drugi akapit.", f'Drugi <a href="{CAT}/">akapit</a>.')
        html = _normalize_description_links(body, {"url": CAT, "category": "Fantasy"})
        self.assertEqual(html.count("<a "), 1)

    def test_automatic_category_target_is_required(self):
        targets = [{"kind": "category", "url": CAT, "label": "Książki > Fantasy", "name": "Fantasy"}]
        html = _normalize_description_links(BODY, targets)
        self.assertIn("w kategorii <a", html)

    def test_category_scope_picks_single_category_even_below_threshold(self):
        with patch("internal_linking.requests.post") as post:
            post.return_value.json.return_value = typed_response([(1.8, .4), (2.0, .5)])
            result = select_internal_links(source(), {"api_key": "k", "scope": "category",
                                                      "related_fetcher": related(categories=2, products=3)})
        self.assertEqual(len(post.call_args.kwargs["json"]["state"]["candidates"]), 2)
        self.assertEqual([link["kind"] for link in result["links"]], ["category"])
        self.assertTrue(any("kategoria" in w.lower() for w in result["warnings"]))


class StyleVariantTests(unittest.TestCase):
    def test_variants_differ_between_products_and_on_regeneration(self):
        variants = {style_variant_instructions(pick_style_variant(f"sku{i}")) for i in range(12)}
        self.assertGreater(len(variants), 6)
        self.assertNotEqual(style_variant_instructions(pick_style_variant("a", "t", 0)),
                            style_variant_instructions(pick_style_variant("a", "t", 1)))


class RegenerationTests(unittest.TestCase):
    def test_editor_links_become_required_targets(self):
        html = f'<p>Zob. <a href="{CAT}">fantastyka</a> i <a href="https://bookland.com.pl/x">X</a></p>'
        targets = editor_link_targets(html, [{"kind": "category", "url": CAT, "label": "Fantasy", "name": "Fantasy"}])
        self.assertEqual(targets[0]["kind"], "category")
        self.assertEqual(targets[1], {"kind": "editor", "url": "https://bookland.com.pl/x", "label": "X", "required": True})

    def test_missing_headings_trigger_repair_attempt(self):
        no_headings = "<p>" + "Tekst opisu z <b>ważnym</b> i <b>drugim</b> pojęciem. " * 6 + "</p>"
        full = ("<p>" + "Tekst opisu z <b>ważnym</b> i <b>drugim</b> pojęciem. " * 5 + "</p><h2>Treść książki</h2>") * 2 + \
            "<h2>Dla kogo</h2><p>" + "Tekst opisu z <b>ważnym</b> i <b>drugim</b> pojęciem. " * 5 + "</p>"
        replies = iter([no_headings, full])
        calls = []

        def request(prompt):
            calls.append(prompt)
            return SimpleNamespace(text=next(replies), candidates=[SimpleNamespace(finish_reason="STOP")])

        result = generate_description_result(request, contents="x", context={}, normalize_response=lambda t: t,
                                             repair_warning_codes=("HEADING_COUNT",), sleep=lambda _: None)
        self.assertEqual(len(calls), 2)
        self.assertIn("śródtytuły", calls[1])
        self.assertNotIn("HEADING_COUNT", [w["code"] for w in result["validation_warnings"]])


if __name__ == "__main__":
    unittest.main()


class LiveTestRegressionTests(unittest.TestCase):
    """Przypadki z testu na 15 produktach (Mikołajek, Magiczne ścieżki. Pojazdy)."""

    def test_editor_field_with_collective_and_publisher_falls_back_to_authors(self):
        from akeneo import parse_akeneo_product

        values = {
            "name": [{"scope": None, "locale": None, "data": "Mikołajek. Mój tata jest najlepszy"}],
            "publisher": [{"scope": None, "locale": None, "data": "Znak Emotikon"}],
            "redakcja": [{"scope": None, "locale": None, "data": "Opracowanie zbiorowe, Znak Emotikon"}],
            "autor": [{"scope": None, "locale": None, "data": "René Goscinny, Jean-Jacques Sempé"}],
        }
        parsed = parse_akeneo_product({"identifier": "X", "values": values}, "Bookland", "pl_PL")
        self.assertEqual(parsed["contributors"], ["René Goscinny", "Jean-Jacques Sempé"])
        self.assertEqual(parsed["contributor_role"], "")

    def test_opracowanie_zbiorowe_is_collective(self):
        self.assertTrue(is_collective_work("Opracowanie zbiorowe"))
        fixed = fix_collective_work_phrasing("Książka X, za której redakcję naukową odpowiadają opracowanie zbiorowe, ukazała się")
        self.assertIn("przygotowana w ramach pracy zbiorowej", fixed)

    def test_publisher_adjective_is_declined(self):
        text = "ukazał się nakładem <b>Wydawnictwa Literackie</b>, a Wydawnictwa Dolnośląskiego nie ruszamy; Wydawnictwo Literackie wydało"
        fixed = fix_collective_work_phrasing(text)
        self.assertIn("<b>Wydawnictwa Literackiego</b>", fixed)
        self.assertIn("Wydawnictwa Dolnośląskiego nie", fixed)
        self.assertIn("Wydawnictwo Literackie wydało", fixed)

    def test_role_detection_uses_whole_words(self):
        from text_utils import detect_contributor_role

        self.assertEqual(detect_contributor_role("The author is credited with the series"), "")
        self.assertEqual(detect_contributor_role("Praca pod redakcją Jana Nowaka"), "redakcja naukowa")
