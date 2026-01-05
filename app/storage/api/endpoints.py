"""
Storage Node API - REST endpoints for internal storage operations.

This API is used by Processor Nodes to:
- Query file metadata
- Search files
- Download files
- Upload files
- Sync with other Storage Nodes

The API is NOT exposed directly to clients - it's for internal cluster communication.
"""

import importlib
import logging
import os
import socket
import shutil
import sqlite3
import tempfile
import hashlib
import httpx
from datetime import datetime, timezone
from typing import List, Optional
from pathlib import Path

try:
    fastapi = importlib.import_module("fastapi")
    pydantic = importlib.import_module("pydantic")
    cors_middleware = importlib.import_module("fastapi.middleware.cors")
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing required dependencies. Install fastapi and pydantic."
    ) from exc

FastAPI = fastapi.FastAPI
Query = fastapi.Query
Request = fastapi.Request
HTTPException = fastapi.HTTPException
FileResponse = fastapi.responses.FileResponse
StreamingResponse = fastapi.responses.StreamingResponse
BackgroundTasks = fastapi.BackgroundTasks
BaseModel = pydantic.BaseModel
UploadFile = fastapi.UploadFile
File = fastapi.File
Form = fastapi.Form
CORSMiddleware = cors_middleware.CORSMiddleware
RedirectResponse = fastapi.responses.RedirectResponse

from ..db.crud import (
    delete_file,
    get_file,
    get_file_count,
    list_files,
    search_files,
    upsert_file,
    upsert_storage_node,
)
from ..db.db import init_db, DB_PATH
from ..services.scanner import sync, FILES_ROOT, compute_file_id, compute_shard_key
from ..services.sync_service import get_sync_service
from ..services import file_handler
from ..services.node_manager import get_node_manager

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("storage.access")

# Storage Node identifier
STORAGE_ID = os.getenv("STORAGE_ID", os.getenv("SERVER_ID", "storage_1"))
HOST_ID = os.getenv("HOST_ID", os.getenv("PHYSICAL_HOST", socket.gethostname()))

# ============================================================================
# SHARD MEMBERSHIP CACHE
# ============================================================================
# Cache ligero para evitar validaciones DNS repetitivas en lecturas
# Formato: {shard_id: {"data": {...}, "epoch": int, "cached_at": float}}
_shard_membership_cache = {}
SHARD_CACHE_TTL = 5.0  # 5 segundos de cache


def _ensure_shard_membership(shard_id: Optional[str], require_primary: bool = False):
    """Verifica que este nodo pertenezca (y opcionalmente sea primario) del shard.
    
    Utiliza cache ligero con TTL de 5s para evitar validaciones DNS repetitivas.
    El cache se invalida automáticamente si el epoch del shard cambia.
    """
    if not shard_id:
        return

    import time
    current_time = time.time()
    
    # Verificar si hay entrada en cache válida
    cached = _shard_membership_cache.get(shard_id)
    if cached and (current_time - cached["cached_at"]) < SHARD_CACHE_TTL:
        data = cached["data"]
        # Validar membership y primary usando datos cacheados
        replicas = data.get("replicas", [])
        primary_id = data.get("primary")
        primary_url = data.get("primary_url")
        shard_epoch = data.get("epoch", 0)
        
        if STORAGE_ID not in replicas:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_shard_member",
                    "message": f"Storage {STORAGE_ID} no es replica del shard {shard_id}",
                    "replicas": replicas,
                    "primary": primary_id,
                },
            )
        
        if require_primary and STORAGE_ID != primary_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_shard_primary",
                    "message": f"Storage {STORAGE_ID} no es el primary del shard {shard_id}",
                    "primary": primary_id,
                    "primary_url": primary_url,
                },
            )
        
        return {"epoch": shard_epoch, "primary": primary_id, "replicas": replicas}

    # Cache miss o expirado: consultar DNS
    dns_alias = os.getenv("DNS_ALIAS", "dns")
    dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
    url = f"http://{dns_alias}:{dns_port}/shard/resolve/{shard_id}"

    try:
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("No se pudo validar shard %s contra DNS: %s", shard_id, exc)
        raise HTTPException(status_code=503, detail="Shard membership validation failed")

    replicas = data.get("replicas", [])
    primary_id = data.get("primary")
    primary_url = data.get("primary_url")
    shard_epoch = data.get("epoch", 0)
    
    # Invalidar cache si el epoch cambió
    if cached and cached["epoch"] != shard_epoch:
        logger.debug(f"Invalidando cache de shard {shard_id}: epoch {cached['epoch']} -> {shard_epoch}")
        _shard_membership_cache.pop(shard_id, None)
    
    # Actualizar cache
    _shard_membership_cache[shard_id] = {
        "data": data,
        "epoch": shard_epoch,
        "cached_at": current_time,
    }

    if STORAGE_ID not in replicas:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_shard_member",
                "message": f"Storage {STORAGE_ID} no es replica del shard {shard_id}",
                "replicas": replicas,
                "primary": primary_id,
            },
        )
    
    # CRITICAL FIX: Validar primary ANTES del return
    if require_primary and STORAGE_ID != primary_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_shard_primary",
                "message": f"Storage {STORAGE_ID} no es el primary del shard {shard_id}",
                "primary": primary_id,
                "primary_url": primary_url,
            },
        )
    
    return {"epoch": shard_epoch, "primary": primary_id, "replicas": replicas}


