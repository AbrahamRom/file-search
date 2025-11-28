"""Logging configuration utilities for the DNS service."""

from __future__ import annotations

import logging
import os
from logging.config import dictConfig
from pathlib import Path
from typing import Optional

# La raíz de este servicio es el directorio padre de este archivo (app/dns_service)
_SERVICE_ROOT = Path(__file__).resolve().parent


def _resolve_log_dir(raw_value: Optional[Path | str]) -> Path:
    """Return an absolute path for the log directory."""
    if raw_value in (None, ""):
        return _SERVICE_ROOT / "logs"

    candidate = Path(raw_value)
    if not candidate.is_absolute():
        candidate = (_SERVICE_ROOT / candidate).resolve()

    return candidate


DEFAULT_LOG_DIR = _resolve_log_dir(os.getenv("LOG_DIR"))
DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def configure_logging(
    *,
    log_dir: Optional[Path | str] = None,
    level: Optional[str] = None,
) -> Path:
    """Configure logging for the DNS service."""

    target_dir = _resolve_log_dir(log_dir) if log_dir else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        target_dir.chmod(0o777)
    except Exception:
        pass

    app_log_path = target_dir / "dns_service.log"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
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
                    "backupCount": 3,
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
                    "level": "INFO",
                    "propagate": False,
                },
            },
        }
    )

    logging.getLogger(__name__).info("DNS Service logging configured at %s", target_dir)
    return target_dir