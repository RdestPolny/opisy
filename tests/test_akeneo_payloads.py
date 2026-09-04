import unittest

from akeneo_payloads import (
    build_attribute_value,
    build_metatag_payload,
    build_metatag_product_update,
    parse_collection_response,
    serialize_collection_updates,
)


class AkeneoPayloadTests(unittest.TestCase):
    def test_builds_scoped_non_localized_values_for_current_schema(self):
        value = build_attribute_value(
            "Przykładowy meta title",
            {"scopable": True, "localizable": False},
            "Bookland",
            "pl_PL",
        )
        self.assertEqual(
            value,
            [{"data": "Przykładowy meta title", "scope": "Bookland", "locale": None}],
        )

    def test_payload_updates_only_two_meta_attributes(self):
        payload = build_metatag_payload(
            "Tytuł SEO",
            "Opis SEO produktu",
            title_attribute={"scopable": True, "localizable": False},
            description_attribute={"scopable": True, "localizable": False},
            channel="Bookland",
            locale="pl_PL",
        )
        self.assertEqual(set(payload), {"values"})
        self.assertEqual(set(payload["values"]), {"meta_title", "meta_description"})
        self.assertEqual(payload["values"]["meta_title"][0]["scope"], "Bookland")
        self.assertIsNone(payload["values"]["meta_description"][0]["locale"])

    def test_honors_localizable_and_non_scopable_attribute(self):
        value = build_attribute_value(
            "Treść",
            {"scopable": False, "localizable": True},
            "Bookland",
            "pl_PL",
        )
        self.assertEqual(value, [{"data": "Treść", "scope": None, "locale": "pl_PL"}])

    def test_rejects_blank_fields(self):
        with self.assertRaises(ValueError):
            build_metatag_payload(
                "",
                "Opis",
                title_attribute={},
                description_attribute={},
                channel="Bookland",
                locale="pl_PL",
            )

    def test_builds_and_serializes_collection_update(self):
        update = build_metatag_product_update(
            "9788301230371",
            "Tytuł SEO",
            "Opis SEO produktu",
            title_attribute={"scopable": True, "localizable": False},
            description_attribute={"scopable": True, "localizable": False},
            channel="Bookland",
            locale="pl_PL",
        )
        encoded = serialize_collection_updates([update])
        self.assertNotIn("\n", encoded)
        self.assertIn('"identifier":"9788301230371"', encoded)
        self.assertNotIn("description_html", encoded)

    def test_collection_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            serialize_collection_updates([{"identifier": str(index)} for index in range(101)])

    def test_parses_per_line_collection_status(self):
        response = (
            '{"line":1,"identifier":"one","status_code":204}\n'
            '{"line":2,"identifier":"two","status_code":422,"message":"invalid"}'
        )
        self.assertEqual(
            parse_collection_response(response),
            [
                {"line": 1, "identifier": "one", "status_code": 204},
                {"line": 2, "identifier": "two", "status_code": 422, "message": "invalid"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
