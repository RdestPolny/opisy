"""Run the real description adapters without executing Streamlit or opening a DB."""
import ast
import html
import json
import re
import unittest
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from google.genai import types
from description_generation import generate_description_result
from description_output import analyze_description_html, description_link_snapshot, deliver_description_result, sanitize_html
from test_description_output import LINK, full_description


class DescriptionAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).parents[1] / "app.py").read_text()
        tree = ast.parse(source)
        names = {
            "generate_description", "process_product_from_akeneo", "build_system_prompt_full",
            "build_system_prompt_link_only", "build_description_user_message", "detect_contributor_role",
            "normalize_spaces", "normalize_for_compare", "strip_html", "strip_code_fences",
            "clean_ai_fingerprints", "split_contributor_names", "format_contributor_names",
            "description_link_targets", "description_link_instructions",
            "parse_akeneo_product", "_prepare_product_data", "_value_from_values", "_list_from_values", "safe_string_value",
        }
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        fields = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LINK_FIELDS" for t in n.targets))
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fields, *nodes], type_ignores=[])
        ast.fix_missing_locations(module)
        self.product = {"title": "Przykładowa książka", "description": "Źródło produktu", "contributors": ["Jan Kowalski"], "contributor_role": "", "author": "Jan Kowalski"}
        self.sdk = Mock(return_value=SimpleNamespace(text=full_description(link_paragraph=1, missing_bold=True)))
        self.app = {
            "types": types, "html": html, "re": re, "unicodedata": unicodedata, "json": json,
            "_POLISH_CHARS": str.maketrans("ąćęłńóśźż", "acelnoszz"),
            "GEMINI_MODEL": "existing-configured-model",
            "DESCRIPTION_PROMPT_VERSION": "test-version",
            "generate_description_result": generate_description_result,
            "get_gemini_client": lambda: SimpleNamespace(models=SimpleNamespace(generate_content=self.sdk)),
            "akeneo_get_product_details": lambda *args: self.product,
            "validate_description_quality": lambda value: ("ok", "Opis OK"),
            "generate_product_url": lambda title: "https://example.com/product",
            "sanitize_html": sanitize_html,
            "analyze_description_html": analyze_description_html,
            "description_link_snapshot": description_link_snapshot,
            "select_internal_links": Mock(return_value={"links": [], "warnings": [], "candidates": []}),
            "check_internal_link_url": Mock(),
            "akeneo_get_option_label": lambda code, value, *args: {"sp": "szkoła podstawowa", "ang": "język angielski"}.get(value, value),
        }
        exec(compile(module, str(Path(__file__).parents[1] / "app.py"), "exec"), self.app)

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