def _ensure_shard_membership_or_redirect(shard_id: Optional[str], require_primary: bool = False):
    """"    Igual que _ensure_shard_membership pero devuelve redirect 307 si el nodo no pertenece al shard.
    Pensado para endpoints de lectura. Usa el mismo cache que _ensure_shard_membership.
    """
    if not shard_id:
        return None

    import time
    current_time = time.time()
    
    # Verificar cache
    cached = _shard_membership_cache.get(shard_id)
    if cached and (current_time - cached["cached_at"]) < SHARD_CACHE_TTL:
        data = cached["data"]
        replicas = data.get("replicas", [])
        primary_id = data.get("primary")
        primary_url = data.get("primary_url")
        
        if STORAGE_ID not in replicas or (require_primary and STORAGE_ID != primary_id):
            detail = {
                "error": "wrong_shard",
                "message": f"Storage {STORAGE_ID} no es miembro del shard {shard_id}",
                "replicas": replicas,
                "primary": primary_id,
                "primary_url": primary_url,
            }
            if primary_url:
                raise HTTPException(status_code=307, detail=detail, headers={"Location": primary_url})
            raise HTTPException(status_code=403, detail=detail)
        
        return data

    # Cache miss: consultar DNS
    dns_alias = os.getenv("DNS_ALIAS", "dns")
    dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
    url = f"http://{dns_alias}:{dns_port}/shard/resolve/{shard_id}"

    try:
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("No se pudo validar shard %s contra DNS: %s", shard_id, exc)
        
        # FAIL-OPEN READS: Si DNS no responde y tenemos cache (aunque expirado), usarlo para lecturas
        if cached and not require_primary:
            logger.info(
                f"[RESILIENT READ] Usando cache expirado para shard {shard_id} "
                f"(edad: {current_time - cached['cached_at']:.1f}s)"
            )
            data = cached["data"]
            replicas = data.get("replicas", [])
            
            # Validar membership con datos cacheados
            if STORAGE_ID not in replicas:
                raise HTTPException(
                    status_code=503, 
                    detail="DNS unreachable and node not in cached replica set"
                )
            
            return data
        
        # FAIL-CLOSED WRITES: Para escrituras o sin cache, rechazar
        raise HTTPException(status_code=503, detail="Shard membership validation failed")

    replicas = data.get("replicas", [])
    primary_id = data.get("primary")
    primary_url = data.get("primary_url")
    shard_epoch = data.get("epoch", 0)
    
    # Invalidar cache si epoch cambió
    if cached and cached["epoch"] != shard_epoch:
        _shard_membership_cache.pop(shard_id, None)
    
    # Actualizar cache
    _shard_membership_cache[shard_id] = {
        "data": data,
        "epoch": shard_epoch,
        "cached_at": current_time,
    }

    if STORAGE_ID not in replicas or (require_primary and STORAGE_ID != primary_id):
        detail = {
            "error": "wrong_shard",
            "message": f"Storage {STORAGE_ID} no es miembro del shard {shard_id}",
            "replicas": replicas,
            "primary": primary_id,
            "primary_url": primary_url,
        }
        if primary_url:
            # Redirigir al primario conocido
            raise HTTPException(status_code=307, detail=detail, headers={"Location": primary_url})
        raise HTTPException(status_code=403, detail=detail)

    return data

app = FastAPI(
    title="Storage Node API",
    version="1.0.0",
    description="Internal API for Storage Node operations",
)

# ============================================================================
# CORS CONFIGURATION
# ============================================================================
# Permitir CORS para que el navegador pueda descargar archivos
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, especificar los orígenes permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # Importante para descargas
)


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


