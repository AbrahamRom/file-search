import importlib
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from typing import List, Optional
from pathlib import Path

try:
    fastapi = importlib.import_module("fastapi")
    pydantic = importlib.import_module("pydantic")
except ModuleNotFoundError as exc:  # pragma: no cover - dev convenience
    raise SystemExit(
        "Faltan dependencias obligatorias. Instala fastapi y pydantic antes de ejecutar la API."
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

from ..db.crud import delete_file, list_files, search_files, upsert_file
from ..db.db import init_db, DB_PATH
from ..services.scanner import sync, _DEFAULT_ROOT, _compute_file_id
from ..services import file_handler

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.access")

app = FastAPI(title="File Search API", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    init_db()
    summary = sync()
    logger.info("Base de datos inicializada y sincronizada", extra={"sync": summary})

@app.get("/health")
def health():
    return {"status": "ok"}

class FileIn(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    last_modified: datetime

@app.post("/files")
def upsert_file_endpoint(payload: FileIn):
    upsert_file(
        file_id=payload.file_id,
        name=payload.name,
        path=payload.path,
        size=payload.size,
        last_modified=payload.last_modified,
    )
    logger.info("Archivo %s (%s) registrado/actualizado", payload.file_id, payload.path)
    return {"status": "file upserted"}

@app.delete("/files/{file_id}")
def delete_file_endpoint(file_id: str):
    delete_file(file_id=file_id)
    logger.info("Archivo %s eliminado", file_id)
    return {"status": "file deleted"}


@app.get("/files", response_model=List[FileIn])
def list_files_endpoint(request: Request):
    results = list_files()
    logger.info("Listando %d archivos", len(results))

    client_host = request.client.host if request.client else "-"
    access_message = (
        f"{client_host} {request.method} {request.url.path} -> {len(results)} archivos"
    )
    access_logger.info(access_message)
    return results

@app.post("/upload")
async def upload_file_endpoint(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(None)
):
    """
    Sube un archivo a la carpeta files y lo registra en la base de datos.
    Si se especifica una carpeta, el archivo se guardará en esa subcarpeta.
    """
    try:
        # Crear la ruta de destino
        target_dir = _DEFAULT_ROOT
        if folder:
            target_dir = target_dir / folder
            os.makedirs(target_dir, exist_ok=True)
            try:
                os.chmod(target_dir, 0o777)
            except Exception:
                pass
        
        # Guardar el archivo
        file_path = target_dir / file.filename
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Obtener metadatos del archivo
        stat = file_path.stat()
        last_modified = datetime.fromtimestamp(stat.st_mtime)
        
        # Calcular el ID del archivo basado en su ruta relativa
        relative_path = file_path.relative_to(_DEFAULT_ROOT).as_posix()
        file_id = _compute_file_id(relative_path)
        
        # Registrar el archivo en la base de datos
        upsert_file(
            file_id=file_id,
            name=file.filename,
            path=str(file_path.resolve()),
            size=stat.st_size,
            last_modified=last_modified,
        )
        
        logger.info("Archivo %s subido y registrado correctamente", file.filename)
        
        # Registrar acceso
        client_host = request.client.host if request.client else "-"
        access_message = f"{client_host} {request.method} {request.url.path} -> archivo '{file.filename}' subido"
        access_logger.info(access_message)
        
        return {
            "status": "success",
            "file_id": file_id,
            "filename": file.filename,
            "size": stat.st_size
        }
    except Exception as e:
        logger.error("Error al subir archivo: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error al subir archivo: {str(e)}")


@app.get("/files/{file_id}/download")
def download_file_endpoint(file_id: str, request: Request):
    try:
        target = file_handler.resolve_download(file_id)
    except file_handler.FileRecordNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    except file_handler.FileOnDiskNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no disponible en disco")

    logger.info("Descarga solicitada para archivo %s", file_id)

    client_host = request.client.host if request.client else "-"
    access_logger.info(
        "%s %s %s -> descarga '%s'",
        client_host,
        request.method,
        request.url.path,
        target.filename,
    )

    return FileResponse(
        path=target.path,
        filename=target.filename,
        media_type=target.media_type,
    )

@app.get("/search", response_model=List[FileIn])
def search_files_endpoint(
    request: Request,
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    results = search_files(query=query, limit=limit, offset=offset)
    logger.info(
        "Búsqueda query='%s' -> %d resultados (limit=%d, offset=%d)",
        query,
        len(results),
        limit,
        offset,
    )
    client_host = request.client.host if request.client else "-"
    access_message = (
        f"{client_host} {request.method} {request.url.path} "
        f"query='{query}' -> {len(results)} resultados "
        f"(limit={limit}, offset={offset})"
    )
    access_logger.info(access_message)
    return results


# ============================================================================
# ENDPOINTS INTERNOS PARA SINCRONIZACIÓN
# Solo usados por servidores BACKUP para sincronizar desde el PRIMARY
# ============================================================================

@app.get("/internal/db_snapshot")
def get_db_snapshot(request: Request, background_tasks: BackgroundTasks):
    """
    Genera y retorna un snapshot consistente de la base de datos SQLite.
    Usa la API de backup de SQLite para garantizar consistencia.
    """
    client_host = request.client.host if request.client else "-"
    logger.info(f"[SYNC] Solicitud de snapshot de DB desde {client_host}")
    
    try:
        # Crear archivo temporal para el backup
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp_path = tmp.name
        
        # Usar la API de backup de SQLite para crear una copia consistente
        source_conn = sqlite3.connect(str(DB_PATH))
        dest_conn = sqlite3.connect(tmp_path)
        
        with dest_conn:
            source_conn.backup(dest_conn)
        
        source_conn.close()
        dest_conn.close()
        
        # Retornar el archivo
        file_size = os.path.getsize(tmp_path)
        logger.info(f"[SYNC] Snapshot de DB generado: {file_size / 1024:.2f} KB")
        
        def cleanup():
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        
        # Programar limpieza después de enviar la respuesta
        background_tasks.add_task(cleanup)
        
        return FileResponse(
            path=tmp_path,
            filename="db_snapshot.db",
            media_type="application/octet-stream"
        )
        
    except Exception as e:
        logger.error(f"[SYNC] Error generando snapshot de DB: {e}")
        raise HTTPException(status_code=500, detail=f"Error generando snapshot: {str(e)}")


@app.get("/internal/files")
def list_files_for_sync(request: Request):
    """
    Lista todos los archivos disponibles para sincronización.
    Incluye metadatos necesarios para determinar si un archivo necesita actualizarse.
    """
    client_host = request.client.host if request.client else "-"
    logger.info(f"[SYNC] Solicitud de lista de archivos desde {client_host}")
    
    files = []
    
    try:
        if _DEFAULT_ROOT.exists():
            for file_path in _DEFAULT_ROOT.rglob("*"):
                if not file_path.is_file():
                    continue
                
                relative_path = file_path.relative_to(_DEFAULT_ROOT).as_posix()
                stat = file_path.stat()
                
                files.append({
                    "relative_path": relative_path,
                    "size": stat.st_size,
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
        logger.info(f"[SYNC] Lista de archivos generada: {len(files)} archivos")
        
        return {
            "files": files,
            "total": len(files),
            "root": str(_DEFAULT_ROOT)
        }
        
    except Exception as e:
        logger.error(f"[SYNC] Error listando archivos: {e}")
        raise HTTPException(status_code=500, detail=f"Error listando archivos: {str(e)}")


@app.get("/internal/file/{file_path:path}")
def get_file_for_sync(file_path: str, request: Request):
    """
    Descarga un archivo específico para sincronización.
    El path es relativo al directorio de archivos.
    """
    client_host = request.client.host if request.client else "-"
    logger.info(f"[SYNC] Solicitud de archivo '{file_path}' desde {client_host}")
    
    try:
        full_path = _DEFAULT_ROOT / file_path
        
        # Verificar que el archivo existe y está dentro del directorio permitido
        if not full_path.exists():
            raise HTTPException(status_code=404, detail="Archivo no encontrado")
        
        # Seguridad: verificar que no se sale del directorio raíz
        try:
            full_path.resolve().relative_to(_DEFAULT_ROOT.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Acceso denegado")
        
        logger.info(f"[SYNC] Enviando archivo: {file_path} ({full_path.stat().st_size} bytes)")
        
        return FileResponse(
            path=str(full_path),
            filename=full_path.name,
            media_type="application/octet-stream"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SYNC] Error enviando archivo {file_path}: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")