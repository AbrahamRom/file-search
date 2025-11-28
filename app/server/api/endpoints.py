import importlib
import logging
import os
import shutil
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
BaseModel = pydantic.BaseModel
UploadFile = fastapi.UploadFile
File = fastapi.File
Form = fastapi.Form

from ..db.crud import delete_file, list_files, search_files, upsert_file
from ..db.db import init_db
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