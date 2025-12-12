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
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import List, Optional
from pathlib import Path

try:
    fastapi = importlib.import_module("fastapi")
    pydantic = importlib.import_module("pydantic")
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

from ..db.crud import (
    delete_file,
    get_file,
    list_files,
    search_files,
    upsert_file,
    get_file_count,
)
from ..db.db import init_db, DB_PATH
from ..services.scanner import sync, FILES_ROOT, compute_file_id
from ..services import file_handler

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("storage.access")

# Storage Node identifier
STORAGE_ID = os.getenv("STORAGE_ID", os.getenv("SERVER_ID", "storage_1"))

app = FastAPI(
    title="Storage Node API",
    version="1.0.0",
    description="Internal API for Storage Node operations",
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
    record = get_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileMetadata(**record)


@app.post("/files")
def upsert_file_endpoint(payload: FileUpsertRequest):
    """Insert or update file metadata."""
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
    """
    try:
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
            shutil.copyfileobj(file.file, buffer)
        
        # Get file metadata
        stat = file_path.stat()
        last_modified = datetime.fromtimestamp(stat.st_mtime)
        
        # Compute file ID
        relative_path = file_path.relative_to(FILES_ROOT).as_posix()
        file_id = compute_file_id(relative_path)
        
        # Register in database
        upsert_file(
            file_id=file_id,
            name=file.filename,
            path=str(file_path.resolve()),
            size=stat.st_size,
            last_modified=last_modified,
            shard_id=shard_id,
        )
        
        logger.info("File %s uploaded successfully", file.filename)
        
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
            "storage_id": STORAGE_ID,
        }
        
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
    List all physical files for synchronization.
    Returns file paths and metadata needed to determine what needs syncing.
    """
    client_host = request.client.host if request.client else "-"
    logger.info("[SYNC] File list requested from %s", client_host)
    
    files = []
    
    try:
        if FILES_ROOT.exists():
            for file_path in FILES_ROOT.rglob("*"):
                if not file_path.is_file():
                    continue
                
                relative_path = file_path.relative_to(FILES_ROOT).as_posix()
                stat = file_path.stat()
                
                files.append({
                    "relative_path": relative_path,
                    "size": stat.st_size,
                    "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
        
        logger.info("[SYNC] File list generated: %d files", len(files))
        
        return {
            "files": files,
            "total": len(files),
            "root": str(FILES_ROOT),
            "storage_id": STORAGE_ID,
        }
        
    except Exception as e:
        logger.error("[SYNC] Error listing files: %s", e)
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
):
    """
    Recibe una versión de un archivo desde otro Storage.

    Se usa para resolver conflictos con política LWW: si un BACKUP tiene
    una versión más nueva (por last_modified), la empuja al PRIMARY.
    """
    client_host = request.client.host if request.client else "-"
    full_path = _resolve_safe_relative_path(relative_path)
    modified_dt = _parse_iso_datetime(last_modified)

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Escritura atómica: escribir a tmp y mover.
        with tempfile.NamedTemporaryFile(delete=False, dir=str(full_path.parent)) as tmp:
            content = await file.read()
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
        )

        logger.info(
            "[SYNC] Replicated file from %s: %s (%d bytes)",
            client_host,
            relative_path,
            size,
        )

        return {
            "status": "replicated",
            "relative_path": relative_path,
            "size": size,
            "last_modified": modified_dt.isoformat(),
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
