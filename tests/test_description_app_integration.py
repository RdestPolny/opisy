"""Run the real description adapters without executing Streamlit or opening a DB."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import akeneo
import descriptions
from description_output import deliver_description_result
from internal_linking import TRUSTED_ORIGINS
from test_description_output import LINK, full_description


class DescriptionAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.product = {"title": "Przykładowa książka", "description": "Źródło produktu", "contributors": ["Jan Kowalski"], "contributor_role": "", "author": "Jan Kowalski"}
        self.sdk = Mock(return_value=SimpleNamespace(text=full_description(link_paragraph=1, missing_bold=True)))
        fakes = {
            descriptions: {
                "GEMINI_MODEL": "existing-configured-model",
                "DESCRIPTION_PROMPT_VERSION": "test-version",
                "get_gemini_client": lambda: SimpleNamespace(models=SimpleNamespace(generate_content=self.sdk)),
                "akeneo_get_product_details": lambda *args: self.product,
                "validate_description_quality": lambda value: ("ok", "Opis OK"),
                "generate_product_url": lambda title: "https://example.com/product",
                "select_internal_links": Mock(return_value={"links": [], "warnings": [], "candidates": []}),
                "check_internal_link_url": Mock(),
                "TRUSTED_ORIGINS": TRUSTED_ORIGINS,
            },
            akeneo: {
                "akeneo_get_option_label": lambda code, value, *args: {"sp": "szkoła podstawowa", "ang": "język angielski"}.get(value, value),
                "akeneo_is_select_attribute": lambda code: code in {"school_type", "subject"},
            },
        }
        for module, attributes in fakes.items():
            patcher = patch.multiple(module, **attributes)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = {
            "process_product_from_akeneo": descriptions.process_product_from_akeneo,
            "select_internal_links": descriptions.select_internal_links,
            "check_internal_link_url": descriptions.check_internal_link_url,
            "parse_akeneo_product": akeneo.parse_akeneo_product,
            "_prepare_product_data": akeneo._prepare_product_data,
        }

    def test_generation_to_delivery_preserves_model_html_warnings_and_context(self):
        result = self.app["process_product_from_akeneo"]("SKU1", "fake-token", "Bookland", "pl_PL", internal_link={"url": LINK, "category": "kryminał"}, use_research=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertEqual(self.sdk.call_count, 1)
        self.assertEqual(self.sdk.call_args.kwargs["model"], "existing-configured-model")
        self.assertEqual(self.sdk.call_args.kwargs["config"].http_options.retry_options.attempts, 1)
        self.assertEqual(result["model"], "existing-configured-model")
        self.assertEqual(result["api_attempts"], 1)
        send = Mock()
        deliver_description_result(result, result["description_html"], send, channel="other-channel", locale="en_US")
        send.assert_called_once_with("SKU1", full_description(link_paragraph=1, missing_bold=True), "Bookland", "pl_PL")
        self.assertTrue(result["validation_warnings"])

    def test_link_only_generation_and_delivery_do_not_require_contributors(self):
        value = f'<p>Opis <a href="{LINK}">kategorii</a>.</p>'
        self.sdk.return_value = SimpleNamespace(text=value)
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL", internal_link={"url": LINK, "category": "kryminał"}, link_only=True, use_research=False)
        self.assertEqual(result["required_contributors"], [])
        self.assertIsNone(result["error"])
        report = deliver_description_result(result, value, Mock(), channel="Bookland", locale="pl_PL")
        self.assertEqual(report.warnings, [])

    def test_prompt_gets_category_name_not_breadcrumb_as_anchor(self):
        targets = [{"kind": "category", "url": LINK, "label": "Książki > Fantastyka i horror > Fantasy", "name": "Fantasy"},
                   {"kind": "product", "url": LINK + "-2", "label": "Dwie wieże", "relation": "ten sam autor: Tolkien"}]
        prompt = descriptions.description_link_instructions(targets)
        payload = json.loads(prompt.split("Cele: ", 1)[1])
        self.assertEqual(payload[0], {"url": LINK, "kind": "category", "nazwa_kategorii": "Fantasy",
                                      "sciezka": "Książki > Fantastyka i horror > Fantasy"})
        self.assertEqual(payload[1]["label"], "Dwie wieże")
        self.assertIn("Nigdy nie wstawiaj do tekstu całej ścieżki kategorii", prompt)
        manual = json.loads(descriptions.description_link_instructions({"url": LINK, "category": "kryminał"}).split("Cele: ", 1)[1])
        self.assertEqual(manual, [{"url": LINK, "kind": "category", "nazwa_kategorii": "kryminał"}])

    def test_external_links_from_model_are_stripped_without_linking(self):
        planted = full_description(link_paragraph=0).replace("</p>", ' <a href="https://evil.example/x">tu</a></p>', 1)
        self.sdk.return_value = SimpleNamespace(text=planted)
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL", use_research=False)
        self.assertIsNone(result["error"])
        self.assertNotIn("evil.example", result["description_html"])
        self.assertEqual(result["allowed_links"], [])

    def test_link_only_keeps_links_from_source_description(self):
        old = "https://bookland.com.pl/istniejacy"
        self.product["description"] = f'<p>Stary opis <a href="{old}">z linkiem</a>.</p>'
        value = f'<p>Stary opis <a href="{old}">z linkiem</a> i <a href="{LINK}">kategorią</a>.</p>'
        self.sdk.return_value = SimpleNamespace(text=value)
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL",
                    internal_link={"url": LINK, "category": "kryminał"}, link_only=True, use_research=False)
        self.assertIn(old, result["description_html"])
        self.assertIn(LINK, result["description_html"])

    def test_text_attributes_are_not_rewritten_as_option_codes(self):
        values = {"publisher": [{"data": "WSiP-Edukacja", "scope": None, "locale": None}],
                  "school_type": [{"data": "sp", "scope": "Bookland", "locale": "pl_PL"}]}
        parsed = self.app["parse_akeneo_product"]({"identifier": "SKU1", "values": values}, "Bookland", "pl_PL", "fake-token")
        self.assertEqual(parsed["publisher"], "WSiP-Edukacja")
        self.assertEqual(parsed["school"], "szkoła podstawowa")

    def test_api_failure_is_not_stored_as_html(self):
        self.sdk.side_effect = ValueError("invalid request")
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL", use_research=False)
        self.assertTrue(result["error"])
        self.assertEqual(result["description_html"], "")
        self.assertEqual(self.sdk.call_count, 1)

    def test_real_akeneo_parser_preserves_linking_facts_and_resolves_options(self):
        values = {name: [{"data": data, "scope": "Bookland", "locale": "pl_PL"}]
                  for name, data in {"name": "English Path 2", "school_type": "sp", "grade": "2",
                                     "subject": "ang", "series": "English Path", "edition": "2025"}.items()}
        parsed = self.app["parse_akeneo_product"]({"identifier": "SKU1", "values": values,
                     "categories": ["ang_sp_2"], "associations": {"RELATED": {"products": ["SKU2"]}}},
                     "Bookland", "pl_PL", "fake-token")
        prepared = self.app["_prepare_product_data"](parsed)
        self.assertEqual(prepared["categories"], ["ang_sp_2"])
        self.assertEqual(prepared["school"], "szkoła podstawowa")
        self.assertEqual(prepared["subject"], "język angielski")
        self.assertEqual(prepared["grade"], "2")
        self.assertEqual(prepared["edition"], "2025")
        self.assertEqual(prepared["associations"], parsed["associations"])

    def test_linking_facts_never_fall_back_to_other_store_or_locale(self):
        values = {"edition": [{"data": "2025", "scope": "OtherStore", "locale": "en_US"}],
                  "grade": [{"data": "2", "scope": "Bookland", "locale": "en_US"}],
                  "school_type": [{"data": "sp", "scope": "OtherStore", "locale": "pl_PL"}]}
        parsed = self.app["parse_akeneo_product"]({"values": values}, "Bookland", "pl_PL", "fake-token")
        self.assertEqual(parsed["edition"], "")
        self.assertEqual(parsed["grade"], "")
        self.assertEqual(parsed["school"], "")

    def test_automatic_zero_links_leaves_existing_description_without_gemini(self):
        original = '<p>Istniejący opis <a href="https://bookland.com.pl/istniejacy">z linkiem</a>.</p>'
        self.product["description"] = original
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL",
                    internal_link={"mode": "automatic"}, link_only=True, use_research=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["description_html"], original)
        self.assertTrue(result["link_only"])
        self.sdk.assert_not_called()
        send = Mock()
        deliver_description_result(result, original, send, channel="Bookland", locale="pl_PL")
        self.assertEqual(send.call_args.args[1], original)
        self.assertEqual(result["allowed_links"], [])
        deliver_description_result(result, original + '<p><a href="https://outside.example/new">Nowy</a></p>',
                                   send, channel="Bookland", locale="pl_PL")
        self.assertNotIn("outside.example", send.call_args.args[1])
        self.assertIn('href="https://bookland.com.pl/istniejacy"', send.call_args.args[1])

    def test_link_only_without_any_configuration_never_rewrites_source(self):
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL", link_only=True)
        self.assertEqual(result["description_html"], self.product["description"])
        self.assertIsNone(result["error"])
        self.sdk.assert_not_called()

    def test_link_only_without_source_reports_error_without_api_call(self):
        self.product["description"] = ""
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL", link_only=True)
        self.assertTrue(result["error"])
        self.sdk.assert_not_called()

    def test_automatic_selected_urls_survive_generation_edit_and_delivery(self):
        targets = [{"url": LINK, "label": "Kryminały", "kind": "category"},
                   {"url": "https://bookland.com.pl/cwiczenia", "label": "Ćwiczenia", "kind": "product"}]
        self.app["select_internal_links"].return_value = {"links": targets, "warnings": [], "candidates": []}
        self.sdk.return_value.text = '<p><a href="' + LINK + '">Kryminały</a> i <a href="https://bookland.com.pl/cwiczenia">ćwiczenia</a>.</p>'
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL",
                    internal_link={"mode": "automatic"}, use_research=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["allowed_links"], [target["url"] for target in targets])
        self.assertEqual(self.app["check_internal_link_url"].call_count, 2)
        self.assertIn("Cele:", self.sdk.call_args.kwargs["config"].system_instruction)
        edited = result["description_html"] + '<p><a href="https://example.com/obcy">Obcy link</a>.</p>'
        send = Mock()
        report = deliver_description_result(result, edited, send, channel="Bookland", locale="pl_PL")
        self.assertNotIn("example.com", send.call_args.args[1])
        self.assertIn("Obcy link", send.call_args.args[1])
        self.assertIn("LINK_TARGET_REMOVED", {issue["code"] for issue in report.warnings})

    def test_luigis_targets_skip_live_check_and_pass_relation_to_prompt(self):
        targets = [{"url": "https://bookland.com.pl/podreczniki-ogolne/szkolne/szkola-podstawowa-klasa-5/matematyka",
                    "label": "Podręczniki szkolne > Szkolne > Szkoła Podstawowa Klasa 5 > Matematyka",
                    "kind": "category", "origin": "luigisbox"},
                   {"url": "https://bookland.com.pl/wrog", "label": "Wróg", "kind": "product", "origin": "luigisbox",
                    "relation": "ten sam autor: Łukasz Orbitowski"}]
        self.app["select_internal_links"].return_value = {"links": targets, "warnings": [], "candidates": []}
        self.sdk.return_value.text = '<p>Opis z <a href="https://bookland.com.pl/wrog">powieścią Wróg</a>.</p>'
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL",
                    internal_link={"mode": "automatic"}, use_research=False)
        self.app["check_internal_link_url"].assert_not_called()
        self.assertEqual(result["allowed_links"], [target["url"] for target in targets])
        prompt = self.sdk.call_args.kwargs["config"].system_instruction
        self.assertIn("ten sam autor: Łukasz Orbitowski", prompt)
        self.assertIn('"kind": "product"', prompt)

    def test_invalid_storefront_target_is_skipped_without_losing_description(self):
        target = {"url": LINK, "label": "Kryminały", "kind": "category"}
        self.app["select_internal_links"].return_value = {"links": [target], "warnings": [], "candidates": [{**target, "selected": True}]}
        self.app["check_internal_link_url"].side_effect = ValueError("Cel nie jest dostępny")
        result = self.app["process_product_from_akeneo"]("SKU1", "fake", "Bookland", "pl_PL",
                    internal_link={"mode": "automatic"}, use_research=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["allowed_links"], [])
        self.assertEqual(result["link_report"]["links"], [])
        self.assertFalse(result["link_report"]["candidates"][0]["selected"])
        self.assertIn("Cel nie jest dostępny", result["link_report"]["warnings"])
        self.assertNotIn("href", result["description_html"])


if __name__ == "__main__":
    unittest.main()
