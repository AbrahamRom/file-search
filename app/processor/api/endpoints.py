"""
Processor Node API - Public endpoints for clients.

This is the main entry point for client requests. The Processor Node:
1. Receives requests from clients
2. Resolves Storage Node via DNS
3. Routes requests to the Storage Node
4. Handles failover transparently (via DNS re-resolution)

The Processor Node is STATELESS:
- Does NOT know about PRIMARY/BACKUP
- Does NOT maintain a list of Storage Nodes
- Only asks DNS: "Where is the Storage Node?"
- DNS handles failover and returns the active node
"""

import asyncio
import importlib
import logging
import os
from datetime import datetime
from typing import List, Optional
from contextlib import asynccontextmanager
import time
import mimetypes
import socket
import httpx

try:
    fastapi = importlib.import_module("fastapi")
    pydantic = importlib.import_module("pydantic")
    cors_middleware = importlib.import_module("fastapi.middleware.cors")
    slowapi = importlib.import_module("slowapi")
    slowapi_util = importlib.import_module("slowapi.util")
    slowapi_errors = importlib.import_module("slowapi.errors")
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing required dependencies. Install fastapi, pydantic and slowapi."
    ) from exc

FastAPI = fastapi.FastAPI
Query = fastapi.Query
Request = fastapi.Request
HTTPException = fastapi.HTTPException
Response = fastapi.Response
StreamingResponse = fastapi.responses.StreamingResponse
BaseModel = pydantic.BaseModel
UploadFile = fastapi.UploadFile
File = fastapi.File
Form = fastapi.Form
CORSMiddleware = cors_middleware.CORSMiddleware
Limiter = slowapi.Limiter
get_remote_address = slowapi_util.get_remote_address
RateLimitExceeded = slowapi_errors.RateLimitExceeded

from ..services.storage_client import (
    StorageClient,
    StorageError,
    StorageUnavailableError,
    get_storage_client,
    init_storage_client,
)
from ..services.circuit_breaker import get_circuit_breaker_registry
from common.cors_config import get_cors_config

# Importar DNSClientHA común
from common.resolver import DNSClientHA

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("processor.access")

# Processor Node identifier
PROCESSOR_ID = os.getenv("PROCESSOR_ID", "processor_1")

# DNS configuration for discovering Storage Nodes
DNS_ALIAS = os.getenv("DNS_ALIAS", "dns")
DNS_PORT = int(os.getenv("DNS_SERVICE_PORT", 5353))

# Heartbeat interval for DNS registration
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 5))

# External port (accessible from outside Docker, e.g., from browser)
PROCESSOR_EXTERNAL_PORT = os.getenv("PROCESSOR_EXTERNAL_PORT")  # Optional

# External IP (accessible from outside Docker, e.g., from browser - host's public IP)
PROCESSOR_EXTERNAL_IP = os.getenv("PROCESSOR_EXTERNAL_IP")  # Optional

# Flag para controlar el loop de heartbeat
_heartbeat_task: Optional[asyncio.Task] = None

