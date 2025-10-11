import os
import sys

# Asegurar que la carpeta "server" esté en sys.path para resolver "import api.*"
BASE_DIR = os.path.dirname(__file__)
SERVER_DIR = os.path.join(BASE_DIR, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from server.logging_config import configure_logging

configure_logging()

from server.api.endpoints import app

if __name__ == "__main__":
    import importlib

    try:
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:  # pragma: no cover - dev convenience
        raise SystemExit(
            "uvicorn no está instalado. Ejecuta `pip install uvicorn` dentro del contenedor."
        ) from exc

    uvicorn.run(app, host="0.0.0.0", port=8000)