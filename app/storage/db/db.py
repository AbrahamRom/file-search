"""
Database connection management for Storage Node.
Uses SQLite with support for future migration to PostgreSQL or other databases.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Permitir configurar la ruta de la DB por variable de entorno
_db_from_env = os.getenv("DB_PATH")
DB_PATH = (
    Path(_db_from_env) if _db_from_env else Path(__file__).with_name("storage.db")
)


@contextmanager
def get_conn():
    """
    Context manager for database connections.
    Handles connection lifecycle with auto-commit/rollback.
    """
    # Ensure parent directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """
    Initialize database schema from schema.sql.
    Creates tables if they don't exist.
    """
    schema_path = Path(__file__).with_name("schema.sql")
    with get_conn() as conn:
        sql = schema_path.read_text(encoding="utf-8")
        cur = conn.cursor()
        # Use executescript to handle triggers correctly
        try:
            cur.executescript(sql)
        except sqlite3.OperationalError as e:
            print(f"Error executing schema: {e}")
            raise


__all__ = ["DB_PATH", "get_conn", "init_db"]
