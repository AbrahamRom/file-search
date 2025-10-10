from fastapi import FastAPI, Query
from pydantic import BaseModel
from typing import Optional, List

from ..db.db import init_db
from ..db.crud import upsert_file, delete_file, search_files
from datetime import datetime

app = FastAPI(title="File Search API", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    init_db()

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
    return {"status": "file upserted"}

@app.delete("/files/{file_id}")
def delete_file_endpoint(file_id: str):
    delete_file(file_id=file_id)
    return {"status": "file deleted"}

@app.get("/search", response_model=List[FileIn])
def search_files_endpoint(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    results = search_files(query=query, limit=limit, offset=offset)
    return results