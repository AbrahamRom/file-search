"""
Main entry point for Processor Node.
Runs the FastAPI application with Uvicorn.
"""

import asyncio
import importlib
import logging
import os
import sys

# Ensure the app directory is in the path
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Configure logging - use relative import
from .logging_config import configure_logging
configure_logging()

logger = logging.getLogger(__name__)

from .api.endpoints import app


def _get_server_config() -> tuple[str, int]:
    """Get server host and port from environment."""
    host = os.getenv("PROCESSOR_HOST", "0.0.0.0")
    port_raw = os.getenv("PROCESSOR_PORT", "8000")

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        logger.warning(
            "PROCESSOR_PORT='%s' is not a valid integer. Using default 8000.",
            port_raw,
        )
        port = 8000

    return host, port


if __name__ == "__main__":
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "uvicorn is not installed. Run `pip install uvicorn`."
        ) from exc

    host, port = _get_server_config()
    
    logger.info("Starting Processor Node on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_config=None)
