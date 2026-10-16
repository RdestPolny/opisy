"""Uruchamia cały app.py w Streamlit AppTest z fikcyjnymi sekretami (bez sieci)."""
import os
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"
SECRETS = {
    "AKENEO_BASE_URL": "https://akeneo.invalid",
    "AKENEO_CLIENT_ID": "id",
    "AKENEO_SECRET": "secret",
    "AKENEO_USERNAME": "user",
    "AKENEO_PASSWORD": "password",
    "GOOGLE_API_KEY": "key",
}


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def run_app(self):
        at = AppTest.from_file(str(APP), default_timeout=60)
        for key, value in SECRETS.items():
            at.secrets[key] = value
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def test_app_renders_in_both_modes(self):
        at = self.run_app()
        self.assertTrue(at.sidebar)
        mode = next(r for r in at.radio if "Generator opisów" in r.options)
        mode.set_value(next(o for o in mode.options if o != mode.value)).run()
        self.assertFalse(at.exception, [e.value for e in at.exception])

    def test_results_tab_exports_and_backup_with_data(self):
        import job_store

        job_store.init_db()  # ścieżka względna -> baza w katalogu tymczasowym testu
        job_store.bulk_upsert_meta_jobs([{
            "sku": "A", "channel": "Bookland", "locale": "pl_PL", "store_view_code": "sv",
            "product_data": {"title": "Tytuł", "author": "", "description": "Opis"},
        }])
        job_store.save_meta_results_bulk([{"job_key": job_store.make_job_key("A", "Bookland", "pl_PL"),
                                           "meta_title": "Tytuł", "meta_description": "Opis", "status": "completed"}])
        at = self.run_app()
        for label in ("Przygotuj: CSV Magento", "Przygotuj: Raport jakości", "Przygotuj: kopię danych"):
            next(b for b in at.button if b.label.startswith(label)).click().run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
        next(t for t in at.toggle if "Akeneo" in t.label).set_value(True).run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any(b.label.startswith("Wyślij do Akeneo (1)") for b in at.button))


    def test_search_marks_products_missing_from_shop(self):
        from unittest.mock import patch

        import akeneo
        import ui

        products = [{"identifier": "A", "title": "W sklepie"}, {"identifier": "B", "title": "Poza sklepem"},
                    {"identifier": "C", "title": "Wyprzedany"}]
        statuses = {"A": {"status": "ok", "url": "u"}, "B": {"status": "missing", "url": ""},
                    "C": {"status": "out_of_stock", "url": "u"}}
        ui._cached_shop_statuses.clear()
        with patch.object(akeneo, "akeneo_search_products", return_value=products), \
             patch.object(akeneo, "akeneo_get_token", return_value="t"), \
             patch.object(ui, "luigis_item_statuses", return_value=statuses):
            at = self.run_app()
            next(t for t in at.text_input if t.label == "Szukaj produktu").set_value("matematyka")
            next(b for b in at.button if b.label == "Szukaj").click().run()
            labels = [c.label for c in at.checkbox if c.key and c.key.startswith("search_")]
            self.assertEqual(len(labels), 3)
            self.assertIn("⛔ brak w sklepie", labels[1])
            self.assertIn("⚠️ brak na stanie", labels[2])
            self.assertNotIn("⛔", labels[0])
            next(c for c in at.checkbox if c.key == "hide_unlinkable_search").check().run()
            labels = [c.label for c in at.checkbox if c.key and c.key.startswith("search_")]
            self.assertEqual([label.split(" - ")[0] for label in labels], ["A", "C"])
            self.assertFalse(at.exception, [e.value for e in at.exception])


if __name__ == "__main__":
    unittest.main()
