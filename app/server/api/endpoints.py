import importlib
import logging
from datetime import datetime
from typing import List, Optional

try:
    fastapi = importlib.import_module("fastapi")
    pydantic = importlib.import_module("pydantic")
except ModuleNotFoundError as exc:  # pragma: no cover - dev convenience
    raise SystemExit(
        "Faltan dependencias obligatorias. Instala fastapi y pydantic antes de ejecutar la API."
    ) from exc

FastAPI = fastapi.FastAPI
Query = fastapi.Query
BaseModel = pydantic.BaseModel

from ..db.crud import delete_file, search_files, upsert_file
from ..db.db import init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="File Search API", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("Base de datos inicializada correctamente")

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

@app.get("/search", response_model=List[FileIn])
def search_files_endpoint(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    results = search_files(query=query, limit=limit, offset=offset)
    logger.debug(
        "Búsqueda ejecutada", extra={
            "query": query,
            "limit": limit,
            "offset": offset,
            "results": len(results),
        }
    )
    return results