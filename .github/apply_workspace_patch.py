from pathlib import Path

app_path = Path("app.py")
app = app_path.read_text()


def replace_once(old: str, new: str, label: str) -> None:
    global app
    count = app.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    app = app.replace(old, new, 1)


replace_once(
    "import unicodedata\n",
    "import unicodedata\n\nfrom workspace_storage import (\n    LEGACY_WORKSPACE_ID,\n    create_workspace as storage_create_workspace,\n    clear_workspace as storage_clear_workspace,\n    ensure_workspace_schema,\n    list_workspaces as storage_list_workspaces,\n    load_workspace as storage_load_workspace,\n    save_workspace as storage_save_workspace,\n)\n",
    "workspace imports",
)
replace_once('APP_VERSION = "4.9.1"', 'APP_VERSION = "4.9.2"', "version bump")

old_init_tail = '''        ensure_column("batch_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_jobs_run ON meta_jobs(run_id)")
'''
new_init_tail = '''        ensure_column("batch_jobs", "run_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_jobs_run ON meta_jobs(run_id)")
        ensure_workspace_schema(conn)
'''
replace_once(old_init_tail, new_init_tail, "workspace schema init")

start = app.index("def load_description_workspace() -> Dict:")
end = app.index("\n\ndef add_optimized_product(", start)
workspace_wrappers = '''def active_description_workspace_id() -> str:
    return st.session_state.get("description_workspace_id", LEGACY_WORKSPACE_ID)


def load_description_workspace(workspace_id: Optional[str] = None) -> Dict:
    with db_connect() as conn:
        return storage_load_workspace(conn, workspace_id or active_description_workspace_id())


def save_description_workspace(
    selected_skus: Sequence[str],
    results: Sequence[Dict],
    workspace_id: Optional[str] = None,
) -> None:
    with db_connect() as conn:
        storage_save_workspace(
            conn,
            workspace_id or active_description_workspace_id(),
            selected_skus,
            results,
        )


def clear_description_workspace(workspace_id: Optional[str] = None) -> None:
    with db_connect() as conn:
        storage_clear_workspace(conn, workspace_id or active_description_workspace_id())


def list_description_workspaces() -> List[Dict]:
    with db_connect() as conn:
        return storage_list_workspaces(conn)


def create_description_workspace(name: str) -> Dict:
    with db_connect() as conn:
        return storage_create_workspace(conn, name)
'''
app = app[:start] + workspace_wrappers + app[end:]

replace_once(
    '''def init_session_state() -> None:
    workspace = load_description_workspace()
    restored_results = workspace["results"]
''',
    '''def init_session_state() -> None:
    workspace_id = st.session_state.get("description_workspace_id", LEGACY_WORKSPACE_ID)
    workspace = load_description_workspace(workspace_id)
    restored_results = workspace["results"]
''',
    "session workspace id",
)
replace_once(
    '        "bulk_results": restored_results,\n        "generator_mode": "Generator opisów",\n',
    '        "bulk_results": restored_results,\n        "description_workspace_id": workspace_id,\n        "generator_mode": "Generator opisów",\n',
    "session default workspace id",
)

marker = "\n\ndef clear_product_queue() -> None:\n"
switch_fn = '''

def switch_description_workspace(workspace_id: str) -> None:
    workspace = load_description_workspace(workspace_id)
    restored_results = workspace["results"]
    restored_products = {sku: {"title": sku} for sku in workspace["selected_skus"]}
    for result in restored_results:
        sku = result.get("sku")
        if sku in restored_products:
            restored_products[sku]["title"] = result.get("title", sku)

    st.session_state.description_workspace_id = workspace_id
    st.session_state.bulk_results = restored_results
    st.session_state.bulk_selected_products = restored_products
    st.session_state.products_to_send = {}
    st.session_state.interactive_seed_results = {}
    st.session_state.last_interactive_checkpoint_path = ""
    st.session_state.search_res = []
    st.session_state.manual_product_input = ""
    st.session_state.active_editor_sku = ""
    for key in list(st.session_state):
        if key.startswith(("edit_", "visual_editor_", "send_", "search_")):
            st.session_state.pop(key, None)
'''
if marker not in app:
    raise SystemExit("switch function marker not found")
