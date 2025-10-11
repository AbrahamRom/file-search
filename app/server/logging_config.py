"""Logging configuration utilities for the file-search application."""

from __future__ import annotations

import logging
import os
from logging.config import dictConfig
from pathlib import Path
from typing import Optional

DEFAULT_LOG_DIR = Path(
    os.getenv("LOG_DIR", Path(__file__).resolve().parents[1] / "logs")
)
DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DEFAULT_ACCESS_LOG_LEVEL = os.getenv("ACCESS_LOG_LEVEL", "INFO")


def configure_logging(
    *,
    log_dir: Optional[Path | str] = None,
    level: Optional[str] = None,
    access_level: Optional[str] = None,
) -> Path:
    """Configure logging for the application and return the resolved log directory."""

    target_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    app_log_path = target_dir / "application.log"
    access_log_path = target_dir / "access.log"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
                "access": {
                    "format": "%(asctime)s [%(levelname)s] %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": "DEBUG",
                    "formatter": "standard",
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": level or DEFAULT_LOG_LEVEL,
                    "formatter": "standard",
                    "filename": str(app_log_path),
                    "maxBytes": 5 * 1024 * 1024,  # 5 MB
                    "backupCount": 5,
                    "encoding": "utf-8",
                },
                "access_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": access_level or DEFAULT_ACCESS_LOG_LEVEL,
                    "formatter": "access",
                    "filename": str(access_log_path),
                    "maxBytes": 5 * 1024 * 1024,
                    "backupCount": 5,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                "": {
                    "handlers": ["console", "file"],
                    "level": level or DEFAULT_LOG_LEVEL,
                    "propagate": False,
                },
                "uvicorn": {
                    "handlers": ["console", "file"],
                    "level": level or DEFAULT_LOG_LEVEL,
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["console", "file"],
                    "level": level or DEFAULT_LOG_LEVEL,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["console", "access_file"],
                    "level": access_level or DEFAULT_ACCESS_LOG_LEVEL,
                    "propagate": False,
                },
            },
        }
    )

    logging.getLogger(__name__).info("Logging configured; writing to %s", target_dir)
    return target_dir


__all__ = ["configure_logging", "DEFAULT_LOG_DIR"]
