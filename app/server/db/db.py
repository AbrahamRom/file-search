import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Permitir configurar la ruta de la DB por variable de entorno (útil en Docker)
_db_from_env = os.getenv("DB_PATH")
DB_PATH = (
    Path(_db_from_env) if _db_from_env else Path(__file__).with_name("doc_search.db")
)

@contextmanager
def get_conn():
    # Ensure parent directory exists (no-op if same dir)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Open connection (use str for compatibility); tune args if needed
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        # Commit only if no exception occurred in caller
        conn.commit()
    except Exception:
        # Roll back if something failed, then re-raise
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db() -> None:
    """Initialize database schema from schema.sql.

    """
    schema_path = Path(__file__).with_name("schema.sql")
    with get_conn() as conn:
        sql = schema_path.read_text(encoding="utf-8")
        cur = conn.cursor()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError as e:
                print(f"Error executing statement: {stmt}\nError: {e}")
                raise