"""Data-access layer: the SQLite connection, the schema and a few small helpers.

All SQL of the system goes through these helpers, so a move to another database touches this module and the SQL
strings in webapp/services, nothing else. Connections are opened in autocommit mode; `transaction()` groups
statements (BEGIN IMMEDIATE takes the write lock at once, so two writers never interleave).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


def connect(path: Path) -> sqlite3.Connection:
    """Open the database (creating the schema when needed). One connection per request or per thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        conn.execute("PRAGMA journal_mode = WAL")     # readers do not block the writer (the worker imports)
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error). Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r is not None else None


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    r = conn.execute(sql, params).fetchone()
    return r[0] if r is not None else None


def insert(conn: sqlite3.Connection, table: str, values: Dict[str, Any]) -> int:
    cols = list(values)
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [values[c] for c in cols])
    return int(cur.lastrowid)


def upsert(conn: sqlite3.Connection, table: str, values: Dict[str, Any], key: Iterable[str],
           keep: Iterable[str] = ()) -> int:
    """Insert, or update the row with the same `key` columns (which need a UNIQUE / PRIMARY KEY constraint).
    Columns in `keep` are only written on insert (e.g. the workflow status of an alert). Returns the row id."""
    key, keep = list(key), set(keep)
    cols = list(values)
    update = [c for c in cols if c not in key and c not in keep]
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({', '.join(key)}) DO "
           + (f"UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in update)}" if update else "NOTHING"))
    conn.execute(sql, [values[c] for c in cols])
    where = " AND ".join(f"{k} = ?" for k in key)
    rid = scalar(conn, f"SELECT rowid FROM {table} WHERE {where}", [values[k] for k in key])
    return int(rid) if rid is not None else 0


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    from .settings import DEFAULTS
    v = scalar(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    if v is None:
        return DEFAULTS.get(key, default)
    return json.loads(v)


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def js(value: Any) -> str:
    """JSON text for a TEXT column."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def unjs(text: Optional[str], default: Any = None) -> Any:
    return json.loads(text) if text else default
