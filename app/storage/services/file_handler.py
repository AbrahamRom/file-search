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
    
    Args:
        file_id: The unique identifier of the file
        
    Returns:
        DownloadTarget with path, filename, media_type, and size
        
    Raises:
        FileRecordNotFoundError: If file metadata not in database
        FileOnDiskNotFoundError: If file not found on disk
    """
    record = crud.get_file(file_id)
    if not record:
        logger.debug("Download requested for unknown file_id=%s", file_id)
        raise FileRecordNotFoundError(file_id)

    path = Path(record["path"])
    if not path.is_file():
        logger.warning(
            "File metadata found but missing on disk: file_id=%s path=%s",
            file_id,
            path,
        )
        raise FileOnDiskNotFoundError(str(path))

    filename = record["name"] or path.name
    media_type = _detect_media_type(filename)
    size = record.get("size", path.stat().st_size)

    return DownloadTarget(
        path=path,
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