class FileUpsertRequest(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    last_modified: datetime
    shard_id: Optional[str] = None


class StorageStatusResponse(BaseModel):
    storage_id: str
    status: str
    file_count: int
    db_path: str
    files_root: str
    role: str


class SearchResponse(BaseModel):
    results: List[FileMetadata]
    total: int
    query: str
    limit: int
    offset: int


# ============================================================================
# LIFECYCLE EVENTS
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize database and sync files on startup."""
    init_db()
    summary = sync()
    # Register this storage node locally for shard placement/host awareness
    try:
        upsert_storage_node(node_id=STORAGE_ID, host_id=HOST_ID, status="up")
    except Exception as exc:
        logger.warning("Could not register storage node metadata: %s", exc)
    logger.info(
        "Storage Node %s initialized. DB synced: %s",
        STORAGE_ID,
        summary,
    )


# ============================================================================
# HEALTH & STATUS ENDPOINTS
# ============================================================================

@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok", "storage_id": STORAGE_ID}


@app.get("/status", response_model=StorageStatusResponse)
def get_status():
    """Get detailed status of this Storage Node."""
    return StorageStatusResponse(
        storage_id=STORAGE_ID,
        status="healthy",
        file_count=get_file_count(),
        db_path=str(DB_PATH),
        files_root=str(FILES_ROOT),
        role=os.getenv("STORAGE_ROLE", "PRIMARY"),
    )


# ============================================================================
# FILE METADATA ENDPOINTS (for Processor Nodes)
# ============================================================================

@app.get("/files", response_model=List[FileMetadata])
def list_files_endpoint(
    request: Request,
    shard_id: Optional[str] = Query(None),
):
    """
    List all files in this Storage Node.
    Optionally filter by shard_id for sharded deployments.
    """
    _ensure_shard_membership_or_redirect(shard_id)
    results = list_files(shard_id=shard_id)
    
    client_host = request.client.host if request.client else "-"
    access_logger.info(
        "%s GET /files -> %d files",
        client_host,
        len(results),
    )
    
    return [FileMetadata(**r) for r in results]


@app.get("/files/{file_id}", response_model=FileMetadata)
def get_file_endpoint(file_id: str, request: Request):
    """Get metadata for a specific file."""
    shard_id = request.query_params.get("shard_id")
    _ensure_shard_membership_or_redirect(shard_id)
    record = get_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileMetadata(**record)


@app.post("/files")
def upsert_file_endpoint(payload: FileUpsertRequest):
    """Insert or update file metadata."""
    # CRITICAL: Validar que somos PRIMARY del shard antes de escribir
    _ensure_shard_membership(payload.shard_id, require_primary=True)
    
    upsert_file(
        file_id=payload.file_id,
        name=payload.name,
        path=payload.path,
        size=payload.size,
        last_modified=payload.last_modified,
        shard_id=payload.shard_id,
    )
    logger.info("File %s (%s) upserted", payload.file_id, payload.name)
    return {"status": "upserted", "file_id": payload.file_id}


@app.delete("/files/{file_id}")
def delete_file_endpoint(file_id: str):
    """Delete file metadata and optionally the physical file."""
    # Get file info before deleting
    record = get_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    
    # CRITICAL: Validar que somos PRIMARY del shard antes de eliminar
    _ensure_shard_membership(record.get("shard_id"), require_primary=True)
    
    # Delete from database
    delete_file(file_id=file_id)
    
    # Optionally delete physical file
    if os.getenv("DELETE_PHYSICAL_FILES", "false").lower() == "true":
        try:
            Path(record["path"]).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Could not delete physical file: %s", e)
    
    logger.info("File %s deleted", file_id)
    return {"status": "deleted", "file_id": file_id}


# ============================================================================
# SEARCH ENDPOINT
# ============================================================================

@app.get("/search", response_model=SearchResponse)
def search_files_endpoint(
    request: Request,
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    shard_id: Optional[str] = Query(None),
):
    """
    Search files by name.
    Returns matching files from this Storage Node.
    """
    _ensure_shard_membership_or_redirect(shard_id)
    results = search_files(
        query=query,
        limit=limit,
        offset=offset,
        shard_id=shard_id,
    )
    
    client_host = request.client.host if request.client else "-"
    access_logger.info(
        "%s GET /search?query=%s -> %d results",
        client_host,
        query,
        len(results),
    )
    
    return SearchResponse(
        results=[FileMetadata(**r) for r in results],
        total=len(results),
        query=query,
        limit=limit,
        offset=offset,
    )


# ============================================================================
# FILE DOWNLOAD ENDPOINT
# ============================================================================

@app.get("/files/{file_id}/download")
def download_file_endpoint(file_id: str, request: Request):
    """Download a file by its ID."""
    shard_id = request.query_params.get("shard_id")
    _ensure_shard_membership_or_redirect(shard_id)
    try:
        target = file_handler.resolve_download(file_id)
    except file_handler.FileRecordNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except file_handler.FileOnDiskNotFoundError:
        raise HTTPException(status_code=404, detail="File not available on disk")

    client_host = request.client.host if request.client else "-"
    access_logger.info(
        "%s GET /files/%s/download -> %s",
        client_host,
        file_id,
        target.filename,
    )

    return FileResponse(
        path=str(target.path),
        filename=target.filename,
        media_type=target.media_type,
    )


# ============================================================================
# FILE UPLOAD ENDPOINT
# ============================================================================

@app.post("/upload")
async def upload_file_endpoint(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(None),
    shard_id: str = Form(None),
):
    """
    Upload a file to this Storage Node.
    The file is saved to disk and registered in the database.
    
    FENCING: Only accepts writes if node is PRIMARY with valid lease.
    """
    num_shards = int(os.getenv("NUM_SHARDS", 1))
    shard_strategy = os.getenv("SHARD_STRATEGY", os.getenv("SHARD_ASSIGN_STRATEGY", "hash"))

    if not shard_id and num_shards > 1:
        # Calcular shard determinístico si no viene informado
        path_for_hash = f"{folder}/{file.filename}" if folder else file.filename
        shard_id = compute_shard_key(path_for_hash, strategy=shard_strategy)
        if shard_id is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_shard",
                    "message": "Shard id is required when sharding is enabled",
                },
            )

    # Validar pertenencia/rol por shard (si aplica)
    shard_info = _ensure_shard_membership(shard_id, require_primary=True)
    
    # EPOCH FENCING: Validar que nuestro epoch no esté atrasado respecto al shard
    if shard_info:
        shard_epoch = shard_info.get("epoch", 0)
        node_mgr = get_node_manager()
        if node_mgr.primary_epoch and node_mgr.primary_epoch < shard_epoch:
            logger.warning(
                f"[EPOCH FENCING] Upload rejected: write_epoch {node_mgr.primary_epoch} < shard_epoch {shard_epoch}"
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_epoch",
                    "message": "Storage node epoch is behind shard epoch",
                    "storage_epoch": node_mgr.primary_epoch,
                    "shard_epoch": shard_epoch,
                }
            )

    # Fencing: validar rol y lease
    node_mgr = get_node_manager()
    if not node_mgr.is_primary:
        logger.warning(f"[FENCING] Upload rejected: not PRIMARY (role={node_mgr.role})")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_primary",
                "message": "This node is not PRIMARY",
                "current_role": node_mgr.role,
                "primary_url": node_mgr.primary_url
            }
        )
    
    if not node_mgr.has_valid_lease():
        logger.warning(f"[FENCING] Upload rejected: lease expired (epoch={node_mgr.primary_epoch})")
        raise HTTPException(
            status_code=409,
            detail={
                "error": "lease_expired",
                "message": "PRIMARY lease has expired",
                "epoch": node_mgr.primary_epoch
            }
        )
    
    try:
        # Read content first for hash calculation
        content = await file.read()
        content_hash = hashlib.sha256(content).hexdigest()
        
        # Create target directory
        target_dir = FILES_ROOT
        if folder:
            target_dir = target_dir / folder
            os.makedirs(target_dir, exist_ok=True)
            try:
                os.chmod(target_dir, 0o777)
            except Exception:
                pass
        
        # Save file
        file_path = target_dir / file.filename
        with open(file_path, "wb") as buffer:
            buffer.write(content)
        
        # Get file metadata
        stat = file_path.stat()
        last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        
        # Compute file ID
        relative_path = file_path.relative_to(FILES_ROOT).as_posix()
        file_id = compute_file_id(relative_path)
        
        # Register in database with hash and epoch
        upsert_file(
            file_id=file_id,
            name=file.filename,
            path=str(file_path.resolve()),
            size=stat.st_size,
            last_modified=last_modified,
            shard_id=shard_id,
            content_hash=content_hash,
            origin_node=STORAGE_ID,
            write_epoch=node_mgr.primary_epoch,
        )
        
        logger.info("File %s uploaded successfully (hash=%s, epoch=%s)", file.filename, content_hash[:8], node_mgr.primary_epoch)
        
        # Notificar a DNS sobre el shard placement con origin_node
        if shard_id:
            try:
                dns_alias = os.getenv("DNS_ALIAS", "dns")
                dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
                dns_url = f"http://{dns_alias}:{dns_port}/shard/update"
                
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(dns_url, json={
                        "shard_id": shard_id,
                        "origin_node": STORAGE_ID,
                    })
                    logger.debug(f"Notificado DNS sobre shard {shard_id} con origin_node={STORAGE_ID}")
            except Exception as exc:
                logger.warning(f"No se pudo notificar DNS sobre shard {shard_id}: {exc}")
        
        client_host = request.client.host if request.client else "-"
        access_logger.info(
            "%s POST /upload -> %s (%d bytes)",
            client_host,
            file.filename,
            stat.st_size,
        )
        
        return {
            "status": "success",
            "file_id": file_id,
            "filename": file.filename,
            "size": stat.st_size,
            "content_hash": content_hash,
            "storage_id": STORAGE_ID,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error uploading file: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Upload error: {str(e)}")


# ============================================================================
# SYNC ENDPOINTS (for Storage Node replication)
# ============================================================================

@app.get("/internal/db_snapshot")
def get_db_snapshot(request: Request, background_tasks: BackgroundTasks):
    """
    Generate and return a consistent snapshot of the SQLite database.
    Used by backup Storage Nodes for synchronization.
    """
    client_host = request.client.host if request.client else "-"
    logger.info("[SYNC] DB snapshot requested from %s", client_host)
    
    try:
        # Create temporary file for backup
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp_path = tmp.name
        
        # Use SQLite backup API for consistency
        source_conn = sqlite3.connect(str(DB_PATH))
        dest_conn = sqlite3.connect(tmp_path)
        
        with dest_conn:
            source_conn.backup(dest_conn)
        
        source_conn.close()
        dest_conn.close()
        
        file_size = os.path.getsize(tmp_path)
        logger.info("[SYNC] DB snapshot generated: %.2f KB", file_size / 1024)
        
        def cleanup():
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        
        background_tasks.add_task(cleanup)
        
        return FileResponse(
            path=tmp_path,
            filename="db_snapshot.db",
            media_type="application/octet-stream",
        )
        
    except Exception as e:
        logger.error("[SYNC] Error generating DB snapshot: %s", e)
        raise HTTPException(status_code=500, detail=f"Snapshot error: {str(e)}")


@app.get("/internal/files")
def list_files_for_sync(request: Request):
    """
    List all files for synchronization - combines filesystem and database.
    
    Returns:
    - Files physically on disk (for normal sync)
    - Files in database but NOT on disk (marked with exists_on_disk=false)
    
    This allows other nodes to know which files they need to download,
    including "orphaned" records after network partitions.
    """
    client_host = request.client.host if request.client else "-"
    logger.info("[SYNC] File list requested from %s", client_host)
    
    files = []
    total_size = 0
    files_on_disk = 0
    files_missing = 0
    
    # Track file_ids we've already added (to avoid duplicates)
    seen_file_ids = set()
    
    try:
        # 1. First, scan physical files on disk
        if FILES_ROOT.exists():
            for file_path in FILES_ROOT.rglob("*"):
                if not file_path.is_file():
                    continue
                
                relative_path = file_path.relative_to(FILES_ROOT).as_posix()
                stat = file_path.stat()
                file_size = stat.st_size
                total_size += file_size
                
                # Compute file_id for cross-reference
                file_id = compute_file_id(relative_path)
                seen_file_ids.add(file_id)
                files_on_disk += 1
                
                files.append({
                    "file_id": file_id,
                    "relative_path": relative_path,
                    "name": file_path.name,
                    "size": file_size,
                    "size_mb": round(file_size / (1024 * 1024), 2),
                    "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "extension": file_path.suffix.lower() or "no_extension",
                    "exists_on_disk": True,
                })
        
        # 2. Check database for files NOT on disk (orphaned records)
        try:
            db_files = list_files()  # Get all files from database
            for db_record in db_files:
                file_id = db_record["file_id"]
                
                # Skip if already added from disk scan
                if file_id in seen_file_ids:
                    continue
                
                # This file is in DB but not on disk
                file_path = Path(db_record["path"])
                relative_path = file_path.relative_to(FILES_ROOT).as_posix() if str(file_path).startswith(str(FILES_ROOT)) else db_record["name"]
                files_missing += 1
                
                files.append({
                    "file_id": file_id,
                    "relative_path": relative_path,
                    "name": db_record["name"],
                    "size": db_record["size"],
                    "size_mb": round(db_record["size"] / (1024 * 1024), 2),
                    "last_modified": db_record["last_modified"].isoformat() if isinstance(db_record["last_modified"], datetime) else db_record["last_modified"],
                    "extension": file_path.suffix.lower() or "no_extension",
                    "exists_on_disk": False,  # Mark as missing on disk
                    "content_hash": db_record.get("content_hash"),
                    "origin_node": db_record.get("origin_node"),
                })
                
        except Exception as e:
            logger.warning("[SYNC] Error reading DB for orphaned files: %s", e)
        
        logger.info(
            "[SYNC] File list generated: %d files (%d on disk, %d missing) (%.2f MB)", 
            len(files), files_on_disk, files_missing, total_size / (1024 * 1024)
        )
        
        return {
            "files": files,
            "total": len(files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "root": str(FILES_ROOT),
            "storage_id": STORAGE_ID,
            "node_role": get_node_manager().role,
            "files_on_disk": files_on_disk,
            "files_missing_on_disk": files_missing,
        }
        
    except Exception as e:
        logger.error("[SYNC] Error listing files: %s", e)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/internal/files/metadata")
def list_files_with_db_metadata(request: Request):
    """
    List all documents with complete metadata from both filesystem and database.
    Useful for inspecting the internal state of this storage node.
    
    Returns:
    - Files registered in DB with their metadata (content_hash, origin_node, write_epoch)
    - Physical file status (exists on disk, size matches, etc.)
    - Node information (storage_id, role, epoch)
    """
    client_host = request.client.host if request.client else "-"
    logger.info("[INTERNAL] Full metadata list requested from %s", client_host)
    
    try:
        # Get all files from database
        db_files = list_files()
        
        # Get node manager info
        node_mgr = get_node_manager()
        
        detailed_files = []
        total_db_size = 0
        files_on_disk = 0
        files_missing = 0
        
        for db_record in db_files:
            file_path = Path(db_record["path"])
            file_exists = file_path.exists()
            
            file_info = {
                "file_id": db_record["file_id"],
                "name": db_record["name"],
                "path": db_record["path"],
                "size": db_record["size"],
                "size_mb": round(db_record["size"] / (1024 * 1024), 2),
                "last_modified": db_record["last_modified"].isoformat() if isinstance(db_record["last_modified"], datetime) else db_record["last_modified"],
                "shard_id": db_record.get("shard_id"),
                "extension": file_path.suffix.lower() or "no_extension",
                
                # Database-specific metadata
                "content_hash": db_record.get("content_hash"),
                "origin_node": db_record.get("origin_node"),
                "write_epoch": db_record.get("write_epoch"),
                
                # Physical file status
                "exists_on_disk": file_exists,
                "disk_size": file_path.stat().st_size if file_exists else None,
                "size_matches": file_path.stat().st_size == db_record["size"] if file_exists else None,
            }
            
            detailed_files.append(file_info)
            total_db_size += db_record["size"]
            
            if file_exists:
                files_on_disk += 1
            else:
                files_missing += 1
        
        # Statistics by extension
        extensions_stats = {}
        for file_info in detailed_files:
            ext = file_info["extension"]
            if ext not in extensions_stats:
                extensions_stats[ext] = {"count": 0, "total_size": 0}
            extensions_stats[ext]["count"] += 1
            extensions_stats[ext]["total_size"] += file_info["size"]
        
        logger.info(
            "[INTERNAL] Metadata list generated: %d files, %d on disk, %d missing",
            len(detailed_files),
            files_on_disk,
            files_missing,
        )
        
        return {
            "files": detailed_files,
            "summary": {
                "total_files": len(detailed_files),
                "files_on_disk": files_on_disk,
                "files_missing": files_missing,
                "total_size_bytes": total_db_size,
                "total_size_mb": round(total_db_size / (1024 * 1024), 2),
            },
            "extensions": extensions_stats,
            "node_info": {
                "storage_id": STORAGE_ID,
                "role": node_mgr.role,
                "is_primary": node_mgr.is_primary,
                "primary_epoch": node_mgr.primary_epoch,
                "has_valid_lease": node_mgr.has_valid_lease() if node_mgr.is_primary else None,
                "primary_url": node_mgr.primary_url if not node_mgr.is_primary else None,
            },
            "paths": {
                "files_root": str(FILES_ROOT),
                "db_path": str(DB_PATH),
            },
        }
        
    except Exception as e:
        logger.error("[INTERNAL] Error generating metadata list: %s", e)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


def _parse_iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid last_modified")

    # Normalizar a UTC si viene naive (en contenedores suele ser UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_safe_relative_path(relative_path: str) -> Path:
    if not relative_path:
        raise HTTPException(status_code=400, detail="relative_path is required")

    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Invalid relative_path")

    full_path = (FILES_ROOT / rel).resolve()
    try:
        full_path.relative_to(FILES_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return full_path


@app.post("/internal/replicate-file")
async def replicate_file_from_peer(
    request: Request,
    file: UploadFile = File(...),
    relative_path: str = Form(...),
    last_modified: str = Form(...),
    shard_id: str = Form(None),
    content_hash: str = Form(None),
    origin_node: str = Form(None),
    write_epoch: int = Form(None),
):
    """
    Recibe una versión de un archivo desde otro Storage.

    Se usa para resolver conflictos con política LWW: si un BACKUP tiene
    una versión más nueva (por last_modified), la empuja al PRIMARY.
    """
    client_host = request.client.host if request.client else "-"
    full_path = _resolve_safe_relative_path(relative_path)
    modified_dt = _parse_iso_datetime(last_modified)

    # CRITICAL: Validar que somos PRIMARY del shard para aceptar replicación
    # (el BACKUP empuja al PRIMARY, no al revés)
    _ensure_shard_membership(shard_id, require_primary=True)

    try:
        # Read content for hash verification/calculation
        content = await file.read()
        if content_hash:
            # Verify hash if provided
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != content_hash:
                logger.warning(f"[SYNC] Hash mismatch for {relative_path}: expected {content_hash[:8]}, got {actual_hash[:8]}")
        else:
            content_hash = hashlib.sha256(content).hexdigest()
        
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Escritura atómica: escribir a tmp y mover.
        with tempfile.NamedTemporaryFile(delete=False, dir=str(full_path.parent)) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        os.replace(tmp_path, str(full_path))

        # Preservar mtime para evitar loops de sincronización.
        mtime = modified_dt.timestamp()
        os.utime(full_path, (mtime, mtime))

        size = full_path.stat().st_size
        file_id = compute_file_id(Path(relative_path).as_posix())

        upsert_file(
            file_id=file_id,
            name=full_path.name,
            path=str(full_path),
            size=size,
            last_modified=modified_dt,
            shard_id=shard_id,
            content_hash=content_hash,
            origin_node=origin_node or "unknown",
            write_epoch=write_epoch,
        )

        logger.info(
            "[SYNC] Replicated file from %s: %s (%d bytes, hash=%s)",
            client_host,
            relative_path,
            size,
            content_hash[:8] if content_hash else "none",
        )

        return {
            "status": "replicated",
            "relative_path": relative_path,
            "size": size,
            "last_modified": modified_dt.isoformat(),
            "content_hash": content_hash,
            "storage_id": STORAGE_ID,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[SYNC] Error replicating file %s: %s", relative_path, e)
        raise HTTPException(status_code=500, detail=f"Replication error: {str(e)}")


@app.get("/internal/file/{file_path:path}")
def get_file_for_sync(file_path: str, request: Request):
    """
    Download a specific file for synchronization.
    The path is relative to the files root directory.
    """
    client_host = request.client.host if request.client else "-"
    logger.info("[SYNC] File %s requested from %s", file_path, client_host)
    
    try:
        full_path = FILES_ROOT / file_path
        
        if not full_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        # Security: verify path is within FILES_ROOT
        try:
            full_path.resolve().relative_to(FILES_ROOT.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")
        
        logger.info(
            "[SYNC] Sending file: %s (%d bytes)",
            file_path,
            full_path.stat().st_size,
        )
        
        return FileResponse(
            path=str(full_path),
            filename=full_path.name,
            media_type="application/octet-stream",
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[SYNC] Error sending file %s: %s", file_path, e)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/internal/resync")
async def trigger_resync():
    """
    Trigger a resync of the file system with the database.
    Useful after manual file operations.
    
    NOTA: Este endpoint es solo para operaciones internas/admin,
    no requiere validación de shard ya que sincroniza TODO el nodo.
    """
    try:
        summary = sync()
        logger.info("[SYNC] Manual resync completed: %s", summary)
        return {
            "status": "success",
            "summary": summary,
            "storage_id": STORAGE_ID,
        }
    except Exception as e:
        logger.error("[SYNC] Resync error: %s", e)
        raise HTTPException(status_code=500, detail=f"Resync error: {str(e)}")


@app.post("/internal/full-sync")
async def trigger_full_sync(request: Request, primary_url: Optional[str] = None):
    """Forzar un full_sync usando SyncService con el primary indicado."""
    sync_service = get_sync_service()
    source = primary_url or request.query_params.get("primary_url")

    if not source:
        raise HTTPException(status_code=400, detail="primary_url es requerido")

    try:
        sync_service.set_primary_url(source)
        ok = await sync_service.full_sync()
        if not ok:
            raise HTTPException(status_code=503, detail="full_sync falló")

        return {
            "status": "synced",
            "storage_id": STORAGE_ID,
            "primary_url": source,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[SYNC] Error en full_sync manual: %s", exc)
        raise HTTPException(status_code=500, detail="Error ejecutando full_sync")


@app.post("/internal/replicate-shard")
async def trigger_shard_replication(
    request: Request,
    shard_id: str = Query(...),
    source_node_url: str = Query(...),
):
    """
    Trigger replication for a specific shard from source node.
    
    Used by DNS after placement changes to sync only the affected shard.
    More efficient than full-sync for targeted updates.
    """
    sync_service = get_sync_service()
    
    logger.info(f"Shard replication requested: {shard_id} from {source_node_url}")
    
    result = await sync_service.replicate_shard(shard_id, source_node_url)
    
    return {
        "status": "success" if result["errors"] == 0 else "partial",
        "shard_id": shard_id,
        "source_node_url": source_node_url,
        "downloaded": result["downloaded"],
        "skipped": result["skipped"],
        "errors": result["errors"],
    }


# ============================================================================
# RECONCILIATION ENDPOINT
# ============================================================================

@app.post("/internal/reconcile")
async def reconcile_files(request: Request, background_tasks: BackgroundTasks):
    """
    Reconciliation endpoint for post-partition recovery.
    
    Compares local database with filesystem and downloads missing files
    from ANY available storage node (not just PRIMARY).
    
    This is essential after network partitions where:
    - Database synced but physical files didn't transfer
    - Files were uploaded to different partitions
    
    Can be called manually post-incident or automatically by sync service.
    """
    import httpx
    import socket
    
    client_host = request.client.host if request.client else "-"
    logger.info("[RECONCILE] Reconciliation requested from %s", client_host)
    
    node_mgr = get_node_manager()
    
    # Get all files in DB
    db_files = list_files()
    
    # Find files missing on disk
    missing_files = []
    for db_record in db_files:
        file_path = Path(db_record["path"])
        if not file_path.exists():
            missing_files.append({
                "file_id": db_record["file_id"],
                "name": db_record["name"],
                "path": str(file_path),
                "relative_path": file_path.relative_to(FILES_ROOT).as_posix() if str(file_path).startswith(str(FILES_ROOT)) else db_record["name"],
                "size": db_record["size"],
                "origin_node": db_record.get("origin_node"),
            })
    
    if not missing_files:
        logger.info("[RECONCILE] No missing files found - storage is consistent")
        return {
            "status": "consistent",
            "storage_id": STORAGE_ID,
            "role": node_mgr.role,
            "total_files": len(db_files),
            "missing_files": 0,
            "downloaded": 0,
            "errors": 0,
        }
    
    logger.info("[RECONCILE] Found %d missing files, attempting recovery from other nodes", len(missing_files))
    
    # Discover all storage nodes via DNS
    dns_alias = os.getenv("DNS_ALIAS", "dns")
    dns_port = int(os.getenv("DNS_SERVICE_PORT", 5353))
    
    storage_nodes = []
    try:
        # Discover DNS IPs
        results = socket.getaddrinfo(dns_alias, dns_port, socket.AF_INET, socket.SOCK_STREAM)
        dns_ips = list(set(result[4][0] for result in results))
        
        # Get storage list from any DNS
        async with httpx.AsyncClient(timeout=5.0) as client:
            for dns_ip in dns_ips:
                try:
                    response = await client.get(f"http://{dns_ip}:{dns_port}/server/list")
                    if response.status_code == 200:
                        storage_nodes = response.json().get("servers", [])
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.warning("[RECONCILE] Could not discover storage nodes via DNS: %s", e)
    
    # Add primary URL if known and not in list
    if node_mgr.primary_url:
        primary_found = any(s.get("url") == node_mgr.primary_url for s in storage_nodes)
        if not primary_found:
            storage_nodes.append({"url": node_mgr.primary_url, "server_id": "primary_from_manager"})
    
    # Filter out self
    my_id = STORAGE_ID
    other_nodes = [s for s in storage_nodes if s.get("server_id") != my_id]
    
    if not other_nodes:
        logger.warning("[RECONCILE] No other storage nodes available for file recovery")
        return {
            "status": "incomplete",
            "storage_id": STORAGE_ID,
            "role": node_mgr.role,
            "total_files": len(db_files),
            "missing_files": len(missing_files),
            "downloaded": 0,
            "errors": len(missing_files),
            "error": "No other storage nodes available",
        }
    
    logger.info("[RECONCILE] Found %d other storage nodes: %s", len(other_nodes), [n.get("server_id") for n in other_nodes])
    
    downloaded = 0
    errors = 0
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for missing in missing_files:
            file_id = missing["file_id"]
            relative_path = missing["relative_path"]
            target_path = Path(missing["path"])
            
            file_downloaded = False
            
            # Try each storage node
            for node in other_nodes:
                node_url = node.get("url")
                node_id = node.get("server_id", "unknown")
                
                if not node_url:
                    continue
                
                try:
                    # Try to download from this node
                    download_url = f"{node_url}/files/{file_id}/download"
                    logger.debug("[RECONCILE] Trying to download %s from %s", file_id, node_id)
                    
                    response = await client.get(download_url)
                    
                    if response.status_code == 200:
                        # Ensure directory exists
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        # Save file
                        with open(target_path, "wb") as f:
                            f.write(response.content)
                        
                        downloaded += 1
                        file_downloaded = True
                        logger.info("[RECONCILE] Downloaded %s from %s", relative_path, node_id)
                        break
                        
                    elif response.status_code == 404:
                        logger.debug("[RECONCILE] File %s not found on %s", file_id, node_id)
                        continue
                    else:
                        logger.warning("[RECONCILE] Error %d from %s for %s", response.status_code, node_id, file_id)
                        continue
                        
                except Exception as e:
                    logger.warning("[RECONCILE] Failed to reach %s for %s: %s", node_id, file_id, e)
                    continue
            
            if not file_downloaded:
                errors += 1
                logger.warning("[RECONCILE] Could not recover file %s from any node", relative_path)
    
    status = "complete" if errors == 0 else "partial"
    logger.info(
        "[RECONCILE] Reconciliation %s: %d downloaded, %d errors out of %d missing",
        status, downloaded, errors, len(missing_files)
    )
    
    return {
        "status": status,
        "storage_id": STORAGE_ID,
        "role": node_mgr.role,
        "total_files": len(db_files),
        "missing_files": len(missing_files),
        "downloaded": downloaded,
        "errors": errors,
        "other_nodes_checked": len(other_nodes),
    }
