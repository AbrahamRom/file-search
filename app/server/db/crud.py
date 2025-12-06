from typing import List, Optional, Set

from .db import get_conn

def upsert_file(
    *,
    file_id: str,
    name: str,
    path: str,
    size: int,
    last_modified,
) -> None:
    """
    Insert or update a file record in the files table.
    last_modified may be a datetime or an ISO-8601 string.
    """
    # normalize last_modified to ISO string if it's a datetime-like object
    if hasattr(last_modified, "isoformat"):
        last_modified = last_modified.isoformat()

    sql = """
    INSERT INTO files (file_id, name, path, size, last_modified)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(file_id) DO UPDATE SET
        name = excluded.name,
        path = excluded.path,
        size = excluded.size,
        last_modified = excluded.last_modified
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id, name, path, size, last_modified))

def delete_file(*,file_id:str) -> None:
    """Delete a file from Data Base"""

    sql = "DELETE FROM files WHERE file_id=?"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))


def list_file_ids() -> Set[str]:
    """Return the set of file identifiers currently stored in the database."""

    sql = "SELECT file_id FROM files"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return {row["file_id"] for row in rows}


def get_file(file_id: str) -> Optional[dict]:
    """Retrieve a single file record by its identifier."""

    sql = """
    SELECT file_id, name, path, size, last_modified
    FROM files
    WHERE file_id = ?
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def search_files(*, query: str, limit: int = 10, offset: int = 0) -> List[dict]:
    """Search files by name only using a simple LIKE query."""
    like_query = f"%{query}%"
    sql = """
    SELECT file_id, name, path, size, last_modified
    FROM files
    WHERE name LIKE ?
    ORDER BY name ASC
    LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (like_query, limit, offset))
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def list_files() -> List[dict]:
    """Return all files stored in the database ordered by name."""

    sql = """
    SELECT file_id, name, path, size, last_modified
    FROM files
    ORDER BY name ASC
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return [dict(row) for row in rows]