app = app.replace(marker, switch_fn + marker, 1)

sidebar_anchor = '''    st.caption(
        "Tworzy wyłącznie pełne opisy HTML."
        if not st.session_state.meta_only
        else "Tworzy wyłącznie meta title i meta description. Opisy produktów pozostają bez zmian."
    )

    st.markdown("---")
    st.subheader("Ustawienia wspólne")
'''
sidebar_replacement = '''    st.caption(
        "Tworzy wyłącznie pełne opisy HTML."
        if not st.session_state.meta_only
        else "Tworzy wyłącznie meta title i meta description. Opisy produktów pozostają bez zmian."
    )

    if not st.session_state.meta_only:
        st.markdown("---")
        st.subheader("Kolejka robocza")
        workspace_rows = list_description_workspaces()
        workspace_names = {item["workspace_id"]: item["workspace_name"] for item in workspace_rows}
        workspace_ids = list(workspace_names)
        current_workspace_id = st.session_state.get("description_workspace_id", LEGACY_WORKSPACE_ID)
        if current_workspace_id not in workspace_names and workspace_ids:
            current_workspace_id = workspace_ids[0]
            switch_description_workspace(current_workspace_id)

        selected_workspace_id = st.selectbox(
            "Wybierz kolejkę",
            workspace_ids,
            index=workspace_ids.index(current_workspace_id) if current_workspace_id in workspace_ids else 0,
            format_func=lambda item: workspace_names[item],
            help="Każda kolejka ma własną listę produktów, wyniki i wersje robocze opisów.",
        )
        if selected_workspace_id != st.session_state.get("description_workspace_id"):
            switch_description_workspace(selected_workspace_id)
            st.rerun()

        with st.expander("Dodaj nową kolejkę"):
            new_workspace_name = st.text_input(
                "Nazwa kolejki",
                placeholder="np. Bartek, Marcin, BOK 1",
                key="new_description_workspace_name",
            )
            if st.button("Utwórz i przełącz", key="create_description_workspace"):
                try:
                    created_workspace = create_description_workspace(new_workspace_name)
                    st.session_state.new_description_workspace_name = ""
                    switch_description_workspace(created_workspace["workspace_id"])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

        active_workspace_name = workspace_names.get(
            st.session_state.get("description_workspace_id"),
            st.session_state.get("description_workspace_id", ""),
        )
        st.caption(
            f"Aktywna kolejka: {active_workspace_name}. Wyczyść kolejkę usuwa tylko jej wersję roboczą."
        )

    st.markdown("---")
    st.subheader("Ustawienia wspólne")
'''
replace_once(sidebar_anchor, sidebar_replacement, "sidebar workspace selector")
app_path.write_text(app)

