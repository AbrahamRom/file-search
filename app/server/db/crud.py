from typing import List
from db import get_conn

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
        conn.commit()

def delete_file(*,file_id:str) -> None:
    """Delete a file from Data Base"""

    sql = "DELETE FROM files WHERE file_id=?"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))
        conn.commit()

def search_files(*, query: str, limit: int = 10, offset: int = 0) -> List[dict]:
    """Search files by name or path using a simple LIKE query."""
    like_query = f"%{query}%"
    sql = """
    SELECT file_id, name, path, size, last_modified
    FROM files
    WHERE name LIKE ? OR path LIKE ?
    ORDER BY name ASC
    LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (like_query, like_query, limit, offset))
        rows = cur.fetchall()
        return [dict(row) for row in rows]