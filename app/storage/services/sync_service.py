"""
Sync Service for Storage Node BACKUPs.
Synchronizes the SQLite database and files from the PRIMARY.
"""

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import hashlib
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..db.db import DB_PATH
from .scanner import FILES_ROOT

logger = logging.getLogger(__name__)

# Configuration
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", 10))  # Sync every 10 seconds
SYNC_TIMEOUT = int(os.getenv("SYNC_TIMEOUT", 30))


class SyncService:
    """
    Synchronization service for BACKUP Storage Nodes.
    
    Features:
    - Downloads database snapshot from PRIMARY
    - Synchronizes new/modified files
    - Logs all operations
    """
    
    def __init__(self):
        self._running: bool = False
        self._last_sync: Optional[datetime] = None
        self._sync_count: int = 0
        self._primary_url: Optional[str] = None
        self._is_syncing: bool = False
        
        logger.info(f"[SyncService] Initialized. Interval: {SYNC_INTERVAL}s")
    
    def set_primary_url(self, url: Optional[str]):
        """Set the URL of the current PRIMARY."""
        if url != self._primary_url:
            self._primary_url = url
            logger.info(f"[SyncService] PRIMARY URL updated: {url}")
    
    async def sync_database(self) -> bool:
        """
        Download the database snapshot from the PRIMARY.
        Uses SQLite backup API for consistency.
        """
        if not self._primary_url:
            logger.warning("[SyncService] No PRIMARY URL configured")
            return False
        
        try:
            logger.info(f"[SyncService] Syncing database from {self._primary_url}...")
            
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT) as client:
                response = await client.get(f"{self._primary_url}/internal/db_snapshot")
                
                if response.status_code == 200:
                    # Save to temporary file first
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
                        tmp.write(response.content)
                        tmp_path = tmp.name
                    
                    # Verify downloaded file integrity
                    try:
                        conn = sqlite3.connect(tmp_path)
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA integrity_check")
                        result = cursor.fetchone()
                        conn.close()
                        
                        if result[0] != "ok":
                            logger.error(f"[SyncService] Downloaded DB is corrupt: {result}")
                            os.unlink(tmp_path)
                            return False
                            
                    except Exception as e:
                        logger.error(f"[SyncService] Error verifying DB: {e}")
                        os.unlink(tmp_path)
                        return False
                    
                    # Replace local database
                    db_path = str(DB_PATH)
                    
                    # Ensure directory exists
                    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Move temporary file to final location
                    shutil.move(tmp_path, db_path)
                    
                    size_kb = os.path.getsize(db_path) / 1024
                    logger.info(f"[SyncService] Database synced ({size_kb:.2f} KB)")
                    return True
                else:
                    logger.error(f"[SyncService] Error getting DB: {response.status_code}")
                    
        except httpx.TimeoutException:
            logger.error("[SyncService] Timeout syncing database")
        except Exception as e:
            logger.error(f"[SyncService] Error syncing DB: {e}")
        
        return False
    
    async def sync_files(self) -> Dict[str, int]:
        """
        Synchronize files from the PRIMARY.
        Last-Modified-Wins (LWW):
        - Si el PRIMARY tiene una versión más nueva, se descarga y se preserva mtime.
        - Si el BACKUP tiene una versión más nueva, se empuja al PRIMARY (replicate-file).
        """
        if not self._primary_url:
            logger.warning("[SyncService] No PRIMARY URL configured")
            return {"downloaded": 0, "errors": 0}
        
        downloaded = 0
        pushed = 0
        errors = 0

        def _parse_remote_mtime(value: str) -> Optional[float]:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(value)
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()

        def _safe_primary_file_url(relative_path: str) -> str:
            # Mantener '/' para el path converter de FastAPI
            return f"{self._primary_url}/internal/file/{quote(relative_path, safe='/')}"
        
        try:
            logger.info(f"[SyncService] Syncing files from {self._primary_url}...")
            
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT) as client:
                # Get file list from PRIMARY
                response = await client.get(f"{self._primary_url}/internal/files")
                
                if response.status_code != 200:
                    logger.error(f"[SyncService] Error getting file list: {response.status_code}")
                    return {"downloaded": 0, "errors": 1}
                
                remote_files = response.json().get("files", [])
                logger.debug(f"[SyncService] Remote files: {len(remote_files)}")
                
                # Compare with local files
                conflicted = 0
                for remote_file in remote_files:
                    relative_path = remote_file["relative_path"]
                    remote_size = remote_file["size"]
                    remote_mtime = _parse_remote_mtime(remote_file.get("last_modified"))
                    remote_hash = remote_file.get("content_hash")
                    
                    local_path = FILES_ROOT / relative_path

                    # LWW with small tolerance to avoid jitter
                    tolerance = 0.001
                    if not local_path.exists():
                        action = "download"
                    else:
                        stat = local_path.stat()
                        local_mtime = stat.st_mtime
                        local_size = stat.st_size
                        
                        # Calculate local hash if remote has hash
                        local_hash = None
                        if remote_hash and local_path.is_file():
                            try:
                                with open(local_path, "rb") as f:
                                    local_hash = hashlib.sha256(f.read()).hexdigest()
                            except Exception as e:
                                logger.warning(f"[SyncService] Error calculating hash for {relative_path}: {e}")

                        # Conflict detection: same mtime but different hash
                        if (remote_hash and local_hash and 
                            remote_hash != local_hash and 
                            abs(remote_mtime - local_mtime) <= tolerance):
                            # Conflict: preserve local as .conflict and download remote
                            try:
                                conflict_name = f"{local_path.stem}.conflict.{local_hash[:8]}{local_path.suffix}"
                                conflict_path = local_path.parent / conflict_name
                                shutil.copy2(local_path, conflict_path)
                                logger.warning(
                                    f"[CONFLICT] Detected divergent content for {relative_path}. "
                                    f"Preserved local as {conflict_name}, will download PRIMARY version."
                                )
                                conflicted += 1
                                action = "download"
                            except Exception as e:
                                logger.error(f"[CONFLICT] Error preserving conflict copy: {e}")
                                action = "skip"
                        elif remote_mtime is None:
                            # Fallback: comportamiento previo por tamaño
                            action = "download" if local_size != remote_size else "skip"
                        else:
                            if remote_mtime > local_mtime + tolerance:
                                action = "download"
                            elif local_mtime > remote_mtime + tolerance:
                                action = "push"
                            else:
                                # Tie-breaker para converger: preferir PRIMARY
                                action = "download" if local_size != remote_size else "skip"

                    if action == "download":
                        try:
                            # Download file
                            file_response = await client.get(_safe_primary_file_url(relative_path))
                            
                            if file_response.status_code == 200:
                                # Create directory if needed
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                
                                # Save file
                                with open(local_path, "wb") as f:
                                    f.write(file_response.content)

                                # Preservar mtime remoto para evitar loops
                                if remote_mtime is not None:
                                    try:
                                        os.utime(local_path, (remote_mtime, remote_mtime))
                                    except Exception:
                                        pass
                                
                                downloaded += 1
                                logger.info(f"[SyncService] Downloaded: {relative_path}")
                            else:
                                errors += 1
                                logger.warning(f"[SyncService] Error downloading {relative_path}: {file_response.status_code}")
                                
                        except Exception as e:
                            errors += 1
                            logger.error(f"[SyncService] Error downloading {relative_path}: {e}")

                    elif action == "push":
                        # Local is newer -> push to PRIMARY (LWW)
                        try:
                            if not local_path.is_file():
                                continue

                            local_stat = local_path.stat()
                            payload_mtime = datetime.fromtimestamp(local_stat.st_mtime, tz=timezone.utc).isoformat()

                            with open(local_path, "rb") as f:
                                files = {"file": (local_path.name, f, "application/octet-stream")}
                                data = {
                                    "relative_path": relative_path,
                                    "last_modified": payload_mtime,
                                }
                                push_resp = await client.post(
                                    f"{self._primary_url}/internal/replicate-file",
                                    files=files,
                                    data=data,
                                )

                            if push_resp.status_code in (200, 201):
                                pushed += 1
                                logger.info(f"[SyncService] Pushed newer local version to PRIMARY: {relative_path}")
                            else:
                                errors += 1
                                logger.warning(
                                    f"[SyncService] Error pushing {relative_path}: {push_resp.status_code} {push_resp.text}"
                                )
                        except Exception as e:
                            errors += 1
                            logger.error(f"[SyncService] Error pushing {relative_path}: {e}")
            
            if downloaded > 0 or pushed > 0 or conflicted > 0:
                logger.info(f"[SyncService] File sync completed: {downloaded} downloaded, {pushed} pushed, {conflicted} conflicts, {errors} errors")
            
        except httpx.TimeoutException:
            logger.error("[SyncService] Timeout syncing files")
            errors += 1
        except Exception as e:
            logger.error(f"[SyncService] Error syncing files: {e}")
            errors += 1
        
        return {"downloaded": downloaded, "pushed": pushed, "conflicted": conflicted, "errors": errors}
    
    async def full_sync(self) -> bool:
        """
        Perform a full sync (DB + files).
        """
        if self._is_syncing:
            logger.debug("[SyncService] Sync already in progress")
            return False
        
        self._is_syncing = True
        success = True
        
        try:
            start_time = datetime.now()
            logger.info(f"[SyncService] Starting full sync...")
            
            # 1. Sync database
            db_ok = await self.sync_database()
            if not db_ok:
                success = False
            
            # 2. Sync files
            file_result = await self.sync_files()
            if file_result["errors"] > 0:
                success = False
            
            # Update stats
            self._last_sync = datetime.now()
            self._sync_count += 1
            
            elapsed = (self._last_sync - start_time).total_seconds()
            logger.info(
                f"[SyncService] Sync #{self._sync_count} completed in {elapsed:.2f}s. "
                f"DB: {'OK' if db_ok else 'FAIL'}, Files: {file_result['downloaded']} downloaded"
            )
            
        except Exception as e:
            logger.error(f"[SyncService] Error in full sync: {e}")
            success = False
        finally:
            self._is_syncing = False
        
        return success
    
    async def sync_loop(self, get_primary_url_fn):
        """
        Infinite sync loop for BACKUP nodes.
        
        Args:
            get_primary_url_fn: Function that returns the current PRIMARY URL
        """
        self._running = True
        logger.info(f"[SyncService] Starting sync loop (interval: {SYNC_INTERVAL}s)")
        
        while self._running:
            try:
                # Get current PRIMARY URL
                primary_url = get_primary_url_fn()
                self.set_primary_url(primary_url)
                
                if primary_url:
                    await self.full_sync()
                else:
                    logger.debug("[SyncService] No PRIMARY available for sync")
                    
            except Exception as e:
                logger.error(f"[SyncService] Error in sync loop: {e}")
            
            await asyncio.sleep(SYNC_INTERVAL)
    
    def stop(self):
        """Stop the sync loop."""
        self._running = False
        logger.info("[SyncService] Stopped")
    
    def get_status(self) -> Dict:
        """Return current sync service status."""
        return {
            "running": self._running,
            "is_syncing": self._is_syncing,
            "primary_url": self._primary_url,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "sync_count": self._sync_count,
            "sync_interval": SYNC_INTERVAL,
        }


# Global instance
_sync_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    """Get the global SyncService instance."""
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service


__all__ = ["SyncService", "get_sync_service"]
