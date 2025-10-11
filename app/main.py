import logging
import os
import sys

# Asegurar que la carpeta "server" esté en sys.path para resolver "import api.*"
BASE_DIR = os.path.dirname(__file__)
SERVER_DIR = os.path.join(BASE_DIR, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from server.logging_config import configure_logging

configure_logging()

logger = logging.getLogger(__name__)

from server.api.endpoints import app


def _get_server_runtime_config() -> tuple[str, int]:
    host = os.getenv("API_HOST", "0.0.0.0")
    port_raw = os.getenv("API_PORT", "8000")

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        logger.warning(
            "API_PORT='%s' no es un entero válido. Se utilizará el puerto por defecto 8000.",
            port_raw,
        )
        port = 8000

    return host, port

if __name__ == "__main__":
    import importlib

    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:  # pragma: no cover - dev convenience
        raise SystemExit(
            "uvicorn no está instalado. Ejecuta `pip install uvicorn` dentro del contenedor."
        ) from exc

    host, port = _get_server_runtime_config()

    uvicorn.run(app, host=host, port=port)