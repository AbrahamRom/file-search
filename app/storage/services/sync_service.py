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
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime

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
        Only downloads new or modified files.
        """
        if not self._primary_url:
            logger.warning("[SyncService] No PRIMARY URL configured")
            return {"downloaded": 0, "errors": 0}
        
        downloaded = 0
        errors = 0
        
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
                for remote_file in remote_files:
                    relative_path = remote_file["relative_path"]
                    remote_size = remote_file["size"]
                    
                    local_path = FILES_ROOT / relative_path
                    
                    # Check if we need to download
                    need_download = False
                    
                    if not local_path.exists():
                        need_download = True
                        logger.debug(f"[SyncService] New file: {relative_path}")
                    else:
                        local_size = local_path.stat().st_size
                        if local_size != remote_size:
                            need_download = True
                            logger.debug(f"[SyncService] Modified file: {relative_path}")
                    
                    if need_download:
                        try:
                            # Download file
                            file_response = await client.get(
                                f"{self._primary_url}/internal/file/{relative_path}"
                            )
                            
                            if file_response.status_code == 200:
                                # Create directory if needed
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                
                                # Save file
                                with open(local_path, "wb") as f:
                                    f.write(file_response.content)
                                
                                downloaded += 1
                                logger.info(f"[SyncService] Downloaded: {relative_path}")
                            else:
                                errors += 1
                                logger.warning(f"[SyncService] Error downloading {relative_path}: {file_response.status_code}")
                                
                        except Exception as e:
                            errors += 1
                            logger.error(f"[SyncService] Error downloading {relative_path}: {e}")
            
            if downloaded > 0:
                logger.info(f"[SyncService] File sync completed: {downloaded} downloaded, {errors} errors")
            
        except httpx.TimeoutException:
            logger.error("[SyncService] Timeout syncing files")
            errors += 1
        except Exception as e:
            logger.error(f"[SyncService] Error syncing files: {e}")
            errors += 1
        
        return {"downloaded": downloaded, "errors": errors}
    
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
