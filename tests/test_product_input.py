import unittest

from product_input import ProductInputResolutionError, resolve_product_inputs


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payload)


class ResolveProductInputsTests(unittest.TestCase):
    def test_resolves_mixed_sku_and_bookland_url(self):
        session = FakeSession({
            "data": {
                "products": {
                    "items": [{
                        "sku": "9788396146267",
                        "name": "Gruffalo",
                        "url_key": "gruffalo",
                    }]
                }
            }
        })

        resolved, errors = resolve_product_inputs([
            "ABC-123",
            "https://bookland.com.pl/gruffalo/?source=test#opis",
        ], session=session)

        self.assertEqual(errors, [])
        self.assertEqual([item["sku"] for item in resolved], ["ABC-123", "9788396146267"])
        self.assertEqual(resolved[1]["title"], "Gruffalo")
        self.assertEqual(
            session.calls[0]["json"]["variables"]["urlKeys"],
            ["gruffalo"],
        )

    def test_accepts_bookland_url_without_scheme(self):
        session = FakeSession({
            "data": {
                "products": {
                    "items": [{
                        "sku": "9780349434278",
                        "name": "Twisted Love",
                        "url_key": "twisted-love",
                    }]
                }
            }
        })

        resolved, errors = resolve_product_inputs(
            ["www.bookland.com.pl/twisted-love"],
            session=session,
        )

        self.assertEqual(errors, [])
        self.assertEqual(resolved[0]["sku"], "9780349434278")

    def test_rejects_external_url_without_network_call(self):
        session = FakeSession({})

        resolved, errors = resolve_product_inputs(
            ["https://example.com/product", "9780000000000"],
            session=session,
        )

        self.assertEqual([item["sku"] for item in resolved], ["9780000000000"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Nieobsługiwana domena", errors[0])
        self.assertEqual(session.calls, [])

    def test_rejects_unsupported_url_protocol(self):
        session = FakeSession({})

        resolved, errors = resolve_product_inputs(
            ["ftp://bookland.com.pl/gruffalo"],
            session=session,
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Nieobsługiwany protokół", errors[0])
        self.assertEqual(session.calls, [])

    def test_rejects_bookland_homepage(self):
        session = FakeSession({})

        resolved, errors = resolve_product_inputs(
            ["bookland.com.pl"],
            session=session,
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("nie wskazuje produktu", errors[0])
        self.assertEqual(session.calls, [])

    def test_reports_missing_product(self):
        session = FakeSession({"data": {"products": {"items": []}}})

        resolved, errors = resolve_product_inputs(
            ["https://bookland.com.pl/brak-produktu"],
            session=session,
        )

        self.assertEqual(resolved, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Nie znaleziono produktu", errors[0])

    def test_raises_for_graphql_errors(self):
        session = FakeSession({"errors": [{"message": "failure"}]})

        with self.assertRaises(ProductInputResolutionError):
            resolve_product_inputs(
                ["https://bookland.com.pl/gruffalo"],
                session=session,
            )


if __name__ == "__main__":
    unittest.main()
