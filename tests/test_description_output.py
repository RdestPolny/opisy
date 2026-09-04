import unittest

from description_output import is_meta_only_result, is_reusable_result, sanitize_html, validate_description_html


class DescriptionOutputTests(unittest.TestCase):
    def test_accepts_required_structure(self):
        paragraph = "Konkretny opis produktu oparty wyłącznie na przekazanych informacjach. " * 5
        html = (
            f"<p><b>Pełny tytuł książki</b> {paragraph} <b>ważna perspektywa autora</b></p>"
            f"<h2>Pierwszy</h2><p><b>główne zagadnienie publikacji</b> {paragraph} "
            f"<b>rzetelne przykłady</b></p><h2>Drugi</h2>"
            f"<p><b>praktyczna korzyść</b> {paragraph} <b>docelowa grupa odbiorców</b></p>"
        )
        self.assertEqual(validate_description_html(html), [])

    def test_rejects_missing_heading_and_empty_output(self):
        self.assertTrue(validate_description_html(""))
        self.assertIn(
            "opis musi zawierać co najmniej dwa śródtytuły (nagłówki <h2> lub <h3>)",
            validate_description_html("<p>Wstęp.</p><h2>Jeden</h2><p>Opis.</p>"),
        )

    def test_rejects_short_or_unbolded_paragraph_and_heading_punctuation(self):
        paragraph = "Długi konkretny opis produktu oparty na danych katalogowych. " * 6
        html = f"<p><b>Dobry wstęp</b> {paragraph}</p><h2>Nagłówek.</h2><p>{paragraph}</p><h2>Drugi</h2><p><b>Za krótko</b></p>"
        errors = validate_description_html(html)
        self.assertIn("każdy z głównych akapitów musi mieć co najmniej 180 znaków", errors)
        self.assertIn("każdy akapit musi zawierać co najmniej dwa merytoryczne wyróżnienia <b>", errors)
        self.assertIn("nagłówki <h2> i <h3> nie mogą kończyć się znakiem interpunkcyjnym", errors)

    def test_weak_generic_bold_phrase_is_soft_by_default(self):
        paragraph = "Konkretny opis produktu oparty wyłącznie na przekazanych informacjach. " * 5
        html = (
            f"<p><b>książka</b> {paragraph} <b>pełny tytuł publikacji</b></p>"
            f"<h2>Pierwszy</h2><p><b>główne zagadnienie</b> {paragraph} <b>istotny kontekst</b></p>"
            f"<h2>Drugi</h2><p><b>praktyczna korzyść</b> {paragraph} <b>grupa docelowa</b></p>"
        )
        self.assertNotIn(
            "pogrubienia muszą obejmować konkretne frazy, a nie ogólne pojedyncze słowa",
            validate_description_html(html),
        )
        self.assertIn(
            "pogrubienia muszą obejmować konkretne frazy, a nie ogólne pojedyncze słowa",
            validate_description_html(html, strict_bold_quality=True),
        )

    def test_long_specific_bold_phrase_is_not_automatically_weak(self):
        paragraph = "Konkretny opis produktu oparty wyłącznie na przekazanych informacjach. " * 5
        html = (
            f"<p><b>bardzo szczegółowy opis konkretnego problemu omawianego w tej publikacji</b> {paragraph} <b>pełny tytuł publikacji</b></p>"
            f"<h2>Pierwszy</h2><p><b>główne zagadnienie</b> {paragraph} <b>istotny kontekst</b></p>"
            f"<h2>Drugi</h2><p><b>praktyczna korzyść</b> {paragraph} <b>grupa docelowa</b></p>"
        )
        self.assertNotIn(
            "pogrubienia muszą obejmować konkretne frazy, a nie ogólne pojedyncze słowa",
            validate_description_html(html, strict_bold_quality=True),
        )

    def test_sanitize_html_cleans_spans_and_styles(self):
        dirty = '<p>Książka <b>Tytuł</b> <span style="font-family: inherit; color: red;">tekst w spanie</span> dalszy tekst</p>'
        cleaned = sanitize_html(dirty)
        self.assertNotIn("<span", cleaned)
        self.assertNotIn("style=", cleaned)
        self.assertIn("tekst w spanie", cleaned)
        self.assertEqual(validate_description_html(dirty, require_full_structure=False), [])

    def test_requires_internal_link(self):
        self.assertIn(
            "opis musi zawierać dokładnie jeden link z wymaganym adresem URL",
            validate_description_html("<p>Gotowy opis.</p>", require_full_structure=False, required_link="https://example.com"),
        )

    def test_requires_exact_href_and_rejects_extra_links(self):
        expected = "https://bookland.com.pl/ksiazki/kryminal"
        wrong = f'<p>{expected} <a href="https://www.idealne-dziecko-link">kryminał</a></p>'
        self.assertTrue(validate_description_html(wrong, require_full_structure=False, required_link=expected))
        correct = f'<p><a href="{expected}/">kryminał</a></p>'
        self.assertEqual(validate_description_html(correct, require_full_structure=False, required_link=expected), [])
        extra = correct.replace("</p>", '<a href="https://example.com">drugi</a></p>')
        self.assertTrue(validate_description_html(extra, require_full_structure=False, required_link=expected))

    def test_requires_link_in_selected_paragraph(self):
        expected = "https://bookland.com.pl/ksiazki/kryminal"
        second = (
            "<p>Pierwszy akapit.</p>"
            f'<p>Drugi <a href="{expected}">kryminał</a>.</p>'
            "<p>Trzeci akapit.</p>"
        )
        self.assertEqual(
            validate_description_html(
                second,
                require_full_structure=False,
                required_link=expected,
                required_link_paragraph=2,
            ),
            [],
        )
        first = (
            f'<p>Pierwszy <a href="{expected}">kryminał</a>.</p>'
            "<p>Drugi akapit.</p>"
            "<p>Trzeci akapit.</p>"
        )
        self.assertIn(
            "link wewnętrzny musi znajdować się w akapicie 2",
            validate_description_html(
                first,
                require_full_structure=False,
                required_link=expected,
                required_link_paragraph=2,
            ),
        )

    def test_requires_all_contributors_and_editor_role(self):
        contributors = ["Agnieszka Bień", "Grażyna Iwanowicz-Palus", "Artur Wdowiak"]
        html = (
            "<p>Redakcja naukowa: Agnieszka Bień, Grażyna Iwanowicz-Palus i Artur Wdowiak.</p>"
        )
        self.assertEqual(
            validate_description_html(
                html,
                require_full_structure=False,
                required_contributors=contributors,
                required_contributor_role="redakcja naukowa",
            ),
            [],
        )
        errors = validate_description_html(
            "<p>Autorzy: Agnieszka Bień i Artur Wdowiak.</p>",
            require_full_structure=False,
            required_contributors=contributors,
            required_contributor_role="redakcja naukowa",
        )
        self.assertIn("opis pomija twórców: Grażyna Iwanowicz-Palus", errors)
        self.assertIn("opis musi wskazywać, że wymienione osoby odpowiadają za redakcję naukową", errors)

    def test_description_wins_over_stale_meta_only_checkpoint_flag(self):
        result = {"description_html": "<p>Pełny opis</p>", "meta_only": True}
        self.assertFalse(is_meta_only_result(result))
        self.assertTrue(is_meta_only_result({"description_html": "", "meta_only": False}))

    def test_generators_accept_only_their_own_results(self):
        description = {"description_html": "<p>Opis</p>", "meta_title": "", "meta_description": ""}
        metatags = {"description_html": "", "meta_title": "Tytuł", "meta_description": "Opis meta"}
        self.assertTrue(is_reusable_result(description, meta_only=False))
        self.assertFalse(is_reusable_result(description, meta_only=True))
        self.assertTrue(is_reusable_result(metatags, meta_only=True))
        self.assertFalse(is_reusable_result(metatags, meta_only=False))


if __name__ == "__main__":
    unittest.main()