# Cache de DNS URL saludable (para registro/heartbeat)
_cached_dns_url: Optional[str] = None
_cached_dns_ts: float = 0.0
_dns_url_cache_ttl: float = float(os.getenv("DNS_URL_CACHE_TTL", 10))


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class FileMetadata(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    last_modified: datetime
    shard_id: Optional[str] = None


class SearchResponse(BaseModel):
    results: List[FileMetadata]
    total: int
    query: str
    limit: int
    offset: int


class UploadResponse(BaseModel):
    status: str
    file_id: str
    filename: str
    size: int
    storage_id: str


class ProcessorStatus(BaseModel):
    processor_id: str
    status: str
    current_storage: Optional[dict]
    dns_cache: dict
    circuit_breakers: dict


class HealthResponse(BaseModel):
    status: str
    processor_id: str
    storage_available: bool
    dns_healthy: bool


# ============================================================================
# STORAGE CLIENT INITIALIZATION (via DNS)
# ============================================================================

_storage_client: Optional[StorageClient] = None
_dns_client: Optional[DNSClientHA] = None


# ============================================================================
# DNS REGISTRATION FOR PROCESSORS
# ============================================================================

async def _get_my_ip() -> str:
    """
    Get this processor's hostname for registration with DNS.
    
    In Docker overlay networks, we use the container hostname (e.g., processor_1)
    instead of IP addresses because Docker's internal DNS resolves hostnames
    within the overlay network correctly.
    """
    import socket
    
    # First, try to use the hostname directly (works best in Docker overlay networks)
    hostname = socket.gethostname()
    
    # If hostname looks like a container name, use it
    if hostname and not hostname.startswith("localhost"):
        # Verify we can resolve this hostname
        try:
            socket.gethostbyname(hostname)
            return hostname
        except socket.gaierror:
            pass  # Fall through to IP detection
    
    # Fallback: try to get IP address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((DNS_ALIAS, DNS_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(hostname) if hostname else "127.0.0.1"


async def _discover_dns_url() -> Optional[str]:
    """Discover a healthy DNS server URL (with failover across all alias IPs)."""
    global _cached_dns_url, _cached_dns_ts

    # Solo validar cache si no ha expirado el TTL
    if _cached_dns_url and (time.time() - _cached_dns_ts) < _dns_url_cache_ttl:
        return _cached_dns_url

    import socket
    import httpx

    # Intentar descubrir con reintentos
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            results = socket.getaddrinfo(DNS_ALIAS, DNS_PORT, socket.AF_INET, socket.SOCK_STREAM)
            ips = sorted(set(result[4][0] for result in results))
        except Exception as e:
            logger.warning("Error discovering DNS (attempt %d/%d): %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                await asyncio.sleep(1 * (attempt + 1))  # Backoff exponencial
                continue
            ips = []

        if not ips:
            if attempt < max_attempts - 1:
                logger.debug("No DNS IPs found, retrying in %ds...", 1 * (attempt + 1))
                await asyncio.sleep(1 * (attempt + 1))
                continue
            return None

        async with httpx.AsyncClient(timeout=2.0) as client:
            for ip in ips:
                url = f"http://{ip}:{DNS_PORT}"
                try:
                    resp = await client.get(f"{url}/health")
                    if resp.status_code == 200:
                        _cached_dns_url = url
                        _cached_dns_ts = time.time()
                        logger.info("Discovered healthy DNS at: %s", url)
                        return url
                except Exception as ex:
                    logger.debug("DNS %s health check failed: %s", url, ex)
                    continue
        
        # Si llegamos aquí, ningún servidor respondió en este intento
        if attempt < max_attempts - 1:
            logger.warning("All DNS servers unreachable (attempt %d/%d), retrying...", attempt + 1, max_attempts)
            await asyncio.sleep(2 * (attempt + 1))

    logger.error("Failed to discover any healthy DNS server after %d attempts", max_attempts)
    return None


async def _register_with_dns():
    """Register this processor with the DNS service."""
    import httpx
    
    # Reintentar registro con backoff exponencial
    max_attempts = 5
    for attempt in range(max_attempts):
        dns_url = await _discover_dns_url()
        if not dns_url:
            if attempt < max_attempts - 1:
                wait_time = min(2 ** attempt, 30)  # Backoff exponencial hasta 30s
                logger.warning("Could not discover DNS server for registration (attempt %d/%d), retrying in %ds...", 
                             attempt + 1, max_attempts, wait_time)
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error("Could not discover DNS server for registration after %d attempts", max_attempts)
                return False
        
        my_ip = await _get_my_ip()
        port = int(os.getenv("PROCESSOR_PORT", 8000))
        external_port = int(PROCESSOR_EXTERNAL_PORT) if PROCESSOR_EXTERNAL_PORT else None
        external_ip = PROCESSOR_EXTERNAL_IP  # IP del host físico (accesible desde navegador)
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payload = {
                    "processor_id": PROCESSOR_ID,
                    "ip": my_ip,
                    "port": port
                }
                if external_port:
                    payload["external_port"] = external_port
                if external_ip:
                    payload["external_ip"] = external_ip
                    
                response = await client.post(
                    f"{dns_url}/processor/register",
                    json=payload
                )
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Registered with DNS: %s (total processors: %d)", 
                               PROCESSOR_ID, data.get("total_processors", 0))
                    return True
                else:
                    logger.error("DNS registration failed: %s", response.text)
        except Exception as e:
            logger.warning("Error registering with DNS (attempt %d/%d): %s", attempt + 1, max_attempts, e)
            global _cached_dns_url, _cached_dns_ts
            _cached_dns_url = None
            _cached_dns_ts = 0.0
            
            if attempt < max_attempts - 1:
                wait_time = min(2 ** attempt, 30)
                await asyncio.sleep(wait_time)
                continue
    
    return False


async def _send_heartbeat():
    """Send heartbeat to DNS service."""
    import httpx
    
    dns_url = await _discover_dns_url()
    if not dns_url:
        return False
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{dns_url}/processor/heartbeat",
                json={"processor_id": PROCESSOR_ID}
            )
            return response.status_code == 200
    except Exception as e:
        logger.debug("Heartbeat failed: %s", e)
        global _cached_dns_url, _cached_dns_ts
        _cached_dns_url = None
        _cached_dns_ts = 0.0
        # Try to re-register if heartbeat fails
        return await _register_with_dns()


