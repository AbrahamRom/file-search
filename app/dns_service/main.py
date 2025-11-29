import socket
import logging
import os
import threading
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .logging_config import configure_logging

# Docker SDK (habilitado por requirements)
try:
    import docker  # type: ignore
except Exception:
    docker = None

# Configurar logs al iniciar
configure_logging()
logger = logging.getLogger("dns_service")

app = FastAPI(title="Internal DNS Service", version="1.0.0")

class ResolutionResponse(BaseModel):
    hostname: str
    ip: str
    ttl: int

@app.get("/health")
def health():
    return {"status": "ok", "service": "dns-resolver"}

@app.get("/resolve/{hostname}", response_model=ResolutionResponse)
def resolve_hostname(hostname: str):
    """
    Resuelve un nombre de host a su dirección IP utilizando el DNS del entorno (Docker).
    """
    logger.info(f"Solicitud de resolución recibida para: {hostname}")
    
    try:
        # Esta llamada usa el resolver del sistema (en el contenedor, el DNS de Docker 127.0.0.11)
        ip_address = socket.gethostbyname(hostname)
        
        logger.info(f"Resolución exitosa: {hostname} -> {ip_address}")
        
        return ResolutionResponse(
            hostname=hostname, 
            ip=ip_address, 
            ttl=300  # 5 minutos de caché sugerido
        )
        
    except socket.gaierror as e:
        logger.warning(f"No se pudo resolver el host '{hostname}': {e}")
        raise HTTPException(status_code=404, detail=f"Hostname '{hostname}' not found or unreachable")
    except Exception as e:
        logger.error(f"Error inesperado resolviendo '{hostname}': {e}")
        raise HTTPException(status_code=500, detail="Internal DNS error")


# --- Watchdog: asegurar que el cliente esté levantado ---
def _ensure_client_running():
    """Verifica si el contenedor del cliente está corriendo; si no, intenta arrancarlo.

    Busca por etiqueta de docker-compose: com.docker.compose.service=client.
    """
    if docker is None:
        logger.debug("Docker SDK no disponible; watchdog deshabilitado.")
        return

    try:
        client = docker.from_env()
        # Filtrar por servicio de compose
        containers = client.containers.list(all=True, filters={
            "label": ["com.docker.compose.service=client"]
        })

        running = [c for c in containers if getattr(c, "status", "") == "running"]
        if running:
            return

        # Arrancar el primero que esté detenido
        for c in containers:
            status = getattr(c, "status", "")
            if status in ("exited", "created"):
                c.start()
                logger.info("Watchdog: cliente '%s' arrancado", c.name)
                return

        # Si no hay contenedores del cliente, registrar advertencia
        if not containers:
            logger.warning("Watchdog: no se encontraron contenedores del servicio 'client'.")
    except Exception as e:
        logger.error("Watchdog: error asegurando cliente: %s", e)


def _start_watchdog():
    enabled = os.getenv("ENABLE_CLIENT_WATCHDOG", "true").lower() in ("1", "true", "yes")
    interval = int(os.getenv("CLIENT_WATCHDOG_INTERVAL", "5"))
    if not enabled:
        logger.info("Watchdog de cliente deshabilitado por configuración.")
        return

    def _loop():
        # Pequeño delay para esperar a que compose cree contenedores
        time.sleep(3)
        while True:
            try:
                _ensure_client_running()
            except Exception as e:
                logger.error("Watchdog loop error: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()


@app.on_event("startup")
def _on_startup():
    _start_watchdog()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DNS_PORT", 5353))
    uvicorn.run(app, host="0.0.0.0", port=port)