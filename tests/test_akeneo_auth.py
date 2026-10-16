import unittest

from akeneo_auth import TokenStore


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.fetched = 0
        self.store = TokenStore(clock=lambda: self.now)

    def fetch(self):
        self.fetched += 1
        return f"t{self.fetched}", 3600

    def test_reuses_token_until_refresh_margin(self):
        self.assertEqual(self.store.get(self.fetch), "t1")
        self.now = 3600 - 301
        self.assertEqual(self.store.get(self.fetch), "t1")
        self.now = 3600 - 299
        self.assertEqual(self.store.get(self.fetch), "t2")

    def test_rejected_token_is_refreshed_once(self):
        self.store.get(self.fetch)
        self.assertEqual(self.store.get(self.fetch, force=True, stale="t1"), "t2")
        # Inny wątek dostał 401 na starym tokenie, ale nowy już jest – bez kolejnego logowania.
        self.assertEqual(self.store.get(self.fetch, force=True, stale="t1"), "t2")
        self.assertEqual(self.fetched, 2)


if __name__ == "__main__":
    unittest.main()
