"""
Servicio de sincronización para servidores BACKUP.
Sincroniza la base de datos SQLite y los archivos desde el PRIMARY.
"""

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

import httpx

from ..db.db import DB_PATH
from .scanner import _DEFAULT_ROOT

logger = logging.getLogger(__name__)

# Configuración
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", 10))  # Sincronizar cada 10 segundos
SYNC_TIMEOUT = int(os.getenv("SYNC_TIMEOUT", 30))


class SyncService:
    """
    Servicio de sincronización para nodos BACKUP.
    
    Funcionalidades:
    - Descarga snapshot de la base de datos desde el PRIMARY
    - Sincroniza archivos nuevos/modificados
    - Registra todas las operaciones en logs
    """
    
    def __init__(self):
        self._running: bool = False
        self._last_sync: Optional[datetime] = None
        self._sync_count: int = 0
        self._primary_url: Optional[str] = None
        self._is_syncing: bool = False
        
        logger.info(f"[SyncService] Inicializado. Intervalo: {SYNC_INTERVAL}s")
    
    def set_primary_url(self, url: Optional[str]):
        """Establece la URL del PRIMARY actual."""
        if url != self._primary_url:
            self._primary_url = url
            logger.info(f"[SyncService] PRIMARY URL actualizado: {url}")
    
    async def sync_database(self) -> bool:
        """
        Descarga el snapshot de la base de datos desde el PRIMARY.
        Usa la API de backup de SQLite para consistencia.
        """
        if not self._primary_url:
            logger.warning("[SyncService] No hay PRIMARY URL configurado")
            return False
        
        try:
            logger.info(f"[SyncService] Sincronizando base de datos desde {self._primary_url}...")
            
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT) as client:
                response = await client.get(f"{self._primary_url}/internal/db_snapshot")
                
                if response.status_code == 200:
                    # Guardar en archivo temporal primero
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
                        tmp.write(response.content)
                        tmp_path = tmp.name
                    
                    # Verificar integridad del archivo descargado
                    try:
                        conn = sqlite3.connect(tmp_path)
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA integrity_check")
                        result = cursor.fetchone()
                        conn.close()
                        
                        if result[0] != "ok":
                            logger.error(f"[SyncService] DB descargada corrupta: {result}")
                            os.unlink(tmp_path)
                            return False
                            
                    except Exception as e:
                        logger.error(f"[SyncService] Error verificando DB: {e}")
                        os.unlink(tmp_path)
                        return False
                    
                    # Reemplazar la base de datos local
                    db_path = str(DB_PATH)
                    
                    # Asegurar que el directorio existe
                    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Mover el archivo temporal a la ubicación final
                    shutil.move(tmp_path, db_path)
                    
                    size_kb = os.path.getsize(db_path) / 1024
                    logger.info(f"[SyncService] Base de datos sincronizada ({size_kb:.2f} KB)")
                    return True
                else:
                    logger.error(f"[SyncService] Error obteniendo DB: {response.status_code}")
                    
        except httpx.TimeoutException:
            logger.error("[SyncService] Timeout sincronizando base de datos")
        except Exception as e:
            logger.error(f"[SyncService] Error sincronizando DB: {e}")
        
        return False
    
    async def sync_files(self) -> Dict[str, int]:
        """
        Sincroniza los archivos desde el PRIMARY.
        Solo descarga archivos nuevos o modificados.
        """
        if not self._primary_url:
            logger.warning("[SyncService] No hay PRIMARY URL configurado")
            return {"downloaded": 0, "errors": 0}
        
        downloaded = 0
        errors = 0
        
        try:
            logger.info(f"[SyncService] Sincronizando archivos desde {self._primary_url}...")
            
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT) as client:
                # Obtener lista de archivos del PRIMARY
                response = await client.get(f"{self._primary_url}/internal/files")
                
                if response.status_code != 200:
                    logger.error(f"[SyncService] Error obteniendo lista de archivos: {response.status_code}")
                    return {"downloaded": 0, "errors": 1}
                
                remote_files = response.json().get("files", [])
                logger.debug(f"[SyncService] Archivos remotos: {len(remote_files)}")
                
                # Comparar con archivos locales
                for remote_file in remote_files:
                    relative_path = remote_file["relative_path"]
                    remote_size = remote_file["size"]
                    remote_mtime = remote_file["last_modified"]
                    
                    local_path = _DEFAULT_ROOT / relative_path
                    
                    # Verificar si necesitamos descargar
                    need_download = False
                    
                    if not local_path.exists():
                        need_download = True
                        logger.debug(f"[SyncService] Archivo nuevo: {relative_path}")
                    else:
                        local_size = local_path.stat().st_size
                        if local_size != remote_size:
                            need_download = True
                            logger.debug(f"[SyncService] Archivo modificado: {relative_path}")
                    
                    if need_download:
                        try:
                            # Descargar archivo
                            file_response = await client.get(
                                f"{self._primary_url}/internal/file/{relative_path}"
                            )
                            
                            if file_response.status_code == 200:
                                # Crear directorio si no existe
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                
                                # Guardar archivo
                                with open(local_path, "wb") as f:
                                    f.write(file_response.content)
                                
                                downloaded += 1
                                logger.info(f"[SyncService] Archivo descargado: {relative_path}")
                            else:
                                errors += 1
                                logger.warning(f"[SyncService] Error descargando {relative_path}: {file_response.status_code}")
                                
                        except Exception as e:
                            errors += 1
                            logger.error(f"[SyncService] Error descargando {relative_path}: {e}")
            
            if downloaded > 0:
                logger.info(f"[SyncService] Sincronización de archivos completada: {downloaded} descargados, {errors} errores")
            
        except httpx.TimeoutException:
            logger.error("[SyncService] Timeout sincronizando archivos")
            errors += 1
        except Exception as e:
            logger.error(f"[SyncService] Error sincronizando archivos: {e}")
            errors += 1
        
        return {"downloaded": downloaded, "errors": errors}
    
    async def full_sync(self) -> bool:
        """
        Realiza una sincronización completa (DB + archivos).
        """
        if self._is_syncing:
            logger.debug("[SyncService] Ya hay una sincronización en curso")
            return False
        
        self._is_syncing = True
        success = True
        
        try:
            start_time = datetime.now()
            logger.info(f"[SyncService] Iniciando sincronización completa...")
            
            # 1. Sincronizar base de datos
            db_ok = await self.sync_database()
            if not db_ok:
                success = False
            
            # 2. Sincronizar archivos
            file_result = await self.sync_files()
            if file_result["errors"] > 0:
                success = False
            
            # Actualizar estadísticas
            self._last_sync = datetime.now()
            self._sync_count += 1
            
            elapsed = (self._last_sync - start_time).total_seconds()
            logger.info(
                f"[SyncService] Sincronización #{self._sync_count} completada en {elapsed:.2f}s. "
                f"DB: {'OK' if db_ok else 'FAIL'}, Archivos: {file_result['downloaded']} descargados"
            )
            
        except Exception as e:
            logger.error(f"[SyncService] Error en sincronización completa: {e}")
            success = False
        finally:
            self._is_syncing = False
        
        return success
    
    async def sync_loop(self, get_primary_url_fn):
        """
        Loop infinito de sincronización para nodos BACKUP.
        
        Args:
            get_primary_url_fn: Función que retorna la URL del PRIMARY actual
        """
        self._running = True
        logger.info(f"[SyncService] Iniciando sync loop (intervalo: {SYNC_INTERVAL}s)")
        
        while self._running:
            try:
                # Obtener URL del PRIMARY actual
                primary_url = get_primary_url_fn()
                self.set_primary_url(primary_url)
                
                if primary_url:
                    await self.full_sync()
                else:
                    logger.debug("[SyncService] No hay PRIMARY disponible para sincronizar")
                    
            except Exception as e:
                logger.error(f"[SyncService] Error en sync loop: {e}")
            
            await asyncio.sleep(SYNC_INTERVAL)
    
    def stop(self):
        """Detiene el loop de sincronización."""
        self._running = False
        logger.info("[SyncService] Detenido")
    
    def get_status(self) -> Dict:
        """Retorna el estado actual del servicio de sincronización."""
        return {
            "running": self._running,
            "is_syncing": self._is_syncing,
            "primary_url": self._primary_url,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "sync_count": self._sync_count,
            "sync_interval": SYNC_INTERVAL
        }


# Instancia global
_sync_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    """Obtiene la instancia global del SyncService."""
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service
