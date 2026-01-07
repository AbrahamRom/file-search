"""
File handler utilities for Storage Node.
Resolves file downloads safely from the database metadata.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from ..db import crud

logger = logging.getLogger(__name__)


class FileRecordNotFoundError(Exception):
    """Raised when the file metadata is not present in the database."""
    pass


class FileOnDiskNotFoundError(Exception):
    """Raised when the file path stored in the database is missing on disk."""
    pass


@dataclass(frozen=True)
class DownloadTarget:
    """Information required to serve a file download."""
    path: Path
    filename: str
    media_type: str
    size: int


def _detect_media_type(filename: str) -> str:
    """Return a best-effort MIME type for the provided filename."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def resolve_download(file_id: str) -> DownloadTarget:
    """
    Locate the file on disk and return the information for downloading it.
    
    SECURITY: Implements defense-in-depth with 3 validation layers:
    1. Validates file_id format (SHA1 hash: 40 hex characters)
    2. Validates path doesn't contain dangerous sequences
    3. Validates canonicalized path is within FILES_ROOT
    
    Args:
        file_id: The unique identifier of the file (SHA1 hash)
        
    Returns:
        DownloadTarget with path, filename, media_type, and size
        
    Raises:
        FileRecordNotFoundError: If file metadata not in database or validation fails
        FileOnDiskNotFoundError: If file not found on disk
    """
    # LAYER 1: Validate file_id format (must be 40-character hex string)
    if not isinstance(file_id, str) or len(file_id) != 40:
        logger.warning("[SECURITY] Invalid file_id format: %s", file_id)
        raise FileRecordNotFoundError(f"Invalid file_id format")
    
    if not all(c in '0123456789abcdef' for c in file_id.lower()):
        logger.warning("[SECURITY] file_id contains non-hex characters: %s", file_id)
        raise FileRecordNotFoundError(f"Invalid file_id format")
    
    # Get record from database
    record = crud.get_file(file_id)
    if not record:
        logger.debug("Download requested for unknown file_id=%s", file_id)
        raise FileRecordNotFoundError(file_id)

    stored_path = record["path"]
    
    # LAYER 2: Validate that path doesn't contain dangerous sequences
    if ".." in stored_path or stored_path.startswith("/etc") or stored_path.startswith("/root"):
        logger.warning(
            "[SECURITY] Path traversal attempt detected: file_id=%s, path=%s",
            file_id,
            stored_path,
        )
        raise FileRecordNotFoundError(f"Invalid path")
    
    # LAYER 3: Validate canonicalized path is within FILES_ROOT
    from ..services.scanner import FILES_ROOT
    
    base_path = Path(FILES_ROOT).resolve()
    target_path = Path(stored_path).resolve()
    
    # Verify that target_path is a child of base_path
    try:
        target_path.relative_to(base_path)
    except ValueError:
        # Path is outside FILES_ROOT
        logger.error(
            "[SECURITY] Path outside FILES_ROOT: file_id=%s, "
            "path=%s, resolved=%s, base=%s",
            file_id,
            stored_path,
            target_path,
            base_path,
        )
        raise FileRecordNotFoundError(f"Path traversal blocked")
    
    # Validate file exists on disk
    if not target_path.is_file():
        logger.warning(
            "File metadata found but missing on disk: file_id=%s path=%s",
            file_id,
            target_path,
        )
        raise FileOnDiskNotFoundError(str(target_path))

    filename = record["name"] or target_path.name
    media_type = _detect_media_type(filename)
    size = record.get("size", target_path.stat().st_size)

    return DownloadTarget(
        path=target_path,
        filename=filename,
        media_type=media_type,
        size=size,
    )


def get_file_content(file_id: str) -> bytes:
    """
    Read and return the entire content of a file.
    
    Args:
        file_id: The unique identifier of the file
        
    Returns:
        File content as bytes
        
    Raises:
        FileRecordNotFoundError: If file metadata not in database
        FileOnDiskNotFoundError: If file not found on disk
    """
    target = resolve_download(file_id)
    return target.path.read_bytes()


def file_exists(file_id: str) -> bool:
    """
    Check if a file exists in the database and on disk.
    
    Args:
        file_id: The unique identifier of the file
        
    Returns:
        True if file exists in DB and on disk, False otherwise
    """
    record = crud.get_file(file_id)
    if not record:
        return False
    
    path = Path(record["path"])
    return path.is_file()


__all__ = [
    "DownloadTarget",
    "FileRecordNotFoundError",
    "FileOnDiskNotFoundError",
    "resolve_download",
    "get_file_content",
    "file_exists",
]
