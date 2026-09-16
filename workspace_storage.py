from __future__ import annotations

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
