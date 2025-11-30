import socket
import logging
import os
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .logging_config import configure_logging

# Configurar logs al iniciar
configure_logging()
logger = logging.getLogger("dns_service")

app = FastAPI(title="Internal DNS Service", version="1.0.0")

CLIENT_PRIMARY_HOST = os.getenv("CLIENT_PRIMARY_HOST", "client_primary")
CLIENT_BACKUP_HOST = os.getenv("CLIENT_BACKUP_HOST", "client_backup")
CLIENT_PORT = int(os.getenv("CLIENT_PORT", "8501"))
CLIENT_CHECK_INTERVAL = int(os.getenv("CLIENT_CHECK_INTERVAL", "3"))
_active_client = CLIENT_PRIMARY_HOST
_monitor_task = None

class ResolutionResponse(BaseModel):
    hostname: str
    ip: str
    ttl: int

@app.get("/health")
def health():
    return {"status": "ok", "service": "dns-resolver"}

@app.get("/resolve/{hostname}", response_model=ResolutionResponse)
def resolve_hostname(hostname: str):
    logger.info(f"Solicitud de resolución recibida para: {hostname}")
    try:
        target = hostname
        if hostname == "client":
            target = _active_client
        ip_address = socket.gethostbyname(target)
        return ResolutionResponse(hostname=hostname, ip=ip_address, ttl=3)
    except socket.gaierror as e:
        logger.warning(f"No se pudo resolver el host '{hostname}': {e}")
        raise HTTPException(status_code=404, detail=f"Hostname '{hostname}' not found or unreachable")
    except Exception as e:
        logger.error(f"Error inesperado resolviendo '{hostname}': {e}")
        raise HTTPException(status_code=500, detail="Internal DNS error")

@app.on_event("startup")
async def _start_monitor():
    global _monitor_task
    async def check(host: str) -> bool:
        try:
            url = f"http://{host}:{CLIENT_PORT}/"
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(url)
            return r.status_code == 200
        except Exception:
            return False
    async def monitor():
        global _active_client
        while True:
            p = await check(CLIENT_PRIMARY_HOST)
            b = await check(CLIENT_BACKUP_HOST)
            if p:
                _active_client = CLIENT_PRIMARY_HOST
            elif b:
                _active_client = CLIENT_BACKUP_HOST
            await asyncio.sleep(CLIENT_CHECK_INTERVAL)
    _monitor_task = asyncio.create_task(monitor())

@app.on_event("shutdown")
async def _stop_monitor():
    if _monitor_task:
        _monitor_task.cancel()

@app.get("/client/status")
def client_status():
    return {
        "primary": CLIENT_PRIMARY_HOST,
        "backup": CLIENT_BACKUP_HOST,
        "port": CLIENT_PORT,
        "active": _active_client,
    }

async def _target_base() -> str:
    global _active_client
    try:
        ip = socket.gethostbyname(_active_client)
        return f"http://{ip}:{CLIENT_PORT}"
    except Exception:
        other = CLIENT_BACKUP_HOST if _active_client == CLIENT_PRIMARY_HOST else CLIENT_PRIMARY_HOST
        try:
            ip = socket.gethostbyname(other)
            _active_client = other
            return f"http://{ip}:{CLIENT_PORT}"
        except Exception:
            raise

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DNS_PORT", 5353))
    uvicorn.run(app, host="0.0.0.0", port=port)
