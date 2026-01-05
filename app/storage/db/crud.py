"""
CRUD operations for Storage Node database.
All file metadata operations are centralized here.
"""

import json
from typing import List, Optional, Set, Dict

from .db import get_conn


def upsert_file(
    *,
    file_id: str,
    name: str,
    path: str,
    size: int,
    last_modified,
    shard_id: Optional[str] = None,
    content_hash: Optional[str] = None,
    version: Optional[int] = None,
    origin_node: Optional[str] = None,
    write_epoch: Optional[int] = None,
) -> None:
    """
    Insert or update a file record in the files table.
    last_modified may be a datetime or an ISO-8601 string.
    
    Args:
        file_id: Unique identifier for the file (SHA1 of relative path)
        name: File name
        path: Absolute path to file on disk
        size: File size in bytes
        last_modified: Last modification timestamp
        shard_id: Optional shard identifier for future sharding support
        content_hash: SHA256 hash of content for conflict detection
        version: Version counter (auto-increments if not provided)
        origin_node: Node that wrote this version
        write_epoch: Primary epoch when written (for fencing)
    """
    # Normalize last_modified to ISO string if it's a datetime-like object
    if hasattr(last_modified, "isoformat"):
        last_modified = last_modified.isoformat()

    # Get current version if updating
    if version is None:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT version FROM files WHERE file_id = ?", (file_id,))
            row = cur.fetchone()
            version = (row["version"] + 1) if row else 1

    sql = """
    INSERT INTO files (file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(file_id) DO UPDATE SET
        name = excluded.name,
        path = excluded.path,
        size = excluded.size,
        last_modified = excluded.last_modified,
        shard_id = excluded.shard_id,
        content_hash = excluded.content_hash,
        version = excluded.version,
        origin_node = excluded.origin_node,
        write_epoch = excluded.write_epoch
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch))


def delete_file(*, file_id: str) -> bool:
    """
    Delete a file record from the database.
    
    Returns:
        True if a record was deleted, False otherwise.
    """
    sql = "DELETE FROM files WHERE file_id = ?"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))
        return cur.rowcount > 0


def get_file(file_id: str) -> Optional[dict]:
    """
    Retrieve a single file record by its identifier.
    
    Returns:
        Dictionary with file metadata or None if not found.
    """
    sql = """
    SELECT file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch
    FROM files
    WHERE file_id = ?
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_file_ids() -> Set[str]:
    """
    Return the set of file identifiers currently stored in the database.
    """
    sql = "SELECT file_id FROM files"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return {row["file_id"] for row in rows}


def list_files(*, shard_id: Optional[str] = None) -> List[dict]:
    """
    Return all files stored in the database ordered by name.
    
    Args:
        shard_id: Optional filter by shard (for future sharding)
    """
    if shard_id:
        sql = """
        SELECT file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch
        FROM files
        WHERE shard_id = ?
        ORDER BY name ASC
        """
        params = (shard_id,)
    else:
        sql = """
        SELECT file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch
        FROM files
        ORDER BY name ASC
        """
        params = ()

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def search_files(
    *,
    query: str,
    limit: int = 10,
    offset: int = 0,
    shard_id: Optional[str] = None,
) -> List[dict]:
    """
    Search files by name using a simple LIKE query.
    
    Args:
        query: Search term (partial match on name)
        limit: Maximum number of results
        offset: Number of results to skip
        shard_id: Optional filter by shard
    """
    like_query = f"%{query}%"
    
    if shard_id:
        sql = """
        SELECT file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch
        FROM files
        WHERE name LIKE ? AND shard_id = ?
        ORDER BY name ASC
        LIMIT ? OFFSET ?
        """
        params = (like_query, shard_id, limit, offset)
    else:
        sql = """
        SELECT file_id, name, path, size, last_modified, shard_id, content_hash, version, origin_node, write_epoch
        FROM files
        WHERE name LIKE ?
        ORDER BY name ASC
        LIMIT ? OFFSET ?
        """
        params = (like_query, limit, offset)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def get_file_count() -> int:
    """Return the total number of files in the database."""
    sql = "SELECT COUNT(*) as count FROM files"
    
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        return row["count"] if row else 0


# ============================================================================
# Shard and storage node metadata
# ============================================================================

