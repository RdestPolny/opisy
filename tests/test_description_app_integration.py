"""Run the real description adapters without executing Streamlit or opening a DB."""
import ast
import html
import re
import unittest
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from google.genai import types
from description_generation import generate_description_result
from description_output import deliver_description_result
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
        }
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        ast.fix_missing_locations(module)
        self.product = {"title": "Przykładowa książka", "description": "Źródło produktu", "contributors": ["Jan Kowalski"], "contributor_role": "", "author": "Jan Kowalski"}
        self.sdk = Mock(return_value=SimpleNamespace(text=full_description(link_paragraph=1, missing_bold=True)))
        self.app = {
            "types": types, "html": html, "re": re, "unicodedata": unicodedata,
            "_POLISH_CHARS": str.maketrans("ąćęłńóśźż", "acelnoszz"),
            "GEMINI_MODEL": "existing-configured-model",
            "DESCRIPTION_PROMPT_VERSION": "test-version",
            "generate_description_result": generate_description_result,
            "get_gemini_client": lambda: SimpleNamespace(models=SimpleNamespace(generate_content=self.sdk)),
            "akeneo_get_product_details": lambda *args: self.product,
            "_prepare_product_data": lambda product: product,
            "validate_description_quality": lambda value: ("ok", "Opis OK"),
            "generate_product_url": lambda title: "https://example.com/product",
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


if __name__ == "__main__":
    unittest.main()
