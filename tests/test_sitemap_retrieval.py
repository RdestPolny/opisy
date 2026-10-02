import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sitemap_retrieval import (
    BOOKLAND_SITEMAP_INDEX_URL,
    refresh_sitemap_cache,
    search_sitemap_candidates,
    sitemap_cache_status,
)


class SitemapRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "sitemap.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_refresh_builds_fts_once_and_search_excludes_same_product(self):
        child_a = "https://bookland.com.pl/pub/product-sitemap.xml"
        child_b = "https://bookland.com.pl/pub/category-sitemap.xml"

        def fake_read(url):
            if url == BOOKLAND_SITEMAP_INDEX_URL:
                return [child_a, child_b], []
            if url == child_a:
                return [], [
                    {
                        "url": "https://bookland.com.pl/matematyka-4-cwiczenia",
                        "label": "Matematyka 4 ćwiczenia",
                        "path": "/matematyka-4-cwiczenia",
                        "kind": "product",
                        "source_sitemap": child_a,
                    },
                    {
                        "url": "https://bookland.com.pl/matematyka-4-zeszyt-cwiczen",
                        "label": "Matematyka 4 zeszyt ćwiczeń",
                        "path": "/matematyka-4-zeszyt-cwiczen",
                        "kind": "product",
                        "source_sitemap": child_a,
                    },
                    {
                        "url": "https://bookland.com.pl/historia-4-podrecznik",
                        "label": "Historia 4 podręcznik",
                        "path": "/historia-4-podrecznik",
                        "kind": "product",
                        "source_sitemap": child_a,
                    },
                ]
            if url == child_b:
                return [], [
                    {
                        "url": "https://bookland.com.pl/matematyka",
                        "label": "Matematyka",
                        "path": "/matematyka",
                        "kind": "category",
                        "source_sitemap": child_b,
                    }
                ]
            raise AssertionError(url)

        with patch("sitemap_retrieval._read_xml_stream", side_effect=fake_read) as read:
            stats = refresh_sitemap_cache(
                index_url=BOOKLAND_SITEMAP_INDEX_URL,
                db_path=self.db_path,
                workers=2,
            )

        self.assertEqual(stats["url_count"], 4)
        self.assertEqual(stats["sitemap_count"], 3)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sitemap_cache_status(self.db_path)["url_count"], 4)

        product = {
            "title": "Matematyka 4 ćwiczenia",
            "series": "Matematyka",
            "subject": "Matematyka",
            "grade": "4",
        }
        with patch("sitemap_retrieval._read_xml_stream") as read_again:
            candidates = search_sitemap_candidates(
                product,
                db_path=self.db_path,
                index_url=BOOKLAND_SITEMAP_INDEX_URL,
                limit=8,
            )
        read_again.assert_not_called()
        urls = [candidate["url"] for candidate in candidates]
        self.assertNotIn("https://bookland.com.pl/matematyka-4-cwiczenia", urls)
        self.assertIn("https://bookland.com.pl/matematyka-4-zeszyt-cwiczen", urls)
        self.assertTrue(all(candidate["origin"] == "sitemap" for candidate in candidates))

    def test_empty_product_does_not_scan_network_when_cache_exists(self):
        child = "https://bookland.com.pl/pub/product-sitemap.xml"

        def fake_read(url):
            if url == BOOKLAND_SITEMAP_INDEX_URL:
                return [child], []
            return [], [{
                "url": "https://bookland.com.pl/angielski-a1",
                "label": "Angielski A1",
                "path": "/angielski-a1",
                "kind": "product",
                "source_sitemap": child,
            }]

        with patch("sitemap_retrieval._read_xml_stream", side_effect=fake_read):
            refresh_sitemap_cache(db_path=self.db_path)

        with patch("sitemap_retrieval._read_xml_stream") as read:
            self.assertEqual(search_sitemap_candidates({}, db_path=self.db_path), [])
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