storage = r'''from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Dict, List, Sequence

LEGACY_WORKSPACE_ID = "wspolna"
LEGACY_WORKSPACE_NAME = "Wspólna (legacy)"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_workspace_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS description_workspaces (
            workspace_id TEXT PRIMARY KEY,
            workspace_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            selected_skus TEXT NOT NULL DEFAULT '[]',
            results TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )
        """
    )
    existing = conn.execute("SELECT COUNT(*) FROM description_workspaces").fetchone()[0]
    if existing:
        return
    legacy_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='description_workspace'"
    ).fetchone()
    legacy_row = None
    if legacy_exists:
        legacy_row = conn.execute(
            "SELECT selected_skus, results, updated_at FROM description_workspace WHERE id=1"
        ).fetchone()
    if legacy_row:
        selected_skus, results, updated_at = legacy_row
    else:
        selected_skus, results, updated_at = "[]", "[]", _utcnow_iso()
    conn.execute(
        """
        INSERT INTO description_workspaces(
            workspace_id, workspace_name, selected_skus, results, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (LEGACY_WORKSPACE_ID, LEGACY_WORKSPACE_NAME, selected_skus, results, updated_at),
    )


def list_workspaces(conn: sqlite3.Connection) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT workspace_id, workspace_name, updated_at
        FROM description_workspaces
        ORDER BY CASE WHEN workspace_id=? THEN 0 ELSE 1 END, workspace_name COLLATE NOCASE
        """,
        (LEGACY_WORKSPACE_ID,),
    ).fetchall()
    return [dict(row) if hasattr(row, "keys") else {
        "workspace_id": row[0], "workspace_name": row[1], "updated_at": row[2]
    } for row in rows]


def _decode_list(value: str) -> list:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def load_workspace(conn: sqlite3.Connection, workspace_id: str) -> Dict:
    row = conn.execute(
        """
        SELECT workspace_id, workspace_name, selected_skus, results, updated_at
        FROM description_workspaces WHERE workspace_id=?
        """,
        (workspace_id,),
    ).fetchone()
    if not row:
        return {"workspace_id": workspace_id, "workspace_name": workspace_id, "selected_skus": [], "results": [], "updated_at": ""}
    getter = (lambda key, index: row[key]) if hasattr(row, "keys") else (lambda key, index: row[index])
    return {
        "workspace_id": getter("workspace_id", 0),
        "workspace_name": getter("workspace_name", 1),
        "selected_skus": _decode_list(getter("selected_skus", 2)),
        "results": _decode_list(getter("results", 3)),
        "updated_at": getter("updated_at", 4),
    }


def save_workspace(conn: sqlite3.Connection, workspace_id: str, selected_skus: Sequence[str], results: Sequence[Dict]) -> None:
    if not conn.execute("SELECT 1 FROM description_workspaces WHERE workspace_id=?", (workspace_id,)).fetchone():
        raise ValueError("Wybrana kolejka już nie istnieje. Odśwież aplikację i wybierz inną.")
    conn.execute(
        "UPDATE description_workspaces SET selected_skus=?, results=?, updated_at=? WHERE workspace_id=?",
        (json.dumps(list(selected_skus), ensure_ascii=False), json.dumps(list(results), ensure_ascii=False, default=str), _utcnow_iso(), workspace_id),
    )


def clear_workspace(conn: sqlite3.Connection, workspace_id: str) -> None:
    if not conn.execute("SELECT 1 FROM description_workspaces WHERE workspace_id=?", (workspace_id,)).fetchone():
        return
    conn.execute(
        "UPDATE description_workspaces SET selected_skus='[]', results='[]', updated_at=? WHERE workspace_id=?",
        (_utcnow_iso(), workspace_id),
    )


def create_workspace(conn: sqlite3.Connection, name: str) -> Dict:
    cleaned = " ".join((name or "").strip().split())
    if len(cleaned) < 2:
        raise ValueError("Podaj nazwę kolejki (minimum 2 znaki).")
    if len(cleaned) > 60:
        raise ValueError("Nazwa kolejki może mieć maksymalnie 60 znaków.")
    if conn.execute("SELECT 1 FROM description_workspaces WHERE workspace_name=? COLLATE NOCASE", (cleaned,)).fetchone():
        raise ValueError("Kolejka o tej nazwie już istnieje.")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-") or "kolejka"
    digest = hashlib.sha256(f"{cleaned}|{_utcnow_iso()}".encode()).hexdigest()[:8]
    workspace_id = f"{slug[:40]}-{digest}"
    now = _utcnow_iso()
    conn.execute(
        "INSERT INTO description_workspaces(workspace_id, workspace_name, selected_skus, results, updated_at) VALUES (?, ?, '[]', '[]', ?)",
        (workspace_id, cleaned, now),
    )
    return {"workspace_id": workspace_id, "workspace_name": cleaned, "updated_at": now}
'''
Path("workspace_storage.py").write_text(storage)

tests = r'''import sqlite3
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
'''
Path("tests/test_workspace_storage.py").write_text(tests)

changelog = Path("CHANGELOG.md")
entry = '''# 4.9.2

Generator opisów obsługuje teraz niezależne kolejki robocze. Każda kolejka ma własną listę SKU, wyniki i edycje; wyczyszczenie jednej kolejki nie wpływa na pozostałe. Dotychczasowy wspólny workspace jest zachowany jako „Wspólna (legacy)”.

'''
changelog.write_text(entry + changelog.read_text())