async def _heartbeat_loop():
    """Background task that sends periodic heartbeats to DNS."""
    logger.info("Starting heartbeat loop (interval: %ds)", HEARTBEAT_INTERVAL)
    
    # Initial registration
    await _register_with_dns()
    
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            success = await _send_heartbeat()
            if not success:
                logger.warning("Heartbeat failed, will retry...")
        except asyncio.CancelledError:
            logger.info("Heartbeat loop cancelled")
            break
        except Exception as e:
            logger.error("Error in heartbeat loop: %s", e)


async def get_storage_client_instance() -> StorageClient:
    """Get or create the storage client instance."""
    global _storage_client, _dns_client
    
    if _storage_client is None:
        # Initialize DNS client first
        if _dns_client is None:
            dns_alias = os.getenv("DNS_ALIAS", "dns")
            dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
            _dns_client = DNSClientHA(dns_alias=dns_alias, dns_port=dns_port)
            logger.info("DNS client initialized: %s:%d", dns_alias, dns_port)
        
        # Initialize storage client with DNS client
        _storage_client = await init_storage_client(dns_client=_dns_client)
        logger.info("Storage client initialized with DNS-based resolution")
    
    return _storage_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _heartbeat_task
    
    # Startup
    logger.info("Processor Node %s starting...", PROCESSOR_ID)
    await get_storage_client_instance()
    
    # Start heartbeat loop for DNS registration
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    logger.info("Heartbeat task started")
    
    yield
    
    # Shutdown
    global _storage_client, _dns_client
    
    # Cancel heartbeat task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        _heartbeat_task = None
    
    if _storage_client:
        await _storage_client.__aexit__(None, None, None)
        _storage_client = None
    _dns_client = None  # No async cleanup needed for DNSClientHA
    logger.info("Processor Node %s shutdown complete", PROCESSOR_ID)


app = FastAPI(
    title="Processor Node API",
    version="2.0.0",
    description="Stateless processing layer with DNS-based Storage Node discovery",
    lifespan=lifespan,
)

# ============================================================================
# RATE LIMITING CONFIGURATION - PROTECCIÓN CONTRA DoS
# ============================================================================
# Crear limitador basado en IP del cliente
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# Handler para errores de rate limit
@app.exception_handler(RateLimitExceeded)
async def ratelimit_handler(request: Request, exc: RateLimitExceeded):
    """Handle rate limit exceeded errors."""
    client_host = request.client.host if request.client else "-"
    logger.warning(
        "Rate limit exceeded for IP %s on endpoint %s",
        client_host,
        request.url.path,
    )
    return {
        "detail": "Too many requests. Please try again later.",
        "retry_after": exc.headers.get("retry-after", "60"),
        "status": 429,
    }

# ============================================================================
# CORS CONFIGURATION - SEGURA Y CENTRALIZADA
# ============================================================================
# Aplicar configuración CORS segura basada en ambiente
cors_config = get_cors_config()
app.add_middleware(
    CORSMiddleware,
    **cors_config,  # ✅ Distribuye: allow_origins, allow_credentials, etc.
)


# ============================================================================
# HEALTH & STATUS ENDPOINTS
# ============================================================================

@limiter.limit("100/minute")
@app.get("/health", response_model=HealthResponse)
async def health(request: Request):
    """Health check endpoint."""
    global _dns_client
    
    client = await get_storage_client_instance()
    health_info = await client.check_health()
    
    dns_healthy = False
    if _dns_client:
        # Check if any DNS server is reachable
        dns_servers = _dns_client.get_servers_status()
        dns_healthy = any(s.get("healthy") for s in dns_servers)
    
    storage_available = health_info.get("healthy", False)
    
    return HealthResponse(
        status="ok" if (storage_available and dns_healthy) else "degraded",
        processor_id=PROCESSOR_ID,
        storage_available=storage_available,
        dns_healthy=dns_healthy,
    )


