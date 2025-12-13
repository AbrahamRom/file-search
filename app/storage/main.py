"""
Main entry point for Storage Node.
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
from .services.node_manager import get_node_manager, NodeManager
from .services.sync_service import get_sync_service, SyncService


# Global instances
node_manager: NodeManager = get_node_manager()
sync_service: SyncService = get_sync_service()

# Task handle for sync loop (to avoid starting multiple)
_sync_task: asyncio.Task = None


def on_become_primary():
    """Callback when this node becomes PRIMARY."""
    global _sync_task
    logger.info("*** THIS NODE IS NOW PRIMARY ***")
    sync_service.stop()
    _sync_task = None


def on_become_backup():
    """
    Callback when this node becomes BACKUP.
    Starts sync loop and triggers immediate full sync for reconciliation.
    """
    global _sync_task
    logger.info("*** THIS NODE IS NOW BACKUP - Starting synchronization ***")
    
    # Schedule immediate sync and start sync loop
    async def _start_backup_sync():
        global _sync_task
        # First, do an immediate full sync for reconciliation
        primary_url = node_manager.primary_url
        if primary_url:
            logger.info("[RECONCILE] Triggering immediate full sync after role change to BACKUP")
            sync_service.set_primary_url(primary_url)
            await sync_service.full_sync()
        
        # Then start the sync loop if not already running
        if _sync_task is None or _sync_task.done():
            _sync_task = asyncio.create_task(
                sync_service.sync_loop(
                    get_primary_url_fn=lambda: node_manager.primary_url
                )
            )
            logger.info("Sync loop started for BACKUP node")
    
    # Schedule the async work
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_start_backup_sync())
        else:
            loop.run_until_complete(_start_backup_sync())
    except RuntimeError:
        # No event loop, will be handled by startup
        pass


# Register callbacks
node_manager.set_callbacks(
    on_become_primary=on_become_primary,
    on_become_backup=on_become_backup,
)


@app.on_event("startup")
async def startup_cluster():
    """
    Initialize the Storage Node in the cluster:
    1. Wait for DNS to be available
    2. Register with DNS and get role assignment
    3. Start heartbeat loop
    4. If BACKUP, start sync loop
    """
    global _sync_task
    
    logger.info("=" * 60)
    logger.info("Starting Storage Node in cluster mode...")
    logger.info(f"Storage ID: {node_manager.server_id}")
    logger.info("=" * 60)
    
    # Wait and register with DNS
    success = await node_manager.register()
    
    if not success:
        logger.error("Could not register with DNS. Continuing without cluster.")
    else:
        # Start heartbeat loop
        asyncio.create_task(node_manager.heartbeat_loop())
        
        # If we're BACKUP, start synchronization
        if node_manager.is_backup:
            # Immediate full sync for initial reconciliation
            primary_url = node_manager.primary_url
            if primary_url:
                logger.info("[RECONCILE] Initial full sync as BACKUP node")
                sync_service.set_primary_url(primary_url)
                await sync_service.full_sync()
            
            _sync_task = asyncio.create_task(
                sync_service.sync_loop(
                    get_primary_url_fn=lambda: node_manager.primary_url
                )
            )
            logger.info("Sync loop started for BACKUP node")
    
    logger.info(f"Storage Node started. Role: {node_manager.role}")


@app.on_event("shutdown")
async def shutdown_cluster():
    """Cleanup on shutdown."""
    logger.info("Shutting down Storage Node...")
    node_manager.stop()
    sync_service.stop()


@app.get("/node/status")
def get_node_status():
    """Get the status of this node in the cluster."""
    return {
        "node": node_manager.get_status(),
        "sync": sync_service.get_status(),
    }


def _get_server_config() -> tuple[str, int]:
    """Get server host and port from environment."""
    host = os.getenv("STORAGE_HOST", "0.0.0.0")
    port_raw = os.getenv("STORAGE_PORT", os.getenv("SERVER_PORT", "8000"))

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        logger.warning(
            "STORAGE_PORT='%s' is not a valid integer. Using default 8000.",
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
    
    logger.info("Starting Storage Node on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_config=None)
