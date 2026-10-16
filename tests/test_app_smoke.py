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


if __name__ == "__main__":
    unittest.main()