@limiter.limit("50/minute")
@app.get("/status", response_model=ProcessorStatus)
async def get_status(request: Request):
    """Get detailed status of this Processor Node."""
    global _dns_client
    
    client = await get_storage_client_instance()
    registry = get_circuit_breaker_registry()
    
    health_info = await client.check_health()
    
    current_storage = None
    if health_info.get("storage_id"):
        current_storage = {
            "storage_id": health_info.get("storage_id"),
            "url": health_info.get("storage_url"),
            "healthy": health_info.get("healthy"),
        }
    
    dns_cache = {}
    if _dns_client:
        dns_cache = _dns_client.get_storage_cache_status()
    
    return ProcessorStatus(
        processor_id=PROCESSOR_ID,
        status="healthy" if health_info.get("healthy") else "degraded",
        current_storage=current_storage,
        dns_cache=dns_cache,
        circuit_breakers=registry.get_all_status(),
    )


# ============================================================================
# FILE OPERATIONS (forwarded to Storage Nodes)
# ============================================================================

@limiter.limit("30/minute")
@app.get("/files", response_model=List[FileMetadata])
async def list_files(request: Request):
    """
    List all files.
    
    This endpoint forwards the request to the Storage Node.
    In a sharded setup, it will aggregate results from all shards.
    """
    client = await get_storage_client_instance()
    
    try:
        files = await client.list_files()
        
        client_host = request.client.host if request.client else "-"
        access_logger.info(
            "%s GET /files -> %d files",
            client_host,
            len(files),
        )
        
        return [FileMetadata(**f) for f in files]
        
    except StorageUnavailableError as e:
        logger.error("Storage unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        logger.error("Storage error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@limiter.limit("50/minute")
@app.get("/files/{file_id}", response_model=FileMetadata)
async def get_file(request: Request, file_id: str):
    """Get file metadata by ID."""
    client = await get_storage_client_instance()
    
    try:
        file_data = await client.get_file(file_id)
        
        if not file_data:
            raise HTTPException(status_code=404, detail="File not found")
        
        return FileMetadata(**file_data)
        
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@limiter.limit("5/minute")
@app.delete("/files/{file_id}")
async def delete_file(request: Request, file_id: str):
    """Delete a file."""
    client = await get_storage_client_instance()
    
    try:
        success = await client.delete_file(file_id)
        
        if not success:
            raise HTTPException(status_code=404, detail="File not found")
        
        client_host = request.client.host if request.client else "-"
        access_logger.info("%s DELETE /files/%s", client_host, file_id)
        
        return {"status": "deleted", "file_id": file_id}
        
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@limiter.limit("20/minute")
@app.get("/files/{file_id}/download")
async def download_file(request: Request, file_id: str):
    """
    Download a file.
    
    Streams the file content from the Storage Node.
    """
    client = await get_storage_client_instance()
    
    try:
        # First, get file metadata
        file_data = await client.get_file(file_id)
        if not file_data:
            raise HTTPException(status_code=404, detail="File not found")
        
        # Download content
        content = await client.download_file(file_id)
        
        client_host = request.client.host if request.client else "-"
        access_logger.info(
            "%s GET /files/%s/download -> %s",
            client_host,
            file_id,
            file_data["name"],
        )
        
        # Determine media type
        import mimetypes
        media_type, _ = mimetypes.guess_type(file_data["name"])
        media_type = media_type or "application/octet-stream"
        
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{file_data["name"]}"',
            },
        )
        
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ============================================================================
# SEARCH ENDPOINT
# ============================================================================

@limiter.limit("30/minute")
@app.get("/search", response_model=SearchResponse)
async def search_files(
    request: Request,
    query: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
):
    """
    Search files by name.
    
    In a sharded setup, this will use scatter-gather to search all shards
    and aggregate the results.
    """
    client = await get_storage_client_instance()
    
    try:
        # For now, direct query. With sharding, use scatter_gather_search
        result = await client.search_files(query=query, limit=limit, offset=offset)
        
        client_host = request.client.host if request.client else "-"
        access_logger.info(
            "%s GET /search?query=%s -> %d results",
            client_host,
            query,
            len(result.get("results", [])),
        )
        
        return SearchResponse(
            results=[FileMetadata(**r) for r in result.get("results", [])],
            total=result.get("total", 0),
            query=query,
            limit=limit,
            offset=offset,
        )
        
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ============================================================================
# UPLOAD ENDPOINT
# ============================================================================

