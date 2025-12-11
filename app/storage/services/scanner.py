"""
File scanner utilities for Storage Node.
Scans the files directory and synchronizes it with the database.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Set, Optional

from ..db import crud

logger = logging.getLogger(__name__)

# Determine the files root directory
_env_root = os.getenv("FILES_ROOT")
_volume_root = Path("/app/files")
_fallback_root = Path(__file__).resolve().parents[2] / "files"

if _env_root:
    FILES_ROOT = Path(_env_root)
elif _volume_root.exists():
    FILES_ROOT = _volume_root
else:
    FILES_ROOT = _fallback_root


@dataclass(frozen=True)
class FileRecord:
    """Metadata describing a file that will be stored in the database."""
    file_id: str
    name: str
    path: str
    size: int
    last_modified: datetime
    shard_id: Optional[str] = None


def compute_file_id(relative_path: str) -> str:
    """
    Return a stable identifier for a file based on its relative path.
    Uses SHA1 hash for consistent, collision-resistant IDs.
    """
    digest = hashlib.sha1(relative_path.encode("utf-8"))
    return digest.hexdigest()


def compute_shard_key(filename: str, strategy: str = "alpha") -> str:
    """
    Compute shard key for a file based on the sharding strategy.
    
    Strategies:
        - "alpha": Alphabetical range (A-M, N-Z)
        - "hash": Hash-based distribution
        - "none": No sharding (returns None)
    
    For future use when implementing horizontal partitioning.
    """
    if strategy == "none":
        return None
    
    if strategy == "alpha":
        first_char = filename[0].upper() if filename else "A"
        if first_char < "N":
            return "shard_a_m"
        else:
            return "shard_n_z"
    
    if strategy == "hash":
        hash_val = int(hashlib.md5(filename.encode()).hexdigest(), 16)
        # Distribute across N shards
        num_shards = int(os.getenv("NUM_SHARDS", 2))
        return f"shard_{hash_val % num_shards}"
    
    return None


def _iter_file_records(
    root: Path,
    shard_id: Optional[str] = None,
) -> Iterator[FileRecord]:
    """
    Yield FileRecord instances for every file under root.
    
    Sub-directories are traversed recursively. Hidden files/folders are included.
    """
    if not root.exists():
        logger.info("Files root %s does not exist; creating it", root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o777)
        except Exception:
            pass

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue

        relative_path = file_path.relative_to(root).as_posix()
        stat = file_path.stat()
        last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        yield FileRecord(
            file_id=compute_file_id(relative_path),
            name=file_path.name,
            path=str(file_path.resolve()),
            size=stat.st_size,
            last_modified=last_modified,
            shard_id=shard_id,
        )


def scan(
    root: Optional[Path] = None,
    shard_id: Optional[str] = None,
) -> List[FileRecord]:
    """
    Return the list of files detected under root (defaults to FILES_ROOT).
    
    Args:
        root: Directory to scan (defaults to FILES_ROOT)
        shard_id: Optional shard identifier for the scanned files
    """
    target_root = root or FILES_ROOT
    logger.debug("Scanning files under %s", target_root)
    return list(_iter_file_records(target_root, shard_id))


def sync(
    root: Optional[Path] = None,
    shard_id: Optional[str] = None,
) -> Dict[str, int]:
    """
    Synchronize the database with the files currently present on disk.
    
    Returns a dictionary summarizing the operation:
        - discovered: number of files found on disk
        - upserted: number of records upserted in the database
        - deleted: number of records removed for files no longer present
    """
    target_root = root or FILES_ROOT
    records = scan(target_root, shard_id)

    logger.info("Synchronizing %d files into the database", len(records))

    existing_ids: Set[str] = crud.list_file_ids()
    seen_ids: Set[str] = set()
    upserted = 0

    for record in records:
        seen_ids.add(record.file_id)
        crud.upsert_file(
            file_id=record.file_id,
            name=record.name,
            path=record.path,
            size=record.size,
            last_modified=record.last_modified,
            shard_id=record.shard_id,
        )
        upserted += 1

    deleted = 0
    for stale_id in existing_ids - seen_ids:
        crud.delete_file(file_id=stale_id)
        deleted += 1

    logger.info(
        "Scan complete: discovered=%d, upserted=%d, deleted=%d",
        len(records),
        upserted,
        deleted,
    )

    return {
        "discovered": len(records),
        "upserted": upserted,
        "deleted": deleted,
    }


__all__ = [
    "FileRecord",
    "FILES_ROOT",
    "compute_file_id",
    "compute_shard_key",
    "scan",
    "sync",
]
