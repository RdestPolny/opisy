import unittest
from html.parser import HTMLParser
from unittest.mock import Mock

from description_output import (
    analyze_description_html, deliver_description_result, is_meta_only_result,
    is_reusable_result, preserve_description_on_failure, refresh_description_result,
    sanitize_html, validate_description_html,
    current_description_value,
)


LINK = "https://bookland.com.pl/ksiazki/kryminal"


def full_description(*, link_paragraph=2, missing_bold=False):
    text = "Konkretny opis produktu oparty wyłącznie na przekazanych informacjach. " * 5
    parts = []
    for index in range(1, 4):
        if index > 1:
            parts.append(f"<h2>Zagadnienie {index}</h2>")
        second_bold = "" if missing_bold and index == 2 else "<b>rzetelne przykłady</b>"
        link = f'<a href="{LINK}">kryminał</a>' if index == link_paragraph else ""
        parts.append(f"<p><b>ważne zagadnienie</b> {text} {second_bold} {link}</p>")
    return "".join(parts)


class DescriptionOutputTests(unittest.TestCase):
    def codes(self, report):
        return {issue["code"] for issue in report.warnings}

    def test_required_structure_is_clean(self):
        report = analyze_description_html(full_description(), required_link=LINK, required_link_paragraph=2)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_short_unbolded_description_is_deliverable_with_warnings(self):
        value = "<p>Wstęp.</p><h2>Nagłówek.</h2><p>Opis.</p>"
        report = analyze_description_html(value)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.clean_html, value)
        self.assertTrue({"DESCRIPTION_LENGTH", "HEADING_COUNT", "PARAGRAPH_COUNT", "PARAGRAPH_LENGTH", "BOLD_COUNT", "HEADING_PUNCTUATION"} <= self.codes(report))

    def test_missing_bold_and_wrong_link_position_are_only_warnings(self):
        value = full_description(link_paragraph=1, missing_bold=True)
        report = analyze_description_html(value, required_link=LINK, required_link_paragraph=2)
        self.assertEqual(report.errors, [])
        self.assertEqual(self.codes(report), {"BOLD_COUNT", "LINK_POSITION"})
        self.assertEqual(validate_description_html(value, required_link=LINK, required_link_paragraph=2), [])

    def test_missing_wrong_or_extra_links_are_diagnosed(self):
        for value, code in [
            ("<p>Opis.</p>", "LINK_MISSING"),
            ('<p><a href="https://example.com">Opis.</a></p>', "LINK_TARGET_MISMATCH"),
            (f'<p><a href="{LINK}">Opis.</a><a href="https://example.com">Inny</a></p>', "LINK_COUNT"),
        ]:
            with self.subTest(code=code):
                report = analyze_description_html(value, require_full_structure=False, required_link=LINK)
                self.assertEqual(report.errors, [])
                self.assertIn(code, self.codes(report))

    def test_url_normalization_accepts_trailing_slash(self):
        report = analyze_description_html(f'<p><a href="{LINK}/">Opis</a></p>', require_full_structure=False, required_link=LINK)
        self.assertEqual(report.warnings, [])

    def test_inflected_author_is_uncertain_not_blocked(self):
        report = analyze_description_html("<p>Publikacja Jana Kowalskiego.</p>", require_full_structure=False, required_contributors=["Jan Kowalski"])
        self.assertEqual(report.errors, [])
        self.assertIn("CONTRIBUTOR_UNVERIFIED", self.codes(report))

    def test_singular_scientific_editor_is_recognized(self):
        report = analyze_description_html("<p>Redaktor naukowy: Jan Kowalski.</p>", require_full_structure=False, required_contributors=["Jan Kowalski"], required_contributor_role="redakcja naukowa")
        self.assertEqual(report.warnings, [])

    def test_missing_contributors_and_role_are_warnings(self):
        report = analyze_description_html("<p>Opis książki.</p>", require_full_structure=False, required_contributors=["Agnieszka Bień", "Artur Wdowiak"], required_contributor_role="redakcja naukowa")
        self.assertEqual(report.errors, [])
        self.assertEqual(self.codes(report), {"CONTRIBUTOR_UNVERIFIED", "ROLE_UNVERIFIED"})

    def test_generic_bold_quality_is_optional_diagnostic(self):
        value = full_description().replace("ważne zagadnienie", "książka")
        self.assertNotIn("BOLD_QUALITY", self.codes(analyze_description_html(value)))
        report = analyze_description_html(value, strict_bold_quality=True)
        self.assertEqual(report.errors, [])
        self.assertIn("BOLD_QUALITY", self.codes(report))

    def test_unknown_validator_options_and_bad_config_fail_before_generation(self):
        for options in [{"typo": True}, {"required_link": "javascript:void(0)"}, {"required_link": "https://["}, {"required_link_paragraph": -1}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                analyze_description_html("<p>Opis.</p>", **options)

    def test_empty_or_only_active_content_is_not_deliverable(self):
        for value in ["", "<p>&nbsp;</p>", "<script>void(0)</script>", "<p><img src=x onerror='void(0)'></p>"]:
            with self.subTest(value=value):
                self.assertTrue(analyze_description_html(value).errors)

    def test_sanitizer_preserves_safe_text_and_normalizes_formatting(self):
        value = '<div>Książka <strong>Tytuł</strong> <span style="color:red">tekst</span> <em>dalej</em></div>'
        self.assertEqual(sanitize_html(value), '<p>Książka <b>Tytuł</b> tekst dalej</p>')

    def test_malformed_html_is_repaired_locally(self):
        report = analyze_description_html("<div><div>Treść<b>opis</div></div>", require_full_structure=False)
        self.assertEqual(report.errors, [])
        self.assertIn("Treść", report.clean_html)
        self.assertEqual(sanitize_html(report.clean_html), report.clean_html)

    def test_unsafe_urls_are_removed_including_encoded_protocols(self):
        for href in ["javascript:void(0)", "jav&#x61;script:void(0)", "java&#10;script:void(0)", "data:text/html,test", "//example.com", "/relative"]:
            with self.subTest(href=href):
                cleaned = sanitize_html(f'<p><a href="{href}">Zachowany tekst</a></p>')
                self.assertNotIn("href=", cleaned)
                self.assertIn("Zachowany tekst", cleaned)

    def test_attribute_reconstruction_cannot_inject_events(self):
        value = "<p><a href='https://example.com/\" onmouseover=\"void(0)'>tekst</a></p>"
        cleaned = sanitize_html(value)
        class Attributes(HTMLParser):
            attrs = []
            def handle_starttag(self, tag, attrs):
                self.attrs.extend(attrs)
        parser = Attributes()
        parser.feed(cleaned)
        self.assertEqual([name for name, _ in parser.attrs], ["href"])
        self.assertIn("&quot;", cleaned)
        self.assertEqual(sanitize_html(cleaned), cleaned)

    def test_active_content_and_attributes_are_removed(self):
        value = '<p onclick="void(0)">Treść<script>BAD</script><svg onload="void(0)"><text>BAD</text></svg><iframe>BAD</iframe></p>'
        self.assertEqual(sanitize_html(value), "<p>Treść</p>")

    def test_warning_description_is_reusable_and_delivered_in_saved_context(self):
        value = full_description(link_paragraph=1, missing_bold=True)
        result = {"sku": "SKU1", "description_html": value, "required_link": LINK, "required_link_paragraph": 2, "channel": "saved-channel", "locale": "pl_PL"}
        send = Mock()
        report = deliver_description_result(result, value, send, channel="other", locale="en_US")
        send.assert_called_once_with("SKU1", report.clean_html, "saved-channel", "pl_PL")
        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertTrue(is_reusable_result(result, meta_only=False))

    def test_delivery_uses_exact_sanitized_value_and_rejects_empty(self):
        send = Mock()
        result = {"sku": "SKU1"}
        report = deliver_description_result(result, '<p onmouseover="void(0)">Treść</p>', send, channel="Bookland", locale="pl_PL")
        self.assertEqual(send.call_args.args[1], report.clean_html)
        self.assertEqual(result["description_html"], report.clean_html)
        with self.assertRaises(ValueError):
            deliver_description_result(result, "", send, channel="Bookland", locale="pl_PL")
        self.assertEqual(send.call_count, 1)

    def test_link_only_uses_same_context_even_for_old_saved_results(self):
        result = {"sku": "SKU1", "link_only": True, "required_link": LINK, "required_link_paragraph": 2, "required_contributors": ["Jan Kowalski"], "required_contributor_role": "redakcja naukowa"}
        report = deliver_description_result(result, f'<p>Opis <a href="{LINK}">kategorii</a>.</p>', Mock(), channel="Bookland", locale="pl_PL")
        self.assertEqual(report.warnings, [])

    def test_edit_replaces_stale_warnings_and_recovers_from_empty(self):
        result = {}
        refresh_description_result(result, "<p>Krótko</p>")
        self.assertTrue(result["validation_warnings"])
        refresh_description_result(result, "")
        self.assertTrue(result["error"])
        refresh_description_result(result, full_description())
        self.assertIsNone(result["error"])
        self.assertEqual(result["validation_warnings"], [])

    def test_failed_regeneration_keeps_previous_text_and_is_exportable(self):
        previous = {"sku": "SKU1", "description_html": full_description(), "error": None}
        result = preserve_description_on_failure(previous, {"description_html": "", "error": "timeout"})
        self.assertEqual(result["description_html"], previous["description_html"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["validation_warnings"][0]["code"], "REGENERATION_FAILED")
        self.assertNotIn("generation_warnings", previous)
        self.assertTrue(is_reusable_result(result, meta_only=False))

    def test_successful_regeneration_replaces_previous_text(self):
        replacement = {"description_html": "<p>Nowa treść</p>", "error": None}
        self.assertEqual(preserve_description_on_failure({"description_html": "<p>Stara</p>"}, replacement), replacement)

    def test_export_and_delivery_see_component_edit_before_preview_renders(self):
        result = {"sku": "SKU1", "editor_seed": "seed", "description_html": "<p>Stary</p>"}
        state = {"edit_SKU1": "<p>Stary</p>", "visual_editor_SKU1_seed": {"html": "<p>Najnowszy</p>"}}
        self.assertEqual(current_description_value(result, state), "<p>Najnowszy</p>")
        state["visual_editor_SKU1_seed"]["html"] = ""
        self.assertEqual(current_description_value(result, state), "")

    def test_new_generation_does_not_restore_previous_editor_text(self):
        result = {"sku": "SKU1", "description_html": "<p>Nowa generacja</p>"}
        state = {"edit_SKU1": "<p>Poprzednia edycja</p>", "visual_editor_SKU1_old": {"html": "<p>Stary</p>"}}
        self.assertEqual(current_description_value(result, state), "<p>Nowa generacja</p>")

    def test_description_wins_over_stale_meta_flag(self):
        self.assertFalse(is_meta_only_result({"description_html": "<p>Opis</p>", "meta_only": True}))

    def test_generators_reuse_only_own_successful_results(self):
        description = {"description_html": "<p>Opis</p>"}
        metadata = {"meta_title": "Tytuł", "meta_description": "Opis meta"}
        self.assertTrue(is_reusable_result(description, meta_only=False))
        self.assertFalse(is_reusable_result(description, meta_only=True))
        self.assertTrue(is_reusable_result(metadata, meta_only=True))
        self.assertFalse(is_reusable_result(metadata, meta_only=False))
        self.assertFalse(is_reusable_result({**description, "error": "timeout"}, meta_only=False))


if __name__ == "__main__":
    unittest.main()
