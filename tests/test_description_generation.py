import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from description_generation import generate_description_result
from test_description_output import LINK, full_description


class APIError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"API error {code}")


class DescriptionGenerationTests(unittest.TestCase):
    def generate(self, request, **kwargs):
        return generate_description_result(request, contents="Dane produktu", context=kwargs.pop("context", {}), normalize_response=lambda text: text, sleep=Mock(), **kwargs)

    def test_warnings_return_full_text_after_one_call(self):
        candidate = full_description(link_paragraph=1, missing_bold=True)
        request = Mock(side_effect=[SimpleNamespace(text=candidate), TimeoutError()])
        result = self.generate(request, context={"required_link": LINK, "required_link_paragraph": 2})
        self.assertEqual(result["description_html"], candidate)
        self.assertIsNone(result["error"])
        self.assertEqual(result["api_attempts"], 1)
        self.assertEqual({i["code"] for i in result["validation_warnings"]}, {"LINK_POSITION", "BOLD_COUNT"})
        self.assertEqual(request.call_count, 1)

    def test_permanent_api_errors_are_not_retried(self):
        for code in [400, 401, 403, 404]:
            with self.subTest(code=code):
                request = Mock(side_effect=APIError(code))
                result = self.generate(request)
                self.assertTrue(result["error"])
                self.assertEqual(result["description_html"], "")
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["api_attempts"], 1)

    def test_transient_errors_share_three_attempt_budget(self):
        for error in [APIError(503), APIError(429), TimeoutError("timeout")]:
            with self.subTest(error=error):
                request = Mock(side_effect=error)
                result = self.generate(request)
                self.assertEqual(request.call_count, 3)
                self.assertEqual(result["api_attempts"], 3)
                self.assertTrue(result["error"])

    def test_empty_response_and_api_failure_use_same_budget(self):
        request = Mock(side_effect=[SimpleNamespace(text=""), APIError(503), SimpleNamespace(text=full_description())])
        result = self.generate(request)
        self.assertEqual(result["api_attempts"], 3)
        self.assertIsNone(result["error"])

    def test_empty_output_never_contains_error_message_as_description(self):
        request = Mock(return_value=SimpleNamespace(text=""))
        result = self.generate(request)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(result["description_html"], "")
        self.assertIn("pusty", result["error"])

    def test_limit_signal_preserves_usable_content_as_warning(self):
        request = Mock(return_value=SimpleNamespace(text=full_description(), candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")]))
        result = self.generate(request)
        self.assertIsNone(result["error"])
        self.assertEqual(result["generation_warnings"][0]["code"], "OUTPUT_MAY_BE_TRUNCATED")
        self.assertEqual(request.call_count, 1)

    def test_provider_blocked_empty_response_is_not_retried(self):
        request = Mock(return_value=SimpleNamespace(text="", candidates=[SimpleNamespace(finish_reason="SAFETY")]))
        result = self.generate(request)
        self.assertTrue(result["error"])
        self.assertEqual(request.call_count, 1)

    def test_configuration_errors_do_not_spend_api_calls(self):
        request = Mock()
        with self.assertRaises(ValueError):
            self.generate(request, context={"typo": True})
        request.assert_not_called()

    def test_rate_limit_uses_retry_after_without_extra_generation_loops(self):
        error = APIError(429)
        error.response = SimpleNamespace(headers={"Retry-After": "7"})
        sleep = Mock()
        result = generate_description_result(
            Mock(side_effect=[error, SimpleNamespace(text=full_description())]),
            contents="Dane produktu", context={}, normalize_response=lambda text: text, sleep=sleep,
        )
        sleep.assert_called_once_with(7)
        self.assertEqual(result["api_attempts"], 2)

    def test_sanitized_result_is_returned_without_active_attributes(self):
        candidate = full_description().replace("<p>", '<p onclick="void(0)">', 1)
        result = self.generate(Mock(return_value=SimpleNamespace(text=candidate)))
        self.assertNotIn("onclick", result["description_html"])
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
