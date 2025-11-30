import asyncio
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
from server.services.node_manager import get_node_manager, NodeManager
from server.services.sync_service import get_sync_service, SyncService


# Instancias globales
node_manager: NodeManager = get_node_manager()
sync_service: SyncService = get_sync_service()


def on_become_primary():
    """Callback cuando el nodo se convierte en PRIMARY."""
    logger.info("*** ESTE NODO ES AHORA EL PRIMARY ***")
    sync_service.stop()


def on_become_backup():
    """Callback cuando el nodo se convierte en BACKUP."""
    logger.info("*** ESTE NODO ES AHORA BACKUP - Iniciando sincronización ***")


# Registrar callbacks
node_manager.set_callbacks(
    on_become_primary=on_become_primary,
    on_become_backup=on_become_backup
)


@app.on_event("startup")
async def startup_cluster():
    """
    Inicialización del servidor en el clúster:
    1. Esperar a que el DNS esté disponible
    2. Registrarse con el DNS y obtener rol
    3. Iniciar heartbeat loop
    4. Si es BACKUP, iniciar sync loop
    """
    logger.info("=" * 60)
    logger.info("Iniciando servidor en modo clúster...")
    logger.info(f"Server ID: {node_manager.server_id}")
    logger.info("=" * 60)
    
    # Esperar y registrarse con DNS
    success = await node_manager.register()
    
    if not success:
        logger.error("No se pudo registrar con el DNS. Continuando sin clúster.")
    else:
        # Iniciar heartbeat loop
        asyncio.create_task(node_manager.heartbeat_loop())
        
        # Si somos BACKUP, iniciar sincronización
        if node_manager.is_backup:
            asyncio.create_task(
                sync_service.sync_loop(
                    get_primary_url_fn=lambda: node_manager.primary_url
                )
            )
            logger.info("Sync loop iniciado para BACKUP")
    
    logger.info(f"Servidor iniciado. Rol: {node_manager.role}")


@app.on_event("shutdown")
async def shutdown_cluster():
    """Cleanup al cerrar el servidor."""
    logger.info("Cerrando servidor...")
    node_manager.stop()
    sync_service.stop()


@app.get("/node/status")
def get_node_status():
    """Endpoint para obtener el estado del nodo en el clúster."""
    return {
        "node": node_manager.get_status(),
        "sync": sync_service.get_status()
    }


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

    uvicorn.run(app, host=host, port=port, log_config=None)