def upsert_storage_node(*, node_id: str, host_id: str, status: str = "unknown", last_heartbeat: Optional[str] = None) -> None:
    """Register or update a storage node with host affinity."""
    sql = """
    INSERT INTO storage_nodes (node_id, host_id, status, last_heartbeat)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(node_id) DO UPDATE SET
        host_id = excluded.host_id,
        status = excluded.status,
        last_heartbeat = excluded.last_heartbeat,
        updated_at = CURRENT_TIMESTAMP
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (node_id, host_id, status, last_heartbeat))


def list_storage_nodes() -> List[Dict]:
    """Return all known storage nodes."""
    sql = """
    SELECT node_id, host_id, status, last_heartbeat, updated_at
    FROM storage_nodes
    ORDER BY node_id
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def upsert_shard(*, shard_id: str, primary_id: str, replica_ids: List[str], epoch: int = 0) -> None:
    """Create or update shard placement (replica_ids must include primary)."""
    replica_json = json.dumps(replica_ids)
    sql = """
    INSERT INTO shards (shard_id, primary_id, replica_ids, epoch)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(shard_id) DO UPDATE SET
        primary_id = excluded.primary_id,
        replica_ids = excluded.replica_ids,
        epoch = excluded.epoch,
        updated_at = CURRENT_TIMESTAMP
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (shard_id, primary_id, replica_json, epoch))


def get_shard(shard_id: str) -> Optional[Dict]:
    """Return shard placement info."""
    sql = """
    SELECT shard_id, primary_id, replica_ids, epoch, updated_at
    FROM shards
    WHERE shard_id = ?
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (shard_id,))
        row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["replica_ids"] = json.loads(data.get("replica_ids", "[]"))
        except json.JSONDecodeError:
            data["replica_ids"] = []
        return data


def list_shards() -> List[Dict]:
    """Return all shard placements."""
    sql = """
    SELECT shard_id, primary_id, replica_ids, epoch, updated_at
    FROM shards
    ORDER BY shard_id
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        results: List[Dict] = []
        for row in rows:
            data = dict(row)
            try:
                data["replica_ids"] = json.loads(data.get("replica_ids", "[]"))
            except json.JSONDecodeError:
                data["replica_ids"] = []
            results.append(data)
        return results


# =========================================================================
# Sync metadata helpers (reusable key/value store)
# =========================================================================

def set_sync_metadata(*, key: str, value: str) -> None:
    """Insert or update a sync metadata entry."""
    sql = """
    INSERT INTO sync_metadata (key, value)
    VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET
        value = excluded.value,
        updated_at = CURRENT_TIMESTAMP
    """

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (key, value))


def get_sync_metadata(key: str) -> Optional[str]:
    """Return the stored metadata value for the given key (or None)."""
    sql = "SELECT value FROM sync_metadata WHERE key = ?"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (key,))
        row = cur.fetchone()
        return row[0] if row else None


# ============================================================================
# FUTURE: Full-text search support (índice invertido)
# ============================================================================

def upsert_keyword(*, keyword: str, file_id: str, frequency: int = 1) -> None:
    """
    Insert or update a keyword-file relationship for full-text search.
    (To be implemented when indexing is enabled)
    """
    sql = """
    INSERT INTO file_keywords (keyword, file_id, frequency)
    VALUES (?, ?, ?)
    ON CONFLICT(keyword, file_id) DO UPDATE SET
        frequency = excluded.frequency
    """
    
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (keyword, file_id, frequency))


def search_by_content(
    *,
    keywords: List[str],
    limit: int = 10,
    offset: int = 0,
) -> List[dict]:
    """
    Search files by content keywords using the inverted index.
    (To be implemented when indexing is enabled)
    
    Returns files that contain ALL specified keywords, ranked by total frequency.
    """
    if not keywords:
        return []
    
    placeholders = ",".join("?" * len(keywords))
    sql = f"""
    SELECT f.file_id, f.name, f.path, f.size, f.last_modified, f.shard_id,
           SUM(fk.frequency) as relevance
    FROM files f
    INNER JOIN file_keywords fk ON f.file_id = fk.file_id
    WHERE fk.keyword IN ({placeholders})
    GROUP BY f.file_id
    HAVING COUNT(DISTINCT fk.keyword) = ?
    ORDER BY relevance DESC
    LIMIT ? OFFSET ?
    """
    
    params = (*keywords, len(keywords), limit, offset)
    
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def clear_keywords_for_file(*, file_id: str) -> None:
    """Delete all keyword associations for a file (before re-indexing)."""
    sql = "DELETE FROM file_keywords WHERE file_id = ?"
    
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (file_id,))


__all__ = [
    "upsert_file",
    "delete_file",
    "get_file",
    "list_files",
    "list_file_ids",
    "search_files",
    "get_file_count",
    "upsert_storage_node",
    "list_storage_nodes",
    "upsert_shard",
    "get_shard",
    "list_shards",
    "set_sync_metadata",
    "get_sync_metadata",
    # Future full-text search
    "upsert_keyword",
    "search_by_content",
    "clear_keywords_for_file",
]
