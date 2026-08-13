import unittest

from description_output import is_meta_only_result, is_reusable_result, validate_description_html


class DescriptionOutputTests(unittest.TestCase):
    def test_accepts_required_structure(self):
        paragraph = "Konkretny opis produktu oparty wyłącznie na przekazanych informacjach. " * 4
        html = f"<p>{paragraph}</p><h2>Pierwszy</h2><p>{paragraph}</p><h2>Drugi</h2><p>{paragraph}</p><h3>Finał</h3>"
        self.assertEqual(validate_description_html(html), [])

    def test_rejects_missing_heading_and_empty_output(self):
        self.assertTrue(validate_description_html(""))
        self.assertIn(
            "opis musi zawierać dokładnie dwa nagłówki <h2>",
            validate_description_html("<p>Wstęp.</p><h2>Jeden</h2><p>Opis.</p><h3>Finał</h3>"),
        )

    def test_requires_internal_link(self):
        self.assertIn(
            "brakuje wymaganego linku wewnętrznego",
            validate_description_html("<p>Gotowy opis.</p>", require_full_structure=False, required_link="https://example.com"),
        )

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
