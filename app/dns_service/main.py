import socket
import logging
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .logging_config import configure_logging

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DNS_PORT", 5353))
    uvicorn.run(app, host="0.0.0.0", port=port)