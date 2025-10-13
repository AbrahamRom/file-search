"""Utilities to resolve file downloads safely from the database metadata."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from ..db import crud

logger = logging.getLogger(__name__)


class FileRecordNotFoundError(Exception):
	"""Raised when the file metadata is not present in the database."""


class FileOnDiskNotFoundError(Exception):
	"""Raised when the file path stored in the database is missing on disk."""


@dataclass(frozen=True)
class DownloadTarget:
	"""Information required to serve a file download."""

	path: Path
	filename: str
	media_type: str


def _detect_media_type(filename: str) -> str:
	"""Return a best-effort MIME type for the provided filename."""

	guessed, _ = mimetypes.guess_type(filename)
	return guessed or "application/octet-stream"


def resolve_download(file_id: str) -> DownloadTarget:
	"""Locate the file on disk and return the information for downloading it."""

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
		raise FileOnDiskNotFoundError(path)

	filename = record["name"] or path.name
	media_type = _detect_media_type(filename)

	return DownloadTarget(path=path, filename=filename, media_type=media_type)


__all__ = [
	"DownloadTarget",
	"FileRecordNotFoundError",
	"FileOnDiskNotFoundError",
	"resolve_download",
]