@limiter.limit("10/minute")
@app.post("/upload", response_model=UploadResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(None, max_length=200),
):
    """
    Upload a file.
    
    The file is forwarded to the Storage Node for persistence.
    """
    client = await get_storage_client_instance()
    
    try:
        # Validar longitud del nombre de archivo
        if len(file.filename) > 255:
            raise HTTPException(
                status_code=400,
                detail="Filename too long. Maximum: 255 characters"
            )
        
        # Validar caracteres en folder para prevenir path traversal
        if folder:
            if ".." in folder or folder.startswith("/"):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid folder path"
                )
        
        # Leer contenido validando tamaño máximo (500 MB)
        MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
        content = b""
        total_size = 0
        
        while chunk := await file.read(8192):  # Leer en chunks de 8KB
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB"
                )
            content += chunk
        
        # Upload to storage
        result = await client.upload_file(
            filename=file.filename,
            content=content,
            folder=folder,
        )
        
        client_host = request.client.host if request.client else "-"
        access_logger.info(
            "%s POST /upload -> %s (%d bytes)",
            client_host,
            file.filename,
            len(content),
        )
        
        return UploadResponse(
            status="success",
            file_id=result["file_id"],
            filename=result["filename"],
            size=result["size"],
            storage_id=result.get("storage_id", "unknown"),
        )
        
    except StorageUnavailableError:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ============================================================================
# ADMIN ENDPOINTS
# ============================================================================

@limiter.limit("2/minute")
@app.post("/admin/health-check")
async def trigger_health_check(request: Request):
    """Trigger health check of Storage Node via DNS."""
    client = await get_storage_client_instance()
    results = await client.check_health()
    
    return {
        "status": "completed",
        "storage_health": results,
        "timestamp": datetime.now().isoformat(),
    }


@limiter.limit("2/minute")
@app.post("/admin/reset-circuit-breakers")
async def reset_circuit_breakers(request: Request):
    """Reset all circuit breakers."""
    registry = get_circuit_breaker_registry()
    await registry.reset_all()
    
    return {
        "status": "reset",
        "circuit_breakers": registry.get_all_status(),
    }


@limiter.limit("2/minute")
@app.post("/admin/invalidate-dns-cache")
async def invalidate_dns_cache(request: Request):
    """Manually invalidate DNS cache to force re-resolution."""
    global _dns_client
    
    if _dns_client:
        _dns_client.invalidate_storage_cache()
        return {
            "status": "invalidated",
            "dns_cache": _dns_client.get_storage_cache_status(),
        }
    
    return {"status": "no_client", "message": "DNS client not initialized"}


@limiter.limit("50/minute")
@app.get("/admin/dns-status")
async def get_dns_status(request: Request):
    """Get DNS client status and all registered Storage Nodes."""
    global _dns_client
    
    if not _dns_client:
        return {"status": "not_initialized"}
    
    try:
        all_nodes = _dns_client.list_storage_servers()
        dns_servers = _dns_client.get_servers_status()
        
        return {
            "status": "ok",
            "cache": _dns_client.get_storage_cache_status(),
            "registered_storage_nodes": all_nodes,
            "dns_servers": dns_servers,
            "primary_dns": _dns_client.get_primary_url(),
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "cache": _dns_client.get_storage_cache_status(),
        }


@limiter.limit("50/minute")
@app.get("/node/status")
async def get_node_status(request: Request):
    """
    Endpoint for compatibility with existing client.
    Returns node status in the expected format.
    """
    client = await get_storage_client_instance()
    health_info = await client.check_health()
    
    return {
        "node": {
            "processor_id": PROCESSOR_ID,
            "role": "PROCESSOR",
            "current_storage": health_info.get("storage_id"),
            "storage_url": health_info.get("storage_url"),
        },
        "dns": {
            "cache": health_info.get("dns_cache", {}),
        },
        "sync": {
            "status": "N/A - Processor is stateless",
        },
    }
