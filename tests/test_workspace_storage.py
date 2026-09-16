import sqlite3
import unittest
from workspace_storage import LEGACY_WORKSPACE_ID, clear_workspace, create_workspace, ensure_workspace_schema, list_workspaces, load_workspace, save_workspace

class WorkspaceStorageTests(unittest.TestCase):
    def connect(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    def test_migrates_shared_legacy_workspace_without_losing_results(self):
        conn = self.connect()
        conn.execute("CREATE TABLE description_workspace (id INTEGER PRIMARY KEY, selected_skus TEXT, results TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO description_workspace VALUES (1, '[\"SKU1\"]', '[{\"sku\":\"SKU1\"}]', '2026-09-16T10:00:00+00:00')")
        ensure_workspace_schema(conn)
        workspace = load_workspace(conn, LEGACY_WORKSPACE_ID)
        self.assertEqual(workspace["selected_skus"], ["SKU1"])
        self.assertEqual(workspace["results"][0]["sku"], "SKU1")

    def test_queues_are_isolated_and_clear_affects_only_selected_queue(self):
        conn = self.connect()
        ensure_workspace_schema(conn)
        bartek = create_workspace(conn, "Bartek")
        bok = create_workspace(conn, "BOK 1")
        save_workspace(conn, bartek["workspace_id"], ["A"], [{"sku": "A"}])
        save_workspace(conn, bok["workspace_id"], ["B"], [{"sku": "B"}])
        clear_workspace(conn, bartek["workspace_id"])
        self.assertEqual(load_workspace(conn, bartek["workspace_id"])["selected_skus"], [])
        self.assertEqual(load_workspace(conn, bok["workspace_id"])["selected_skus"], ["B"])
        self.assertEqual(load_workspace(conn, bok["workspace_id"])["results"], [{"sku": "B"}])

    def test_duplicate_queue_name_is_rejected_case_insensitively(self):
        conn = self.connect()
        ensure_workspace_schema(conn)
        create_workspace(conn, "Bartek")
        with self.assertRaises(ValueError):
            create_workspace(conn, "bartek")

    def test_list_contains_legacy_and_created_queues(self):
        conn = self.connect()
        ensure_workspace_schema(conn)
        create_workspace(conn, "Marcin")
        names = [item["workspace_name"] for item in list_workspaces(conn)]
        self.assertEqual(names[0], "Wspólna (legacy)")
        self.assertIn("Marcin", names)

if __name__ == "__main__":
    unittest.main()
